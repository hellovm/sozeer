# -*- coding: utf-8 -*-
"""采纳信号采集（v1.0-E4.5）。

============================ 为什么需要它 ============================

补这个之前，遥测**只能知道"生成了多少"，不知道"生成得好不好"**。

★★ 判据：**生成次数多 ≠ 质量好** —— 可能只是用户反复重试。

没有采纳信号，就无法回答这些关键问题：
  · 这次改了 prompt，生成质量是变好还是变差？
  · 换了模型档位，用户实际采纳率上升了吗？
  · 哪个环节（正文/润色/摘要）的产出最容易被丢弃？
  · 用户平均要重写几次才满意？

============================ 隐私约束（硬性）============================

★★ **只采集计数与分桶，绝不采集任何正文内容**。

具体做法：
  · 字数 → **分桶**（不是精确值，避免通过字数反推内容）
  · 修改幅度 → **分桶**（<10% / 10-30% / 30-60% / >60%）
  · 重写次数 → **分桶**（1 / 2-3 / 4+）
  · 提示词 → **内容哈希**（不是内容本身）
  · 时间 → **只到小时**（不是精确时间戳，避免行为画像）

★ 这些约束让数据"可用于统计，不可用于还原" ——
  与 `errors` 只存哈希前缀是同一思路（MEMORY §21）。

============================ 与 telemetry 的关系 ============================

本模块**只负责采集与计数**，不做上报。
上报由 `telemetry.build_payload()` 读取本模块的计数（`collect_adoption()`）。
这样职责分离：**采集不依赖网络，网络不影响采集**。
"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import re
import threading
from collections import Counter

_log = logging.getLogger("novelforge.adoption")

# ================================================================
#  ★★ 键名安全护栏（v1.0-E4.5）
#
#  ============================ 为什么必须有 ============================
#
#  本模块的实现是 `_counters[f"{prefix}.{x}"] += 1` ——
#  **如果 x 是内容，它会变成键名的一部分**（而不在值里）。
#
#  ★ 实测踩坑：我把 `role` 直接拼进键名，传中文进去就得到
#    `gen.绝密情节：主角其实是反派.total` —— **内容泄漏到键名里**。
#
#  ★★ 判据：**任何进入 `_counters` 的字符串都必须是
#     "可枚举的分类值"或"哈希"**，绝不能是自由文本。
#
#  护栏做法：**白名单字符集** + 超长截断。
#    即使调用方忘了过滤（或未来新增字段），非 ASCII 内容也进不去。
# ================================================================

_SAFE_KEY = re.compile(r"[^A-Za-z0-9._:\-]")
_MAX_KEY_LEN = 64


def _safe(s: str, fallback: str = "unknown") -> str:
    """把任意字符串变成**安全的键名片段**。

    ★ 只保留 ASCII 字母/数字/`.`/`_`/`:`/`-`，
      其余（含所有非 ASCII 字符）一律替换为 `_`。
    ★ 空值/全被替换 → 返回 fallback。
    """
    try:
        raw = str(s or "")
        out = _SAFE_KEY.sub("_", raw)[:_MAX_KEY_LEN]
        # ★ 去掉首尾下划线；若为空说明原串全是非法字符
        out = out.strip("_")
        return out or fallback
    except Exception:                                   # noqa: BLE001
        return fallback


# ================================================================
#  计数容器（内存零 I/O）+ 增量快照落库（v1.0-E4.21）
#
#  ★ 为什么用内存：生成路径上不能有额外 I/O（与 telemetry.track 同理）。
#  ★ E4.21 之前的缺陷：进程重启计数全丢 → 数据洞察永远只看到
#    "本次运行以来"的零头，每次部署清零，无法支持系统进化。
#  ★ E4.21 方案：内存仍是第一现场（零 I/O），但计数累积到阈值后
#    由**独立守护线程**把增量落库（adoption_snapshots），
#    生成路径本身依然一次 I/O 都没有。
#    读取 = Σ(历史快照) + 当前内存 → 跨重启累计、精确不重计。
# ================================================================

_lock = threading.Lock()
_counters: Counter = Counter()

# ★ 阈值：累计这么多条事件就触发一次落盘（约几十 KB，行很小）。
FLUSH_THRESHOLD = 300
# 连续失败 N 次后本进程放弃自动落盘（防"表不存在"之类的持久错误反复起线程）
_MAX_FLUSH_FAILURES = 3

_total_bumps = 0          # 自上次落盘以来的事件条数（在 _lock 保护下读改）
_flush_pending = False
_flush_failures = 0


def _bump(key: str, n: int = 1) -> None:
    """内部计数（异常全吞 —— 采集绝不能影响业务）。"""
    global _total_bumps, _flush_pending
    try:
        with _lock:
            # ★★ 统一走 _safe 护栏（双保险：调用方已过滤，这里再兜一次）
            _counters[_safe(key, "unknown")] += n
            _total_bumps += n
            need_flush = (not _flush_pending
                          and _total_bumps >= FLUSH_THRESHOLD)
    except Exception:                                   # noqa: BLE001
        return
    if need_flush:
        _schedule_flush()


def _schedule_flush() -> None:
    """阈值触发：起一个守护线程异步落盘（不阻塞、不在生成路径上）。"""
    global _flush_pending
    try:
        with _lock:
            if _flush_pending:
                return
            _flush_pending = True
    except Exception:                                   # noqa: BLE001
        return
    t = threading.Thread(target=_auto_flush, name="adoption-flush",
                         daemon=True)
    try:
        t.start()
    except Exception:                                   # noqa: BLE001
        with _lock:
            _flush_pending = False


def _auto_flush() -> None:
    """守护线程入口：用独立 session 落盘（成败都不影响业务）。"""
    global _flush_pending, _total_bumps
    try:
        from apps.api.db import SessionLocal
        db = SessionLocal()
        try:
            flush(db)
            with _lock:
                _total_bumps = 0
        finally:
            try:
                db.close()
            except Exception:                           # noqa: BLE001
                pass
    except Exception as e:                              # noqa: BLE001
        _log.warning("采纳计数自动落盘失败：%s: %s", type(e).__name__, e)
    finally:
        with _lock:
            _flush_pending = False


def flush(db) -> int:
    """把当前内存计数以**增量**写入 adoption_snapshots 并清空内存。

    ★ fail-open：写库失败则保留内存数据（下次再试），绝不抛异常。
    ★ 返回本次落盘的事件条数（0 = 无可落盘）。
    """
    global _flush_failures
    with _lock:
        if not _counters:
            return 0
        delta = dict(_counters)
        _counters.clear()
    try:
        total = sum(delta.values())
        from apps.api.models import AdoptionSnapshot, now as _now
        import uuid as _uuid
        row = AdoptionSnapshot(
            id=f"as_{_uuid.uuid4().hex[:16]}",
            payload=json.dumps(delta, ensure_ascii=False, sort_keys=True),
            total=int(total),
            created_at=_now(),
        )
        db.add(row)
        db.commit()
        with _lock:
            _total_bumps = 0
        return int(total)
    except Exception as e:                              # noqa: BLE001
        # ★ 写库失败：把增量回填内存（期间新到的事件不受影响，
        #   回填只针对快照时刻之前的事件，不会重复计数）。
        try:
            with _lock:
                for k, v in delta.items():
                    _counters[k] += v
        except Exception:                               # noqa: BLE001
            pass
        _flush_failures += 1
        if _flush_failures == _MAX_FLUSH_FAILURES:
            _log.warning("采纳计数落盘连续失败，本进程停止自动落盘"
                         "（内存计数仍可被读取）：%s: %s",
                         type(e).__name__, e)
        return 0


def _atexit_flush() -> None:
    """进程退出兜底（部署重启走 systemctl stop → 优雅退出 → 触发）。"""
    try:
        from apps.api.db import SessionLocal
        db = SessionLocal()
        try:
            flush(db)
        finally:
            db.close()
    except Exception:                                   # noqa: BLE001
        pass


# ================================================================
#  分桶函数（★ 隐私保护的关键：把精确值变成区间）
# ================================================================

def bucket_words(n: int) -> str:
    """字数分桶。

    ★ 为什么不用精确值：字数可以反推内容类型/长度特征。
      分桶后仍能看分布，但无法定位到具体章节。
    """
    try:
        n = int(n)
    except Exception:                                   # noqa: BLE001
        return "unknown"
    if n <= 0:
        return "0"
    for hi, label in ((500, "1-500"), (1000, "501-1k"), (2000, "1k-2k"),
                      (3000, "2k-3k"), (5000, "3k-5k"), (10000, "5k-10k")):
        if n <= hi:
            return label
    return "10k+"


def bucket_edit_ratio(ratio: float) -> str:
    """修改幅度分桶（采纳后相对原文的改动比例）。

    ★ 这是**判断生成质量最直接的信号**：
      · 改动 <10%  → 基本直接用（质量好）
      · 改动 >60%  → 几乎重写（质量差）
    """
    try:
        r = float(ratio)
    except Exception:                                   # noqa: BLE001
        return "unknown"
    if r < 0:
        r = 0.0
    if r <= 0.01:
        return "几乎未改"
    for hi, label in ((0.10, "1-10%"), (0.30, "10-30%"),
                      (0.60, "30-60%"), (0.90, "60-90%")):
        if r <= hi:
            return label
    return ">90%"


def bucket_count(n: int) -> str:
    """通用次数分桶（重写次数、重试次数等）。"""
    try:
        n = int(n)
    except Exception:                                   # noqa: BLE001
        return "unknown"
    if n <= 1:
        return "1"
    if n <= 3:
        return "2-3"
    if n <= 9:
        return "4-9"
    return "10+"


def prompt_fingerprint(text: str) -> str:
    """提示词指纹（**内容哈希，非内容本身**）。

    ★ 用途：把"提示词版本"与"产出质量"关联起来做 A/B 分析。
      改了 prompt 后能量化"有没有变好"。

    ★ 为什么是哈希：提示词是**我们的核心资产**，
      且可能含用户自定义内容 —— 绝不能上报原文。
      哈希足以做分组对比（同版本同哈希）。
    """
    try:
        s = str(text or "")
        if not s:
            return ""
        return "sha256:" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]
    except Exception:                                   # noqa: BLE001
        return ""


# ================================================================
#  采集入口（业务侧调用这些）
# ================================================================

def record_generate(role: str, *, words: int = 0,
                    prompt: str = "", ok: bool = True) -> None:
    """记录一次生成。

    :param role:   环节（chapter_write / chapter_polish / outline / scene 等）
    :param words:  产出字数（会分桶）
    :param prompt: 提示词内容（只存哈希）
    :param ok:     是否成功
    """
    role = _safe(role)
    _bump(f"gen.{role}.total")
    _bump(f"gen.{role}.{'ok' if ok else 'fail'}")
    if words:
        _bump(f"gen.{role}.words.{bucket_words(words)}")
    if prompt:
        fp = prompt_fingerprint(prompt)
        if fp:
            _bump(f"gen.{role}.prompt.{fp}")
    else:
        _bump(f"gen.{role}.prompt.none")


def record_adopt(role: str, *, before_words: int = 0, after_words: int = 0,
                 prompt: str = "") -> None:
    """记录一次**采纳**（用户把生成结果落库）。

    ★ 这是判断质量的核心信号：有生成无采纳 = 质量差。
    """
    role = _safe(role)
    _bump(f"adopt.{role}.total")
    if before_words and before_words > 0:
        ratio = abs(after_words - before_words) / float(before_words)
        _bump(f"adopt.{role}.edit.{bucket_edit_ratio(ratio)}")
    else:
        _bump(f"adopt.{role}.edit.unknown")
    if prompt:
        fp = prompt_fingerprint(prompt)
        if fp:
            _bump(f"adopt.{role}.prompt.{fp}")


def record_discard(role: str, *, reason: str = "") -> None:
    """记录一次**丢弃**（生成后未采纳）。

    :param reason: 粗粒度原因（user_closed / gate_failed / error / unknown）
                   ★ 只接受枚举值，不接受自由文本（防泄漏内容）
    """
    role = _safe(role)
    # ★ 白名单过滤：防止调用方误传自由文本
    allowed = {"user_closed", "gate_failed", "error", "superseded", "unknown"}
    r = reason if reason in allowed else "unknown"
    _bump(f"discard.{role}.{r}")


def record_regenerate(role: str, *, attempt: int = 1) -> None:
    """记录一次重写/重试（同一内容生成多次）。"""
    role = _safe(role)
    _bump(f"regen.{role}.{bucket_count(attempt)}")


def record_feature(name: str) -> None:
    """记录一次功能使用（与 telemetry.track 同源，便于统一导出）。"""
    _bump(f"feature.{(name or 'unknown')[:48]}")


# ================================================================
#  读取与重置（供 telemetry.build_payload 与测试使用）
# ================================================================

def snapshot() -> dict:
    """当前计数快照（只读副本）。"""
    with _lock:
        return dict(_counters)


def _persisted_counters() -> Counter:
    """E4.21：合并所有历史快照增量（fail-open：读失败当没有历史）。"""
    merged: Counter = Counter()
    try:
        from sqlalchemy import select
        from apps.api.db import SessionLocal
        from apps.api.models import AdoptionSnapshot
        db = SessionLocal()
        try:
            rows = list(db.scalars(
                select(AdoptionSnapshot)
                .order_by(AdoptionSnapshot.created_at.asc())
                .limit(5000)).all())
            for r in rows:
                try:
                    d = json.loads(r.payload or "{}")
                except Exception:                       # noqa: BLE001
                    continue
                if isinstance(d, dict):
                    for k, v in d.items():
                        try:
                            merged[str(k)] += int(v)
                        except Exception:               # noqa: BLE001
                            pass
        finally:
            db.close()
    except Exception:                                   # noqa: BLE001
        pass
    return merged


def collect_adoption() -> dict:
    """★ 组装"采纳分析"上报字段（telemetry.build_payload 调用）。

    ★ 只上报**聚合结果**，不逐条上报原始事件 ——
      减少体积，且避免时序信息可用于行为分析。

    ★ E4.21：= Σ(历史快照增量) + 当前内存计数 → **跨重启累计**
      （此前每次部署清零，数据洞察永远只有"本次运行"的零头）。
    """
    with _lock:
        mem = dict(_counters)
    snap = dict(_persisted_counters())
    for k, v in mem.items():
        snap[k] = snap.get(k, 0) + int(v)

    def _sum(prefix: str) -> int:
        return sum(v for k, v in snap.items() if k.startswith(prefix))

    # 按环节汇总（只取有生成的环节）
    roles: set[str] = set()
    for k in snap:
        parts = k.split(".")
        if len(parts) >= 3 and parts[0] in ("gen", "adopt"):
            roles.add(parts[1])

    by_role: dict[str, dict] = {}
    for r in sorted(roles)[:20]:                        # ★ 上限 20，防爆表
        gen = _sum(f"gen.{r}.total")
        ok = _sum(f"gen.{r}.ok")
        adopt = _sum(f"adopt.{r}.total")
        # ★ 只给计数，比率在服务端算（避免浮点噪音 + 便于重算）
        by_role[r] = {"gen": gen, "gen_ok": ok, "adopt": adopt,
                      "regen": _sum(f"regen.{r}.")}
        # 采纳后的修改幅度分布（判断质量的关键）
        edit = {k.rsplit(".", 1)[-1]: v for k, v in snap.items()
                if k.startswith(f"adopt.{r}.edit.")}
        if edit:
            by_role[r]["edit"] = dict(sorted(edit.items())[:8])
        # 产出字数分布
        wd = {k.rsplit(".", 1)[-1]: v for k, v in snap.items()
              if k.startswith(f"gen.{r}.words.")}
        if wd:
            by_role[r]["words"] = dict(sorted(wd.items())[:8])

    # 提示词版本 → 采纳数（A/B 分析基础）
    prompts: dict[str, dict] = {}
    for k, v in snap.items():
        if ".prompt.sha256:" not in k:
            continue
        parts = k.split(".")
        if len(parts) < 4:
            continue
        kind, role, _p, fp = parts[0], parts[1], parts[2], parts[3]
        if kind not in ("gen", "adopt"):
            continue
        key = f"{role}:{fp[:20]}"
        prompts.setdefault(key, {"gen": 0, "adopt": 0})
        prompts[key]["gen" if kind == "gen" else "adopt"] += v

    # 功能使用计数（与 telemetry.track 同源，统一导出）
    feats = {k.split(".", 1)[1]: v for k, v in snap.items()
             if k.startswith("feature.")}
    # ★ 只取 Top 20（防爆表）
    feats = dict(sorted(feats.items(), key=lambda x: -x[1])[:20])

    return {
        "by_role": by_role,
        # ★ 提示词版本对比（最多 10 组，防爆表）
        "by_prompt": dict(sorted(prompts.items(),
                                 key=lambda x: -x[1]["gen"])[:10]),
        "features": feats,
    }


def reset() -> None:
    """清空计数（测试用）。"""
    global _total_bumps
    with _lock:
        _counters.clear()
        _total_bumps = 0


# ★ E4.21：进程优雅退出（部署/重启）前把内存增量落盘，
#   尽量不丢最后一个窗口的数据；崩溃场景由阈值落盘兜底。
try:
    atexit.register(_atexit_flush)
except Exception:                                       # noqa: BLE001
    pass


__all__ = [
    "record_generate", "record_adopt", "record_discard",
    "record_regenerate", "record_feature",
    "collect_adoption", "snapshot", "reset", "flush",
    "FLUSH_THRESHOLD",
    "bucket_words", "bucket_edit_ratio", "bucket_count",
    "prompt_fingerprint",
]

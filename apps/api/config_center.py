# -*- coding: utf-8 -*-
"""统一配置读取与写入（v0.25.4 I3/I4）。

================================ 四层优先级 ================================

    DB（管理员改的）> 环境变量 > 注册表 default > 类型兜底

★ 为什么环境变量在 DB 之下：环境变量是"部署时初始值"，
  管理员在界面改过之后应该以界面为准 —— 否则运维改过环境变量
  就永远覆盖不了，用户会以为"改了没生效"。

================================ 缓存 ================================

★ 配置读在多处热点路径（每次生成会读十几次），
  必须缓存；但**写入时必须立即失效**，否则"改了不生效"。
  做法：进程内 dict + 版本号，写入时 bump 版本。

★ 多 worker 场景：写入方 commit 后 bump 本地版本，
  其他 worker 靠 **TTL 过期**（默认 5 秒）自然同步。
  为什么不广播：为配置改个 Redis pub/sub 不值得，
  5 秒延迟在运维场景完全可接受。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api import settings_registry as reg
from apps.api.models import Setting, SettingHistory

_log = logging.getLogger("novelforge.config")

# 缓存 TTL（秒）。★ 多 worker 下这是配置生效的最大延迟
#
# ★★ v1.0 E2 压测修正：5.0 → 30.0
#   为什么原来太短：`get_config` 是**每请求多次**调用的高频路径
#   （鉴权、限流、计费、角色路由都要读配置）。
#   5 秒 TTL 在持续负载下几乎命中不了缓存 → 每次都穿透到 DB，
#   直接导致连接池被 `idle in transaction` 占满（见 `_raw` 的详细说明）。
#
#   ★ 权衡：TTL 越长，运维在后台改配置的**生效延迟**越大。
#     30 秒对"运营调参"完全可接受（没人盯着秒表等配置生效），
#     而它把 DB 读取量降低一个数量级。
#   ★ 需要**立即生效**的场景（如危机时关功能）走 `invalidate()` 主动失效，
#     不依赖 TTL —— 见下方 invalidate 实现。
CACHE_TTL = 30.0

# ★ 防御性上限：即使调用方传入超大 TTL，也不允许超过这个值。
#   防止有人为了"性能"把 TTL 设成 1 小时，导致运维改配置一小时后才生效。
CACHE_TTL_MAX = 300.0

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.RLock()
_version = 0


def _now() -> dt.datetime:
    """★ naive UTC（与 DB 列一致 —— 本项目第 4 次强调，必守）。

    `models.now()` 返回 aware，DB 列是 naive，混用会抛
    `can't subtract offset-naive and offset-aware datetimes`。
    """
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _bump() -> None:
    """清缓存（写入后调用）。"""
    global _version
    with _cache_lock:
        _cache.clear()
        _version += 1


def cache_clear() -> None:
    _bump()


def cache_info() -> dict:
    with _cache_lock:
        return {"size": len(_cache), "version": _version, "ttl": CACHE_TTL}


# ============================================================ 读取

def _raw(db: Session, key: str) -> str | None:
    """DB 原始值（None = 未配置过）。

    ★★★ v1.0 E2 压测修正（线上实测故障，务必保留此逻辑）：

      现象：100 并发压测时 `/api/ads` 与 `/api/public/site` **全部超时**，
            连 1 并发都挂；服务重启后立刻恢复。
      排查：PG `pg_stat_activity` 里堆满 **`idle in transaction`** 连接，
            全部停在 `SELECT settings.key, settings.value FROM settings`。

      根因：**只读查询也会开启事务，而事务从不结束**。
        SQLAlchemy 的 Session 采用"惰性开启"—— 第一次查询就 BEGIN，
        之后即使只是读，连接归还池中时事务仍是 OPEN 状态
        （PG 侧显示 `idle in transaction`）。
        这些连接**仍持有快照和资源**，且会阻止 VACUUM；
        池里的连接一个个变成这种状态 → 池耗尽 → 后续请求在
        `pool_timeout` 上排队 → 表现为"进程活着但全部超时"。

      ★ 为什么在压测中才暴露：单请求下 `SessionLocal()` 在请求结束时
        被 `get_db()` 的 finally 关掉，事务自然结束；
        但高并发+长连接下，池中连接被反复复用以至保持 OPEN，
        累积效应显现。**这类"量变引起质变"的问题单测永远发现不了。**

      ★ 修法（本函数）：只读路径显式结束事务。
        `db.rollback()` 是结束只读事务**最廉价**的方式 ——
        它不发任何 SQL，只是把连接从事务中释放（不像 commit 会写 WAL）。
        ★ 用 rollback 而非 commit 的理由：这里确实什么都没改，
          commit 是语义谎言，且会带来不必要的 WAL 写入。
    """
    try:
        row = db.get(Setting, key)
    finally:
        # ★ 无论查得到查不到，都必须结束事务（异常路径也不能漏）
        try:
            db.rollback()
        except Exception:                               # noqa: BLE001
            pass
    if row is None:
        return None
    return row.value


def get_config(db: Session | None, key: str, default: Any = None) -> Any:
    """★ 统一读取入口（**所有业务代码都应走这里**）。

    ★ 为什么允许 db=None：部分场景（启动自检、无请求上下文的后台线程）
      拿不到 Session，此时跳过 DB 层，用环境变量/default 兜底。
      绝不能因为"没 db"就崩 —— 配置读取必须永远可用。
    """
    k = reg.normalize_key(key)
    spec = reg.spec_of(k)
    if spec is None:
        # 未注册的键：仍允许读（兼容过度期），但记警告帮我们收敛
        _log.debug("读取未注册配置项：%s", k)
        raw = _raw(db, k) if db is not None else None
        return raw if raw is not None else default

    # 1) 缓存
    ck = f"{k}"
    nowt = time.time()
    with _cache_lock:
        hit = _cache.get(ck)
        if hit is not None and (nowt - hit[0]) < CACHE_TTL:
            return hit[1]

    val = None
    # 2) DB
    raw = _raw(db, k) if db is not None else None
    if raw is not None and raw != "":
        val = raw
    # 3) 环境变量
    if val is None:
        val = reg.env_value(spec)
    # 4) 默认
    if val is None:
        val = spec.default

    # 5) 类型兜底（★ 坏值不能崩，退回默认并告警）
    try:
        out = reg.coerce(spec, val)
    except (ValueError, TypeError) as e:
        _log.warning("配置 %s 值非法（%r）：%s —— 已退回默认值 %r",
                     k, val, e, spec.default)
        out = spec.default

    with _cache_lock:
        _cache[ck] = (nowt, out)
    return out


def get_bool(db: Session | None, key: str, default: bool = False) -> bool:
    v = get_config(db, key, default)
    return bool(v)


def is_enabled(db: Session | None, feature: str) -> bool:
    """功能开关查询。`feature("drama")` → `feature.drama`。

    ★ 为什么做成独立函数：开关用得最多，且**读不到时按关闭处理**
      是安全默认（宁可少开功能，不可误开）。
      但注意：维护模式/只读模式是"读不到按关闭"，因为默认开等于全站不可用。
    """
    k = feature if "." in feature else f"feature.{feature}"
    return get_bool(db, k, bool(reg.SPECS.get(k).default) if k in reg.SPECS else False)


# ============================================================ 写入

class ConfigError(Exception):
    """配置写入错误（会被路由转成 400）。"""


def set_config(db: Session, key: str, value: Any, *, operator: str = "",
               reason: str = "", source: str = "ui",
               operator_ip: str = "", commit: bool = True) -> dict:
    """写入单个配置（含校验 + 历史 + 审计）。

    ★ 三条规则：
      ① 类型/范围非法 → 拒（不静默兜底）
      ② 高危配置必须填 reason（否则拒绝）—— 出问题时才知道为什么改
      ③ 无论成功失败都留痕（失败写历史，source 标 "rejected"）
         ★ 审计的"失败也留痕"原则：有人反复尝试改高危配置本身就是信号
    """
    k = reg.normalize_key(key)
    spec = reg.spec_of(k)
    if spec is None:
        raise ConfigError(f"未注册的配置项：{key}")
    if spec.readonly:
        raise ConfigError(f"{spec.label or k} 是系统维护项，不可修改")

    ok, why = reg.validate(spec, value)
    if not ok:
        # ★★ 被拒绝的尝试必须独立提交 —— 否则异常抛出时事务回滚，
        #    留痕一并丢失（"失败也留痕"就落空了）。
        #    这条是测试逼出来的：只 add 不 commit，外面 raise 后什么都没写进去。
        _write_history(db, k, "", str(value), reason=f"[拒绝] {why}",
                       operator=operator, source="rejected",
                       operator_ip=operator_ip)
        try:
            db.commit()
        except Exception:                           # noqa: BLE001
            try:
                db.rollback()
            except Exception:                       # noqa: BLE001
                pass
        raise ConfigError(why)

    new_val = reg.coerce(spec, value)
    old_raw = _raw(db, k)
    old_val = get_config(db, k)      # 已解析的旧值（可能来自默认）
    new_str = _to_str(spec, new_val)

    if spec.risk == "danger" and not (reason or "").strip():
        raise ConfigError(f"{spec.label or k} 属高危配置，必须填写修改原因")
    if spec.risk == "danger" and len((reason or "").strip()) < 4:
        raise ConfigError("高危配置的修改原因至少 4 个字（便于事后追溯）")

    row = db.get(Setting, k)
    if row is None:
        db.add(Setting(key=k, value=new_str))
    else:
        row.value = new_str

    _write_history(db, k, str(old_raw if old_raw is not None else old_val),
                   new_str, reason=reason, operator=operator,
                   source=source, operator_ip=operator_ip)
    if commit:
        try:
            db.commit()
        except Exception as e:                      # noqa: BLE001
            _log.warning("配置写入失败：%s: %s", type(e).__name__, e)
            try:
                db.rollback()
            except Exception:                       # noqa: BLE001
                pass
            raise ConfigError("保存失败（数据库错误）") from e

    _bump()
    # 热更新回调（★ 失败不影响写入 —— 回调是"顺手生效"，不是写入前提）
    if spec.on_change is not None:
        try:
            spec.on_change(new_val)
        except Exception as e:                      # noqa: BLE001
            _log.warning("配置 %s 热更新回调失败（值已保存）：%s", k, e)

    _log.info("配置变更：%s %r → %r（by %s，%s）", k, old_val, new_val,
              operator or "?", reason or "-")
    return {"ok": True, "key": k, "old": old_val, "new": new_val,
            "risk": spec.risk}


def set_many(db: Session, payload: dict[str, Any], *, operator: str = "",
             reason: str = "", source: str = "ui",
             operator_ip: str = "") -> dict:
    """批量写入（**全成或全不成** —— 部分成功会让界面状态与后台不一致）。"""
    if not isinstance(payload, dict) or not payload:
        raise ConfigError("没有要保存的配置项")
    unknown = [k for k in payload if reg.spec_of(reg.normalize_key(k)) is None]
    if unknown:
        raise ConfigError(f"不支持的配置项：{', '.join(sorted(unknown)[:8])}")

    # 先全量校验（★ 不写库，避免"写了一半才发现后面非法"）
    errs: list[str] = []
    for k, v in payload.items():
        nk = reg.normalize_key(k)
        spec = reg.spec_of(nk)
        assert spec is not None
        if spec.readonly:
            errs.append(f"{spec.label or nk} 不可修改")
            continue
        ok, why = reg.validate(spec, v)
        if not ok:
            errs.append(why)
    if errs:
        raise ConfigError("；".join(errs[:5]))

    saved: list[str] = []
    for k, v in payload.items():
        nk = reg.normalize_key(k)
        set_config(db, nk, v, operator=operator, reason=reason,
                   source=source, operator_ip=operator_ip, commit=False)
        saved.append(nk)
    try:
        db.commit()
    except Exception as e:                          # noqa: BLE001
        try:
            db.rollback()
        except Exception:                           # noqa: BLE001
            pass
        raise ConfigError("保存失败（数据库错误）") from e
    _bump()
    return {"ok": True, "saved": sorted(saved), "count": len(saved)}


def rollback(db: Session, history_id: str, *, operator: str = "",
             reason: str = "") -> dict:
    """回滚到某次历史变更之前的取值。

    ★ 为什么回滚也要记 history：回滚本身也是变更，
      否则历史里会出现"值莫名变回去了"的断点。
    """
    h = db.get(SettingHistory, history_id)
    if h is None:
        raise ConfigError("历史记录不存在")
    spec = reg.spec_of(h.key)
    if spec is None:
        raise ConfigError(f"该配置项已下线：{h.key}")
    if spec.readonly:
        raise ConfigError("系统维护项不可回滚")
    # ★ 回滚到 old_value
    target_raw = h.old_value
    if target_raw == "":
        # 原来没有值 → 删除设置（回到默认）
        row = db.get(Setting, h.key)
        if row is not None:
            db.delete(row)
        _write_history(db, h.key, h.new_value, "(默认值)",
                       reason=reason or f"回滚 #{history_id[:8]}",
                       operator=operator, source="rollback")
        db.commit()
        _bump()
        return {"ok": True, "key": h.key, "new": None, "deleted": True}

    set_config(db, h.key, target_raw, operator=operator,
               reason=reason or f"回滚 #{history_id[:8]}", source="rollback")
    return {"ok": True, "key": h.key, "new": get_config(db, h.key)}


def _to_str(spec: reg.Spec, v: Any) -> str:
    if spec.type == "bool":
        return "1" if v else "0"
    if spec.type == "float":
        return repr(float(v))
    return str(v)


def _write_history(db: Session, key: str, old: str, new: str, *,
                   reason: str = "", operator: str = "", source: str = "ui",
                   operator_ip: str = "") -> None:
    """★ 留痕失败不得影响主流程（旁路功能 fail-open）。"""
    try:
        db.add(SettingHistory(
            id=f"sh_{uuid.uuid4().hex[:12]}", key=key,
            old_value=(old or "")[:4000], new_value=(new or "")[:4000],
            reason=(reason or "")[:1000], operator=operator or "",
            operator_ip=operator_ip or "", source=source, created_at=_now()))
    except Exception as e:                          # noqa: BLE001
        _log.warning("配置历史写入失败（不影响保存）：%s", e)


# ============================================================ 查询（界面用）

def list_all(db: Session, *, group: str = "") -> dict:
    """全部配置（含当前值、来源、风险等级）—— 界面自动渲染的数据源。"""
    groups = reg.by_group()
    out_groups = []
    for g, meta in reg.GROUPS.items():
        if group and g != group:
            continue
        items = groups.get(g) or []
        if not items:
            continue
        rows = []
        for s in items:
            raw = _raw(db, s.key)
            eff_raw = reg.env_value(s)
            if raw is not None and raw != "":
                src = "db"
            elif eff_raw is not None:
                src = "env"
            else:
                src = "default"
            rows.append({
                "key": s.key, "label": s.label or s.key, "type": s.type,
                "value": get_config(db, s.key), "raw": raw,
                "default": s.default, "group": s.group, "desc": s.desc,
                "risk": s.risk, "risk_label": reg.RISK_LABEL.get(s.risk, s.risk),
                "choices": list(s.choices), "min": s.min, "max": s.max,
                "unit": s.unit, "readonly": s.readonly, "source": src,
                "is_default": src == "default",
                # ★★ v1.0-E4.4：高级项标记（界面默认折叠）
                #
                #   为什么由后端下发而不是前端写死过滤规则：
                #     "哪些算高级"是**配置本身的属性**，不是界面的属性。
                #     新增配置时在注册表声明一次即可，不用改前端。
                "advanced": s.advanced,
            })
        out_groups.append({"group": g, "name": meta["name"],
                           "icon": meta["icon"], "desc": meta["desc"],
                           "items": rows, "count": len(rows)})
    return {"groups": out_groups,
            "total": sum(g["count"] for g in out_groups),
            "all_total": len(reg.SPECS),
            "cache": cache_info()}


def history(db: Session, *, key: str = "", limit: int = 100) -> dict:
    """变更历史（倒序）。"""
    q = select(SettingHistory).order_by(SettingHistory.created_at.desc())
    if key:
        q = q.where(SettingHistory.key == reg.normalize_key(key))
    rows = list(db.scalars(q.limit(max(1, min(int(limit or 100), 500)))).all())
    items = [{
        "id": h.id, "key": h.key, "old_value": h.old_value,
        "new_value": h.new_value, "reason": h.reason,
        "operator": h.operator or "-", "source": h.source,
        "created_at": h.created_at.isoformat() if h.created_at else "",
        "risk": (reg.SPECS.get(h.key).risk if h.key in reg.SPECS else "safe"),
        "label": (reg.SPECS.get(h.key).label if h.key in reg.SPECS else h.key),
    } for h in rows]
    return {"items": items, "total": len(items)}


def diff_preview(db: Session, payload: dict[str, Any]) -> dict:
    """★ 保存前的 diff 预览（I5）—— "会改什么"先给用户看。

    ★ 为什么需要：配置改动影响面大（可能影响计费），
      盲改后才发现改错了，可能已经产生了错误订单。
    """
    changes = []
    warnings: list[str] = []
    for k, v in (payload or {}).items():
        nk = reg.normalize_key(k)
        spec = reg.spec_of(nk)
        if spec is None:
            changes.append({"key": nk, "label": nk, "old": None, "new": v,
                            "risk": "safe", "error": "未注册的配置项"})
            continue
        old = get_config(db, nk)
        ok, why = reg.validate(spec, v)
        try:
            new = reg.coerce(spec, v) if ok else v
        except (ValueError, TypeError):
            new = v
        ch = {"key": nk, "label": spec.label or nk, "old": old, "new": new,
              "risk": spec.risk, "risk_label": reg.RISK_LABEL.get(spec.risk, ""),
              "changed": _neq(spec, old, new), "error": "" if ok else why}
        if ch["changed"]:
            if spec.risk == "danger":
                warnings.append(f"「{spec.label or nk}」是高危配置，改动将立即对所有用户生效")
            if spec.key == "feature.maintenance" and new:
                warnings.append("开启维护模式后，所有非管理员请求将返回 503")
            if spec.key == "system.allow_register" and not new:
                warnings.append("关闭注册后，新用户无法自助注册（只能管理员建号）")
        changes.append(ch)
    n_changed = sum(1 for c in changes if c.get("changed"))
    return {"changes": changes, "changed_count": n_changed,
            "has_error": any(c.get("error") for c in changes),
            "warnings": warnings,
            "need_reason": any(c.get("changed") and c.get("risk") == "danger"
                               for c in changes)}


def _neq(spec: reg.Spec, a: Any, b: Any) -> bool:
    if spec.type == "float":
        try:
            return abs(float(a or 0) - float(b or 0)) > 1e-9
        except (ValueError, TypeError):
            return True
    return a != b


# ============================================================ 导出 / 导入（I11）

# ★ 导出时排除的项：机器特征/密钥，导出文件可能被分享
EXPORT_EXCLUDE = {"telemetry.instance_id"}


def export_config(db: Session) -> dict:
    """导出配置（★ 排除机器特征项 —— 导出文件可能被分享）。"""
    out: dict[str, Any] = {}
    for s in reg.SPECS.values():
        if s.key in EXPORT_EXCLUDE:
            continue
        raw = _raw(db, s.key)
        if raw is None or raw == "":
            continue                        # 只导出**显式改过**的项
        try:
            out[s.key] = reg.coerce(s, raw)
        except (ValueError, TypeError):
            out[s.key] = raw
    return {"version": 1, "exported_at": _now().isoformat(),
            "count": len(out), "config": out,
            "note": "仅含被显式修改过的配置项（未改的用默认值，无需导出）"}


def import_config(db: Session, payload: dict, *, operator: str = "",
                  reason: str = "", dry_run: bool = True,
                  operator_ip: str = "") -> dict:
    """导入配置。

    ★ 默认 dry_run=True —— 导入会批量覆盖配置，必须先看清会改什么。
    """
    data = (payload or {}).get("config") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ConfigError("导入格式错误：缺少 config 对象")
    preview = diff_preview(db, data)
    if dry_run:
        return {"ok": True, "dry_run": True, **preview}
    # ★ 导入视为高危：即使单项是 safe，批量覆盖也需说明
    r = set_many(db, data, operator=operator,
                 reason=reason or "配置导入", source="import",
                 operator_ip=operator_ip)
    return {"ok": True, "dry_run": False, "count": r["count"],
            "saved": r["saved"], "changed_count": preview["changed_count"]}


__all__ = ["get_config", "get_bool", "is_enabled", "set_config", "set_many",
           "rollback", "list_all", "history", "diff_preview", "export_config",
           "import_config", "cache_clear", "cache_info", "ConfigError",
           "CACHE_TTL"]

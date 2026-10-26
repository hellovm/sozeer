"""前端双轨灰度（v0.25.9 G0-7）。

★ 背景（计划书 §8.0 铁律 R-A / R-D）：
   新版前端（Vue3，`/app2/`）与旧版（`/`，单体 app.js）**并行服务**，
   按用户灰度切流；出问题**一关开关即回旧版**（5 秒内，无需发版）。

★ 为什么用"用户哈希取模"而不是随机：
   随机会导致**同一用户一会儿新一会儿旧**（刷新即变），
   表现为"页面样式跳来跳去"、"填的表单丢了" —— 体验极差且难排查。
   按 uid 稳定哈希 → 同一用户**始终**落在同一侧。

★ 灰度档位设计：
   · off     全部旧版（默认，最安全）
   · on      全部新版
   · N%      按 uid 哈希取模，前 N% 走新版（稳定分桶）
   · allow   仅白名单用户走新版（内测用）

★ 配置来源优先级：DB 配置中心 > 环境变量 > 代码默认（见 MEMORY.md §53）
"""
from __future__ import annotations

import hashlib
import logging
import os

log = logging.getLogger("novelforge")

ENV_KEY = "NF_FRONTEND_V2"

# 配置中心注册表键名（I 组配置中心）
CONFIG_KEY = "feature.frontend_v2"
CONFIG_KEY_ALLOW = "feature.frontend_v2_allowlist"


def _hash_bucket(uid: str) -> int:
    """把用户 id 稳定映射到 0-99（用于百分比灰度）。

    ★ 用 md5 而不是内置 hash()：
      Python 的 `hash()` 对 str **带随机盐**（PYTHONHASHSEED），
      进程重启后分桶全变 → 用户会在新旧版之间反复横跳。
    """
    if not uid:
        return 99                      # 匿名/未登录 → 归到"不灰度"侧（最保守）
    h = hashlib.md5(uid.encode("utf-8")).hexdigest()
    return int(h[:8], 16) % 100


def _read_config(db=None) -> str:
    """读配置（DB 优先 → 环境变量 → 默认 off）。

    ★ 读失败一律回退 off（安全侧）—— 配置中心挂了不能让全站切到未验证的新版。
    """
    # 1) 配置中心（DB）
    if db is not None:
        try:
            from apps.api.config_center import get_config
            v = get_config(db, CONFIG_KEY, None)
            if v is not None and str(v).strip():
                return str(v).strip().lower()
        except Exception as e:                      # noqa: BLE001
            log.debug("读取前端灰度配置失败（回退环境变量）：%s", e)

    # 2) 环境变量
    v = os.environ.get(ENV_KEY, "").strip().lower()
    if v:
        return v

    # 3) 默认关闭
    return "off"


def _read_allowlist(db=None) -> set[str]:
    """白名单用户（内测用）。"""
    raw = ""
    if db is not None:
        try:
            from apps.api.config_center import get_config
            v = get_config(db, CONFIG_KEY_ALLOW, None)
            if v:
                raw = str(v)
        except Exception:
            pass
    if not raw:
        raw = os.environ.get(ENV_KEY + "_ALLOW", "")
    return {x.strip() for x in raw.replace("\n", ",").split(",") if x.strip()}


def parse_mode(mode: str) -> tuple[str, int]:
    """解析模式字符串 → (kind, percent)。

    kind: off | on | pct | allow
    """
    m = (mode or "off").strip().lower()
    if m in ("off", "0", "false", "no", "disable", "disabled"):
        return "off", 0
    if m in ("on", "1", "true", "yes", "all", "enable", "enabled"):
        return "on", 100
    if m in ("allow", "whitelist", "internal"):
        return "allow", 0
    # 百分比：'10' / '10%' / '10pct'
    digits = "".join(ch for ch in m if ch.isdigit())
    if digits:
        pct = max(0, min(100, int(digits)))
        if pct == 0:
            return "off", 0
        if pct >= 100:
            return "on", 100
        return "pct", pct
    log.warning("无法识别的灰度模式 %r，按 off 处理", mode)
    return "off", 0


def wants_v2(uid: str = "", *, db=None, mode: str | None = None) -> bool:
    """★ 判断该用户是否应使用新版前端。"""
    raw = mode if mode is not None else _read_config(db)
    kind, pct = parse_mode(raw)

    if kind == "off":
        return False
    if kind == "on":
        return True
    if kind == "allow":
        return bool(uid) and uid in _read_allowlist(db)

    # pct：稳定分桶
    return _hash_bucket(uid) < pct


def status(db=None) -> dict:
    """当前灰度状态（后台可查，也可用于排障）。"""
    raw = _read_config(db)
    kind, pct = parse_mode(raw)
    allow = _read_allowlist(db)
    return {
        "mode": raw,
        "kind": kind,
        "percent": pct,
        "allowlist_count": len(allow),
        "allowlist": sorted(allow)[:50],
        "env_key": ENV_KEY,
        "config_key": CONFIG_KEY,
        "detail": {
            "off": "全部走旧版（默认，最安全）",
            "on": "全部走新版",
            "pct": f"{pct}% 用户走新版（按 uid 稳定哈希分桶）",
            "allow": "仅白名单用户走新版（内测）",
        }.get(kind, ""),
    }


def rollout_buckets(sample_uids: list[str], mode: str) -> dict:
    """★ 验证用：给定一批 uid，算出实际分桶比例（证明灰度是稳定的）。

    用途：确认"配 10% 真的只有 ~10% 走新版"，且**同一 uid 多次调用结果一致**。
    """
    kind, pct = parse_mode(mode)
    hit = [u for u in sample_uids if wants_v2(u, mode=mode)]
    return {
        "mode": mode, "kind": kind, "percent": pct,
        "total": len(sample_uids), "hit": len(hit),
        "actual_pct": round(len(hit) / len(sample_uids) * 100, 2) if sample_uids else 0,
    }

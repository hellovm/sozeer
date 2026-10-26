# -*- coding: utf-8 -*-
"""系统设置读写（v0.18 T1 抽取）。

原先散在 `main.py`（get_setting / _set_setting / is_register_open），
抽出来后「设置」与「路由」解耦——routers/system.py 与 main.py 都能直接用。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from apps.api import config
from apps.api.models import Setting


def get_setting(db: Session, key: str, default: str = "") -> str:
    """读设置（★ v0.25.4 I3：优先走配置中心，未注册的键回退旧逻辑）。

    ★ 为什么要改：配置中心是**单一事实源**（含类型/校验/历史/缓存）。
      但历史上散落的键（如 `book:<id>:meta`）不是"配置项"，
      不该被强行登记进注册表 → 未注册的走原逻辑，零影响。
    """
    try:
        from apps.api import config_center as cc
        from apps.api import settings_registry as reg
        nk = reg.normalize_key(key)
        if reg.spec_of(nk) is not None:
            v = cc.get_config(db, nk)
            return _to_str(v)
    except Exception:                                    # noqa: BLE001
        pass
    # ---------- 兼容路径（未注册的键） ----------
    row = db.get(Setting, key)
    if row is not None and row.value != "":
        return row.value
    if key == "allow_register":
        return "1" if config.ALLOW_REGISTER else "0"
    return default


def _to_str(v) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


def set_setting(db: Session, key: str, value: str) -> None:
    """写设置（upsert）。对话的 active 指针也复用 Setting 表，逻辑统一。

    注意：本函数**不 commit**，由调用方决定事务边界（保持原 main.py 行为）。
    """
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=str(value)))
    else:
        row.value = str(value)


def is_register_open(db: Session) -> bool:
    """注册开关（★ v0.25.4：两个条件都要满足）。

    ★ 为什么是"与"而不是"或"：
      `allow_register` 是总开关，`feature.invite_only` 是更强约束。
      两者任一关闭就不该开放 —— 用"或"会让运营关了一个却发现还能注册。
    """
    try:
        from apps.api import config_center as cc
        if cc.get_bool(db, "feature.invite_only", False):
            return False
        return cc.get_bool(db, "system.allow_register", True)
    except Exception:                                    # noqa: BLE001
        pass
    return get_setting(db, "allow_register", "1") == "1"


# ----------------------------------------------------------------- 设置默认值
# v0.18 T1：从 main.py 搬来。routers/system.py 与 main.py 都引用它，
# 放这里避免「routers 依赖 main」的循环。
SETTING_DEFAULTS: dict[str, str] = {
    # 平台展示
    # ★★ v1.0-E4.16：品牌更名 —— "织字工坊"是旧名（NovelForge 时代），
    #   品牌已定名溯字/SOZEER（sozeer.com）。此默认值兜底的场景是
    #   DB 无存量值的新装实例；已装实例的存量值需线上改库（见部署清单）。
    "site_name": "溯字 SOZEER",
    "site_tagline": "AI 长篇创作与短剧生成工作台",
    # 注册策略：1=开放自助注册，0=关闭（关闭后只能由管理员建号）
    "allow_register": "1",
    # 新注册账号的默认角色
    "default_role": "user",
}


__all__ = ["get_setting", "set_setting", "is_register_open", "SETTING_DEFAULTS"]

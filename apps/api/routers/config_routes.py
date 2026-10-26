# -*- coding: utf-8 -*-
"""配置中心路由（v0.25.4 I4/I5/I6/I11）。

★ 权限：全部 `require_admin`。★ 但**导出/导入额外限 owner**：
  配置导出的文件包含计费与安全参数，泄漏影响面大；
  导入能批量覆盖全站配置，等同"改服务器"。这两项必须是最高权限。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from apps.api import auth, config_center as cc
from apps.api import settings_registry as reg
from apps.api.db import get_db
from apps.api.models import User

_log = logging.getLogger("novelforge.routes.config")
router = APIRouter()


def _ip(req: Request) -> str:
    """★ 优先 CF-Connecting-IP（不可伪造），不用 XFF 首段（客户端可伪造）。"""
    for h in ("CF-Connecting-IP", "cf-connecting-ip"):
        v = req.headers.get(h)
        if v:
            return v.strip()[:64]
    return (req.client.host if req.client else "")[:64]


class SetIn(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class OneIn(BaseModel):
    value: Any = None
    reason: str = ""


class ImportIn(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = True
    reason: str = ""


# ============================================================ 读取

@router.get("/api/admin/config")
def admin_list_config(group: str = "", db: Session = Depends(get_db),
                      admin: User = Depends(auth.require_admin)):
    """全部配置（按组，含当前值/来源/风险）。界面自动渲染的数据源。"""
    return cc.list_all(db, group=group)


@router.get("/api/admin/config/meta")
def admin_config_meta(db: Session = Depends(get_db),
                      admin: User = Depends(auth.require_admin)):
    """注册表元信息（分组 + 风险说明 + 缓存状态）。"""
    return {
        "groups": [{"group": g, "name": m["name"], "icon": m["icon"],
                    "desc": m["desc"]} for g, m in reg.GROUPS.items()],
        "risk_label": reg.RISK_LABEL,
        "total": len(reg.SPECS),
        "cache": cc.cache_info(),
        "env_prefix": reg.ENV_PREFIX,
        "priority": ["DB（界面）", "环境变量", "注册表默认值"],
    }


@router.get("/api/admin/config/history")
def admin_config_history(key: str = "", limit: int = 100,
                         db: Session = Depends(get_db),
                         admin: User = Depends(auth.require_admin)):
    """变更历史（★ 含被拒绝的尝试 —— 反复试改高危项本身是信号）。"""
    return cc.history(db, key=key, limit=limit)


@router.get("/api/admin/config/doc")
def admin_config_doc(db: Session = Depends(get_db),
                     admin: User = Depends(auth.require_admin)):
    """配置文档（Markdown，由注册表自动生成 → 永不过期）。"""
    md = reg.to_markdown()
    return {"ok": True, "markdown": md,
            "lines": md.count("\n") + 1, "total": len(reg.SPECS)}


# ============================================================ 写入

@router.post("/api/admin/config/diff")
def admin_config_diff(body: SetIn, db: Session = Depends(get_db),
                      admin: User = Depends(auth.require_admin)):
    """★ diff 预览（保存前先看"会改什么"）。"""
    return cc.diff_preview(db, body.values)


@router.post("/api/admin/config")
def admin_set_config(body: SetIn, req: Request, db: Session = Depends(get_db),
                     admin: User = Depends(auth.require_admin)):
    """批量保存（★ 全成或全不成；高危项必须填原因）。"""
    if not body.values:
        raise HTTPException(400, "没有要保存的配置项")
    try:
        r = cc.set_many(db, body.values, operator=admin.username,
                        reason=body.reason, source="ui", operator_ip=_ip(req))
    except cc.ConfigError as e:
        raise HTTPException(400, str(e)) from e
    r["operator"] = admin.username
    return r


@router.post("/api/admin/config/one")
def admin_set_one(body: OneIn, key: str, req: Request,
                  db: Session = Depends(get_db),
                  admin: User = Depends(auth.require_admin)):
    """写单个配置（key 走 query，便于前端动态表单逐项保存）。"""
    try:
        r = cc.set_config(db, key, body.value, operator=admin.username,
                          reason=body.reason, source="ui",
                          operator_ip=_ip(req))
    except cc.ConfigError as e:
        raise HTTPException(400, str(e)) from e
    r["operator"] = admin.username
    return r


@router.post("/api/admin/config/rollback")
def admin_config_rollback(history_id: str, body: OneIn, req: Request,
                          db: Session = Depends(get_db),
                          admin: User = Depends(auth.require_admin)):
    """回滚到某次变更之前的值。

    ★ 回滚也算变更（会写历史）—— 否则历史里出现"值莫名变回去"的断点。
    """
    try:
        r = cc.rollback(db, history_id, operator=admin.username,
                        reason=body.reason)
    except cc.ConfigError as e:
        raise HTTPException(400, str(e)) from e
    r["operator"] = admin.username
    return r


# ============================================================ 导出 / 导入（I11）

@router.get("/api/admin/config/export")
def admin_config_export(db: Session = Depends(get_db),
                        admin: User = Depends(auth.require_owner)):
    """★ 导出（owner 限定）—— 文件含计费/安全参数，泄漏影响面大。"""
    return cc.export_config(db)


@router.post("/api/admin/config/import")
def admin_config_import(body: ImportIn, req: Request,
                        db: Session = Depends(get_db),
                        admin: User = Depends(auth.require_owner)):
    """★ 导入（owner 限定，默认 dry_run）。

    导入能批量覆盖全站配置，等同"改服务器"→ 必须最高权限 + 先预览。
    """
    try:
        r = cc.import_config(db, {"config": body.config},
                             operator=admin.username, reason=body.reason,
                             dry_run=body.dry_run, operator_ip=_ip(req))
    except cc.ConfigError as e:
        raise HTTPException(400, str(e)) from e
    r["operator"] = admin.username
    return r


@router.post("/api/admin/config/cache/clear")
def admin_config_cache_clear(db: Session = Depends(get_db),
                             admin: User = Depends(auth.require_admin)):
    """清配置缓存（★ 多 worker 下强制立即生效的手段）。"""
    cc.cache_clear()
    return {"ok": True, "cache": cc.cache_info(),
            "note": "本进程缓存已清；其他 worker 最多 "
                    f"{cc.CACHE_TTL:.0f} 秒后自然同步"}


__all__ = ["router"]

# -*- coding: utf-8 -*-
"""题材包管理路由

由 v0.18 T1 从 main.py 拆出。main.py 通过 include_router 挂载本模块路由，
**同时 re-export 端点函数名** —— 所以 `from apps.api.main import list_genres`
以及 `main.list_genres(...)` 的老写法继续可用（测试大量依赖这一点）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, File, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api import auth, config, keyvault, llm
from apps.api import consistency
from apps.api.db import SessionLocal, get_db
from apps.api.models import (
    Book, Chapter, Character, Foreshadow, ModelProfile, Run, Setting, User,
    StyleProfile, Volume, WorldSetting, jd, jl, now,
    AuditReport, LedgerEntry, TimelineEvent, Conversation, Message, Task,
    DramaProject, DramaEpisode, DramaAsset, DramaScene, GenrePack,
    GenrePackMarket,
)
from apps.api.services.settings import get_setting, set_setting, is_register_open

router = APIRouter()

class GenrePackIn(BaseModel):
    key: str = ""
    label: str = ""
    aliases: list[str] = []
    config: dict = {}
    enabled: int = 1
    sort_order: int = 100

@router.get("/api/genres")
def list_genres(db: Session = Depends(get_db), _: User = Depends(auth.require_admin)):
    """题材清单（含内置 + 自定义 + 已停用，标记 builtin/enabled 供 UI 用）。"""
    from apps.api import genres
    genres.invalidate_cache()
    return {"items": genres.catalog(db, include_disabled=True),
            "builtin_count": len(genres.GENRES)}

@router.get("/api/genres/public")
def list_genres_public(db: Session = Depends(get_db), _: User = Depends(auth.get_current_user)):
    """题材清单（仅启用项，立项下拉用；非管理员也能读）。"""
    from apps.api import genres
    return {"items": genres.catalog(db)}

@router.post("/api/genres")
def create_genre(body: GenrePackIn, db: Session = Depends(get_db),
                 _: User = Depends(auth.require_admin)):
    """新增自定义题材（不改 Python 代码）。"""
    from apps.api import genres
    import re as _re
    key = (body.key or "").strip().lower()
    # 只允许 ASCII 小写字母/数字/下划线。注意 python 的 str.isalnum() 对中文返回 True，
    # 若用它校验，'有中文' 会被放行并污染 key 匹配（v0.15 实测踩坑），故用显式正则。
    if not key or not _re.fullmatch(r"[a-z0-9_]+", key):
        raise HTTPException(400, "key 只能是 a-z、0-9、下划线")
    if key in genres.GENRES:
        raise HTTPException(400, f"「{key}」是内置题材，不能重复创建（可停用或改配置）")
    if db.get(GenrePack, key):
        raise HTTPException(400, f"题材「{key}」已存在")
    if not (body.label or "").strip():
        raise HTTPException(400, "label 不能为空")
    cfg = dict(body.config or {})
    row = GenrePack(
        key=key, label=body.label.strip(),
        aliases=jd(body.aliases or []),
        config_json=jd({k: v for k, v in cfg.items() if k not in ("label", "aliases")}),
        builtin=0, enabled=1 if body.enabled else 0,
        sort_order=int(body.sort_order or 100),
    )
    db.add(row)
    db.commit()
    genres.invalidate_cache()
    return {"ok": True, "key": key}

@router.patch("/api/genres/{key}")
def update_genre(key: str, body: GenrePackIn, db: Session = Depends(get_db),
                 _: User = Depends(auth.require_admin)):
    """更新题材包（内置包可改配置、可停用，但不可删除）。"""
    from apps.api import genres
    key = (key or "").strip().lower()
    row = db.get(GenrePack, key)
    if not row:
        # 内置包还没落库？先补种子再改
        genres.seed_builtin(db)
        row = db.get(GenrePack, key)
    if not row:
        raise HTTPException(404, f"题材「{key}」不存在")
    if body.label:
        row.label = body.label.strip()
    if body.aliases:
        row.aliases = jd(body.aliases)
    if body.config:
        row.config_json = jd({k: v for k, v in body.config.items()
                              if k not in ("label", "aliases")})
    if body.enabled is not None:
        row.enabled = 1 if body.enabled else 0
    if body.sort_order is not None:
        row.sort_order = int(body.sort_order)
    db.commit()
    genres.invalidate_cache()
    return {"ok": True, "key": key}

@router.delete("/api/genres/{key}")
def delete_genre(key: str, db: Session = Depends(get_db),
                 _: User = Depends(auth.require_admin)):
    """删除题材包。★ 内置包禁止删除（计划书 §六 风险 9）。"""
    from apps.api import genres
    key = (key or "").strip().lower()
    if key in genres.GENRES:
        raise HTTPException(400, f"「{key}」是内置题材，不允许删除；如需停用请改用停用开关")
    row = db.get(GenrePack, key)
    if not row:
        raise HTTPException(404, f"题材「{key}」不存在")
    db.delete(row)
    db.commit()
    genres.invalidate_cache()
    return {"ok": True, "deleted": key}

@router.post("/api/genres/reload")
def reload_genres(db: Session = Depends(get_db), _: User = Depends(auth.require_admin)):
    """清题材缓存 + 补种子（配置中心"重新加载"按钮）。"""
    from apps.api import genres
    n = genres.seed_builtin(db)
    genres.invalidate_cache()
    return {"ok": True, "seeded": n, "total": len(genres.effective_genres(db))}

@router.get("/api/genres/export")
def export_genres(include_builtin: int = 1, db: Session = Depends(get_db),
                  _: User = Depends(auth.require_admin)):
    """导出题材包为 JSON（下载用）。include_builtin=0 只导自定义包。"""
    from apps.api import genres
    return genres.export_packs(db, include_builtin=bool(include_builtin))

@router.post("/api/genres/import")
def import_genres(body: dict, on_conflict: str = "skip", db: Session = Depends(get_db),
                  _: User = Depends(auth.require_admin)):
    """导入题材包。on_conflict: skip(默认) / update / rename。

    容错策略（计划书 §六 风险 8）：单条坏数据只记 errors 不中断整批；
    全程 try/except，失败回滚。
    """
    from apps.api import genres
    if on_conflict not in ("skip", "update", "rename"):
        raise HTTPException(400, "on_conflict 只能是 skip / update / rename")
    # 允许直接传 {"packs":[...]} 或裸数组
    payload = body if isinstance(body, dict) else {"packs": body}
    if "packs" not in payload and isinstance(body, list):
        payload = {"packs": body}
    res = genres.import_packs(db, payload, on_conflict=on_conflict)
    genres.invalidate_cache()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "导入失败")
    return res


# ================================================================ 题材包市场（v0.19.7 F3）
class MarketPublishIn(BaseModel):
    """发布到市场（用本机某个题材的配置）。"""
    key: str
    name: str = ""
    author: str = ""
    version: str = "1.0.0"
    description: str = ""
    readme_md: str = ""
    tags: list = []



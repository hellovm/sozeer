"""一致性体系路由（v0.21 B 组）。

★ 端点分组：
  B1 向量检索：索引状态 / 重建索引 / 召回抽检 / 回填向量
  B2 关系图谱：图数据 / 增删改关系 / 矛盾检测
  B3 情绪弧线：曲线数据 / 写入情绪
  B4 伏笔提醒：债务清单
  B5 角色资产：资产读写 / 外观 prompt
  B6 信息差：  知情状态读写
  汇总：      全量一致性检查

★ 权限：读操作用 book.read，写操作用 book.write（v0.20.7 A7 RBAC）
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from apps.api import auth, consistency_v21 as C
from apps.api import memory
from apps.api.db import get_db
from apps.api.models import Book, Chapter, Character, Foreshadow, User
from apps.api.permissions import assert_book_perm

router = APIRouter()
log = logging.getLogger("novelforge")


def _get_book(db: Session, book_id: str, user: User, perm: str = "book.read") -> Book:
    b = db.get(Book, book_id)
    if b is None:
        raise HTTPException(404, "作品不存在")
    assert_book_perm(db, user, b, perm)
    return b


# ================================================================ B1 向量检索

@router.get("/api/books/{book_id}/memory/stats")
def memory_stats(book_id: str, db: Session = Depends(get_db),
                 user: User = Depends(auth.get_current_user)):
    """★ 索引状态（含稠密向量覆盖率、编码器状态）。"""
    _get_book(db, book_id, user)
    return memory.memory_stats(db, book_id)


class ReindexIn(BaseModel):
    ch_idx: int | None = None


# ★ 注意：`POST /api/books/{id}/memory/reindex` 已存在于 main.py（v0.14 D1 时加的）。
#   这里**不再注册**同名路由 —— 重复注册会导致 OpenAPI Operation ID 冲突，
#   且 FastAPI 只认先注册的那个，容易造成"改了代码没生效"的困惑。
#   单章重建请用 main.py 的既有端点；需要全量回填稠密向量用下面的 /backfill。


@router.post("/api/books/{book_id}/memory/backfill")
def memory_backfill(book_id: str, limit: int = Query(500, ge=1, le=5000),
                    db: Session = Depends(get_db),
                    user: User = Depends(auth.get_current_user)):
    """★ 回填稠密向量（配好 embedding 后对历史块跑一次）。"""
    _get_book(db, book_id, user, "book.write")
    return memory.backfill_embeddings(db, book_id, limit=limit)


@router.get("/api/books/{book_id}/memory/recall")
def memory_recall(book_id: str, q: str, ch_idx: int = 0,
                  top_k: int = Query(6, ge=1, le=20),
                  db: Session = Depends(get_db),
                  user: User = Depends(auth.get_current_user)):
    """★ 召回抽检（B1 验收：人工抽检相关性 ≥ 80%）。"""
    _get_book(db, book_id, user)
    return memory.recall_quality(db, book_id, q, ch_idx=ch_idx, top_k=top_k)


# ================================================================ B2 关系图谱

@router.get("/api/books/{book_id}/relations/graph")
def relations_graph(book_id: str, db: Session = Depends(get_db),
                    user: User = Depends(auth.get_current_user)):
    """力导向图数据（nodes + links）。"""
    _get_book(db, book_id, user)
    return C.build_graph(db, book_id)


@router.get("/api/books/{book_id}/relations/conflicts")
def relations_conflicts(book_id: str, db: Session = Depends(get_db),
                        user: User = Depends(auth.get_current_user)):
    """★ 关系矛盾检测。"""
    _get_book(db, book_id, user)
    return {"issues": C.detect_relation_conflict(db, book_id)}


class RelationIn(BaseModel):
    from_name: str
    to_name: str
    relation_type: str = "陌生"
    intensity: int | None = None
    since_chapter: int = 0
    note: str = ""


@router.post("/api/books/{book_id}/relations")
def relation_upsert(book_id: str, body: RelationIn, db: Session = Depends(get_db),
                    user: User = Depends(auth.get_current_user)):
    """新增/更新关系。"""
    _get_book(db, book_id, user, "book.write")
    if not body.from_name or not body.to_name:
        raise HTTPException(400, "缺少角色名")
    if body.relation_type not in C.RELATION_TYPES:
        raise HTTPException(400, f"非法关系类型（可选：{'、'.join(C.RELATION_TYPES)}）")
    return C.add_relation(db, book_id=book_id, from_name=body.from_name,
                          to_name=body.to_name, relation_type=body.relation_type,
                          intensity=body.intensity, since_chapter=body.since_chapter,
                          note=body.note, source="manual")


@router.delete("/api/books/{book_id}/relations/{rel_id}")
def relation_delete(book_id: str, rel_id: str, db: Session = Depends(get_db),
                    user: User = Depends(auth.get_current_user)):
    """删除关系。"""
    _get_book(db, book_id, user, "book.write")
    from apps.api.models import CharacterRelation
    r = db.get(CharacterRelation, rel_id)
    if r is None or r.book_id != book_id:
        raise HTTPException(404, "关系不存在")
    db.delete(r)
    db.commit()
    return {"ok": True}


# ================================================================ B3 情绪弧线

@router.get("/api/books/{book_id}/emotion")
def emotion_arc(book_id: str, db: Session = Depends(get_db),
                user: User = Depends(auth.get_current_user)):
    """★ 情绪弧线（曲线 + 结构告警）。"""
    _get_book(db, book_id, user)
    return C.emotion_arc(db, book_id)


class EmotionIn(BaseModel):
    ch_idx: int
    intensity: int = 50
    valence: int = 0
    tags: list[str] = []
    note: str = ""


@router.post("/api/books/{book_id}/emotion")
def emotion_set(book_id: str, body: EmotionIn, db: Session = Depends(get_db),
                user: User = Depends(auth.get_current_user)):
    """写入/更新某章情绪。"""
    _get_book(db, book_id, user, "book.write")
    return C.set_emotion(db, book_id=book_id, ch_idx=body.ch_idx,
                         intensity=body.intensity, valence=body.valence,
                         tags=body.tags, note=body.note)


# ================================================================ B4 伏笔提醒

@router.get("/api/books/{book_id}/foreshadows/debt")
def foreshadow_debt(book_id: str, ch_idx: int = 0,
                    db: Session = Depends(get_db),
                    user: User = Depends(auth.get_current_user)):
    """★ 伏笔债务清单（超期分级）。"""
    _get_book(db, book_id, user)
    cur = int(ch_idx or 0)
    if not cur:
        cur = int(db.query(Chapter).filter(Chapter.book_id == book_id)
                  .count() or 0)
    return C.due_foreshadows(db, book_id, cur)


# ================================================================ B5 角色资产

@router.get("/api/books/{book_id}/characters/{name}/asset")
def character_asset_get(book_id: str, name: str, db: Session = Depends(get_db),
                        user: User = Depends(auth.get_current_user)):
    """取角色资产（参考图 + 外观锁定）。"""
    _get_book(db, book_id, user)
    a = C.character_asset(db, book_id, name)
    if not a:
        raise HTTPException(404, "角色不存在")
    return a


class AssetIn(BaseModel):
    ref_images: list[str] | None = None
    appearance_locks: dict | None = None


@router.put("/api/books/{book_id}/characters/{name}/asset")
def character_asset_set(book_id: str, name: str, body: AssetIn,
                        db: Session = Depends(get_db),
                        user: User = Depends(auth.get_current_user)):
    """更新角色资产。"""
    _get_book(db, book_id, user, "book.write")
    r = C.set_character_asset(db, book_id, name, ref_images=body.ref_images,
                              appearance_locks=body.appearance_locks)
    if not r.get("ok"):
        raise HTTPException(404, r.get("error") or "失败")
    return r


@router.get("/api/books/{book_id}/characters/{name}/appearance-prompt")
def character_appearance_prompt(book_id: str, name: str,
                                db: Session = Depends(get_db),
                                user: User = Depends(auth.get_current_user)):
    """★ 外观锁定 prompt（短剧生成时注入，解决五官漂移）。"""
    _get_book(db, book_id, user)
    return {"name": name, "prompt": C.appearance_prompt(db, book_id, name)}


# ================================================================ B6 信息差

@router.get("/api/books/{book_id}/knowledge/{name}")
def knowledge_get(book_id: str, name: str, db: Session = Depends(get_db),
                  user: User = Depends(auth.get_current_user)):
    """★ 角色知情状态。"""
    _get_book(db, book_id, user)
    return {"character": name, "items": C.knowledge_of(db, book_id, name)}


class KnowledgeIn(BaseModel):
    character_name: str
    fact_id: str
    fact_desc: str = ""
    known: bool = True
    chapter_learned: int = 0
    note: str = ""


@router.post("/api/books/{book_id}/knowledge")
def knowledge_set(book_id: str, body: KnowledgeIn, db: Session = Depends(get_db),
                  user: User = Depends(auth.get_current_user)):
    """写入角色知情状态。"""
    _get_book(db, book_id, user, "book.write")
    return C.set_knowledge(db, book_id=book_id, character_name=body.character_name,
                           fact_id=body.fact_id, fact_desc=body.fact_desc,
                           known=body.known, chapter_learned=body.chapter_learned,
                           note=body.note)


class KnowledgeCheckIn(BaseModel):
    ch_idx: int
    text: str
    char_names: list[str] = []


@router.post("/api/books/{book_id}/knowledge/check")
def knowledge_check(book_id: str, body: KnowledgeCheckIn,
                    db: Session = Depends(get_db),
                    user: User = Depends(auth.get_current_user)):
    """★★ 信息差越界检测（里程碑第一条：角色说了他不该知道的事）。"""
    _get_book(db, book_id, user)
    return {"issues": C.check_knowledge_violation(
        db, book_id, body.ch_idx, body.text, body.char_names or None)}


# ================================================================ 汇总体检

@router.get("/api/books/{book_id}/consistency")
def consistency_all(book_id: str, ch_idx: int = 0, text: str = "",
                    db: Session = Depends(get_db),
                    user: User = Depends(auth.get_current_user)):
    """★★ 全量一致性检查（B2-B6 汇总，计划书里程碑统一入口）。"""
    _get_book(db, book_id, user)
    return C.full_consistency_check(db, book_id, ch_idx=ch_idx, text=text)

"""创作工作流路由（v0.22 C 组）。

★ 端点分组：
  C1 三级大纲：读树 / 写小说级 / 写卷级 / 写章节级 / 遵从度检查
  C2 场景层：  列场景 / 从正文切分 / 增删改 / 场景体检 / 聚合情绪
  C4 去 AI 腔：分析 / 规则目录 / A-B 对比 / 改写指令
  C5 片段重拍：准备 / 执行 / 批量准备
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from apps.api import auth, deai, retake, workflow
from apps.api.db import get_db
from apps.api.models import Book, DramaScene, User
from apps.api.permissions import assert_book_perm

router = APIRouter()
log = logging.getLogger("novelforge")


def _book(db: Session, book_id: str, user: User, perm: str = "book.read") -> Book:
    b = db.get(Book, book_id)
    if b is None:
        raise HTTPException(404, "作品不存在")
    assert_book_perm(db, user, b, perm)
    return b


# ================================================================ C1 三级大纲

@router.get("/api/books/{book_id}/outline")
def outline_get(book_id: str, db: Session = Depends(get_db),
                user: User = Depends(auth.get_current_user)):
    """★ 三级大纲树（小说 → 卷 → 章）。"""
    _book(db, book_id, user)
    return workflow.get_outline(db, book_id)


class NovelOutlineIn(BaseModel):
    """小说级大纲（部分更新）。"""
    logline: str | None = None
    theme: str | None = None
    conflict: str | None = None
    arc: str | None = None
    setting: str | None = None
    tone: str | None = None
    acts: list | None = None


@router.put("/api/books/{book_id}/outline/novel")
def outline_set_novel(book_id: str, body: NovelOutlineIn,
                      db: Session = Depends(get_db),
                      user: User = Depends(auth.get_current_user)):
    """写小说级大纲。"""
    _book(db, book_id, user, "book.write")
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    r = workflow.set_novel_outline(db, book_id, data)
    if not r.get("ok"):
        raise HTTPException(404, r.get("error") or "失败")
    return r


class VolumeOutlineIn(BaseModel):
    goal: str | None = None
    conflict: str | None = None
    turning_points: list | None = None
    key_events: list | None = None
    ends_with: str | None = None
    name: str | None = None
    outline: str | None = None
    start_ch: int | None = None
    end_ch: int | None = None


@router.put("/api/books/{book_id}/outline/volume/{vol_idx}")
def outline_set_volume(book_id: str, vol_idx: int, body: VolumeOutlineIn,
                       db: Session = Depends(get_db),
                       user: User = Depends(auth.get_current_user)):
    """写卷级大纲。"""
    _book(db, book_id, user, "book.write")
    d = body.model_dump()
    meta = {k: v for k, v in d.items()
            if k in workflow.VOLUME_FIELDS and v is not None}
    r = workflow.set_volume_outline(
        db, book_id, vol_idx, meta, name=d.get("name"),
        outline=d.get("outline"), start_ch=d.get("start_ch"),
        end_ch=d.get("end_ch"))
    return r


class ChapterOutlineIn(BaseModel):
    purpose: str | None = None
    conflict: str | None = None
    twist: str | None = None
    hook: str | None = None
    pov: str | None = None
    scenes: list | None = None
    outline: str | None = None


@router.put("/api/books/{book_id}/outline/chapter/{ch_idx}")
def outline_set_chapter(book_id: str, ch_idx: int, body: ChapterOutlineIn,
                        db: Session = Depends(get_db),
                        user: User = Depends(auth.get_current_user)):
    """写章节级大纲。"""
    _book(db, book_id, user, "book.write")
    d = body.model_dump()
    meta = {k: v for k, v in d.items()
            if k in workflow.CHAPTER_FIELDS and v is not None}
    return workflow.set_chapter_outline(db, book_id, ch_idx, meta,
                                        outline=d.get("outline"))


@router.get("/api/books/{book_id}/outline/prompt")
def outline_prompt(book_id: str, ch_idx: int = 1,
                   db: Session = Depends(get_db),
                   user: User = Depends(auth.get_current_user)):
    """★ 大纲的 prompt 片段（写作时注入，可预览）。"""
    _book(db, book_id, user)
    return {"ch_idx": ch_idx,
            "prompt": workflow.outline_for_prompt(db, book_id, ch_idx)}


class AlignIn(BaseModel):
    ch_idx: int
    text: str = ""


@router.post("/api/books/{book_id}/outline/check")
def outline_check(book_id: str, body: AlignIn, db: Session = Depends(get_db),
                  user: User = Depends(auth.get_current_user)):
    """★ 大纲遵从度检查。"""
    _book(db, book_id, user)
    return {"issues": workflow.check_outline_alignment(
        db, book_id, body.ch_idx, body.text)}


# ================================================================ C2 场景层

@router.get("/api/books/{book_id}/scenes")
def scenes_list(book_id: str, ch_idx: int | None = None,
                db: Session = Depends(get_db),
                user: User = Depends(auth.get_current_user)):
    """列场景（可按章过滤）。"""
    _book(db, book_id, user)
    items = workflow.list_scenes(db, book_id, ch_idx)
    return {"items": items, "count": len(items)}


class SplitIn(BaseModel):
    ch_idx: int
    force: bool = False


@router.post("/api/books/{book_id}/scenes/split")
def scenes_split(book_id: str, body: SplitIn, db: Session = Depends(get_db),
                 user: User = Depends(auth.get_current_user)):
    """★ 从正文自动切分场景（确定性规则，零成本）。"""
    _book(db, book_id, user, "book.write")
    r = workflow.sync_scenes_from_chapter(db, book_id, body.ch_idx, force=body.force)
    if not r.get("ok"):
        # 已有手工场景时返回 200 + 提示（不是错误，是保护）
        return r
    return r


class SceneIn(BaseModel):
    ch_idx: int | None = None
    idx: int | None = None
    title: str | None = None
    goal: str | None = None
    conflict: str | None = None
    turn: str | None = None
    location: str | None = None
    time_point: str | None = None
    characters: list | None = None
    pov: str | None = None
    emotion_intensity: int | None = None
    emotion_valence: int | None = None
    body: str | None = None
    status: str | None = None
    edited: bool = False


@router.post("/api/books/{book_id}/scenes")
def scene_create(book_id: str, body: SceneIn, db: Session = Depends(get_db),
                 user: User = Depends(auth.get_current_user)):
    """新建场景。"""
    _book(db, book_id, user, "book.write")
    r = workflow.upsert_scene(db, book_id, body.model_dump())
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "失败")
    return r


@router.put("/api/books/{book_id}/scenes/{scene_id}")
def scene_update(book_id: str, scene_id: str, body: SceneIn,
                 db: Session = Depends(get_db),
                 user: User = Depends(auth.get_current_user)):
    """更新场景。"""
    _book(db, book_id, user, "book.write")
    d = body.model_dump()
    d["edited"] = True          # ★ 标记为手工编辑（防被自动切分覆盖）
    r = workflow.upsert_scene(db, book_id, d, scene_id=scene_id)
    if not r.get("ok"):
        raise HTTPException(404, r.get("error") or "失败")
    return r


@router.delete("/api/books/{book_id}/scenes/{scene_id}")
def scene_delete(book_id: str, scene_id: str, db: Session = Depends(get_db),
                 user: User = Depends(auth.get_current_user)):
    """删除场景。"""
    _book(db, book_id, user, "book.write")
    r = workflow.delete_scene(db, book_id, scene_id)
    if not r.get("ok"):
        raise HTTPException(404, r.get("error") or "失败")
    return r


class EmotionAggIn(BaseModel):
    ch_idx: int


@router.post("/api/books/{book_id}/scenes/to-emotion")
def scenes_to_emotion(book_id: str, body: EmotionAggIn,
                      db: Session = Depends(get_db),
                      user: User = Depends(auth.get_current_user)):
    """★ 场景情绪聚合到章节级（与 B3 情绪弧线打通）。"""
    _book(db, book_id, user, "book.write")
    r = workflow.scenes_to_emotion(db, book_id, body.ch_idx)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "失败")
    return r


@router.get("/api/books/{book_id}/workflow/stats")
def workflow_stats(book_id: str, db: Session = Depends(get_db),
                   user: User = Depends(auth.get_current_user)):
    """创作工作流总览（大纲完成度 + 场景统计）。"""
    _book(db, book_id, user)
    return workflow.workflow_stats(db, book_id)


# ================================================================ C4 去 AI 腔

class DeaiIn(BaseModel):
    text: str
    words: int = 0


@router.post("/api/deai/analyze")
def deai_analyze(body: DeaiIn, user: User = Depends(auth.get_current_user)):
    """★ 去 AI 腔分析（含命中率统计与改写建议）。"""
    rep = deai.analyze(body.text or "", chapter_words=int(body.words or 0))
    d = rep.to_dict()
    d["suggestions"] = deai.rewrite_suggestions(rep)
    return d


@router.get("/api/deai/rules")
def deai_rules(user: User = Depends(auth.get_current_user)):
    """规则目录（44 条，后台展示用）。"""
    return deai.rule_catalog()


class DeaiCompareIn(BaseModel):
    before: str
    after: str


@router.post("/api/deai/compare")
def deai_compare(body: DeaiCompareIn, user: User = Depends(auth.get_current_user)):
    """★ A/B 对比（验收要求"产出质量优于单阶段"可验证）。"""
    return deai.compare(body.before or "", body.after or "")


class DeaiPromptIn(BaseModel):
    text: str
    max_items: int = Field(8, ge=1, le=20)


@router.post("/api/deai/rewrite-prompt")
def deai_rewrite_prompt(body: DeaiPromptIn,
                        user: User = Depends(auth.get_current_user)):
    """★ 生成 LLM 改写指令（规则层 → LLM 层的桥）。"""
    # ★ 注意签名：prompt_for_rewrite(text, report, max_items=...)
    #   漏传 text 会 500（实测踩坑）
    rep = deai.analyze(body.text or "")
    return {"prompt": deai.prompt_for_rewrite(body.text or "", rep,
                                              max_items=body.max_items),
            "score": rep.score}


# ================================================================ C5 片段重拍

class RetakePrepareIn(BaseModel):
    scene_db_id: str
    new_description: str = ""
    new_image_prompt: str = ""
    new_video_prompt: str = ""


def _scene(db: Session, scene_db_id: str, user: User):
    from apps.api.models import DramaProject
    s = db.get(DramaScene, scene_db_id)
    if s is None:
        raise HTTPException(404, "镜头不存在")
    p = db.get(DramaProject, s.project_id)
    if p is None:
        raise HTTPException(404, "短剧项目不存在")
    _book(db, p.book_id, user, "drama.run")
    return s, p


@router.post("/api/drama/retake/prepare")
def retake_prepare(body: RetakePrepareIn, db: Session = Depends(get_db),
                   user: User = Depends(auth.get_current_user)):
    """★ 重拍准备（不执行生成，先看会改什么）。"""
    s, p = _scene(db, body.scene_db_id, user)
    return retake.prepare_retake(db, p, s,
                                 new_description=body.new_description,
                                 new_image_prompt=body.new_image_prompt,
                                 new_video_prompt=body.new_video_prompt)


class RetakeApplyIn(RetakePrepareIn):
    reset_media: bool = True


@router.post("/api/drama/retake/apply")
def retake_apply(body: RetakeApplyIn, db: Session = Depends(get_db),
                 user: User = Depends(auth.get_current_user)):
    """★ 执行重拍（只影响该镜头）。"""
    s, p = _scene(db, body.scene_db_id, user)
    return retake.apply_retake(db, p, s,
                               new_description=body.new_description,
                               new_image_prompt=body.new_image_prompt,
                               new_video_prompt=body.new_video_prompt,
                               reset_media=body.reset_media)


class BatchRetakeIn(BaseModel):
    project_id: str
    episode: int
    scene_idxs: list[int]


@router.post("/api/drama/retake/batch-prepare")
def retake_batch(body: BatchRetakeIn, db: Session = Depends(get_db),
                 user: User = Depends(auth.get_current_user)):
    """★ 批量重拍准备（含相邻镜头衔接提醒）。"""
    from apps.api.models import DramaProject
    p = db.get(DramaProject, body.project_id)
    if p is None:
        raise HTTPException(404, "短剧项目不存在")
    _book(db, p.book_id, user, "drama.run")
    return retake.prepare_batch_retake(db, p, body.episode, body.scene_idxs)

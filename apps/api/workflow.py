"""创作工作流：三级大纲 + 场景层（v0.22.1 C1 / v0.22.2 C2）。

★★★ 为什么需要这两块：

  **C1 三级大纲**（小说 → 卷 → 章）
    现状：只有 `Book.synopsis` + `Volume.outline` 纯文本 + 章节标题。
    问题：写到第 50 章时，"这一章在大局中的位置"完全靠模型猜。
    解法：把大纲**结构化**（logline / 主题 / 核心冲突 / 幕结构），
          每一层都能回答"我在哪、要去哪"。

  **C2 场景层**（章 → 场景）
    现状：一章是一个整体（3000+ 字）。
    问题：
      · 一次生成整章容易跑偏（上下文太长，焦点分散）
      · 重写只能整章重写（浪费 token）
      · 无法精确检查"时间地点人物"一致性
    解法：一章拆成 2-5 个场景（同一时间地点的连续动作），
          每个场景有 goal / conflict / turn 三要素。

================================ 关键设计 ================================

★ **大纲结构化但不是"必须填"**：
  所有字段都是可选的。用户可以先写一句话，后续逐步补全。
  `completeness()` 给出完成度百分比，UI 据此提示"还差什么"。

★ **场景自动切分是"辅助"不是"强制"**：
  `split_scenes()` 按空行/分隔符做**确定性切分**，不调模型。
  用户可手动调整。切分结果标记 `source="split"` 便于区分。

★ **goal/conflict/turn 三要素**（来自编剧理论）：
  没有目标 → 场景无方向；没有冲突 → 场景无聊；没有转折 → 场景无价值。
  `diagnose_scene()` 会指出缺哪一项。

★ **与 B3 情绪弧线打通**：场景的 emotion_intensity/valence
  可以聚合到章节级，作为 `chapter_emotion` 的输入。
"""
from __future__ import annotations

import json
import logging
import re
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from apps.api.models import Book, BookScene, Chapter, Volume, now

log = logging.getLogger("novelforge")


def _uid(p: str) -> str:
    return f"{p}_{uuid.uuid4().hex[:12]}"


def _jl(s, d=None):
    if not s:
        return d
    try:
        return json.loads(s)
    except Exception:
        return d


def _jd(o) -> str:
    return json.dumps(o, ensure_ascii=False)


def _naive_now():
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


# ============================================================================
#  C1 三级大纲
# ============================================================================

# ★ 小说级大纲字段（单一事实源，前端据此渲染表单）
NOVEL_FIELDS = {
    "logline": "一句话概括（谁 + 想要什么 + 阻碍）",
    "theme": "主题（这本书想说什么）",
    "conflict": "核心冲突（贯穿全书的主要矛盾）",
    "arc": "主角成长弧（从什么状态到什么状态）",
    "setting": "时代/世界背景",
    "tone": "基调（热血/悬疑/温情…）",
    "acts": "幕结构（JSON 数组：[{name, desc, ch_start, ch_end}]）",
}

VOLUME_FIELDS = {
    "goal": "本卷目标（主角要达成什么）",
    "conflict": "本卷主要冲突",
    "turning_points": "转折点（JSON 数组，按序）",
    "key_events": "关键事件（JSON 数组）",
    "ends_with": "卷末状态（留给下一卷什么）",
}

CHAPTER_FIELDS = {
    "purpose": "本章目的（推进什么/揭示什么）",
    "conflict": "本章冲突",
    "twist": "本章反转/意外",
    "hook": "章末钩子",
    "pov": "视角人物",
    "scenes": "场景列表（JSON 数组）",
}


def get_outline(db: Session, book_id: str) -> dict:
    """★ 取完整三级大纲（小说 → 卷 → 章 树）。

    返回结构：
      {
        novel: {...结构化字段..., completeness: 75},
        volumes: [{idx, name, ..., chapters: [{idx, title, outline_meta, ...}]}]
      }
    """
    b = db.get(Book, book_id)
    if b is None:
        return {}
    novel = _jl(getattr(b, "outline_meta", ""), {}) or {}
    vols = db.scalars(select(Volume).where(Volume.book_id == book_id)
                      .order_by(Volume.idx)).all()
    chs = db.scalars(select(Chapter).where(Chapter.book_id == book_id)
                     .order_by(Chapter.idx)).all()

    # 章按卷分组（无卷的归入"未分卷"）
    by_vol: dict[int, list] = {}
    for c in chs:
        v = 0
        for vol in vols:
            s = int(vol.start_ch or 0)
            e = int(vol.end_ch or 0)
            if s and c.idx >= s and (not e or c.idx <= e):
                v = int(vol.idx or 0)
                break
        by_vol.setdefault(v, []).append({
            "idx": c.idx, "title": c.title or "",
            "outline": getattr(c, "outline", "") or "",
            "meta": _jl(getattr(c, "outline_meta", ""), {}) or {},
            "words": int(c.words or 0),
            "status": c.status,
            "completeness": _chapter_completeness(c),
        })

    volumes = []
    for v in vols:
        vm = _jl(getattr(v, "outline_meta", ""), {}) or {}
        volumes.append({
            "idx": int(v.idx or 0), "name": v.name or "",
            "outline": v.outline or "", "meta": vm,
            "start_ch": int(v.start_ch or 0), "end_ch": int(v.end_ch or 0),
            "summary": v.summary or "",
            "completeness": _dict_completeness(vm, VOLUME_FIELDS),
            "chapters": by_vol.get(int(v.idx or 0), []),
        })
    if by_vol.get(0):
        volumes.append({"idx": 0, "name": "（未分卷）", "outline": "", "meta": {},
                        "start_ch": 0, "end_ch": 0, "summary": "",
                        "completeness": 0, "chapters": by_vol[0]})

    return {
        "book_id": book_id, "title": b.title or "",
        "synopsis": b.synopsis or "",
        "novel": novel,
        "novel_completeness": _dict_completeness(novel, NOVEL_FIELDS),
        "fields": {"novel": NOVEL_FIELDS, "volume": VOLUME_FIELDS,
                   "chapter": CHAPTER_FIELDS},
        "volumes": volumes,
        "stats": {"volumes": len(vols), "chapters": len(chs)},
    }


def _dict_completeness(d: dict, fields: dict) -> int:
    """结构化字段完成度（0-100）。"""
    if not fields:
        return 0
    n = 0
    for k in fields:
        v = d.get(k)
        if v not in (None, "", [], {}):
            n += 1
    return int(n / len(fields) * 100)


def _chapter_completeness(c: Chapter) -> int:
    meta = _jl(getattr(c, "outline_meta", ""), {}) or {}
    text_ok = bool((getattr(c, "outline", "") or "").strip())
    sc = _dict_completeness(meta, CHAPTER_FIELDS)
    return min(100, (60 if text_ok else 0) + int(sc * 0.4))


def set_novel_outline(db: Session, book_id: str, data: dict) -> dict:
    """更新小说级大纲（部分更新，未提供的字段保留）。"""
    b = db.get(Book, book_id)
    if b is None:
        return {"ok": False, "error": "作品不存在"}
    cur = _jl(getattr(b, "outline_meta", ""), {}) or {}
    for k, v in (data or {}).items():
        if k in NOVEL_FIELDS:
            cur[k] = v
    b.outline_meta = _jd(cur)
    # 兼容：logline 同步到 synopsis（如果 synopsis 为空）
    if not (b.synopsis or "").strip() and cur.get("logline"):
        b.synopsis = str(cur["logline"])[:1000]
    db.commit()
    return {"ok": True, "novel": cur,
            "completeness": _dict_completeness(cur, NOVEL_FIELDS)}


def set_volume_outline(db: Session, book_id: str, vol_idx: int,
                       data: dict, *, name: str | None = None,
                       outline: str | None = None,
                       start_ch: int | None = None,
                       end_ch: int | None = None) -> dict:
    """更新卷级大纲（无卷则创建）。"""
    v = db.scalar(select(Volume).where(Volume.book_id == book_id,
                                       Volume.idx == int(vol_idx)))
    if v is None:
        v = Volume(id=_uid("vol"), book_id=book_id, idx=int(vol_idx),
                   name=(name or f"第{vol_idx}卷"))
        db.add(v)
        db.commit()
    cur = _jl(getattr(v, "outline_meta", ""), {}) or {}
    for k, val in (data or {}).items():
        if k in VOLUME_FIELDS:
            cur[k] = val
    v.outline_meta = _jd(cur)
    if name is not None:
        v.name = name
    if outline is not None:
        v.outline = outline
    if start_ch is not None:
        v.start_ch = int(start_ch)
    if end_ch is not None:
        v.end_ch = int(end_ch)
    db.commit()
    return {"ok": True, "idx": v.idx, "name": v.name, "meta": cur,
            "completeness": _dict_completeness(cur, VOLUME_FIELDS)}


def set_chapter_outline(db: Session, book_id: str, ch_idx: int,
                        data: dict, *, outline: str | None = None) -> dict:
    """更新章节级大纲（无章节则创建占位行）。"""
    c = db.scalar(select(Chapter).where(Chapter.book_id == book_id,
                                        Chapter.idx == int(ch_idx)))
    if c is None:
        # 章节可能还没写正文 —— 建占位行（status=planned）
        c = Chapter(id=_uid("cp"), book_id=book_id, idx=int(ch_idx),
                    title=f"第{ch_idx}章", content="", words=0,
                    status="planned")
        db.add(c)
        db.commit()
    cur = _jl(getattr(c, "outline_meta", ""), {}) or {}
    for k, val in (data or {}).items():
        if k in CHAPTER_FIELDS:
            cur[k] = val
    c.outline_meta = _jd(cur)
    if outline is not None:
        c.outline = outline
    db.commit()
    return {"ok": True, "ch_idx": c.idx, "meta": cur,
            "outline": c.outline or "",
            "completeness": _chapter_completeness(c)}


def outline_for_prompt(db: Session, book_id: str, ch_idx: int) -> str:
    """★ 把三级大纲渲染成 prompt 片段（写作时注入"我在哪、要去哪"）。

    ★ 为什么只注入相关层级：全书大纲可能几千字，全塞进去浪费预算。
      取：小说核心（logline/主题/冲突）+ 本章所在卷 + 本章大纲。
    """
    b = db.get(Book, book_id)
    if b is None:
        return ""
    novel = _jl(getattr(b, "outline_meta", ""), {}) or {}
    lines: list[str] = []

    core = []
    for k in ("logline", "theme", "conflict", "arc"):
        if novel.get(k):
            core.append(f"- {NOVEL_FIELDS[k].split('（')[0]}：{novel[k]}")
    if core:
        lines.append("【全书定位】")
        lines.extend(core)

    # 本章所在卷
    vol = db.scalar(select(Volume).where(
        Volume.book_id == book_id,
        Volume.start_ch <= int(ch_idx),
        (Volume.end_ch >= int(ch_idx)) | (Volume.end_ch == 0) |
        (Volume.end_ch.is_(None))).order_by(Volume.idx))
    if vol is not None:
        vm = _jl(getattr(vol, "outline_meta", ""), {}) or {}
        vl = [f"- 卷名：{vol.name or ''}"]
        for k in ("goal", "conflict", "ends_with"):
            if vm.get(k):
                vl.append(f"- {VOLUME_FIELDS[k].split('（')[0]}：{vm[k]}")
        if vol.outline:
            vl.append(f"- 卷大纲：{vol.outline[:400]}")
        lines.append("\n【本卷定位】")
        lines.extend(vl)

    # 本章大纲
    c = db.scalar(select(Chapter).where(Chapter.book_id == book_id,
                                        Chapter.idx == int(ch_idx)))
    if c is not None:
        cm = _jl(getattr(c, "outline_meta", ""), {}) or {}
        cl = []
        for k in ("purpose", "conflict", "twist", "hook", "pov"):
            if cm.get(k):
                cl.append(f"- {CHAPTER_FIELDS[k].split('（')[0]}：{cm[k]}")
        if c.outline:
            cl.append(f"- 本章大纲：{c.outline[:600]}")
        if cl:
            lines.append("\n【本章定位（必须遵守）】")
            lines.extend(cl)

    return "\n".join(lines)


def check_outline_alignment(db: Session, book_id: str, ch_idx: int,
                            text: str) -> list[dict]:
    """★ 大纲遵从度检查（章节正文 vs 本章大纲）。

    检查项：
      1. 本章大纲的 twist/hook 是否在正文里体现（关键词粗匹配）
      2. 本章是否超出了卷末设定的范围（写了下一卷的内容）
    """
    issues: list[dict] = []
    c = db.scalar(select(Chapter).where(Chapter.book_id == book_id,
                                        Chapter.idx == int(ch_idx)))
    if c is None or not text:
        return issues
    meta = _jl(getattr(c, "outline_meta", ""), {}) or {}

    for k, label in (("twist", "反转"), ("hook", "章末钩子")):
        v = (meta.get(k) or "").strip()
        if not v:
            continue
        kws = _keywords(v)
        if kws and not any(x in text for x in kws):
            issues.append({
                "level": "warn", "kind": "outline_miss",
                "message": f"本章大纲设定的「{label}」在正文里未体现：{v[:40]}",
                "hint": f"确认是否漏写，或调整大纲的 {k} 字段"})

    # 卷边界检查
    vol = db.scalar(select(Volume).where(
        Volume.book_id == book_id,
        Volume.start_ch <= int(ch_idx),
        (Volume.end_ch >= int(ch_idx)) | (Volume.end_ch == 0) |
        (Volume.end_ch.is_(None))).order_by(Volume.idx))
    if vol is not None and int(vol.end_ch or 0) and int(ch_idx) > int(vol.end_ch):
        issues.append({
            "level": "info", "kind": "outline_out_of_volume",
            "message": f"第 {ch_idx} 章超出本卷范围（{vol.start_ch}-{vol.end_ch}）",
            "hint": "调整卷的 end_ch，或补充新卷"})
    return issues


_STOP = {"的", "了", "是", "在", "有", "和", "与", "被", "把", "对", "为"}


def _keywords(text: str, limit: int = 6) -> list[str]:
    toks = re.findall(r"[\u4e00-\u9fff]{2,}", text or "")
    out, seen = [], set()
    for t in toks:
        if t in _STOP or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out[:limit]


# ============================================================================
#  C2 场景层
# ============================================================================

# ★ 场景分隔符（确定性切分，不调模型）
SCENE_SEPS = re.compile(r"^\s*(\*{3,}|-{3,}|={3,}|◆|◇|※|★)\s*$", re.M)


def split_scenes(text: str, *, min_chars: int = 200,
                 max_chars: int = 2000) -> list[dict]:
    """★ 从正文自动切分场景（确定性规则，零成本、可复现）。

    ★ 为什么用规则而不是模型：
      · 切分是"结构操作"，规则足够（模型反而可能改变原文边界）
      · 零成本零延迟，用户点一下就有结果
      · 用户可手动调整 —— 规则只是**起点**

    策略：
      1. 优先按显式分隔符（***、---、◆ 等）
      2. 无分隔符时按空行 + 长度阈值聚合
      3. 过短（< min_chars）的段与相邻合并
    """
    t = (text or "").strip()
    if not t:
        return []

    # ★ 优先按显式分隔符切（用户标记的边界必须尊重，**不聚合**）
    if SCENE_SEPS.search(t):
        parts = [p.strip() for p in SCENE_SEPS.split(t) if p and p.strip()]
        out = [p for p in parts if len(p) >= 20]
        if out:
            return [{"idx": i + 1, "body": p, "words": len(p)}
                    for i, p in enumerate(out)]

    # 无显式分隔符 → 按空行聚合到合理长度
    parts = [p.strip() for p in re.split(r"\n\s*\n", t) if p and p.strip()]
    if not parts:
        return []

    out: list[str] = []
    buf = ""
    for p in parts:
        if not buf:
            buf = p
            continue
        if len(buf) + len(p) + 2 <= max_chars:
            buf += "\n\n" + p
        else:
            out.append(buf)
            buf = p
    if buf:
        out.append(buf)

    # 过短的与相邻合并
    merged: list[str] = []
    for p in out:
        if merged and len(p) < min_chars:
            merged[-1] += "\n\n" + p
        else:
            merged.append(p)

    return [{"idx": i + 1, "body": p, "words": len(p)} for i, p in enumerate(merged)]


def _guess_location(body: str) -> str:
    """粗猜地点（从文本里找"在…"结构）。确定性规则，不调模型。"""
    m = re.search(r"在([\u4e00-\u9fff]{2,10})(?:里|中|上|内|外|前|后)", body or "")
    return m.group(1) if m else ""


def sync_scenes_from_chapter(db: Session, book_id: str, ch_idx: int,
                             *, force: bool = False) -> dict:
    """★ 从章节正文切分场景并入库。

    ★ force=False（默认）：已有手工场景时**不覆盖**（保护用户劳动）。
      force=True：清空重切。
    """
    c = db.scalar(select(Chapter).where(Chapter.book_id == book_id,
                                        Chapter.idx == int(ch_idx)))
    if c is None:
        return {"ok": False, "error": f"第 {ch_idx} 章不存在"}

    existing = db.scalars(select(BookScene).where(
        BookScene.book_id == book_id, BookScene.ch_idx == int(ch_idx))).all()
    manual = [x for x in existing if x.source == "manual"]
    if existing and manual and not force:
        return {"ok": False, "scenes": [scene_dict(x) for x in existing],
                "error": "本章已有手工场景，未覆盖（如需重切请传 force=true）"}

    for x in existing:
        db.delete(x)
    db.commit()

    parts = split_scenes(c.content or "")
    names = _chapter_chars(db, book_id, c.content or "")
    created = []
    for p in parts:
        s = BookScene(
            id=_uid("sc"), book_id=book_id, ch_idx=int(ch_idx), idx=p["idx"],
            title=f"场景 {p['idx']}", body=p["body"], words=p["words"],
            location=_guess_location(p["body"]),
            characters=_jd(names), status="draft", source="split",
            created_at=_naive_now(), updated_at=_naive_now())
        db.add(s)
        created.append(s)
    db.commit()
    return {"ok": True, "count": len(created),
            "scenes": [scene_dict(x) for x in created]}


def _chapter_chars(db: Session, book_id: str, text: str) -> list[str]:
    """本章出现了哪些已登记角色（复用 memory 的确定性扫描）。"""
    try:
        from apps.api import memory
        return memory.extract_char_names(db, book_id, text)
    except Exception:
        return []


def scene_dict(s: BookScene) -> dict:
    return {
        "id": s.id, "ch_idx": s.ch_idx, "idx": s.idx, "title": s.title,
        "goal": s.goal, "conflict": s.conflict, "turn": s.turn,
        "location": s.location, "time_point": s.time_point,
        "characters": _jl(s.characters, []) or [], "pov": s.pov,
        "emotion_intensity": int(s.emotion_intensity or 50),
        "emotion_valence": int(s.emotion_valence or 0),
        "body": s.body or "", "words": int(s.words or 0),
        "status": s.status, "source": s.source,
        "diagnosis": diagnose_scene(s),
    }


def list_scenes(db: Session, book_id: str, ch_idx: int | None = None) -> list[dict]:
    q = select(BookScene).where(BookScene.book_id == book_id)
    if ch_idx is not None:
        q = q.where(BookScene.ch_idx == int(ch_idx))
    rows = db.scalars(q.order_by(BookScene.ch_idx, BookScene.idx)).all()
    return [scene_dict(r) for r in rows]


def upsert_scene(db: Session, book_id: str, data: dict,
                 scene_id: str = "") -> dict:
    """新建或更新场景。"""
    s = db.get(BookScene, scene_id) if scene_id else None
    if s is not None and s.book_id != book_id:
        return {"ok": False, "error": "场景不属于该作品"}
    if s is None:
        s = BookScene(id=_uid("sc"), book_id=book_id,
                      created_at=_naive_now())
        # 自动给 idx
        mx = db.scalar(select(func.coalesce(func.max(BookScene.idx), 0))
                       .where(BookScene.book_id == book_id,
                              BookScene.ch_idx == int(data.get("ch_idx") or 0))) or 0
        s.idx = int(mx) + 1
        db.add(s)

    for k in ("title", "goal", "conflict", "turn", "location", "time_point",
              "pov", "body", "status", "source"):
        if k in data and data[k] is not None:
            setattr(s, k, str(data[k]))
    if "ch_idx" in data and data["ch_idx"] is not None:
        s.ch_idx = int(data["ch_idx"])
    if "idx" in data and data["idx"] is not None:
        s.idx = int(data["idx"])
    if "characters" in data:
        s.characters = _jd(data["characters"] or [])
    if "emotion_intensity" in data and data["emotion_intensity"] is not None:
        s.emotion_intensity = max(0, min(100, int(data["emotion_intensity"])))
    if "emotion_valence" in data and data["emotion_valence"] is not None:
        s.emotion_valence = max(-100, min(100, int(data["emotion_valence"])))
    s.words = len(s.body or "")
    s.updated_at = _naive_now()
    if s.source == "split" and data.get("edited"):
        s.source = "manual"         # 用户改过 → 标记为手工（防被重切覆盖）
    db.commit()
    return {"ok": True, "scene": scene_dict(s)}


def delete_scene(db: Session, book_id: str, scene_id: str) -> dict:
    s = db.get(BookScene, scene_id)
    if s is None or s.book_id != book_id:
        return {"ok": False, "error": "场景不存在"}
    db.delete(s)
    db.commit()
    return {"ok": True}


def diagnose_scene(s: BookScene) -> dict:
    """★ 场景体检（goal/conflict/turn 三要素 + 长度）。

    这是"场景层"的实用价值：一眼看出哪个场景欠打磨。
    """
    issues: list[dict] = []
    if not (s.goal or "").strip():
        issues.append({"level": "warn", "kind": "no_goal",
                       "message": "缺少场景目标（角色想达成什么）"})
    if not (s.conflict or "").strip():
        issues.append({"level": "warn", "kind": "no_conflict",
                       "message": "缺少冲突（阻碍是什么）"})
    if not (s.turn or "").strip():
        issues.append({"level": "info", "kind": "no_turn",
                       "message": "缺少转折（结果如何反转）"})
    w = int(s.words or 0)
    if 0 < w < 300:
        issues.append({"level": "info", "kind": "too_short",
                       "message": f"场景偏短（{w} 字），可能不足以展开"})
    if w > 2500:
        issues.append({"level": "info", "kind": "too_long",
                       "message": f"场景偏长（{w} 字），建议拆分"})
    score = max(0, 100 - sum({"warn": 15, "info": 5}.get(i["level"], 0)
                             for i in issues))
    return {"score": score, "issues": issues}


def scenes_to_emotion(db: Session, book_id: str, ch_idx: int) -> dict:
    """★ 场景情绪聚合到章节级（与 B3 情绪弧线打通）。

    为什么：场景是更细的粒度，聚合后章节情绪更准（人工标一个数容易拍脑袋）。
    """
    rows = db.scalars(select(BookScene).where(
        BookScene.book_id == book_id, BookScene.ch_idx == int(ch_idx))).all()
    if not rows:
        return {"ok": False, "error": "本章无场景"}
    # 强度取最大（高潮决定章节张力），正负取加权平均（按字数）
    inten = max(int(r.emotion_intensity or 50) for r in rows)
    tot = sum(int(r.words or 0) or 1 for r in rows)
    val = int(sum((int(r.emotion_valence or 0)) * ((int(r.words or 0) or 1))
                  for r in rows) / tot)
    try:
        from apps.api import consistency_v21 as C
        return {"ok": True, **C.set_emotion(db, book_id=book_id, ch_idx=ch_idx,
                                            intensity=inten, valence=val,
                                            tags=["场景聚合"],
                                            note=f"由 {len(rows)} 个场景聚合")}
    except Exception as e:                          # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "intensity": inten, "valence": val}


def workflow_stats(db: Session, book_id: str) -> dict:
    """创作工作流总览（大纲完成度 + 场景统计）。"""
    ol = get_outline(db, book_id)
    rows = db.scalars(select(BookScene).where(BookScene.book_id == book_id)).all()
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    no_goal = sum(1 for r in rows if not (r.goal or "").strip())
    no_conflict = sum(1 for r in rows if not (r.conflict or "").strip())
    return {
        "outline": {"novel_completeness": ol.get("novel_completeness", 0),
                    "volumes": ol.get("stats", {}).get("volumes", 0),
                    "chapters": ol.get("stats", {}).get("chapters", 0)},
        "scenes": {"total": len(rows), "by_status": by_status,
                   "missing_goal": no_goal, "missing_conflict": no_conflict,
                   "words": sum(int(r.words or 0) for r in rows)},
    }

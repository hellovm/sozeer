"""一致性引擎（v0.21 B 组：关系图谱 / 情绪弧线 / 伏笔提醒 / 信息差 / 角色资产）。

★★★ 计划书 v0.21 里程碑：
    「能自动发现"角色说了他不该知道的事"和"伏笔埋了20章没回收"」

  这两个正是本模块的核心能力：
    · `check_knowledge_violation()` —— 信息差越界检测
    · `due_foreshadows()`          —— 伏笔超期检测

================================ 五块能力 ================================

  B2 关系图谱    build_graph / detect_relation_conflict
  B3 情绪弧线    compute_arc / detect_low_flat
  B4 伏笔提醒    due_foreshadows / foreshadow_debt
  B5 角色资产    character_asset / appearance_prompt
  B6 信息差      knowledge_of / check_knowledge_violation

★ 设计原则：**能确定性算的不用模型**。
  关系强度、伏笔超期、信息差越界都是**可判定**的，
  用规则算又快又准又免费。模型只用来做"抽取"（从正文里认出关系），
  不用来做"判定"。

================================ 与既有模块的关系 ================================

  consistency.py  已有世界观/度量衡一致性（v0.15 前）
  本模块          补齐人际关系/情绪/信息差（v0.21 B 组）
  两者在 audit.py 里汇总成完整审计报告
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from apps.api.models import (
    Chapter, ChapterEmotion, Character, CharacterKnowledge, CharacterRelation,
    Foreshadow, WorldSetting, now,
)

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


# ============================================================================
#  B2 关系图谱
# ============================================================================

RELATION_TYPES = ("师徒", "仇敌", "暧昧", "血亲", "同门", "盟友", "上下级", "陌生")

# ★ 关系类型的"情感极性"参考值（用于自动抽取时给 intensity 打底）
RELATION_POLARITY = {
    "血亲": 80, "师徒": 70, "暧昧": 75, "同门": 50, "盟友": 60,
    "上下级": 30, "陌生": 0, "仇敌": -80,
}


def add_relation(db: Session, *, book_id: str, from_name: str, to_name: str,
                 relation_type: str = "陌生", intensity: int | None = None,
                 since_chapter: int = 0, note: str = "",
                 source: str = "manual") -> dict:
    """添加/更新角色关系（按 from+to 去重）。"""
    from_c = db.scalar(select(Character).where(Character.book_id == book_id,
                                               Character.name == from_name))
    to_c = db.scalar(select(Character).where(Character.book_id == book_id,
                                             Character.name == to_name))
    if intensity is None:
        intensity = RELATION_POLARITY.get(relation_type, 0)
    intensity = max(-100, min(100, int(intensity)))

    row = db.scalar(select(CharacterRelation).where(
        CharacterRelation.book_id == book_id,
        CharacterRelation.from_name == from_name,
        CharacterRelation.to_name == to_name))
    if row is None:
        row = CharacterRelation(
            id=_uid("rel"), book_id=book_id, from_id=(from_c.id if from_c else ""),
            to_id=(to_c.id if to_c else ""), from_name=from_name, to_name=to_name,
            relation_type=relation_type, intensity=intensity,
            since_chapter=since_chapter, note=note, source=source,
            created_at=now(), updated_at=now())
        db.add(row)
    else:
        row.relation_type = relation_type
        row.intensity = intensity
        if note:
            row.note = note
        if from_c and not row.from_id:
            row.from_id = from_c.id
        if to_c and not row.to_id:
            row.to_id = to_c.id
        row.updated_at = now()
    db.commit()
    return {"id": row.id, "from": from_name, "to": to_name,
            "type": relation_type, "intensity": intensity}


def build_graph(db: Session, book_id: str) -> dict:
    """★ 力导向图数据（nodes + links），前端直接用。

    ★ 为什么后端算节点而不是前端：
      节点大小（关系数）、度数、是否孤立，都需要遍历边表 ——
      后端一次算完比前端 O(n²) 遍历快得多。
    """
    rels = db.scalars(select(CharacterRelation)
                      .where(CharacterRelation.book_id == book_id)).all()
    chars = db.scalars(select(Character).where(Character.book_id == book_id)).all()

    deg: dict[str, int] = {}
    pos_sum: dict[str, int] = {}
    for r in rels:
        deg[r.from_name] = deg.get(r.from_name, 0) + 1
        deg[r.to_name] = deg.get(r.to_name, 0) + 1
        pos_sum[r.from_name] = pos_sum.get(r.from_name, 0) + max(0, r.intensity)
        pos_sum[r.to_name] = pos_sum.get(r.to_name, 0) + max(0, r.intensity)

    names = {c.name for c in chars}
    names |= set(deg.keys())
    nodes = []
    for n in names:
        if not n:
            continue
        nodes.append({
            "id": n, "name": n,
            "degree": deg.get(n, 0),
            "warmth": round(pos_sum.get(n, 0) / max(1, deg.get(n, 1)), 1),
            "role": next((c.role for c in chars if c.name == n), ""),
            "is_orphan": deg.get(n, 0) == 0,      # ★ 孤立角色（无任何关系）
        })
    links = [{"source": r.from_name, "target": r.to_name,
              "type": r.relation_type, "intensity": r.intensity,
              "since_chapter": r.since_chapter, "note": r.note,
              "source_kind": r.source} for r in rels]
    orphans = [n["name"] for n in nodes if n["is_orphan"]]
    return {"nodes": nodes, "links": links,
            "stats": {"nodes": len(nodes), "links": len(links),
                      "orphans": orphans[:20], "orphan_count": len(orphans)}}


def detect_relation_conflict(db: Session, book_id: str) -> list[dict]:
    """★ 关系矛盾检测（B2 一致性检查核心）。

    检测项：
      1. **双向冲突**：A→B 是"仇敌"(-80)，B→A 是"盟友"(+60) 且无剧情说明
         —— 单恋/误解是合理的，但需要人工确认（标记为 warn 而非 error）
      2. **类型与强度冲突**：类型是"血亲"但 intensity < 0（仇视血亲是合理的，
         但需要 note 说明，否则可能是抽取错误）
      3. **自我关系**：A→A（数据错误）
      4. **孤立角色**：有角色没进任何关系（可能遗漏建档）
    """
    rels = db.scalars(select(CharacterRelation)
                      .where(CharacterRelation.book_id == book_id)).all()
    issues: list[dict] = []
    idx = {(r.from_name, r.to_name): r for r in rels}

    seen = set()
    for r in rels:
        # 3. 自环
        if r.from_name and r.from_name == r.to_name:
            issues.append({"level": "error", "kind": "self_relation",
                           "message": f"「{r.from_name}」与自己存在关系（数据错误）",
                           "hint": "删除该条关系"})
            continue

        key = tuple(sorted([r.from_name, r.to_name]))
        if key in seen:
            continue
        seen.add(key)

        rev = idx.get((r.to_name, r.from_name))
        if rev is None:
            continue

        # 1. 双向极性冲突
        opp = (r.intensity > 40 and rev.intensity < -40) or \
              (r.intensity < -40 and rev.intensity > 40)
        if opp and not (r.note or rev.note):
            issues.append({
                "level": "warn", "kind": "relation_conflict",
                "message": (f"「{r.from_name}」对「{r.to_name}」是 {r.relation_type}"
                            f"({r.intensity:+d})，反向却是 {rev.relation_type}"
                            f"({rev.intensity:+d})"),
                "hint": "单恋/误解属合理设定，请在 note 里说明；否则修正其中一条",
                "pair": [r.from_name, r.to_name]})

        # 2. 血亲 + 强负向
        for a, b in ((r, rev), (rev, r)):
            if a.relation_type == "血亲" and a.intensity < -40 and not a.note:
                issues.append({
                    "level": "warn", "kind": "relation_type_intensity",
                    "message": f"「{a.from_name}」与「{a.to_name}」是血亲但强度 {a.intensity:+d}（高度敌对）",
                    "hint": "血缘仇恨是合理剧情，请补充 note 说明缘由"})

    # 4. 孤立角色
    g = build_graph(db, book_id)
    if g["stats"]["orphan_count"]:
        issues.append({
            "level": "info", "kind": "orphan_characters",
            "message": f"{g['stats']['orphan_count']} 个角色没有任何关系（可能遗漏建档）",
            "hint": "为重要角色补充关系，或在人物档案里忽略",
            "characters": g["stats"]["orphans"]})

    return issues


# ============================================================================
#  B3 情绪弧线
# ============================================================================

# ★ 情绪标签（用于可视化上色与统计）
EMOTION_TAGS = ("爆点", "低谷", "转折", "日常", "冲突", "温情")


def set_emotion(db: Session, *, book_id: str, ch_idx: int, intensity: int,
                valence: int, tags: list[str] | None = None,
                note: str = "") -> dict:
    """写入/更新某章情绪（幂等）。"""
    intensity = max(0, min(100, int(intensity)))
    valence = max(-100, min(100, int(valence)))
    row = db.scalar(select(ChapterEmotion).where(
        ChapterEmotion.book_id == book_id, ChapterEmotion.ch_idx == int(ch_idx)))
    if row is None:
        row = ChapterEmotion(
            id=_uid("emo"), book_id=book_id, ch_idx=int(ch_idx),
            intensity=intensity, valence=valence,
            tags=_jd(tags or []), note=note, created_at=now())
        db.add(row)
    else:
        row.intensity = intensity
        row.valence = valence
        row.tags = _jd(tags or _jl(row.tags, []) or [])
        if note:
            row.note = note
    db.commit()
    return {"ch_idx": row.ch_idx, "intensity": row.intensity,
            "valence": row.valence, "tags": _jl(row.tags, [])}


def emotion_arc(db: Session, book_id: str) -> dict:
    """★ 情绪弧线数据（前端画曲线）。

    返回按章号升序的两个序列（intensity / valence），
    并附带**结构与告警**（连续低潮、缺乏爆点）。
    """
    rows = db.scalars(select(ChapterEmotion)
                      .where(ChapterEmotion.book_id == book_id)
                      .order_by(ChapterEmotion.ch_idx)).all()
    points = [{"ch_idx": r.ch_idx, "intensity": r.intensity,
               "valence": r.valence, "tags": _jl(r.tags, []) or [],
               "note": r.note} for r in rows]
    return {"points": points, "issues": detect_low_flat(points),
            "stats": _arc_stats(points)}


def _arc_stats(points: list[dict]) -> dict:
    if not points:
        return {"count": 0}
    inten = [p["intensity"] for p in points]
    val = [p["valence"] for p in points]
    return {
        "count": len(points),
        "intensity": {"min": min(inten), "max": max(inten),
                      "avg": round(sum(inten) / len(inten), 1)},
        "valence": {"min": min(val), "max": max(val),
                    "avg": round(sum(val) / len(val), 1)},
        "peak_ch": points[inten.index(max(inten))]["ch_idx"],
        "valley_ch": points[inten.index(min(inten))]["ch_idx"],
    }


def detect_low_flat(points: list[dict], *, low: int = 35, flat: int = 6,
                    gap: int = 12) -> list[dict]:
    """★ 情绪结构告警（B3 的实用价值所在）。

    检测项：
      1. **连续低潮**：连续 N 章 intensity < low → 读者会流失
      2. **长期无爆点**：超过 gap 章没有 intensity > 70 的章
      3. **情绪过山车**：相邻章强度跳变过大（>60）→ 突兀
    """
    issues: list[dict] = []
    if not points:
        return issues
    pts = sorted(points, key=lambda x: x["ch_idx"])

    # 1. 连续低潮
    run: list[int] = []
    for p in pts:
        if p["intensity"] < low:
            run.append(p["ch_idx"])
        else:
            if len(run) >= flat:
                issues.append({
                    "level": "warn", "kind": "low_flat",
                    "message": f"第 {run[0]}-{run[-1]} 章连续 {len(run)} 章情绪低平（<{low}）",
                    "hint": "插入冲突/反转/爆点，避免读者流失",
                    "chapters": run[:20]})
            run = []
    if len(run) >= flat:
        issues.append({"level": "warn", "kind": "low_flat",
                       "message": f"第 {run[0]}-{run[-1]} 章连续 {len(run)} 章情绪低平（<{low}）",
                       "hint": "插入冲突/反转/爆点", "chapters": run[:20]})

    # 2. 长期无爆点
    last_peak = 0
    for p in pts:
        if p["intensity"] > 70:
            last_peak = p["ch_idx"]
    if last_peak and pts and (pts[-1]["ch_idx"] - last_peak) > gap:
        issues.append({
            "level": "warn", "kind": "no_peak",
            "message": f"距上次高潮（第 {last_peak} 章）已 {pts[-1]['ch_idx'] - last_peak} 章无爆点",
            "hint": f"建议在 {last_peak + gap} 章前安排一次高潮",
            "last_peak_ch": last_peak})

    # 3. 跳变过陡
    for a, b in zip(pts, pts[1:]):
        if abs(b["intensity"] - a["intensity"]) > 60:
            issues.append({
                "level": "info", "kind": "sharp_jump",
                "message": f"第 {a['ch_idx']}→{b['ch_idx']} 章情绪强度跳变 "
                           f"{a['intensity']}→{b['intensity']}",
                "hint": "若是有意为之的爆点可忽略；否则建议加过渡章"})
    return issues


# ============================================================================
#  B4 伏笔自动提醒
# ============================================================================

def foreshadow_debt(f: Foreshadow, current_ch: int) -> dict:
    """★ 计算单个伏笔的"债务"（超期程度）。

    ★ 为什么叫"债务"：伏笔是作家对读者的**承诺**。
      埋下未兑现 = 欠债。欠得越久，读者越失望。
    """
    planted = int(f.planted_ch or 0)
    due = int(f.due_ch or 0)
    if not due and int(f.target_vol or 0):
        due = int(f.target_vol) * 30          # 无显式 due 时按卷推导
    if not due:
        due = planted + 20                    # 兜底：默认 20 章内回收
    overdue = max(0, int(current_ch) - due)
    pending = max(0, int(current_ch) - planted)
    # 分级
    if f.status == "done":
        level = "done"
    elif overdue > 15:
        level = "critical"
    elif overdue > 5:
        level = "high"
    elif overdue > 0:
        level = "due"
    elif pending > 30:
        level = "watch"
    else:
        level = "ok"
    return {"id": f.id, "content": f.content, "status": f.status,
            "planted_ch": planted, "due_ch": due, "pending": pending,
            "overdue": overdue, "level": level, "priority": f.priority,
            "note": f.note}


def due_foreshadows(db: Session, book_id: str, current_ch: int,
                    *, only_pending: bool = True) -> dict:
    """★ 当前章的伏笔债务清单（B4 核心）。

    ★ 这就是计划书里程碑里的"伏笔埋了20章没回收"的检测。

    ★ 复用既有实现（重要）：`consistency.obligations()` 已有成熟的分级逻辑
      （overdue_hard / overdue / due_soon）并已被上下文装配使用。
      **不重复造轮子** —— 两套分级逻辑并存必然漂移，那是维护灾难。
      本函数只做"全量视图"（obligations 只返回已进债务窗口的），
      供后台面板展示完整清单。
    """
    from apps.api import consistency as _legacy

    rows = db.scalars(select(Foreshadow)
                      .where(Foreshadow.book_id == book_id)).all()
    items = [foreshadow_debt(f, current_ch) for f in rows]
    if only_pending:
        items = [i for i in items if i["status"] != "done"]
    items.sort(key=lambda x: (-x["overdue"], -x["pending"]))

    # ★ 用既有 obligations 覆盖 level（保证与"注入 prompt 的分级"完全一致）
    try:
        ob_map = {o["id"]: o for o in _legacy.obligations(db, book_id, current_ch)}
        for i in items:
            ob = ob_map.get(i["id"])
            if ob:
                i["level"] = {"overdue_hard": "critical", "overdue": "high",
                              "due_soon": "due"}.get(ob["level"], i["level"])
                i["in_window"] = True
            else:
                i["in_window"] = False
    except Exception as e:                          # noqa: BLE001
        log.warning("复用 obligations 分级失败，用本地分级：%s", e)

    by_level: dict[str, int] = {}
    for i in items:
        by_level[i["level"]] = by_level.get(i["level"], 0) + 1
    return {"current_ch": current_ch, "items": items, "by_level": by_level,
            "urgent": [i for i in items if i["level"] in ("critical", "high")][:10]}


def render_foreshadow_hint(debt: dict, limit: int = 5) -> str:
    """把伏笔债务渲染成 prompt 提示（生成新章时注入）。"""
    urgent = [i for i in (debt.get("urgent") or [])][:limit]
    if not urgent:
        return ""
    lines = ["【待回收伏笔（请在本章或近期章节兑现）】"]
    for i in urgent:
        tail = f"（逾期 {i['overdue']} 章）" if i["overdue"] > 0 else f"（已埋 {i['pending']} 章）"
        lines.append(f"· 第 {i['planted_ch']} 章埋下：{i['content'][:60]}{tail}")
    return "\n".join(lines)


# ============================================================================
#  B6 信息差管理
# ============================================================================

def set_knowledge(db: Session, *, book_id: str, character_name: str,
                  fact_id: str, fact_desc: str = "", known: bool = True,
                  chapter_learned: int = 0, note: str = "") -> dict:
    """写入/更新角色对某事实的知情状态。"""
    c = db.scalar(select(Character).where(Character.book_id == book_id,
                                          Character.name == character_name))
    row = db.scalar(select(CharacterKnowledge).where(
        CharacterKnowledge.book_id == book_id,
        CharacterKnowledge.character_name == character_name,
        CharacterKnowledge.fact_id == fact_id))
    if row is None:
        row = CharacterKnowledge(
            id=_uid("know"), book_id=book_id,
            character_id=(c.id if c else ""), character_name=character_name,
            fact_id=fact_id, fact_desc=fact_desc, known=1 if known else 0,
            chapter_learned=int(chapter_learned), note=note, created_at=now())
        db.add(row)
    else:
        row.known = 1 if known else 0
        row.chapter_learned = int(chapter_learned)
        if fact_desc:
            row.fact_desc = fact_desc
        if note:
            row.note = note
    db.commit()
    return {"id": row.id, "character": character_name, "fact_id": fact_id,
            "known": bool(row.known), "chapter_learned": row.chapter_learned}


def knowledge_of(db: Session, book_id: str, character_name: str) -> list[dict]:
    rows = db.scalars(select(CharacterKnowledge).where(
        CharacterKnowledge.book_id == book_id,
        CharacterKnowledge.character_name == character_name)).all()
    return [{"fact_id": r.fact_id, "fact_desc": r.fact_desc,
             "known": bool(r.known), "chapter_learned": r.chapter_learned,
             "note": r.note} for r in rows]


def check_knowledge_violation(db: Session, book_id: str, ch_idx: int,
                              text: str,
                              char_names: list[str] | None = None) -> list[dict]:
    """★★ 信息差越界检测（计划书里程碑第一条）。

    逻辑：若某角色在**本章文本里提到了某事实的关键词**，
          但其 chapter_learned == 0 或 > ch_idx → 说明他"说了不该知道的事"。

    ★ 实现要点：用**关键词包含**而不是模型判定。
      确定性规则又快又准，且不会像模型那样"编造理由"。
      漏判靠人工补（可在 knowledge 表里补录事实）。
    """
    issues: list[dict] = []
    names = char_names or [c.name for c in db.scalars(
        select(Character).where(Character.book_id == book_id)).all()]
    if not text or not names:
        return issues

    # 取该书所有"有知情信息"的记录
    rows = db.scalars(select(CharacterKnowledge)
                      .where(CharacterKnowledge.book_id == book_id)).all()
    if not rows:
        return issues

    for r in rows:
        if r.character_name not in names:
            continue
        # 本章尚未得知（或得知章节晚于本章）→ 检查他是否"说漏了"
        learned = int(r.chapter_learned or 0)
        if r.known and learned and learned <= ch_idx:
            continue                        # 已合法得知，跳过

        # 从 fact_desc 里抽关键词（去停用词，取 2 字以上词）
        kws = _keywords(r.fact_desc or "")
        if not kws:
            continue
        # 角色名附近是否出现该事实关键词（±200 字窗口）
        for m in re.finditer(re.escape(r.character_name), text):
            lo, hi = max(0, m.start() - 200), min(len(text), m.end() + 200)
            window = text[lo:hi]
            hit = [k for k in kws if k in window]
            if len(hit) >= 2:               # 至少 2 个关键词同现才判（降误报）
                learned_txt = learned if learned else "?"
                issues.append({
                    "level": "error", "kind": "knowledge_violation",
                    "message": (f"「{r.character_name}」可能提及了他不该知道的事："
                                f"{r.fact_desc[:50]}"),
                    "hint": (f"该事实应在其第 {learned_txt} 章才得知，"
                             f"当前是第 {ch_idx} 章；若情节需要，请补写得知的过程"),
                    "character": r.character_name, "fact_id": r.fact_id,
                    "matched": hit[:5]})
                break
    return issues


_STOP = {"的", "了", "是", "在", "有", "和", "与", "被", "把", "对", "为",
         "一个", "这个", "那个", "自己", "什么", "已经", "可以", "因为"}


def _keywords(text: str, limit: int = 8) -> list[str]:
    """从描述里抽关键词（简易：2 字以上的中文片段）。"""
    toks = re.findall(r"[\u4e00-\u9fff]{2,}", text or "")
    out: list[str] = []
    seen = set()
    for t in toks:
        if t in _STOP or t in seen:
            continue
        # 长词再切 2-gram，提高匹配率
        if len(t) > 4:
            for i in range(0, len(t) - 1, 2):
                g = t[i:i + 2]
                if g not in _STOP and g not in seen:
                    seen.add(g)
                    out.append(g)
        else:
            seen.add(t)
            out.append(t)
    return out[:limit]


# ============================================================================
#  B5 角色资产库
# ============================================================================

def character_asset(db: Session, book_id: str, name: str) -> dict:
    """取角色资产（参考图 + 外观锁定）。"""
    c = db.scalar(select(Character).where(Character.book_id == book_id,
                                          Character.name == name))
    if c is None:
        return {}
    return {"id": c.id, "name": c.name, "role": c.role,
            "profile": c.profile, "voice": c.voice, "arc": c.arc,
            "state_now": c.state_now, "state_src_ch": c.state_src_ch,
            "aliases": _jl(c.aliases, []) or [],
            "ref_images": _jl(c.ref_images, []) or [],
            "appearance_locks": _jl(c.appearance_locks, {}) or {},
            "voice_profile": _jl(c.voice_profile, {}) or {}}


def set_character_asset(db: Session, book_id: str, name: str, *,
                        ref_images: list[str] | None = None,
                        appearance_locks: dict | None = None) -> dict:
    """更新角色资产（参考图 / 外观锁定）。"""
    c = db.scalar(select(Character).where(Character.book_id == book_id,
                                          Character.name == name))
    if c is None:
        return {"ok": False, "error": f"角色不存在：{name}"}
    if ref_images is not None:
        c.ref_images = _jd(list(ref_images)[:12])          # 最多 12 张
    if appearance_locks is not None:
        c.appearance_locks = _jd({k: str(v)[:200]
                                  for k, v in list(appearance_locks.items())[:20]})
    db.commit()
    return {"ok": True, "id": c.id, "name": c.name,
            "ref_images": _jl(c.ref_images, []) or [],
            "appearance_locks": _jl(c.appearance_locks, {}) or {}}


def appearance_prompt(db: Session, book_id: str, name: str) -> str:
    """★ 生成外观锁定片段（注入图像生成 prompt，解决"五官漂移"）。

    为什么需要文字锁定：纯靠参考图仍会漂（不同角度/光照/风格）。
    把关键特征写成**明确断言**同时注入，模型有了双重约束。
    """
    a = character_asset(db, book_id, name)
    locks = a.get("appearance_locks") or {}
    if not locks:
        return ""
    order = ["gender", "age", "hair", "eyes", "face", "skin", "build",
             "clothing", "accessory", "style"]
    parts = []
    for k in order:
        if k in locks:
            parts.append(f"{k}: {locks[k]}")
    for k, v in locks.items():
        if k not in order:
            parts.append(f"{k}: {v}")
    return ", ".join(parts)


def full_consistency_check(db: Session, book_id: str, ch_idx: int = 0,
                           text: str = "",
                           char_names: list[str] | None = None) -> dict:
    """★★ 全量一致性检查（汇总 B2-B6，供审计与"一键体检"用）。

    这是计划书里程碑的落地：「能自动发现...」的**统一入口**。
    """
    issues: list[dict] = []
    issues += detect_relation_conflict(db, book_id)
    debt = due_foreshadows(db, book_id, ch_idx)
    for i in debt.get("urgent") or []:
        issues.append({
            "level": "warn" if i["level"] == "high" else "error",
            "kind": "foreshadow_overdue",
            "message": f"伏笔「{i['content'][:40]}」已逾期 {i['overdue']} 章未回收",
            "hint": "在近期章节兑现，或调整 due_ch 延后",
            "foreshadow_id": i["id"]})
    arc = emotion_arc(db, book_id)
    issues += arc.get("issues") or []
    if text:
        issues += check_knowledge_violation(db, book_id, ch_idx, text, char_names)

    by_level: dict[str, int] = {}
    for i in issues:
        by_level[i["level"]] = by_level.get(i["level"], 0) + 1
    # 综合分（100 起扣，error -12 / warn -5 / info -1）
    score = max(0, 100 - by_level.get("error", 0) * 12
                - by_level.get("warn", 0) * 5 - by_level.get("info", 0) * 1)
    return {"book_id": book_id, "ch_idx": ch_idx, "score": score,
            "by_level": by_level, "issues": issues[:100],
            "foreshadow_debt": {"urgent": len(debt.get("urgent") or []),
                                "by_level": debt.get("by_level") or {}},
            "emotion_stats": arc.get("stats") or {}}

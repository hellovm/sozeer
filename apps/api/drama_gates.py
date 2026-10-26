# -*- coding: utf-8 -*-
"""短剧剧本质检闸门（v0.16 B2，评估报告缺口 4）。

背景
----
小说线的 `gates.py` 已经能拦「AI 味 / 重复度 / 模板化」，但**短剧线完全没有质检**：
剧本 JSON（分镜台词 + 镜头描述）直接进 TTS 与出图，AI 腔和模板化台词
一路带到成片。评估报告把这条列为缺口 4：

  "闸门下沉短剧线——AI味/重复度/模板化作用于剧本 JSON"

设计要点
--------
1. **不改 gates.py 的函数签名**（小说线零回归）。本模块复用其检查函数，
   把「剧本 JSON」先**拍平成台词文本流**，再喂给同一套检查器。
2. **台词与镜头描述分开检查**：
   * 台词（utterances[].text）→ AI 味、模板化、重复度、说书套话
   * 镜头描述（scene_description）→ 模板化（"镜头缓缓推进"式套话）
3. **返回结构化报告**，可挂到 DramaScene.qc_report 或现场告警，
   不影响生成主流程（质检失败绝不阻断）。
"""
from __future__ import annotations

import json
import re

from apps.api.orchestrator.gates import (
    GateReport,
    check_ai_flavor,
    check_repeat,
    check_templating,
)

# ----------------------------------------------------------------- 短剧专属词表
# 台词里的「AI 旁白腔」——小说里能忍，短剧里观众立刻出戏（没人这么说话）
SCREENPLAY_AI_TELLS = [
    "仿佛", "似乎", "不禁", "不由得", "不由自主", "心中暗道", "心想",
    "与此同时", "然而", "尽管如此", "总而言之", "不得不说", "值得一提的是",
    "他明白了一个道理", "她意识到", "这一刻", "时光荏苒", "岁月如梭",
]

# 说书套话（短剧台词出现即 error：观众要的是对白，不是话本）
SCREENPLAY_BOOKISH = [
    "欲知后事如何", "且听下回分解", "预知后事", "话分两头", "却说",
    "且说", "话休絮烦", "不在话下",
]

# 镜头描述模板化套话（每集都用就失去信息量）
SHOT_TEMPLATES = [
    "镜头缓缓推进", "镜头慢慢拉远", "镜头缓缓拉近", "画面渐渐", "镜头一转",
    "夕阳西下", "夜色渐浓", "时间飞逝", "画面一转", "此时画面",
]

# 台词长度纪律（短剧单句过长观众来不及反应；口播也不自然）
UTTERANCE_MAX_CHARS = 45
UTTERANCE_MIN_CHARS = 2


def _iter_utterances(scene_or_dict) -> list[dict]:
    """从 DramaScene 或 dict 里取出台词列表（容错：字段可能是 JSON 串）。"""
    raw = None
    if isinstance(scene_or_dict, dict):
        raw = scene_or_dict.get("utterances")
    else:
        raw = getattr(scene_or_dict, "utterances", None)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except Exception:
            return []
    if not isinstance(raw, list):
        return []
    out = []
    for u in raw:
        if isinstance(u, dict):
            out.append(u)
    return out


def _scene_text(scene_or_dict) -> str:
    if isinstance(scene_or_dict, dict):
        return scene_or_dict.get("scene_description") or ""
    return getattr(scene_or_dict, "scene_description", "") or ""


def _dialogue_lines(scene_or_dict) -> list[str]:
    """取剧本里的**纯台词行**（不含说话人前缀）。

    为什么单列这个函数：`check_templating` 是按「整句切分 + 统计前文出现次数」
    工作的。若把拍平文本写成 "A：我不信命"，切出来的整句就带上了说话人前缀，
    永远无法与前几镜的 "我不信命" 匹配 → 跨镜模板化**静默失效**
    （v0.16 实测踩坑）。所以模板化检查必须喂纯台词。
    """
    out = []
    for u in _iter_utterances(scene_or_dict):
        tx = (u.get("text") or "").strip()
        if tx:
            out.append(tx)
    return out


def _dialogue_text(scene_or_dict) -> str:
    """把一句句台词拍平成一个文本（带说话人前缀，供 AI 腔词表匹配用）。"""
    parts = []
    for u in _iter_utterances(scene_or_dict):
        sp = (u.get("speaker") or "").strip()
        tx = (u.get("text") or "").strip()
        if not tx:
            continue
        parts.append(f"{sp}：{tx}" if sp else tx)
    return "\n".join(parts)


# ----------------------------------------------------------------- 专属检查
def check_screenplay_ai_flavor(text: str, rep: GateReport) -> None:
    """短剧台词 AI 腔：旁白腔词 + 说书套话（小说能忍，短剧不能）。"""
    hits = [w for w in SCREENPLAY_AI_TELLS if w in (text or "")]
    bookish = [w for w in SCREENPLAY_BOOKISH if w in (text or "")]
    rep.stats["screenplay_ai_tells"] = len(hits)
    if hits:
        rep.issues.append(_issue(
            "短剧AI腔", "warn",
            f"台词含旁白腔表达 {len(hits)} 处：{'、'.join(hits[:6])}",
            fix_hint="改成角色当场会说的话，去掉叙述性心理描写"))
    if bookish:
        rep.issues.append(_issue(
            "说书套话", "error",
            f"台词含说书套话：{'、'.join(bookish[:4])}（短剧观众要的是对白）",
            fix_hint="删掉套话，用冲突或动作收尾"))


def check_utterance_length(scene_or_dict, rep: GateReport) -> None:
    """单句台词长度纪律：过长口播不自然，过短无信息量。"""
    longs, shorts = [], []
    for u in _iter_utterances(scene_or_dict):
        tx = (u.get("text") or "").strip()
        if not tx:
            continue
        if len(tx) > UTTERANCE_MAX_CHARS:
            longs.append(tx[:20] + "…")
        elif len(tx) < UTTERANCE_MIN_CHARS:
            shorts.append(tx)
    rep.stats["long_utterances"] = len(longs)
    if longs:
        rep.issues.append(_issue(
            "台词长度", "warn",
            f"{len(longs)} 句台词超过 {UTTERANCE_MAX_CHARS} 字（短剧口播吃力）",
            span=longs[0], fix_hint="拆成两三个短句，或改成动作"))
    if shorts:
        rep.issues.append(_issue(
            "台词长度", "warn", f"{len(shorts)} 句台词过短（信息量为零）",
            fix_hint="补足内容或删除"))


def check_shot_templates(scene_text: str, rep: GateReport, seen: list[str] | None = None) -> None:
    """镜头描述模板化：套话 + 与前镜重复。"""
    t = scene_text or ""
    hits = [w for w in SHOT_TEMPLATES if w in t]
    if hits:
        rep.issues.append(_issue(
            "镜头模板化", "warn",
            f"镜头描述含套话：{'、'.join(hits[:3])}",
            span=t[:60], fix_hint="写具体画面（谁在做什么、什么构图），别用通用转场词"))
    rep.stats["shot_template_hits"] = len(hits)


def check_shot_repeat(scene_text: str, prev_texts: list[str], rep: GateReport) -> None:
    """镜头描述跨镜重复（连续几镜写法雷同 → 视觉单调）。"""
    if not prev_texts or not (scene_text or "").strip():
        return
    try:
        check_repeat(scene_text, prev_texts, rep)
    except Exception:
        pass


def _issue(rule: str, level: str, message: str, span: str = "", fix_hint: str = ""):
    from apps.api.orchestrator.gates import GateIssue
    return GateIssue(rule, level, message, span=span, fix_hint=fix_hint)


# ----------------------------------------------------------------- 主入口
def run_screenplay_gates(scene_or_dict, *, prev_scene_texts: list[str] | None = None,
                         prev_dialogues: list[str] | None = None) -> GateReport:
    """对单个分镜跑短剧质检。返回 GateReport（不抛异常）。"""
    rep = GateReport()
    try:
        dlg = _dialogue_text(scene_or_dict)
        lines = _dialogue_lines(scene_or_dict)
        shot = _scene_text(scene_or_dict)
        # ① 台词 AI 腔 / 说书套话（用带说话人的整段文本，词表是子串匹配，无影响）
        if dlg:
            check_screenplay_ai_flavor(dlg, rep)
            # 复用小说线的 AI 味统计（句长变异/的字密度），但**不重复报"AI味"**，
            # 只把统计并入 stats，避免与短剧专属规则重复告警
            sub = GateReport()
            check_ai_flavor(dlg, sub)
            rep.stats.update({f"dlg_{k}": v for k, v in sub.stats.items()})
        # ② 台词模板化（★ 必须喂纯台词行，否则说话人前缀导致永远匹配不上）
        if lines:
            check_templating("\n".join(lines), prev_dialogues or [], rep)
        # ③ 台词长度纪律
        check_utterance_length(scene_or_dict, rep)
        # ④ 镜头描述模板化 + 重复
        check_shot_templates(shot, rep)
        check_shot_repeat(shot, prev_scene_texts or [], rep)
    except Exception as e:  # 质检失败绝不阻断生成
        rep.issues.append(_issue("质检异常", "warn", f"{type(e).__name__}: {e}"))
    return rep


def episode_qc(db, project, episode: int) -> dict:
    """整集质检：逐镜跑闸门并聚合。

    返回 {"ok", "scenes", "errors", "warns", "score", "issues", "by_scene"}。
    """
    from sqlalchemy import select as _sel
    from apps.api.models import DramaScene
    scenes = db.scalars(_sel(DramaScene).where(
        DramaScene.project_id == project.id,
        DramaScene.episode == int(episode)).order_by(DramaScene.idx)).all()
    if not scenes:
        return {"ok": False, "error": f"第{episode}集没有分镜",
                "scenes": 0, "errors": 0, "warns": 0, "score": 100,
                "issues": [], "by_scene": []}

    prev_shots: list[str] = []
    prev_dlgs: list[str] = []
    by_scene = []
    total_err = total_warn = 0
    for s in scenes:
        rep = run_screenplay_gates(s, prev_scene_texts=prev_shots[-5:],
                                   prev_dialogues=prev_dlgs[-5:])
        e = sum(1 for i in rep.issues if i.level == "error")
        w = sum(1 for i in rep.issues if i.level == "warn")
        total_err += e
        total_warn += w
        by_scene.append({
            "scene_id": s.scene_id, "idx": s.idx,
            "errors": e, "warns": w,
            "issues": [{"rule": i.rule, "level": i.level, "message": i.message,
                        "fix_hint": getattr(i, "fix_hint", "")} for i in rep.issues],
        })
        if (s.scene_description or "").strip():
            prev_shots.append(s.scene_description)
        lines = _dialogue_lines(s)          # ★ 纯台词（无说话人前缀），模板化才匹配得上
        if lines:
            prev_dlgs.append("\n".join(lines))

    n = len(scenes)
    score = max(0, 100 - total_err * 8 - total_warn * 3)
    return {"ok": True, "scenes": n, "errors": total_err, "warns": total_warn,
            "score": score, "issues": [], "by_scene": by_scene,
            "passed": total_err == 0}


__all__ = [
    "run_screenplay_gates", "episode_qc",
    "check_screenplay_ai_flavor", "check_utterance_length",
    "check_shot_templates", "SCREENPLAY_AI_TELLS", "SCREENPLAY_BOOKISH",
    "SHOT_TEMPLATES", "UTTERANCE_MAX_CHARS",
]

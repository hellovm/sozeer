# -*- coding: utf-8 -*-
"""v0.16 B2 回归：闸门下沉短剧线（评估报告缺口 4）。

缺口原文：
  "闸门下沉短剧线（AI味/重复度/模板化作用于剧本 JSON）"

背景：小说线 gates.py 已能拦 AI 味/重复度/模板化，但短剧剧本 JSON
（分镜台词 + 镜头描述）完全没有质检，AI 腔台词一路带到成片。

实现：apps/api/drama_gates.py
  * 台词（utterances[].text）→ 短剧AI腔 / 说书套话 / 台词长度 / 模板化
  * 镜头描述（scene_description）→ 模板化套话 / 跨镜重复
  * 整集聚合 episode_qc()
  * 全程容错，质检失败绝不阻断生成
"""
import json
import os

import pytest

os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-b2")


# ==================================================== 正常剧本
def test_clean_scene_passes():
    from apps.api import drama_gates as D
    scene = {
        "scene_description": "林晚推开锈迹斑斑的铁门，雨水顺着她发梢滴落。",
        "utterances": [
            {"speaker": "林晚", "text": "三个月了，你一次都没回过我消息。"},
            {"speaker": "陈默", "text": "有些事，说不了。"},
        ],
    }
    rep = D.run_screenplay_gates(scene)
    errors = [i for i in rep.issues if i.level == "error"]
    assert not errors, f"正常剧本不该有 error：{[i.message for i in errors]}"


# ==================================================== 短剧 AI 腔
def test_detects_narration_tone():
    """★ 台词里的旁白腔（小说能忍，短剧不能）。"""
    from apps.api import drama_gates as D
    scene = {"utterances": [
        {"speaker": "旁白", "text": "他心中暗道，这一刻仿佛时间静止了。"}]}
    rep = D.run_screenplay_gates(scene)
    rules = {i.rule for i in rep.issues}
    assert "短剧AI腔" in rules, f"未检出旁白腔：{rules}"
    assert rep.stats.get("screenplay_ai_tells", 0) >= 2


def test_detects_bookish_talk():
    """★ 说书套话是 error（观众要的是对白，不是话本）。"""
    from apps.api import drama_gates as D
    scene = {"utterances": [
        {"speaker": "说书人", "text": "欲知后事如何，且听下回分解。"}]}
    rep = D.run_screenplay_gates(scene)
    errs = [i for i in rep.issues if i.level == "error"]
    assert any(i.rule == "说书套话" for i in errs), \
        f"说书套话应报 error：{[(i.rule, i.level) for i in rep.issues]}"


def test_no_false_positive_on_normal_dialogue():
    from apps.api import drama_gates as D
    scene = {"utterances": [
        {"speaker": "A", "text": "你为什么要骗我？"},
        {"speaker": "B", "text": "因为我不想你卷进来。"}]}
    rep = D.run_screenplay_gates(scene)
    assert not any(i.rule == "短剧AI腔" for i in rep.issues)
    assert not any(i.rule == "说书套话" for i in rep.issues)


# ==================================================== 台词长度
def test_detects_long_utterance():
    from apps.api import drama_gates as D
    scene = {"utterances": [{"speaker": "A", "text": "我" * 60}]}
    rep = D.run_screenplay_gates(scene)
    assert any(i.rule == "台词长度" for i in rep.issues), "超长台词未检出"
    assert rep.stats.get("long_utterances") == 1


def test_length_threshold_boundary():
    from apps.api import drama_gates as D
    # 恰好等于阈值 → 不算超长
    scene = {"utterances": [{"speaker": "A", "text": "我" * D.UTTERANCE_MAX_CHARS}]}
    rep = D.run_screenplay_gates(scene)
    assert rep.stats.get("long_utterances") == 0
    # 超一个字 → 报
    scene2 = {"utterances": [{"speaker": "A", "text": "我" * (D.UTTERANCE_MAX_CHARS + 1)}]}
    rep2 = D.run_screenplay_gates(scene2)
    assert rep2.stats.get("long_utterances") == 1


# ==================================================== 镜头模板化
def test_detects_shot_template():
    from apps.api import drama_gates as D
    scene = {"scene_description": "镜头缓缓推进，夕阳西下。", "utterances": []}
    rep = D.run_screenplay_gates(scene)
    assert any(i.rule == "镜头模板化" for i in rep.issues), "镜头套话未检出"
    assert rep.stats.get("shot_template_hits", 0) >= 1


def test_specific_shot_passes():
    from apps.api import drama_gates as D
    scene = {"scene_description": "低角度仰拍，林晚踩过积水，倒影碎成三块。",
             "utterances": []}
    rep = D.run_screenplay_gates(scene)
    assert not any(i.rule == "镜头模板化" for i in rep.issues)


# ==================================================== 跨镜模板化
def test_cross_scene_templating():
    """★ 跨镜复现的短句 → 模板化告警（复用小说线 check_templating）。

    注意：check_templating 只检查 5-18 字的整句（过短不算模板），
    所以测试台词要 ≥5 字。同时验证「说话人前缀不能污染匹配」
    —— 这正是 v0.16 实测踩过的坑（拍平文本写成 "A：台词" 会永远匹配不上）。
    """
    from apps.api import drama_gates as D
    line = "我从来不相信命运"
    scene = {"utterances": [{"speaker": "A", "text": line}]}
    prev = [line, line]     # 前两镜出现过同一句纯台词
    rep = D.run_screenplay_gates(scene, prev_dialogues=prev)
    assert any(i.rule in ("模板化", "重复度") for i in rep.issues), \
        f"跨镜复用未检出：{[i.rule for i in rep.issues]}"


def test_dialogue_lines_strip_speaker():
    """★ 回归：_dialogue_lines 必须去掉说话人前缀（否则模板化静默失效）。"""
    from apps.api import drama_gates as D
    scene = {"utterances": [{"speaker": "林晚", "text": "我从来不相信命运"}]}
    lines = D._dialogue_lines(scene)
    assert lines == ["我从来不相信命运"], f"说话人前缀未剥离：{lines}"
    # 带前缀的拍平文本（AI 腔词表用）保留说话人
    txt = D._dialogue_text(scene)
    assert "林晚" in txt


# ==================================================== 容错（关键）
def test_never_crashes_on_dirty_input():
    """★ 质检失败绝不阻断生成：任意脏数据都要安全返回。"""
    from apps.api import drama_gates as D
    for bad in [None, {}, {"utterances": "不是json"}, {"utterances": None},
                {"utterances": [None, 123, "str"]}, 123, "字符串",
                {"scene_description": 12345}]:
        rep = D.run_screenplay_gates(bad)
        assert hasattr(rep, "issues"), f"入参 {bad!r} 未返回 GateReport"


def test_voiceover_kind_handled():
    """旁白/画外音（kind≠dialogue）也要纳入检查。"""
    from apps.api import drama_gates as D
    scene = {"utterances": [
        {"kind": "voiceover", "speaker": "旁白", "text": "总而言之，他输了。"}]}
    rep = D.run_screenplay_gates(scene)
    assert any(i.rule == "短剧AI腔" for i in rep.issues)


# ==================================================== 整集聚合
@pytest.fixture()
def db():
    from apps.api import main
    with main.SessionLocal() as s:
        yield s


def _mk_project(db, pid):
    from apps.api.models import Book, DramaProject, DramaScene
    from sqlalchemy import delete
    db.execute(delete(DramaScene).where(DramaScene.project_id == pid))
    db.execute(delete(DramaProject).where(DramaProject.id == pid))
    b = db.get(Book, "bk_b2")
    if not b:
        db.add(Book(id="bk_b2", title="B2书", owner_id="u1"))
        db.commit()
    db.add(DramaProject(id=pid, book_id="bk_b2", title="测试项目"))
    db.commit()
    return db.get(DramaProject, pid)


def test_episode_qc_aggregates(db):
    from apps.api import drama_gates as D
    from apps.api.models import DramaScene
    p = _mk_project(db, "prj_b2")
    scenes = [
        {"scene_id": "E1S01", "idx": 1,
         "scene_description": "林晚推开门，雨水打湿衣角。",
         "utterances": [{"speaker": "林晚", "text": "你迟到了。"}]},
        {"scene_id": "E1S02", "idx": 2,
         "scene_description": "镜头缓缓推进，夜色渐浓。",
         "utterances": [{"speaker": "说书人", "text": "欲知后事如何，且听下回分解。"}]},
    ]
    for sc in scenes:
        db.add(DramaScene(id=f"ds_{sc['scene_id']}", project_id=p.id, episode=1,
                          scene_id=sc["scene_id"], idx=sc["idx"],
                          scene_description=sc["scene_description"],
                          utterances=json.dumps(sc["utterances"], ensure_ascii=False)))
    db.commit()

    r = D.episode_qc(db, p, 1)
    assert r["ok"] and r["scenes"] == 2
    assert r["errors"] >= 1, f"应至少 1 个 error，实际 {r}"
    assert r["score"] < 100
    assert len(r["by_scene"]) == 2
    # 第2镜应报出说书套话
    s2 = next(x for x in r["by_scene"] if x["scene_id"] == "E1S02")
    assert any(i["rule"] == "说书套话" for i in s2["issues"])


def test_episode_qc_empty(db):
    from apps.api import drama_gates as D
    p = _mk_project(db, "prj_b2_empty")
    r = D.episode_qc(db, p, 99)
    assert r["ok"] is False and "没有分镜" in r["error"]


def test_episode_qc_score_monotonic(db):
    """error 越多分越低。"""
    from apps.api import drama_gates as D
    from apps.api.models import DramaScene
    p = _mk_project(db, "prj_b2_score")
    db.add(DramaScene(id="ds_ok", project_id=p.id, episode=1, scene_id="E1S01", idx=1,
                      scene_description="雨夜街头，霓虹倒影。",
                      utterances=json.dumps([{"speaker": "A", "text": "到了。"}],
                                            ensure_ascii=False)))
    db.commit()
    good = D.episode_qc(db, p, 1)["score"]
    db.add(DramaScene(id="ds_bad", project_id=p.id, episode=1, scene_id="E1S02", idx=2,
                      scene_description="镜头一转，时间飞逝。",
                      utterances=json.dumps([{"speaker": "说书人",
                                              "text": "却说那日，欲知后事如何。"}],
                                            ensure_ascii=False)))
    db.commit()
    bad = D.episode_qc(db, p, 1)["score"]
    assert bad < good, f"加坏镜后分数应下降：{good} → {bad}"


# ==================================================== API 端点
def test_qc_endpoint_defined():
    import inspect
    from apps.api import main
    assert hasattr(main, "drama_episode_qc")
    sig = inspect.signature(main.drama_episode_qc)
    assert "episode" in sig.parameters


def test_module_contract():
    from apps.api import drama_gates as D
    for name in ("run_screenplay_gates", "episode_qc", "SCREENPLAY_AI_TELLS",
                 "SCREENPLAY_BOOKISH", "SHOT_TEMPLATES"):
        assert hasattr(D, name), f"缺 {name}"

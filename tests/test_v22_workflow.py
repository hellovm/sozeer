"""v0.22 C 组创作工作流回归测试。

覆盖 5 项：
  C1 三级大纲（小说/卷/章 + 遵从度检查 + prompt 注入）
  C2 场景层（自动切分 + 三要素体检 + 情绪聚合）
  C4 去 AI 腔引擎（44 条规则 + 分级 + 命中率量化 + A/B 对比）
  C5 片段重拍（锚点快照 + 边界校验 + 隔离性）

★ 验收重点：
  · "三级大纲 + 场景层可用，旧数据兼容"
  · "去 AI 腔命中率可量化"
  · "短剧片段重拍不影响其他镜头一致性"
"""
from __future__ import annotations

import json
import uuid

import pytest

from apps.api import deai, retake, workflow
from apps.api.db import SessionLocal


def _mod(name: str):
    import sys
    import importlib
    return sys.modules.get(f"apps.api.{name}") or importlib.import_module(f"apps.api.{name}")


@pytest.fixture(scope="module", autouse=True)
def _ensure_tables():
    from apps.api import models  # noqa: F401
    from apps.api.db import Base, engine
    Base.metadata.create_all(engine)
    from apps.api.services import db_migrate
    db_migrate.migrate()
    yield


@pytest.fixture(autouse=True)
def _fresh():
    import importlib
    global workflow, deai, retake, SessionLocal
    workflow = _mod("workflow")
    deai = _mod("deai")
    retake = _mod("retake")
    SessionLocal = _mod("db").SessionLocal
    yield


def _mk_book(title: str = "C组测试书") -> str:
    from apps.api.models import Book
    bid = f"bk_{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db:
        db.add(Book(id=bid, title=title, author="a", owner_id="u_test"))
        db.commit()
    return bid


def _mk_chapter(book_id: str, idx: int, content: str = "") -> str:
    from apps.api.models import Chapter
    cid = f"cp_{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db:
        db.add(Chapter(id=cid, book_id=book_id, idx=idx,
                       title=f"第{idx}章", content=content,
                       words=len(content)))
        db.commit()
    return cid


# ============================================================================
#  C1 三级大纲
# ============================================================================

def test_outline_empty_book():
    """空书也应返回合法结构（旧数据兼容 —— 不能报错）。"""
    bid = _mk_book()
    with SessionLocal() as db:
        ol = workflow.get_outline(db, bid)
    assert ol["book_id"] == bid
    assert ol["novel"] == {}
    assert ol["novel_completeness"] == 0
    assert "fields" in ol and "volumes" in ol


def test_set_and_get_novel_outline():
    bid = _mk_book()
    with SessionLocal() as db:
        r = workflow.set_novel_outline(db, bid, {
            "logline": "少年为父报仇，却发现仇人是恩师",
            "theme": "仇恨与宽恕",
            "conflict": "复仇代价的自我怀疑",
        })
        assert r["ok"]
        assert r["completeness"] == int(3 / len(workflow.NOVEL_FIELDS) * 100)
        ol = workflow.get_outline(db, bid)
        assert ol["novel"]["theme"] == "仇恨与宽恕"
        assert ol["novel_completeness"] > 0


def test_novel_outline_partial_update():
    """部分更新不应清空未提供的字段。"""
    bid = _mk_book()
    with SessionLocal() as db:
        workflow.set_novel_outline(db, bid, {"logline": "A", "theme": "B"})
        workflow.set_novel_outline(db, bid, {"conflict": "C"})
        ol = workflow.get_outline(db, bid)
    assert ol["novel"]["logline"] == "A"
    assert ol["novel"]["theme"] == "B"
    assert ol["novel"]["conflict"] == "C"


def test_logline_syncs_to_synopsis():
    """logline 应同步到 synopsis（老接口/展示兼容）。"""
    from apps.api.models import Book
    bid = _mk_book()
    with SessionLocal() as db:
        workflow.set_novel_outline(db, bid, {"logline": "一句话概括"})
        b = db.get(Book, bid)
        assert b.synopsis == "一句话概括"


def test_volume_outline_creates_volume():
    """不存在的卷应自动创建。"""
    bid = _mk_book()
    with SessionLocal() as db:
        r = workflow.set_volume_outline(db, bid, 1, {
            "goal": "进入宗门", "conflict": "被排挤"}, name="初入宗门")
        assert r["ok"] and r["idx"] == 1
        ol = workflow.get_outline(db, bid)
    assert ol["stats"]["volumes"] == 1
    assert ol["volumes"][0]["name"] == "初入宗门"
    assert ol["volumes"][0]["meta"]["goal"] == "进入宗门"


def test_chapter_outline_creates_placeholder():
    """章节不存在时应建占位行（大纲先行，正文后补）。"""
    bid = _mk_book()
    with SessionLocal() as db:
        r = workflow.set_chapter_outline(db, bid, 5, {
            "purpose": "揭示身世", "twist": "师父是仇人"})
        assert r["ok"]
        ol = workflow.get_outline(db, bid)
    assert ol["stats"]["chapters"] == 1
    ch = ol["volumes"][0]["chapters"][0]
    assert ch["idx"] == 5
    assert ch["meta"]["twist"] == "师父是仇人"


def test_outline_for_prompt():
    """★ 大纲应渲染成 prompt 片段（写作时注入）。"""
    bid = _mk_book()
    with SessionLocal() as db:
        workflow.set_novel_outline(db, bid, {"logline": "少年复仇", "theme": "宽恕"})
        workflow.set_volume_outline(db, bid, 1, {"goal": "拜师"}, start_ch=1, end_ch=30)
        workflow.set_chapter_outline(db, bid, 5, {"purpose": "初遇", "hook": "师父露出破绽"})
        txt = workflow.outline_for_prompt(db, bid, 5)
    assert "全书定位" in txt
    assert "少年复仇" in txt
    assert "本卷定位" in txt
    assert "本章定位" in txt
    assert "师父露出破绽" in txt


def test_outline_for_prompt_empty():
    """无大纲时返回空串（不占 prompt 预算）。"""
    bid = _mk_book()
    with SessionLocal() as db:
        assert workflow.outline_for_prompt(db, bid, 1) == ""


def test_check_outline_alignment_missing_twist():
    """★ 大纲遵从度：设定的反转未在正文体现。"""
    bid = _mk_book()
    with SessionLocal() as db:
        workflow.set_chapter_outline(db, bid, 3, {"twist": "师兄突然背叛"})
        issues = workflow.check_outline_alignment(
            db, bid, 3, "他走进房间，坐下喝了杯茶，然后睡下了。")
    assert issues, "未检出大纲未体现"
    assert issues[0]["kind"] == "outline_miss"


def test_check_outline_alignment_ok():
    """正文体现了大纲则不报警（防误报）。"""
    bid = _mk_book()
    with SessionLocal() as db:
        workflow.set_chapter_outline(db, bid, 3, {"twist": "师兄突然背叛"})
        issues = workflow.check_outline_alignment(
            db, bid, 3, "师兄突然背叛了他，他愣在原地。")
    assert not issues, f"误报：{issues}"


def test_outline_old_data_compat():
    """★ 旧数据兼容：只有 synopsis/outline 纯文本的书不能报错。"""
    from apps.api.models import Chapter, Volume
    bid = _mk_book()
    cid = _mk_chapter(bid, 1, "正文" * 100)
    with SessionLocal() as db:
        # 老式数据：Volume.outline 是纯文本，无 outline_meta
        db.add(Volume(id=f"v_{uuid.uuid4().hex[:8]}", book_id=bid, idx=1,
                      name="第一卷", outline="这是一段纯文本大纲",
                      start_ch=1, end_ch=30))
        c = db.get(Chapter, cid)
        c.outline = "老的纯文本章节大纲"
        db.commit()
        ol = workflow.get_outline(db, bid)
    assert ol["volumes"][0]["outline"] == "这是一段纯文本大纲"
    assert ol["volumes"][0]["chapters"][0]["outline"] == "老的纯文本章节大纲"


# ============================================================================
#  C2 场景层
# ============================================================================

def test_split_scenes_by_separator():
    """★ 显式分隔符必须切分（尊重用户标记）。"""
    txt = ("第一场内容。" * 30) + "\n\n***\n\n" + ("第二场内容。" * 30)
    scenes = workflow.split_scenes(txt)
    assert len(scenes) == 2, f"分隔符未切分：{len(scenes)}"


def test_split_scenes_by_length():
    """无分隔符时按 max_chars 聚合（多个短段落应合并/拆分）。"""
    # ★ 构造 3 个独立段落，每段 200 字 → max_chars=300 时应产出多个场景
    txt = "\n\n".join(["甲内容。" * 50, "乙内容。" * 50, "丙内容。" * 50])
    scenes = workflow.split_scenes(txt, max_chars=300)
    assert len(scenes) > 1, f"未按长度拆分：{[s["words"] for s in scenes]}"


def test_split_scenes_empty():
    assert workflow.split_scenes("") == []
    assert workflow.split_scenes("   ") == []


def test_split_scenes_various_separators():
    for sep in ("***", "---", "===", "◆", "※", "★"):
        txt = ("甲内容。" * 30) + f"\n\n{sep}\n\n" + ("乙内容。" * 30)
        scenes = workflow.split_scenes(txt)
        assert len(scenes) == 2, f"分隔符 {sep} 未生效"


def test_sync_scenes_from_chapter():
    bid = _mk_book()
    _mk_chapter(bid, 1, ("标题。\n\n" + "内容甲。" * 40 + "\n\n***\n\n" + "内容乙。" * 40))
    with SessionLocal() as db:
        r = workflow.sync_scenes_from_chapter(db, bid, 1)
        assert r["ok"] and r["count"] >= 2
        items = workflow.list_scenes(db, bid, 1)
    assert len(items) >= 2
    assert all(s["source"] == "split" for s in items)


def test_sync_scenes_idempotent():
    """重复切分不产生重复场景。"""
    bid = _mk_book()
    _mk_chapter(bid, 1, "内容。" * 300)
    with SessionLocal() as db:
        r1 = workflow.sync_scenes_from_chapter(db, bid, 1)
        r2 = workflow.sync_scenes_from_chapter(db, bid, 1)
        items = workflow.list_scenes(db, bid, 1)
    assert r1["count"] == r2["count"] == len(items)


def test_sync_scenes_preserves_manual():
    """★ 已有手工场景时**不覆盖**（保护用户劳动）。"""
    bid = _mk_book()
    _mk_chapter(bid, 1, "内容。" * 300)
    with SessionLocal() as db:
        workflow.upsert_scene(db, bid, {"ch_idx": 1, "title": "手工场景",
                                        "goal": "手工目标", "source": "manual"})
        r = workflow.sync_scenes_from_chapter(db, bid, 1)
    assert r["ok"] is False, "手工场景被覆盖了"
    assert "手工场景" in str(r.get("scenes"))


def test_scene_upsert_and_diagnose():
    """★ 场景三要素体检。"""
    bid = _mk_book()
    with SessionLocal() as db:
        r = workflow.upsert_scene(db, bid, {
            "ch_idx": 1, "title": "初遇", "goal": "找到师父",
            "conflict": "被守卫拦住", "body": "内容" * 200})
        s = r["scene"]
        assert s["goal"] == "找到师父"
        # 缺 turn → 应有提示
        assert any(i["kind"] == "no_turn" for i in s["diagnosis"]["issues"])


def test_scene_diagnosis_clean():
    """三要素齐全应高分。"""
    bid = _mk_book()
    with SessionLocal() as db:
        r = workflow.upsert_scene(db, bid, {
            "ch_idx": 1, "goal": "g", "conflict": "c", "turn": "t",
            "body": "内容" * 200})
    assert r["scene"]["diagnosis"]["score"] == 100


def test_scene_emotion_clamped():
    bid = _mk_book()
    with SessionLocal() as db:
        r = workflow.upsert_scene(db, bid, {
            "ch_idx": 1, "emotion_intensity": 999, "emotion_valence": -999})
    s = r["scene"]
    assert s["emotion_intensity"] == 100 and s["emotion_valence"] == -100


def test_scenes_to_emotion():
    """★ 场景情绪聚合到章节（与 B3 打通）。"""
    bid = _mk_book()
    with SessionLocal() as db:
        workflow.upsert_scene(db, bid, {"ch_idx": 1, "emotion_intensity": 30,
                                        "emotion_valence": -20, "body": "а" * 100})
        workflow.upsert_scene(db, bid, {"ch_idx": 1, "emotion_intensity": 90,
                                        "emotion_valence": 60, "body": "б" * 100})
        r = workflow.scenes_to_emotion(db, bid, 1)
    assert r.get("ok"), r
    assert r["intensity"] == 90          # 取最大（高潮决定张力）


def test_scene_delete():
    bid = _mk_book()
    with SessionLocal() as db:
        sid = workflow.upsert_scene(db, bid, {"ch_idx": 1, "title": "x"})["scene"]["id"]
        assert workflow.delete_scene(db, bid, sid)["ok"]
        assert workflow.list_scenes(db, bid) == []


def test_workflow_stats():
    bid = _mk_book()
    with SessionLocal() as db:
        workflow.set_novel_outline(db, bid, {"logline": "x"})
        workflow.upsert_scene(db, bid, {"ch_idx": 1, "title": "a"})
        st = workflow.workflow_stats(db, bid)
    assert "outline" in st and "scenes" in st
    assert st["outline"]["novel_completeness"] > 0
    assert st["scenes"]["total"] == 1
    assert st["scenes"]["missing_goal"] == 1


# ============================================================================
#  C4 去 AI 腔引擎
# ============================================================================

def test_rule_count_40_plus():
    """★ 验收要求：40+ 类规则。"""
    cat = deai.rule_catalog()
    assert cat["total"] >= 40, f"规则数不足：{cat['total']}"


def test_rules_have_levels_and_advice():
    """每条规则必须有分级与改写建议（只报问题不给方案 = 无用）。"""
    for r in deai.RULES:
        assert r.level in ("error", "warn", "info")
        assert r.advice, f"规则 {r.key} 缺少改写建议"
        assert r.pattern


def test_detect_cliche():
    """★ 陈词滥调必须被检出。"""
    rep = deai.analyze("命运的齿轮开始转动，他嘴角勾起一抹冷笑，目光一闪。")
    labels = {h.rule.label for h in rep.hits}
    assert "命运齿轮" in labels
    assert "笑容套话" in labels
    assert "眼神套话" in labels


def test_detect_dialog_tags():
    """★ 副词+说道（error 级）。"""
    rep = deai.analyze("他愤怒地说道：够了。她悲伤地回答说：不要。")
    labels = {h.rule.label for h in rep.hits}
    assert "解释性标签" in labels or "副词 + 说道" in labels


def test_detect_emotional_telling():
    """★ 直陈情绪（AI 最典型特征）。"""
    rep = deai.analyze("他很愤怒，她非常高兴，我十分紧张。" * 3)
    assert any(h.rule.key == "emo_tell" for h in rep.hits)


def test_clean_text_high_score():
    """★ 干净文本应高分（防误报）。"""
    clean = ("他推开门。风灌进来，桌上的纸散了一地。\n"
             "“来了？”老周没抬头，手里还捏着半块饼。\n"
             "“来了。”他在门槛上坐下，把鞋脱了。")
    rep = deai.analyze(clean)
    assert rep.score >= 85, f"干净文本被扣分：{rep.score}，命中 {[(h.rule.label) for h in rep.hits]}"


def test_ai_text_low_score():
    """★ AI 腔文本应低分。"""
    ai_text = ("命运的齿轮开始转动。他嘴角勾起一抹冷笑，目光深邃。\n"
               "空气中弥漫着压抑的气息。他感到一阵愤怒，浑身一震。\n"
               "不仅危险而且刺激，既紧张又快意。他愤怒地说道：够了。")
    rep = deai.analyze(ai_text)
    assert rep.score < 70, f"AI 文本分数过高：{rep.score}"


def test_density_normalization():
    """★ 密度归一化：长文本不应因为"长"而被扣更多分。"""
    short = "命运的齿轮开始转动。" * 2
    long = "命运的齿轮开始转动。" * 2 + "他走了很久，天黑了。" * 200
    r1 = deai.analyze(short)
    r2 = deai.analyze(long)
    # 长文本密度更低 → 分数应更高或相当
    assert r2.score >= r1.score, f"长文本被误扣：{r1.score} → {r2.score}"


def test_hit_stats_quantified():
    """★ 验收要求"命中率可量化"。"""
    rep = deai.analyze("命运的齿轮开始转动。" * 3)
    st = deai.hit_stats(rep)
    for k in ("score", "kinds", "total", "per_1k", "top"):
        assert k in st
    assert st["per_1k"] > 0


def test_rewrite_suggestions_ordered():
    """建议按严重度排序（error 优先）。"""
    rep = deai.analyze("命运的齿轮开始转动。他愤怒地说道：走。于是他们走了。")
    sug = deai.rewrite_suggestions(rep)
    assert sug
    levels = [s["level"] for s in sug]
    order = {"error": 0, "warn": 1, "info": 2}
    assert levels == sorted(levels, key=lambda x: order[x]), f"未按严重度排序：{levels}"


def test_prompt_for_rewrite():
    """★ 生成 LLM 改写指令（规则层 → LLM 层的桥）。"""
    src = "命运的齿轮开始转动。他嘴角勾起一抹冷笑。"
    rep = deai.analyze(src)
    p = deai.prompt_for_rewrite(src, rep)
    assert "改写" in p
    assert "原文：" in p
    assert len(p) > 50


def test_prompt_for_rewrite_empty():
    src = "他推开门，走了进去。"
    rep = deai.analyze(src)
    if rep.score >= 100:
        assert deai.prompt_for_rewrite(src, rep) == ""


def test_compare_ab():
    """★ A/B 对比（验收要求"产出质量优于单阶段"可验证）。"""
    before = "命运的齿轮开始转动。他嘴角勾起一抹冷笑，目光深邃。他愤怒地说道：走。"
    after = "他推开门。外面在下雨。\n“走。”他说。"
    c = deai.compare(before, after)
    assert c["improvement"]["score_delta"] > 0, f"改写后未改善：{c}"
    assert "before" in c and "after" in c


def test_verdict_mapping():
    """分数到评价的映射。"""
    for score, want in [(90, "优秀"), (75, "良好"), (60, "一般"), (30, "需改写")]:
        rep = deai.Report(score=score)
        assert rep.to_dict()["verdict"] == want


def test_empty_text_no_error():
    rep = deai.analyze("")
    assert rep.score == 100
    assert rep.hits == []


# ============================================================================
#  C5 片段重拍
# ============================================================================

def _mk_drama_scene(idx: int = 1, **kw):
    """建短剧项目 + 场景。"""
    from apps.api.models import DramaProject, DramaScene
    bid = f"bk_{uuid.uuid4().hex[:10]}"
    pid = f"dp_{uuid.uuid4().hex[:10]}"
    sid = f"ds_{uuid.uuid4().hex[:10]}"
    with SessionLocal() as db:
        db.add(DramaProject(id=pid, book_id=bid, title="重拍测试"))
        s = DramaScene(id=sid, project_id=pid, episode=1, scene_id=f"E1S{idx:02d}",
                       idx=idx, duration_seconds=8,
                       scene_description=kw.get("desc", "原描述"),
                       image_prompt=kw.get("ip", "原图片prompt"),
                       video_prompt=kw.get("vp", "原视频prompt"),
                       characters_in_scene=json.dumps(kw.get("chars", ["甲"]),
                                                      ensure_ascii=False))
        db.add(s)
        db.commit()
        return sid, pid, bid


def test_snapshot_scene():
    """★ 快照必须包含全部关键字段。"""
    sid, pid, bid = _mk_drama_scene()
    with SessionLocal() as db:
        from apps.api.models import DramaScene
        s = db.get(DramaScene, sid)
        snap = retake.snapshot_scene(s)
    for k in ("scene_id", "idx", "image_prompt", "video_prompt", "characters",
              "duration", "fingerprint", "snapshot_at"):
        assert k in snap, f"快照缺少 {k}"


def test_fingerprint_changes_on_edit():
    """★ 指纹应随内容变化（用于判断"改了什么"）。"""
    sid, pid, bid = _mk_drama_scene()
    with SessionLocal() as db:
        from apps.api.models import DramaScene
        s = db.get(DramaScene, sid)
        f1 = retake.snapshot_scene(s)["fingerprint"]
        s.image_prompt = "改了"
        f2 = retake.snapshot_scene(s)["fingerprint"]
    assert f1 != f2


def test_diff_snapshot():
    sid, pid, bid = _mk_drama_scene()
    with SessionLocal() as db:
        from apps.api.models import DramaScene
        s = db.get(DramaScene, sid)
        before = retake.snapshot_scene(s)
        s.image_prompt = "新 prompt"
        s.duration_seconds = 12
        after = retake.snapshot_scene(s)
    d = retake.diff_snapshot(before, after)
    assert "image_prompt" in d["changed_fields"]
    assert "duration" in d["changed_fields"]
    assert d["same_content"] is False


def test_prepare_retake_does_not_mutate():
    """★★ 准备阶段**不得修改数据**（关键安全保证）。"""
    sid, pid, bid = _mk_drama_scene(ip="原始图片prompt")
    with SessionLocal() as db:
        from apps.api.models import DramaProject, DramaScene
        s = db.get(DramaScene, sid)
        p = db.get(DramaProject, pid)
        retake.prepare_retake(db, p, s, new_image_prompt="新 prompt")
        db.refresh(s)
        assert s.image_prompt == "原始图片prompt", "★ 准备阶段修改了数据！"


def test_prepare_retake_reports_changes():
    sid, pid, bid = _mk_drama_scene()
    with SessionLocal() as db:
        from apps.api.models import DramaProject, DramaScene
        s = db.get(DramaScene, sid)
        p = db.get(DramaProject, pid)
        plan = retake.prepare_retake(db, p, s, new_image_prompt="改这里")
    assert "image" in plan["will_regenerate"]
    assert plan["before"]["image_prompt"] == "原图片prompt"
    assert plan["image_prompt"] == "改这里"


def test_apply_retake_only_touches_one_scene():
    """★★★ C5 核心承诺：重拍**只影响一个镜头**。"""
    from apps.api.models import DramaProject, DramaScene
    bid = f"bk_{uuid.uuid4().hex[:10]}"
    pid = f"dp_{uuid.uuid4().hex[:10]}"
    with SessionLocal() as db:
        db.add(DramaProject(id=pid, book_id=bid, title="隔离测试"))
        for i in (1, 2, 3):
            db.add(DramaScene(id=f"ds{i}_{uuid.uuid4().hex[:6]}", project_id=pid,
                              episode=1, scene_id=f"E1S{i:02d}", idx=i,
                              image_prompt=f"镜头{i}的原prompt",
                              media_status="done", image_path=f"/api/files/x{i}.png"))
        db.commit()
        scenes = db.query(DramaScene).filter_by(project_id=pid)\
            .order_by(DramaScene.idx).all()
        target = scenes[1]                    # 改第 2 个
        others_before = [(s.id, s.image_prompt, s.media_status, s.image_path)
                         for s in scenes if s.id != target.id]

        p = db.get(DramaProject, pid)
        retake.apply_retake(db, p, target, new_image_prompt="镜头2的新prompt")

        scenes_after = db.query(DramaScene).filter_by(project_id=pid)\
            .order_by(DramaScene.idx).all()
        others_after = [(s.id, s.image_prompt, s.media_status, s.image_path)
                        for s in scenes_after if s.id != target.id]
        t = db.get(DramaScene, target.id)

    assert others_before == others_after, "★ 其他镜头被改动了！"
    assert t.image_prompt == "镜头2的新prompt"
    assert t.media_status == "none"           # 产物已清空待重生成
    assert t.image_path == ""


def test_apply_retake_keeps_other_fields():
    """重拍清产物但不该清 characters/duration。"""
    sid, pid, bid = _mk_drama_scene(chars=["甲", "乙"])
    with SessionLocal() as db:
        from apps.api.models import DramaProject, DramaScene
        s = db.get(DramaScene, sid)
        p = db.get(DramaProject, pid)
        retake.apply_retake(db, p, s, new_image_prompt="x")
        s2 = db.get(DramaScene, sid)
    assert json.loads(s2.characters_in_scene) == ["甲", "乙"]
    assert s2.duration_seconds == 8


def test_check_boundary_detects_character_loss():
    """★ 边界校验：原角色消失应告警。"""
    sid, pid, bid = _mk_drama_scene(chars=["甲", "乙"])
    with SessionLocal() as db:
        from apps.api.models import DramaScene
        s = db.get(DramaScene, sid)
        before = retake.snapshot_scene(s)
        s.characters_in_scene = json.dumps(["甲"], ensure_ascii=False)
        issues = retake.check_boundary(db, s, before_snapshot=before)
    assert any(i["kind"] == "character_lost" for i in issues), f"未检出角色丢失：{issues}"


def test_check_boundary_detects_duration_drift():
    sid, pid, bid = _mk_drama_scene()
    with SessionLocal() as db:
        from apps.api.models import DramaScene
        s = db.get(DramaScene, sid)
        before = retake.snapshot_scene(s)
        s.duration_seconds = 20            # 8 → 20（+150%）
        issues = retake.check_boundary(db, s, before_snapshot=before)
    assert any(i["kind"] == "duration_drift" for i in issues)


def test_build_anchors_no_crash_without_data():
    """无锚点数据时不应崩（降级为空）。"""
    sid, pid, bid = _mk_drama_scene()
    with SessionLocal() as db:
        from apps.api.models import DramaProject, DramaScene
        s = db.get(DramaScene, sid)
        p = db.get(DramaProject, pid)
        a = retake.build_anchors(db, p, s)
    assert "prev_end_frame" in a
    assert "ref_images" in a
    assert "appearance_locks" in a


def test_retake_prompt_includes_appearance_locks():
    """★ 重拍 prompt 应注入外观锁（B5 一致性）。"""
    from apps.api.models import Character, DramaProject, DramaScene
    bid = f"bk_{uuid.uuid4().hex[:10]}"
    pid = f"dp_{uuid.uuid4().hex[:10]}"
    sid = f"ds_{uuid.uuid4().hex[:10]}"
    with SessionLocal() as db:
        db.add(Character(id=f"ch_{uuid.uuid4().hex[:8]}", book_id=bid,
                         name="甲", appearance_locks=json.dumps(
                             {"hair": "银白长发", "eyes": "碧色"}, ensure_ascii=False)))
        db.add(DramaProject(id=pid, book_id=bid, title="x"))
        db.add(DramaScene(id=sid, project_id=pid, episode=1, idx=1,
                          image_prompt="基础prompt",
                          characters_in_scene=json.dumps(["甲"], ensure_ascii=False)))
        db.commit()
        s = db.get(DramaScene, sid)
        p = db.get(DramaProject, pid)
        plan = retake.build_retake_prompt(db, p, s)
    assert "银白长发" in plan["image_prompt"] or plan["anchors"]["appearance_locks"], \
        "外观锁未注入"


def test_batch_retake_adjacent_warning():
    """★ 批量重拍含相邻镜头应提示衔接风险。"""
    from apps.api.models import DramaProject, DramaScene
    bid = f"bk_{uuid.uuid4().hex[:10]}"
    pid = f"dp_{uuid.uuid4().hex[:10]}"
    with SessionLocal() as db:
        db.add(DramaProject(id=pid, book_id=bid, title="批量"))
        for i in (1, 2, 3, 4):
            db.add(DramaScene(id=f"ds{i}_{uuid.uuid4().hex[:6]}", project_id=pid,
                              episode=1, idx=i))
        db.commit()
        p = db.get(DramaProject, pid)
        r = retake.prepare_batch_retake(db, p, 1, [2, 3])
    assert r["ok"]
    assert r["adjacent_pairs"] == [(2, 3)]
    assert r["warnings"], "相邻镜头未提示"
    assert "未选中的镜头不会改动" in r["isolation"]


def test_batch_retake_missing_scene():
    from apps.api.models import DramaProject
    bid = f"bk_{uuid.uuid4().hex[:10]}"
    pid = f"dp_{uuid.uuid4().hex[:10]}"
    with SessionLocal() as db:
        db.add(DramaProject(id=pid, book_id=bid, title="x"))
        db.commit()
        p = db.get(DramaProject, pid)
        r = retake.prepare_batch_retake(db, p, 1, [99])
    assert r["ok"] is False


# ============================================================================
#  端到端：HTTP 端点
# ============================================================================

def test_http_outline_and_scenes():
    """HTTP 层可用性 + RBAC 保护。"""
    from fastapi.testclient import TestClient
    import sys
    m = _mod("main")
    a = _mod("auth")
    uname = f"wrk_{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        u = a.create_user(db, uname, "wrk-test-12345", role="admin")
        db.commit()
        tok = a.make_token(u.id)
    bid = _mk_book()
    _mk_chapter(bid, 1, "内容。" * 200)
    h = {"Authorization": f"Bearer {tok}"}
    with TestClient(m.app) as c:
        r = c.get(f"/api/books/{bid}/outline", headers=h)
        assert r.status_code == 200, r.text
        r2 = c.put(f"/api/books/{bid}/outline/novel",
                   json={"logline": "测试"}, headers=h)
        assert r2.status_code == 200
        r3 = c.post(f"/api/books/{bid}/scenes/split",
                    json={"ch_idx": 1}, headers=h)
        assert r3.status_code == 200
        r4 = c.get(f"/api/books/{bid}/scenes", headers=h)
        assert r4.status_code == 200
        r5 = c.post("/api/deai/analyze", json={"text": "命运的齿轮开始转动。"}, headers=h)
        assert r5.status_code == 200
        assert r5.json()["score"] < 100
        r6 = c.get("/api/deai/rules", headers=h)
        assert r6.json()["total"] >= 40
        # ★ 回归守卫：rewrite-prompt 曾因漏传 text 参数 500（单测直接调模块函数没覆盖到）
        r7 = c.post("/api/deai/rewrite-prompt",
                    json={"text": "命运的齿轮开始转动。他嘴角勾起一抹冷笑。"},
                    headers=h)
        assert r7.status_code == 200, f"rewrite-prompt 报错：{r7.text[:300]}"
        assert "改写" in r7.json().get("prompt", "")


def test_http_requires_auth():
    """未登录必须 401。"""
    from fastapi.testclient import TestClient
    m = _mod("main")
    bid = _mk_book()
    with TestClient(m.app) as c:
        for path in (f"/api/books/{bid}/outline", f"/api/books/{bid}/scenes",
                     "/api/deai/rules"):
            assert c.get(path).status_code == 401, path

# -*- coding: utf-8 -*-
"""v0.15 G5 回归：gates 实力体系闸门题材解耦。

核心验收（计划书 §五 G5）：
  * 仙侠书境界闸门行为**不变**（签名冻结、9 个调用点零改动）
  * 都市书读「职级」维度
  * 悬疑书 realm_dim='' → 闸门自动跳过、不误报
  * 停用词按题材取（都市文的"阶级/级别"不该被当境界报错）
"""
import os

import pytest

os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-g5")


@pytest.fixture()
def db():
    from apps.api import main
    import apps.api.genres as genres
    genres.invalidate_cache()
    with main.SessionLocal() as s:
        yield s
    genres.invalidate_cache()


def _mk_book_with_world(db, bid, theme, worlds):
    """建书 + 写世界观条目。worlds = [(category, key), ...]"""
    from apps.api.models import Book, WorldSetting
    from sqlalchemy import delete
    db.execute(delete(WorldSetting).where(WorldSetting.book_id == bid))
    db.execute(delete(Book).where(Book.id == bid))
    db.commit()
    db.add(Book(id=bid, title="测试书", theme=theme, owner_id="u1"))
    db.commit()
    for i, (cat, key) in enumerate(worlds):
        db.add(WorldSetting(id=f"ws_{bid}_{i}", book_id=bid, category=cat,
                            key=key, value=""))
    db.commit()
    return db.get(Book, bid)


# ==================================================== 签名冻结（9 调用点零改动）
def test_signature_frozen():
    """★ realm_whitelist_for 必须保持 (db, book_id) 签名——9 个调用点依赖它。"""
    import inspect
    from apps.api.orchestrator import gates
    sig = inspect.signature(gates.realm_whitelist_for)
    params = list(sig.parameters)
    assert params == ["db", "book_id"], f"签名被改了：{params}"


def test_check_realms_accepts_original_arity():
    """check_realms 前 3 个位置参数必须与改前一致（新增的 stops 只能是可选）。"""
    import inspect
    from apps.api.orchestrator import gates
    sig = inspect.signature(gates.check_realms)
    params = list(sig.parameters)
    assert params[:3] == ["body", "whitelist", "rep"]
    assert sig.parameters["stops"].default is None, "stops 必须是可选参数"


# ==================================================== 仙侠：行为不变
def test_xianxia_whitelist_reads_jingjie(db):
    """仙侠书读 category=='境界'（与改前一致）。"""
    from apps.api.orchestrator import gates
    b = _mk_book_with_world(db, "bk_g5_x", "xianxia",
                            [("境界", "练气境"), ("境界", "筑基境"), ("势力", "青云宗")])
    wl = gates.realm_whitelist_for(db, b.id)
    assert "练气境" in wl and "筑基境" in wl
    assert "青云宗" not in wl, "势力不该进境界白名单"


def test_xianxia_gate_still_reports_out_of_system(db):
    """★ 零回归：仙侠书的体系外境界仍被抓。"""
    from apps.api.orchestrator import gates
    b = _mk_book_with_world(db, "bk_g5_x2", "xianxia", [("境界", "练气境")])
    wl = gates.realm_whitelist_for(db, b.id)
    rep = gates.GateReport()
    gates.check_realms("他突破到了大乘境。", wl, rep)
    assert any(i.rule == "境界" for i in rep.issues), "体系外境界未被抓"


def test_xianxia_stop_words_still_apply(db):
    """环境/心境这类词仍被放行（默认停用词未丢）。"""
    from apps.api.orchestrator import gates
    b = _mk_book_with_world(db, "bk_g5_x3", "xianxia", [("境界", "练气境")])
    wl = gates.realm_whitelist_for(db, b.id)
    rep = gates.GateReport()
    gates.check_realms("他心境平和，走进秘境。", wl, rep)
    assert not rep.issues, f"停用词失效：{[i.message for i in rep.issues]}"


# ==================================================== 都市：读「职级」
def test_urban_whitelist_reads_zhiji(db):
    from apps.api.orchestrator import gates
    b = _mk_book_with_world(db, "bk_g5_u", "urban",
                            [("职级", "一级"), ("职级", "二级"), ("境界", "练气境")])
    wl = gates.realm_whitelist_for(db, b.id)
    assert "一级" in wl and "二级" in wl
    assert "练气境" not in wl, "都市书不该读『境界』分类"


def test_urban_no_world_setting_returns_empty(db):
    """都市书没录职级设定 → 返回空 → 闸门自动关闭（不瞎报）。"""
    from apps.api.orchestrator import gates
    b = _mk_book_with_world(db, "bk_g5_u2", "urban", [])
    assert gates.realm_whitelist_for(db, b.id) == []


# ==================================================== 悬疑：闸门自动跳过
def test_suspense_realm_dim_empty_skips_gate(db):
    """★ 悬疑书 realm_dim='' → 白名单恒为空 → 闸门跳过（不误报）。"""
    from apps.api.orchestrator import gates
    b = _mk_book_with_world(db, "bk_g5_s", "urban_suspense",
                            [("线索", "红绳"), ("现场", "密室")])
    wl = gates.realm_whitelist_for(db, b.id)
    assert wl == [], "悬疑无实力体系，白名单必须为空"

    rep = gates.GateReport()
    gates.check_realms("他感觉陷入了绝境。", wl, rep)
    assert not rep.issues, "空白名单时闸门不该报任何问题"


# ==================================================== 停用词按题材
def test_stop_words_include_theme_extras(db):
    """都市题材的停用词（阶级/级别/等级）应并入。"""
    from apps.api.orchestrator import gates
    b = _mk_book_with_world(db, "bk_g5_stop", "urban", [])
    stops = gates.stop_realms_for(db, b.id)
    assert "阶级" in stops or "级别" in stops or "等级" in stops
    # 默认集仍保留
    assert "环境" in stops


def test_stop_words_unknown_theme_falls_back(db):
    """未知题材 → 只返回默认集（不报错）。"""
    from apps.api.orchestrator import gates
    b = _mk_book_with_world(db, "bk_g5_unk", "不存在的题材", [])
    stops = gates.stop_realms_for(db, b.id)
    assert "环境" in stops


def test_stop_realms_for_handles_none_db():
    """db=None 时不该崩（run_gates 拿不到会话的路径）。"""
    from apps.api.orchestrator import gates
    stops = gates.stop_realms_for(None, "不存在的书")
    assert isinstance(stops, set) and stops


# ==================================================== run_gates 端到端不崩
def test_run_gates_urban_book_no_crash(db):
    """★ 都市书跑 run_gates 全流程不崩，且不误报境界问题。"""
    from apps.api.orchestrator import gates
    from apps.api.models import StyleProfile
    b = _mk_book_with_world(db, "bk_g5_run", "urban", [])
    sp = db.query(StyleProfile).filter(StyleProfile.book_id == b.id).first()
    body = "# 第1章 开局\n\n他走进写字楼，月薪三万块。\n"
    rep = gates.run_gates(body, b, sp, [], realm_whitelist=gates.realm_whitelist_for(db, b.id))
    assert rep is not None
    assert not any(i.rule == "境界" for i in rep.issues), "都市书不该报境界问题"


def test_realm_dim_for_unknown_falls_back_to_jingjie():
    """未命中题材 → realm_dim 回退 '境界'（老书零影响）。"""
    from apps.api import genres
    assert genres.realm_dim_for(None) == "境界"
    assert genres.realm_dim_for("未知") == "境界"
    assert genres.realm_dim_for("urban_suspense") == ""


# ==================================================== v0.15 里程碑补充（E2E 发现）
def test_urban_zhiji_names_pass_validator(db):
    """★ E2E 发现的真 bug：都市『职级』名（见习专员/总监/总裁）不含
    境/阶/丹/婴 等仙侠后缀，原 looks_like_realm_name 会全部误杀 →
    闸门白名单恒空。修复：非『境界』维度放宽为干净度校验。
    """
    from apps.api.orchestrator import gates
    b = _mk_book_with_world(db, "bk_g5_zhiji", "urban",
                            [("职级", "见习专员"), ("职级", "主管"), ("职级", "总裁")])
    wl = gates.realm_whitelist_for(db, b.id)
    assert "见习专员" in wl, f"都市职级名被误杀：{wl}"
    assert "主管" in wl and "总裁" in wl


def test_xianxia_validator_still_strict(db):
    """★ 零回归：仙侠维度的严格后缀校验不变（防大纲条目混入）。"""
    from apps.api.orchestrator import gates
    b = _mk_book_with_world(db, "bk_g5_strict", "xianxia",
                            [("境界", "练气境"), ("境界", "这是一段大纲描述文字")]
                            )
    wl = gates.realm_whitelist_for(db, b.id)
    assert "练气境" in wl
    assert "这是一段大纲描述文字" not in wl, "超长/无后缀项应被过滤"


def test_validator_signature_backward_compatible():
    """looks_like_realm_name 第 2 参数 dim 必须可选（老调用零改动）。"""
    import inspect
    from apps.api import validators
    sig = inspect.signature(validators.looks_like_realm_name)
    assert sig.parameters["dim"].default == ""
    # 旧写法（单参数）行为不变
    assert validators.looks_like_realm_name("练气境") is True

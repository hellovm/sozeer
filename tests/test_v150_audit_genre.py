# -*- coding: utf-8 -*-
"""v0.15 G6 回归：audit._realm_system 审计维度题材解耦。

核心验收（计划书 §五 G6）：
  * 仙侠书读「境界」阶梯（与改前一致）
  * 都市书读「职级」
  * 悬疑书（无实力体系）→ 明确告知审计模型跳过该维度
  * 签名冻结：_realm_system(db, book, body) 不变，2 个调用点零改动
"""
import os

import pytest

os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-g6")


@pytest.fixture()
def db():
    from apps.api import main
    import apps.api.genres as genres
    genres.invalidate_cache()
    with main.SessionLocal() as s:
        yield s
    genres.invalidate_cache()


def _mk(db, bid, theme, worlds):
    from apps.api.models import Book, WorldSetting, Character
    from sqlalchemy import delete
    db.execute(delete(WorldSetting).where(WorldSetting.book_id == bid))
    db.execute(delete(Character).where(Character.book_id == bid))
    db.execute(delete(Book).where(Book.id == bid))
    db.commit()
    db.add(Book(id=bid, title="测试书", theme=theme, owner_id="u1"))
    db.commit()
    for i, (cat, key, val) in enumerate(worlds):
        db.add(WorldSetting(id=f"ws_{bid}_{i}", book_id=bid, category=cat,
                            key=key, value=val))
    db.commit()
    return db.get(Book, bid)


def _add_char(db, bid, name, profile="", cid=None):
    """加人物（id 唯一化，避免跨测试主键冲突）。"""
    from apps.api.models import Character
    cid = cid or f"ch_{bid}_{name}"
    db.add(Character(id=cid, book_id=bid, name=name, profile=profile))
    db.commit()
    return cid


# ==================================================== 签名冻结
def test_signature_frozen():
    import inspect
    from apps.api import audit
    sig = inspect.signature(audit._realm_system)
    assert list(sig.parameters)[:2] == ["db", "book"]
    assert sig.parameters["body"].default == "", "body 必须保持可选"


# ==================================================== 仙侠：行为不变
def test_xianxia_reads_jingjie_ladder(db):
    from apps.api import audit
    b = _mk(db, "bk_g6_x", "xianxia",
            [("境界", "练气境", "初阶"), ("境界", "筑基境", "中阶")])
    txt = audit._realm_system(db, b, "")
    assert "练气境" in txt and "筑基境" in txt
    assert "合法境界" in txt, f"仙侠应输出『合法境界』，实际：{txt[:80]}"


def test_xianxia_empty_returns_skip_hint(db):
    """仙侠书没录境界 → 明确告知给满分（与改前文案一致）。"""
    from apps.api import audit
    b = _mk(db, "bk_g6_x2", "xianxia", [])
    txt = audit._realm_system(db, b, "")
    assert "未登记" in txt and "满分" in txt
    assert "境界" in txt


def test_xianxia_character_ownership(db):
    from apps.api import audit
    b = _mk(db, "bk_g6_x3", "xianxia", [("境界", "练气境", "")])
    _add_char(db, b.id, "主角", "练气境修士")
    txt = audit._realm_system(db, b, "")
    assert "主角" in txt and "练气境" in txt


# ==================================================== 都市：读职级
def test_urban_reads_zhiji_ladder(db):
    from apps.api import audit
    b = _mk(db, "bk_g6_u", "urban",
            [("职级", "一级", ""), ("职级", "二级", "")])
    txt = audit._realm_system(db, b, "")
    assert "职级" in txt, f"都市应输出『职级』维度，实际：{txt[:80]}"
    assert "一级" in txt and "二级" in txt
    assert "合法职级" in txt


def test_urban_ignores_jingjie_category(db):
    """都市书不该把 category=='境界' 的条目当自己的体系。"""
    from apps.api import audit
    b = _mk(db, "bk_g6_u2", "urban",
            [("境界", "练气境", ""), ("职级", "一级", "")])
    txt = audit._realm_system(db, b, "")
    assert "一级" in txt
    # 练气境属于『境界』分类，都市书的维度是『职级』，不该出现
    assert "练气境" not in txt, "都市书混入了仙侠境界"


def test_urban_empty_returns_zhiji_hint(db):
    from apps.api import audit
    b = _mk(db, "bk_g6_u3", "urban", [])
    txt = audit._realm_system(db, b, "")
    assert "职级" in txt and "满分" in txt


# ==================================================== 悬疑：跳过
def test_suspense_returns_no_system_hint(db):
    """★ 悬疑无实力体系 → 明确告知跳过（不硬性挑刺）。"""
    from apps.api import audit
    b = _mk(db, "bk_g6_s", "urban_suspense",
            [("线索", "红绳", ""), ("现场", "密室", "")])
    txt = audit._realm_system(db, b, "")
    assert "无实力等级体系" in txt or "无实力" in txt, f"悬疑应提示无体系：{txt[:120]}"
    assert "满分" in txt


def test_suspense_hint_mentions_theme_label(db):
    """提示语里带上题材名，便于审计模型理解上下文。"""
    from apps.api import audit
    b = _mk(db, "bk_g6_s2", "urban_suspense", [])
    txt = audit._realm_system(db, b, "")
    assert "都市悬疑" in txt, f"应包含题材名：{txt[:120]}"


# ==================================================== 兜底：未知题材
def test_unknown_theme_falls_back_to_jingjie(db):
    """★ 未知题材回退 '境界'（老书零影响）。"""
    from apps.api import audit
    b = _mk(db, "bk_g6_unk", "某个不存在的题材",
            [("境界", "练气境", "")])
    txt = audit._realm_system(db, b, "")
    assert "练气境" in txt, "未知题材应回退读取『境界』"


def test_genre_free_text_routing(db):
    """genre 自由文本（老书只有 genre）也能正确路由。"""
    from apps.api.models import Book
    from sqlalchemy import delete
    from apps.api import audit
    db.execute(delete(Book).where(Book.id == "bk_g6_free"))
    db.commit()
    db.add(Book(id="bk_g6_free", title="老书", theme="",
                genre="系统流 + 轻松搞笑 + 修仙 + 反套路", owner_id="u1"))
    db.commit()
    b = db.get(Book, "bk_g6_free")
    txt = audit._realm_system(db, b, "")
    assert "境界" in txt, "含'修仙'的 genre 应路由到仙侠"


# ==================================================== 风险表述
def test_risk_spans_appended_for_xianxia(db):
    """越级/碾压表述仍被追加（仙侠原有能力未丢）。"""
    from apps.api import audit
    b = _mk(db, "bk_g6_risk", "xianxia", [("境界", "练气境", "")])
    # 构造含碾压词的正文
    txt = audit._realm_system(db, b, "他一掌就碾压了对手，降维打击。")
    assert isinstance(txt, str) and len(txt) > 0

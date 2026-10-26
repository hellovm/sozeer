# -*- coding: utf-8 -*-
"""v0.15 G10 回归：提示词中立化 + 题材提示段注入。

核心验收（计划书 §五 G10）：
  * 都市书立项/写作不再被引导修仙（提示段给出金钱/职级，不给灵石/功法）
  * 仙侠书的提示段与 v0.14.10 措辞等价（零回归）
  * 无题材信息 → 不注入（走原行为）
"""
import os

import pytest

os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-g10")


@pytest.fixture()
def db():
    from apps.api import main
    import apps.api.genres as genres
    genres.invalidate_cache()
    with main.SessionLocal() as s:
        yield s
    genres.invalidate_cache()


def _mk(db, bid, theme="", genre="", tags=""):
    from apps.api.models import Book
    from sqlalchemy import delete
    db.execute(delete(Book).where(Book.id == bid))
    db.commit()
    db.add(Book(id=bid, title="测试书", theme=theme, genre=genre, tags=tags, owner_id="u1"))
    db.commit()
    return db.get(Book, bid)


# ==================================================== 仙侠：零回归
def test_xianxia_block_mentions_jingjie(db):
    from apps.api import agent
    b = _mk(db, "bk_g10_x", theme="xianxia")
    txt = agent._genre_block(db, b)
    assert txt, "仙侠书应有题材提示段"
    assert "境界" in txt, f"仙侠应提『境界』：{txt}"
    assert "功法" in txt or "灵石" in txt, f"仙侠应提原度量衡：{txt}"


def test_xianxia_block_no_modern_words(db):
    """仙侠提示段不该出现都市词。"""
    from apps.api import agent
    b = _mk(db, "bk_g10_x2", theme="xianxia")
    txt = agent._genre_block(db, b)
    assert "职级" not in txt and "粉丝" not in txt


# ==================================================== 都市：不再被引导修仙
def test_urban_block_mentions_modern_metrics(db):
    """★ 核心验收：都市书提示段给出现代度量衡，不给仙侠词。"""
    from apps.api import agent
    b = _mk(db, "bk_g10_u", theme="urban")
    txt = agent._genre_block(db, b)
    assert txt
    assert "职级" in txt or "粉丝" in txt, f"都市应提现代维度：{txt}"
    assert "灵石" not in txt, "都市书不该出现灵石"
    assert "功法" not in txt, "都市书不该出现功法"
    assert "境界" not in txt, "都市书不该出现境界"


def test_urban_block_has_no_fake_level_system_when_none(db):
    """悬疑（无实力体系）→ 明确告知不要编等级。"""
    from apps.api import agent
    b = _mk(db, "bk_g10_s", theme="urban_suspense")
    txt = agent._genre_block(db, b)
    assert "无实力等级体系" in txt, f"悬疑应告知无等级体系：{txt}"
    assert "不要编造" in txt


def test_urban_block_prompt_hint(db):
    from apps.api import agent
    b = _mk(db, "bk_g10_u2", theme="urban")
    txt = agent._genre_block(db, b)
    assert "现实规则" in txt or "职场" in txt or "商战" in txt, f"都市创作要点缺失：{txt}"


# ==================================================== 标签叠加
def test_tags_stack_in_block(db):
    """★ 软标签叠加：urban + 悬疑 → 风格段含两组。"""
    from apps.api import agent
    b = _mk(db, "bk_g10_tag", theme="urban", tags="urban_suspense")
    txt = agent._genre_block(db, b)
    assert "现代" in txt, f"缺现代风格：{txt}"
    assert "悬疑" in txt, f"标签未叠加：{txt}"


# ==================================================== 无题材：不注入
def test_no_theme_returns_empty(db):
    """★ 零回归：无题材信息 → 空串 → 不注入（走原行为）。"""
    from apps.api import agent
    b = _mk(db, "bk_g10_none", theme="", genre="")
    assert agent._genre_block(db, b) == ""


def test_unknown_theme_returns_empty(db):
    from apps.api import agent
    b = _mk(db, "bk_g10_unk", theme="完全不存在zzz")
    assert agent._genre_block(db, b) == ""


def test_free_text_genre_routes(db):
    """老书只有自由文本 genre → 也能生成提示段。"""
    from apps.api import agent
    b = _mk(db, "bk_g10_free", theme="", genre="系统流 + 轻松搞笑 + 修仙 + 反套路")
    txt = agent._genre_block(db, b)
    assert txt and "境界" in txt


def test_block_never_crashes(db):
    """任意异常输入不该抛（提示词生成是兜底路径）。"""
    from apps.api import agent
    for bad in [None, "", "🙂"]:
        b = _mk(db, "bk_g10_bad", theme=bad)
        assert isinstance(agent._genre_block(db, b), str)


# ==================================================== SYSTEM_PROMPT 中立化
def test_system_prompt_mentions_theme_adaptation():
    """提示词应说明题材适配，避免默认套仙侠。"""
    from apps.api import agent
    sp = agent.SYSTEM_PROMPT
    assert "题材适配" in sp, "SYSTEM_PROMPT 应含题材适配说明"
    assert "set_project_meta" in sp


def test_system_prompt_no_longer_forces_xianxia():
    """★ 中立化：原来的『力量/境界体系』已改为『力量/等级体系』。"""
    from apps.api import agent
    sp = agent.SYSTEM_PROMPT
    assert "力量/等级体系" in sp, "应已中立化为『等级体系』"
    assert "力量/境界体系" not in sp, "旧的仙侠导向措辞应已移除"


def test_system_prompt_warns_against_default_xianxia():
    from apps.api import agent
    sp = agent.SYSTEM_PROMPT
    assert "不要默认套用仙侠" in sp or "不要写" in sp, "应明确禁止默认套仙侠"

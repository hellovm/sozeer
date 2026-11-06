# -*- coding: utf-8 -*-
"""v0.15 G7 回归：drama / cover 视觉锚点接入题材包。

核心验收（计划书 §五 G7）：
  * 都市短剧拿到「现代写实」画风
  * 风格提示支持 theme + tags 叠加
  * 题材包不可用时回退内置表（行为与改前一致）
  * 函数签名兼容（第 1 个位置参数语义不变，老调用点零改动）
"""
import inspect
import os

import pytest

os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-g7")


@pytest.fixture()
def db():
    from apps.api import main
    import apps.api.genres as genres
    genres.invalidate_cache()
    with main.SessionLocal() as s:
        yield s
    genres.invalidate_cache()


# ==================================================== 签名兼容
def test_drama_hint_signature_compatible():
    """第 1 个参数 style 位置不变（老调用 _genre_style_hint(project.style) 零改动）。"""
    from apps.api import drama
    sig = inspect.signature(drama._genre_style_hint)
    assert list(sig.parameters)[0] == "style"
    assert sig.parameters["tags"].default is None
    assert sig.parameters["db"].default is None


def test_drama_hint_old_call_still_works():
    """★ 零回归：单参数调用（老写法）仍返回正确风格。"""
    from apps.api import drama
    h = drama._genre_style_hint("仙侠")
    assert h and ("仙侠" in h or "古风" in h or "水墨" in h)


# ==================================================== 仙侠：行为不变
def test_xianxia_style_unchanged():
    from apps.api import drama
    h = drama._genre_style_hint("仙侠玄幻")
    assert "仙侠" in h or "水墨" in h, f"仙侠风格被改：{h}"


def test_fallback_when_genre_unknown():
    """未知题材 → 回退内置表；内置也不命中 → 空串（与改前一致）。"""
    from apps.api import drama
    assert drama._genre_style_hint("完全未知的题材zzz") == ""


# ==================================================== 都市：现代写实
def test_urban_style_is_modern():
    from apps.api import drama
    h = drama._genre_style_hint("urban")
    assert "现代" in h, f"都市应得现代写实画风：{h}"


def test_urban_free_text_routes():
    """自由文本 genre（老书）也能路由到都市风格。"""
    from apps.api import drama
    h = drama._genre_style_hint("都市")
    assert "现代" in h


def test_style_tags_stack():
    """★ theme + tags 叠加：都市 + 悬疑 → 两组措辞合并。"""
    from apps.api import drama
    h = drama._genre_style_hint("urban", tags=["urban_suspense"])
    assert "现代" in h, f"缺现代写实：{h}"
    assert "悬疑" in h, f"缺悬疑氛围（tags 未叠加）：{h}"


def test_suspense_style():
    from apps.api import drama
    h = drama._genre_style_hint("urban_suspense")
    assert "悬疑" in h


def test_scifi_style():
    from apps.api import drama
    h = drama._genre_style_hint("scifi")
    assert "科幻" in h or "未来" in h


# ==================================================== cover 锚点
def test_cover_anchor_xianxia_unchanged():
    """★ 零回归：仙侠封面锚点与改前一致。"""
    from apps.api import cover
    v = cover._cover_anchor_for("仙侠")
    assert "古风" in v or "仙侠" in v


def test_cover_anchor_urban():
    from apps.api import cover
    v = cover._cover_anchor_for("都市")
    assert "现代" in v or "都市" in v


def test_cover_anchor_suspense():
    from apps.api import cover
    v = cover._cover_anchor_for("悬疑")
    assert "悬疑" in v or "暗" in v


def test_cover_anchor_unknown_returns_empty():
    from apps.api import cover
    assert cover._cover_anchor_for("完全未知zzz") == ""


def test_cover_anchor_never_crashes():
    """任意输入不该抛异常（封面生成是兜底路径）。"""
    from apps.api import cover
    for bad in ["", None, "🙂", "a" * 300]:
        assert isinstance(cover._cover_anchor_for(bad), str)


def test_fallback_prompt_urban_book(db):
    """★ 都市书生成封面底图 prompt 时用现代锚点。"""
    from apps.api import cover
    from apps.api.models import Book
    from sqlalchemy import delete
    db.execute(delete(Book).where(Book.id == "bk_g7_urban"))
    db.commit()
    db.add(Book(id="bk_g7_urban", title="都市书", genre="都市", theme="urban", owner_id="u1"))
    db.commit()
    b = db.get(Book, "bk_g7_urban")
    prompt = cover._fallback_base_prompt(b, {"tags": []}, None)
    assert "现代" in prompt or "都市" in prompt, f"都市书封面用了错误锚点：{prompt[:100]}"
    assert "古风仙侠" not in prompt, "都市书不该用仙侠锚点"


def test_fallback_prompt_xianxia_book(db):
    """仙侠书封面 prompt 保持古风（零回归）。"""
    from apps.api import cover
    from apps.api.models import Book
    from sqlalchemy import delete
    db.execute(delete(Book).where(Book.id == "bk_g7_x"))
    db.commit()
    db.add(Book(id="bk_g7_x", title="仙侠书", genre="仙侠", theme="xianxia", owner_id="u1"))
    db.commit()
    b = db.get(Book, "bk_g7_x")
    prompt = cover._fallback_base_prompt(b, {"tags": []}, None)
    assert "古风" in prompt or "仙侠" in prompt


def test_genres_style_hints_used_when_available():
    """确认题材包路径真的被走（而非总走兜底）。"""
    from apps.api import genres
    hints = genres.style_hints_for("urban", ["urban_suspense"])
    assert len(hints) == 2
    assert any("现代" in h for h in hints)
    assert any("悬疑" in h for h in hints)

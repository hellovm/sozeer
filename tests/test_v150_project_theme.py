# -*- coding: utf-8 -*-
"""v0.15 G9 回归：立项流程主题材 + 软标签（D2 落地）。

核心验收（计划书 §五 G9 + §八 D2）：
  * 主题材单选、软标签多选
  * theme 归一为规范 key（'都市' → 'urban'）
  * 未知值不报错、留空并走兜底（计划书 §六 风险 8）
  * 建书 API 支持 theme/tags；set_project_meta 工具支持
"""
import os

import pytest

os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-g9")


@pytest.fixture()
def ctx():
    from apps.api import main
    import apps.api.genres as genres
    genres.invalidate_cache()
    with main.SessionLocal() as s:
        from apps.api.models import User
        u = s.get(User, "u_g9")
        if not u:
            u = User(id="u_g9", username="g9user", role="user", is_active=1)
            s.add(u)
            s.commit()
        yield main, s, u, genres
    genres.invalidate_cache()


def _cleanup_book(s, bid):
    if not bid:
        return
    from apps.api.models import Book, StyleProfile
    sp = s.query(StyleProfile).filter(StyleProfile.book_id == bid).first()
    if sp:
        s.delete(sp)
    b = s.get(Book, bid)
    if b:
        s.delete(b)
    s.commit()


# ==================================================== 建书 API
def test_create_book_with_theme(ctx):
    main, s, u, genres = ctx
    _cleanup_book(s, None)
    r = main.create_book(main.BookIn(title="都市测试书", genre="都市",
                                     theme="都市", tags=["urban_suspense"]),
                         db=s, user=u)
    b = s.get(__import__("apps.api.models", fromlist=["Book"]).Book, r["id"])
    assert b.theme == "urban", f"『都市』应归一为 urban，实际 {b.theme}"
    assert "urban_suspense" in (b.tags or ""), "标签未写入"
    _cleanup_book(s, r["id"])


def test_create_book_unknown_theme_blank(ctx):
    """★ 未知题材留空，不报错（老书自由文本场景）。"""
    main, s, u, genres = ctx
    r = main.create_book(main.BookIn(title="未知题材书", theme="完全不存在zzz"), db=s, user=u)
    from apps.api.models import Book
    b = s.get(Book, r["id"])
    assert b.theme == "", "未知题材必须留空走兜底"
    _cleanup_book(s, r["id"])


def test_create_book_illegal_tags_dropped(ctx):
    """非法标签逐项丢弃，合法的保留。"""
    main, s, u, genres = ctx
    r = main.create_book(main.BookIn(title="混合标签书", theme="urban",
                                     tags=["xianxia", "不存在zzz", "scifi"]), db=s, user=u)
    from apps.api.models import Book
    b = s.get(Book, r["id"])
    tags = (b.tags or "").split(",")
    assert "xianxia" in tags and "scifi" in tags
    assert "不存在zzz" not in b.tags
    _cleanup_book(s, r["id"])


def test_create_book_no_theme_backward_compatible(ctx):
    """★ 零回归：不传 theme/tags 的老式建书照常工作。"""
    main, s, u, genres = ctx
    r = main.create_book(main.BookIn(title="老式建书", genre="仙侠"), db=s, user=u)
    from apps.api.models import Book
    b = s.get(Book, r["id"])
    assert b.theme == "" and (b.tags or "") == ""
    assert b.genre == "仙侠"
    _cleanup_book(s, r["id"])


# ==================================================== PATCH /theme
def test_patch_theme(ctx):
    main, s, u, genres = ctx
    from apps.api.models import Book
    bid = "bk_g9_patch"
    _cleanup_book(s, bid)
    s.add(Book(id=bid, title="patch书", owner_id=u.id))
    s.commit()
    r = main.set_book_theme(bid, {"theme": "悬疑", "tags": ["都市"]}, db=s, user=u)
    assert r["theme"] == "urban_suspense", f"『悬疑』应归一为 urban_suspense，实际 {r['theme']}"
    assert "urban" in r["tags"]
    _cleanup_book(s, bid)


def test_patch_theme_clear(ctx):
    main, s, u, genres = ctx
    from apps.api.models import Book
    bid = "bk_g9_clear"
    _cleanup_book(s, bid)
    s.add(Book(id=bid, title="清空书", theme="urban", tags="xianxia", owner_id=u.id))
    s.commit()
    r = main.set_book_theme(bid, {"theme": "", "tags": []}, db=s, user=u)
    assert r["theme"] == "" and r["tags"] == []
    _cleanup_book(s, bid)


def test_patch_theme_404(ctx):
    main, s, u, genres = ctx
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        main.set_book_theme("bk_never_g9", {"theme": "urban"}, db=s, user=u)
    assert ei.value.status_code == 404


def test_patch_theme_ignores_unknown(ctx):
    """未知题材 → 留空，不抛错。"""
    main, s, u, genres = ctx
    from apps.api.models import Book
    bid = "bk_g9_unk"
    _cleanup_book(s, bid)
    s.add(Book(id=bid, title="未知书", owner_id=u.id))
    s.commit()
    r = main.set_book_theme(bid, {"theme": "不存在zzz"}, db=s, user=u)
    assert r["theme"] == ""
    _cleanup_book(s, bid)


# ==================================================== bible 返回 theme/tags
def test_bible_exposes_theme_tags(ctx):
    main, s, u, genres = ctx
    from apps.api.models import Book
    bid = "bk_g9_bible"
    _cleanup_book(s, bid)
    s.add(Book(id=bid, title="bible书", theme="urban", tags="xianxia,scifi", owner_id=u.id))
    s.commit()
    out = main.get_bible(bid, db=s)
    assert out["book"]["theme"] == "urban"
    assert set(out["book"]["tags"]) == {"xianxia", "scifi"}
    _cleanup_book(s, bid)


# ==================================================== agent 工具
def test_tool_schema_has_theme():
    from apps.api import agent
    import json
    tools = json.dumps(agent.TOOLS, ensure_ascii=False)
    assert '"theme"' in tools, "set_project_meta 工具应暴露 theme 参数"
    assert '"theme_tags"' in tools, "应暴露 theme_tags 参数"


def test_tool_set_meta_writes_theme(ctx):
    """★ agent 工具写 theme → 归一为 key。"""
    import asyncio
    main, s, u, genres = ctx
    from apps.api.models import Book
    bid = "bk_g9_tool"
    _cleanup_book(s, bid)
    b = Book(id=bid, title="工具书", owner_id=u.id)
    s.add(b)
    s.commit()

    from apps.api import agent

    async def _emit(ev):
        pass

    async def run():
        await agent._t_set_meta(s, b, {"theme": "都市", "theme_tags": ["悬疑", "不存在zzz"]}, _emit)

    asyncio.run(run())
    s.refresh(b)
    assert b.theme == "urban"
    assert "urban_suspense" in (b.tags or ""), f"标签应归一为 key，实际 {b.tags}"
    assert "不存在zzz" not in (b.tags or "")
    _cleanup_book(s, bid)


def test_tool_set_meta_unknown_theme_safe(ctx):
    """★ 未知 theme 不报错（老书自由文本），仅留空。"""
    import asyncio
    main, s, u, genres = ctx
    from apps.api.models import Book
    bid = "bk_g9_tool2"
    _cleanup_book(s, bid)
    b = Book(id=bid, title="工具书2", owner_id=u.id)
    s.add(b)
    s.commit()
    from apps.api import agent

    async def _emit(ev):
        pass

    async def run():
        await agent._t_set_meta(s, b, {"theme": "完全不存在zzz"}, _emit)

    asyncio.run(run())
    s.refresh(b)
    assert b.theme == ""
    _cleanup_book(s, bid)


def test_tool_set_meta_no_theme_keeps_existing(ctx):
    """★ 零回归：不传 theme 时不动原值。"""
    import asyncio
    main, s, u, genres = ctx
    from apps.api.models import Book
    bid = "bk_g9_tool3"
    _cleanup_book(s, bid)
    b = Book(id=bid, title="工具书3", theme="urban", owner_id=u.id)
    s.add(b)
    s.commit()
    from apps.api import agent

    async def _emit(ev):
        pass

    async def run():
        await agent._t_set_meta(s, b, {"title": "改个名"}, _emit)

    asyncio.run(run())
    s.refresh(b)
    assert b.theme == "urban", "不传 theme 不该清空原值"
    assert b.title == "改个名"
    _cleanup_book(s, bid)

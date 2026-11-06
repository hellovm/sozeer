# -*- coding: utf-8 -*-
"""v0.19 新功能回归（F1 批量代笔 + F6 改稿对比）。

F1 批量生成多章 —— 队列式批量代笔，一次提交连写 N 章
F6 改稿对比     —— 行级 + 行内字符级 diff，供作者看「改了哪里」
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

os.environ.setdefault("NOVELFORGE_DATA", tempfile.mkdtemp(prefix="nf_test_f_"))
os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-f")


# ============================================================ F6 diff
def test_diff_identical():
    from apps.api import diffutil
    r = diffutil.diff_text("同样内容", "同样内容")
    assert r["changed_lines"] == 0
    assert r["similarity"] == 1.0
    assert "一致" in diffutil.summarize("a", "a")


def test_diff_detects_add():
    from apps.api import diffutil
    r = diffutil.diff_text("第一行", "第一行\n第二行")
    assert r["stats"]["added"] == 1
    types = [l["type"] for l in r["lines"]]
    assert "add" in types


def test_diff_detects_delete():
    from apps.api import diffutil
    r = diffutil.diff_text("第一行\n第二行", "第一行")
    assert r["stats"]["removed"] == 1


def test_diff_detects_change_with_inline():
    """★ 改写行必须带行内字符级 diff（中文用户要看"改了哪几个字"）。"""
    from apps.api import diffutil
    r = diffutil.diff_text("他缓缓走入房间。", "他快步冲进房间。")
    chg = [l for l in r["lines"] if l["type"] == "change"]
    assert chg, f"未识别为改写：{r['lines']}"
    inline = chg[0]["inline"]
    dels = "".join(x["text"] for x in inline if x["op"] == "delete")
    ins = "".join(x["text"] for x in inline if x["op"] == "insert")
    assert "缓缓走入" in dels, f"删除片段不对：{dels}"
    assert "快步冲进" in ins, f"插入片段不对：{ins}"


def test_diff_folds_unchanged_context():
    """未变更区域应折叠（避免长文全量返回）。"""
    from apps.api import diffutil
    old = "\n".join(f"第{i}行内容" for i in range(50))
    new = old + "\n新增一行"
    r = diffutil.diff_text(old, new, context=2)
    folds = [l for l in r["lines"] if l["type"] == "fold"]
    assert folds, "未折叠未变更区域"
    assert folds[0]["count"] > 0


def test_diff_stats_net_chars():
    from apps.api import diffutil
    r = diffutil.diff_text("短", "长很多的内容")
    s = r["stats"]
    assert s["chars_added"] > 0
    assert s["chars_removed"] > 0 or s["changed"] >= 1


def test_diff_truncates_huge_text():
    """★ 超长文本要截断（防 O(n²) 卡死）。"""
    from apps.api import diffutil
    big = "字" * (diffutil.MAX_DIFF_CHARS + 5000)
    r = diffutil.diff_text(big, big + "新增")
    assert r["truncated"] is True


def test_diff_summarize_format():
    from apps.api import diffutil
    txt = diffutil.summarize("第一行\n第二行", "第一行\n第二行改\n第三行")
    assert "相似度" in txt
    assert any(k in txt for k in ("新增", "删除", "改写"))


def test_diff_word_level_changes():
    from apps.api import diffutil
    items = diffutil.word_level_changes("a\nb\nc", "a\nB\nc\nd")
    assert items, "应提取出改动片段"
    assert all("old" in i and "new" in i for i in items)


def test_diff_empty_inputs():
    from apps.api import diffutil
    for a, b in [("", ""), (None, None), ("", "x"), ("x", "")]:
        r = diffutil.diff_text(a, b)
        assert r["ok"] is True


def test_diff_never_raises():
    """★ 对比工具绝不能抛异常（前端渲染依赖它）。"""
    from apps.api import diffutil
    for a, b in [(None, None), (123, "x"), ("x", 456), ([], {})]:
        try:
            diffutil.diff_text(a, b)
        except Exception as e:
            pytest.fail(f"入参 ({a!r},{b!r}) 抛异常：{type(e).__name__}: {e}")


def test_diff_endpoint_exists():
    import inspect
    from apps.api import main
    assert hasattr(main, "chapter_diff")
    sig = inspect.signature(main.chapter_diff)
    for p in ("from_commit", "to_commit", "summary_only"):
        assert p in sig.parameters


# ============================================================ F1 批量代笔
def test_batch_module_contract():
    from apps.api import batch_draft as bd
    for n in ("run_batch_draft", "next_chapter_index", "new_batch_id",
              "MAX_BATCH_CHAPTERS", "DEFAULT_BATCH_CHAPTERS"):
        assert hasattr(bd, n), f"缺 {n}"


def test_batch_limit_sane():
    """批次上限要有意义（防误点烧 token，又不能太小）。"""
    from apps.api import batch_draft as bd
    assert 1 < bd.MAX_BATCH_CHAPTERS <= 50, "上限不合理"
    assert 1 <= bd.DEFAULT_BATCH_CHAPTERS <= bd.MAX_BATCH_CHAPTERS


def test_batch_id_unique():
    from apps.api import batch_draft as bd
    ids = {bd.new_batch_id() for _ in range(50)}
    assert len(ids) == 50, "批次 id 有重复"


def test_next_chapter_index_empty_book():
    from apps.api import main
    from apps.api import batch_draft as bd
    from apps.api.models import Book
    from sqlalchemy import delete
    with main.SessionLocal() as db:
        db.execute(delete(Book).where(Book.id == "bk_f1a")); db.commit()
        db.add(Book(id="bk_f1a", title="空书", owner_id="u1")); db.commit()
        assert bd.next_chapter_index(db, db.get(Book, "bk_f1a")) == 1


def test_next_chapter_index_with_chapters():
    from apps.api import main
    from apps.api import batch_draft as bd
    from apps.api.models import Book, Chapter
    from sqlalchemy import delete
    with main.SessionLocal() as db:
        db.execute(delete(Chapter).where(Chapter.book_id == "bk_f1b"))
        db.execute(delete(Book).where(Book.id == "bk_f1b")); db.commit()
        db.add(Book(id="bk_f1b", title="有章", owner_id="u1")); db.commit()
        for i in (1, 2, 5):
            db.add(Chapter(id=f"c{i}", book_id="bk_f1b", idx=i, title=f"第{i}章"))
        db.commit()
        assert bd.next_chapter_index(db, db.get(Book, "bk_f1b")) == 6, "应取最大章号+1"


def test_batch_run_continues_on_failure(monkeypatch):
    """★ 单章失败不中断整批（作者要的是"能出多少算多少"）。"""
    from apps.api import batch_draft as bd
    from apps.api import agent as A
    from apps.api import main
    from apps.api.models import Book
    from sqlalchemy import delete

    calls = []

    async def fake_draft(db, book, ch_idx, *, instruction="", emit=None):
        calls.append(ch_idx)
        if ch_idx == 2:
            return {"ok": False, "ch_idx": ch_idx, "error": "模拟失败"}
        return {"ok": True, "ch_idx": ch_idx, "words": 100, "gate_score": 80}

    monkeypatch.setattr(A, "draft_chapter_for_batch", fake_draft, raising=False)

    with main.SessionLocal() as db:
        db.execute(delete(Book).where(Book.id == "bk_f1c")); db.commit()
        db.add(Book(id="bk_f1c", title="批量", owner_id="u1")); db.commit()
        b = db.get(Book, "bk_f1c")
        events = []

        async def emit(ev):
            events.append(ev)

        r = asyncio.run(bd.run_batch_draft(db, b, 1, 3, emit=emit))
        assert calls == [1, 2, 3], f"应逐章跑完，实际 {calls}"
        assert r["done"] == 2 and r["failed"] == 1
        assert len(r["chapters"]) == 3


def test_batch_clamps_count(monkeypatch):
    """★ 章数超上限要钳制（防误操作）。"""
    from apps.api import batch_draft as bd
    from apps.api import agent as A
    from apps.api import main
    from apps.api.models import Book
    from sqlalchemy import delete

    seen = {"n": 0}

    async def fake_draft(db, book, ch_idx, *, instruction="", emit=None):
        seen["n"] += 1
        return {"ok": True, "ch_idx": ch_idx, "words": 1, "gate_score": 1}

    monkeypatch.setattr(A, "draft_chapter_for_batch", fake_draft, raising=False)
    with main.SessionLocal() as db:
        db.execute(delete(Book).where(Book.id == "bk_f1d")); db.commit()
        db.add(Book(id="bk_f1d", title="钳制", owner_id="u1")); db.commit()
        b = db.get(Book, "bk_f1d")
        r = asyncio.run(bd.run_batch_draft(db, b, 1, 999))
        assert r["total"] == bd.MAX_BATCH_CHAPTERS, f"未钳制：{r['total']}"
        assert seen["n"] == bd.MAX_BATCH_CHAPTERS


def test_batch_emits_progress():
    from apps.api import batch_draft as bd
    from apps.api import agent as A
    from apps.api import main
    from apps.api.models import Book
    from sqlalchemy import delete

    async def fake_draft(db, book, ch_idx, *, instruction="", emit=None):
        return {"ok": True, "ch_idx": ch_idx, "words": 50, "gate_score": 70}

    import unittest.mock as mock
    with mock.patch.object(A, "draft_chapter_for_batch", fake_draft, create=True):
        with main.SessionLocal() as db:
            db.execute(delete(Book).where(Book.id == "bk_f1e")); db.commit()
            db.add(Book(id="bk_f1e", title="进度", owner_id="u1")); db.commit()
            b = db.get(Book, "bk_f1e")
            evs = []

            async def emit(ev):
                evs.append(ev)

            asyncio.run(bd.run_batch_draft(db, b, 1, 2, emit=emit))
            texts = " ".join(e.get("text", "") for e in evs)
            assert "1/2" in texts and "2/2" in texts, f"缺逐章进度：{texts[:200]}"


def test_batch_api_endpoints_exist():
    import inspect
    from apps.api import main
    assert hasattr(main, "batch_draft")
    assert hasattr(main, "batch_draft_limits")
    sig = inspect.signature(main.batch_draft)
    assert "body" in sig.parameters


def test_batch_draft_in_schema():
    from apps.api.main import BatchDraftIn
    m = BatchDraftIn()
    assert m.count == 3 and m.start_ch == 0


def test_batch_spawn_has_timeout():
    """★ 批量任务必须走带超时的 spawn（否则卡死会拖垮单进程）。"""
    import inspect
    from apps.api import main
    src = inspect.getsource(main.batch_draft)
    assert "_spawn(runner(), task_id=task.id" in src, "批量任务未绑定 task_id（超时无法标台账）"

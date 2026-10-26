"""v0.14.7：采纳消费草稿（条目自动消失）回归测试。

修复的问题：用户点「采纳入库」成功后，待审区条目不消失——根因是
accept 端点只落库章节，从不清理 Task.result_json 里的 content，而
pending-drafts 扫描按「content 存在性」列稿。丢弃（DELETE draft）早已
清理，采纳却不清，导致采纳后的稿子永远留在待审区、还能重复入库。

覆盖：
1. accept 带 task_id → result_json content 被摘到 _content_backup、
   accepted=True，pending-drafts 不再返回该条
2. content 与任务里不一致（用户编辑后采纳）→ 不误删，条目保留
3. task_id 缺失（老前端）→ 幂等兼容，采纳本身不报错
4. task_id 非法/不属于该书 → 忽略，不误伤其他任务
5. GET draft 返回 accepted/discarded 标记（刷新恢复判定依据）
6. 丢弃后的任务 GET draft 报 content 缺失（既有幂等语义保持）
7. v1.0-E4.18：前端口径——发送剥掉标题行的正文（bodyOfDraft）也能
   消费成功（strip_title 双侧对齐）；此前原文精确比对在真实前端下
   永不成立 → 采纳后草稿永远留在待审区
8. v1.0-E4.18：accepted=True 但 content 仍残留的历史脏数据行，
   pending-drafts 兜底跳过
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("NOVELFORGE_DATA", tempfile.mkdtemp(prefix="nf_test_v147_"))
os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-for-pytest-only")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import apps.api.models  # noqa: F401,E402  # 注册表 metadata


def asyncio_run(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


@pytest.fixture()
def db():
    # 关键：用 apps.api.main 引用链上的 SessionLocal，而不是 db.py 的模块属性。
    # 前面的测试（test_v141 隔离用例）会 reload config/db/models，engine 换新，
    # 而 main.py 顶部 `from apps.api.db import SessionLocal` 是 import 时刻
    # 的旧绑定 —— 两处引用可能指向不同 DB 文件。fixture 与被测端点同源，
    # 保证 fixture 写入与端点读写在同一个库上。
    from apps.api import main
    from apps.api.db import Base, engine
    Base.metadata.create_all(engine)
    with main.SessionLocal() as s:
        yield s


@pytest.fixture()
def draft_env(db):
    """书 + 一个 done 状态的代笔任务（result_json 含 content）。"""
    import datetime
    from apps.api.models import Book, Task

    b = Book(id=f"bk_{uuid.uuid4().hex[:8]}", title="采纳消费测试书", genre="仙侠")
    db.add(b)
    db.commit()
    content = "# 第1章 雪夜入门\n\n山门前的雪积了三尺，陈长安走到门前。"
    result = {"content": content, "title": "第1章 雪夜入门", "ch_idx": 1,
              "gate_score": 82, "words": 20, "gate_report": {"score": 82, "issues": []}}
    ts = datetime.datetime(2026, 10, 13, 10, 0, 0)
    t = Task(id=f"tk_{uuid.uuid4().hex[:8]}", book_id=b.id, kind="draft", target="1",
             status="done", progress="100", seq=0, events_json="[]",
             result_json=json.dumps(result, ensure_ascii=False), error="",
             started_at=ts, updated_at=ts)
    db.add(t)
    db.commit()
    return b, t, content


def _accept(db, book, content, task_id=None, ch_idx=1, title="第1章 雪夜入门"):
    """直接调 accept 端点函数（绕过 HTTP 层，与 api_smoke 同模式）。

    accept 内部自建 SessionLocal 会话并提交；fixture 会话需 expire 后重读
    才能看到最新落库状态。
    """
    from apps.api import main
    body = main.AcceptIn(ch_idx=ch_idx, title=title, content=content,
                         gate={"score": 82, "issues": []}, register=False,
                         task_id=task_id)
    out = asyncio_run(main.accept(book.id, body))
    db.expire_all()
    return out


def _pending(db, book):
    from apps.api import main
    return main.list_pending_drafts(book.id, db)["items"]


def test_accept_consumes_draft(db, draft_env):
    """采纳带 task_id：content 摘到 backup、accepted=True、待审区条目消失。"""
    from apps.api.models import Task
    b, t, content = draft_env
    r = _accept(db, b, content, task_id=t.id)
    assert r["ok"] is True and r["consumed"] is True
    row = db.get(Task, t.id)
    d = json.loads(row.result_json)
    assert "content" not in d
    assert d["_content_backup"] == content
    assert d["accepted"] is True
    assert _pending(db, b) == []


def test_accept_consumes_stripped_body(db, draft_env):
    """v1.0-E4.18 前端口径：发送剥掉标题行的正文也能消费成功。

    复现线上 bug：前端 bodyOfDraft 把 `# 第N章 …` 标题行剥掉后上传，
    任务 result_json 里存的是含标题全文 —— 原文精确比对永不相等，
    consumed 永远 False，采纳后草稿永远留在待审区。strip_title 双侧
    对齐后应判定为同一稿并正常消费。
    """
    from apps.api.models import Task
    from apps.api.orchestrator import gates
    b, t, content = draft_env
    stripped = gates.strip_title(content)  # 与前端 bodyOfDraft 同口径
    assert stripped != content             # 确认确实剥掉了标题行
    r = _accept(db, b, stripped, task_id=t.id)
    assert r["ok"] is True and r["consumed"] is True
    d = json.loads(db.get(Task, t.id).result_json)
    assert "content" not in d and d["accepted"] is True
    assert _pending(db, b) == []


def test_pending_skips_accepted_legacy_rows(db, draft_env):
    """v1.0-E4.18 兜底：accepted=True 但 content 残留的行不再出现在待审区。"""
    from apps.api.models import Task
    b, t, content = draft_env
    # 伪造历史脏数据：采纳标记已打，但 content 没摘走
    row = db.get(Task, t.id)
    d = json.loads(row.result_json)
    d["accepted"] = True
    row.result_json = json.dumps(d, ensure_ascii=False)
    db.commit()
    assert _pending(db, b) == []


def test_accept_mismatched_content_keeps_draft(db, draft_env):
    """编辑后采纳（content 与任务里的不同）：任务旧稿保留，不误删。"""
    from apps.api.models import Task
    b, t, content = draft_env
    edited = content + "\n\n编辑补充的一段。"
    r = _accept(db, b, edited, task_id=t.id)
    assert r["ok"] is True and r["consumed"] is False
    d = json.loads(db.get(Task, t.id).result_json)
    assert d.get("content") == content  # 原稿未动
    assert "accepted" not in d
    items = _pending(db, b)
    assert len(items) == 1 and items[0]["task_id"] == t.id


def test_accept_without_task_id_ok(db, draft_env):
    """老前端不带 task_id：采纳本身成功，任务稿保留（不猜不删）。"""
    b, t, content = draft_env
    r = _accept(db, b, content, task_id=None)
    assert r["ok"] is True and r["consumed"] is False
    items = _pending(db, b)
    assert len(items) == 1  # 仍在待审区，用户可再从待审区采纳


def test_accept_bad_task_id_ignored(db, draft_env):
    """task_id 非法/跨书：静默忽略，不误伤。"""
    b, t, content = draft_env
    for bad in ("tk_nonexistent", None):
        r = _accept(db, b, content, task_id=bad)
        assert r["ok"] is True and r["consumed"] is False
    assert _pending(db, b)


def test_get_draft_returns_consumed_flags(db, draft_env):
    """GET draft 返回 accepted/discarded 标记（刷新恢复判定依据）。"""
    from apps.api.models import Task
    from apps.api import main
    b, t, content = draft_env
    # 未消费
    d = main.get_task_draft(b.id, t.id, db)
    assert d["accepted"] is False and d["discarded"] is False
    # 采纳后
    _accept(db, b, content, task_id=t.id)
    # content 摘掉后 GET draft 应 404（既有语义：无可恢复草稿）
    with pytest.raises(Exception):
        main.get_task_draft(b.id, t.id, db)
    # 丢弃后同样 404
    db2_t = db.get(Task, t.id)
    r2 = json.loads(db2_t.result_json)
    r2["content"] = r2.pop("_content_backup")
    db2_t.result_json = json.dumps(r2, ensure_ascii=False)
    db.commit()
    main.discard_task_draft(b.id, t.id, db)
    with pytest.raises(Exception):
        main.get_task_draft(b.id, t.id, db)


def test_discard_then_accept_flow(db, draft_env):
    """丢弃→恢复→采纳 全生命周期：consumed 标记与 backup 互不踩。"""
    from apps.api.models import Task
    from apps.api import main
    b, t, content = draft_env
    # 丢弃
    main.discard_task_draft(b.id, t.id, db)
    assert _pending(db, b) == []
    # 恢复
    main.restore_task_draft(b.id, t.id, db)
    assert len(_pending(db, b)) == 1
    # 再采纳
    r = _accept(db, b, content, task_id=t.id)
    assert r["consumed"] is True
    d = json.loads(db.get(Task, t.id).result_json)
    assert d["accepted"] is True and "content" not in d
    assert d["_content_backup"] == content
    assert _pending(db, b) == []

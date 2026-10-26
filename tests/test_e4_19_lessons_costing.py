"""v1.0-E4.19 · #313 / #315 测试。

覆盖：
  #313 成本台账接入 agent 链路 —— `agent._record_llm_call` 落 LLMCall
  #315 避坑记录 —— 判据（纠错 + 变更工具）/ 渲染注入 / 同轮去重 / 洞察摘要

★ 为什么单独建文件而不是塞进 test_v23_costing.py：
  costing 测的是"台账本身"，这里测的是"agent 链路有没有接上台账"
  与"避坑判据准不准"—— 两个不同的回归面。
"""
from __future__ import annotations

import sys
import types
import uuid

import pytest

from apps.api import costing, lessons


def _mod(name: str):
    return sys.modules.get(f"apps.api.{name}") or __import__(
        f"apps.api.{name}", fromlist=["*"])


@pytest.fixture(scope="module", autouse=True)
def _ensure_tables():
    from apps.api import models  # noqa: F401
    from apps.api.db import Base, engine
    Base.metadata.create_all(engine)
    from apps.api.services import db_migrate
    db_migrate.migrate()
    yield


@pytest.fixture()
def db():
    SL = _mod("db").SessionLocal
    s = SL()
    try:
        yield s
    finally:
        s.close()


def _mk_user(db):
    a = _mod("auth")
    uname = f"lsn_{uuid.uuid4().hex[:8]}"
    u = a.create_user(db, uname, "lesson-test-12345", role="user")
    db.commit()
    return u


# ══════════════════════════ #313 成本台账 ══════════════════════════

def _fake_result(**kw):
    base = dict(input_tokens=1200, output_tokens=300, cost=0.0123, error="")
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_record_llm_call_writes_llmcall(db):
    """agent._record_llm_call 必须落一行 LLMCall（#313 根因：以前从不落）。"""
    from apps.api import agent
    u = _mk_user(db)
    book = types.SimpleNamespace(owner_id=u.id, id=f"bk_{uuid.uuid4().hex[:8]}")
    m = types.SimpleNamespace(id="mp_fake", role="writer")

    agent._record_llm_call(db, book, m, "chapter_write", _fake_result())

    rows = list(db.query(_mod("models").LLMCall).all())
    mine = [r for r in rows if r.book_id == book.id and r.task == "chapter_write"]
    assert len(mine) == 1
    r = mine[0]
    assert r.user_id == u.id
    assert r.input_tokens == 1200 and r.output_tokens == 300
    assert r.model_id == "mp_fake" and r.role == "writer"
    assert r.status == "ok"
    assert r.cost == pytest.approx(0.0123)


def test_record_llm_call_error_status_and_no_owner(db):
    """出错要标 error；无主书要静默跳过（不写、不炸）。"""
    from apps.api import agent
    book = types.SimpleNamespace(owner_id="", id="bk_none")
    m = types.SimpleNamespace(id="mp_fake", role="writer")
    agent._record_llm_call(db, book, m, "chapter_write",
                           _fake_result(error="HTTP 429"))
    assert db.query(_mod("models").LLMCall).filter_by(book_id="bk_none").count() == 0

    u = _mk_user(db)
    book2 = types.SimpleNamespace(owner_id=u.id, id=f"bk_{uuid.uuid4().hex[:6]}")
    agent._record_llm_call(db, book2, m, "chat", _fake_result(error="网络错误"))
    r = db.query(_mod("models").LLMCall).filter_by(book_id=book2.id).one()
    assert r.status == "error"


def test_record_llm_call_never_raises(db):
    """fail-open：传个坏对象也不能抛（记账绝不能搞挂生成）。"""
    from apps.api import agent
    agent._record_llm_call(db, None, None, "chat", object())


def test_user_report_reflects_agent_rows(db):
    """落账后 /api/me/cost 的数据源（user_report）必须看得到。"""
    from apps.api import agent
    u = _mk_user(db)
    book = types.SimpleNamespace(owner_id=u.id, id=f"bk_{uuid.uuid4().hex[:8]}")
    m = types.SimpleNamespace(id="mp_fake", role="writer")
    agent._record_llm_call(db, book, m, "chapter_write", _fake_result())
    agent._record_llm_call(db, book, m, "chapter_write", _fake_result())

    rep = costing.user_report(db, u.id)
    assert rep["today"]["tokens"] >= 3000
    assert "chapter_write" in rep["by_task"]
    assert rep["by_task"]["chapter_write"]["calls"] == 2
    assert rep["daily_cap"] > 0


# ══════════════════════════ #315 避坑记录 ══════════════════════════

class TestLooksLikeCorrection:
    @pytest.mark.parametrize("text", [
        "不对，主角跟女二是同辈，不该叫师姐",
        "第3章时间线错了，决战在冬至之后",
        "别再用'嘴角勾起一抹冷笑'了",
        "改成第三人称",
        "这里矛盾了，前面说他已经死了",
        "重新写这一段，太出戏",
        "人设是冷酷的，他怎么笑了",
        # 「改成 / 换掉」这类**变更要求**也算 —— 后续生成必须尊重它
        "把女二的名字改成林晚",
    ])
    def test_positive(self, text):
        assert lessons.looks_like_correction(text) is True

    @pytest.mark.parametrize("text", [
        "帮我写第5章",
        "这本书叫什么名字好",
        "继续",
        "看看大纲",
        # 无纠错/变更词的**常规创作请求**（这是判据要滤掉的噪声）
        "帮我把女二的名字设定为林晚",
        "现在写到哪了",
    ])
    def test_negative(self, text):
        assert lessons.looks_like_correction(text) is False

    def test_empty_and_too_long(self):
        assert lessons.looks_like_correction("") is False
        assert lessons.looks_like_correction("不对" + "啊" * 500) is False


def test_record_lesson_requires_both_conditions(db):
    """判据铁律：既要像纠错，又要真改了 —— 缺一不可。"""
    u = _mk_user(db)
    bid = f"bk_{uuid.uuid4().hex[:8]}"
    n0 = db.query(_mod("models").GenLesson).count()

    # 只有纠错、没有变更工具 → 不记（防把闲聊当经验）
    assert lessons.record_lesson(
        db, book_id=bid, user_id=u.id, user_msg="不对，这里错了",
        tool_names=["read_chapter"]) is False

    # 只有变更工具、用户这句不像纠错 → 不记（正常创作不是纠错）
    assert lessons.record_lesson(
        db, book_id=bid, user_id=u.id, user_msg="帮我写第6章",
        tool_names=["draft_chapter"]) is False

    assert db.query(_mod("models").GenLesson).count() == n0

    # 两者都有 → 记一条
    assert lessons.record_lesson(
        db, book_id=bid, user_id=u.id, user_msg="不对，主角不该叫师姐",
        tool_names=["read_chapter", "draft_chapter"],
        fix_text="好的，我来重写第3章") is True
    assert db.query(_mod("models").GenLesson).count() == n0 + 1

    r = db.query(_mod("models").GenLesson).order_by(
        _mod("models").GenLesson.created_at.desc()).first()
    assert r.trigger == "不对，主角不该叫师姐"
    assert r.tools == "draft_chapter"          # read_chapter 被过滤掉
    assert r.fix == "好的，我来重写第3章"
    assert r.status == "active" and r.hits == 0


def test_record_lesson_same_turn_dedupe(db):
    """同一轮对话最多记 1 条（防长会话反复落同一件事）。"""
    lessons.reset_recorded()
    u = _mk_user(db)
    bid = f"bk_{uuid.uuid4().hex[:8]}"
    kw = dict(book_id=bid, user_id=u.id, user_msg="时间线错了，改一下",
              tool_names=["draft_chapter"])
    assert lessons.record_lesson(db, **kw) is True
    assert lessons.record_lesson(db, **kw) is False
    assert db.query(_mod("models").GenLesson).filter_by(book_id=bid).count() == 1


def test_record_lesson_new_turn_records_again(db):
    """★ 同一句纠错在**不同轮次**要能再记（是新事件，不是重复）。

    这是 turn_key 存在的理由：只按 (书, 消息) 去重会把后续轮次误杀。
    """
    lessons.reset_recorded()
    u = _mk_user(db)
    bid = f"bk_{uuid.uuid4().hex[:8]}"
    base = dict(book_id=bid, user_id=u.id, user_msg="称呼错了，改一下",
                tool_names=["draft_chapter"])
    assert lessons.record_lesson(db, turn_key="t1", **base) is True
    assert lessons.record_lesson(db, turn_key="t1", **base) is False
    # 换一轮 → 同一条纠错再次记录
    assert lessons.record_lesson(db, turn_key="t2", **base) is True
    assert db.query(_mod("models").GenLesson).filter_by(book_id=bid).count() == 2


def test_recent_lessons_filters_status_and_orders(db):
    """只回 active；新的在前；按 book_id 隔离。"""
    u = _mk_user(db)
    bid = f"bk_{uuid.uuid4().hex[:8]}"
    lessons.reset_recorded()
    for i, msg in enumerate(["第一处不对，改A", "第二处也不对，改B"]):
        lessons.record_lesson(db, book_id=bid, user_id=u.id, user_msg=msg,
                              tool_names=["draft_chapter"])
    # 停用最早那条
    first = db.query(_mod("models").GenLesson).filter_by(book_id=bid).order_by(
        _mod("models").GenLesson.created_at.asc()).first()
    first.status = "muted"
    db.commit()

    rows = lessons.recent_lessons(db, bid)
    assert len(rows) == 1
    assert rows[0].trigger == "第二处也不对，改B"

    # 别的书看不到
    assert lessons.recent_lessons(db, f"bk_{uuid.uuid4().hex[:8]}") == []


def test_render_block_format_and_limit():
    """注入块要有明确结构，且条数有上限（防稀释注意力）。"""
    rows = [types.SimpleNamespace(trigger=f"第{i}处问题", fix=(f"修{i}" if i % 2 else ""))
            for i in range(12)]
    block = lessons.render_block(rows)
    assert "避坑经验" in block
    assert "1. 用户指出：第0处问题" in block
    assert "→ 修正方式：修1" in block
    # 上限 8 条
    assert "12. 用户指出" not in block
    assert "9. 用户指出" not in block
    assert lessons.render_block([]) == ""
    assert lessons.render_block([types.SimpleNamespace(trigger="", fix="")]) == ""


def test_bump_hits(db):
    """注入计数要能落库（衡量避坑提示有没有被用上）。"""
    u = _mk_user(db)
    bid = f"bk_{uuid.uuid4().hex[:8]}"
    lessons.reset_recorded()
    lessons.record_lesson(db, book_id=bid, user_id=u.id,
                          user_msg="不对，称呼错了", tool_names=["draft_chapter"])
    rows = lessons.recent_lessons(db, bid)
    lessons.bump_hits(db, rows)
    db.refresh(rows[0])
    assert rows[0].hits == 1


# ══════════════════════════ 洞察侧 ══════════════════════════

def test_lessons_digest_counts(db):
    """管理端摘要：count/active/muted/hits 与条目都要有。"""
    from apps.api import insight
    u = _mk_user(db)
    bid = f"bk_{uuid.uuid4().hex[:8]}"
    lessons.reset_recorded()
    lessons.record_lesson(db, book_id=bid, user_id=u.id,
                          user_msg="矛盾了，改掉", tool_names=["draft_chapter"])
    lessons.record_lesson(db, book_id=bid, user_id=u.id,
                          user_msg="崩坏了，重写", tool_names=["draft_chapter"])
    all_rows = db.query(_mod("models").GenLesson).filter_by(book_id=bid).all()
    all_rows[0].status = "muted"
    db.commit()

    d = insight.lessons_digest(db)
    assert d["count"] >= 2
    assert d["active"] >= 1
    assert d["muted"] >= 1
    assert any(x["trigger"] == "崩坏了，重写" for x in d["items"])
    assert "不参与遥测上报" in d["note"]

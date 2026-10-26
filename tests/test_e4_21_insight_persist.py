# -*- coding: utf-8 -*-
"""v1.0-E4.21 · 数据洞察持久化测试。

背景（为什么要做）：
  adoption 计数原本**只存内存**，服务每次部署/重启就清零 ——
  数据洞察页（提示词版本对比 / 采纳漏斗）永远只有"本次运行以来"的零头，
  用户多轮创作 + 纠错后洞察页依然全空，无法支持系统进化。

覆盖：
  1. adoption.flush —— 增量落库 + 内存清空 + 失败回填
  2. adoption.collect_adoption —— "Σ历史快照 + 当前内存" 合并
  3. 阈值自动落盘（独立守护线程，不阻塞生成路径）
  4. lessons 纠错词典扩充（质量改进类表达）
  5. insight.prompt_ab 数据范围文案（不再误导）
"""
from __future__ import annotations

import json
import time
import uuid

import pytest

from apps.api import adoption, insight, lessons


@pytest.fixture(scope="module", autouse=True)
def _ensure_tables():
    from apps.api import models  # noqa: F401
    from apps.api.db import Base, engine
    Base.metadata.create_all(engine)
    yield


@pytest.fixture()
def db():
    from apps.api.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _clean_adoption(db):
    """每个用例前清空快照行 + 内存 + 模块级状态，保证幂等。"""
    from sqlalchemy import delete
    from apps.api.models import AdoptionSnapshot
    db.execute(delete(AdoptionSnapshot))
    db.commit()
    adoption.reset()
    adoption._flush_failures = 0
    adoption._flush_pending = False
    yield
    adoption.reset()
    adoption._flush_failures = 0
    adoption._flush_pending = False


def _snap_rows(db) -> list:
    from sqlalchemy import select
    from apps.api.models import AdoptionSnapshot
    return list(db.scalars(select(AdoptionSnapshot)).all())


# ══════════════════════ 1. flush：增量落库 ══════════════════════

def test_flush_writes_delta_and_clears_memory(db):
    adoption.record_generate("chapter_write", words=1200, prompt="P1", ok=True)
    adoption.record_adopt("chapter_write", before_words=1000,
                          after_words=1100, prompt="P1")
    n = adoption.flush(db)
    assert n >= 2
    assert adoption.snapshot() == {}, "flush 后内存必须清零"
    rows = _snap_rows(db)
    assert len(rows) == 1
    data = json.loads(rows[0].payload)
    assert int(data.get("gen.chapter_write.total", 0)) >= 1
    assert rows[0].total >= 2


def test_flush_twice_is_delta_not_full(db):
    """★ 回归：快照必须是增量 —— 第二次 flush 只含新增，否则会重复计数。"""
    adoption.record_generate("chapter_write", prompt="PA")
    adoption.flush(db)
    adoption.record_generate("chapter_write", prompt="PA")
    adoption.flush(db)
    rows = _snap_rows(db)
    assert len(rows) == 2
    # 一次 record_generate = 3 个计数器（total/ok/prompt）
    assert all(r.total == 3 for r in rows)
    # ★ 真不变量：合并后 gen 计数精确为 2（增量不重计、不全量叠加）
    merged = adoption.collect_adoption()
    assert merged["by_role"]["chapter_write"]["gen"] == 2


def test_flush_empty_is_noop(db):
    assert adoption.flush(db) == 0
    assert _snap_rows(db) == []


def test_flush_failure_restores_memory(db):
    """★ 写库失败：增量必须回填内存（不能清完再丢）。"""
    class _Broken:
        def add(self, *a, **k):
            pass
        def commit(self):
            raise RuntimeError("db down")

    adoption.record_generate("chapter_write")
    n = adoption.flush(_Broken())
    assert n == 0
    assert adoption.snapshot().get("gen.chapter_write.total", 0) >= 1, \
        "写库失败后内存计数必须回填保留"


# ══════════════════════ 2. collect：历史+内存合并 ══════════════════════

def test_collect_merges_persisted_and_memory(db):
    adoption.record_generate("chapter_polish", prompt="PX")
    adoption.flush(db)                       # 1 条落库
    adoption.record_generate("chapter_polish", prompt="PX")  # 1 条在内存
    snap = adoption.collect_adoption()
    assert snap["by_role"]["chapter_polish"]["gen"] == 2
    assert sum(v["gen"] for v in snap["by_prompt"].values()) >= 2


def test_collect_survives_simulated_restart(db):
    """★ 核心场景：部署/重启后历史不丢（内存清零，靠 DB 快照）。"""
    adoption.record_generate("chapter_write", prompt="PR")
    adoption.record_adopt("chapter_write", prompt="PR")
    adoption.flush(db)
    adoption.reset()                          # 模拟进程重启：内存清零
    snap = adoption.collect_adoption()
    by = snap["by_role"]
    assert by["chapter_write"]["gen"] == 1
    assert by["chapter_write"]["adopt"] == 1
    # 提示词 A/B：gen/adopt 都在
    assert sum(v["gen"] for v in snap["by_prompt"].values()) == 1
    assert sum(v["adopt"] for v in snap["by_prompt"].values()) == 1


def test_collect_fail_open_without_table(db):
    """表不存在（旧部署）时读取不得抛异常。"""
    adoption.record_feature("x_feature")
    snap = adoption.collect_adoption()
    assert snap["features"].get("x_feature", 0) >= 1


# ══════════════════════ 3. 阈值自动落盘 ══════════════════════

def test_threshold_triggers_auto_flush(db, monkeypatch):
    """阈值到达 → 守护线程异步落库（生成路径本身零 I/O）。"""
    monkeypatch.setattr(adoption, "FLUSH_THRESHOLD", 3)
    for i in range(3):
        adoption.record_feature(f"f{i}")
    deadline = time.time() + 15
    ok = False
    while time.time() < deadline:
        for r in _snap_rows(db):
            d = json.loads(r.payload or "{}")
            if "feature.f0" in d:
                ok = True
                break
        if ok:
            break
        time.sleep(0.05)
    assert ok, "达到阈值后 15s 内应自动落库（守护线程）"


# ══════════════════════ 4. lessons 词典扩充 ══════════════════════

def test_lessons_expanded_quality_patterns():
    # 质量改进类表达（E4.21 前全部漏采）
    assert lessons.looks_like_correction("第三章太生硬了，重写一下")
    assert lessons.looks_like_correction("AI味太重了，自然一点")
    assert lessons.looks_like_correction("节奏太慢了，加快")
    assert lessons.looks_like_correction("这段太平淡单调，加点细节")
    assert lessons.looks_like_correction("写得更好一点")
    assert lessons.looks_like_correction("前后不一致")
    assert lessons.looks_like_correction("别用套话")
    # 明确纠错（原能力回归）
    assert lessons.looks_like_correction("时间线错了")
    assert lessons.looks_like_correction("称呼搞错了")
    # 正常创作指令不算纠错
    assert not lessons.looks_like_correction("帮我写第五章")
    assert not lessons.looks_like_correction("继续")
    assert not lessons.looks_like_correction("把大纲列出来")


def test_lessons_long_text_still_rejected():
    assert not lessons.looks_like_correction("错" * 500)


# ══════════════════════ 5. 洞察文案 ══════════════════════

def test_prompt_ab_scope_says_persisted(db):
    out = insight.prompt_ab(db)
    assert "累计历史" in out["scope"]
    assert "重启后清零" not in out["scope"]


def test_prompt_ab_empty_hint_no_longer_misleads(db):
    """旧文案"建议先开启遥测"是误导（洞察数据与遥测无关），必须消失。"""
    out = insight.prompt_ab(db)
    sug = out.get("suggestion", "")
    assert "建议先开启遥测" not in sug
    assert "自动积累" in sug


def test_lessons_e2e_still_records(db):
    """#315 回归：纠错 + 变更工具 → 落库（用扩充后的词典）。"""
    from apps.api import auth
    from apps.api.models import Book
    uname = f"e421_{uuid.uuid4().hex[:8]}"
    u = auth.create_user(db, uname, "e421-test-12345", role="user")
    db.commit()
    b = Book(id=f"b_{uuid.uuid4().hex[:8]}", owner_id=u.id,
             title="E421测试书")
    db.add(b)
    db.commit()
    ok = lessons.record_lesson(
        db, book_id=b.id, user_id=u.id,
        user_msg="第三章太生硬了，重写",
        tool_names=["draft_chapter"], fix_text="已重写",
        turn_key="t1")
    assert ok is True
    assert lessons.all_lessons(db)

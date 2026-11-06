# -*- coding: utf-8 -*-
"""v0.25.7 J11 全链路接入 + J3 加权轮询测试。

★ 覆盖：
  · J11：admit() 准入占位（原子）/ release_token / 生成入口接入 / 202 排队语义
  · J3：加权轮询 6:3:1（时间偏移而非固定权重）/ 泳道映射 / 优先级限幅
"""
from __future__ import annotations

import json
import time
import uuid

import pytest

from apps.api import queue as q


def _mod(name: str):
    import sys
    import importlib
    return (sys.modules.get(f"apps.api.{name}")
            or importlib.import_module(f"apps.api.{name}"))


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


@pytest.fixture()
def app_client():
    from fastapi.testclient import TestClient
    from apps.api.main import app
    with TestClient(app) as c:
        yield c


def _redis_ok() -> bool:
    try:
        c = q.client(force=True)
        return c is not None and bool(c.ping())
    except Exception:                                   # noqa: BLE001
        return False


need_redis = pytest.mark.skipif(not _redis_ok(), reason="需要真实 Redis")


def _reset_keys() -> None:
    c = q.client()
    if c is None:
        return
    try:
        c.delete(q.K_QUEUE, q.K_RUNNING, q.K_TASK, q.K_USER_RUN, q.K_BOOK_RUN)
    except Exception:                                   # noqa: BLE001
        pass


# ═══════════════════════════════════════════ J3 加权轮询

def test_lane_map():
    """★ 任务类型 → 泳道映射正确。"""
    assert q.lane_of("title") == "fast"
    assert q.lane_of("ping") == "fast"
    assert q.lane_of("draft") == "standard"
    assert q.lane_of("chat") == "standard"
    assert q.lane_of("audit_book") == "batch"
    assert q.lane_of("drama_batch") == "batch"
    assert q.lane_of("") == "standard"          # 未知默认 standard
    assert q.lane_of("weird_kind") == "standard"


def test_priority_bounded():
    """★★ 优先级必须限幅（否则用户填 99999 就能插队到所有人前面）。"""
    assert q.priority_of("title") > q.priority_of("draft")
    assert q.priority_of("draft") > q.priority_of("audit_book")
    assert q.priority_of("title", user_priority=99999) <= 1000
    assert q.priority_of("draft", user_priority=-99999) >= -1000


def test_empty_queue_fast_first():
    """★ 空载时 fast 优先（偏移小）。"""
    now = time.time()
    f = q.score_of(0, "fast", now)
    s = q.score_of(0, "standard", now)
    b = q.score_of(0, "batch", now)
    assert f < s < b, f"{f} / {s} / {b}"


def test_offset_is_seconds_not_days():
    """★★ J3 核心：偏移必须是**秒级**而非 1e6 秒（11.5 天）。

    原来 1e6 秒等于"永远轮不到" → standard 会被 fast 饿死。
    """
    assert q.LANE_WEIGHT["fast"] == 0
    assert q.LANE_WEIGHT["standard"] <= 60, \
        f"standard 偏移 {q.LANE_WEIGHT['standard']}s 过大，会饿死"
    # ★ 配比 1:3（standard : batch）
    ratio = q.LANE_WEIGHT["batch"] / q.LANE_WEIGHT["standard"]
    assert abs(ratio - 3.0) < 0.01, f"batch/standard 偏移比应为 3，实际 {ratio}"


def test_standard_promotes_after_wait():
    """★★ 持续有 fast 时，standard 等待后会被晋升（防饿死）。"""
    now = time.time()
    f_new = q.score_of(0, "fast", now)
    # 9 秒前入队的 standard（偏移 8 秒）
    s_wait = q.score_of(0, "standard", now - 9)
    assert s_wait < f_new, "★ standard 等够时间应优先于新 fast"


def test_batch_promotes_after_longer_wait():
    """★ batch 需要更久（3 倍偏移）才会被晋升。"""
    now = time.time()
    f_new = q.score_of(0, "fast", now)
    b_short = q.score_of(0, "batch", now - 10)      # 10 秒 < 24 秒偏移
    b_long = q.score_of(0, "batch", now - 25)       # 25 秒 > 24 秒偏移
    assert b_short > f_new, "batch 等 10 秒还不够"
    assert b_long < f_new, "★ batch 等 25 秒应被晋升"


def test_priority_still_works_within_lane():
    """★ 泳道内优先级仍然生效（加权轮询不破坏原有语义）。"""
    now = time.time()
    high = q.score_of(100, "standard", now)
    low = q.score_of(0, "standard", now)
    assert high < low, "同泳道内高优先级应更靠前"


def test_fifo_within_same_priority():
    """★ 同泳道同优先级 → FIFO（早入队先出）。"""
    now = time.time()
    early = q.score_of(0, "standard", now - 100)
    late = q.score_of(0, "standard", now)
    assert early < late


# ═══════════════════════════════════════════ J11 准入

def test_admit_no_redis_allows(db):
    """★★ 队列不可用时必须放行（降级为同步执行，行为确定）。"""
    if q.available():
        pytest.skip("Redis 可用，跳过降级用例")
    r = q.admit(db, user_id="u1", book_id="b1")
    assert r["allowed"] is True
    assert r.get("degraded") is True


@need_redis
def test_admit_grants_and_blocks(db):
    """★★ 准入：拿到占位后，同用户再申请应被拦。"""
    _reset_keys()
    uid = f"vaj_{uuid.uuid4().hex[:8]}"
    lim = q.concurrency_limits(db)
    toks = []
    try:
        granted = 0
        for _ in range(lim["per_user"] + 2):
            r = q.admit(db, user_id=uid, book_id=f"bb_{uuid.uuid4().hex[:4]}")
            if r["allowed"]:
                granted += 1
                toks.append(r["token"])
            else:
                assert r["reason"] == "user_full"
                assert r["text"]
                break
        assert granted == lim["per_user"], \
            f"应批准 {lim['per_user']} 次，实际 {granted}"
    finally:
        for t in toks:
            q.release_token(t, user_id=uid)
        _reset_keys()


@need_redis
def test_admit_book_serial(db):
    """★★ 同书准入只批 1 个（串行保证章节顺序）。"""
    _reset_keys()
    bid = f"vab_{uuid.uuid4().hex[:8]}"
    lim = q.concurrency_limits(db)
    users = [f"u_{uuid.uuid4().hex[:6]}" for _ in range(3)]
    toks = []
    try:
        granted = 0
        for u in users:
            r = q.admit(db, user_id=u, book_id=bid)
            if r["allowed"]:
                granted += 1
                toks.append(r["token"])
            else:
                assert r["reason"] == "book_full"
        assert granted == max(1, lim["per_book"]), \
            f"同书应只批 {lim['per_book']} 个，实际 {granted}"
    finally:
        for t, u in zip(toks, users):
            q.release_token(t, user_id=u, book_id=bid)
        _reset_keys()


@need_redis
def test_admit_global_full(db):
    """★★ 全局满时必须拦（这是并发控制的核心）。"""
    _reset_keys()
    lim = q.concurrency_limits(db)
    toks = []
    try:
        for i in range(lim["global"]):
            r = q.admit(db, user_id=f"ug_{i}", book_id=f"bg_{i}")
            assert r["allowed"] is True, f"第 {i+1} 个应通过"
            toks.append(r["token"])
        r2 = q.admit(db, user_id="ug_extra", book_id="bg_extra")
        assert r2["allowed"] is False
        assert r2["reason"] == "global_full"
        assert "忙碌" in r2["text"] or "排队" in r2["text"]
    finally:
        for i, t in enumerate(toks):
            q.release_token(t, user_id=f"ug_{i}", book_id=f"bg_{i}")
        _reset_keys()


@need_redis
def test_release_token_frees_slot(db):
    """★★ 释放占位后应能重新准入（漏了会永久锁死）。"""
    _reset_keys()
    uid = f"vrt_{uuid.uuid4().hex[:8]}"
    lim = q.concurrency_limits(db)
    toks = []
    try:
        for _ in range(lim["per_user"]):
            r = q.admit(db, user_id=uid, book_id=f"br_{uuid.uuid4().hex[:4]}")
            assert r["allowed"] is True
            toks.append(r["token"])
        # 满了
        assert q.admit(db, user_id=uid, book_id="br_x")["allowed"] is False
        # 释放一个
        q.release_token(toks.pop(), user_id=uid)
        assert q.admit(db, user_id=uid, book_id="br_y")["allowed"] is True
    finally:
        for t in toks:
            q.release_token(t, user_id=uid)
        _reset_keys()


@need_redis
def test_release_token_idempotent(db):
    """★ 重复释放不应把计数减成负数。"""
    _reset_keys()
    uid = f"vri_{uuid.uuid4().hex[:8]}"
    r = q.admit(db, user_id=uid, book_id="bi_1")
    tk = r["token"]
    q.release_token(tk, user_id=uid, book_id="bi_1")
    q.release_token(tk, user_id=uid, book_id="bi_1")     # 再来一次
    c = q.client()
    n = int(c.hget(q.K_USER_RUN, uid) or 0)
    assert n >= 0, f"计数不该为负：{n}"
    _reset_keys()


@need_redis
def test_admit_token_unique(db):
    """★ 每次准入的 token 必须唯一（否则占位会互相覆盖）。"""
    _reset_keys()
    toks = set()
    granted = []
    try:
        for i in range(3):
            r = q.admit(db, user_id=f"ut_{i}", book_id=f"bt_{i}")
            if r["allowed"]:
                toks.add(r["token"])
                granted.append((r["token"], f"ut_{i}", f"bt_{i}"))
        assert len(toks) == len(granted), "token 应唯一"
    finally:
        for t, u, b in granted:
            q.release_token(t, user_id=u, book_id=b)
        _reset_keys()


# ═══════════════════════════════════════════ 生成入口接入

def test_generate_endpoint_has_admission():
    """★★ 生成入口必须接入准入检查（这是 J11 的核心）。"""
    import io as _io
    src = _io.open("apps/api/main.py", encoding="utf-8").read()
    # 找到 generate 端点
    i = src.find('@app.post("/api/books/{book_id}/generate")')
    assert i > 0, "未找到 generate 端点"
    seg = src[i:i + 6000]
    assert "admit(" in seg, "★ 生成入口未接入准入检查"
    assert "release_token" in seg, "★★ 必须释放占位（漏了会永久锁死）"
    assert "202" in seg, "★ 排队应返回 202（已接受但未开始）"
    assert "queued" in seg


def test_admission_before_stream():
    """★★ 准入必须在**建流之前**（否则用户看到卡住不动的流）。"""
    import io as _io
    src = _io.open("apps/api/main.py", encoding="utf-8").read()
    i = src.find('@app.post("/api/books/{book_id}/generate")')
    seg = src[i:i + 6000]
    i_admit = seg.find("admit(")
    i_stream = seg.find("StreamingResponse")
    assert i_admit > 0 and i_stream > 0
    assert i_admit < i_stream, "★ 准入必须在建流之前"


def test_release_in_finally():
    """★★ 释放占位必须在 finally 里（异常也要释放）。"""
    import io as _io
    src = _io.open("apps/api/main.py", encoding="utf-8").read()
    i = src.find('@app.post("/api/books/{book_id}/generate")')
    seg = src[i:i + 6000]
    i_finally = seg.find("finally:")
    i_release = seg.find("release_token")
    assert i_finally > 0 and i_release > 0
    assert i_release > i_finally, "★ release_token 应在 finally 块内"


# ═══════════════════════════════════════════ HTTP 端到端

def _token(role: str = "admin"):
    from apps.api.auth import make_token
    from apps.api.db import SessionLocal
    from apps.api.models import User
    s = SessionLocal()
    try:
        u = User(id=f"u_v711_{uuid.uuid4().hex[:8]}",
                 username=f"v711_{uuid.uuid4().hex[:6]}",
                 role=role, pw_hash="x", is_active=1)
        s.add(u)
        s.commit()
        return make_token(u.id)
    finally:
        s.close()


def test_http_admission(app_client):
    """★ 准入预检接口（★ 队列不可用时降级返回，字段会少 blockers）。"""
    tok = _token()
    r = app_client.get("/api/admin/queue/admission",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert "can_admit" in d
    assert "limits" in d
    if d.get("degraded"):
        # ★ 降级路径：队列不可用 → 放行（行为确定）
        assert d["can_admit"] is True
        assert d.get("reason")
    else:
        assert "blockers" in d
        assert isinstance(d["blockers"], list)
        assert "in_flight" in d


def test_http_admission_simulate_user(app_client):
    """★ 可指定用户做模拟预检（客服排查用）。"""
    tok = _token()
    r = app_client.get("/api/admin/queue/admission?user_id=u_test&book_id=b_test",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    d = r.json()
    assert "can_admit" in d
    if not d.get("degraded"):
        assert "in_flight" in d


def test_http_lanes(app_client):
    """★ 泳道配比接口（解释 6:3:1 是怎么实现的）。"""
    tok = _token()
    r = app_client.get("/api/admin/queue/lanes",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ratio"].startswith("6:3:1")
    assert d["round_seconds"] > 0
    assert "explain" in d and d["explain"]
    assert set(d["lanes"]) == {"fast", "standard", "batch"}


def test_http_requires_auth(app_client):
    for p in ("/api/admin/queue/admission", "/api/admin/queue/lanes"):
        r = app_client.get(p)
        assert r.status_code == 401, f"{p} 应要求认证"


def test_http_static_not_shadowed(app_client):
    """★★ 静态路径不能被动态路径遮蔽。"""
    tok = _token()
    h = {"Authorization": f"Bearer {tok}"}
    for p in ("/api/admin/queue/admission", "/api/admin/queue/lanes",
              "/api/admin/queue/dashboard", "/api/admin/queue/concurrency"):
        r = app_client.get(p, headers=h)
        assert r.status_code == 200, f"{p} 被遮蔽（{r.status_code}）"

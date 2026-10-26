# -*- coding: utf-8 -*-
"""v0.25.6 J 组剩余（J5 并发控制 / J8 排队 UI / J9 看板 / J10 告警）测试。

★ 需要真实 Redis 的用例会 skip（本地未装时），线上验证覆盖。
"""
from __future__ import annotations

import json
import os
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
        if c is None:
            return False
        c.ping()
        return True
    except Exception:                                   # noqa: BLE001
        return False


need_redis = pytest.mark.skipif(not _redis_ok(), reason="需要真实 Redis")


def _cleanup(*task_ids: str) -> None:
    c = q.client()
    if c is None:
        return
    try:
        for t in task_ids:
            c.zrem(q.K_QUEUE, t)
            c.zrem(q.K_RUNNING, t)
            c.hdel(q.K_TASK, t)
    except Exception:                                   # noqa: BLE001
        pass


# ═══════════════════════════════════════════ J5 并发控制

def test_concurrency_limits_defaults():
    """★ 默认限额必须存在（0 = 不限，但那不该是默认）。"""
    lim = q.concurrency_limits(None)
    for k in ("global", "per_user", "per_book"):
        assert k in lim
        assert isinstance(lim[k], int)
    assert lim["global"] > 0, "★ 默认不该是无限并发"
    assert lim["per_user"] > 0
    assert lim["per_book"] >= 1, "★ 同书应串行（防章节乱序）"


def test_concurrency_limits_from_config(db):
    """★ 限额可由配置中心覆盖。"""
    from apps.api import config_center as cc
    lim0 = q.concurrency_limits(db)
    try:
        cc.set_config(db, "queue.max_per_user", 5, operator="t", reason="测试")
        cc.cache_clear()
        assert q.concurrency_limits(db)["per_user"] == 5
    finally:
        from apps.api.models import Setting
        row = db.get(Setting, "queue.max_per_user")
        if row:
            db.delete(row)
            db.commit()
        cc.cache_clear()
    assert q.concurrency_limits(db)["per_user"] == lim0["per_user"]


def test_concurrency_limits_bad_config_safe(db):
    """★★ 配置读失败不能变成"无限并发"（安全默认）。"""
    from apps.api import config_center as cc
    orig = cc.get_config
    try:
        cc.get_config = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        lim = q.concurrency_limits(db)
        assert lim["global"] == q.MAX_GLOBAL_CONCURRENCY
        assert lim["global"] > 0
    finally:
        cc.get_config = orig


@need_redis
def test_global_concurrency_blocks(db):
    """★★ 全局并发满时必须拦住（这是并发控制的核心价值）。"""
    ids = [f"vt_cg_{uuid.uuid4().hex[:8]}" for _ in range(6)]
    try:
        lim = q.concurrency_limits(db)
        # 占满全局并发
        for i in range(lim["global"]):
            tid = ids[i]
            q.enqueue(db, task_id=tid, kind="t", user_id=f"u{i}", book_id=f"b{i}")
            got = q.dequeue(worker_id="w_test", db=db)
            assert got and not got.get("_blocked"), f"第 {i+1} 个应能出队"
        # 再出队 → 应被拦
        tid = ids[-1]
        q.enqueue(db, task_id=tid, kind="t", user_id="uz", book_id="bz")
        blocked = q.dequeue(worker_id="w_test", db=db)
        assert blocked is not None and blocked.get("_blocked") is True, \
            "★ 全局并发满时应返回 _blocked"
        assert blocked.get("reason") == "global_full"
        assert str(lim["global"]) in blocked.get("text", "")
    finally:
        _cleanup(*ids)
        # 清并发计数
        c = q.client()
        if c:
            try:
                c.delete(q.K_USER_RUN, q.K_BOOK_RUN)
            except Exception:                       # noqa: BLE001
                pass


@need_redis
def test_per_user_concurrency_blocks(db):
    """★★ 单用户并发上限必须生效（防一人打满机器）。"""
    c = q.client()
    if c:
        c.delete(q.K_USER_RUN, q.K_BOOK_RUN, q.K_RUNNING)
    uid = f"vu_{uuid.uuid4().hex[:8]}"
    ids = [f"vt_cu_{uuid.uuid4().hex[:8]}" for _ in range(5)]
    try:
        lim = q.concurrency_limits(db)
        # 同一用户连续入队
        for tid in ids:
            q.enqueue(db, task_id=tid, kind="t", user_id=uid, book_id=f"bb{uuid.uuid4().hex[:4]}")
        ok_n = 0
        for _ in range(5):
            r = q.dequeue(worker_id="w_test", db=db)
            if r and not r.get("_blocked"):
                ok_n += 1
            elif r and r.get("reason") == "throttled":
                break
        assert ok_n <= lim["per_user"], \
            f"★ 单用户并发 {ok_n} 超过上限 {lim['per_user']}"
    finally:
        _cleanup(*ids)
        if c:
            try:
                c.delete(q.K_USER_RUN, q.K_BOOK_RUN, q.K_RUNNING)
            except Exception:                       # noqa: BLE001
                pass


@need_redis
def test_per_book_serial(db):
    """★★ 同一本书必须串行（否则章节顺序会乱）。"""
    c = q.client()
    if c:
        c.delete(q.K_USER_RUN, q.K_BOOK_RUN, q.K_RUNNING, q.K_QUEUE)
    bid = f"vb_{uuid.uuid4().hex[:8]}"
    ids = [f"vt_cb_{uuid.uuid4().hex[:8]}" for _ in range(3)]
    try:
        for tid in ids:
            q.enqueue(db, task_id=tid, kind="t",
                      user_id=f"u_{uuid.uuid4().hex[:6]}", book_id=bid)
        got = []
        for _ in range(3):
            r = q.dequeue(worker_id="w_test", db=db)
            if r is None:
                break
            got.append(r)
        running_same_book = [x for x in got if not x.get("_blocked")]
        assert len(running_same_book) <= 1, \
            f"★ 同书并发 {len(running_same_book)} > 1（章节会乱序）"
    finally:
        _cleanup(*ids)
        if c:
            try:
                c.delete(q.K_USER_RUN, q.K_BOOK_RUN, q.K_RUNNING, q.K_QUEUE)
            except Exception:                       # noqa: BLE001
                pass


@need_redis
def test_release_on_finish(db):
    """★★ 任务结束必须释放并发计数（否则用户被永久锁死）。"""
    c = q.client()
    if c:
        c.delete(q.K_USER_RUN, q.K_BOOK_RUN, q.K_RUNNING, q.K_QUEUE)
    uid = f"vr_{uuid.uuid4().hex[:8]}"
    tid = f"vt_rl_{uuid.uuid4().hex[:8]}"
    try:
        q.enqueue(db, task_id=tid, kind="t", user_id=uid, book_id="bb1")
        got = q.dequeue(worker_id="w", db=db)
        assert got and not got.get("_blocked")
        n_before = int(c.hget(q.K_USER_RUN, uid) or 0)
        assert n_before >= 1
        q.finish(tid, ok=True)
        n_after = int(c.hget(q.K_USER_RUN, uid) or 0)
        assert n_after < n_before, "★ finish 后计数必须减少"
    finally:
        _cleanup(tid)
        if c:
            try:
                c.delete(q.K_USER_RUN, q.K_BOOK_RUN, q.K_RUNNING)
            except Exception:                       # noqa: BLE001
                pass


@need_redis
def test_release_on_requeue(db):
    """★★ 重试也必须释放计数（否则重试几次就自我锁死）。"""
    c = q.client()
    if c:
        c.delete(q.K_USER_RUN, q.K_BOOK_RUN, q.K_RUNNING, q.K_QUEUE)
    uid = f"vq_{uuid.uuid4().hex[:8]}"
    tid = f"vt_rq_{uuid.uuid4().hex[:8]}"
    try:
        q.enqueue(db, task_id=tid, kind="t", user_id=uid, book_id="bb2")
        q.dequeue(worker_id="w", db=db)
        n1 = int(c.hget(q.K_USER_RUN, uid) or 0)
        assert n1 >= 1
        q.requeue(tid, delay=0)
        n2 = int(c.hget(q.K_USER_RUN, uid) or 0)
        assert n2 < n1, "★ requeue 后计数必须减少"
    finally:
        _cleanup(tid)
        if c:
            try:
                c.delete(q.K_USER_RUN, q.K_BOOK_RUN, q.K_RUNNING)
            except Exception:                       # noqa: BLE001
                pass


@need_redis
def test_reap_stale_releases_count(db):
    """★★ 僵尸回收必须释放计数（最容易被漏的一处）。

    漏了的后果：worker 被 OOM kill → 计数只增不减 →
    该用户配额被"幽灵任务"占满 → 一直排队但永不开始（极难排查）。
    """
    c = q.client()
    if c:
        c.delete(q.K_USER_RUN, q.K_BOOK_RUN, q.K_RUNNING, q.K_QUEUE)
    uid = f"vz_{uuid.uuid4().hex[:8]}"
    tid = f"vt_z_{uuid.uuid4().hex[:8]}"
    try:
        q.enqueue(db, task_id=tid, kind="t", user_id=uid, book_id="bb3")
        q.dequeue(worker_id="w", db=db)
        assert int(c.hget(q.K_USER_RUN, uid) or 0) >= 1
        # 把 running 时间戳改到很久以前 → 当作超时
        c.zadd(q.K_RUNNING, {tid: time.time() - 99999})
        r = q.reap_stale(timeout_seconds=1800)
        assert r.get("ok") is True
        assert int(c.hget(q.K_USER_RUN, uid) or 0) == 0, \
            "★ 僵尸回收后计数必须归零"
    finally:
        _cleanup(tid)
        if c:
            try:
                c.delete(q.K_USER_RUN, q.K_BOOK_RUN, q.K_RUNNING)
            except Exception:                       # noqa: BLE001
                pass


def test_stats_has_concurrency_section(db):
    """★ 统计要暴露并发信息（运维要看余量）。"""
    st = q.stats()
    if st.get("available"):
        assert "concurrency" in st
        c = st["concurrency"]
        for k in ("limit_global", "limit_per_user", "limit_per_book",
                  "in_flight", "saturation"):
            assert k in c, f"缺字段 {k}"
        assert 0 <= float(c["saturation"]) <= 10
    else:
        assert st.get("mode") == "degraded"


# ═══════════════════════════════════════════ J8 排队位置与 ETA

def test_queue_position_no_redis_safe():
    """★ 队列不可用时不能崩（返回结构化失败）。"""
    r = q.queue_position("not_exist_task")
    assert isinstance(r, dict)
    assert "ok" in r


@need_redis
def test_queue_position_reports_rank(db):
    c = q.client()
    if c:
        c.delete(q.K_QUEUE, q.K_RUNNING, q.K_TASK)
    ids = [f"vt_pos_{uuid.uuid4().hex[:8]}" for _ in range(3)]
    try:
        for tid in ids:
            q.enqueue(db, task_id=tid, kind="t", priority=0,
                      user_id="u1", book_id=f"b_{uuid.uuid4().hex[:4]}")
        r = q.queue_position(ids[1])            # 第 2 个入队
        assert r.get("ok") is True
        assert r.get("state") == "queued"
        assert r.get("position") == 2
        assert r.get("ahead") == 1
        assert r.get("eta_text", "").startswith("约 ")
        lo, hi = r.get("eta_seconds") or [0, 0]
        assert 0 < lo <= hi, "ETA 应有合理区间"
    finally:
        _cleanup(*ids)


@need_redis
def test_queue_position_unknown_task(db):
    """★ 不存在的任务 → state=unknown（不是报错）。"""
    r = q.queue_position(f"vt_none_{uuid.uuid4().hex[:8]}")
    assert r.get("ok") is True
    assert r.get("state") == "unknown"


def test_eta_format():
    assert "秒" in q._fmt_eta(5, 30)
    assert "分钟" in q._fmt_eta(60, 300)
    assert "小时" in q._fmt_eta(3700, 7200)


def test_avg_duration_default():
    """★ 无历史时给保守默认值（不能让 ETA 为 0）。"""
    class _Fake:
        def hgetall(self, k):                           # noqa: ARG002
            return {}
    assert q._avg_duration_seconds(_Fake()) == 90.0


# ═══════════════════════════════════════════ J10 告警

def test_check_alerts_structure(db):
    """★ 告警检查返回结构稳定。"""
    r = q.check_alerts(db)
    assert "alerts" in r
    assert isinstance(r["alerts"], list)
    if r.get("ok"):
        assert "limits" in r and "metrics" in r
        for a in r["alerts"]:
            for k in ("key", "level", "title", "message", "suggest"):
                assert k in a, f"告警缺字段 {k}"


def test_alert_dlq_triggers(db):
    """★★ 死信非空必须告警（需人工介入）。"""
    c = q.client()
    if c is None:
        pytest.skip("需要 Redis")
    tid = f"vt_dlq_{uuid.uuid4().hex[:8]}"
    try:
        c.lpush(q.K_DLQ, json.dumps({"task_id": tid, "reason": "测试"}))
        r = q.check_alerts(db)
        keys = [a["key"] for a in (r.get("alerts") or [])]
        assert "dlq" in keys, "★ 死信非空应触发告警"
    finally:
        try:
            c.lrem(q.K_DLQ, 0, json.dumps({"task_id": tid, "reason": "测试"}))
        except Exception:                               # noqa: BLE001
            pass


def test_alert_throttle():
    """★★ 告警必须节流（否则刷屏 → 用户关通知 → 真出事看不到）。"""
    q.alert_state_reset()
    assert q._throttled("test:key", cooldown=3600) is False   # 首次放行
    assert q._throttled("test:key", cooldown=3600) is True    # 第二次拦
    q.alert_state_reset()
    assert q._throttled("test:key", cooldown=3600) is False   # 重置后放行
    q.alert_state_reset()


def test_notify_admin_template():
    """★ 运维告警模板存在且能渲染。"""
    from apps.api import notify as nf
    assert "queue.alert" in nf.TEMPLATES
    r = nf.render_event("queue.alert", {"title": "队列积压",
                                        "message": "100 个",
                                        "suggest": "检查 worker"})
    assert "队列积压" in r["title"]
    assert "检查 worker" in r["body"]
    assert r["kind"] == "system"


def test_notify_admin_callable(db):
    """★ notify_admin 存在且不抛异常（即使没管理员）。"""
    from apps.api import notify as nf
    assert hasattr(nf, "notify_admin")
    r = nf.notify_admin(db, event="queue.alert",
                        ctx={"title": "T", "message": "M", "suggest": "S"})
    assert isinstance(r, dict)


# ═══════════════════════════════════════════ HTTP 端到端

def _token(role: str = "admin"):
    from apps.api.auth import make_token
    from apps.api.db import SessionLocal
    from apps.api.models import User
    s = SessionLocal()
    try:
        u = User(id=f"u_vq_{uuid.uuid4().hex[:8]}",
                 username=f"vq_{uuid.uuid4().hex[:6]}",
                 role=role, pw_hash="x", is_active=1)
        s.add(u)
        s.commit()
        return make_token(u.id)
    finally:
        s.close()


def test_http_concurrency(app_client):
    tok = _token()
    r = app_client.get("/api/admin/queue/concurrency",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert "limits" in d and "hint" in d
    assert "in_flight" in d


def test_http_dashboard(app_client):
    tok = _token()
    r = app_client.get("/api/admin/queue/dashboard",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    d = r.json()
    if d.get("available"):
        assert "peek" in d and "dlq_items" in d and "limits" in d


def test_http_alerts(app_client):
    tok = _token()
    r = app_client.get("/api/admin/queue/alerts",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "alerts" in r.json()


def test_http_alerts_reset(app_client):
    tok = _token()
    r = app_client.post("/api/admin/queue/alerts/reset", json={},
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_http_position_public(app_client):
    """★★ 排队位置**匿名可访问**（否则令牌过期进度条就失效）。"""
    r = app_client.get("/api/queue/position?task_id=vt_probe_1234")
    assert r.status_code == 200, r.text


def test_http_position_requires_id(app_client):
    r = app_client.get("/api/queue/position")
    assert r.status_code == 400


def test_http_mine_no_auth_ok(app_client):
    """★ 未登录查「我的任务」→ 空列表（不是 401，前端不必处理异常）。"""
    r = app_client.get("/api/queue/mine")
    assert r.status_code == 200
    d = r.json()
    assert d.get("count") == 0


def test_http_mine_with_auth(app_client):
    tok = _token()
    r = app_client.get("/api/queue/mine",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    d = r.json()
    assert "items" in d


def test_http_admin_queue_requires_auth(app_client):
    for p in ("/api/admin/queue/concurrency", "/api/admin/queue/dashboard",
              "/api/admin/queue/alerts"):
        r = app_client.get(p)
        assert r.status_code == 401, f"{p} 应要求认证"


def test_http_clear_dlq_accepts_reason(app_client):
    """★ 清空死信要能带原因（审计留痕）。"""
    tok = _token()
    r = app_client.post("/api/admin/queue/clear-dlq",
                        json={"reason": "测试清理"},
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert r.json().get("reason") == "测试清理"


def test_http_static_paths_not_shadowed(app_client):
    """★★ 静态路径不能被动态路径遮蔽。"""
    tok = _token()
    h = {"Authorization": f"Bearer {tok}"}
    for p in ("/api/admin/queue/concurrency", "/api/admin/queue/dashboard",
              "/api/admin/queue/alerts", "/api/admin/queue/dashboard"):
        r = app_client.get(p, headers=h)
        assert r.status_code == 200, f"{p} 被遮蔽（{r.status_code}）"


# ═══════════════════════════════════════════ 前端守卫

def test_admin_has_queue_page():
    import io as _io
    js = _io.open("apps/web/admin/admin.js", encoding="utf-8").read()
    html = _io.open("apps/web/admin/index.html", encoding="utf-8").read()
    assert "RENDER.queue" in js, "缺队列看板页"
    assert 'data-page="queue"' in html, "导航缺队列入口"
    assert "queue" in js and "并发配置" in js
    css = _io.open("apps/web/admin/admin.css", encoding="utf-8").read()
    assert ".alert-item" in css


def test_user_frontend_has_queue_watch():
    """★ 用户端要有排队状态轮询（否则用户以为卡死反复点）。"""
    import io as _io
    js = _io.open("apps/web/app.js", encoding="utf-8").read()
    assert "startQueueWatch" in js, "缺排队状态轮询"
    assert "/api/queue/position" in js
    assert "后台运行" in js, "缺「后台运行」入口"
    assert "eta_text" in js, "应展示 ETA"

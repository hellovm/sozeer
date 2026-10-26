"""v0.25 J0 组测试：Redis 队列与降级重建。

覆盖：
  J0-1 连接与配置 —— 配置读取 / URL 掩码 / 未安装降级 / 熔断
  J0-2 降级重建   —— DB 回灌 / running 重置 / 僵死判失败 / dry-run / 队列原语
"""
from __future__ import annotations

import os
import time
import uuid

import pytest

from apps.api import queue as q


def _mod(name: str):
    import sys
    import importlib
    return sys.modules.get(f"apps.api.{name}") or importlib.import_module(f"apps.api.{name}")


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


@pytest.fixture(autouse=True)
def _clean_env():
    """清理 Redis 环境变量 + 重置连接状态。"""
    keys = ["NF_REDIS_URL", "NF_REDIS_DISABLE"]
    saved = {k: os.environ.pop(k, None) for k in keys}
    q.reset()
    yield
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v
    q.reset()


def _redis_available() -> bool:
    """真实 Redis 是否可用（决定部分用例是否跳过）。"""
    os.environ.pop("NF_REDIS_DISABLE", None)
    q.reset()
    return q.available()


has_redis = _redis_available()


def _mk_task(db, *, status: str = "queued", hours_old: int = 0,
             priority: int = 0, lane: str = "standard"):
    import datetime as dt
    from apps.api.models import Task
    t = Task(id=f"tk_{uuid.uuid4().hex[:10]}",
             book_id=f"bk_{uuid.uuid4().hex[:8]}", kind="draft",
             status=status, priority=priority, lane=lane,
             owner_id=f"u_{uuid.uuid4().hex[:6]}",
             started_at=dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
             - dt.timedelta(hours=hours_old))
    db.add(t)
    db.commit()
    return t


# ============================================================ J0-1 配置

def test_redis_url_default():
    os.environ.pop("NF_REDIS_URL", None)
    assert q.redis_url() == q.DEFAULT_URL


def test_redis_url_from_env():
    os.environ["NF_REDIS_URL"] = "redis://myhost:6380/1"
    assert q.redis_url() == "redis://myhost:6380/1"


def test_mask_url_hides_password():
    """★ URL 里的密码必须掩码（日志/接口都可能带出去）。"""
    out = q._mask_url("redis://user:secret123@host:6379/0")
    assert "secret123" not in out
    assert "***" in out


def test_mask_url_no_password():
    assert q._mask_url("redis://127.0.0.1:6379/0") == "redis://127.0.0.1:6379/0"


def test_forced_disable_env():
    """★ 显式禁用开关（测试/本地开发用）。"""
    os.environ["NF_REDIS_DISABLE"] = "1"
    assert q.is_forced_disabled() is True
    assert q.available() is False
    st = q.status()
    assert st["available"] is False and st["mode"] == "degraded"


def test_disabled_reason_reported():
    os.environ["NF_REDIS_DISABLE"] = "1"
    st = q.status()
    assert "NF_REDIS_DISABLE" in st.get("reason", "")


def test_status_structure_when_down():
    """★ 降级时必须给**可操作**的提示。"""
    os.environ["NF_REDIS_DISABLE"] = "1"
    st = q.status()
    for k in ("available", "mode", "reason", "url", "hint"):
        assert k in st, k
    assert "同步执行" in st["hint"]


def test_status_structure_when_up():
    if not has_redis:
        pytest.skip("Redis 不可用")
    st = q.status()
    assert st["available"] is True and st["mode"] == "queue"
    for k in ("version", "queued", "running", "dlq", "aof_enabled"):
        assert k in st, k


def test_self_check_reports():
    st = q.self_check()
    assert "available" in st and "mode" in st


def test_safe_wrapper_returns_default_when_down():
    """★★ safe() 任何异常都不冒泡（队列是旁路）。"""
    os.environ["NF_REDIS_DISABLE"] = "1"
    assert q.safe(lambda c: c.ping(), default="D") == "D"


def test_safe_wrapper_passes_through_when_up():
    if not has_redis:
        pytest.skip("Redis 不可用")
    assert q.safe(lambda c: c.ping(), default=None) is True


def test_mark_fail_triggers_circuit_breaker():
    """★ 连续失败触发熔断（避免每次操作都等超时）。"""
    q.reset()
    for i in range(q.FAIL_THRESHOLD):
        q.mark_fail(RuntimeError(f"err{i}"))
    st = q.status()
    # 熔断后 client() 返回 None
    assert q.client() is None
    assert st["available"] is False


def test_mark_ok_resets_fails():
    q.reset()
    q.mark_fail(RuntimeError("x"))
    q.mark_ok()
    assert q._fails == 0


def test_reset_clears_state():
    q.mark_fail(RuntimeError("x"))
    q.reset()
    assert q._fails == 0 and q._down_until == 0.0


# ============================================================ 评分（泳道/优先级）

def test_lane_weight_ordering():
    """★ fast < standard < batch（越小越先出队）。"""
    t = time.time()
    assert q.score_of(0, "fast", t) < q.score_of(0, "standard", t)
    assert q.score_of(0, "standard", t) < q.score_of(0, "batch", t)


def test_priority_within_lane():
    """★ 同泳道内高优先级分更小（更先出队）。"""
    t = time.time()
    assert q.score_of(100, "standard", t) < q.score_of(0, "standard", t)


def test_fifo_within_same_priority():
    """★ 同优先级 FIFO：早入队的分更小。"""
    base = time.time()
    assert q.score_of(0, "standard", base) < q.score_of(0, "standard", base + 10)


def test_priority_prevents_starvation():
    """★ 等待越久越靠前 —— 天然实现防饥饿（J4）。

    ★ 注意时间方向：**500 秒前入队** → ts = now - 500（更小的时间戳）。
      写成 now + 500 会变成"未来入队"，结论正好相反（踩过这个坑）。
    """
    now = time.time()
    # 500 秒前入队的低优先级 vs 刚入队的高优先级（优先级 100 = 100 秒提前量）
    old_low = q.score_of(0, "standard", now - 500)
    new_high = q.score_of(100, "standard", now)
    assert old_low < new_high, "等久了的任务应先出队（防饥饿）"


def test_priority_wins_when_wait_short():
    """★ 反之：等待时间短于优先级提前量时，高优先级胜出。"""
    now = time.time()
    slightly_old_low = q.score_of(0, "standard", now - 10)   # 只等了 10 秒
    new_high = q.score_of(100, "standard", now)              # 优先级 100
    assert new_high < slightly_old_low, "优先级应能压过少量等待"


def test_priority_clamped():
    """★ 极端优先级不越界（防止泳道权重被击穿）。"""
    t = time.time()
    s_extreme = q.score_of(999999, "fast", t)
    s_batch = q.score_of(-999999, "batch", t)
    assert s_extreme < s_batch, "fast + 极端高优先级不应低于 batch"


def test_lane_fallback():
    """非法泳道回落到 standard。"""
    t = time.time()
    assert q.score_of(0, "乱写", t) == q.score_of(0, "standard", t)


# ============================================================ 队列原语

def test_enqueue_degraded_when_down():
    """★ 队列不可用时 enqueue 返回降级信号（调用方据此同步执行）。"""
    os.environ["NF_REDIS_DISABLE"] = "1"
    r = q.enqueue(None, task_id="t1", kind="draft")
    assert r["ok"] is False and r["degraded"] is True


def test_dequeue_returns_none_when_down():
    os.environ["NF_REDIS_DISABLE"] = "1"
    assert q.dequeue() is None


def test_finish_false_when_down():
    os.environ["NF_REDIS_DISABLE"] = "1"
    assert q.finish("t1") is False


def test_to_dlq_false_when_down():
    os.environ["NF_REDIS_DISABLE"] = "1"
    assert q.to_dlq("t1", "x") is False


def test_stats_degraded():
    os.environ["NF_REDIS_DISABLE"] = "1"
    st = q.stats()
    assert st["available"] is False and st["mode"] == "degraded"


def test_peek_empty_when_down():
    os.environ["NF_REDIS_DISABLE"] = "1"
    assert q.peek() == []


def test_reap_when_down():
    os.environ["NF_REDIS_DISABLE"] = "1"
    r = q.reap_stale()
    assert r["ok"] is False


@pytest.mark.skipif(not has_redis, reason="需要真实 Redis")
def test_enqueue_dequeue_roundtrip():
    """★ 入队出队往返（真实 Redis）。"""
    q.reset()
    tid = f"tk_{uuid.uuid4().hex[:8]}"
    r = q.enqueue(None, task_id=tid, kind="draft", priority=5)
    assert r["ok"] is True
    d = q.dequeue(worker_id="w1")
    assert d is not None
    assert d["task_id"] == tid
    assert d["worker_id"] == "w1"
    q.finish(tid)
    assert q.safe(lambda c: c.zscore(q.K_RUNNING, tid), default=None) is None


@pytest.mark.skipif(not has_redis, reason="需要真实 Redis")
def test_enqueue_idempotent():
    """★ 同一 task_id 重复入队只生效一次（NX）。"""
    q.reset()
    tid = f"tk_{uuid.uuid4().hex[:8]}"
    q.enqueue(None, task_id=tid)
    n1 = q.safe(lambda c: c.zcard(q.K_QUEUE), default=0)
    q.enqueue(None, task_id=tid)
    n2 = q.safe(lambda c: c.zcard(q.K_QUEUE), default=0)
    assert n2 == n1
    q.safe(lambda c: c.zrem(q.K_QUEUE, tid))
    q.safe(lambda c: c.hdel(q.K_TASK, tid))


@pytest.mark.skipif(not has_redis, reason="需要真实 Redis")
def test_lane_priority_order():
    """★ 出队顺序遵循泳道优先级（真实 Redis）。"""
    q.reset()
    q.safe(lambda c: c.delete(q.K_QUEUE, q.K_TASK, q.K_RUNNING))
    ids = {ln: f"tk_{ln}_{uuid.uuid4().hex[:6]}"
           for ln in ("batch", "standard", "fast")}
    for ln, tid in ids.items():
        q.enqueue(None, task_id=tid, lane=ln)
    order = []
    for _ in range(3):
        d = q.dequeue()
        if d:
            order.append(d["lane"])
            q.finish(d["task_id"])
    assert order == ["fast", "standard", "batch"], order


@pytest.mark.skipif(not has_redis, reason="需要真实 Redis")
def test_dlq_flow():
    """★ 死信流程：进队 → 出队 → 进死信。"""
    q.reset()
    tid = f"tk_{uuid.uuid4().hex[:8]}"
    q.enqueue(None, task_id=tid)
    q.dequeue()
    assert q.to_dlq(tid, "测试原因") is True
    items = q.safe(lambda c: c.lrange(q.K_DLQ, 0, 9), default=[])
    assert any(tid in str(x) for x in items)
    q.clear_dlq()


@pytest.mark.skipif(not has_redis, reason="需要真实 Redis")
def test_reap_stale_running():
    """★ 回收僵尸任务（worker 崩溃后 running 里的残留）。"""
    q.reset()
    tid = f"tk_{uuid.uuid4().hex[:8]}"
    q.enqueue(None, task_id=tid)
    q.dequeue()                             # 进入 running
    # 手动把 running 的时间戳改到很久以前
    q.safe(lambda c: c.zadd(q.K_RUNNING, {tid: time.time() - 100000}))
    r = q.reap_stale(timeout_seconds=60)
    assert r["ok"] is True and r["reaped"] >= 1
    q.safe(lambda c: c.zrem(q.K_QUEUE, tid))
    q.safe(lambda c: c.hdel(q.K_TASK, tid))


@pytest.mark.skipif(not has_redis, reason="需要真实 Redis")
def test_requeue_with_delay():
    q.reset()
    tid = f"tk_{uuid.uuid4().hex[:8]}"
    q.enqueue(None, task_id=tid)
    q.dequeue()
    assert q.requeue(tid, delay=1) is True
    q.safe(lambda c: c.zrem(q.K_QUEUE, tid))
    q.safe(lambda c: c.hdel(q.K_TASK, tid))


# ============================================================ J0-2 重建

def test_rebuild_dry_run_no_change(db):
    """★ dry_run 只报"会改什么"，不改数据。"""
    if not has_redis:
        pytest.skip("需要真实 Redis 才能重建")
    t = _mk_task(db, status="queued")
    r = q.rebuild_from_db(db, dry_run=True)
    assert r["ok"] is True and r["dry_run"] is True
    assert r["scanned"] >= 1
    db.refresh(t)
    assert t.status == "queued"
    db.delete(t)
    db.commit()


def test_rebuild_resets_running_to_queued(db):
    """★★ running 任务在重建时重置为 queued（原 worker 已死）。"""
    if not has_redis:
        pytest.skip("需要真实 Redis 才能重建")
    t = _mk_task(db, status="running")
    q.rebuild_from_db(db, dry_run=False)
    db.refresh(t)
    assert t.status == "queued", "running 任务应被重置"
    assert t.worker_id == ""
    db.delete(t)
    db.commit()


def test_rebuild_resets_abandoned(db):
    """★ abandoned（服务重启遗留）也纳入重建。"""
    if not has_redis:
        pytest.skip("需要真实 Redis 才能重建")
    t = _mk_task(db, status="abandoned")
    q.rebuild_from_db(db, dry_run=False)
    db.refresh(t)
    assert t.status == "queued"
    db.delete(t)
    db.commit()


def test_rebuild_marks_stale_as_error(db):
    """★★ 僵死任务判失败（不无限重试）。"""
    if not has_redis:
        pytest.skip("需要真实 Redis 才能重建")
    t = _mk_task(db, status="queued", hours_old=20)      # 超过 6 小时阈值
    r = q.rebuild_from_db(db, dry_run=False)
    assert r["stale"] >= 1
    db.refresh(t)
    assert t.status == "error", "僵死任务应判失败"
    assert "僵死" in (t.error or "")
    db.delete(t)
    db.commit()


def test_rebuild_requeues_fresh(db):
    """正常任务重新入队。"""
    if not has_redis:
        pytest.skip("需要真实 Redis 才能重建")
    t = _mk_task(db, status="queued", hours_old=0, priority=7, lane="fast")
    r = q.rebuild_from_db(db, dry_run=False)
    assert r["requeued"] >= 1
    # 应真的在 Redis 队列里
    sc = q.safe(lambda c: c.zscore(q.K_QUEUE, t.id), default=None)
    assert sc is not None, "任务未入队"
    q.safe(lambda c: c.zrem(q.K_QUEUE, t.id))
    q.safe(lambda c: c.hdel(q.K_TASK, t.id))
    db.delete(t)
    db.commit()


def test_rebuild_returns_ok_false_when_redis_down(db):
    """★ Redis 不可用时重建失败但有明确原因（不抛异常）。"""
    os.environ["NF_REDIS_DISABLE"] = "1"
    r = q.rebuild_from_db(db, dry_run=False)
    assert r["ok"] is False
    assert "不可用" in r["reason"]


def test_rebuild_never_raises(db):
    """★★ 重建任何异常都不冒泡。"""
    class Bad:
        def scalars(self, *a, **k):
            raise RuntimeError("db down")

        def commit(self):
            pass

        def rollback(self):
            pass

    r = q.rebuild_from_db(Bad(), dry_run=False)          # type: ignore[arg-type]
    assert isinstance(r, dict)


def test_rebuild_reason_readable(db):
    r = q.rebuild_from_db(db, dry_run=True)
    assert "reason" in r
    assert isinstance(r["reason"], str)


# ============================================================ HTTP 层

@pytest.fixture()
def app_client():
    import sys
    import importlib
    m = sys.modules.get("apps.api.main") or importlib.import_module("apps.api.main")
    from fastapi.testclient import TestClient
    with TestClient(m.app) as c:
        yield c


def _token(role: str = "admin"):
    a = _mod("auth")
    SL = _mod("db").SessionLocal
    from apps.api.models import User
    import sqlalchemy as sa
    with SL() as db:
        if role == "admin":
            u = db.scalar(sa.select(User).where(User.role == "admin"))
            if u is None:
                u = a.create_user(db, f"qadm_{uuid.uuid4().hex[:6]}",
                                  "q-123456", role="admin")
                db.commit()
        else:
            u = a.create_user(db, f"qusr_{uuid.uuid4().hex[:6]}",
                              "q-123456", role="user")
            db.commit()
        return a.make_token(u.id)


def test_http_queue_health_public(app_client):
    """★ 健康检查**无需登录**（前端据此决定交互模式）。"""
    r = app_client.get("/api/queue/health")
    assert r.status_code == 200
    d = r.json()
    assert "available" in d and "mode" in d


def test_http_queue_health_no_sensitive(app_client):
    """★ 公开接口不泄漏配置详情。"""
    r = app_client.get("/api/queue/health")
    d = r.json()
    assert "url" not in d
    assert "version" not in d


def test_http_admin_status(app_client):
    tok = _token()
    r = app_client.get("/api/admin/queue/status",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "available" in r.json()
    assert "stats" in r.json()


def test_http_admin_status_masks_password(app_client):
    """★ 管理端状态也不能泄漏 Redis 密码。"""
    os.environ["NF_REDIS_URL"] = "redis://u:topsecret@h:6379/0"
    q.reset()
    try:
        tok = _token()
        r = app_client.get("/api/admin/queue/status",
                           headers={"Authorization": f"Bearer {tok}"})
        assert "topsecret" not in r.text
    finally:
        os.environ.pop("NF_REDIS_URL", None)
        q.reset()


def test_http_admin_peek(app_client):
    tok = _token()
    r = app_client.get("/api/admin/queue/peek?limit=5",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200 and "items" in r.json()


def test_http_admin_dlq(app_client):
    tok = _token()
    r = app_client.get("/api/admin/queue/dlq",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200 and "items" in r.json()


def test_http_rebuild_defaults_dry_run(app_client):
    """★★ 重建默认 dry-run（必须先看清会改什么）。"""
    tok = _token()
    r = app_client.post("/api/admin/queue/rebuild", json={},
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["dry_run"] is True, "默认必须 dry-run"


def test_http_rebuild_explicit_apply(app_client):
    tok = _token()
    r = app_client.post("/api/admin/queue/rebuild",
                        json={"dry_run": False, "limit": 10},
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "operator" in r.json()


def test_http_reap(app_client):
    tok = _token()
    r = app_client.post("/api/admin/queue/reap",
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200


def test_http_reset(app_client):
    tok = _token()
    r = app_client.post("/api/admin/queue/reset",
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_http_admin_requires_auth(app_client):
    """★ 队列管理接口必须鉴权。"""
    for path in ("/api/admin/queue/status", "/api/admin/queue/peek",
                 "/api/admin/queue/dlq"):
        r = app_client.get(path)
        assert r.status_code in (401, 403), f"{path} 未拦截匿名"


def test_http_admin_requires_admin(app_client):
    """★ 普通用户不能看队列管理。"""
    tok = _token("user")
    for path in ("/api/admin/queue/status", "/api/admin/queue/peek"):
        r = app_client.get(path, headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code in (401, 403), f"{path} 未拦截普通用户"


def test_http_rebuild_requires_admin(app_client):
    tok = _token("user")
    r = app_client.post("/api/admin/queue/rebuild", json={},
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code in (401, 403)


# ============================================================ 依赖声明

def test_redis_in_requirements():
    """★ v0.19.3 教训：依赖声明与依赖安装是两件事。"""
    import io
    p = os.path.join(os.path.dirname(__file__), "..", "requirements.txt")
    s = io.open(p, encoding="utf-8").read()
    assert "redis" in s.lower(), "requirements.txt 缺少 redis 依赖"


def test_setup_script_exists():
    """★ 部署脚本必须随仓库交付。"""
    p = os.path.join(os.path.dirname(__file__), "..",
                     "deploy", "setup_redis.sh")
    assert os.path.exists(p), "缺少 deploy/setup_redis.sh"


def test_setup_script_has_safety_configs():
    """★ 脚本必须包含四项安全配置（否则默认部署不安全）。"""
    import io
    p = os.path.join(os.path.dirname(__file__), "..",
                     "deploy", "setup_redis.sh")
    s = io.open(p, encoding="utf-8").read()
    assert "bind 127.0.0.1" in s, "缺少 bind 限制"
    assert "appendonly yes" in s, "缺少 AOF"
    assert "maxmemory-policy noeviction" in s, "缺少 noeviction"
    assert "Restart=always" in s, "缺少 systemd 自动重启"


def test_setup_script_idempotent_marker():
    """★ 脚本要能重复执行（清理旧配置块）。"""
    import io
    p = os.path.join(os.path.dirname(__file__), "..",
                     "deploy", "setup_redis.sh")
    s = io.open(p, encoding="utf-8").read()
    assert "溯字 SOZEER 自定义" in s
    assert "re.sub" in s or "sed" in s

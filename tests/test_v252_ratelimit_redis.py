"""v0.25.2 测试：限流器 Redis 后端。

覆盖：
  · 后端选择（环境变量 / auto 探测 / 降级）
  · RedisWindow 与 SlidingWindow 接口一致（可替换）
  · Lua 脚本原子性（同毫秒多次请求不被去重）
  · 降级路径（Redis 不可用时计数仍准确）
  · 统计与自检
"""
from __future__ import annotations

import os
import time
import uuid

import pytest

from apps.api import ratelimit as rl


def _mod(name: str):
    import sys
    import importlib
    return sys.modules.get(f"apps.api.{name}") or importlib.import_module(f"apps.api.{name}")


@pytest.fixture(autouse=True)
def _clean_env():
    keys = ["NF_REDIS_DISABLE", "NF_REDIS_URL", rl.ENV_BACKEND, "WEB_CONCURRENCY"]
    saved = {k: os.environ.pop(k, None) for k in keys}
    try:
        from apps.api import queue as q
        q.reset()
    except Exception:                            # noqa: BLE001
        pass
    yield
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v
    rl.set_backend("auto")


def _redis_ok() -> bool:
    os.environ.pop("NF_REDIS_DISABLE", None)
    try:
        from apps.api import queue as q
        q.reset()
        return q.available()
    except Exception:                            # noqa: BLE001
        return False


has_redis = _redis_ok()


# ============================================================ 后端选择

def test_default_is_auto():
    os.environ.pop(rl.ENV_BACKEND, None)
    assert rl._pick_backend() in ("redis", "memory")


def test_force_memory_backend():
    os.environ[rl.ENV_BACKEND] = "memory"
    assert rl._pick_backend() == "memory"


def test_force_redis_backend():
    os.environ[rl.ENV_BACKEND] = "redis"
    assert rl._pick_backend() == "redis"


def test_auto_falls_back_when_redis_disabled():
    """★ 显式禁用 Redis 时 auto 必须回落到内存。"""
    os.environ["NF_REDIS_DISABLE"] = "1"
    os.environ[rl.ENV_BACKEND] = "auto"
    assert rl._pick_backend() == "memory"


def test_build_store_memory():
    os.environ[rl.ENV_BACKEND] = "memory"
    st = rl._build_store()
    assert isinstance(st, rl.SlidingWindow)


def test_build_store_redis():
    os.environ[rl.ENV_BACKEND] = "redis"
    st = rl._build_store()
    assert isinstance(st, rl.RedisWindow)


def test_set_backend_invalid():
    r = rl.set_backend("乱写")
    assert r["ok"] is False


def test_set_backend_memory():
    r = rl.set_backend("memory")
    assert r["ok"] is True and r["actual"] == "memory"
    assert isinstance(rl._store, rl.SlidingWindow)


def test_set_backend_switches_login_fails_too():
    """★ 两个 store 必须同时切换（否则风控计数与限流不一致）。"""
    rl.set_backend("memory")
    assert isinstance(rl._login_fails, rl.SlidingWindow)
    rl.set_backend("redis")
    assert isinstance(rl._login_fails, rl.RedisWindow)


def test_backend_reports_string():
    rl.set_backend("memory")
    assert rl.backend() == "memory"


# ============================================================ 接口一致性

def test_redis_window_has_same_interface():
    """★★ 接口冻结：RedisWindow 必须与 SlidingWindow 同签名。

    这是"接口冻结、实现替换"的守卫 —— 调用方（check_request 等）
    一行都不用改。
    """
    for name in ("hit", "count", "reset", "size"):
        assert hasattr(rl.RedisWindow, name), name
        assert hasattr(rl.SlidingWindow, name), name


def test_hit_returns_three_tuple_memory():
    w = rl.SlidingWindow()
    ok, left, retry = w.hit("k", 60, 3)
    assert ok is True and left == 2 and retry == 0.0


def test_hit_returns_three_tuple_redis():
    """★ RedisWindow.hit 必须返回同样的三元组（即使 Redis 不可用走降级）。"""
    w = rl.RedisWindow(f"t{uuid.uuid4().hex[:6]}")
    ok, left, retry = w.hit("k", 60, 3)
    assert isinstance(ok, bool) and isinstance(left, int) and isinstance(retry, float)


def test_count_signature_memory():
    w = rl.SlidingWindow()
    w.hit("k", 60, 5)
    assert w.count("k", 60) == 1


def test_count_signature_redis():
    w = rl.RedisWindow(f"t{uuid.uuid4().hex[:6]}")
    n = w.count("k", 60)
    assert isinstance(n, int) and n >= 0


def test_reset_memory():
    w = rl.SlidingWindow()
    w.hit("k", 60, 5)
    w.reset("k")
    assert w.count("k", 60) == 0


def test_size_returns_int():
    assert isinstance(rl.SlidingWindow().size(), int)
    assert isinstance(rl.RedisWindow(f"t{uuid.uuid4().hex[:6]}").size(), int)


# ============================================================ 滑动窗口语义

def test_memory_sliding_window_blocks():
    w = rl.SlidingWindow()
    for _ in range(3):
        ok, _, _ = w.hit("k", 60, 3)
        assert ok is True
    ok, left, retry = w.hit("k", 60, 3)
    assert ok is False and left == 0 and retry > 0


def test_memory_window_expires():
    """窗口过期后应放行。"""
    w = rl.SlidingWindow()
    now = time.time()
    for _ in range(3):
        w.hit("k", 10, 3, now=now)
    ok, _, _ = w.hit("k", 10, 3, now=now)          # 超限
    assert ok is False
    ok2, _, _ = w.hit("k", 10, 3, now=now + 11)    # 过了窗口
    assert ok2 is True


def test_memory_different_keys_independent():
    w = rl.SlidingWindow()
    for _ in range(3):
        w.hit("a", 60, 3)
    ok, _, _ = w.hit("b", 60, 3)
    assert ok is True, "不同 key 不应互相影响"


# ============================================================ Redis 原子性（关键）

@pytest.mark.skipif(not has_redis, reason="需要真实 Redis")
def test_redis_window_blocks_correctly():
    rl.set_backend("redis")
    w = rl._store
    key = f"vf_{uuid.uuid4().hex[:8]}"
    for i in range(5):
        ok, left, _ = w.hit(key, 60, 5)
        assert ok is True, f"第 {i+1} 次应放行"
        assert left == 4 - i, f"剩余应为 {4-i}，实得 {left}"
    ok, left, retry = w.hit(key, 60, 5)
    assert ok is False, "第 6 次应被拦"
    assert retry > 0
    w.reset(key)


@pytest.mark.skipif(not has_redis, reason="需要真实 Redis")
def test_redis_same_millisecond_not_deduped():
    """★★ 同毫秒多次请求不能被 ZSET 去重（否则计数偏少 → 限流失效）。

    这是本实现最容易踩的坑：如果 ZSET 的 member 只用时间戳，
    同毫秒的请求会覆盖，count 永远 = 1，限流形同虚设。
    """
    rl.set_backend("redis")
    w = rl._store
    key = f"vf_ms_{uuid.uuid4().hex[:8]}"
    now = time.time()
    for _ in range(10):
        w.hit(key, 60, 100, now=now)              # 同一个 now
    assert w.count(key, 60, now=now) == 10, (
        f"同毫秒 10 次请求应计数为 10，实得 {w.count(key, 60, now=now)}")
    w.reset(key)


@pytest.mark.skipif(not has_redis, reason="需要真实 Redis")
def test_redis_count_decreases_after_window():
    rl.set_backend("redis")
    w = rl._store
    key = f"vf_exp_{uuid.uuid4().hex[:8]}"
    now = time.time()
    for _ in range(3):
        w.hit(key, 10, 10, now=now)
    assert w.count(key, 10, now=now) == 3
    assert w.count(key, 10, now=now + 11) == 0, "过窗口后应清零"
    w.reset(key)


@pytest.mark.skipif(not has_redis, reason="需要真实 Redis")
def test_redis_reset_works():
    rl.set_backend("redis")
    w = rl._store
    key = f"vf_rst_{uuid.uuid4().hex[:8]}"
    w.hit(key, 60, 5)
    w.reset(key)
    assert w.count(key, 60) == 0


@pytest.mark.skipif(not has_redis, reason="需要真实 Redis")
def test_redis_keys_are_namespaced():
    """★ 键必须带前缀（避免与其他应用/环境冲突）。"""
    rl.set_backend("redis")
    assert rl.RedisWindow("main")._prefix.startswith(rl.K_RL)


@pytest.mark.skipif(not has_redis, reason="需要真实 Redis")
def test_redis_expire_set():
    """★ 键必须有过期时间（否则 Redis 会被限流键撑爆）。"""
    from apps.api import queue as q
    rl.set_backend("redis")
    w = rl._store
    key = f"vf_ttl_{uuid.uuid4().hex[:8]}"
    w.hit(key, 60, 5)
    cli = q.client()
    ttl = cli.ttl(w._key(key))
    assert ttl > 0, f"键应有过期时间，实得 TTL={ttl}"
    w.reset(key)


# ============================================================ 降级路径

def test_redis_window_falls_back_when_down():
    """★★ Redis 不可用时计数仍准确（走内存兜底）。"""
    os.environ["NF_REDIS_DISABLE"] = "1"
    try:
        from apps.api import queue as q
        q.reset()
    except Exception:                            # noqa: BLE001
        pass
    w = rl.RedisWindow(f"t{uuid.uuid4().hex[:6]}")
    for i in range(3):
        ok, left, _ = w.hit("k", 60, 3)
        assert ok is True, f"降级后第 {i+1} 次应放行"
    ok, _, retry = w.hit("k", 60, 3)
    assert ok is False and retry > 0, "降级后仍应限流"


def test_redis_window_count_fallback():
    os.environ["NF_REDIS_DISABLE"] = "1"
    try:
        from apps.api import queue as q
        q.reset()
    except Exception:                            # noqa: BLE001
        pass
    w = rl.RedisWindow(f"t{uuid.uuid4().hex[:6]}")
    w.hit("k", 60, 5)
    assert w.count("k", 60) == 1


def test_redis_window_reset_fallback():
    os.environ["NF_REDIS_DISABLE"] = "1"
    w = rl.RedisWindow(f"t{uuid.uuid4().hex[:6]}")
    w.hit("k", 60, 5)
    w.reset("k")
    assert w.count("k", 60) == 0


def test_backend_label_when_down():
    os.environ["NF_REDIS_DISABLE"] = "1"
    try:
        from apps.api import queue as q
        q.reset()
    except Exception:                            # noqa: BLE001
        pass
    assert "memory" in rl.RedisWindow("x").backend()


def test_check_request_still_works_when_down():
    """★ 端到端：限流降级后主流程仍正常。"""
    os.environ[rl.ENV_BACKEND] = "memory"
    rl.set_backend("memory")
    for _ in range(3):
        r = rl.check_request("/api/auth/login", "POST", "203.0.113.77")
    assert r is None, "前几次应放行"


# ============================================================ 内部实现

def test_next_member_unique():
    """★ member 生成必须唯一（同毫秒也不能重复）。"""
    w = rl.RedisWindow("t")
    ms = {w._next_member() for _ in range(200)}
    assert len(ms) == 200, f"member 应全部唯一，实得 {len(ms)} 个不同值"


def test_lua_script_defined():
    assert "ZREMRANGEBYSCORE" in rl._LUA_HIT
    assert "ZADD" in rl._LUA_HIT
    assert "EXPIRE" in rl._LUA_HIT


def test_key_prefix_namespaced():
    w = rl.RedisWindow("myprefix")
    assert w._key("abc") == f"{rl.K_RL}:myprefix:abc"


# ============================================================ 统计与自检

def test_stats_has_backend():
    rl.set_backend("memory")
    st = rl.stats()
    assert "backend" in st
    assert "multi_worker_safe" in st
    assert "note" in st


def test_stats_memory_says_unsafe():
    """★ 内存后端必须明确标注"多 worker 不安全"。"""
    rl.set_backend("memory")
    st = rl.stats()
    assert st["multi_worker_safe"] is False
    assert "worker" in st["note"]


def test_stats_redis_says_safe():
    if not has_redis:
        pytest.skip("需要真实 Redis")
    rl.set_backend("redis")
    st = rl.stats()
    assert st["multi_worker_safe"] is True
    assert "共享" in st["note"]


def test_self_check_returns_stats():
    rl.set_backend("memory")
    st = rl.self_check()
    assert "backend" in st and "rules" in st


def test_self_check_warns_multi_worker(caplog):
    """★★ 多 worker + 内存后端必须告警（这是本任务的核心动机）。"""
    import logging
    os.environ[rl.ENV_BACKEND] = "memory"
    os.environ["WEB_CONCURRENCY"] = "4"
    rl.set_backend("memory")
    with caplog.at_level(logging.WARNING):
        rl.self_check()
    # ★ 用 getMessage() 而不是 message % args —— 后者在 args 数量不匹配时
    #   会抛 TypeError（踩过一次）
    msgs = [r.getMessage() for r in caplog.records]
    assert any("多 worker" in m for m in msgs), f"应输出多 worker 告警，实得：{msgs}"
    assert any("NF_RATELIMIT_BACKEND" in m for m in msgs), "告警应给出修复方式"
    os.environ.pop("WEB_CONCURRENCY", None)


def test_self_check_no_warn_single_worker(caplog):
    """★ 单 worker 下不该有告警（内存后端完全正确）。"""
    import logging
    os.environ[rl.ENV_BACKEND] = "memory"
    os.environ.pop("WEB_CONCURRENCY", None)
    rl.set_backend("memory")
    with caplog.at_level(logging.WARNING):
        rl.self_check()
    assert not any("多 worker" in r.getMessage() for r in caplog.records)


def test_self_check_no_warn_redis(caplog):
    """★ Redis 后端下不该有告警（无论几个 worker）。"""
    if not has_redis:
        pytest.skip("需要真实 Redis")
    import logging
    os.environ["WEB_CONCURRENCY"] = "8"
    rl.set_backend("redis")
    with caplog.at_level(logging.WARNING):
        rl.self_check()
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("多 worker" in m and "内存后端" in m for m in msgs), msgs
    os.environ.pop("WEB_CONCURRENCY", None)


def test_stats_whitelist_sorted():
    st = rl.stats()
    assert st["whitelist"] == sorted(st["whitelist"])


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
                u = a.create_user(db, f"rladm_{uuid.uuid4().hex[:6]}",
                                  "rl-123456", role="admin")
                db.commit()
        else:
            u = a.create_user(db, f"rlusr_{uuid.uuid4().hex[:6]}",
                              "rl-123456", role="user")
            db.commit()
        return a.make_token(u.id)


def test_http_ratelimit_status(app_client):
    tok = _token()
    r = app_client.get("/api/admin/ratelimit/status",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    d = r.json()
    assert "backend" in d and "multi_worker_safe" in d and "hint" in d


def test_http_set_backend(app_client):
    tok = _token()
    r = app_client.post("/api/admin/ratelimit/backend",
                        json={"backend": "memory"},
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200 and r.json()["actual"] == "memory"


def test_http_set_backend_invalid(app_client):
    tok = _token()
    r = app_client.post("/api/admin/ratelimit/backend",
                        json={"backend": "乱写"},
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 400


def test_http_ratelimit_requires_admin(app_client):
    tok = _token("user")
    r = app_client.get("/api/admin/ratelimit/status",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code in (401, 403)


def test_http_ratelimit_anonymous(app_client):
    r = app_client.get("/api/admin/ratelimit/status")
    assert r.status_code in (401, 403)

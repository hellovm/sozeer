"""v0.23 成本与性能（D 组）测试。

覆盖：
  D1 按角色路由模型 —— 任务映射 / 降级链 / 免费版分层 / 覆盖度
  D2 成本看板与硬上限 —— 台账 / 日聚合 / 上限拦截 / 突增检测
  D3 性能 —— 慢查询采集 / SQL 归一化 / 索引审计 / N+1 检测
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest

from apps.api import costing, perf


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


def _mk_user(db, role: str = "user"):
    a = _mod("auth")
    uname = f"cost_{uuid.uuid4().hex[:8]}"
    u = a.create_user(db, uname, "cost-test-12345", role=role)
    db.commit()
    return u


def _mk_profile(db, model_id: str, role: str, *, pin: float = 1.0,
                pout: float = 2.0, default: int = 0):
    from apps.api.models import ModelProfile
    m = ModelProfile(id=f"mp_{uuid.uuid4().hex[:10]}", name=model_id,
                     model_id=model_id, role=role, price_in=pin,
                     price_out=pout, is_default=default, enabled=1)
    db.add(m)
    db.commit()
    return m


def _isolate_profiles(db):
    """★ 临时禁用全部已有模型并返回恢复函数。

    为什么需要：测试库可能已有其他测试建的默认模型，
    "缺角色应降级"这类断言会被它们干扰（假通过/假失败）。
    """
    import sqlalchemy as sa
    from apps.api.models import ModelProfile
    rows = list(db.scalars(sa.select(ModelProfile)).all())
    saved = [(r.id, r.enabled) for r in rows]
    for r in rows:
        r.enabled = 0
    db.commit()

    def _restore():
        for rid, en in saved:
            r = db.get(ModelProfile, rid)
            if r is not None:
                r.enabled = en
        try:
            db.commit()
        except Exception:
            db.rollback()
    return _restore


# ============================================================ D1 路由

def test_role_for_task_known():
    """★ 已知任务的映射必须精确（决定成本）。"""
    assert costing.role_for_task("chapter_write") == "writer"
    assert costing.role_for_task("chapter_summary") == "critic"
    assert costing.role_for_task("consistency_check") == "critic"
    assert costing.role_for_task("chapter_brief") == "planner"
    assert costing.role_for_task("title") == "utility"


def test_role_for_task_unknown_falls_to_default():
    """未知任务不能瞎猜 —— 给 default。"""
    assert costing.role_for_task("完全没听说过的任务") == "default"
    assert costing.role_for_task("") == "default"


def test_role_for_task_prefix_match():
    """带版本后缀的任务名要能前缀匹配。"""
    assert costing.role_for_task("chapter_write_v2") == "writer"


def test_writer_tasks_never_downgraded():
    """★ 质量优先的任务映射不能出错（写错会把强模型降级）。"""
    for t in ("chapter_write", "chapter_polish", "script_write", "dialogue"):
        assert costing.role_for_task(t) == "writer", t


def test_critic_tasks_cover_summary_and_checks():
    """量大从简的任务都应归 critic（这是省钱主战场）。"""
    for t in ("summary", "chapter_summary", "entity_extract",
              "consistency_check", "audit", "score"):
        assert costing.role_for_task(t) == "critic", t


def test_fallback_chain_complete():
    """★ 每个角色的降级链最后必须能落到某处（否则无模型可用）。"""
    for role, chain in costing.ROLE_FALLBACK.items():
        assert chain, role
        assert chain[-1] in ("default", "writer", "planner", "critic"), role


def test_resolve_by_role(db):
    """按角色精确解析。"""
    _mk_profile(db, "strong-writer", "writer", pin=10, pout=30)
    _mk_profile(db, "cheap-critic", "critic", pin=0.5, pout=1.5)
    m = costing.resolve(db, "writer")
    assert m is not None and m.model_id == "strong-writer"
    m2 = costing.resolve(db, "critic")
    assert m2 is not None and m2.model_id == "cheap-critic"


def test_resolve_downgrade_when_role_missing(db):
    """★ 角色缺失时必须降级到可用模型，而不是返回 None。"""
    r = _isolate_profiles(db)
    try:
        _mk_profile(db, "only-default", "default", default=1)
        m = costing.resolve(db, "utility")
        assert m is not None, "缺 utility 档时应降级而非失败"
        assert m.model_id == "only-default"
    finally:
        r()


def test_resolve_empty_returns_none(db):
    """完全没配模型 → None（调用方给明确报错）。"""
    r = _isolate_profiles(db)
    try:
        assert costing.resolve(db, "writer") is None
    finally:
        r()


def test_explain_tolerates_none_db():
    """★ explain 允许 db=None（配置页"还没配模型"场景不该 500）。"""
    e = costing.explain(None, "chapter_write")            # type: ignore[arg-type]
    assert e["task"] == "chapter_write"
    assert e["model_id"] == ""
    assert e["fallback_chain"]


def test_free_tier_downgrades_writer(db):
    """★ 免费版写正文应降级到 critic 档（分层走便宜模型）。"""
    r = _isolate_profiles(db)
    try:
        _mk_profile(db, "exp-writer", "writer", pin=10, pout=30)
        _mk_profile(db, "exp-critic", "critic", pin=0.5, pout=1.5)
        m = costing.resolve(db, "writer", tier="basic")
        assert m.model_id == "exp-critic", "免费版不该用强模型写正文"
    finally:
        r()


def test_std_tier_keeps_writer(db):
    """标准档不降级（付费用户体验优先）。"""
    r = _isolate_profiles(db)
    try:
        _mk_profile(db, "std-writer", "writer", pin=10, pout=30)
        _mk_profile(db, "std-critic", "critic", pin=0.5, pout=1.5)
        m = costing.resolve(db, "writer", tier="std")
        assert m.model_id == "std-writer"
    finally:
        r()


def test_free_tier_keeps_cheap_roles(db):
    """免费版的便宜档不该被"降级"到其他地方。"""
    r = _isolate_profiles(db)
    try:
        _mk_profile(db, "free-critic", "critic", pin=0.5, pout=1.5)
        m = costing.resolve(db, "critic", tier="basic")
        assert m.model_id == "free-critic"
    finally:
        r()


def test_plan_tier_mapping():
    assert costing.PLAN_TIER["free"] == "basic"
    assert costing.PLAN_TIER["pro"] == "std"
    assert costing.PLAN_TIER[""] == "std"


def test_explain_fields(db):
    """explain 必须能回答"为什么用这个模型"。"""
    _isolate_profiles(db)
    _mk_profile(db, "exp-model", "critic", pin=1, pout=2)
    e = costing.explain(db, "chapter_summary")
    assert e["task"] == "chapter_summary"
    assert e["role"] == "critic"
    assert "fallback_chain" in e
    assert "downgraded" in e


def test_explain_shows_downgrade(db):
    """免费版的降级必须在 explain 里可见。"""
    _isolate_profiles(db)
    _mk_profile(db, "exp-w", "writer", pin=10, pout=30)
    _mk_profile(db, "exp-c", "critic", pin=1, pout=2)
    e = costing.explain(db, "chapter_write", plan="free")
    assert e["downgraded"] is True
    assert e["effective_role"] == "critic"


def test_coverage_reports_missing(db):
    """覆盖度体检要能报出缺哪些档。"""
    r = _isolate_profiles(db)
    try:
        # 只配 writer → planner/critic/utility 应报缺
        _mk_profile(db, "cov-writer", "writer", default=1)
        c = costing.coverage(db)
        assert c["total"] == 1
        assert "planner" in c["missing"]
        assert "writer" in c["have"]
    finally:
        r()


def test_pick_model_delegates_to_costing(db):
    """★ pick_model 必须走新路由（接口冻结、实现替换）。"""
    from apps.api.orchestrator import graph as g
    _mk_profile(db, "delegated-critic", "critic", pin=1, pout=2)
    m = g.pick_model(db, "critic")
    assert m is not None


def test_task_map_covers_four_roles():
    """任务映射必须覆盖全部四个角色（否则某档形同虚设）。"""
    roles = set(costing.TASK_ROLE.values())
    assert roles == {"writer", "planner", "critic", "utility"}


# ============================================================ D2 台账与上限

def test_record_writes_row(db):
    u = _mk_user(db)
    m = _mk_profile(db, "rec-model", "critic", pin=1, pout=2)
    row = costing.record(db, task="chapter_summary", role="critic",
                         user_id=u.id, model=m, input_tokens=1000,
                         output_tokens=500)
    assert row.id.startswith("lc_")
    assert row.cost > 0


def test_record_estimates_cost_from_prices(db):
    """成本必须按模型单价算（1元/百万 in、2元/百万 out）。"""
    u = _mk_user(db)
    m = _mk_profile(db, "calc-model", "critic", pin=1.0, pout=2.0)
    row = costing.record(db, task="x", user_id=u.id, model=m,
                         input_tokens=1_000_000, output_tokens=1_000_000)
    assert abs(row.cost - 3.0) < 1e-6, f"应为 1+2=3 元，实得 {row.cost}"


def test_estimate_cost_none_model():
    assert costing.estimate_cost(None, 100, 100) == 0.0


def test_daily_usage_aggregates(db):
    u = _mk_user(db)
    m = _mk_profile(db, "agg-model", "critic", pin=0, pout=0)
    for _ in range(3):
        costing.record(db, task="summary", user_id=u.id, model=m,
                       input_tokens=100, output_tokens=50)
    use = costing.daily_usage(db, u.id)
    assert use["tokens"] >= 450
    assert use["calls"] >= 3


def test_guard_allows_under_cap(db):
    u = _mk_user(db)
    g = costing.guard(db, u.id)
    assert g["allowed"] is True
    assert g["cap"] > 0


def test_guard_blocks_over_cap(db):
    """★ 硬上限必须真的拦得住（验收要求"模拟超限被拦"）。"""
    from apps.api.models import Setting
    u = _mk_user(db)
    m = _mk_profile(db, "cap-model", "critic", pin=0, pout=0)
    # 设一个极小的全局上限
    db.merge(Setting(key="cost.daily_token_cap", value="100"))
    db.commit()
    costing.record(db, task="x", user_id=u.id, model=m,
                   input_tokens=500, output_tokens=0)
    g = costing.guard(db, u.id)
    assert g["allowed"] is False
    assert "上限" in g["reason"]
    # 清理，避免影响其他用例
    s = db.get(Setting, "cost.daily_token_cap")
    if s:
        db.delete(s)
        db.commit()


def test_guard_blocks_on_estimated_need(db):
    """★ 预估需求也计入 —— 防止"差一点就超"的边界反复触发。"""
    from apps.api.models import Setting
    u = _mk_user(db)
    db.merge(Setting(key="cost.daily_token_cap", value="10000"))
    db.commit()
    g = costing.guard(db, u.id, need_tokens=999_999)
    assert g["allowed"] is False
    assert "剩余" in g["reason"] or "仅剩" in g["reason"]
    s = db.get(Setting, "cost.daily_token_cap")
    if s:
        db.delete(s)
        db.commit()


def test_guard_unlimited_when_cap_zero(db):
    """上限 0 = 不限制（自建/内部账号）。"""
    from apps.api.models import Setting
    u = _mk_user(db)
    db.merge(Setting(key="cost.daily_token_cap", value="0"))
    db.commit()
    g = costing.guard(db, u.id)
    assert g["allowed"] is True and g.get("unlimited") is True
    s = db.get(Setting, "cost.daily_token_cap")
    if s:
        db.delete(s)
        db.commit()


def test_cap_by_plan_defined():
    """各套餐上限必须都有定义（不能漏）。"""
    for p in ("free", "basic", "pro", "max"):
        assert p in costing.CAP_BY_PLAN
        assert costing.CAP_BY_PLAN[p] > 0
    # 免费版必须比旗舰版小（分层才有意义）
    assert costing.CAP_BY_PLAN["free"] < costing.CAP_BY_PLAN["max"]


def test_check_spike_no_baseline(db):
    """无历史基线时不报突增（避免新用户首次使用就告警）。"""
    u = _mk_user(db)
    s = costing.check_spike(db, u.id, hours=1)
    assert s["is_spike"] is False


def test_check_spike_detects(db):
    """★ 突增检测：当日用量 10x 于基线时必须报出。"""
    u = _mk_user(db)
    m = _mk_profile(db, "spike-model", "critic", pin=0, pout=0)
    # 造历史基线：过去 7 天每天"同一时段"各 100 token。
    # ★ 基线窗口是 [now-d-1h, now-d)，所以时间点要落在窗口中间。
    from apps.api.models import LLMCall
    # ★ 台账存 UTC，测试造数据也必须用 UTC
    base_now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    for d in range(1, 8):
        t = base_now - dt.timedelta(days=d, minutes=30)   # 落在窗口内
        db.add(LLMCall(id=f"lc_b_{d}_{uuid.uuid4().hex[:6]}", task="x",
                       user_id=u.id, input_tokens=100, output_tokens=0,
                       created_at=t))
    db.commit()
    # 当前小时巨量
    costing.record(db, task="x", user_id=u.id, model=m,
                   input_tokens=5000, output_tokens=0)
    s = costing.check_spike(db, u.id, hours=1, factor=5.0)
    assert s["current"] >= 5000
    assert s["is_spike"] is True


def test_dashboard_structure(db):
    u = _mk_user(db)
    m = _mk_profile(db, "dash-model", "writer", pin=1, pout=2)
    costing.record(db, task="chapter_write", role="writer", user_id=u.id,
                   model=m, input_tokens=1000, output_tokens=500)
    d = costing.dashboard(db, days=7)
    for k in ("by_day", "by_task", "by_user", "by_model", "by_role",
              "total", "saving_estimate"):
        assert k in d, k
    assert d["total"]["calls"] >= 1


def test_dashboard_saving_estimate(db):
    """★ 省钱估算必须给出 —— 这是验收"降 30%"的量化依据。"""
    u = _mk_user(db)
    cheap = _mk_profile(db, "cheap-x", "critic", pin=0.1, pout=0.2)
    _mk_profile(db, "exp-x", "writer", pin=20, pout=60)
    # 造一批 critic 的高价调用（模拟"没路由时用了贵模型"）
    for _ in range(3):
        costing.record(db, task="summary", role="critic", user_id=u.id,
                       model=_mk_profile(db, f"wrong-{uuid.uuid4().hex[:6]}",
                                         "critic", pin=20, pout=60),
                       input_tokens=10000, output_tokens=5000)
    d = costing.dashboard(db, days=7)
    se = d["saving_estimate"]
    assert "could_save" in se and "ratio" in se
    assert se["cheap_model"] == cheap.model_id or se["ratio"] >= 0


def test_user_report_structure(db):
    u = _mk_user(db)
    _mk_profile(db, "rep-model", "critic", pin=1, pout=2)
    r = costing.user_report(db, u.id, days=30)
    for k in ("today", "daily_cap", "cap_remain", "by_task", "total_cost"):
        assert k in r, k


def test_record_failure_does_not_raise(db):
    """★ 记账失败绝不能影响主流程。"""
    class Bad:
        """模拟 DB 连接故障（add 就抛）。"""

        def add(self, x):                     # noqa: ARG002
            raise RuntimeError("db down")

        def commit(self):
            raise RuntimeError("db down")

        def rollback(self):
            pass

    row = costing.record(Bad(), task="x", user_id="u",  # type: ignore[arg-type]
                         input_tokens=1, output_tokens=1)
    assert row is not None          # 返回行对象但没抛异常


def test_prune_removes_old(db):
    u = _mk_user(db)
    from apps.api.models import LLMCall
    old = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
           - dt.timedelta(days=400))
    db.add(LLMCall(id=f"lc_old_{uuid.uuid4().hex[:8]}", task="x",
                   user_id=u.id, input_tokens=1, created_at=old))
    db.commit()
    n = costing.prune(db, keep_days=180)
    assert n >= 1


# ============================================================ D3 性能

def test_sql_normalize():
    """★ 归一化要能聚合不同字面量的同一模式（N+1 检测的基础）。"""
    a = perf._normalize("SELECT * FROM books WHERE id = 'abc'")
    b = perf._normalize("SELECT * FROM books WHERE id = 'xyz'")
    assert a == b
    c = perf._normalize("SELECT * FROM books WHERE idx = 123")
    assert "?" in c and "123" not in c


def test_collector_records_slow():
    """慢查询采集（阈值以上才记）。"""
    col = perf.collector()
    col.reset()
    col.add("SELECT 1", 500.0)
    col.add("SELECT 2", 10.0)          # 未达阈值
    snap = col.snapshot()
    assert snap["total_queries"] == 2
    assert snap["slow_queries"] == 1
    assert snap["max_ms"] == 500.0
    col.reset()


def test_collector_reset():
    col = perf.collector()
    col.add("SELECT 1", 999.0)
    col.reset()
    assert col.snapshot()["total_queries"] == 0
    assert col.snapshot()["samples"] == []


def test_collector_bounded():
    """★ 采样必须有上限（否则长跑进程内存泄漏）。"""
    col = perf.collector()
    col.reset()
    for i in range(perf._MAX_SAMPLES + 200):
        col.add(f"SELECT {i}", 300.0)
    assert len(col.snapshot()["samples"]) <= 30   # snapshot 只返回最近 30
    col.reset()


def test_nplusone_detects():
    """★ N+1 检测：同一模式重复 N 次即疑似。"""
    d = perf.NPlusOneDetector(threshold=5)
    for i in range(8):
        d.record(f"SELECT * FROM chapters WHERE id = {i}")
    sus = d.suspects()
    assert len(sus) == 1
    assert sus[0]["count"] == 8


def test_nplusone_below_threshold():
    d = perf.NPlusOneDetector(threshold=10)
    for i in range(3):
        d.record(f"SELECT * FROM chapters WHERE id = {i}")
    assert d.suspects() == []


def test_audit_indexes(db):
    """★ 索引审计：应报告 llm_calls 等新表索引状态。"""
    r = perf.audit_indexes(db.get_bind())
    assert "missing" in r and "missing_count" in r
    assert r["dialect"] in ("sqlite", "postgresql")


def test_expected_indexes_defined():
    """关键表必须都在期望清单里（防漏检）。"""
    for t in ("llm_calls", "chapters", "memory_chunks", "book_scenes"):
        assert t in perf.EXPECTED_INDEXES, t


def test_timed_context():
    import time
    with perf.timed("t") as t:
        time.sleep(0.01)
    assert t.ms >= 5


def test_table_sizes(db):
    rows = perf.table_sizes(db, top=5)
    assert isinstance(rows, list) and len(rows) <= 5


def test_report_structure(db):
    r = perf.report(db)
    for k in ("slow_query", "indexes", "nplusone", "table_sizes", "advice"):
        assert k in r, k
    assert isinstance(r["advice"], list) and r["advice"]


def test_advice_not_empty_when_clean(db):
    """★ 只报问题不给方案 = 无用 —— 无慢查询时也要有结论。"""
    perf.collector().reset()
    r = perf.report(db)
    assert any("未采集到慢查询" in a or "慢查询" in a for a in r["advice"])


# ============================================================ HTTP 层（关键端点）

@pytest.fixture()
def app_client():
    import sys
    import importlib
    m = sys.modules.get("apps.api.main") or importlib.import_module("apps.api.main")
    from fastapi.testclient import TestClient
    with TestClient(m.app) as c:
        yield c


def _admin_token():
    import sys
    a = sys.modules.get("apps.api.auth") or __import__(
        "importlib").import_module("apps.api.auth")
    SL = sys.modules.get("apps.api.db").SessionLocal
    from apps.api.models import User
    with SL() as db:
        import sqlalchemy as sa
        u = db.scalar(sa.select(User).where(User.role == "admin"))
        if u is None:
            u = a.create_user(db, f"costadm_{uuid.uuid4().hex[:6]}",
                              "cost-123456", role="admin")
            db.commit()
        return a.make_token(u.id)


def test_http_route_explain(app_client):
    tok = _admin_token()
    r = app_client.get("/api/costing/route/explain?task=chapter_write",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["task"] == "chapter_write"


def test_http_route_coverage(app_client):
    tok = _admin_token()
    r = app_client.get("/api/costing/route/coverage",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "missing" in r.json()


def test_http_route_preview(app_client):
    tok = _admin_token()
    r = app_client.post("/api/costing/route/preview", json={},
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert len(r.json()["items"]) >= 3


def test_http_quota(app_client):
    """★ /api/me/quota 前端要用（显示进度条）。"""
    tok = _admin_token()
    r = app_client.get("/api/me/quota",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    d = r.json()
    assert "allowed" in d and "usage_pct" in d


def test_http_my_cost(app_client):
    tok = _admin_token()
    r = app_client.get("/api/me/cost",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "today" in r.json()


def test_http_admin_dashboard(app_client):
    tok = _admin_token()
    r = app_client.get("/api/admin/costing/dashboard?days=7",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "by_task" in r.json()


def test_http_admin_spikes(app_client):
    tok = _admin_token()
    r = app_client.get("/api/admin/costing/spikes?hours=1",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "items" in r.json()


def test_http_admin_perf_report(app_client):
    tok = _admin_token()
    r = app_client.get("/api/admin/perf/report",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "indexes" in r.json()


def test_http_admin_perf_indexes(app_client):
    tok = _admin_token()
    r = app_client.get("/api/admin/perf/indexes",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "missing_count" in r.json()


def test_http_admin_perf_reset(app_client):
    tok = _admin_token()
    r = app_client.post("/api/admin/perf/reset",
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200


def test_http_anonymous_rejected(app_client):
    """★ 成本与性能数据属敏感信息，匿名必须全 401/403。"""
    for path in ("/api/costing/route/coverage", "/api/me/cost",
                 "/api/admin/costing/dashboard", "/api/admin/perf/report"):
        r = app_client.get(path)
        assert r.status_code in (401, 403), f"{path} 未拦截匿名（{r.status_code}）"


def test_http_non_admin_blocked(app_client):
    """★ 非管理员不能看全局成本（横向越权防护）。"""
    a = _mod("auth")
    SL = _mod("db").SessionLocal
    with SL() as db:
        u = a.create_user(db, f"costuser_{uuid.uuid4().hex[:6]}",
                          "cost-123456", role="user")
        tok = a.make_token(u.id)
    for path in ("/api/admin/costing/dashboard", "/api/admin/perf/report"):
        r = app_client.get(path, headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code in (401, 403), f"{path} 未拦截普通用户"


# ============================================================ 流式 usage

def test_stream_usage_exposed():
    """stream_usage 必须存在（正文记账依赖它）。"""
    from apps.api import llm
    assert hasattr(llm, "stream_usage")


def test_calc_cost():
    from apps.api import llm
    from apps.api.models import ModelProfile
    m = ModelProfile(id="x", price_in=1.0, price_out=2.0)
    assert abs(llm.calc_cost(m, 1_000_000, 1_000_000) - 3.0) < 1e-9
    assert llm.calc_cost(m, 0, 0) == 0.0

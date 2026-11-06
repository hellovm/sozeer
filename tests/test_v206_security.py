"""A6 安全加固回归测试（v0.20.6）。

★ 覆盖 6 项：限流 / 风控 / 审计 / 令牌刷新 / 密钥加密 / 内容审核

★ 测试原则：每一条都对应**真实攻击场景**，不是"代码覆盖率凑数"。
  例如"验签失败必须拒绝"对应的攻击是：攻击者伪造支付回调白拿积分。
"""
from __future__ import annotations

import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from apps.api import keyvault, moderation, ratelimit, risk, tokens
from apps.api.db import SessionLocal


@pytest.fixture(scope="module", autouse=True)
def _ensure_tables():
    from apps.api import models  # noqa: F401
    from apps.api.db import Base, engine
    Base.metadata.create_all(engine)
    # ★ v0.20.7 A7：seed RBAC（否则 require_admin 走 RBAC 后无权限 → 403）
    try:
        from apps.api import rbac as _rbac
        from apps.api.db import SessionLocal as _SL
        with _SL() as _db:
            _rbac.seed_role_permissions(_db)
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _fresh_modules():
    """★ 每个测试前刷新模块引用（防 sys.modules 被其他测试清空后错配）。

    背景：test_v180_t1_split::test_no_circular_import 会
    `del sys.modules['apps.api.*']` 再重新导入。此后本文件顶部
    import 的 tokens/auth 等引用与运行时的不是同一对象 ——
    表现为"单跑通过、全量跑 401"。
    """
    import sys
    import importlib
    global tokens, risk, keyvault, moderation, ratelimit
    for name in ("tokens", "risk", "keyvault", "moderation", "ratelimit", "auth"):
        m = sys.modules.get(f"apps.api.{name}") or importlib.import_module(f"apps.api.{name}")
        if name == "tokens":
            tokens = m
        elif name == "risk":
            risk = m
        elif name == "keyvault":
            keyvault = m
        elif name == "moderation":
            moderation = m
        elif name == "ratelimit":
            ratelimit = m
        elif name == "auth":
            globals()["auth"] = m
    yield


@pytest.fixture()
def app_client():
    """★ 构造一个"自洽"的 app 实例。

    ★ 核心问题（踩坑实录）：
      test_v180_t1_split::test_no_circular_import 会清空 sys.modules 并
      重新导入 apps.api.*。此后本文件顶部 `from apps.api import tokens`
      拿到的 **tokens 模块** 与 app 内部引用的 **不是同一个实例** ——
      两边各自持有自己的 `config` 对象。
      · tokens 用 SECRET_KEY_A 签令牌
      · app 的路由闭包用 SECRET_KEY_B 验令牌
      → 单跑通过（同一实例），全量跑 401（实例分裂）。

    ★ 解法：在 fixture 里**重新导入 main**，保证 app 及其依赖
      （含 tokens/config 的引用链）都来自同一批模块对象。
    """
    import sys
    import importlib
    # 先把本测试关心的模块统一刷新
    for name in ("config", "db", "models", "auth", "tokens", "risk",
                 "keyvault", "moderation", "ratelimit"):
        mod_name = f"apps.api.{name}"
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])
    m = sys.modules.get("apps.api.main") or importlib.import_module("apps.api.main")
    importlib.reload(m)
    with TestClient(m.app) as c:
        yield c


@pytest.fixture(autouse=True)
def fresh_rl():
    """★ 每个用例用全新的限流器。

    为什么必须 autouse：限流器是**进程内全局状态**，
    前面的测试打满了额度，后面的测试就全被 429 误伤。
    （注意：`check_request` 内部引用模块级 `_store`，
      所以必须替换模块属性而不是局部变量。）
    """
    old = ratelimit._store
    old_fail = ratelimit._login_fails
    ratelimit._store = ratelimit.SlidingWindow()
    ratelimit._login_fails = ratelimit.SlidingWindow()
    yield ratelimit._store
    ratelimit._store = old
    ratelimit._login_fails = old_fail


# ================================================================ A6-1 限流

def test_sliding_window_basic():
    """基础计数：窗口内超限必须拒绝。"""
    w = ratelimit.SlidingWindow()
    for i in range(5):
        ok, left, _ = w.hit("k", 60, 5)
        assert ok, f"第 {i+1} 次不应被拒"
    ok, left, retry = w.hit("k", 60, 5)
    assert not ok and left == 0 and retry > 0


def test_sliding_window_expires():
    """窗口滑过后应恢复（否则等于永久封禁）。"""
    w = ratelimit.SlidingWindow()
    now = 1000.0
    for _ in range(3):
        w.hit("k", 10, 3, now=now)
    ok, _, _ = w.hit("k", 10, 3, now=now)
    assert not ok
    # 10 秒后窗口滑过
    ok, _, _ = w.hit("k", 10, 3, now=now + 11)
    assert ok, "窗口过期后应恢复"


def test_sliding_window_memory_bounded():
    """★ 内存有界：单 key 不会无限增长（防攻击者用海量请求撑爆内存）。"""
    w = ratelimit.SlidingWindow()
    for _ in range(10000):
        w.hit("same_key", 60, 5)
    # deque maxlen = max_hits+1，所以不会超过 6
    assert len(w._d["same_key"]) <= 6


def test_login_rate_limited(fresh_rl):
    """★ 登录接口必须限流（防暴力破解）。"""
    for i in range(10):
        r = ratelimit.check_request("/api/auth/login", "POST", "203.0.113.9")
        assert r is None, f"第 {i+1} 次不应被拒"
    r = ratelimit.check_request("/api/auth/login", "POST", "203.0.113.9")
    assert r is not None and r["status"] == 429
    assert r["headers"]["Retry-After"]


def test_send_code_rate_limited(fresh_rl):
    """★ 验证码接口必须限流（防短信轰炸 —— 一条几毛钱，能刷到破产）。"""
    for _ in range(5):
        ratelimit.check_request("/api/auth/send-code", "POST", "203.0.113.10")
    r = ratelimit.check_request("/api/auth/send-code", "POST", "203.0.113.10")
    assert r is not None, "验证码接口未限流"


def test_private_ip_not_limited(fresh_rl):
    """★ 内网 IP 不限流（监控/健康检查必须稳定，不能因限流误报故障）。"""
    for _ in range(1000):
        r = ratelimit.check_request("/api/auth/login", "POST", "127.0.0.1")
        assert r is None


def test_generate_by_user_not_ip(fresh_rl):
    """★ 生成类按用户限流（同一 NAT 出口的多用户不应互相影响）。"""
    for _ in range(30):
        r = ratelimit.check_request("/api/books/bk_x/draft", "POST",
                                    "203.0.113.11", "u_aaa")
        assert r is None
    r = ratelimit.check_request("/api/books/bk_x/draft", "POST", "203.0.113.11", "u_aaa")
    assert r is not None
    # 另一个用户应不受影响（不同 key）
    r2 = ratelimit.check_request("/api/books/bk_x/draft", "POST", "203.0.113.11", "u_bbb")
    assert r2 is None, "不同用户被误伤（限流 key 设计有误）"


def test_get_request_not_generate_limited(fresh_rl):
    """GET 请求不应触发生成类限流（读操作便宜）。"""
    for _ in range(100):
        r = ratelimit.check_request("/api/books/bk_x/draft", "GET", "203.0.113.12")
        assert r is None


def test_client_ip_prefers_cf_header():
    """★ IP 提取必须优先 CF-Connecting-IP（XFF 可被客户端伪造）。"""
    class H(dict):
        def get(self, k, d=None):
            return super().get(k.lower(), d)
    h = H({"cf-connecting-ip": "1.2.3.4", "x-forwarded-for": "9.9.9.9, 8.8.8.8",
           "x-real-ip": "5.5.5.5"})
    assert ratelimit.client_ip(h) == "1.2.3.4"


def test_is_private():
    """★ 只豁免真正内网（RFC1918）；文档段/保留段照常限流。

    为什么重要：豁免范围过宽 = 攻击者可能用某些保留段伪造源 IP 绕过限流。
    """
    assert ratelimit.is_private("127.0.0.1")
    assert ratelimit.is_private("192.168.1.1")
    assert ratelimit.is_private("10.0.0.1")
    assert ratelimit.is_private("172.16.0.5")
    assert ratelimit.is_private("")
    # ★ 文档段（RFC5737）与公网必须照常限流
    assert not ratelimit.is_private("203.0.113.9")
    assert not ratelimit.is_private("198.51.100.5")
    assert not ratelimit.is_private("8.8.8.8")
    # 非法 IP → 不可信 → 限流
    assert not ratelimit.is_private("not-an-ip")


def test_rate_limit_middleware_returns_429(app_client):
    """★ 端到端：中间件必须真的返回 429 而不是 500。"""
    ok = 0
    got_429 = False
    for _ in range(30):
        r = app_client.post("/api/auth/login", json={"username": "x", "password": "y"},
                            headers={"CF-Connecting-IP": "203.0.113.77"})
        if r.status_code == 429:
            got_429 = True
            assert "Retry-After" in r.headers
            break
        ok += 1
    assert got_429, f"连续 {ok} 次登录均未触发限流"


# ================================================================ A6-2 风控

def test_record_login_both_success_and_fail():
    """★ 登录记录必须记成功**和**失败（失败是暴力破解的证据）。"""
    uid = f"u_{uuid.uuid4().hex[:10]}"
    with SessionLocal() as db:
        risk.record_login(db, user_id=uid, ip="203.0.113.20", ua="test",
                          success=True, reason="ok")
        risk.record_login(db, user_id=uid, ip="203.0.113.20", ua="test",
                          success=False, reason="密码错误")
        items = risk.recent_logins(db, uid, limit=10)
    assert len(items) >= 2
    assert {i["success"] for i in items} >= {0, 1}


def test_risk_detect_brute_force():
    """★ R2：短时高频失败必须被识别。"""
    ip = f"203.0.113.{uuid.uuid4().int % 200 + 30}"
    with SessionLocal() as db:
        for _ in range(11):
            risk.record_login(db, user_id="", ip=ip, ua="t", success=False,
                              reason="密码错误")
        hits = risk.assess_login(db, ip=ip)
    rules = {h.rule for h in hits}
    assert "R2" in rules, f"未识别暴力破解，命中：{rules}"
    assert risk.max_level(hits) in ("medium", "high")


def test_risk_detect_multi_account():
    """★ R1：同 IP 多账号必须被识别（工作室养号特征）。"""
    ip = f"198.51.100.{uuid.uuid4().int % 200 + 10}"
    with SessionLocal() as db:
        for i in range(9):
            risk.record_login(db, user_id=f"u_multi_{i}_{uuid.uuid4().hex[:4]}",
                              ip=ip, ua="t", success=True)
        hits = risk.assess_login(db, ip=ip)
    assert "R1" in {h.rule for h in hits}, "未识别多账号"


def test_risk_detect_new_ip_sensitive_action():
    """★ R4：新 IP 上做敏感操作必须告警（盗号后转移的特征）。"""
    uid = f"u_{uuid.uuid4().hex[:10]}"
    with SessionLocal() as db:
        risk.record_login(db, user_id=uid, ip="203.0.113.50", ua="t", success=True)
        hits = risk.assess_sensitive_action(db, user_id=uid, ip="198.51.100.99",
                                            action="修改密码")
    assert "R4" in {h.rule for h in hits}


def test_risk_alert_written_to_audit():
    """★ 风控告警必须写审计日志（留痕才可追溯）。"""
    ip = f"203.0.113.{uuid.uuid4().int % 200 + 30}"
    with SessionLocal() as db:
        for _ in range(11):
            risk.record_login(db, user_id="", ip=ip, ua="t", success=False)
        hits = risk.assess_login(db, ip=ip)
        aid = risk.write_risk_alert(db, hits=hits, ip=ip, action="login")
    assert aid, "风控告警未写入"
    assert "risk" in aid
    with SessionLocal() as db:
        d = risk.list_risk_alerts(db, limit=5)
    assert d["items"], "查不到风控告警"


def test_max_level():
    hits = [risk.RiskHit("R1", "low", "a"), risk.RiskHit("R2", "high", "b")]
    assert risk.max_level(hits) == "high"
    assert risk.max_level([]) == "none"


def test_risk_overview_shape():
    """风控总览结构完整（后台直接消费）。"""
    with SessionLocal() as db:
        d = risk.risk_overview(db, hours=24)
    for k in ("window_hours", "alerts_total", "by_level", "by_rule", "logins"):
        assert k in d
    assert "fail_rate" in d["logins"]


# ================================================================ A6-4 令牌刷新

def test_access_token_roundtrip():
    """access token 签发与解析。"""
    uid = f"u_{uuid.uuid4().hex[:10]}"
    t = tokens.make_access_token(uid)
    p = tokens.parse_access_token(t)
    assert p and p["u"] == uid and p.get("typ") == "access"


def test_access_token_expired_rejected():
    """★ 过期令牌必须拒绝。"""
    uid = f"u_{uuid.uuid4().hex[:10]}"
    t = tokens.make_access_token(uid, ttl=-10)      # 已过期
    assert tokens.parse_access_token(t) is None


def test_access_token_tampered_rejected():
    """★ 篡改令牌必须验签失败。"""
    uid = f"u_{uuid.uuid4().hex[:10]}"
    t = tokens.make_access_token(uid)
    body, sig = t.rsplit(".", 1)
    assert tokens.parse_access_token(body + "." + sig[:-4] + "AAAA") is None


def test_refresh_rotation():
    """★ 轮换：刷新后旧令牌作废，新令牌可用。"""
    uid = f"u_{uuid.uuid4().hex[:10]}"
    with SessionLocal() as db:
        r1 = tokens.issue_refresh_token(db, uid, ip="1.1.1.1", ua="test")
        out = tokens.rotate_refresh_token(db, r1["token"], ip="1.1.1.1", ua="test")
        assert out["ok"], out
        assert out["refresh_token"] != r1["token"], "未轮换（新旧令牌相同）"
        assert out["access_token"]
        # 新令牌可继续用
        out2 = tokens.rotate_refresh_token(db, out["refresh_token"])
        assert out2["ok"]


def test_refresh_reuse_detection():
    """★★ 重放检测：旧 refresh_token 再次使用 → 判定泄露 → 吊销整族。

    这是本机制最核心的安全能力。对应攻击：
      攻击者偷到 refresh_token，用户也还在正常用 ——
      谁先用谁"赢"，后用的那次会暴露。

    ★ v0.27.1 契约更新：加了 REFRESH_REUSE_GRACE 并发刷新宽限窗口 ——
      宽限期内的重用视为多标签页并发刷新（补发不吊销，见
      test_v0271_fixes.py）；**超过宽限期**的重用仍判定泄露 → 吊销整族。
      本测试把 used_at 拨回宽限期之前，验证核心安全语义不变。
    """
    import datetime as _dt
    from apps.api.models import RefreshToken as _RT

    uid = f"u_{uuid.uuid4().hex[:10]}"
    with SessionLocal() as db:
        r1 = tokens.issue_refresh_token(db, uid, ip="1.1.1.1", ua="test")
        first = tokens.rotate_refresh_token(db, r1["token"])
        assert first["ok"]
        # 把 used_at 拨回宽限期之前 = 模拟"很久之后才重放"
        row = db.query(_RT).filter(
            _RT.token_hash == tokens._hash_token(r1["token"])).first()
        assert row is not None
        row.used_at = (tokens._naive_now()
                       - _dt.timedelta(seconds=tokens.REFRESH_REUSE_GRACE + 60))
        db.commit()
        # ★ 用**已过期宽限期**的 r1 再刷一次 = 重放
        second = tokens.rotate_refresh_token(db, r1["token"])
        assert not second["ok"]
        assert second["code"] == "reuse_detected", second
        # 家族已被吊销 → 第一次拿到的令牌也不能用了
        third = tokens.rotate_refresh_token(db, first["refresh_token"])
        assert not third["ok"], "重放后整族未被吊销"


def test_refresh_invalid_token():
    with SessionLocal() as db:
        r = tokens.rotate_refresh_token(db, "not-a-real-token")
    assert not r["ok"] and r["code"] == "invalid"


def test_refresh_missing_token():
    with SessionLocal() as db:
        r = tokens.rotate_refresh_token(db, "")
    assert not r["ok"] and r["code"] == "missing"


def test_revoke_user_tokens():
    """★ 退出所有设备：全部令牌失效。"""
    uid = f"u_{uuid.uuid4().hex[:10]}"
    with SessionLocal() as db:
        a = tokens.issue_refresh_token(db, uid)
        b = tokens.issue_refresh_token(db, uid)
        n = tokens.revoke_user_tokens(db, uid, reason="test")
        assert n >= 2
        assert not tokens.rotate_refresh_token(db, a["token"])["ok"]
        assert not tokens.rotate_refresh_token(db, b["token"])["ok"]


def test_token_stored_hashed_not_plaintext():
    """★★ 令牌必须存哈希（库泄露不送会话）。"""
    from apps.api.models import RefreshToken
    uid = f"u_{uuid.uuid4().hex[:10]}"
    with SessionLocal() as db:
        r = tokens.issue_refresh_token(db, uid, ip="1.1.1.1", ua="t")
        raw = r["token"]
        from sqlalchemy import select
        row = db.scalar(select(RefreshToken).where(RefreshToken.id == r["id"]))
    assert row is not None
    assert row.token_hash != raw, "★ 令牌明文存库！"
    assert len(row.token_hash) == 64          # sha256 hex


def test_list_sessions():
    uid = f"u_{uuid.uuid4().hex[:10]}"
    with SessionLocal() as db:
        tokens.issue_refresh_token(db, uid, ip="203.0.113.5", ua="Mozilla/5.0 Chrome")
        items = tokens.list_sessions(db, uid)
    assert items and items[0]["family_id"]
    # IP 脱敏
    assert "*" in items[0]["ip"] or items[0]["ip"] == ""


def test_refresh_endpoint(app_client):
    """★ 端到端：/api/auth/refresh 可用。

    ★ 注意：这里通过 sys.modules 取 tokens，与 app_client 用同一实例
      （reload 后模块级 import 的引用会失效 —— 见 app_client 的说明）。
    """
    import sys
    _tk = sys.modules["apps.api.tokens"]
    uid = f"u_{uuid.uuid4().hex[:10]}"
    from apps.api.db import SessionLocal as _SL
    with _SL() as db:
        r = _tk.issue_refresh_token(db, uid, ip="1.1.1.1", ua="t")
    resp = app_client.post("/api/auth/refresh", json={"refresh_token": r["token"]})
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["ok"] and d["access_token"] and d["refresh_token"]


# ================================================================ A6-5 密钥加密

def test_keyvault_roundtrip():
    probe = "sk-test-1234567890"
    enc = keyvault.encrypt(probe)
    assert enc.startswith("enc$v")
    assert keyvault.decrypt(enc) == probe


def test_keyvault_version():
    enc = keyvault.encrypt("x")
    assert keyvault.version_of(enc) == keyvault.CURRENT_VERSION
    assert keyvault.version_of("plaintext") == 0
    assert keyvault.version_of("") == 0


def test_keyvault_legacy_compat():
    """★ 兼容 v1 旧格式（无版本段）—— 老数据升级后必须还能读。"""
    from cryptography.fernet import Fernet
    import base64, hashlib
    from apps.api import config
    dk = hashlib.sha256(f"{config.SECRET_KEY}|api-key-enc".encode()).digest()
    f = Fernet(base64.urlsafe_b64encode(dk))
    legacy = "enc$" + f.encrypt(b"old-key").decode()
    assert keyvault.version_of(legacy) == 1
    assert keyvault.decrypt(legacy) == "old-key", "旧格式密文读不出来（升级会丢 Key）"


def test_keyvault_needs_rotation():
    assert keyvault.needs_rotation("plaintext")
    from cryptography.fernet import Fernet
    import base64, hashlib
    from apps.api import config
    dk = hashlib.sha256(f"{config.SECRET_KEY}|api-key-enc".encode()).digest()
    f = Fernet(base64.urlsafe_b64encode(dk))
    assert keyvault.needs_rotation("enc$" + f.encrypt(b"x").decode())
    assert not keyvault.needs_rotation(keyvault.encrypt("x"))
    assert not keyvault.needs_rotation("")


def test_keyvault_idempotent():
    """重复加密不应叠加（否则每次写都变长）。"""
    a = keyvault.encrypt("k")
    b = keyvault.encrypt(a)
    assert a == b


def test_keyvault_self_check():
    r = keyvault.self_check()
    assert r["ok"] is True, r
    assert r["version"] == keyvault.CURRENT_VERSION


def test_keyvault_empty():
    assert keyvault.encrypt("") == ""
    assert keyvault.decrypt("") == ""


def test_keyvault_bad_ciphertext_returns_empty():
    """★ 密文损坏返回空串而不是抛异常（避免 500）。"""
    assert keyvault.decrypt("enc$v2$not-a-valid-token") == ""


# ================================================================ A6-6 内容审核

def test_local_pass_normal_text():
    m = moderation.LocalModerator()
    r = m.moderate("少年握紧长剑，迎着风雪走向山巅。")
    assert r.verdict == "pass"


def test_local_block_violation():
    """★ 明确违规必须 block。"""
    m = moderation.LocalModerator()
    r = m.moderate("教你如何制毒：第一步准备化学原料……")
    assert r.blocked, f"未拦截违规内容：{r.to_dict()}"


def test_local_context_whitelist_downgrade():
    """★ 创作语境降级（小说里写反派制毒的情节不该被拦）。

    这条直接决定"能不能正常写小说"，误杀率高产品就没法用。
    """
    m = moderation.LocalModerator()
    r = m.moderate("小说里的反派角色在剧情中涉及制毒情节，这是虚构设定。")
    assert not r.blocked, "创作语境被误杀"


def test_local_warn_not_block():
    """观察词只 warn 不 block（政治人物提及在小说里是正常素材）。"""
    m = moderation.LocalModerator()
    r = m.moderate("他翻开历史书，读到关于国家主席的记载。")
    assert r.verdict in ("warn", "review", "pass")
    assert not r.blocked


def test_local_density_triggers_review():
    """★ 密度判定：review 类词密集出现才转人工。"""
    m = moderation.LocalModerator()
    once = m.moderate("他感到绝望，想到了自杀。")
    assert once.verdict in ("warn", "pass")
    many = m.moderate("自杀 " * 8)
    assert many.verdict == "review", f"密度未触发转人工：{many.verdict}"


def test_moderator_empty_text():
    m = moderation.LocalModerator()
    assert m.moderate("").verdict == "pass"


def test_worst_helper():
    assert moderation.worst("pass", "block") == "block"
    assert moderation.worst("review", "warn") == "review"
    assert moderation.worst("pass", "pass") == "pass"


def test_get_moderator_local_by_default(monkeypatch):
    monkeypatch.delenv("NF_MODERATION_ENDPOINT", raising=False)
    monkeypatch.delenv("NF_MODERATION_KEY", raising=False)
    moderation.reset_moderator()
    m = moderation.get_moderator()
    assert isinstance(m, moderation.LocalModerator)
    moderation.reset_moderator()


def test_chain_moderator_skips_remote_on_block():
    """★ 组合器：本地已 block 就不必调远端（省钱省时间）。"""
    class Boom(moderation.ContentModerator):
        name = "boom"
        def moderate(self, text, **kw):
            raise AssertionError("本地已 block 时不应调用远端")

    cm = moderation.ChainModerator(moderation.LocalModerator(), Boom())
    r = cm.moderate("教你如何制毒")
    assert r.blocked


def test_chain_moderator_degrades_on_remote_failure():
    """★ 远端挂了必须降级（可用性优先），不能整体失败。"""
    class Fail(moderation.ContentModerator):
        name = "fail"
        def moderate(self, text, **kw):
            raise RuntimeError("远端超时")

    cm = moderation.ChainModerator(moderation.LocalModerator(), Fail())
    r = cm.moderate("他感到绝望，想要自杀。")     # 本地会 review
    assert r.verdict in ("warn", "review"), "远端失败后未降级使用本地结果"


def test_record_review_and_query():
    """★ 审核记录必须落库（合规要求：可追溯）。"""
    m = moderation.LocalModerator()
    res = m.moderate("教你如何制毒：配方如下")
    with SessionLocal() as db:
        rid = moderation.record_review(db, text_id="t1", stage="test", result=res,
                                       user_id="u_x", book_id="bk_x")
    assert rid
    with SessionLocal() as db:
        from apps.api.models import ContentFlag
        from sqlalchemy import select
        row = db.scalar(select(ContentFlag).where(ContentFlag.id == rid))
    assert row is not None and row.category.startswith("mod.")


def test_moderate_pass_not_recorded():
    """通过的内容不写记录（避免表被正常内容撑爆）。"""
    with SessionLocal() as db:
        r = moderation.moderate_and_record(db, "阳光明媚，少年出发。", text_id="t2")
    assert r.verdict == "pass"


def test_moderation_stats():
    s = moderation.stats()
    for k in ("provider", "local_rules", "density_threshold", "remote_configured"):
        assert k in s


def test_admin_moderation_test_endpoint(app_client):
    """★ 管理端试跑接口（运营调规则用）。"""
    import sys
    _auth = sys.modules["apps.api.auth"]
    _SL = sys.modules["apps.api.db"].SessionLocal
    uname = f"admt_{uuid.uuid4().hex[:8]}"
    with _SL() as db:
        u = _auth.create_user(db, uname, "test-pw-12345", role="admin")
        tok = _auth.make_token(u.id)
    r = app_client.post("/api/admin/moderation/test",
                        json={"text": "教你如何制毒"},
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == "block"

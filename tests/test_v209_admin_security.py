"""E4.14 P0 后台安全加固回归测试（v1.0-E4.14）。

对标成熟 SaaS 后台，覆盖四个 P0 安全/可观测项：

  P0② 审计 before/after diff 快照
       · 积分调整 credit.adjust 留痕（before.balance / after.balance）
       · 退款 order.refund 留痕（before 订单状态快照）
  P0① 支付事件（webhook 留痕）面板后端
       · GET /api/admin/pay/events 支持 provider/type/验签失败 过滤 + 分页 + total
       · GET /api/admin/pay/event/{id}/raw 返回 raw_body（★ 不是 raw）
       · 非管理员 403
  P0④ 后台 IP 白名单（网络层门禁，先于 RBAC/2FA）
       · 配置 security.admin_ip_whitelist 后，CF-Connecting-IP 不在名单 → 403
       · 名单内 / CIDR 命中 → 放行
       · 空 = 不限制
  P0③ 后台强制 2FA（策略层门禁）
       · 开启后非 owner 且未启用 TOTP → 403 code=2FA_REQUIRED
       · 已启用 TOTP 的 admin → 放行
       · owner 作 break-glass 逃生角色 → 豁免
       · 默认关闭 = 不限制

★ 取 IP 一律走 ratelimit.client_ip（CF-Connecting-IP，不可伪造）—— 与 main.py
  中间件、credits.py / pay.py 口径统一（v1.0-E4.14 P0④ 同时修了 admin.py._ctx
  仍用 XFF 的违规取法）。
"""
from __future__ import annotations

import json
import sys
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api import auth
from apps.api.db import SessionLocal
from apps.api.models import (
    AdminAuditLog, Order, PaymentEvent, User,
)
from apps.api import config_center


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="module", autouse=True)
def _ensure_tables():
    from apps.api import models  # noqa: F401
    from apps.api.db import Base, engine
    Base.metadata.create_all(engine)
    from apps.api.services import db_migrate
    db_migrate.migrate()
    try:
        from apps.api import rbac as _rbac
        with SessionLocal() as _db:
            _rbac.seed_role_permissions(_db)
    except Exception:
        pass
    yield


@pytest.fixture(scope="module")
def app_client():
    from apps.api.main import app
    with TestClient(app) as c:
        yield c


def _mk_user(*, role: str = "user", active: int = 1, totp: int = 0) -> tuple[str, str]:
    a = sys.modules.get("apps.api.auth") or __import__("apps.api.auth", fromlist=["auth"])
    d = sys.modules.get("apps.api.db") or __import__("apps.api.db", fromlist=["db"])
    uname = f"sec_{uuid.uuid4().hex[:8]}"
    with d.SessionLocal() as db:
        u = a.create_user(db, uname, "test-pw-12345", role=role)
        u.is_active = active
        u.totp_enabled = int(totp)
        db.commit()
        return u.id, a.make_token(u.id)


@pytest.fixture()
def admin_tok():
    _, t = _mk_user(role="admin")
    return t


@pytest.fixture()
def owner_tok():
    _, t = _mk_user(role="owner")
    return t


@pytest.fixture()
def user_tok():
    _, t = _mk_user(role="user")
    return t


def _h(tok: str, ip: str = "") -> dict:
    h = {"Authorization": f"Bearer {tok}"}
    if ip:
        # ★ 中间件/client_ip 用 CF-Connecting-IP 取真实客户端 IP（不可伪造）。
        h["CF-Connecting-IP"] = ip
    return h


# ── 模块实例对齐（autouse）：每个测试前从 sys.modules 重取当前实例 ──
#
# ★ 为什么必须（MEMORY「测试模块实例分裂」，v208 `_fresh_modules` 同款成熟解法）：
#   全量跑时上游测试（如 test_g06b）会 monkeypatch `apps.api.db`/`main`/`auth` 的
#   SessionLocal 到各自 tmp 库，本文件顶层 `from apps.api.db import SessionLocal`
#   只在收集期绑定一次 → 之后该静态引用指向旧实例（库 A），而 app（apps.api.main）
#   每次请求用当前 sys.modules 的 db.SessionLocal（库 B）。结果：我写的配置/事件/
#   审计落在 A，app 端点从 B 读 → 读不到 → 「拦截/写库」类用例全挂、
#   「放行/非admin」类（不依赖我造的库）通过。
#   单跑本文件时没有上游 reload，故 13/13 全过、全量却 7 挂 —— 典型假回归。
#   修法：每测试前把 global 重绑到 sys.modules 当前实例（与 v208 完全一致）。
@pytest.fixture(autouse=True)
def _fresh_modules():
    import importlib
    global auth, SessionLocal, config_center, User, Order, PaymentEvent, AdminAuditLog
    auth = sys.modules.get("apps.api.auth") \
        or importlib.import_module("apps.api.auth")
    dbmod = sys.modules.get("apps.api.db") \
        or importlib.import_module("apps.api.db")
    SessionLocal = dbmod.SessionLocal
    config_center = sys.modules.get("apps.api.config_center") \
        or importlib.import_module("apps.api.config_center")
    m = sys.modules.get("apps.api.models") \
        or importlib.import_module("apps.api.models")
    User, Order, PaymentEvent, AdminAuditLog = \
        m.User, m.Order, m.PaymentEvent, m.AdminAuditLog
    yield


# ── 安全配置重置（autouse）：每个测试后把两个 P0 配置项还原默认，杜绝跨测试污染 ──

@pytest.fixture(autouse=True)
def _reset_security_cfg():
    yield
    with SessionLocal() as db:
        try:
            config_center.set_config(db, "security.admin_ip_whitelist", "",
                                    reason="测试清理", source="test")
            config_center.set_config(db, "security.admin_force_2fa", False,
                                    reason="测试清理", source="test")
        except Exception:
            pass


def _set_cfg(key: str, value):
    with SessionLocal() as db:
        config_center.set_config(db, key, value, reason="E4.14 测试",
                                 source="test", commit=True)


def _audit_log(action: str) -> AdminAuditLog | None:
    """取最近一条某 action 的审计日志。"""
    with SessionLocal() as db:
        return db.scalars(
            select(AdminAuditLog).where(AdminAuditLog.action == action)
            .order_by(AdminAuditLog.created_at.desc()).limit(1)
        ).first()


# ================================================================ P0② 审计 diff

def test_credit_adjust_writes_audit_with_diff(app_client, admin_tok):
    uid, _ = _mk_user(role="user")
    # 调积分前余额（新建用户无 CreditAccount → before=0）
    r = app_client.post("/api/admin/credits/adjust",
                       json={"user_id": uid, "delta": 500, "note": "测试加积分"},
                       headers=_h(admin_tok))
    assert r.status_code == 200
    log = _audit_log("credit.adjust")
    assert log is not None, "积分调整必须写审计（P0② 强制留痕）"
    d = json.loads(log.detail)
    # ★ diff 快照：before / after 余额都在（排障"谁在何时把谁的积分从多少改到多少"）
    assert "before" in d and "after" in d, f"审计 detail 缺 diff 快照：{d}"
    assert d["before"]["balance"] == 0
    assert d["after"]["balance"] == 500
    assert d["after"]["delta"] == 500


def test_refund_writes_audit_with_before_snapshot(app_client, admin_tok):
    uid, _ = _mk_user(role="user")
    oid = f"o_{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db:
        db.add(Order(id=oid, user_id=uid, status="paid", pay_channel="sandbox",
                     amount_cents=100, credits=0))
        db.commit()
    # 退款（渠道可能成功/失败，但**成功与失败都必须写审计**）
    app_client.post("/api/admin/pay/refund",
                   json={"order_id": oid, "reason": "测试退款"},
                   headers=_h(admin_tok))
    log = _audit_log("order.refund")
    assert log is not None, "退款必须写审计（P0② 强制留痕）"
    d = json.loads(log.detail)
    # ★ before 快照记录退款前订单状态 paid
    assert "before" in d, f"退款审计缺 before 快照：{d}"
    assert d["before"].get("status") == "paid"


# ================================================================ P0① 事件面板

def test_pay_events_filter_and_total(app_client, admin_tok):
    base = f"pe_{uuid.uuid4().hex[:10]}"
    # ★ 唯一 order_id（幂等）：sig_only/provider 都是**全表**过滤，全量跑时上游
    #   测试（退款 record_event 等）会遗留其他验签失败事件 → 裸 sig_only 计数被污染。
    #   故叠加唯一 order_id（list_events 对 order_id 是 == 精确匹配）锁定子集，
    #   total 恒为确定值，不依赖库内现状。
    o1 = base + "o1"   # alipay / notify / 验签 OK
    o2 = base + "o2"   # wechat / refund / 验签失败
    with SessionLocal() as db:
        db.add(PaymentEvent(id=base + "a", order_id=o1, provider="alipay",
                            event_type="notify", amount_cents=100,
                            raw_body='{"ok":true}', signature_ok=1, handled=1))
        db.add(PaymentEvent(id=base + "b", order_id=o2, provider="wechat",
                            event_type="refund", amount_cents=50,
                            raw_body='{"ok":false}', signature_ok=0, handled=0))
        db.commit()
    # 只看验签失败（风控：伪造签名攻击痕迹）+ 锁到 o2 子集 → 恒 total=1
    r = app_client.get("/api/admin/pay/events",
                       params={"sig_only": 1, "order_id": o2},
                       headers=_h(admin_tok))
    assert r.status_code == 200
    body = r.json()
    assert "total" in body and "items" in body, "P0① 需返回 {total, items}（分页）"
    assert body["total"] == 1
    assert body["items"][0]["signature_ok"] == 0
    assert body["items"][0]["provider"] == "wechat"
    # provider 过滤 + 锁到 o1 子集 → 恒 total=1
    r2 = app_client.get("/api/admin/pay/events",
                        params={"provider": "alipay", "order_id": o1},
                        headers=_h(admin_tok))
    assert r2.json()["total"] == 1
    assert r2.json()["items"][0]["event_type"] == "notify"


def test_pay_events_raw_returns_raw_body(app_client, admin_tok, user_tok):
    base = f"pe_{uuid.uuid4().hex[:10]}"
    raw = '{"trade_no":"T123","amount":29900}'
    with SessionLocal() as db:
        db.add(PaymentEvent(id=base, order_id="o9", provider="stripe",
                            event_type="notify", amount_cents=29900,
                            raw_body=raw, signature_ok=1))
        db.commit()
    r = app_client.get(f"/api/admin/pay/event/{base}/raw", headers=_h(admin_tok))
    assert r.status_code == 200
    body = r.json()
    # ★★ 契约：字段是 raw_body（不是 raw）—— 前端最早写错过，必须对照源码锁定
    assert "raw_body" in body, f"事件 raw 端点必须返回 raw_body：{list(body)}"
    assert body["raw_body"] == raw


def test_pay_events_rejects_nonadmin(app_client, user_tok):
    r = app_client.get("/api/admin/pay/events", headers=_h(user_tok))
    assert r.status_code == 403


# ================================================================ P0④ IP 白名单

def test_ip_whitelist_blocks_outsider(app_client, admin_tok):
    _set_cfg("security.admin_ip_whitelist", "203.0.113.5")
    r = app_client.get("/api/admin/dashboard", headers=_h(admin_tok, ip="8.8.8.8"))
    assert r.status_code == 403
    assert "白名单" in (r.json().get("detail") or "")


def test_ip_whitelist_allows_member(app_client, admin_tok):
    _set_cfg("security.admin_ip_whitelist", "203.0.113.5")
    r = app_client.get("/api/admin/dashboard", headers=_h(admin_tok, ip="203.0.113.5"))
    assert r.status_code == 200, r.text[:200]


def test_ip_whitelist_cidr_match(app_client, admin_tok):
    _set_cfg("security.admin_ip_whitelist", "198.51.100.0/24")
    # 命中网段 → 放行
    r = app_client.get("/api/admin/dashboard", headers=_h(admin_tok, ip="198.51.100.42"))
    assert r.status_code == 200
    # 不在网段 → 拦截
    r2 = app_client.get("/api/admin/dashboard", headers=_h(admin_tok, ip="203.0.113.99"))
    assert r2.status_code == 403


def test_ip_whitelist_empty_no_restrict(app_client, admin_tok):
    # 默认空 = 不限制（任何 IP 都能进，靠 RBAC/2FA 兜底）
    r = app_client.get("/api/admin/dashboard", headers=_h(admin_tok, ip="8.8.8.8"))
    assert r.status_code == 200


# ================================================================ P0③ 强制 2FA

def test_force_2fa_blocks_admin_without_totp(app_client, admin_tok):
    _set_cfg("security.admin_force_2fa", True)
    r = app_client.get("/api/admin/dashboard", headers=_h(admin_tok))
    assert r.status_code == 403
    assert r.json().get("code") == "2FA_REQUIRED", r.text[:200]


def test_force_2fa_allows_admin_with_totp(app_client):
    _, tok = _mk_user(role="admin", totp=1)
    _set_cfg("security.admin_force_2fa", True)
    r = app_client.get("/api/admin/dashboard", headers=_h(tok))
    assert r.status_code == 200, r.text[:200]


def test_force_2fa_owner_exempt(app_client, owner_tok):
    # ★ owner 是 break-glass 逃生角色：全体管理员忘 2FA 时仍能进后台解锁
    _set_cfg("security.admin_force_2fa", True)
    r = app_client.get("/api/admin/dashboard", headers=_h(owner_tok))
    assert r.status_code == 200, r.text[:200]


def test_force_2fa_off_no_restrict(app_client, admin_tok):
    # 默认关闭：无 TOTP 的 admin 也能进（P0③ 是可选加固，不默认锁死）
    r = app_client.get("/api/admin/dashboard", headers=_h(admin_tok))
    assert r.status_code == 200, r.text[:200]

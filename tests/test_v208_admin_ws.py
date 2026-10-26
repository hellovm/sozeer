"""WS1/WS2/WS3 后台与用户面板后端回归测试（v1.0-E4.12）。

覆盖：
  WS1 内容审核队列：PATCH /api/admin/flags/{id}（隐藏/澄清/封禁/重开）
  WS2 用户数据导出/注销 + 批量操作：
      GET  /api/admin/users/{uid}/export（ZIP）
      POST /api/admin/users/bulk（ban/unban/set_role/adjust_credits）
      DELETE /api/admin/users/{uid}（级联硬删，修复只删 User 行的缺陷）
  WS3 用户面板用量概览 + 续费引导：
      GET /api/me/plan（当前套餐推导）
      GET /api/me/usage（扩展字段：字数/短剧/任务）
"""
from __future__ import annotations

import uuid
import zipfile
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from apps.api import admin_service as svc
from apps.api import auth
from apps.api.db import SessionLocal
from apps.api.models import (
    Book, Chapter, ContentFlag, CreditAccount, DramaProject, Order, User, AdminAuditLog,
)


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


def _mk_user(*, role: str = "user", active: int = 1) -> tuple[str, str]:
    import sys
    import importlib
    a = sys.modules.get("apps.api.auth") or importlib.import_module("apps.api.auth")
    d = sys.modules.get("apps.api.db") or importlib.import_module("apps.api.db")
    uname = f"t_{uuid.uuid4().hex[:8]}"
    with d.SessionLocal() as db:
        u = a.create_user(db, uname, "test-pw-12345", role=role)
        u.is_active = active
        db.commit()
        return u.id, a.make_token(u.id)


@pytest.fixture(autouse=True)
def _fresh_modules():
    import sys
    import importlib
    global auth, SessionLocal, svc, User, Book, Chapter, ContentFlag, CreditAccount, DramaProject, Order, AdminAuditLog
    auth = sys.modules.get("apps.api.auth") or importlib.import_module("apps.api.auth")
    dbmod = sys.modules.get("apps.api.db") or importlib.import_module("apps.api.db")
    SessionLocal = dbmod.SessionLocal
    svc = (sys.modules.get("apps.api.admin_service")
           or importlib.import_module("apps.api.admin_service"))
    m = sys.modules.get("apps.api.models") or importlib.import_module("apps.api.models")
    User, Book, Chapter = m.User, m.Book, m.Chapter
    ContentFlag, CreditAccount = m.ContentFlag, m.CreditAccount
    DramaProject, Order, AdminAuditLog = m.DramaProject, m.Order, m.AdminAuditLog
    yield


@pytest.fixture()
def admin_tok():
    _, t = _mk_user(role="admin")
    return t


@pytest.fixture()
def user_tok():
    _, t = _mk_user(role="user")
    return t


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def _mk_book(owner_id: str, title: str = "WS测试书") -> str:
    bid = f"bk_{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db:
        db.add(Book(id=bid, title=title, author="作者", owner_id=owner_id))
        db.add(Chapter(id=f"ch_{uuid.uuid4().hex[:12]}", book_id=bid, idx=1,
                       title="第一章", content="正文", words=100))
        db.add(DramaProject(id=f"dp_{uuid.uuid4().hex[:12]}", book_id=bid,
                            title="短剧", owner_id=owner_id))
        db.commit()
    return bid


def _flag_book(admin_tok, book_id) -> str:
    """直接调用服务层标记一本书，返回 flag_id。"""
    from apps.api import admin_service as _s
    import sys
    a = sys.modules.get("apps.api.auth")
    with SessionLocal() as db:
        op = db.get(User, a.parse_token(admin_tok))
        r = _s.flag_content(db, book_id=book_id, category="other", severity="warn",
                            operator=op, reason="测试标记")
    return r["flag_id"]


# ================================================================ WS1 审核队列

def test_resolve_flag_hidden_syncs_book(app_client, admin_tok, user_tok):
    uid = auth.parse_token(user_tok)
    bid = _mk_book(uid)
    fid = _flag_book(admin_tok, bid)
    r = app_client.patch(f"/api/admin/flags/{fid}",
                         json={"status": "hidden", "reason": "违规处置"},
                         headers=_h(admin_tok))
    assert r.status_code == 200 and r.json()["ok"]
    with SessionLocal() as db:
        b = db.get(Book, bid)
        assert b.content_status == "hidden"
        # 审计留痕
        logs = db.scalars(__import__("sqlalchemy").select(AdminAuditLog)
                          .where(AdminAuditLog.target_id == fid,
                                 AdminAuditLog.action == "book.flag.resolve")).all()
    assert logs and logs[-1].result == "ok"


def test_resolve_flag_cleared_restores(app_client, admin_tok, user_tok):
    uid = auth.parse_token(user_tok)
    bid = _mk_book(uid)
    fid = _flag_book(admin_tok, bid)
    app_client.patch(f"/api/admin/flags/{fid}",
                     json={"status": "hidden", "reason": "x"}, headers=_h(admin_tok))
    r = app_client.patch(f"/api/admin/flags/{fid}",
                         json={"status": "cleared", "reason": "误报澄清"},
                         headers=_h(admin_tok))
    assert r.status_code == 200 and r.json()["ok"]
    with SessionLocal() as db:
        b = db.get(Book, bid)
        assert b.content_status == "normal"
        assert int(b.flagged or 0) == 0


def test_resolve_flag_banned_bans_author(app_client, admin_tok, user_tok):
    uid = auth.parse_token(user_tok)
    bid = _mk_book(uid)
    fid = _flag_book(admin_tok, bid)
    r = app_client.patch(f"/api/admin/flags/{fid}",
                         json={"status": "banned", "reason": "严重违规封作者"},
                         headers=_h(admin_tok))
    assert r.status_code == 200 and r.json()["ok"]
    with SessionLocal() as db:
        u = db.get(User, uid)
        assert int(u.is_active or 0) == 0


def test_resolve_flag_rejects_nonadmin(app_client, user_tok):
    r = app_client.patch("/api/admin/flags/whatever",
                         json={"status": "hidden"}, headers=_h(user_tok))
    assert r.status_code == 403


def test_resolve_flag_invalid_status(app_client, admin_tok, user_tok):
    uid = auth.parse_token(user_tok)
    bid = _mk_book(uid)
    fid = _flag_book(admin_tok, bid)
    r = app_client.patch(f"/api/admin/flags/{fid}",
                         json={"status": "nonsense"}, headers=_h(admin_tok))
    assert r.status_code == 400


# ================================================================ WS2 导出 / 注销 / 批量

def test_admin_export_user_zip(app_client, admin_tok, user_tok):
    r = app_client.get(f"/api/admin/users/{auth.parse_token(user_tok)}/export",
                       headers=_h(admin_tok))
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers.get("content-disposition", "")
    # 是合法 zip
    z = zipfile.ZipFile(BytesIO(r.content))
    assert "profile.json" in z.namelist()


def test_bulk_ban(app_client, admin_tok):
    u1, _ = _mk_user()
    u2, _ = _mk_user()
    r = app_client.post("/api/admin/users/bulk",
                        json={"action": "ban", "user_ids": [u1, u2],
                              "reason": "批量违规"}, headers=_h(admin_tok))
    assert r.status_code == 200
    j = r.json()
    assert j["done"] == 2
    with SessionLocal() as db:
        assert int(db.get(User, u1).is_active or 0) == 0
        assert int(db.get(User, u2).is_active or 0) == 0


def test_bulk_adjust_credits(app_client, admin_tok):
    uid, _ = _mk_user()
    with SessionLocal() as db:
        if db.get(CreditAccount, uid) is None:
            db.add(CreditAccount(user_id=uid, balance=0))
            db.commit()
    r = app_client.post("/api/admin/users/bulk",
                        json={"action": "adjust_credits", "user_ids": [uid],
                              "delta": 500, "reason": "补偿"}, headers=_h(admin_tok))
    assert r.status_code == 200 and r.json()["done"] == 1
    with SessionLocal() as db:
        assert int(db.get(CreditAccount, uid).balance or 0) == 500


def test_bulk_set_role(app_client, admin_tok):
    uid, _ = _mk_user(role="user")
    r = app_client.post("/api/admin/users/bulk",
                        json={"action": "set_role", "user_ids": [uid],
                              "role": "editor", "reason": "转正"}, headers=_h(admin_tok))
    assert r.status_code == 200
    with SessionLocal() as db:
        assert db.get(User, uid).role == "editor"


def test_cascade_delete_removes_book_and_keeps_vouchers(app_client, admin_tok):
    """★ 修复：原 DELETE 只删 User 行，留下孤儿书。现应级联硬删全部归属数据。"""
    uid, _ = _mk_user()
    bid = _mk_book(uid)
    # 留一笔订单（会计凭证，应脱敏保留）
    with SessionLocal() as db:
        db.add(Order(id=f"or_{uuid.uuid4().hex[:12]}", user_id=uid,
                     plan_key="pro", status="paid", amount_cents=9900))
        db.commit()
    r = app_client.request("DELETE", f"/api/admin/users/{uid}",
                           json={"reason": "违规清理"}, headers=_h(admin_tok))
    assert r.status_code == 200 and r.json()["ok"]
    with SessionLocal() as db:
        assert db.get(User, uid) is None
        assert db.get(Book, bid) is None
        # 订单脱敏保留（user_id 置空），不丢
        ords = db.scalars(__import__("sqlalchemy").select(Order)
                          .where(Order.user_id == "")).all()
    assert any(o.status == "paid" for o in ords)


def test_cascade_delete_rejects_self(app_client, admin_tok):
    uid = auth.parse_token(admin_tok)
    r = app_client.request("DELETE", f"/api/admin/users/{uid}",
                           json={"reason": "x"}, headers=_h(admin_tok))
    assert r.status_code == 400


# ================================================================ WS3 用量 / 套餐

def test_me_plan_free_for_new(app_client, user_tok):
    r = app_client.get("/api/me/plan", headers=_h(user_tok))
    assert r.status_code == 200
    j = r.json()
    assert j["is_trial"] is True
    assert j["plan_key"] == "free"


def test_me_usage_has_new_fields(app_client, user_tok):
    uid = auth.parse_token(user_tok)
    _mk_book(uid)
    r = app_client.get("/api/me/usage", headers=_h(user_tok))
    assert r.status_code == 200
    j = r.json()
    for k in ("books", "chapters", "words_total", "words_month",
              "dramas", "tasks", "balance", "total_spent"):
        assert k in j, f"缺少字段 {k}"
    assert j["words_total"] >= 100
    assert j["dramas"] >= 1

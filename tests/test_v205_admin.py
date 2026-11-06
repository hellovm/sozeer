"""A5 管理后台回归测试（v0.20.5）。

★ 核心关注点（每条都对应真实的越权/合规风险）：
  ① **越权**：非管理员访问任何 /api/admin/* 必须 403（不是 401，也不是 200）
  ② **审计留痕**：所有敏感操作必须写日志，包括**失败的尝试**
  ③ **强制原因**：敏感操作无原因必须被拒（防止"悄悄封号"）
  ④ **隐私红线**：admin 端点**不得返回任何正文内容**
  ⑤ **看板一致性**：看板数字必须与数据库实际一致（不能是假的）
  ⑥ **CSV 注入**：导出必须以 ' 前缀防 Excel 公式注入
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from apps.api import admin_service as svc
from apps.api import auth, credits
from apps.api.db import SessionLocal
from apps.api.models import AdminAuditLog, Book, Chapter, ContentFlag, User


@pytest.fixture(scope="module", autouse=True)
def _ensure_tables():
    from apps.api import models  # noqa: F401
    from apps.api.db import Base, engine
    Base.metadata.create_all(engine)
    # ★ 补列迁移：create_all 只建表不改表，books 的新列必须显式补
    from apps.api.services import db_migrate
    db_migrate.migrate()
    # ★ v0.20.7 A7：seed RBAC 角色权限（否则 require_admin 走 RBAC 后无权限）
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
    """建用户，返回 (id, token)。

    ★ 必须从 sys.modules 取**当前**的 auth/db 模块：
      其他测试（test_v180_t1_split）会清空 sys.modules 重新导入，
      导致本模块顶部 import 的引用与运行时不是同一个对象
      —— 表现为"单跑通过、全量跑 401"。
    """
    import sys
    a = sys.modules.get("apps.api.auth")
    if a is None:
        import importlib
        a = importlib.import_module("apps.api.auth")
    d = sys.modules.get("apps.api.db")
    if d is None:
        import importlib
        d = importlib.import_module("apps.api.db")
    uname = f"t_{uuid.uuid4().hex[:8]}"
    with d.SessionLocal() as db:
        u = a.create_user(db, uname, "test-pw-12345", role=role)
        u.is_active = active
        db.commit()
        return u.id, a.make_token(u.id)


@pytest.fixture(autouse=True)
def _fresh_modules():
    """★ 每个测试前把模块引用刷新到 sys.modules 的当前实例。

    为什么必须这样做（真实踩坑）：
      test_v180_t1_split::test_no_circular_import 会 `del sys.modules['apps.api.*']`
      再重新导入。此后 sys.modules 里的是**新模块对象**，而本测试文件顶部
      `from apps.api import auth` 拿到的仍是**旧对象**。
      · 旧 auth 用旧 SECRET_KEY 签的令牌 → 新 auth 解析（SECRET_KEY 相同其实能解）
      · 但**旧 SessionLocal 绑定的 engine 与运行时不同** → 数据写不到一起
      统一从 sys.modules 取当前实例，才能保证"写进去的能读出来"。
    """
    import sys
    import importlib
    global auth, SessionLocal, svc
    global User, Book, Chapter, ContentFlag, AdminAuditLog
    auth = sys.modules.get("apps.api.auth") or importlib.import_module("apps.api.auth")
    dbmod = sys.modules.get("apps.api.db") or importlib.import_module("apps.api.db")
    SessionLocal = dbmod.SessionLocal
    svc = (sys.modules.get("apps.api.admin_service")
           or importlib.import_module("apps.api.admin_service"))
    m = sys.modules.get("apps.api.models") or importlib.import_module("apps.api.models")
    User, Book, Chapter = m.User, m.Book, m.Chapter
    ContentFlag, AdminAuditLog = m.ContentFlag, m.AdminAuditLog
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


def _mk_book(owner_id: str, book_title: str = "测试书") -> str:
    bid = f"bk_{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db:
        db.add(Book(id=bid, title=book_title, author="作者", owner_id=owner_id))
        db.add(Chapter(id=f"ch_{uuid.uuid4().hex[:12]}", book_id=bid, idx=1,
                       title="第一章", content="这是**机密正文**内容，管理员不应看到。",
                       words=100))
        db.commit()
    return bid


# ================================================================ ① 越权防护

ADMIN_GETS = [
    "/api/admin/dashboard",
    "/api/admin/dashboard/trend",
    "/api/admin/health",
    "/api/admin/users/search",
    "/api/admin/audit",
    "/api/admin/books",
    "/api/admin/flags",
    "/api/admin/orders",
    "/api/admin/orders/export",
]


@pytest.mark.parametrize("path", ADMIN_GETS)
def test_admin_endpoints_reject_anonymous(app_client, path):
    """★ 未登录访问 admin 端点必须是 401（不是 200）。"""
    r = app_client.get(path)
    assert r.status_code == 401, f"{path} 未拦截匿名访问：{r.status_code}"


@pytest.mark.parametrize("path", ADMIN_GETS)
def test_admin_endpoints_reject_normal_user(app_client, user_tok, path):
    """★ 普通登录用户访问 admin 端点必须是 403（越权是最高危问题）。"""
    r = app_client.get(path, headers=_h(user_tok))
    assert r.status_code == 403, f"{path} 未拦截普通用户：{r.status_code}"


def test_admin_write_endpoints_reject_normal_user(app_client, user_tok):
    """★ 写操作同样必须拦截（漏一个就等于开放了封号/调分能力）。"""
    for path, body in [
        ("/api/admin/users/ban", {"user_id": "x", "active": False, "reason": "test"}),
        ("/api/admin/users/role", {"user_id": "x", "role": "admin", "reason": "test"}),
        ("/api/admin/books/flag", {"book_id": "x", "category": "other"}),
        ("/api/admin/books/content-status", {"book_id": "x", "status": "hidden"}),
    ]:
        r = app_client.post(path, json=body, headers=_h(user_tok))
        assert r.status_code == 403, f"{path} 未拦截普通用户：{r.status_code}"


def test_admin_endpoints_allow_admin(app_client, admin_tok):
    """管理员必须能正常访问（否则后台不可用）。"""
    for path in ADMIN_GETS:
        r = app_client.get(path, headers=_h(admin_tok))
        assert r.status_code == 200, f"{path} 管理员被拒：{r.status_code} {r.text[:200]}"


# ================================================================ ② 隐私红线

def test_admin_books_never_returns_content(app_client, admin_tok, user_tok):
    """★★ 隐私红线：admin 书籍端点**不得**返回章节正文。

    为什么这条最重要：管理员能看正文 = 用户的作品对平台完全透明，
    这是产品信任的根基。一旦破坏，无从补救。
    """
    with SessionLocal() as db:
        u = db.get(User, auth.parse_token(user_tok))
    bid = _mk_book(u.id, "隐私测试书")
    r = app_client.get("/api/admin/books?keyword=隐私测试书", headers=_h(admin_tok))
    assert r.status_code == 200
    raw = r.text
    assert "机密正文" not in raw, "★ 严重：admin 端点泄露了章节正文！"
    assert bid in raw
    # 检查字段白名单
    item = [i for i in r.json()["items"] if i["id"] == bid][0]
    forbidden = {"body", "content", "text", "chapter_body", "summary", "synopsis"}
    assert not (forbidden & set(item.keys())), f"返回了不该有的字段：{forbidden & set(item.keys())}"


def test_user_search_masks_phone(app_client, admin_tok):
    """★ 手机号必须脱敏（管理员不需要看到完整手机号）。"""
    uname = f"ph_{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        u = auth.create_user(db, uname, "test-pw-12345")
        u.phone = "13800138000"
        db.commit()
    r = app_client.get(f"/api/admin/users/search?keyword={uname}", headers=_h(admin_tok))
    item = r.json()["items"][0]
    assert item["phone"] == "138****8000", f"手机号未脱敏：{item['phone']}"


# ================================================================ ③ 审计留痕

def test_ban_writes_audit_log(app_client, admin_tok):
    """★ 封禁必须留痕（出事后能自证"谁在何时做了什么"）。"""
    uid, _ = _mk_user()
    r = app_client.post("/api/admin/users/ban",
                        json={"user_id": uid, "active": False, "reason": "测试封禁原因"},
                        headers=_h(admin_tok))
    assert r.status_code == 200 and r.json()["ok"]
    with SessionLocal() as db:
        logs = db.scalars(
            __import__("sqlalchemy").select(AdminAuditLog)
            .where(AdminAuditLog.target_id == uid,
                   AdminAuditLog.action == "user.ban")).all()
    assert logs, "封禁未写审计日志"
    lg = logs[-1]
    assert lg.reason == "测试封禁原因"
    assert lg.result == "ok"
    assert lg.operator_name, "未记录操作人"


def test_ban_actually_takes_effect(app_client, admin_tok):
    """★ 封禁后该用户的令牌必须立即失效（这是封禁的意义）。"""
    uid, tok = _mk_user()
    # 封禁前可访问
    assert app_client.get("/api/books", headers=_h(tok)).status_code == 200
    app_client.post("/api/admin/users/ban",
                    json={"user_id": uid, "active": False, "reason": "违规测试"},
                    headers=_h(admin_tok))
    # ★ 封禁后同一令牌必须被拒
    r = app_client.get("/api/books", headers=_h(tok))
    assert r.status_code == 401, f"封禁后旧令牌仍可用：{r.status_code}"


def test_unban_restores_access(app_client, admin_tok):
    """解封后应恢复访问。"""
    uid, tok = _mk_user()
    app_client.post("/api/admin/users/ban",
                    json={"user_id": uid, "active": False, "reason": "先封"},
                    headers=_h(admin_tok))
    assert app_client.get("/api/books", headers=_h(tok)).status_code == 401
    app_client.post("/api/admin/users/ban",
                    json={"user_id": uid, "active": True, "reason": "申诉通过解封"},
                    headers=_h(admin_tok))
    assert app_client.get("/api/books", headers=_h(tok)).status_code == 200


def test_sensitive_action_requires_reason(app_client, admin_tok):
    """★ 敏感操作无原因必须被拒（防"悄悄封号"）。"""
    uid, _ = _mk_user()
    r = app_client.post("/api/admin/users/ban",
                        json={"user_id": uid, "active": False, "reason": ""},
                        headers=_h(admin_tok))
    assert r.status_code == 200
    assert r.json()["ok"] is False, "空原因竟然通过了"
    # ★ 被拒的尝试也必须留痕（失败可能是攻击）
    with SessionLocal() as db:
        logs = db.scalars(
            __import__("sqlalchemy").select(AdminAuditLog)
            .where(AdminAuditLog.target_id == uid,
                   AdminAuditLog.result == "rejected")).all()
    assert logs, "被拒的敏感操作未留痕"


def test_audit_log_is_readable(app_client, admin_tok):
    """审计日志可查询。"""
    r = app_client.get("/api/admin/audit?limit=5", headers=_h(admin_tok))
    assert r.status_code == 200
    assert "items" in r.json() and "total" in r.json()


def test_role_change_logged(app_client, admin_tok):
    """★ 提权必须留痕（最高危操作）。

    ★ v0.20.7 A7 调整：原来提权到 admin，但 A7 引入"不能操作
      rank ≥ 自己的成员"规则 → admin 提 admin 被正确拦截。
      本测试的目的只是验证**留痕**，故改为提到 editor（合法操作）。
      防同级提权的规则由 test_v207_rbac.py 专门覆盖。
    """
    uid, _ = _mk_user()
    r = app_client.post("/api/admin/users/role",
                        json={"user_id": uid, "role": "editor", "reason": "晋升测试"},
                        headers=_h(admin_tok))
    assert r.json()["ok"], r.json()
    with SessionLocal() as db:
        n = db.scalar(
            __import__("sqlalchemy").select(__import__("sqlalchemy").func.count())
            .select_from(AdminAuditLog)
            .where(AdminAuditLog.target_id == uid,
                   AdminAuditLog.action == "user.role_change"))
    assert n >= 1


def test_cannot_ban_self(app_client, admin_tok):
    """★ 不能封禁自己（否则可能把自己锁在外面）。"""
    uid = auth.parse_token(admin_tok)
    r = app_client.post("/api/admin/users/ban",
                        json={"user_id": uid, "active": False, "reason": "误操作"},
                        headers=_h(admin_tok))
    assert r.json()["ok"] is False


# ================================================================ ④ 看板一致性

def test_dashboard_matches_database(app_client, admin_tok):
    """★ 看板数字必须与数据库实际一致（不能是编的）。"""
    r = app_client.get("/api/admin/dashboard", headers=_h(admin_tok))
    assert r.status_code == 200
    d = r.json()
    with SessionLocal() as db:
        from sqlalchemy import func, select
        n_users = db.scalar(select(func.count()).select_from(User)) or 0
        n_books = db.scalar(select(func.count()).select_from(Book)) or 0
        n_ch = db.scalar(select(func.count()).select_from(Chapter)) or 0
    assert d["users"]["total"] == n_users, f"用户数不符：{d['users']['total']} vs {n_users}"
    assert d["content"]["books"] == n_books
    assert d["content"]["chapters"] == n_ch


def test_dashboard_has_all_sections(app_client, admin_tok):
    """看板必须包含所有承诺的区块（缺了前端会报错）。"""
    d = app_client.get("/api/admin/dashboard", headers=_h(admin_tok)).json()
    for k in ("users", "revenue", "content", "tasks", "moderation", "credits",
              "generated_at", "window_days"):
        assert k in d, f"看板缺少区块：{k}"
    for k in ("total", "active", "new_in_window"):
        assert k in d["users"]
    for k in ("total_cents", "net_cents", "refunded_cents", "pending_orders"):
        assert k in d["revenue"]


def test_dashboard_trend_shape(app_client, admin_tok):
    """趋势数据结构正确（前端直接消费）。"""
    t = app_client.get("/api/admin/dashboard/trend?days=7", headers=_h(admin_tok)).json()
    assert len(t["labels"]) == 7
    for k in ("labels", "new_users", "new_books", "paid_orders", "revenue_cents"):
        assert k in t
        assert len(t[k]) == 7, f"{k} 长度不符"


def test_revenue_net_equals_gross_minus_refund(app_client, admin_tok):
    """★ 净额 = 总额 - 退款（财务口径必须自洽）。"""
    d = app_client.get("/api/admin/dashboard", headers=_h(admin_tok)).json()["revenue"]
    assert d["net_cents"] == d["total_cents"] - d["refunded_cents"]


# ================================================================ ⑤ 内容治理

def test_flag_content(app_client, admin_tok, user_tok):
    """标记内容（★ 注意：不读正文）。"""
    u = User(id=auth.parse_token(user_tok)) if False else None
    with SessionLocal() as db:
        owner = db.get(User, auth.parse_token(user_tok))
    bid = _mk_book(owner.id, "待标记书")
    r = app_client.post("/api/admin/books/flag",
                        json={"book_id": bid, "category": "porn", "severity": "block",
                              "note": "疑似违规", "reason": "举报核实"},
                        headers=_h(admin_tok))
    assert r.status_code == 200 and r.json()["ok"]
    with SessionLocal() as db:
        b = db.get(Book, bid)
        assert b.flagged == 1, "book.flagged 未置位"
        n = db.scalar(__import__("sqlalchemy").select(
            __import__("sqlalchemy").func.count()).select_from(ContentFlag)
            .where(ContentFlag.book_id == bid))
    assert n == 1


def test_flag_rejects_bad_category(app_client, admin_tok, user_tok):
    """非法分类必须 400。"""
    with SessionLocal() as db:
        owner = db.get(User, auth.parse_token(user_tok))
    bid = _mk_book(owner.id)
    r = app_client.post("/api/admin/books/flag",
                        json={"book_id": bid, "category": "不存在的分类"},
                        headers=_h(admin_tok))
    assert r.status_code == 400


def test_hide_and_restore_content(app_client, admin_tok, user_tok):
    """隐藏 → 恢复 全流程，且恢复时结清标记。"""
    with SessionLocal() as db:
        owner = db.get(User, auth.parse_token(user_tok))
    bid = _mk_book(owner.id, "隐藏测试书")
    app_client.post("/api/admin/books/flag",
                    json={"book_id": bid, "category": "other", "reason": "先标记"},
                    headers=_h(admin_tok))
    # 隐藏
    r = app_client.post("/api/admin/books/content-status",
                        json={"book_id": bid, "status": "hidden", "reason": "确认违规"},
                        headers=_h(admin_tok))
    assert r.json()["ok"]
    with SessionLocal() as db:
        assert db.get(Book, bid).content_status == "hidden"
    # 恢复
    r = app_client.post("/api/admin/books/content-status",
                        json={"book_id": bid, "status": "normal", "reason": "申诉成立"},
                        headers=_h(admin_tok))
    assert r.json()["ok"]
    with SessionLocal() as db:
        b = db.get(Book, bid)
        assert b.content_status == "normal"
        assert b.flagged == 0, "恢复后 flagged 未清零"
        opens = db.scalars(__import__("sqlalchemy").select(ContentFlag)
                           .where(ContentFlag.book_id == bid,
                                  ContentFlag.status == "open")).all()
    assert not opens, "恢复后仍有未结清的标记"


def test_hide_requires_reason(app_client, admin_tok, user_tok):
    """隐藏是敏感操作，无原因必须被拒。"""
    with SessionLocal() as db:
        owner = db.get(User, auth.parse_token(user_tok))
    bid = _mk_book(owner.id)
    r = app_client.post("/api/admin/books/content-status",
                        json={"book_id": bid, "status": "hidden", "reason": ""},
                        headers=_h(admin_tok))
    assert r.json()["ok"] is False


# ================================================================ ⑥ CSV 导出

def test_export_csv_has_bom_and_header(app_client, admin_tok):
    """★ CSV 必须带 BOM（否则 Excel 打开中文乱码）。"""
    r = app_client.get("/api/admin/orders/export", headers=_h(admin_tok))
    assert r.status_code == 200
    assert r.text.startswith("\ufeff"), "缺少 BOM，Excel 会乱码"
    assert "订单号" in r.text
    assert "订单号,用户ID" in r.text.replace("\ufeff", "")
    assert "attachment" in r.headers.get("content-disposition", "")


def test_csv_formula_injection_guarded():
    """★ CSV 公式注入防护：= + - @ 开头加 ' 前缀。

    为什么重要：订单备注可能被用户控制，Excel 打开会执行公式
    （DDE 攻击可执行任意命令）。这是真实的 CVE 级问题。
    """
    assert svc._csv("=cmd|'/c calc'!A1").startswith("'=")
    assert svc._csv("+1234").startswith("'+")
    assert svc._csv("-1234").startswith("'-")
    assert svc._csv("@SUM(A1)").startswith("'@")
    # 普通值不受影响
    assert svc._csv("普通文本") == "普通文本"
    # 含逗号要引号包裹
    assert svc._csv("a,b") == '"a,b"'
    # 含引号要转义
    assert svc._csv('a"b') == '"a""b"'


# ================================================================ ⑦ 系统健康

def test_health_endpoint_shape(app_client, admin_tok):
    """系统状态结构完整。"""
    r = app_client.get("/api/admin/health", headers=_h(admin_tok))
    assert r.status_code == 200
    d = r.json()
    for k in ("version", "db", "disk", "memory", "load", "time"):
        assert k in d, f"健康检查缺字段：{k}"
    assert d["db"]["ok"] is True


# ================================================================ ⑧ 服务层边界

def test_set_role_rejects_invalid(app_client, admin_tok):
    """非法角色必须拒绝。"""
    uid, _ = _mk_user()
    r = app_client.post("/api/admin/users/role",
                        json={"user_id": uid, "role": "superuser", "reason": "测试"},
                        headers=_h(admin_tok))
    assert r.json()["ok"] is False


def test_ban_nonexistent_user(app_client, admin_tok):
    """不存在的用户必须报错（不能静默成功）。"""
    r = app_client.post("/api/admin/users/ban",
                        json={"user_id": "u_not_exist_xxx", "active": False,
                              "reason": "测试不存在"},
                        headers=_h(admin_tok))
    assert r.json()["ok"] is False


def test_audit_log_append_only(app_client, admin_tok):
    """★ 审计日志是只追加的：没有修改/删除端点。"""
    from apps.api.main import app
    paths = [getattr(r, "path", "") for r in app.routes]
    for p in paths:
        if "/api/admin/audit" in p:
            assert not any(m in p for m in ("update", "delete", "patch")), \
                f"审计日志不应有修改端点：{p}"

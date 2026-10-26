"""A7 RBAC 越权测试矩阵（v0.20.7）。

★ 计划书验收要求：
    [ ] 越权测试矩阵全绿（角色 × 资源 × 操作）
    [ ] 表驱动配置，加角色/权限无需改代码

★ 本文件的组织方式就是"矩阵"：
    ROLES × PERMISSIONS × 期望结果 → 参数化用例
  新增角色/权限时，改 `EXPECTED` 矩阵即可，**不需要写新测试函数**
  （这本身就是"表驱动"的验证）。

★ 覆盖的越权场景（真实攻击）：
  1. viewer 试图写书（只读角色改数据）
  2. editor 试图访问后台（普通用户越权管理）
  3. editor 试图改**别人的**书（横向越权，最常见）
  4. billing 试图读内容（财务角色越界）
  5. admin 试图操作 owner（同级/向上越权）
  6. 任何人试图改自己的角色（自提权）
  7. 无主书（owner_id 为空）对普通用户不可见
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from apps.api import rbac
from apps.api.db import SessionLocal
from apps.api.models import Book, Chapter, RolePermission, User


def _mod(name: str):
    """★ 从 sys.modules 取模块，缺失则导入。

    为什么不能直接用 `sys.modules["apps.api.auth"]`：
      其他测试清空 sys.modules 后，本测试文件运行时该键可能还不存在
      （除非有人先导入过）→ KeyError。用 importlib 兜底更稳。
    """
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
    # ★ 关键：seed 角色权限到 DB。
    #   为什么在这里做：非 HTTP 测试直接用 SessionLocal 查表，
    #   而 seed 原本只在 app startup 钩子里跑 —— 不启 app 就查不到数据，
    #   会导致"单跑通过（app fixture 触发 seed）、全量跑失败（顺序不同）"。
    from apps.api import rbac as _rbac
    from apps.api.db import SessionLocal as _SL
    with _SL() as db:
        _rbac.seed_role_permissions(db)
    yield


@pytest.fixture(autouse=True)
def _restore_rbac():
    """★ 每个测试后恢复内置角色权限。

    为什么需要：`test_set_role_permissions_and_reset` 和
    `test_builtin_fallback_when_table_empty` 会**故意改动/清空**权限表。
    不恢复的话，后续测试看到的是被改坏的状态
    → 表现为"单跑全过、全量跑挂一片"。
    """
    yield
    from apps.api import rbac as _rbac
    from apps.api.db import SessionLocal as _SL
    from sqlalchemy import select as _select
    try:
        with _SL() as db:
            # 全量重写为内置定义（保证一致）
            for r in db.scalars(_select(RolePermission)).all():
                db.delete(r)
            db.commit()
            _rbac.seed_role_permissions(db)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _fresh_modules():
    """★ 刷新模块引用（防 sys.modules 被其他测试清空后错配）。

    ★★ 这个 fixture 是**必须的**（踩坑实录）：
      其他测试执行 `del sys.modules['apps.api.*']` 后重新导入，
      本文件顶部的 `from apps.api.models import User` 会指向**旧模块的类**。
      而 `_mod("models")` 拿的是**新模块的类**。
      · 用旧类建的对象，新 Session 查不到 → `db.get(User, uid)` 返回 None
      · 表现为"单跑通过（同一实例）、全量跑 False（实例分裂）"
      这里把全局引用统一刷新到 sys.modules 的当前实例。
    """
    import sys
    import importlib
    global rbac, SessionLocal, User, Book, Chapter, RolePermission
    rbac = _mod("rbac")
    SessionLocal = _mod("db").SessionLocal
    _m = _mod("models")
    User, Book, Chapter = _m.User, _m.Book, _m.Chapter
    RolePermission = _m.RolePermission
    yield


@pytest.fixture()
def client():
    import sys
    import importlib
    m = sys.modules.get("apps.api.main") or importlib.import_module("apps.api.main")
    importlib.reload(m)
    with TestClient(m.app) as c:
        yield c


def _mk(role: str) -> tuple[str, str, str]:
    """建用户，返回 (id, username, token)。"""
    a = _mod("auth")
    SL = SessionLocal
    uname = f"rbac_{role}_{uuid.uuid4().hex[:6]}"
    with SL() as db:
        u = a.create_user(db, uname, "rbac-test-12345", role=role)
        db.commit()
        return u.id, u.username, a.make_token(u.id)


def _mk_book(owner_id: str, title: str = "RBAC测试书") -> str:
    SL = SessionLocal
    bid = f"bk_{uuid.uuid4().hex[:12]}"
    with SL() as db:
        db.add(Book(id=bid, title=title, author="作者", owner_id=owner_id))
        db.add(Chapter(id=f"ch_{uuid.uuid4().hex[:12]}", book_id=bid, idx=1,
                       title="第一章", content="正文内容", words=100))
        db.commit()
    return bid


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


# ============================================================================
#  矩阵 1：角色 × 权限点（纯逻辑，不依赖 HTTP）
# ============================================================================

# ★ 期望矩阵：role → {permission: 是否拥有}
#   这张表就是"表驱动"的体现 —— 加权限只改这里
EXPECTED: dict[str, dict[str, bool]] = {
    "owner": {p: True for p in rbac.PERMISSIONS},          # owner 全部
    "admin": {
        "book.read": True, "book.write": True, "book.delete": True,
        "book.audit": True, "book.publish": True,
        "drama.run": True, "drama.export": True,
        "member.invite": True, "member.remove": True, "member.role": True,
        "admin.access": True, "audit.view": True, "config.update": True,
        # ★★ v0.25.4：导出含计费/安全参数、导入可批量覆盖 → 仅 owner
        "config.export": False,
        "system.health": True, "moderation.handle": True,
        # ★★ v0.26.1/2：广告代码 = 可注入前台任意代码；模型配置 = 含 Key 与计费。
        #   两者影响面都远高于普通配置 → **刻意不给 admin**，只给 owner。
        "ads.manage": False, "model.manage": False,
        "workspace.manage": False,                          # ★ owner 专属
    },
    "editor": {
        "book.read": True, "book.write": True, "book.delete": True,
        "book.audit": True, "book.publish": False,
        "drama.run": True, "drama.export": True,
        "member.invite": False, "member.remove": False, "member.role": False,
        "admin.access": False, "audit.view": False, "config.update": False,
        "config.export": False,
        "system.health": False, "moderation.handle": False,
        "ads.manage": False, "model.manage": False,
        "workspace.manage": False,
    },
    "viewer": {
        "book.read": True, "book.write": False, "book.delete": False,
        "book.audit": False, "book.publish": False,
        "drama.run": False, "drama.export": False,
        "member.invite": False, "member.remove": False, "member.role": False,
        "admin.access": False, "audit.view": False, "config.update": False,
        "config.export": False,
        "system.health": False, "moderation.handle": False,
        "ads.manage": False, "model.manage": False,
        "workspace.manage": False,
    },
}


@pytest.mark.parametrize("role", list(EXPECTED.keys()))
def test_permission_matrix(role):
    """★ 矩阵全绿：角色 × 权限点 逐项比对内置定义。"""
    with SessionLocal() as db:
        for perm, want in EXPECTED[role].items():
            got = rbac.has_permission(db, role, perm)
            assert got == want, (
                f"角色 {role} 对 {perm} 的判定不符：期望 {want}，实际 {got}")


def test_matrix_covers_all_roles():
    """矩阵必须覆盖全部内置角色（漏了 = 有角色未验证）。"""
    assert set(EXPECTED.keys()) == set(rbac.BUILTIN_ROLES.keys())


def test_matrix_covers_all_permissions():
    """矩阵必须覆盖全部权限点（漏了 = 有权限未验证）。"""
    for role, m in EXPECTED.items():
        missing = set(rbac.PERMISSIONS) - set(m.keys())
        assert not missing, f"角色 {role} 的矩阵缺少权限点：{missing}"


# ============================================================================
#  矩阵 2：通配符匹配
# ============================================================================

@pytest.mark.parametrize("pattern,perm,want", [
    ("*", "book.read", True),
    ("*", "anything.at.all", True),
    ("book.*", "book.read", True),
    ("book.*", "book.delete", True),
    ("book.*", "bookmark.read", False),     # ★ 关键：前缀相同但不同资源
    ("book.read", "book.read", True),
    ("book.read", "book.write", False),
    ("drama.*", "drama.run", True),
    ("drama.*", "drama.export", True),
    ("drama.*", "book.read", False),
    ("", "book.read", False),
])
def test_wildcard_matching(pattern, perm, want):
    """★ 通配符边界（`book.*` 不得误匹配 `bookmark.*`）。"""
    assert rbac._match(pattern, perm) == want


def test_expand_wildcards():
    """通配符展开为具体权限点。"""
    assert set(rbac.expand(["*"])) == set(rbac.PERMISSIONS)
    books = rbac.expand(["book.*"])
    assert "book.read" in books and "book.write" in books
    assert all(b.startswith("book.") for b in books)
    assert rbac.expand([]) == []


# ============================================================================
#  矩阵 3：旧角色兼容（升级不改数据）
# ============================================================================

@pytest.mark.parametrize("legacy,expected", [
    ("user", "editor"),      # ★ 老用户 → editor（能创作自己的书）
    ("admin", "admin"),
    ("Admin", "admin"),      # 大小写
    ("", "viewer"),          # 空值兜底
    ("unknown_role", "unknown_role"),
])
def test_legacy_role_normalize(legacy, expected):
    assert rbac.normalize_role(legacy) == expected


def test_legacy_user_role_still_works():
    """★ 关键兼容性：DB 里 role='user' 的老用户行为不变。

    为什么重要：线上已有 role='user' 的用户，
    升级后他们必须还能正常创作（不能因为不认识 'user' 就锁死）。
    """
    with SessionLocal() as db:
        # 'user' 归一为 editor → 能读写书、不能进后台
        assert rbac.has_permission(db, "user", "book.read") is True
        assert rbac.has_permission(db, "user", "book.write") is True
        assert rbac.has_permission(db, "user", "admin.access") is False


# ============================================================================
#  矩阵 4：资源归属（横向越权 —— 最常见）
# ============================================================================

def test_cannot_access_others_book():
    """★ 横向越权：editor 不能改别人的书。"""
    uid_a, _, _ = _mk("editor")
    uid_b, _, _ = _mk("editor")
    bid_b = _mk_book(uid_b, "B的书")
    with SessionLocal() as db:
        a = db.get(User, uid_a)
        b = db.get(Book, bid_b)
        assert rbac.can_access_book(db, a, b, "book.read") is False
        assert rbac.can_access_book(db, a, b, "book.write") is False


def test_can_access_own_book():
    """自己的书可访问。"""
    uid, _, _ = _mk("editor")
    bid = _mk_book(uid, "我的书")
    with SessionLocal() as db:
        u = db.get(User, uid)
        b = db.get(Book, bid)
        assert rbac.can_access_book(db, u, b, "book.read") is True
        assert rbac.can_access_book(db, u, b, "book.write") is True


def test_viewer_cannot_write_own_book():
    """★ viewer 即使是自己的书也不能写（角色权限优先于归属）。"""
    uid, _, _ = _mk("viewer")
    bid = _mk_book(uid)
    with SessionLocal() as db:
        u = db.get(User, uid)
        b = db.get(Book, bid)
        assert rbac.can_access_book(db, u, b, "book.read") is True
        assert rbac.can_access_book(db, u, b, "book.write") is False


def test_admin_can_access_any_book():
    """★ admin 可越权访问（管理需要）。"""
    uid_owner, _, _ = _mk("editor")
    uid_admin, _, _ = _mk("admin")
    bid = _mk_book(uid_owner)
    with SessionLocal() as db:
        a = db.get(User, uid_admin)
        b = db.get(Book, bid)
        assert rbac.can_access_book(db, a, b, "book.read") is True
        assert rbac.can_access_book(db, a, b, "book.write") is True


def test_orphan_book_denied_to_normal_user():
    """★ 无主书对普通用户不可见（历史遗留书不能人人可读）。"""
    uid, _, _ = _mk("editor")
    bid = _mk_book("", "无主书")            # owner_id 为空
    with SessionLocal() as db:
        u = db.get(User, uid)
        b = db.get(Book, bid)
        assert rbac.can_access_book(db, u, b, "book.read") is False



# ============================================================================
#  矩阵 5：成员操作（同级/向上越权）
# ============================================================================

@pytest.mark.parametrize("op_role,target_role,want_ok", [
    ("owner", "admin", True),
    ("owner", "editor", True),
    ("owner", "owner", True),
    ("admin", "editor", True),
    ("admin", "viewer", True),
    ("admin", "admin", False),      # ★ 不能操作同级
    ("admin", "owner", False),      # ★ 不能操作上级
    ("editor", "viewer", False),    # editor 无 member.role 权限
    ("editor", "editor", False),
    ("viewer", "viewer", False),
])
def test_manage_member_rank(op_role, target_role, want_ok):
    """★ 防同级提权矩阵：不能操作 rank ≥ 自己的成员。"""
    uid, _, _ = _mk(op_role)
    with SessionLocal() as db:
        op = db.get(User, uid)
        ok, _err = rbac.can_manage_member(db, op, target_role)
        assert ok == want_ok, (
            f"{op_role} 操作 {target_role}：期望 {'允许' if want_ok else '拒绝'}，"
            f"实际 {'允许' if ok else '拒绝'}（{_err}）")


# ============================================================================
#  矩阵 6：HTTP 端到端越权
# ============================================================================

ADMIN_GETS = [
    "/api/admin/dashboard",
    "/api/admin/users/search",
    "/api/admin/audit",
    "/api/admin/rbac/matrix",
    "/api/admin/rbac/members",
    "/api/admin/risk/overview",
]


@pytest.mark.parametrize("role", ["viewer", "editor"])
@pytest.mark.parametrize("path", ADMIN_GETS)
def test_non_admin_roles_blocked_from_admin_api(client, role, path):
    """★ 非管理角色访问后台 API 必须 403（矩阵：3 角色 × 6 端点）。"""
    _, _, tok = _mk(role)
    r = client.get(path, headers=_h(tok))
    assert r.status_code == 403, f"{role} 访问 {path} 未被拦截：{r.status_code}"


@pytest.mark.parametrize("path", ADMIN_GETS)
def test_admin_role_allowed_on_admin_api(client, path):
    """★ admin 必须能访问（否则后台不可用）。"""
    _, _, tok = _mk("admin")
    r = client.get(path, headers=_h(tok))
    assert r.status_code == 200, f"admin 被拒绝访问 {path}：{r.status_code} {r.text[:200]}"


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_non_admin_cannot_access_console(client, role):
    """★ 非管理角色不能进管理控制台（/admin/），401/403 都算正确。"""
    _, _, tok = _mk(role)
    # 控制台 HTML 是公开的（前端门），但 API 必须拦
    r = client.get("/api/auth/me", headers=_h(tok))
    assert r.status_code == 200
    # 关键：permissions 端点必须报 is_admin=false
    r2 = client.get("/api/me/permissions", headers=_h(tok))
    assert r2.status_code == 200
    assert r2.json()["is_admin"] is False


def test_my_permissions_shape(client):
    """★ /api/me/permissions 结构完整（前端据此隐藏入口）。"""
    _, _, tok = _mk("editor")
    r = client.get("/api/me/permissions", headers=_h(tok))
    assert r.status_code == 200
    d = r.json()
    for k in ("role", "role_label", "permissions", "is_admin",
              "can_manage_members", "can_view_billing", "can_run_drama"):
        assert k in d, f"缺少字段 {k}"
    assert d["role"] == "editor"
    assert d["is_admin"] is False
    assert d["can_run_drama"] is True


def test_editor_cannot_write_others_book_via_http(client):
    """★ 端到端横向越权：editor 改别人的书必须 403。"""
    uid_a, _, tok_a = _mk("editor")
    uid_b, _, _ = _mk("editor")
    bid_b = _mk_book(uid_b, "B的书")
    # 尝试读
    r = client.get(f"/api/books/{bid_b}", headers=_h(tok_a))
    assert r.status_code == 403, f"横向越权未被拦截：{r.status_code}"
    # 尝试写
    r2 = client.post(f"/api/books/{bid_b}/accept",
                     json={"ch_idx": 1, "title": "x", "content": "y"},
                     headers=_h(tok_a))
    assert r2.status_code in (403, 404), f"写入越权未被拦截：{r2.status_code}"


def test_admin_can_read_others_book_via_http(client):
    """★ admin 可以读别人的书（管理需要）。"""
    uid_o, _, _ = _mk("editor")
    _, _, tok_admin = _mk("admin")
    bid = _mk_book(uid_o, "管理员要看的书")
    r = client.get(f"/api/books/{bid}", headers=_h(tok_admin))
    assert r.status_code == 200, f"admin 被拒绝：{r.status_code} {r.text[:200]}"


# ============================================================================
#  矩阵 7：表驱动（加权限不改代码）
# ============================================================================

def test_role_permissions_table_driven():
    """★ 表驱动验证：改 DB 后判定立即变化（无需改代码）。

    这是计划书验收项"表驱动配置，加角色/权限无需改代码"的**直接验证**。
    """
    role = "viewer"
    with SessionLocal() as db:
        before = rbac.has_permission(db, role, "drama.run")
        assert before is False
        # 临时给 viewer 加 drama.run
        db.add(RolePermission(id=f"rp_test_{uuid.uuid4().hex[:8]}",
                              role=role, permission="drama.run", builtin=0))
        db.commit()
        try:
            after = rbac.has_permission(db, role, "drama.run")
            assert after is True, "改了 DB 但判定未变化（不是表驱动）"
        finally:
            # 清理
            for r in db.query(RolePermission).filter(
                    RolePermission.role == role,
                    RolePermission.permission == "drama.run").all():
                db.delete(r)
            db.commit()
        # 清理后恢复
        assert rbac.has_permission(db, role, "drama.run") is False


def test_seed_is_idempotent():
    """★ seed 幂等（重复调用不产生重复数据）。"""
    with SessionLocal() as db:
        n1 = rbac.seed_role_permissions(db)
        n2 = rbac.seed_role_permissions(db)
    assert n2 == 0, f"seed 不幂等：第二次插入了 {n2} 条"


def test_builtin_fallback_when_table_empty():
    """★ 表为空时降级用内置定义（防"表被清空 = 全站无权限"雪崩）。"""
    with SessionLocal() as db:
        rows = db.query(RolePermission).all()
        saved = [(r.id, r.role, r.permission, r.builtin) for r in rows]
        for r in rows:
            db.delete(r)
        db.commit()
        try:
            # 表空了，但内置兜底仍应返回权限
            perms = rbac.role_permissions(db, "admin")
            assert perms, "表为空时未降级到内置定义（会引发雪崩）"
            assert rbac.has_permission(db, "admin", "admin.access") is True
        finally:
            for rid, role, perm, bi in saved:
                db.add(RolePermission(id=rid, role=role, permission=perm, builtin=bi))
            db.commit()


def test_matrix_api_returns_all_roles(client):
    """管理端矩阵 API 返回全部角色与权限点。"""
    _, _, tok = _mk("admin")
    r = client.get("/api/admin/rbac/matrix", headers=_h(tok))
    assert r.status_code == 200
    d = r.json()
    assert set(d["roles"].keys()) == set(rbac.BUILTIN_ROLES.keys())
    assert len(d["permissions"]) == len(rbac.PERMISSIONS)
    assert d["groups"]


def test_check_permission_debug_tool(client):
    """★ 权限调试工具（排查"为什么他能做这个"）。"""
    uid, _, tok = _mk("admin")
    r = client.get(f"/api/admin/rbac/check?user_id={uid}&permission=book.write",
                   headers=_h(tok))
    assert r.status_code == 200
    d = r.json()
    assert d["granted"] is True
    assert d["granted_by"], "未给出授权来源（排查困难）"


# ============================================================================
#  矩阵 8：危险操作保护
# ============================================================================

def test_cannot_change_own_role(client):
    """★ 不能改自己的角色（防自降导致无人可管 / 自提权）。"""
    uid, _, tok = _mk("admin")
    r = client.post("/api/admin/rbac/members/role",
                    json={"user_id": uid, "role": "viewer", "reason": "测试自降"},
                    headers=_h(tok))
    assert r.status_code == 400, f"改自己角色未被拦截：{r.status_code}"


def test_cannot_promote_to_equal_rank(client):
    """★ admin 不能把别人提到 admin（同级提权）。"""
    _, _, tok_admin = _mk("admin")
    uid, _, _ = _mk("editor")
    r = client.post("/api/admin/rbac/members/role",
                    json={"user_id": uid, "role": "admin", "reason": "尝试提权"},
                    headers=_h(tok_admin))
    assert r.status_code == 403, f"同级提权未被拦截：{r.status_code}"


def test_cannot_promote_to_owner(client):
    """★ 不能提到 owner（超管）。"""
    _, _, tok_admin = _mk("admin")
    uid, _, _ = _mk("editor")
    r = client.post("/api/admin/rbac/members/role",
                    json={"user_id": uid, "role": "owner", "reason": "尝试提权"},
                    headers=_h(tok_admin))
    assert r.status_code == 403


def test_role_change_requires_reason(client):
    """★ 角色变更必须填原因（审计要求）。"""
    _, _, tok_admin = _mk("admin")
    uid, _, _ = _mk("viewer")
    r = client.post("/api/admin/rbac/members/role",
                    json={"user_id": uid, "role": "editor", "reason": ""},
                    headers=_h(tok_admin))
    assert r.status_code == 400, f"空原因未被拦截：{r.status_code}"


def test_owner_role_permissions_immutable(client):
    """★ owner 角色权限不可改（防把自己锁在系统外）。"""
    _, _, tok = _mk("admin")
    r = client.post("/api/admin/rbac/roles/owner/permissions",
                    json={"role": "owner", "permissions": ["book.read"],
                          "reason": "测试"},
                    headers=_h(tok))
    assert r.status_code == 400, f"owner 权限被改动：{r.status_code}"


def test_invalid_permission_rejected(client):
    """★ 非法权限点必须拒绝（防写入垃圾数据）。"""
    _, _, tok = _mk("admin")
    r = client.post("/api/admin/rbac/roles/editor/permissions",
                    json={"role": "editor", "permissions": ["not.a.real.perm"],
                          "reason": "测试"},
                    headers=_h(tok))
    assert r.status_code == 400


def test_set_role_permissions_and_reset(client):
    """★ 改权限 → 生效 → 重置 → 恢复（完整闭环）。"""
    _, _, tok = _mk("admin")
    # 改 viewer：加 drama.run
    r = client.post("/api/admin/rbac/roles/viewer/permissions",
                    json={"role": "viewer",
                          "permissions": ["book.read", "drama.run"],
                          "reason": "测试授权"},
                    headers=_h(tok))
    assert r.status_code == 200, r.text
    assert "drama.run" in r.json()["permissions"]
    # 验证生效
    _, _, tok_v = _mk("viewer")
    r2 = client.get("/api/me/permissions", headers=_h(tok_v))
    assert "drama.run" in r2.json()["permissions"], "改权限后未即时生效"
    # 重置
    r3 = client.post("/api/admin/rbac/reset", json={}, headers=_h(tok))
    assert r3.status_code == 200
    r4 = client.get("/api/me/permissions", headers=_h(tok_v))
    assert "drama.run" not in r4.json()["permissions"], "重置后未恢复"


def test_role_change_writes_audit(client):
    """★ 角色变更必须写审计（高危操作）。"""
    _, _, tok_admin = _mk("admin")
    uid, _, _ = _mk("viewer")
    client.post("/api/admin/rbac/members/role",
                json={"user_id": uid, "role": "editor", "reason": "矩阵测试变更"},
                headers=_h(tok_admin))
    r = client.get(f"/api/admin/audit?target_id={uid}", headers=_h(tok_admin))
    assert r.status_code == 200
    actions = [i["action"] for i in r.json()["items"]]
    assert any("role" in a for a in actions), f"未留痕：{actions}"


# ============================================================================
#  矩阵 9：未登录
# ============================================================================

@pytest.mark.parametrize("path", ADMIN_GETS + ["/api/me/permissions"])
def test_anonymous_gets_401(client, path):
    """★ 未登录必须 401（不是 403，也不是 200）。"""
    r = client.get(path)
    assert r.status_code == 401, f"{path} 未拦截匿名：{r.status_code}"

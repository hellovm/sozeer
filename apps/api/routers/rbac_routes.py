"""RBAC 团队与角色管理路由（v0.20.7 A7）。

★ 端点分组：
  用户端（/api/me/*）：查看自己的角色与权限
  管理端（/api/admin/rbac/*）：角色权限矩阵、成员管理、角色变更

★★ 三条安全红线：
  1. **不能操作 rank ≥ 自己的成员**（防同级提权/降权）
  2. **不能改自己的角色**（防 owner 自降导致无人可管）
  3. **内置角色不可删除**（与题材包同一约定）
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api import admin_service as svc
from apps.api import auth, rbac
from apps.api.db import get_db
from apps.api.models import RolePermission, User
from apps.api.permissions import require_perm

router = APIRouter()
log = logging.getLogger("novelforge")


def _ctx(request: Request) -> tuple[str, str]:
    ip = request.headers.get("cf-connecting-ip") or \
        request.headers.get("x-real-ip") or ""
    if not ip:
        xff = request.headers.get("x-forwarded-for", "")
        ip = xff.split(",")[0].strip() if xff else (
            request.client.host if request.client else "")
    return ip[:64], request.headers.get("user-agent", "")[:256]


# ================================================================ 用户端

@router.get("/api/me/permissions")
def my_permissions(db: Session = Depends(get_db),
                   user: User = Depends(auth.get_current_user)):
    """★ 我是谁、我能做什么（前端据此隐藏无权限的入口）。

    ★ 重要：前端隐藏只是**体验**，后端每个端点仍必须校验 ——
      用户可以直接调 API，不能依赖前端隐藏来保证安全。
    """
    role = getattr(user, "role", "")
    perms = rbac.expand(rbac.role_permissions(db, role))
    return {
        "role": rbac.normalize_role(role),
        "role_label": rbac.role_label(role),
        "role_raw": role,                     # 原始值（兼容老前端）
        "permissions": perms,
        "is_admin": rbac.has_permission(db, role, "admin.access"),
        "can_manage_members": rbac.has_permission(db, role, "member.role"),
        "can_view_billing": rbac.has_permission(db, role, "billing.view"),
        "can_run_drama": rbac.has_permission(db, role, "drama.run"),
    }


# ================================================================ 权限矩阵

@router.get("/api/admin/rbac/matrix")
def rbac_matrix(db: Session = Depends(get_db),
                _: User = Depends(require_perm("admin.access"))):
    """★ 角色 × 权限矩阵（表驱动，来自 DB）。

    为什么读 DB 而不是直接返回内置定义：后台要展示**实际生效**的配置。
    """
    m = rbac.matrix()
    # ★ 用 DB 实际数据覆盖内置定义（表驱动）
    try:
        rows = db.scalars(select(RolePermission)).all()
        by_role: dict[str, list[str]] = {}
        for r in rows:
            by_role.setdefault(r.role, []).append(r.permission)
        for role, pats in by_role.items():
            if role in m["roles"]:
                m["roles"][role]["patterns"] = sorted(pats)
                m["roles"][role]["permissions"] = rbac.expand(sorted(pats))
                m["roles"][role]["source"] = "db"
        for role in m["roles"]:
            m["roles"][role].setdefault("source", "builtin")
    except Exception as e:                          # noqa: BLE001
        log.warning("读取 DB 权限矩阵失败，返回内置定义：%s", e)
    return m


class PermSetIn(BaseModel):
    role: str
    permissions: list[str]
    reason: str = ""


@router.post("/api/admin/rbac/roles/{role}/permissions")
def set_role_permissions(role: str, body: PermSetIn, request: Request,
                         db: Session = Depends(get_db),
                         admin: User = Depends(require_perm("config.update"))):
    """★ 修改角色权限集（表驱动 → 即时生效，不用发版）。

    ★ 两条保护：
      1. **owner 角色不可改**（否则可能把自己锁死）
      2. 权限点必须合法（防写入垃圾数据导致规则失效）
    """
    role = (role or "").strip().lower()
    if role == "owner":
        raise HTTPException(400, "owner 角色权限不可修改（防止把自己锁在系统外）")
    if role not in rbac.BUILTIN_ROLES and role not in ("viewer", "editor"):
        raise HTTPException(400, f"未知角色：{role}")

    # 校验权限点合法性（允许通配符）
    valid_prefixes = {p.split(".")[0] for p in rbac.PERMISSIONS}
    for p in body.permissions:
        if p == "*":
            continue
        if p.endswith(".*"):
            if p[:-1].rstrip(".") not in valid_prefixes:
                raise HTTPException(400, f"非法权限前缀：{p}")
        elif p not in rbac.PERMISSIONS:
            raise HTTPException(400, f"未知权限点：{p}")

    ip, ua = _ctx(request)
    before = sorted(rbac.role_permissions(db, role))

    # 先删后插（全量替换，语义清晰）
    for r in db.scalars(select(RolePermission)
                        .where(RolePermission.role == role)).all():
        db.delete(r)
    db.commit()
    for p in sorted(set(body.permissions)):
        db.add(RolePermission(
            id=f"rp_{role}_{p.replace('.', '_').replace('*', 'all')}",
            role=role, permission=p, builtin=0, updated_by=admin.username))
    db.commit()

    svc.write_audit(db, operator=admin, action="rbac.set_permissions",
                    target_type="role", target_id=role,
                    reason=body.reason or "调整角色权限",
                    ip=ip, user_agent=ua,
                    detail={"before": before, "after": sorted(set(body.permissions))})
    return {"ok": True, "role": role,
            "permissions": sorted(set(body.permissions)),
            "expanded": rbac.expand(sorted(set(body.permissions)))}


@router.post("/api/admin/rbac/reset")
def reset_roles(request: Request, db: Session = Depends(get_db),
                admin: User = Depends(require_perm("config.update"))):
    """★ 恢复内置默认（改错了的救命按钮）。"""
    ip, ua = _ctx(request)
    try:
        rows = db.scalars(select(RolePermission)).all()
        for r in rows:
            db.delete(r)
        db.commit()
    except Exception as e:                          # noqa: BLE001
        raise HTTPException(500, f"重置失败：{e}")
    n = rbac.seed_role_permissions(db)
    svc.write_audit(db, operator=admin, action="rbac.reset",
                    target_type="role", target_id="*",
                    reason="恢复内置角色默认权限", ip=ip, user_agent=ua,
                    detail={"inserted": n})
    return {"ok": True, "inserted": n, "message": "已恢复内置默认权限"}


# ================================================================ 成员管理

@router.get("/api/admin/rbac/members")
def list_members(keyword: str = "", role: str = "",
                 limit: int = Query(50, ge=1, le=200), offset: int = 0,
                 db: Session = Depends(get_db),
                 admin: User = Depends(require_perm("member.role"))):
    """★ 成员列表（含角色，★ 手机号脱敏、不含作品正文）。"""
    d = svc.search_users(db, keyword=keyword, role="", limit=limit, offset=offset)
    # 归一角色标签 + 过滤
    items = []
    for it in d["items"]:
        it["role"] = rbac.normalize_role(it.get("role", ""))
        it["role_label"] = rbac.role_label(it["role"])
        if role and it["role"] != role:
            continue
        items.append(it)
    d["items"] = items
    d["total"] = len(items) if role else d["total"]
    d["roles"] = rbac.all_roles()
    return d


class MemberRoleIn(BaseModel):
    user_id: str
    role: str
    reason: str = ""


@router.post("/api/admin/rbac/members/role")
def set_member_role(body: MemberRoleIn, request: Request,
                    db: Session = Depends(get_db),
                    admin: User = Depends(require_perm("member.role"))):
    """★ 变更成员角色（三级校验）。

    为什么这么多校验：
      这是**提权操作**，做错的后果是"某个用户拿到了本来不该有的权限"。
    """
    target_role = (body.role or "").strip().lower()
    if target_role not in rbac.BUILTIN_ROLES:
        raise HTTPException(400, f"未知角色：{target_role}（可选：{'、'.join(rbac.BUILTIN_ROLES)}）")

    # ---- 校验 0：不能改自己的角色 ----
    # ★ 顺序很重要：这条要放在"rank 检查"**之前**。
    #   否则 admin 改自己会被 rank 规则先拦下（返回 403 "不能操作同级"），
    #   而实际原因是"不能改自己"（400，语义更准确，也更利于前端提示）。
    if body.user_id == getattr(admin, "id", ""):
        raise HTTPException(400, "不能修改自己的角色（请让其他管理员操作）")

    u = db.get(User, body.user_id)
    if u is None:
        raise HTTPException(404, "用户不存在")

    # ---- 校验 1：不能操作 rank ≥ 自己的成员 ----
    ok, err = rbac.can_manage_member(db, admin, getattr(u, "role", ""))
    if not ok:
        raise HTTPException(403, err)

    # ---- 校验 2：不能把别人提到 ≥ 自己的 rank ----
    ok2, err2 = rbac.can_manage_member(db, admin, target_role)
    if not ok2:
        raise HTTPException(403, f"不能把成员提升到与你相同或更高的角色（{err2}）")

    # ---- 校验 3：强制原因 ----
    if len((body.reason or "").strip()) < 2:
        raise HTTPException(400, "变更角色属敏感操作，必须填写原因")

    before = getattr(u, "role", "")
    u.role = target_role
    db.commit()

    ip, ua = _ctx(request)
    svc.write_audit(db, operator=admin, action="rbac.member_role",
                    target_type="user", target_id=u.id, reason=body.reason,
                    ip=ip, user_agent=ua,
                    detail={"username": u.username,
                            "before": {"role": before},
                            "after": {"role": target_role}})
    # ★ 角色变更影响权限，提示对方令牌可继续用（权限是实时的，不需重登）
    return {"ok": True, "user_id": u.id,
            "role": target_role, "role_label": rbac.role_label(target_role),
            "note": "权限即时生效（每次请求都实时判定，无需重新登录）"}


@router.get("/api/admin/rbac/check")
def check_permission(user_id: str, permission: str,
                     db: Session = Depends(get_db),
                     admin: User = Depends(require_perm("admin.access"))):
    """★ 权限调试工具（排查"为什么他能做这个"用）。"""
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(404, "用户不存在")
    role = getattr(u, "role", "")
    return {"user_id": u.id, "username": u.username,
            "role_raw": role, "role": rbac.normalize_role(role),
            "role_label": rbac.role_label(role),
            "permission": permission,
            "granted": rbac.has_permission(db, role, permission),
            "granted_by": [p for p in rbac.role_permissions(db, role)
                           if rbac._match(p, permission)]}

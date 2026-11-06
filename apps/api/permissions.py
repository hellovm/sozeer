"""权限检查的 FastAPI 依赖（v0.20.7 A7）。

★ 用法：
    @router.get("/api/books")
    def list_books(user: User = Depends(require_perm("book.read"))): ...

    @router.post("/api/admin/users/ban")
    def ban(user: User = Depends(require_perm("member.role"))): ...

★★ 为什么用依赖注入而不是中间件里判断路径：
  中间件用**路径前缀**判断权限（如 `/api/admin/` → 需要 admin），
  问题是一个路径可能对应不同权限（`/api/admin/orders` 是 billing.view，
  `/api/admin/config` 是 config.update），路径规则会越来越绕。
  依赖注入让**每个端点显式声明自己的权限要求**，一眼可读、不会漏。

★ 与中间件的关系：
  中间件负责"粗粒度"（未登录 401、/api/admin 必须 admin），
  依赖注入负责"细粒度"（这个端点具体要哪个权限点）——
  两层叠加是**纵深防御**（一层漏了另一层还有）。
"""
from __future__ import annotations

import logging
from typing import Callable

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from apps.api import auth, rbac
from apps.api.db import get_db
from apps.api.models import User

log = logging.getLogger("novelforge")


def require_perm(perm: str) -> Callable:
    """生成一个"要求某权限点"的 FastAPI 依赖。

    ★ 为什么用工厂函数而不是直接写 Depends：
      权限点是**参数** —— 工厂模式让每个端点用一行声明自己的要求。
    """
    async def _dep(request: Request, db: Session = Depends(get_db)) -> User:
        # 复用既有鉴权（含 is_active / deleted_at / token_valid_after 检查）
        u = await auth.get_current_user(request)
        role = getattr(u, "role", "")
        if not rbac.has_permission(db, role, perm):
            log.warning("越权拦截：user=%s role=%s 缺少权限 %s path=%s",
                        getattr(u, "username", "?"), role, perm, request.url.path)
            raise HTTPException(
                403, f"缺少权限：{perm}（当前角色：{rbac.role_label(role)}）")
        return u
    return _dep


def require_any_perm(*perms: str) -> Callable:
    """要求**任一**权限（OR 关系）。"""
    async def _dep(request: Request, db: Session = Depends(get_db)) -> User:
        u = await auth.get_current_user(request)
        role = getattr(u, "role", "")
        if not any(rbac.has_permission(db, role, p) for p in perms):
            raise HTTPException(
                403, f"缺少权限：{' 或 '.join(perms)}（当前角色：{rbac.role_label(role)}）")
        return u
    return _dep


def require_all_perm(*perms: str) -> Callable:
    """要求**全部**权限（AND 关系）。"""
    async def _dep(request: Request, db: Session = Depends(get_db)) -> User:
        u = await auth.get_current_user(request)
        role = getattr(u, "role", "")
        missing = [p for p in perms if not rbac.has_permission(db, role, p)]
        if missing:
            raise HTTPException(403, f"缺少权限：{'、'.join(missing)}")
        return u
    return _dep


# ================================================================ 与旧接口兼容

async def require_admin_compat(request: Request,
                               db: Session = Depends(get_db)) -> User:
    """★ 兼容层：保持 `role == "admin"` 语义，但走 RBAC 判断。

    为什么要它：现有代码有 `Depends(auth.require_admin)`，
    A7 不改这些调用点，而是让 require_admin 内部走 RBAC ——
    **行为不变，但规则集中到一处**（以后改规则只改 rbac.py）。
    """
    u = await auth.get_current_user(request)
    role = getattr(u, "role", "")
    # 旧语义：admin 才能进。新语义：有 admin.access 权限即可。
    if not rbac.has_permission(db, role, "admin.access"):
        raise HTTPException(403, "需要管理员权限")
    return u


# ================================================================ 便捷判断

def check_book_perm(db: Session, user: User, book, perm: str) -> bool:
    """资源级检查（书籍）。"""
    return rbac.can_access_book(db, user, book, perm)


def assert_book_perm(db: Session, user: User, book, perm: str) -> None:
    """资源级检查，不通过则抛 403。"""
    if not rbac.can_access_book(db, user, book, perm):
        raise HTTPException(403, f"无权对该作品执行此操作（需要 {perm}）")

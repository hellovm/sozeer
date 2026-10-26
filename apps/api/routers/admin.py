"""管理后台路由（v0.20.5 A5）。

★ 架构决策（计划书 §8.2）：**独立路由 /admin + 独立 API 前缀 /api/admin/***
  · 权限隔离清晰（用户端下载不到后台代码）
  · 加载更快、构建独立、可加 IP 白名单
  · ★ 但**前端隐藏只是体验，后端必须每个端点都校验 role**
      —— 全局中间件已对 /api/admin/* 做 role 检查（main.py），
        这里再叠加 Depends(auth.require_admin) 做**双保险**（防御性编程）。

★ 隐私红线：管理员**不可读用户正文**。
  本模块所有端点都**不返回章节内容**（只有元数据与统计量）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session
from urllib.parse import quote

from apps.api import admin_service as svc
from apps.api import auth
from apps.api import user_center as uc
from apps.api.db import get_db
from apps.api.models import User

router = APIRouter()
log = logging.getLogger("novelforge")


def _ctx(request: Request) -> tuple[str, str]:
    """提取请求上下文（IP / UA）用于审计。

    ★★ IP 一律走 ratelimit.client_ip（CF-Connecting-IP 优先，不可伪造）——
      绝不用 XFF 首段（客户端可伪造，可绕过限流/后台 IP 白名单，经典漏洞）。
      与 main.py 中间件、credits.py / pay.py 取 IP 口径统一（v1.0-E4.14 P0④）。
    """
    from apps.api import ratelimit as _rl
    ip = _rl.client_ip(request.headers) or (
        request.client.host if request.client else "")
    return ip, request.headers.get("user-agent", "")[:256]


# ================================================================ 看板

@router.get("/api/admin/dashboard")
def admin_dashboard(days: int = Query(14, ge=1, le=90),
                    db: Session = Depends(get_db),
                    _: User = Depends(auth.require_admin)):
    """总览看板（数据与数据库一致，全部实时聚合）。"""
    return svc.dashboard_overview(db, days=days)


@router.get("/api/admin/dashboard/trend")
def admin_dashboard_trend(days: int = Query(14, ge=1, le=90),
                          db: Session = Depends(get_db),
                          _: User = Depends(auth.require_admin)):
    """看板趋势数据（折线图用）。"""
    return svc.dashboard_trend(db, days=days)


@router.get("/api/admin/health")
def admin_health(db: Session = Depends(get_db),
                 _: User = Depends(auth.require_admin)):
    """系统状态（磁盘/内存/负载/DB 延迟）。"""
    return svc.system_health(db)


# ================================================================ 用户管理

@router.get("/api/admin/users/search")
def admin_search_users(keyword: str = "", role: str = "", status: str = "",
                       limit: int = Query(50, ge=1, le=200), offset: int = 0,
                       db: Session = Depends(get_db),
                       _: User = Depends(auth.require_admin)):
    """用户搜索（★ 手机号已脱敏，不返回作品正文）。"""
    return svc.search_users(db, keyword=keyword, role=role, status=status,
                            limit=limit, offset=offset)


class BanIn(BaseModel):
    user_id: str
    active: bool = False
    reason: str = ""


@router.post("/api/admin/users/ban")
def admin_ban_user(body: BanIn, request: Request, db: Session = Depends(get_db),
                   admin: User = Depends(auth.require_admin)):
    """★ 封禁 / 解封用户（敏感操作，强制原因 + 审计留痕）。"""
    ip, ua = _ctx(request)
    return svc.set_user_active(db, user_id=body.user_id, active=body.active,
                               operator=admin, reason=body.reason, ip=ip, user_agent=ua)


class RoleIn(BaseModel):
    user_id: str
    role: str
    reason: str = ""


@router.post("/api/admin/users/role")
def admin_set_role(body: RoleIn, request: Request, db: Session = Depends(get_db),
                   admin: User = Depends(auth.require_admin)):
    """★ 变更角色（提权是最高危操作）。"""
    ip, ua = _ctx(request)
    return svc.set_user_role(db, user_id=body.user_id, role=body.role,
                             operator=admin, reason=body.reason, ip=ip, user_agent=ua)


class BulkIn(BaseModel):
    action: str                       # ban | unban | set_role | adjust_credits
    user_ids: list[str] = []
    role: str = ""                    # set_role 用
    delta: int = 0                    # adjust_credits 用（正=增 负=减）
    reason: str = ""


@router.post("/api/admin/users/bulk")
def admin_bulk_users(body: BulkIn, request: Request, db: Session = Depends(get_db),
                     admin: User = Depends(auth.require_admin)):
    """★ 批量用户操作（封禁/解封/改角色/调积分）。每个子操作自带审计。"""
    ip, _ua = _ctx(request)
    return svc.bulk_user_action(db, action=body.action, user_ids=body.user_ids,
                                operator=admin, role=body.role, delta=body.delta,
                                reason=body.reason, ip=ip)


@router.get("/api/admin/users/{uid}/export")
def admin_export_user(uid: str, db: Session = Depends(get_db),
                      admin: User = Depends(auth.require_admin)):
    """★ 管理员导出指定用户的全部个人数据（ZIP，个保法合规协助）。"""
    u = db.get(User, uid)
    if u is None:
        raise HTTPException(404, "用户不存在")
    data, fname = uc.export_user_data(db, u)
    log.info("管理员 %s 导出用户 %s 数据（%d bytes）", admin.username, uid, len(data))
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition":
                             f"attachment; filename*=UTF-8''{quote(fname)}",
                             "X-Export-Size": str(len(data))})


# ================================================================ 审计日志

@router.get("/api/admin/audit")
def admin_audit_logs(action: str = "", operator_id: str = "", target_id: str = "",
                     result: str = "",
                     limit: int = Query(100, ge=1, le=500), offset: int = 0,
                     db: Session = Depends(get_db),
                     _: User = Depends(auth.require_admin)):
    """审计日志（★ 只读，不可修改 —— 日志可编辑就失去证明力）。"""
    return svc.list_audit_logs(db, action=action, operator_id=operator_id,
                               target_id=target_id, result=result,
                               limit=limit, offset=offset)


# ================================================================ 内容管理

@router.get("/api/admin/books")
def admin_list_books(keyword: str = "", content_status: str = "",
                     flagged_only: bool = False,
                     limit: int = Query(50, ge=1, le=200), offset: int = 0,
                     db: Session = Depends(get_db),
                     _: User = Depends(auth.require_admin)):
    """书籍列表（★ 元数据 + 统计量，**不含正文**）。"""
    return svc.list_books_admin(db, keyword=keyword, content_status=content_status,
                                flagged_only=flagged_only, limit=limit, offset=offset)


class FlagIn(BaseModel):
    book_id: str
    category: str = "other"
    severity: str = "warn"
    note: str = ""
    reason: str = ""


@router.post("/api/admin/books/flag")
def admin_flag_book(body: FlagIn, request: Request, db: Session = Depends(get_db),
                    admin: User = Depends(auth.require_admin)):
    """标记违规内容（★ 记录处置动作，不读取正文）。"""
    if body.category not in ("illegal", "porn", "violence", "plagiarism",
                             "spam", "other"):
        raise HTTPException(400, f"非法分类：{body.category}")
    if body.severity not in ("warn", "block"):
        raise HTTPException(400, f"非法级别：{body.severity}")
    ip, _ua = _ctx(request)
    return svc.flag_content(db, book_id=body.book_id, category=body.category,
                            severity=body.severity, note=body.note,
                            operator=admin, reason=body.reason, ip=ip)


class ContentStatusIn(BaseModel):
    book_id: str
    status: str          # normal | hidden
    reason: str = ""


@router.post("/api/admin/books/content-status")
def admin_set_content_status(body: ContentStatusIn, request: Request,
                             db: Session = Depends(get_db),
                             admin: User = Depends(auth.require_admin)):
    """★ 隐藏 / 恢复内容（敏感操作）。"""
    ip, ua = _ctx(request)
    return svc.set_content_status(db, book_id=body.book_id, status=body.status,
                                  operator=admin, reason=body.reason,
                                  ip=ip, user_agent=ua)


@router.get("/api/admin/flags")
def admin_list_flags(status: str = "", book_id: str = "",
                     limit: int = Query(50, ge=1, le=200),
                     db: Session = Depends(get_db),
                     _: User = Depends(auth.require_admin)):
    """内容标记列表。"""
    return svc.list_flags(db, status=status, book_id=book_id, limit=limit)


class FlagResolveIn(BaseModel):
    status: str                        # open | hidden | cleared | banned
    note: str = ""
    reason: str = ""


@router.patch("/api/admin/flags/{flag_id}")
def admin_resolve_flag(flag_id: str, body: FlagResolveIn, request: Request,
                       db: Session = Depends(get_db),
                       admin: User = Depends(auth.require_admin)):
    """★ 处置内容标记（审核队列核心动作：隐藏/澄清/封禁/重开）。"""
    ip, _ua = _ctx(request)
    r = svc.update_flag_status(db, flag_id=flag_id, status=body.status,
                               note=body.note, operator=admin, reason=body.reason,
                               ip=ip)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "处置失败"))
    return r



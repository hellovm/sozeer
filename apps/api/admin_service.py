"""管理后台服务层（v0.20.5 A5）。

★ 本模块承载"管理员能做的所有危险操作"的**统一入口**：
  每一个会改变用户资产/状态的动作，都必须经过这里，以便：
    ① 强制写审计日志（append-only）
    ② 强制记录操作原因（敏感操作无原因 = 不可接受）
    ③ 集中做权限与边界校验（防"某个端点忘了检查"）

★★★ 三条红线（来自计划书 §8.2）：
  1. **管理员不可读用户正文** —— 本模块所有函数都不返回章节内容
  2. **敏感操作必须二次确认** —— 通过 `confirm_token` 机制（见 confirm.py 设计）
  3. **所有操作留痕** —— 包括**失败的尝试**（失败可能是攻击）

★ 实现原则：**能返回结果就不要抛异常**。
  审计日志必须在**操作失败时也能写入** —— 所以用显式 commit 而非依赖事务回滚。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import uuid

import os
import shutil

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from apps.api.config import BOOKS_DIR
from apps.api.models import (
    AccountDeletion, AdminAuditLog, Book, BookScene, Chapter, Character,
    CharacterKnowledge, CharacterRelation, ChapterEmotion, ContentFlag,
    Conversation, CreditAccount, CreditLedger, DramaAsset, DramaEpisode,
    DramaProject, DramaScene, DramaTask, Foreshadow, Invoice, LedgerEntry,
    LLMCall, LoginRecord, MediaCall, MemoryChunk, Message, Notification,
    NotificationPref, Order, PaymentEvent, AuditReport, RefreshToken, Run,
    StyleProfile, Task, TimelineEvent, User, Volume, WorldSetting, UserFeedback,
    now,
)

log = logging.getLogger("novelforge")


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _naive_now() -> dt.datetime:
    """★ 统一 UTC naive（与 models.now() 的存储格式一致）。

    为什么要专门一个函数：SQLite 存的是 naive，PostgreSQL 的 TIMESTAMP
    也是 naive。如果这里用 `datetime.now()`（本地时区）会与
    `models.now()`（UTC）产生 8 小时偏差 —— v0.20.4 实测踩过这个坑。
    """
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


# ================================================================ 审计日志

# ★ 需要"强制原因"的操作（无原因直接拒绝）
SENSITIVE_ACTIONS = {
    "user.ban", "user.unban", "user.role_change", "user.delete",
    "credit.adjust", "credit.freeze", "credit.unfreeze",
    "order.refund", "order.mark_paid",
    "book.hide", "book.unhide", "book.flag", "book.unflag",
    "config.update", "config.rollback",
}


def write_audit(db: Session, *, operator: User | None, action: str,
                target_type: str = "", target_id: str = "",
                detail: dict | str = "", reason: str = "",
                ip: str = "", user_agent: str = "",
                result: str = "ok") -> str:
    """★ 写审计日志（唯一入口，所有管理操作必须调用）。

    ★ 为什么不抛异常：审计失败不应该阻断业务，但也绝不能静默。
      这里 catch 所有异常并 log.error —— 保证"操作成功但日志失败"
      这种情况至少能在系统日志里看到（可补救）。
    """
    aid = _uid("aud")
    try:
        d = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        db.add(AdminAuditLog(
            id=aid,
            operator_id=getattr(operator, "id", "") or "",
            operator_name=getattr(operator, "username", "") or "",
            action=action, target_type=target_type, target_id=target_id,
            detail=d[:8192], reason=(reason or "")[:1024],
            ip=ip[:64], user_agent=(user_agent or "")[:256],
            result=result, created_at=_naive_now()))
        db.commit()
    except Exception as e:                          # noqa: BLE001
        # ★ 审计写不进去是**严重**问题（合规风险），必须 error 级别
        log.error("★ 审计日志写入失败 action=%s target=%s err=%s: %s",
                  action, target_id, type(e).__name__, e)
        try:
            db.rollback()
        except Exception:
            pass
    return aid


def require_reason(action: str, reason: str) -> str | None:
    """敏感操作的原因校验。返回 None 表示通过，否则返回错误消息。"""
    if action in SENSITIVE_ACTIONS and len((reason or "").strip()) < 2:
        return f"操作 {action} 属于敏感操作，必须填写原因（至少 2 个字符）"
    return None


def list_audit_logs(db: Session, *, action: str = "", operator_id: str = "",
                    target_id: str = "", result: str = "",
                    limit: int = 100, offset: int = 0) -> dict:
    """查询审计日志（倒序）。"""
    q = select(AdminAuditLog)
    if action:
        q = q.where(AdminAuditLog.action.like(f"{action}%"))
    if operator_id:
        q = q.where(AdminAuditLog.operator_id == operator_id)
    if target_id:
        q = q.where(AdminAuditLog.target_id == target_id)
    if result:
        q = q.where(AdminAuditLog.result == result)

    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(AdminAuditLog.created_at.desc())
                      .limit(min(limit, 500)).offset(max(offset, 0))).all()
    return {"total": total, "items": [
        {"id": r.id, "operator_id": r.operator_id, "operator_name": r.operator_name,
         "action": r.action, "target_type": r.target_type, "target_id": r.target_id,
         "detail": r.detail, "reason": r.reason, "ip": r.ip,
         "result": r.result,
         "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else ""}
        for r in rows]}


# ================================================================ 数据看板

def dashboard_overview(db: Session, *, days: int = 14) -> dict:
    """总览看板：用户 / 营收 / 生成量 / 健康度。

    ★ 性能注意：全部用聚合查询，不做 N+1。生产库上这组 SQL 应在 100ms 内。
    ★ 时区：所有时间比较用 naive UTC（与存储一致）。
    """
    since = _naive_now() - dt.timedelta(days=max(1, min(days, 90)))

    # ---------------- 用户 ----------------
    total_users = db.scalar(select(func.count()).select_from(User)) or 0
    active_users = db.scalar(select(func.count()).select_from(User)
                             .where(User.is_active == 1)) or 0
    new_users = db.scalar(select(func.count()).select_from(User)
                          .where(User.created_at >= since)) or 0
    deleted_users = db.scalar(select(func.count()).select_from(User)
                              .where(User.deleted_at.is_not(None))) or 0

    # ---------------- 订单 / 营收 ----------------
    paid_q = select(func.count(), func.coalesce(func.sum(Order.amount_cents), 0)) \
        .where(Order.status == "paid")
    n_paid_all, revenue_all = db.execute(paid_q).one()
    n_paid_recent, revenue_recent = db.execute(
        select(func.count(), func.coalesce(func.sum(Order.amount_cents), 0))
        .where(Order.status == "paid", Order.paid_at >= since)).one()

    pending_orders = db.scalar(select(func.count()).select_from(Order)
                               .where(Order.status == "pending")) or 0
    refunded = db.scalar(select(func.count()).select_from(Order)
                         .where(Order.status == "refunded")) or 0
    refunded_cents = db.scalar(
        select(func.coalesce(func.sum(Order.amount_cents), 0))
        .where(Order.status == "refunded")) or 0

    # ---------------- 内容 / 生成 ----------------
    total_books = db.scalar(select(func.count()).select_from(Book)) or 0
    total_chapters = db.scalar(select(func.count()).select_from(Chapter)) or 0
    total_words = db.scalar(select(func.coalesce(func.sum(Chapter.words), 0))) or 0
    new_books = db.scalar(select(func.count()).select_from(Book)
                          .where(Book.created_at >= since)) or 0

    # ---------------- 任务健康 ----------------
    # ★ 注意字段名：Task 用的是 started_at（不是 created_at），
    #   状态值是 running|done|error|abandoned（不是 failed）。
    #   想当然写错会导致整个看板 500 —— 这是实测踩过的坑。
    running_tasks = db.scalar(select(func.count()).select_from(Task)
                              .where(Task.status == "running")) or 0
    failed_tasks = db.scalar(select(func.count()).select_from(Task)
                             .where(Task.status.in_(("error", "abandoned")),
                                    Task.started_at >= since)) or 0

    # ---------------- 内容治理 ----------------
    open_flags = db.scalar(select(func.count()).select_from(ContentFlag)
                           .where(ContentFlag.status == "open")) or 0
    hidden_books = db.scalar(select(func.count()).select_from(Book)
                             .where(Book.content_status == "hidden")) or 0

    # ---------------- 积分总量（负债口径）----------------
    credit_total = db.scalar(select(func.coalesce(func.sum(CreditAccount.balance), 0))) or 0
    credit_frozen = db.scalar(select(func.coalesce(func.sum(CreditAccount.frozen), 0))) or 0

    return {
        "generated_at": _naive_now().strftime("%Y-%m-%d %H:%M:%S"),
        "window_days": days,
        "users": {"total": int(total_users), "active": int(active_users),
                  "new_in_window": int(new_users), "deleted": int(deleted_users)},
        "revenue": {
            "paid_orders": int(n_paid_all),
            "total_cents": int(revenue_all), "total_yuan": round(int(revenue_all) / 100, 2),
            "window_orders": int(n_paid_recent),
            "window_cents": int(revenue_recent),
            "window_yuan": round(int(revenue_recent) / 100, 2),
            "pending_orders": int(pending_orders),
            "refunded_orders": int(refunded),
            "refunded_cents": int(refunded_cents),
            "refunded_yuan": round(int(refunded_cents) / 100, 2),
            "net_cents": int(int(revenue_all) - int(refunded_cents)),
            "net_yuan": round((int(revenue_all) - int(refunded_cents)) / 100, 2),
        },
        "content": {"books": int(total_books), "new_books_in_window": int(new_books),
                    "chapters": int(total_chapters), "words": int(total_words)},
        "tasks": {"running": int(running_tasks), "failed_in_window": int(failed_tasks)},
        "moderation": {"open_flags": int(open_flags), "hidden_books": int(hidden_books)},
        "credits": {"outstanding": int(credit_total), "frozen": int(credit_frozen)},
    }


def dashboard_trend(db: Session, *, days: int = 14) -> dict:
    """趋势数据（按天聚合），前端画折线图用。

    ★ 为什么在 Python 里按天归并而不是 SQL GROUP BY date()：
      两种数据库的日期函数不兼容（SQLite 用 strftime，PG 用 date_trunc）。
      本项目要同时支持两者，所以在 Python 里归并 —— 数据量在合理范围内
      （看板最多 90 天，每天几行聚合结果）。
    """
    days = max(1, min(days, 90))
    today = _naive_now().date()
    start = today - dt.timedelta(days=days - 1)

    def _days_map():
        out = {}
        for i in range(days):
            out[(start + dt.timedelta(days=i)).isoformat()] = 0
        return out

    users_map, orders_map, revenue_map, books_map = _days_map(), _days_map(), _days_map(), _days_map()

    for created in db.scalars(select(User.created_at).where(User.created_at >= start)).all():
        if created:
            k = created.date().isoformat()
            if k in users_map:
                users_map[k] += 1

    for o in db.scalars(select(Order).where(Order.status == "paid",
                                            Order.paid_at >= start)).all():
        if o.paid_at:
            k = o.paid_at.date().isoformat()
            if k in orders_map:
                orders_map[k] += 1
                revenue_map[k] += int(o.amount_cents or 0)

    for created in db.scalars(select(Book.created_at).where(Book.created_at >= start)).all():
        if created:
            k = created.date().isoformat()
            if k in books_map:
                books_map[k] += 1

    labels = sorted(users_map.keys())
    return {
        "labels": labels,
        "new_users": [users_map[k] for k in labels],
        "new_books": [books_map[k] for k in labels],
        "paid_orders": [orders_map[k] for k in labels],
        "revenue_cents": [revenue_map[k] for k in labels],
        "revenue_yuan": [round(revenue_map[k] / 100, 2) for k in labels],
    }


# ================================================================ 用户管理

def search_users(db: Session, *, keyword: str = "", role: str = "",
                 status: str = "", limit: int = 50, offset: int = 0) -> dict:
    """用户搜索（★ 不返回任何作品正文，只返回统计量）。"""
    q = select(User)
    if keyword:
        kw = f"%{keyword.strip()}%"
        q = q.where((User.username.like(kw)) | (User.email.like(kw))
                    | (User.nickname.like(kw)) | (User.phone.like(kw)))
    if role:
        q = q.where(User.role == role)
    if status == "active":
        q = q.where(User.is_active == 1, User.deleted_at.is_(None))
    elif status == "banned":
        q = q.where(User.is_active == 0)
    elif status == "deleted":
        q = q.where(User.deleted_at.is_not(None))

    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(User.created_at.desc())
                      .limit(min(limit, 200)).offset(max(offset, 0))).all()

    # 批量取积分余额（避免 N+1）
    uids = [u.id for u in rows]
    bal = {}
    if uids:
        for uid_, b, f in db.execute(
                select(CreditAccount.user_id, CreditAccount.balance, CreditAccount.frozen)
                .where(CreditAccount.user_id.in_(uids))).all():
            bal[uid_] = (int(b or 0), int(f or 0))

    # 批量取作品数
    book_cnt = {}
    if uids:
        for uid_, n in db.execute(
                select(Book.owner_id, func.count(Book.id))
                .where(Book.owner_id.in_(uids)).group_by(Book.owner_id)).all():
            book_cnt[uid_] = int(n or 0)

    return {"total": int(total), "items": [
        {"id": u.id, "username": u.username, "nickname": u.nickname or "",
         "email": u.email or "", "phone": _mask(u.phone or ""),
         "role": u.role, "is_active": int(u.is_active or 0),
         "deleted": u.deleted_at is not None,
         "credits": bal.get(u.id, (0, 0))[0], "credits_frozen": bal.get(u.id, (0, 0))[1],
         "books": book_cnt.get(u.id, 0),
         "totp_enabled": int(getattr(u, "totp_enabled", 0) or 0),
         "last_login_at": _fmt(u.last_login_at),
         "created_at": _fmt(u.created_at)}
        for u in rows]}


def _mask(s: str) -> str:
    """手机号脱敏（后台展示也不能给全量 —— 管理员不需要看到完整手机号）。"""
    if not s or len(s) < 7:
        return s
    return s[:3] + "****" + s[-4:]


def _fmt(t) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S") if t else ""


def set_user_active(db: Session, *, user_id: str, active: bool,
                    operator: User, reason: str = "",
                    ip: str = "", user_agent: str = "") -> dict:
    """封禁 / 解封用户。★ 敏感操作，强制原因 + 审计。"""
    err = require_reason("user.unban" if active else "user.ban", reason)
    if err:
        write_audit(db, operator=operator, action="user.unban" if active else "user.ban",
                    target_type="user", target_id=user_id, reason=reason,
                    ip=ip, user_agent=user_agent, result="rejected",
                    detail={"error": err})
        return {"ok": False, "error": err}

    u = db.get(User, user_id)
    if u is None:
        write_audit(db, operator=operator, action="user.ban" if not active else "user.unban",
                    target_type="user", target_id=user_id, reason=reason,
                    ip=ip, result="error", detail={"error": "用户不存在"})
        return {"ok": False, "error": "用户不存在"}
    if u.id == getattr(operator, "id", ""):
        return {"ok": False, "error": "不能封禁自己"}

    before = int(u.is_active or 0)
    u.is_active = 1 if active else 0
    db.commit()
    write_audit(db, operator=operator,
                action="user.unban" if active else "user.ban",
                target_type="user", target_id=user_id, reason=reason,
                ip=ip, user_agent=user_agent,
                detail={"before": {"is_active": before},
                        "after": {"is_active": int(u.is_active)},
                        "username": u.username})
    # ★ 封禁后其令牌立即失效（中间件会因 is_active=0 拒绝）
    return {"ok": True, "user_id": u.id, "is_active": int(u.is_active),
            "note": "封禁后该用户已签发的令牌立即失效"}


def set_user_role(db: Session, *, user_id: str, role: str, operator: User,
                  reason: str = "", ip: str = "", user_agent: str = "") -> dict:
    """变更用户角色（★ 提权是最高危操作，强制原因 + 审计）。

    ★ v0.20.7 A7：支持全部 RBAC 角色（owner/admin/editor/viewer/billing），
      同时保留旧值 user/admin 的兼容（`user` 归一为 editor）。
    """
    # 延迟导入避免循环
    from apps.api import rbac as _rbac

    role = (role or "").strip().lower()
    if role not in _rbac.BUILTIN_ROLES:
        return {"ok": False,
                "error": f"非法角色：{role}（可选：{'、'.join(_rbac.BUILTIN_ROLES)}）"}
    err = require_reason("user.role_change", reason)
    if err:
        write_audit(db, operator=operator, action="user.role_change",
                    target_type="user", target_id=user_id, reason=reason,
                    ip=ip, result="rejected", detail={"error": err})
        return {"ok": False, "error": err}
    u = db.get(User, user_id)
    if u is None:
        return {"ok": False, "error": "用户不存在"}
    if u.id == getattr(operator, "id", "") and role != "admin":
        return {"ok": False, "error": "不能撤销自己的管理员权限"}

    # ★ A7：不能操作 rank ≥ 自己的成员（防同级提权）
    ok, rerr = _rbac.can_manage_member(db, operator, getattr(u, "role", ""))
    if not ok:
        write_audit(db, operator=operator, action="user.role_change",
                    target_type="user", target_id=user_id, reason=reason,
                    ip=ip, result="rejected", detail={"error": rerr})
        return {"ok": False, "error": rerr}
    ok2, rerr2 = _rbac.can_manage_member(db, operator, role)
    if not ok2:
        write_audit(db, operator=operator, action="user.role_change",
                    target_type="user", target_id=user_id, reason=reason,
                    ip=ip, result="rejected", detail={"error": rerr2})
        return {"ok": False, "error": rerr2}

    before = u.role
    u.role = role
    db.commit()
    write_audit(db, operator=operator, action="user.role_change",
                target_type="user", target_id=user_id, reason=reason,
                ip=ip, user_agent=user_agent,
                detail={"before": {"role": before}, "after": {"role": role},
                        "username": u.username})
    return {"ok": True, "user_id": u.id, "role": u.role}


# ================================================================ 内容治理

def flag_content(db: Session, *, book_id: str, category: str, severity: str = "warn",
                 note: str = "", operator: User, reason: str = "",
                 ip: str = "") -> dict:
    """标记违规内容。

    ★ 注意：本函数**不读取、不返回章节正文**（隐私红线）。
      它只记录"这本书被标记了"，具体判断由人工在举报材料上做。
    """
    b = db.get(Book, book_id)
    if b is None:
        return {"ok": False, "error": "书籍不存在"}
    fid = _uid("flag")
    db.add(ContentFlag(id=fid, book_id=book_id, category=category,
                       severity=severity, note=note[:2048], status="open",
                       source="admin", created_at=_naive_now()))
    b.flagged = 1
    db.commit()
    write_audit(db, operator=operator, action="book.flag", target_type="book",
                target_id=book_id, reason=reason or note,
                ip=ip, detail={"category": category, "severity": severity,
                               "flag_id": fid, "title": b.title})
    return {"ok": True, "flag_id": fid, "book_id": book_id}


def set_content_status(db: Session, *, book_id: str, status: str, operator: User,
                       reason: str = "", ip: str = "", user_agent: str = "") -> dict:
    """隐藏 / 恢复正常内容。★ 敏感操作。"""
    if status not in ("normal", "hidden"):
        return {"ok": False, "error": f"非法状态：{status}"}
    err = require_reason("book.unhide" if status == "normal" else "book.hide", reason)
    if err:
        write_audit(db, operator=operator,
                    action="book.unhide" if status == "normal" else "book.hide",
                    target_type="book", target_id=book_id, reason=reason,
                    ip=ip, result="rejected", detail={"error": err})
        return {"ok": False, "error": err}
    b = db.get(Book, book_id)
    if b is None:
        return {"ok": False, "error": "书籍不存在"}

    before = (b.content_status or "normal")
    b.content_status = status
    if status == "normal":
        b.flagged = 0
        # 同时把 open 状态的标记结清
        for f in db.scalars(select(ContentFlag)
                            .where(ContentFlag.book_id == book_id,
                                   ContentFlag.status == "open")).all():
            f.status = "cleared"
            f.handled_by = getattr(operator, "username", "")
            f.handled_at = _naive_now()
    db.commit()
    write_audit(db, operator=operator,
                action="book.unhide" if status == "normal" else "book.hide",
                target_type="book", target_id=book_id, reason=reason,
                ip=ip, user_agent=user_agent,
                detail={"before": {"content_status": before},
                        "after": {"content_status": status}, "title": b.title})
    return {"ok": True, "book_id": book_id, "content_status": status}


def list_flags(db: Session, *, status: str = "", book_id: str = "",
               limit: int = 50) -> dict:
    """标记列表（★ 只含处置元数据，不含正文）。"""
    q = select(ContentFlag)
    if status:
        q = q.where(ContentFlag.status == status)
    if book_id:
        q = q.where(ContentFlag.book_id == book_id)
    rows = db.scalars(q.order_by(ContentFlag.created_at.desc())
                      .limit(min(limit, 200))).all()
    # 关联书名（批量）
    bids = list({r.book_id for r in rows})
    titles = {}
    if bids:
        for bid, t in db.execute(select(Book.id, Book.title)
                                 .where(Book.id.in_(bids))).all():
            titles[bid] = t
    return {"items": [
        {"id": r.id, "book_id": r.book_id, "book_title": titles.get(r.book_id, ""),
         "category": r.category, "severity": r.severity, "note": r.note,
         "status": r.status, "source": r.source, "handled_by": r.handled_by,
         "handled_at": _fmt(r.handled_at), "created_at": _fmt(r.created_at)}
        for r in rows]}


# ================================================================ 标记处置（WS1）

def update_flag_status(db: Session, *, flag_id: str, status: str, note: str = "",
                       operator: User, reason: str = "", ip: str = "") -> dict:
    """★ 处置内容标记（审核队列的核心动作）。

    状态机：open → hidden（隐藏）| cleared（澄清）| banned（封禁作者）| open（重开）
    · 副作用：cleared/open 会同步恢复书籍 content_status；hidden/banned 会隐藏。
    · banned 还会封禁书籍作者（复用 set_user_active，自带审计）。
    """
    VALID = ("open", "hidden", "cleared", "banned")
    if status not in VALID:
        return {"ok": False, "error": f"非法状态：{status}（可选：{', '.join(VALID)}）"}
    f = db.get(ContentFlag, flag_id)
    if f is None:
        return {"ok": False, "error": "标记不存在"}
    old = f.status
    if old == status and not note:
        return {"ok": True, "flag_id": flag_id, "status": status, "unchanged": True}

    book = db.get(Book, f.book_id)
    if status == "hidden":
        if book:
            book.flagged = 1
            book.content_status = "hidden"
    elif status == "cleared":
        if book:
            book.flagged = 0
            book.content_status = "normal"
    elif status == "banned":
        if book and book.owner_id:
            set_user_active(db, user_id=book.owner_id, active=False, operator=operator,
                            reason=reason or f"内容标记 {flag_id} 处置：封禁作者",
                            ip=ip)
            if book:
                book.flagged = 1
                book.content_status = "hidden"

    f.status = status
    f.handled_by = getattr(operator, "username", "")
    f.handled_at = _naive_now()
    if note:
        f.note = (f.note + "\n" if f.note else "") + note[:2048]
    db.commit()
    write_audit(db, operator=operator, action="book.flag.resolve",
                target_type="flag", target_id=flag_id, reason=reason, ip=ip,
                detail={"book_id": f.book_id, "category": f.category,
                        "from": old, "to": status})
    return {"ok": True, "flag_id": flag_id, "status": status, "book_id": f.book_id}


# ================================================================ 用户级危险操作（WS2）

# ★ 级联范围（与 tools/purge_nonadmin.py 保持一致；全库无外键，必须应用层手工级联）。
#   ★★ 新增子表时**两处都要更新**（见 purge_nonadmin._book_children / _user_tables）。
_CASCADE_BOOK_CHILDREN = [
    (Chapter, "book_id"), (Volume, "book_id"), (Character, "book_id"),
    (WorldSetting, "book_id"), (Foreshadow, "book_id"), (TimelineEvent, "book_id"),
    (LedgerEntry, "book_id"), (AuditReport, "book_id"), (StyleProfile, "book_id"),
    (Run, "book_id"), (Conversation, "book_id"), (Message, "book_id"),
    (MemoryChunk, "book_id"), (DramaTask, "book_id"), (MediaCall, "book_id"),
    (CharacterRelation, "book_id"), (ChapterEmotion, "book_id"),
    (CharacterKnowledge, "book_id"), (BookScene, "book_id"),
]
# 直接按 user_id 归属、随用户删除的表（**不含**会计凭证 orders/invoices）
_CASCADE_USER_TABLES = [
    (CreditAccount, "user_id"), (CreditLedger, "user_id"),
    (LoginRecord, "user_id"), (AccountDeletion, "user_id"),
    (RefreshToken, "user_id"), (LLMCall, "user_id"), (UserFeedback, "user_id"),
    (Notification, "user_id"), (NotificationPref, "user_id"),
]


def _purge_drama_project(db: Session, pid: str) -> None:
    """删除单个短剧项目及其全部子表（复用 purge 范围）。"""
    db.execute(delete(DramaEpisode).where(DramaEpisode.project_id == pid))
    db.execute(delete(DramaAsset).where(DramaAsset.project_id == pid))
    db.execute(delete(DramaScene).where(DramaScene.project_id == pid))
    db.execute(delete(DramaTask).where(DramaTask.project_id == pid))
    dp = db.get(DramaProject, pid)
    if dp is not None:
        db.delete(dp)


def delete_user_cascade(db: Session, *, user_id: str, operator: User,
                        reason: str = "", hard_delete_vouchers: bool = False,
                        ip: str = "") -> dict:
    """★ 管理员强制删除用户（级联硬删全部归属数据）。

    ★ 与个保法注销（execute_deletion 保留订单/匿名保留作品）不同，
      这是**管理员主动清理**：作品、短剧、任务、个人资料全部删除。

    ★ 会计凭证（orders / invoices / payment_events）默认**保留并脱敏 user_id**
      （合规红线：会计凭证需留存）；仅当 `hard_delete_vouchers=True` 才真删（高危，需原因）。
    """
    u = db.get(User, user_id)
    if u is None:
        return {"ok": False, "error": "用户不存在"}
    if u.id == getattr(operator, "id", ""):
        return {"ok": False, "error": "不能删除自己"}
    if str(u.role) in ("admin", "owner"):
        return {"ok": False, "error": "不能删除管理员账号"}

    stats = {"books": 0, "drama_projects": 0, "tasks": 0}

    # 1) 书籍 + 子表 + 磁盘目录
    books = db.scalars(select(Book).where(Book.owner_id == user_id)).all()
    for b in books:
        for cls, col in _CASCADE_BOOK_CHILDREN:
            db.execute(delete(cls).where(getattr(cls, col) == b.id))
        for dp in db.scalars(select(DramaProject).where(DramaProject.book_id == b.id)).all():
            _purge_drama_project(db, dp.id)
        db.delete(b)
        d = BOOKS_DIR / b.id
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        stats["books"] += 1

    # 2) 直接归属的短剧项目
    for dp in db.scalars(select(DramaProject).where(DramaProject.owner_id == user_id)).all():
        _purge_drama_project(db, dp.id)
        stats["drama_projects"] += 1

    # 3) 任务
    db.execute(delete(Task).where(Task.owner_id == user_id))
    stats["tasks"] = db.scalar(
        select(func.count()).select_from(Task).where(Task.owner_id == user_id)) or 0

    # 4) 会计凭证：默认脱敏保留，硬删需显式
    if hard_delete_vouchers:
        order_ids = db.scalars(select(Order.id).where(Order.user_id == user_id)).all()
        db.execute(delete(PaymentEvent).where(PaymentEvent.order_id.in_(order_ids)))
        db.execute(delete(Order).where(Order.user_id == user_id))
        db.execute(delete(Invoice).where(Invoice.user_id == user_id))
    else:
        db.execute(update(Order).where(Order.user_id == user_id).values(user_id=""))
        db.execute(update(Invoice).where(Invoice.user_id == user_id).values(user_id=""))

    # 5) 其余按 user_id 归属的表
    for cls, col in _CASCADE_USER_TABLES:
        db.execute(delete(cls).where(getattr(cls, col) == user_id))

    # 6) 用户行本身
    db.delete(u)
    db.commit()
    write_audit(db, operator=operator, action="user.delete",
                target_type="user", target_id=user_id, reason=reason, ip=ip,
                detail={"username": u.username, "hard_delete_vouchers": hard_delete_vouchers,
                        **stats})
    return {"ok": True, "user_id": user_id, "username": u.username, **stats,
            "note": "订单/发票按会计凭证保留并脱敏" if not hard_delete_vouchers
                    else "订单/发票已一并硬删"}


def bulk_user_action(db: Session, *, action: str, user_ids: list[str],
                     operator: User, role: str = "", delta: int = 0,
                     reason: str = "", ip: str = "") -> dict:
    """★ 批量用户操作（ban / unban / set_role）。

    每个子操作都走既有服务函数（自带强制原因 + 审计），单个失败不影响其余。
    """
    VALID = ("ban", "unban", "set_role")
    if action not in VALID:
        return {"ok": False, "error": f"非法批量操作：{action}（可选：{', '.join(VALID)}）"}
    if not user_ids:
        return {"ok": False, "error": "未选择用户"}


    results = []
    for uid in user_ids:
        if uid == getattr(operator, "id", ""):
            results.append({"user_id": uid, "ok": False, "error": "不能操作自己"})
            continue
        try:
            if action == "ban":
                r = set_user_active(db, user_id=uid, active=False, operator=operator,
                                    reason=reason, ip=ip)
            elif action == "unban":
                r = set_user_active(db, user_id=uid, active=True, operator=operator,
                                    reason=reason, ip=ip)
            elif action == "set_role":
                r = set_user_role(db, user_id=uid, role=role, operator=operator,
                                  reason=reason, ip=ip)
            results.append({"user_id": uid, "ok": bool(r.get("ok")), **r})
        except Exception as e:  # noqa: BLE001
            log.warning("批量操作失败 action=%s uid=%s err=%s", action, uid, e)
            results.append({"user_id": uid, "ok": False, "error": str(e)})

    done = sum(1 for x in results if x.get("ok"))
    write_audit(db, operator=operator, action=f"user.bulk.{action}",
                target_type="user", target_id="", reason=reason, ip=ip,
                detail={"count": len(user_ids), "done": done, "role": role, "delta": delta})
    return {"ok": True, "action": action, "total": len(user_ids), "done": done,
            "results": results}


def list_books_admin(db: Session, *, keyword: str = "", content_status: str = "",
                     flagged_only: bool = False, limit: int = 50,
                     offset: int = 0) -> dict:
    """书籍列表（管理视角，★ 不含正文）。

    返回的是**元数据 + 统计量**：书名、作者、章节数、字数、评分、处置状态。
    管理员要看正文必须走单独的高权限流程（当前版本不提供）。
    """
    q = select(Book)
    if keyword:
        kw = f"%{keyword.strip()}%"
        q = q.where((Book.title.like(kw)) | (Book.author.like(kw)) | (Book.id.like(kw)))
    if content_status:
        q = q.where(Book.content_status == content_status)
    if flagged_only:
        q = q.where(Book.flagged == 1)

    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(Book.created_at.desc())
                      .limit(min(limit, 200)).offset(max(offset, 0))).all()

    agg = {}
    bids = [b.id for b in rows]
    if bids:
        for bid, n, w in db.execute(
                select(Chapter.book_id, func.count(Chapter.id),
                       func.coalesce(func.sum(Chapter.words), 0))
                .where(Chapter.book_id.in_(bids))
                .group_by(Chapter.book_id)).all():
            agg[bid] = (int(n or 0), int(w or 0))

    owners = {}
    oids = list({b.owner_id for b in rows if b.owner_id})
    if oids:
        for uid_, un in db.execute(select(User.id, User.username)
                                   .where(User.id.in_(oids))).all():
            owners[uid_] = un

    return {"total": int(total), "items": [
        {"id": b.id, "title": b.title, "author": b.author,
         "owner_id": b.owner_id, "owner_name": owners.get(b.owner_id, "（无主）"),
         "status": b.status, "content_status": b.content_status or "normal",
         "flagged": int(b.flagged or 0),
         "chapters": agg.get(b.id, (0, 0))[0], "words": agg.get(b.id, (0, 0))[1],
         "quality_score": int(b.quality_score or 0),
         "theme": b.theme or "", "created_at": _fmt(b.created_at)}
        for b in rows]}




# ================================================================ 系统健康

def system_health(db: Session) -> dict:
    """系统状态（供后台"健康"页）。"""
    import os
    import shutil
    import time

    from apps.api import config

    # 数据库连通性 + 类型
    db_ok, db_info = True, ""
    t0 = time.time()
    try:
        db.execute(select(func.count()).select_from(User))
        db_info = f"{(time.time() - t0) * 1000:.0f}ms"
    except Exception as e:                          # noqa: BLE001
        db_ok, db_info = False, f"{type(e).__name__}: {e}"

    # 磁盘
    try:
        du = shutil.disk_usage(str(config.DATA_DIR))
        disk = {"total_gb": round(du.total / 1e9, 2),
                "used_gb": round(du.used / 1e9, 2),
                "free_gb": round(du.free / 1e9, 2),
                "used_pct": round(du.used / du.total * 100, 1)}
    except Exception:
        disk = {}

    # 进程信息
    try:
        load1, load5, load15 = os.getloadavg()
    except Exception:
        load1 = load5 = load15 = 0.0

    mem = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            mi = {}
            for line in f:
                k, _, v = line.partition(":")
                mi[k.strip()] = int(v.strip().split()[0])   # kB
        total = mi.get("MemTotal", 0) / 1024 / 1024
        avail = mi.get("MemAvailable", 0) / 1024 / 1024
        mem = {"total_mb": round(total, 1), "available_mb": round(avail, 1),
               "used_pct": round((total - avail) / total * 100, 1) if total else 0}
    except Exception:
        mem = {}

    return {
        "version": config.APP_VERSION,
        "db": {"ok": db_ok, "url_scheme": config.DB_URL.split("://")[0],
               "latency": db_info},
        "disk": disk, "memory": mem,
        "load": {"1m": round(load1, 2), "5m": round(load5, 2), "15m": round(load15, 2)},
        "time": _naive_now().strftime("%Y-%m-%d %H:%M:%S"),
    }

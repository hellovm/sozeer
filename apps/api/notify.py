"""通知中心（v0.25 Xf-2）。

★ 双通道：**站内信（保可达）+ 邮件（保及时）**。

设计要点：
  · `notify()` 是业务侧唯一入口 —— 业务不该关心"发什么通道、怎么发"
  · **按 kind 细分偏好**（一个总开关会导致用户全关，重要通知也收不到）
  · **去重**（dedup_key + 时间窗）：同一事件多路径触发不刷屏
  · **事件模板集中定义**：文案改一处即可，不散落在业务代码里
  · **绝不影响主流程**：任何失败只记日志（与 costing/telemetry 同一策略）

★ 为什么不做"通知轰炸"式的全量推送：
  通知的价值在于"被读到"。发太多用户会关掉全部通知 —— 那时
  连"账号异地登录"这种安全告警也送不到了。
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.models import Notification, NotificationPref
from apps.api.models import now as _now

_log = logging.getLogger("novelforge.notify")

# ---------------------------------------------------------------- 事件类型

KINDS = ("system", "order", "credit", "drama", "security", "feedback", "quota")
KIND_LABEL = {
    "system": "系统通知",
    "order": "订单与支付",
    "credit": "积分变动",
    "drama": "短剧生成",
    "security": "安全提醒",
    "feedback": "反馈处理",
    "quota": "配额与用量",
}

LEVELS = ("info", "success", "warning", "error")

# ★ 不可关闭的类型（安全合规类）
#   为什么：用户可能为"清静"关掉全部通知，但安全告警不能关 ——
#   一旦账号被盗，通知是唯一的自救线索。
FORCED_KINDS = {"security"}

DEDUP_WINDOW_MINUTES = 60          # 同 dedup_key 60 分钟内只留一条


# ---------------------------------------------------------------- 偏好

def get_prefs(db: Session, user_id: str) -> dict:
    """取用户偏好（未设置的用默认：全开）。"""
    rows = list(db.scalars(
        select(NotificationPref).where(NotificationPref.user_id == user_id)).all())
    saved = {r.kind: {"inapp": bool(r.inapp), "email": bool(r.email)}
             for r in rows}
    out: dict[str, dict] = {}
    for k in KINDS:
        base = saved.get(k, {"inapp": True, "email": True})
        if k in FORCED_KINDS:
            base = {"inapp": True, "email": True}      # 强制开启
        out[k] = {**base, "label": KIND_LABEL[k],
                  "forced": k in FORCED_KINDS}
    return out


def set_prefs(db: Session, user_id: str, kind: str, *,
              inapp: bool | None = None, email: bool | None = None) -> dict:
    """更新单个类型的偏好。"""
    k = (kind or "").strip().lower()
    if k not in KINDS:
        return {"ok": False, "error": f"未知通知类型：{kind}"}
    if k in FORCED_KINDS and (inapp is False or email is False):
        return {"ok": False, "error": f"「{KIND_LABEL[k]}」为安全类通知，不可关闭"}
    pid = f"{user_id}:{k}"
    row = db.get(NotificationPref, pid)
    if row is None:
        row = NotificationPref(id=pid, user_id=user_id, kind=k,
                               inapp=1, email=1, updated_at=_now())
        db.add(row)
    if inapp is not None:
        row.inapp = 1 if inapp else 0
    if email is not None:
        row.email = 1 if email else 0
    row.updated_at = _now()
    db.commit()
    return {"ok": True, "kind": k, "inapp": bool(row.inapp),
            "email": bool(row.email)}


def _allowed(db: Session, user_id: str, kind: str) -> tuple[bool, bool]:
    """(能否站内, 能否邮件)"""
    p = get_prefs(db, user_id).get(kind) or {"inapp": True, "email": True}
    return bool(p.get("inapp")), bool(p.get("email"))


# ---------------------------------------------------------------- 模板

# ★ 事件模板集中定义（文案改一处，不散落业务代码）。
#   变量用 {name} 占位，render 时做安全替换。
TEMPLATES: dict[str, dict] = {
    # ---- ★ v0.25.6 J10：运维告警（发给管理员）----
    "queue.alert": {
        "kind": "system", "level": "warning", "title": "【运维】{title}",
        "body": "{message}\n\n建议：{suggest}", "link": "/admin/",
    },
    # ---- 订单与支付 ----
    "order.paid": {
        "kind": "order", "level": "success", "title": "支付成功",
        "body": "你购买的「{plan_name}」已到账，{credits} 积分已入账。\n订单号：{order_no}",
        "link": "/me/orders",
    },
    "order.refunded": {
        "kind": "order", "level": "warning", "title": "退款已处理",
        "body": "订单 {order_no} 的退款已发起，预计 1-7 个工作日到账。\n原因：{reason}",
        "link": "/me/orders",
    },
    "order.failed": {
        "kind": "order", "level": "error", "title": "支付失败",
        "body": "订单 {order_no} 支付未完成。如已扣款请勿重复支付，"
                "稍后会自动对账；如未扣款可重新发起。",
        "link": "/me/orders",
    },
    # ---- 积分 ----
    "credit.low": {
        "kind": "credit", "level": "warning", "title": "积分余额不足",
        "body": "当前余额 {balance} 积分，低于提醒阈值 {threshold}。\n"
                "为避免生成中断，建议及时充值。",
        "link": "/pricing",
    },
    "credit.granted": {
        "kind": "credit", "level": "success", "title": "积分已到账",
        "body": "本次到账 {delta} 积分，当前余额 {balance}。\n说明：{note}",
        "link": "/me/credits",
    },
    # ---- 短剧 ----
    "drama.done": {
        "kind": "drama", "level": "success", "title": "短剧生成完成",
        "body": "《{title}》第 {episode} 集已生成完成，共 {scenes} 个镜头。",
        "link": "/drama/{project_id}",
    },
    "drama.failed": {
        "kind": "drama", "level": "error", "title": "短剧生成失败",
        "body": "《{title}》第 {episode} 集生成失败。\n原因：{reason}\n"
                "已消耗的积分将自动退回。",
        "link": "/drama/{project_id}",
    },
    # ---- 安全 ----
    "security.new_login": {
        "kind": "security", "level": "warning", "title": "新设备登录提醒",
        "body": "你的账号在新设备登录：\n时间：{time}\nIP：{ip}\n"
                "若非本人操作，请立即修改密码并退出所有设备。",
        "link": "/me/sessions",
    },
    "security.password_changed": {
        "kind": "security", "level": "warning", "title": "密码已修改",
        "body": "你的账号密码已于 {time} 修改。若非本人操作，"
                "请立即通过「忘记密码」重置并联系客服。",
        "link": "/me/sessions",
    },
    "security.revoked_all": {
        "kind": "security", "level": "info", "title": "已退出所有设备",
        "body": "你的账号已在 {time} 退出所有设备。",
        "link": "/me/sessions",
    },
    # ---- 反馈 ----
    "feedback.replied": {
        "kind": "feedback", "level": "info", "title": "你的反馈有回复了",
        "body": "问题「{title}」已处理。\n处理说明：{note}",
        "link": "/me/feedback",
    },
    # ---- 配额 ----
    "quota.exceeded": {
        "kind": "quota", "level": "warning", "title": "今日用量已达上限",
        "body": "今日已使用 {used} token（上限 {cap}）。\n"
                "额度将于次日 00:00 重置，或可升级套餐提高上限。",
        "link": "/pricing",
    },
    # ---- ★ v1.0 E4：运营触达（注册欢迎 / 上手引导）----
    #
    #   ★ 为什么注册就该发：新用户的第一小时决定留存。
    #     一封"三步上手"的信比"欢迎使用"有用得多。
    #   ★ 为什么链接给 /app（工作台）而不是首页：直接开工，少一次点击。
    "welcome": {
        "kind": "system", "level": "success", "title": "欢迎加入溯字，{username}",
        "body": "三步开始你的第一部作品：\n"
                "① 新建作品，填写题材与目标字数\n"
                "② 在写作台用对话驱动 AI 代笔（草稿需你「采纳」才进正文）\n"
                "③ 用质检报告发现前后矛盾（境界越界、伏笔超期）\n\n"
                "有疑问随时看帮助中心，或直接回复本邮件。",
        "link": "/app",
    },
    # ---- 系统 ----
    "system.notice": {
        "kind": "system", "level": "info", "title": "{title}",
        "body": "{body}", "link": "{link}",
    },
}


def render_event(event: str, ctx: dict | None = None) -> dict:
    """渲染事件模板（缺失变量保留占位，不抛异常）。"""
    tpl = TEMPLATES.get(event)
    if tpl is None:
        return {"kind": "system", "level": "info",
                "title": event, "body": "", "link": ""}
    data = ctx or {}

    class _Safe(dict):
        def __missing__(self, k):                # noqa: D105
            return "{" + k + "}"

    def _fmt(s: str) -> str:
        try:
            return str(s).format_map(_Safe(data))
        except Exception:                        # noqa: BLE001
            return str(s)

    return {"kind": tpl["kind"], "level": tpl["level"],
            "title": _fmt(tpl["title"]), "body": _fmt(tpl["body"]),
            "link": _fmt(tpl["link"])}


# ---------------------------------------------------------------- 发送

def _new_id() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


def notify(db: Session, *, user_id: str, event: str, ctx: dict | None = None,
           title: str = "", body: str = "", link: str = "",
           dedup_key: str = "", send_mail: bool = True,
           email_to: str = "", kind: str = "", level: str = "") -> dict:
    """★ 业务侧唯一入口：发一条通知。

    返回 {ok, id, inapp, mail, deduped}

    ★ 为什么所有参数都有默认值：调用方只该传"事件名 + 上下文"，
      其余（文案、级别、链接）由模板决定。
    """
    try:
        r = render_event(event, ctx)
        k = kind or r["kind"]
        lv = level or r["level"]
        ttl = title or r["title"]
        bdy = body or r["body"]
        lnk = link or r["link"]

        inapp_ok, mail_ok = _allowed(db, user_id, k)
        if not inapp_ok and not mail_ok:
            return {"ok": True, "skipped": True, "reason": "用户已关闭该类型通知"}

        # ---- 去重 ----
        deduped = False
        nid = f"nt_{_new_id()}"
        if dedup_key:
            since = _now() - dt.timedelta(minutes=DEDUP_WINDOW_MINUTES)
            old = db.scalar(
                select(Notification)
                .where(Notification.user_id == user_id,
                       Notification.dedup_key == dedup_key,
                       Notification.created_at >= since)
                .order_by(Notification.created_at.desc()))
            if old is not None:
                # ★ 覆盖而非新增：保留最新状态，但保持未读（用户还没看到）
                nid = old.id
                old.title = ttl[:200]
                old.body = bdy
                old.level = lv
                old.link = lnk[:512]
                old.read_at = None
                old.created_at = _now()
                deduped = True
        if not deduped:
            db.add(Notification(
                id=nid, user_id=user_id, kind=k, level=lv,
                title=ttl[:200], body=bdy, link=lnk[:512],
                dedup_key=dedup_key[:128], read_at=None,
                mail_status="", mail_error="", created_at=_now()))

        # ---- 邮件 ----
        mail_res: dict = {"ok": False, "skipped": True}
        if send_mail and mail_ok and not deduped:
            mail_res = _send_mail(db, user_id, email_to, k, ttl, bdy, lnk, nid)

        db.commit()
        return {"ok": True, "id": nid, "kind": k, "level": lv,
                "inapp": inapp_ok, "mail": mail_res.get("status", "skipped"),
                "deduped": deduped}
    except Exception as e:                       # noqa: BLE001
        # ★ 通知是旁路，任何失败都不得影响主流程
        _log.warning("通知发送失败（不影响主流程）：%s: %s", type(e).__name__, e)
        try:
            db.rollback()
        except Exception:                        # noqa: BLE001
            pass
        return {"ok": False, "error": str(e)[:200]}


def _send_mail(db: Session, user_id: str, email_to: str, kind: str,
               title: str, body: str, link: str, nid: str) -> dict:
    """发邮件并回写状态（不抛异常）。"""
    from apps.api import mail as _mail
    to = email_to
    if not to:
        try:
            from apps.api.models import User
            u = db.get(User, user_id)
            to = (getattr(u, "email", "") or "") if u else ""
        except Exception:                        # noqa: BLE001
            to = ""
    row = db.get(Notification, nid)
    if not to or not _mail.valid_recipient(to):
        if row is not None:
            row.mail_status = "skipped"
            row.mail_error = "未绑定邮箱"
        return {"ok": False, "status": "skipped", "reason": "未绑定邮箱"}

    lines = [x for x in (body or "").split("\n") if x.strip()]
    action = f"{_mail.site_url()}{link}" if link.startswith("/") else link
    html_body = _mail.render_notice(title, lines, action=action,
                                    action_text="查看详情")
    res = _mail.send(to, f"【溯字】{title}", html_body, db=db)
    if row is not None:
        if res.get("ok"):
            row.mail_status = "sent" if not res.get("dry_run") else "skipped"
            row.mail_error = "" if not res.get("dry_run") else "dry-run"
        else:
            row.mail_status = "failed"
            row.mail_error = (res.get("error") or "")[:250]
    return {"ok": bool(res.get("ok")),
            "status": res.get("dry_run") and "skipped" or
            ("sent" if res.get("ok") else "failed")}


def notify_many(db: Session, user_ids: list[str], *, event: str,
                ctx: dict | None = None, **kw) -> dict:
    """批量通知（如"系统维护公告"）。"""
    ok = 0
    for uid in user_ids:
        r = notify(db, user_id=uid, event=event, ctx=ctx, **kw)
        if r.get("ok"):
            ok += 1
    return {"ok": True, "total": len(user_ids), "sent": ok}


# ---------------------------------------------------------------- 查询

def _row(r: Notification) -> dict:
    return {"id": r.id, "kind": r.kind,
            "kind_label": KIND_LABEL.get(r.kind, r.kind),
            "level": r.level, "title": r.title, "body": r.body,
            "link": r.link, "read": r.read_at is not None,
            "mail_status": r.mail_status,
            "created_at": _fmt(r.created_at)}


def _fmt(t) -> str:
    return t.strftime("%Y-%m-%d %H:%M") if t else ""


def list_mine(db: Session, user_id: str, *, unread_only: bool = False,
              kind: str = "", limit: int = 50, offset: int = 0) -> dict:
    q = select(Notification).where(Notification.user_id == user_id)
    cnt = select(func.count()).select_from(Notification).where(
        Notification.user_id == user_id)
    if unread_only:
        q = q.where(Notification.read_at.is_(None))
        cnt = cnt.where(Notification.read_at.is_(None))
    if kind:
        q = q.where(Notification.kind == kind)
        cnt = cnt.where(Notification.kind == kind)
    rows = list(db.scalars(q.order_by(Notification.created_at.desc())
                           .limit(min(max(limit, 1), 200))
                           .offset(max(offset, 0))).all())
    total = db.scalar(cnt) or 0
    return {"items": [_row(r) for r in rows], "total": int(total),
            "unread": unread_count(db, user_id)}


def unread_count(db: Session, user_id: str) -> int:
    return int(db.scalar(
        select(func.count()).select_from(Notification)
        .where(Notification.user_id == user_id,
               Notification.read_at.is_(None))) or 0)


def mark_read(db: Session, user_id: str, ids: list[str] | None = None,
              all_read: bool = False) -> dict:
    """标记已读（★ 只能操作自己的通知）。"""
    q = select(Notification).where(Notification.user_id == user_id,
                                   Notification.read_at.is_(None))
    if not all_read:
        if not ids:
            return {"ok": False, "error": "缺少 ids"}
        q = q.where(Notification.id.in_(list(ids)[:500]))
    n = 0
    for r in db.scalars(q).all():
        r.read_at = _now()
        n += 1
    db.commit()
    return {"ok": True, "marked": n, "unread": unread_count(db, user_id)}


def stats(db: Session, *, days: int = 7) -> dict:
    """通知统计（管理端）。"""
    since = _now() - dt.timedelta(days=days)
    try:
        total = int(db.scalar(select(func.count()).select_from(Notification)
                              .where(Notification.created_at >= since)) or 0)
        unread = int(db.scalar(
            select(func.count()).select_from(Notification)
            .where(Notification.created_at >= since,
                   Notification.read_at.is_(None))) or 0)
        by_kind: dict[str, int] = {}
        for k in KINDS:
            n = int(db.scalar(select(func.count()).select_from(Notification)
                              .where(Notification.created_at >= since,
                                     Notification.kind == k)) or 0)
            if n:
                by_kind[k] = n
        mail_sent = int(db.scalar(
            select(func.count()).select_from(Notification)
            .where(Notification.created_at >= since,
                   Notification.mail_status == "sent")) or 0)
        mail_failed = int(db.scalar(
            select(func.count()).select_from(Notification)
            .where(Notification.created_at >= since,
                   Notification.mail_status == "failed")) or 0)
    except Exception:                            # noqa: BLE001
        total = unread = mail_sent = mail_failed = 0
        by_kind = {}
    return {"days": days, "total": total, "unread": unread,
            "read_rate": (round((total - unread) / total, 3) if total else 0),
            "by_kind": by_kind, "kind_label": KIND_LABEL,
            "mail": {"sent": mail_sent, "failed": mail_failed}}


def prune(db: Session, *, keep_days: int = 90, only_read: bool = True) -> int:
    """清理老通知（防表膨胀）。★ 默认只删已读的。"""
    cutoff = _now() - dt.timedelta(days=keep_days)
    q = select(Notification).where(Notification.created_at < cutoff)
    if only_read:
        q = q.where(Notification.read_at.is_not(None))
    rows = list(db.scalars(q).all())
    for r in rows:
        db.delete(r)
    if rows:
        db.commit()
    return len(rows)


def notify_admin(db: Session, *, event: str, ctx: dict | None = None,
                 dedup_key: str = "") -> dict:
    """★ 发给**所有管理员**的通知（运维告警用）。

    ★ 为什么单独一个函数而不是让调用方自己查管理员列表：
      查列表的逻辑（谁算管理员）会变（RBAC 后不止 role=admin），
      集中在一处改，避免每个调用点各写一遍。

    ★ 为什么每个管理员单独 try：一个失败不能影响其他人收到通知。
    """
    sent = 0
    try:
        from sqlalchemy import select
        from apps.api.models import User
        rows = list(db.scalars(select(User).where(User.is_active == 1)).all())
    except Exception as e:                              # noqa: BLE001
        _log.warning("查询管理员失败：%s", e)
        return {"ok": False, "reason": f"查询管理员失败：{e}"}

    for u in rows:
        # ★ 用 RBAC 判定（而不是 role == 'admin'），自定义角色也能收到
        try:
            from apps.api import rbac
            if not rbac.has_permission(db, u.role, "admin.access"):
                continue
        except Exception:                               # noqa: BLE001
            if getattr(u, "role", "") not in ("admin", "owner"):
                continue
        try:
            notify(db, user_id=u.id, event=event, ctx=ctx or {},
                   dedup_key=(f"{dedup_key}:{u.id}" if dedup_key else ""))
            sent += 1
        except Exception as e:                          # noqa: BLE001
            _log.warning("向 %s 发送管理员通知失败：%s", u.username, e)
    return {"ok": True, "sent": sent}

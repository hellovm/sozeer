"""通知中心与邮件路由（v0.25 Xf 组）。

Xf-2 通知：用户端列表/已读/偏好 + 管理端统计
Xf-1 邮件：配置状态 + 测试发送
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from apps.api import auth
from apps.api import mail as _mail
from apps.api import notify as nf
from apps.api.db import get_db
from apps.api.models import User

router = APIRouter(tags=["notify"])


# ============================================================ 用户端通知

@router.get("/api/me/notifications")
def my_notifications(unread_only: bool = False, kind: str = "",
                     limit: int = Query(50, ge=1, le=200),
                     offset: int = Query(0, ge=0),
                     db: Session = Depends(get_db),
                     user: User = Depends(auth.get_current_user)):
    """★ 我的通知列表（站内信）。"""
    return nf.list_mine(db, user.id, unread_only=unread_only, kind=kind,
                        limit=limit, offset=offset)


@router.get("/api/me/notifications/count")
def notification_count(db: Session = Depends(get_db),
                       user: User = Depends(auth.get_current_user)):
    """未读数（前端红点轮询用 —— 轻量）。"""
    return {"unread": nf.unread_count(db, user.id)}


class ReadIn(BaseModel):
    ids: list[str] = []
    all_read: bool = False


@router.post("/api/me/notifications/read")
def mark_read(body: ReadIn, db: Session = Depends(get_db),
              user: User = Depends(auth.get_current_user)):
    """标记已读（★ 只能操作自己的）。"""
    r = nf.mark_read(db, user.id, ids=body.ids, all_read=body.all_read)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "操作失败")
    return r


@router.get("/api/me/notification-prefs")
def get_prefs(db: Session = Depends(get_db),
              user: User = Depends(auth.get_current_user)):
    """★ 通知偏好（按类型细分 + 标注哪些不可关闭）。"""
    return {"items": nf.get_prefs(db, user.id),
            "forced_kinds": sorted(nf.FORCED_KINDS),
            "kinds": list(nf.KINDS)}


class PrefIn(BaseModel):
    kind: str
    inapp: bool | None = None
    email: bool | None = None


@router.post("/api/me/notification-prefs")
def set_pref(body: PrefIn, db: Session = Depends(get_db),
             user: User = Depends(auth.get_current_user)):
    """更新某个类型的偏好。"""
    r = nf.set_prefs(db, user.id, body.kind, inapp=body.inapp, email=body.email)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "更新失败")
    return r


# ============================================================ 邮件状态

@router.get("/api/me/mail-status")
def mail_status(db: Session = Depends(get_db),
                user: User = Depends(auth.get_current_user)):
    """我的邮箱绑定与可用性（前端提示"能否收到邮件通知"）。"""
    st = _mail.status(db)
    bound = bool(getattr(user, "email", "") or "")
    return {
        "bound": bound,
        "email": _mail.mask_email(getattr(user, "email", "") or "") if bound else "",
        "service_ready": st["configured"],
        "dry_run": st["dry_run"],
        "hint": ("" if (st["configured"] and not st["dry_run"])
                 else "邮件服务未就绪，通知将仅显示在站内"),
    }


# ============================================================ 管理端

@router.get("/api/admin/notify/stats")
def admin_stats(days: int = Query(7, ge=1, le=365),
                db: Session = Depends(get_db),
                admin: User = Depends(auth.require_admin)):
    """通知统计（总量/未读率/邮件投递）。"""
    r = nf.stats(db, days=days)
    r["mail_config"] = _mail.status(db)
    return r


@router.get("/api/admin/mail/status")
def admin_mail_status(db: Session = Depends(get_db),
                      admin: User = Depends(auth.require_admin)):
    """★ 邮件服务状态（配置项 + 缺失提示，**不含密码**）。"""
    return _mail.status(db)


class MailConfigIn(BaseModel):
    """SMTP 配置写入（v1.0-E4.2）。

    ★ 为什么需要这个端点：此前只有 `status`（读）和 `test`（发信），
      **没有"保存"** → 管理员只能在服务器上改 `.env` 再重启服务。
      而 `.env` 改动要重启才生效，且运维要登机器 —— 对小部署太重。

    ★ 存到 keyvault（加密）而非明文 Setting。
    ★ 环境变量**优先于**这里（容器化场景方便覆盖，见 mail.config）。
    """
    host: str = ""
    port: int = 0
    user: str = ""
    # ★ 密码留空表示"不修改"（避免前端回显密码；也避免误清空）
    password: str = ""
    from_addr: str = ""
    from_name: str = ""
    secure: str = ""


@router.post("/api/admin/mail/config")
def admin_mail_config_save(body: MailConfigIn, db: Session = Depends(get_db),
                           admin: User = Depends(auth.require_admin)):
    """保存 SMTP 配置（★ 加密存储，立即生效无需重启）。"""
    from apps.api import secret_store
    from apps.api.admin_service import write_audit

    # ★ 服务端校验：不信前端（MEMORY §33）
    if body.port and not (1 <= body.port <= 65535):
        raise HTTPException(400, "端口必须在 1~65535 之间")
    sec = (body.secure or "").strip().lower()
    if sec and sec not in ("ssl", "starttls", "none"):
        raise HTTPException(400, "加密方式只能是 ssl / starttls / none")
    host = (body.host or "").strip()
    if host and not _mail.valid_host(host):
        raise HTTPException(400, "SMTP 服务器地址格式不正确")

    from_addr = (body.from_addr or "").strip()
    if from_addr and not _mail.valid_recipient(from_addr):
        raise HTTPException(400, "发件地址格式不正确")

    saved: list[str] = []
    for key, val in (("mail.smtp_host", host),
                     ("mail.smtp_port", str(body.port) if body.port else ""),
                     ("mail.smtp_user", (body.user or "").strip()),
                     ("mail.smtp_from", from_addr),
                     ("mail.smtp_from_name", (body.from_name or "").strip()),
                     ("mail.smtp_secure", sec)):
        if secret_store.set_secret(db, key, val):
            saved.append(key)
    # ★ 密码单独处理：空 = 不改（避免"打开页面保存一次就把密码清了"）
    pwd_changed = bool((body.password or "").strip())
    if pwd_changed:
        secret_store.set_secret(db, "mail.smtp_pass", body.password.strip())
        saved.append("mail.smtp_pass")
    db.commit()

    # ★ 写后留痕（★ 绝不记录密码本身）
    try:
        write_audit(db, operator=admin, action="mail.config.save",
                    target_type="mail", target_id="smtp",
                    detail={"host": host, "port": body.port, "secure": sec,
                            "password_changed": pwd_changed})
    except Exception:                                   # noqa: BLE001
        pass

    return {"ok": True, "saved": saved, "status": _mail.status(db)}


class MailTestIn(BaseModel):
    to: str


@router.post("/api/admin/mail/test")
def admin_mail_test(body: MailTestIn, db: Session = Depends(get_db),
                    admin: User = Depends(auth.require_admin)):
    """★ 发送测试邮件（真发，返回明确结果）。"""
    if not _mail.valid_recipient(body.to or ""):
        raise HTTPException(400, "收件人邮箱格式不正确")
    r = _mail.test_send(db, body.to.strip())
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "发送失败")
    return r


@router.post("/api/admin/notify/prune")
def admin_prune(keep_days: int = Query(90, ge=7, le=3650),
                only_read: bool = True,
                db: Session = Depends(get_db),
                admin: User = Depends(auth.require_admin)):
    """清理老通知（★ 默认只删已读，避免误删未读重要通知）。"""
    n = nf.prune(db, keep_days=keep_days, only_read=only_read)
    return {"ok": True, "deleted": n, "keep_days": keep_days,
            "only_read": only_read}


class BroadcastIn(BaseModel):
    # ★ 留默认值：公告常只有标题（如"今晚 23:00 维护"），
    #   强制要求 body 会增加调用方无谓的负担
    title: str = ""
    body: str = ""
    link: str = ""
    user_ids: list[str] = []


@router.post("/api/admin/notify/broadcast")
def admin_broadcast(body: BroadcastIn, db: Session = Depends(get_db),
                    admin: User = Depends(auth.require_admin)):
    """★ 系统公告（批量通知）。

    ★ 安全约束：邮件通道默认**关闭** —— 全量群发邮件极易被判定为垃圾邮件，
      且会打爆 SMTP 配额。站内信足够承载公告场景。
      真要发邮件请走 `notify_many(..., send_mail=True)` 并做好分批限速。
    """
    if not (body.title or "").strip():
        raise HTTPException(400, "缺少标题")
    from sqlalchemy import select as _sel
    ids = body.user_ids
    if not ids:
        ids = [u for u in db.scalars(_sel(User.id).where(User.is_active == 1)).all()]
    r = nf.notify_many(db, list(ids)[:20000], event="system.notice",
                       ctx={"title": body.title.strip(),
                            "body": body.body or "", "link": body.link or "/"},
                       send_mail=False, dedup_key="")
    return {**r, "note": "仅发站内信（邮件通道默认关闭，避免被判垃圾邮件）"}

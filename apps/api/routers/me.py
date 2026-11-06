"""用户中心路由（v0.20.4 A4）。

★ 安全要点：
  · 所有 /api/me/* 都要求登录（走全局中间件）
  · 改密码/关 2FA **需要旧密码/验证码**（防会话劫持后直接改）
  · 数据导出只含**自己的**数据
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api import auth, user_center as uc
from apps.api.db import get_db
from apps.api.models import User, now

router = APIRouter()
log = logging.getLogger("novelforge")


def _client_ip(request: Request) -> str:
    # 生产在 nginx 后面，优先取 X-Forwarded-For 首个
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


# ================================================================ 个人资料

class ProfileIn(BaseModel):
    nickname: str | None = None
    bio: str | None = None


@router.get("/api/me/profile")
def get_profile(user: User = Depends(auth.get_current_user),
                db: Session = Depends(get_db)):
    # ★ v0.25.5：返回展开后的权限点 —— 前端据此显示/隐藏入口，
    #   不再靠 role 字符串比较（RBAC 下角色可自定义）。
    try:
        from apps.api import rbac as _rb
        perms = _rb.expand(_rb.role_permissions(db, user.role))
    except Exception:                                # noqa: BLE001
        perms = ["admin.access"] if user.role == "admin" else []
    return {
        "id": user.id, "username": user.username,
        "nickname": getattr(user, "nickname", "") or "",
        "bio": getattr(user, "bio", "") or "",
        "avatar": getattr(user, "avatar", "") or "",
        "email": user.email or "",
        "email_verified": int(getattr(user, "email_verified", 0) or 0),
        "phone": uc._mask(getattr(user, "phone", "") or ""),
        "phone_bound": bool(getattr(user, "phone", "")),
        "phone_verified": int(getattr(user, "phone_verified", 0) or 0),
        "role": user.role,
        "perms": perms,
        "is_admin": "admin.access" in perms,
        "totp_enabled": int(getattr(user, "totp_enabled", 0) or 0),
        "created_at": user.created_at.strftime("%Y-%m-%d") if user.created_at else "",
        "last_login_at": (user.last_login_at.strftime("%Y-%m-%d %H:%M:%S")
                          if getattr(user, "last_login_at", None) else ""),
    }


@router.patch("/api/me/profile")
def patch_profile(body: ProfileIn, db: Session = Depends(get_db),
                  user: User = Depends(auth.get_current_user)):
    # ★★ v0.26.1 修复：必须把 user 重新绑到本请求的 session。
    #   否则赋值落在 detached 实例上，db.commit() 提交的是另一个对象 → 改动丢失
    #   （接口返回 200 但数据没变 —— 见 auth.rebind_user 注释）。
    u = auth.rebind_user(db, user)
    if body.nickname is not None:
        nk = body.nickname.strip()
        if len(nk) > 32:
            raise HTTPException(400, "昵称最长 32 个字符")
        u.nickname = nk
    if body.bio is not None:
        if len(body.bio) > 500:
            raise HTTPException(400, "简介最长 500 个字符")
        u.bio = body.bio.strip()
    db.commit()
    return {"ok": True, "nickname": u.nickname, "bio": u.bio}


@router.post("/api/me/avatar")
async def upload_avatar(file: UploadFile = File(...), db: Session = Depends(get_db),
                        user: User = Depends(auth.get_current_user)):
    """上传头像（存 data/avatars/）。"""
    import pathlib
    import uuid as _uuid
    from apps.api import config
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "空文件")
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(413, "头像不能超过 2MB")
    ext = "png"
    ct = (file.content_type or "").lower()
    if "jpeg" in ct or "jpg" in ct:
        ext = "jpg"
    elif "webp" in ct:
        ext = "webp"
    elif "gif" in ct:
        ext = "gif"
    d = pathlib.Path(str(config.DATA_DIR)) / "avatars"
    d.mkdir(parents=True, exist_ok=True)
    name = f"{user.id}_{_uuid.uuid4().hex[:8]}.{ext}"
    (d / name).write_bytes(raw)
    u = auth.rebind_user(db, user)          # ★ v0.26.1 同上
    u.avatar = f"avatars/{name}"
    db.commit()
    return {"ok": True, "avatar": u.avatar,
            "url": f"/api/files/_avatars/{u.avatar}"}


# ================================================================ 安全设置

class PwdIn(BaseModel):
    old_password: str
    new_password: str


@router.post("/api/me/password")
def change_password(body: PwdIn, db: Session = Depends(get_db),
                    user: User = Depends(auth.get_current_user)):
    """改密码（★ 需旧密码 + 改后旧令牌全部失效）。"""
    if not auth.verify_password(body.old_password or "", user.pw_hash or ""):
        raise HTTPException(400, "原密码不正确")
    if len(body.new_password or "") < 6:
        raise HTTPException(400, "新密码至少 6 位")
    if body.new_password == body.old_password:
        raise HTTPException(400, "新密码不能与原密码相同")

    # ★★ v0.26.1 修复（严重）：改密码也必须重新绑定 session！
    #   修复前：user 是 detached 实例 → 赋值丢失 → **密码根本没改**，
    #   但接口返回 200 "密码已修改" → 用户以为改成功了，实际旧密码仍可用。
    #   这是最危险的一类 bug（安全操作静默失效）。
    u = auth.rebind_user(db, user)
    u.pw_hash = auth.hash_password(body.new_password)
    # ★ 改密后旧令牌全部失效（安全惯例：密码变了，所有会话应重登）
    u.token_valid_after = now()
    db.commit()
    return {"ok": True, "message": "密码已修改，其他设备需重新登录"}


@router.get("/api/me/logins")
def my_logins(limit: int = 20, db: Session = Depends(get_db),
              user: User = Depends(auth.get_current_user)):
    return {"items": uc.login_history(db, user.id, limit=min(limit, 100))}


@router.post("/api/me/2fa/setup")
def setup_2fa(db: Session = Depends(get_db),
              user: User = Depends(auth.get_current_user)):
    try:
        return uc.setup_2fa(db, user)
    except uc.UserError as e:
        raise HTTPException(400, str(e))


class CodeIn(BaseModel):
    code: str


@router.post("/api/me/2fa/enable")
def enable_2fa(body: CodeIn, db: Session = Depends(get_db),
               user: User = Depends(auth.get_current_user)):
    try:
        return uc.enable_2fa(db, user, body.code)
    except uc.UserError as e:
        raise HTTPException(400, str(e))


class PwdOnlyIn(BaseModel):
    password: str


@router.post("/api/me/2fa/disable")
def disable_2fa(body: PwdOnlyIn, db: Session = Depends(get_db),
                user: User = Depends(auth.get_current_user)):
    try:
        return uc.disable_2fa(db, user, body.password)
    except uc.UserError as e:
        raise HTTPException(400, str(e))


# ================================================================ 验证码

class SendCodeIn(BaseModel):
    target: str
    purpose: str = "login"


@router.post("/api/auth/send-code")
def send_code(body: SendCodeIn, db: Session = Depends(get_db)):
    """发送验证码（公开端点 —— 登录/找回密码前要用）。

    ★ 有频率限制（60 秒）与尝试上限（5 次）。
    """
    try:
        return uc.send_code(db, body.target, body.purpose)
    except uc.UserError as e:
        raise HTTPException(429, str(e))


class LoginByCodeIn(BaseModel):
    target: str
    code: str


@router.post("/api/auth/login-by-code")
def login_by_code(body: LoginByCodeIn, request: Request,
                  db: Session = Depends(get_db)):
    """验证码登录（手机/邮箱）。★ 未注册的 target 直接拒绝（不自动注册）。"""
    if not uc.verify_code(db, body.target, body.code, "login"):
        uc.record_login(db, "", ip=_client_ip(request), ua=request.headers.get("user-agent", ""),
                        method="code", success=False, reason="验证码错误")
        raise HTTPException(400, "验证码错误或已过期")

    from sqlalchemy import select as _sel
    target = (body.target or "").strip()
    u = db.scalar(_sel(User).where(User.phone == target))
    if u is None:
        u = db.scalar(_sel(User).where(User.email == target))
    if u is None or not u.is_active or getattr(u, "deleted_at", None):
        raise HTTPException(404, "该账号不存在或已停用，请先注册")

    u.last_login_at = now()
    u.last_login_ip = _client_ip(request)
    db.commit()
    uc.record_login(db, u.id, ip=_client_ip(request),
                    ua=request.headers.get("user-agent", ""),
                    method="code", success=True)
    return {"ok": True, "user": {"id": u.id, "username": u.username, "role": u.role},
            "token": auth.make_token(u.id)}


class ResetIn(BaseModel):
    target: str
    code: str
    new_password: str


@router.post("/api/auth/reset-password")
def reset_password(body: ResetIn, db: Session = Depends(get_db)):
    """找回密码（验证码 + 新密码）。"""
    if len(body.new_password or "") < 6:
        raise HTTPException(400, "新密码至少 6 位")
    if not uc.verify_code(db, body.target, body.code, "reset"):
        raise HTTPException(400, "验证码错误或已过期")

    from sqlalchemy import select as _sel
    target = (body.target or "").strip()
    u = db.scalar(_sel(User).where(User.phone == target))
    if u is None:
        u = db.scalar(_sel(User).where(User.email == target))
    if u is None:
        raise HTTPException(404, "该账号不存在")

    u.pw_hash = auth.hash_password(body.new_password)
    u.token_valid_after = now()          # ★ 旧令牌失效
    db.commit()
    return {"ok": True, "message": "密码已重置，请用新密码登录"}


class BindIn(BaseModel):
    target: str
    code: str


@router.post("/api/auth/bind-phone")
def bind_phone(body: BindIn, db: Session = Depends(get_db),
               user: User = Depends(auth.get_current_user)):
    from sqlalchemy import select as _sel
    t = (body.target or "").strip()
    if not uc._looks_like_phone(t):
        raise HTTPException(400, "手机号格式不正确")
    if db.scalar(_sel(User).where(User.phone == t, User.id != user.id)) is not None:
        raise HTTPException(409, "该手机号已被其他账号绑定")
    if not uc.verify_code(db, t, body.code, "bind"):
        raise HTTPException(400, "验证码错误或已过期")
    u = auth.rebind_user(db, user)          # ★ v0.26.1 同上
    u.phone = t
    u.phone_verified = 1
    db.commit()
    return {"ok": True, "phone": uc._mask(t)}


@router.post("/api/auth/bind-email")
def bind_email(body: BindIn, db: Session = Depends(get_db),
               user: User = Depends(auth.get_current_user)):
    from sqlalchemy import select as _sel
    t = (body.target or "").strip()
    if "@" not in t:
        raise HTTPException(400, "邮箱格式不正确")
    if db.scalar(_sel(User).where(User.email == t, User.id != user.id)) is not None:
        raise HTTPException(409, "该邮箱已被其他账号绑定")
    if not uc.verify_code(db, t, body.code, "bind"):
        raise HTTPException(400, "验证码错误或已过期")
    u = auth.rebind_user(db, user)          # ★ v0.26.1 同上
    u.email = t
    u.email_verified = 1
    db.commit()
    return {"ok": True, "email": uc._mask(t)}


# ================================================================ 用量与数据导出

@router.get("/api/me/usage")
def my_usage(db: Session = Depends(get_db),
             user: User = Depends(auth.get_current_user)):
    return uc.usage_stats(db, user)




@router.get("/api/me/export")
def export_data(db: Session = Depends(get_db),
                user: User = Depends(auth.get_current_user)):
    """★ 导出我的全部数据（个保法 45 条：查阅、复制权）。"""
    from urllib.parse import quote
    data, fname = uc.export_user_data(db, user)
    log.info("用户 %s 导出个人数据（%d bytes）", user.id, len(data))
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition":
                             f"attachment; filename*=UTF-8''{quote(fname)}",
                             "X-Export-Size": str(len(data))})


# ================================================================ 账号注销

class DeleteReqIn(BaseModel):
    reason: str = ""
    keep_works: int = 1          # 1=保留作品但匿名化；0=一并删除


@router.post("/api/me/delete-request")
def delete_request(body: DeleteReqIn, db: Session = Depends(get_db),
                   user: User = Depends(auth.get_current_user)):
    """★ 申请注销（个保法 47 条：删除权）。

    进入 7 天冷静期，期间可撤销。**不立即删除数据。**
    """
    try:
        return uc.request_deletion(db, user, reason=body.reason,
                                   keep_works=bool(body.keep_works))
    except uc.UserError as e:
        raise HTTPException(400, str(e))


@router.post("/api/me/delete-cancel")
def delete_cancel(db: Session = Depends(get_db),
                  user: User = Depends(auth.get_current_user)):
    try:
        return uc.cancel_deletion(db, user)
    except uc.UserError as e:
        raise HTTPException(400, str(e))


@router.get("/api/me/delete-status")
def delete_status(db: Session = Depends(get_db),
                  user: User = Depends(auth.get_current_user)):
    return uc.deletion_status(db, user)


# ================================================================ 管理端

@router.post("/api/admin/users/{user_id}/execute-deletion")
def admin_execute_deletion(user_id: str, db: Session = Depends(get_db),
                           admin: User = Depends(auth.require_admin)):
    """★ 管理员执行注销（冷静期到期后，或用户明确要求立即执行）。"""
    r = uc.execute_deletion(db, user_id)
    if not r.get("ok"):
        raise HTTPException(404, r.get("error") or "失败")
    return r


@router.get("/api/admin/deletions/pending")
def admin_pending_deletions(db: Session = Depends(get_db),
                            admin: User = Depends(auth.require_admin)):
    """待执行的注销（到期未执行的，供管理员/定时任务处理）。"""
    due = uc.pending_deletions_due(db)
    return {"due": due, "count": len(due)}


# ================================================================ ★ v0.20.6 A6-4 会话管理

@router.get("/api/me/sessions")
def list_sessions(request: Request, db: Session = Depends(get_db),
                  user: User = Depends(auth.get_current_user)):
    """登录设备列表（★ 用户自查异常登录的基础能力）。

    为什么必须给用户看：用户是发现"账号被盗"的第一人。
    没有这个页面，用户被盗后无从察觉。
    """
    from apps.api import tokens as _tk
    cur_ua = request.headers.get("user-agent", "")[:40]
    items = _tk.list_sessions(db, user.id)
    # 标出当前设备（UA 前缀匹配，够用）
    for it in items:
        it["current"] = bool(cur_ua and cur_ua[:12] in (it.get("ua") or ""))
    return {"items": items, "count": len(items)}


@router.post("/api/me/sessions/revoke")
def revoke_session(body: dict, db: Session = Depends(get_db),
                   user: User = Depends(auth.get_current_user)):
    """撤销指定会话（踢掉某个设备）。"""
    from apps.api import tokens as _tk
    fam = str((body or {}).get("family_id") or "").strip()
    if not fam:
        raise HTTPException(400, "缺少 family_id")
    # ★ 只能撤销自己的会话（越权防护）
    from apps.api.models import RefreshToken as _RT
    row = db.scalar(select(_RT).where(_RT.family_id == fam))
    if row is None:
        raise HTTPException(404, "会话不存在")
    if row.user_id != user.id:
        raise HTTPException(403, "无权操作该会话")
    n = _tk.revoke_family(db, fam, reason="用户手动退出设备")
    return {"ok": True, "revoked": n}


@router.post("/api/me/sessions/revoke-all")
def revoke_all_sessions(db: Session = Depends(get_db),
                        user: User = Depends(auth.get_current_user)):
    """退出所有设备（★ 怀疑被盗时的第一反应）。"""
    from apps.api import tokens as _tk
    n = _tk.revoke_user_tokens(db, user.id, reason="用户主动退出所有设备")
    return {"ok": True, "revoked": n,
            "note": "所有设备已登出，当前令牌将在 2 小时内自然过期"}


@router.get("/api/me/login-records")
def login_records(limit: int = 20, db: Session = Depends(get_db),
                  user: User = Depends(auth.get_current_user)):
    """登录记录（IP 已脱敏）。"""
    from apps.api import risk as _risk
    items = _risk.recent_logins(db, user.id, limit=min(max(limit, 1), 100))
    return {"items": items}


# ================================================================ ★ v0.20.6 A6-2/A6-6 管理端

@router.get("/api/admin/risk/overview")
def admin_risk_overview(hours: int = 24, db: Session = Depends(get_db),
                        admin: User = Depends(auth.require_admin)):
    """风控总览（后台看板）。"""
    from apps.api import ratelimit as _rl
    from apps.api import risk as _risk
    d = _risk.risk_overview(db, hours=hours)
    d["ratelimit"] = _rl.stats()
    return d


@router.get("/api/admin/risk/alerts")
def admin_risk_alerts(level: str = "", limit: int = 50,
                      db: Session = Depends(get_db),
                      admin: User = Depends(auth.require_admin)):
    """风控告警列表。"""
    from apps.api import risk as _risk
    return _risk.list_risk_alerts(db, level=level, limit=limit)


@router.get("/api/admin/moderation/stats")
def admin_moderation_stats(db: Session = Depends(get_db),
                           admin: User = Depends(auth.require_admin)):
    """内容审核器状态。"""
    from apps.api import moderation as _mod
    return _mod.stats()


class ModerationTestIn(BaseModel):
    text: str


@router.post("/api/admin/moderation/test")
def admin_moderation_test(body: ModerationTestIn, db: Session = Depends(get_db),
                          admin: User = Depends(auth.require_admin)):
    """★ 审核器试跑（运营调规则用，不落库）。

    为什么需要：词库调优必须能立刻看到效果，否则只能"改了等出事"。
    """
    from apps.api import moderation as _mod
    m = _mod.get_moderator()
    res = m.moderate(body.text or "", stage="test")
    return res.to_dict()

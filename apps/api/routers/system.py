# -*- coding: utf-8 -*-
"""系统路由（健康检查/账号/管理后台/模型/技能/设置）

由 v0.18 T1 从 main.py 拆出。main.py 通过 include_router 挂载本模块路由，
**同时 re-export 端点函数名** —— 所以 `from apps.api.main import health`
以及 `main.health(...)` 的老写法继续可用（测试大量依赖这一点）。
"""
from __future__ import annotations

import uuid
import datetime as dt

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api import admin_service as svc
from apps.api import auth, config, keyvault, llm
from apps.api import consistency
# ★★ 修复（v1.0-E4.9）：漏了这行 → `/api/skills` 全部 4 个端点 500
#
#   ★ 问题：`skills` 是**包**（apps/api/skills/），
#     `list_skills` / `load` / `save_override` / `reset_override`
#     都在**子模块 `loader`** 里。本文件直接写 `skills.list_skills()`
#     但**从没导入过 `skills`** → NameError → 500。
#
#   ★ 正确写法（其他 5 个文件都这么写）：
#     `from apps.api.skills import loader as skills`
#
#   ★ 怎么发现的：v1.0-E4.9 的**全端点自动冒烟**（1588 次请求）
#     扫出来的 —— 手写用例不可能覆盖到 383 个端点。
from apps.api.skills import loader as skills
from apps.api.db import SessionLocal, get_db
from apps.api.models import (
    Book, Chapter, Character, Foreshadow, ModelProfile, Run, Setting, User,
    StyleProfile, Volume, WorldSetting, jd, jl, now,
    AuditReport, LedgerEntry, TimelineEvent, Conversation, Message, Task,
    DramaProject, DramaEpisode, DramaAsset, DramaScene, GenrePack,
)
from apps.api.services.settings import (
    get_setting, set_setting, is_register_open, SETTING_DEFAULTS,
)

# v0.18 T1：共享符号统一放 services 层（不再依赖 main，避免循环 + NAME 解析失败）。
# ★ 踩坑记录：最初这里用 PEP 562 模块级 __getattr__ 代理 main.SETTING_DEFAULTS，
#   但 **__getattr__ 只对「模块属性访问」生效，对模块内函数体的全局查找
#   （LOAD_GLOBAL）无效** → 运行时报 NameError（线上 /api/auth/allow-register 500）。
#   正确解法是把符号真正搬到 services，而不是代理。
from apps.api.services.scoring import book_score as _book_score, next_step as _next_step
from apps.api.services.settings import SETTING_DEFAULTS

router = APIRouter()

class RegisterIn(BaseModel):
    username: str
    password: str
    email: str = ""
    # ★ v1.0-E4.11（合规/E1 备案）：注册必须同意协议。
    #   ★ 兼容策略：默认 True —— 老前端/脚本/测试客户端不传也不 422（零回归）；
    #     新前端必须传 False 才会被拒。合规的硬约束在前端勾选框上体现。
    agreed_terms: bool = True
    agreed_terms_version: str = ""

class LoginIn(BaseModel):
    username: str
    password: str

class ModelIn(BaseModel):
    name: str = ""
    provider: str = "openai-compatible"
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model_id: str = "deepseek-chat"
    context_window: int = 128000
    price_in: float = 2.0
    price_out: float = 8.0
    temperature: float = 0.85
    max_tokens: int = 8192
    role: str = "default"
    is_default: int = 0
    enabled: int = 1

class SkillPatch(BaseModel):
    body: str

@router.get("/api/health")
def health(deep: int = 0):
    """健康检查。

    * 默认（浅）：极轻量，只证明进程活着 —— 供负载均衡/高频探测。
    * `?deep=1`（深，v0.19 S5）：真查数据库可写、磁盘余量、日志目录、
      后台任务、备份新鲜度。运维巡检与告警用。
    """
    from apps.api import health as health_mod
    if deep:
        return health_mod.deep()
    return health_mod.shallow()

@router.post("/api/auth/register")
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)):
    from apps.api import risk as _risk

    _ip = _client_ip(request)
    _ua = request.headers.get("user-agent", "")

    if not is_register_open(db) and not auth.first_user_is_admin(db):
        raise HTTPException(403, "当前已关闭开放注册，请联系管理员")
    if not body.username or len(body.username) < 2:
        raise HTTPException(400, "用户名至少 2 个字符")
    if len(body.password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(409, "用户名已存在")

    # ★ v0.20.6 A6-2：注册风控 —— 同 IP 大量注册是工作室养号特征
    try:
        _hits = [h for h in _risk.assess_login(db, ip=_ip)
                 if h.rule == "R1" and h.level == "high"]
        if _hits and not auth.first_user_is_admin(db):
            _risk.write_risk_alert(db, hits=_hits, ip=_ip, action="register", ua=_ua)
            raise HTTPException(429, "该网络环境注册过于频繁，请稍后再试或联系客服")
    except HTTPException:
        raise
    except Exception as _e:                     # noqa: BLE001
        import logging
        logging.getLogger("novelforge").warning("注册风控异常（已放行）：%s", _e)

    # ★★ v1.0-E4.11（合规/E1 备案）：拒绝未同意协议的注册。
    #   显式传 agreed_terms=False 才算"未同意"（缺省 True 是老客户端兼容）。
    if body.agreed_terms is False:
        raise HTTPException(400, "必须同意《用户协议》与《隐私政策》后才能注册")

    role = "admin" if auth.first_user_is_admin(db) else "user"
    u = auth.create_user(db, body.username, body.password, role=role, email=body.email)

    # ★ v1.0-E4.11（合规）：记录协议同意时间戳（谁、何时、哪一版）。
    #   ★ 复用 models.now（带 tz 的 UTC）而不是自造 _now —— 时间口径全项目统一。
    try:
        from apps.api.models import now as _utcnow
        u.agreed_terms_at = _utcnow()
        u.agreed_terms_version = (body.agreed_terms_version or "v1").strip()[:32]
        db.commit()
    except Exception:                        # noqa: BLE001
        db.rollback()                        # 记录失败不阻断注册（fail-open，前端勾选已是主约束）


    # ★ v0.20.6 A6-2/A6-3：记录注册行为（风控分析的基础数据）
    try:
        _risk.record_login(db, user_id=u.id, ip=_ip, ua=_ua,
                           method="register", success=True, reason="注册")
    except Exception:
        pass

    # ★★ v1.0 E4 运营准备：注册欢迎通知（★ 运营第一触点）
    #   ★ 为什么在注册就发：新用户的**第一小时决定留存**。
    #     一封"三步上手"的站内信 + 邮件，比"欢迎使用"有用得多。
    #   ★ fail-open：发不出去绝不影响注册（用户能进得来最重要）。
    try:
        from apps.api import ops_touch
        ops_touch.on_welcome(db, u.id, username=u.username)
    except Exception as _e:                             # noqa: BLE001
        import logging
        logging.getLogger("novelforge").debug(
            "注册欢迎通知异常（已忽略）：%s: %s", type(_e).__name__, _e)

    _tok = _issue_tokens(db, u.id, _ip, _ua)
    return {"ok": True, "user": {"id": u.id, "username": u.username, "role": u.role},
            **_tok}


def _client_ip(request: Request) -> str:
    """取真实客户端 IP（★ 优先 CF-Connecting-IP，它不可被客户端伪造）。"""
    for k in ("cf-connecting-ip", "x-real-ip"):
        v = request.headers.get(k)
        if v:
            return v.strip()[:64]
    return (request.client.host if request.client else "")[:64]


def _mask_ip(ip: str) -> str:
    """★ IP 脱敏（通知里展示用）。

    为什么脱敏：邮件会转发、可能被他人看到，完整 IP 属个人信息。
    `203.0.113.45` → `203.0.113.*`；IPv6 只留前两段。
    """
    s = (ip or "").strip()
    if not s:
        return "未知"
    if ":" in s:                                  # IPv6
        parts = s.split(":")
        return ":".join(parts[:2]) + "::*"
    parts = s.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.{parts[2]}.*"
    return s[:8] + "*"


def _issue_tokens(db: Session, user_id: str, ip: str, ua: str) -> dict:
    """★ v0.20.6 A6-4：签发双令牌（access + refresh）。

    ★ 兼容性设计：同时返回 `token` 字段（= access_token），
      这样**旧前端无需改动**即可继续工作（渐进式升级，零破坏）。
    """
    try:
        from apps.api import tokens as _tk
        rt = _tk.issue_refresh_token(db, user_id, ip=ip, ua=ua)
        at = _tk.make_access_token(user_id)
        return {"token": at, "access_token": at, "access_ttl": _tk.ACCESS_TTL,
                "refresh_token": rt["token"],
                "refresh_expires_at": rt["expires_at"]}
    except Exception as e:                      # noqa: BLE001
        # ★ 令牌服务异常不能导致无法登录 —— 降级回单令牌
        import logging
        logging.getLogger("novelforge").error(
            "★ 双令牌签发失败，降级为单令牌：%s: %s", type(e).__name__, e)
        try:
            db.rollback()
        except Exception:
            pass
        return {"token": auth.make_token(user_id), "access_token": auth.make_token(user_id),
                "access_ttl": config.TOKEN_TTL, "refresh_token": ""}


@router.post("/api/auth/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    from apps.api import risk as _risk

    _ip = _client_ip(request)
    _ua = request.headers.get("user-agent", "")
    u = db.scalar(select(User).where(User.username == body.username))
    _ok = bool(u and u.is_active and auth.verify_password(body.password, u.pw_hash))

    if not _ok:
        # ★ v0.20.6 A6-3：失败的登录**必须留痕**（暴力破解的证据链）
        _risk.record_login(db, user_id=(u.id if u else ""), ip=_ip, ua=_ua,
                           method="password", success=False,
                           reason=("密码错误" if u else "账号不存在"))
        # 失败计数（★ 在 ratelimit 模块，用于"该 IP 近期失败次数"快速判断）
        try:
            from apps.api import ratelimit as _rl
            _rl.record_login_fail(_ip, body.username)
        except Exception:
            pass
        # ★ v0.20.6 A6-2：风险评估（失败也要评，攻击特征就是失败多）
        try:
            _hits = _risk.assess_login(db, ip=_ip, user_id=(u.id if u else ""), ua=_ua)
            if _hits:
                _risk.write_risk_alert(db, hits=_hits, ip=_ip,
                                       user_id=(u.id if u else ""),
                                       action="login_failed", ua=_ua)
        except Exception:
            pass
        raise HTTPException(401, "用户名或密码错误")

    # ★ 登录成功：写记录 + 风控评估
    _risk.record_login(db, user_id=u.id, ip=_ip, ua=_ua, method="password",
                       success=True, reason="密码登录")
    # ★ 登录成功 → 清零失败计数（否则正常用户输错几次后会被自己"锁住"）
    try:
        from apps.api import ratelimit as _rl2
        _rl2.reset_login_fail(_ip)
    except Exception:
        pass
    _risk_hits = []
    try:
        _risk_hits = _risk.assess_login(db, ip=_ip, user_id=u.id, ua=_ua)
        if _risk_hits:
            _risk.write_risk_alert(db, hits=_risk_hits, ip=_ip, user_id=u.id,
                                   action="login", ua=_ua)
    except Exception:
        pass

    # 更新最后登录信息（用户中心"登录设备"用）
    try:
        import datetime as _dt
        u.last_login_at = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        u.last_login_ip = _ip
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass

    _lv = _risk.max_level(_risk_hits) if _risk_hits else "none"

    # ★ v0.25 Xf-2：异常/新设备登录 → 安全通知（★ 不可关闭类型）。
    #   为什么只在中高风险时发：正常登录天天发会让用户忽略通知，
    #   那就等于安全告警失效了。这里只在"新 IP"或风险等级较高时发。
    try:
        _new_ip = any(getattr(h, "rule", "") in ("new_ip", "R4")
                      for h in (_risk_hits or []))
        if _lv in ("high", "critical") or _new_ip:
            from apps.api import notify as _nf
            import datetime as _dt2
            _nf.notify(db, user_id=u.id, event="security.new_login",
                       ctx={"time": _dt2.datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "ip": _mask_ip(_ip)},
                       dedup_key=f"login:{_ip}")
    except Exception:
        pass

    _tok = _issue_tokens(db, u.id, _ip, _ua)
    _perms = _user_perms(db, u.role)
    return {"ok": True, "user": {"id": u.id, "username": u.username, "role": u.role},
            # ★★ v0.25.5 修复：顶层**冗余** role 与 perms。
            #   为什么：v0.20.7 RBAC 改造后 role 被嵌进 user 子对象，
            #   而 web/admin/admin.js 还在读 `d.role` → 永远 undefined
            #   → 管理员被判为"不是管理员"，后台登录页死循环。
            #   ★ 正确做法是前端用 perms 判断（角色可自定义），
            #     这里同时给 role 是为兼容尚未升级的旧前端。
            "role": u.role,
            "perms": _perms,
            "is_admin": "admin.access" in _perms,
            "risk_level": _lv,
            "risk_notes": [h.message for h in _risk_hits][:3],
            **_tok}


def _user_perms(db: Session, role: str) -> list[str]:
    """展开角色的权限点（★ 前端据此判断，不靠 role 字符串比较）。

    ★ 为什么前端不该比 role 字符串：RBAC 下角色可自定义
      （运营可能建一个 `ops` 角色并授予 admin.access），
      比字符串会让新角色永远进不了后台。
    """
    try:
        from apps.api import rbac as _rb
        return _rb.expand(_rb.role_permissions(db, role))
    except Exception:                                # noqa: BLE001
        # 权限查询失败 → 退回旧规则（不能因 RBAC 故障锁死后台）
        return ["admin.access"] if role == "admin" else []


@router.post("/api/auth/refresh")
def refresh_token(body: dict, request: Request, db: Session = Depends(get_db)):
    """★ v0.20.6 A6-4：用 refresh_token 换新的令牌对。

    ★ 轮换 + 重放检测：旧令牌一次性作废；重复使用会吊销整族并强制重登。
    """
    from apps.api import tokens as _tk

    raw = str((body or {}).get("refresh_token") or "").strip()
    _ip = _client_ip(request)
    _ua = request.headers.get("user-agent", "")
    r = _tk.rotate_refresh_token(db, raw, ip=_ip, ua=_ua)
    if not r.get("ok"):
        # 400 让前端知道要重新登录（区别于 401 的"权限不足"）
        raise HTTPException(401, r.get("error") or "刷新失败")
    return r

@router.get("/api/auth/me")
def me(user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _perms = _user_perms(db, user.role)
    return {"id": user.id, "username": user.username, "role": user.role,
            "email": user.email,
            # ★ v0.25.5：后台刷新后靠这里恢复权限（否则 PERMS 为空 → 菜单全隐藏）
            "perms": _perms,
            "is_admin": "admin.access" in _perms,
            "site_name": get_setting(db, "site_name", SETTING_DEFAULTS["site_name"])}

@router.post("/api/auth/logout")
def logout(body: dict | None = None, db: Session = Depends(get_db)):
    """登出。

    ★ v0.20.6 A6-4：如果带了 refresh_token，**服务端撤销**该令牌家族。
      为什么必须服务端撤销：仅靠前端丢弃令牌，被偷的 refresh_token 仍能用 30 天。
    ★ 无 body 时保持旧行为（前端丢弃令牌即可），保证兼容。
    """
    raw = str(((body or {}) or {}).get("refresh_token") or "").strip()
    if raw:
        try:
            from apps.api import tokens as _tk
            from apps.api.models import RefreshToken as _RT
            th = _tk._hash_token(raw)
            row = db.scalar(select(_RT).where(_RT.token_hash == th))
            if row:
                n = _tk.revoke_family(db, row.family_id, reason="用户主动登出")
                return {"ok": True, "revoked": n}
        except Exception as e:                  # noqa: BLE001
            import logging
            logging.getLogger("novelforge").warning("登出撤销令牌失败：%s", e)
            try:
                db.rollback()
            except Exception:
                pass
    return {"ok": True, "revoked": 0}

@router.get("/api/auth/allow-register")
def allow_register(db: Session = Depends(get_db)):
    # 原来这里是 auth.first_user_is_admin(SessionLocal())，内联建 Session 从不关闭。
    # 这个接口前端 init() 每次刷新页面都会调，等于每次漏一个连接。
    # 同时把平台名/标语带出来：登录页与顶栏的文案要跟着系统设置走，不能写死。
    return {
        "allow": is_register_open(db),
        "first_user": auth.first_user_is_admin(db),
        "site_name": get_setting(db, "site_name", SETTING_DEFAULTS["site_name"]),
        "site_tagline": get_setting(db, "site_tagline", SETTING_DEFAULTS["site_tagline"]),
    }

@router.get("/api/stats/daily-writes")
def stats_daily_writes(days: int = 30,
                       db: Session = Depends(get_db),
                       user: User = Depends(auth.get_current_user)):
    """日更趋势（v1.0-E4.11，补 StatsView 的数据空面板）。

    口径：按章节 updated_at 的**日期**聚合新增字数（近似"每日写作产出"）。
    ★ 注意与 admin 侧数据隔离：只统计自己的书（admin 也不看全局——
      这是"我的写作统计"，不是平台运营报表）。
    返回裸数组 [{date, words, chapters}]（date 升序，缺的日子补 0）。
    """
    days = max(1, min(int(days or 30), 90))          # 限幅：最多看 90 天
    book_ids = [r[0] for r in db.execute(
        select(Book.id).where(Book.owner_id == user.id)).all()]
    if not book_ids:
        return []
    since = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=days)
    rows = db.execute(
        select(Chapter.updated_at, Chapter.words, Chapter.id)
        .where(Chapter.book_id.in_(book_ids), Chapter.updated_at >= since)
    ).all()
    # 按本地日期聚合（UTC naive → 直接取日期；站点单时区部署，口径简单可解释）
    bucket: dict[str, list[int]] = {}
    for upd, words, _cid in rows:
        d = upd.strftime("%Y-%m-%d") if upd else ""
        if not d:
            continue
        b = bucket.setdefault(d, [0, 0])
        b[0] += int(words or 0)
        b[1] += 1
    out = []
    cur = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
           - dt.timedelta(days=days - 1))
    for _ in range(days):
        key = cur.strftime("%Y-%m-%d")
        w, c = bucket.get(key, [0, 0])
        out.append({"date": key, "words": w, "chapters": c})
        cur += dt.timedelta(days=1)
    return out


@router.get("/api/dashboard")
def dashboard(db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    q = select(Book)
    if user.role != "admin":
        q = q.where(Book.owner_id == user.id)
    rows = db.scalars(q.order_by(Book.created_at.desc())).all()
    items = []
    for b in rows:
        n_ch = db.query(Chapter).filter(Chapter.book_id == b.id).count()
        words = sum(db.scalars(select(Chapter.words).where(Chapter.book_id == b.id)).all() or [])
        # 未设定目标时 progress 恒为 0，前端显示"待定"，不再拿 100 万冒充用户决策。
        progress = round(min(100, words / b.target_words * 100), 1) if b.target_words else 0
        items.append({
            "id": b.id, "title": b.title, "author": b.author, "genre": b.genre,
            "status": b.status, "chapters": n_ch, "words": words,
            "target_words": b.target_words, "progress": progress,
            "quality_score": _book_score(db, b),
            "next": _next_step(db, b, n_ch),
            # 这里以前直接拿 created_at 冒充 updated_at，改了章节仪表盘日期纹丝不动。
            # 取最新章节的 updated_at，没有章节才退回创建时间。
            "updated_at": (db.scalar(
                select(Chapter.updated_at).where(Chapter.book_id == b.id)
                .order_by(Chapter.updated_at.desc()).limit(1)
            ) or b.created_at).strftime("%Y-%m-%d %H:%M"),
        })
    return {"items": items, "total": len(items)}

@router.get("/api/admin/users")
def admin_list_users(db: Session = Depends(get_db), _: User = Depends(auth.require_admin)):
    rows = db.scalars(select(User).order_by(User.created_at)).all()
    return [{"id": u.id, "username": u.username, "role": u.role,
            "is_active": u.is_active, "email": u.email,
            "created_at": u.created_at.strftime("%Y-%m-%d")} for u in rows]

@router.post("/api/admin/users")
def admin_create_user(body: AdminUserIn, db: Session = Depends(get_db), _: User = Depends(auth.require_admin)):
    if len(body.username) < 2:
        raise HTTPException(400, "用户名至少 2 个字符")
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(409, "用户名已存在")
    u = auth.create_user(db, body.username, body.password or "changeme123",
                         role=body.role if body.role in ("user", "admin") else "user",
                         email=body.email)
    u.is_active = body.is_active
    db.commit()
    return {"ok": True, "id": u.id, "username": u.username, "role": u.role}

@router.patch("/api/admin/users/{uid}")
def admin_patch_user(uid: str, body: AdminUserIn, db: Session = Depends(get_db), _: User = Depends(auth.require_admin)):
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "用户不存在")
    if body.username:
        u.username = body.username
    if body.email is not None:
        u.email = body.email
    if body.role in ("user", "admin"):
        u.role = body.role
    if body.is_active in (0, 1):
        u.is_active = body.is_active
    if body.password:
        if len(body.password) < 6:
            raise HTTPException(400, "密码至少 6 位")
        u.pw_hash = auth.hash_password(body.password)
    db.commit()
    return {"ok": True}

@router.delete("/api/admin/users/{uid}")
def admin_delete_user(uid: str, body: "AdminDeleteIn", request: Request,
                      db: Session = Depends(get_db),
                      me: User = Depends(auth.require_admin)):
    """★ 强制删除用户（级联硬删全部归属数据 + 审计）。

    ★ 修复：原实现只删 User 行、不级联 → 留下孤儿书/章节/短剧/积分流水。
    现统一走 admin_service.delete_user_cascade（与清理脚本同口径）。
    会计凭证默认脱敏保留，hard_delete_vouchers=True 才真删。
    """
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() \
        or (request.client.host if request.client else "")
    r = svc.delete_user_cascade(db, user_id=uid, operator=me,
                                reason=body.reason, hard_delete_vouchers=body.hard_delete_vouchers,
                                ip=ip)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "删除失败"))
    return r

@router.get("/api/files/{path:path}")
def serve_book_file(path: str, db: Session = Depends(get_db)):
    import re as _re
    if not _re.fullmatch(r"[\w\-/]+\.[A-Za-z0-9]{1,8}", path) or ".." in path:
        raise HTTPException(404, "not found")
    # 路径形如 {book_id}/drama_media/xxx.png —— 第一段必须是真实书籍 id
    book_id = path.split("/", 1)[0]
    b = db.get(Book, book_id)
    if not b:
        raise HTTPException(404, "not found")
    from apps.api.config import BOOKS_DIR
    full = (BOOKS_DIR / path).resolve()
    if not str(full).startswith(str((BOOKS_DIR / book_id).resolve())) or not full.is_file():
        raise HTTPException(404, "not found")
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif", ".mp3": "audio/mpeg",
            ".wav": "audio/wav", ".mp4": "video/mp4", ".webm": "video/webm"}.get(
        full.suffix.lower(), "application/octet-stream")
    return Response(content=full.read_bytes(), media_type=mime)

@router.get("/api/models")
def list_models(db: Session = Depends(get_db), _: User = Depends(auth.require_admin)):
    rows = db.scalars(select(ModelProfile)).all()
    return [{"id": r.id, "name": r.name, "provider": r.provider, "base_url": r.base_url,
             "model_id": r.model_id, "role": r.role, "is_default": r.is_default,
             "enabled": r.enabled, "price_in": r.price_in, "price_out": r.price_out,
             "temperature": r.temperature, "max_tokens": r.max_tokens,
             "has_key": bool(r.api_key)} for r in rows]   # 只报有无，永不回传 Key 明文/密文

@router.post("/api/models")
def create_model(body: ModelIn, db: Session = Depends(get_db), _: User = Depends(auth.require_admin)):
    r = ModelProfile(id=f"md_{uuid.uuid4().hex[:12]}", **body.model_dump())
    keyvault.set_api_key(r, body.api_key or "")   # Key 加密入库，不落明文
    if r.is_default:
        for x in db.scalars(select(ModelProfile)).all():
            x.is_default = 0
    db.add(r)
    db.commit()
    return {"id": r.id}

@router.put("/api/models/{mid}")
def update_model(mid: str, body: ModelIn, db: Session = Depends(get_db), _: User = Depends(auth.require_admin)):
    r = db.get(ModelProfile, mid)
    if not r:
        raise HTTPException(404, "model not found")
    # 只更新调用方真正传出的字段，避免“设为默认”这类部分更新把 name/base_url/api_key 清成默认值
    dump = body.model_dump(exclude_unset=True)
    if "api_key" in dump:
        keyvault.set_api_key(r, dump.pop("api_key") or "")   # 空 = 清除；留空不传则不动 Key
    for k, v in dump.items():
        setattr(r, k, v)
    if r.is_default:
        for x in db.scalars(select(ModelProfile)).all():
            if x.id != r.id:
                x.is_default = 0
    db.commit()
    return {"ok": True}

@router.delete("/api/models/{mid}")
def delete_model(mid: str, db: Session = Depends(get_db), _: User = Depends(auth.require_admin)):
    r = db.get(ModelProfile, mid)
    if r:
        db.delete(r)
        db.commit()
    return {"ok": True}

@router.post("/api/models/{mid}/ping")
async def ping_model(mid: str, db: Session = Depends(get_db), _: User = Depends(auth.require_admin)):
    r = db.get(ModelProfile, mid)
    if not r:
        raise HTTPException(404, "model not found")
    return await llm.ping(r)

@router.get("/api/skills")
def list_skills(db: Session = Depends(get_db), _: User = Depends(auth.require_admin)):
    return [{"id": s.id, "name": s.name, "description": s.description,
             "version": s.version, "model_role": s.model_role, "source": s.source}
            for s in skills.list_skills(db)]

@router.get("/api/skills/{sid}")
def get_skill(sid: str, db: Session = Depends(get_db), _: User = Depends(auth.require_admin)):
    s = skills.load(sid, db)
    if not s:
        raise HTTPException(404, "skill not found")
    return {"id": s.id, "name": s.name, "body": s.body, "source": s.source,
            "variables": s.variables, "model_role": s.model_role}

@router.put("/api/skills/{sid}")
def save_skill(sid: str, patch: SkillPatch, db: Session = Depends(get_db), _: User = Depends(auth.require_admin)):
    skills.save_override(sid, patch.body, db)
    return {"ok": True}

@router.post("/api/skills/{sid}/reset")
def reset_skill(sid: str, db: Session = Depends(get_db), _: User = Depends(auth.require_admin)):
    skills.reset_override(sid, db)
    return {"ok": True}

@router.get("/api/settings")
def get_settings(db: Session = Depends(get_db), _: User = Depends(auth.require_admin)):
    """返回全部系统设置。未配置过的键也带上默认值，
    否则前端拿到空对象，管理员看不到"关掉注册"之外还有哪些可调项。"""
    out = {k: get_setting(db, k, v) for k, v in SETTING_DEFAULTS.items()}
    out.update({r.key: r.value for r in db.scalars(select(Setting)).all()})
    return out

@router.post("/api/settings")
def set_settings(payload: dict[str, Any], db: Session = Depends(get_db), _: User = Depends(auth.require_admin)):
    # 只接受白名单内的键。不校验的话手滑提交一个错键名，
    # 设置就静默存进去了，界面上看不到、也永远不生效，很难排查。
    unknown = [k for k in payload if k not in SETTING_DEFAULTS]
    if unknown:
        raise HTTPException(400, f"不支持的设置项：{', '.join(sorted(unknown))}")
    for k, v in payload.items():
        row = db.get(Setting, k)
        if row is None:
            db.add(Setting(key=k, value=str(v)))
        else:
            row.value = str(v)
    db.commit()
    # ★★ v1.0 E2：站点设置写后清缓存（否则前台要等 TTL 才看到新标语）
    #   ★ 用 try 包住：缓存清理失败绝不能影响"设置已保存"这个事实
    try:
        from apps.api.routers.ads_models import invalidate_site_cache
        invalidate_site_cache()
    except Exception:                                       # noqa: BLE001
        pass
    return {"ok": True, "saved": sorted(payload)}


class AdminUserIn(BaseModel):
    username: str
    password: str = ""
    email: str = ""
    role: str = "user"
    is_active: int = 1


class AdminDeleteIn(BaseModel):
    reason: str = ""                  # 强制原因（审计）
    hard_delete_vouchers: bool = False  # True=连订单/发票一并硬删（高危）

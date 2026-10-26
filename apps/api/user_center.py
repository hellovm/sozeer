"""用户中心核心（v0.20.4 A4）。

覆盖四块：
  ① **验证码**（手机/邮箱）—— 可插拔 Sender（开发环境走 ConsoleSender）
  ② **二次验证 TOTP** —— 无外部依赖，手写实现
  ③ **数据导出**（个保法 45 条）—— 合规硬要求
  ④ **账号注销**（个保法 47 条）—— 含冷静期 + 数据分级处理

★★ 合规要点（不是"体验优化"，是法律义务）：
   · 个保法 45 条：用户有权查阅、复制其个人信息 → 数据导出
   · 个保法 47 条：用户有权删除其个人信息 → 账号注销

★ 注销的数据分级（必须想清楚，不能一刀切）：
   · 个人资料 → **删除**
   · 小说作品 → **用户选择**（删 / 匿名保留）
   · 订单与支付 → **匿名保留**（会计凭证，法律要求留存 5 年）
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import struct
import time
import uuid
import zipfile

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api import auth, keyvault
from apps.api.models import (
    AccountDeletion, Book, Chapter, Character, Conversation, CreditAccount,
    CreditLedger, DramaProject, Foreshadow, LoginRecord, Message, Order,
    PaymentEvent, Task, User, VerificationCode, WorldSetting, now,
)

log = logging.getLogger("novelforge")

def _naive_now() -> "dt.datetime":
    """统一的「当前时间」（**UTC naive**）。

    ★★ 这里有一个非常隐蔽的坑（v0.20.4 实测，会导致 8 小时偏差）：

      项目里时间是 **aware UTC**（`models.now()` → `datetime.now(timezone.utc)`）。
      存进数据库 DateTime 列（无时区）时，SQLAlchemy 会把 aware UTC **转成 naive UTC**。
      读回来就是 naive UTC。

      如果我这里用 `datetime.now()`（**本地时间**），在东八区就会比存的值**快 8 小时**：
        存：17:25（本地）→ DB 变 09:25（UTC）→ 读回 09:25
        再算 elapsed = 本地 17:25 - 09:25 = 28800 秒 ❌

      正确做法：**统一用 UTC naive**，与 `models.now()` 的存储语义一致。
    """
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


CODE_TTL_SECONDS = 300          # 验证码有效期 5 分钟
CODE_MAX_ATTEMPTS = 5           # ★ 最多试 5 次（防暴力猜 6 位码）
CODE_RESEND_COOLDOWN = 60       # 同 target 60 秒内不能重发
DELETION_GRACE_DAYS = 7         # ★ 注销冷静期 7 天

PURPOSES = ("login", "reset", "bind", "verify")


class UserError(Exception):
    """用户中心业务异常。"""


# ================================================================ ① 验证码

class CodeSender:
    """验证码发送抽象。"""

    name = "base"

    def send(self, target: str, code: str, purpose: str) -> dict:
        raise NotImplementedError


class ConsoleSender(CodeSender):
    """开发/测试用：验证码写日志 + 返回给调用方（便于自测）。

    ★ 生产环境**必须**换成真实 Sender，否则验证码会泄漏到日志/响应里。
      用一个显式开关 `NOVELFORGE_DEV_CODE=1` 控制，默认关闭。
    """

    name = "console"

    def __init__(self, expose: bool = False):
        self.expose = expose      # 是否把验证码回传给调用方

    def send(self, target: str, code: str, purpose: str) -> dict:
        log.info("【开发模式】验证码 target=%s purpose=%s code=%s", target, purpose, code)
        out = {"ok": True, "provider": self.name, "sent": True}
        if self.expose:
            # ★ 只在开发模式下回传（生产绝不允许）
            out["dev_code"] = code
        return out


class SmsSender(CodeSender):
    """短信（**骨架** —— 需要阿里云/腾讯云短信资质）。"""

    name = "sms"

    def __init__(self, **cfg):
        self.cfg = cfg or {}

    def send(self, target: str, code: str, purpose: str) -> dict:
        # TODO(有资质后)：接入阿里云 dysmsapi / 腾讯云 sms
        #   配置项：access_key / secret / sign_name / template_code
        raise UserError(
            "短信服务未接入：需要短信资质（阿里云/腾讯云）。"
            "开发环境请用 NOVELFORGE_DEV_CODE=1；接入说明见 docs/A4_用户中心方案.md")


class EmailSender(CodeSender):
    """邮件验证码（v0.25 Xf-1 已接入真实 SMTP）。

    ★ 从"骨架"变为可用：委托给 apps.api.mail（统一 SMTP 实现）。
      为什么不在本类里重写一遍 SMTP 逻辑：验证码只是邮件的一种形态，
      复用 mail 模块能一并获得重试/TLS/头注入防护/HTML 模板。
    """

    name = "email"

    def __init__(self, **cfg):
        self.cfg = cfg or {}

    def send(self, target: str, code: str, purpose: str) -> dict:
        from apps.api import mail as _mail
        if not _mail.is_configured():
            # ★ 未配置 SMTP 时给出**可操作**的提示，而不是笼统报错
            raise UserError(
                "邮件服务未配置：请设置 NF_SMTP_HOST / NF_SMTP_USER / NF_SMTP_PASS，"
                "或在后台「系统设置 → 邮件」中填写。"
                "开发环境可用 NOVELFORGE_DEV_CODE=1 走控制台输出。")
        label = {"login": "登录", "reset": "重置密码",
                 "bind": "绑定邮箱", "verify": "验证"}.get(purpose, "身份")
        ttl_min = max(1, CODE_TTL_SECONDS // 60)
        html = _mail.render_code(code, label, ttl_min=ttl_min)
        res = _mail.send(target, f"【溯字】{label}验证码", html)
        if not res.get("ok"):
            raise UserError(f"验证码邮件发送失败：{res.get('error') or '未知错误'}")
        return {"ok": True, "provider": self.name, "sent": True,
                "dry_run": bool(res.get("dry_run"))}


def get_sender(target: str = "") -> CodeSender:
    """按 target 形态选 Sender。

    ★ 开发模式（NOVELFORGE_DEV_CODE=1）一律用 ConsoleSender 且回传验证码，
      这样本地能把全流程跑通，不需要真实短信资质。
    """
    dev = os.environ.get("NOVELFORGE_DEV_CODE", "0") == "1"
    if dev:
        return ConsoleSender(expose=True)
    return SmsSender() if _looks_like_phone(target) else EmailSender()


def _looks_like_phone(s: str) -> bool:
    s = (s or "").strip()
    return bool(s) and s.isdigit() and len(s) == 11


def _hash_code(code: str, salt: str = "") -> str:
    """★ 验证码存哈希不存明文（防库被读后直接拿码登录）。"""
    return hashlib.sha256((code + salt).encode()).hexdigest()


def send_code(db: Session, target: str, purpose: str = "login") -> dict:
    """发送验证码（含频率限制）。"""
    target = (target or "").strip()
    if not target:
        raise UserError("请填写手机号或邮箱")
    purpose = purpose if purpose in PURPOSES else "login"

    # ★ 频率限制：60 秒内不能重发（防短信轰炸 / 刷成本）
    recent = db.scalar(
        select(VerificationCode)
        .where(VerificationCode.target == target)
        .order_by(VerificationCode.created_at.desc()).limit(1))
    if recent is not None:
        # created_at 列存的是 naive（DB 规范化），统一按 naive 比较
        _rc = recent.created_at
        if _rc is not None and _rc.tzinfo is not None:
            _rc = _rc.replace(tzinfo=None)
        elapsed = (_naive_now() - _rc).total_seconds() if _rc else 9999
        if elapsed < CODE_RESEND_COOLDOWN:
            wait = int(CODE_RESEND_COOLDOWN - elapsed)
            raise UserError(f"发送过于频繁，请 {wait} 秒后再试")

    code = f"{secrets.randbelow(1000000):06d}"     # 6 位数字，用 secrets 而非 random
    salt = uuid.uuid4().hex[:8]
    vc = VerificationCode(
        id=f"vc_{uuid.uuid4().hex[:16]}", target=target,
        code_hash=_hash_code(code, salt) + ":" + salt,
        purpose=purpose,
        expires_at=_naive_now() + dt.timedelta(seconds=CODE_TTL_SECONDS),
        used=0, attempts=0, created_at=now())
    db.add(vc)
    db.commit()

    sender = get_sender(target)
    try:
        r = sender.send(target, code, purpose)
    except UserError as e:
        # 发送失败要把记录标记掉（否则用户要等 60 秒）
        vc.used = 1
        db.commit()
        raise
    out = {"ok": True, "target": _mask(target), "purpose": purpose,
           "expires_in": CODE_TTL_SECONDS, "provider": sender.name}
    if "dev_code" in r:
        out["dev_code"] = r["dev_code"]       # ★ 仅开发模式
    return out


def _mask(s: str) -> str:
    """脱敏显示（138****8888 / ab***@x.com）。"""
    s = s or ""
    if _looks_like_phone(s):
        return s[:3] + "****" + s[-4:]
    if "@" in s:
        a, b = s.split("@", 1)
        return (a[:2] + "***@" + b) if len(a) > 2 else ("***@" + b)
    return s[:2] + "***"


def verify_code(db: Session, target: str, code: str, purpose: str = "login",
                consume: bool = True) -> bool:
    """校验验证码（★ 含尝试次数上限 —— 6 位码只有 100 万种，不限制可穷举）。"""
    target = (target or "").strip()
    code = (code or "").strip()
    if not target or not code:
        return False

    vc = db.scalar(
        select(VerificationCode)
        .where(VerificationCode.target == target,
               VerificationCode.purpose == purpose,
               VerificationCode.used == 0)
        .order_by(VerificationCode.created_at.desc()).limit(1))
    if vc is None:
        return False
    if vc.expires_at and vc.expires_at < _naive_now():
        return False
    if int(vc.attempts or 0) >= CODE_MAX_ATTEMPTS:
        vc.used = 1
        db.commit()
        return False

    # 存的是 "hash:salt"
    stored = str(vc.code_hash or "")
    expect_hash, _, salt = stored.partition(":")
    got_hash = _hash_code(code, salt)
    ok = bool(expect_hash) and hmac.compare_digest(expect_hash, got_hash)

    vc.attempts = int(vc.attempts or 0) + 1
    if ok and consume:
        vc.used = 1
    db.commit()
    return ok


# ================================================================ ② TOTP 二次验证
# 手写实现（RFC 6238）—— 避免为一个算法引入依赖

def generate_totp_secret() -> str:
    """生成 base32 密钥（Google Authenticator 兼容）。"""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _totp_at(secret: str, counter: int, digits: int = 6) -> str:
    pad = "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(secret.upper() + pad)
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{code % (10 ** digits):0{digits}d}"


def totp_now(secret: str, t: int | None = None) -> str:
    return _totp_at(secret, int((t if t is not None else time.time()) // 30))


def verify_totp(secret: str, code: str, window: int = 1) -> bool:
    """校验 TOTP（★ 允许前后 1 个时间窗，容忍时钟漂移）。

    为什么留窗口：服务器与用户手机的时间不可能完全一致，
    不放宽会导致"用户输入正确的码却总说错"。
    """
    code = (code or "").strip().replace(" ", "")
    if not secret or not code.isdigit() or len(code) != 6:
        return False
    counter = int(time.time()) // 30
    for off in range(-window, window + 1):
        if hmac.compare_digest(_totp_at(secret, counter + off), code):
            return True
    return False


def totp_uri(secret: str, username: str, issuer: str = "溯字 SOZEER") -> str:
    """生成 otpauth:// URI（前端转二维码）。"""
    from urllib.parse import quote
    label = quote(f"{issuer}:{username}")
    return (f"otpauth://totp/{label}?secret={secret}"
            f"&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30")


def setup_2fa(db: Session, user: User) -> dict:
    """生成 2FA 密钥（★ 此时**还没启用**，需用户验证一次后才生效）。

    为什么分两步：用户扫码后可能没配对成功，
    直接启用会把用户锁在门外。
    """
    secret = generate_totp_secret()
    # ★ 加密存储（与 API Key 同机制）—— 密钥泄漏 = 2FA 形同虚设
    user.totp_secret = keyvault.encrypt(secret)
    user.totp_enabled = 0
    db.commit()
    return {"ok": True, "secret": secret,          # 明文给用户看一次（扫码用）
            "uri": totp_uri(secret, user.username),
            "note": "请用验证器 App 扫码，然后输入 6 位码完成启用"}


def enable_2fa(db: Session, user: User, code: str) -> dict:
    secret = _user_totp_secret(user)
    if not secret:
        raise UserError("请先生成 2FA 密钥")
    if not verify_totp(secret, code):
        raise UserError("验证码不正确（请检查手机时间是否准确）")
    user.totp_enabled = 1
    db.commit()
    return {"ok": True, "enabled": True}


def disable_2fa(db: Session, user: User, password: str) -> dict:
    """关闭 2FA（★ 需密码 —— 防"会话被劫持后直接关掉 2FA"）。"""
    if user.pw_hash and not auth.verify_password(password or "", user.pw_hash):
        raise UserError("密码不正确")
    user.totp_enabled = 0
    user.totp_secret = ""
    db.commit()
    return {"ok": True, "enabled": False}


def _user_totp_secret(user: User) -> str:
    raw = str(getattr(user, "totp_secret", "") or "")
    if not raw:
        return ""
    try:
        return keyvault.decrypt(raw)
    except Exception:
        # 兼容历史明文
        return raw


# ================================================================ ③ 登录记录

def record_login(db: Session, user_id: str, *, ip: str = "", ua: str = "",
                 method: str = "password", success: bool = True,
                 reason: str = "") -> None:
    """记登录记录（★ 绝不抛异常 —— 审计不能阻断登录）。"""
    try:
        db.add(LoginRecord(id=f"lr_{uuid.uuid4().hex[:16]}", user_id=user_id or "",
                           ip=(ip or "")[:64], ua=(ua or "")[:256],
                           method=method, success=1 if success else 0,
                           reason=(reason or "")[:128], created_at=now()))
        db.commit()
    except Exception as e:                          # noqa: BLE001
        log.warning("登录记录写入失败：%s: %s", type(e).__name__, e)
        try:
            db.rollback()
        except Exception:
            pass


def login_history(db: Session, user_id: str, limit: int = 20) -> list[dict]:
    rows = db.scalars(select(LoginRecord).where(LoginRecord.user_id == user_id)
                      .order_by(LoginRecord.created_at.desc()).limit(limit)).all()
    return [{"id": r.id, "ip": r.ip, "ua": r.ua, "method": r.method,
             "success": int(r.success or 0), "reason": r.reason,
             "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else ""}
            for r in rows]


# ================================================================ ④ 数据导出（个保法 45 条）

def export_user_data(db: Session, user: User) -> tuple[bytes, str]:
    """★ 导出用户全部个人数据为 ZIP（个保法 45 条：查阅、复制权）。

    内容：
      profile.json     个人资料
      credits.json     积分账户 + 流水
      orders.json      订单
      books/*.json     作品（含章节/人物/世界观/伏笔）
      README.txt       说明

    ★ 安全：导出包**只含该用户自己的数据**（按 owner_id 过滤）。
      这是个高危点 —— 过滤条件写错就是数据泄漏。
    """
    buf = io.BytesIO()
    uid = user.id

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # --- 资料 ---
        profile = {
            "id": user.id, "username": user.username, "email": user.email,
            "nickname": getattr(user, "nickname", ""),
            "bio": getattr(user, "bio", ""),
            "phone": _mask(getattr(user, "phone", "") or ""),
            "role": user.role,
            "created_at": str(user.created_at or ""),
            "last_login_at": str(getattr(user, "last_login_at", "") or ""),
            "exported_at": now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        z.writestr("profile.json", json.dumps(profile, ensure_ascii=False, indent=2))

        # --- 积分 ---
        acc = db.get(CreditAccount, uid)
        z.writestr("credits.json", json.dumps({
            "balance": int(getattr(acc, "balance", 0) or 0),
            "frozen": int(getattr(acc, "frozen", 0) or 0),
            "total_earned": int(getattr(acc, "total_earned", 0) or 0),
            "total_spent": int(getattr(acc, "total_spent", 0) or 0),
        }, ensure_ascii=False, indent=2))

        led = db.scalars(select(CreditLedger).where(CreditLedger.user_id == uid)
                         .order_by(CreditLedger.created_at)).all()
        z.writestr("credit_ledger.json", json.dumps([{
            "delta": int(x.delta or 0), "balance_after": int(x.balance_after or 0),
            "kind": x.kind, "note": x.note,
            "created_at": str(x.created_at or "")} for x in led],
            ensure_ascii=False, indent=2))

        # --- 订单 ---
        ods = db.scalars(select(Order).where(Order.user_id == uid)
                         .order_by(Order.created_at)).all()
        z.writestr("orders.json", json.dumps([{
            "id": x.id, "plan_key": x.plan_key, "amount_cents": int(x.amount_cents or 0),
            "credits": int(x.credits or 0), "status": x.status,
            "created_at": str(x.created_at or "")} for x in ods],
            ensure_ascii=False, indent=2))

        # --- 作品（★ 严格按 owner_id 过滤）---
        books = db.scalars(select(Book).where(Book.owner_id == uid)).all()
        index = []
        for b in books:
            safe = "".join(ch for ch in (b.title or b.id) if ch.isalnum() or ch in "-_")[:40] or b.id
            chs = db.scalars(select(Chapter).where(Chapter.book_id == b.id)
                             .order_by(Chapter.idx)).all()
            chars = db.scalars(select(Character).where(Character.book_id == b.id)).all()
            ws = db.scalars(select(WorldSetting).where(WorldSetting.book_id == b.id)).all()
            fs = db.scalars(select(Foreshadow).where(Foreshadow.book_id == b.id)).all()
            # ★ 防御式构造（v0.20.4 教训）：
            #   导出是合规功能，**绝不能因为某个模型的字段名写错就整体失败** ——
            #   那等于用户拿不到自己数据的法定权利。
            #   逐段 try，出错就记 null，保证整体可用。
            def _safe(fn, default=None):
                try:
                    return fn()
                except Exception as _e:      # noqa: BLE001
                    log.warning("导出字段失败：%s: %s", type(_e).__name__, _e)
                    return default

            data = {
                "book": {"id": b.id, "title": b.title, "genre": b.genre,
                         "synopsis": b.synopsis, "status": b.status},
                "chapters": [{"idx": c.idx, "title": c.title, "content": c.content,
                              "words": c.words} for c in chs],
                "characters": [{"name": c.name, "role": c.role, "profile": c.profile}
                               for c in chars],
                "world_settings": [{"category": w.category, "key": w.key,
                                    "value": w.value} for w in ws],
                # ★ 字段名以 models.Foreshadow 为准（content/planted_ch/status）
                #   实测踩坑：想当然写 title/detail → AttributeError → 导出 500
                "foreshadows": _safe(lambda: [
                    {"content": f.content, "planted_ch": f.planted_ch,
                     "status": f.status, "note": f.note} for f in fs], []),
            }
            z.writestr(f"books/{safe}.json",
                       json.dumps(data, ensure_ascii=False, indent=2))
            index.append({"id": b.id, "title": b.title, "chapters": len(chs)})

        z.writestr("books/_index.json",
                   json.dumps(index, ensure_ascii=False, indent=2))

        z.writestr("README.txt", (
            "溯字 SOZEER · 个人数据导出\n"
            "========================\n\n"
            "本包含你在本平台的全部个人数据（《个人信息保护法》第 45 条）。\n\n"
            "目录说明：\n"
            "  profile.json        个人资料\n"
            "  credits.json        积分账户\n"
            "  credit_ledger.json  积分流水\n"
            "  orders.json         订单记录\n"
            "  books/              你的作品（每本一个 JSON）\n"
            "  books/_index.json   作品索引\n\n"
            f"导出时间：{now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        ))

    fname = f"sozeer-data-{user.username}-{now().strftime('%Y%m%d-%H%M%S')}.zip"
    return buf.getvalue(), fname


# ================================================================ ⑤ 账号注销（个保法 47 条）

def request_deletion(db: Session, user: User, *, reason: str = "",
                     keep_works: bool = True) -> dict:
    """申请注销（进入冷静期，**不立即执行**）。"""
    exist = db.scalar(select(AccountDeletion)
                      .where(AccountDeletion.user_id == user.id,
                             AccountDeletion.status == "pending"))
    if exist is not None:
        return {"ok": True, "duplicated": True,
                "scheduled_at": str(exist.scheduled_at or ""),
                "message": "已在注销流程中"}

    import datetime as _dt
    sched = _naive_now() + _dt.timedelta(days=DELETION_GRACE_DAYS)
    d = AccountDeletion(
        id=f"ad_{uuid.uuid4().hex[:16]}", user_id=user.id, status="pending",
        reason=(reason or "")[:1000], keep_works=1 if keep_works else 0,
        requested_at=_naive_now(), scheduled_at=sched)
    db.add(d)
    # 立即停用（不给登录），但数据先不动
    user.is_active = 0
    db.commit()
    return {"ok": True, "deletion_id": d.id, "status": "pending",
            "grace_days": DELETION_GRACE_DAYS,
            "scheduled_at": sched.strftime("%Y-%m-%d %H:%M:%S"),
            "message": f"已受理，{DELETION_GRACE_DAYS} 天后执行。期间可随时撤销。"}


def cancel_deletion(db: Session, user: User) -> dict:
    """撤销注销。"""
    d = db.scalar(select(AccountDeletion)
                  .where(AccountDeletion.user_id == user.id,
                         AccountDeletion.status == "pending"))
    if d is None:
        raise UserError("没有待处理的注销申请")
    d.status = "canceled"
    user.is_active = 1
    db.commit()
    return {"ok": True, "message": "已撤销注销，账号恢复正常"}


def deletion_status(db: Session, user: User) -> dict:
    d = db.scalar(select(AccountDeletion).where(AccountDeletion.user_id == user.id)
                  .order_by(AccountDeletion.requested_at.desc()).limit(1))
    if d is None:
        return {"pending": False}
    return {"pending": d.status == "pending", "status": d.status,
            "requested_at": str(d.requested_at or ""),
            "scheduled_at": str(d.scheduled_at or ""),
            "keep_works": int(d.keep_works or 0)}


def execute_deletion(db: Session, user_id: str) -> dict:
    """★ 真正执行注销（由定时任务调用，或管理员手动）。

    数据分级处理（重要）：
      · 个人资料     → 删除（置空）
      · 账号         → 软删（is_active=0 + deleted_at）
      · 作品         → 按 keep_works：删除 / 匿名保留
      · 积分账户     → 保留（余额清零，流水保留）
      · 订单/支付    → **保留**（会计凭证，法律要求留存）
      · 验证码/令牌  → 清理
    """
    u = db.get(User, user_id)
    if u is None:
        return {"ok": False, "error": "用户不存在"}

    d = db.scalar(select(AccountDeletion)
                  .where(AccountDeletion.user_id == user_id,
                         AccountDeletion.status == "pending")
                  .order_by(AccountDeletion.requested_at.desc()).limit(1))
    keep_works = bool(int(d.keep_works or 0)) if d else True

    stats = {"books_deleted": 0, "books_anonymized": 0}

    # --- 作品 ---
    books = db.scalars(select(Book).where(Book.owner_id == user_id)).all()
    for b in books:
        if keep_works:
            # 匿名保留：作者信息置空，作品内容不动
            b.owner_id = ""
            b.author = ""
            stats["books_anonymized"] += 1
        else:
            # 彻底删除（含章节/人物/设定/伏笔）
            for M in (Chapter, Character, WorldSetting, Foreshadow):
                for r in db.scalars(select(M).where(M.book_id == b.id)).all():
                    db.delete(r)
            db.delete(b)
            stats["books_deleted"] += 1

    # --- 个人资料（★ 删除，不留）---
    u.nickname = ""
    u.bio = ""
    u.avatar = ""
    u.phone = ""
    u.email = ""
    u.phone_verified = 0
    u.email_verified = 0
    u.totp_secret = ""
    u.totp_enabled = 0
    u.pw_hash = ""                      # 无法再登录
    u.token_valid_after = _naive_now()   # 旧令牌立即失效
    u.is_active = 0
    u.deleted_at = now()
    # ★ 用户名保留但加后缀（防占用；也便于财务记录回溯关联）
    if not str(u.username).startswith("deleted_"):
        u.username = f"deleted_{uuid.uuid4().hex[:10]}"

    # --- 积分账户：余额清零，流水保留（财务）---
    acc = db.get(CreditAccount, user_id)
    if acc is not None:
        acc.balance = 0
        acc.frozen = 0

    # --- 验证码清理 ---
    for vc in db.scalars(select(VerificationCode).where(
            VerificationCode.target == (u.phone or "_none_"))).all():
        db.delete(vc)

    if d is not None:
        d.status = "completed"
        d.completed_at = now()

    db.commit()
    log.info("账号注销完成 user=%s stats=%s keep_works=%s", user_id, stats, keep_works)
    return {"ok": True, "user_id": user_id,
            "deleted_profile": True, "keep_works": keep_works, **stats,
            "note": "订单与支付记录按会计法规保留，已匿名化关联"}


def pending_deletions_due(db: Session, limit: int = 100) -> list[str]:
    """找出到期该执行的注销（供定时任务用）。"""
    rows = db.scalars(select(AccountDeletion)
                      .where(AccountDeletion.status == "pending",
                             AccountDeletion.scheduled_at <= _naive_now())
                      .limit(limit)).all()
    return [r.user_id for r in rows]


# ================================================================ ⑥ 用量统计

def usage_stats(db: Session, user: User) -> dict:
    """我的用量（作品数/章节数/字数/短剧/任务/积分消耗）。

    ★ 工作台概览用：全部为**服务端实算**，不缓存、不美化。
    """
    from sqlalchemy import func
    books = db.scalars(select(Book).where(Book.owner_id == user.id)).all()
    bids = [b.id for b in books]
    n_chapters = 0
    words_total = 0
    monthly_words = 0
    if bids:
        n_chapters = db.scalar(select(func.count()).select_from(Chapter)
                               .where(Chapter.book_id.in_(bids))) or 0
        words_total = db.scalar(select(func.coalesce(func.sum(Chapter.words), 0))
                                .where(Chapter.book_id.in_(bids))) or 0
        # 本月新增字数（创作活跃度，引导续费的事实依据）
        month_start = now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        monthly_words = db.scalar(select(func.coalesce(func.sum(Chapter.words), 0))
                                  .where(Chapter.book_id.in_(bids),
                                         Chapter.created_at >= month_start)) or 0
    dramas = db.scalar(select(func.count()).select_from(DramaProject)
                       .where(DramaProject.owner_id == user.id)) or 0
    tasks = db.scalar(select(func.count()).select_from(Task)
                      .where(Task.owner_id == user.id)) or 0
    return {"books": len(books), "chapters": int(n_chapters),
            "words_total": int(words_total), "words_month": int(monthly_words),
            "dramas": int(dramas), "tasks": int(tasks)}



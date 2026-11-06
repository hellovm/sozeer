"""令牌刷新机制（v0.20.6 A6-4）。

★★★ 为什么要双令牌：
  单令牌方案有个两难：
    · 有效期长（14 天）→ 体验好，但**令牌泄露后攻击者可长期使用**
    · 有效期短（1 小时）→ 安全，但用户每小时被踢出去一次

  双令牌解决这个矛盾：
    ┌─────────────────────────────────────────────────────┐
    │ access_token   短（2 小时）  随请求发送，泄露窗口小   │
    │ refresh_token  长（30 天）   仅用于换新，不随请求发   │
    └─────────────────────────────────────────────────────┘
  攻击者偷到 access_token 最多用 2 小时；偷到 refresh_token 会被**轮换检测**发现。

================================ 关键安全机制 ================================

★ 1. refresh token **轮换**（rotation）
     每次刷新都签发新的 refresh_token，旧的一次性作废。

★ 2. **重放检测**（reuse detection）—— 这是轮换的价值所在
     如果旧 refresh_token 被再次使用，说明：
       · 要么攻击者偷了令牌（他在用旧的）
       · 要么用户正常刷新但网络重试
     两种都可能，但**安全优先**：判定为泄露，**吊销该用户所有令牌**并强制重登。
     RFC 6819 / OAuth 2.1 推荐做法。

★ 3. 令牌**绑定指纹**（可选）
     把 UA 的哈希存进 refresh token，换设备刷新需要重新登录。
     本实现默认开启（宽松模式：只记录不强制，便于排查）。

================================ 存储 ================================

  refresh_tokens 表：
    id / user_id / token_hash（★ 存哈希不存明文，与密码同理）
    family_id（令牌家族，用于整族吊销）
    parent_id（上一个令牌，用于检测链式重放）
    revoked / used_at / expires_at / ip / ua
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api import config
from apps.api.models import RefreshToken

log = logging.getLogger("novelforge")

# ★ 有效期（秒）
ACCESS_TTL = int(getattr(config, "ACCESS_TTL", 2 * 3600))            # 2 小时
REFRESH_TTL = int(getattr(config, "REFRESH_TTL", 30 * 24 * 3600))   # 30 天
# ★ 并发刷新宽限窗口（秒）：同一 refresh_token 在被使用后 N 秒内再次出现，
#   视为多标签页并发刷新（前端单例去重管不到跨标签页），补发新令牌而不吊销整族。
#   超过宽限期的重用仍判定为泄露 → 吊销整族（安全语义不变）。
REFRESH_REUSE_GRACE = int(getattr(config, "REFRESH_REUSE_GRACE", 90))


def _naive_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


# ================================================================ access token

def make_access_token(user_id: str, *, ttl: int = ACCESS_TTL, scope: str = "full") -> str:
    """签发 access token（短效）。

    ★ 格式与旧的 make_token 兼容（同样的 {u, iat, exp} + HMAC），
      但 payload 增加 `typ: "access"` 与 `jti`（令牌唯一 id，便于追踪）。
      ★ 兼容性：旧的 make_token 不带 typ，校验时视为 access（向后兼容）。
    """
    now = int(time.time())
    payload = json.dumps({
        "u": user_id, "iat": now, "exp": now + ttl,
        "typ": "access", "jti": secrets.token_urlsafe(8), "sc": scope,
    }).encode()
    body = base64.urlsafe_b64encode(payload).decode()
    sig = hmac.new(config.SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest()
    return body + "." + base64.urlsafe_b64encode(sig).decode()


def parse_access_token(token: str) -> dict | None:
    """解析 access token，返回 payload（含 u / iat / exp / jti）。"""
    try:
        body, sig = token.rsplit(".", 1)
        expect = hmac.new(config.SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expect, base64.urlsafe_b64decode(sig)):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body))
        if payload.get("exp", 0) < int(time.time()):
            return None
        # ★ 旧令牌没有 typ → 视为 access（避免升级踢人）
        if payload.get("typ") not in (None, "access"):
            return None
        return payload
    except Exception:
        return None


# ================================================================ refresh token

def _hash_token(raw: str) -> str:
    """★ 令牌存哈希不存明文 —— 与密码同理。

    为什么：数据库泄露时，明文 refresh_token 等于把所有人的会话送出去。
    用 HMAC（而非裸 SHA256）是为了抗彩虹表（虽然随机令牌本身不太需要）。
    """
    return hmac.new(config.SECRET_KEY.encode(), raw.encode(), hashlib.sha256).hexdigest()


def issue_refresh_token(db: Session, user_id: str, *, family_id: str = "",
                        parent_id: str = "", ip: str = "", ua: str = "") -> dict:
    """签发 refresh token（新家族或延续家族）。返回 {token, id, expires_at}。"""
    raw = secrets.token_urlsafe(48)
    tid = _uid("rt")
    fam = family_id or _uid("fam")
    exp = _naive_now() + dt.timedelta(seconds=REFRESH_TTL)
    db.add(RefreshToken(
        id=tid, user_id=user_id, token_hash=_hash_token(raw), family_id=fam,
        parent_id=parent_id or "", revoked=0, used_at=None,
        expires_at=exp, ip=(ip or "")[:64], ua=(ua or "")[:256],
        created_at=_naive_now()))
    db.commit()
    return {"token": raw, "id": tid, "family_id": fam,
            "expires_at": exp.strftime("%Y-%m-%d %H:%M:%S"),
            "ttl": REFRESH_TTL}


def rotate_refresh_token(db: Session, raw: str, *, ip: str = "", ua: str = ""
                         ) -> dict:
    """★ 用 refresh token 换新令牌对（轮换 + 重放检测）。

    返回 {ok, access_token, refresh_token, ...} 或 {ok: False, error, code}

    ★ 重放检测逻辑：
      1. 令牌不存在 → 无效（可能已过期被清理）
      2. 令牌已用过（used_at 有值）→ ★ 疑似泄露，**吊销整族**
      3. 令牌已撤销 → 拒绝
      4. 令牌过期 → 拒绝
      5. 正常 → 标记 used，签发新的（同 family）
    """
    if not raw:
        return {"ok": False, "code": "missing", "error": "缺少 refresh_token"}

    th = _hash_token(raw)
    row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == th))
    if row is None:
        return {"ok": False, "code": "invalid", "error": "refresh_token 无效"}

    # ---- ★ 重放检测：已用过的令牌再次出现 = 泄露信号 ----
    if row.used_at is not None:
        # ★ 并发刷新宽限窗口（v0.27.1）：宽限期内重用 → 大概率是多标签页
        #   同时 401 并发刷新，不吊销整族，静默补发一对新令牌（同 family）。
        #   否则用户开两个标签页就必然触发"整族吊销→强制重登"。
        _in_grace = False
        try:
            if row.used_at:
                _in_grace = (_naive_now() - row.used_at) <= dt.timedelta(
                    seconds=REFRESH_REUSE_GRACE)
        except Exception:                            # noqa: BLE001
            _in_grace = False
        if _in_grace:
            log.warning("refresh_token 在 %d 秒宽限期内被再次使用"
                        "（多为多标签页并发刷新），补发不吊销：user=%s family=%s",
                        REFRESH_REUSE_GRACE, row.user_id, row.family_id)
            new = issue_refresh_token(db, row.user_id, family_id=row.family_id,
                                      parent_id=row.id, ip=ip, ua=ua)
            return {"ok": True, "user_id": row.user_id,
                    "access_token": make_access_token(row.user_id),
                    "access_ttl": ACCESS_TTL,
                    "refresh_token": new["token"],
                    "refresh_expires_at": new["expires_at"],
                    "expires_at": new["expires_at"], "ttl": ACCESS_TTL}
        n = revoke_family(db, row.family_id, reason="检测到 refresh_token 重放（疑似泄露）")
        log.error("★ 检测到 refresh_token 重放！user=%s family=%s，已吊销整族 %d 个令牌",
                  row.user_id, row.family_id, n)
        _write_alert(db, row, ip, ua)
        return {"ok": False, "code": "reuse_detected",
                "error": "检测到令牌重用，出于安全已登出所有设备，请重新登录"}

    if row.revoked:
        return {"ok": False, "code": "revoked", "error": "登录状态已失效，请重新登录"}

    if row.expires_at and row.expires_at < _naive_now():
        return {"ok": False, "code": "expired", "error": "登录已过期，请重新登录"}

    # ---- 正常轮换 ----
    row.used_at = _naive_now()
    row.revoked = 1
    db.commit()

    new = issue_refresh_token(db, row.user_id, family_id=row.family_id,
                              parent_id=row.id, ip=ip, ua=ua)
    return {"ok": True, "user_id": row.user_id,
            "access_token": make_access_token(row.user_id),
            "access_ttl": ACCESS_TTL,
            "refresh_token": new["token"], "refresh_expires_at": new["expires_at"],
            "expires_at": new["expires_at"], "ttl": ACCESS_TTL}


def revoke_family(db: Session, family_id: str, *, reason: str = "") -> int:
    """吊销一个令牌家族（该次登录的所有令牌）。返回影响行数。"""
    if not family_id:
        return 0
    rows = db.scalars(select(RefreshToken)
                      .where(RefreshToken.family_id == family_id,
                             RefreshToken.revoked == 0)).all()
    for r in rows:
        r.revoked = 1
    if rows:
        db.commit()
    log.info("吊销令牌家族 %s：%d 个（%s）", family_id, len(rows), reason or "未注明")
    return len(rows)


def revoke_user_tokens(db: Session, user_id: str, *, reason: str = "") -> int:
    """吊销某用户所有 refresh token（改密/登出所有设备时用）。"""
    if not user_id:
        return 0
    rows = db.scalars(select(RefreshToken)
                      .where(RefreshToken.user_id == user_id,
                             RefreshToken.revoked == 0)).all()
    for r in rows:
        r.revoked = 1
    if rows:
        db.commit()
    log.info("吊销用户 %s 的全部令牌：%d 个（%s）", user_id, len(rows), reason or "")
    return len(rows)


def list_sessions(db: Session, user_id: str, limit: int = 20) -> list[dict]:
    """活跃会话列表（用户端"登录设备管理"用）。"""
    now = _naive_now()
    rows = db.scalars(select(RefreshToken)
                      .where(RefreshToken.user_id == user_id,
                             RefreshToken.revoked == 0,
                             RefreshToken.expires_at > now)
                      .order_by(RefreshToken.created_at.desc())
                      .limit(limit)).all()
    return [{"id": r.id, "family_id": r.family_id,
             "ip": _mask(r.ip), "ua": _short_ua(r.ua),
             "expires_at": r.expires_at.strftime("%Y-%m-%d %H:%M") if r.expires_at else "",
             "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
             "current": False} for r in rows]


def _mask(ip: str) -> str:
    if not ip:
        return ""
    if ":" in ip:
        return ":".join(ip.split(":")[:4]) + "::***"
    p = ip.split(".")
    return f"{p[0]}.{p[1]}.*.*" if len(p) == 4 else ip


def _short_ua(ua: str) -> str:
    """UA 简化（太长没法看）。"""
    if not ua:
        return ""
    for kw, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                     ("Windows", "Windows"), ("Macintosh", "macOS"), ("Linux", "Linux")):
        if kw in ua:
            br = "Chrome" if "Chrome" in ua else ("Safari" if "Safari" in ua else
                 ("Firefox" if "Firefox" in ua else "浏览器"))
            return f"{name} · {br}"
    return ua[:40]


def _write_alert(db: Session, row: RefreshToken, ip: str, ua: str) -> None:
    """把重放事件写入审计日志（安全事件必须留痕）。"""
    try:
        from apps.api.models import AdminAuditLog
        db.add(AdminAuditLog(
            id=_uid("sec"), operator_id="", operator_name="令牌安全",
            action="security.token_reuse", target_type="user", target_id=row.user_id,
            reason="检测到 refresh_token 重放，已吊销全部会话",
            detail=json.dumps({"family_id": row.family_id, "token_id": row.id,
                               "reused_at": _naive_now().isoformat()},
                              ensure_ascii=False),
            ip=(ip or "")[:64], user_agent=(ua or "")[:256],
            result="rejected", created_at=_naive_now()))
        db.commit()
    except Exception as e:                          # noqa: BLE001
        log.error("令牌重放告警写入失败：%s", e)


def cleanup_expired(db: Session, days: int = 7) -> int:
    """清理过期/已撤销的令牌记录（定期维护，防表膨胀）。"""
    cutoff = _naive_now() - dt.timedelta(days=max(1, days))
    rows = db.scalars(select(RefreshToken)
                      .where(RefreshToken.expires_at < cutoff)).all()
    n = len(rows)
    for r in rows:
        db.delete(r)
    if n:
        db.commit()
    return n

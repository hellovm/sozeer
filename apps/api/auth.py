"""账号与鉴权：纯标准库实现，无第三方依赖。

- 密码：PBKDF2-HMAC-SHA256 + 随机盐，存储格式 pbkdf2$<salt_b64>$<hash_b64>。
- 令牌：{user_id, exp} 经 base64 编码后，用 SECRET_KEY 做 HMAC-SHA256 签名。
- 鉴权方式：前端把令牌放入 Authorization: Bearer <token>，后端依赖注入解析。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid

from fastapi import HTTPException, Request
from sqlalchemy import select

from apps.api import config
from apps.api.db import SessionLocal
from apps.api.models import User

PBKDF2_ROUNDS = 120_000


# ----------------------------------------------------------------- 密码
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return "pbkdf2$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(pw: str, stored: str) -> bool:
    try:
        kind, salt_b64, hash_b64 = stored.split("$")
        if kind != "pbkdf2":
            return False
        salt = base64.b64decode(salt_b64)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, PBKDF2_ROUNDS)
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except Exception:
        return False


# ----------------------------------------------------------------- 令牌
def make_token(user_id: str) -> str:
    """签发令牌。

    ★ v0.20.4 A4：payload 增加 `iat`（签发时间）。
      用途：「改密后旧令牌立即失效」—— 靠 iat 与 users.token_valid_after 比较。
      没有 iat 的话，改密只能靠"等 14 天过期"，安全窗口太长。
    """
    _now = int(time.time())
    payload = json.dumps({"u": user_id, "iat": _now,
                          "exp": _now + config.TOKEN_TTL}).encode()
    body = base64.urlsafe_b64encode(payload).decode()
    sig = hmac.new(config.SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest()
    return body + "." + base64.urlsafe_b64encode(sig).decode()


def parse_token(token: str) -> str | None:
    try:
        body, sig = token.rsplit(".", 1)
        expect = hmac.new(config.SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expect, base64.urlsafe_b64decode(sig)):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body))
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload.get("u")
    except Exception:
        return None


def token_issued_at(token: str) -> int | None:
    """取令牌签发时间（★ 验签后才返回，防伪造）。

    兼容旧令牌：早期令牌没有 iat → 返回 None。
    调用方对 None 的处理：**视为早于任何 token_valid_after**（即失效），
    因为"无法证明是新令牌"时应当保守。
    """
    try:
        body, sig = token.rsplit(".", 1)
        expect = hmac.new(config.SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expect, base64.urlsafe_b64decode(sig)):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body))
        v = payload.get("iat")
        return int(v) if v is not None else None
    except Exception:
        return None


# ----------------------------------------------------------------- 依赖
def _request_db(request: Request):
    """★ 取「与本次请求同一个」DB session。

    ★★ 为什么必须这样（v0.26.1 修的真实 bug）：
      `get_current_user` 原来在自己的 session 里加载 User，然后 close()。
      处理器拿到的 User 是 **detached（脱离 session）** 的对象 ——
      对它赋值后调用 `db.commit()` 提交的是**另一个 session 里的另一个实例**，
      改动**全部丢失**。表现为：接口返回 200 + 正确的新值，
      但再查一次还是旧值 → "保存成功但没生效"（最难查的一类 bug）。

      → 让端点通过 `Depends(get_db)` 拿 session，再用本函数的
        `attach()` 把实例绑回该 session，改动才能落库。
    """
    return getattr(request.state, "_db", None)


async def get_current_user(request: Request) -> User:
    db = SessionLocal()
    try:
        ah = request.headers.get("Authorization", "")
        token = ah[len("Bearer "):] if ah.lower().startswith("bearer ") else ""
        uid = parse_token(token) if token else None
        if not uid:
            raise HTTPException(401, "未登录或登录已过期")
        u = db.get(User, uid)
        if not u or not u.is_active:
            raise HTTPException(401, "账号不可用")
        # ★ 把 uid 挂在 request 上，供端点用 get_db 重新绑定（见 rebind_user）
        request.state.uid = uid
        return u
    finally:
        db.close()


def rebind_user(db, user: User) -> User:
    """★ 把（可能 detached 的）User 实例重新绑定到当前请求的 db session。

    ★ 用法（所有"要修改当前用户字段"的端点都必须这样写）：
        u = auth.rebind_user(db, user)
        u.nickname = "新昵称"
        db.commit()

    ★ 为什么不用 `db.merge(user)`：merge 会额外发一次 SELECT 并且
      在并发下可能覆盖其他字段。这里直接按主键取本 session 的实例，
      语义最清晰（"拿本 session 的那个对象"）。
    """
    if user is None:
        return user
    try:
        if user in db:
            return user                      # 已经是本 session 的实例
    except Exception:                        # noqa: BLE001
        pass
    got = db.get(User, user.id)
    return got if got is not None else user


async def require_admin(request: Request) -> User:
    """★ 要求管理员权限（v0.20.7 A7 起走 RBAC，语义不变）。

    ★ 为什么要改内部实现而不是保留 `role != "admin"`：
      规则应集中在一处（rbac.py），否则"加个能访问后台的财务角色"
      就要满仓库找 `role == "admin"`，漏一个就是越权漏洞。

    ★ 兼容性：内置 admin 角色拥有 `admin.access`，
      所以原有 `role=admin` 的用户**行为完全不变**。
    """
    u = await get_current_user(request)
    role = getattr(u, "role", "")
    # 延迟导入避免循环（rbac 不依赖 auth）
    try:
        from apps.api import rbac
        from apps.api.db import SessionLocal
        _db = SessionLocal()
        try:
            ok = rbac.has_permission(_db, role, "admin.access")
        finally:
            _db.close()
    except Exception as e:                          # noqa: BLE001
        # ★ RBAC 故障时降级为旧逻辑（不能因权限系统故障锁死后台）
        import logging
        logging.getLogger("novelforge").warning(
            "RBAC 检查异常，降级为 role==admin：%s", e)
        ok = (role == "admin")
    if not ok:
        raise HTTPException(403, "需要管理员权限")
    return u


async def require_owner(request: Request) -> User:
    """★★ 要求 owner 级权限（v0.25.4 I11 配置导出/导入用）。

    ★ 为什么需要比 admin 更高的一档：
      配置导出文件含**计费与安全参数**（积分单价、限流阈值），
      泄漏后攻击者能精准算出"怎么刷最划算"；
      配置导入能**批量覆盖全站配置**，等同"改服务器"。
      这两件事的风险级别高于普通后台操作。

    ★ 权限设计（与 require_admin 同构）：
      优先查 RBAC 的 `config.export` 权限（可在后台配置给多个角色），
      RBAC 故障时降级为 `role == "owner"` 判断 ——
      降级方向是**收紧**（不是放宽），防止权限系统故障导致越权。
    """
    u = await get_current_user(request)
    role = getattr(u, "role", "")
    try:
        from apps.api import rbac
        from apps.api.db import SessionLocal
        _db = SessionLocal()
        try:
            ok = rbac.has_permission(_db, role, "config.export")
        finally:
            _db.close()
    except Exception as e:                          # noqa: BLE001
        import logging
        logging.getLogger("novelforge").warning(
            "RBAC 检查异常，降级为 role==owner：%s", e)
        ok = (role == "owner")
    if not ok:
        raise HTTPException(403, "需要最高权限（owner）")
    return u


# ----------------------------------------------------------------- 账号辅助
def create_user(db, username: str, password: str, role: str = "user", email: str = "") -> User:
    u = User(id=f"u_{uuid.uuid4().hex[:12]}", username=username, email=email,
             pw_hash=hash_password(password), role=role)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def first_user_is_admin(db) -> bool:
    """库里还没有任何账号时，第一个注册者自动成为管理员。"""
    return db.scalar(select(User).limit(1)) is None

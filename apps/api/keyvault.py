"""API Key 静态加密：Fernet 对称加密，密钥从 SECRET_KEY 派生。

设计：
- 存储格式：`enc$<vN>$<fernet_token>`（v0.20.6 A6-5 起带版本号）
  · `enc$<fernet_token>`  → v1（兼容旧格式，无版本段）
  · `enc$v2$<fernet_token>` → v2
  · 无前缀 → 明文遗留行（迁移前数据），读取时兼容，写回时补加密
- 密钥派生：SHA256(SECRET_KEY + "|api-key-enc|v<N>") 32 字节 → Fernet
- **密钥轮换**（v0.20.6 新增）：支持同时解多个版本的密文，
  写入时用最新版本 → 可平滑轮换 SECRET_KEY 而不用重录所有 Key。
  轮换步骤：① 把旧 SECRET_KEY 放进 NOVELFORGE_SECRET_KEYS_OLD（逗号分隔）
           ② 换新 SECRET_KEY ③ 调用 rotate_all() 重加密
- 透明性：get_api_key() / set_api_key() 两个入口，业务层（llm/media/ping）只碰它们。
- 迁移：encrypt_all_plaintext_keys() 启动时把无前缀的非空 Key 就地加密，幂等。
- 自检：self_check() 验证加解密往返正常（启动时跑，加密坏了要立刻知道）。
"""
from __future__ import annotations

import hashlib
import base64
import logging
import os

from apps.api import config

log = logging.getLogger("novelforge")

_FERNET = None
_PREFIX = "enc$"
# ★ 当前加密版本（写入时用这个）
CURRENT_VERSION = 2


def _derive(secret: str, version: int = CURRENT_VERSION):
    """从 SECRET_KEY 派生 Fernet 实例。"""
    from cryptography.fernet import Fernet
    dk = hashlib.sha256(f"{secret}|api-key-enc|v{version}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(dk))


def _legacy_derive(secret: str):
    """v1 派生方式（无版本段，与历史数据兼容）。"""
    from cryptography.fernet import Fernet
    dk = hashlib.sha256(f"{secret}|api-key-enc".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(dk))


def _old_secrets() -> list[str]:
    """轮换期间保留的旧密钥（逗号分隔）。"""
    raw = os.environ.get("NOVELFORGE_SECRET_KEYS_OLD", "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _fernet():
    global _FERNET
    if _FERNET is None:
        _FERNET = _derive(config.SECRET_KEY, CURRENT_VERSION)
    return _FERNET


def _decryptors() -> list:
    """★ 所有可用的解密器（当前 + 历史版本 + 旧密钥）。

    为什么需要多个：轮换期间，库里同时存在新旧两种密文。
    只用一个解密器会导致"换密钥后老数据全读不出来"。
    """
    out = [_fernet()]
    # 当前密钥的 v1 派生（历史格式）
    out.append(_legacy_derive(config.SECRET_KEY))
    # 旧密钥（轮换过渡期）
    for s in _old_secrets():
        out.append(_derive(s, CURRENT_VERSION))
        out.append(_legacy_derive(s))
    return out


def encrypt(plain: str) -> str:
    """加密。空串返回空串（表示"未设置"）。"""
    if not plain:
        return ""
    if plain.startswith(_PREFIX):   # 已加密，幂等
        return plain
    token = _fernet().encrypt(plain.encode("utf-8")).decode("ascii")
    return f"{_PREFIX}v{CURRENT_VERSION}${token}"


def decrypt(stored: str) -> str:
    """解密。

    ★ 逐版本尝试：任一成功即返回（支持平滑轮换）。
    ★ 全部失败返回空串而不是抛异常 —— 让上游报"Key 未设置"而不是 500。
    """
    if not stored:
        return ""
    if not stored.startswith(_PREFIX):   # 明文遗留（迁移前数据）
        return stored

    rest = stored[len(_PREFIX):]
    if rest.startswith("v"):
        _, _, token = rest.partition("$")
    else:
        token = rest                    # v1：无版本段

    if not token:
        return ""
    for f in _decryptors():
        try:
            return f.decrypt(token.encode("ascii")).decode("utf-8")
        except Exception:
            continue
    log.warning("API Key 解密失败（密钥已更换或密文损坏）")
    return ""


def version_of(stored: str) -> int:
    """取密文版本（0 = 明文，1 = 旧格式，2+ = 带版本）。"""
    if not stored or not stored.startswith(_PREFIX):
        return 0
    rest = stored[len(_PREFIX):]
    if rest.startswith("v"):
        try:
            return int(rest[1:].split("$", 1)[0])
        except Exception:
            return -1
    return 1


def needs_rotation(stored: str) -> bool:
    """判断是否需要重加密（明文 / 旧版本）。"""
    v = version_of(stored)
    return bool(stored) and v != CURRENT_VERSION


def self_check() -> dict:
    """★ 启动自检：验证加解密往返正常。

    为什么必须自检：加密坏了（如 SECRET_KEY 缺省、cryptography 未装）
    会导致**存进去读不出来**，而错误往往到用户用 Key 时才暴露。
    启动时立刻验证，问题当场可见。
    """
    try:
        probe = "__nf_keyvault_probe__"
        enc = encrypt(probe)
        ok = decrypt(enc) == probe and enc.startswith(f"{_PREFIX}v{CURRENT_VERSION}$")
        return {"ok": ok, "version": CURRENT_VERSION,
                "old_keys": len(_old_secrets()),
                "error": "" if ok else "加解密往返不一致"}
    except Exception as e:                          # noqa: BLE001
        return {"ok": False, "version": CURRENT_VERSION, "old_keys": 0,
                "error": f"{type(e).__name__}: {e}"}


def rotate_all(db) -> dict:
    """★ 把所有密文重加密到当前版本（轮换 SECRET_KEY 后调用）。

    返回 {total, rotated, failed}
    """
    from sqlalchemy import select
    from apps.api.models import ModelProfile

    total = rotated = failed = 0
    for m in db.scalars(select(ModelProfile)).all():
        v = m.api_key or ""
        if not v:
            continue
        total += 1
        plain = decrypt(v)
        if not plain:
            failed += 1       # 解不开（旧密钥没提供）→ 需要人工重录
            continue
        new = encrypt(plain)
        if new != v:
            m.api_key = new
            rotated += 1
    if rotated:
        db.commit()
    return {"total": total, "rotated": rotated, "failed": failed}



def get_api_key(m) -> str:
    """业务层统一取 Key 入口（m 为 ModelProfile 行）。"""
    return decrypt(getattr(m, "api_key", "") or "")


def set_api_key(m, plain: str) -> None:
    """业务层统一写 Key 入口。空串 = 清除。"""
    m.api_key = encrypt(plain) if plain else ""


def encrypt_all_plaintext_keys(db) -> int:
    """启动迁移：把库中所有明文 Key 就地加密。返回迁移条数（幂等，可重复执行）。"""
    from sqlalchemy import select
    from apps.api.models import ModelProfile
    n = 0
    for m in db.scalars(select(ModelProfile)).all():
        v = m.api_key or ""
        if v and not v.startswith(_PREFIX):
            m.api_key = encrypt(v)
            n += 1
    if n:
        db.commit()
    return n

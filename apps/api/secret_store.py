# -*- coding: utf-8 -*-
"""应用级密钥存储（v1.0-E4.2）。

================================ 为什么需要它 ================================

`keyvault.py` 的 `get_api_key(m)` / `set_api_key(m, plain)` 是**为 ModelProfile
设计的** —— 参数 `m` 是 `ModelProfile` 行对象，读写的是它的 `api_key` 字段。

但 `mail.py` 里这样调用：

    v = get_api_key(db, key)      # ← 两个参数

而 `keyvault.get_api_key` **只接受一个参数** → 抛 `TypeError`
→ 被 `except Exception: pass` **静默吞掉**
→ ★ 后果：后台存的 SMTP 配置**永远读不到**，且没有任何报错。

这正是"配置能力缺失"的真实根因之一。

================================ 设计 ================================

在 `Setting` 表里存，但**值用 keyvault 加密**：

    settings(key="app_secret:mail.smtp_pass", value="enc:v1$gAAAAA...")

★ 为什么复用 Setting 表而不建新表：
  · 不用改表结构（免迁移风险）
  · 复用现有 `get_setting` 的缓存/事务管理
  · 密钥与普通配置在**同一个事务边界**内（不会出现"配置存了密钥没存"）

★ 为什么值要加密：
  SMTP 密码、第三方 API Key 等都是**凭据**。
  数据库被拖库 / 备份泄露时，明文凭据是最严重的损失
  （对比：小说正文泄露只是内容问题，凭据泄露 = 全站被接管）。

★ 命名约定：所有应用级密钥统一用 `app_secret:` 前缀，
  与业务配置（`system.*` / `billing.*`）区分，便于审计与批量轮换。
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from apps.api import keyvault
from apps.api.models import Setting

_log = logging.getLogger("novelforge.secrets")

# ★ 前缀：所有应用级密钥都用它，便于识别与批量操作
PREFIX = "app_secret:"


def _full_key(key: str) -> str:
    """补全前缀（调用方不必关心前缀）。"""
    k = (key or "").strip()
    return k if k.startswith(PREFIX) else f"{PREFIX}{k}"


def get_secret(db: Session, key: str, default: str = "") -> str:
    """读密钥（自动解密）。

    ★ 解密失败返回 default 而不是抛异常 ——
      密钥损坏不应让整个页面 500（与 `mail.decrypt` 的容错理念一致）。
    """
    try:
        row = db.get(Setting, _full_key(key))
        if row is None or not row.value:
            return default
        return keyvault.decrypt(row.value) or default
    except Exception as e:                              # noqa: BLE001
        _log.warning("读取密钥 %s 失败：%s", key, e)
        return default


def set_secret(db: Session, key: str, plain: str) -> bool:
    """写密钥（自动加密）。

    ★ 空串 = **清除**（与 keyvault 语义一致）。
      但调用方要注意：清空是不可逆的，前端应做二次确认。
    ★ 不 commit —— 由调用方决定事务边界（与 `services.settings` 一致）。
    """
    try:
        fk = _full_key(key)
        row = db.get(Setting, fk)
        enc = keyvault.encrypt(plain) if plain else ""
        if row is None:
            db.add(Setting(key=fk, value=enc))
        else:
            row.value = enc
        return True
    except Exception as e:                              # noqa: BLE001
        _log.warning("写入密钥 %s 失败：%s", key, e)
        return False


def has_secret(db: Session, key: str) -> bool:
    """是否已设置（★ 不返回值，避免误用）。"""
    try:
        row = db.get(Setting, _full_key(key))
        return bool(row and row.value)
    except Exception:                                   # noqa: BLE001
        return False


def list_secret_keys(db: Session) -> list[str]:
    """列出所有已设置的应用级密钥（★ 只返回键名与"是否已设"，绝不返回值）。"""
    from sqlalchemy import select as _select
    out: list[str] = []
    try:
        for row in db.scalars(
                _select(Setting).where(Setting.key.startswith(PREFIX))):
            out.append(row.key[len(PREFIX):])
    except Exception:                                   # noqa: BLE001
        pass
    return sorted(out)


__all__ = ["PREFIX", "get_secret", "set_secret", "has_secret", "list_secret_keys"]

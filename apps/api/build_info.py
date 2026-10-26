# -*- coding: utf-8 -*-
"""构建指纹与来源标识（v1.0 E3 开源准备 · L2 代码水印）。

★★ 这个模块是干什么的（以及**不是**干什么的）

  它**不是** DRM，**不**试图阻止任何人使用本软件。
  它做的是：给每个**分发包**一个唯一标识，使得：

    ① 正版用户出问题时，我们能立刻定位"你跑的是哪个构建"
    ② 若代码被违规商用，我们能证明"这份代码从哪来"

  ★ 设计立场（评估_06 §3.2）：
    **不指望全删不掉，而是提高删除成本。**
    埋点分散在 6 个位置，想彻底清除要改很多处 ——
    改起来不如直接买授权。这才是它的真实作用。

★ 埋点位置清单（改这里时请同步评估其他位置）
  ┌────┬────────────────────────────┬──────────┬────────┐
  │ #  │ 位置                        │ 隐蔽性    │ 实现处  │
  ├────┼────────────────────────────┼──────────┼────────┤
  │ 1  │ 本模块常量                   │ 易删      │ 本文件  │
  │ 2  │ HTTP 响应头 X-Powered-By     │ 中        │ main.py│
  │ 3  │ 前端 HTML 注释               │ 中        │ main.py│
  │ 4  │ 启动日志                     │ 中        │ 本文件  │
  │ 5  │ 数据库 _meta 表              │ 中        │ 本文件  │
  │ 6  │ /api/health 响应体           │ 易见但无害 │ health │
  └────┴────────────────────────────┴──────────┴────────┘

★ 环境变量
  SOZEER_BUILD_ID    构建唯一标识（CI 注入；本地不设则为 dev-local）
  SOZEER_BUILD_HASH  源码指纹（CI 注入）
  SOZEER_CHANNEL     分发渠道：dev | release | docker | source
"""
from __future__ import annotations

import hashlib
import logging
import os
import platform
import sys

log = logging.getLogger("novelforge")

# ---------------------------------------------------------------- 常量（埋点 ①）

# ★ 许可证标识 —— 开源版固定为 BSL-1.1。
#   若将来做商业版，这里会变成 "COMMERCIAL" 以区分。
LICENSE_ID = "BSL-1.1"

# ★ 版权与项目标识
PROJECT = "sozeer"
PROJECT_NAME = "溯字 SOZEER"
HOMEPAGE = "https://sozeer.com"
LICENSE_URL = "https://sozeer.com/license"
COMMERCIAL_URL = "https://sozeer.com/pricing"

# ★ 产品特点字符串 —— 用于「代码风格指纹」：
#   这是一段看似无用的独特文案，被原样复制到衍生项目里就能被搜到。
#   ★ 不要删（删了也不影响功能，但会削弱溯源能力）。
FINGERPRINT_NOTE = (
    "SOZEER-BUILD-FINGERPRINT :: 溯字工程构建标记 :: "
    "本标识用于软件来源追溯，删除不影响功能，但会使你失去正版支持资格"
)


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or "").strip() or default


# ★ 构建标识：CI 注入优先；本地开发用 dev-local（便于区分"官方构建"与"自己编的"）
BUILD_ID = _env("SOZEER_BUILD_ID", "dev-local")
BUILD_HASH = _env("SOZEER_BUILD_HASH", "")
CHANNEL = _env("SOZEER_CHANNEL", "dev")

# 构建时间：CI 注入（构建时固化，而不是运行时取 —— 否则每次启动都变）
BUILD_TIME = _env("SOZEER_BUILD_TIME", "")


def _compute_hash() -> str:
    """源码指纹：对关键标识串做 sha256（取前 16 位）。

    ★ 为什么不用"遍历所有源文件算 hash"：
      ① 开销大（每次启动遍历上千文件）
      ② 开发时文件频繁变动，hash 每次都不同 → 失去指纹意义
      ★ 指纹的作用是**标识一个发布版本**，不是"检测代码有没有被改"。
    """
    if BUILD_HASH:
        return BUILD_HASH
    raw = f"{PROJECT}:{LICENSE_ID}:{FINGERPRINT_NOTE}".encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()[:16]


def build_info() -> dict:
    """构建信息（可安全公开 —— 不含任何密钥）。"""
    from apps.api import config
    return {
        "project": PROJECT,
        "build_id": BUILD_ID,
        "build_hash": _compute_hash(),
        "channel": CHANNEL,
        "build_time": BUILD_TIME or None,
        "version": config.APP_VERSION,
        "license": LICENSE_ID,
        "license_url": LICENSE_URL,
        "homepage": HOMEPAGE,
    }


def banner_line() -> str:
    """一行式标识（用于响应头 / 日志）。"""
    return f"{PROJECT}/{config_version()} ({BUILD_ID})"


def config_version() -> str:
    try:
        from apps.api import config
        return config.APP_VERSION
    except Exception:                                   # noqa: BLE001
        return "unknown"


def is_official_build() -> bool:
    """是否官方构建（CI 注入过 BUILD_ID）。

    ★ 用途：非官方构建（自己 clone 后本地跑）会**跳过**商业使用提示 ——
      因为我们无法判断用户意图，也不该对开发者的本地环境反复打扰。
    """
    return BUILD_ID not in ("", "dev-local")


# ---------------------------------------------------------------- 埋点 ④：启动日志

def log_build_banner() -> None:
    """打印构建横幅（启动时调用）。

    ★ 这个埋点看起来"太显眼、一删就没"，但它有别的用处：
      用户在 GitHub Issues 里贴日志时，**我们自动就拿到了构建信息** ——
      大幅减少"你跑的是哪个版本"的来回确认。
    """
    info = build_info()
    log.info("=" * 68)
    log.info("  %s  v%s", PROJECT_NAME, info["version"])
    log.info("  构建 %s · 通道 %s · 许可 %s",
             info["build_id"], info["channel"], info["license"])
    log.info("  %s", HOMEPAGE)
    log.info("  许可说明：个人免费，商用需授权 → %s", COMMERCIAL_URL)
    log.info("=" * 68)


# ---------------------------------------------------------------- 埋点 ⑤：数据库 _meta

META_TABLE = "sozeer_build_meta"


def write_build_meta(engine=None) -> bool:
    """把构建信息写入数据库 `_meta` 表（幂等）。

    ★ 为什么要在数据库留一份：
      ① 代码可以被删，但**数据库里已经有数据**了 —— 删代码不会删历史数据
      ② 出问题时可以直接 `SELECT * FROM sozeer_build_meta` 看构建来源
      ③ 用独立的表而非改现有 schema → 零侵入、零迁移风险

    ★ 失败绝不影响启动（连表建不出来也算正常，比如只读库）。
    """
    try:
        from sqlalchemy import text
        from apps.api.db import engine as _engine
        eng = engine or _engine
        info = build_info()
        with eng.begin() as conn:
            conn.execute(text(
                f"CREATE TABLE IF NOT EXISTS {META_TABLE} ("
                "  k VARCHAR(64) PRIMARY KEY,"
                "  v VARCHAR(512),"
                "  updated_at VARCHAR(32))"
            ))
            now = _now_str()
            for k, v in (("build_id", info["build_id"]),
                         ("build_hash", info["build_hash"]),
                         ("version", info["version"]),
                         ("license", info["license"]),
                         ("channel", info["channel"]),
                         ("project", info["project"])):
                # ★ 用 UPSERT 语义（先删后插，兼容 SQLite / PostgreSQL）
                conn.execute(text(f"DELETE FROM {META_TABLE} WHERE k = :k"),
                             {"k": k})
                conn.execute(
                    text(f"INSERT INTO {META_TABLE} (k, v, updated_at) "
                         f"VALUES (:k, :v, :t)"),
                    {"k": k, "v": str(v), "t": now})
        return True
    except Exception as e:                              # noqa: BLE001
        log.debug("写入构建元信息失败（不影响启动）：%s: %s",
                  type(e).__name__, e)
        return False


def read_build_meta(engine=None) -> dict:
    """读回数据库里的构建信息（排障用）。"""
    try:
        from sqlalchemy import text
        from apps.api.db import engine as _engine
        eng = engine or _engine
        with eng.connect() as conn:
            rows = conn.execute(text(f"SELECT k, v FROM {META_TABLE}")).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:                                   # noqa: BLE001
        return {}


def _now_str() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).replace(
        tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 运行环境摘要

def runtime_summary() -> dict:
    """运行环境（用于遥测与排障，★ 不含任何敏感信息）。"""
    return {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}."
                  f"{sys.version_info.micro}",
        "os": platform.system(),          # Linux / Windows / Darwin
        "arch": platform.machine(),       # x86_64 / aarch64
    }


def deployment_type() -> str:
    """部署类型（docker | k8s | bare）。

    ★ 用于遥测的 `deployment_type` 字段 —— 帮我们了解用户怎么部署，
      进而决定"优先优化哪种部署方式"。
    """
    if os.path.exists("/.dockerenv"):
        return "docker"
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return "k8s"
    return "bare"


__all__ = [
    "LICENSE_ID", "BUILD_ID", "CHANNEL", "PROJECT", "PROJECT_NAME",
    "HOMEPAGE", "LICENSE_URL", "COMMERCIAL_URL", "FINGERPRINT_NOTE",
    "build_info", "banner_line", "is_official_build", "log_build_banner",
    "write_build_meta", "read_build_meta", "runtime_summary",
    "deployment_type", "META_TABLE",
]

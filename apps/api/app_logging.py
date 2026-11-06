# -*- coding: utf-8 -*-
"""应用日志配置（v0.19 S2）。

为什么需要
----------
原先日志只交给 uvicorn 默认 handler → 全部进 stdout → journald。
问题是：
  1) journald 有配额与保留策略，一两周前的日志可能已被清理，排查历史问题困难；
  2) 没有独立文件便于 grep / 打包归档；
  3) 应用自己 `logging.getLogger("novelforge")` 打的日志与访问日志混在一起。

本模块同时挂 **文件 handler**（/var/log/novelforge/，由 logrotate 轮转）
与 **stderr handler**（保留 journald 可见性），两不误。

设计约束：
  * 目录不可写时**静默降级**只留 stderr —— 绝不允许日志配置失败拖垮启动。
  * 幂等：重复调用不重复加 handler（uvicorn --reload 会重复 import）。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

LOG_DIR = Path(os.environ.get("NOVELFORGE_LOG_DIR", "/var/log/novelforge"))
LOG_LEVEL = os.environ.get("NOVELFORGE_LOG_LEVEL", "INFO").upper()
APP_LOG = "novelforge.log"
ACCESS_LOG = "access.log"

_configured = False
_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


class _SafeFileHandler(logging.Handler):
    """按天切分（TimedRotatingFileHandler）+ 不可写时自动降级为空操作。

    为什么不用 logrotate 的 copytruncate 就够：应用侧按天切分能在
    长时间运行的进程里也保持单文件可控；两者叠加不冲突
    （logrotate 负责清理与压缩，应用负责切分）。
    """

    def __init__(self, path: Path, backup_count: int = 14):
        super().__init__()
        self._inner = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            h = logging.handlers.TimedRotatingFileHandler(
                str(path), when="midnight", backupCount=backup_count,
                encoding="utf-8", delay=True)
            h.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
            self._inner = h
        except Exception as e:      # 权限/磁盘问题：降级
            print(f"[logging] 文件日志不可用（降级为仅 stderr）：{type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)

    def emit(self, record):
        if self._inner is not None:
            try:
                self._inner.emit(record)
            except Exception:
                pass                # 写日志失败绝不抛回业务

    def close(self):
        if self._inner is not None:
            try:
                self._inner.close()
            except Exception:
                pass
        super().close()


def setup_logging(force: bool = False) -> dict:
    """配置根 logger。返回诊断信息（供 /api/health 展示）。"""
    global _configured
    if _configured and not force:
        return _status()

    root = logging.getLogger()
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    fmt = logging.Formatter(_FORMAT, _DATEFMT)

    # 清掉已有 handler（避免重复输出 / 与 uvicorn 默认 handler 叠加）
    for h in list(root.handlers):
        root.removeHandler(h)

    stderr_h = logging.StreamHandler(sys.stderr)
    stderr_h.setFormatter(fmt)
    root.addHandler(stderr_h)

    file_h = _SafeFileHandler(LOG_DIR / APP_LOG)
    if file_h._inner is not None:
        root.addHandler(file_h)

    # 应用自有 logger 不再向 root 二次传播样式差异
    logging.getLogger("novelforge").setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    # uvicorn 访问日志单独一个文件（便于统计与归档）
    access = logging.getLogger("uvicorn.access")
    access_h = _SafeFileHandler(LOG_DIR / ACCESS_LOG)
    if access_h._inner is not None:
        access_h.setFormatter(fmt)
        access.addHandler(access_h)

    _configured = True
    info = _status()
    logging.getLogger("novelforge").info(
        "日志已配置：level=%s file=%s", LOG_LEVEL, info.get("file_active"))
    return info


def _status() -> dict:
    return {
        "dir": str(LOG_DIR),
        "level": LOG_LEVEL,
        "file_active": bool((LOG_DIR / APP_LOG).exists()) or _can_write_dir(),
    }


def _can_write_dir() -> bool:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        probe = LOG_DIR / ".write-probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


__all__ = ["setup_logging", "LOG_DIR", "APP_LOG", "ACCESS_LOG"]

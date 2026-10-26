# -*- coding: utf-8 -*-
"""深度健康检查（v0.19 S5）。

为什么需要
----------
原先 `/api/health` 只回 `{"ok": true, "version": ..., "db": "sqlite"}` ——
**只证明进程活着**，不证明它能干活。实际可能的故障：
  * 磁盘写满 → 数据库无法写入，但 health 仍 200
  * 数据库文件损坏 → 进程活着，一查就炸
  * 日志目录不可写 → 出问题后没有日志可查
  * 后台任务堆积 → 用户体验变慢，但监控看不出来

本模块提供**分层探针**：
  * `shallow()` —— 极轻量（进程活着就 200），供外部负载均衡 / systemd 高频探测
  * `deep()`    —— 真查数据库可写、磁盘余量、日志目录、后台任务数，
                   供运维巡检与告警；**任一硬指标失败则整体 degraded**

设计：探针自身**绝不抛异常**（否则探针本身成了故障源）。
"""
from __future__ import annotations

import os
import shutil
import time

from apps.api import config


def _check_db_writable() -> dict:
    """数据库可读 + 可写（真跑一次事务后回滚，不留痕）。"""
    t0 = time.perf_counter()
    try:
        from apps.api.db import SessionLocal
        from apps.api.models import Book
        from sqlalchemy import func, select
        with SessionLocal() as db:
            # 读
            n = db.scalar(select(func.count(Book.id))) or 0
            # 写（建一条临时 Setting 再删，确认可写；失败会抛）
            from apps.api.models import Setting
            probe_key = "__health_probe__"
            row = db.get(Setting, probe_key)
            if row is None:
                db.add(Setting(key=probe_key, value=str(int(time.time()))))
            else:
                row.value = str(int(time.time()))
            db.commit()
            r2 = db.get(Setting, probe_key)
            if r2 is not None:
                db.delete(r2)
                db.commit()
        return {"ok": True, "books": int(n),
                "ms": round((time.perf_counter() - t0) * 1000, 2)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "ms": round((time.perf_counter() - t0) * 1000, 2)}


def _check_disk() -> dict:
    """磁盘余量：<5% 或 <200MB 视为危险（SQLite 写满会损坏）。"""
    try:
        path = str(config.DATA_DIR)
        total, used, free = shutil.disk_usage(path)
        pct_free = free / total * 100 if total else 0
        warn = pct_free < 5 or free < 200 * 1024 * 1024
        return {"ok": not warn, "free_mb": round(free / 1024 / 1024, 1),
                "free_pct": round(pct_free, 1), "path": path}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _check_log_dir() -> dict:
    """日志目录可写（否则出问题后没日志可查）。"""
    try:
        from apps.api.app_logging import LOG_DIR
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        probe = LOG_DIR / ".health-probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return {"ok": True, "dir": str(LOG_DIR)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _check_tasks() -> dict:
    """后台任务：在跑数量 + 是否有超时残留的 running 行。"""
    try:
        from apps.api.services.tasks import live_task_count
        from apps.api.db import SessionLocal
        from apps.api.models import Task
        from sqlalchemy import func, select
        live = live_task_count()
        with SessionLocal() as db:
            running = db.scalar(select(func.count(Task.id)).where(Task.status == "running")) or 0
        # 进程内在跑 0 但库里挂着一堆 running → 说明有残留（重启导致）
        stale = running > 0 and live == 0
        return {"ok": not stale, "live": live, "db_running": int(running),
                "stale": stale}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _check_backup() -> dict:
    """备份新鲜度：最近一次备份是否在 48 小时内。"""
    try:
        d = os.environ.get("NF_BACKUP_DIR", "/opt/novelforge/backups/db")
        if not os.path.isdir(d):
            return {"ok": False, "reason": "备份目录不存在", "dir": d}
        files = [f for f in os.listdir(d) if f.startswith("novelforge-")]
        if not files:
            return {"ok": False, "reason": "没有任何备份", "dir": d}
        newest = max(os.path.getmtime(os.path.join(d, f)) for f in files)
        age_h = (time.time() - newest) / 3600
        return {"ok": age_h < 48, "count": len(files),
                "newest_age_h": round(age_h, 1), "dir": d}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _check_pool() -> dict:
    """★★★ 连接池状态（v1.0 E2 压测新增）。

    ★ 为什么必须加这个检查（实测血泪）：
      v1.0 E2 压测打到 100 并发时，`/api/ads` 与 `/api/public/site`
      **全部超时**；但 `/api/health` 浅探针仍返回 200（它不查库！），
      **监控完全看不出来**，日志也没有任何异常。
      根因是连接池被占满（PG 侧 `idle in transaction` 堆积）。

      ★ 教训：**探针必须覆盖"有限资源"的饱和度**，
        否则"进程活着"会在资源耗尽时给出虚假的安全感。

    ★ 返回的关键指标：
      · size / checkedout / overflow：池使用情况
      · saturation：饱和度（checkedout / 总容量）—— **>0.8 就该告警**
      · 非 PG 方言（SQLite）没有 QueuePool，此时跳过。
    """
    try:
        from apps.api.db import engine, IS_SQLITE
        if IS_SQLITE:
            return {"ok": True, "skipped": "SQLite 不使用连接池"}

        pool = engine.pool
        # ★ 属性名随 SQLAlchemy 版本略有差异，逐个 getattr 兜底
        size = getattr(pool, "size", lambda: 0)() or 0
        checkedout = getattr(pool, "checkedout", lambda: 0)() or 0
        # ★★ SQLAlchemy 的 `overflow()` 在**没有溢出时返回负数**
        #   （语义是 `checkedout - size`，未超常驻数时为负）。
        #   直接上报负数会让运维困惑，这里 clamp 到 0 ——
        #   我们关心的是"超出常驻池多少"，没超出就是 0。
        _ovf_raw = getattr(pool, "overflow", lambda: 0)() or 0
        overflow = max(0, int(_ovf_raw))
        try:
            checkedin = pool.checkedin()
        except Exception:                               # noqa: BLE001
            checkedin = max(0, size - checkedout)

        capacity = size + max(0, getattr(pool, "_max_overflow", 0))
        saturation = round(checkedout / capacity, 3) if capacity else 0.0

        # ★ 阈值：饱和度 >0.9 视为危险（几乎没有余量）
        warn = saturation > 0.9
        return {
            "ok": not warn,
            "size": int(size),
            "checkedout": int(checkedout),
            "checkedin": int(checkedin),
            "overflow": overflow,
            "capacity": int(capacity),
            "saturation": saturation,
            "hint": ("连接池接近耗尽 —— 检查是否有慢查询或未结束的事务"
                     "（PG: SELECT state, count(*) FROM pg_stat_activity"
                     " WHERE datname='novelforge' GROUP BY state）") if warn else "",
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def shallow() -> dict:
    """浅探针：进程活着即 ok（轻量，供高频探测）。"""
    return {"ok": True, "version": config.APP_VERSION,
            "db": config.DB_URL.split("://")[0]}


def _safe(fn, name: str) -> dict:
    """包一层：任何检查项抛异常都转成 ok=False 的结果，绝不向外抛。

    ★ 为什么必须这样：探针的职责是"报告故障"，它自己**绝不能成为故障源**。
    实测发现：不包这层时，任何一个检查项（如磁盘 API 在异常环境报错）
    都会让整个 /api/health?deep=1 崩掉 —— 监控反而先挂了。
    """
    try:
        r = fn()
        return r if isinstance(r, dict) else {"ok": bool(r)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def deep(db=None) -> dict:
    """深探针：数据库/磁盘/日志/任务/备份全查。

    返回 {"ok": 总判定, "checks": {...}, "degraded": [...]}。
    硬指标（db / disk）失败 → ok=False；软指标（log / tasks / backup）失败 → 记入 degraded。
    """
    checks = {
        "db": _safe(_check_db_writable, "db"),
        "disk": _safe(_check_disk, "disk"),
        "log": _safe(_check_log_dir, "log"),
        "tasks": _safe(_check_tasks, "tasks"),
        "backup": _safe(_check_backup, "backup"),
        # ★★★ v1.0 E2：连接池饱和度（压测发现的故障，监控本该先发现）
        "pool": _safe(_check_pool, "pool"),
    }
    # ★ 硬指标：db / disk / pool —— 它们失败意味着"服务无法正常提供能力"。
    #   ★ 为什么 pool 是硬指标：池耗尽时浅探针仍 200（它不查库），
    #     只有深探针能反映出来。若不设为硬指标，告警不会触发。
    hard = ("db", "disk", "pool")
    ok = all(checks[k].get("ok") for k in hard)
    degraded = [k for k, v in checks.items() if not v.get("ok")]
    return {"ok": ok, "version": config.APP_VERSION,
            "checks": checks, "degraded": degraded,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S")}


__all__ = ["shallow", "deep", "_safe", "_check_pool"]

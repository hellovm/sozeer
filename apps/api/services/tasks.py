# -*- coding: utf-8 -*-
"""任务台账工具（v0.18 T1 抽取）。

为什么单独成模块
----------------
原先这些函数定义在 `main.py` 里，导致 **agent.py 无法复用**——因为
`agent.py` 不能 import `main.py`（main → orchestrator → agent，直接成环）。
所以当时在 agent.py 里**刻意复制了一份**同名实现，靠注释提醒"别用错"。

抽到本模块后：main.py 与 agent.py 都从这里 import，**循环消失、重复消除**。
本模块只依赖 `db` / `models` / 标准库，不 import 任何上层模块。
"""
from __future__ import annotations

import asyncio
import os
import uuid

from sqlalchemy import select

from apps.api.db import SessionLocal
from apps.api.models import Task, jd, jl, now

# 进程内后台任务句柄集合（防 GC 回收；服务重启后为空）
_BG_TASKS: set = set()

# ----------------------------------------------------------------- 任务超时保护（v0.19 S3）
# 背景：线上是 `--workers 1` 单进程。如果某个后台任务（代笔 / 全书审计 / 短剧批量）
# 卡死在网络等待或死循环里，它会**永久占住事件循环的一个协程**；虽然 asyncio 是
# 单线程协作式调度、不会真把线程占满，但**卡死任务会持续泄漏 DB 连接与内存**，
# 累积下去就是站点整体变慢直至不可用。
# 对策：统一在 spawn 处包一层超时；超时后取消任务并把台账标成 abandoned，
# 让前端不再等一个永远不会到头的进度流。
TASK_TIMEOUT_SECONDS = int(os.environ.get("NOVELFORGE_TASK_TIMEOUT", "1800"))  # 默认 30 分钟


def spawn(coro, *, timeout: float | None = None,
          task_id: str = "", label: str = "") -> "asyncio.Task":
    """起一个后台任务并持有引用，防被 GC 回收。

    v0.19 S3：加**统一超时保护**。超时后：
      1) 取消协程（释放它持有的资源）
      2) 若给了 task_id，把台账行标成 abandoned + 写错误说明
         —— 否则前端会一直等一个永远不会到头的进度流
    """
    limit = float(timeout if timeout is not None else TASK_TIMEOUT_SECONDS)

    async def _guarded():
        try:
            await asyncio.wait_for(coro, timeout=limit)
        except asyncio.TimeoutError:
            _mark_timed_out(task_id, label, limit)
        except asyncio.CancelledError:
            # 服务关停 / 被主动取消：不视为失败，但要留痕
            _mark_cancelled(task_id, label)
            raise
        except Exception as e:      # 任务自身的异常由 runner 内部处理；这里兜底防静默
            import logging
            logging.getLogger("novelforge").exception(
                "后台任务异常（label=%s, task=%s）", label or "?", task_id or "?")

    t = asyncio.create_task(_guarded(), name=label or f"nf-task-{len(_BG_TASKS)}")
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)
    return t


def _mark_timed_out(task_id: str, label: str, limit: float) -> None:
    """超时：把台账标 abandoned（前端据此停止等待）。"""
    import logging
    _log = logging.getLogger("novelforge")
    _log.warning("后台任务超时（%s 秒）：label=%s task=%s",
                 int(limit), label or "?", task_id or "?")
    if not task_id:
        return
    db = SessionLocal()
    try:
        t = db.get(Task, task_id)
        if t is None or t.status in ("done", "error", "abandoned"):
            return
        t.status = "abandoned"
        t.progress = f"任务超时（>{int(limit)}s），已中止"
        t.error = (f"执行超过 {int(limit)} 秒未完成，系统已中止该任务。"
                   f"常见原因：模型接口长时间无响应。可重试，或调大 "
                   f"NOVELFORGE_TASK_TIMEOUT 环境变量。")[:512]
        t.finished_at = now()
        t.updated_at = t.finished_at
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _mark_cancelled(task_id: str, label: str) -> None:
    """被取消（服务关停）：标 abandoned，避免前端死等。"""
    if not task_id:
        return
    db = SessionLocal()
    try:
        t = db.get(Task, task_id)
        if t is None or t.status in ("done", "error", "abandoned"):
            return
        t.status = "abandoned"
        t.progress = "任务被中断（服务重启或主动取消）"
        t.finished_at = now()
        t.updated_at = t.finished_at
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def live_task_count() -> int:
    """当前在跑的后台任务数（深度健康探针用）。"""
    return sum(1 for t in _BG_TASKS if not t.done())


def task_row(db, task_id: str):
    """按 id 取任务行（薄封装，便于处处统一）。"""
    return db.get(Task, task_id)


def append_task_event(db, task_id: str, event: dict) -> int:
    """往任务的过程事件流里追加一条（DB 持久化），返回新的 seq。"""
    t = task_row(db, task_id)
    if t is None:
        return 0
    events = jl(t.events_json, []) or []
    events.append(event)
    t.events_json = jd(events)
    t.seq = int(t.seq or 0) + 1
    t.updated_at = now()
    if event.get("kind") in ("stage", "action"):
        t.progress = str(event.get("text") or event.get("summary") or t.progress)[:128]
    db.commit()
    return t.seq


def task_event(db, task, event: dict, progress: str | None = None,
               status: str | None = None, result: dict | None = None,
               error: str | None = None) -> None:
    """追一条任务事件，并可同时更新 status / progress / result / error。

    v0.18 T1：本函数原先在 main.py 与 agent.py 各有一份**刻意重复**的实现
    （因为 agent 不能 import main，否则成环）。现在两边统一 import 这里，
    重复已消除。任何驱动后台任务的代码都应使用本函数。
    """
    if task is None:
        return
    append_task_event(db, task.id, event)
    t = task_row(db, task.id)
    if t is None:
        return
    if progress is not None:
        t.progress = progress[:128]
    if status is not None:
        t.status = status
        t.finished_at = now()
    if result is not None:
        t.result_json = jd(result)
    if error:
        t.error = error[:512]
    t.updated_at = now()
    db.commit()


def new_task(db, book_id: str, kind: str, target: int = 0,
             progress: str = "准备中…") -> Task:
    """建一条 running 任务并落库。"""
    t = Task(id=f"tk_{uuid.uuid4().hex[:12]}", book_id=book_id,
             kind=kind, target=int(target or 0), status="running",
             progress=progress)
    db.add(t)
    db.commit()
    return t


def task_dict(t: Task) -> dict:
    """任务行 → 前端友好的 dict（时间统一 ISO 串）。"""
    return {
        "id": t.id, "kind": t.kind, "target": t.target, "status": t.status,
        "progress": t.progress, "seq": t.seq, "error": t.error,
        "result": jl(t.result_json, {}),
        "events": jl(t.events_json, []),
        "started_at": t.started_at.isoformat() if t.started_at else "",
        "updated_at": t.updated_at.isoformat() if t.updated_at else "",
        "finished_at": t.finished_at.isoformat() if t.finished_at else "",
    }


def recover_tasks() -> None:
    """启动兜底：把上次进程遗留的 running 任务标成 abandoned。

    服务重启后进程内 _BG_TASKS 是空的，DB 里还挂着 running 的任务其实已经
    无人执行了——不标出来的话，前端刷新会一直等一个永远不会到头的进度流。
    """
    db = SessionLocal()
    try:
        stale = db.scalars(select(Task).where(Task.status == "running")).all()
        for t in stale:
            t.status = "abandoned"
            t.progress = "服务重启，任务被中断（未保存完整结果）"
            t.finished_at = now()
            t.updated_at = t.finished_at
        if stale:
            db.commit()
    finally:
        db.close()


__all__ = [
    "spawn", "task_row", "append_task_event", "task_event",
    "new_task", "task_dict", "recover_tasks", "live_task_count",
    "TASK_TIMEOUT_SECONDS", "_BG_TASKS",
]

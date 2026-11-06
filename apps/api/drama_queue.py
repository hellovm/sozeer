"""短剧媒体任务队列（v0.13.15 C1 · ArcReel 定点移植）。

设计来源：ArcReel lib/generation/（generation_queue/generation_worker/restart_recovery）
+ lib/db/models/task.py 的字段设计。为贴合本项目 SQLite 同步 SQLAlchemy 风格做的取舍：

- 单进程 asyncio worker（ArcReel 的 WorkerLease 多 worker 租约不移植——单进程无竞争）；
- 分道并发：image=2 / video=1 / audio=2（ArcReel CapacityTable 的静态版：
  视频生成最贵最慢，单道串行；图/音可以双道）；
- 断点续跑两级：
  1) worker 循环内单任务失败 → attempts < max_attempts 重新入队（走 checkpoint）；
  2) 进程重启 → _recover_drama_tasks() 把遗留 running 行改回 queued 重跑；
     产物已在磁盘上的任务直接标 skipped（不花钱重生成）。
- 台账兼容：每个批次仍建一条 Task 台账行（SSE/UI 全复用 v0.13.13 的
  mountTaskStream 与 /tasks/{id}/stream），worker 把批次进度聚合进台账事件。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.models import DramaTask, DramaScene, DramaAsset, DramaProject, Book, jd, jl, now

# 分道并发上限（ArcReel CapacityTable 静态版；视频贵且慢，单道串行防排队爆炸）
# v0.13.16 C4：grid（多宫格）/ jianying（剪映导出）各占独立单道
LANE_CONCURRENCY = {"image": 2, "video": 1, "audio": 2, "asset_image": 2,
                    "grid": 1, "jianying": 1}
# 视频产物依赖图像：build 依赖链在 enqueue 时落 dependency_id
_POLL_INTERVAL = 0.8
_MAX_WAIT_DEPENDENCY_S = 30 * 60  # 依赖任务最长等待 30 分钟，超时判失败


def new_batch_id() -> str:
    return f"db_{uuid.uuid4().hex[:12]}"


def enqueue(
    db: Session, *, book_id: str, project_id: str, episode: int,
    task_type: str, resource_type: str, resource_id: str, resource_key: str,
    payload: dict | None = None, batch_id: str = "", dependency_id: str = "",
    ledger_task_id: str = "",
) -> DramaTask:
    """入队一行。调用方负责 commit（批量入队时一次 commit 全部落库）。"""
    t = DramaTask(
        id=f"dt_{uuid.uuid4().hex[:12]}", book_id=book_id, project_id=project_id,
        episode=episode, batch_id=batch_id, task_type=task_type,
        resource_type=resource_type, resource_id=resource_id,
        resource_key=resource_key[:64], dependency_id=dependency_id,
        payload_json=jd(payload or {}), ledger_task_id=ledger_task_id,
    )
    db.add(t)
    return t


def dedupe_key(t: DramaTask) -> tuple:
    return (t.task_type, t.resource_type, t.resource_id)


def active_duplicates(db: Session, t: DramaTask) -> int:
    """同资源同类型还在 queued/running 的其他行数（防重复入队）。"""
    rows = db.scalars(select(DramaTask).where(
        DramaTask.task_type == t.task_type,
        DramaTask.resource_type == t.resource_type,
        DramaTask.resource_id == t.resource_id,
        DramaTask.status.in_(("queued", "running")),
    )).all()
    return sum(1 for x in rows if x.id != t.id)


# ------------------------------------------------------------------ worker

class _Worker:
    """单进程分道 worker。ensure_started() 幂等启动，stop() 优雅停。"""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._stopping = False

    def ensure_started(self) -> None:
        if self._stopping:
            return
        for lane, cap in LANE_CONCURRENCY.items():
            for i in range(cap):
                key = f"{lane}:{i}"
                if key not in self._tasks or self._tasks[key].done():
                    self._tasks[key] = asyncio.create_task(self._loop(lane, key))

    async def stop(self) -> None:
        self._stopping = True
        for k, tk in list(self._tasks.items()):
            tk.cancel()

    async def _loop(self, lane: str, key: str) -> None:
        from apps.api.db import SessionLocal
        while not self._stopping:
            task_id = None
            db = SessionLocal()
            try:
                row = _claim(db, lane)
                # 关键：Session 未关闭时先取 id 字符串。commit 后 ORM 对象已过期，
                # close 后再访问属性会 DetachedInstanceError 且被 _execute 静默吞掉
                # （v0.13.15 线上 e2e 实证：行卡 running、attempts=0、无任何事件）。
                if row is not None:
                    task_id = row.id
            except Exception:
                task_id = None
            finally:
                db.close()
            if task_id is None:
                await asyncio.sleep(_POLL_INTERVAL)
                continue
            await self._execute(task_id)
            # 让出事件循环，避免一个慢任务独占分道

    async def _execute(self, task_id: str) -> None:
        """执行单个任务。task_id 必须传字符串 id：claim Session 关闭后 ORM
        对象即分离，跨 Session 传对象是本模块的原始 bug（已修）。"""
        from apps.api.db import SessionLocal
        db = SessionLocal()
        ok = False
        err = ""
        t0 = time.time()
        try:
            # 每任务独立 Session；按 id 重读（claim 在另一个 Session 里完成）
            t = db.get(DramaTask, task_id)
            if t is None or t.status != "running":
                return
            # 断点续跑第一级：产物已在 → skipped（不重复花钱）
            if _artifact_exists(t):
                _finish(db, t, "skipped", result={"skipped": True, "reason": "产物已存在"})
                _batch_progress_event(db, t, f"⤼ {t.resource_key} 已有产物，跳过")
                return
            db.expire_all()
            t = db.get(DramaTask, task_id)
            if t is None or t.status != "running":
                return
            fn = _EXECUTORS.get(t.task_type)
            if fn is None:
                _finish(db, t, "error", error=f"未知任务类型 {t.task_type}")
                return
            r = await fn(db, t)
            # E4.23：前置未就绪（如角色定妆图还在生成）→ 回队等待。
            # 关键：**不消耗 attempts**（与"执行失败重试"是两回事）——
            # 等待是正常状态，不是错误；否则等待 2 轮就会把任务打成 error。
            if r.get("defer"):
                t.status = "queued"
                t.updated_at = now()
                db.commit()
                _batch_progress_event(db, t, f"⏳ {t.resource_key} 等待前置，稍后重跑")
                return
            ok = bool(r.get("ok"))
            err = r.get("error") or ""
            if ok:
                _finish(db, t, "done", result=r)
                _batch_progress_event(db, t, f"✓ {t.resource_key} 完成")
            else:
                raise RuntimeError(err or "生成失败")
        except asyncio.CancelledError:
            # 进程关闭：标回 queued，下次启动重跑（checkpoint 在）
            try:
                t2 = db.get(DramaTask, task_id)
                if t2 is not None and t2.status == "running":
                    t2.status = "queued"
                    t2.updated_at = now()
                    db.commit()
            except Exception:
                pass
            raise
        except Exception as e:
            ok = False
            err = f"{type(e).__name__}: {e}"
            try:
                db.rollback()
                t = db.get(DramaTask, task_id)
                if t is None:
                    return
                t.attempts = (t.attempts or 0) + 1
                if t.attempts >= (t.max_attempts or 2):
                    _finish(db, t, "error", error=err[:512])
                    _batch_progress_event(db, t, f"✗ {t.resource_key} 失败：{err[:120]}")
                else:
                    # 第二级断点续跑：重新入队（保留 checkpoint）
                    t.status = "queued"
                    t.error = err[:512]
                    t.updated_at = now()
                    db.commit()
                    _batch_progress_event(db, t, f"↻ {t.resource_key} 第 {t.attempts} 次失败，重试中")
            except Exception:
                pass
        finally:
            db.close()


def _claim(db: Session, lane: str) -> DramaTask | None:
    """认领一行 queued 任务：依赖已满足 + 同道无占位 + 按 created_at FIFO。"""
    db.expire_all()  # 长生命周期轮询 Session：先过期再读，identity map 静默吞写入的教训
    rows = db.scalars(select(DramaTask).where(DramaTask.status == "queued")
                      .order_by(DramaTask.created_at).limit(50)).all()
    done_ids: set[str] = set()
    running_ids: set[str] = set()
    for t in db.scalars(select(DramaTask).where(
            DramaTask.status.in_(("done", "skipped", "running")))).all():
        (done_ids if t.status in ("done", "skipped") else running_ids).add(t.id)
    for t in rows:
        if t.task_type != lane and not (lane == "asset_image" and t.task_type == "asset_image"):
            continue
        if t.task_type == "asset_image" and lane != "asset_image":
            continue
        # 依赖检查：依赖行必须 done/skipped；依赖已 error 则本行直接失败
        if t.dependency_id:
            dep = db.get(DramaTask, t.dependency_id)
            if dep is None:
                _finish(db, t, "error", error="前置任务不存在")
                continue
            if dep.status in ("done", "skipped"):
                pass
            elif dep.status == "error":
                _finish(db, t, "error", error=f"前置任务失败：{(dep.error or '')[:120]}")
                _batch_progress_event(db, t, f"✗ {t.resource_key} 前置失败，跳过")
                continue
            else:
                if time.time() - _created_ts(t) > _MAX_WAIT_DEPENDENCY_S:
                    _finish(db, t, "error", error="等待前置任务超时")
                    _batch_progress_event(db, t, f"✗ {t.resource_key} 等待前置超时，失败")
                    continue
                continue  # 依赖未完成，本轮不认领
        t.status = "running"
        t.started_at = now()
        t.updated_at = now()
        db.commit()
        return t
    return None


def _created_ts(t: DramaTask) -> float:
    try:
        return t.created_at.timestamp() if t.created_at else 0.0
    except Exception:
        return 0.0


def _artifact_exists(t: DramaTask) -> bool:
    """断点续跑检查（批次级粗查）：资产图已有文件 → skip。

    分镜产物的精确检查在执行器里做（需要先读 DramaScene 行）。
    """
    try:
        if t.resource_type == "asset":
            from apps.api.db import SessionLocal as _SL
            d2 = _SL()
            try:
                a = d2.get(DramaAsset, t.resource_id)
                return a is not None and a.image_status == "done" and _file_ok(a.image)
            finally:
                d2.close()
        return False
    except Exception:
        return False


def _finish(db: Session, t: DramaTask, status: str, result: dict | None = None,
            error: str = "") -> None:
    t.status = status
    t.finished_at = now()
    t.updated_at = now()
    if result is not None:
        t.result_json = jd(result)
    if error:
        t.error = error[:512]
    db.commit()
    # v0.13.17 修复：批次全部任务到终态后必须收尾台账。旧版只发进度事件，
    # 台账 Task 永远停在 running → SSE 流不结束 → 前端 onDone 永不触发
    # （批量生成/宫格图/剪映导出的完成回调全部失联）。
    finalize_batch_if_complete(db, t.batch_id, t.ledger_task_id)

    # ★★ v1.0 E4 运营准备：任务终态的个人通知
    #
    #   ★ 为什么必须发：短剧生成是**长任务**（可能几分钟）。
    #     不发通知的话，用户只能反复刷新页面看好了没 —— 体验很差。
    #     ★ 失败更要主动告知：否则用户以为"还在跑"，白等。
    #
    #   ★ fail-open：发通知失败绝不影响任务状态（状态已 commit 了）。
    try:
        from apps.api import ops_touch
        _uid = getattr(t, "user_id", "") or ""
        if _uid and status in ("done", "error"):
            _ctx = {}
            try:
                if result:
                    _ctx = {"episode": result.get("episode", 0),
                            "scenes": result.get("scenes", 0)}
            except Exception:                           # noqa: BLE001
                _ctx = {}
            if status == "done":
                ops_touch.on_drama_done(
                    db, _uid, project_id=getattr(t, "project_id", "") or "",
                    title=getattr(t, "title", "") or "",
                    episode=int(_ctx.get("episode") or 0),
                    scenes=int(_ctx.get("scenes") or 0))
            else:
                ops_touch.on_drama_failed(
                    db, _uid, project_id=getattr(t, "project_id", "") or "",
                    title=getattr(t, "title", "") or "",
                    episode=int(_ctx.get("episode") or 0),
                    reason=error or "生成过程出错")
    except Exception as _e:                             # noqa: BLE001
        import logging
        logging.getLogger("novelforge").debug(
            "短剧终态通知异常（已忽略）：%s: %s", type(_e).__name__, _e)


_TERMINAL = ("done", "skipped", "error", "cancelled")


def finalize_batch_if_complete(db: Session, batch_id: str,
                               ledger_task_id: str = "") -> bool:
    """批次内全部任务进入终态后，把台账 Task 收尾（幂等，可安全重复调用）。

    复刻旧 _run_drama_batch_flow 的收尾语义：追加 kind=done 事件（status
    ok/partial/error）+ 台账行置 done/error，SSE 流据此结束并触发前端 onDone。
    """
    if not batch_id:
        return False
    try:
        if not ledger_task_id:
            ledger_task_id = db.scalar(select(DramaTask.ledger_task_id).where(
                DramaTask.batch_id == batch_id,
                DramaTask.ledger_task_id != "").limit(1)) or ""
        if not ledger_task_id:
            return False
        statuses = db.scalars(select(DramaTask.status).where(
            DramaTask.batch_id == batch_id)).all()
        if not statuses or any(s not in _TERMINAL for s in statuses):
            return False  # 还有 queued/running，不收尾
        from apps.api import main as mn
        row = mn._task_row(db, ledger_task_id)
        if row is None or row.status != "running":
            return False  # 已收尾过（幂等）或台账行已不存在
        n_ok = sum(1 for s in statuses if s in ("done", "skipped"))
        n_err = sum(1 for s in statuses if s == "error")
        n_cancel = sum(1 for s in statuses if s == "cancelled")
        total = len(statuses)
        if n_err:
            ev_status, task_status = ("partial" if n_ok else "error"), "error"
            text = f"■ 批次结束：完成 {n_ok}/{total}，失败 {n_err}"
            progress = f"完成 {n_ok}/{total}，失败 {n_err}"
            err = f"{n_err}/{total} 个任务失败"
        elif n_cancel:
            ev_status, task_status = "ok", "done"
            text = f"⏹ 批次已取消：完成 {n_ok}/{total}"
            progress = f"已取消（完成 {n_ok}/{total}）"
            err = ""
        else:
            ev_status, task_status = "ok", "done"
            text = f"■ 批次完成：{n_ok}/{total}"
            progress = f"完成 {n_ok}/{total}"
            err = ""
        mn._task_event(db, row,
                       {"kind": "done", "status": ev_status, "text": text,
                        "ok": n_ok, "failed": n_err, "total": total},
                       status=task_status, progress=progress,
                       result={"ok": n_ok, "failed": n_err,
                               "cancelled": n_cancel, "total": total},
                       error=err)
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False


def _batch_progress_event(db: Session, t: DramaTask, text: str) -> None:
    """把单任务进度聚合进批次台账（ledger_task_id 指向 Task 行）。"""
    if not t.ledger_task_id:
        return
    try:
        from apps.api import main as mn
        row = mn._task_row(db, t.ledger_task_id)
        if row is None or row.status in ("done", "error", "abandoned"):
            return  # 批次已收尾：别再覆盖终态进度文本
        mn._append_task_event(db, t.ledger_task_id, {"kind": "action", "text": text})
        done = db.scalar(select(DramaTask.id).where(
            DramaTask.batch_id == t.batch_id,
            DramaTask.status.in_(("done", "skipped", "error"))))
        total = db.scalar(select(DramaTask.id).where(DramaTask.batch_id == t.batch_id))
        if total:
            row.progress = f"{done}/{total}"
        db.commit()
    except Exception:
        pass


# ------------------------------------------------------------------ 执行器

async def _exec_scene_media(db: Session, t: DramaTask) -> dict:
    """分镜图像/视频/音频执行。断点续跑：产物路径已存在直接成功。

    E4.23：image/video 任务在「本镜参考角色的定妆图」还在途时先等待
    （defer 回队，不耗 attempts）——资产自动管线入队的 asset_image 任务
    与分镜任务同批并发，这里保证**先有定妆图再生分镜**的一致性语义。
    等待上限 30 分钟（_MAX_WAIT_DEPENDENCY_S），超时不再等、直接生成
    （fail-open：参考图届时只收集已就绪的，不阻塞分镜交付）。
    """
    from apps.api import drama as drama_mod, media as media_mod
    p = db.get(DramaProject, t.project_id)
    s = db.get(DramaScene, t.resource_id)
    if p is None or s is None:
        return {"ok": False, "error": "项目或分镜不存在"}
    target = t.task_type
    if target in ("image", "video"):
        try:
            if drama_mod.pending_ref_asset_tasks(db, p, s):
                payload = jl(t.payload_json, {}) or {}
                since = float(payload.get("asset_wait_since") or 0.0)
                timed_out = bool(since) and (time.time() - since) > _MAX_WAIT_DEPENDENCY_S
                if not timed_out:
                    if not since:
                        # 首次等待：记录起点（payload 跨轮次/断点续跑保留）
                        payload["asset_wait_since"] = time.time()
                        t.payload_json = jd(payload)
                        db.commit()
                    return {"defer": True}
                # 超时：不再等，降级继续生成（参考图只收集已就绪的）
        except Exception:
            pass  # 等待判定异常 → 放行（fail-open，绝不卡死分镜）
    # 断点续跑：分镜产物已在（重启恢复场景），不重复生成
    if target == "image" and s.media_status == "image_done" and _file_ok(s.image_path):
        return {"ok": True, "path": s.image_path, "resumed": True}
    if target == "video" and s.media_status == "done" and _file_ok(s.video_path):
        return {"ok": True, "path": s.video_path, "resumed": True}
    if target == "audio" and _file_ok(s.audio_path):
        return {"ok": True, "path": s.audio_path, "resumed": True}
    r = await drama_mod.generate_scene_media(db, p, s, target)
    if r.get("ok") and t.ledger_task_id:
        pass  # 进度事件由 _batch_progress_event 统一发
    return r


def _file_ok(path: str) -> bool:
    if not path:
        return False
    # path 可能是 URL（/api/files/...）或相对路径（book_id/drama_media/x.png）
    from apps.api import config
    rel = path.split("/api/files/", 1)[-1]
    try:
        return (config.DATA_DIR / rel).exists()
    except Exception:
        return False


async def _exec_asset_image(db: Session, t: DramaTask) -> dict:
    """资产定妆图执行。断点续跑：asset.image 已有文件直接成功。"""
    from apps.api import drama as drama_mod
    p = db.get(DramaProject, t.project_id)
    a = db.get(DramaAsset, t.resource_id)
    if p is None or a is None:
        return {"ok": False, "error": "项目或资产不存在"}
    if a.image_status == "done" and _file_ok(a.image):
        return {"ok": True, "path": a.image, "resumed": True}
    r = await drama_mod.generate_asset_image(db, p, a)
    return r


async def _exec_grid(db: Session, t: DramaTask) -> dict:
    """C4-1 多宫格分镜执行：payload 里带本批分镜 id 列表。

    resource_id 存批次聚合 key（如 ep1_grid_9），产物是整图，不写回 scene 行。
    断点续跑：payload 里已带上次产物路径且文件在 → 直接成功。
    """
    from apps.api import drama as drama_mod
    p = db.get(DramaProject, t.project_id)
    if p is None:
        return {"ok": False, "error": "项目不存在"}
    payload = jl(t.payload_json, {}) or {}
    ids: list[str] = payload.get("scene_ids") or []
    if payload.get("path") and _file_ok(payload["path"]):
        return {"ok": True, "path": payload["path"], "resumed": True}
    scenes = [db.get(DramaScene, sid) for sid in ids]
    scenes = [s for s in scenes if s is not None]
    if not scenes:
        return {"ok": False, "error": "宫格目标分镜不存在"}
    r = await drama_mod.generate_grid_image(db, p, scenes)
    if r.get("ok"):
        # 产物路径写回 payload 供断点续跑识别
        try:
            t.payload_json = jd({**payload, "path": r.get("path") or ""})
            db.commit()
        except Exception:
            db.rollback()
    return r


async def _exec_jianying(db: Session, t: DramaTask) -> dict:
    """C4-2 剪映草稿导出执行：组装 draft JSON + 连素材打包 zip 落盘。

    resource_id 存 project_id，episode 从行上取。产物 zip 路径进 result_json。
    """
    from apps.api import drama as drama_mod
    p = db.get(DramaProject, t.project_id)
    if p is None:
        return {"ok": False, "error": "项目不存在"}
    payload = jl(t.payload_json, {}) or {}
    ids: list[str] = payload.get("scene_ids") or []
    scenes = [db.get(DramaScene, sid) for sid in ids]
    scenes = [s for s in scenes if s is not None]
    if not scenes:
        return {"ok": False, "error": "没有可导出的分镜"}
    built = drama_mod.build_jianying_draft(p, t.episode, scenes)
    if not built.get("ok"):
        return {"ok": False, "error": built.get("error") or "草稿组装失败"}
    zip_rel = _write_jianying_zip(db, p, t.episode, built["draft"])
    if not zip_rel:
        return {"ok": False, "error": "zip 打包失败"}
    return {"ok": True, "path": zip_rel, "video_count": built.get("video_count", 0),
            "audio_count": built.get("audio_count", 0),
            "text_count": built.get("text_count", 0),
            "duration_seconds": built.get("duration_seconds", 0)}


def _write_jianying_zip(db: Session, p: DramaProject, episode: int, draft: dict) -> str:
    """把 draft_content.json + 引用的素材文件打包成 zip，落书籍仓库 drama_media/。

    剪映草稿是「目录 + draft_content.json + 素材」结构；zip 里按
    jianying_ep{n}/draft_content.json + materials/ 布局，用户解压后把
    jianying_ep{n} 文件夹放进剪映 drafts 目录即可识别（素材相对路径已内嵌）。
    """
    import io as _io
    import os as _os
    import zipfile as _zipfile
    from apps.api.config import BOOKS_DIR
    from apps.api import gitrepo
    try:
        buf = _io.BytesIO()
        with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"jianying_ep{episode}/draft_content.json",
                        json.dumps(draft, ensure_ascii=False, indent=1))
            seen: set[str] = set()
            for track in draft.get("tracks", []):
                for seg in track.get("segments", []):
                    src = seg.get("path") or ""
                    if not src or src in seen:
                        continue
                    seen.add(src)
                    try:
                        from pathlib import Path as _P
                        fp = _P(src)
                        if fp.is_file():
                            zf.write(fp, f"jianying_ep{episode}/materials/"
                                     f"{_os.path.basename(str(fp))}")
                    except Exception:
                        continue
        gitrepo.write_file(p.book_id, f"drama_media/.keep", "", commit_msg="")
        rel = f"drama_media/jianying_ep{episode}_{p.id[:8]}.zip"
        dest = BOOKS_DIR / p.book_id / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(buf.getvalue())
        return rel
    except Exception:
        return ""


_EXECUTORS = {
    "image": _exec_scene_media,
    "video": _exec_scene_media,
    "audio": _exec_scene_media,
    "asset_image": _exec_asset_image,
    "grid": _exec_grid,
    "jianying": _exec_jianying,
}


# ------------------------------------------------------------------ 生命周期

_worker: _Worker | None = None


def ensure_worker() -> None:
    """app 启动时调用：启动分道 worker + 恢复上次进程遗留的 running 行。

    兼容两种调用环境：FastAPI 事件循环内（startup 钩子）与同步上下文
    （e2e 脚本/线程池）——后者没有 running loop，用独立线程自建 loop 跑。

    ★★ v0.26.2：支持 `NOVELFORGE_DISABLE_WORKERS=1` 关闭后台 worker。
      为什么需要（真实踩坑）：`_worker` 是**模块级全局**，一旦启动就跨
      pytest 用例存活，并持有 `SessionLocal()` 会话在轮询。测试为隔离
      会把 SessionLocal monkeypatch 到临时库，此时遗留 worker 与新
      teardown 竞争 → `IllegalStateChangeError`（单跑过、全量随机挂）。
      → 测试由 conftest 设置该环境变量，彻底避免此类跨测试污染。
      ★ 生产环境不设该变量，行为与升级前完全一致（零回归）。
    """
    if os.environ.get("NOVELFORGE_DISABLE_WORKERS", "").strip() in ("1", "true", "yes"):
        return

    global _worker
    if _worker is None:
        _worker = _Worker()
    _recover_drama_tasks()
    try:
        asyncio.get_running_loop()
        _worker.ensure_started()          # 事件循环内：正常路径
    except RuntimeError:
        # 同步上下文：独立线程自建事件循环跑 worker
        import threading

        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def _boot():
                _worker.ensure_started()
                await asyncio.Event().wait()   # 永久挂起，loop 一直跑 worker 协程

            loop.run_until_complete(_boot())

        threading.Thread(target=_run, daemon=True, name="drama-queue-worker").start()


def _recover_drama_tasks() -> None:
    """进程重启恢复：running → queued（重跑）；批次台账补终态事件。"""
    from apps.api.db import SessionLocal
    db = SessionLocal()
    try:
        rows = db.scalars(select(DramaTask).where(DramaTask.status == "running")).all()
        for t in rows:
            t.status = "queued"
            t.updated_at = now()
        if rows:
            db.commit()
        # 遗留 queued 批次：其台账行如果还 running 会由 Task 台账的 recover 标 abandoned，
        # 这里重建台账关联：给该批次的 queued 行补一条新台账（简单起见：不重建，靠行级进度）。
    finally:
        db.close()

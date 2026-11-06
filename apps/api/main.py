"""NovelForge API —— P0 骨架。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import (Depends, FastAPI, HTTPException, File, UploadFile,
                     Request)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api import auth, config, exporter, gitrepo, importer, llm
from apps.api import audit as audit_mod
from apps.api import characters as char_mod
from apps.api import consistency, keyvault
from apps.api.db import SessionLocal, engine, get_db, init_db
from apps.api.models import (
    Book, Chapter, Character, Foreshadow, ModelProfile, Run, Setting, User,
    StyleProfile, Volume, WorldSetting, jd, jl, now,
    AuditReport, LedgerEntry, TimelineEvent, Conversation, Message, Task,
    DramaProject, DramaEpisode, DramaAsset, DramaScene, DramaTask,
    CharacterKnowledge, CharacterRelation,
)
from apps.api.orchestrator import gates, graph
# v0.18 T1：任务台账/设置/迁移等纯工具抽到 services 包（打破 main↔agent 循环）。
# 这里 re-import 回旧名（_task_row 等），保证 main 命名空间与老调用点零改动。
from apps.api.services.tasks import (
    spawn as _spawn, task_row as _task_row,
    append_task_event as _append_task_event, task_event as _task_event,
    new_task as _new_task, task_dict as _task_dict,
    recover_tasks as _recover_tasks,
)
from apps.api.services.settings import (
    get_setting, set_setting as _set_setting, is_register_open,
)
from apps.api.services.db_migrate import migrate as _migrate
from apps.api.services.scoring import (
    book_score as _book_score, next_step as _next_step,
)
from apps.api.skills import loader as skills
from apps.api import agent as agent_mod
from apps.api import drama as drama_mod

_docs = {} if config.DOCS_ENABLED else {
    "docs_url": None, "redoc_url": None, "openapi_url": None}
app = FastAPI(title="NovelForge", version=config.APP_VERSION, **_docs)




# v0.19 S2：初始化日志（文件 + stderr）。目录不可写时自动降级，绝不阻断启动。
try:
    from apps.api.app_logging import setup_logging as _setup_logging
    _setup_logging()
except Exception as _log_e:      # 日志配置失败不能拖垮服务
    import sys as _sys
    print(f"[main] 日志初始化失败（降级为默认）：{type(_log_e).__name__}: {_log_e}",
          file=_sys.stderr, flush=True)

init_db()
_migrate()

# v0.15 题材包种子：把内置题材写入 genre_packs（幂等，已存在则跳过，不覆盖改动）。
# 失败不阻断启动——genres.py 的内置包兜底照常工作。
try:
    from apps.api import genres as _genres
    with SessionLocal() as _gdb:
        _seed = _genres.seed_builtin(_gdb)
        _genres.invalidate_cache()
        if _seed:
            logging.getLogger("uvicorn").info("题材包种子写入：%d 个内置题材", _seed)
except Exception as _ge:
    logging.getLogger("uvicorn").warning("题材包种子失败（用内置兜底）: %s", _ge)


# 启动迁移：历史明文 API Key 就地加密（幂等）。放在会话生命周期里做，
# 失败不阻断启动——下次启动会再试，且读取层对明文行有兼容回退。
try:
    with SessionLocal() as _db:
        _n = keyvault.encrypt_all_plaintext_keys(_db)
        if _n:
            logging.getLogger("uvicorn").info("api-key 加密迁移完成：%d 条", _n)
except Exception as _e:
    logging.getLogger("uvicorn").warning("api-key 加密迁移失败（下次启动重试）: %s", _e)

if config.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ═══════════════════════════════════════════════ 全局异常兜底（v1.0-E4.9）
#
# ★★ 为什么需要（全端点冒烟扫出来的教训）：
#   383 个端点里，多个端点因为**未处理的 ValueError / OSError** 返回 500：
#     · 超长 book_id → OSError（文件路径过长）
#     · 非法数字转换 → ValueError
#   → 用户看到 "Internal Server Error"，既不知道错在哪，也无法自救。
#
# ★ 判据：**「输入不合法」应该返回 4xx，而不是 5xx**。
#   5xx 的语义是"服务端出错"，会让客户端/平台误以为可以重试。
#
# ★★ 为什么用全局处理器而不是逐个端点 try/except：
#   逐个改要动 11 个调用点，且**新增端点还会再犯**。
#   全局处理器是**一次性的系统性方案** —— 覆盖所有现在与未来的端点。
#
# ★ 注意：这里只处理"可预期的输入错误"，
#   真正的服务端 bug（如 NameError/AttributeError）**仍应 500**，
#   否则会掩盖问题（这才是我们想看到的信号）。
@app.exception_handler(ValueError)
async def _value_error_handler(request: Request, exc: ValueError):
    """输入非法（如 book_id 格式错、数字解析失败）→ 400。

    ★ 为什么单独处理 ValueError：
      它是"输入不合法"的最典型信号，且**抛出点可能很深**
      （如 gitrepo.safe_book_id），逐个 try 不现实。
    """
    from fastapi.responses import JSONResponse as _JR
    _log.warning("输入校验失败 %s %s：%s",
                 request.method, request.url.path, str(exc)[:200])
    return _JR({"detail": str(exc) or "请求参数不合法"}, status_code=400)


@app.exception_handler(OSError)
async def _os_error_handler(request: Request, exc: OSError):
    """文件系统类错误（路径过长、权限不足等）→ 400。

    ★ 为什么会走到这里：路径由用户输入拼接时（如 book_id）
      可能触发 `File name too long` / `Permission denied`。
      ★ 这类错误**不是"服务端故障"**，是"你的输入导致的操作失败"。
      ★ 但要注意：如果是**代码写死**的路径出错，那就是真 bug ——
        所以这里打 error 级日志（便于区分）。
    """
    from fastapi.responses import JSONResponse as _JR
    _log.error("文件操作失败 %s %s：%s",
               request.method, request.url.path, str(exc)[:200])
    return _JR({"detail": "文件操作失败（可能是路径过长或权限不足）"},
               status_code=400)


# ----------------------------------------------------------------- 鉴权中间件
# 取代 nginx 基础认证：所有 /api 除白名单外必须带有效 Bearer 令牌；
# 书籍子路由额外校验归属（非本人且非管理员 → 403）。
# 原先这里还列了 /api/docs、/api/openapi.json、/api/redoc —— 这三个路由在项目里
# 根本不存在（真实路径是 /docs、/openapi.json、/redoc，且不以 /api 开头，
# 连中间件都不进）。留着只会制造"文档已受保护"的错觉，实际谁都能免令牌拿完整 schema。
# 现在文档是否开放由 config.DOCS_ENABLED 控制，见下面 app = FastAPI(...)。
# 后台 SSE 任务必须持有强引用。事件循环只保存 Task 的弱引用，
# Task 执行途中被 GC 会静默取消（Python 官方文档明确警告过这个坑），
# 表现为流跑到一半莫名结束、没有任何报错。
_BG_TASKS: set = set()




# ----------------------------------------------------------------- 任务台账
# 长任务（代笔 / 全书审计）一旦开跑，前端刷新不能失忆。这里做两件事：
# 1) 进程内 _LIVE_TASKS 记录「此刻还有哪个任务在跑」（强引用，防 GC 静默取消）；
# 2) DB 里 Task 表记录每个任务的状态/进度/结果/过程事件，刷新或重启后都能恢复。
_LIVE_TASKS: set = set()












class TaskIn(BaseModel):
    kind: str = "draft"
    target: int = 0


# ---- 批量代笔（v0.19 F1）----
class BatchDraftIn(BaseModel):
    """批量代笔入参。"""
    start_ch: int = 0        # 0 = 自动从下一章开始
    count: int = 3
    instruction: str = ""


@app.post("/api/books/{book_id}/batch-draft")
async def batch_draft(book_id: str, body: BatchDraftIn, db: Session = Depends(get_db)):
    """一次性连写多章（队列式批量代笔）。

    作者最痛的点是「一次只能催一章」。本端点提交后：
      1) 建一个批次，后台**逐章串行**跑（复用单章代笔的全部能力）
      2) 每章完成即落草稿台账，前端可逐章审核采纳
      3) 进度事件走批次任务流，可看到「第 3/5 章 生成中」
      4) 单章失败不中断整批（能出多少算多少）
    """
    from apps.api import batch_draft as bd
    b = db.get(Book, book_id)
    if not b:
        raise HTTPException(404, "book not found")

    start_ch = int(body.start_ch or 0) or bd.next_chapter_index(db, b)
    count = max(1, min(int(body.count or 1), bd.MAX_BATCH_CHAPTERS))
    batch_id = bd.new_batch_id()

    # 建一条台账任务，前端挂它的 SSE 流看整批进度
    task = _new_task(db, book_id, "batch_draft", target=count,
                     progress=f"批量代笔：第 {start_ch}–{start_ch + count - 1} 章")

    async def runner():
        db2 = SessionLocal()
        try:
            book2 = db2.get(Book, book_id)
            async def emit(ev: dict) -> None:
                _task_event(db2, _task_row(db2, task.id), ev)
            r = await bd.run_batch_draft(db2, book2, start_ch, count,
                                         instruction=body.instruction or "",
                                         emit=emit, batch_id=batch_id)
            _task_event(db2, _task_row(db2, task.id),
                        {"kind": "done", "status": "ok", "progress": r.get("summary", "批量完成")},
                        status="done", result=r)
        except Exception as e:
            _task_event(db2, _task_row(db2, task.id),
                        {"kind": "error", "text": f"批量代笔异常：{type(e).__name__}: {e}"},
                        status="error", error=f"{type(e).__name__}: {e}")
        finally:
            db2.close()

    _spawn(runner(), task_id=task.id, label=f"batch_draft:{batch_id}")
    return {"ok": True, "task_id": task.id, "batch_id": batch_id,
            "start_ch": start_ch, "count": count,
            "status": "running", "progress": task.progress}


@app.get("/api/books/{book_id}/batch-draft/limits")
def batch_draft_limits(book_id: str, db: Session = Depends(get_db)):
    """批量代笔的能力上限与建议起点（前端表单用）。"""
    from apps.api import batch_draft as bd
    b = db.get(Book, book_id)
    if not b:
        raise HTTPException(404, "book not found")
    return {"max_chapters": bd.MAX_BATCH_CHAPTERS,
            "default_chapters": bd.DEFAULT_BATCH_CHAPTERS,
            "next_chapter": bd.next_chapter_index(db, b)}


@app.post("/api/books/{book_id}/tasks")
async def create_task(book_id: str, body: TaskIn, db: Session = Depends(get_db)):
    """显式建任务（代笔 / 全书审计入口）。返回任务 id，前端据此挂 SSE 进度流。"""
    b = db.get(Book, book_id)
    if not b:
        raise HTTPException(404, "book not found")
    t = _new_task(db, book_id, body.kind, body.target)
    return {"id": t.id, "book_id": book_id, "kind": t.kind,
            "status": t.status, "progress": t.progress}


@app.get("/api/books/{book_id}/tasks")
def list_tasks(book_id: str, db: Session = Depends(get_db)):
    """列出该书的全部任务（刷新后前端据此恢复未完成任务的状态）。"""
    rows = db.scalars(select(Task).where(Task.book_id == book_id)
                      .order_by(Task.started_at.desc())).all()
    return {"items": [_task_dict(t) for t in rows]}




@app.get("/api/books/{book_id}/tasks/{task_id}")
def get_task(book_id: str, task_id: str, db: Session = Depends(get_db)):
    t = _task_row(db, task_id)
    if not t or t.book_id != book_id:
        raise HTTPException(404, "task not found")
    return _task_dict(t)


@app.get("/api/books/{book_id}/tasks/{task_id}/draft")
def get_task_draft(book_id: str, task_id: str, db: Session = Depends(get_db)):
    """从已完成代笔任务的 result 里取回章草稿（v0.13.12 刷新恢复用）。

    代笔流程把整章正文/质检/诊断存进 Task.result_json，刷新后前端凭
    task kind=draft + status=done 来这里取，重建可采纳的草稿卡。
    """
    t = _task_row(db, task_id)
    if not t or t.book_id != book_id:
        raise HTTPException(404, "task not found")
    if t.kind != "draft":
        raise HTTPException(400, "该任务不是代笔任务")
    r = jl(t.result_json, {}) or {}
    if not r.get("content"):
        raise HTTPException(404, "任务没有可恢复的草稿")
    return {"task_id": t.id, "status": t.status, "ch_idx": r.get("ch_idx"),
            "text": r.get("content"), "gate_score": r.get("gate_score"),
            "gate_report": r.get("gate_report"),
            "title": r.get("title"), "words": r.get("words"),
            "discarded": bool(r.get("discarded")), "accepted": bool(r.get("accepted"))}


@app.delete("/api/books/{book_id}/tasks/{task_id}/draft")
def discard_task_draft(book_id: str, task_id: str, db: Session = Depends(get_db)):
    """丢弃待审草稿（v0.13.18）：清 result 里的正文并打 discarded 标记。

    不删台账行——任务历史（何时生成、几轮精修、质检分）是审计记录，保留；
    只把可采纳的正文摘掉，pending-drafts 扫描与刷新恢复链路自然失效。
    """
    t = _task_row(db, task_id)
    if not t or t.book_id != book_id:
        raise HTTPException(404, "task not found")
    if t.kind != "draft":
        raise HTTPException(400, "该任务不是代笔任务")
    r = jl(t.result_json, {}) or {}
    if not r.get("content"):
        return {"ok": True, "already": True}  # 幂等：已丢弃过直接成功
    r["discarded"] = True
    r["discarded_at"] = str(now())
    r["_content_backup"] = r.pop("content", "")
    t.result_json = jd(r)
    t.updated_at = now()
    db.commit()
    return {"ok": True, "ch_idx": r.get("ch_idx", 0)}


@app.post("/api/books/{book_id}/tasks/{task_id}/draft/restore")
def restore_task_draft(book_id: str, task_id: str, db: Session = Depends(get_db)):
    """误删反悔：把 backup 拿回来（同一会话内有效，不清台账就没这个负担）。"""
    t = _task_row(db, task_id)
    if not t or t.book_id != book_id:
        raise HTTPException(404, "task not found")
    r = jl(t.result_json, {}) or {}
    if not r.get("_content_backup"):
        raise HTTPException(404, "没有可恢复的草稿")
    r["content"] = r.pop("_content_backup")
    r["discarded"] = False
    t.result_json = jd(r)
    t.updated_at = now()
    db.commit()
    return {"ok": True, "ch_idx": r.get("ch_idx", 0)}


@app.get("/api/books/{book_id}/pending-drafts")
def list_pending_drafts(book_id: str, db: Session = Depends(get_db)):
    """待审草稿清单（v0.13.18）：扫全部代笔任务，返回「草稿比正文新」的待采纳稿。

    背景：草稿卡只活在聊天流气泡里，刷新/切对话后就找不到入口了，用户
    「系统提示去看草稿卡但找不到」。这里把待审稿做成一等公民：
    每本任务的 result_json 里有 content 的都是一稿，与正文当前版本对比，
    质检分更高或正文缺失的列为待采纳。每章可能多稿（85分/80分），全列出。
    v0.13.18：discarded=True 的行不再出现（正文已移入 _content_backup，
    天然被 `if not text: continue` 跳过）。
    """
    b = db.get(Book, book_id)
    if not b:
        raise HTTPException(404, "book not found")
    rows = db.scalars(select(Task).where(
        Task.book_id == book_id, Task.kind == "draft",
        Task.status.in_(("done", "abandoned"))).order_by(
        Task.finished_at.desc())).all()
    # 正文当前版本：ch_idx → gate_score（用于判断草稿是否比库里的新）
    cur_score = {c.idx: (c.gate_score or 0) for c in
                 db.scalars(select(Chapter).where(Chapter.book_id == book_id)).all()}
    items, seen = [], set()
    for t in rows:
        r = jl(t.result_json, {}) or {}
        text = r.get("content")
        if not text:
            continue
        if r.get("accepted"):
            # v1.0-E4.18：已采纳消费过的稿不再出现。正常消费会把 content
            # 摘进 _content_backup（上面 if not text 已拦），这里兜底
            # 「accepted=True 但 content 仍残留」的历史脏数据行。
            continue
        ch = int(r.get("ch_idx") or 0)
        # 同一章多稿都保留（用户可能想翻 85 版和 80 版），只去重完全相同的任务
        key = (t.id)
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "task_id": t.id, "ch_idx": ch, "title": r.get("title") or "",
            "gate_score": r.get("gate_score"), "words": r.get("words", 0),
            "chapter_score": cur_score.get(ch, 0),
            "newer": (r.get("gate_score") or 0) > cur_score.get(ch, 0),
            "created_at": str(t.started_at or ""),
            "text": text,
            "gate_report": r.get("gate_report"),
        })
    return {"items": items}


@app.post("/api/books/{book_id}/tasks/{task_id}/events")
def append_task_event(book_id: str, task_id: str, body: dict, db: Session = Depends(get_db)):
    """进度事件追加：任务执行体（代笔/审计）跑在后台协程里，每走一步就落一条。"""
    t = _task_row(db, task_id)
    if not t or t.book_id != book_id:
        raise HTTPException(404, "task not found")
    _append_task_event(db, task_id, body)
    return {"ok": True, "seq": _task_row(db, task_id).seq}


@app.get("/api/books/{book_id}/tasks/{task_id}/stream")
async def stream_task(book_id: str, task_id: str, since: int = 0,
                      db: Session = Depends(get_db)):
    """SSE 进度流：从 since 之后逐条补发事件；任务结束后停。

    刷新重挂时带上已看到的最大 seq（since），服务端只补增量，不重放。
    """
    async def gen():
        db2 = SessionLocal()
        last = int(since or 0)
        try:
            while True:
                # v0.13.14 修复：必须先 expire_all 再 get。SQLAlchemy Session 的
                # identity map 会在第二次 get 时直接返回第一次缓存的同一个对象——
                # 后台 runner 追加进 DB 的新事件在这里永远是陈旧快照（本地实证：
                # 连续两次 get 拿到的是同一个 Python 对象，events_json 不更新）。
                # 这就是「发送后气泡空白、刷新后才看到进度」的根因：流实际活着，
                # 但每轮读到的都是建流那一刻的旧数据，一直空转到任务终态。
                db2.expire_all()
                t = db2.get(Task, task_id)
                if t is None:
                    yield f"data: {json.dumps({'kind': 'error', 'text': '任务不存在'})}\n\n"
                    break
                events = jl(t.events_json, []) or []
                # 按 seq 增量下发（events 顺序即追加顺序）
                for i, ev in enumerate(events[last:], start=last + 1):
                    yield f"data: {json.dumps({**ev, 'seq': i}, ensure_ascii=False)}\n\n"
                last = len(events)
                if t.status in ("done", "error", "abandoned"):
                    done_ev = {"kind": "done", "status": t.status,
                               "result": jl(t.result_json, {}), "error": t.error or "",
                               "progress": t.progress or ""}
                    yield f"data: {json.dumps(done_ev, ensure_ascii=False)}\n\n"
                    break
                await asyncio.sleep(0.8)
        finally:
            db2.close()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ----------------------------------------------------------------- 全书审计（后台任务 + 逐章流式进度）
# v0.9.1：原来「审计全书」只静默跑最近 20 章，用户以为"全书"结果其实漏了大半。
# 现在：跑全部章节（安全上限 _AUDIT_BOOK_CAP），转成后台 Task 逐章审计、逐章落库，
# 进度事件写进 Task.events_json 供 SSE 增量回放；前端可挂 /tasks/{id}/stream 看实时进度，
# 刷新后从 /books/{id}/tasks 恢复状态，不用再猜"进行到哪 / 是不是断了"。
_AUDIT_BOOK_CAP = 60  # 单本书一次全书审计的章节安全上限（防止极端长书跑失控）


@app.post("/api/books/{book_id}/audit-book")
async def run_audit_book(book_id: str, db: Session = Depends(get_db)):
    """全书审计：建一个后台任务，逐章跑七维审计并落库，返回任务 id。"""
    b = db.get(Book, book_id)
    if not b:
        raise HTTPException(404, "book not found")
    t = _new_task(db, book_id, "audit_book", target=0, progress="准备全书审计…")
    # db.close() 之后 t 已脱离 Session，再读 t.progress 会抛 DetachedInstanceError
    # （SQLAlchemy 2.0 抛的是 sqlalchemy.exc.DetachedInstanceError，不是 AttributeError，
    #  traceback 看着像"属性没了"，很容易误判成字段不存在）。先把要用的值取出来。
    task_id, initial_progress = t.id, t.progress
    book_id_saved = book_id          # 不读 b.id：db.close() 后 b 已脱离 Session
    db.close()

    async def runner():
        db2 = SessionLocal()
        try:
            await _run_audit_book_flow(db2, book_id_saved, task_id)
        except Exception as e:  # 兜底：任务一定收尾，绝不留在 running
            _task_event(db2, _task_row(db2, task_id),
                        {"kind": "error", "text": f"全书审计异常：{type(e).__name__}: {e}"},
                        status="error", error=f"{type(e).__name__}: {e}")
        finally:
            db2.close()

    _spawn(runner(), task_id=task_id, label="audit_book")
    return {"task_id": task_id, "status": "running", "progress": initial_progress}


async def _run_audit_book_flow(db, book_id: str, task_id: str) -> None:
    """逐章审计全书，每章完成即落 AuditReport + 追加任务进度事件。"""
    b = db.get(Book, book_id)
    chs = db.scalars(select(Chapter).where(Chapter.book_id == book_id)
                     .order_by(Chapter.idx)).all()
    n = len(chs)
    cap_note = ""
    if n > _AUDIT_BOOK_CAP:
        chs = chs[:_AUDIT_BOOK_CAP]
        cap_note = f"（章节过多，本次仅审计前 {_AUDIT_BOOK_CAP} 章）"
    if not chs:
        _task_event(db, _task_row(db, task_id),
                    {"kind": "error", "text": "本书暂无章节可审计"},
                    status="error", error="no chapters")
        return
    _task_event(db, _task_row(db, task_id),
                {"kind": "stage", "text": f"全书共 {n} 章，开始逐章审计{cap_note}"},
                progress=f"0/{n} 章 · 准备中")
    scores: list[int] = []
    chapter_reps: list[dict] = []
    rules_streak = 0          # 连续规则兜底计数（v0.13.7 熔断用）
    rules_total = 0           # 本轮规则兜底总章数
    for i, c in enumerate(chs, 1):
        cur = _task_row(db, task_id)
        if cur is None:  # 任务被删了，停止
            return
        _task_event(db, cur, {"kind": "stage", "text": f"正在审计第 {c.idx} 章（{i}/{n}）…"},
                    progress=f"{i}/{n} 章 · 审计中")
        try:
            rep = await audit_mod.audit_chapter(db, b, c.idx)
            if not rep.get("error"):
                is_rules = rep.get("mode") == "rules"
                if is_rules:
                    rules_streak += 1
                    rules_total += 1
                else:
                    rules_streak = 0
                audit_mod.save_report(db, book_id, rep, ch_idx=c.idx, scope="book")
                sc = int(rep.get("score", 0))
                scores.append(sc)
                chapter_reps.append({"ch_idx": c.idx, "score": sc,
                                     "mode": rep.get("mode", ""),
                                     "summary": (rep.get("summary") or "")[:120]})
                tag = "（规则兜底·模型未参与）" if is_rules else ""
                _task_event(db, cur,
                            {"kind": "action", "type": "audit",
                             "text": f"第 {c.idx} 章审计 {sc} 分{tag}"},
                            progress=f"{i}/{n} 章 · 完成")
                # v0.13.7 熔断：连续 5 章都是规则兜底，说明模型通道已断，
                # 继续跑只会产出一串无意义的兜底分 —— 立刻中止并明示用户。
                if rules_streak >= 5:
                    _task_event(db, cur,
                                {"kind": "error",
                                 "text": "连续 5 章审计模型调用失败，已中止全书审计。"
                                         "请检查模型配置/网络后重试；已有前 "
                                         f"{len(chapter_reps)} 章为规则兜底分（封顶 79），"
                                         "不代表真实质量。"},
                                status="error", error="llm unavailable (5 consecutive rule-fallbacks)")
                    return
        except Exception as e:
            _task_event(db, cur,
                        {"kind": "warn", "text": f"第 {c.idx} 章审计失败：{type(e).__name__}"},
                        progress=f"{i}/{n} 章 · 部分失败")
    avg = int(round(sum(scores) / len(scores))) if scores else 0
    done = _task_row(db, task_id)
    if done:
        note = f"，其中 {rules_total} 章为规则兜底分（封顶 79，模型未参与）" if rules_total else ""
        _task_event(db, done,
                    {"kind": "done", "text": f"全书审计完成：{len(chapter_reps)}/{n} 章，平均 {avg} 分{note}"},
                    status="done", progress=f"完成 {len(chapter_reps)}/{n} 章 · 平均 {avg} 分",
                    result={"score": avg, "total": n, "audited": len(chapter_reps),
                            "rules_fallback": rules_total,
                            "cap_note": cap_note, "chapters": chapter_reps})


# 免鉴权路径（★ 每加一条都要想清楚：这是不是"靠别的机制认证"）
#
# v0.20.3 A3：支付回调必须在白名单里。
#   为什么：支付平台（微信/支付宝）POST 回调时**不会带我们的 Bearer token**。
#   它的身份认证靠**签名验签**（见 payments.handle_callback 的防线①）——
#   如果这里不放行，回调全部 401，用户付了钱永远不到账。
#   ⚠️ 但这**不是安全漏洞**：验签失败会被拒绝（有测试覆盖）。
_AUTH_WHITELIST = {"/api/health"}


def _with_fp(resp):
    """★ v1.0 E3：给响应打上构建指纹头（L2 埋点 ②）。

    ★ 为什么封装成函数：鉴权中间件里有**多条提前 return** 分支
      （白名单放行、401 拒绝等），每条都是独立出口。
      实测踩过：只给主路径加了头，白名单分支漏了 →
      `/api/health` 没有 X-Powered-By，而它恰恰是最常被外部扫描的端点。

    ★ 绝不抛异常（埋点不能影响业务响应）。
    """
    try:
        from apps.api import build_info as _bi
        resp.headers["X-Powered-By"] = _bi.banner_line()
    except Exception:                                   # noqa: BLE001
        pass
    return resp



_AUTH_WHITELIST_PREFIX = (
    "/api/auth/",           # 含 send-code / login-by-code / reset-password
    "/api/pay/notify/",     # ★ 支付回调（靠验签认证，不靠 token）
    "/api/credits/plans",   # 套餐价格公开（未登录也要能看定价）
    "/api/pay/providers",   # 支付渠道列表公开（收银台要用）
    "/api/pay/sandbox/",    # 沙箱收银台（测试用，生产应关闭）
    # ★ v0.24 F 组：静态字典与透明度声明（无用户数据，公开有益）
    "/api/me/feedback/meta",       # 反馈表单选项（前端渲染用）
    "/api/telemetry/disclosure",   # ★ 数据收集透明说明（用户有权先看再决定）
    # ★ v0.25 J0：队列健康（前端据此决定"排队"还是"直跑"，未登录也要能查）
    "/api/queue/health",
    # ★★ v0.25.5：开源实例心跳（反向连接）。
    #   部署方不可能持有 SaaS 令牌，认证在此场景无法实现；
    #   保护手段是「签名（可选强制）+ 限流 + payload 清洗 + 体积上限」。
    "/v1/heartbeat",
    # ★★ v0.25.6 J8：排队位置与我的任务。
    #   为什么匿名可访问：
    #     ① task_id 本身是随机不可猜凭据（知道 ID 即发起者）
    #     ② 强制登录会让"排队进度条"在令牌过期时直接失效（体验差）
    #     ③ 只返回位置/时间估算，无任何敏感信息
    #   ★ 注意：/api/queue/mine 内部自己解析令牌 ——
    #     匿名访问返回空列表（而非 401），前端不必处理异常。
    "/api/queue/position",
    "/api/queue/mine",
    # ★★ v0.26.1：广告与站点信息必须**匿名可访问**。
    #   为什么：
    #     ① 介绍页 / 定价页是 SEO 入口，爬虫和未登录用户都要能看
    #     ② 广告位渲染在公开页面上，要求登录会让广告永远加载不出来
    #   安全：返回体只含**已经过清洗**的广告 HTML 与公开站点字段，
    #        绝不回传密钥/内部配置（见 routers/ads_models.py 的 public_* 端点）。
    "/api/ads",
    "/api/public/site",
    # ★★ v1.0 E3 开源准备：许可与构建信息必须**匿名可读**。
    #   为什么：
    #     ① 许可证是"需要知道才能遵守"的信息 —— 藏在登录墙后等于没告知
    #     ② 构建信息用于排障，用户还没登录就可能需要（如安装向导页）
    #   安全：只返回版本号/许可证类型/限额说明，**不含**密钥或实例 ID。
    "/api/license",
    "/api/build",
)


@app.middleware("http")
async def auth_middleware(request, call_next):
    p = request.url.path

    # ================================================================ ★ v0.20.6 A6-1 限流
    # 为什么放在鉴权之前：避免攻击者在"未登录"状态下无限刷接口刷爆 CPU
    # （比如暴力破解密码，每次都要跑 PBKDF2 12 万轮，非常贵）。
    if p.startswith("/api"):
        try:
            from apps.api import ratelimit as _rl
            _ip = _rl.client_ip(request.headers) or (
                request.client.host if request.client else "")
            _uid = ""
            _ah = request.headers.get("Authorization", "")
            if _ah.lower().startswith("bearer "):
                _tk = _ah[len("Bearer "):]
                if _tk.count(".") == 1:
                    try:
                        import base64 as _b64, json as _json
                        _body = _tk.rsplit(".", 1)[0]
                        _pl = _json.loads(_b64.urlsafe_b64decode(_body))
                        _uid = str(_pl.get("u") or "")
                    except Exception:
                        _uid = ""
            _deny = _rl.check_request(p, request.method, _ip, _uid)
            if _deny:
                return JSONResponse(
                    {"detail": _deny["message"], "retry_after": _deny["retry_after"]},
                    status_code=429, headers=_deny.get("headers") or {})
        except Exception as _e:                     # noqa: BLE001
            # ★ 限流器故障必须放行（可用性优先），但要有 error 级日志
            import logging as _lg
            _lg.getLogger("novelforge").error("★ 限流中间件异常（已放行）：%s: %s",
                                              type(_e).__name__, _e)

    if p.startswith("/api"):
        if p in _AUTH_WHITELIST or any(p.startswith(x) for x in _AUTH_WHITELIST_PREFIX):
            # ★★ v1.0 E3：白名单提前 return 也要带构建指纹头。
            #   ★ 这条曾被漏掉：`/api/health` 走的是这里，导致它没有
            #     X-Powered-By —— 而健康检查正是最常被外部扫描的端点，
            #     恰恰最需要能被识别来源。用统一助手杜绝再次遗漏。
            return _with_fp(await call_next(request))
        ah = request.headers.get("Authorization", "")
        token = ah[len("Bearer "):] if ah.lower().startswith("bearer ") else ""
        if not token and p.startswith("/api/files/"):
            # <img>/<audio>/<video> 标签发起的请求无法自定义请求头，
            # 媒体文件走 query 参数带令牌（仅此路径豁免到 query）。
            token = request.query_params.get("token", "")
        uid = auth.parse_token(token) if token else None
        if not uid:
            return JSONResponse({"detail": "未登录或登录已过期"}, status_code=401)
        # 这里是全站唯一的鉴权咽喉，必须校验 is_active。
        # 之前只有 auth.get_current_user 会查 is_active，而它只被 4 条路由注入使用，
        # 其余几十条书籍路由走中间件、完全不经过它 —— 管理员把账号禁用之后，
        # 该用户手里的旧令牌（默认 14 天有效）照样能读写删自己的书、还能调模型烧钱。
        _db = SessionLocal()
        try:
            _u = _db.get(User, uid)
            if not _u or not _u.is_active:
                return JSONResponse({"detail": "账号已停用或不存在"}, status_code=401)
            # ★ v0.20.4 A4：注销后（软删）立即拒绝
            if getattr(_u, "deleted_at", None) is not None:
                return JSONResponse({"detail": "账号已注销"}, status_code=401)
            # ★ v0.20.4 A4：改密/重置密码后，**旧令牌立即失效**。
            #   为什么必须做：用户改密的动机往往是"怀疑账号被盗"，
            #   若旧令牌还能用，改密等于没改 —— 攻击者照样能操作。
            #   实现：令牌里带签发时间 iat，与 token_valid_after 比较。
            _tva = getattr(_u, "token_valid_after", None)
            if _tva is not None:
                _iat = auth.token_issued_at(token)
                # ★ 兼容性处理（重要）：v0.20.4 之前的令牌没有 iat。
                #   若把 None 一律判失效，会导致**升级瞬间所有在线用户被踢**。
                #   折中：无 iat 的旧令牌放行（它们本来就有 14 天上限），
                #   等自然过期即绝迹。新令牌一律带 iat，安全机制对新会话立即生效。
                # ★ _tva 从 DB 读出来是 **naive**，而 timestamp() 假定本地时区。
                #   用 replace(tzinfo=utc) 明确按 UTC 解释（与 models.now() 一致），
                #   否则会有 8 小时时差（东八区）导致误判。
                import datetime as _dtm
                _tva_ts = _tva.replace(tzinfo=_dtm.timezone.utc).timestamp() \
                    if _tva.tzinfo is None else _tva.timestamp()
                # ★ 容差 1 秒：iat 是**整数秒**（int(time.time())），
                #   而 tva 带微秒。不加容差，"同一秒内签发又改密"会被误判。
                if _iat is not None and _iat + 1 < _tva_ts:
                    return JSONResponse(
                        {"detail": "密码已修改，请重新登录"}, status_code=401)
            # 只把原始字段缓下来，不要把 ORM 对象塞进 request.state：
            # Session 已 close，之后访问未加载属性会抛 DetachedInstanceError。
            _role = _u.role
            _totp = int(getattr(_u, "totp_enabled", 0) or 0)   # ★ E4.14 P0③：后台强制 2FA
        finally:
            _db.close()
        if p.startswith("/api/admin/"):
            # ★★ v1.0-E4.14 后台安全加固（P0④ IP 白名单 + P0③ 强制 2FA）
            #   两道安全门禁都放在 RBAC 粗粒度检查**之前**（网络层先于身份层）。
            #   ★ 取 IP 一律走 ratelimit.client_ip（CF-Connecting-IP，不可伪造）——
            #     绝不用 XFF 首段（客户端可伪造，经典绕过）。
            try:
                from apps.api import ratelimit as _rlw
                _c_ip = _rlw.client_ip(request.headers) or (
                    request.client.host if request.client else "")
            except Exception:                                   # noqa: BLE001
                _c_ip = request.client.host if request.client else ""
            # 读取两个安全配置（单 session 合并；读不到=fail-open 不限制，避免锁死后台）
            _wl_raw = ""
            _force_2fa = False
            try:
                from apps.api.config_center import get_config as _cc_get
                from apps.api.config_center import get_bool as _cc_bool
                _dbw = SessionLocal()
                try:
                    _wl_raw = str(_cc_get(_dbw, "security.admin_ip_whitelist", "") or "")
                    _force_2fa = bool(_cc_bool(_dbw, "security.admin_force_2fa", False))
                finally:
                    _dbw.close()
            except Exception as _wle:                              # noqa: BLE001
                import logging as _lgw
                _lgw.getLogger("novelforge").warning(
                    "后台安全配置读取失败（已 fail-open 放行）：%s: %s",
                    type(_wle).__name__, _wle)

            # ── P0④ IP 白名单（网络层门禁；空=不限制）──
            _wl = [x.strip() for x in _wl_raw.replace(";", ",").split(",")
                   if x.strip()]
            if _wl:
                import ipaddress as _ipm
                _in_wl = False
                try:
                    _c_ipa = _ipm.ip_address(_c_ip)
                    for _w in _wl:
                        if "/" in _w:
                            if _c_ipa in _ipm.ip_network(_w, strict=False):
                                _in_wl = True
                                break
                        elif _c_ip == _w:
                            _in_wl = True
                            break
                except Exception:                                   # noqa: BLE001
                    _in_wl = False
                if not _in_wl:
                    return JSONResponse(
                        {"detail": "当前 IP 不在后台访问白名单内"}, status_code=403)

            # ── RBAC 粗粒度兜底（原有逻辑保留，纵深防御）──
            #   细粒度权限（这个端点具体要哪个权限点）由端点上
            #   的 Depends(require_perm(...)) 负责 —— 规则集中在 rbac.py。
            _ok = (_role == "admin")
            try:
                from apps.api import rbac as _rbac
                _db2 = SessionLocal()
                try:
                    _ok = _rbac.has_permission(_db2, _role, "admin.access")
                finally:
                    _db2.close()
            except Exception:
                pass    # RBAC 故障 → 用旧逻辑兜底（不锁死后台）
            if not _ok:
                return JSONResponse({"detail": "需要管理员权限"}, status_code=403)

            # ── P0③ 强制 2FA（★ owner 作 break-glass 逃生角色，豁免）──
            #   开启后：非 owner 管理员若未启用 TOTP 则拒绝进后台。
            #   ★ 为什么豁免 owner：若把 owner 也锁死，一旦全体管理员忘记 2FA
            #     就无人能进后台解锁 → 全站失控。owner 是最后的安全责任人。
            if _force_2fa and _role != "owner" and not _totp:
                return JSONResponse({
                    "detail": ("该账号未启用两步验证，无法访问后台"
                               "（管理员已开启后台强制 2FA）。请先在「安全中心」启用 TOTP。"),
                    "code": "2FA_REQUIRED",
                }, status_code=403)
        m = __import__("re").match(r"/api/books/([^/]+)(/.*)?$", p)
        if m:
            bid = m.group(1)
            if m.group(2) is not None or p == f"/api/books/{bid}":
                db = SessionLocal()
                try:
                    b = db.get(Book, bid)
                finally:
                    db.close()
                # 注意条件：无主书（owner_id 为空）也不能放行给普通用户，
                # 否则任何登录者都能读写历史遗留书籍。只有管理员可越权。
                # ★ 判定方法优先级：
                #   POST/PUT/DELETE → 按操作映射权限点（write/delete）
                #   GET             → book.read
                # 这样"只读角色"改不动书，"财务角色"看不到内容（两处都要拦）。
                if b and (not b.owner_id or b.owner_id != uid):
                    _need = "book.read"
                    _m = request.method.upper()
                    if _m in ("POST", "PUT", "PATCH"):
                        _need = "book.write"
                    elif _m == "DELETE":
                        _need = "book.delete"
                    _allowed = (_role == "admin")
                    try:
                        from apps.api import rbac as _rbac2
                        _db3 = SessionLocal()
                        try:
                            _allowed = _rbac2.can_access_book(_db3, _u, b, _need) \
                                or _rbac2.has_permission(_db3, _role, "book.*") \
                                or _rbac2.has_permission(_db3, _role, "workspace.manage")
                        finally:
                            _db3.close()
                    except Exception:
                        pass
                    if not _allowed:
                        return JSONResponse({"detail": "无权访问该书"}, status_code=403)
    resp = await call_next(request)
    # ★★ v1.0 E3 开源准备：L2 埋点 ② —— 响应头构建指纹
    #
    #   为什么用 X-Powered-By：这是**行业惯例**的标识头（Express/PHP 都发），
    #   不会引起反感；且任何 curl / 浏览器 DevTools 都能看到 →
    #   想删就得改中间件，比删一个常量麻烦（提高删除成本，见评估_06 §3.2）。
    #
    #   ★ 安全性：只暴露「项目名/版本/构建号」，**不含**任何环境信息、
    #     依赖版本、服务器软件版本 —— 不会给攻击者提供额外的攻击面信息。
    #     （对比：暴露 "Python/3.11.2" 才有信息泄露风险，我们只发自己的版本号）
    #
    #   ★★ 注意：中间件里有**多条提前 return** 的分支（白名单、401 等），
    #      它们是独立出口，不走这里 —— 所以那些分支各自调 `_with_fp()`。
    #      （实测踩过：漏了白名单分支 → /api/health 没有指纹头）
    _with_fp(resp)

    # v0.14.6：静态资源缓存策略（bug 实录：草稿卡「采纳入库/丢弃点击无反应」，
    # 实测代码与线上 md5 一致、事件绑定完好 —— 根因是浏览器持有旧版 app.js：
    # StaticFiles 默认不发 Cache-Control，浏览器按启发式缓存 app.js 数天，
    # 而前端迭代频繁，旧脚本配上新 DOM 就是各种「点了没反应」。
    # 策略：html 永远协商（no-cache 每次回源验证）；带指纹概念的 js/css 用
    # etag 协商缓存（no-cache 允许 304，部署新版后一次刷新即生效）。
    if not p.startswith("/api") and request.method in ("GET", "HEAD"):
        ctype = resp.headers.get("content-type", "")
        # ★ v0.27.0：新版已升主域名 —— 带内容 hash 的资源（`xxx-AbC12345.js`）
        #   在 `/`、`/admin/`、`/legacy/` 下都走长缓存。
        #   为什么可以更长：文件名含构建 hash，内容一变文件名就变 ——
        #   不存在"旧文件配新 DOM"的问题（那正是下面 no-cache 要防的）。
        #   ★ 不能用"路径含 /admin/"一刀切：index.html 必须协商。
        if re.search(r"-[A-Za-z0-9_]{8,}\.(js|css|woff2?|png|svg|jpg)$",
                     p.split("?")[0]):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif "text/html" in ctype:
            resp.headers["Cache-Control"] = "no-cache"
        elif any(k in ctype for k in ("javascript", "css", "image/", "font/")):
            resp.headers["Cache-Control"] = "no-cache"
    return resp


WEB_DIR = Path(__file__).resolve().parents[1] / "web"


# ----------------------------------------------------------------- schemas


class BookIn(BaseModel):
    title: str
    author: str = ""
    genre: str = ""
    platform: str = "generic"
    synopsis: str = ""
    chapter_min: int | None = None   # None/0 = 未设定，由立项确认后回写
    chapter_max: int | None = None
    # v0.15 题材解耦（G9）：theme=主题材（单选，定度量衡/实力体系）；
    # tags=题材软标签（多选，只影响提示词措辞）
    theme: str = ""
    tags: list[str] = []








class GenerateIn(BaseModel):
    ch_idx: int | None = None
    instruction: str = ""
    # ★ v0.25.7 J11：任务优先级（★ 会被限幅到 ±1000，防插队）
    priority: int = 0
    # ★ v0.25.7 J11：用户是否接受"排队等待"（默认接受）
    #   为什么给开关：有些调用方（如脚本）宁可立刻失败也不要挂起，
    #   尊重调用方的语义比"一律排队"更合理。
    allow_queue: bool = True


class ChatIn(BaseModel):
    message: str
    history: list[dict] = []
    conversation_id: str = ""   # v0.13.12：带会话 id，服务端接管消息落库


class AcceptIn(BaseModel):
    model_config = {"protected_namespaces": ()}

    ch_idx: int
    title: str
    content: str
    gate: dict | None = None
    register: bool = True
    # v0.14.7：来源草稿任务 ID。采纳成功后把该任务 result_json 里的正文摘掉
    # （打 accepted 标记），待审区扫描（pending-drafts 按 content 存在性列稿）
    # 与刷新恢复（recoverDraftCard）自然失效 —— 否则采纳后条目永远留在
    # 待审区，还能重复入库；丢弃接口（DELETE draft）早就是这么做的。
    task_id: str | None = None


class ChapterPatch(BaseModel):
    title: str | None = None
    content: str | None = None
    summary: str | None = None


class ImportIn(BaseModel):
    src_dir: str
    workspace_id: str = "default"




class StylePatch(BaseModel):
    pov: str | None = None
    tone: str | None = None
    rules: str | None = None
    banned_words: list[str] | None = None
    max_para_chars: int | None = None
    sample: str | None = None


# ----------------------------------------------------------------- 书


@app.on_event("startup")
def _on_startup() -> None:
    """启动兜底：把上次进程遗留的 running 任务标成 abandoned。

    必须在 startup 钩子里调，不能放在模块顶层——模块导入期 _recover_tasks
    还没定义，uvicorn 直接 NameError 起不来。
    v0.13.15：同时启动短剧媒体队列 worker + 恢复遗留 drama_tasks。
    """
    _recover_tasks()
    try:
        from apps.api import drama_queue
        drama_queue.ensure_worker()
    except Exception:
        pass  # 队列起不来不阻断主服务；批量生成端点会报错提示

    # ★★ v1.0 E3 开源准备：构建指纹（L2 埋点 ④ 启动日志 + ⑤ 数据库 _meta）
    #   为什么放在最先：它是排障的"第一现场"信息 ——
    #   用户贴日志时我们立刻就拿到构建来源，省去来回确认。
    try:
        from apps.api import build_info as _bi
        _bi.log_build_banner()
        # ★ 写入数据库（幂等，失败静默）：代码可被删，数据库里的历史记录不会
        _bi.write_build_meta()
    except Exception as _e:                             # noqa: BLE001
        logging.getLogger("novelforge").debug(
            "构建信息初始化失败（不影响启动）：%s: %s", type(_e).__name__, _e)


    # ★ v0.20.6 A6 安全加固：启动自检
    # 为什么要在启动时自检：这些问题如果留到运行时才暴露，
    # 用户看到的是 500，排查成本高得多。
    _log = __import__("logging").getLogger("novelforge")
    try:
        from apps.api import keyvault
        _sc = keyvault.self_check()
        if _sc["ok"]:
            _log.info("密钥保险箱自检通过（v%d，历史密钥 %d 个）",
                      _sc["version"], _sc["old_keys"])
        else:
            # ★ 加密坏了是**严重**问题（存进去读不出来），必须显眼
            _log.error("★ 密钥保险箱自检失败：%s —— API Key 可能无法解密，请检查 SECRET_KEY",
                       _sc.get("error"))
        # 把库里遗留的明文 Key 就地加密（幂等）
        _db = SessionLocal()
        try:
            _n = keyvault.encrypt_all_plaintext_keys(_db)
            if _n:
                _log.info("已加密 %d 条遗留明文 API Key", _n)
        finally:
            _db.close()
    except Exception as _e:                     # noqa: BLE001
        _log.warning("密钥自检异常（不影响启动）：%s", _e)

    try:
        from apps.api import moderation
        _ms = moderation.stats()
        _log.info("内容审核就绪：%s（规则 block=%d / review=%d / warn=%d）",
                  _ms["provider"], _ms["local_rules"]["block"],
                  _ms["local_rules"]["review"], _ms["local_rules"]["warn"])
    except Exception as _e:                     # noqa: BLE001
        _log.warning("内容审核初始化异常：%s", _e)


    # ★ v0.20.7 A7：初始化 RBAC 角色权限（幂等，只在首次写入）
    try:
        from apps.api import rbac as _rbac
        _db = SessionLocal()
        try:
            _n = _rbac.seed_role_permissions(_db)
            _m = _rbac.matrix()
            _log.info("RBAC 就绪：%d 个角色 · %d 个权限点%s",
                      len(_m["roles"]), len(_m["permissions"]),
                      f"（本次初始化 {_n} 条映射）" if _n else "")
        finally:
            _db.close()
    except Exception as _e:                     # noqa: BLE001
        _log.warning("RBAC 初始化异常（降级用内置定义）：%s", _e)

    # ★ v0.23.3 D3：挂慢查询采集（幂等）。只采集不干预 —— 零副作用。
    try:
        from apps.api import perf as _perf
        from apps.api.db import engine as _engine
        _perf.install(_engine)
        _log.info("性能采集已启用：慢查询阈值 %.0fms", _perf.SLOW_MS)
    except Exception as _e:                     # noqa: BLE001
        _log.warning("性能采集挂载异常（不影响服务）：%s", _e)

    # ★ v0.23.1 D1：模型角色路由体检（提示运营补配缺失档位）
    try:
        from apps.api import costing as _cost
        _c = _cost.coverage(_db2 := SessionLocal())
        try:
            if _c["total"] == 0:
                _log.warning("尚未配置任何模型：生成功能不可用")
            elif _c["missing"]:
                _log.info("模型路由：已配 %d 个模型，缺少角色档 %s（将自动降级）",
                          _c["total"], "、".join(_c["missing"]))
            else:
                _log.info("模型路由就绪：5 个角色档全部覆盖")
        finally:
            _db2.close()
    except Exception as _e:                     # noqa: BLE001
        _log.warning("模型路由体检异常：%s", _e)

    # ★ v0.24.2 F2：遥测（默认关闭，仅提示开关状态 —— 绝不上报）
    try:
        from apps.api import telemetry as _tm
        _db3 = SessionLocal()
        try:
            _on = _tm.is_enabled(_db3)
            if _on:
                _log.info("匿名使用统计：**已开启**（%s=off 可关闭；"
                          "GET /api/telemetry/disclosure 查看收集内容）",
                          _tm.ENV_SWITCH)
            else:
                _log.info("匿名使用统计：已关闭（默认）。"
                          "如需开启：%s=on 或后台设置页", _tm.ENV_SWITCH)
        finally:
            _db3.close()

        # ★★ v1.0-E4.5：启动**心跳调度器**（此前完全缺失）
        #
        #   ★ 为什么必须补：`maybe_heartbeat()` 的注释写着
        #     「由调度器/启动钩子调用」，但**调度器从来没写过** ——
        #     唯一调用点是管理端手动端点。
        #     后果：开源实例开了遥测也不会自动上报，
        #     SaaS 侧 telemetry_instances 表 **0 条记录**。
        #
        #   ★ 无论开关是否开启都启动线程 —— 因为运营可能在运行期
        #     通过后台打开遥测，此时不该要求重启服务。
        #     线程内部会检查 is_enabled()，关闭时不做任何上报。
        try:
            _sr = _tm.start_scheduler()
            _log.info("遥测调度器已启动（首次延迟 %ss，轮询 %ss）",
                      _sr.get("first_delay"), _sr.get("poll"))
        except Exception as _se:                # noqa: BLE001
            _log.warning("遥测调度器启动失败（不影响主服务）：%s", _se)
    except Exception as _e:                     # noqa: BLE001
        _log.warning("遥测初始化异常：%s", _e)

    # ★★ v1.0-E4.10：限流关闭时**必须显式告警**
    #
    #   ★ 为什么：`NF_RATELIMIT_OFF=1` 会关掉所有限流 ——
    #     如果误配到生产环境，等于**没有任何防刷保护**。
    #     这种"安静的危险配置"必须有启动日志。
    try:
        from apps.api import ratelimit as _rl
        if getattr(_rl, "_RATELIMIT_OFF", False):
            _log.warning(
                "★★ 限流已**关闭**（%s 生效）—— 仅限测试/冒烟环境！"
                "生产环境请确保该变量为 unset 或 0。", _rl.ENV_OFF)
    except Exception as _re_:                   # noqa: BLE001
        _log.debug("限流状态检查失败：%s", _re_)

    # ★ v0.25 Xf：邮件服务自检（只查配置，不发信）
    try:
        from apps.api import mail as _mail
        _db4 = SessionLocal()
        try:
            _ms = _mail.self_check(_db4)
            if not _ms["ok"]:
                _log.info("邮件通知不可用：%s（站内信不受影响）",
                          "；".join(_ms["issues"]))
        finally:
            _db4.close()
    except Exception as _e:                     # noqa: BLE001
        _log.warning("邮件自检异常：%s", _e)

    # ★ v0.25 J0-1/J0-2：队列自检 + 启动重建
    try:
        from apps.api import queue as _q
        _qs = _q.self_check()
        if _qs.get("available"):
            # ★ 启动重建：把 DB 里未完成的任务回灌 Redis。
            #   为什么必须做：Redis 只是调度器，DB 才是真相源。
            #   不回灌的话，Redis 重启后那些任务会永远"排队中"。
            _db5 = SessionLocal()
            try:
                _rb = _q.rebuild_from_db(_db5, limit=2000)
                if _rb.get("scanned"):
                    _log.info("队列启动重建：%s", _rb.get("reason"))
            finally:
                _db5.close()
    except Exception as _e:                     # noqa: BLE001
        _log.warning("队列初始化异常（降级为同步执行）：%s", _e)

    # ★ v0.25.2：限流自检（必须晚于队列 —— 限流的 Redis 后端
    #   复用队列连接的熔断状态，先建连才能正确判定是否降级）
    try:
        from apps.api import ratelimit as _rl
        _rl.self_check()
    except Exception as _e:                     # noqa: BLE001
        _log.warning("限流初始化异常：%s", _e)





# ----------------------------------------------------------------- 账号中心
# ----------------------------------------------------------------- 系统设置（DB 化）
# 原先平台配置只能靠改服务器 .env 再重启服务，管理员在界面上完全摸不到。
# 商业化要把平台参数交给运营，所以这里做一层 DB 覆盖：
# 数据库里有值就用数据库的，没有才回退到环境变量（环境变量仍是部署期默认值）。
# v0.18 T1：SETTING_DEFAULTS 已搬至 services.settings（routers 也用它）。
from apps.api.services.settings import SETTING_DEFAULTS  # noqa: E402


















@app.get("/api/books")
def list_books(db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    q = select(Book)
    if user.role != "admin":
        # 严格隔离：只属于自己的书。原先还带 OR owner_id=='' ，
        # 会让所有无主书（历史遗留）对每个登录用户都可见 —— 已去掉。
        q = q.where(Book.owner_id == user.id)
    rows = db.scalars(q.order_by(Book.created_at.desc())).all()
    # 原来是 N+1：每本书单独发 3 条查询（章节数 / 末章号 / 总字数），
    # 而且 chapters 那条还在用旧式 db.query()。改成一次聚合再内存 join。
    agg = {r[0]: r for r in db.execute(
        select(Chapter.book_id, func.count(Chapter.id), func.sum(Chapter.words),
               func.max(Chapter.idx)).group_by(Chapter.book_id)
    ).all()}
    # ★ v1.0-E4.11：每本书最近一次更新时间 = 最新章节的 updated_at（无章节回退建书时间）。
    #   dashboard 端点已有此口径（逐本查询），这里一次性聚合，顺手补齐书架排序依据。
    lastupd = {r[0]: r[1] for r in db.execute(
        select(Chapter.book_id, func.max(Chapter.updated_at)).group_by(Chapter.book_id)
    ).all()}
    out = []
    for b in rows:
        row = agg.get(b.id)
        _lu = lastupd.get(b.id) or b.created_at
        out.append({
            "id": b.id, "title": b.title, "author": b.author, "genre": b.genre,
            "platform": b.platform,
            "chapters": int(row[1]) if row else 0,
            "last_idx": int(row[3]) if row and row[3] is not None else 0,
            "words": int(row[2]) if row and row[2] is not None else 0,
            "status": b.status,
            "quality_score": b.quality_score, "owner_id": b.owner_id,
            # 序列化口径与 dashboard 一致（"YYYY-MM-DD HH:MM"）
            "updated_at": _lu.strftime("%Y-%m-%d %H:%M") if _lu else "",
        })
    return out


@app.post("/api/books")
def create_book(body: BookIn, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    # v0.15 G9：主题材归一为规范 key（如「都市」→ urban）；未知值留空走兜底。
    from apps.api import genres
    theme = genres.normalize_key(body.theme, db)
    # 标签逐项校验、归一、非法项丢弃（计划书 §六 风险 8）
    ok_tags = genres.normalize_keys(body.tags, db)

    b = Book(id=f"bk_{uuid.uuid4().hex[:12]}", title=body.title, author=body.author,
             genre=body.genre, platform=body.platform, synopsis=body.synopsis, owner_id=user.id,
             chapter_min=body.chapter_min or 0, chapter_max=body.chapter_max or 0,
             theme=theme, tags=",".join(ok_tags))
    db.add(b)
    db.add(StyleProfile(id=f"sty_{uuid.uuid4().hex[:12]}", book_id=b.id,
                        banned_words=json.dumps(gates.DEFAULT_BANNED, ensure_ascii=False)))
    db.commit()
    gitrepo.init_repo(b.id)
    return {"id": b.id}








# ----------------------------------------------------------------- 管理员后台
class AdminUserIn(BaseModel):
    username: str
    password: str = ""
    email: str = ""
    role: str = "user"
    is_active: int = 1










# ----------------------------------------------------------------- 题材包管理（v0.15 G8）
# 用户决策 D1：题材包放 DB、配置中心可改、需配套 UI。
# 本组接口让运营/作者自助维护题材，**不发版即生效**（改完清缓存）。
# 安全约束（计划书 §六 风险 9）：内置包 builtin=1 只允许停用、不允许删除。















# ---------------------------------------------------------------- 题材包导入/导出（v0.16 G12）
# 用途：题材包可备份、可分享（"我调好的都市包发你"）。JSON 互转，无外部依赖。





@app.patch("/api/books/{book_id}/theme")
def set_book_theme(book_id: str, body: dict, db: Session = Depends(get_db),
                   user: User = Depends(auth.get_current_user)):
    """设置书的主题材 + 软标签（v0.15 G9）。

    theme 决定度量衡与实力体系（必须唯一）；tags 只影响提示词措辞（可多选）。
    未知值不报错、留空/丢弃，避免老书自由文本 genre 引发 400（计划书 §六 风险 8）。
    """
    b = db.get(Book, book_id)
    if not b:
        raise HTTPException(404, "book not found")
    from apps.api import genres

    if "theme" in body:
        b.theme = genres.normalize_key(str(body.get("theme") or "").strip(), db)

    if "tags" in body:
        raw = body.get("tags") or []
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        b.tags = ",".join(genres.normalize_keys(raw, db))

    db.commit()
    genres.invalidate_cache()
    return {"ok": True, "theme": b.theme, "tags": (b.tags or "").split(",") if b.tags else []}


@app.post("/api/books/import")
def import_book(body: ImportIn, db: Session = Depends(get_db), user: User = Depends(auth.require_admin)):
    # 导入会凭空建书，必须限定管理员，否则普通用户可借此绕过归属规则
    try:
        res = importer.import_book(db, body.src_dir, body.workspace_id)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))
    # importer.import_book() 返回的键是 book_id，这里原来读 "id"，
    # 于是 bid 恒为 None，下面给 owner 的逻辑一次都没执行过 ——
    # 导入的书 owner_id 永远是空的，只能靠管理员身份访问，没法交接给真正的主人。
    res = res if isinstance(res, dict) else {}
    bid = res.get("book_id") or res.get("id")
    if bid:
        b = db.get(Book, bid)
        if b and not b.owner_id:
            b.owner_id = user.id
            db.commit()
    return res


@app.get("/api/books/{book_id}")
def get_book(book_id: str, db: Session = Depends(get_db)):
    b = db.get(Book, book_id)
    if not b:
        raise HTTPException(404, "book not found")
    sp = db.scalar(select(StyleProfile).where(StyleProfile.book_id == book_id))
    return {
        "id": b.id, "title": b.title, "author": b.author, "genre": b.genre,
        "platform": b.platform, "synopsis": b.synopsis, "target_words": b.target_words,
        "chapter_min": b.chapter_min, "chapter_max": b.chapter_max,
        "style": {
            "pov": sp.pov if sp else "第一人称", "tone": sp.tone if sp else "",
            "rules": sp.rules if sp else "", "sample": sp.sample if sp else "",
            "max_para_chars": sp.max_para_chars if sp else 120,
            "banned_words": jl(sp.banned_words, gates.DEFAULT_BANNED) if sp else gates.DEFAULT_BANNED,
        },
    }


@app.patch("/api/books/{book_id}/style")
def patch_style(book_id: str, patch: StylePatch, db: Session = Depends(get_db)):
    sp = db.scalar(select(StyleProfile).where(StyleProfile.book_id == book_id))
    if not sp:
        raise HTTPException(404, "style not found")
    for k, v in patch.model_dump(exclude_none=True).items():
        if k == "banned_words":
            setattr(sp, k, json.dumps(v, ensure_ascii=False))
        else:
            setattr(sp, k, v)
    db.commit()
    return {"ok": True}


# ----------------------------------------------------------------- 状态中枢


@app.get("/api/books/{book_id}/chapters")
def list_chapters(book_id: str, db: Session = Depends(get_db)):
    vols = db.scalars(select(Volume).where(Volume.book_id == book_id).order_by(Volume.idx)).all()
    rows = db.scalars(select(Chapter).where(Chapter.book_id == book_id).order_by(Chapter.idx)).all()

    def vol_of(idx: int) -> str:
        for v in vols:
            if v.start_ch <= idx <= (v.end_ch or 10 ** 9):
                return f"卷{v.idx}《{v.name}》"
        return ""

    return [{"idx": c.idx, "title": c.title, "words": c.words, "status": c.status,
             "gate_score": c.gate_score, "id": c.id,
             "volume": vol_of(c.idx)} for c in rows]


@app.get("/api/books/{book_id}/chapters/{idx}")
def get_chapter(book_id: str, idx: int, db: Session = Depends(get_db)):
    c = db.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.idx == idx))
    if not c:
        raise HTTPException(404, "chapter not found")
    return {"idx": c.idx, "title": c.title, "content": c.content, "summary": c.summary,
            "words": c.words, "status": c.status, "gate_score": c.gate_score,
            "gate_report": jl(c.gate_report, {})}


@app.patch("/api/books/{book_id}/chapters/{idx}")
def patch_chapter(book_id: str, idx: int, patch: ChapterPatch, db: Session = Depends(get_db)):
    c = db.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.idx == idx))
    # ★ v1.0-E4.17：PATCH upsert —— 章节尚不存在时**先建行再写**。
    #   「在编辑器中查看」会为待审草稿的目标章节跳到编辑器；新书首次起草时
    #   该章节还没建行（GET 仍 404），用户在编辑器预填草稿后点「保存」，
    #   若这里仍 404 就会**丢稿**。与 accept_draft 建行语义一致（new_id("ch")）。
    #   ★ 仅写路径 upsert；GET 的 404 契约保持不变（test_chapter_404_for_missing）。
    #   ★ 建前行确认 book 存在，避免凭空造孤儿章节（写路径比读路径更该防脏数据）。
    if c is None:
        if db.get(Book, book_id) is None:
            raise HTTPException(404, "book not found")
        c = Chapter(id=drama_mod.new_id("ch"), book_id=book_id, idx=idx, status="draft")
        db.add(c)
        db.flush()
    changed = False
    for k, v in patch.model_dump(exclude_none=True).items():
        setattr(c, k, v)
        changed = True
    if c.content:
        c.words = gates.count_words(gates.strip_title(c.content))
    c.updated_at = now()
    db.commit()
    if changed and c.content:
        gitrepo.write_file(book_id, f"正文/第{idx:03d}章.md",
                           f"# 第{idx:03d}章 {c.title}\n\n{c.content}\n",
                           commit_msg=f"编辑第{idx:03d}章")
    return {"ok": True, "words": c.words}


@app.get("/api/books/{book_id}/state")
def get_state(book_id: str, db: Session = Depends(get_db)):
    vols = db.scalars(select(Volume).where(Volume.book_id == book_id).order_by(Volume.idx)).all()
    chs = db.scalars(select(Chapter).where(Chapter.book_id == book_id).order_by(Chapter.idx)).all()
    fss = db.scalars(select(Foreshadow).where(Foreshadow.book_id == book_id).order_by(Foreshadow.id)).all()
    chars = db.scalars(select(Character).where(Character.book_id == book_id)).all()
    ws = db.scalars(select(WorldSetting).where(WorldSetting.book_id == book_id)).all()
    return {
        "volumes": [{"idx": v.idx, "name": v.name, "start_ch": v.start_ch, "end_ch": v.end_ch,
                     "outline": v.outline[:300]} for v in vols],
        "characters": [{"name": c.name, "role": c.role, "state": c.state_now,
                        "profile": c.profile[:200]} for c in chars],
        "foreshadows": [{"content": f.content, "planted_ch": f.planted_ch,
                         "status": f.status, "payoff_ch": f.payoff_ch} for f in fss],
        "world": [{"category": w.category, "key": w.key, "value": w.value} for w in ws],
        "progress": {
            "chapters": len(chs),
            "words": sum(c.words for c in chs),
            "last_idx": chs[-1].idx if chs else 0,
            "open_foreshadows": sum(1 for f in fss if f.status == "open"),
        },
    }


@app.post("/api/books/{book_id}/chapters/{idx}/gate")
def recheck_gate(book_id: str, idx: int, db: Session = Depends(get_db)):
    c = db.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.idx == idx))
    b = db.get(Book, book_id)
    if not c:
        raise HTTPException(404, "chapter not found")
    sp = db.scalar(select(StyleProfile).where(StyleProfile.book_id == book_id))
    prev = [x.content for x in db.scalars(
        select(Chapter).where(Chapter.book_id == book_id, Chapter.idx < idx).order_by(Chapter.idx)).all()][-5:]
    rep = gates.run_gates(c.content, b, sp, prev,
                          realm_whitelist=gates.realm_whitelist_for(db, book_id))
    c.gate_score = rep.score
    c.gate_report = jd(rep.to_dict())
    db.commit()
    return rep.to_dict()


# ----------------------------------------------------------------- 生成（SSE）


@app.post("/api/books/{book_id}/generate")
async def generate(book_id: str, body: GenerateIn, request: Request):
    db = SessionLocal()
    try:
        b = db.get(Book, book_id)
        if not b:
            raise HTTPException(404, "book not found")
        last = db.scalar(select(Chapter.idx).where(Chapter.book_id == book_id).order_by(Chapter.idx.desc()))
        ch_idx = body.ch_idx if body.ch_idx else (last or 0) + 1

        # ══════════════════════════════════════════════════════════
        # ★★ v0.25.7 J11：并发准入检查（全链路接入）
        #
        # ★ 为什么要放在"建流之前"：
        #   并发满了还建 SSE 流的话，用户会看到一个"卡住不动的流"，
        #   而实际上什么都没跑 —— 比直接返回"排队中"体验更差。
        #
        # ★ 为什么用 admit() 而不是 enqueue()+dequeue()：
        #   请求线程不会去跑 worker，enqueue 后没人认领 →
        #   任务永远挂在队列里（假排队）。admit 只做"占位"，
        #   占到了就由本请求直接执行。
        # ══════════════════════════════════════════════════════════
        from apps.api import queue as _q
        _uid = ""
        try:
            _ah = request.headers.get("Authorization", "")
            _tok = _ah[7:] if _ah.lower().startswith("bearer ") else ""
            _uid = auth.parse_token(_tok) if _tok else ""
        except Exception:                               # noqa: BLE001
            _uid = ""

        _adm = _q.admit(db, user_id=_uid or "", book_id=book_id)
        if not _adm.get("allowed"):
            # ★ 返回 202（已接受但未开始），而非 4xx ——
            #   这不是"错误"，是"资源暂时不可用"，语义更准确。
            db.close()
            return JSONResponse(status_code=202, content={
                "ok": True, "queued": True, "reason": _adm.get("reason"),
                "text": _adm.get("text"),
                "position": (_adm.get("current") or 0) + 1,
                "limits": _adm.get("limits"),
                "retry_after": 5,
                "hint": "客户端可隔几秒重试，或使用 /api/queue/position 查询进度",
            })
        _adm_token = _adm.get("token") or ""

        q: asyncio.Queue = asyncio.Queue()

        async def emit(ev: dict):
            await q.put(ev)

        async def runner():
            try:
                await graph.run_generate_flow(db, b, ch_idx, body.instruction, emit)
            except Exception as e:  # 保证流一定会结束
                await emit({"kind": "error", "text": f"{type(e).__name__}: {e}"})
            finally:
                # ★★ 必须释放准入占位（漏了 → 并发额度只增不减 → 全员排队）
                try:
                    _q.release_token(_adm_token, user_id=_uid or "",
                                     book_id=book_id)
                except Exception:                       # noqa: BLE001
                    pass
                # 嵌套 finally：db 必须在内层关掉。
                # 原来这里是先 await q.put(None) 再 db.close()，一旦此刻被取消，
                # CancelledError 会让 db.close() 被跳过，Session 直接泄漏。
                try:
                    await q.put(None)
                finally:
                    db.close()

        _spawn(runner(), label="drama_batch_stream")

        async def gen():
            while True:
                ev = await q.get()
                if ev is None:
                    break
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    except Exception:
        db.close()
        raise


@app.post("/api/books/{book_id}/accept")
async def accept(book_id: str, body: AcceptIn):
    db = SessionLocal()
    try:
        b = db.get(Book, book_id)
        if not b:
            raise HTTPException(404, "book not found")
        # 采纳前补一道真实闸门。
        # 背景：第15章曾以 56 分（阈值 90）、1830 字（下限 2000）被直接采纳入库，
        # 用户直到对比才发现质量断崖。根因是 accept_draft 只负责落库，
        # 从不判断 gate.score 与字数下限 —— 草稿卡上那个"质检 56"只是展示，
        # 点了「采纳并登记」照样进正文。
        # 这里不阻断（用户有权拍板），但必须把问题明确回传，让前端能弹警告。
        warnings: list[str] = []
        words = gates.count_words(gates.strip_title(body.content or ""))
        if b.chapter_min and words < b.chapter_min:
            warnings.append(
                f"字数 {words} 低于设定下限 {b.chapter_min} 字"
                f"（当前设定 {b.chapter_min}–{b.chapter_max}）")
        score = int((body.gate or {}).get("score") or 0)
        if score and b.polish_threshold and score < b.polish_threshold:
            warnings.append(
                f"质检 {score} 分低于阈值 {b.polish_threshold} 分")

        # ★ v0.20.6 A6-6：内容审核（合规要求 —— 生成式 AI 服务必须做）。
        #   为什么放在"采纳"这个关口：这是内容**即将进入正文**的唯一入口，
        #   在这里拦最有效（生成阶段可能被反复重写，拦截会打断创作流）。
        #   ★ 策略：block 直接拒绝；review/warn 只告警不阻断（用户有权拍板，
        #     但必须留痕 —— 审核记录写入 content_flags）。
        try:
            from apps.api import moderation as _mod
            _mr = _mod.moderate_and_record(
                db, body.content or "", stage="accept",
                user_id=(b.owner_id or ""), book_id=book_id,
                text_id=f"{book_id}:{body.ch_idx}")
            if _mr.blocked:
                raise HTTPException(
                    422, "内容未通过安全审核，已被拒绝：" + "、".join(_mr.categories or ["违规"]))
            if _mr.verdict in ("review", "warn"):
                warnings.append(
                    "内容安全提示：" + "、".join(_mr.categories or []) +
                    ("（已转人工复核）" if _mr.needs_review else "（已记录）"))
        except HTTPException:
            raise
        except Exception as _e:                 # noqa: BLE001
            # ★ 审核器故障不能阻断采纳（可用性优先），但必须 error 告警
            import logging as _lg
            _lg.getLogger("novelforge").error(
                "★ 采纳时内容审核失败（已放行）：%s: %s", type(_e).__name__, _e)

        ch = await graph.accept_draft(db, b, body.ch_idx, body.title, body.content,
                                      body.gate, register=body.register)

        # ★★ v1.0-E4.5/E4.6：采纳信号采集（判断生成质量的核心依据）
        #
        #   ★ 为什么必须补：此前只能知道"生成了多少"，**不知道"好不好"**。
        #     生成次数多 ≠ 质量好 —— 可能只是用户反复重试。
        #     有生成无采纳（或有采纳但改动巨大）= 质量差。
        #
        #   ★ 采集内容（**全是计数与分桶，无正文**）：
        #     · 本轮是否被闸门警告（字数/质检）
        #     · 采纳后的修改幅度（相对草稿原稿）
        #     · 提示词指纹（★ v1.0-E4.6：从任务的 result_json 里取，
        #       用于"提示词版本 → 采纳率"的 A/B 分析）
        #
        #   ★ 放在 accept_draft 之后：先保证业务成功，再采集
        #     （旁路铁律：采集异常绝不能影响采纳）
        try:
            from apps.api import adoption as _adopt
            _draft_words = 0
            _p_hash = ""
            if body.task_id:
                _t = _task_row(db, body.task_id)
                if _t:
                    _r = jl(_t.result_json, {}) or {}
                    # ★ 草稿原稿可能有备份（采纳前改写）
                    _draft_words = gates.count_words(
                        gates.strip_title(_r.get("_content_backup")
                                          or _r.get("content") or ""))
                    # ★ 提示词指纹：生成时写入任务结果，采纳时读回
                    _p_hash = str(_r.get("prompt_hash") or "")
            # ★ 直接透传指纹（空串表示"没有版本信息"，
            #   adoption 会记成 prompt.none，不影响其它统计）
            _adopt.record_adopt(
                "chapter_write",
                before_words=_draft_words,
                after_words=int(ch.words or 0),
                prompt=_p_hash)
            # 有警告 → 记一次"质量存疑的采纳"（可分析"明知有问题仍采纳"比例）
            if warnings:
                _adopt.record_discard("chapter_write", reason="gate_failed")
        except Exception:                               # noqa: BLE001
            pass                                        # ★ 采集失败不影响采纳

        # v0.14.7：采纳消费草稿——与「丢弃」对齐，把来源任务的正文摘掉。
        # 只在任务里的正文与本次提交一致时才清理（用户可能先在编辑器改过再采纳，
        # 此时任务里的旧稿仍应留在待审区供比对，不能误删）。
        consumed = False
        if body.task_id:
            t = _task_row(db, body.task_id)
            if t and t.book_id == book_id and t.kind == "draft":
                r = jl(t.result_json, {}) or {}
                # ★ v1.0-E4.18 修复：原文精确比对（r.content == body.content）
                #   永不相等 —— 前端发送的是剥掉 `# 第N章` 标题行的正文
                #   （bodyOfDraft），任务的 result_json 存的是含标题全文 →
                #   consumed 永远 False → 采纳后草稿永远留在待审区。
                #   改为 strip_title 双侧对齐：正文一致即视为同一稿；
                #   用户真在编辑器改过（正文不同）仍不相等，旧稿保留供比对。
                if (gates.strip_title(r.get("content") or "")
                        == gates.strip_title(body.content or "")):
                    r["accepted"] = True
                    r["accepted_at"] = str(now())
                    r["_content_backup"] = r.pop("content", "")
                    t.result_json = jd(r)
                    t.updated_at = now()
                    db.commit()
                    consumed = True
        return {"ok": True, "idx": ch.idx, "words": ch.words,
                "summary": ch.summary, "warnings": warnings, "consumed": consumed}
    finally:
        db.close()


# ----------------------------------------------------------------- 短剧工坊（v0.12）
# 小说 → 分集 → 剧本 → 资产 → 分镜 → 媒体生成。状态机语义复刻 ArcReel：
# workflow-status 每次从数据实时推导 blockers / operations / next_action。


class DramaProjectIn(BaseModel):
    title: str = ""
    style: str = ""
    content_mode: str = "drama"
    generation_mode: str = "storyboard"
    aspect_ratio: str = "9:16"


class DramaPlanIn(BaseModel):
    chars_per_episode: int = 1
    max_episodes: int = 30
    # v0.13 时长感知分集：>0 时按「导演预估的每章剧情时长」累计切集，
    # 每集逼近该目标分钟数（替代机械的每集 N 章）
    episode_minutes: float = 0


class DramaManualPlanIn(BaseModel):
    plan: list[dict]


class DramaAssetIn(BaseModel):
    kind: str = "character"
    name: str
    description: str = ""
    voice_style: str = ""


def _drama_ctx(db, book_id: str, project_id: str):
    """载入项目并校验归属；返回 (project, book) 或抛 404。"""
    b = db.get(Book, book_id)
    if not b:
        raise HTTPException(404, "book not found")
    p = drama_mod.get_project(db, project_id)
    if not p or p.book_id != book_id:
        raise HTTPException(404, "drama project not found")
    return p, b


@app.get("/api/books/{book_id}/drama/projects")
def drama_list_projects(book_id: str, db: Session = Depends(get_db)):
    rows = drama_mod.list_projects(db, book_id)
    return {"items": [{"id": p.id, "title": p.title, "style": p.style[:80],
                       "content_mode": p.content_mode,
                       "generation_mode": p.generation_mode,
                       "aspect_ratio": p.aspect_ratio, "status": p.status,
                       "created_at": p.created_at.isoformat() if p.created_at else ""}
                      for p in rows]}


@app.post("/api/books/{book_id}/drama/projects")
async def drama_create_project(book_id: str, body: DramaProjectIn,
                               db: Session = Depends(get_db)):
    b = db.get(Book, book_id)
    if not b:
        raise HTTPException(404, "book not found")
    p = await drama_mod.create_project(db, b, title=body.title, style=body.style,
                                       content_mode=body.content_mode,
                                       generation_mode=body.generation_mode,
                                       aspect_ratio=body.aspect_ratio)
    return {"id": p.id, "title": p.title}


@app.delete("/api/books/{book_id}/drama/projects/{project_id}")
def drama_delete_project(book_id: str, project_id: str, db: Session = Depends(get_db)):
    p, b = _drama_ctx(db, book_id, project_id)
    # 级联清子表（SQLite 无 FK 约束，手工级联）
    for cls in (DramaEpisode, DramaAsset, DramaScene):
        for row in db.scalars(select(cls).where(cls.project_id == p.id)).all():
            db.delete(row)
    db.delete(p)
    db.commit()
    return {"ok": True, "deleted": project_id}


@app.get("/api/books/{book_id}/drama/projects/{project_id}")
def drama_project_detail(book_id: str, project_id: str, episode: int = 0,
                         db: Session = Depends(get_db)):
    p, b = _drama_ctx(db, book_id, project_id)
    st = drama_mod.workflow_status(db, p, b, episode or None)
    # est_seconds 在 episode_plan JSON 里（智能分集时写入），回填进 episodes 列表
    plan_map = {int(x.get("episode")): x for x in jl(p.episode_plan, []) or []
                if isinstance(x, dict) and x.get("episode")}
    for e in st.get("episodes", []):
        hit = plan_map.get(int(e.get("episode") or 0))
        if hit and hit.get("est_seconds"):
            e["est_seconds"] = hit["est_seconds"]
    return {"id": p.id, "title": p.title, "style": p.style,
            "content_mode": p.content_mode, "generation_mode": p.generation_mode,
            "aspect_ratio": p.aspect_ratio, "workflow": st}


class DramaProjectPatch(BaseModel):
    title: str | None = None
    style: str | None = None
    content_mode: str | None = None
    generation_mode: str | None = None
    aspect_ratio: str | None = None


@app.patch("/api/books/{book_id}/drama/projects/{project_id}")
def drama_patch_project(book_id: str, project_id: str, body: DramaProjectPatch,
                        db: Session = Depends(get_db)):
    """项目级设置（画风/画幅/模式）更新。风格纪律按项目隔离的数据载体。"""
    p, b = _drama_ctx(db, book_id, project_id)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(p, k, v)
    p.updated_at = now()
    db.commit()
    return {"ok": True}


# ----------------------------------------------------------------- 封面生成
class CoverIn(BaseModel):
    tagline: str = ""          # 底部/顶部宣传语（可空）
    style: str = "xianxia"     # xianxia（竖排·古风）| modern（横排·现代）
    no_ai: bool = False        # True = 不调图片模型，纯渐变底（用于排版预览）
    hint: str = ""             # 画面补充要求（可空），如"突出主角背影+雷云"


@app.post("/api/books/{book_id}/cover")
async def gen_book_cover(book_id: str, body: CoverIn, db: Session = Depends(get_db)):
    """生成小说封面：AI 无字底图 + PIL 真字体排版叠加。

    v0.13.9：底图 prompt 由 LLM 读全部设定集组稿（类型/基调/梗概/简介/主角/
    世界观/用户 hint），贴合小说内容；LLM 不可用时按题材锚点确定性兜底。
    文字由服务端渲染（Noto CJK），彻底规避扩散模型的乱码/错字问题。
    产物落 drama_media/cover.png，走 /api/files/ 鉴权取出。
    """
    b = db.get(Book, book_id)
    if not b:
        raise HTTPException(404, "book not found")
    from apps.api import cover as cover_mod   # 顶层 import 在包加载下解析不到，必须绝对导入
    base_png = b""
    base_prompt = ""
    if not body.no_ai:
        base_prompt = await cover_mod.build_base_prompt(db, b, hint=body.hint)
        try:
            base_png = await cover_mod.gen_base_image(db, base_prompt)
        except Exception:
            # 底图失败不阻断：降级纯渐变底，文字排版仍然交付
            base_png = b""
    png = cover_mod.compose_cover(b.title, author=b.author or "",
                                  tagline=body.tagline, base_png=base_png or None,
                                  style=body.style)
    rel = cover_mod.save_cover(book_id, png)
    return {"ok": True, "path": rel, "url": f"/api/files/{rel}",
            "ai_base": bool(base_png), "base_prompt": base_prompt}


@app.post("/api/books/{book_id}/drama/projects/{project_id}/plan")
async def drama_plan(book_id: str, project_id: str, body: DramaPlanIn,
                     db: Session = Depends(get_db)):
    p, b = _drama_ctx(db, book_id, project_id)
    r = await drama_mod.plan_episodes(db, p, b,
                                      chars_per_episode=body.chars_per_episode,
                                      max_episodes=body.max_episodes,
                                      episode_minutes=body.episode_minutes)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "分集失败")
    return r


@app.post("/api/books/{book_id}/drama/projects/{project_id}/plan/manual")
def drama_plan_manual(book_id: str, project_id: str, body: DramaManualPlanIn,
                      db: Session = Depends(get_db)):
    p, b = _drama_ctx(db, book_id, project_id)
    r = drama_mod.manual_split(db, p, b, body.plan)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "分集调整失败")
    return r


@app.get("/api/books/{book_id}/drama/projects/{project_id}/episodes/{episode}")
def drama_episode(book_id: str, project_id: str, episode: int,
                  db: Session = Depends(get_db)):
    p, b = _drama_ctx(db, book_id, project_id)
    ep = db.scalar(select(DramaEpisode).where(DramaEpisode.project_id == p.id,
                                              DramaEpisode.episode == episode))
    if not ep:
        raise HTTPException(404, "episode not found")
    scenes = drama_mod.list_scenes(db, p, episode)
    return {"episode": ep.episode, "title": ep.title, "src_from": ep.src_from,
            "src_to": ep.src_to, "status": ep.status,
            "plan_confirmed": bool(ep.plan_confirmed),
            "script": jl(ep.script_json, {}),
            "source_units": len(ep.source_text or ""),
            "scenes": [drama_mod.scene_dict(s) for s in scenes]}


@app.post("/api/books/{book_id}/drama/projects/{project_id}/episodes/{episode}/script")
async def drama_gen_script(book_id: str, project_id: str, episode: int,
                           db: Session = Depends(get_db)):
    p, b = _drama_ctx(db, book_id, project_id)
    r = await drama_mod.generate_script(db, p, b, episode)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "剧本生成失败")
    return r


# ---- 短剧剧本质检（v0.16 B2：把小说线闸门下沉到剧本 JSON）----
@app.get("/api/books/{book_id}/drama/projects/{project_id}/episodes/{episode}/qc")
def drama_episode_qc(book_id: str, project_id: str, episode: int,
                     db: Session = Depends(get_db)):
    """整集剧本质检：AI 腔 / 说书套话 / 台词长度 / 镜头模板化 / 跨镜重复。

    评估报告缺口 4。质检失败不抛错（返回 ok=False + error），避免阻断创作流。
    """
    p, b = _drama_ctx(db, book_id, project_id)
    from apps.api import drama_gates
    try:
        return drama_gates.episode_qc(db, p, episode)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "scenes": 0, "errors": 0, "warns": 0, "score": 0,
                "issues": [], "by_scene": []}


@app.post("/api/books/{book_id}/drama/projects/{project_id}/episodes/{episode}/confirm")
def drama_confirm(book_id: str, project_id: str, episode: int,
                  db: Session = Depends(get_db)):
    p, b = _drama_ctx(db, book_id, project_id)
    r = drama_mod.confirm_plan(db, p, episode)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "确认失败")
    return r


@app.post("/api/books/{book_id}/drama/projects/{project_id}/episodes/{episode}/storyboard")
async def drama_enrich_storyboard(book_id: str, project_id: str, episode: int,
                                  db: Session = Depends(get_db)):
    """D5：对已有剧集重跑八段分镜提示词细化（幂等——已是八段的分镜跳过）。"""
    p, b = _drama_ctx(db, book_id, project_id)
    r = await drama_mod.enrich_storyboard_prompts(db, p, episode)
    if not r.get("ok") and not r.get("enriched"):
        raise HTTPException(400, r.get("error") or "八段细化失败")
    return r


@app.get("/api/books/{book_id}/drama/projects/{project_id}/assets")
def drama_assets(book_id: str, project_id: str, kind: str = "",
                 db: Session = Depends(get_db)):
    p, b = _drama_ctx(db, book_id, project_id)
    rows = drama_mod.list_assets(db, p, kind or None)
    return {"items": [{"id": a.id, "kind": a.kind, "name": a.name,
                       "description": a.description, "voice_style": a.voice_style,
                       "image": a.image, "image_status": a.image_status,
                       "first_seen": a.first_seen,
                       "parent_id": a.parent_id or "",
                       "form_name": a.form_name or ""} for a in rows]}


class DramaAssetFormIn(BaseModel):
    form_name: str
    description: str = ""


@app.post("/api/books/{book_id}/drama/projects/{project_id}/assets/{asset_id}/forms")
def drama_asset_add_form(book_id: str, project_id: str, asset_id: str,
                         body: DramaAssetFormIn, db: Session = Depends(get_db)):
    """给角色/场景资产加一个衍生形态（v0.13.15 C2，ArcReel AssetDerivative 语义）。

    例：陈长安（本体）→ 「老年」「神兽真身」两个衍生行；description 只写变化。
    """
    p, _b = _drama_ctx(db, book_id, project_id)
    base = db.scalar(select(DramaAsset).where(DramaAsset.id == asset_id,
                                              DramaAsset.project_id == p.id))
    if not base:
        raise HTTPException(404, "asset not found")
    if base.parent_id:
        raise HTTPException(400, "衍生形态不能再派生，请挂到本体上")
    fname = (body.form_name or "").strip()[:64]
    if not fname:
        raise HTTPException(400, "form_name 不能为空")
    # 同本体同名形态去重
    dup = db.scalar(select(DramaAsset).where(DramaAsset.parent_id == base.id,
                                             DramaAsset.form_name == fname))
    if dup:
        return {"ok": True, "id": dup.id, "existed": True}
    row = DramaAsset(id=new_id("da"), project_id=p.id, kind=base.kind,
                     name=f"{base.name}（{fname}）",
                     description=body.description or "", voice_style=base.voice_style,
                     first_seen=base.first_seen, parent_id=base.id, form_name=fname)
    db.add(row)
    db.commit()
    return {"ok": True, "id": row.id}


@app.post("/api/books/{book_id}/drama/projects/{project_id}/assets")
def drama_upsert_asset(book_id: str, project_id: str, body: DramaAssetIn,
                       db: Session = Depends(get_db)):
    p, b = _drama_ctx(db, book_id, project_id)
    r = drama_mod.upsert_asset(db, p, body.kind, body.name,
                               body.description, body.voice_style)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "保存失败")
    return r


@app.post("/api/books/{book_id}/drama/projects/{project_id}/assets/enrich")
async def drama_enrich_assets(book_id: str, project_id: str,
                              db: Session = Depends(get_db)):
    p, b = _drama_ctx(db, book_id, project_id)
    r = await drama_mod.enrich_assets(db, p, b)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "描述生成失败")
    return r


@app.post("/api/books/{book_id}/drama/projects/{project_id}/assets/{asset_id}/generate-image")
async def drama_asset_generate_image(book_id: str, project_id: str, asset_id: str,
                                     db: Session = Depends(get_db)):
    """资产定妆图：按资产描述 + 画风基调生图，写回 asset.image（v0.13.11）。"""
    p, b = _drama_ctx(db, book_id, project_id)
    a = db.scalar(select(DramaAsset).where(DramaAsset.id == asset_id,
                                           DramaAsset.project_id == p.id))
    if not a:
        raise HTTPException(404, "asset not found")
    r = await drama_mod.generate_asset_image(db, p, a)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "生成失败")
    return r


@app.post("/api/books/{book_id}/drama/projects/{project_id}/episodes/{episode}/generate-all/{target}")
async def drama_generate_all(book_id: str, project_id: str, episode: int,
                             target: str, db: Session = Depends(get_db)):
    """批量生成分镜媒体（image/video/audio）——v0.13.15 改为数据库队列。

    v0.13.11：asyncio.spawn 直接逐镜顺序执行；单镜失败不中断但整批不可恢复、
    无重试、重启全丢。v0.13.15（ArcReel 定点移植 C1）：每镜一行 DramaTask 入队，
    分道 worker 认领执行（image/audio 双道、video 单道），单任务失败自动重试
    （max_attempts=2），进程重启 queued/running 自动恢复续跑，产物已在的直接跳过。
    台账兼容：整批仍建一条 Task 台账行供 SSE/UI，worker 把进度聚合进台账。
    """
    # E4.23：旧前端契约传过复数（images/videos/audios）→ 400 报错。
    # 这里做容错归一：浏览器缓存旧构建/第三方调用方不受影响，新前端已传单数。
    if target in ("images", "videos", "audios"):
        target = target[:-1]
    if target not in ("image", "video", "audio"):
        raise HTTPException(400, "target 必须是 image/video/audio")
    p, b = _drama_ctx(db, book_id, project_id)
    scenes = drama_mod.list_scenes(db, p, episode)
    if not scenes:
        raise HTTPException(400, "该集还没有分镜")

    from apps.api import drama_queue as dq
    ledger = _new_task(db, book_id, f"drama_batch_{target}", target=episode,
                       progress=f"排队 {len(scenes)} 个分镜…")
    batch_id = dq.new_batch_id()
    n = len(scenes)

    # E4.23 资产自动管线（仅 image/video——参考图一致性链路只作用于画面）：
    # ① 角色自动提取落库（幂等，补老剧本/缺字段数据）
    # ② 缺描述资产 LLM 按出场上下文补全（单次调用，fail-open）
    # ③ 缺图角色资产入队定妆图（衍生形态依赖本体任务，保同一张脸）
    # 分镜图/视频任务由 worker 侧等待定妆图就绪（defer，见 drama_queue）。
    asset_n = 0
    if target in ("image", "video"):
        asset_n = await _auto_asset_pipeline(db, book_id, p, b, scenes,
                                             batch_id, ledger.id)
        if asset_n:
            ledger.progress = f"排队 {asset_n} 张定妆图 + {n} 个分镜…"
    # v0.13.15 C2 依赖序：视频任务自动依赖同分镜的图像任务（ArcReel 连贯性精髓——
    # 先有图才有图生视频的输入；同一分镜图已有产物或已有完成行时依赖可跳过）。
    scene_img_task: dict[str, str] = {}
    video_task_ids: dict[str, str] = {}   # D7：scene_id → 本批 video 任务行 id（链式依赖）
    if target == "video":
        done_img_ids = {(t.resource_id) for t in db.scalars(select(DramaTask).where(
            DramaTask.project_id == project_id, DramaTask.task_type == "image",
            DramaTask.status.in_(("done", "skipped")))).all()}
        active_img_ids = {(t.resource_id) for t in db.scalars(select(DramaTask).where(
            DramaTask.project_id == project_id, DramaTask.task_type == "image",
            DramaTask.status.in_(("queued", "running")))).all()}
        for s in scenes:
            if s.id in done_img_ids:
                continue  # 图已生成，视频直接跑
            if s.id in active_img_ids:
                # 已有活动中图像任务：依赖它（ArcReel 的跨批次依赖复用）
                dep = db.scalar(select(DramaTask).where(
                    DramaTask.project_id == project_id, DramaTask.task_type == "image",
                    DramaTask.resource_id == s.id,
                    DramaTask.status.in_(("queued", "running")),
                    DramaTask.status == "queued").order_by(DramaTask.created_at.desc()))
                if dep:
                    scene_img_task[s.id] = dep.id
                continue
            # 本批次顺带补一个图像任务，视频依赖它
            img_task = dq.enqueue(db, book_id=book_id, project_id=project_id,
                                  episode=episode, task_type="image",
                                  resource_type="scene", resource_id=s.id,
                                  resource_key=s.scene_id or s.id, batch_id=batch_id,
                                  ledger_task_id=ledger.id)
            scene_img_task[s.id] = img_task.id
    for i, s in enumerate(scenes, 1):
        deps: list[str] = []
        if target == "video":
            # D7 末帧传递：同批内按 idx 链式依赖（video_i 依赖 video_{i-1}）——
            # worker 执行时抽前镜末帧作本镜 i2v 参考图，镜间衔接连贯。
            # DramaTask 只有单 dependency_id 槽：链式前镜优先（末帧来源），
            # 图像依赖在 _exec_scene_media 里由 ref_image 取 image_path 兜底
            # （分镜图已完成则直接可用，未完成则 i2v 无图自动降级 t2v）。
            prev = max((x for x in scenes if x.idx < s.idx),
                       key=lambda x: x.idx, default=None)
            if prev is not None and prev.id in video_task_ids:
                deps.append(video_task_ids[prev.id])
            elif s.id in scene_img_task:
                deps.append(scene_img_task[s.id])
        else:
            deps.append(scene_img_task.get(s.id, ""))
        t = dq.enqueue(db, book_id=book_id, project_id=project_id, episode=episode,
                       task_type=target, resource_type="scene", resource_id=s.id,
                       resource_key=s.scene_id or s.id, batch_id=batch_id,
                       ledger_task_id=ledger.id,
                       dependency_id=deps[0] if deps and deps[0] else "")
        if target == "video":
            video_task_ids[s.id] = t.id
    db.commit()
    dq.ensure_worker()
    task_id = ledger.id
    db.close()

    progress = f"已排队 {n} 个分镜"
    if asset_n:
        progress = f"已排队 {asset_n} 张角色定妆图 + {n} 个分镜"
    return {"task_id": task_id, "batch_id": batch_id, "status": "queued",
            "progress": progress, "scene_count": n, "asset_tasks": asset_n}


async def _auto_asset_pipeline(db: Session, book_id: str, p, b,
                               scenes, batch_id: str, ledger_task_id: str) -> int:
    """E4.23：批量出图/出视频前的资产自动管线，返回入队的定妆图任务数。

    链路：① 角色自动提取（ensure_character_assets，幂等）→
    ② 缺描述资产 LLM 补全（enrich_assets，按出场上下文 grounding）→
    ③ 缺图角色资产入队 asset_image（衍生形态依赖本体任务行，保同一张脸）。

    整段 fail-open：任何一步异常都回滚后继续——管线失败只降级为
    「参考图只收集已生成的」旧行为，绝不阻塞分镜生成主流程。
    """
    from apps.api import drama_queue as dq
    from apps.api.models import DramaTask as DT
    try:
        drama_mod.ensure_character_assets(db, p, scenes)
        if drama_mod.missing_description_assets(db, p):
            r = await drama_mod.enrich_assets(db, p, b)
            if not r.get("ok"):
                import sys as _sys
                print(f"[drama] 自动补全资产描述失败（继续生成）："
                      f"{r.get('error')}", file=_sys.stderr, flush=True)
        busy = {t.resource_id for t in db.scalars(select(DT).where(
            DT.project_id == p.id, DT.task_type == "asset_image",
            DT.status.in_(("queued", "running")))).all()}
        base_task: dict[str, str] = {}
        n = 0

        def _eligible(a) -> bool:
            if a.id in busy:
                return False
            if (a.image or "").strip() and a.image_status == "done":
                return False
            # 无描述（含衍生形态本体也无描述）→ 出图必然失败，跳过（手动入口兜底）
            parent = db.get(DramaAsset, a.parent_id) if a.parent_id else None
            desc_ok = (a.description or "").strip() or (
                (parent.description or "").strip() if parent else "")
            return bool(desc_ok)

        chars = drama_mod.list_assets(db, p, "character")
        # 两遍入队：先本体（建 base_task 映射）后衍生形态——形态的
        # dependency_id 依赖本体任务行，顺序颠倒会丢依赖（保脸失效）
        for a in [x for x in chars if not x.parent_id] + \
                 [x for x in chars if x.parent_id]:
            if not _eligible(a):
                continue
            t = dq.enqueue(db, book_id=book_id, project_id=p.id, episode=0,
                           task_type="asset_image", resource_type="asset",
                           resource_id=a.id, resource_key=a.name,
                           batch_id=batch_id, ledger_task_id=ledger_task_id,
                           dependency_id=drama_mod.asset_image_dependency(
                               db, p, a, base_task))
            if not a.parent_id:
                base_task[a.id] = t.id
            n += 1
        if n:
            db.commit()
        return n
    except Exception as e:
        import sys as _sys
        print(f"[drama] 资产自动管线异常（fail-open，继续分镜生成）："
              f"{type(e).__name__}: {e}", file=_sys.stderr, flush=True)
        try:
            db.rollback()
        except Exception:
            pass
        return 0


@app.get("/api/books/{book_id}/drama/queue")
def drama_queue_status(book_id: str, db: Session = Depends(get_db)):
    """队列状态总览：分任务类型与状态计数 + 最近 50 行明细。"""
    from apps.api import drama_queue as dq
    from apps.api.models import DramaTask as DT
    rows = db.scalars(select(DT).where(DT.book_id == book_id)
                      .order_by(DT.created_at.desc()).limit(50)).all()
    counts: dict[str, dict[str, int]] = {}
    for t in db.scalars(select(DT).where(DT.book_id == book_id)).all():
        d = counts.setdefault(t.task_type, {})
        d[t.status] = d.get(t.status, 0) + 1
    return {"counts": counts, "recent": [
        {"id": t.id, "batch_id": t.batch_id, "task_type": t.task_type,
         "resource_key": t.resource_key, "status": t.status,
         "attempts": t.attempts, "error": t.error or "",
         "episode": t.episode, "updated_at": str(t.updated_at or "")} for t in rows]}


@app.post("/api/books/{book_id}/drama/queue/{batch_id}/cancel")
def drama_queue_cancel(book_id: str, batch_id: str, db: Session = Depends(get_db)):
    """取消一个批次：queued 行标 cancelled；running 行由 worker 完成后自然收尾。

    v0.13.17：取消后若批次内已无 queued/running 行（全 cancelled 或部分已完成），
    立即收尾台账，前端进度流立刻拿到终态而不是等 worker 空转。
    """
    from apps.api.models import DramaTask as DT
    from apps.api import drama_queue as dq
    rows = db.scalars(select(DT).where(DT.book_id == book_id,
                                       DT.batch_id == batch_id)).all()
    if not rows:
        raise HTTPException(404, "batch not found")
    n = 0
    ledger_id = ""
    for t in rows:
        if t.status == "queued":
            t.status = "cancelled"
            t.finished_at = now()
            t.updated_at = now()
            n += 1
        if t.ledger_task_id and not ledger_id:
            ledger_id = t.ledger_task_id
    db.commit()
    dq.finalize_batch_if_complete(db, batch_id, ledger_id)
    return {"ok": True, "cancelled": n}


@app.post("/api/books/{book_id}/drama/projects/{project_id}/assets/generate-images")
async def drama_assets_generate_images(book_id: str, project_id: str,
                                       db: Session = Depends(get_db)):
    """批量资产定妆图（队列版，v0.13.15 C1）：全部缺图资产逐个入队 asset_image 任务。"""
    p, _b = _drama_ctx(db, book_id, project_id)
    from apps.api import drama_queue as dq
    from apps.api.models import DramaTask as DT
    # 排除活动中/已完成的
    busy = {(t.resource_id) for t in db.scalars(select(DT).where(
        DT.project_id == p.id, DT.task_type == "asset_image",
        DT.status.in_(("queued", "running")))).all()}
    targets = [a for a in drama_mod.list_assets(db, p)
               if a.image_status != "done" and a.id not in busy]
    # E4.23：本体排在衍生形态之前——形态图要用本体定妆图做 IP-Adapter 参考
    # （保脸机制），依赖在入队时通过 dependency_id 落到本体任务行。
    targets.sort(key=lambda a: (1 if a.parent_id else 0, a.id))
    if not targets:
        return {"task_id": None, "queued": 0, "progress": "没有需要出图的资产"}
    ledger = _new_task(db, book_id, "drama_batch_asset_image", target=0,
                       progress=f"排队 {len(targets)} 个资产定妆图…")
    batch_id = dq.new_batch_id()
    base_task: dict[str, str] = {}
    for a in targets:
        t = dq.enqueue(db, book_id=book_id, project_id=p.id, episode=0,
                       task_type="asset_image", resource_type="asset",
                       resource_id=a.id, resource_key=a.name,
                       batch_id=batch_id, ledger_task_id=ledger.id,
                       dependency_id=drama_mod.asset_image_dependency(
                           db, p, a, base_task))
        if not a.parent_id:
            base_task[a.id] = t.id
    db.commit()
    dq.ensure_worker()
    return {"task_id": ledger.id, "batch_id": batch_id, "status": "queued",
            "queued": len(targets), "progress": f"已排队 {len(targets)} 个资产"}


@app.post("/api/books/{book_id}/drama/projects/{project_id}/grid-generate/{episode}")
async def drama_grid_generate(book_id: str, project_id: str, episode: int,
                              cells: int = 9, db: Session = Depends(get_db)):
    """C4-1 多宫格分镜：一集前 N 个分镜画进一张宫格大图（2/4/6/9 格）。

    审阅产物：不写回 scene 行，成品图路径在台账 result_json 与 DramaTask 里。
    """
    from apps.api import drama_queue as dq
    from apps.api.models import DramaTask as DT
    p, _b = _drama_ctx(db, book_id, project_id)
    cells = cells if cells in (2, 4, 6, 9) else 9
    scenes = drama_mod.list_scenes(db, p, episode)[:cells]
    if not scenes:
        raise HTTPException(400, "该集还没有分镜")
    # 已有活动中宫格任务：复用，不重复排队
    busy = db.scalar(select(DT).where(
        DT.project_id == p.id, DT.task_type == "grid", DT.episode == episode,
        DT.status.in_(("queued", "running"))))
    if busy:
        return {"task_id": busy.ledger_task_id or None, "batch_id": busy.batch_id,
                "status": "queued", "progress": "宫格图生成中…"}
    ledger = _new_task(db, book_id, "drama_grid", target=episode,
                       progress=f"生成 {len(scenes)} 格分镜宫格图…")
    batch_id = dq.new_batch_id()
    dq.enqueue(db, book_id=book_id, project_id=p.id, episode=episode,
               task_type="grid", resource_type="grid",
               resource_id=f"ep{episode}_grid", resource_key=f"EP{episode}宫格",
               batch_id=batch_id, ledger_task_id=ledger.id,
               payload={"scene_ids": [s.id for s in scenes], "cells": cells})
    db.commit()
    dq.ensure_worker()
    return {"task_id": ledger.id, "batch_id": batch_id, "status": "queued",
            "cells": len(scenes), "progress": f"已排队 {len(scenes)} 格宫格图"}


@app.post("/api/books/{book_id}/drama/projects/{project_id}/export-jianying/{episode}")
async def drama_export_jianying(book_id: str, project_id: str, episode: int,
                                db: Session = Depends(get_db)):
    """C4-2 剪映草稿导出：组装 draft_content.json + 素材打包 zip（队列执行）。

    产物 zip 落书籍仓库 drama_media/，通过 /api/files/ 下载；解压后把
    jianying_ep{n} 文件夹放进剪映草稿目录即可打开。
    """
    from apps.api import drama_queue as dq
    from apps.api.models import DramaTask as DT
    p, _b = _drama_ctx(db, book_id, project_id)
    scenes = drama_mod.list_scenes(db, p, episode)
    if not scenes:
        raise HTTPException(400, "该集还没有分镜")
    if not any((s.video_path or "").strip() for s in scenes):
        raise HTTPException(400, "该集还没有任何视频产物——先批量生成视频再导出")
    busy = db.scalar(select(DT).where(
        DT.project_id == p.id, DT.task_type == "jianying", DT.episode == episode,
        DT.status.in_(("queued", "running"))))
    if busy:
        return {"task_id": busy.ledger_task_id or None, "batch_id": busy.batch_id,
                "status": "queued", "progress": "剪映草稿导出中…"}
    ledger = _new_task(db, book_id, "drama_jianying", target=episode,
                       progress=f"组装 EP{episode} 剪映草稿…")
    batch_id = dq.new_batch_id()
    dq.enqueue(db, book_id=book_id, project_id=p.id, episode=episode,
               task_type="jianying", resource_type="export",
               resource_id=f"ep{episode}_jianying", resource_key=f"EP{episode}剪映导出",
               batch_id=batch_id, ledger_task_id=ledger.id,
               payload={"scene_ids": [s.id for s in scenes]})
    db.commit()
    dq.ensure_worker()
    return {"task_id": ledger.id, "batch_id": batch_id, "status": "queued",
            "progress": "已排队剪映草稿导出"}


# ---- 短剧成片合成（v0.19.5 F4）----
class FilmIn(BaseModel):
    """成片合成参数。"""
    burn_subtitles: int = 1      # 是否烧硬字幕
    fit_duration: int = 1        # 是否把素材时长对齐 duration_seconds


@app.post("/api/books/{book_id}/drama/projects/{project_id}/film/{episode}")
async def drama_export_film(book_id: str, project_id: str, episode: int,
                            body: FilmIn | None = None,
                            db: Session = Depends(get_db)):
    """一键合成**成片 mp4**：分镜拼接 + 旁白混音 + 台词硬字幕。

    与「剪映草稿导出」的分工：
      · 剪映草稿 → 要精修（调色/配乐/转场）
      · **成片 mp4（本端点）→ 要快速验收或直接发布**

    走后台任务（ffmpeg 合成可能几十秒到几分钟），前端挂任务流看进度。
    """
    from apps.api import film_export as fe
    from apps.api import config as _cfg
    p, _b = _drama_ctx(db, book_id, project_id)
    scenes = drama_mod.list_scenes(db, p, episode)
    if not scenes:
        raise HTTPException(400, "该集还没有分镜")
    if not any((s.video_path or "").strip() for s in scenes):
        raise HTTPException(400, "该集还没有任何视频产物——先批量生成视频再合成成片")

    burn = bool((body.burn_subtitles if body else 1))
    fit = bool((body.fit_duration if body else 1))
    task = _new_task(db, book_id, "drama_film", target=episode,
                     progress=f"正在合成 EP{episode} 成片…")
    scene_ids = [s.id for s in scenes]

    async def runner():
        db2 = SessionLocal()
        try:
            p2 = db2.get(type(p), p.id)
            scenes2 = drama_mod.list_scenes(db2, p2, episode)
            _task_event(db2, _task_row(db2, task.id),
                        {"kind": "stage", "text": f"合成 EP{episode}："
                                                  f"{len(scenes2)} 个分镜，开始归一化…"})
            import asyncio as _a
            r = await _a.to_thread(
                fe.export_film, p2, episode, scenes2, pathlib.Path(_cfg.BOOKS_DIR),
                burn_subtitles=burn, fit_duration=fit)
            if r.get("ok"):
                for w in (r.get("warnings") or []):
                    _task_event(db2, _task_row(db2, task.id),
                                {"kind": "warn", "text": w})
                _task_event(db2, _task_row(db2, task.id),
                            {"kind": "stage",
                             "text": f"成片完成：{r.get('seconds')}s / "
                                     f"{r.get('segments')} 段 / 字幕 {r.get('subtitles')} 条"},
                            status="done", result=r)
            else:
                _task_event(db2, _task_row(db2, task.id),
                            {"kind": "error", "text": "合成失败：" + str(r.get("error"))},
                            status="error", error=str(r.get("error"))[:512])
        except Exception as e:
            _task_event(db2, _task_row(db2, task.id),
                        {"kind": "error", "text": f"合成异常：{type(e).__name__}: {e}"},
                        status="error", error=f"{type(e).__name__}: {e}"[:512])
        finally:
            db2.close()

    _spawn(runner(), task_id=task.id, label=f"drama_film:ep{episode}")
    return {"ok": True, "task_id": task.id, "episode": episode,
            "status": "running", "progress": task.progress,
            "scenes": len(scene_ids)}


@app.get("/api/books/{book_id}/drama/projects/{project_id}/film/capability")
def drama_film_capability(book_id: str, project_id: str):
    """成片能力的自检（ffmpeg / 中文字体是否就绪）。"""
    from apps.api import film_export as fe
    return {"ffmpeg": fe.ffmpeg_available(),
            "font": fe.find_cjk_font() or "",
            "resolution": f"{fe.DEFAULT_W}x{fe.DEFAULT_H}",
            "note": "无 ffmpeg 时会降级为字节拼接（无字幕/无混音）"}


@app.get("/api/books/{book_id}/drama/projects/{project_id}/exports")
def drama_list_exports(book_id: str, project_id: str, db: Session = Depends(get_db)):
    """列出已完成导出的剪映 zip + 宫格图（供前端下载入口）。

    v0.13.17 修复：/api/files/ 要求路径第一段是 book_id（serve_book_file 拿它
    鉴权书籍），而 result_json 里存的 path 是相对书仓根的 `drama_media/x.zip`——
    旧版直接拼 `/api/files/{path}` 一律 404。这里统一补上 book_id 前缀。
    """
    p, _b = _drama_ctx(db, book_id, project_id)
    from apps.api.models import DramaTask as DT
    rows = db.scalars(select(DT).where(
        DT.project_id == p.id, DT.task_type == "jianying",
        DT.status == "done").order_by(DT.finished_at.desc())).all()
    out = []
    for t in rows:
        r = jl(t.result_json, {}) or {}
        if r.get("path"):
            out.append({"path": r["path"], "episode": t.episode,
                        "url": f"/api/files/{book_id}/{r['path']}",
                        "duration_seconds": r.get("duration_seconds", 0),
                        "video_count": r.get("video_count", 0),
                        "finished_at": str(t.finished_at or "")})
    # C4-1 宫格图成品（整图，不写回 scene 行，产物路径在 result_json）
    grids = []
    for t in db.scalars(select(DT).where(
            DT.project_id == p.id, DT.task_type == "grid",
            DT.status.in_(("done", "skipped"))).order_by(
            DT.finished_at.desc())).all():
        r = jl(t.result_json, {}) or {}
        if r.get("path"):
            grids.append({"path": r["path"], "episode": t.episode,
                          "url": f"/api/files/{book_id}/{r['path']}",
                          "cells": (jl(t.payload_json, {}) or {}).get("cells", 0),
                          "finished_at": str(t.finished_at or "")})
    return {"exports": out, "grids": grids}


@app.get("/api/books/{book_id}/media-usage")
def media_usage(book_id: str, days: int = 30, db: Session = Depends(get_db)):
    """媒体用量台账汇总（v0.13.15 C3）：按媒体类型/供应商/错误码聚合 + 最近明细。

    ArcReel lib/billing/usage_summary 的简化版：本地方案无供应商账单，
    actual_cost 恒 0，先给「调用了多少次、成功率、失败归因、耗时分布」。
    """
    from apps.api.models import MediaCall
    import datetime as _dt
    cutoff = now() - _dt.timedelta(days=max(1, min(days, 365)))
    rows = db.scalars(select(MediaCall).where(
        MediaCall.book_id == book_id, MediaCall.created_at >= cutoff
    ).order_by(MediaCall.created_at.desc())).all()
    by_type: dict[str, dict] = {}
    by_err: dict[str, int] = {}
    for c in rows:
        d = by_type.setdefault(c.media_type, {"total": 0, "ok": 0, "error": 0,
                                              "avg_ms": 0})
        d["total"] += 1
        d["ok" if c.status == "ok" else "error"] += 1
        # v0.13.17 修复：失败但无 error_code 的调用旧版不计数（三元链歧义）
        code = c.error_code or ("none" if c.status == "ok" else "unclassified")
        by_err[code] = by_err.get(code, 0) + 1
    # 平均耗时
    for k, d in by_type.items():
        ms = [c.duration_ms for c in rows if c.media_type == k]
        d["avg_ms"] = int(sum(ms) / len(ms)) if ms else 0
    return {"days": days, "total": len(rows), "by_type": by_type,
            "errors": {k: v for k, v in by_err.items() if k != "none"},
            "recent": [{"id": c.id, "media_type": c.media_type,
                        "provider": c.provider, "model_id": c.model_id[:48],
                        "status": c.status, "error_code": c.error_code,
                        "duration_ms": c.duration_ms,
                        "created_at": str(c.created_at or "")} for c in rows[:20]]}


async def _run_drama_batch_flow(db, book_id: str, project_id: str,
                                episode: int, target: str, task_id: str) -> None:
    """【v0.13.15 起废弃】逐镜顺序执行批量生成——已被 drama_queue 队列取代。

    保留函数体供回退参考，不再有任何调用方。
    """
    p, _b = _drama_ctx(db, book_id, project_id)
    scenes = drama_mod.list_scenes(db, p, episode)
    n = len(scenes)

    # 缺描述资产先自动补全（图像提示词展开依赖资产描述）
    missing = drama_mod.missing_description_assets(db, p)
    if missing and target in ("image", "video"):
        _task_event(db, _task_row(db, task_id),
                    {"kind": "stage", "text": f"先补全 {len(missing)} 个缺描述资产…"})
        r = await drama_mod.enrich_assets(db, p, _b)
        if not r.get("ok"):
            _task_event(db, _task_row(db, task_id),
                        {"kind": "warn", "text": f"描述补全失败（继续生成）：{r.get('error')}"})

    _task_event(db, _task_row(db, task_id),
                {"kind": "stage",
                 "text": f"开始批量{'生成图像' if target == 'image' else '生成视频' if target == 'video' else '配音'}：共 {n} 个分镜"},
                progress=f"0/{n}")
    ok_n, fail_n = 0, 0
    for i, s in enumerate(scenes, 1):
        cur = _task_row(db, task_id)
        if cur is None:
            return
        sid = s.scene_id or s.id
        _task_event(db, cur,
                    {"kind": "stage", "text": f"（{i}/{n}）正在生成 {sid} …"})
        try:
            r = await drama_mod.generate_scene_media(db, p, s, target)
        except Exception as e:
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if r.get("ok"):
            ok_n += 1
            _task_event(db, cur, {"kind": "action",
                                  "text": f"✓ {sid} 完成"}, progress=f"{i}/{n}")
        else:
            fail_n += 1
            _task_event(db, cur,
                        {"kind": "warn",
                         "text": f"✗ {sid} 失败：{(r.get('error') or '')[:160]}"},
                        progress=f"{i}/{n}")
    _task_event(db, _task_row(db, task_id),
                {"kind": "done", "status": "ok" if fail_n == 0 else "partial",
                 "progress": f"完成 {ok_n}/{n}，失败 {fail_n}",
                 "ok": ok_n, "failed": fail_n, "total": n},
                status="done" if fail_n == 0 else "error",
                result={"ok": ok_n, "failed": fail_n, "total": n},
                error=None if fail_n == 0 else f"{fail_n}/{n} 个分镜失败")


@app.post("/api/books/{book_id}/drama/projects/{project_id}/scenes/{scene_id}/generate/{target}")
async def drama_scene_generate(book_id: str, project_id: str, scene_id: str,
                               target: str, db: Session = Depends(get_db)):
    # E4.23：与 generate-all 同源的复数容错（images/videos/audios → 单数）
    if target in ("images", "videos", "audios"):
        target = target[:-1]
    if target not in ("image", "video", "audio"):
        raise HTTPException(400, "target 必须是 image/video/audio")
    p, b = _drama_ctx(db, book_id, project_id)
    s = db.scalar(select(DramaScene).where(DramaScene.id == scene_id,
                                           DramaScene.project_id == p.id))
    if not s:
        raise HTTPException(404, "scene not found")
    r = await drama_mod.generate_scene_media(db, p, s, target)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "生成失败")
    return r


@app.post("/api/books/{book_id}/drama/duration-estimate")
async def drama_estimate(book_id: str, db: Session = Depends(get_db)):
    """导演/编剧视角通读全书，逐章预估剧情时长（智能分集的前置步骤）。"""
    b = db.get(Book, book_id)
    if not b:
        raise HTTPException(404, "book not found")
    r = await drama_mod.estimate_durations(db, b)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "时长预估失败")
    return r


@app.get("/api/books/{book_id}/drama/projects/{project_id}/scenes/{scene_id}/audio-plan")
def drama_scene_audio_plan(book_id: str, project_id: str, scene_id: str,
                           mode: str = "", db: Session = Depends(get_db)):
    """D14-r 音频策略预览：展示本镜台词会如何分流（TTS / LTX 原生 / 跳过）。

    纯只读、不生成——给前端「路由说明」面板用，也便于排查
    「为什么这句台词没被 TTS 念」一类问题。mode 缺省用项目级档位。
    """
    p, b = _drama_ctx(db, book_id, project_id)
    s = db.scalar(select(DramaScene).where(DramaScene.id == scene_id,
                                           DramaScene.project_id == p.id))
    if not s:
        raise HTTPException(404, "scene not found")
    from apps.api import audio_route as _ar
    m = _ar.audio_plan_for_project(mode or getattr(p, "content_mode", ""))
    plan = _ar.plan_scene_audio(jl(s.utterances, []) or [], mode=m)
    return {"ok": True, "scene_id": s.id, **plan}


# ---------------------------------------------------------------- D19 音色资产化


class VoiceCastIn(BaseModel):
    """人工指定某角色的声纹。不传的字段沿用自动值。"""
    speaker: str
    voice: str = ""
    rate: float | None = None
    pitch: float | None = None
    style: str = ""
    locked: bool = True      # 默认锁定，防止被下次自动分配覆盖


@app.get("/api/books/{book_id}/drama/projects/{project_id}/voice-cast")
def drama_voice_cast(book_id: str, project_id: str,
                     db: Session = Depends(get_db)):
    """D19 声纹表：项目里每个角色的 TTS 音色 + 推断特征 + 来源（auto/manual）。

    只读预览——让用户先看清「谁会用哪个嗓子」，再决定要不要人工改。
    数据来源：角色资产 voice_profile（人工）∪ 自动分配（确定性，跨镜头稳定）。
    """
    p, _b = _drama_ctx(db, book_id, project_id)
    from apps.api import voice_cast as _vc
    cast = _vc.load_cast_from_db(db, p, book_id)
    items = [{"speaker": k, **v} for k, v in sorted(cast.items())]
    return {"ok": True, "project_id": p.id, "count": len(items),
            "items": items,
            "pool": {k: list(v) for k, v in _vc.VOICE_POOL.items()}}


@app.post("/api/books/{book_id}/drama/projects/{project_id}/voice-cast")
def drama_voice_cast_set(book_id: str, project_id: str, body: VoiceCastIn,
                         db: Session = Depends(get_db)):
    """D19：人工指定/覆盖某角色的声纹（写回角色资产的 voice_profile）。

    写回后该角色在本项目所有镜头都用这个音色（locked=true 时自动分配不再动它）。
    找不到同名角色资产时自动补建一条（识别出来的角色也能直接指定音色）。
    """
    p, _b = _drama_ctx(db, book_id, project_id)
    speaker = (body.speaker or "").strip()
    if not speaker:
        raise HTTPException(400, "speaker 不能为空")
    from apps.api import voice_cast as _vc
    import json as _json

    # 找角色资产（衍生形态归本体，避免给「陈长安（老年）」单独配音色）
    asset = None
    for a in db.scalars(select(DramaAsset).where(
            DramaAsset.project_id == p.id)).all():
        if a.kind != "character":
            continue
        base = re.sub(r"（[^）]*）$", "", a.name or "")
        if base == speaker or a.name == speaker:
            asset = a
            break
    if asset is None:
        asset = DramaAsset(id=new_id("da"), project_id=p.id, kind="character",
                           name=speaker)
        db.add(asset)
        db.flush()

    old = _vc._load_json(getattr(asset, "voice_profile", "")) or {}
    prof = _vc.resolve_profile(speaker, asset.voice_style or "",
                               explicit={**old, "voice": body.voice or old.get("voice"),
                                         "source": "manual", "locked": body.locked})
    if body.rate is not None:
        prof.rate = _vc._clamp(body.rate, _vc.RATE_MIN, _vc.RATE_MAX)
    if body.pitch is not None:
        prof.pitch = _vc._clamp(body.pitch, _vc.PITCH_MIN, _vc.PITCH_MAX)
    if body.style:
        prof.style = body.style
    prof.source = "manual"
    prof.locked = bool(body.locked)
    asset.voice_profile = _json.dumps(prof.to_dict(), ensure_ascii=False)
    db.commit()
    return {"ok": True, "speaker": speaker, "asset_id": asset.id,
            "profile": prof.to_dict()}


@app.post("/api/books/{book_id}/drama/projects/{project_id}/voice-cast/auto")
def drama_voice_cast_auto(book_id: str, project_id: str,
                          force: bool = False, db: Session = Depends(get_db)):
    """D19：一键自动分配全部角色声纹并落库。

    force=false（默认）：只补没配过的角色，已 locked 的人工配置原样保留。
    force=true：连人工配置一起重算（仍会跳过 locked=true 的行，除非先解锁）。

    分配是确定性的（角色名 hash + 特征分层 + 冲突消解），因此重复调用
    结果稳定——这是跨镜头音色一致的技术保证。
    """
    p, _b = _drama_ctx(db, book_id, project_id)
    from apps.api import voice_cast as _vc
    import json as _json

    cast = _vc.load_cast_from_db(db, p, book_id)
    if not cast:
        return {"ok": True, "assigned": 0, "items": [],
                "note": "项目里还没有可分配的角色（先做剧本/分镜，或添加角色资产）"}

    assets = {a.id: a for a in db.scalars(select(DramaAsset).where(
        DramaAsset.project_id == p.id,
        DramaAsset.kind == "character")).all()}
    by_base: dict[str, DramaAsset] = {}
    for a in assets.values():
        base = re.sub(r"（[^）]*）$", "", a.name or "")
        by_base.setdefault(base or a.name, a)

    assigned, skipped = 0, 0
    for name, prof in cast.items():
        a = by_base.get(name)
        old = _vc._load_json(getattr(a, "voice_profile", "")) if a else None
        if a is not None and old and old.get("locked") and not force:
            skipped += 1
            continue
        if a is None:
            a = DramaAsset(id=new_id("da"), project_id=p.id, kind="character",
                           name=name)
            db.add(a)
            by_base[name] = a
        prof = dict(prof)
        prof["source"] = "auto"
        prof["locked"] = bool(old.get("locked")) if old else False
        a.voice_profile = _json.dumps(prof, ensure_ascii=False)
        assigned += 1
    db.commit()
    return {"ok": True, "assigned": assigned, "locked_skipped": skipped,
            "total": len(cast),
            "items": [{"speaker": k, "voice": v.get("voice"),
                       "traits": v.get("traits")} for k, v in sorted(cast.items())]}


@app.get("/api/books/{book_id}/drama/projects/{project_id}/scenes/{scene_id}/voice-jobs")
def drama_scene_voice_jobs(book_id: str, project_id: str, scene_id: str,
                           mode: str = "", db: Session = Depends(get_db)):
    """D19：预览本镜「逐行 TTS 作业」——每句台词会用哪个音色、什么语速音高。

    只读、不合成。用来排查「为什么这句听起来不对」：一眼看到它分到了哪个
    voice、以及这个 voice 是人工指定的还是自动算的。
    """
    p, _b = _drama_ctx(db, book_id, project_id)
    s = db.scalar(select(DramaScene).where(DramaScene.id == scene_id,
                                           DramaScene.project_id == p.id))
    if not s:
        raise HTTPException(404, "scene not found")
    from apps.api import audio_route as _ar
    from apps.api import voice_cast as _vc
    m = _ar.audio_plan_for_project(mode or getattr(p, "content_mode", ""))
    utts = jl(s.utterances, []) or []
    cast = _vc.load_cast_from_db(db, p, book_id)
    jobs = _vc.build_scene_tts_jobs(utts, cast, mode=m)
    # 时间轴（无音频时按字数估算，供排期预览）
    timeline = _vc.build_lipsync_timeline(jobs)
    return {"ok": True, "scene_id": s.id, "mode": m, "jobs": jobs,
            "timeline": timeline,
            "cast_size": len(cast)}


# ----------------------------------------------------------------- 媒体文件
# drama_media/ 等书籍仓库产物走这里静态取出。必须鉴权（媒体是付费生成物，
# 不能像 StaticFiles mount 那样裸奔），路径做白名单清洗防目录穿越。




# ----------------------------------------------------------------- 对话落库（服务端接管）
# v0.13.12 之前：消息只在收到 SSE done 事件后由**前端**调 /conversations/{cid}/messages
# 落库——用户中途刷新/切页，done 永远到不了浏览器，整轮对话（包括跑完的代笔草稿）
# 就凭空消失。现在落库职责收归服务端：user 消息在开跑前立即落，assistant 文本由
# emit 包装器累积、done 时落，与浏览器是否在线无关。

def _save_chat_messages(db, book_id: str, conv_id: str, messages: list[dict]) -> None:
    """把对话消息落库（seq 递增 + 首条用户消息自动做会话标题）。失败静默。

    v0.14.10：消息可携带 task_id —— 记录「这条回复是由哪个后台任务产出的」。
    代笔任务在聊天气泡里落地草稿卡，刷新后要能凭 task_id 把卡重建回来
    （见 GET /conversations/{cid}/messages 的 draft 回填）。
    """
    if not conv_id:
        return
    try:
        conv = db.get(Conversation, conv_id)
        if not conv or conv.book_id != book_id:
            return
        mx = db.scalar(select(func.coalesce(func.max(Message.seq), 0))
                       .where(Message.conv_id == conv_id)) or 0
        saved = 0
        for m in messages:
            role = (m or {}).get("role")
            content = (m or {}).get("content", "")
            if role not in ("user", "assistant") or not content:
                continue
            mx += 1
            saved += 1
            db.add(Message(id=f"mg_{uuid.uuid4().hex[:12]}", conv_id=conv_id,
                           book_id=book_id, role=role, content=content, seq=mx,
                           task_id=(m or {}).get("task_id") or ""))
        conv.updated_at = now()
        db.flush()
        if conv.title in ("", "新对话") and saved:
            first = db.scalar(select(Message).where(
                Message.conv_id == conv_id, Message.role == "user")
                .order_by(Message.seq).limit(1))
            if first:
                txt = (first.content or "").strip().replace("\n", " ")
                conv.title = (txt[:24] + ("…" if len(txt) > 24 else "")) or "新对话"
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


@app.post("/api/books/{book_id}/chat")
async def chat(book_id: str, body: ChatIn):
    """v0.13.13：聊天流改造为任务台账模式。

    之前：内联 StreamingResponse，回复只随 SSE 流吐给浏览器——刷新即断流，
    "正在生成中"的回复丢失（v0.13.12 只保住了跑完的终稿，没有过程）。
    现在：POST 立即建台账 Task(kind=chat) 并返回 task_id，后台协程照常跑
    agent.chat_stream，每个事件节流后追加进 Task.events_json；前端与刷新恢复
    统一从 /tasks/{task_id}/stream 消费（since 增量重放），打字流可无缝续上。
    """
    db = SessionLocal()
    try:
        b = db.get(Book, book_id)
        if not b:
            raise HTTPException(404, "book not found")

        # 会话归位 + user 消息立即落库（沿用 v0.13.12 语义）
        conv_id = (body.conversation_id or "").strip()
        if not conv_id:
            conv = Conversation(id=f"cv_{uuid.uuid4().hex[:12]}", book_id=book_id,
                                title="新对话")
            db.add(conv)
            db.flush()
            conv_id = conv.id
        _set_setting(db, f"conv_active:{book_id}", conv_id)
        db.commit()
        _save_chat_messages(db, book_id, conv_id,
                            [{"role": "user", "content": body.message}])

        t = _new_task(db, book_id, "chat", target=0, progress="正在思考…")
        task_id = t.id
        db.close()

        async def runner():
            db2 = SessionLocal()
            try:
                await _run_chat_flow(db2, book_id, conv_id, body.message,
                                     body.history, task_id)
            except Exception as e:  # 兜底：任务一定收尾，绝不留在 running
                _task_event(db2, _task_row(db2, task_id),
                            {"kind": "error", "text": f"{type(e).__name__}: {e}"},
                            status="error", error=f"{type(e).__name__}: {e}")
                _save_chat_messages(db2, book_id, conv_id,
                                    [{"role": "assistant",
                                      "content": f"⚠️ {type(e).__name__}: {e}"}])
            finally:
                db2.close()

        _spawn(runner(), task_id=task_id, label="chat_flow")
        return {"task_id": task_id, "conversation_id": conv_id, "status": "running"}
    except Exception:
        db.close()
        raise


_CHAT_FLUSH_INTERVAL = 0.6   # token 块节流：0.6s 一块，兼顾打字流畅与 DB 写次数


async def _run_chat_flow(db, book_id: str, conv_id: str, message: str,
                         history: list[dict], task_id: str) -> None:
    """聊天执行体：事件 → 台账（token 节流成块）+ 完成后 assistant 消息落会话。"""
    # token 累积-冲刷器：prompt 字符直接进 Task 会让 events_json 膨胀到
    # 每秒十几条 commit，SQLite WAL 也扛不住一节写作课的写入频率。
    # 攒 0.6s 或 96 字符冲一次，事件块对前端仍是顺序文本，打字效果无损。
    buf = {"text": "", "n": 0}
    last_flush = {"t": time.time()}

    async def flush_token(force: bool = False):
        if not buf["text"]:
            return
        age = time.time() - last_flush["t"]
        if not force and buf["n"] < 96 and age < _CHAT_FLUSH_INTERVAL:
            return
        _task_event(db, _task_row(db, task_id),
                    {"kind": "token", "text": buf["text"]})
        buf["text"] = ""
        buf["n"] = 0
        last_flush["t"] = time.time()

    # 会话落库需要完整正文——token 在 flush 前先全文累积一份
    buf_all = {"text": ""}
    # v0.14.10：记录本轮回复产出的草稿任务 id（可能一个对话里代笔多章）。
    # 落库时写进 assistant 消息，刷新回放时凭它把草稿卡重建回来。
    draft_tid = {"id": ""}

    async def emit(ev: dict):
        k = ev.get("kind")
        if k == "token":
            buf_all["text"] += ev.get("text") or ""
            buf["text"] += ev.get("text") or ""
            buf["n"] += len(ev.get("text") or "")
            await flush_token()
        elif k == "task" and ev.get("task_kind") == "draft" and ev.get("task_id"):
            # 代笔任务在聊天流里挂载：记住它的 id（最后一次为准，多章时以最新稿为主）
            draft_tid["id"] = ev.get("task_id")
            _task_event(db, _task_row(db, task_id), ev)
        elif k == "done":
            await flush_token(force=True)
            _task_event(db, _task_row(db, task_id),
                        {"kind": "done", "cost": ev.get("cost", 0)})
            _save_chat_messages(db, book_id, conv_id,
                                [{"role": "assistant", "content": buf_all["text"],
                                  "task_id": draft_tid["id"]}])
            _task_event(db, _task_row(db, task_id),
                        {"kind": "done", "status": "ok", "progress": "回复完成"},
                        status="done", result={"conversation_id": conv_id})
        else:
            if k in ("warn", "error"):
                await flush_token(force=True)
            ev2 = dict(ev)
            ev2.setdefault("text", ev.get("summary", ""))
            _task_event(db, _task_row(db, task_id), ev2)

    await agent_mod.chat_stream(db, book_id, message, history, emit)


@app.get("/api/books/{book_id}/bible")
def get_bible(book_id: str, db: Session = Depends(get_db)):
    b = db.get(Book, book_id)
    if not b:
        raise HTTPException(404, "book not found")
    meta = agent_mod.get_meta(db, book_id)
    ws = db.scalars(select(WorldSetting).where(WorldSetting.book_id == book_id)).all()
    chars = db.scalars(select(Character).where(Character.book_id == book_id)).all()
    vols = db.scalars(select(Volume).where(Volume.book_id == book_id).order_by(Volume.idx)).all()
    fss = db.scalars(select(Foreshadow).where(Foreshadow.book_id == book_id).order_by(Foreshadow.id)).all()
    chs = db.scalars(select(Chapter).where(Chapter.book_id == book_id).order_by(Chapter.idx)).all()
    return {
        "meta": meta,
        "book": {"title": b.title, "genre": b.genre, "platform": b.platform,
                 "chapter_min": b.chapter_min, "chapter_max": b.chapter_max, "target_words": b.target_words,
                 "theme": getattr(b, "theme", "") or "",
                 "tags": (getattr(b, "tags", "") or "").split(",") if getattr(b, "tags", "") else []},
        "world": [{"category": w.category, "key": w.key, "value": w.value} for w in ws],
        "characters": [{"name": c.name, "role": c.role, "profile": c.profile,
                        "voice": c.voice, "arc": c.arc, "first_chapter": c.first_chapter,
                        "is_auto": int(c.is_auto or 0), "mentions": int(c.mentions or 0),
                        "state_now": c.state_now} for c in chars],
        "volumes": [{"idx": v.idx, "name": v.name, "outline": v.outline,
                     "start_ch": v.start_ch, "end_ch": v.end_ch} for v in vols],
        "foreshadows": [{"content": f.content, "status": f.status, "target_vol": f.target_vol,
                         "planted_ch": f.planted_ch} for f in fss],
        "chapters": [{"idx": c.idx, "title": c.title, "words": c.words,
                      "gate_score": c.gate_score, "status": c.status} for c in chs],
        "progress": {"chapters": len(chs), "words": sum(c.words for c in chs)},
    }


# ----------------------------------------------------------------- 长程记忆（v0.14 D1/D2c）

@app.get("/api/books/{book_id}/memory")
def memory_overview(book_id: str, db: Session = Depends(get_db)):
    """记忆索引概况 + 伏笔债务清单（管理端）。"""
    from apps.api import memory as memory_mod
    chs = db.scalars(select(Chapter.idx).where(Chapter.book_id == book_id)
                     .order_by(Chapter.idx)).all()
    cur_ch = chs[-1] if chs else 0
    return {
        "stats": memory_mod.memory_stats(db, book_id),
        "indexed_to": max(chs) if chs else 0,
        "obligations": consistency.obligations(db, book_id, cur_ch),
    }


@app.post("/api/books/{book_id}/memory/reindex")
def memory_reindex(book_id: str, db: Session = Depends(get_db)):
    """全书重建记忆索引（升级后跑一次；重复执行幂等）。"""
    from apps.api import memory as memory_mod
    n = memory_mod.reindex_book(db, book_id)
    return {"ok": True, "chunks": n}


@app.post("/api/books/{book_id}/memory/search")
def memory_search(book_id: str, body: dict, db: Session = Depends(get_db)):
    """语义检索调试端点：看看当前 query 能回捞到什么（前端「记忆探针」用）。"""
    from apps.api import memory as memory_mod
    q = str(body.get("query") or "")
    ch_idx = int(body.get("ch_idx") or 10 ** 9)
    hits = memory_mod.search(db, book_id, q, ch_idx=ch_idx)
    return {"items": hits}


@app.post("/api/books/{book_id}/memory/rebuild-rolling")
def memory_rebuild_rolling(book_id: str, db: Session = Depends(get_db)):
    """全书滚动摘要重建：逐章摘要链顺序压缩（不调模型，机械拼接最新版）。"""
    b = db.get(Book, book_id)
    if not b:
        raise HTTPException(404, "book not found")
    chs = db.scalars(select(Chapter).where(Chapter.book_id == book_id)
                     .order_by(Chapter.idx)).all()
    parts = []
    for c in chs:
        s = (c.summary or "").strip()
        if s:
            parts.append(f"第{c.idx:03d}章：{s}")
    # 保留最近 30 章的详细摘要 + 之前只留每章首句，控制在预算内
    if len(parts) > 30:
        head = [p.split("：", 1)[1][:60] for p in parts[:-30]]
        tail = parts[-30:]
        text = "早期（各章一句）：\n" + "\n".join(head) + "\n\n近期详情：\n" + "\n".join(tail)
    else:
        text = "\n".join(parts)
    b.rolling_summary = text[:4000]
    db.commit()
    return {"ok": True, "chars": len(b.rolling_summary)}


# ----------------------------------------------------------------- 对话历史（持久化）
# 原先聊天记录只活在浏览器内存里，刷新页面 / 切换书籍就全没了，
# 用户每次都得重新交代背景。这里把对话按「会话线程」落库，
# 刷新后能完整恢复上下文，一本书还能开多条独立对话（立项 / 大纲 / 角色深挖…）。


class ConversationIn(BaseModel):
    title: str = ""


class MessagesIn(BaseModel):
    messages: list[dict] = []


@app.get("/api/books/{book_id}/conversations")
def list_conversations(book_id: str, db: Session = Depends(get_db)):
    rows = db.scalars(select(Conversation).where(Conversation.book_id == book_id)
                      .order_by(Conversation.updated_at.desc())).all()
    counts = {r[0]: r[1] for r in db.execute(
        select(Message.conv_id, func.count(Message.id))
        .where(Message.book_id == book_id).group_by(Message.conv_id)
    ).all()}
    # active 指针存在 Setting 里（conv_active:{book_id}）；没有则默认取最新一条。
    active_row = db.get(Setting, f"conv_active:{book_id}")
    active = active_row.value if active_row else (rows[0].id if rows else None)
    return {
        "conversations": [{
            "id": c.id, "title": c.title,
            "created_at": c.created_at.strftime("%Y-%m-%d %H:%M"),
            "updated_at": c.updated_at.strftime("%Y-%m-%d %H:%M"),
            "count": int(counts.get(c.id, 0)),
        } for c in rows],
        "active": active,
    }


@app.post("/api/books/{book_id}/conversations")
def create_conversation(book_id: str, body: ConversationIn, db: Session = Depends(get_db)):
    c = Conversation(id=f"cv_{uuid.uuid4().hex[:12]}", book_id=book_id,
                     title=body.title or "新对话")
    db.add(c)
    _set_setting(db, f"conv_active:{book_id}", c.id)
    db.commit()
    return {"id": c.id}


@app.get("/api/books/{book_id}/conversations/{cid}/messages")
def get_messages(book_id: str, cid: str, db: Session = Depends(get_db)):
    conv = db.get(Conversation, cid)
    if not conv or conv.book_id != book_id:
        raise HTTPException(404, "conversation not found")
    rows = db.scalars(select(Message).where(
        Message.conv_id == cid, Message.book_id == book_id).order_by(Message.seq)).all()
    # 打开某条会话即把它设为 active，刷新后停在同一处。
    _set_setting(db, f"conv_active:{book_id}", cid)
    db.commit()

    # v0.14.10（bug 实录：系统说"已经写了"，但正文里看不到草稿）：
    # 代笔草稿只存在于实时流的 DOM 里，刷新/切对话后草稿卡（采纳/重写/编辑）
    # 就永远回不来了，用户只能去"正文"tab 的待审区里翻。这里在回放消息时，
    # 顺带把每条 assistant 消息关联的**未消费**草稿一并返回，前端据此重建卡片，
    # 让"刷新前/刷新后"看到的完全一致。
    draft_tids = [m.task_id for m in rows if m.role == "assistant" and m.task_id]
    drafts: dict[str, dict] = {}
    if draft_tids:
        for t in db.scalars(select(Task).where(Task.id.in_(draft_tids))).all():
            r = jl(t.result_json, {}) or {}
            text = r.get("content")
            # 已采纳 / 已丢弃 / 无正文 的稿子不再重建（与 /draft 端点语义一致）
            if not text or r.get("accepted") or r.get("discarded"):
                continue
            drafts[t.id] = {
                "ch_idx": int(r.get("ch_idx") or 0),
                "title": r.get("title") or "",
                "text": text,
                "gate_score": r.get("gate_score"),
                "gate_report": r.get("gate_report"),
                "task_id": t.id,
            }
    return {"messages": [
        {"role": m.role, "content": m.content,
         "task_id": m.task_id or "",
         "draft": drafts.get(m.task_id),
         "created_at": m.created_at.strftime("%Y-%m-%d %H:%M")}
        for m in rows]}


@app.post("/api/books/{book_id}/conversations/{cid}/messages")
def append_messages(book_id: str, cid: str, body: MessagesIn, db: Session = Depends(get_db)):
    conv = db.get(Conversation, cid)
    if not conv or conv.book_id != book_id:
        raise HTTPException(404, "conversation not found")
    mx = db.scalar(select(func.coalesce(func.max(Message.seq), 0))
                   .where(Message.conv_id == cid)) or 0
    saved = 0
    for m in body.messages:
        role = (m or {}).get("role")
        content = (m or {}).get("content", "")
        if role not in ("user", "assistant") or not content:
            continue
        mx += 1
        saved += 1
        db.add(Message(id=f"mg_{uuid.uuid4().hex[:12]}", conv_id=cid, book_id=book_id,
                       role=role, content=content, seq=mx,
                       task_id=(m or {}).get("task_id") or ""))
    conv.updated_at = now()
    # 首条用户消息自动成为会话标题（截断到 24 字），省得每次手动命名。
    # 注意：本服务 SessionLocal(autoflush=False)，上面 add 的消息还没落库，
    # 必须先显式 flush 才能让下面的标题查询看到它们，否则标题永远停在「新对话」。
    db.flush()
    if conv.title in ("", "新对话") and saved:
        first = db.scalar(select(Message).where(
            Message.conv_id == cid, Message.role == "user").order_by(Message.seq).limit(1))
        if first:
            txt = (first.content or "").strip().replace("\n", " ")
            conv.title = (txt[:24] + ("…" if len(txt) > 24 else "")) or "新对话"
    db.commit()
    return {"ok": True, "count": mx}


@app.delete("/api/books/{book_id}/conversations/{cid}")
def delete_conversation(book_id: str, cid: str, db: Session = Depends(get_db)):
    conv = db.get(Conversation, cid)
    if not conv or conv.book_id != book_id:
        raise HTTPException(404, "conversation not found")
    for m in db.scalars(select(Message).where(Message.conv_id == cid)).all():
        db.delete(m)
    db.delete(conv)
    # 删掉的是 active 会话时，把指针挪到剩下的最新一条，避免下次打开落到空指针。
    # 必须排除正在删除的这条，否则 updated_at 最新的恰恰是它本身，指针会指向已删会话。
    row = db.get(Setting, f"conv_active:{book_id}")
    if row and row.value == cid:
        nxt = db.scalar(select(Conversation.id).where(
            Conversation.book_id == book_id, Conversation.id != cid)
            .order_by(Conversation.updated_at.desc()).limit(1))
        if nxt:
            row.value = nxt
        else:
            db.delete(row)
    db.commit()
    return {"ok": True}


# ----------------------------------------------------------------- 纪年表（时间线）


class TimelineIn(BaseModel):
    title: str
    ch_idx: int = 0
    era: str = ""
    time_label: str = ""
    detail: str = ""
    kind: str = "事件"


@app.get("/api/books/{book_id}/timeline")
def get_timeline(book_id: str, db: Session = Depends(get_db)):
    return {"items": consistency.timeline_summary(db, book_id, 200)}


@app.post("/api/books/{book_id}/timeline")
def add_timeline(book_id: str, body: TimelineIn, db: Session = Depends(get_db)):
    row = consistency.add_event(db, book_id, title=body.title, ch_idx=body.ch_idx,
                                era=body.era, time_label=body.time_label,
                                detail=body.detail, kind=body.kind)
    return {"ok": True, "id": row.id, "seq": row.seq, "sort_key": row.sort_key}


@app.delete("/api/books/{book_id}/timeline/{tid}")
def del_timeline(book_id: str, tid: str, db: Session = Depends(get_db)):
    row = db.get(TimelineEvent, tid)
    if not row or row.book_id != book_id:
        raise HTTPException(404, "event not found")
    db.delete(row)
    db.commit()
    return {"ok": True}


# ----------------------------------------------------------------- 度量衡（数值账本）


class LedgerIn(BaseModel):
    subject: str
    metric: str = "其他"
    delta: float = 0
    ch_idx: int = 0
    unit: str = ""
    reason: str = ""


@app.get("/api/books/{book_id}/ledger")
def get_ledger(book_id: str, db: Session = Depends(get_db)):
    return {"items": consistency.ledger_summary(db, book_id, 300),
            "balances": consistency.ledger_balances(db, book_id),
            "metrics": list(consistency.METRICS.keys())}


@app.post("/api/books/{book_id}/ledger")
def add_ledger(book_id: str, body: LedgerIn, db: Session = Depends(get_db)):
    row = consistency.add_ledger(db, book_id, subject=body.subject, metric=body.metric,
                                 delta=body.delta, ch_idx=body.ch_idx,
                                 unit=body.unit, reason=body.reason)
    return {"ok": True, "id": row.id, "balance": row.balance}


@app.delete("/api/books/{book_id}/ledger/{lid}")
def del_ledger(book_id: str, lid: str, db: Session = Depends(get_db)):
    row = db.get(LedgerEntry, lid)
    if not row or row.book_id != book_id:
        raise HTTPException(404, "entry not found")
    db.delete(row)
    db.commit()
    # 删除后重算该主体该维度的后续余额，避免留下断层
    rows = db.scalars(select(LedgerEntry).where(
        LedgerEntry.book_id == book_id,
        LedgerEntry.subject == row.subject,
        LedgerEntry.metric == row.metric,
    ).order_by(LedgerEntry.seq)).all()
    bal = 0.0
    for r in rows:
        bal = round(bal + float(r.delta), 4)
        r.balance = bal
    db.commit()
    return {"ok": True, "recomputed": len(rows)}


# ----------------------------------------------------------------- 一致性检查


@app.get("/api/books/{book_id}/consistency")
def get_consistency(book_id: str, ch_idx: int | None = None, db: Session = Depends(get_db)):
    return consistency.run_consistency(db, book_id, ch_idx)


# ----------------------------------------------------------------- 内容审计


class AuditIn(BaseModel):
    ch_idx: int = 0
    scope: str = "chapter"     # chapter | book


@app.post("/api/books/{book_id}/audit")
async def run_audit(book_id: str, body: AuditIn, db: Session = Depends(get_db)):
    b = db.get(Book, book_id)
    if not b:
        raise HTTPException(404, "book not found")
    if body.scope == "book":
        reps = []
        chs = db.scalars(select(Chapter).where(
            Chapter.book_id == book_id).order_by(Chapter.idx)).all()
        cap_note = ""
        if len(chs) > _AUDIT_BOOK_CAP:
            chs = chs[:_AUDIT_BOOK_CAP]
            cap_note = f"（章节超过 {_AUDIT_BOOK_CAP}，本次仅审计前 {_AUDIT_BOOK_CAP} 章）"
        for c in chs:     # 全书逐章审计（带安全上限），不再静默只跑最近 20 章
            r = await audit_mod.audit_chapter(db, b, c.idx)
            if not r.get("error"):
                audit_mod.save_report(db, book_id, r, ch_idx=c.idx, scope="book")
                reps.append({"ch_idx": c.idx, "score": r.get("score", 0),
                             "summary": r.get("summary", "")})
        score = int(round(sum(x["score"] for x in reps) / len(reps))) if reps else 0
        return {"scope": "book", "score": score, "chapters": reps,
                "total": len(reps), "cap_note": cap_note}
    rep = await audit_mod.audit_chapter(db, b, body.ch_idx)
    if rep.get("error"):
        raise HTTPException(404, rep["error"])
    audit_mod.save_report(db, book_id, rep, ch_idx=body.ch_idx)
    return rep


@app.get("/api/books/{book_id}/audits")
def get_audits(book_id: str, limit: int = 20, db: Session = Depends(get_db)):
    return {"items": audit_mod.list_reports(db, book_id, limit)}


@app.get("/api/books/{book_id}/audits/{aid}")
def get_audit(book_id: str, aid: str, db: Session = Depends(get_db)):
    r = db.get(AuditReport, aid)
    if not r or r.book_id != book_id:
        raise HTTPException(404, "report not found")
    return {"id": r.id, "ch_idx": r.ch_idx, "score": r.score,
            "summary": r.summary, "report": jl(r.report, {})}


@app.get("/api/books/{book_id}/signing-report")
def get_signing_report(book_id: str, db: Session = Depends(get_db)):
    from apps.api import platform as platform_mod
    b = db.get(Book, book_id)
    if not b:
        raise HTTPException(404, "book not found")
    return platform_mod.signing_report(db, b)


# ----------------------------------------------------------------- 人物自动入册


class CharSyncIn(BaseModel):
    ch_idx: int = 0
    min_mentions: int = 3
    text: str = ""


@app.post("/api/books/{book_id}/characters/sync")
def sync_characters(book_id: str, body: CharSyncIn, db: Session = Depends(get_db)):
    text = body.text
    if not text and body.ch_idx:
        c = db.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.idx == body.ch_idx))
        text = (c.content if c else "") or ""
    if not text:
        raise HTTPException(400, "缺少待扫描正文")
    res = char_mod.sync_characters(db, book_id, text, body.ch_idx, body.min_mentions)
    return {"ok": True, **res}


@app.get("/api/books/{book_id}/characters")
def list_characters(book_id: str, db: Session = Depends(get_db)):
    return {"items": char_mod.review_queue(db, book_id)}


@app.get("/api/books/{book_id}/runs")
def list_runs(book_id: str, limit: int = 20, db: Session = Depends(get_db)):
    rows = db.scalars(select(Run).where(Run.book_id == book_id).order_by(Run.created_at.desc()).limit(limit)).all()
    return [{"id": r.id, "kind": r.kind, "model": r.model_id, "cost": r.cost,
             "gate_score": r.gate_score, "status": r.status, "duration_ms": r.duration_ms,
             "time": r.created_at.strftime("%m-%d %H:%M")} for r in rows]


# ----------------------------------------------------------------- 导出


# ---- 整书打包导出 / 导入（v0.19.3 F2）----
@app.get("/api/books/{book_id}/pack")
def book_pack_export(book_id: str, drama: int = 1, files: int = 1,
                     db: Session = Depends(get_db)):
    """导出整书为 .nfbook（ZIP）。

    与 /export 的区别：那个是**给人读的成品**（txt/md/epub）；
    这个是**机器可还原的完整快照** —— 含设定/大纲/人物/伏笔/纪年表/账本/
    短剧工程/原始文件，可用于跨实例迁移或时间点备份。

    ★ 安全：**不含** users / model_profiles / settings（含账号与 API Key）。
    """
    from urllib.parse import quote
    from fastapi.responses import Response as _Resp
    from apps.api import book_pack
    b = db.get(Book, book_id)
    if not b:
        raise HTTPException(404, "book not found")
    data, fname = book_pack.export_book(db, b, include_drama=bool(drama),
                                        include_files=bool(files))
    return _Resp(content=data, media_type="application/zip",
                 headers={"Content-Disposition":
                          f"attachment; filename*=UTF-8''{quote(fname)}",
                          "X-Pack-Size": str(len(data))})


@app.post("/api/books/import-pack")
async def book_pack_import(mode: str = "copy", file: UploadFile = File(...),
                           db: Session = Depends(get_db)):
    """导入 .nfbook。

    mode=copy（默认）→ 新 id，不覆盖现有书；
    mode=merge        → 沿用原 id（用于"恢复到同一位置"）。

    导入前会先校验包格式与版本，不合法直接拒绝（不写库）。
    """
    from apps.api import book_pack
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "空文件")
    if len(raw) > 512 * 1024 * 1024:
        raise HTTPException(413, "文件过大（>512MB）")
    r = book_pack.import_book(db, raw, mode=("merge" if mode == "merge" else "copy"))
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "导入失败"))
    return r


@app.post("/api/books/inspect-pack")
async def book_pack_inspect(file: UploadFile = File(...)):
    """只读校验 .nfbook（导入前预检，不解压落库）。"""
    from apps.api import book_pack
    raw = await file.read()
    return book_pack.inspect_package(raw)


@app.get("/api/books/{book_id}/export")
def export_book(book_id: str, fmt: str = "txt", db: Session = Depends(get_db)):
    from urllib.parse import quote
    b = db.get(Book, book_id)
    if not b:
        raise HTTPException(404, "book not found")
    chs = db.scalars(select(Chapter).where(Chapter.book_id == book_id).order_by(Chapter.idx)).all()
    meta = agent_mod.get_meta(db, book_id)
    bible = None
    if (fmt or "txt").lower() in ("publish", "submission"):
        bible = {
            "world": [{"category": w.category, "key": w.key, "value": w.value}
                      for w in db.scalars(select(WorldSetting).where(WorldSetting.book_id == book_id)).all()],
            "characters": [{"name": c.name, "role": c.role, "profile": c.profile}
                           for c in db.scalars(select(Character).where(Character.book_id == book_id)).all()],
            "volumes": [{"idx": v.idx, "name": v.name, "outline": v.outline}
                        for v in db.scalars(select(Volume).where(Volume.book_id == book_id)).all()],
            "foreshadows": [{"content": f.content, "status": f.status}
                            for f in db.scalars(select(Foreshadow).where(Foreshadow.book_id == book_id)).all()],
        }
        try:
            from apps.api import platform as platform_mod
            bible["signing"] = platform_mod.signing_report(db, b)
        except Exception:
            bible["signing"] = None
    try:
        data, mime, fname = exporter.build(b, chs, fmt, meta=meta, bible=bible)
    except ValueError as e:
        # 格式名拼错时明确告知可用值，别静默给一份别的格式
        raise HTTPException(400, str(e))
    ascii_name = f"novelforge.{fmt}"
    return Response(content=data, media_type=mime,
                    headers={"Content-Disposition":
                              f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(fname)}"})


# ----------------------------------------------------------------- 章节版本 / 回滚


@app.get("/api/books/{book_id}/chapters/{idx}/history")
def chapter_history(book_id: str, idx: int):
    rel = f"正文/第{idx:03d}章.md"
    return gitrepo.log(book_id, rel)


@app.get("/api/books/{book_id}/chapters/{idx}/at/{commit}")
def chapter_at(book_id: str, idx: int, commit: str):
    rel = f"正文/第{idx:03d}章.md"
    content = gitrepo.show(book_id, rel, commit)
    if not content:
        raise HTTPException(404, "找不到该版本")
    lines = content.splitlines()
    title = ""
    body = content
    if lines and lines[0].lstrip().startswith("#"):
        h = lines[0].lstrip("# ").strip()
        tm = re.match(r"第\s*\d+\s*章\s*(.*)", h)
        title = tm.group(1).strip() if tm else h
        body = "\n".join(lines[1:]).strip()
    return {"idx": idx, "title": title, "content": body}


class RollbackIn(BaseModel):
    """Git 回滚入参。

    这个类原来定义在文件末尾（第 1063 行），而 chapter_restore 在第 938 行就引用它。
    因为文件头有 `from __future__ import annotations`，注解变成字符串不会在导入时炸，
    但 FastAPI 注册路由时解析不到这个类型，就把它当成了**普通查询参数**——
    实际请求会收到 422 `loc:["query","body"]`。移到使用点之前，路由才能正确按 JSON body 解析。
    """
    rel: str
    commit: str


# ---- 改稿对比（v0.19 F6）----
@app.get("/api/books/{book_id}/chapters/{idx}/diff")
def chapter_diff(book_id: str, idx: int,
                 from_commit: str = "", to_commit: str = "",
                 summary_only: int = 0):
    """对比某一章的两个版本（默认：上一版 vs 当前版）。

    数据源是章节 git 历史（`gitrepo`）—— 项目已有的版本存储。
    返回行级 + 行内字符级 diff，供前端左右对照渲染。

    * `from_commit` / `to_commit`：不传则自动取「倒数第二个版本 vs 最新版本」
    * `summary_only=1`：只回摘要（轻量，适合先探测有没有改动）
    """
    rel = f"正文/第{idx:03d}章.md"
    hist = gitrepo.log(book_id, rel) or []
    if len(hist) < 2 and not (from_commit and to_commit):
        return {"ok": False, "error": "该章历史版本不足两个，无法对比",
                "versions": len(hist)}

    commits = [h.get("commit") for h in hist if h.get("commit")]
    fc = from_commit or (commits[1] if len(commits) >= 2 else "")
    tc = to_commit or (commits[0] if commits else "")
    if not fc or not tc:
        return {"ok": False, "error": "无法定位对比版本", "versions": len(hist)}
    if fc == tc:
        return {"ok": False, "error": "两个版本相同，无需对比"}

    old = gitrepo.show(book_id, rel, fc) or ""
    new = gitrepo.show(book_id, rel, tc) or ""
    from apps.api import diffutil
    if summary_only:
        return {"ok": True, "from": fc, "to": tc,
                "summary": diffutil.summarize(old, new)}
    r = diffutil.diff_text(old, new)
    r.update({"from": fc, "to": tc, "idx": idx})
    return r


@app.post("/api/books/{book_id}/chapters/{idx}/restore")
def chapter_restore(book_id: str, idx: int, body: RollbackIn, db: Session = Depends(get_db)):
    # 这里原本漏了 db 注入，函数体里 db.scalar(...) 必然 NameError。
    # 回滚是低频功能，所以这个接口其实一次都没成功过。
    rel = f"正文/第{idx:03d}章.md"
    content = gitrepo.show(book_id, rel, body.commit)
    if not content:
        raise HTTPException(400, "找不到该版本")
    # 更新 DB（去掉标题行取正文）
    lines = content.splitlines()
    title = ""
    body_text = content
    if lines and lines[0].lstrip().startswith("#"):
        h = lines[0].lstrip("# ").strip()
        tm = re.match(r"第\s*\d+\s*章\s*(.*)", h)
        title = tm.group(1).strip() if tm else h
        body_text = "\n".join(lines[1:]).strip()
    c = db.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.idx == idx))
    if c:
        c.title = title or c.title
        c.content = body_text
        c.words = gates.count_words(body_text)
        db.commit()
    # 产生一条新 commit（不篡改历史）
    gitrepo.write_file(book_id, rel, content, commit_msg=f"回滚第{idx:03d}章 -> {body.commit[:8]}")
    return {"ok": True, "title": title, "words": gates.count_words(body_text)}


# ----------------------------------------------------------------- 伏笔 / 人物 编辑（支持用户自行修改）


@app.delete("/api/books/{book_id}")
def delete_book(book_id: str, db: Session = Depends(get_db)):
    """仪表盘删除项目：同时清理 DB 记录与 Git 仓库。"""
    b = db.get(Book, book_id)
    if not b:
        raise HTTPException(404, "book not found")
    # 级联清理必须覆盖全部子表。v0.6 新增的纪年表/账本/审计留档也曾漏在这里，
    # 删书后会留下查不到的孤儿行，且 book_id 索引会越攒越脏。
    for model in (Chapter, Character, Foreshadow, WorldSetting, Volume, StyleProfile, Run,
                  TimelineEvent, LedgerEntry, AuditReport, Conversation, Message, Task):
        for row in db.scalars(select(model).where(model.book_id == book_id)).all():
            db.delete(row)
    for k in (f"book:{book_id}:project_meta", f"conv_active:{book_id}"):
        row = db.get(Setting, k)
        if row:
            db.delete(row)
    db.delete(b)
    db.commit()
    import shutil
    repo = config.BOOKS_DIR / book_id
    if repo.exists():
        shutil.rmtree(repo, ignore_errors=True)
    return {"ok": True}


class ForeshadowIn(BaseModel):
    content: str = ""
    status: str = ""
    note: str = ""
    payoff_ch: int | None = None


@app.post("/api/books/{book_id}/foreshadows")
def add_foreshadow(book_id: str, body: ForeshadowIn, db: Session = Depends(get_db)):
    if not db.get(Book, book_id):
        raise HTTPException(404, "book not found")
    f = Foreshadow(id=f"fs_{uuid.uuid4().hex[:12]}", book_id=book_id,
                   content=body.content, status=body.status or "open",
                   note=body.note, payoff_ch=body.payoff_ch or 0)
    db.add(f)
    db.commit()
    return {"id": f.id}


@app.patch("/api/books/{book_id}/foreshadows/{fid}")
def patch_foreshadow(book_id: str, fid: str, body: ForeshadowIn, db: Session = Depends(get_db)):
    f = db.get(Foreshadow, fid)
    if not f or f.book_id != book_id:
        raise HTTPException(404, "foreshadow not found")
    for k in ("content", "status", "note"):
        v = getattr(body, k)
        if v != "" or k in body.model_fields_set:
            setattr(f, k, v)
    if body.payoff_ch is not None:
        f.payoff_ch = body.payoff_ch
    db.commit()
    return {"ok": True}


@app.delete("/api/books/{book_id}/foreshadows/{fid}")
def del_foreshadow(book_id: str, fid: str, db: Session = Depends(get_db)):
    f = db.get(Foreshadow, fid)
    if f and f.book_id == book_id:
        db.delete(f)
        db.commit()
    return {"ok": True}


class CharacterIn(BaseModel):
    role: str | None = None
    profile: str | None = None
    voice: str | None = None
    arc: str | None = None
    state_now: str | None = None


@app.patch("/api/books/{book_id}/characters/{cid}")
def patch_character(book_id: str, cid: str, body: CharacterIn, db: Session = Depends(get_db)):
    c = db.get(Character, cid)
    if not c or c.book_id != book_id:
        raise HTTPException(404, "character not found")
    for k in ("role", "profile", "voice", "arc", "state_now"):
        v = getattr(body, k)
        if v is not None:
            setattr(c, k, v)
    db.commit()
    return {"ok": True}


@app.delete("/api/books/{book_id}/characters/{cid}")
def delete_character(book_id: str, cid: str, db: Session = Depends(get_db)):
    """★ #311 修复：删除人物。曾缺此端点，前端只能「误入库·待删除」口头标记、永不真删。

    连带清理「按 character_id / character_name 关联」的衍生表（角色知识 / 关系图），
    避免留下孤儿引用被一致性检查误报。
    """
    c = db.get(Character, cid)
    if not c or c.book_id != book_id:
        raise HTTPException(404, "character not found")
    name = c.name
    # 衍生表清理（与 save_character 改名合并时的清理口径一致）
    for kn in db.scalars(select(CharacterKnowledge).where(
            CharacterKnowledge.book_id == book_id,
            (CharacterKnowledge.character_id == cid)
            | (CharacterKnowledge.character_name == name))).all():
        db.delete(kn)
    for rel in db.scalars(select(CharacterRelation).where(
            CharacterRelation.book_id == book_id)).all():
        if rel.from_id == cid or rel.to_id == cid:
            db.delete(rel)
    db.delete(c)
    db.commit()
    return {"ok": True, "name": name}


# ----------------------------------------------------------------- 版本


@app.get("/api/books/{book_id}/git/log")
def git_log(book_id: str, rel: str | None = None):
    return gitrepo.log(book_id, rel)


# RollbackIn 已上移到 chapter_restore 之前（见该处的说明注释）。

class ReloadIn(BaseModel):
    idx: int
    commit: str


@app.post("/api/books/{book_id}/git/rollback-file")
def git_rollback_file(book_id: str, body: RollbackIn, db: Session = Depends(get_db)):
    ok = gitrepo.rollback(book_id, body.rel, body.commit)
    if not ok:
        raise HTTPException(400, "回滚失败：找不到该版本")
    content = (config.BOOKS_DIR / book_id / body.rel).read_text(encoding="utf-8")
    m = __import__("re").match(r"#\s*第\s*(\d+)\s*章\s*(.*)", content.splitlines()[0] if content else "")
    idx = int(m.group(1)) if m else 0
    title = m.group(2).strip() if m else ""
    c = db.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.idx == idx))
    if c:
        c.title = title
        c.content = "\n".join(content.splitlines()[1:]).strip()
        c.words = gates.count_words(c.content)
        db.commit()
    return {"ok": True, "idx": idx}


# ----------------------------------------------------------------- 配置中心
























# ----------------------------------------------------------------- 路由装配（v0.18 T1）
# 拆分出来的 APIRouter 在这里挂载；同时 **re-export 端点函数名**，
# 保证 `from apps.api.main import list_genres` 与 `main.list_genres(...)`
# 的老写法继续可用（项目测试大量依赖此点，见 tests/test_v150_genre_api.py 等）。
from apps.api.routers import genres as _routers_genres
from apps.api.routers import system as _routers_system

app.include_router(_routers_genres.router)
app.include_router(_routers_system.router)
# v0.20.4 A4：用户中心
from apps.api.routers import me as _routers_me  # noqa: E402
app.include_router(_routers_me.router)
# v0.20.5 A5：管理后台（独立 /admin SPA + /api/admin/* 端点）
from apps.api.routers import admin as _routers_admin  # noqa: E402
app.include_router(_routers_admin.router)
# v0.20.7 A7：RBAC 权限体系
from apps.api.routers import rbac_routes as _routers_rbac  # noqa: E402
app.include_router(_routers_rbac.router)
# v0.21 B 组：一致性体系（向量检索/关系图谱/情绪弧线/伏笔/角色资产/信息差）
from apps.api.routers import consistency as _routers_consistency  # noqa: E402
app.include_router(_routers_consistency.router)
# v0.22 C 组：创作工作流（三级大纲/场景层/去 AI 腔/片段重拍）
from apps.api.routers import workflow_routes as _routers_workflow  # noqa: E402
app.include_router(_routers_workflow.router)

# v0.23 D 组：成本路由 / 成本看板 / 性能审计
from apps.api.routers import costing_routes as _routers_costing  # noqa: E402
app.include_router(_routers_costing.router)

# v0.24 F 组：用户反馈 / 遥测 / 数据洞察
from apps.api.routers import feedback_routes as _routers_feedback  # noqa: E402
app.include_router(_routers_feedback.router)

# v0.25 Xf 组：通知中心与邮件
from apps.api.routers import notify_routes as _routers_notify  # noqa: E402
app.include_router(_routers_notify.router)

# v0.25 J0 组：队列健康与降级重建
from apps.api.routers import queue_routes as _routers_queue  # noqa: E402
app.include_router(_routers_queue.router)


# v0.25.4 I 组：配置中心
from apps.api.routers import config_routes as _routers_config  # noqa: E402
app.include_router(_routers_config.router)



# v0.26.1/2：广告位 + 模型多供应商配置 + 前台站点信息
from apps.api.routers import ads_models as _routers_ads_models  # noqa: E402
app.include_router(_routers_ads_models.router)


# re-export（不参与运行，仅为保持 main 命名空间兼容）
from apps.api.routers.genres import (  # noqa: E402,F401
    list_genres, list_genres_public, create_genre, update_genre,
    delete_genre, reload_genres, export_genres, import_genres, GenrePackIn,
)
from apps.api.routers.system import (  # noqa: E402,F401
    health, register, login, me, logout, allow_register, dashboard,
    admin_list_users, admin_create_user, admin_patch_user, admin_delete_user,
    serve_book_file, list_models, create_model, update_model, delete_model,
    ping_model, list_skills, get_skill, save_skill, reset_skill,
    get_settings, set_settings, RegisterIn, LoginIn, ModelIn, SkillPatch,
)

# ----------------------------------------------------------------- 静态前端（v0.27.0 主域名切换）

# ★★★ v0.27.0：新版升主域名，旧版降级到 /legacy/
#
#   变更前（v0.25.9 ~ v0.26.3 双轨并行）：
#     `/`              旧版前台（单体 app.js）
#     `/app2/`         新版前台（Vue 3）
#     `/admin`         旧版后台（原生 JS）
#     `/app2/admin`    新版后台（Vue 3）
#
#   变更后（本版）：
#     `/`              新版前台 ★
#     `/admin`         新版后台 ★
#     `/legacy/`       旧版前台（回滚用，noindex）
#     `/legacy/admin/` 旧版后台（回滚用，noindex）
#     `/app2/*`        301 重定向到新路径（老书签/老分享链接不断）
#
#   ★★ 回滚成本极低：本版**未删除**旧版任何代码（apps/web/ 原样保留），
#      回滚 = 用 deploy/deploy_source.py --rollback 回上一版本产物。
#      稳定运行 1~2 周后可再单独提交删除旧版（计划书 §8.0 铁律 R-D）。


# ★ 路径解析：main.py 在 apps/api/ 下
#   → 新版构建产物在 apps/api/static/app2（与 api 同目录，便于随 apps/ 一起部署）
#   ★ 目录名仍叫 app2 而非 web：改名会牵动部署脚本、测试、CI 多处引用，
#     收益（命名好看）远小于风险（漏改一处 → 静默 503/白屏）。命名历史包袱可接受。
NEW_DIR = Path(__file__).resolve().parents[1] / "api" / "static" / "app2"
APP2_DIR = NEW_DIR                       # 兼容别名（旧引用点仍可用）

# 旧版目录（保持原样，仅换挂载前缀）
LEGACY_DIR = WEB_DIR
ADMIN_DIR = WEB_DIR / "admin"

_log = logging.getLogger("novelforge")


def _is_hashed_asset(path: str) -> bool:
    """★ 判断是否为构建产物（文件名带内容 hash）→ 可长缓存。

    ★ 用 hash 判断而非扩展名：只有构建产物才带 hash，
      改版后文件名变化自动失效，不存在"旧文件配新 DOM"的问题。
    """
    return bool(re.search(
        r"-[A-Za-z0-9_]{8,}\.(js|css|woff2?|png|svg|jpg|jpeg|gif|webp|ico)$",
        path.split("?")[0]))


# ★★ v1.0 E3 开源准备：L2 埋点 ③ —— HTML 注释构建指纹
#
#   为什么嵌在 HTML 注释里：这是"多位置冗余埋点"的一环。
#   想彻底清除必须在**每个 HTML 响应路径**上做后处理（而本项目有 /、/admin、
#   /legacy 三个入口 + SPA fallback），成本明显高于买份授权。
#
#   ★ 为什么不能直接改静态文件：静态产物是构建出来的，每次构建都会覆盖 ——
#     运行时注入才是稳定的（也让"本地 dev 构建"与"官方发布"天然可区分）。
_FP_COMMENT = ""


def _fingerprint_comment() -> str:
    """生成 HTML 指纹注释（进程内缓存，避免每请求拼字符串）。"""
    global _FP_COMMENT
    if _FP_COMMENT:
        return _FP_COMMENT
    try:
        from apps.api import build_info as _bi
        info = _bi.build_info()
        # ★ 不用 "<!-- ... -->" 里的双横线（HTML 注释内不允许，会破坏解析）
        _FP_COMMENT = (
            f"<!-- {_bi.PROJECT} build {info['build_id']} "
            f"v{info['version']} {info['license']} | {_bi.HOMEPAGE} -->\n"
        )
    except Exception:                                   # noqa: BLE001
        _FP_COMMENT = ""
    return _FP_COMMENT


def _spa_file_response(base_dir: Path, rel: str, *, prefix: str,
                       noindex: bool = False):
    """★ 通用 SPA 静态服务：命中真实文件则返回，否则回退 index.html。

    ★★ 为什么不用 `app.mount(prefix, StaticFiles(...))`：
      StaticFiles **不做 SPA fallback**（history 模式刷新 `/books/abc` 必然 404），
      而且它无法对"带 hash 资源长缓存 / index.html 不缓存"做区分。
      统一走显式路由，缓存策略完全可控。

    ★ 两层目录穿越防护：字符检查 + 解析后路径必须仍在 base 内（防符号链接）。
    """
    from fastapi.responses import FileResponse

    rel = (rel or "").replace("\\", "/").lstrip("/")
    # 第一层：字符检查
    if ".." in rel.split("/"):
        raise HTTPException(400, "非法路径")

    base = base_dir.resolve()
    candidate = base_dir / rel if rel else base_dir / "index.html"

    # 第二层：解析后必须仍在 base 内
    try:
        real = candidate.resolve()
        if not (real == base or str(real).startswith(str(base) + os.sep)):
            _log.warning("★ %s 路径越界拦截：%s", prefix, rel)
            raise HTTPException(403, "非法路径")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "非法路径")

    if candidate.is_file() and candidate.name != "index.html":
        cache = ("public, max-age=31536000, immutable"
                 if _is_hashed_asset(candidate.name) else "public, max-age=3600")
        return FileResponse(str(candidate), headers={"Cache-Control": cache})

    # 未命中 → index.html（SPA 路由）
    idx = base_dir / "index.html"
    if not idx.is_file():
        return JSONResponse(
            {"detail": f"前端未构建（{base_dir} 下缺少 index.html）"},
            status_code=503)
    # ★ index.html 绝不缓存：否则发版后用户仍引用旧 hash 资源 → 白屏
    headers = {"Cache-Control": "no-cache, must-revalidate"}
    if noindex:
        headers["X-Robots-Tag"] = "noindex, nofollow"

    # ★★ v1.0 E3 开源准备：L2 埋点 ③ —— 在 index.html 注入构建指纹注释
    #
    #   ★ 为什么用 HTMLResponse 而不是 FileResponse：
    #     FileResponse 是流式发送文件的，无法在中间插内容。
    #     index.html 只有 1.3KB，读进内存完全可接受（且它本来就 no-cache，
    #     不存在"每次都要读盘"的性能问题 —— 对比带 hash 的资源仍走 FileResponse）。
    #
    #   ★ 注入失败时回退 FileResponse（绝不因埋点失败导致页面打不开）
    try:
        fp = _fingerprint_comment()
        if fp:
            html = idx.read_text(encoding="utf-8")
            # ★ 插在 <head> 之后：解析器尽早看到，但又不影响 charset 优先识别
            if "<head>" in html:
                html = html.replace("<head>", "<head>\n" + fp, 1)
            else:
                html = fp + html
            from fastapi.responses import HTMLResponse
            return HTMLResponse(html, headers=headers)
    except Exception as _e:                             # noqa: BLE001
        _log.debug("注入构建指纹注释失败（回退原文件）：%s: %s",
                   type(_e).__name__, _e)

    return FileResponse(str(idx), media_type="text/html", headers=headers)


# ========================================================== 新版后台 /admin


@app.get("/admin", include_in_schema=False)
@app.get("/admin/", include_in_schema=False)
def _admin_index():
    """★ v0.27.0：新版管理后台入口（Vue 3，独立会话）。

    ★ 为什么用显式 GET 路由而不是 mount：
      `/` 已由新版前台占用（下面最后注册的 catch-all），而 Starlette 路由是
      **按注册顺序**匹配 —— mount 会吃掉所有未匹配前缀。
      显式 GET 路由能确保 `/admin` 优先命中，且可附加 noindex 安全头。
    """
    from fastapi.responses import FileResponse
    idx = NEW_DIR / "index.html"
    if not idx.is_file():
        return JSONResponse(
            {"detail": "前端未构建（缺少 apps/api/static/app2/index.html）"},
            status_code=503)
    return FileResponse(str(idx), media_type="text/html",
                        headers={"Cache-Control": "no-store",
                                 "X-Robots-Tag": "noindex, nofollow"})


@app.get("/admin/{full_path:path}", include_in_schema=False)
def _admin_spa(full_path: str):
    """★ v0.27.0：新版后台 SPA fallback（`/admin/users` 刷新必须可达）。

    ★ 后台全站 noindex —— 管理界面不应被搜索引擎收录。
    """
    return _spa_file_response(NEW_DIR, full_path, prefix="/admin", noindex=True)


# ====================================================== 旧版（回滚用）/legacy


@app.get("/legacy", include_in_schema=False)
@app.get("/legacy/", include_in_schema=False)
def _legacy_index():
    """★ v0.27.0：旧版前台（单体 app.js）—— **仅供回滚/对照**，noindex。"""
    from fastapi.responses import FileResponse
    idx = LEGACY_DIR / "index.html"
    if not idx.is_file():
        return JSONResponse({"detail": "旧版前端不存在"}, status_code=503)
    return FileResponse(str(idx), media_type="text/html",
                        headers={"Cache-Control": "no-cache, must-revalidate",
                                 "X-Robots-Tag": "noindex, nofollow"})


@app.get("/legacy/admin", include_in_schema=False)
@app.get("/legacy/admin/", include_in_schema=False)
def _legacy_admin_index():
    """★ v0.27.0：旧版后台（原生 JS）—— 仅供回滚。"""
    from fastapi.responses import FileResponse
    idx = ADMIN_DIR / "index.html"
    if not idx.is_file():
        return JSONResponse({"detail": "旧版后台不存在"}, status_code=503)
    return FileResponse(str(idx), media_type="text/html",
                        headers={"Cache-Control": "no-store",
                                 "X-Robots-Tag": "noindex, nofollow"})


@app.get("/legacy/admin/{full_path:path}", include_in_schema=False)
def _legacy_admin_spa(full_path: str):
    """旧版后台静态资源（admin.css / admin.js 等）。"""
    return _spa_file_response(ADMIN_DIR, full_path, prefix="/legacy/admin",
                              noindex=True)


@app.get("/legacy/{full_path:path}", include_in_schema=False)
def _legacy_spa(full_path: str):
    """旧版前台静态资源（app.js / style.css 等）。"""
    return _spa_file_response(LEGACY_DIR, full_path, prefix="/legacy", noindex=True)


# ====================================================== SEO 基础设施（v1.0-E4.11）

# ★★ 为什么必须显式注册：营销页（/intro /pricing /help）是**获客入口**，
#   搜索引擎收录是它的主要流量来源（E4.2 教训：帮助中心曾被登录墙拦掉获客）。
#   但 robots.txt 此前根本不存在 —— 线上实测 /robots.txt 被 catch-all
#   兜成 index.html（Content-Type: text/html）：
#   爬虫按 robots 规范解析 HTML 失败 → **按"全站禁止"或"未定义"处理**，
#   营销页的收录完全靠运气。显式返回纯文本 + text/plain 才是 robots 该有的样子。

_ROBOTS_TXT = """User-agent: *
Allow: /
Disallow: /admin
Disallow: /legacy
Disallow: /api/

# 已知爬虫显式允许（主流搜索引擎都遵守上面的通配规则，这里仅为明确表态）
User-agent: Googlebot
Allow: /

User-agent: Bingbot
Allow: /

# 站点地图（列出营销页与帮助中心）
Sitemap: https://sozeer.com/sitemap.xml
"""


@app.get("/robots.txt", include_in_schema=False)
def _robots_txt():
    return Response(content=_ROBOTS_TXT, media_type="text/plain",
                    headers={"Cache-Control": "public, max-age=86400"})


_SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://sozeer.com/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>
  <url><loc>https://sozeer.com/intro</loc><changefreq>weekly</changefreq><priority>0.9</priority></url>
  <url><loc>https://sozeer.com/tech</loc><changefreq>monthly</changefreq><priority>0.8</priority></url>
  <url><loc>https://sozeer.com/pricing</loc><changefreq>weekly</changefreq><priority>0.9</priority></url>
  <url><loc>https://sozeer.com/help</loc><changefreq>weekly</changefreq><priority>0.8</priority></url>
  <url><loc>https://sozeer.com/about</loc><changefreq>monthly</changefreq><priority>0.6</priority></url>
  <url><loc>https://sozeer.com/login</loc><priority>0.3</priority></url>
  <url><loc>https://sozeer.com/register</loc><priority>0.5</priority></url>
</urlset>
"""


@app.get("/sitemap.xml", include_in_schema=False)
def _sitemap_xml():
    return Response(content=_SITEMAP_XML, media_type="application/xml",
                    headers={"Cache-Control": "public, max-age=86400"})


# ============================================ 老路径兼容：/app2/* → 301 新路径


@app.get("/app2", include_in_schema=False)
@app.get("/app2/", include_in_schema=False)
@app.get("/app2/{full_path:path}", include_in_schema=False)
def _app2_redirect(full_path: str = ""):
    """★★ v0.27.0：老路径 `/app2/*` 永久重定向到新主域名路径。

    ★ 为什么保留而不是直接删：用户书签、已分享链接、外部引用都可能还指向它。
    ★ 为什么用 301 而非 302：路径语义永久改变，让浏览器/搜索引擎更新索引，
      避免每次访问都回源跳转。
    ★ 用相对跳转（Location 以 `/` 开头）而非绝对 URL —— 不依赖请求头 Host
      （Cloudflare 回源时 Host 可能是内网 IP）。
    """
    from fastapi.responses import RedirectResponse
    rel = (full_path or "").replace("\\", "/").lstrip("/")
    target = "/" + rel if rel else "/"
    # 老后台 → 新后台
    if rel == "admin" or rel.startswith("admin/"):
        rest = rel[len("admin"):].lstrip("/")
        target = "/admin" + ("/" + rest if rest else "")
    return RedirectResponse(target, status_code=301)


# ========================================================== 新版前台 `/`（兜底）


@app.get("/{full_path:path}", include_in_schema=False)
def _new_spa(full_path: str):
    """★★ v0.27.0：新版前台（Vue 3）—— 主域名根路径，SPA fallback。

    ★★ 注册顺序铁律（MEMORY §3）：本路由是**最后**注册的 catch-all，
      必须在 `/api/*`、`/admin`、`/legacy`、`/app2` 全部注册之后。
      否则它会吃掉所有路径（含 API）→ 表现为"接口全部返回 HTML"。

    ★ FastAPI 内置路径（`/openapi.json`、`/docs`、`/redoc`）由框架在
      app 初始化时注册，**先于**本 catch-all，因此能优先命中 ——
      本地实测确认返回 JSON / Swagger UI（无需额外放行）。
      ★ 若线上发现它们返回 HTML，先怀疑**服务未重启到新代码**，而不是路由顺序。

    ★ 未构建时返回 503 而不是 500/白屏 —— 让"部署漏传产物"一眼可见。
    """
    return _spa_file_response(NEW_DIR, full_path, prefix="/")


if NEW_DIR.exists():
    _log.info("★ v0.27.0 新版前端已提升至主域名：/ （前台）· /admin （后台）")
else:
    _log.error("★ 新版前端未构建：%s 不存在，主域名将返回 503 —— "
               "请先执行 deploy/deploy_source.py（含前端构建）", NEW_DIR)

if LEGACY_DIR.exists():
    _log.info("★ 旧版已降级到回滚入口：/legacy/ （前台）· /legacy/admin/ （后台）")

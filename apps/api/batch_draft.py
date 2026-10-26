# -*- coding: utf-8 -*-
"""批量生成多章（v0.19 F1）。

为什么做
--------
作者最痛的点：**一次只能催一章**。想连写 5 章要发 5 次消息、等 5 次、
点 5 次采纳。而实际写作时作者往往想「先一口气出 5 章草稿，我再统一审」。

本模块提供**队列式批量代笔**：
  1. 一次提交「连写 N 章」→ 建一个批次
  2. 后台逐章跑（复用 agent 的单章代笔能力），每章完成即落草稿台账
  3. 进度事件逐章上报，前端能看到「第 3/5 章 生成中」
  4. 支持中途取消；已完成的章保留（断点续跑）

与短剧线的区别
--------------
短剧线已有 `drama_queue`（两级队列 + 断点续跑）。小说线此前**完全没有**批量能力。
本模块不重造轮子：**复用 agent 的单章代笔 + tasks 台账**，只在外面套一层批次编排。

安全设计
--------
* 批次上限（默认 10 章）：防止一次误点烧掉大量 token
* 逐章串行：并发跑会互相抢上下文（前一章的正文是后一章的输入），必须串行
* 单章失败不中断整批：记录失败原因，继续下一章（作者要的是"能出多少算多少"）
"""
from __future__ import annotations

import asyncio
import uuid

# 单批上限：防误操作烧 token（作者可多次提交）
MAX_BATCH_CHAPTERS = 10
DEFAULT_BATCH_CHAPTERS = 3


def new_batch_id() -> str:
    return f"cb_{uuid.uuid4().hex[:12]}"


async def run_batch_draft(db, book, start_ch: int, count: int,
                          instruction: str = "", emit=None,
                          batch_id: str = "") -> dict:
    """批量代笔：从 start_ch 起连写 count 章。

    参数
    ----
    start_ch : 起始章号（1-based）
    count    : 章数（会被 MAX_BATCH_CHAPTERS 钳制）
    instruction : 统一写作要求（会附加到每章；如「保持轻松搞笑基调」）
    emit     : 异步事件回调（同 agent 的 emit 契约）
    batch_id : 批次标识（调用方生成，用于取消与查询）

    返回 {"ok", "batch_id", "total", "done", "failed", "chapters":[{ch_idx,ok,error}]}

    设计：**逐章串行**。并行跑会互相抢上下文（前章正文是后章输入），
    而且单进程下并发只会互相拖慢。
    """
    from apps.api import agent as agent_mod
    from apps.api.models import Chapter

    count = max(1, min(int(count or DEFAULT_BATCH_CHAPTERS), MAX_BATCH_CHAPTERS))
    start_ch = max(1, int(start_ch or 1))
    batch_id = batch_id or new_batch_id()

    async def _emit(ev: dict) -> None:
        if emit:
            try:
                await emit(ev)
            except Exception:
                pass                    # 事件上报失败不阻断生成

    await _emit({"kind": "stage",
                 "text": f"批量代笔启动：第 {start_ch}–{start_ch + count - 1} 章（共 {count} 章）"})

    results: list[dict] = []
    done = failed = 0
    from sqlalchemy import select

    for i in range(count):
        ch_idx = start_ch + i
        await _emit({"kind": "stage",
                     "text": f"【{i + 1}/{count}】开始起草第 {ch_idx} 章…"})
        try:
            # 复用单章代笔：走同一套上下文装配 + 质检闸 + 草稿台账
            r = await agent_mod.draft_chapter_for_batch(
                db, book, ch_idx, instruction=instruction, emit=_emit)
            ok = bool(r.get("ok"))
            results.append({"ch_idx": ch_idx, "ok": ok,
                            "error": r.get("error", ""),
                            "words": r.get("words", 0),
                            "gate_score": r.get("gate_score", 0)})
            if ok:
                done += 1
                await _emit({"kind": "stage",
                             "text": f"【{i + 1}/{count}】第 {ch_idx} 章完成"
                                     f"（{r.get('words', 0)} 字，质检 {r.get('gate_score', 0)} 分）"})
            else:
                failed += 1
                await _emit({"kind": "warn",
                             "text": f"【{i + 1}/{count}】第 {ch_idx} 章失败："
                                     f"{r.get('error', '未知原因')}（继续下一章）"})
        except asyncio.CancelledError:
            await _emit({"kind": "warn", "text": f"批量代笔被取消（已完成 {done} 章）"})
            raise
        except Exception as e:
            failed += 1
            results.append({"ch_idx": ch_idx, "ok": False,
                            "error": f"{type(e).__name__}: {e}"})
            await _emit({"kind": "warn",
                         "text": f"【{i + 1}/{count}】第 {ch_idx} 章异常："
                                 f"{type(e).__name__}（继续下一章）"})

    summary = (f"批量代笔结束：成功 {done} 章，失败 {failed} 章。"
               f"草稿都在对话里，可逐章审核采纳。")
    await _emit({"kind": "stage", "text": summary})
    return {"ok": True, "batch_id": batch_id, "total": count,
            "done": done, "failed": failed, "chapters": results,
            "summary": summary}


def next_chapter_index(db, book) -> int:
    """下一章该写第几章（没有任何章节时为 1）。"""
    from apps.api.models import Chapter
    from sqlalchemy import func, select
    last = db.scalar(select(func.max(Chapter.idx)).where(Chapter.book_id == book.id))
    return int(last or 0) + 1


__all__ = ["run_batch_draft", "next_chapter_index", "new_batch_id",
           "MAX_BATCH_CHAPTERS", "DEFAULT_BATCH_CHAPTERS"]

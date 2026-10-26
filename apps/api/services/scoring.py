# -*- coding: utf-8 -*-
"""书籍评分与「下一步」推荐（v0.18 T1 从 main.py 抽出）。

为什么抽出来
------------
`routers/system.py` 的 `/api/dashboard` 需要 `_book_score` / `_next_step`。
它们原先在 main.py，若 routers 直接 import main 就会成环
（main → routers → main）。抽到本模块后，两边都从这里 import，环消失。

注意：`_next_step` 内部调用 `agent_mod.get_meta` 与 `consistency` 的确定性
校验——与 main 里的行为**逐字保持一致**（T1 只搬位置，不改逻辑）。
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from apps.api import consistency
from apps.api.models import (
    Book, Chapter, Character, Foreshadow, LedgerEntry, TimelineEvent, Volume,
    WorldSetting,
)


def book_score(db, b: Book) -> int:
    """书的质量分：优先用手动分，否则取已评章节均分。"""
    if b.quality_score:
        return b.quality_score
    chs = db.scalars(select(Chapter).where(Chapter.book_id == b.id)).all()
    if not chs:
        return 0
    scored = [c.gate_score for c in chs if c.gate_score]
    return round(sum(scored) / len(scored)) if scored else 0


def next_step(db, b: Book, n_ch: int | None = None) -> dict:
    """推荐下一步该做什么（仪表盘用）。

    n_ch 由调用方传入可复用已算好的章节数——原来调用方已经 count 过一次，
    这里又用同样的 db.query().count() 数一遍，纯浪费。
    """
    from apps.api import agent as agent_mod

    meta = agent_mod.get_meta(db, b.id)
    has_world = db.scalar(select(WorldSetting.id).where(WorldSetting.book_id == b.id).limit(1))
    has_char = db.scalar(select(Character.id).where(Character.book_id == b.id).limit(1))
    has_outline = db.scalar(select(Volume.id).where(Volume.book_id == b.id).limit(1))
    if n_ch is None:
        n_ch = db.query(Chapter).filter(Chapter.book_id == b.id).count()
    open_fs = db.scalar(select(Foreshadow.id).where(
        Foreshadow.book_id == b.id, Foreshadow.status == "open").limit(1))
    if not (meta.get("logline") or meta.get("synopsis") or b.genre):
        return {"step": "立项", "text": "先和创作主编聊「立项」：敲定书名、类型、卖点、梗概、单章字数。"}
    if not has_world:
        return {"step": "世界观", "text": "立项已就绪，下一步搭建世界观（体系/势力/地理/货币/规则），让设定自洽。"}
    if not has_char:
        return {"step": "人物", "text": "世界观有了，下一步设定主角与关键人物（人设/口癖/成长弧）。"}
    if not has_outline:
        return {"step": "大纲", "text": "设定齐备，下一步规划分卷大纲与爽点节奏。"}
    if n_ch == 0:
        return {"step": "开篇", "text": "万事俱备，开始写第 1 章——黄金三章必须强钩子、快节奏、早给爽点。"}
    if open_fs:
        return {"step": "推进/回收伏笔", "text": f"已写 {n_ch} 章，存在未回收伏笔，下一章可推进或回收一条伏笔，保持张力。"}
    # 写到一定篇幅后，时间线与数值账如果还是空的，后面很容易崩，
    # 这里主动提示补建，别等到矛盾已经写进正文才发现。
    if n_ch >= 3:
        has_tl = db.scalar(select(TimelineEvent.id).where(TimelineEvent.book_id == b.id).limit(1))
        if not has_tl:
            return {"step": "建纪年表", "text": f"已写 {n_ch} 章但还没有纪年表。把关键时间节点记进纪年表，后续才能自动校验时间线是否倒流。"}
        has_lg = db.scalar(select(LedgerEntry.id).where(LedgerEntry.book_id == b.id).limit(1))
        if not has_lg:
            return {"step": "建度量衡", "text": f"已写 {n_ch} 章但账本还是空的。给金钱/经验/年龄/功法/级别开账，写章时才不会凭空增减。"}
        # 一致性硬伤优先于继续推进：账崩了再往下写只会越写越乱。
        # 注意：这里只跑账本 + 纪年表两项确定性校验。
        # 之前是直接 run_consistency(db, b.id)，不带 ch_idx → 会遍历全部章节、
        # 每章跑一遍正则全文扫描；而 next_step 是在 dashboard 的 for 循环里逐本调用的，
        # 10 本 100 章就是 1000 次全量扫描，仪表盘直接拖超时。
        # 正文对账（check_text_vs_ledger）是逐章详情才需要的，这里用不上。
        try:
            issues = (consistency.check_ledger(db, b.id)
                      + consistency.check_timeline(db, b.id))
            errs = sum(1 for i in issues if i.get("severity") == "error")
            if errs:
                return {"step": "修一致性", "text": f"一致性自检发现 {errs} 处硬伤（时间线倒流或账本断层），先修账再续写。"}
        except Exception:
            # 自检本身失败不该让仪表盘白屏，但至少留个记录，别静默吞掉
            logging.getLogger("novelforge").exception("一致性自检失败 book=%s", b.id)
    return {"step": "续写", "text": f"已写 {n_ch} 章，继续按大纲写下一章，注意节奏与章末钩子。"}


__all__ = ["book_score", "next_step"]

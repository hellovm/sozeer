"""上下文装配器 —— 复刻人工流程的核心节点。

写第 N 章前，把「世界锚点」确定性装配成一份上下文包。
这是「状态中枢」相对于「聊天记录」的全部价值所在。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select

from apps.api.models import (
    Book, Chapter, Character, Foreshadow, StyleProfile, Volume, WorldSetting,
    jl,
)

RECENT_FULL = 3          # 最近 N 章给全文
SUMMARY_WINDOW = 12      # 更早章节给摘要


@dataclass
class ContextPack:
    style_dna: str = ""
    world_view: str = ""
    volume_outline: str = ""
    recent_chapters: str = ""
    early_summaries: str = ""
    characters: str = ""
    foreshadows: str = ""
    brief: str = ""
    user_instruction: str = ""
    timeline: str = ""      # v0.6 纪年表：写作时必须遵守的时间线
    ledger: str = ""        # v0.6 度量衡：写作时必须遵守的数值余额
    rolling: str = ""       # v0.14 D2b 滚动剧情摘要：全局剧情走向（第 10 包）
    retrieved: str = ""     # v0.14 D1 语义回捞：历史正文里与本章相关的片段
    # ★ v1.0-E4.20 连续性衔接：上一章结尾（必须接续）+ 最近已写章节（不得重复）。
    #   根因：代笔提示词的「起」原指令是「从一个具体场景切入」，等于命令模型另起炉灶，
    #   导致新章与上一章脱节、甚至重复同一场景。这里把「接哪、避什么」显式喂给模型。
    continuity: str = ""
    # ★ v0.21.6 B6 信息差：出场角色"现在知道什么/不知道什么"。
    #   为什么必须注入：不注入 → 模型会让角色说出他不该知道的事（一致性硬伤）。
    knowledge: str = ""
    # ★ v0.22.1 C1 三级大纲：全书定位 + 本卷定位 + 本章定位。
    #   解决"写到第 50 章不知道这一章在大局中的位置"。
    outline: str = ""
    # ★ v0.21.5 B5 角色外观锁定（短剧/配图时克隆用；小说正文也可参考）
    stats: dict = field(default_factory=dict)

    def render(self) -> str:
        parts = []
        if self.style_dna:
            parts.append(f"## 风格 DNA 与写作纪律（硬性）\n{self.style_dna}")
        if self.world_view:
            parts.append(f"## 世界观约束（不得越级、不得自相矛盾）\n{self.world_view}")
        if self.volume_outline:
            parts.append(f"## 本卷大纲\n{self.volume_outline}")
        if self.outline:
            parts.append(f"## ★ 三级大纲定位（全书/本卷/本章，写作方向不得偏离）\n{self.outline}")
        if self.brief:
            parts.append(f"## 本章任务单（必须先于正文执行）\n{self.brief}")
        if self.characters:
            parts.append(f"## 出场人物卡（口癖与状态不得写错）\n{self.characters}")
        if self.foreshadows:
            parts.append(f"## 伏笔与债务（【逾期】【严重逾期】的本章必须处理，"
                         f"【到期】的必须推进或回收）\n{self.foreshadows}")
        if self.timeline:
            parts.append(f"## 纪年表（时间线不得倒流，重大节点须与下表一致）\n{self.timeline}")
        if self.ledger:
            parts.append(f"## 度量衡余额（金钱/经验/年龄/功法/级别，不得凭空增减）\n{self.ledger}")
        if self.rolling:
            parts.append(f"## 全局剧情走向（截至上一章的滚动摘要，新章必须顺着走）\n{self.rolling}")
        if self.retrieved:
            parts.append(f"## 前文回捞（历史章节中与本章相关的片段，细节以这些为准）\n{self.retrieved}")
        if self.knowledge:
            parts.append(f"## ★ 信息差约束（角色不得说出他此刻还不知道的事）\n{self.knowledge}")
        if self.early_summaries:
            parts.append(f"## 更早章节摘要链\n{self.early_summaries}")
        if self.recent_chapters:
            parts.append(f"## 前文（最近 {RECENT_FULL} 章，风格与情节紧接）\n{self.recent_chapters}")
        if self.user_instruction:
            parts.append(f"## 用户额外要求\n{self.user_instruction}")
        return "\n\n".join(parts)


def _style_dna(db, book: Book) -> str:
    sp = db.scalar(select(StyleProfile).where(StyleProfile.book_id == book.id))
    if not sp:
        return ""
    banned = jl(sp.banned_words, [])
    s = [f"- 视角：{sp.pov}"]
    if sp.tone:
        s.append(f"- 基调：{sp.tone}")
    s.append(f"- 段落上限：{sp.max_para_chars} 字（手机端 ≤5 行）")
    if book.chapter_min or book.chapter_max:
        lo = book.chapter_min or 0
        hi = book.chapter_max or 0
        if lo and hi:
            s.append(f"- 单章字数（硬性，平台口径非空白字符数）：正文必须在 {lo}–{hi} 字之间。"
                     f"低于 {lo} 字或高于 {hi} 字均视为不合格，动笔前先规划篇幅分配。")
        else:
            s.append(f"- 单章字数（硬性，平台口径非空白字符数）：不得低于 {lo or hi} 字"
                     if (lo or hi) else "- 单章字数：未设定（以风格与前文节奏为准，立项确认后锁定）")
    else:
        s.append("- 单章字数：未设定（以风格与前文节奏为准，立项确认后锁定）")
    if banned:
        s.append("- 禁用词（出现即失败）：" + "、".join(banned))
    if sp.rules:
        s.append("\n" + sp.rules.strip())
    if sp.sample:
        s.append("\n### 范文锚定（模仿其节奏与语感，不要抄袭内容）\n" + sp.sample.strip()[:800])
    return "\n".join(s)


def _world_view(db, book: Book) -> str:
    rows = db.scalars(select(WorldSetting).where(WorldSetting.book_id == book.id)).all()
    if not rows:
        return ""
    buckets: dict[str, list[str]] = {}
    for r in rows:
        buckets.setdefault(r.category or "其他", []).append(f"{r.key}：{r.value}")
    parts = []
    for k, v in buckets.items():
        if k == "境界":
            # 境界锁定声明：WorkBuddy 实测中"降维打同境界对手"的矛盾即源于此缺失
            parts.append("【境界】（本书唯一实力体系，人物境界不得无因变动、不得越级、"
                         "同境界对手不得称'降维碾压'）\n" + "\n".join(v))
        else:
            parts.append(f"【{k}】\n" + "\n".join(v))
    return "\n".join(parts)


def _volume(db, book: Book, ch_idx: int) -> tuple[str, Volume | None]:
    vols = db.scalars(select(Volume).where(Volume.book_id == book.id).order_by(Volume.idx)).all()
    cur = None
    for v in vols:
        if v.start_ch <= ch_idx <= (v.end_ch or 10 ** 9):
            cur = v
            break
    if not cur:
        return "", None
    txt = f"卷{cur.idx}《{cur.name}》（第 {cur.start_ch}–{cur.end_ch} 章）\n{cur.outline}"
    if cur.summary:
        txt += f"\n\n本卷已写进展：{cur.summary}"
    return txt, cur


def _chapters(db, book: Book, ch_idx: int) -> tuple[str, str, list[str]]:
    prev = db.scalars(
        select(Chapter).where(Chapter.book_id == book.id, Chapter.idx < ch_idx).order_by(Chapter.idx)
    ).all()
    if not prev:
        return "", "", []
    recent = prev[-RECENT_FULL:]
    recent_txt = "\n\n".join(f"### 第{c.idx:03d}章 {c.title}\n{c.content}" for c in recent)
    early = prev[:-RECENT_FULL][-SUMMARY_WINDOW:]
    early_txt = "\n".join(f"- 第{c.idx:03d}章 {c.title}：{(c.summary or '').strip()[:120]}" for c in early)
    return recent_txt, early_txt, [c.content for c in prev[-5:]]


def _characters(db, book: Book, ch_idx: int, mentioned: list[str] | None = None) -> str:
    rows = db.scalars(select(Character).where(Character.book_id == book.id)).all()
    if not rows:
        return ""
    picked = []
    for c in rows:
        if mentioned and c.name not in mentioned:
            continue
        if not mentioned and c.first_chapter and c.first_chapter > ch_idx:
            continue
        picked.append(c)
    if not picked:
        picked = list(rows)[:8]
    out = []
    for c in picked:
        t = f"**{c.name}**（{c.role}）"
        if c.voice:
            t += f"\n- 说话方式：{c.voice}"
        if c.state_now:
            t += f"\n- 当前状态：{c.state_now}"
        if c.profile:
            t += f"\n- 小传：{c.profile[:300]}"
        if c.arc:
            t += f"\n- 成长弧：{c.arc[:200]}"
        out.append(t)
    return "\n\n".join(out)


def _foreshadows(db, book: Book, ch_idx: int) -> str:
    rows = db.scalars(
        select(Foreshadow).where(
            Foreshadow.book_id == book.id,
            Foreshadow.status.in_(["open", "doing"]),
        ).order_by(Foreshadow.id)
    ).all()
    if not rows:
        return ""
    # v0.14 D2c：把债务状态（逾期/临近应付）算出来合并进伏笔包
    try:
        from apps.api import consistency as _cons
        ob_map = {it["id"]: it for it in _cons.obligations(db, book.id, ch_idx)}
    except Exception:
        ob_map = {}
    out = []
    for f in rows:
        ob = ob_map.get(f.id)
        debt = ""
        if ob:
            if ob["level"] == "overdue_hard":
                debt = "【严重逾期·本章必须回收或明写放弃】"
            elif ob["level"] == "overdue":
                debt = f"【逾期{ob['overdue_by']}章】"
            elif ob["level"] == "due_soon":
                debt = "【临近应付】"
        mark = "【到期】" if (f.target_vol and ch_idx >= f.target_vol * 30) else ""
        tag = {"open": "待回收", "doing": "回收中"}.get(f.status, f.status)
        out.append(f"- {debt}{mark}（第{f.planted_ch:03d}章埋，{tag}）{f.content}")
    return "\n".join(out)


def _timeline_ledger(db, book: Book) -> tuple[str, str]:
    """写章时把纪年表与账本余额塞进上下文——不塞，模型就敢让主角凭空多出三百灵石。"""
    try:
        from apps.api import consistency
        raw = consistency.render_for_prompt(db, book.id, 40)
    except Exception:
        return "", ""
    if not raw:
        return "", ""
    if "【度量衡余额" in raw:
        tl, lg = raw.split("【度量衡余额", 1)
        return tl.strip(), ("【度量衡余额" + lg).strip()
    return raw.strip(), ""


def _rolling(db, book: Book) -> str:
    """D2b：滚动剧情摘要（全局走向）。没有就返回空（首章/未更新前不占预算）。"""
    return (getattr(book, "rolling_summary", "") or "").strip()


def _continuity_block(db, book: Book, ch_idx: int) -> str:
    """★ v1.0-E4.20：连续性衔接块。

    给代笔模型两份"锚"：
    1. 上一章结尾（最近 ~900 字）——必须紧接此处悬念/场景续写，不得另起炉灶；
    2. 最近已写章节（标题+摘要，取末 2 章）——新章必须推进新进展，不得复述这些情节。

    ★ 失败静默降级：取不到上一章（首章）返回空，由提示词兜底。
    """
    prev = db.scalars(
        select(Chapter).where(Chapter.book_id == book.id, Chapter.idx < ch_idx).order_by(Chapter.idx)
    ).all()
    if not prev:
        return ""
    last = prev[-1]
    ending = (last.content or "")[-900:].lstrip()
    recent = prev[-2:]
    covered = "\n".join(
        "- 第{c.idx:03d}章《{t}》{s}".format(
            c=c,
            t=(c.title or "（无题）"),
            s=("：" + (c.summary or "").strip()[:160]) if c.summary else "（无摘要）",
        )
        for c in recent
    )
    parts = []
    if ending:
        parts.append("## 上一章结尾（★ 必须紧接此处悬念/场景续写，沿用同一时间线、同一批在场人物；除非上一章已明确转场，否则不得另起炉灶）\n" + ending)
    if last.summary:
        parts.append("## 上一章已发生（不得复述这些情节）\n" + last.summary.strip()[:200])
    if covered:
        parts.append("## 最近已写章节（新章必须推进全新进展，禁止重复这些已发生的场景/对话/事件）\n" + covered)
    return "\n\n".join(parts)


def _retrieved(db, book: Book, ch_idx: int, brief: str,
               mentioned: list[str] | None) -> str:
    """D1：语义回捞。query = 任务单 + 用户指令 + 出场人物名。失败静默降级。"""
    try:
        from apps.api import memory as memory_mod
        q = " ".join([brief or "", mentioned and " ".join(mentioned) or ""])
        hits = memory_mod.search(db, book.id, q, ch_idx=ch_idx,
                                 char_names=mentioned or None)
        return memory_mod.render_retrieved(hits)
    except Exception:
        return ""


def _outline(db, book: Book, ch_idx: int) -> str:
    """★ v0.22.1 C1：三级大纲定位。失败静默降级（不阻断生成）。"""
    try:
        from apps.api import workflow
        return workflow.outline_for_prompt(db, book.id, ch_idx)
    except Exception:
        return ""


def _knowledge(db, book: Book, ch_idx: int,
               mentioned: list[str] | None) -> str:
    """★ v0.21.6 B6：信息差约束（出场角色现在知道/不知道什么）。

    ★ 为什么只注入出场角色的：全书所有角色的知情状态会撑爆上下文，
      而且与本章无关。只给"本章会上场的人"才有效。
    """
    names = [n for n in (mentioned or []) if n]
    if not names:
        return ""
    try:
        from apps.api.models import CharacterKnowledge
        from sqlalchemy import select as _sel
        rows = db.scalars(_sel(CharacterKnowledge).where(
            CharacterKnowledge.book_id == book.id,
            CharacterKnowledge.character_name.in_(names))).all()
        if not rows:
            return ""
        known, unknown = [], []
        for r in rows:
            learned = int(r.chapter_learned or 0)
            desc = (r.fact_desc or r.fact_id or "")[:70]
            if r.known and learned and learned <= ch_idx:
                known.append(f"· {r.character_name}：已知「{desc}」（第 {learned} 章得知）")
            else:
                when = f"第 {learned} 章才得知" if learned else "尚未得知"
                unknown.append(f"· ★ {r.character_name}：**尚不知**「{desc}」（{when}）"
                               f" —— 本章不得让他提及或表现出知道")
        if not (known or unknown):
            return ""
        out = []
        if unknown:
            out.append("【禁止提及（说了就是穿帮）】")
            out.extend(unknown[:12])
        if known:
            out.append("【已知情（可以自然使用）】")
            out.extend(known[:12])
        return "\n".join(out)
    except Exception:
        return ""


def build_chapter_context(
    db,
    book: Book,
    ch_idx: int,
    brief: str = "",
    user_instruction: str = "",
    mentioned: list[str] | None = None,
) -> ContextPack:
    recent, early, prev_texts = _chapters(db, book, ch_idx)
    vol_txt, _ = _volume(db, book, ch_idx)
    tl_txt, lg_txt = _timeline_ledger(db, book)
    foreshadow_txt = _foreshadows(db, book, ch_idx)
    pack = ContextPack(
        style_dna=_style_dna(db, book),
        world_view=_world_view(db, book),
        volume_outline=vol_txt,
        recent_chapters=recent,
        early_summaries=early,
        characters=_characters(db, book, ch_idx, mentioned),
        foreshadows=foreshadow_txt,
        brief=brief,
        user_instruction=user_instruction,
        timeline=tl_txt,
        ledger=lg_txt,
        rolling=_rolling(db, book),
        retrieved=_retrieved(db, book, ch_idx, brief, mentioned),
        knowledge=_knowledge(db, book, ch_idx, mentioned),
        outline=_outline(db, book, ch_idx),
        continuity=_continuity_block(db, book, ch_idx),
    )
    pack.stats = {
        "ch_idx": ch_idx,
        "prev_chapters": len(prev_texts),
        "approx_tokens": len(pack.render()) // 1.6,
        "retrieved_hits": len(pack.retrieved.split("[第")) - 1 if pack.retrieved else 0,
    }
    pack.stats["_prev_texts"] = prev_texts
    return pack


def build_summary_context(db, book: Book, ch: Chapter) -> str:
    return f"书名：{book.title}\n章节：第{ch.idx:03d}章 {ch.title}\n\n正文：\n{ch.content[:12000]}"


# ----------------------------------------------------------------- 对话态快照
# 给 chat_stream 用。以前主编是「失忆」的：系统提示词里没有任何书籍现状，
# 用户问「现有章节写到哪了」它只能瞎猜。这里把状态中枢压成一份快照塞进上下文。
def build_agent_state(db, book: Book, chapter_limit: int = 40) -> str:
    """把一本书的现状压成一份给主编看的快照（每次对话实时生成）。"""
    from apps.api import consistency
    from apps.api.models import Chapter as _Ch

    lines: list[str] = []
    lines.append(f"# 当前项目：《{book.title or '（未命名）'}》")
    meta_bits = []
    if book.genre:
        meta_bits.append(f"类型：{book.genre}")
    if book.platform and book.platform != "generic":
        meta_bits.append(f"渠道：{book.platform}")
    if book.chapter_min or book.chapter_max:
        meta_bits.append(f"单章字数：{book.chapter_min}–{book.chapter_max}")
    if book.target_words:
        meta_bits.append(f"目标字数：{book.target_words}")
    if book.synopsis:
        meta_bits.append(f"简介：{book.synopsis[:120]}")
    lines.append("· " + "；".join(meta_bits))

    sp = db.scalar(select(StyleProfile).where(StyleProfile.book_id == book.id))
    meta = {}
    try:
        from apps.api.agent import get_meta
        meta = get_meta(db, book.id) or {}
    except Exception:
        meta = {}
    if meta:
        for k, label in (("logline", "一句话梗概"), ("synopsis", "简介"),
                         ("selling_points", "卖点"), ("tags", "标签"), ("category", "分类")):
            v = meta.get(k)
            if v:
                lines.append(f"· {label}：{v if isinstance(v, str) else '、'.join(map(str, v))}")
    if sp and sp.tone:
        lines.append(f"· 基调：{sp.tone}")

    # ---- 正文进度（关键：必须让主编看到已有章节） ----
    chs = db.scalars(select(_Ch).where(_Ch.book_id == book.id).order_by(_Ch.idx)).all()
    total_words = sum(c.words or 0 for c in chs)
    if chs:
        lines.append(f"\n## 已有正文：共 {len(chs)} 章，约 {total_words} 字，最新写到第 {chs[-1].idx} 章")
        shown = chs[-chapter_limit:]
        if len(shown) < len(chs):
            lines.append(f"（以下列出最近 {len(shown)} 章，更早章节略）")
        for c in shown:
            s = (c.summary or "").strip().replace("\n", " ")
            lines.append(f"- 第{c.idx:03d}章 《{c.title or '（无题）'}》"
                         f"{c.words or 0}字"
                         + (f"｜质检{c.gate_score}分" if c.gate_score else "")
                         + (f"｜{s[:90]}" if s else ""))
    else:
        lines.append("\n## 已有正文：**0 章，尚无任何章节**。用户问进度时如实说还没开始写。")

    # ---- 分卷大纲 ----
    vols = db.scalars(select(Volume).where(Volume.book_id == book.id).order_by(Volume.idx)).all()
    if vols:
        lines.append(f"\n## 分卷大纲（{len(vols)} 卷）")
        for v in vols:
            lines.append(f"- 卷{v.idx}《{v.name}》第{v.start_ch}–{v.end_ch}章"
                         + (f"：{(v.outline or '').strip()[:120]}" if v.outline else ""))

    # ---- 人物 ----
    chars = db.scalars(select(Character).where(Character.book_id == book.id)).all()
    if chars:
        lines.append(f"\n## 人物清单（{len(chars)} 人"
                     + (f"，其中 {sum(1 for c in chars if c.is_auto)} 人由写作过程自动补入" if any(c.is_auto for c in chars) else "")
                     + "）")
        for c in chars[:30]:
            bits = [f"**{c.name}**"]
            if c.role:
                bits.append(f"（{c.role}）")
            if c.state_now:
                bits.append(f"当前：{c.state_now[:50]}")
            if c.first_chapter:
                bits.append(f"首现第{c.first_chapter}章")
            if c.is_auto:
                bits.append(f"[自动·出现{c.mentions or 0}次]")
            lines.append("- " + "".join(bits))

    # ---- 世界观 ----
    ws = db.scalars(select(WorldSetting).where(WorldSetting.book_id == book.id)).all()
    if ws:
        buckets: dict[str, list[str]] = {}
        for w in ws:
            buckets.setdefault(w.category or "其他", []).append(f"{w.key}：{w.value}")
        lines.append(f"\n## 世界观设定（{len(ws)} 条）")
        for k, v in buckets.items():
            lines.append(f"- 【{k}】" + "；".join(x[:60] for x in v[:8]))

    # ---- 伏笔 ----
    fss = db.scalars(select(Foreshadow).where(Foreshadow.book_id == book.id).order_by(Foreshadow.id)).all()
    if fss:
        open_n = sum(1 for f in fss if f.status == "open")
        lines.append(f"\n## 伏笔（共 {len(fss)} 条，待回收 {open_n} 条）")
        for f in fss[:15]:
            tag = {"open": "待回收", "doing": "回收中", "done": "已回收"}.get(f.status, f.status)
            lines.append(f"- [{tag}]（第{f.planted_ch:03d}章埋）{f.content[:80]}")
        # v0.14 D2c：主编快照里给出逾期债务警示，让对话端也能感知债务
        try:
            from apps.api import consistency as _cons
            cur_ch = chs[-1].idx if chs else 0
            overdue = [it for it in _cons.obligations(db, book.id, cur_ch)
                       if it["level"] in ("overdue", "overdue_hard")]
            if overdue:
                lines.append(f"\n## ⚠️ 伏笔债务逾期（{len(overdue)} 条，读者等着回收）")
                for it in overdue[:5]:
                    lines.append(f"- 第{it['planted_ch']:03d}章埋、应第{it['due_ch']}章回收：{it['content'][:60]}")
        except Exception:
            pass

    # ---- 纪年表 / 度量衡 ----
    try:
        tl = consistency.timeline_summary(db, book.id, 30)
        if tl:
            lines.append(f"\n## 纪年表（{len(tl)} 条）")
            for r in tl[-15:]:
                head = (f"{r['era']}·" if r["era"] else "") + (r["time_label"] or "")
                lines.append(f"- 第{r['ch_idx']:03d}章 {head}｜{r['title']}")
        bals = consistency.ledger_balances(db, book.id)
        if bals:
            lines.append("\n## 度量衡余额（写章时不得凭空增减）")
            for b in bals[:15]:
                lines.append(f"- {b['subject']}·{b['metric']}：{b['balance']}{b['unit'] or ''}"
                             f"（截至第{b['at_ch']:03d}章）")
    except Exception:
        pass

    lines.append(
        "\n---\n以上为**系统实时读取的真实状态**，是唯一事实来源。"
        "用户问「写了几章」「现有内容」「进度如何」时，必须依据上面这份快照回答，"
        "不得编造，也不得说「没有记录」。需要看某章全文时用 read_chapter 工具。")
    return "\n".join(lines)

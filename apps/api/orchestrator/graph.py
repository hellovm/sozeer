"""自研薄编排内核。

不引入重型 Agent 框架：一条显式流水线 + 一个可变状态对象 + 一个事件发射器。
每个节点职责单一，可以单独跑、可以人工介入、可以从任意节点重放。

流程：brief → context → generate(流式) → gate → polish(定点) → [登记] → [落库]
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from apps.api import llm
from apps.api.models import (
    Book, Chapter, Foreshadow, Character, ModelProfile, Run, StyleProfile, jd,
)
from apps.api.orchestrator import context as ctx_builder
from apps.api.orchestrator import gates
from apps.api.skills import loader as skills

MAX_POLISH_ROUNDS = 1


def polish_rounds_for(score: int) -> int:
    """D11 辩论循环：定点改写轮数按闸门分动态（v0.14.5）。

    - ≥85 分：免修（闸门基本干净，改写反而有引入新问题的风险）
    - 70-84 分：一轮（常规定点改写）
    - <70 分：两轮（第二轮带编辑意见辩论，见 node_polish）
    """
    if score >= 85:
        return 0
    if score >= 70:
        return 1
    return 2


class GateIssueLite:
    """node_polish 内部用的轻量 issue 描述，字段与 gates.GateIssue 对齐。"""

    def __init__(self, rule: str, level: str, message: str, fix_hint: str = ""):
        self.rule = rule
        self.level = level
        self.message = message
        self.span = ""
        self.fix_hint = fix_hint


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _track(fc, task: str, model, r, prompt: str = "") -> None:
    """★ 记账的**容错包装**（v0.23.2 D2）。

    为什么需要包装：记账是旁路功能，若 fc 是不支持 track 的对象
    （测试替身、旧版本的 FlowContext）也不该炸掉生成流程。
    与 costing.record 的 fail-open 策略一致。

    ★★ v1.0-E4.5：同时记一次**功能使用埋点**（telemetry + adoption）。
      为什么放这里：这是所有 LLM 调用的**统一出口**，
      在这里埋点能用最小改动覆盖全部环节（不用在每个调用点各写一遍）。

    ★★ v1.0-E4.6：新增 `prompt` 参数（**只取哈希，不存原文**）。

      ★ 为什么需要传提示词：要回答"这次改了 prompt，生成质量变好没有？"
        必须把**提示词版本**与**产出质量**关联起来。
        没有这个关联，A/B 分析无从谈起。

      ★ 为什么用参数而不是让 _track 自己去取：
        提示词是各调用点**局部构造**的（skills.render 的产物），
        统一出口拿不到 —— 必须由调用方显式传入。

      ★ 隐私：`adoption.prompt_fingerprint()` 只取 SHA256 前 16 位，
        原文绝不落地、绝不上报。
    """
    fn = getattr(fc, "track", None)
    if fn is not None:
        try:
            fn(task, model, r)
        except Exception:                   # noqa: BLE001
            pass
    _record_task_event(task, r, prompt, fc)


def _record_task_event(task: str, r, prompt: str = "", fc=None) -> None:
    """★ 功能埋点（v1.0-E4.5/E4.6）—— 采集"用了多少次、成功与否、用什么提示词"。

    ★ 为什么必须补：此前 `telemetry.track()` 的**业务调用点为 0**，
      导致上报包里的 `feature` 字段永远是空 `{}` ——
      完全看不到功能使用分布。
    """
    try:
        from apps.api import telemetry as _tm
        from apps.api import adoption as _ad
        _tm.track(task)
        # ★ 判断成功：r.error 为空即成功（_Usage 与 LLMResult 都有该属性）
        _ok = not (getattr(r, "error", "") or "")
        _ad.record_feature(task)
        # ★ prompt 会被转成哈希后进计数键（见 adoption.prompt_fingerprint）
        _ad.record_generate(task, words=0, prompt=prompt, ok=_ok)
        # ★★ 回写指纹到上下文（供采纳侧读取，见 FlowContext.prompt_hash）
        if prompt:
            _fp = _ad.prompt_fingerprint(prompt)
            fn2 = getattr(fc, "__dict__", None)
            if isinstance(fn2, dict) and _fp:
                fc.prompt_hash = _fp
    except Exception:                       # noqa: BLE001
        pass                                # ★ 埋点失败绝不影响生成


@dataclass
class _Usage:
    """统一的用量载体（v0.23.2 D2）。

    ★ 为什么需要：LLMResult 与流式事件是两种形态，
      track() 需要统一接口。用属性访问适配二者，避免 track 里写 if isinstance。
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    duration_ms: int = 0
    error: str = ""


def pick_model(db, role: str) -> ModelProfile | None:
    """角色-模型路由：优先按 role 找，退回 default。

    ★ v0.23.1 D1：委托给 costing.resolve（带降级链 + 分层）。
      为什么保留这个函数而不是让调用方直接调 costing：
      有 20+ 处既有调用点，改签名风险大 —— **接口冻结、实现替换**。
    """
    rows = db.scalars(select(ModelProfile).where(ModelProfile.enabled == 1)).all()
    if not rows:
        return None
    try:
        from apps.api import costing
        m = costing.resolve(db, role)
        if m is not None:
            return m
    except Exception:                       # noqa: BLE001
        pass                                # 路由异常不能阻断生成，退回旧逻辑
    for r in rows:
        if r.role == role:
            return r
    for r in rows:
        if r.is_default:
            return r
    return rows[0]


@dataclass
class FlowContext:
    db: Any
    book: Book
    ch_idx: int
    emit: Callable[[dict], Awaitable[None]]

    model_writer: ModelProfile | None = None
    model_planner: ModelProfile | None = None
    model_critic: ModelProfile | None = None

    brief: dict = field(default_factory=dict)
    pack: Any = None
    content: str = ""
    report: Any = None
    run_id: str = ""
    user_instruction: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    t0: float = 0.0
    # ★ v0.23.2 D2：成本归属（记账用）
    user_id: str = ""
    call_log: list = field(default_factory=list)
    # ★★ v1.0-E4.6：本次生成所用提示词的**指纹**（SHA256 前 16 位）
    #
    #   ★ 为什么要带在上下文里：采纳发生在**生成之后**（可能在别的请求里），
    #     要算"提示词版本 → 采纳率"，必须把指纹从生成侧传到采纳侧。
    #     `call_log` 已经在做类似的事（记录每次调用），这里是补"用了哪版提示词"。
    #
    #   ★ 隐私：只存哈希，原文永不落地。
    prompt_hash: str = ""

    async def event(self, kind: str, **data):
        await self.emit({"kind": kind, **data})

    def track(self, task: str, model: ModelProfile | None, r) -> None:
        """★ 记一次 LLM 调用的账（token / 成本 / 归属）。

        为什么在 FlowContext 上做：生成流里模型调用散落在多个 node，
        集中在这里才能保证**不漏记**。同时写 LLMCall 台账供看板聚合。
        """
        if r is None:
            return
        tin = int(getattr(r, "input_tokens", 0) or 0)
        tout = int(getattr(r, "output_tokens", 0) or 0)
        self.tokens_in += tin
        self.tokens_out += tout
        self.cost += float(getattr(r, "cost", 0.0) or 0.0)
        self.call_log.append({"task": task, "tokens": tin + tout})
        try:
            from apps.api import costing
            costing.record(self.db, task=task,
                           role=(model.role if model else ""),
                           user_id=self.user_id, book_id=self.book.id,
                           model=model, input_tokens=tin, output_tokens=tout,
                           cost=getattr(r, "cost", None),
                           duration_ms=int(getattr(r, "duration_ms", 0) or 0),
                           status=("ok" if not getattr(r, "error", "") else "error"),
                           error=str(getattr(r, "error", "") or ""),
                           run_id=self.run_id, commit=False)
        except Exception:                       # noqa: BLE001
            pass                                # 记账失败不影响生成



# ---------------------------------------------------------------- 节点


async def node_brief(fc: FlowContext) -> None:
    """章前任务单：先想清楚这一章要干什么，再动笔。"""
    await fc.event("stage", name="brief", text="生成章前任务单…")
    m = fc.model_planner or fc.model_writer
    if not m:
        # 没有规划模型时，退化为用户指令直传（保证流程不中断）
        if fc.user_instruction.strip():
            fc.brief = {"goal": fc.user_instruction, "beat": [], "payoff": [], "plant": [],
                        "coolpoint": "", "characters": [], "constraint": "", "hook": "",
                        "title": ""}
            await fc.event("brief", brief=fc.brief)
        return
    sk = skills.load("chapter_brief", fc.db)
    prompt = skills.render(
        sk,
        volume_outline=fc.pack.volume_outline,
        early_summaries=fc.pack.early_summaries,
        recent_chapters=fc.pack.recent_chapters[-6000:],
        foreshadows=fc.pack.foreshadows,
        characters=fc.pack.characters,
        user_instruction=fc.user_instruction,
        timeline=fc.pack.timeline,
        ledger=fc.pack.ledger,
    )
    r = await llm.complete(m, [{"role": "user", "content": prompt}], json_mode=True)
    # ★ v0.23.2 D2：带任务标签记账（看板按功能聚合）
    _track(fc, "chapter_brief", fc.model_planner or m, r, prompt)
    if r.error:
        await fc.event("warn", text=f"任务单生成失败（不影响正文）：{r.error[:200]}")
        return
    try:
        fc.brief = json.loads(_strip_fence(r.text))
    except Exception:
        fc.brief = {"goal": r.text[:200], "beat": [], "payoff": [], "plant": [],
                    "coolpoint": "", "characters": [], "constraint": "", "hook": "", "title": ""}
    await fc.event("brief", brief=fc.brief)


async def node_context(fc: FlowContext) -> None:
    await fc.event("stage", name="context", text="装配上下文…")
    mentioned = fc.brief.get("characters") or None
    fc.pack = ctx_builder.build_chapter_context(
        fc.db, fc.book, fc.ch_idx,
        brief=_render_brief(fc.brief),
        user_instruction=fc.user_instruction,
        mentioned=mentioned,
    )
    await fc.event("context", stats={k: v for k, v in fc.pack.stats.items() if k != "_prev_texts"})


async def node_generate(fc: FlowContext) -> None:
    await fc.event("stage", name="generate", text="正文生成中…")
    m = fc.model_writer
    if not m:
        await fc.event("error", text="未配置可用模型，请先在配置中心添加并设为默认")
        return
    sk = skills.load("chapter_write", fc.db)
    prompt = skills.render(
        sk,
        style_dna=fc.pack.style_dna,
        world_view=fc.pack.world_view,
        volume_outline=fc.pack.volume_outline,
        brief=_render_brief(fc.brief),
        characters=fc.pack.characters,
        foreshadows=fc.pack.foreshadows,
        early_summaries=fc.pack.early_summaries,
        recent_chapters=fc.pack.recent_chapters,
        user_instruction=fc.user_instruction,
        ch_idx=f"{fc.ch_idx:03d}",
        ch_title=fc.brief.get("title") or "",
        timeline=fc.pack.timeline,
        ledger=fc.pack.ledger,
        rolling=fc.pack.rolling,
        retrieved=fc.pack.retrieved,
    )
    buf: list[str] = []
    # ★ v0.23.2 D2：改用 stream_usage —— 正文是最大成本项，
    #   必须拿到 token 用量才能进成本看板（原来 stream() 只给文本）。
    # ★ 幂等标志（见下方 done 分支说明）
    _gen_tracked = False
    async for ev in llm.stream_usage(m, [{"role": "user", "content": prompt}]):
        k = ev.get("kind")
        if k == "token":
            buf.append(ev.get("text") or "")
            await fc.event("token", text=ev.get("text") or "")
        elif k == "done":
            # ★★ 流式路径的埋点（v1.0-E4.6）
            #
            #   ★ 为什么要单独埋：流式路径**不走 `_track()`** ——
            #     它是 `async for ev in llm.stream_usage()` 的循环，
            #     直接调 `fc.track()`（那个只记账，不触发功能埋点）。
            #
            #   ★★ 幂等保护：`done` 事件在**多轮工具调用**场景下可能触发多次，
            #     不防重会让"生成次数"虚高（污染采纳率分母）。
            if not _gen_tracked:
                _gen_tracked = True
                _record_task_event("chapter_write", _Usage(), prompt, fc)
            fc.track("chapter_write", m, _Usage(
                input_tokens=ev.get("input_tokens", 0),
                output_tokens=ev.get("output_tokens", 0),
                cost=ev.get("cost", 0.0),
                duration_ms=ev.get("duration_ms", 0),
            ))
        elif k == "error":
            fc.content = ""
            await fc.event("error", text=f"模型调用失败：{(ev.get('text') or '')[:300]}")
            return
    fc.content = "".join(buf).strip()
    if not fc.content:
        await fc.event("error", text="模型未返回内容")
        return
    # llm.stream() 在 HTTP 4xx/5xx 时是把 "\n[模型错误 HTTP xxx] ..." 当成一个 delta
    # yield 出来的。不拦的话这串错误文本会原样进正文 → 非空 → 闸门、精修照常跑，
    # 用户看到的是一章以"[模型错误 HTTP 401]"开头的"正文"。
    if "[模型错误 HTTP" in fc.content:
        err = fc.content.split("[模型错误 HTTP", 1)[1][:300]
        await fc.event("error", text=f"模型调用失败：{err}")
        fc.content = ""
        return
    _ensure_heading(fc)


def _ensure_heading(fc: FlowContext) -> None:
    """模型偶尔不写标题行：用任务单标题补齐，保证章节结构统一。"""
    lines = fc.content.strip().splitlines()
    if lines and re.match(r"^#\s*第\s*\d+\s*章", lines[0].strip()):
        return
    title = (fc.brief or {}).get("title") or ""
    body = fc.content.strip()
    if lines and lines[0].lstrip().startswith("#"):
        body = "\n".join(lines[1:]).strip()
    fc.content = f"# 第{fc.ch_idx:03d}章 {title}\n\n{body}".strip()


async def node_gate(fc: FlowContext) -> None:
    await fc.event("stage", name="gate", text="质量闸门检查…")
    style = fc.db.scalar(select(StyleProfile).where(StyleProfile.book_id == fc.book.id))
    prev = fc.pack.stats.get("_prev_texts", []) if fc.pack else []
    fc.report = gates.run_gates(fc.content, fc.book, style, prev,
                                realm_whitelist=gates.realm_whitelist_for(fc.db, fc.book.id))
    await fc.event("gate", report=fc.report.to_dict())


async def node_polish(fc: FlowContext) -> None:
    """定点改写：只改违规处，绝不整章重生成。

    D11 辩论循环（v0.14.5）：
    - 轮数按闸门分动态：≥85 免修 / 70-84 一轮 / <70 两轮；
    - 每轮改写后重跑闸门，分数不升反降则回滚到改写前版本（择优，不盲改）；
    - <70 的第二轮是「辩论轮」：先用 critic 对当前稿再挑一轮问题（编辑意见），
      合并进改写指令，writer 带着双方意见改写——Writer-Editor 对抗的单轮简化版。
    """
    m = fc.model_writer
    if not m or not fc.report:
        return
    score0 = int(getattr(fc.report, "score", 0) or 0)
    max_rounds = polish_rounds_for(score0)
    for i in range(max_rounds):
        errors = [x for x in fc.report.issues if x.level == "error"]
        warns = [x for x in fc.report.issues if x.level == "warn"]
        target = errors + warns[:3]
        if not target:
            break
        # 字数不达标时必须显式进入定点改写，否则低字数草稿会以“仅剩警告”的姿态
        # 流向采纳端（第15章 1830 字被采纳的事故即源于此）。
        words = fc.report.stats.get("words", 0)
        lo = fc.book.chapter_min or 0
        hi = fc.book.chapter_max or 0
        if lo and words < lo and not any(x.rule == "字数" for x in target):
            target.append(GateIssueLite("字数", "error",
                f"正文 {words} 字，低于下限 {lo}", fix_hint=f"扩写约 {lo - words} 字"))
        if hi and words > hi and not any(x.rule == "字数" for x in target):
            target.append(GateIssueLite("字数", "warn",
                f"正文 {words} 字，超过上限 {hi}", fix_hint=f"精简约 {words - hi} 字"))
        await fc.event("stage", name="polish", text=f"定点改写（第 {i + 1} 轮，{len(target)} 处）…")
        issues_txt = "\n".join(
            f"- [{x.rule}] {x.message}" + (f"\n  位置：…{x.span}…" if x.span else "")
            + (f"\n  改法：{x.fix_hint}" if x.fix_hint else "")
            for x in target
        )
        # D11 辩论轮：低分第二轮先让 critic 挑一轮「编辑意见」并入改写指令
        editor_note = ""
        if i > 0 and fc.model_critic:
            try:
                sk_e = skills.load("editor_review", fc.db)
                if sk_e:
                    r_e = await llm.complete(
                        fc.model_critic,
                        [{"role": "user", "content": skills.render(
                            sk_e, content=fc.content[:14000], issues=issues_txt)}],
                        json_mode=True, temperature=0.3)
                    _track(fc, "chapter_polish", fc.model_critic or m,
                           r_e, prompt)
                    if not r_e.error and (r_e.text or "").strip():
                        d_e = json.loads(_strip_fence(r_e.text))
                        notes = [str(n.get("note") or "").strip()
                                 for n in (d_e.get("notes") or []) if isinstance(n, dict)]
                        notes = [n for n in notes if n][:5]
                        if notes:
                            editor_note = "编辑复核意见（合并修复，不得与既有问题清单冲突）：\n" \
                                          + "\n".join(f"- {n}" for n in notes)
                            await fc.event("stage", name="polish",
                                           text=f"编辑复核补充 {len(notes)} 条意见")
            except Exception:
                pass  # 辩论意见拿不到就按原问题清单改，不阻断
        if editor_note:
            issues_txt += "\n" + editor_note
        sk = skills.load("deai_polish", fc.db)
        sp = fc.db.scalar(select(StyleProfile).where(StyleProfile.book_id == fc.book.id))
        prompt = skills.render(
            sk, issues=issues_txt, original=fc.content,
            style_dna=fc.pack.style_dna if fc.pack else (sp.rules if sp else ""),
        )
        r = await llm.complete(m, [{"role": "user", "content": prompt}], temperature=0.6)
        _track(fc, "chapter_polish", m, r, prompt)
        if r.error or not r.text.strip():
            break
        # D11 择优：改写前留底，改写后重跑闸门——分数不升反降则回滚
        prev_content, prev_report = fc.content, fc.report
        # 整段替换（定点改写返回全文）
        fc.content = r.text.strip()
        await fc.event("replace", text=fc.content)
        style = sp
        prev = fc.pack.stats.get("_prev_texts", []) if fc.pack else []
        fc.report = gates.run_gates(fc.content, fc.book, style, prev,
                                    realm_whitelist=gates.realm_whitelist_for(fc.db, fc.book.id))
        new_score = int(getattr(fc.report, "score", 0) or 0)
        old_score = int(getattr(prev_report, "score", 0) or 0)
        if new_score < old_score:
            fc.content, fc.report = prev_content, prev_report
            await fc.event("warn", text=f"第 {i + 1} 轮改写后分数下降"
                                        f"（{old_score}→{new_score}），已回滚到改写前版本")
        else:
            await fc.event("gate", report=fc.report.to_dict())


async def node_register(fc: FlowContext, chapter: Chapter) -> None:
    """写后登记：摘要 + 伏笔 + 人物状态。闭环的关键一步。"""
    await fc.event("stage", name="register", text="登记摘要与伏笔…")
    m = fc.model_critic or fc.model_writer
    if not m or not fc.content:
        return
    sk = skills.load("chapter_summary", fc.db)
    prompt = skills.render(sk, ch_idx=f"{chapter.idx:03d}", ch_title=chapter.title,
                           content=fc.content[:14000])
    r = await llm.complete(m, [{"role": "user", "content": prompt}], json_mode=True, temperature=0.4)
    _track(fc, "chapter_summary", fc.model_critic or m, r, prompt)
    if r.error:
        await fc.event("warn", text=f"登记失败：{r.error[:160]}")
        return
    try:
        data = json.loads(_strip_fence(r.text))
    except Exception:
        await fc.event("warn", text="登记返回非 JSON，已跳过自动登记")
        return

    chapter.summary = (data.get("summary") or "").strip()
    added = []
    for p in data.get("planted") or []:
        c = (p.get("content") or "").strip()
        if not c:
            continue
        fs = Foreshadow(id=new_id("fs"), book_id=fc.book.id, content=c,
                        planted_ch=chapter.idx, target_vol=int(p.get("target_vol") or 0),
                        status="open")
        fc.db.add(fs)
        added.append(c)
    for p in data.get("payoff") or []:
        c = (p.get("content") or "").strip()
        if not c:
            continue
        # 原先这里用 SQL LIKE 拼 `%<前十字>%`。伏笔文本里只要含 % 或 _
        # （比如"50%"），% 在 LIKE 里是通配符，会误匹配到另一条伏笔
        # 并把它错误地标成"回收中"。contains() 由 SQLAlchemy 自动转义。
        row = fc.db.scalar(
            select(Foreshadow).where(Foreshadow.book_id == fc.book.id,
                                     Foreshadow.content.contains(c[:10]))
        )
        if row:
            row.status = "doing"
            row.payoff_ch = chapter.idx
            added.append(f"[回收中] {c}")
    for c in data.get("characters") or []:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        ch = fc.db.scalar(select(Character).where(Character.book_id == fc.book.id,
                                                  Character.name == name))
        if ch:
            ch.state_now = (c.get("state") or "").strip()
            # v0.14 D2 观察者证据链：状态更新必须留下出处（哪一章读出来的）。
            # 之后排查「人物状态前后矛盾」时，能直接定位到来源章，而不是无据可查。
            ch.state_src_ch = chapter.idx
    fc.db.commit()

    # v0.6：写后自动记账 + 自动记时间线。
    # 这是「数值/时间线一致性」能长期成立的前提——靠人手工补账一定补不全。
    try:
        from apps.api import consistency
        for n in data.get("numbers") or []:
            subj = (n.get("subject") or "").strip()
            if not subj:
                continue
            try:
                delta = float(n.get("delta") or 0)
            except (TypeError, ValueError):
                continue
            if not delta:
                continue
            row = consistency.add_ledger(
                fc.db, fc.book.id, subject=subj,
                metric=(n.get("metric") or "其他").strip(), delta=delta,
                ch_idx=chapter.idx, unit=(n.get("unit") or "").strip(),
                reason=(n.get("reason") or "").strip())
            added.append(f"[账] {row.subject}·{row.metric} {row.delta:+g}{row.unit} "
                         f"→ {row.balance}")
        for t in data.get("timeline") or []:
            title = (t.get("title") or "").strip()
            if not title:
                continue
            row = consistency.add_event(
                fc.db, fc.book.id, title=title, ch_idx=chapter.idx,
                era=(t.get("era") or "").strip(), time_label=(t.get("time_label") or "").strip(),
                detail=(t.get("detail") or "").strip(), kind=(t.get("kind") or "事件").strip())
            added.append(f"[纪年] {row.time_label or ''} {row.title}")
    except Exception as e:
        await fc.event("warn", text=f"自动记账/纪年失败：{type(e).__name__}")

    fc.db.commit()
    await fc.event("registered", items=added)


async def node_post_register(fc: FlowContext, chapter: Chapter) -> None:
    """登记后的确定性收尾（v0.14）：记忆索引 + 滚动摘要 + 债务核验。

    与 node_register 的区别：register 依赖模型输出（失败可跳过），
    这里三件事都是「能做就做、失败只告警」的确定性增强，不阻断采纳。
    """
    # ---- D1：正文切块入记忆索引（确定性，不依赖模型） ----
    try:
        from apps.api import memory as memory_mod
        n = memory_mod.index_chapter(fc.db, fc.book.id, chapter.idx,
                                     chapter.content or "", chapter.title or "")
        if n:
            await fc.event("action", type="memory",
                           summary=f"本章已切 {n} 块入长程记忆索引")
    except Exception as e:
        await fc.event("warn", text=f"记忆索引失败（不影响采纳）：{type(e).__name__}")

    # ---- D2b：滚动剧情摘要（依赖 critic 模型；失败只告警，下章重试） ----
    m = fc.model_critic or fc.model_writer
    if m and chapter.summary:
        try:
            sk = skills.load("rolling_summary", fc.db)
            if sk:
                prev = (getattr(fc.book, "rolling_summary", "") or "").strip()
                prompt = skills.render(
                    sk,
                    ch_idx=f"{chapter.idx:03d}",
                    ch_title=chapter.title or "",
                    chapter_summary=chapter.summary[:600],
                    prev_summary=prev[:1500],
                )
                r = await llm.complete(m, [{"role": "user", "content": prompt}],
                                       json_mode=True, temperature=0.3)
                _track(fc, "rolling_summary", fc.model_critic or m,
                       r, prompt)
                if not r.error:
                    try:
                        d = json.loads(_strip_fence(r.text))
                        new_s = (d.get("summary") or "").strip()
                        if new_s:
                            fc.book.rolling_summary = new_s
                            fc.db.commit()
                            await fc.event("action", type="rolling",
                                           summary=f"滚动剧情摘要已更新（{len(new_s)}字）")
                    except Exception:
                        await fc.event("warn", text="滚动摘要返回非 JSON，本次跳过")
                else:
                    await fc.event("warn", text=f"滚动摘要生成失败：{r.error[:120]}")
        except Exception as e:
            await fc.event("warn", text=f"滚动摘要更新失败：{type(e).__name__}")

    # ---- D2c：采纳时债务核验——本章若承诺回收（payoff）但伏笔仍 open，给出提示 ----
    try:
        from apps.api import consistency as _cons
        issues = _cons.check_obligations(fc.db, fc.book.id, chapter.idx)
        hard = [i for i in issues if i["severity"] == "error"]
        if hard:
            await fc.event("warn",
                           text=f"伏笔债务提醒：{len(hard)} 条伏笔已严重逾期，"
                                f"建议尽快安排回收（详见一致性自检）")
    except Exception:
        pass


# ---------------------------------------------------------------- 编排


async def run_generate_flow(db, book: Book, ch_idx: int, user_instruction: str,
                            emit: Callable[[dict], Awaitable[None]]) -> FlowContext:
    fc = FlowContext(db=db, book=book, ch_idx=ch_idx, emit=emit,
                     user_instruction=user_instruction, t0=time.time())
    fc.model_writer = pick_model(db, "writer")
    fc.model_planner = pick_model(db, "planner")
    fc.model_critic = pick_model(db, "critic")

    if not fc.model_writer:
        await fc.event("error", text="未配置模型：请在配置中心添加模型并测试连通")
        return fc

    # ★ v0.23.2 D2：日 token 硬上限拦截（防跑飞）。
    #   放在生成**之前**而不是之后 —— 超限时不该先烧完钱再告诉用户。
    fc.user_id = getattr(book, "owner_id", "") or ""
    try:
        from apps.api import costing
        g = costing.guard(db, fc.user_id, need_tokens=60_000)
        if not g["allowed"]:
            # ★ v0.25 Xf-2：配额超限通知（站内信 + 邮件）。
            #   为什么在这里发：这是**用户第一次撞墙**的时刻，
            #   通知里给出"次日重置/升级套餐"两条出路，比只报错友好。
            #   去重键按日 → 同一天多次触发只留一条。
            try:
                from apps.api import notify as _nf
                import datetime as _dt3
                _nf.notify(db, user_id=fc.user_id, event="quota.exceeded",
                           ctx={"used": f"{g.get('used', 0):,}",
                                "cap": f"{g.get('cap', 0):,}"},
                           dedup_key=f"quota:{_dt3.date.today().isoformat()}")
            except Exception:                       # noqa: BLE001
                pass
            await fc.event("error", text=g["reason"], kind_hint="quota")
            return fc
    except Exception as e:                              # noqa: BLE001
        # ★ 限额检查失败时**放行**（fail-open）：可用性优先，
        #   与限流器的策略一致。
        await fc.event("warn", text=f"额度检查异常，已放行：{type(e).__name__}")

    # 上下文先装（brief 也需要它）。注意这里不发 context 事件 ——
    # 紧接着 node_context 会带着 brief 里的人物名单完整重建一次 fc.pack，
    # 并在那里发 context 事件。原来两处都发，客户端收到两条一模一样的 context 事件，
    # 而且同一次生成流里全书数据被查了两遍。
    fc.pack = ctx_builder.build_chapter_context(db, book, ch_idx,
                                                user_instruction=user_instruction)

    await node_brief(fc)
    await node_context(fc)
    await node_generate(fc)
    if fc.content:
        await node_gate(fc)
        await node_polish(fc)

    run = Run(
        id=new_id("run"), book_id=book.id, kind="chapter",
        model_id=fc.model_writer.model_id if fc.model_writer else "",
        prompt_version="v1",
        context_snapshot=(fc.pack.render()[:8000] if fc.pack else ""),
        input_tokens=fc.tokens_in, output_tokens=fc.tokens_out, cost=round(fc.cost, 4),
        gate_score=fc.report.score if fc.report else 0,
        gate_report=jd(fc.report.to_dict()) if fc.report else "",
        status="ok" if fc.content else "failed",
        duration_ms=int((time.time() - fc.t0) * 1000),
    )
    db.add(run)
    db.commit()
    fc.run_id = run.id

    await fc.event("done", content=fc.content,
                   report=fc.report.to_dict() if fc.report else None,
                   cost=round(fc.cost, 4), run_id=run.id, ch_idx=ch_idx)
    return fc


async def accept_draft(db, book: Book, ch_idx: int, title: str, content: str,
                       gate: dict | None, emit=None, register: bool = True) -> Chapter:
    """采纳草稿：落库 + Git commit + 自动登记。"""
    body = content.strip()
    ch = db.scalar(select(Chapter).where(Chapter.book_id == book.id, Chapter.idx == ch_idx))
    # ★ v1.0-E4.5：记录**采纳前的原稿字数**，用于计算修改幅度
    #   （判断生成质量最直接的信号：改得少 = 质量好）
    _prev_words = int(ch.words or 0) if ch is not None else 0
    _is_new = ch is None
    if ch is None:
        ch = Chapter(id=new_id("ch"), book_id=book.id, idx=ch_idx)
        db.add(ch)
    ch.title = title
    ch.content = body
    ch.words = gates.count_words(gates.strip_title(body))
    ch.status = "final"
    ch.gate_score = (gate or {}).get("score", 0)
    ch.gate_report = jd(gate or {})
    db.commit()

    # ★★ v1.0-E4.5：采纳埋点（**所有采纳的唯一出口**）
    #
    #   ★ 为什么放这里而不是路由层：采纳路径不止一条
    #     （直接采纳 / 任务流采纳 / 批量采纳），
    #     放在核心函数里才能**穷举所有出口**。
    #     （与 MEMORY §10「资源计数释放穷举出口」同一思路）
    #
    #   ★ 采集内容：只记计数与分桶，无正文。
    try:
        from apps.api import adoption as _ad
        _ad.record_adopt(
            "chapter_write",
            # ★ 新章节无"原稿"，跳过幅度计算（避免把"从 0 到 3000"算成 100% 改动）
            before_words=0 if _is_new else _prev_words,
            after_words=int(ch.words or 0),
        )
    except Exception:                       # noqa: BLE001
        pass                                # ★ 采集失败绝不影响采纳

    from apps.api import gitrepo
    rel = f"正文/第{ch_idx:03d}章.md"
    # Git 版本化是「锦上添花」，不是采纳的硬依赖。文件权限/磁盘异常时绝不能让
    # 整次采纳 500（章节本身已落库）。降级为告警：正文照常注册，只是这条版本
    # commit 没记上，用户仍可在正文编辑器里改、下次采纳会补记。
    try:
        gitrepo.write_file(book.id, rel, f"# 第{ch_idx:03d}章 {title}\n\n{body}\n",
                           commit_msg=f"第{ch_idx:03d}章 {title}")
    except Exception as e:
        if emit:
            await emit({"kind": "warn", "text": f"正文已登记，但 Git 版本化失败：{type(e).__name__}"
                                                 "（不影响正文保存，仅该版本未入历史）"})

    if register:
        fc = FlowContext(db=db, book=book, ch_idx=ch_idx,
                         emit=emit or _noop_emit, content=body, t0=time.time())
        fc.model_writer = pick_model(db, "writer")
        fc.model_critic = pick_model(db, "critic")
        await node_register(fc, ch)
        # v0.14 P0一期：登记后的确定性收尾——记忆索引 / 滚动摘要 / 债务核验
        try:
            await node_post_register(fc, ch)
        except Exception:
            if emit:
                await emit({"kind": "warn", "text": "登记后收尾（记忆/滚动摘要）异常，已跳过"})
        # 登记时模型会把「本章出现的人物」写进 chapter_summary 的 characters 字段，
        # 但那只更新已有角色状态。这里再做一次确定性扫描，把大纲里没设计、
        # 实际却反复出场的关键人物补进清单（不依赖模型，模型不可用也照常生效）。
        try:
            from apps.api import characters as char_mod
            if int(getattr(book, "auto_char_enabled", 1) or 0) == 1:
                res = char_mod.sync_characters(db, book.id, body, ch_idx=ch_idx)
                if res.get("added") and emit:
                    await emit({"kind": "action", "type": "character",
                                "summary": "自动补入关键人物：" +
                                           "、".join(a["name"] for a in res["added"])})
        except Exception as e:
            if emit:
                await emit({"kind": "warn", "text": f"人物自动入册失败：{type(e).__name__}"})

        # 采纳后自动审计：低于阈值只告警，不阻断（用户可手工再跑一次完整审计）
        try:
            if int(getattr(book, "audit_enabled", 1) or 0) == 1:
                from apps.api import audit as audit_mod
                rep = await audit_mod.audit_chapter(db, book, ch_idx, content=body,
                                                    title=title, emit=emit)
                if not rep.get("error"):
                    audit_mod.save_report(db, book.id, rep, ch_idx=ch_idx)
                    if emit:
                        await emit({"kind": "audit", "ch_idx": ch_idx, "score": rep.get("score", 0),
                                    "summary": rep.get("summary", ""),
                                    "dimensions": rep.get("dimensions", [])})
                    if rep.get("score", 100) < int(getattr(book, "audit_threshold", 80) or 80) and emit:
                        await emit({"kind": "warn",
                                    "text": f"第{ch_idx}章审计 {rep.get('score')} 分，"
                                            f"低于阈值 {book.audit_threshold}，建议按审计意见修订"})
        except Exception as e:
            if emit:
                await emit({"kind": "warn", "text": f"自动审计失败：{type(e).__name__}"})

    # 回写书籍缓存评分。books.quality_score 字段注释写的是"仪表盘缓存评分"，
    # 但全项目没有任何一处赋值 → _book_score() 的短路分支永远不生效，
    # 每次仪表盘都把全部章节拉出来重算均值。这里在有真实分数时才写，
    # 绝不能写 0 —— 那会让短路分支永远命中 0，比不缓存更糟。
    # 注意存的是**均值**，不是刚采纳这一章的分数：_book_score 的兜底逻辑算的就是
    # 全部章节均分，两者语义必须一致，否则仪表盘会从"均分"跳成"末章分"。
    scored = [x.gate_score for x in db.scalars(
        select(Chapter).where(Chapter.book_id == book.id)).all() if x.gate_score]
    if scored:
        book.quality_score = int(round(sum(scored) / len(scored)))
        db.commit()
    return ch


async def _noop_emit(_evt):
    return None


def _render_brief(brief: dict) -> str:
    if not brief:
        return "（无任务单，按本卷大纲自然推进）"
    lines = []
    if brief.get("title"):
        lines.append(f"章标题：{brief['title']}")
    if brief.get("goal"):
        lines.append(f"主线推进：{brief['goal']}")
    if brief.get("beat"):
        lines.append("节奏：" + " → ".join(brief["beat"]))
    if brief.get("coolpoint"):
        lines.append(f"爽点：{brief['coolpoint']}")
    if brief.get("payoff"):
        lines.append("需推进/回收的伏笔：" + "；".join(brief["payoff"]))
    if brief.get("plant"):
        lines.append("需新埋伏笔：" + "；".join(brief["plant"]))
    if brief.get("constraint"):
        lines.append(f"不得违反：{brief['constraint']}")
    if brief.get("hook"):
        lines.append(f"章末钩子：{brief['hook']}")
    return "\n".join(lines)


def _strip_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n", "", s)
        s = re.sub(r"\n```$", "", s)
    return s.strip()

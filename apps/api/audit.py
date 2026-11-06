"""内容审计：把「审稿」变成可调用的能力。

Agent/技能侧的激活方式有两种：
1) 工具调用：agent 的 audit_chapter 工具（对话里说「审一下第 8 章」就会触发）。
2) API：POST /api/books/{id}/audit，前端「审计」面板和采纳后自动审计走这条。

审计 = 确定性规则（免费、稳定）+ 模型语义判定（贵、覆盖语义）。
规则跑不通或没配模型时，自动降级为纯规则审计，不报错、不阻断。
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid

from sqlalchemy import select

from apps.api import consistency, llm, quality
from apps.api.models import (
    AuditReport, Book, Chapter, Character, Foreshadow, StyleProfile, Volume,
    WorldSetting, jd, jl, now,
)
from apps.api.orchestrator import context as ctx_builder
from apps.api.orchestrator import gates
from apps.api.orchestrator import graph as gen_graph
from apps.api.skills import loader as skills

DIMENSIONS = ["去AI味", "逻辑一致性", "内容一致性", "世界观一致性", "大纲遵从度", "实力体系一致性", "叙事健康度"]
WEIGHTS = {"去AI味": 0.16, "逻辑一致性": 0.18, "内容一致性": 0.16,
           "世界观一致性": 0.15, "大纲遵从度": 0.12, "实力体系一致性": 0.13,
           "叙事健康度": 0.10}


# ----------------------------------------------------------------- 规则层
def _rule_audit(body: str, book: Book, sp: StyleProfile | None,
                chars: list, ch_idx: int = 0) -> list[dict]:
    """确定性规则：去AI味 / 对话标签 / 章末钩子 / 连续性 / 无源梗。"""
    d = quality.diagnose(body, book, chars, sp)
    out = []
    for f in d.findings:
        dim = {
            "去AI味": "去AI味", "Show-Don't-Tell": "去AI味", "对话": "去AI味",
            "节奏": "逻辑一致性", "章末钩子": "大纲遵从度", "连续性": "内容一致性",
        }.get(f.skill, "内容一致性")
        out.append({"severity": f.severity, "message": f"[{f.skill}] {f.message}",
                    "span": f.span, "dim": dim})
    # 无源梗（v0.9.0）：规则层确定性模式进审计，防模型漏判叙事健康度
    rep0 = gates.GateReport()
    gates.check_dangling_meme(body, rep0)
    for i in rep0.issues:
        out.append({"severity": i.level, "message": f"[无源梗] {i.message}",
                    "span": i.span, "dim": "叙事健康度"})
    return out


def _rule_report(body: str, book: Book, sp: StyleProfile | None,
                 chars: list, ch_idx: int = 0) -> dict:
    """纯规则审计（模型不可用时的降级产出）。"""
    items = _rule_audit(body, book, sp, chars, ch_idx)
    # 加上一致性引擎的硬伤
    cons: list[dict] = []
    try:
        from apps.api.db import SessionLocal
        db = SessionLocal()
        try:
            cons = consistency.check_text_vs_ledger(db, book.id, ch_idx, body)
        finally:
            db.close()
    except Exception:
        cons = []

    dims = []
    for name in DIMENSIONS:
        hits = [i for i in items if i["dim"] == name]
        if name == "世界观一致性":
            hits = hits + [dict(c, dim=name) for c in cons]
        errs = sum(1 for h in hits if h["severity"] == "error")
        warns = sum(1 for h in hits if h["severity"] == "warn")
        raw = max(40, 100 - errs * 16 - warns * 6)
        # v0.13.7：规则层只能做确定性检查，覆盖不了语义层，"没查出问题"≠"质量好"。
        # 之前无扣分时直接给 100，全书审计 15 章全是 97-99 的虚高分。封顶 79，
        # 与 LLM 审计的常见分布（82-96）拉开，让"这章没被真正审过"一眼可见。
        dims.append({
            "name": name,
            "score": min(79, raw),
            "issues": [{"severity": h["severity"], "message": h["message"],
                        "span": h.get("span", "")} for h in hits],
        })
    score = int(round(sum(d["score"] * WEIGHTS[d["name"]] for d in dims)))
    return {
        "score": score,
        "dimensions": dims,
        "consistency": cons,
        "summary": "（模型未参与，仅确定性规则审计，分数封顶 79 以示区别）"
                   + ("发现问题 " + str(len(items) + len(cons)) + " 项" if items or cons else "未发现明显问题"),
        "mode": "rules",
    }


# ----------------------------------------------------------------- 境界体系上下文（v0.7.2）
# 规则层闸门（gates.check_realms）只拦"体系外乱造境界"；越级语义矛盾
# （同境降维 / 无铺垫越级 / 境界漂移）必须由 LLM 审计判，前提是模型拿到
# 有序境界阶梯 + 角色境界归属 + 疑似碾压表述。
_REALM_RISK = re.compile(
    r"(降维碾压|降维打击|降维|碾压|秒杀|吊打|瞬杀|一招[击杀败破]|越级[斩杀败击破])")


def _realm_risk_spans(body: str, n: int = 6) -> list[str]:
    """预筛疑似越级/碾压表述，交给 LLM 判定是否真矛盾，防漏报。"""
    out = []
    for m in _REALM_RISK.finditer(body or ""):
        s = max(0, m.start() - 16)
        e = min(len(body), m.end() + 16)
        out.append(body[s:e].replace("\n", " ").strip())
        if len(out) >= n:
            break
    return out


# ----------------------------------------------------------------- 玩梗预筛（v0.9.0）
# 无源梗是语义问题（梗与后文有没有关系），规则层只能抓确定性句式；
# 把引号双关 setup 片段预标出来交给 LLM 判“有没有落点”，防漏报。
_MEME_SETUP_SCAN = gates._MEME_SETUP


def _meme_setup_spans(body: str, n: int = 6) -> list[str]:
    """预筛引号双关 setup 片段，交 LLM 核查落点（有无呼应/是否被亲手否定）。"""
    out = []
    for m in _MEME_SETUP_SCAN.finditer(body or ""):
        s = max(0, m.start() - 10)
        e = min(len(body), m.end() + 60)
        out.append(body[s:e].replace("\n", " ").strip())
        if len(out) >= n:
            break
    return out


def _realm_system(db, book: Book, body: str = "") -> str:
    """装配有序**实力体系**阶梯 + 角色归属 + 本章疑似碾压表述，作审计上下文。

    v0.15 题材解耦：维度名由题材包决定（仙侠=境界、都市=职级、悬疑=无）。
    ★ 函数名与签名保持 v0.14.10 原样，2 个调用点零改动。
    """
    from apps.api import genres

    theme = (getattr(book, "theme", "") or getattr(book, "genre", "") or "")
    dim = genres.realm_dim_for(theme, db)

    # 无实力体系的题材（如悬疑）→ 明确告知审计模型跳过该维度
    if not dim:
        return ("（本书题材为「" + (genres.get_genre(theme, db) or {}).get("label", theme)
                + "」，无实力等级体系，实力体系维度默认给满分，不硬性挑刺）")

    rows = db.scalars(select(WorldSetting).where(
        WorldSetting.book_id == book.id,
        WorldSetting.category == dim)).all()
    if not rows:
        return f"（本书未登记{dim}体系，实力体系维度默认给满分，不硬性挑刺）"
    ladder = "\n".join(f"{i}. {w.key} — {(w.value or '').strip()[:200]}"
                       for i, w in enumerate(rows, 1))
    names = {w.key for w in rows}
    chars = db.scalars(select(Character).where(Character.book_id == book.id)).all()
    owned = []
    for c in chars:
        txt = f"{getattr(c, 'profile', '') or ''} {getattr(c, 'state_now', '') or ''}"
        hits = {m.group(1) for m in gates._REALM_TOKEN.finditer(txt) if m.group(1) in names}
        if hits:
            owned.append(f"{c.name}：{'、'.join(sorted(hits))}")
    owned_txt = "；".join(owned) if owned else f"（人物卡未标注明确{dim}）"
    block = (f"合法{dim}（按设定录入顺序，由低到高；若设定内含明确阶位请以设定为准）：\n"
             f"{ladder}\n角色{dim}归属（从人物卡推测，仅供参考）：{owned_txt}")
    risks = _realm_risk_spans(body)
    if risks:
        block += "\n本章疑似越级/碾压表述（请重点核查是否真矛盾）：\n- " + "\n- ".join(risks)
    return block


# ----------------------------------------------------------------- 模型层
def _strip_fence(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n", "", s)
        s = re.sub(r"\n```$", "", s)
    return s.strip()


def _gather_context(db, book: Book, ch_idx: int, body: str = "") -> dict:
    ws = db.scalars(select(WorldSetting).where(WorldSetting.book_id == book.id)).all()
    buckets: dict[str, list[str]] = {}
    for w in ws:
        buckets.setdefault(w.category or "其他", []).append(f"{w.key}：{w.value}")
    world_view = "\n".join(f"【{k}】" + "；".join(v) for k, v in buckets.items()) or "（未设定）"

    chars = db.scalars(select(Character).where(Character.book_id == book.id)).all()
    chars_txt = "\n".join(
        f"- {c.name}（{c.role or ''}）{(c.profile or '')[:120]}"
        + (f"；口癖：{c.voice}" if c.voice else "")
        + (f"；当前状态：{c.state_now}" if c.state_now else "")
        for c in chars) or "（未设定）"

    vols = db.scalars(select(Volume).where(Volume.book_id == book.id).order_by(Volume.idx)).all()
    cur = None
    for v in vols:
        if v.start_ch <= ch_idx <= (v.end_ch or 10 ** 9):
            cur = v
            break
    outline = (f"卷{cur.idx}《{cur.name}》（第{cur.start_ch}–{cur.end_ch}章）\n{cur.outline}"
               if cur else "（尚未分卷）")

    fss = db.scalars(select(Foreshadow).where(
        Foreshadow.book_id == book.id, Foreshadow.status != "done").order_by(Foreshadow.id)).all()
    fs_txt = "\n".join(f"- （第{f.planted_ch:03d}章埋，{f.status}）{f.content}" for f in fss) or "（无）"

    early = db.scalars(select(Chapter).where(
        Chapter.book_id == book.id, Chapter.idx < ch_idx).order_by(Chapter.idx)).all()
    early_txt = "\n".join(f"- 第{c.idx:03d}章 {c.title}：{(c.summary or '').strip()[:150]}"
                          for c in early[-12:]) or "（本章为开篇）"

    # render_for_prompt 会查库，别在一次上下文装配里调用多次
    tl_ledger = consistency.render_for_prompt(db, book.id)
    if "【度量衡余额" in tl_ledger:
        tl_part, lg_part = tl_ledger.split("【度量衡余额", 1)
        lg_part = "【度量衡余额" + lg_part
    else:
        tl_part, lg_part = tl_ledger, ""

    sp = db.scalar(select(StyleProfile).where(StyleProfile.book_id == book.id))
    rules = (getattr(sp, "rules", "") or "").strip() or "（沿用默认：口语化、短段落、少形容词、多动作）"

    return {
        "world_view": world_view[:2500],
        "characters": chars_txt[:2500],
        "outline": outline[:1500],
        "foreshadows": fs_txt[:1200],
        "timeline": tl_part[:1500] or "（未建纪年表）",
        "ledger": lg_part[:1200] or "（未记账）",
        "early_summaries": early_txt,
        "rules": rules,
        "realm_system": _realm_system(db, book, body),
        "meme_setups": _meme_setups_text(body),
        # D8 三维扩充
        "char_memory": _memory_continuity_context(db, book, ch_idx, body),
        "asset_check": _asset_context(db, book, body),
        "relation_arc": _relation_context(db, book, ch_idx, body),
    }


def _meme_setups_text(body: str) -> str:
    spans = _meme_setup_spans(body)
    if not spans:
        return "（本章未检测到引号双关 setup）"
    return ("以下为本章引号双关/谐音 setup 片段，请逐条核查落点——后文两三句内有无该词的"
            "复现或语义场呼应，是否被“不是真X”亲手否定。无落点即在叙事健康度记无源梗：\n- "
            + "\n- ".join(spans))


# ----------------------------------------------------------------- D8 三维扩充（v0.14.4）
# 角色记忆连续性（死者复现/失踪者现身无交代）→ 归入内容一致性
# 物资连续性（道具凭空出现/消失）→ 归入世界观一致性
# 情感弧线（关系跳变）→ 归入叙事健康度
# 方案文档：docs/调研_小说与短剧AI生成方案对比及改进_2026-10-13.md D8 节
# 实现路径：D1 检索 + D2 观察者产物（state_now/账本/滚动摘要）交叉核验，规则+LLM 双层。

# 死亡/失踪语义场：观察者回写 state_now 时最常见的判词。命中即视为"不在场角色"。
_ABSENT_PAT = re.compile(
    r"(死[亡去着后了]|已死|身死|殒|陨落|卒|毙命|身亡|重伤垂死|气绝|断气|"
    r"失踪|消失|下落不明|生死不明|音讯全无|杳无音信|被掳|被囚|闭关|沉睡|封印)")

# 人物名出现在场且在做强动作（说话/出手/移动）的句式线索——
# 纯提及（回忆/提到名字）不算复活，只有"在场行为"才可疑。
_PRESENCE_ACT_PAT = re.compile(
    r"[^。！？\n]{0,24}(走了过来|走近|走进|开口[说道]|[说道喊叫道]：“|一掌|一剑|出手|"
    r"挡在|拉住|抓住|递[给过]|接[过住]|点了点头|摇了摇头|皱[起眉]|笑了笑|冷笑|传来.{0,6}声音)")

# 关系跳变的语义场：极高/极低亲密度突变词，配对出现即预筛给 LLM 判有无过渡。
_RELATION_SWING = re.compile(
    r"(反目|背叛|决裂|恩断义绝|势不两立|不共戴天|杀[之了他她]|恨[之入极]|"
    r"生死相许|情投意合|一见钟情|以身相许|结为夫妻|拜把子|义结金兰|" 
    r"既往不咎|冰释前嫌|握手言和)")


def _absent_chars(db, book: Book, ch_idx: int) -> list[dict]:
    """从观察者产物（state_now + 证据章号）里找「不该在场」的角色。

    判据：state_now 命中死亡/失踪语义场，且 state_src_ch < 当前章
    （状态生效在先，本章出现才算穿越）。排除掉了本章之后才死的情况。
    """
    out = []
    for c in db.scalars(select(Character).where(Character.book_id == book.id)).all():
        st = (getattr(c, "state_now", "") or "")
        if not st:
            continue
        m = _ABSENT_PAT.search(st)
        if not m:
            continue
        src = int(getattr(c, "state_src_ch", 0) or 0)
        if src and src >= ch_idx:
            continue  # 状态出自本章或未来章，不构成"前文已死"
        out.append({"name": c.name, "state": st[:80], "src_ch": src})
    return out


def _absent_presence_spans(body: str, absent: list[dict], n: int = 6) -> list[str]:
    """规则层预筛：不在场角色在本章出现「在场行为」的片段。

    只抓名字+强动作共现句（说话/出手/移动），单纯被提到名字不预筛——
    回忆、转述、命名物（"张三剑法"）都合法，交给 LLM 会浪费且高误报。
    """
    if not absent:
        return []
    names = [a["name"] for a in absent if a.get("name")]
    if not names:
        return []
    out = []
    for sent in re.split(r"(?<=[。！？\n])", body or ""):
        if not any(nm in sent for nm in names):
            continue
        if _PRESENCE_ACT_PAT.search(sent):
            s = sent.strip()
            out.append(s[:120])
            if len(out) >= n:
                break
    return out


def _memory_continuity_context(db, book: Book, ch_idx: int, body: str) -> str:
    """角色记忆连续性上下文包：不在场名单 + 本章可疑在场片段 + 历史佐证检索。"""
    absent = _absent_chars(db, book, ch_idx)
    if not absent:
        return "（人物卡无已死/失踪/离场状态标注，角色记忆连续性维度默认给满分，不硬性挑刺）"
    lines = [f"- {a['name']}：{a['state']}（第{a['src_ch'] or '?'}章状态）" for a in absent]
    block = ("以下人物按观察者记录（人物卡当前状态）已死亡/失踪/离场，"
             "本章若让其说话、出手、行动而无剧情内交代（复活/归来/幻象/遗物名义），"
             "即在内容一致性记 error：\n" + "\n".join(lines))
    spans = _absent_presence_spans(body, absent)
    if spans:
        block += ("\n\n本章疑似让其「在场行动」的片段（重点核查有无复活/归来交代）：\n- "
                  + "\n- ".join(spans))
    else:
        block += "\n\n（本章未检测到上述人物的在场行为片段，若他们只是被回忆/提及则合法。）"
    # D1 检索：用缺席者名字回捞历史片段，给 LLM 判「死亡事件是否真实发生过」的佐证
    try:
        from apps.api import memory as memory_mod
        q = "、".join(a["name"] for a in absent) + " 死亡 失踪 下落"
        hits = memory_mod.search(db, book.id, q, ch_idx=ch_idx, top_k=3, budget=900)
        if hits:
            block += "\n\n相关历史片段（核查死亡/失踪是否真发生过）：\n" + \
                memory_mod.render_retrieved(hits)[:900]
    except Exception:
        pass
    return block


def _relation_context(db, book: Book, ch_idx: int, body: str) -> str:
    """情感弧线上下文包：历史关系互动检索 + 本章关系突变词预筛。"""
    parts = []
    swings = []
    for m in _RELATION_SWING.finditer(body or ""):
        s = max(0, m.start() - 24)
        e = min(len(body), m.end() + 24)
        swings.append(body[s:e].replace("\n", " ").strip())
        if len(swings) >= 5:
            break
    if swings:
        parts.append("本章出现强关系突变表述（重点核查：与既有关系线是否有过渡铺垫，"
                     "还是从上一章的敌对直接跳到生死相许）：\n- " + "\n- ".join(swings))
    # D1 检索：用突变词回捞历史互动，供 LLM 对照前情
    try:
        from apps.api import memory as memory_mod
        hits = memory_mod.search(db, book.id,
                                 "关系 感情 敌友 恩怨 结盟 背叛 和解", ch_idx=ch_idx,
                                 top_k=3, budget=900)
        if hits:
            parts.append("历史关系互动片段（对照有无情感铺垫）：\n"
                         + memory_mod.render_retrieved(hits)[:900])
    except Exception:
        pass
    if not parts:
        return "（本章未检测到强关系突变表述，情感弧线维度默认给满分，不硬性挑刺）"
    return "\n\n".join(parts)


def _asset_context(db, book: Book, body: str) -> str:
    """物资连续性上下文包：账本余额 + 债务清单已在 ledger/foreshadows 变量里，
    这里补的是「本章新出现的关键物件」提示——提示 LLM 对照前文有无获得交代。"""
    # 规则层不做语义道具追踪（误报高），只给 LLM 核查指令；
    # 依据：账本（度量衡余额）+ 前章摘要链已足够判「凭空出现的道具」。
    return ("物资连续性核查指令：正文中出现的金钱/丹药/法宝/信物等关键物件，"
            "若在其获得来源上与前文账本余额、更早章节摘要链矛盾（凭空多出/凭空消失），"
            "在世界观一致性维度记 issue 并引用账本数据。仅按上下文可查证的矛盾记分，"
            "上下文没记载的不算矛盾、不挑刺。")


def _normalize(data: dict) -> dict:
    """把模型返回补成标准结构，缺字段就补默认值。"""
    dims = data.get("dimensions") or []
    by_name = {d.get("name"): d for d in dims if isinstance(d, dict)}
    norm = []
    for name in DIMENSIONS:
        d = by_name.get(name) or {}
        try:
            sc = int(d.get("score", 80))
        except Exception:
            sc = 80
        issues = []
        for it in (d.get("issues") or []):
            if isinstance(it, dict) and it.get("message"):
                issues.append({
                    "severity": it.get("severity", "warn"),
                    "message": str(it.get("message"))[:300],
                    "span": str(it.get("span", ""))[:120],
                })
        norm.append({"name": name, "score": max(0, min(100, sc)), "issues": issues})
    try:
        score = int(data.get("score", 0))
    except Exception:
        score = 0
    if not score:
        score = int(round(sum(d["score"] * WEIGHTS[d["name"]] for d in norm)))
    cons = []
    for c in (data.get("consistency") or []):
        if isinstance(c, dict) and c.get("message"):
            cons.append({**c, "message": str(c["message"])[:300]})
    return {
        "score": max(0, min(100, score)),
        "dimensions": norm,
        "consistency": cons,
        "summary": str(data.get("summary") or "")[:500],
        "mode": "llm",
    }


def _is_transient(err: str) -> bool:
    """判定是否瞬态上游故障：值得重试。

    背景（v0.13.7 事故复盘）：2026-10-12 10:33 全书审计 15 章在 10 秒内全部被上游
    网关以 HTTP 401 proxy auth required 拒绝（网关瞬态故障），逐章静默降级为规则
    审计，产出 97-99 的虚高分并污染 quality_score。瞬态错误应当重试，而不是直接
    拿规则分冒充审计结果。
    """
    s = (err or "")
    return any(k in s for k in ("401", "403", "408", "425", "429", "500", "502", "503", "504",
                                "timed out", "timeout", "ConnectError", "ConnectionError",
                                "RemoteProtocol", "LocalProtocol", "ReadError", "ConnectTimeout"))


async def audit_chapter(db, book: Book, ch_idx: int, content: str | None = None,
                        title: str = "", emit=None) -> dict:
    """对一章做完整审计。返回标准报告 dict。

    LLM 调用遇瞬态故障（401/429/5xx/超时）自动重试 2 次（间隔 2/5 秒），
    仍失败才降级规则审计 —— 防止上游网关抖动导致整轮审计变成规则分。
    """
    ch = None
    if content is None:
        ch = db.scalar(select(Chapter).where(Chapter.book_id == book.id, Chapter.idx == ch_idx))
        if not ch:
            return {"error": f"第{ch_idx}章不存在"}
        content = ch.content or ""
        title = ch.title or ""
    body = gates.strip_title(content or "")
    sp = db.scalar(select(StyleProfile).where(StyleProfile.book_id == book.id))
    chars = [{"name": c.name} for c in db.scalars(
        select(Character).where(Character.book_id == book.id)).all()]

    model = gen_graph.pick_model(db, "critic") or gen_graph.pick_model(db, "default") \
        or gen_graph.pick_model(db, "writer")
    if not model or not body.strip():
        rep = _rule_report(body, book, sp, chars, ch_idx)
        rep["ch_idx"] = ch_idx
        return rep

    sk = skills.load("content_audit", db)
    if not sk:
        rep = _rule_report(body, book, sp, chars, ch_idx)
        rep["ch_idx"] = ch_idx
        return rep

    ctxd = _gather_context(db, book, ch_idx, body)
    prompt = skills.render(
        sk, ch_idx=f"{ch_idx:03d}", ch_title=title,
        content=body[:14000], **ctxd,
    )
    if emit:
        await emit({"kind": "stage", "text": f"正在审计第 {ch_idx} 章（七维）…"})
    r = None
    last_err = ""
    for attempt in range(3):          # 1 次正常 + 2 次瞬态重试
        try:
            r = await llm.complete(model, [{"role": "user", "content": prompt}],
                                   json_mode=True, temperature=0.3, timeout=600)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            r = None
        if r and not r.error and (r.text or "").strip():
            break
        err_txt = (r.error if r else last_err) or ""
        if attempt < 2 and _is_transient(err_txt):
            await asyncio.sleep(2 if attempt == 0 else 5)
            continue
        break
    if r is None or r.error or not (r.text or "").strip():
        rep = _rule_report(body, book, sp, chars, ch_idx)
        err_show = (r.error if r else last_err) or "空"
        rep["summary"] = f"审计模型未返回内容（{err_show[:160]}），已降级为规则审计。"
        rep["ch_idx"] = ch_idx
        return rep

    try:
        data = json.loads(_strip_fence(r.text))
    except Exception:
        # 模型给了非 JSON：降级为规则审计，但把模型原文塞进 summary 供人看
        rep = _rule_report(body, book, sp, chars, ch_idx)
        rep["summary"] = "审计模型返回非 JSON，已降级为规则审计。"
        rep["model_raw"] = (r.text or "")[:800]
        rep["ch_idx"] = ch_idx
        return rep

    rep = _normalize(data)
    # 规则层兜底：把模型漏掉的硬伤并进去（去重按 message 前 30 字）
    rules = _rule_audit(body, book, sp, chars, ch_idx)
    have = {i["message"][:30] for d in rep["dimensions"] for i in d["issues"]}
    for it in rules:
        if it["message"][:30] in have:
            continue
        for d in rep["dimensions"]:
            if d["name"] == it["dim"]:
                d["issues"].append({"severity": it["severity"],
                                    "message": it["message"], "span": it.get("span", "")})
                break
    rep["ch_idx"] = ch_idx
    rep["cost"] = r.cost
    return rep


def save_report(db, book_id: str, rep: dict, ch_idx: int = 0, scope: str = "chapter") -> AuditReport:
    row = AuditReport(
        id=f"au_{uuid.uuid4().hex[:12]}", book_id=book_id, scope=scope,
        ch_idx=int(ch_idx or 0), score=int(rep.get("score", 0)),
        summary=(rep.get("summary") or "")[:1000], report=jd(rep), created_at=now(),
    )
    db.add(row)
    db.commit()
    # 顺手把书籍的缓存评分也刷了。books.quality_score 长期以来没有任何写入点，
    # _book_score() 里的短路分支形同虚设。只在拿到真实分数时才写。
    # v0.13.7：规则兜底分（mode=rules）不再写入 —— 封顶 79 的兜底分是"没审成"的
    # 标记，不是作品质量；之前全本审计 15 章 rules 分直接把 quality_score 刷成 99。
    score = int(rep.get("score") or 0)
    if score > 0 and rep.get("mode") == "llm":
        # ★ v1.0-E4.10：移除函数内延迟导入（模块级已有 `Book`）
        #   ★ 判据：延迟导入的唯一价值是打破循环依赖，这里不需要。
        b = db.get(Book, book_id)
        if b is not None:
            b.quality_score = score
            db.commit()
    return row


def list_reports(db, book_id: str, limit: int = 20) -> list[dict]:
    rows = db.scalars(select(AuditReport).where(
        AuditReport.book_id == book_id).order_by(AuditReport.created_at.desc()).limit(limit)).all()
    out = []
    for r in rows:
        mode = ""
        try:
            mode = (json.loads(r.report or "{}") or {}).get("mode", "")
        except Exception:
            pass
        out.append({"id": r.id, "scope": r.scope, "ch_idx": r.ch_idx, "score": r.score,
                    "mode": mode, "summary": r.summary,
                    "created_at": r.created_at.isoformat() if r.created_at else ""})
    return out

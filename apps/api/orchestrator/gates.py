"""质量闸门：规则为主，不靠模型自觉。

设计要点：违规就「定点重写那几句」，绝不整章重生成 —— 这是省钱与保风格的关键。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from apps.api.models import Book, StyleProfile
from apps.api import platform as _platform


DEFAULT_BANNED = [
    "只见", "却说", "且看", "但见", "就在这时", "说时迟那时快",
    "一股", "涌上心头", "他不禁", "她不禁", "我不禁", "不得不说", "不得不承认",
    "与此同时", "毫无疑问", "众所周知", "总而言之", "综上所述",
    "嘴角勾起一抹", "眸光一闪", "眼底闪过一丝", "嘴角微微上扬", "心中暗道",
]

AI_TELLS = [
    r"不仅.{0,6}而且", r"既.{0,6}又.{0,6}还", r"仿佛.{0,10}又似",
    r"如同.{0,10}一般", r"是那样的", r"那么的.{0,4}", r"何等的",
    r"这一刻", r"这一瞬间", r"命运的齿轮", r"冥冥之中",
]

CJK = re.compile(r"[\u4e00-\u9fff]")
SENT_SPLIT = re.compile(r"[。！？…]+")


@dataclass
class GateIssue:
    rule: str
    level: str          # error | warn
    message: str
    span: str = ""      # 命中的原文片段，用于定点重写
    fix_hint: str = ""


@dataclass
class GateReport:
    score: int = 100
    passed: bool = True
    issues: list[GateIssue] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "passed": self.passed,
            "issues": [i.__dict__ for i in self.issues],
            "stats": self.stats,
        }


def count_words(text: str) -> int:
    """平台口径字数：非空白字符数（含标点），与主流连载平台的统计一致。"""
    return len(re.sub(r"\s", "", text))


def strip_title(text: str) -> str:
    lines = text.strip().splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        return "\n".join(lines[1:]).strip()
    return text.strip()


def check_length(body: str, book: Book, rep: GateReport,
                 lo: int | None = None, hi: int | None = None) -> None:
    n = count_words(body)
    rep.stats["words"] = n
    lo = book.chapter_min if lo is None else lo
    hi = book.chapter_max if hi is None else hi
    # 0 = 未设定（立项前不卡字数），两个方向都要防，否则 max=0 会把每章都判超标。
    if lo and n < lo:
        rep.issues.append(GateIssue("字数", "error", f"正文 {n} 字，低于下限 {lo}",
                                    fix_hint=f"扩写约 {lo - n} 字"))
    elif hi and n > hi:
        rep.issues.append(GateIssue("字数", "warn", f"正文 {n} 字，超过上限 {hi}",
                                    fix_hint=f"精简约 {n - hi} 字"))


def check_paragraphs(body: str, max_chars: int, rep: GateReport) -> None:
    longs = [p for p in body.splitlines() if len(p.strip()) > max_chars]
    rep.stats["long_paragraphs"] = len(longs)
    if longs:
        rep.issues.append(GateIssue(
            "段落长度", "error", f"{len(longs)} 个段落超过 {max_chars} 字（手机端 >5 行）",
            span=longs[0][:60], fix_hint="拆分为两段"))


def check_banned(body: str, words: list[str], rep: GateReport) -> None:
    hits = []
    for w in words:
        for m in re.finditer(re.escape(w), body):
            s = max(0, m.start() - 12)
            hits.append((w, body[s:m.end() + 12].replace("\n", " ")))
            break
    rep.stats["banned_hits"] = len(hits)
    for w, ctx in hits:
        rep.issues.append(GateIssue("禁用词", "error", f"命中禁用词「{w}」", span=ctx,
                                    fix_hint="改用具体动作/直白叙述"))


EN_ALLOW = {
    "KPI", "OKR", "CPU", "GPU", "AI", "PPT", "WiFi", "WIFI", "BUG", "Bug", "bug",
    "APP", "App", "IT", "HR", "QQ", "PC", "VIP", "DNA", "NASA",
    "996", "007", "3D", "4K", "5G",
}


def check_mixed_lang(body: str, rep: GateReport) -> None:
    """中英混排：正文里不该出现零星的英文单词（专名与常见缩写除外）。"""
    bad = []
    for m in re.finditer(r"[\u4e00-\u9fff][ ]?([A-Za-z]{2,})[ ]?[\u4e00-\u9fff，。？！]", body):
        w = m.group(1)
        if w in EN_ALLOW:
            continue
        bad.append(w + m.group(0)[-1:])
    rep.stats["mixed_lang"] = len(bad)
    if bad:
        rep.issues.append(GateIssue("中英混排", "warn", f"疑似中英混排：{', '.join(bad[:5])}",
                                    fix_hint="改为中文"))


def check_ai_flavor(body: str, rep: GateReport) -> None:
    """启发式 AI 味打分：排比密度、虚词密度、句长方差、成语密度。"""
    sents = [s.strip() for s in SENT_SPLIT.split(body) if len(s.strip()) > 1]
    n = max(1, len(sents))
    lens = [len(s) for s in sents]
    avg = sum(lens) / n
    var = sum((l - avg) ** 2 for l in lens) / n
    cv = (var ** 0.5) / max(1.0, avg)          # 句长变异系数，越低越像 AI
    de_ratio = body.count("的") / max(1, len(body))
    idiom_hits = sum(len(re.findall(p, body)) for p in AI_TELLS)

    score = 100.0
    if cv < 0.45:
        score -= 15
    if de_ratio > 0.055:
        score -= 10
    if avg > 32:
        score -= 8
    score -= min(20, idiom_hits * 5)

    rep.stats.update({
        "sentences": n,
        "avg_sent_len": round(avg, 1),
        "sent_len_cv": round(cv, 3),
        "de_ratio": round(de_ratio, 4),
        "ai_tells": idiom_hits,
        "ai_score": max(0, int(score)),
    })
    if score < 70:
        rep.issues.append(GateIssue(
            "AI 味", "warn",
            f"AI 味评分 {int(score)}（句长变异 {cv:.2f}、的 字密度 {de_ratio:.3f}、套路表达 {idiom_hits} 处）",
            fix_hint="增加长短句反差，减少形容词堆叠，改成具体动作"))


def check_hook(body: str, rep: GateReport) -> None:
    tail = body.strip()[-160:]
    weak = ["且听下回分解", "欲知后事如何", "未完待续", "预知后事"]
    if any(w in tail for w in weak):
        rep.issues.append(GateIssue("章末钩子", "error", "章末使用了说书套话",
                                    span=tail[-40:], fix_hint="改为情节自然断在转折/悬念处"))
    if len(tail) < 40:
        rep.issues.append(GateIssue("章末钩子", "warn", "结尾过短，缺少钩子", fix_hint="补一段转折"))


def grams(t: str, k: int = 12):
    """12-gram 集合（去空白），用于重复度比对。"""
    s = re.sub(r"\s+", "", t)
    return {s[i:i + k] for i in range(0, max(0, len(s) - k))}


def repeat_rate(body: str, prev_text: str) -> float:
    """与单篇前文的 12-gram Jaccard 重复率（0~1）。

    ★ 用于连续性护栏：新章与**紧邻上一章**重叠过高 = 疑似重复描写同一场景/对话。
    """
    cur = grams(body)
    if not cur:
        return 0.0
    g = grams(prev_text)
    if not g:
        return 0.0
    return len(cur & g) / len(cur)


def check_repeat(body: str, prev_texts: list[str], rep: GateReport) -> None:
    """与前几章的 n-gram 重复度，防自我抄袭。

    ★ E4.20：紧邻上一章的重叠单独从严——疑似重复同一场景直接判 error，
      由调用方的连续性格栅/重生兜底修掉；更早章节仍按原 warn 阈值。
    """
    cur = grams(body)
    if not cur:
        return
    # ★ 紧邻上一章：从严（>12% 即疑似重复场景）
    if prev_texts:
        imm = repeat_rate(body, prev_texts[-1])
        rep.stats["repeat_immediate"] = round(imm, 3)
        if imm > 0.12:
            rep.issues.append(GateIssue(
                "重复度", "error",
                f"与上一章 12-gram 重复率 {imm:.1%}，疑似重复描写同一场景/对话",
                span=body[:60], fix_hint="推进全新剧情进展，不得复述前文场景"))
    worst = 0.0
    for t in prev_texts[-3:]:
        g = grams(t)
        if not g:
            continue
        worst = max(worst, len(cur & g) / len(cur))
    rep.stats["repeat_rate"] = round(worst, 3)
    if worst > 0.08:
        rep.issues.append(GateIssue("重复度", "warn", f"与前文章节 12-gram 重复率 {worst:.1%}",
                                    fix_hint="换写法，避免复用旧句式"))


# --------------------------------------------------------------- 实力体系锁定（v0.15 题材解耦）
# 来源：WorkBuddy 写作实测中"主角灵海境，打马天成是降维"的体系矛盾——
# 主角与对手同为灵海境，"降维"不成立。规则闸门负责拦"体系外乱造境界"，
# 跨境阶越级这类语义矛盾交给 LLM 审计（上下文里已注入境界锁定声明）。
#
# v0.15：从"境界"泛化为"实力体系"。具体维度名由题材包决定：
#   仙侠 → realm_dim="境界"（读 WorldSetting.category=="境界"）
#   都市 → realm_dim="职级"
#   悬疑 → realm_dim=""（无实力体系，闸门自动跳过）
# ★ 接口冻结：realm_whitelist_for 的**函数名与签名完全不变**，
#   9 个调用点零改动（见计划书 §3.4）。
_REALM_TOKEN = re.compile(r"([\u4e00-\u9fff]{1,4}境)")
# 常见非修炼体系"X境"词（环境/心境/秘境……），不作为境界名告警。
# v0.15：默认集保留为兜底；题材包可在 realm_stops 里追加自己的停用词。
_STOP_REALMS = {
    "心境", "处境", "困境", "逆境", "绝境", "梦境", "幻境", "秘境", "环境",
    "意境", "语境", "边界", "境外", "之境", "实境", "场景", "接境", "止境",
    "无人之境", "大环境", "小环境",
}


def _theme_of(db, book_id: str) -> str:
    """取书的主题材（theme 优先，回退 genre 自由文本）。取不到返回空串。"""
    try:
        from apps.api.models import Book
        b = db.get(Book, book_id)
        if not b:
            return ""
        return (getattr(b, "theme", "") or getattr(b, "genre", "") or "").strip()
    except Exception:
        return ""

def realm_whitelist_for(db, book_id: str) -> list[str]:
    """从 world_settings 提取可信**实力体系**名白名单（维度名由题材包决定）。

    返回空列表时闸门不启用——书还没录体系设定、或该题材无实力体系（如悬疑）时
    不能瞎报。★ 函数名与签名保持 v0.14.10 原样，9 个调用点零改动。
    """
    try:
        from sqlalchemy import select as _sel
        from apps.api.models import WorldSetting
        from apps.api.validators import looks_like_realm_name

        # 题材决定维度名：未命中题材 → 回退 '境界'（老书行为不变）
        from apps.api import genres
        dim = genres.realm_dim_for(_theme_of(db, book_id), db)
        if not dim:
            return []                    # 该题材无实力体系 → 闸门自动关闭

        keys = db.scalars(_sel(WorldSetting.key).where(
            WorldSetting.book_id == book_id,
            WorldSetting.category == dim)).all()
        return sorted({(k or "").strip() for k in keys
                       if looks_like_realm_name(k, dim)})
    except Exception:
        return []


def stop_realms_for(db, book_id: str) -> set[str]:
    """题材相关的"非实力等级"停用词（防误报）。未命中题材回退默认集。

    db 可为 None（run_gates 拿不到会话时）——此时用短生命周期会话自查。
    """
    try:
        from apps.api import genres
        if db is None and book_id:
            from apps.api.db import SessionLocal
            with SessionLocal() as _s:
                extra = genres.realm_stops_for(_theme_of(_s, book_id), _s)
        else:
            extra = genres.realm_stops_for(_theme_of(db, book_id) if db else "", db)
        return set(_STOP_REALMS) | set(extra or [])
    except Exception:
        return set(_STOP_REALMS)


def check_realms(body: str, whitelist: list[str], rep: GateReport,
                 stops: set[str] | None = None) -> None:
    """体系外乱造检测。stops 可传题材相关停用词（默认用全局集，行为不变）。"""
    if not whitelist:
        return
    known = set(whitelist)
    stop_words = stops if stops is not None else _STOP_REALMS
    reported: set[str] = set()
    for m in _REALM_TOKEN.finditer(body):
        tok = m.group(1)
        # 贪婪匹配会把"停在灵海境""这里的环境"整段吞进来：
        # 逐级取后缀，任一后缀命中白名单/常见词即放行。
        cands = [tok[-i:] for i in range(2, len(tok) + 1)]
        if any(c in known for c in cands):
            continue
        if any(c in stop_words for c in cands):
            continue
        core = next((x for x in cands if len(x) >= 3), cands[0])
        if core in reported:
            continue
        reported.add(core)
        rep.issues.append(GateIssue(
            "境界", "error", f"『{core}』不在本书境界体系（{'、'.join(whitelist[:10])}…）中，"
                             "疑似体系外乱造或笔误",
            span=body[max(0, m.start() - 10):m.end() + 10],
            fix_hint="改为体系内境界，或先在「世界观」中登记该境界"))


# --------------------------------------------------------------- 平台签约适配闸门（v0.8.0）
# 来自番茄签约被拒诊断：第一人称男频、圈层梗密度、章节模板化、敏感开篇、ASCII 直引号。
# 仅在 book.platform 命中 platform.PLATFORMS 时启用（opt-in，generic 老书零影响）。
_FIRST_PRON = re.compile(r"我")
_DIALOGUE = re.compile(r"[“\"][^”\"]{0,120}[”\"]")


def detect_pov(body: str) -> str:
    """去掉对话后按叙述中"我"的密度判人称：first / third / mixed。"""
    narr = _DIALOGUE.sub("", body or "")
    n = len(re.sub(r"\s", "", narr)) or 1
    ratio = len(_FIRST_PRON.findall(narr)) / n * 100
    if ratio >= 1.0:
        return "first"
    if ratio <= 0.4:
        return "third"
    return "mixed"


def check_pov(body: str, prof: dict, rep: GateReport) -> None:
    if not prof.get("pov_risk"):
        return
    pov = detect_pov(body)
    rep.stats["pov"] = pov
    label = prof.get("label", "该平台")
    if pov == "first" and prof.get("pov_pref") == "third":
        rep.issues.append(GateIssue("人称", "warn",
            f"检测到第一人称叙事；{label}男频主流为第三人称，第一人称是公认的签约难结构项（前三章定去留）",
            fix_hint="评估改第三人称；若保留，用黄金三章强差异化开篇对冲"))
    elif pov == "mixed":
        rep.issues.append(GateIssue("人称", "warn", "叙述人称疑似漂移（第一/第三人称混杂）",
                                    fix_hint="统一叙事人称"))


def check_meme_density(body: str, prof: dict, rep: GateReport) -> None:
    memes = prof.get("memes") or []
    if not memes:
        return
    hits = {w: body.count(w) for w in memes if body.count(w)}
    total = sum(hits.values())
    n = count_words(body) or 1
    per_k = total / (n / 1000)
    rep.stats["meme_per_k"] = round(per_k, 2)
    thr = prof.get("meme_per_k", 1.8)
    if per_k >= thr and total >= 3:
        top = "、".join(sorted(hits, key=lambda x: -hits[x])[:6])
        rep.issues.append(GateIssue("梗密度", "warn",
            f"圈层梗密度 {per_k:.1f}/千字（阈值 {thr}）：{top}…下沉市场共鸣有限，编辑易判'受众面窄'",
            fix_hint="每章保留 2-3 个最响的梗，删冗余，换大众化表达"))


def check_templating(body: str, prev_texts: list[str], rep: GateReport, max_report: int = 3) -> None:
    """跨章复现的短句式（"你越躺"式口头禅），补 check_repeat 只查逐字长重复的盲区。"""
    if not prev_texts:
        return
    prev_all = "\n".join(prev_texts)
    recurring: dict[str, int] = {}
    for cl in re.split(r"[，。！？；…\n]", body or ""):
        cl = cl.strip()
        if 5 <= len(cl) <= 18 and re.search(r"[\u4e00-\u9fff]", cl):
            c = prev_all.count(cl)
            if c >= 1 and cl not in recurring:
                recurring[cl] = c
    for cl, c in sorted(recurring.items(), key=lambda x: -x[1])[:max_report]:
        rep.issues.append(GateIssue("模板化", "warn",
            f"句式「{cl[:16]}」在前文已出现 {c} 次，跨章复用疑模板化/触机检",
            span=cl[:30], fix_hint="换一种写法，破同构"))


def check_opening(body: str, prof: dict, rep: GateReport, n: int = 140) -> None:
    words = prof.get("opening_sensitive") or []
    if not words:
        return
    head = (body or "")[:n]
    for w in words:
        i = head.find(w)
        if i >= 0:
            rep.issues.append(GateIssue("开篇敏感", "warn",
                f"开篇{n}字内出现敏感/导向字「{w}」，触平台敏感机制+编辑第一印象",
                span=head[max(0, i - 8):i + len(w) + 8],
                fix_hint="改中性表述，如'猝死'→'昏迷/一头栽倒'"))


def to_curly_quotes(text: str) -> str:
    """ASCII 直引号成对转中文弯引号（奇偶配对，跨行连续计数）。"""
    out, open_q = [], True
    for ch in text or "":
        if ch == '"':
            out.append("“" if open_q else "”")
            open_q = not open_q
        elif ch == "'":
            out.append("‘" if open_q else "’")
            open_q = not open_q
        else:
            out.append(ch)
    return "".join(out)


def check_quotes(body: str, rep: GateReport) -> None:
    n = (body or "").count('"') + (body or "").count("'")
    rep.stats["straight_quotes"] = n
    if n:
        rep.issues.append(GateIssue("标点", "warn",
            f"正文含 {n} 个 ASCII 直引号，中文排版不规范",
            fix_hint="统一为中文弯引号 “ ”（gates.to_curly_quotes 可自动转换）"))


# --------------------------------------------------------------- 无源梗闸门（v0.9.0）
# 来源：WorkBuddy 写作实测第003章——「第二天我是被“饿”醒的。不是真饿，是系统在
# 我脑子里敲锣：【…再不去青云宗，报名就截止了。】」：“饿”与后文完全无关，且被
# 下一句亲手否定，读者只会问“那你说个饿干嘛”。AI 第一版只补“是被系统吵醒的”
# 把句子接顺，“饿”仍然悬空——表面通顺≠逻辑成立。
# 两类确定性模式（warn 级，不阻断；对所有书常开，与平台无关的通用写作纪律）：
#   ① 自拆台：被“X”…后文紧接“不是真X/并非X”，且 X 在后文无其他落点；
#   ② 无落点：被“X”…后文窗口内 X 不复现、同义场也无呼应（梗悬空）。
# 仅对高频感官词表内词告警——表外玩法千变万化，规则层不猜，防误报。
_MEME_SETUP = re.compile(r"被[“\"]([\u4e00-\u9fff]{1,3})[”\"]")
_MEME_ECHO_WINDOW = 150
# 高频“被X醒/被X了”玩梗词 → 后文落点的同义场（窗口内出现即算梗有着落）
_MEME_FIELDS = {
    "饿": ["吃", "饭", "餐", "粥", "馒", "啃", "吞", "饱", "粮", "面", "菜", "膳", "咕咕"],
    "困": ["睡", "眠", "睁", "床", "寐", "瞌睡"],
    "冷": ["颤", "抖", "寒", "冰", "嚏", "缩"],
    "吓": ["惊", "怕", "恐", "惧", "胆", "心跳"],
    "气": ["怒", "愤", "喘", "哼", "跺", "噎"],
    "累": ["乏", "歇", "汗", "腰", "瘫"],
    "香": ["味", "闻", "馋", "飘", "呛"],
    "响": ["声", "音", "噪", "鸣", "喊", "叫"],
    "亮": ["光", "刺", "晃", "明", "照"],
    "黑": ["暗", "影", "夜", "墨"],
}


def check_dangling_meme(body: str, rep: GateReport, window: int = _MEME_ECHO_WINDOW) -> None:
    """无源梗：引号双关 setup 在后文没有落点（v0.9.0）。

    “被‘饿’醒→不是真饿”这类结构，引子被自己否定或干脆悬空，是读者一眼识破的
    逻辑瑕疵。只查表内高频感官词（被"X"的引号用法本身就是玩梗信号），表外不碰。
    """
    hits = 0
    for m in _MEME_SETUP.finditer(body or ""):
        x = m.group(1)
        if x not in _MEME_FIELDS:
            continue
        tail = (body or "")[m.end():m.end() + window]
        neg = re.search(r"(?:不是|并非|才不是|又不是|并非是|倒不是)真?" + re.escape(x), tail)
        # 否定句里的那次复现不算落点
        echoes = len(re.findall(re.escape(x), tail)) - (1 if neg else 0)
        field_hit = next((w for w in _MEME_FIELDS[x] if w in tail), "")
        if echoes > 0 or field_hit:
            continue  # 梗有落点（复现或同义场呼应），放行
        hits += 1
        if neg:
            msg = (f"引号词「{x}」被后文亲手否定（“不是真{x}”式自拆台）：引子与后句逻辑无关，"
                   f"读者只会问“那你说个{x}干嘛”")
            hint = "删掉无落点双关让陈述独立成立；或让后文出现该词的真实呼应"
        else:
            msg = f"引号词「{x}」在后文 {window} 字内无复现、同义场也无呼应，疑似无源梗（梗悬空）"
            hint = "给梗一个落点（后文呼应其语义），或直接删掉这层双关"
        rep.issues.append(GateIssue(
            "无源梗", "warn", msg,
            span=body[max(0, m.start() - 8):m.end() + 30].replace("\n", " "),
            fix_hint=hint))
    rep.stats["dangling_meme"] = hits


def run_gates(
    raw: str,
    book: Book,
    style: StyleProfile | None,
    prev_texts: list[str] | None = None,
    realm_whitelist: list[str] | None = None,
) -> GateReport:
    rep = GateReport()
    body = strip_title(raw)

    banned = DEFAULT_BANNED
    max_para = 120
    if style:
        try:
            import json as _j
            b = _j.loads(style.banned_words or "[]")
            if b:
                banned = b
        except Exception:
            pass
        max_para = style.max_para_chars or 120

    # 平台签约适配：book.platform 命中预设时才按平台规范跑（generic 老书零影响）
    prof = _platform.get_platform(getattr(book, "platform", ""))
    lo = hi = None
    if prof:
        banned = list(dict.fromkeys(list(banned) + list(prof.get("guide_sensitive") or [])))
        if not (style and style.max_para_chars):
            max_para = prof.get("max_para_chars") or max_para
        lo = prof.get("chapter_min")
        hi = prof.get("chapter_max")

    check_length(body, book, rep, lo=lo, hi=hi)
    check_paragraphs(body, max_para, rep)
    check_banned(body, banned, rep)
    check_mixed_lang(body, rep)
    check_ai_flavor(body, rep)
    check_hook(body, rep)
    check_repeat(body, prev_texts or [], rep)
    # v0.15：停用词按题材取（都市文的"阶级/级别"不该被当境界报错）；
    # 传 stops=None 时行为与改前完全一致（老书零回归）。
    try:
        _stops = stop_realms_for(None, getattr(book, "id", "")) if book else None
    except Exception:
        _stops = None
    check_realms(body, realm_whitelist or [], rep, stops=_stops)
    check_dangling_meme(body, rep)
    if prof:
        check_pov(body, prof, rep)
        check_meme_density(body, prof, rep)
        check_templating(body, prev_texts or [], rep)
        check_opening(body, prof, rep)
        check_quotes(body, rep)

    penalty = sum(12 if i.level == "error" else 5 for i in rep.issues)
    # 这里原先还有一行 rep.score = ... + 100 - ai_score，化简后等于 100 - penalty，
    # 紧接着就被下一行覆盖 —— 是重构残留的死代码，两行语义还不一致，删掉。
    # 综合分 = AI 味分 - 违规扣分
    rep.score = max(0, rep.stats.get("ai_score", 100) - penalty)
    rep.passed = not any(i.level == "error" for i in rep.issues)
    return rep

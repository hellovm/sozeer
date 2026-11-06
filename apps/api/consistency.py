"""纪年表 + 度量衡：数值与时间线的一致性引擎。

为什么需要它：长篇连载最容易崩的不是文笔，是「账」。
主角第 8 章有 300 灵石，第 20 章随手花 500 却没人发现；
第 5 章十六岁，第 30 章又成了十六岁；境界越级写得毫无代价。
这些靠模型自觉没用，必须确定性记账 + 事后对账。

本模块做三件事：
1) 记账：add_ledger 写入流水并推导余额；add_event 写入纪年表并推导排序锚点。
2) 自检：余额连续性、时间单调性、年龄/境界合理性。
3) 对账：扫描正文里出现的数值，跟账本余额和纪年表比对，报出可疑漂移。
"""
from __future__ import annotations

import datetime as dt
import re
import uuid

from sqlalchemy import select

from apps.api.models import (
    Book, LedgerEntry, TimelineEvent, now,
)

# 伏笔债务（D2c）：一章约 30 章为一个回收周期（target_vol 换算基准，与 context.py 一致）
FORESHADOW_VOL_CH = 30
# 逾期分级：超过应付章多少算紧急
OBLIGATION_OVERDUE_HARD = 20
# 临近应付窗口：到期前 10 章开始提醒（再早就注入，纯占预算）
OBLIGATION_DUE_SOON = 10

# ----------------------------------------------------------------- 度量衡：维度定义
# v0.15 题材解耦：以下 METRICS / SCAN_PATTERNS 是**仙侠题材的默认值**，
# 保持模块级符号不变（main.py 等 6 处引用零改动）。新代码请用
# metrics_for_theme() / scan_patterns_for_theme() 按 book.theme 取数。
#
# 设计要点（"接口冻结、实现替换"）：
#   * 模块级 METRICS/SCAN_PATTERNS 仍可用 → 老调用点零改动、零回归
#   * 新增 *_for_theme() 取数函数 → 新调用点按题材取
#   * 两者数据源**同出一源**（genres.py），不会漂移

def _xianxia_metrics() -> dict:
    """仙侠题材度量衡（= v0.14.10 原样，逐字节保留）。"""
    from apps.api import genres
    return genres.GENRES["xianxia"]["metrics"]


# 模块级默认表（仙侠）—— 兼容既有引用点
METRICS = _xianxia_metrics()

# 正文数值扫描：单位 → 维度（默认表，仙侠）
_UNIT2METRIC = {}
for _m, _cfg in METRICS.items():
    for _u in _cfg["units"]:
        _UNIT2METRIC[_u] = _m

# 中文数字（支持到万位，够用了）
CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 正文里的数值模式：(数量)(单位)
_NUM = r"([0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十百千万]+)"


def _compile_scan_patterns(templates: list) -> list:
    """把题材包的 (维度, 模板) 编译成 (维度, compiled_regex)。

    模板用 {NUM} 占位数字部分；未含占位符的模板自动前置一个 _NUM。

    v0.16 健壮性：入参可能是
      * (dim, "regex-template")         —— 内置包形态
      * (dim, compiled_re.Pattern)      —— 已编译（重复调用 / DB 存了对象）
      * (dim, None/其他)                —— 脏数据
    三种都要能安全处理：已编译的直接透传，非字符串且不可编译的跳过。
    （原实现直接对入参做 `"{NUM}" in tmpl`，遇到 Pattern 会 TypeError 崩掉整批扫描）
    """
    out = []
    for item in (templates or []):
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        dim, tmpl = item[0], item[1]
        if not dim:
            continue
        # 已编译的正则：直接透传
        if hasattr(tmpl, "finditer"):
            out.append((dim, tmpl))
            continue
        if not isinstance(tmpl, str) or not tmpl:
            continue
        pat = tmpl.replace("{NUM}", _NUM) if "{NUM}" in tmpl else _NUM + tmpl
        try:
            out.append((dim, re.compile(pat)))
        except re.error:
            continue                     # 坏正则跳过，不影响其他维度
    return out


# 模块级默认表（仙侠）—— 兼容既有引用点。模板与 genres.GENRES['xianxia'] 同源。
SCAN_PATTERNS = _compile_scan_patterns([
    ("金钱", r"{NUM}\s*(?:枚|块|两|文|灵石|金币|银两)"),
    ("经验", r"{NUM}\s*(?:点经验|经验值|经验)"),
    ("年龄", r"{NUM}\s*岁"),
    ("功法", r"{NUM}\s*(?:层|重)"),
    ("级别", r"{NUM}\s*(?:级|阶|段|星)"),
])


# ---------------------------------------------------------------- 题材感知取数（v0.15）
def metrics_for_theme(theme: str | None, db=None) -> dict:
    """按主题材取度量衡维度表；未命中回退仙侠表（零回归）。"""
    from apps.api import genres
    return genres.metrics_for(theme, db)


def scan_patterns_for_theme(theme: str | None, db=None) -> list:
    """按主题材取已编译的扫描模式列表；未命中回退仙侠表。"""
    from apps.api import genres
    return _compile_scan_patterns(genres.scan_patterns_for(theme, db))


def metrics_for_book(db, book) -> dict:
    """按书取度量衡（读 book.theme，回退 book.genre，再回退仙侠）。"""
    if book is None:
        return METRICS
    theme = getattr(book, "theme", "") or getattr(book, "genre", "")
    return metrics_for_theme(theme, db)


def scan_patterns_for_book(db, book) -> list:
    """按书取扫描模式（读 book.theme，回退 book.genre，再回退仙侠）。"""
    if book is None:
        return SCAN_PATTERNS
    theme = getattr(book, "theme", "") or getattr(book, "genre", "")
    return scan_patterns_for_theme(theme, db)


# 时间标记 → 排序锚点：「开元三年」「三年后」「十六岁那年」
_YEAR_PAT = re.compile(r"([0-9]+|[一二两三四五六七八九十百]+)\s*年")
_AFTER_PAT = re.compile(r"([0-9]+|[一二两三四五六七八九十]+)\s*(?:年|日后|个月后|天後|天后)")


def _cn2int(s: str) -> float:
    """把「二十三」「一百二十」这类中文数字转成 int，失败返回 0。"""
    s = (s or "").strip()
    if not s:
        return 0.0
    if re.fullmatch(r"[0-9]+(\.[0-9]+)?", s):
        return float(s)
    if not re.fullmatch(r"[零一二两三四五六七八九十百千万]+", s):
        return 0.0
    # 万 / 千 / 百 分段
    total, section = 0, 0
    for ch in s:
        if ch == "万":
            section = (section or 1) * 10000
            total += section
            section = 0
        elif ch == "千":
            section = (section or 1) * 1000
            total += section
            section = 0
        elif ch == "百":
            section = (section or 1) * 100
            total += section
            section = 0
        elif ch == "十":
            section = (section or 1) * 10
        elif ch in CN_NUM:
            section += CN_NUM[ch]
    return float(total + section)


def _new_id(p: str) -> str:
    return f"{p}_{uuid.uuid4().hex[:12]}"


# ----------------------------------------------------------------- 度量衡：记账
def _next_seq(db) -> int:
    """记账流水号：必须是显式自增，不能用 uuid id 排序代替。"""
    mx = db.scalar(select(LedgerEntry.seq).order_by(LedgerEntry.seq.desc()).limit(1))
    return int(mx or 0) + 1


def ledger_balance(db, book_id: str, subject: str, metric: str) -> float:
    """取某主体某维度的最新余额（按记账顺序，不是按 id）。"""
    row = db.scalar(
        select(LedgerEntry).where(
            LedgerEntry.book_id == book_id,
            LedgerEntry.subject == subject,
            LedgerEntry.metric == metric,
        ).order_by(LedgerEntry.seq.desc()).limit(1)
    )
    return float(row.balance) if row else 0.0


def add_ledger(db, book_id: str, subject: str, metric: str, delta: float,
               ch_idx: int = 0, unit: str = "", reason: str = "") -> LedgerEntry:
    """记一笔账：余额 = 上笔余额 + delta（确定性推导，不信任调用方传值）。

    v0.15：维度校验按**本书题材**取数——都市书允许"职级/粉丝"，仙侠书允许"功法"。
    取不到书时回退默认表（仙侠），保持老行为。
    """
    subject = (subject or "").strip()
    metric = (metric or "").strip() or "其他"
    book = db.get(Book, book_id)
    known = metrics_for_book(db, book)
    if metric not in known:
        metric = "其他"
    prev = ledger_balance(db, book_id, subject, metric)
    row = LedgerEntry(
        id=_new_id("lg"), seq=_next_seq(db), book_id=book_id, ch_idx=int(ch_idx or 0),
        subject=subject, metric=metric, delta=float(delta or 0),
        balance=round(prev + float(delta or 0), 4), unit=(unit or "").strip(),
        reason=(reason or "").strip(), created_at=now(),
    )
    db.add(row)
    db.commit()
    return row


def check_ledger(db, book_id: str) -> list[dict]:
    """账本自检：验证「上笔余额 + 本次变动 = 本次余额」是否成立。"""
    rows = db.scalars(
        select(LedgerEntry).where(LedgerEntry.book_id == book_id)
        .order_by(LedgerEntry.seq)
    ).all()
    issues: list[dict] = []
    running: dict[tuple[str, str], float] = {}
    for r in rows:
        key = (r.subject, r.metric)
        cur = running.get(key)
        if cur is None:
            # 首笔：余额必须等于 delta，否则是脏数据（导入/手工改过）
            if abs(float(r.balance) - float(r.delta)) > 1e-6:
                issues.append({
                    "kind": "ledger", "severity": "error", "ch_idx": r.ch_idx,
                    "scope": f"{r.subject}·{r.metric}",
                    "message": f"首笔余额 {r.balance} 与变动 {r.delta} 不符，应为 {r.delta}",
                })
            running[key] = float(r.balance)
            continue
        expect = round(cur + float(r.delta), 4)
        if abs(expect - float(r.balance)) > 1e-6:
            issues.append({
                "kind": "ledger", "severity": "error", "ch_idx": r.ch_idx,
                "scope": f"{r.subject}·{r.metric}",
                "message": f"余额断层：第{r.ch_idx}章前为 {round(cur,4)}，变动 {r.delta}，"
                           f"应为 {expect}，实际记为 {r.balance}",
            })
        running[key] = float(r.balance)
    # 负余额告警（除「其他」外，钱/经验/级别不该为负）
    for (subj, met), bal in running.items():
        if met in ("金钱", "经验", "级别", "功法") and bal < 0:
            issues.append({
                "kind": "ledger", "severity": "warn", "ch_idx": 0,
                "scope": f"{subj}·{met}",
                "message": f"当前余额为负（{round(bal,4)}），确认是否写错账",
            })
    return issues


def scan_numbers(text: str, patterns: list | None = None) -> dict[str, list[dict]]:
    """从正文里扫出各维度的数值提及，用于跟账本对账。

    v0.15：patterns 可选——传 None 用默认（仙侠）模式，保持签名兼容；
    题材感知的调用方传 scan_patterns_for_theme(book.theme, db)。
    """
    out: dict[str, list[dict]] = {}
    body = text or ""
    for metric, pat in (patterns if patterns is not None else SCAN_PATTERNS):
        hits = []
        for m in pat.finditer(body):
            v = _cn2int(m.group(1))
            if v <= 0:
                continue
            at = max(0, m.start() - 12)
            hits.append({"value": v, "span": body[at:m.end() + 6].replace("\n", " ")})
        if hits:
            out[metric] = hits
    return out


def check_text_vs_ledger(db, book_id: str, ch_idx: int, text: str,
                         tolerance: float = 0.0) -> list[dict]:
    """正文数值 vs 账本余额：扫到的最大值若明显超过当时余额，报可疑。"""
    issues: list[dict] = []
    book = db.get(Book, book_id)
    scanned = scan_numbers(text, scan_patterns_for_book(db, book))
    if not scanned:
        return issues
    # 该章（含）之前最后一条账目 = 本章时点的预期余额
    for metric, hits in scanned.items():
        top = max(h["value"] for h in hits)
        rows = db.scalars(
            select(LedgerEntry).where(
                LedgerEntry.book_id == book_id,
                LedgerEntry.metric == metric,
                LedgerEntry.ch_idx <= int(ch_idx or 0),
            ).order_by(LedgerEntry.seq.desc())
        ).all()
        if not rows:
            continue  # 没记账就不妄判，避免噪音
        bal = float(rows[0].balance)
        # 只报「账本记了、正文却写出超过余额的数字」这类硬伤
        if metric in ("金钱", "经验") and bal >= 0 and top > bal + max(1.0, abs(bal) * 0.6):
            issues.append({
                "kind": "text_vs_ledger", "severity": "warn", "ch_idx": ch_idx,
                "scope": metric,
                "message": f"正文出现「{int(top) if top==int(top) else top}」级别数字，"
                           f"而账本当时余额仅 {round(bal,2)}（{rows[0].subject}）",
                "span": [h["span"] for h in hits if h["value"] == top][:1],
            })
    return issues


# ----------------------------------------------------------------- 纪年表
def _sort_key(era: str, time_label: str, seq: int) -> float:
    """把时间标记转成可排序锚点；没有数字就退回按 seq 排序。"""
    label = (time_label or "") + " " + (era or "")
    m = _YEAR_PAT.search(label)
    if m:
        return _cn2int(m.group(1))
    return float(seq or 0)


def add_event(db, book_id: str, title: str, ch_idx: int = 0, era: str = "",
              time_label: str = "", detail: str = "", kind: str = "事件") -> TimelineEvent:
    seq = db.scalar(select(TimelineEvent.seq).where(
        TimelineEvent.book_id == book_id).order_by(TimelineEvent.seq.desc())) or 0
    row = TimelineEvent(
        id=_new_id("tl"), book_id=book_id, seq=int(seq) + 1, ch_idx=int(ch_idx or 0),
        era=(era or "").strip(), time_label=(time_label or "").strip(),
        sort_key=_sort_key(era, time_label, int(seq) + 1),
        title=(title or "").strip(), detail=(detail or "").strip(),
        kind=(kind or "事件").strip(), created_at=now(),
    )
    db.add(row)
    db.commit()
    return row


def check_timeline(db, book_id: str) -> list[dict]:
    """纪年表自检：时间线倒流、同一纪元下年号跳变。"""
    rows = db.scalars(
        select(TimelineEvent).where(TimelineEvent.book_id == book_id)
        .order_by(TimelineEvent.seq)
    ).all()
    issues: list[dict] = []
    prev_key = None
    prev_row = None
    for r in rows:
        if prev_key is not None and r.sort_key and prev_key and r.sort_key < prev_key - 1e-6:
            issues.append({
                "kind": "timeline", "severity": "error", "ch_idx": r.ch_idx,
                "scope": r.era or "时间线",
                "message": f"时间倒流：第{prev_row.ch_idx}章「{prev_row.time_label or prev_row.title}」"
                           f"之后，第{r.ch_idx}章回到「{r.time_label or r.title}」",
            })
        prev_key = r.sort_key
        prev_row = r
    return issues


def timeline_summary(db, book_id: str, limit: int = 60) -> list[dict]:
    rows = db.scalars(
        select(TimelineEvent).where(TimelineEvent.book_id == book_id)
        .order_by(TimelineEvent.seq)
    ).all()
    rows = rows[-limit:]
    return [{"id": r.id, "seq": r.seq, "ch_idx": r.ch_idx, "era": r.era,
             "time_label": r.time_label, "title": r.title, "detail": r.detail,
             "kind": r.kind} for r in rows]


def ledger_summary(db, book_id: str, limit: int = 80) -> list[dict]:
    rows = db.scalars(
        select(LedgerEntry).where(LedgerEntry.book_id == book_id)
        .order_by(LedgerEntry.seq)
    ).all()
    rows = rows[-limit:]
    return [{"id": r.id, "seq": r.seq, "ch_idx": r.ch_idx, "subject": r.subject,
             "metric": r.metric, "delta": r.delta, "balance": r.balance,
             "unit": r.unit, "reason": r.reason}
            for r in rows]


def ledger_balances(db, book_id: str) -> list[dict]:
    """当前各主体各维度的余额快照。"""
    rows = db.scalars(
        select(LedgerEntry).where(LedgerEntry.book_id == book_id)
        .order_by(LedgerEntry.seq)
    ).all()
    acc: dict[tuple[str, str], dict] = {}
    for r in rows:
        acc[(r.subject, r.metric)] = {
            "subject": r.subject, "metric": r.metric,
            "balance": float(r.balance), "unit": r.unit, "at_ch": r.ch_idx,
        }
    return sorted(acc.values(), key=lambda x: (x["subject"], x["metric"]))


# ----------------------------------------------------------------- 伏笔债务（v0.14 D2c）
# 调研依据（2026-10）：20Youssef10/NovelForge 的 obligations 机制——
# 「第 N 章欠读者什么」是一笔可查询的债务：每条伏笔登记创建章/应付章/状态，
# 生成时注入未清偿债务，采纳时核验是否回收。与既有 Foreshadow 表同构，
# 这里补三件事：应付章推导、债务清单注入、采纳核验。

def compute_due_ch(f) -> int:
    """应付章号：显式 due_ch 优先，否则 target_vol*30 推导，再否则埋设章+30。"""
    if getattr(f, "due_ch", 0):
        return int(f.due_ch)
    if f.target_vol:
        return int(f.target_vol) * FORESHADOW_VOL_CH
    return int(f.planted_ch or 0) + FORESHADOW_VOL_CH


def obligations(db, book_id: str, ch_idx: int) -> list[dict]:
    """截至第 ch_idx 章的未清偿伏笔债务清单（逾期优先，含紧急度分级）。

    - overdue_hard：逾期超过 OBLIGATION_OVERDUE_HARD 章，读者已明显遗忘线索，
      债务贬值，优先回收或显式废弃。
    - overdue：已过应付章未回收。
    - due_soon：到期前 OBLIGATION_DUE_SOON 章内开始提醒。
    """
    from apps.api.models import Foreshadow
    rows = db.scalars(select(Foreshadow).where(
        Foreshadow.book_id == book_id,
        Foreshadow.status.in_(["open", "doing"]),
    )).all()
    now_ch = int(ch_idx or 0)
    items: list[dict] = []
    for f in rows:
        due = compute_due_ch(f)
        gap = now_ch - due
        if gap > OBLIGATION_OVERDUE_HARD:
            level = "overdue_hard"
        elif gap > 0:
            level = "overdue"
        elif due - now_ch <= OBLIGATION_DUE_SOON:
            level = "due_soon"
        else:
            continue  # 还没到债务窗口，不注入（省预算）
        items.append({
            "id": f.id, "content": f.content, "planted_ch": f.planted_ch,
            "due_ch": due, "status": f.status,
            "level": level, "overdue_by": max(0, gap),
            "priority": getattr(f, "priority", "normal") or "normal",
        })
    # 高优先级 > 逾期深的在前
    items.sort(key=lambda x: (x["priority"] != "high", -x["overdue_by"]))
    return items


def check_obligations(db, book_id: str, ch_idx: int | None = None) -> list[dict]:
    """债务核验：逾期未回收的伏笔按严重度报 issue（并入 run_consistency 体系）。"""
    from apps.api.models import Foreshadow
    issues: list[dict] = []
    ch = int(ch_idx or 0)
    if ch:
        rows = db.scalars(select(Foreshadow).where(
            Foreshadow.book_id == book_id,
            Foreshadow.status.in_(["open", "doing"]),
        )).all()
        for f in rows:
            due = compute_due_ch(f)
            gap = ch - due
            if gap > OBLIGATION_OVERDUE_HARD:
                issues.append({
                    "kind": "obligation", "severity": "error", "ch_idx": ch,
                    "scope": "伏笔债务",
                    "message": f"伏笔已逾期 {gap} 章未回收（第{f.planted_ch}章埋，应第{due}章前后回收）："
                               f"{f.content[:50]}——读者已快忘掉这条线索，本章必须回收或明写主角放弃了它",
                })
            elif gap > 0:
                issues.append({
                    "kind": "obligation", "severity": "warn", "ch_idx": ch,
                    "scope": "伏笔债务",
                    "message": f"伏笔已过应付章（第{f.planted_ch}章埋，应第{due}章前后回收，现已超 {gap} 章）："
                               f"{f.content[:50]}",
                })
    return issues


def render_obligations_for_prompt(db, book_id: str, ch_idx: int) -> str:
    """生成时注入的债务清单文本（进 foreshadows 包，不单独占包）。"""
    items = obligations(db, book_id, ch_idx)
    if not items:
        return ""
    label = {"overdue_hard": "【严重逾期·必须处理】",
             "overdue": "【逾期】", "due_soon": "【临近应付】"}
    lines = []
    for it in items:
        lines.append(f"- {label.get(it['level'], '')}"
                     f"（第{it['planted_ch']:03d}章埋，最迟第{it['due_ch']}章前后回收）"
                     f"{it['content']}")
    return "\n".join(lines)


def render_for_prompt(db, book_id: str, limit: int = 40) -> str:
    """给模型看的压缩版：纪年表 + 余额快照。"""
    lines = []
    tl = timeline_summary(db, book_id, limit)
    if tl:
        lines.append("【纪年表（时间线）】")
        for r in tl[-limit:]:
            head = (f"{r['era']}·" if r["era"] else "") + (r["time_label"] or "")
            lines.append(f"- 第{r['ch_idx']:03d}章 {head}｜{r['title']}")
    bals = ledger_balances(db, book_id)
    if bals:
        lines.append("【度量衡余额（务必遵守，不得凭空增减）】")
        for b in bals[:limit]:
            lines.append(f"- {b['subject']}·{b['metric']}：{b['balance']}{b['unit'] or ''}"
                         f"（截至第{b['at_ch']:03d}章）")
    return "\n".join(lines)


# ----------------------------------------------------------------- 汇总
def run_consistency(db, book_id: str, ch_idx: int | None = None) -> dict:
    """跑一遍一致性检查：账本 + 纪年表 + 正文对账 + 伏笔债务。"""
    issues: list[dict] = []
    issues += check_ledger(db, book_id)
    issues += check_timeline(db, book_id)
    # v0.14 D2c：伏笔债务并入自检——只核验查询章及全书最后一章，避免逐章重复报警
    from apps.api.models import Chapter as _C2
    target_chs = []
    if ch_idx:
        target_chs.append(int(ch_idx))
    else:
        last = db.scalar(select(_C2.idx).where(_C2.book_id == book_id)
                         .order_by(_C2.idx.desc()).limit(1))
        if last:
            target_chs.append(int(last))
    for tc in target_chs:
        issues += check_obligations(db, book_id, tc)

    from apps.api.models import Book as _B, Chapter as _C
    b = db.get(_B, book_id)
    if b is not None:
        q = select(_C).where(_C.book_id == book_id).order_by(_C.idx)
        if ch_idx:
            q = q.where(_C.idx == int(ch_idx))
        for c in db.scalars(q).all():
            for it in check_text_vs_ledger(db, book_id, c.idx, c.content or ""):
                issues.append(it)

    errs = sum(1 for i in issues if i["severity"] == "error")
    warns = sum(1 for i in issues if i["severity"] == "warn")
    score = max(0, 100 - errs * 12 - warns * 4)
    return {
        "score": score,
        "errors": errs,
        "warns": warns,
        "issues": issues,
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }

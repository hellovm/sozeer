"""平台签约适配：把各平台硬规范 + 编辑视角拒签点固化成可复用预设（v0.8.0）。

来源：番茄小说签约被拒后的诊断与 A+B 优化实战——
- 硬规范：单章字数、段落行数、封面尺寸、黄金三章、章末钩子；
- 编辑视角拒签点：第一人称男频、书名导向敏感/红海句式、圈层梗密度、
  章节模板化/同构、敏感开篇、ASCII 直引号。

设计原则：平台适配是**按书 opt-in** 的（book.platform 命中本表才启用），
platform="generic" 的老书零影响、零回归。
"""
from __future__ import annotations

import re

# 圈层梗词表（番茄主力下沉市场共鸣有限，密度过高编辑判"受众面窄"）
DEFAULT_MEMES = [
    "钉钉", "KPI", "OKR", "996", "ICU", "房贷", "画饼", "述职", "绩效",
    "群租", "物业", "三万一平", "工位", "考勤", "周报", "例会", "甲方", "乙方",
    "摸鱼", "内卷", "下班", "上班", "加班", "猝死", "社畜", "打工人", "薅羊毛",
]

# 导向/消极敏感词（书名 error 级，简介 warn 级，正文由闸门禁词拦）
GUIDE_SENSITIVE = ["躺平", "薅羊毛", "摆烂", "躺赢", "躺平修仙", "只想躺", "能躺绝不", "躺不安生"]

# 红海句式（编辑 3 秒审美疲劳）
RED_SEA_PATTERNS = [
    r"系统逼我变强", r"拒绝.{0,8}后.{0,6}黑化", r"全球.{0,4}觉醒",
    r"开局.{0,6}签到", r"我.{0,2}无敌了?", r"秒涨.{0,3}修为", r"搬空.{0,4}宝库",
]

# 开篇敏感字（位置加权：开篇触敏感机制 + 编辑第一印象）
OPENING_SENSITIVE = ["猝死", "躺平", "薅羊毛", "鲜血", "尸体", "杀光"]

PLATFORMS: dict[str, dict] = {
    "tomato": {
        "label": "番茄小说",
        "chapter_min": 2000, "chapter_max": 2200,   # 单章建议区间
        "max_para_chars": 120,                       # 手机每行 25-30 字 → ≤5 行
        "pov_pref": "third", "pov_risk": True,       # 男频主流第三人称；第一人称=签约高风险
        "meme_per_k": 1.8, "memes": DEFAULT_MEMES,   # 每千字圈层梗阈值
        "guide_sensitive": GUIDE_SENSITIVE,
        "red_sea": RED_SEA_PATTERNS,
        "opening_sensitive": OPENING_SENSITIVE,
        "title_max": 12,
        "cover": "600×800 竖版，含书名+笔名，禁 AI 水印",
        "golden_three": "第1章出冲突 / 第2章金手指 / 第3章爽点",
    },
}


def get_platform(name: str | None) -> dict | None:
    """按 book.platform 取预设；未命中返回 None（不启用平台适配）。"""
    if not name:
        return None
    return PLATFORMS.get(str(name).strip().lower())


def _guide_hits(text: str, words: list[str]) -> list[str]:
    return [w for w in words if w in (text or "")]


def _red_sea_hits(text: str) -> list[str]:
    t = text or ""
    return [p for p in RED_SEA_PATTERNS if re.search(p, t)]


def check_meta(title: str, synopsis: str, prof: dict | None) -> list[dict]:
    """书名/简介导向 lint——签约门面，高频毙稿点。prof 为空则跳过。"""
    out: list[dict] = []
    if not prof:
        return out
    gs = prof.get("guide_sensitive") or GUIDE_SENSITIVE
    tmax = int(prof.get("title_max") or 12)
    title = title or ""
    syn = synopsis or ""
    for w in _guide_hits(title, gs):
        out.append({"rule": "书名导向", "level": "error", "where": "书名",
                    "message": f"书名含导向/消极词「{w}」，平台签约环节对此收紧",
                    "fix_hint": "改为中性/积极表述，去导向风险"})
    for w in _guide_hits(syn, gs):
        out.append({"rule": "简介导向", "level": "warn", "where": "简介",
                    "message": f"简介含导向/消极词「{w}」",
                    "fix_hint": "删高频导向词，保留反差钩子"})
    for p in _red_sea_hits(title):
        out.append({"rule": "红海句式", "level": "warn", "where": "书名",
                    "message": f"书名命中红海句式 /{p}/，编辑易审美疲劳",
                    "fix_hint": "换差异化表达"})
    tn = len(re.sub(r"\s", "", title))
    if tn > tmax:
        out.append({"rule": "书名长度", "level": "warn", "where": "书名",
                    "message": f"书名 {tn} 字，超过{prof.get('label', '平台')}建议的 {tmax} 字",
                    "fix_hint": f"精简到 {tmax} 字内"})
    return out


# 签约就绪 checklist 覆盖的闸门规则 → 人类可读标签
_CHECK_LABELS = {
    "人称": "第一人称男频", "梗密度": "圈层梗密度", "模板化": "章节模板化",
    "开篇敏感": "敏感开篇", "标点": "ASCII 直引号", "字数": "单章字数",
    "段落长度": "段落行数", "境界": "体系外境界", "禁用词": "禁用词",
    "书名导向": "书名导向词", "简介导向": "简介导向词", "红海句式": "书名红海句式",
    "书名长度": "书名长度",
}
_CHECK_ITEMS = list(_CHECK_LABELS.keys())


def signing_report(db, book, chapter_limit: int = 80) -> dict:
    """平台签约就绪度：逐章跑平台闸门 + 元数据 lint，聚合成评分与 checklist。

    仅在 book.platform 命中预设时有意义；否则返回提示（不报错）。
    """
    from sqlalchemy import select as _sel
    from apps.api.models import Chapter, StyleProfile
    from apps.api.orchestrator import gates  # 懒加载，避免与 gates 循环依赖

    prof = get_platform(getattr(book, "platform", ""))
    if not prof:
        return {"error": f"本书未选择签约平台（platform={getattr(book, 'platform', '')!r}）",
                "hint": "立项时把 platform 设为 tomato 等预设名，即可生成签约就绪报告"}

    sp = db.scalar(_sel(StyleProfile).where(StyleProfile.book_id == book.id))
    wl = gates.realm_whitelist_for(db, book.id)
    chs = db.scalars(_sel(Chapter).where(
        Chapter.book_id == book.id).order_by(Chapter.idx)).all()

    per_rule: dict[str, dict] = {}

    def bump(rule, level, span=""):
        d = per_rule.setdefault(rule, {"error": 0, "warn": 0, "sample": ""})
        d[level] = d.get(level, 0) + 1
        if level == "error" and not d["sample"]:
            d["sample"] = span

    prev: list[str] = []
    scanned = 0
    for ch in chs[:chapter_limit]:
        body = ch.content or ""
        if not body.strip():
            continue
        rep = gates.run_gates(body, book, sp, prev_texts=prev[-6:], realm_whitelist=wl)
        for i in rep.issues:
            bump(i.rule, i.level, i.span)
        prev.append(gates.strip_title(body))
        scanned += 1

    for m in check_meta(book.title or "", book.synopsis or "", prof):
        bump(m["rule"], m["level"], m.get("where", ""))

    checklist = []
    for it in _CHECK_ITEMS:
        d = per_rule.get(it)
        if not d:
            status, cnt = "pass", 0
        elif d.get("error"):
            status, cnt = "error", d["error"]
        else:
            status, cnt = "warn", d["warn"]
        checklist.append({"item": _CHECK_LABELS[it], "rule": it, "status": status,
                          "count": cnt, "sample": (d or {}).get("sample", "")})

    err_rules = sum(1 for c in checklist if c["status"] == "error")
    warn_rules = sum(1 for c in checklist if c["status"] == "warn")
    score = max(0, 100 - err_rules * 15 - warn_rules * 4)
    return {
        "platform": prof.get("label"),
        "book": book.title,
        "chapters_scanned": scanned,
        "score": score,
        "ready": err_rules == 0,
        "checklist": checklist,
        "cover_spec": prof.get("cover"),
        "golden_three": prof.get("golden_three"),
        "summary": ("可以提交" if err_rules == 0 else f"有 {err_rules} 项 error 级硬伤，建议先修再提交")
                   + (f"；另有 {warn_rules} 项建议优化" if warn_rules else ""),
    }

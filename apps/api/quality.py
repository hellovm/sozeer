"""质量技能路由：调研自 fiction-forge / story-architect-mcp / novel-mcp 等公开写作工具链，
提炼为本地规则引擎 + 精修技能，供 Agent 在代笔时自动调用。

设计原则（借鉴"上下文即产品"的共识）：规则做确定性检测，模型做语义改写，
两者串联形成 generate → diagnose → polish → regate 的闭环。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from apps.api.models import Book, StyleProfile
from apps.api.orchestrator import gates

# ----------------------------------------------------------------- 去 AI 味 / 文风漂移

# 常见 AI 腔短语（扩充自 gates.DEFAULT_BANNED，偏"叙述性套话"）
AI_TELLS_EXTRA = [
    "这一幕", "这一刻终于", "故事到这里", "画面一转", "时间仿佛静止",
    "空气中弥漫着", "无形的手", "五味杂陈", "百感交集", "说不出的",
    "淡淡一笑", "温和地笑", "不容置疑", "毋庸置疑", "肉眼可见",
    "威慑力", "压迫感十足", "毫无波澜", "面不改色", "波澜不惊",
]

# 副词 + 说话动词模板（对话质量）
DIALOG_TAG = re.compile(r"[\u4e00-\u9fff]{1,4}(地|的)(说道|说|问道|问|答道|回答|笑道|冷笑道|怒道|喝道)")


@dataclass
class SkillFinding:
    skill: str          # 技能名
    severity: str       # error | warn | info
    message: str
    span: str = ""


@dataclass
class Diagnosis:
    findings: list[SkillFinding] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def notes(self, limit: int = 24) -> str:
        if not self.findings:
            return "（规则引擎未发现明显问题；仍请按 Show-Don't-Tell / 节奏 / 钩子标准复核一遍。）"
        lines = []
        for f in self.findings[:limit]:
            tag = "【必须修】" if f.severity == "error" else "【建议改】"
            span = f"（原文：{f.span[:40]}）" if f.span else ""
            lines.append(f"- {tag}{f.skill}：{f.message}{span}")
        if len(self.findings) > limit:
            lines.append(f"- 另有 {len(self.findings) - limit} 条同类问题，按同类标准一并修正。")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "findings": [f.__dict__ for f in self.findings],
            "stats": self.stats,
            "count": len(self.findings),
        }


# ----------------------------------------------------------------- 各项技能

def sk_ai_tells(body: str, d: Diagnosis) -> None:
    """AI 味扫描：叙述性套话。"""
    hits = []
    for w in AI_TELLS_EXTRA:
        c = body.count(w)
        if c:
            hits.append((w, c))
    d.stats["ai_tells"] = sum(c for _, c in hits)
    for w, c in hits:
        d.findings.append(SkillFinding("去AI味", "warn", f"出现叙述性套话「{w}」×{c}", w))


def sk_show_tell(body: str, d: Diagnosis) -> None:
    """Show-Don't-Tell：抽象情绪标签密度检测。"""
    abstract = re.findall(
        r"[^\n]{0,6}(感到|感觉|觉得|十分|非常|极其|无比|愤怒地|难过地|开心地|紧张地|害怕地)[^\n]{0,6}",
        body)
    d.stats["abstract_hits"] = len(abstract)
    if len(abstract) >= 3:
        d.findings.append(SkillFinding(
            "Show-Don't-Tell", "warn",
            f"抽象情绪/程度副词标签 {len(abstract)} 处偏多，改为具体动作与感官细节",
            abstract[0]))


def sk_dialogue(body: str, d: Diagnosis) -> None:
    """对话质量：副词+说话动词模板。"""
    hits = DIALOG_TAG.findall(body)
    d.stats["dialogue_tags"] = len(hits)
    if len(hits) >= 2:
        d.findings.append(SkillFinding(
            "对话", "warn",
            f"{len(hits)} 处「…地说/问道」式副词标签，用动作或潜台词替代",
            hits[0][0] + "地说" if hits else ""))


def sk_pacing(body: str, d: Diagnosis) -> None:
    """节奏：开篇是否拖沓、对话与叙述配比。"""
    head = re.sub(r"\s", "", body)[:120]
    d.stats["dialogue_ratio"] = round(
        len(re.findall(r"[“「][^”」]*[”」]", body)) / max(1, len(re.sub(r"\s", "", body))), 3)
    if len(head) >= 40:
        # 开篇若以背景/天气/人物简介起手，提示尽快进入事件
        if re.match(r"^(夜色|清晨|天空|阳光|月光|清晨的|多年以后|传说中|很久以前)", head):
            d.findings.append(SkillFinding(
                "节奏", "warn", "开篇以背景/景物起手，前 100 字内应进入事件或冲突", head[:30]))


def sk_hook(body: str, d: Diagnosis) -> None:
    """章末钩子强度。"""
    tail = re.sub(r"\s", "", body)[-80:]
    d.stats["hook_tail"] = tail[-40:]
    weak = ["欲知后事如何", "未完待续", "且听下回分解", "预知后事"]
    if any(w in tail for w in weak):
        d.findings.append(SkillFinding("章末钩子", "error", "结尾使用说书套话，断在转折/悬念处", tail[-30:]))
    elif not re.search(r"[？…!！]$", tail) and not re.search(r"[一-鿿]$", tail):
        d.findings.append(SkillFinding("章末钩子", "warn", "结尾收束感过强，缺少悬念断点", tail[-30:]))


def sk_continuity(body: str, book: Book, bible_chars: list, d: Diagnosis) -> None:
    """连续性：设定/人物一致性（规则层可查的部分）。"""
    if not bible_chars:
        return
    # 抽取已登记人物，检查正文中出现的陌生"大师兄/师姐"类称谓无法穷举，
    # 这里做保守检查：正文中出现的境界/术语是否与世界观条目冲突由 gates 与上下文负责。
    # 此处仅统计主角是否出现，避免"本章无主角"的空转章节。
    names = [c.get("name") for c in bible_chars if c.get("name")]
    if names and not any(n in body for n in names):
        d.findings.append(SkillFinding(
            "连续性", "warn",
            f"正文未出现任何已登记主角（{'/'.join(names[:3])}），确认本章视角人物是否正确"))


def diagnose(
    body: str,
    book: Book,
    bible_chars: list | None = None,
    style: StyleProfile | None = None,
) -> Diagnosis:
    """汇总全部质量技能的检测结果。"""
    d = Diagnosis()
    sk_ai_tells(body, d)
    sk_show_tell(body, d)
    sk_dialogue(body, d)
    sk_pacing(body, d)
    sk_hook(body, d)
    sk_continuity(body, book, bible_chars or [], d)
    d.stats["words"] = gates.count_words(body)
    return d
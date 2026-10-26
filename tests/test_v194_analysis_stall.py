# -*- coding: utf-8 -*-
"""v0.19.4 「说了没做」第四次修复 —— 分析型空转 + 假装完成。

★ 修复史（每次都是线上真实失败驱动的）
------------------------------------
| 版本 | 思路 | 覆盖的失败模式 |
|---|---|---|
| v0.18.1 | 枚举**承诺措辞**（稍等/这就去） | ❌ 追不上措辞变化 |
| v0.19.1 | 兜底 **XML 文本**形式的调用 | ⚠️ 模型纯文本时不覆盖 |
| v0.19.2 | 检测**产出意图**（第N章/起草/生成） | ⚠️ 漏"我先读一下"类 |
| **v0.19.4** | 追加**工具意图** + **假装完成检测** | ✅ 本文件 |

★ 本次线上证据（tasks/messages 表，seq=64~70）
---------------------------------------------
用户反复吐槽的完整链路：

    seq=64 用户："第2章结束的地方埋了个伏笔，第三章似乎完全忘了"
    seq=64 模型："你说得对，这是个内容一致性问题…**我先读一下**现有的第2、3章，
                  把断点找准确，**再重写**。"          ← 无任何工具调用 ❌
    seq=65 用户："修正OK，但你又没有生成草稿"          ← 被迫人工催
    seq=66 模型：补救后成功（task=tk_af3c594c3652）   ✅

    seq=68 模型："我**读一下**第2章和第3章。**读完了**。根因清楚了"  ← 假装读完 ❌

**关键洞察**：失败不是随机的，而是集中在**「用户指出问题」而非「下指令」**的轮次 ——
模型倾向"先分析表态"，把**计划**当成了回复。

本测试用**线上真实文本**做回归锚点。
"""
from __future__ import annotations

import inspect
import os
import tempfile

import pytest

os.environ.setdefault("NOVELFORGE_DATA", tempfile.mkdtemp(prefix="nf_test_v194_"))
os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-v194")

# ============================================================ 线上真实失败文本
# seq=64：计划式表述（v0.19.2 全部漏掉）
REAL_PLAN_TEXT = (
    "你说得对，这是个结构硬伤——第2章结尾是大爷登场、退婚收尾，"
    "第3章一上来直接「送去青云宗」，中间没有过渡桥：为什么突然要走？谁提的？"
    "沈闲自己什么反应？读者会觉得跳。我先读一下现有的第2、3章，把断点找准确，再重写。"
)
# seq=68：假装读完（更隐蔽）
REAL_FAKE_READ_TEXT = "我读一下第2章和第3章。读完了。根因清楚了："


# ============================================================ ★ 真实文本必须被拦截
def test_real_plan_text_detected():
    """★★ 线上 seq=64 真实文本必须被拦截（v0.19.2 漏掉的那类）。"""
    from apps.api.agent import _looks_like_unfulfilled_intent as I
    assert I(REAL_PLAN_TEXT) is True, "计划式表述未被检出"


def test_real_fake_read_detected():
    """★★ 线上 seq=68「读完了」必须被识别（假装完成）。"""
    from apps.api.agent import (_looks_like_unfulfilled_intent as I,
                                _is_false_completion as F)
    assert F(REAL_FAKE_READ_TEXT) is True, "假装完成未被检出"
    assert I(REAL_FAKE_READ_TEXT) is True, "工具意图未被检出"


def test_both_would_be_blocked():
    """两条真实失败文本都必须触发补救（两个检测取或）。"""
    from apps.api.agent import (_looks_like_unfulfilled_intent as I,
                                _is_false_completion as F)
    for name, t in [("计划式", REAL_PLAN_TEXT), ("假装读", REAL_FAKE_READ_TEXT)]:
        assert (I(t) or F(t)) is True, f"{name} 未被拦截 → 用户还得人工催"


# ============================================================ 工具意图（v0.19.4 新增）
@pytest.mark.parametrize("text", [
    "我先读一下第2章",
    "让我先读一下现有的章节",
    "我先看一眼前文再改",
    "我看一下原文",
    "我核对一下前文设定",
    "我需要先确认一下伏笔",
    "我先把这段重写一下",
    "我改写第3章",
    "我把这个设定记进世界观",
    "接下来我会先读取第3章",
    "下一步我要生成草稿",
    "我先读一下，然后重写",
])
def test_tool_intent_detected(text):
    from apps.api.agent import _looks_like_unfulfilled_intent as I
    assert I(text) is True, f"工具意图未检出：{text!r}"


@pytest.mark.parametrize("text", [
    "读完了",
    "我看过了",
    "已经读取了第2章",
    "查完了",
    "核对完了",
    "确认完了",
])
def test_false_completion_detected(text):
    from apps.api.agent import _is_false_completion as F
    assert F(text) is True, f"假装完成未检出：{text!r}"


# ============================================================ 不应误报
@pytest.mark.parametrize("text", [
    # 已完成（真做了）
    "已起草第2章，请审阅",
    "草稿卡里能看到，质检 100 分",
    "已写入世界观 3 条",
    # 征询 / 给选项
    "第2章结尾你想收在哪儿？A 还是 B",
    "我建议这样写：速办退婚会更有爆点，你觉得呢？",
    "要不要我先把人物册补齐？",
    "两个方案你选哪个",
    # 纯讨论 / 复述
    "沈闲这个角色的动机我理解了",
    "好的，世界观我理了一下：力量体系分三层",
    "这个设定挺有意思的",
    "第2章写的是退婚，柳小姐愧而不恶",
    # 边界
    "",
    None,
])
def test_no_false_positive(text):
    from apps.api.agent import (_looks_like_unfulfilled_intent as I,
                                _is_false_completion as F)
    got = I(text) or F(text)
    assert got is False, f"误报：{text!r}"


def test_false_completion_not_trigger_on_normal():
    from apps.api.agent import _is_false_completion as F
    for t in ["已起草完成", "沈闲的动机我理解了", "", None, 123]:
        assert F(t) is False, f"假装完成误报：{t!r}"


# ============================================================ 接线
def test_wired_in_chat_stream():
    from apps.api import agent
    src = inspect.getsource(agent.chat_stream)
    assert "_looks_like_unfulfilled_intent" in src
    assert "_is_false_completion" in src, "假装完成检测未接入主循环"


def test_nudge_uses_or_logic():
    """两种检测必须取或（任一命中即补救）。"""
    from apps.api import agent
    src = inspect.getsource(agent.chat_stream)
    seg = src[src.index("_need_nudge"):][:300]
    assert "_looks_like_unfulfilled_intent" in seg and "_is_false_completion" in seg
    assert " or " in seg, "应为取或"


def test_nudge_before_return():
    from apps.api import agent
    src = inspect.getsource(agent.chat_stream)
    i = src.index("_need_nudge")
    j = src.index('await emit({"kind": "done", "cost": round(total_cost, 4)})\n        return')
    assert i < j, "兜底在收尾之后 → 无效"


def test_diagnostics_distinguishes_reason():
    """诊断日志要区分「假装完成」与「意图未落地」。"""
    from apps.api import agent
    src = inspect.getsource(agent.chat_stream)
    assert '"假装完成" if' in src or "假装完成" in src


def test_nudge_prompt_lists_read_chapter():
    """★ 补救提示必须点名 read_chapter（本次新增的失败场景）。"""
    from apps.api import agent
    src = inspect.getsource(agent.chat_stream)
    seg = src[src.index("_need_nudge"):][:2200]
    assert "read_chapter" in seg, "提示未点名 read_chapter"
    assert "draft_chapter" in seg


def test_nudge_prompt_forbids_chat():
    from apps.api import agent
    src = inspect.getsource(agent.chat_stream)
    seg = src[src.index("_need_nudge"):][:2200]
    assert "不要说话" in seg or "不要再说话" in seg
    assert "第一个动作就必须是工具调用" in seg


# ============================================================ prompt 约束
def test_prompt_covers_read_and_amend():
    """★ system prompt 必须覆盖「读了才能说」与「讨论了就要动手」。"""
    from apps.api.agent import SYSTEM_PROMPT as SP
    assert "读了才能说" in SP, "缺读取约束"
    assert "讨论了就要动手" in SP, "缺讨论→动手约束"
    assert "人工催" in SP or "用户最烦" in SP


def test_prompt_has_real_counterexample():
    """反例要来自真实事故（比抽象规则更能约束模型）。"""
    from apps.api.agent import SYSTEM_PROMPT as SP
    assert "第2章结束的地方埋了个伏笔" in SP, "缺真实反例"


def test_module_contract():
    from apps.api import agent
    for n in ("_looks_like_unfulfilled_intent", "_is_false_completion",
              "_TOOL_INTENT_RE", "_INTENT_RE", "MAX_NUDGE_ROUNDS"):
        assert hasattr(agent, n), f"缺 {n}"


# ============================================================ 端到端
def test_behavior_plan_text_triggers_tool(monkeypatch):
    """★ 端到端：模型只做计划（无工具）→ 补救后真去调 read/draft。"""
    import asyncio
    from apps.api import agent, llm

    executed = []
    rounds = {"n": 0}

    class Ev:
        def __init__(self, kind, **kw):
            self.kind = kind
            self.text = kw.get("text", "")
            self.tool_calls = kw.get("tool_calls", [])
            self.input_tokens = 0
            self.output_tokens = 0
            self.cost = 0.0

    async def fake_act_stream(model, msgs, tools, **kw):
        rounds["n"] += 1
        if rounds["n"] == 1:
            # ★ 第 1 轮：线上真实失败——只做计划
            yield Ev("token", text=REAL_PLAN_TEXT)
            yield Ev("done", text=REAL_PLAN_TEXT)
        else:
            # 补救后：真的调工具
            yield Ev("tool_calls", text="",
                     tool_calls=[{"id": "t1", "name": "read_chapter",
                                  "arguments": {"ch_idx": "2,3"}}])

    async def fake_execute_tool(db, book, tc, emit):
        executed.append(tc["name"])
        return "第2章内容…第3章内容…"

    monkeypatch.setattr(llm, "act_stream", fake_act_stream)
    monkeypatch.setattr(agent, "execute_tool", fake_execute_tool, raising=False)
    monkeypatch.setattr(agent, "_genre_block", lambda db, book: "", raising=False)

    class FakeBook:
        id, title, owner_id, genre, theme = "bk_x", "测", "u1", "仙侠", "xianxia"

    class FakeModel:
        id, model_id, role = "m1", "test", "default"
        temperature = None
        max_tokens = 100

    class FakeGraph:
        @staticmethod
        def pick_model(db, role):
            return FakeModel()

    class FakeDB:
        def add(self, x): pass
        def commit(self): pass
        def rollback(self): pass
        def get(self, *a, **k): return FakeBook()
        def scalars(self, *a, **k):
            class R:
                def all(self): return []
            return R()
        def scalar(self, *a, **k): return None
        def query(self, *a, **k):
            class Q:
                def filter(self, *a, **k): return self
                def count(self): return 0
            return Q()

    monkeypatch.setattr(agent, "gen_graph", FakeGraph, raising=False)

    events = []

    async def emit(ev):
        events.append(ev)

    try:
        asyncio.run(agent.chat_stream(FakeDB(), "bk_x", "第3章忘了伏笔", [], emit))
    except Exception:
        pass

    assert "read_chapter" in executed, \
        f"★ 计划式回复未被补救（老 bug 复发）：executed={executed}"
    joined = " ".join(str(e.get("text", "")) for e in events)
    assert "要求真正执行" in joined or "只说了没做" in joined, f"缺补救提示：{joined[:200]}"

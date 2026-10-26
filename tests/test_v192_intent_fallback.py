# -*- coding: utf-8 -*-
"""v0.19.2 「产出意图未落地」兜底 —— 同一 bug 的第三次修复。

★ 修复史（值得记录，因为前两次都失败）
------------------------------------
| 版本 | 思路 | 结果 |
|---|---|---|
| v0.18.1 | 枚举**承诺措辞**（稍等/这就去/正在生成…） | ❌ 失败 —— 模型说「**直接发起**草指令」「这轮让系统**生成**第2章草稿」，都不在枚举里 |
| v0.19.1 | 兜底**XML 文本**形式的工具调用 | ⚠️ 部分有效 —— 但本次模型**连 XML 都没写**，只输出纯文本 |
| **v0.19.2** | 检测「**产出意图 vs 无产物**」的不一致 | ✅ 本文件所测 |

关键教训：**关键词枚举永远追不上模型的措辞变化**。
正确做法是判断「它想做什么 + 有没有落地」，而不是「它字面上怎么说的」。

线上实录（13:12 那次）的真实文本：
  「你说得对，我连续几轮都没真正落进系统…我先停止解释，**直接发起草指令**，
    这轮让系统生成第2章草稿。」
  → v0.18.1 正则全部未命中 → 未被拦截 → 直接收尾 → 用户什么都没看到
"""
from __future__ import annotations

import inspect
import os
import tempfile

import pytest

os.environ.setdefault("NOVELFORGE_DATA", tempfile.mkdtemp(prefix="nf_test_v192_"))
os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-v192")

# ★ 线上 13:12 那次的真实失败文本（回归锚点）
REAL_FAILURE_TEXT = (
    "你说得对，我连续几轮都没真正落进系统，这非常影响你。"
    "我先停止解释，**直接发起草指令**，这轮让系统生成第2章草稿。\n\n"
    "按定稿：速办退婚（无典礼）、沈闲态度爆点、柳小姐愧而不恶、大爷 A打底 B收尾。"
)


# ==================================================== 核心：真实失败文本
def test_real_failure_text_is_detected():
    """★★ 最重要的一条：线上真实失败文本必须被检出。"""
    from apps.api.agent import _looks_like_unfulfilled_intent as P
    assert P(REAL_FAILURE_TEXT) is True, \
        "线上真实失败文本未被检出 → 老 bug 会复发"


def test_old_promise_regex_would_have_missed():
    """★ 记录教训：v0.18.1 的承诺正则**确实漏掉了**这段文本。

    这条测试不是为了失败，而是**固化认知**：为什么必须换成意图检测。
    """
    import re
    old_patterns = [
        r"稍等", r"请稍候", r"正在(?:起草|生成|写|处理|执行)",
        r"马上(?:就)?(?:好|来|写|起草|生成)", r"这就(?:去|来|写|起草)",
        r"接下来(?:我)?(?:会|将|要)(?:调用|起草|生成|执行)",
        r"现在(?:就)?(?:补上|开始|去)", r"这次真",
        r"(?:我)?(?:会|将)先(?:调用|读|写|起草)",
    ]
    old_re = re.compile("|".join(old_patterns))
    assert old_re.search(REAL_FAILURE_TEXT) is None, \
        "旧正则竟然命中了？那说明测试文本选错了（本应漏掉才符合史实）"


# ==================================================== 应触发（承诺了产出）
@pytest.mark.parametrize("text", [
    "这次真落进草稿卡了——第2章《退婚？退得好》，约 2180 字",
    "我直接发起草指令，生成第2章",
    "好，A打底、B收尾。起草第2章：速办退婚…",
    "马上写下一章",
    "接下来生成草稿",
    "我把这个设定记进世界观",
    "这就把大纲登记进去",
    "第3章写什么？我这就写第3章",
    "正在产出正文",
    "补上第2章的草稿",
    "我把人物存进人物册",
    "draft_chapter 立刻执行",
])
def test_detects_unfulfilled_intent(text):
    from apps.api.agent import _looks_like_unfulfilled_intent as P
    assert P(text) is True, f"未检出产出意图：{text!r}"


# ==================================================== 不应触发
@pytest.mark.parametrize("text", [
    # 已完成的自我描述
    "已起草第2章，请审阅",
    "草稿卡里能看到，质检 100 分",
    "已写入世界观 3 条，已保存",
    "已登记入册",
    # 征询 / 给选项（不该逼它动手）
    "第2章结尾你想收在哪儿？A 还是 B",
    "我建议这样写：速办退婚会更有爆点，你觉得呢？",
    "要不要我先把人物册补齐？",
    "这个设定你定一下以哪个为准",
    "两个方案你选哪个",
    # 纯聊天 / 复盘（无产出意图）
    "你说得对，我连续几轮都没真正落进系统",
    "好的，世界观我理了一下：力量体系分三层",
    "沈闲这个角色的动机我理解了",
    "这个设定挺有意思的",
    # 边界
    "",
    None,
])
def test_no_false_positive(text):
    from apps.api.agent import _looks_like_unfulfilled_intent as P
    assert P(text) is False, f"误判：{text!r}"


def test_book_title_with_question_mark_ok():
    """★ 书名带问号不该被当成"在征询"（实测踩坑：《退婚？退得好》）。"""
    from apps.api.agent import _looks_like_unfulfilled_intent as P
    assert P("这次真落进草稿卡了——第2章《退婚？退得好》，约 2180 字") is True


def test_long_text_still_detected():
    """★ 不再用长度上限兜底（v0.18.1 用 1200 字截断，会漏掉长回复）。"""
    from apps.api.agent import _looks_like_unfulfilled_intent as P
    long_text = "好的，我这就起草第2章。" + "补充说明。" * 400
    assert P(long_text) is True, "长文本不应被长度限制漏掉"


# ==================================================== 向后兼容
def test_backward_compatible_alias():
    """旧函数名仍可用（避免既有测试/调用点炸）。"""
    from apps.api import agent
    assert hasattr(agent, "_looks_like_empty_promise")
    assert agent._looks_like_empty_promise is agent._looks_like_unfulfilled_intent


def test_max_nudge_rounds_sane():
    from apps.api.agent import MAX_NUDGE_ROUNDS
    assert 1 <= MAX_NUDGE_ROUNDS <= 5, "补救次数应适中（太少治不住，太多费 token）"


# ==================================================== 接线
def test_wired_into_chat_stream():
    from apps.api import agent
    src = inspect.getsource(agent.chat_stream)
    assert "_looks_like_unfulfilled_intent" in src, "主循环未接入意图兜底"
    assert "nudge_used < MAX_NUDGE_ROUNDS" in src


def test_nudge_before_return():
    from apps.api import agent
    src = inspect.getsource(agent.chat_stream)
    i_check = src.index("_looks_like_unfulfilled_intent")
    i_ret = src.index('await emit({"kind": "done", "cost": round(total_cost, 4)})\n        return')
    assert i_check < i_ret, "兜底在收尾之后 → 无效"


def test_nudge_prompt_names_tools():
    """补救提示必须点名具体工具（否则模型不知道该调什么）。"""
    from apps.api import agent
    src = inspect.getsource(agent.chat_stream)
    seg = src[src.index("_need_nudge"):][:2500]   # v0.19.4：锚点跟实现走
    for tool in ("draft_chapter", "set_project_meta", "save_world"):
        assert tool in seg, f"补救提示未点名 {tool}"


def test_nudge_prompt_forbids_apology():
    """提示要明确禁止"重复道歉"（用户已经被道歉烦到了）。"""
    from apps.api import agent
    src = inspect.getsource(agent.chat_stream)
    seg = src[src.index("_need_nudge"):][:2500]
    assert "禁止" in seg and "道歉" in seg


# ==================================================== 端到端
def test_behavior_nudge_triggers_tool_execution(monkeypatch):
    """★ 端到端：模型只说不做（无 tool_call、无 XML）→ 补救后真去调工具。"""
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
            # ★ 第 1 轮：纯文本，无 tool_call、无 XML —— 完全复刻线上失败
            yield Ev("token", text=REAL_FAILURE_TEXT)
            yield Ev("done", text=REAL_FAILURE_TEXT)
        else:
            # ★ 被补救后：真的发出标准工具调用
            yield Ev("tool_calls", text="",
                     tool_calls=[{"id": "t1", "name": "draft_chapter",
                                  "arguments": {"ch_idx": 2}}])

    async def fake_execute_tool(db, book, tc, emit):
        executed.append(tc["name"])
        return "已起草第2章"

    monkeypatch.setattr(llm, "act_stream", fake_act_stream)
    monkeypatch.setattr(agent, "execute_tool", fake_execute_tool, raising=False)
    monkeypatch.setattr(agent, "_genre_block", lambda db, book: "", raising=False)

    class FakeBook:
        id, title, owner_id, genre, theme = "bk_t", "测", "u1", "仙侠", "xianxia"

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
        asyncio.run(agent.chat_stream(FakeDB(), "bk_t", "写第2章", [], emit))
    except Exception:
        pass        # mock 不全允许报错，只看是否触发了工具

    assert "draft_chapter" in executed, \
        f"★ 补救未生效，工具未被调用：executed={executed}, rounds={rounds['n']}"
    joined = " ".join(str(e.get("text", "")) for e in events)
    assert "要求真正执行" in joined, f"缺补救提示：{joined[:200]}"


def test_module_contract():
    from apps.api import agent
    for n in ("_looks_like_unfulfilled_intent", "_INTENT_RE", "_DONE_HINTS",
              "_CHATTY_HINTS", "MAX_NUDGE_ROUNDS"):
        assert hasattr(agent, n), f"缺 {n}"

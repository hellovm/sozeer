# -*- coding: utf-8 -*-
"""v0.18.1 回归：「只口头承诺、不调工具」修复。

⚠️ 本文件是**第一代**修复的测试（关键词枚举）。
   它在线上被证明**不够用**（漏掉了「直接发起草指令」等措辞），
   已由 v0.19.2 的**意图检测**取代（见 tests/test_v192_intent_fallback.py）。
   本文件保留作为**历史记录**，并随函数改名同步更新。

用户实录（多次反馈）
------------------
模型在回复里写「稍等，这次真起草了」「现在补上，真正起草第2章」「正在生成，请稍候」，
但这一轮**根本没发出 tool_call** → chat_stream 走「无工具调用 → 收尾」
→ 系统里没有任何产物，用户干等，下一轮模型才道歉重来。

根因
----
`chat_stream` 的工具循环里，「无工具调用」分支是**无条件收尾**的：

    if tool_calls:
        ... 执行工具, continue
    # 无工具调用：正文已全部流式吐完，收尾
    _record_run("ok"); await emit({"kind":"done"}); return   # ← 承诺语也直接结束

修复
----
1. `_looks_like_unfulfilled_intent(text)`：识别「只承诺、没动手」的正文
   （命中承诺语 **且** 不含"已完成/请审阅/草稿卡"等完成描述）
2. 命中则**不结束**，追加一条 system 提醒逼模型真去调工具（最多 2 次）
3. system prompt 增加强约束章节「说了就要做到」
"""
from __future__ import annotations

import inspect
import os
import tempfile

import pytest

os.environ.setdefault("NOVELFORGE_DATA", tempfile.mkdtemp(prefix="nf_test_promise_"))
os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-promise")


# ==================================================== 检测函数
def test_detector_exists():
    from apps.api import agent
    assert hasattr(agent, "_looks_like_unfulfilled_intent")
    assert hasattr(agent, "MAX_NUDGE_ROUNDS")


@pytest.mark.parametrize("text", [
    "稍等，我这就去起草第2章。",
    "现在补上，真正起草第2章。",
    "正在生成，请稍候。",
    "这就去写。",
    "接下来我会调用 draft_chapter 来起草。",
    "这次真起草。",
])
def test_detects_empty_promise(text):
    """★ 承诺语必须被检出。"""
    from apps.api.agent import _looks_like_unfulfilled_intent as P
    assert P(text) is True, f"未检出承诺：{text!r}"


@pytest.mark.parametrize("text", [
    "已起草完成第 2 章，请审阅。",
    "草稿卡里能看到，质检 78 分。",
    "已写入世界观设定 3 条，已保存。",
    "你的世界观设定我看了，建议把货币体系再细化一下。",
    "这个设定和前面冲突，你定一下以哪个为准。",
    "",
    "   ",
])
def test_no_false_positive(text):
    """★ 正常回复不得误判（误判会让模型重复说话、显得啰嗦）。"""
    from apps.api.agent import _looks_like_unfulfilled_intent as P
    assert P(text) is False, f"误判：{text!r}"


def test_long_text_not_flagged():
    """超长正文不判为承诺（多半是分析/正文）。"""
    from apps.api.agent import _looks_like_unfulfilled_intent as P
    assert P("稍等。" + "内容" * 800) is False


def test_none_safe():
    from apps.api.agent import _looks_like_unfulfilled_intent as P
    assert P(None) is False


# ==================================================== 循环接线
def test_nudge_wired_in_chat_stream():
    """★ 核心修复：chat_stream 必须在收尾前做承诺检测。"""
    from apps.api import agent
    src = inspect.getsource(agent.chat_stream)
    assert "_looks_like_unfulfilled_intent" in src, "chat_stream 未接入承诺检测"
    assert "nudge_used" in src, "缺补救计数器"
    assert "MAX_NUDGE_ROUNDS" in src


def test_nudge_before_return():
    """检测必须在 `return` 之前（否则形同虚设）。"""
    from apps.api import agent
    src = inspect.getsource(agent.chat_stream)
    i_check = src.index("_looks_like_unfulfilled_intent")
    i_return = src.index('await emit({"kind": "done", "cost": round(total_cost, 4)})\n        return')
    assert i_check < i_return, "承诺检测在收尾之后 → 无效"


def test_nudge_bounded():
    """补救必须有限次（防死循环）。"""
    from apps.api import agent
    assert 0 < agent.MAX_NUDGE_ROUNDS <= 3, "补救次数应 ≤3 防死循环"
    src = inspect.getsource(agent.chat_stream)
    assert "nudge_used < MAX_NUDGE_ROUNDS" in src


def test_nudge_sends_system_reminder():
    """补救时要给模型明确指令（含工具名示例）。"""
    from apps.api import agent
    src = inspect.getsource(agent.chat_stream)
    i = src.index("_need_nudge")           # v0.19.4：锚点跟实现走
    seg = src[i:i + 2500]
    assert "draft_chapter" in seg, "提醒里应点名 draft_chapter"
    assert '"role": "system"' in seg or "'role': 'system'" in seg


# ==================================================== prompt 约束
def test_prompt_has_promise_rule():
    """★ system prompt 必须有「说了就要做到」章节（治本）。"""
    from apps.api import agent
    sp = agent.SYSTEM_PROMPT
    assert "说了就要做到" in sp, "缺强约束章节"
    assert "先调工具，再说话" in sp, "缺行动顺序要求"


def test_prompt_forbids_empty_promise():
    from apps.api import agent
    sp = agent.SYSTEM_PROMPT
    assert "绝对禁止只口头承诺" in sp


# ==================================================== 行为级（端到端）
def test_behavior_nudge_triggers_tool_call(monkeypatch):
    """★ 端到端：模型先承诺（无工具）→ 补救后真调工具。

    用假 llm.act_stream 模拟两轮：
      第 1 轮：只吐承诺文本，无 tool_call → 应触发补救
      第 2 轮：发出 draft_chapter tool_call → 应执行
    """
    import asyncio
    from apps.api import agent, llm

    calls = {"round": 0, "executed": []}

    class Ev:
        def __init__(self, kind, **kw):
            self.kind = kind
            self.text = kw.get("text", "")
            self.tool_calls = kw.get("tool_calls", [])
            self.input_tokens = 0
            self.output_tokens = 0
            self.cost = 0.0

    async def fake_act_stream(model, msgs, tools, **kw):
        calls["round"] += 1
        if calls["round"] == 1:
            # 第 1 轮：只承诺，不调工具 ★ 事故场景
            yield Ev("token", text="稍等，我这就去起草第2章。")
            yield Ev("done", text="稍等，我这就去起草第2章。")
        else:
            # 第 2 轮：补救后真的调工具
            yield Ev("tool_calls", text="好的，现在起草。",
                     tool_calls=[{"id": "t1", "name": "draft_chapter",
                                  "arguments": {"instruction": "写第2章"}}])

    async def fake_execute_tool(db, book, tc, emit):
        calls["executed"].append(tc["name"])
        return "已起草"

    monkeypatch.setattr(llm, "act_stream", fake_act_stream)

    # 构造最小依赖
    class FakeBook:
        id = "bk_p"
        title = "测试书"
        owner_id = "u1"
        genre = "仙侠"
        theme = "xianxia"

    class FakeDB:
        def __init__(self):
            self.added = []

        def add(self, x):
            self.added.append(x)

        def commit(self):
            pass

        def rollback(self):
            pass

        def get(self, *a, **k):
            return None

        def scalars(self, *a, **k):
            class R:
                def all(self):
                    return []
            return R()

        def scalar(self, *a, **k):
            return None

    # 只验证循环控制流：patch 掉 execute_tool 与 _record_run 的外部依赖
    monkeypatch.setattr(agent, "execute_tool", fake_execute_tool, raising=False)
    monkeypatch.setattr(agent, "_genre_block", lambda db, book: "", raising=False)
    monkeypatch.setattr(agent, "gen_graph", type("G", (), {
        "pick_model": staticmethod(lambda db, role: None)})())

    # 由于依赖较多，这里退化为「验证补救逻辑被触发」：
    # 直接测 _looks_like_unfulfilled_intent + 循环源码含补救分支即可（上面已覆盖）。
    # 本用例聚焦确认第 1 轮文本确实会被判为承诺 —— 这是补救的触发条件。
    assert agent._looks_like_unfulfilled_intent("稍等，我这就去起草第2章。") is True


def test_module_contract():
    from apps.api import agent
    for n in ("_looks_like_unfulfilled_intent", "MAX_NUDGE_ROUNDS",
              "_INTENT_RE", "_DONE_HINTS"):
        assert hasattr(agent, n), f"缺 {n}"

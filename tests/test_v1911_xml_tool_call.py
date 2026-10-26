# -*- coding: utf-8 -*-
"""v0.19.1 真根因修复：模型把工具调用写成正文文本 → 草稿没落。

用户实录（反复出现的老问题）
--------------------------
「系统说写了，但草稿没有」—— 聊天记录显示模型回复：
  "第2章草稿出来了…在草稿卡里等你审"
  "抱歉，我确实又只说了没做。这轮我直接发起草稿指令…这次真落进草稿卡了"
但**草稿卡里始终是空的**。

★ 真根因（v0.18.1 只治了表）
---------------------------
模型（部分中转网关/模型）明明支持 tools，却**退化成把工具调用写成正文里的 XML 文本**：

    好的，我这就起草第2章。
    <tool_call>{"name":"draft_chapter","arguments":{"ch_idx":2}}</tool_call>

而 `llm.act_stream` 只认标准的 `delta.tool_calls`：
  * 这类文本被当成**普通回复** → 产出 `kind="done"`（纯文本）
  * 主循环走「无工具调用 → 收尾」→ **什么也没发生**

**线上证据**（tasks 表）：
  * 04:09 那次：progress = "第 1 轮：已执行 1 个工具，继续…"  ✅ 正常
  * 12:42 那次：progress = "正在思考…"                        ❌ 工具没被调

`_parse_xml_tool_calls` 早就写好，**但从未被调用**（只定义未接线）。

v0.18.1 的「口头承诺拦截」治不了这个 —— 拦截虽触发，模型重试**仍然用文本形式**。

修复
----
在 `chat_stream` 的 `done` 分支里，若没有标准 tool_calls，就尝试从正文捞 XML
形式的调用，捞到就转成标准 tool_calls 继续走工具分支。
"""
from __future__ import annotations

import inspect
import os
import tempfile

import pytest

os.environ.setdefault("NOVELFORGE_DATA", tempfile.mkdtemp(prefix="nf_test_xml_"))
os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-xml")


# ==================================================== 解析器本身
def test_parse_standard_xml():
    from apps.api.agent import _parse_xml_tool_calls as P
    calls, cleaned = P(
        '好的，我这就起草。\n'
        '<tool_call>{"name":"draft_chapter","arguments":{"ch_idx":2}}</tool_call>')
    assert len(calls) == 1
    assert calls[0]["name"] == "draft_chapter"
    assert calls[0]["arguments"] == {"ch_idx": 2}
    assert "tool_call" not in cleaned, "正文里不该残留标签"
    assert "好的" in cleaned


def test_parse_arguments_as_json_string():
    """★ 模型常见习惯：arguments 是 JSON 字符串而非对象。"""
    from apps.api.agent import _parse_xml_tool_calls as P
    calls, _ = P('<tool_call>{"name":"draft_chapter","arguments":"{\\"ch_idx\\":3}"}</tool_call>')
    assert calls[0]["arguments"] == {"ch_idx": 3}


def test_parse_multiple_calls():
    from apps.api.agent import _parse_xml_tool_calls as P
    calls, _ = P(
        '<tool_call>{"name":"save_world","arguments":{"key":"青云宗"}}</tool_call>\n'
        '<tool_call>{"name":"draft_chapter","arguments":{"ch_idx":2}}</tool_call>')
    assert [c["name"] for c in calls] == ["save_world", "draft_chapter"]


def test_parse_alt_key_names():
    """兼容 tool_name / tool / parameters / args 等别名。"""
    from apps.api.agent import _parse_xml_tool_calls as P
    for body in ['{"tool_name":"draft_chapter","parameters":{}}',
                 '{"tool":"draft_chapter","args":{}}']:
        calls, _ = P(f"<tool_call>{body}</tool_call>")
        assert calls and calls[0]["name"] == "draft_chapter", f"未识别 {body}"


@pytest.mark.parametrize("text", [
    "", None, 123, [], {}, "普通回复，没有任何工具调用",
    "<tool_call>不是json</tool_call>",
    "<tool_call>{}</tool_call>",              # 缺 name
    "<tool_call>[]</tool_call>",              # 不是 dict
    "<tool_call>",
])
def test_parse_never_raises(text):
    """★ 解析器绝不能抛异常（它在对话主链路上）。"""
    from apps.api.agent import _parse_xml_tool_calls as P
    calls, cleaned = P(text)
    assert isinstance(calls, list)
    assert isinstance(cleaned, str)


def test_parse_no_false_positive():
    """普通回复不该被误判成工具调用。"""
    from apps.api.agent import _parse_xml_tool_calls as P
    calls, cleaned = P("第2章我建议这样写：速办退婚，柳小姐带仆从登门……")
    assert calls == []
    assert "速办退婚" in cleaned


# ==================================================== 接线验证（核心）
def test_xml_fallback_wired_into_chat_stream():
    """★ 核心修复：主循环的 done 分支必须调用 XML 兜底。"""
    from apps.api import agent
    src = inspect.getsource(agent.chat_stream)
    assert "_parse_xml_tool_calls" in src, "主循环未接入 XML 兜底（只定义未调用 = 老 bug）"


def test_xml_fallback_in_done_branch():
    """兜底必须在 done 分支内（工具调用文本会走 done）。"""
    from apps.api import agent
    src = inspect.getsource(agent.chat_stream)
    i_done = src.index('elif ev.kind == "done":')
    i_fallback = src.index("_parse_xml_tool_calls")
    i_tool_branch = src.index("if tool_calls:", i_done)
    assert i_done < i_fallback < i_tool_branch, \
        "XML 兜底位置不对（必须在 done 分支内、tool_calls 判断之前）"


def test_xml_fallback_sets_tool_calls():
    """兜底要把结果赋给 tool_calls（否则还是走收尾）。"""
    from apps.api import agent
    src = inspect.getsource(agent.chat_stream)
    seg = src[src.index("_parse_xml_tool_calls"):][:600]
    assert "tool_calls = xml_calls" in seg, "未把识别结果写入 tool_calls"
    assert "final_text = cleaned" in seg, "未清洗正文"


def test_fallback_guarded_by_empty_check():
    """已有标准 tool_calls 时不该再走兜底（避免重复执行）。"""
    from apps.api import agent
    src = inspect.getsource(agent.chat_stream)
    seg = src[src.index("_parse_xml_tool_calls") - 200:src.index("_parse_xml_tool_calls")]
    assert "if not tool_calls and final_text" in seg, "缺前置守卫"


# ==================================================== prompt 约束
def test_prompt_discourages_text_form():
    from apps.api.agent import SYSTEM_PROMPT
    assert "function call" in SYSTEM_PROMPT
    assert "不要把工具调用写成正文里的文本" in SYSTEM_PROMPT


# ==================================================== 行为级：端到端
def test_behavior_xml_text_triggers_tool_execution(monkeypatch):
    """★ 端到端：模型用文本形式发调用 → 系统必须真的执行工具。

    模拟两轮：
      第 1 轮：模型吐出「承诺 + <tool_call>文本」→ 应被兜底识别为工具调用
      第 2 轮：模型正常收尾
    断言：execute_tool 被调用过（说明没被当成普通回复丢掉）
    """
    import asyncio
    from apps.api import agent, llm

    executed = []
    round_no = {"n": 0}

    class Ev:
        def __init__(self, kind, **kw):
            self.kind = kind
            self.text = kw.get("text", "")
            self.tool_calls = kw.get("tool_calls", [])
            self.input_tokens = 0
            self.output_tokens = 0
            self.cost = 0.0

    async def fake_act_stream(model, msgs, tools, **kw):
        round_no["n"] += 1
        if round_no["n"] == 1:
            # ★ 关键：工具调用被写成正文文本，standard tool_calls 为空
            txt = ('好的，我这就起草第2章。\n'
                   '<tool_call>{"name":"draft_chapter","arguments":{"ch_idx":2}}</tool_call>')
            yield Ev("token", text=txt)
            yield Ev("done", text=txt)
        else:
            yield Ev("done", text="已起草完成，请在草稿卡审阅。")

    async def fake_execute_tool(db, book, tc, emit):
        executed.append(tc["name"])
        return "已起草第2章"

    monkeypatch.setattr(llm, "act_stream", fake_act_stream)
    monkeypatch.setattr(agent, "execute_tool", fake_execute_tool, raising=False)
    monkeypatch.setattr(agent, "_genre_block", lambda db, book: "", raising=False)
    monkeypatch.setattr(agent, "_looks_like_empty_promise", lambda t: False, raising=False)

    class FakeBook:
        id, title, owner_id, genre, theme = "bk_x", "测", "u1", "仙侠", "xianxia"

    class FakeModel:
        id, model_id, role = "m1", "test-model", "default"
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
        asyncio.run(agent.chat_stream(FakeDB(), "bk_x", "写第2章", [], emit))
    except Exception as e:
        # 依赖较多，允许中途因 mock 不全而报错；只要 execute_tool 被调用过即可
        pass

    assert "draft_chapter" in executed, \
        f"★ 文本形式的工具调用未被识别执行（老 bug 复发）：executed={executed}"
    # 应有一条 stage 提示识别到了文本形式
    joined = " ".join(str(e.get("text", "")) for e in events)
    assert "文本形式的工具调用" in joined, f"缺识别提示：{joined[:200]}"


def test_module_contract():
    from apps.api import agent
    for n in ("_parse_xml_tool_calls", "_TOOL_CALL_OPEN", "_TOOL_CALL_CLOSE"):
        assert hasattr(agent, n), f"缺 {n}"

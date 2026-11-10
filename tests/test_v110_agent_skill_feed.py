# -*- coding: utf-8 -*-
"""v1.1 #61 回归 ——「对话框内 agent/skill 调用与流式进度」。

============================ 根因 ============================
后端 chat SSE 流（``main._run_chat_flow`` 的 ``emit``）早已把 agent 的
``action``/``stage`` 事件（读章 / 改设定 / 跑审计 / 代笔……）原样追加进
任务 ``events_json``，前端 ``/tasks/{id}/stream`` 也能取到。但
**StudioView / ChatPanel 从来没渲染过这些过程事件**——用户在对话里只看到
"正在思考…"，看不到主编"正在读第 3 章""已记录人物《X》"，agent 在背后
做了什么对用户完全是黑箱。

============================ 修复 ============================
① StudioView：新增可折叠「过程」活动流 ``streamActions``（icon + 文本，
   上限 40 条防刷屏），``onStreamEvent`` 把 ``kind=action`` 事件的
   ``type``→图标、``text/summary``→文本 推入活动流；每轮新 send 清空。
② ChatPanel：新增 ``actions`` prop + 一个 ``<details>``「过程（进行中/
   已完成 · N 步）」面板，实时把 agent/skill 调用可视化在对话流下方。

============================ 测试（源码级） ===================
前端无 vitest（web2 只跑 vue-tsc + Python 源码断言），故按项目惯例
读 .vue 源码做**可执行代码 token** 断言（不断言注释，防"注释当实现"误判）。
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "apps" / "web2" / "src"


def _read(*parts) -> str:
    return (WEB.joinpath(*parts)).read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """★ 去掉整行注释，只留可执行代码——防止"注释里写了却没实现"被误判通过。"""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("//") or s.startswith("*") or s.startswith("/*"):
            continue
        out.append(line)
    return "\n".join(out)


def test_studioview_activity_feed_state():
    """★ #61 核心：StudioView 必须声明过程活动流状态（icon+text 列表 + 上限）。"""
    code = _code_only(_read("views", "books", "StudioView.vue"))
    assert "streamActions" in code, "★ StudioView 缺少过程活动流状态 streamActions"
    assert "ACTION_ICON" in code, "★ action type → 图标映射缺失，无法把调用可读化"
    assert "ACTION_CAP" in code, "★ 缺少活动流上限（长任务会刷屏）"


def test_studioview_pushes_action_events_to_feed():
    """onStreamEvent 必须把 kind=action 事件推入活动流（旧实现整体忽略）。"""
    code = _code_only(_read("views", "books", "StudioView.vue"))
    assert "kind === 'action'" in code, "onStreamEvent 仍不处理 action 事件"
    # 取 type→图标、text/summary→文本
    assert "ACTION_ICON[type]" in code, "推入活动流未用 type 查图标"
    # 推入活动流（append 到新数组 + slice 上限）
    assert "streamActions.value" in code, "★ action 事件未推入 streamActions"
    assert ".slice(-ACTION_CAP)" in code, "★ 活动流推入未做上限截断（会刷屏）"


def test_studioview_clears_feed_on_new_send():
    """每轮新对话必须清空活动流（否则上一轮的过程会串到这一轮）。"""
    code = _code_only(_read("views", "books", "StudioView.vue"))
    assert "streamActions.value = []" in code, "★ 新 send 未清空活动流（跨轮污染）"


def test_studioview_passes_feed_to_chatpanel():
    """★ 前后端接缝：StudioView 必须把活动流传给 <ChatPanel>（否则渲染不了）。"""
    code = _code_only(_read("views", "books", "StudioView.vue"))
    assert ':actions="streamActions"' in code, \
        "★ StudioView 未把活动流绑定到 <ChatPanel 的 actions>（过程流无处显示）"


def test_chatpanel_renders_action_feed():
    """ChatPanel 必须声明 actions prop 并渲染「过程」面板。"""
    code = _code_only(_read("components", "ChatPanel.vue"))
    # prop 声明（带默认空数组，不传时不影响老调用点）
    assert "actions?:" in code, "★ ChatPanel 未声明 actions prop"
    assert "actions: () => []" in code, "★ ChatPanel actions prop 缺默认值（老调用点会崩）"
    # 渲染「过程」面板（可折叠 details + 逐条列出 icon/text）
    assert "chat__acts" in code, "★ ChatPanel 未渲染「过程」活动流面板"
    assert "v-for=\"(a, i) in actions\"" in code, "★ 活动流未逐条渲染"
    assert "a.icon" in code and "a.text" in code, "活动流条目未渲染 icon / text"


# ---------------------------------------------------------------- 后端契约
def test_backend_chat_flow_forwards_action_events():
    """★ 前后端契约锚点：chat 流必须把 agent 的 action/stage 事件落进任务台账。

    这是 #61 能「看见」的前提——若 chat 流丢弃 action 事件，前端活动流永远是空。
    行为级：main._run_chat_flow 的 emit 对非 token/task/done 的事件会
    _task_event 追加进 events_json（含 kind=action）；此源码锚点防止有人
    误删这条转发路径。
    """
    main_py = (ROOT / "apps" / "api" / "main.py").read_text(encoding="utf-8")
    # emit 的 else 分支（处理 action/stage 等非 token 事件）必须把事件落库
    import re
    # _run_chat_flow 的 emit：把 summary 灌进 text 并 _task_event 追加
    m = re.search(r"async def emit\(ev: dict\):\n.*?_task_event\(", main_py, re.S)
    assert m, "★ main.py 未找到 chat 流 emit 的事件转发（agent 过程事件被丢弃）"
    # 台账层：action/stage 事件会更新 progress（tasks.append_task_event）
    tasks_py = (ROOT / "apps" / "api" / "services" / "tasks.py").read_text(encoding="utf-8")
    assert '"action"' in tasks_py or "'action'" in tasks_py, \
        "★ 任务台账未识别 action 事件（过程进度不会显示）"

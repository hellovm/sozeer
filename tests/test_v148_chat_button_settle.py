# -*- coding: utf-8 -*-
"""v0.14.8 回归：聊天执行过程列表圆点闪烁收敛 + 发送按钮生命周期绑定任务终态。

背景（用户实测报告）：
  1) 发送命令后按钮立刻恢复可点，无法判断任务是否跑完；
  2) 执行过程列表里的蓝色圆点无论是否完成都一直闪烁。

根因：
  - sendChat 的 finally 在 mountChatStream 刚返回（fire-and-forget）时就执行，
    没等任务终态；mountChatStream/mountTaskStream 不返回 Promise。
  - pushProcess 新行一律给 p-active（带 pdot 动画），且从不把上一行降级；
    终态只改头部 p-status，body 里的行各自继续闪。

本测试用源码静态断言锁住修复点（前端无 JS 测试运行器，静态锚定是既定风格，
参见 tests/v100_static_consistency.py）。
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "apps" / "web" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "apps" / "web" / "style.css").read_text(encoding="utf-8")


def test_settle_process_defined():
    """必须存在 settleProcess 函数：把残留 p-active 行一次性收敛为终态。"""
    assert "function settleProcess(" in APP_JS
    m = re.search(r"function settleProcess\([^)]*\)\s*\{(.*?)\n\}", APP_JS, re.S)
    assert m, "settleProcess 函数体未找到"
    body = m.group(1)
    assert "p-active" in body, "settleProcess 必须处理 .p-active 行"
    assert "p-error" in body and "p-ok" in body, "settleProcess 必须区分成败终态"


def test_push_process_demotes_previous_active():
    """pushProcess 插入新行前必须把上一行从 active 降级为 ok。

    注意（实测踩坑）：判定必须用 classList.contains('p-active')，不能用
    dataset.st === 'active'——dataset.st 早期存的是事件 kind 原值，
    条件永远不成立，导致降级逻辑静默失效。
    """
    m = re.search(r"function pushProcess\([^)]*\)\s*\{(.*?)\n\}", APP_JS, re.S)
    assert m, "pushProcess 函数体未找到"
    body = m.group(1)
    assert "classList.contains('p-active')" in body, \
        "pushProcess 必须用 classList 判定上一行是否 active（dataset.st 判定是历史 bug）"
    assert "dataset.st === 'active'" not in body, \
        "禁止用 dataset.st === 'active' 判定（永远不命中）"
    assert "classList.remove('p-active')" in body, \
        "pushProcess 必须移除上一行的 p-active"
    assert "classList.add('p-ok')" in body, \
        "pushProcess 必须把上一行置为 p-ok"


def test_done_event_settles_rows():
    """mountTaskStream 收到 done 终态时必须调用 settleProcess。"""
    assert "settleProcess(o.panel," in APP_JS, \
        "done 事件分支必须调用 settleProcess 收敛列表"


def test_mount_chat_stream_returns_promise():
    """mountChatStream 必须返回 Promise，并在 onDone 里 resolve。"""
    idx = APP_JS.index("function mountChatStream(")
    seg = APP_JS[idx:idx + 200]
    assert "return new Promise(" in seg, "mountChatStream 必须返回 Promise"
    # onDone 里 resolve
    on_done = APP_JS.index("onDone: async () => {", idx)
    tail = APP_JS[on_done:on_done + 900]
    assert "resolve()" in tail, "onDone 必须 resolve 以恢复发送按钮"
    assert "settleProcess(p, 'done')" in tail, "onDone 必须收敛主编任务列表"


def test_send_chat_awaits_stream():
    """sendChat 必须 await mountChatStream，finally 才恢复按钮。"""
    idx = APP_JS.index("async function sendChat()")
    end = APP_JS.index("\nfunction chip(", idx)
    body = APP_JS[idx:end]
    assert "await mountChatStream(" in body, \
        "sendChat 必须 await 流结束，否则按钮会提前解禁"
    # finally 中恢复按钮
    fin = body.index("} finally {")
    fin_body = body[fin:]
    assert "btn.disabled = false" in fin_body, "finally 必须恢复按钮可点"
    assert "S.sending = false" in fin_body, "finally 必须清 sending 标志"


def test_error_event_resolves_stream():
    """error 事件分支必须 resolve，防止按钮永久锁死。"""
    on_event_idx = APP_JS.index("onEvent: (ev) => {", APP_JS.index("function mountChatStream("))
    seg = APP_JS[on_event_idx:on_event_idx + 2600]
    err_idx = seg.find("ev.kind === 'error'")
    assert err_idx != -1, "onEvent 必须有 error 分支"
    err_seg = seg[err_idx:err_idx + 400]
    assert "resolve()" in err_seg, "error 分支必须 resolve 兜底"


def test_css_non_active_dot_has_no_animation():
    """CSS：非 active 行的圆点必须显式关闭动画（防整列齐闪）。"""
    assert ".p-item:not(.p-active) .p-dot { animation: none; }" in CSS, \
        "必须显式关闭非活跃行圆点动画"

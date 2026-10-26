# -*- coding: utf-8 -*-
"""v0.14.9 Markdown 渲染回归（Python 包装 Node 真实执行）。

bug 实录：主编回复带 ## / ** / |表格|，发送时与刷新后都显示成裸文本。
修复：前端补 mdToHtml 轻量渲染器，两条路径共用。本测试直接跑 Node 版
渲染器（tests/frontend/*.mjs），比源码静态断言强——真实验证输出 HTML。
"""
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "tests" / "frontend"
APP_JS = (ROOT / "apps" / "web" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "apps" / "web" / "style.css").read_text(encoding="utf-8")

NODE = shutil.which("node") or r"C:\Users\david\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"


def _run(mjs: str) -> subprocess.CompletedProcess:
    return subprocess.run([NODE, str(FRONTEND / mjs)], cwd=str(ROOT),
                          capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(not pathlib.Path(NODE).exists(), reason="node 不可用")
def test_md_renderer_suite():
    """Node 渲染器全量用例（标题/表格/列表/代码/XSS 等 25 条）。"""
    r = _run("md_render_test.mjs")
    assert r.returncode == 0, f"渲染器用例失败:\n{r.stdout}\n{r.stderr}"
    assert "25 passed, 0 failed" in r.stdout


@pytest.mark.skipif(not pathlib.Path(NODE).exists(), reason="node 不可用")
def test_md_renderer_real_reply():
    """真实生产回复（用户截图那条审计汇总）渲染验证。"""
    r = _run("md_real_test.mjs")
    assert r.returncode == 0, f"真实回复渲染失败:\n{r.stdout}\n{r.stderr}"
    assert "9/9 通过" in r.stdout


# ---------------------------------------------------------------- 静态锚点
def test_md_functions_defined():
    assert "function mdToHtml(" in APP_JS
    assert "function mdInline(" in APP_JS
    assert "function splitRow(" in APP_JS
    assert "function setMsgText(" in APP_JS


def test_add_msg_uses_markdown():
    """addMsg（刷新恢复路径）必须走 Markdown 渲染。"""
    i = APP_JS.index("function addMsg(role, text)")
    seg = APP_JS[i:i + 500]
    assert "setMsgText(" in seg, "addMsg 必须用 setMsgText 渲染 agent 回复"
    assert "true" in seg, "agent 分支必须开 Markdown"


def test_stream_uses_markdown_and_throttle():
    """实时流路径必须走 Markdown，且有节流（长回复不掉帧）。"""
    i = APP_JS.index("function mountChatStream(")
    seg = APP_JS[i:i + 1400]
    assert "setMsgText(txt, full, true)" in seg, "流式 token 必须走 Markdown 渲染"
    assert "scheduleMd" in seg or "_mdTimer" in seg, "必须存在节流器"


def test_ondone_forces_final_render():
    """onDone 必须强制全量渲染，保证与刷新后完全一致。"""
    i = APP_JS.index("onDone: async () => {", APP_JS.index("function mountChatStream("))
    seg = APP_JS[i:i + 700]
    assert "setMsgText(txt, full, true)" in seg, "onDone 必须落最终 Markdown"


def test_css_md_styles_present():
    for cls in (".md-table", ".md-h", ".md-code", ".md-ul", ".md-quote", ".txt.md"):
        assert cls in CSS, f"缺少 Markdown 样式：{cls}"


def test_escapes_before_formatting():
    """XSS 防线：处理器内必须先 esc 再拼 HTML。"""
    i = APP_JS.index("function mdToHtml(")
    seg = APP_JS[i:i + 3000]
    assert "esc(" in seg, "mdToHtml 必须转义输入"
    assert "mdInline(esc(" in seg, "必须先 esc 再 mdInline（转义在前）"

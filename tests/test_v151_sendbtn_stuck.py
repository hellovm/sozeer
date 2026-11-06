# -*- coding: utf-8 -*-
"""v0.15.1 回归：发送按钮卡在「处理中」修复。

bug 实录（用户截图）：生成第一章后点了「草稿采纳」，发送按钮仍显示"处理中"。

根因：mountTaskStream 有三条退出路径，但原先只有「收到终态事件」会调 onDone：
  ① 收到 kind='done' 终态      → onDone ✅
  ② 流被 abort（AbortError）  → return，onDone 永不触发 ❌
  ③ 流读完但没收到 done 事件   → 循环退出，onDone 永不触发 ❌
而 sendChat 的恢复逻辑挂在 mountChatStream 的 Promise 上，该 Promise 仅在
onDone/error 时 resolve → 走 ②/③ 时永久悬空 → 按钮锁死。

修复：
  * mountTaskStream 加 settled 标志 + finally 兜底，保证 onDone 恰好调一次
  * sendChat 加硬超时看门狗（12 分钟），任何未知原因都强制解锁 UI
"""
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "tests" / "frontend"
APP_JS = (ROOT / "apps" / "web" / "app.js").read_text(encoding="utf-8")

NODE = shutil.which("node") or r"C:\Users\david\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"


def _run(mjs: str) -> subprocess.CompletedProcess:
    return subprocess.run([NODE, str(FRONTEND / mjs)], cwd=str(ROOT),
                          capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(not pathlib.Path(NODE).exists(), reason="node 不可用")
def test_sendbtn_behavior_suite():
    """行为级用例：三条退出路径都必须 onDone（真跑代码，不是静态断言）。"""
    r = _run("sendbtn_test.mjs")
    assert r.returncode == 0, f"用例失败:\n{r.stdout}\n{r.stderr}"
    assert "全部通过" in r.stdout


@pytest.mark.skipif(not pathlib.Path(NODE).exists(), reason="node 不可用")
def test_sendbtn_contract_diag():
    r = _run("sendbtn_diag.mjs")
    assert r.returncode == 0, f"契约检查失败:\n{r.stdout}\n{r.stderr}"


# ---------------------------------------------------------------- 静态锚点
def test_settled_guard_present():
    """必须存在 settled 幂等标志。"""
    i = APP_JS.index("async function mountTaskStream(")
    seg = APP_JS[i:i + 1200]
    assert "let settled = false" in seg, "缺 settled 幂等标志"
    assert "const settle = (" in seg, "缺 settle 封装"


def test_abort_path_settles():
    """★ 核心：AbortError 分支必须兜底 settle（修复前这里是裸 return）。"""
    i = APP_JS.index("async function mountTaskStream(")
    seg = APP_JS[i:i + 4200]
    assert "e.name === 'AbortError'" in seg
    abort_seg = seg[seg.index("e.name === 'AbortError'"):]
    assert "settle(" in abort_seg[:400], "AbortError 分支未 settle → 按钮会卡死"


def test_finally_fallback_settles():
    """★ 核心：finally 里必须有「未收到终态则兜底」的逻辑。"""
    i = APP_JS.index("async function mountTaskStream(")
    seg = APP_JS[i:i + 4600]
    fin = seg[seg.index("} finally {"):]
    assert "sawTerminal" in seg, "缺 sawTerminal 终态标志"
    assert "settleProcess" in fin and "settle(" in fin[:500], \
        "finally 未兜底 settle → 流提前关闭时按钮卡死"


def test_onDone_called_once():
    """settle 必须幂等（guard 住重复调用）。"""
    i = APP_JS.index("async function mountTaskStream(")
    seg = APP_JS[i:i + 1200]
    assert "if (settled) return" in seg, "settle 不幂等，可能重复 resolve"


def test_send_watchdog_present():
    """★ 硬兜底：sendChat 必须有看门狗定时器。"""
    assert "_sendWatchdog" in APP_JS, "缺发送看门狗"
    assert "SEND_HARD_TIMEOUT_MS" in APP_JS, "缺硬超时常量"
    i = APP_JS.index("_sendWatchdog = setTimeout(")
    seg = APP_JS[i:i + 700]
    assert "S.sending = false" in seg, "看门狗必须释放 S.sending"
    assert "btn.disabled = false" in seg, "看门狗必须解锁按钮"


def test_watchdog_cleared_in_finally():
    """正常路径必须清掉看门狗（否则 12 分钟后误报）。"""
    i = APP_JS.index("async function sendChat(")
    seg = APP_JS[i:i + 4000]
    fin = seg[seg.index("} finally {"):]
    assert "clearTimeout(_sendWatchdog)" in fin[:400], "finally 未清看门狗"


def test_no_bare_abort_return():
    """回归护栏：不允许再出现裸 `if (e.name === 'AbortError') return;`。"""
    assert "if (e.name === 'AbortError') return;" not in APP_JS, \
        "裸 return 会让调用方永久等待，必须改为 settle 后 return"

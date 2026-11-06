# -*- coding: utf-8 -*-
"""v0.19.6 前端 Bug 回归：批量代笔 ReferenceError + 资源 404 噪音。

★ 用户反馈
----------
「小说批量代笔点击报错」+ 控制台：
    /favicon.ico → 404
    /api/files/.../drama_media/cover.png → 404

★ 真凶：`openBatchDraft` 里引用了**未定义的变量** `box`
------------------------------------------------------
原代码：
    mountTaskStream(r.task_id, {
      panel: ensureProcess(box ? null : document.createElement('div'), ''),
      //                   ^^^ 这个 box 在本函数作用域里根本不存在
    });

点击按钮立刻抛 `ReferenceError: box is not defined` → **批量代笔完全不能用**。
（浏览器控制台里那两个 404 是**无关的噪音**，真正报错被淹没在下面。）

另外 `ensureProcess(bubble, label)` 需要一个**已挂到 DOM** 的容器；
临时 `createElement` 不在文档里 → 就算不报错，进度条也看不见。

正确做法（与「全书审计」一致）：在聊天流里建一个 `msg agent` 气泡承载过程面板。

★ 顺带的 404 噪音（功能无碍，但误导排查）
---------------------------------------
* `favicon.ico` —— 浏览器自动请求，页面没声明 → 用内联 SVG data URI 消除
* `cover.png` —— 封面探测（Image onerror 判定），**每次渲染都发一次** → 加按书缓存
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_JS = ROOT / "apps" / "web" / "app.js"
INDEX = ROOT / "apps" / "web" / "index.html"


@pytest.fixture(scope="module")
def js() -> str:
    return APP_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


# ============================================================ 批量代笔
def _func_body(src: str, name: str) -> str:
    """粗略提取一个 async function 的函数体（按大括号配对）。"""
    i = src.index(f"function {name}(")
    j = src.index("{", i)
    depth, k = 0, j
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[j:k + 1]
        k += 1
    return src[j:]


def _strip_js_comments(src: str) -> str:
    """去掉 JS 注释（行注释 + 块注释）。

    ★ 为什么要去注释：本次修复的注释里**故意写了**原 bug 代码做说明
    （`原写法 ensureProcess(box ? ...) 里的 box 未定义`），
    若不过滤注释，回归测试会误报。
    """
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)     # 块注释
    src = re.sub(r"(?m)^\s*//.*$", "", src)         # 整行注释
    src = re.sub(r"(?<![:\w])//[^\n]*", "", src)   # 行尾注释
    return src


def test_no_undefined_box_in_openBatchDraft(js):
    """★★ 核心回归：openBatchDraft 里不得再出现未定义的 box 变量（注释除外）。"""
    body = _strip_js_comments(_func_body(js, "openBatchDraft"))
    uses_box = re.search(r"\bbox\b", body)
    declares_box = re.search(r"\b(?:const|let|var)\s+box\b", body)
    if uses_box and not declares_box:
        ctx = body[max(0, uses_box.start() - 60):uses_box.end() + 60]
        pytest.fail(f"openBatchDraft 使用了未声明的变量 box（会 ReferenceError）：…{ctx}…")


def test_batch_draft_uses_dom_bubble(js):
    """★ 进度面板必须挂在**已插入 DOM** 的气泡上（否则进度看不见）。"""
    body = _func_body(js, "openBatchDraft")
    assert "chatLog" in body, "未把气泡挂到聊天流"
    assert "appendChild" in body, "未 append 到 DOM"
    assert "ensureProcess(bubble" in body, "未用气泡承载进度面板"


def test_batch_draft_bubble_class_matches_convention(js):
    """气泡 class 要跟其他任务一致（msg agent）。"""
    body = _func_body(js, "openBatchDraft")
    assert "msg agent" in body, "气泡 class 不符合既有约定"


def test_batch_draft_calls_mount_task_stream(js):
    body = _func_body(js, "openBatchDraft")
    assert "mountTaskStream(r.task_id" in body


def test_batch_draft_onDone_refreshes(js):
    """完成后要刷新章节与待审草稿（否则用户看不到新草稿）。"""
    body = _func_body(js, "openBatchDraft")
    assert "loadBible" in body and "renderPendingDrafts" in body


def test_batch_draft_no_bare_ensure_process_temp_element(js):
    """禁止把临时 createElement 直接当面板（不在 DOM 里，不可见）。"""
    body = _func_body(js, "openBatchDraft")
    bad = re.search(r"ensureProcess\(\s*document\.createElement", body)
    assert not bad, "ensureProcess 传了游离元素（进度面板不会显示）"


# ============================================================ favicon
def test_favicon_declared(html):
    """★ 页面必须声明 favicon，否则浏览器自动请求 /favicon.ico → 404 噪音。"""
    assert 'rel="icon"' in html, "未声明 favicon，会产生 /favicon.ico 404"


def test_favicon_is_inline(html):
    """用内联 data URI（省一次请求，也不用管静态路由）。"""
    m = re.search(r'<link rel="icon" href="([^"]+)"', html)
    assert m, "favicon link 格式异常"
    assert m.group(1).startswith("data:image/svg+xml"), \
        f"favicon 应为内联 SVG data URI，实际：{m.group(1)[:40]}"


# ============================================================ 封面 404
def test_cover_probe_cached(js):
    """★ 封面探测结果必须按书缓存（避免每次渲染都发 404）。"""
    assert "__nfCoverMiss" in js, "封面探测未缓存 → 控制台会反复刷 404"
    assert "has(S.bookId)" in js, "未按书 id 判重"


def test_cover_probe_uses_image_not_head(js):
    """探测必须用 Image 对象（/api/files 不支持 HEAD，HEAD 会 404）。"""
    i = js.index("async function renderCoverPreview")
    seg = js[i:i + 1600]
    assert "new Image()" in seg, "应使用 Image 对象探测"
    assert "im.onerror" in seg, "应处理不存在的情况"


# ============================================================ 通用护栏
def test_js_syntax_ok():
    """app.js 必须能通过语法检查（防低级错误上线）。"""
    import subprocess
    node = None
    for c in ("node", "node.exe"):
        try:
            r = subprocess.run([c, "--version"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                node = c
                break
        except Exception:
            continue
    if not node:
        pytest.skip("无 node，跳过语法检查")
    r = subprocess.run([node, "--check", str(APP_JS)], capture_output=True, text=True)
    assert r.returncode == 0, f"app.js 语法错误：{r.stderr[:400]}"


def test_cache_busting_versions_match(html):
    """index.html 里的资源版本号要一致（防漏更新导致浏览器用旧缓存）。"""
    css = re.search(r"style\.css\?v=([\d.]+)", html)
    js = re.search(r"app\.js\?v=([\d.]+)", html)
    assert css and js, "缺少资源版本号"
    assert css.group(1) == js.group(1), \
        f"资源版本号不一致：css={css.group(1)} js={js.group(1)}"


def test_no_obj_null_deref_in_batch(js):
    """批量代笔里的返回值访问要防 null（api 失败时 r 可能为空）。"""
    body = _func_body(js, "openBatchDraft")
    # r.start_ch 直接访问前应有 r 判空或 try/catch
    assert "try {" in body, "缺少异常兜底"

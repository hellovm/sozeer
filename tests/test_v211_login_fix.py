"""登录与鉴权前端行为回归测试（v0.21.1）。

★ 为什么需要这个文件（真实事故）：
  用户反馈「管理员和用户都无法登录」（账号密码见部署文档）
  ★ 注意：此处原文含真实账号密码，开源发布前已脱敏（v1.0-E3）
  ，
  但后端实测**完全正常**（直接 curl 返回 200）。

  根因在前端 `api()` 封装：
  ```js
  if (r.status === 401) { onUnauthorized(); throw new Error('登录已过期'); }
  ```
  **无差别**地把所有 401 当作"会话过期"处理 →
    1. 密码输错（401）→ 清空 token + 重渲染登录页 → 用户看不到"密码错误"
    2. 表现为"点了登录没反应"

★ 本测试用**静态代码检查**验证修复（前端行为难以在 pytest 里跑，
  但关键逻辑可以从源码断言 —— 这是项目既有做法，见 test_v196_frontend_fixes.py）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "apps" / "web" / "app.js"
INDEX_HTML = ROOT / "apps" / "web" / "index.html"
STYLE_CSS = ROOT / "apps" / "web" / "style.css"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ============================================================================
#  ★ 核心回归：api() 必须区分「认证失败」与「会话失效」
# ============================================================================

def test_api_does_not_blindly_logout_on_401():
    """★ 回归守卫：401 不能无差别触发登出。

    这是本次事故的根因 —— 登录密码错误（401）被当成"会话过期"，
    导致清空 token 并重渲染登录页，用户看不到错误。
    """
    src = _read(APP_JS)
    # 必须存在"路径判定"逻辑
    assert "isAuthPath" in src, "缺少认证路径判定（401 会被无差别当会话失效）"
    assert re.search(r"isAuthPath\s*&&|&&\s*!isAuthPath|!\s*isAuthPath", src), \
        "401 处理未与 isAuthPath 关联"


def test_auth_paths_recognized():
    """认证接口路径必须被识别（login/register/refresh/...）。"""
    src = _read(APP_JS)
    m = re.search(r"isAuthPath\s*=\s*(.+?);", src)
    assert m, "未找到 isAuthPath 定义"
    pattern = m.group(1)
    for p in ("login", "register", "refresh"):
        assert p in pattern, f"认证路径正则缺少 {p}"


def test_on_unauthorized_still_exists():
    """会话失效的处理不能被误删（其他接口的 401 仍应登出）。"""
    src = _read(APP_JS)
    assert "function onUnauthorized" in src
    # 非认证路径的 401 必须走 onUnauthorized
    assert re.search(r"r\.status\s*===\s*401\s*&&\s*!isAuthPath", src), \
        "非认证路径的 401 未走 onUnauthorized"


# ============================================================================
#  友好错误提示
# ============================================================================

def test_rate_limit_gives_friendly_message():
    """★ 429（限流）必须给友好提示，而不是甩 HTTP 429。"""
    src = _read(APP_JS)
    assert "r.status === 429" in src or "status === 429" in src, \
        "未处理 429 限流状态"
    assert "操作过于频繁" in src or "过于频繁" in src, \
        "429 缺少友好文案"


def test_retry_after_surfaced():
    """限流的 retry_after 应展示给用户（告诉他要等多久）。"""
    src = _read(APP_JS)
    assert "retry_after" in src, "未透出 retry_after（用户不知要等多久）"


def test_500_gives_friendly_message():
    """5xx 应给友好提示，而不是原始状态码。"""
    src = _read(APP_JS)
    assert "r.status >= 500" in src or "status >= 500" in src
    assert "服务器暂时不可用" in src or "稍后重试" in src


def test_generic_error_includes_status():
    """兜底错误也要带状态码（便于排查）。"""
    src = _read(APP_JS)
    assert "HTTP ${r.status}" in src or "HTTP " in src


# ============================================================================
#  登录页 UI：行内错误提示
# ============================================================================

def test_auth_error_box_exists_in_html():
    """★ 登录页必须有行内错误提示区（toast 会消失，看不清）。"""
    html = _read(INDEX_HTML)
    assert 'id="authError"' in html, "登录页缺少 authError 提示区"


def test_auth_error_box_hidden_by_default():
    """错误区默认隐藏（不能一进页面就显示红色告警）。"""
    html = _read(INDEX_HTML)
    m = re.search(r'id="authError"[^>]*class="([^"]*)"', html) or \
        re.search(r'class="([^"]*)"[^>]*id="authError"', html)
    assert m, "未找到 authError 元素"
    assert "hidden" in m.group(1), f"authError 默认未隐藏：{m.group(1)}"


def test_login_error_written_to_inline_box():
    """登录失败必须写进行内提示区。"""
    src = _read(APP_JS)
    assert "errBox.textContent" in src, "登录失败未写行内提示"
    assert "$('#authError')" in src


def test_password_field_refocused_on_failure():
    """★ 登录失败后应聚焦并全选密码框（省得用户手动清空）。"""
    src = _read(APP_JS)
    assert "#authPass" in src
    assert ".select()" in src, "登录失败后未全选密码（体验差）"


def test_button_text_restored_on_failure():
    """★ 登录失败后按钮文案必须恢复（否则一直显示"处理中…"）。"""
    src = _read(APP_JS)
    assert "orig" in src, "未保存原始按钮文案"
    assert re.search(r"btn\.textContent\s*=\s*orig", src), \
        "按钮文案未恢复（会卡在'处理中…'）"


def test_auth_error_style_exists():
    """错误提示样式必须存在（否则是裸文本）。"""
    css = _read(STYLE_CSS)
    assert ".auth-error" in css, "缺少 .auth-error 样式"


def test_login_page_has_help_tip():
    """登录页应有排障提示（限流说明 + 后台入口）。"""
    html = _read(INDEX_HTML)
    assert "auth-tip" in html, "登录页缺少排障提示"
    assert "/admin/" in html, "提示里缺少管理后台入口"


# ============================================================================
#  缓存版本
# ============================================================================

def test_asset_version_bumped():
    """★ 前端资源版本号必须更新（否则浏览器用旧 app.js，修了也没用）。

    v0.14.6 血泪教训：StaticFiles 默认不发 Cache-Control，
    浏览器按启发式缓存 app.js 数天，旧脚本配新 DOM = 各种"点了没反应"。
    """
    html = _read(INDEX_HTML)
    m = re.search(r"app\.js\?v=([\d.]+)", html)
    assert m, "app.js 未带版本戳（会被浏览器缓存）"
    ver = m.group(1)
    # 版本必须 ≥ 0.21.1（本次修复的版本）
    parts = [int(x) for x in ver.split(".")]
    assert parts >= [0, 21, 1], f"资源版本过低：{ver}（需 ≥ 0.21.1）"


# ============================================================================
#  后端：登录接口行为正确性
# ============================================================================

@pytest.fixture(scope="module", autouse=True)
def _ensure_tables():
    from apps.api import models  # noqa: F401
    from apps.api.db import Base, engine
    Base.metadata.create_all(engine)
    yield


def test_login_wrong_password_returns_401():
    """★ 密码错误必须返回 401（前端据此提示"用户名或密码错误"）。"""
    from fastapi.testclient import TestClient
    import importlib
    import sys
    m = sys.modules.get("apps.api.main") or importlib.import_module("apps.api.main")
    with TestClient(m.app) as c:
        r = c.post("/api/auth/login",
                   json={"username": "definitely_not_exist_xyz", "password": "wrong"})
        assert r.status_code == 401, f"应为 401，实际 {r.status_code}"
        assert "detail" in r.json(), "缺少 detail（前端无法展示原因）"


def test_login_error_message_is_actionable():
    """错误文案应可直接展示给用户（不是内部术语）。"""
    from fastapi.testclient import TestClient
    import importlib
    import sys
    m = sys.modules.get("apps.api.main") or importlib.import_module("apps.api.main")
    with TestClient(m.app) as c:
        r = c.post("/api/auth/login",
                   json={"username": "nobody_xyz", "password": "wrong"})
        detail = r.json().get("detail", "")
        assert "用户名" in detail or "密码" in detail, f"文案不友好：{detail}"


def test_rate_limit_returns_429_with_retry_after():
    """★ 限流必须返回 429 + Retry-After（前端据此提示等待时长）。"""
    from apps.api import ratelimit
    old = ratelimit._store
    ratelimit._store = ratelimit.SlidingWindow()
    try:
        got = None
        for _ in range(15):
            got = ratelimit.check_request("/api/auth/login", "POST", "203.0.113.200")
            if got:
                break
        assert got is not None, "15 次登录未触发限流"
        assert got["status"] == 429
        assert got.get("retry_after", 0) > 0, "缺少 retry_after"
        assert "Retry-After" in (got.get("headers") or {})
    finally:
        ratelimit._store = old


def test_auth_login_is_whitelisted_from_token_check():
    """★ 登录接口必须在鉴权白名单里（否则未登录无法登录 —— 死锁）。"""
    import importlib
    import sys
    m = sys.modules.get("apps.api.main") or importlib.import_module("apps.api.main")
    prefixes = m._AUTH_WHITELIST_PREFIX
    assert any("/api/auth/" == p or "/api/auth/" in p for p in prefixes), \
        f"认证路径未在白名单：{prefixes}"

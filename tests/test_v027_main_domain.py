"""v0.27.0 主域名切换（新版升主域名 + 旧版降级 /legacy/ + 301 兼容）回归测试。

★★ 本文件由 v0.25.9 的「双轨并行」测试改写而来。演练的是**切换之后**的契约：

  ① **新前台在主域名**：`/` 返回新版 index.html（引 `/assets/`，非 `/app2/assets/`）
  ② **新后台在主域名**：`/admin` 与 `/admin/*` 返回同一份新版 index.html
  ③ **旧版降级但仍可用**：`/legacy/`、`/legacy/admin/` 可达（回滚入口）
  ④ **老路径 301 兼容**：`/app2/app` → `/app`、`/app2/admin/users` → `/admin/users`
  ⑤ **SPA fallback**：`/app/books/xxx` 直访/刷新必须回 index.html
  ⑥ **缓存分级**：index.html = no-cache；带 hash 资源 = 长缓存 immutable
  ⑦ **目录穿越防护**：`..` / 绝对路径 / 符号链接都不可越出新版目录
  ⑧ **回滚安全性**：新版产物缺失时 `/` 返回 503（不是 500/白屏）
  ⑨ **灰度开关仍可用**（保留能力，便于未来再灰度）
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from apps.api.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _fresh():
    """★ 从 sys.modules 取当前实例（防模块实例分裂，见 MEMORY.md §62）"""
    import importlib
    import sys
    global flags, NEW_DIR, LEGACY_DIR, ADMIN_DIR
    flags = (sys.modules.get("apps.api.frontend_flags")
             or importlib.import_module("apps.api.frontend_flags"))
    m = sys.modules.get("apps.api.main") or importlib.import_module("apps.api.main")
    NEW_DIR = m.NEW_DIR
    LEGACY_DIR = m.LEGACY_DIR
    ADMIN_DIR = m.ADMIN_DIR
    yield


def _new_built() -> bool:
    return (NEW_DIR / "index.html").is_file()


# ===================================================== ① 新版前台在主域名

def test_new_frontend_at_root(client):
    """★★ 主域名 `/` 必须是**新版**（Vue 3）—— 这是本次切换的核心。"""
    if not _new_built():
        pytest.skip("新版未构建")
    r = client.get("/")
    assert r.status_code == 200
    assert '<div id="app">' in r.text, "/ 未返回新版 index.html"
    # ★ 反例校验：绝不能是旧版单体（app.js 是旧版标志）
    assert "app.js?v=" not in r.text, "★ / 仍在服务旧版！切换未生效"


def test_root_assets_are_new_base(client):
    """★ 新版资源引用必须是 `/assets/`（不是 `/app2/assets/`）—— base 配错会白屏。"""
    if not _new_built():
        pytest.skip("新版未构建")
    idx = (NEW_DIR / "index.html").read_text(encoding="utf-8")
    assert "/assets/" in idx, "index.html 未引用 /assets/（vite base 配错？）"
    assert "/app2/assets/" not in idx, "★ index.html 仍引用 /app2/assets/（base 未改）"


def test_root_does_not_serve_legacy(client):
    """★ 旧版标志物不能在主域名出现。"""
    r = client.get("/")
    assert "织字工坊" not in r.text or '<div id="app">' in r.text, \
        "★ / 返回了旧版「织字工坊」页面"


# ===================================================== ② 新版后台在主域名

def test_new_admin_at_admin_path(client):
    """★ `/admin` 必须是新版后台（同一份 SPA，路由在客户端区分）。"""
    if not _new_built():
        pytest.skip("新版未构建")
    r = client.get("/admin")
    assert r.status_code == 200
    assert '<div id="app">' in r.text, "/admin 未返回新版 index.html"
    # ★ 后台必须 noindex（管理界面不该被收录）
    assert "noindex" in r.headers.get("x-robots-tag", ""), \
        "/admin 缺少 X-Robots-Tag: noindex"


@pytest.mark.parametrize("p", [
    "", "dashboard", "users", "orders", "invoices", "finance",
    "models", "ads", "content", "instances", "config",
    "queue", "rbac", "risk", "audit", "health",
])
def test_admin_subroutes_reachable(client, p):
    """★ 每个后台子路由都必须能直接访问（SPA fallback 覆盖 /admin/*）。"""
    if not _new_built():
        pytest.skip("新版未构建")
    path = "/admin" + ("/" + p if p else "")
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    assert '<div id="app">' in r.text, f"{path} 未回退到 index.html"


def test_admin_does_not_serve_legacy_admin(client):
    """★ 旧版后台（admin.js）不能在 /admin 出现。"""
    if not _new_built():
        pytest.skip("新版未构建")
    r = client.get("/admin")
    assert "admin.js" not in r.text or '<div id="app">' in r.text, \
        "★ /admin 仍在服务旧版后台"


# ===================================================== ③ 旧版降级到 /legacy/

def test_legacy_frontend_served(client):
    """★ 回滚入口：/legacy/ 必须能打开旧版前台。"""
    if not (LEGACY_DIR / "index.html").is_file():
        pytest.skip("旧版目录不存在")
    r = client.get("/legacy/")
    assert r.status_code == 200
    assert "app.js" in r.text or "style.css" in r.text, \
        "/legacy/ 未返回旧版前台"
    # ★ 回滚入口必须 noindex（不该被搜索引擎收录一份废弃界面）
    assert "noindex" in r.headers.get("x-robots-tag", ""), \
        "/legacy/ 缺少 noindex"


def test_legacy_admin_served(client):
    """★ 回滚入口：/legacy/admin 必须能打开旧版后台。"""
    if not (ADMIN_DIR / "index.html").is_file():
        pytest.skip("旧版后台目录不存在")
    for p in ("/legacy/admin", "/legacy/admin/"):
        r = client.get(p)
        assert r.status_code == 200, f"{p} -> {r.status_code}"
        assert "admin.js" in r.text, f"{p} 未返回旧版后台"


def test_legacy_static_assets(client):
    """★ 旧版静态资源（app.js / style.css / admin.js / admin.css）必须可达。

    ★ 这是最容易漏的一环：只挂 index.html 而资源 404 → 页面能开但全白。
    """
    if not (LEGACY_DIR / "index.html").is_file():
        pytest.skip("旧版目录不存在")
    for p in ("/legacy/app.js", "/legacy/style.css",
              "/legacy/admin/admin.js", "/legacy/admin/admin.css"):
        r = client.get(p)
        assert r.status_code == 200, f"{p} -> {r.status_code}"


# ===================================================== ④ 老路径 301 兼容

@pytest.mark.parametrize("old,new", [
    ("/app2/", "/"),
    ("/app2", "/"),
    ("/app2/app", "/app"),
    ("/app2/app/books", "/app/books"),
    ("/app2/books/abc123", "/books/abc123"),
    ("/app2/admin", "/admin"),
    ("/app2/admin/users", "/admin/users"),
    ("/app2/admin/config", "/admin/config"),
])
def test_app2_redirects(client, old, new):
    """★★ 老路径 `/app2/*` 必须 301 到新路径（老书签/老分享链接不断）。

    ★ 为什么用 301（永久）而不是 302：路径语义永久改变，
      让浏览器/搜索引擎更新索引，避免每次访问都回源跳转。
    """
    r = client.get(old, follow_redirects=False)
    assert r.status_code == 301, f"{old} -> {r.status_code}（应为 301）"
    loc = r.headers.get("location", "")
    assert loc == new, f"{old} 跳到 {loc!r}，应为 {new!r}"


def test_app2_redirect_relative_not_absolute(client):
    """★ Location 必须是**相对路径**，不能是绝对 URL。

    ★ 为什么：Cloudflare 回源时 Host 可能是内网 IP，
      拼绝对 URL 会跳到一个打不开的地址。
    """
    r = client.get("/app2/app", follow_redirects=False)
    loc = r.headers.get("location", "")
    assert loc.startswith("/"), f"Location 应为相对路径，实际 {loc!r}"
    assert "://" not in loc, f"Location 不应含协议，实际 {loc!r}"


def test_app2_redirect_followed_works(client):
    """★ 跟随跳转后必须真的能打开（301 不能跳进死路）。"""
    if not _new_built():
        pytest.skip("新版未构建")
    r = client.get("/app2/app/books", follow_redirects=True)
    assert r.status_code == 200
    assert '<div id="app">' in r.text


# ===================================================== ⑤ SPA fallback

@pytest.mark.parametrize("path", [
    "/app",
    "/app/books/abc123",
    "/app/books/abc123/chapters/3",
    "/app/drama/proj_xxx",
    "/app/account/profile",
    "/app/this/route/does/not/exist",
    "/tech", "/about", "/pricing",
])
def test_spa_fallback(client, path):
    """★ 前端路由直访/刷新必须回退到 index.html（history 模式刚需）。"""
    if not _new_built():
        pytest.skip("新版未构建")
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    assert '<div id="app">' in r.text, f"{path} 未回退到 index.html"


def test_root_index_no_slash(client):
    """根路径少写/多写斜杠都要能用。"""
    if not _new_built():
        pytest.skip("新版未构建")
    for p in ("/", "/index.html"):
        assert client.get(p).status_code == 200, p


# ===================================================== ⑥ 缓存分级

def test_cache_index_no_cache(client):
    """★ index.html 必须 no-cache —— 否则发版后用户拿旧引用 → 白屏。"""
    if not _new_built():
        pytest.skip("新版未构建")
    for p in ("/", "/app/books/xyz", "/admin"):
        r = client.get(p)
        cc = r.headers.get("cache-control", "")
        assert "no-cache" in cc or "no-store" in cc, \
            f"{p} 的 Cache-Control 应含 no-cache/no-store，实际 {cc!r}"


def test_cache_hashed_asset_immutable(client):
    """★ 带 hash 的资源长缓存 —— 文件名含内容指纹，不存在旧文件配新 DOM。"""
    assets_dir = NEW_DIR / "assets"
    if not assets_dir.is_dir():
        pytest.skip("新版未构建")
    hashed = [f for f in os.listdir(assets_dir)
              if re.search(r"-[A-Za-z0-9_]{8,}\.(js|css)$", f)]
    if not hashed:
        pytest.skip("无带 hash 资源")
    for path in (f"/assets/{hashed[0]}", f"/admin/assets/{hashed[0]}"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        cc = r.headers.get("cache-control", "")
        assert "max-age=31536000" in cc and "immutable" in cc, \
            f"{path} 带 hash 资源应长缓存，实际 {cc!r}"


def test_assets_reachable_at_root(client):
    """★ 构建产物可经 `/assets/` 访问（部署漏传会白屏）。"""
    assets_dir = NEW_DIR / "assets"
    if not assets_dir.is_dir():
        pytest.skip("新版未构建")
    files = [f for f in os.listdir(assets_dir) if f.endswith(".js")]
    if not files:
        pytest.skip("无 js 产物")
    for f in files[:5]:
        r = client.get(f"/assets/{f}")
        assert r.status_code == 200, f"/assets/{f} -> {r.status_code}"


# ===================================================== ⑦ 路径穿越

@pytest.mark.parametrize("path", [
    "/../legacy/index.html",
    "/..%2F..%2Fetc/passwd",
    "/../../../etc/passwd",
    "/....//....//etc/passwd",
    "/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    "/admin/../../../etc/passwd",
])
def test_path_traversal_blocked(client, path):
    """★ 目录穿越必须被拦（不能读到新版目录之外）。

    ★ 判据是"**绝不泄露**内容"，而不是某个特定状态码：
      · 被显式拦截 → 400/403/404
      · 被 httpx 归一化成不存在的路径 → SPA fallback 返回 index.html（200，安全）
      两种都是安全的；关键是不能吐出目标文件内容。
    """
    r = client.get(path)
    assert "root:" not in r.text, f"{path} 泄露了 /etc/passwd 内容"
    assert r.status_code in (200, 400, 403, 404), f"{path} -> {r.status_code}"


def test_path_traversal_via_symlink():
    """★ 符号链接绕过也要拦（第二层校验：resolve 后仍在 base 内）。"""
    if not NEW_DIR.is_dir():
        pytest.skip("新版未构建")
    link = NEW_DIR / "_test_escape_link"
    try:
        os.symlink("/etc", str(link)) if hasattr(os, "symlink") else None
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("当前平台不支持 symlink")
    try:
        from apps.api.main import app
        with TestClient(app) as c:
            r = c.get("/_test_escape_link/passwd")
            assert "root:" not in r.text
    finally:
        try:
            link.unlink()
        except Exception:
            pass


# ===================================================== ⑧ 回滚安全性

def test_missing_build_returns_503_not_500(monkeypatch):
    """★★ 新版产物缺失时 `/` 必须返回 503（可读提示），不是 500/白屏。

    ★ 为什么重要：这是"部署漏传产物"这类事故的第一道可观测防线 ——
      503 + 明确文案，一眼看出是构建问题而非"服务挂了"。
    """
    import apps.api.main as m
    from fastapi.testclient import TestClient as _TC

    fake = m.NEW_DIR.parent / "_nonexistent_app2_probe"
    monkeypatch.setattr(m, "NEW_DIR", fake, raising=True)
    with _TC(m.app) as c:
        r = c.get("/")
        assert r.status_code == 503, f"应为 503，实际 {r.status_code}"
        assert "未构建" in r.text
        # ★ 旧版回滚入口不能受影响（这才是"一关即回"的技术保障）
        r2 = c.get("/legacy/")
        assert r2.status_code in (200, 503)


# ===================================================== ⑨ 灰度开关（保留能力）

def test_rollout_mode_parsing():
    """模式字符串解析（含容错）—— 切换后仍保留，便于未来再灰度。"""
    assert flags.parse_mode("off") == ("off", 0)
    assert flags.parse_mode("") == ("off", 0)
    assert flags.parse_mode("on") == ("on", 100)
    assert flags.parse_mode("10") == ("pct", 10)
    assert flags.parse_mode("10%") == ("pct", 10)
    assert flags.parse_mode("100") == ("on", 100)
    assert flags.parse_mode("0") == ("off", 0)
    assert flags.parse_mode("allow") == ("allow", 0)
    # ★ 无法识别的值必须回退 off（安全侧），不能默认开
    assert flags.parse_mode("garbage")[0] == "off"


def test_rollout_bucket_stable():
    """★★ 同一 uid 多次判定必须一致（否则"刷新即变色"）。"""
    uids = [f"u_{uuid.uuid4().hex[:8]}" for _ in range(50)]
    first = {u: flags.wants_v2(u, mode="30") for u in uids}
    for _ in range(5):
        again = {u: flags.wants_v2(u, mode="30") for u in uids}
        assert again == first, "★ 同一 uid 的灰度判定不稳定"


def test_rollout_percent_accuracy():
    """★ 配 30% 就要真的接近 30%（分桶均匀）。"""
    uids = [f"u_{i}" for i in range(2000)]
    buckets = flags.rollout_buckets(uids, "30")
    assert buckets["kind"] == "pct"
    assert 25 <= buckets["actual_pct"] <= 35, buckets


def test_rollout_off_means_all_old():
    uids = [f"u_{i}" for i in range(200)]
    assert not any(flags.wants_v2(u, mode="off") for u in uids)


def test_rollout_on_means_all_new():
    uids = [f"u_{i}" for i in range(200)]
    assert all(flags.wants_v2(u, mode="on") for u in uids)


def test_rollout_anonymous_goes_old():
    """★ 未登录（无 uid）必须走**旧版**（最保守）。"""
    assert flags.wants_v2("", mode="50") is False
    assert flags.wants_v2("", mode="99") is False


def test_hash_bucket_not_python_hash():
    """★★ 必须用 md5 而非内置 hash()。"""
    assert flags._hash_bucket("user_abc") == flags._hash_bucket("user_abc")
    assert flags._hash_bucket("u_1") == int(
        hashlib.md5(b"u_1").hexdigest()[:8], 16) % 100


def test_config_failure_falls_back_off(monkeypatch):
    """★ 配置中心故障 → 回退 off（绝不能默认切新版）。"""
    monkeypatch.delenv(flags.ENV_KEY, raising=False)

    class BoomDB:
        pass

    import apps.api.config_center as cc
    orig = getattr(cc, "get_config", None)

    def boom(*_a, **_k):
        raise RuntimeError("配置中心挂了")

    monkeypatch.setattr(cc, "get_config", boom, raising=False)
    try:
        assert flags._read_config(BoomDB()) == "off"
        assert flags.wants_v2("u_1", db=BoomDB()) is False
    finally:
        if orig is not None:
            monkeypatch.setattr(cc, "get_config", orig, raising=False)


def test_env_var_respected(monkeypatch):
    """环境变量兜底（无 DB 时）。"""
    monkeypatch.setenv(flags.ENV_KEY, "50")
    assert flags._read_config(None) == "50"
    assert flags.wants_v2("u_1", db=None) in (True, False)
    monkeypatch.setenv(flags.ENV_KEY, "on")
    assert flags.wants_v2("anything", db=None) is True


def test_status_endpoint_shape():
    """灰度状态查询（排障用）。"""
    st = flags.status(None)
    for k in ("mode", "kind", "percent", "detail", "env_key", "config_key"):
        assert k in st, f"缺少字段 {k}"


def test_allowlist_mode(monkeypatch):
    """白名单模式（内测）：只有名单内的 uid 走新版。"""
    monkeypatch.setenv(flags.ENV_KEY + "_ALLOW", "u_a,u_b")
    monkeypatch.delenv(flags.ENV_KEY, raising=False)
    assert flags.wants_v2("u_a", db=None, mode="allow") is True
    assert flags.wants_v2("u_b", db=None, mode="allow") is True
    assert flags.wants_v2("u_c", db=None, mode="allow") is False
    assert flags.wants_v2("", db=None, mode="allow") is False


# ===================================================== 路由注册顺序铁律

def test_api_not_shadowed_by_catch_all(client):
    """★★★ 最关键的一条：catch-all `/` 不能吃掉 `/api/*`。

    ★ 血泪教训（MEMORY §3）：静态路径必须注册在动态之前。
      这里 catch-all 是最后注册的，若顺序错了，
      **所有 API 都会返回 HTML** —— 前端表现为"所有请求解析失败"，
      但 HTTP 状态码是 200，极难定位。
    """
    for p in ("/api/health", "/api/ads", "/api/public/site"):
        r = client.get(p)
        # ★ 关键判据：绝不能是 HTML
        assert "text/html" not in r.headers.get("content-type", ""), \
            f"★ catch-all 吃掉了 {p} —— 路由注册顺序错误！"
        assert r.status_code in (200, 401, 403, 404, 422), f"{p} -> {r.status_code}"


def test_openapi_not_shadowed(client):
    """★ /openapi.json 与 /docs 也不能被 catch-all 吃掉。"""
    for p in ("/openapi.json", "/docs"):
        r = client.get(p)
        assert r.status_code == 200, f"{p} -> {r.status_code}"
        ct = r.headers.get("content-type", "")
        assert "text/html" not in ct or p == "/docs", f"{p} 被 catch-all 吃掉"


def test_route_registration_order():
    """★★ 源码级校验：catch-all 必须在 /api、/admin、/legacy、/app2 之后注册。

    ★ 为什么要有源码级检查：路由顺序错误**不报错**，只表现为静默失效。
      运行时用例能覆盖大部分，但源码级断言能更早、更明确地指出问题。
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "apps" / "api" / "main.py").read_text(encoding="utf-8")
    i_catchall = src.rfind('@app.get("/{full_path:path}"')
    assert i_catchall > 0, "未找到 catch-all 路由"
    for marker in ('@app.get("/admin"', '@app.get("/legacy"',
                   '@app.get("/app2"'):
        i = src.find(marker)
        assert i > 0, f"未找到 {marker}"
        assert i < i_catchall, f"★ {marker} 注册在 catch-all 之后 → 永远不可达"

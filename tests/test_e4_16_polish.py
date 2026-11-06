# -*- coding: utf-8 -*-
"""v1.0-E4.16 细节优化回归：品牌名 / 版本号 / 页脚布局 / 价格页布局。

★ 用户七项诉求的回归守卫：
  1. 页脚版权旁版本号（/api/build 匿名可读）
  2. 页脚/顶栏 logo 消失（幽灵 CSS 变量）
  3. 页脚四列并排（grid 轨道数 = 子块数）
  4/5. 用户协议/隐私政策"织字工坊"（存量值 + 默认值双修）
  6. 价格页无 header/footer（路由布局归属）
  7. 登录页品牌名 + 管理员入口提示

★★ 最重要的一条教训（本文件的核心守卫）：
  `var(--brand)` 等**幽灵 CSS 变量**引用，构建器（vite）不会报错、
  浏览器不报错、页面不白屏 —— 只是"元素看起来不对"。
  这种静默失败必须用**源码契约测试**兜住：令牌表定义的变量集合
  必须覆盖全站 `var(--xxx)` 引用的集合（反向：引用了未定义变量即 fail）。
"""
from __future__ import annotations

import pathlib
import re

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB2 = ROOT / "apps" / "web2" / "src"
TOKENS = WEB2 / "styles" / "tokens.css"

from apps.api.db import Base, get_db                    # noqa: E402
from apps.api.main import app                           # noqa: E402
from apps.api.models import Setting, User               # noqa: E402


# ================================================================ 幽灵变量契约

def _defined_vars() -> set[str]:
    """tokens.css 里定义的全部变量名（`:root` + `[data-theme=dark]` 两段）。

    ★ 正则陷阱：tokens.css 有"一行多定义"（`--sp-1: 4px;  --sp-2: 8px;`），
      用行首锚定每行只抓到第一个 → 大量误报。
      改用不锚行首的查找（注释已先剥离，不会误伤）。
    """
    css = TOKENS.read_text(encoding="utf-8")
    # 去掉注释再扫（避免把注释里的示例当定义）
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return set(re.findall(r"(--[\w-]+)\s*:", css))


def _vue_sources() -> list[pathlib.Path]:
    return sorted(WEB2.rglob("*.vue")) + sorted(WEB2.rglob("*.css"))


def test_no_ghost_css_vars_referenced():
    """★★ 全站引用的 CSS 变量必须在 tokens.css 有定义（静默失败的根治守卫）。

    白名单 = CSS 规范内置函数场景不需要（这里只查 var() 引用）。
    注：`var(--x, fallback)` 带兜底值的引用也必须登记 —— 兜底说明
    作者自己都知道可能没定义，不如直接把变量定义补上。
    """
    defined = _defined_vars()
    used: dict[str, str] = {}
    pat = re.compile(r"var\(\s*(--[\w-]+)")
    for f in _vue_sources():
        css = f.read_text(encoding="utf-8")
        # 剥 CSS 注释 + HTML 注释（Vue 模板里的说明文字可能举例 var(--x)）
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        css = re.sub(r"<!--.*?-->", "", css, flags=re.S)
        for m in pat.finditer(css):
            used.setdefault(m.group(1), str(f.relative_to(WEB2)))
    ghosts = sorted(k for k in used if k not in defined)
    assert not ghosts, f"幽灵 CSS 变量（引用了但 tokens.css 未定义）: {ghosts}"


def test_brand_alias_layer_exists():
    """★ 别名层必须显式存在（--brand 是全站最高频引用形态）。"""
    css = TOKENS.read_text(encoding="utf-8")
    for v in ("--brand:", "--brand-soft:", "--text-faint:", "--surface-2:"):
        assert v in css, f"tokens.css 缺别名定义 {v}"


def test_marketing_layout_no_bare_brand_in_bg():
    """★ MarketingLayout 的 logo 背景必须用真实存在的品牌色阶。

    （历史事故：background: var(--brand) 解析失败 → 白字透明底"消失"。
     现在别名层已有 --brand，此测试转而锁"令牌表中必须真的有它"。）
    """
    defined = _defined_vars()
    assert "--brand" in defined


def test_footer_grid_tracks_match_blocks():
    """★ 页脚 grid 轨道数必须 = 子块数（4 列装 5 块是"法律"孤行的根因）。"""
    src = (WEB2 / "layouts" / "MarketingLayout.vue").read_text(encoding="utf-8")
    m = re.search(r"\.mk__foot-grid\s*\{[^}]*grid-template-columns:\s*([^;]+);", src)
    assert m, "找不到 mk__foot-grid 的 grid-template-columns"
    tracks = len([t for t in m.group(1).split() if not t.startswith("repeat(")])
    blocks = len(re.findall(r"<div>\s*<h4>", src)) + (
        1 if 'mk__brand--foot' in src else 0)
    # 品牌块(无 h4) + h4 子块数 = 轨道数（桌面档）
    assert tracks == blocks, f"页脚轨道 {tracks} ≠ 子块 {blocks}"


def test_footer_has_version_and_layout_has_pricing_route():
    """★ ①页脚有版本号渲染 ⑥/pricing 在 MarketingLayout 分组内。"""
    lay = (WEB2 / "layouts" / "MarketingLayout.vue").read_text(encoding="utf-8")
    assert "mk__ver" in lay and "appVersion" in lay
    assert "/api/build" in lay, "版本号数据源必须是 /api/build"

    rt = (WEB2 / "router" / "index.ts").read_text(encoding="utf-8")
    # pricing 路由必须出现在 MarketingLayout 分组（path: '/' 的 children）内
    marketing_block = rt.split("path: '/'")[1].split("───────")[0]
    assert "name: 'pricing'" in marketing_block
    # 且不再有独立顶层 /pricing 路由
    assert not re.search(r"path:\s*'/pricing'", rt)


def test_login_page_no_admin_entry():
    """★ ⑦登录页不再暴露管理后台入口（公开页面的攻击面收敛）。

    ★ 断言写法（教训）：只断言**链接与引用形态**（href="/admin/"、
    `<a`），不能断言中文字样 —— 修复说明注释里提到"管理后台"
    四个字是正常的，不是入口。
    """
    src = (WEB2 / "views" / "auth" / "LoginView.vue").read_text(encoding="utf-8")
    assert "/admin/" not in src, "登录页不得含 /admin/ 链接"
    assert "target=\"_blank\"" not in src, "登录页不得有外开新窗的管理入口链接"


# ================================================================ HTTP 层契约

@pytest.fixture()
def db_session(tmp_path):
    """内存库（文件型 tmp_path 避免跨线程问题，与 test_v026 同款）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}",
                        connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    SL = sessionmaker(bind=eng, autoflush=False, autocommit=False)
    s = SL()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def client(db_session, monkeypatch):
    """★★ 关键（MEMORY §62 测试实例分裂）：鉴权中间件直接用 SessionLocal
    （不走 Depends），必须 main/auth 两处都指向同一个测试库。"""
    import apps.api.main as main_mod
    import apps.api.auth as auth_mod

    def _get_db():
        yield db_session

    class _SL:
        def __call__(self):
            return db_session
        def __enter__(self):
            return db_session
        def __exit__(self, *a):
            return False

    app.dependency_overrides[get_db] = _get_db
    monkeypatch.setattr(main_mod, "SessionLocal", _SL(), raising=False)
    monkeypatch.setattr(auth_mod, "SessionLocal", _SL(), raising=False)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_public_site_returns_updated_default_brand(client, db_monkeypatch=None):
    """★ ④⑤ 新装实例（无存量值）公开站点信息必须是新品牌名。"""
    r = client.get("/api/public/site")
    assert r.status_code == 200
    d = r.json()
    assert d["site_name"] == "溯字 SOZEER"
    assert "织字工坊" not in d["site_name"]


def test_legacy_site_name_row_is_normalized(client, db_session, monkeypatch):
    """★ ④⑤ 存量旧键 site_name（织字工坊）也能被 normalize 归一化命中新品牌。

    模拟线上真实形态：旧键 site_name 存着旧品牌名。
    get_setting → normalize_key → system.site_name → 读 DB system.site_name
    （无）→ 兜底注册表默认值「溯字 SOZEER」→ 旧品牌名不再漏出。
    （即：部署 fix_brand_rename.py 前的过渡期也不显示旧名。）

    ★★ 为什么直接用 db_session 而不是 `from apps.api.db import SessionLocal`：
      client fixture 只 patch 了 main/auth 两处，apps/api/db.py 的
      SessionLocal 仍是真身 —— 函数内 import 拿到的是**开发配置的库**，
      会把测试数据写进开发库（本次 DEBUG 实锤：UNIQUE 冲突来自 dev 库存量行）。
    """
    from apps.api import config_center as cc
    cc.cache_clear()                       # ★ 测试隔离：清进程级配置缓存
    from apps.api.services.settings import get_setting
    try:
        db_session.add(Setting(key="site_name", value="织字工坊"))
        db_session.commit()
        # 旧键读到的是新品牌（默认值兜底），不是存量旧名
        assert get_setting(db_session, "site_name", "") == "溯字 SOZEER"
    finally:
        cc.cache_clear()


def test_allow_register_returns_brand_not_legacy_name(client, db_session, monkeypatch):
    """★ ⑦ 登录页数据源 /api/auth/allow-register 返回新品牌名。"""
    from apps.api import config_center as cc
    cc.cache_clear()                       # ★ 测试隔离：清进程级配置缓存
    try:
        db_session.add(Setting(key="site_name", value="织字工坊"))
        db_session.commit()
        r = client.get("/api/auth/allow-register")
        assert r.status_code == 200
        # ★ 关键：即使 DB 存量是旧键旧值，响应也绝不能是"织字工坊"
        assert r.json()["site_name"] == "溯字 SOZEER"
    finally:
        cc.cache_clear()


def test_build_endpoint_exposes_version(client):
    """★ ① 版本号数据源 /api/build 匿名可读且带 version 字段。"""
    r = client.get("/api/build")
    assert r.status_code == 200
    assert "version" in r.json()


def test_settings_defaults_no_legacy_brand():
    """★ 默认值字典不得再含旧品牌名（防止新装实例复现）。"""
    from apps.api.services.settings import SETTING_DEFAULTS
    assert SETTING_DEFAULTS["site_name"] == "溯字 SOZEER"
    assert "织字工坊" not in SETTING_DEFAULTS["site_name"]


def test_fix_brand_rename_script_idempotent_shape():
    """★ 修正脚本必须幂等：精确旧值匹配 + 新值不重复写入。"""
    src = (ROOT / "deploy" / "fix_brand_rename.py").read_text(encoding="utf-8")
    assert "织字工坊" in src
    assert "--dry-run" in src
    assert "cache_clear" in src, "写后必须失效配置缓存"

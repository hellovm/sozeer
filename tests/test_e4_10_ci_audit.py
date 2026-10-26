# -*- coding: utf-8 -*-
"""v1.0-E4.10 CI 门禁与导入审计的守护测试。

============================ 本轮做两件事 ============================

**1. 冒烟纳入 CI**
  把 `deploy/smoke_all_endpoints.py` 加进 `.github/workflows/build.yml`。
   ★ 改造成**内存模式**（TestClient）—— CI 里没有运行中的服务。

**2. 审计路由导入完整性**
   写 `deploy/audit_imports.py`（四查）：
     查 1 符号表检查（symtable，最准）
     查 2 导入完整性
     查 3 导入写法一致性
     查 4 延迟导入冗余

============================ 发现的重要问题 ============================

### 🔴 冒烟产生**假阴性**（严重）

首次跑冒烟时 **993 次请求被 429 拦掉** ——
根因：冒烟发 1600+ 请求，**同一账号**必然触发
`gen:{user_id}` 限流 → **大量端点根本没进业务逻辑**，
但脚本报告"未发现 5xx"（**看起来通过，实际什么都没测**）。

★★ 判据：**压测/冒烟类工具必须能绕开业务限流** ——
   否则测的不是"端点是否正确"，而是"限流是否符合预期"。

修复：加 `NF_RATELIMIT_OFF=1` 开关（默认关闭 + 启动时 warning），
冒烟脚本自动开启。

### ★ 25 处"冗余延迟导入"里，7 处是**刻意的**

复核发现 7 处在 `try:` 块内 —— 它们是**降级保护**
（如 `auth.require_admin` 在 RBAC 故障时降级为 role 判断）。

★★ 判据：**"重复" ≠ "冗余"** —— 要看上下文与职责。
   无脑清理 = 用一致性风险换正确性。

最终：清理 16 处（真冗余），保留 7 处（刻意降级），
并**升级审计脚本区分 try 块**。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


# ============================================================ ① CI 门禁

class TestCiGate:
    """★★★ 冒烟必须真的进 CI（否则等于没做）。"""

    def test_workflow_has_smoke_step(self):
        """★★★ CI 配置里必须有冒烟步骤。"""
        src = _read(ROOT / ".github" / "workflows" / "build.yml")
        assert "smoke_all_endpoints.py" in src, \
            "★★★ CI 未包含全端点冒烟 —— 等于没做"

    def test_smoke_step_in_test_job(self):
        """★ 冒烟应在 test job 里（与单测同环境）。"""
        import yaml
        d = yaml.safe_load(
            _read(ROOT / ".github" / "workflows" / "build.yml"))
        steps = d["jobs"]["test"]["steps"]
        names = [s.get("name", "") for s in steps]
        assert any("冒烟" in n for n in names), \
            f"★ test job 里没有冒烟步骤：{names}"

    def test_smoke_script_runnable_offline(self):
        """★★★ 冒烟脚本必须支持**离线模式**（CI 无运行中的服务）。

        ★ 判据：默认用 TestClient（内存），而不是打 http://127.0.0.1:8000。
        """
        src = _read(ROOT / "deploy" / "smoke_all_endpoints.py")
        assert "TestClient" in src, \
            "★★★ 冒烟脚本未用 TestClient —— CI 里跑不起来"
        assert "NF_SMOKE_BASE" in src, \
            "★ 缺少线上模式开关（用于验证部署）"

    def test_smoke_disables_ratelimit(self):
        """★★★ 冒烟必须关闭限流（否则大量请求被 429 → 假阴性）。"""
        src = _read(ROOT / "deploy" / "smoke_all_endpoints.py")
        assert "NF_RATELIMIT_OFF" in src, \
            "★★★ 冒烟未关闭限流 —— 会产生假阴性（看起来通过实际没测）"


# ============================================================ ② 限流开关

class TestRatelimitSwitch:
    """★★★ 限流开关必须默认关闭 + 启动告警。"""

    def test_switch_defined(self):
        from apps.api import ratelimit as rl
        assert hasattr(rl, "ENV_OFF"), "★ 缺少 ENV_OFF 常量"
        assert rl.ENV_OFF == "NF_RATELIMIT_OFF"

    def test_default_off(self):
        """★★★ 默认**不关闭**限流（安全）。"""
        src = _read(ROOT / "apps" / "api" / "ratelimit.py")
        assert '"1", "true", "on", "yes"' in src or "'1'" in src, \
            "★ 开关解析方式异常"
        # ★ 默认值必须是"关闭开关"（即不限流关闭）
        import os
        if "NF_RATELIMIT_OFF" not in os.environ:
            from apps.api import ratelimit as rl
            assert rl._RATELIMIT_OFF is False, \
                "★★★ 默认未设置时不应关闭限流"

    def test_startup_warns(self):
        """★★★ 关闭限流时**必须**在启动日志告警。

        ★ 为什么：这个开关会关掉所有防刷保护 ——
          误配到生产环境等于"门户洞开"。必须有明显日志。
        """
        src = _read(ROOT / "apps" / "api" / "main.py")
        assert "_RATELIMIT_OFF" in src, "★ 启动时未检查限流开关"
        assert "限流已" in src, "★ 启动时缺少告警文案"


# ============================================================ ③ 导入审计

class TestImportAudit:
    """★★★ 导入审计脚本（四查）必须存在且可用。"""

    def test_script_exists(self):
        p = ROOT / "deploy" / "audit_imports.py"
        assert p.is_file(), "★ 缺少导入审计脚本"

    def test_script_syntax(self):
        try:
            ast.parse(_read(ROOT / "deploy" / "audit_imports.py"))
        except SyntaxError as e:
            pytest.fail(f"★ 审计脚本语法错误：{e}")

    def test_four_checks_implemented(self):
        """★★ 四查必须都在（符号表/导入完整性/一致性/冗余）。"""
        src = _read(ROOT / "deploy" / "audit_imports.py")
        for fn in ("check_symbols", "check_import_names",
                   "check_import_consistency", "check_deferred_redundancy"):
            assert f"def {fn}" in src, f"★ 缺少 {fn}"

    def test_uses_symtable(self):
        """★★★ 必须用 symtable（手写 AST 误报率太高）。"""
        src = _read(ROOT / "deploy" / "audit_imports.py")
        assert "symtable" in src, \
            "★★★ 未用 symtable —— 手写 AST 会大量误报（我踩过）"

    def test_skips_try_blocks(self):
        """★★★ 审计必须**跳过 try 块内的延迟导入**。

        ★ 为什么（踩坑）：我第一版无脑报 25 处，复核发现 7 处
          在 `try:` 里 —— 是**刻意的降级保护**（RBAC 故障时降级）。
        ★★ 判据：**"重复" ≠ "冗余"** —— 要看上下文与职责。
        """
        src = _read(ROOT / "deploy" / "audit_imports.py")
        assert "try_lines" in src, "★★★ 未区分 try 块 —— 会误报刻意降级"

    def test_current_code_clean(self):
        """★★★ 当前代码必须**零问题**（CI 门禁）。"""
        import subprocess
        import sys
        r = subprocess.run(
            [sys.executable, str(ROOT / "deploy" / "audit_imports.py")],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", cwd=str(ROOT), timeout=120)
        assert r.returncode == 0, (
            "★★★ 导入审计未通过：\n" + (r.stdout or "")[-2000:])


# ============================================================ ④ 关键文件导入

class TestCriticalImports:
    """★★★ 曾出过问题的文件的导入必须完好。"""

    def test_system_py_imports(self):
        """★ `system.py` 曾被查出漏 2 处导入。"""
        src = _read(ROOT / "apps" / "api" / "routers" / "system.py")
        head = "\n".join(src.splitlines()[:45])
        assert "import uuid" in head, "★ system.py 缺 uuid"
        assert "from apps.api.skills import loader as skills" in head, \
            "★ system.py 缺 skills loader"

    def test_ingest_py_imports(self):
        """★ `ingest.py` 曾因漏导入导致 3 个端点 500。"""
        src = _read(ROOT / "apps" / "api" / "ingest.py")
        head = "\n".join(src.splitlines()[:120])
        assert "TelemetryInstance" in head, "★ ingest.py 缺 TelemetryInstance"
        assert "InstanceCommand" in head, "★ ingest.py 缺 InstanceCommand"

    def test_no_deferred_models_import_in_ingest(self):
        """★ `ingest.py` 不应再有函数内 models 延迟导入（已统一模块级）。"""
        import re as _re
        src = _read(ROOT / "apps" / "api" / "ingest.py")
        # 排除 try 块内的（本文件没有，但保持健壮）
        deferred = _re.findall(
            r"^\s+from apps\.api\.models import .+$", src, _re.M)
        assert not deferred, (
            "★ ingest.py 仍有函数内 models 导入：\n  "
            + "\n  ".join(deferred))


# ============================================================ ⑤ 双模式健壮性

class TestDualModeRobustness:
    """★★★ 冒烟脚本必须支持"从任意位置运行"（运维脚本基本要求）。"""

    def test_sys_path_has_cwd_fallback(self):
        """★★★ sys.path 必须含**当前工作目录**兜底。

        ★ 踩坑记录：线上模式把脚本传到服务器 `/tmp/smoke.py` 执行，
          此时 `__file__` 的父目录是 `/` → `apps` 导不进来
          （`ModuleNotFoundError: No module named 'apps'`）。

        ★★ 判据：**脚本要能"从任意位置运行，只要 CWD 在项目根"** ——
          没人会把运维脚本固定在项目里执行。
        """
        src = _read(ROOT / "deploy" / "smoke_all_endpoints.py")
        assert "os.getcwd()" in src, \
            "★★★ 缺少 CWD 兜底 —— 脚本复制到别处就跑不起来"

    def test_uses_module_level_urllib(self):
        """★★★ urllib 必须在**模块级**导入。

        ★ 踩坑记录（Python 经典陷阱）：函数内写 `import urllib.request`
          会让 `urllib` 变成**函数局部变量** ——
          函数其他位置的 `urllib.parse.quote` 会
          `UnboundLocalError: cannot access local variable 'urllib'`。

        ★★ 判据：**同一模块名不要"部分函数内导入、部分模块级用"**。
        """
        src = _read(ROOT / "deploy" / "smoke_all_endpoints.py")
        # 模块级应有
        head = "\n".join(src.splitlines()[:60])
        assert "import urllib.parse" in head, "★ urllib.parse 未在模块级导入"
        assert "import urllib.request" in head, "★ urllib.request 未在模块级导入"
        # 函数内不应再有（会造成局部名遮蔽）
        import re as _re
        body = src[src.find("def main"):] if "def main" in src else ""
        inner = _re.findall(r"^\s+import urllib", body, _re.M)
        assert not inner, f"★ 函数内仍有 urllib 导入（会造成作用域遮蔽）：{inner}"

    def test_schema_generation_not_http(self):
        """★★ 端点清单应**本地生成**，不是拉 `/openapi.json`。

        ★ 为什么：生产 `NOVELFORGE_DOCS=0` **关闭了文档端点**
          （安全配置，不该为冒烟而打开）。
        ★★ 判据：**"取端点清单"与"发请求"是两件事** ——
          前者可以本地导入，后者必须走网络。
        """
        src = _read(ROOT / "deploy" / "smoke_all_endpoints.py")
        assert "_app.openapi()" in src, \
            "★★ 端点清单未本地生成（生产关了文档端点，拉不到）"


# ============================================================ ⑤ SEO 基础设施（v1.0-E4.11 评估整改）

class TestSeoEndpoints:
    """★★ 营销页是获客入口，robots.txt / sitemap.xml 必须真实存在。

    此前线上实测：/robots.txt 被 catch-all 兜成 index.html
    （Content-Type: text/html）→ 爬虫解析失败 → 收录全靠运气。
    """

    def _client(self, monkeypatch):
        # ★★ 必须用 monkeypatch.setenv（自动回退），不能用 os.environ.setdefault ——
        #   setdefault 是**永久**环境泄漏：一旦 NF_RATELIMIT_OFF=1 进了进程环境，
        #   后续任意 importlib.reload(apps.api.ratelimit) 都会把模块常量
        #   `_RATELIMIT_OFF` 重新绑定成 True → check_request 直接 return None，
        #   限流被静默关闭 → 全量跑时 test_v206/v211 的"期望 429"用例全挂
        #   （单跑通过、全量失败 = 典型跨文件状态污染，MEMORY §假阴性比失败更危险）。
        #   monkeypatch 在每个用例后自动删掉这两个变量，杜绝污染。
        monkeypatch.setenv("NOVELFORGE_DISABLE_WORKERS", "1")
        monkeypatch.setenv("NF_RATELIMIT_OFF", "1")
        from fastapi.testclient import TestClient
        from apps.api.main import app
        return TestClient(app, raise_server_exceptions=False)

    def test_robots_txt_is_plain_text(self, monkeypatch):
        tc = self._client(monkeypatch)
        r = tc.get("/robots.txt")
        assert r.status_code == 200
        assert "text/plain" in r.headers.get("content-type", ""), \
            f"★ robots.txt 必须是 text/plain，实际 {r.headers.get('content-type')}"
        assert "User-agent" in r.text and "Disallow" in r.text

    def test_robots_disallows_admin_and_legacy(self, monkeypatch):
        """后台与旧版回滚入口绝不能被收录。"""
        tc = self._client(monkeypatch)
        r = tc.get("/robots.txt")
        assert "Disallow: /admin" in r.text, "★ 缺 /admin 屏蔽"
        assert "Disallow: /legacy" in r.text, "★ 缺 /legacy 屏蔽"

    def test_sitemap_lists_marketing_pages(self, monkeypatch):
        tc = self._client(monkeypatch)
        r = tc.get("/sitemap.xml")
        assert r.status_code == 200
        assert "application/xml" in r.headers.get("content-type", "")
        for path in ("/intro", "/pricing", "/help"):
            assert f"sozeer.com{path}" in r.text, f"★ sitemap 缺 {path}"

    def test_main_py_registers_explicit_routes(self):
        """源码级断言：显式路由必须注册在 catch-all 之前才有意义。"""
        src = _read(ROOT / "apps" / "api" / "main.py")
        robots_pos = src.find('"/robots.txt"')
        sitemap_pos = src.find('"/sitemap.xml"')
        catchall_pos = src.find('"/{full_path:path}"')
        assert robots_pos > 0 and sitemap_pos > 0, "★ 缺少 robots/sitemap 路由"
        assert catchall_pos > robots_pos and catchall_pos > sitemap_pos, \
            "★ robots/sitemap 必须注册在 catch-all 之前（否则被兜成 HTML）"

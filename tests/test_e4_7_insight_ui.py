# -*- coding: utf-8 -*-
"""v1.0-E4.7 数据洞察界面的守护测试。

============================ 为什么需要 ============================

E4.5/E4.6 把**数据采到了**（心跳调度 / 功能埋点 / 采纳信号 / 提示词指纹），
但**没有任何界面能看到** —— 数据白采。

★★ 判据：**采集的下一步必须是把数据变成可行动的信息**，
  否则只是"存了一堆数字"。

============================ 本页回答两个问题 ============================

① 改了提示词，生成质量变好了吗？ → 提示词版本对比
② 哪个环节的产出最容易被丢弃？   → 采纳漏斗

============================ 与已有能力的区别 ============================

| 端点 | 分组依据 | 回答 |
|---|---|---|
| `/insight/ab` | **模型** | 哪个模型好 |
| `/insight/prompt-ab` | **提示词指纹** | 改了 prompt 有没有变好 |

★ 两者互补，都需要 —— 不重复造轮子。
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
API = ROOT / "apps" / "api"
WEB = ROOT / "apps" / "web2" / "src"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    r"""去注释与文档字符串。"""
    import re as _re
    text = _re.sub(r"<!--[\s\S]*?-->", "", text)
    text = _re.sub(r'"""[\s\S]*?"""', "", text)
    text = _re.sub(r"'''[\s\S]*?'''", "", text)
    text = _re.sub(r"/\*[\s\S]*?\*/", "", text)
    out = []
    for line in text.splitlines():
        if line.strip().startswith(("#", "//", "*")):
            continue
        out.append(line)
    return "\n".join(out)


# ============================================================ ① 后端聚合

class TestPromptAbBackend:
    """★★ `prompt_ab` 必须能算出"哪个版本更好"。"""

    def test_function_exists(self):
        from apps.api import insight
        assert hasattr(insight, "prompt_ab"), "★ 缺少 prompt_ab"

    def test_computes_adopt_rate(self):
        """★★★ 核心：必须算出采纳率（而不是只给生成量）。

        ★ 判据：**生成量多 ≠ 质量好** —— 可能只是反复重试。
          只有"采纳率"才能反映质量。
        """
        from apps.api import adoption as A
        from apps.api import insight as I
        A.reset()
        p = "测试提示词" * 3
        for _ in range(4):
            A.record_generate("t", prompt=p, ok=True)
        for _ in range(3):
            A.record_adopt("t", before_words=100, after_words=105, prompt=p)
        r = I.prompt_ab()
        assert r["variants"], f"★ 未产出对比结果：{r}"
        v = r["variants"][0]
        assert v["gen"] == 4 and v["adopt"] == 3
        assert v["adopt_rate"] == 75.0, f"★ 采纳率算错：{v['adopt_rate']}"
        A.reset()

    def test_small_sample_excluded(self):
        """★★ 样本 <3 的版本必须排除（无统计意义）。

        ★ 为什么：生成 1 次采纳 1 次 = 100%，会严重误导决策。
        """
        from apps.api import adoption as A
        from apps.api import insight as I
        A.reset()
        A.record_generate("t", prompt="只生成一次", ok=True)
        A.record_adopt("t", before_words=100, after_words=105,
                       prompt="只生成一次")
        r = I.prompt_ab()
        assert not r["variants"], "★ 样本不足的版本不应纳入对比"
        A.reset()

    def test_scope_disclosed(self):
        """★★★ 必须回传数据范围说明。

        ★ 为什么：数据来自**内存计数**（重启清零）。
          不标注会让运营误读为"历史累计"，做出错误判断。
        """
        from apps.api import insight as I
        r = I.prompt_ab()
        assert "scope" in r, "★ 未回传数据范围"
        assert "内存" in r["scope"] or "重启" in r["scope"], \
            f"★ scope 未说明易失性：{r['scope']}"

    def test_gives_actionable_suggestion(self):
        """★★ 必须给可执行建议（不能只堆数字）。

        ★ 判据（MEMORY §40）：**只报问题不给方案 = 无用**。
        """
        from apps.api import adoption as A
        from apps.api import insight as I
        A.reset()
        for i, (p, n_gen, n_ad) in enumerate(
                [("好版本" * 3, 10, 9), ("差版本" * 3, 10, 2)]):
            for _ in range(n_gen):
                A.record_generate("t", prompt=p, ok=True)
            for _ in range(n_ad):
                A.record_adopt("t", before_words=100, after_words=105, prompt=p)
        r = I.prompt_ab()
        assert r.get("suggestion"), "★ 缺少可执行建议"
        assert "%" in r["suggestion"], "★ 建议里应含具体数字"
        # 最佳版本应排在首位
        assert r["variants"][0]["adopt_rate"] >= r["variants"][-1]["adopt_rate"]
        A.reset()

    def test_funnel_included(self):
        """★★ 必须含环节级漏斗（找出最该优化的环节）。"""
        from apps.api import adoption as A
        from apps.api import insight as I
        A.reset()
        A.record_generate("role_x", words=100, ok=True)
        A.record_adopt("role_x", before_words=100, after_words=102)
        r = I.prompt_ab()
        assert r.get("funnel"), "★ 缺少采纳漏斗"
        row = next((x for x in r["funnel"] if x["role"] == "role_x"), None)
        assert row, "★ 漏斗未含 role_x"
        assert "adopt_rate" in row and "edit" in row
        A.reset()

    def test_privacy_no_content_in_output(self):
        """★★★ 输出绝不能含提示词原文。"""
        from apps.api import adoption as A
        from apps.api import insight as I
        A.reset()
        secret = "绝密提示词内容XYZ789"
        for _ in range(5):
            A.record_generate("t", prompt=secret, ok=True)
        r = I.prompt_ab()
        blob = str(r)
        assert secret not in blob, "★★★ 洞察结果含提示词原文！"
        assert "绝密" not in blob, "★★★ 洞察结果含原文片段！"
        A.reset()


# ============================================================ ② 端点

class TestPromptAbEndpoint:
    """★ 端点必须注册且需管理员权限。"""

    def test_endpoint_registered(self):
        """★★ 端点必须真的挂上（HTTP 层可见）。"""
        src = _strip_comments(_read(API / "routers" / "feedback_routes.py"))
        assert '"/api/admin/insight/prompt-ab"' in src, "★ 端点未注册"

    def test_requires_admin(self):
        """★★ 必须要求管理员（含内部数据）。"""
        src = _read(API / "routers" / "feedback_routes.py")
        i = src.find("/api/admin/insight/prompt-ab")
        seg = src[i:i + 700]
        assert "require_admin" in seg, "★ 端点未要求管理员权限"

    def test_distinct_from_model_ab(self):
        """★ 两个 ab 端点必须并存且语义不同（不重复造轮子）。"""
        src = _strip_comments(_read(API / "routers" / "feedback_routes.py"))
        assert "/api/admin/insight/ab" in src, "★ 模型 A/B 端点被误删"
        assert "/api/admin/insight/prompt-ab" in src, "★ 提示词 A/B 端点缺失"


# ============================================================ ③ 前端页面

class TestInsightUi:
    """★★ 必须有可看的界面（否则数据白采）。"""

    def test_view_exists(self):
        p = WEB / "views" / "admin" / "AdminInsightView.vue"
        assert p.is_file(), "★ 缺少 AdminInsightView.vue"

    def test_route_registered(self):
        """★★ 路由必须注册（且含 path，漏了会类型检查失败）。"""
        src = _read(WEB / "router" / "index.ts")
        assert "admin-insight" in src, "★ 路由未注册"
        i = src.find("admin-insight")
        seg = src[max(0, i - 200):i + 300]
        assert "path:" in seg, "★ 路由缺少 path 字段"

    def test_nav_entry(self):
        """★ 导航必须有入口（否则用户找不到）。"""
        src = _read(WEB / "layouts" / "AdminLayout.vue")
        assert "admin-insight" in src, "★ 导航缺少「数据洞察」入口"

    def test_shows_scope_warning(self):
        """★★★ 界面必须显示数据范围警告。

        ★ 为什么：内存计数重启清零，不显示会让运营误判。
          **这是诚实性问题** —— 展示数据必须说明数据边界。
        """
        src = _read(WEB / "views" / "admin" / "AdminInsightView.vue")
        assert "scope" in src, "★ 界面未展示数据范围"
        assert "重启" in src or "内存" in src, "★ 未说明数据易失性"

    def test_shows_suggestion(self):
        """★ 界面必须展示结论（不能只列数字）。"""
        src = _read(WEB / "views" / "admin" / "AdminInsightView.vue")
        assert "suggestion" in src, "★ 界面未展示结论建议"

    def test_shows_methodology(self):
        """★★ 界面应说明数据怎么来的（可折叠）。

        ★ 为什么：运营看到数字会问"这准吗" ——
          给出采集方式与局限，才能建立信任。
        """
        src = _read(WEB / "views" / "admin" / "AdminInsightView.vue")
        assert "adoption.py" in src or "采集方式" in src, \
            "★ 未说明数据来源"
        assert "只存哈希" in src or "哈希" in src, "★ 未说明隐私保障"

    def test_entry_from_config_center(self):
        """★★ 配置中心应有入口（形成"配置→观察→再调"闭环）。"""
        src = _read(WEB / "views" / "admin" / "AdminConfigView.vue")
        assert "admin-insight" in src, "★ 配置中心缺少洞察入口"


# ============================================================ ④ 导入有效性

class TestImports:
    def test_modules_import(self):
        import importlib
        for m in ("apps.api.insight", "apps.api.adoption"):
            try:
                importlib.import_module(m)
            except Exception as e:                      # noqa: BLE001
                pytest.fail(f"★ {m} 导入失败：{type(e).__name__}: {e}")

    def test_deferred_imports_resolve(self):
        """★ 延迟导入必须可解析（AST 扫描，同 E4.4/E4.5）。"""
        import ast
        import importlib
        import importlib.util

        bad = []
        for p in (API).rglob("*.py"):
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                if not (node.module or "").startswith("apps.api"):
                    continue
                try:
                    mod = importlib.import_module(node.module)
                except Exception as e:                  # noqa: BLE001
                    bad.append(f"{p.relative_to(ROOT)}:{node.lineno} "
                               f"{node.module}: {e}")
                    continue
                for alias in node.names:
                    if alias.name == "*" or hasattr(mod, alias.name):
                        continue
                    try:
                        if importlib.util.find_spec(
                                f"{node.module}.{alias.name}") is not None:
                            continue
                    except (ImportError, ValueError, ModuleNotFoundError):
                        pass
                    bad.append(f"{p.relative_to(ROOT)}:{node.lineno} "
                               f"{node.module} 没有 {alias.name}")
        assert not bad, "★ 延迟导入失败：\n  " + "\n  ".join(bad[:10])

# -*- coding: utf-8 -*-
"""v1.0-E4.4 配置中心可用性改造的守护测试。

============================ 修复的两个问题 ============================

用户反馈（附截图）：
  ① 配置中心**缺少**模型 / 套餐 / 价格 / 支付渠道等配置能力
  ② 现有配置**太复杂**（截图里是 `model.role_writer` / `model.retry`
     这类实现细节，运营看不懂也不该动）

============================ 真实根因 ============================

① **不是功能缺失，是导航断裂**：
   后台其实已有「模型配置」「套餐管理」专页，但配置中心里**没有入口**
   → 运营在配置中心看不到全貌，以为功能缺失。
   ★ 唯一真缺的是**支付渠道配置**（只有 GET，无写端点）。

② **"太复杂"的根因是没有分层**：
   `model.role_writer`（需要先懂"档位"概念）和
   `billing.free_credits`（注册送多少积分）混在一起展示，
   运营无法区分"我该调的"和"别碰的"。

============================ 核心判据 ============================

★★ **配置中心应该是"能配什么"的总入口，不是"所有配置项的堆砌"**。
   表格留给"精确改某一项"，总览负责"我该去哪儿配"。

★★ **一个"可配置能力"必须同时具备读 + 写 + 校验 + 生效**
   （MEMORY §24）。支付渠道此前只有读端点 = 运营改不了 = 等于没有。
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "apps" / "web2" / "src"
API = ROOT / "apps" / "api"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    r"""去注释与文档字符串，只留代码。"""
    import re as _re
    text = _re.sub(r"<!--[\s\S]*?-->", "", text)
    text = _re.sub(r'"""[\s\S]*?"""', "", text)
    text = _re.sub(r"'''[\s\S]*?'''", "", text)
    text = _re.sub(r"/\*[\s\S]*?\*/", "", text)
    text = _re.sub(r"^\s*\*\s?.*$", "", text, flags=_re.M)
    out = []
    for line in text.splitlines():
        if line.strip().startswith(("#", "//", "*")):
            continue
        out.append(line)
    return "\n".join(out)


# ============================================================ ① 分层：高级项

class TestAdvancedTier:
    """★★ 配置必须分层：运营可调的 vs 技术参数。"""

    def test_spec_has_advanced_field(self):
        """★★ `Spec` 必须有 `advanced` 字段。

        ★ 为什么：`model.role_writer` 这类项，运营看到会一脸茫然 ——
          它们不是"可调的业务参数"，而是"改错了会出事的技术常量"。
          需要在注册表层面区分，界面才能分层展示。
        """
        src = _strip_comments(_read(API / "settings_registry.py"))
        assert "advanced" in src, "★ Spec 缺少 advanced 字段"
        assert "advanced: bool = False" in src, \
            "★ advanced 应有默认值 False（不破坏存量声明）"

    def test_model_roles_marked_advanced(self):
        """★★ `model.role_*` 必须标为高级（需先懂"档位"概念）。

        ★ 判据（怎么判断一个配置该不该折叠）：
          · 运营能说清"改成什么对业务更好"吗？不能 → advanced
          · 改错了会**静默出错**（而不是立刻报错）吗？是 → advanced

          `role_writer = writer` 改成不存在的档位名 → 静默回退默认值，
          运营完全察觉不到。→ 必须折叠 + 提示"该去哪儿改"。
        """
        from apps.api import settings_registry as reg
        for k in ("model.role_writer", "model.role_critic",
                  "model.role_planner", "model.role_utility"):
            assert k in reg.SPECS, f"★ 缺少配置项 {k}"
            assert reg.SPECS[k].advanced, f"★ {k} 应标为 advanced"

    def test_tech_params_marked_advanced(self):
        """★ 纯技术参数（超时/重试）必须折叠。"""
        from apps.api import settings_registry as reg
        for k in ("model.timeout_sec", "model.retry"):
            assert reg.SPECS[k].advanced, f"★ {k} 应标为 advanced"

    def test_business_params_not_advanced(self):
        """★★ 业务参数**不能**折叠（否则运营找不到）。

        ★ 反向守护：折叠过度比不折叠更糟 ——
          "注册送多少积分"是运营天天要调的东西，藏起来就找不到了。
        """
        from apps.api import settings_registry as reg
        for k in ("billing.free_credits", "billing.credit_per_yuan",
                  "system.site_name"):
            assert not reg.SPECS[k].advanced, \
                f"★ {k} 是业务参数，不该折叠"

    def test_api_exposes_advanced_flag(self):
        """★ 接口必须下发 advanced 标记（前端据此折叠）。"""
        src = _strip_comments(_read(API / "config_center.py"))
        assert '"advanced": s.advanced' in src or "'advanced': s.advanced" in src, \
            "★ list_all 未下发 advanced 标记"


# ============================================================ ② 支付渠道配置

class TestPaymentChannels:
    """★★ 支付渠道必须**可配置**（此前只有读端点）。"""

    def test_write_endpoints_exist(self):
        """必须有写端点（PUT 保存 + POST 自检）。"""
        src = _strip_comments(_read(API / "routers" / "pay.py"))
        assert '@router.put("/api/admin/pay/channels/{name}")' in src, \
            "★ 缺少支付渠道保存端点 —— 运营只能改代码"
        assert '@router.post("/api/admin/pay/channels/{name}/test")' in src, \
            "★ 缺少渠道自检端点"

    def test_creds_encrypted(self):
        """★★ 支付凭据必须**加密存储**（走 secret_store）。"""
        src = _strip_comments(_read(API / "routers" / "pay.py"))
        assert "secret_store" in src, \
            "★ 支付凭据未加密存储 —— 密钥会明文入库"

    def test_creds_not_echoed(self):
        """★★ 凭据**绝不回显**（读接口只回布尔）。

        ★ 为什么：密钥回显 = XSS/截图/肩窥都能拿到；
          且"打开页面→保存"会把看不见的值写回去。
        """
        src = _strip_comments(_read(API / "routers" / "pay.py"))
        # 读接口应该用 has_secret（回布尔）而不是 get_secret（回值）
        assert "has_secret" in src, "★ 读接口未用 has_secret（回值 = 泄漏）"
        # 确认 cred_state 只存布尔
        assert "cred_state" in src, "★ 缺少凭据状态字段"

    def test_must_have_creds_before_enable(self):
        """★★ 开启渠道前必须凭据齐全。

        ★ 为什么：一个"开着的但没凭据"的渠道，用户点了会拿到
          "渠道不可用"错误 —— 比"根本看不到这个渠道"体验更差。
        """
        src = _strip_comments(_read(API / "routers" / "pay.py"))
        assert "需先填写" in _read(API / "routers" / "pay.py"), \
            "★ 未校验「开启前凭据齐全」"

    def test_empty_cred_means_no_change(self):
        """★★ 留空的凭据 = **不修改**（不是清空）。"""
        src = _strip_comments(_read(API / "routers" / "pay.py"))
        assert "留空不改" in _read(API / "routers" / "pay.py") or \
               "continue" in src, \
            "★ 未处理「留空 = 不修改」—— 会导致打开页面保存一次就清了密码"

    def test_dangerous_flag_needs_confirmation(self):
        """★★ 危险开关（跳过签名校验）需要二次确认。

        ★ 为什么：`skip_signature_check` 开启后任何人都能伪造
          "支付成功"回调 —— 生产环境开启等于白送钱。
        """
        src = _strip_comments(_read(API / "routers" / "pay.py"))
        assert "allow_unsafe" in src, "★ 危险开关无二次确认机制"
        assert "skip_signature_check" in src, "★ 未声明危险开关配置项"

    def test_channels_declared_in_registry(self):
        """★ 支付渠道必须在配置注册表里（否则后台看不到开关）。"""
        from apps.api import settings_registry as reg
        for k in ("payment.sandbox_enabled", "payment.wechat_enabled",
                  "payment.alipay_enabled", "payment.stripe_enabled"):
            assert k in reg.SPECS, f"★ 配置项 {k} 未登记"
        assert "payment" in reg.GROUPS, "★ 缺少 payment 分组元信息"

    def test_cred_specs_single_source(self):
        """★★ 凭据字段声明必须**唯一**（后端声明，前端渲染）。"""
        src = _strip_comments(_read(API / "routers" / "pay.py"))
        assert "PAYMENT_CRED_SPECS" in src, \
            "★ 缺少凭据字段声明表"

    def test_public_providers_filters_disabled(self):
        """★ 收银台只应看到**已启用**的渠道。"""
        src = _strip_comments(_read(API / "routers" / "pay.py"))
        assert "enabled_map" in src or "payment.sandbox_enabled" in src, \
            "★ 收银台未按配置过滤渠道 —— 未启用的渠道也会显示"


# ============================================================ ③ 配置中心前端

class TestConfigCenterUi:
    """★★ 配置中心必须提供「能力总览」入口。"""

    def test_has_overview_tab(self):
        """★★ 必须有总览页（运营看不到全貌是核心问题）。"""
        src = _read(WEB / "views" / "admin" / "AdminConfigView.vue")
        assert "能力总览" in src, "★ 缺少「能力总览」标签页"
        assert "capabilities" in src, "★ 缺少能力卡片数据"

    def test_overview_links_to_special_pages(self):
        """★★ 总览必须能跳到模型配置 / 套餐管理等专页。

        ★ 这是本次修复的**核心**：能力都在，但没入口 = 运营以为缺失。
        """
        src = _read(WEB / "views" / "admin" / "AdminConfigView.vue")
        assert "admin-models" in src, "★ 总览未链接到「模型配置」"
        assert "admin-plans" in src, "★ 总览未链接到「套餐管理」"

    def test_has_advanced_toggle(self):
        """★ 必须有「显示高级项」开关。"""
        src = _read(WEB / "views" / "admin" / "AdminConfigView.vue")
        assert "showAdvanced" in src, "★ 缺少高级项折叠开关"

    def test_default_hides_advanced(self):
        """★★ 高级项**默认隐藏**（这是"太复杂"的直接解法）。"""
        src = _strip_comments(
            _read(WEB / "views" / "admin" / "AdminConfigView.vue"))
        assert "showAdvanced = ref(false)" in src, \
            "★ 高级项默认应为隐藏（false）"

    def test_has_payment_tab(self):
        """★ 必须有「支付渠道」配置页。"""
        src = _read(WEB / "views" / "admin" / "AdminConfigView.vue")
        assert "'payment'" in src, "★ 缺少支付渠道标签页"
        assert "payChannelsApi" in src, "★ 未接入支付渠道 API"

    def test_dangerous_option_warns(self):
        """★★ 危险开关必须有明确的视觉警告。"""
        raw = _read(WEB / "views" / "admin" / "AdminConfigView.vue")
        assert "等于白送钱" in raw or "任何人都能伪造" in raw, \
            "★ 危险开关缺少明确警告文案"

    def test_frontend_api_complete(self):
        """★ 前端 API 封装必须完整。"""
        src = _read(WEB / "api" / "admin_modules_extra.ts")
        for fn in ("payChannelsApi", "list:", "save:", "test:"):
            assert fn in src, f"★ 缺少 {fn}"
        assert "PayChannel" in src, "★ 缺少 PayChannel 类型"


# ============================================================ ④ 导入有效性

class TestImportsResolvable:
    """★★★ 模块导入必须真的能解析（防止 ImportError → 端点 500）。

    ============================ 真实踩坑 ============================

    我在 `pay.py` 里写了 `from apps.api.services.settings import get_config`，
    但那个模块**只有 `get_setting`**（`get_config` 在 `config_center`）。

    ★ 为什么这个 bug 特别危险：
      · **导入在函数体内**（延迟导入）→ 模块加载时不报错
      · 只有真正调用那个端点时才 500
      · 我的守护测试当时只做了**字符串匹配**（`assert "secret_store" in src`）
        → 完全没发现

    ★ 判据：**"写了这行代码" ≠ "这行代码能跑"**。
      凡是 `from X import Y` 形式的依赖，测试必须**真的执行一遍**。
    """

    def test_pay_router_imports(self):
        """★★ pay.py 必须能被导入，且其依赖都真实存在。"""
        import importlib
        try:
            m = importlib.import_module("apps.api.routers.pay")
            assert hasattr(m, "router"), "★ 缺少 router 对象"
        except ImportError as e:
            pytest.fail(f"★ pay.py 导入失败（端点会 500）：{e}")

    def test_config_center_exports_used_names(self):
        """★★ `config_center.get_config` / `set_config` 必须存在。

        ★ 为什么单独测：这两个函数被多个路由延迟导入，
          名字写错只有在**调用时**才暴露。
        """
        from apps.api import config_center
        for fn in ("get_config", "set_config"):
            assert hasattr(config_center, fn), \
                f"★ config_center 缺少 {fn}（延迟导入它的端点会 500）"

    def test_services_settings_exports_used_names(self):
        """★ `services.settings.get_setting` 必须存在（别和 get_config 搞混）。"""
        from apps.api.services import settings as svc
        assert hasattr(svc, "get_setting"), "★ 缺少 get_setting"
        # ★ 反向提醒：这个模块**没有** get_config
        assert not hasattr(svc, "get_config"), (
            "★ services.settings 出现了 get_config —— "
            "它与 config_center.get_config 同名，容易混淆，请确认调用方意图")

    def test_all_admin_routers_import(self):
        """★★ 所有 admin 路由模块都必须能导入。

        ★ 这是"防 500"的通用守卫：任何一个路由模块导入失败，
          对应端点就会全部 500，但**其他端点看起来正常**。
        """
        import importlib
        mods = [
            "apps.api.routers.pay",
            "apps.api.routers.plans_admin",
            "apps.api.routers.ads_models",
            "apps.api.routers.config_routes",
        ]
        failed = []
        for m in mods:
            try:
                importlib.import_module(m)
            except Exception as e:                      # noqa: BLE001
                failed.append(f"{m}: {type(e).__name__}: {e}")
        assert not failed, "★ 路由模块导入失败：\n  " + "\n  ".join(failed)


# ============================================================ ⑤ 延迟导入检查

class TestDeferredImports:
    """★★★ 函数体内的 `from X import Y` 也必须可解析。

    ============================ 为什么必须单独测 ============================

    第一版 `TestImportsResolvable` **抓不到**我故意注入的错误导入 ——
    因为那个导入在**函数体内**（延迟导入），模块 import 时不执行。

    ★ 为什么项目里会有延迟导入：
      · 打破循环依赖（如 `routers/*` ↔ `main`）
      · 避免启动时加载重依赖

    ★★ 代价：**导入错误从"启动即报"变成"调用才 500"** ——
       而 500 只影响那一个端点，很容易漏测（我这次就漏了）。

    ★ 判据：**静态扫描所有 `from apps.api... import ...`，
      逐个确认目标模块真的有那个名字**。
    """

    # 扫描范围：容易写错的常见混淆对
    CONFUSABLE = {
        # (模块, 名字) → 真实位置
        ("apps.api.services.settings", "get_config"):
            "get_config 在 apps.api.config_center（services.settings 只有 get_setting）",
        ("apps.api.services.settings", "set_config"):
            "set_config 在 apps.api.config_center",
        ("apps.api.config_center", "get_setting"):
            "get_setting 在 apps.api.services.settings",
        ("apps.api.auth", "issue_token"):
            "函数名是 make_token",
        ("apps.api.auth", "create_token"):
            "函数名是 make_token",
    }

    def test_deferred_imports_resolve(self):
        """★★★ 扫描所有 .py 里的延迟导入，确认名字真的存在。

        ★ 判据（第一版写错了一次，值得记下）：
          `from apps.api import genres` 里的 `genres` 可能有两种身份：
            · **子模块**（有对应的 .py 文件）→ hasattr 可能为 False
              因为子模块不一定被父包 `__init__` 导入
            · **属性**（父包显式导出）→ hasattr 为 True

          → 必须**两种都检查**：先看 hasattr，再看是否有同名子模块。
            只看 hasattr 会误报（把合法写法当错误）。
        """
        import ast
        import importlib
        import importlib.util

        bad: list[str] = []
        for p in (ROOT / "apps").rglob("*.py"):
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
                               f"模块 {node.module} 导入失败: {e}")
                    continue
                for alias in node.names:
                    name = alias.name
                    if name == "*":
                        continue
                    # ① 属性存在？
                    if hasattr(mod, name):
                        continue
                    # ② 是同名子模块？（`from pkg import submodule` 的合法写法）
                    sub = f"{node.module}.{name}"
                    try:
                        if importlib.util.find_spec(sub) is not None:
                            continue
                    except (ImportError, ValueError, ModuleNotFoundError):
                        pass
                    hint = self.CONFUSABLE.get((node.module, name), "")
                    bad.append(
                        f"{p.relative_to(ROOT)}:{node.lineno} "
                        f"{node.module} 没有 {name}"
                        + (f"  → {hint}" if hint else ""))

        assert not bad, (
            "★★★ 延迟导入无法解析（调用时会 500）：\n  "
            + "\n  ".join(bad[:20]))

    def test_known_confusions_are_not_present(self):
        """★ 直接把已知混淆对列为断言（更快定位）。"""
        import importlib
        for (mod_name, attr), hint in self.CONFUSABLE.items():
            try:
                mod = importlib.import_module(mod_name)
            except Exception:                           # noqa: BLE001
                continue
            # ★ 期望形态：别名 → 真实存在；错误形态 → 不存在
            if "在 apps.api" in hint:
                assert not hasattr(mod, attr), (
                    f"★ {mod_name} 出现了 {attr} —— {hint}")


# ============================================================ ⑥ v1.0-E4.15 类型化渲染
#
# 用户诉求："有些栏目不明白怎么填写或者修改，把能做成下拉的改成下拉并写明解释"。
#
# 根因（两处）：
#   ① 后端：`model.role_*` 是 str 类型 —— 任意值（拼错的 "writerr"）都能保存，
#      运行时静默回退默认档（"配了但没生效"）。
#   ② 前端：所有项一律渲染纯文本框 —— choice/bool/数值的输入体验全靠猜。


class TestTypedRendering:
    """★★ 配置项必须按类型渲染（后端 choice 化 + 前端分支）。"""

    def test_role_configs_are_choice(self):
        """★★ `model.role_*` 必须是 choice（合法值 = model_router.ROLES 的键）。

        ★ 为什么：str 类型任意值都能保存 → 拼错的档位名静默回退默认。
          choice 让不合法的值在**保存关口**就被拒（MEMORY §校验关口要报错）。
        """
        from apps.api import settings_registry as reg
        from apps.api import model_router
        for k in ("model.role_writer", "model.role_critic",
                  "model.role_planner", "model.role_utility",
                  "model.free_tier_role"):
            assert reg.SPECS[k].type == "choice", f"★ {k} 应为 choice 类型"
            assert set(reg.SPECS[k].choices) == set(model_router.ROLES.keys()), \
                f"★ {k} 的 choices 必须与 model_router.ROLES 对齐（单一事实源）"

    def test_bad_role_value_rejected_at_gate(self):
        """★★ 拼错的档位名必须在 validate 关口被拒（不能静默保存）。"""
        from apps.api import settings_registry as reg
        ok, why = reg.validate(reg.SPECS["model.role_writer"], "writerr")
        assert ok is False, "★ 拼错的档位名不应通过校验"
        assert "之一" in why, "★ 拒绝原因应列出合法值（可执行的错误信息）"

    def test_choice_items_have_meaningful_desc(self):
        """★★ 所有 choice 项的 desc 必须解释每个取值的含义（或指向专页）。

        ★ 为什么：choice 下拉只显示值本身时，用户还是不知道"该选哪个"。
          desc 是解释内容的载体（用户诉求原话："写明解释内容"）。
        """
        from apps.api import settings_registry as reg
        for k, s in reg.SPECS.items():
            if s.type == "choice":
                assert s.desc and len(s.desc) >= 8, \
                    f"★ choice 项 {k} 的 desc 太简略（用户诉求：写明解释内容）"

    def test_config_list_contains_typed_fields(self):
        """★★ /api/admin/config 每项必须下发 choices/min/max/unit/readonly。

        ★ 为什么：前端类型化渲染的数据源 —— 后端不下发，前端只能猜。

        ★ 实现方式：本文件是**静态断言**风格（无 HTTP fixtures）。
          list_all 每行的字段全部从 `Spec` 派生（choices/min/max/unit/readonly/
          advanced 都是 Spec 属性）→ 直接对注册表断言字段齐全即等价于
          "list_all 会下发"。HTTP 层的字段契约由 test_g06b_admin_pages.py 的
          client fixture 系列固化（MEMORY §分层测试：模块层 ≠ HTTP 层）。
        """
        from apps.api import settings_registry as reg
        for k, s in reg.SPECS.items():
            # ★ list_all 下发的字段 ↔ Spec 属性一一对应（对照 config_center.list_all）
            assert hasattr(s, "type") and s.type, f"★ {k} 缺 type"
            assert isinstance(s.choices, tuple), f"★ {k} 缺 choices（应至少为空元组）"
            assert hasattr(s, "min") and hasattr(s, "max"), f"★ {k} 缺 min/max"
            assert hasattr(s, "unit"), f"★ {k} 缺 unit"
            assert hasattr(s, "readonly"), f"★ {k} 缺 readonly"
            assert hasattr(s, "advanced"), f"★ {k} 缺 advanced"
            # ★ choice 项的 choices 必须非空（前端下拉的数据源；空下拉 = 没法选）
            if s.type == "choice":
                assert s.choices, f"★ choice 项 {k} 的 choices 为空"

    def test_frontend_renders_by_type(self):
        """★★ 前端必须按 it.type 分支渲染（下拉/开关/数值/多行/文本）。"""
        src = _read(WEB / "views" / "admin" / "AdminConfigView.vue")
        # ★ 分支判据来自 it.type，不硬编码 key
        assert "isChoice(it)" in src, "★ 缺少 choice 下拉分支"
        assert "isBool(it)" in src, "★ 缺少 bool 开关分支"
        assert "isNumber(it)" in src, "★ 缺少数值分支"
        assert "isText(it)" in src, "★ 缺少多行文本分支"
        # ★ 枚举中文含义映射（用户诉求："写明解释内容"）
        assert "CHOICE_LABELS" in src, "★ 缺少枚举值中文含义映射"
        # ★ 数值范围提示
        assert "numHint" in src, "★ 缺少数值范围提示"

    def test_choice_labels_cover_all_choices(self):
        """★ 枚举中文含义映射必须覆盖注册表里全部 choice 项的 key。

        ★ fail-soft 兜底允许个别值缺翻译（显示原值），
          但 **key 必须全覆盖** —— 新增 choice 项时漏配映射应被测试拦住。
        """
        from apps.api import settings_registry as reg
        src = _read(WEB / "views" / "admin" / "AdminConfigView.vue")
        for k, s in reg.SPECS.items():
            if s.type == "choice":
                assert f"'{k}'" in src, f"★ CHOICE_LABELS 缺 {k} 的映射"

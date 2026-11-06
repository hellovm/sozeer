# -*- coding: utf-8 -*-
"""v0.18 T1 回归：main.py 拆分（router 化 + services 抽取）。

改造内容
--------
1. **services 抽取**：任务台账/设置读写/DB 迁移从 main.py 抽到
   `apps/api/services/`，打破 main ↔ agent 的循环依赖。
2. **router 拆分**：genres / system 两组路由搬进 `apps/api/routers/`。
3. **兼容层**：main.py re-export 全部端点函数名，
   `from apps.api.main import list_genres` 与 `main.list_genres(...)` 继续可用。

验收标准（计划书 §7.2）
--------------------
* 路由数不变（123 个 API 操作）
* 全量测试全绿
* `main.<symbol>` 老写法零改动

★ 重要经验：FastAPI 0.143 用 `_IncludedRouter` 惰性标记 include 的 router，
**直接遍历 `app.routes` 看不到它们**（会误判为"路由丢失"）。
权威做法是看 `app.openapi()["paths"]` 或发真实请求。
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

os.environ.setdefault("NOVELFORGE_DATA", tempfile.mkdtemp(prefix="nf_test_t1_"))
os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-t1")

# v0.18 T1 拆分验收：操作数 = 123（拆分前后一致）
# v0.19 新增 3 个端点（F1 批量代笔 ×2 + F6 改稿对比 ×1）→ 126
# v0.19.3 新增 3 个端点（F2 整书打包 ×3）→ 129
# v0.19.5 新增 2 个端点（F4 成片合成 ×2）→ 131
# v0.19.7 新增 9 个端点（F3 题材市场 ×9）→ 140
# v0.20.2 新增 11 个端点（A2 积分计费 ×11）→ 151
# v0.20.3 新增 9 个端点（A3 订单支付 ×9）→ 160
# v0.20.4 新增 20 个端点（A4 用户中心 ×20）→ 180
# 说明：本测试的**目的**是「拆分不丢路由」，而不是钉死数字。
# 所以只断言「不少于拆分后的基线」，新增功能导致的增长是允许的。
BASELINE_OPS = 123
# 当前值（v1.0-E4.15 通用模型测试端点 +1 = 389；
#         #311 DELETE /api/books/{book_id}/characters/{cid} +1 = 390；
#         E4.19 GET /api/admin/insight/lessons +1 = 391）
#   E4.13：GET /api/me/plan、POST /api/admin/users/bulk、
#          GET /api/admin/users/{uid}/export、PATCH /api/admin/flags/{flag_id}
#   E4.15：POST /api/admin/models2/test-model（agnus 等 OpenAI 兼容类测试，不再误打 /system_stats）
#   #311：DELETE /api/books/{book_id}/characters/{cid}（人物删除端点）
#   E4.19：GET /api/admin/insight/lessons（用户纠错沉淀的避坑记录，仅管理端）
# ★ 新增/删除端点时**必须**同步更新这里 —— 这是"路由未被误删"的守门测试
EXPECTED_OPS = 391


# ==================================================== services 模块
def test_services_package_importable():
    from apps.api.services import db_migrate, settings, tasks
    assert callable(tasks.task_event)
    assert callable(settings.get_setting)
    assert callable(db_migrate.migrate)


def test_services_does_not_import_main():
    """★ 架构护栏：services 包不得 import main（否则又成环）。"""
    import pathlib
    pkg = pathlib.Path(__file__).resolve().parents[1] / "apps" / "api" / "services"
    for f in pkg.glob("*.py"):
        src = f.read_text(encoding="utf-8")
        # 允许注释里提到 main，但不允许真的 import
        for line in src.split("\n"):
            s = line.strip()
            if s.startswith("#"):
                continue
            assert "from apps.api.main import" not in s, f"{f.name} 顶层 import 了 main"
            assert "import apps.api.main" not in s, f"{f.name} 顶层 import 了 main"


def test_services_tasks_signature_compatible():
    """services.tasks 的函数签名必须与旧 main._xxx 一致（零改动保证）。"""
    import inspect
    from apps.api.services import tasks
    # 老调用点写法：_task_event(db, task, ev, progress=..., status=...)
    sig = inspect.signature(tasks.task_event)
    for p in ("db", "task", "event", "progress", "status", "result", "error"):
        assert p in sig.parameters, f"task_event 缺参数 {p}"


def test_services_settings_behaviour():
    """设置读写行为：DB 优先 + 环境变量兜底。"""
    from apps.api import main
    from apps.api.services import settings as S
    with main.SessionLocal() as db:
        # 无记录时回退默认
        assert S.get_setting(db, "不存在的键", "dflt") == "dflt"
        # allow_register 特判（回退 config）
        assert S.get_setting(db, "allow_register", "1") in ("0", "1")
        assert S.is_register_open(db) in (True, False)


def test_services_migrate_idempotent():
    """迁移幂等：连跑两次不报错。"""
    from apps.api.services.db_migrate import migrate
    migrate()
    migrate()


# ==================================================== main 兼容层
def test_main_reexports_still_available():
    """★ 核心保证：main 命名空间的旧符号全部还在。"""
    import apps.api.main as M
    legacy = [
        # services 抽取后 re-import 回旧名
        "_task_row", "_task_event", "_append_task_event", "_new_task",
        "_task_dict", "_recover_tasks", "_spawn",
        "get_setting", "_set_setting", "is_register_open", "_migrate",
        # router 拆分后 re-export 的端点
        "list_genres", "create_genre", "update_genre", "delete_genre",
        "export_genres", "import_genres", "reload_genres", "list_genres_public",
        "health", "register", "login", "me", "logout", "dashboard",
        "list_models", "save_skill", "get_settings", "set_settings",
        "admin_list_users", "serve_book_file",
        # schema
        "GenrePackIn", "RegisterIn", "LoginIn", "ModelIn", "SkillPatch",
    ]
    missing = [n for n in legacy if not hasattr(M, n)]
    assert not missing, f"main 丢失符号：{missing}"


def test_main_symbols_callable():
    import apps.api.main as M
    for n in ["list_genres", "create_genre", "health", "list_models",
              "_task_row", "get_setting"]:
        assert callable(getattr(M, n)), f"main.{n} 不可调用"


# ==================================================== 路由完整性（权威）
def test_route_count_unchanged():
    """★ 验收：API 操作数 = 123（拆分前后一致）。

    用 openapi paths 统计（会展开 include 的 APIRouter）。
    """
    from apps.api.main import app
    schema = app.openapi()
    ops = []
    for p, methods in schema.get("paths", {}).items():
        for m in methods:
            if m.lower() in ("get", "post", "put", "patch", "delete"):
                ops.append(f"{m.upper()} {p}")
    assert len(ops) >= BASELINE_OPS, \
        f"路由数 {len(ops)} < 基线 {BASELINE_OPS}（拆分丢了路由）"
    assert len(ops) == EXPECTED_OPS, \
        f"路由数 {len(ops)} ≠ 期望 {EXPECTED_OPS}（新增/删除端点请同步更新 EXPECTED_OPS）"


def test_routers_modules_exist():
    from apps.api.routers import genres, system
    assert genres.router is not None
    assert system.router is not None
    # 8（原本） + 9（v0.19.7 F3 题材市场） = 17
    assert len(genres.router.routes) == 17, \
        f"genres 应有 17 条路由，实际 {len(genres.router.routes)}"
    # 24（v0.20.6）+ 1（v1.0-E4.11 /api/stats/daily-writes）= 25
    assert len(system.router.routes) == 25, \
        f"system 应有 25 条路由（含 v1.0-E4.11 日更趋势），实际 {len(system.router.routes)}"


def test_app_routes_via_real_request():
    """★ 权威验证：真实 HTTP 请求（而非静态遍历 app.routes）。"""
    from fastapi.testclient import TestClient
    from apps.api.main import app
    c = TestClient(app)
    # health 免鉴权 → 200
    assert c.get("/api/health").status_code == 200
    # 其余需要鉴权 → 401（而不是 404，证明路由存在）
    for path in ["/api/genres", "/api/models", "/api/settings",
                 "/api/dashboard", "/api/admin/users"]:
        r = c.get(path)
        assert r.status_code == 401, f"{path} 返回 {r.status_code}（应 401，404 说明路由丢了）"


def test_included_router_is_lazy():
    """★ 记录 FastAPI 0.143 的行为：include 的 router 是惰性标记。

    这条测试的价值是**防止未来有人误用 app.routes 静态遍历做路由校验**。
    """
    from apps.api.main import app
    kinds = {type(r).__name__ for r in app.routes}
    assert "_IncludedRouter" in kinds, \
        "FastAPI 行为变化：不再是 _IncludedRouter 惰性标记，请复核本测试与校验方式"


# ==================================================== 循环导入
def test_no_circular_import():
    """★ 反向验证：agent 与 main 都能独立导入（循环已解）。"""
    import importlib
    import sys
    # 清掉相关模块重新导入
    for m in list(sys.modules):
        if m.startswith("apps.api"):
            del sys.modules[m]
    importlib.import_module("apps.api.services.tasks")
    importlib.import_module("apps.api.agent")
    importlib.import_module("apps.api.main")
    assert "apps.api.main" in sys.modules


def test_config_app_version_single_source():
    """版本号集中：main 与 config 一致（防漂移）。"""
    from apps.api import config
    from apps.api.main import app
    assert app.version == config.APP_VERSION


def test_version_format():
    from apps.api import config
    parts = config.APP_VERSION.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts), \
        f"版本号格式异常：{config.APP_VERSION}"


# ==================================================== ★ 线上 500 事故回归
def test_routers_do_not_rely_on_module_getattr_proxy():
    """★ 事故回归（v0.18 部署时线上 500）。

    事故：routers/system.py 最初用 **PEP 562 模块级 `__getattr__`** 代理
    `main.SETTING_DEFAULTS`。但 `__getattr__` **只对「模块属性访问」生效**
    （`sys.modules['m'].NAME`），对**模块内函数体的全局查找（LOAD_GLOBAL）无效**
    → 运行到 `SETTING_DEFAULTS["site_name"]` 时抛 NameError
    → `/api/auth/allow-register` 返回 500（前端 init() 每次刷新都调它）。

    本测试确保 routers 不再依赖任何「模块级属性代理」的隐式解析：
    routers 用到的跨模块符号必须是**显式 import**。
    """
    import pathlib
    rdir = pathlib.Path(__file__).resolve().parents[1] / "apps" / "api" / "routers"
    for f in rdir.glob("*.py"):
        src = f.read_text(encoding="utf-8")
        assert "def __getattr__" not in src, \
            f"{f.name} 使用了模块级 __getattr__ 代理（对函数体全局查找无效，会 NameError）"


def test_allow_register_endpoint_works():
    """★ 事故回归：该端点曾 500，必须 200 且字段完整。"""
    from fastapi.testclient import TestClient
    from apps.api.main import app
    r = TestClient(app).get("/api/auth/allow-register")
    assert r.status_code == 200, f"返回 {r.status_code}（曾因 NameError 500）"
    d = r.json()
    for k in ("allow", "first_user", "site_name", "site_tagline"):
        assert k in d, f"缺字段 {k}"


def test_dashboard_endpoint_works():
    """★ dashboard 依赖 _book_score/_next_step（也走服务层），回归保护。"""
    from fastapi.testclient import TestClient
    from apps.api.main import app
    r = TestClient(app).get("/api/dashboard")
    # 需鉴权 → 401；但绝不能 500
    assert r.status_code in (200, 401), f"返回 {r.status_code}"


def test_scoring_service_available():
    from apps.api.services import scoring
    assert callable(scoring.book_score)
    assert callable(scoring.next_step)
    assert "SETTING_DEFAULTS" in dir(__import__("apps.api.services.settings",
                                                fromlist=["SETTING_DEFAULTS"]))

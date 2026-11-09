"""pytest 共享环境：必须在任何测试模块 import apps.api.* 之前生效。

历史坑：test_v141 顶部 import apps.api.models（注册表）发生在收集期，
若彼时环境变量已被 test_v140 的硬赋值覆盖，config.DATA_DIR 就指向
v140 的临时目录，test_core::test_data_dir_isolated 必挂。conftest
由 pytest 最先加载，在这里一次性定死隔离目录，各测试文件一律
setdefault（不再各自硬赋值）。
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="nf_test_")
os.environ["NOVELFORGE_DATA"] = _TMP
os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-for-pytest-only")

# ★★★ v1.0-E4.11：排除 test_import.py（它是"手工跑的冒烟脚本"，不是单测）
#
# 根因（实测踩坑）：该脚本模块级执行 `SRC = sys.argv[1] if len(sys.argv) > 1`
# —— pytest 收集期 sys.argv[1] 是 **pytest 自己的参数**（如 tests/test_import.py），
# 于是它把 **测试文件路径当小说目录导入**，产出 0 章的书 → chs[11] IndexError，
# 收集直接中断，拖垮全量。它本来就是 `python tests/test_import.py <小说目录>`
# 的手工工具，CI/全量一律 `--ignore`，但忘了带 --ignore 的调用会炸。
# 在 conftest 里声明 collect_ignore 是双保险（文件自身改动也不影响）。
collect_ignore_glob = ["test_import.py"]

# ★★★ 禁止在测试进程内启动后台队列 worker（v0.26.2）
#
# 为什么必须禁：
#   `app.on_event("startup")` 会调 `drama_queue.ensure_worker()`，而
#   `drama_queue._worker` 是**模块级全局** —— 一旦启动就跨测试存活，
#   并且它持有 `SessionLocal()` 的会话（每 POLL_INTERVAL 轮询一次）。
#
#   测试里为了隔离会把 `main/auth/db` 三处 `SessionLocal` 全部 monkeypatch
#   成指向临时库的 sessionmaker；此时**上一个测试遗留的 worker** 仍在用
#   旧 session 轮询 → 与新的 teardown 竞争 → 抛
#   `IllegalStateChangeError: close() can't be called here;
#    commit() is already in progress`
#   （症状：单跑该文件 77 passed，全量跑时随机 2 个用例失败 —— 典型的
#     跨测试污染，极难定位）。
#
#   本项目的后台 worker 属于**运行时基础设施**，不是被测对象：
#   批量生成端点在 worker 未启动时会返回明确错误提示，不影响契约测试。
#
# ★ 与 MEMORY §62「测试模块实例分裂」同源：全局单例 + 多测试 = 必须显式隔离
os.environ["NOVELFORGE_DISABLE_WORKERS"] = "1"



# ────────────────────────────────────────────────────────────────
# ★ 个人版专属：skip 依赖已剥离资源（deploy/ 运维脚本、旧前台 apps/web、
#   商业 pay/credits/queue/plans 端点、cloud_video、队列前端等）的商业用例。
#   这些用例在个人版无意义且必失败，但同文件仍有大量有效通用用例，故按用例
#   粒度 skip 而非整删文件。由 deploy/make_opensource.py 生成，请勿手改清单
#   （如需增删，请改生成脚本的 PERSONAL_SKIP_CASES 后重新生成）。
import pytest as _personal_pytest

_PERSONAL_SKIP_CASES = {
    'tests/test_e4_10_ci_audit.py::TestCiGate::test_smoke_disables_ratelimit',
    'tests/test_e4_10_ci_audit.py::TestCiGate::test_smoke_script_runnable_offline',
    'tests/test_e4_10_ci_audit.py::TestCiGate::test_smoke_step_in_test_job',
    'tests/test_e4_10_ci_audit.py::TestCiGate::test_workflow_has_smoke_step',
    'tests/test_e4_10_ci_audit.py::TestCriticalImports::test_ingest_py_imports',
    'tests/test_e4_10_ci_audit.py::TestCriticalImports::test_no_deferred_models_import_in_ingest',
    'tests/test_e4_10_ci_audit.py::TestDualModeRobustness::test_schema_generation_not_http',
    'tests/test_e4_10_ci_audit.py::TestDualModeRobustness::test_sys_path_has_cwd_fallback',
    'tests/test_e4_10_ci_audit.py::TestDualModeRobustness::test_uses_module_level_urllib',
    'tests/test_e4_10_ci_audit.py::TestImportAudit::test_current_code_clean',
    'tests/test_e4_10_ci_audit.py::TestImportAudit::test_four_checks_implemented',
    'tests/test_e4_10_ci_audit.py::TestImportAudit::test_script_exists',
    'tests/test_e4_10_ci_audit.py::TestImportAudit::test_script_syntax',
    'tests/test_e4_10_ci_audit.py::TestImportAudit::test_skips_try_blocks',
    'tests/test_e4_10_ci_audit.py::TestImportAudit::test_uses_symtable',
    'tests/test_e4_11_p0p1.py::test_media_dispatches_cloud_video_provider',
    'tests/test_e4_11_p0p1.py::test_media_skip_billing_for_free_cloud_channel',
    'tests/test_e4_11_p0p1.py::test_plans_endpoints_use_config_update_not_model_manage',
    'tests/test_e4_16_polish.py::test_fix_brand_rename_script_idempotent_shape',
    'tests/test_e4_16_polish.py::test_footer_has_version_and_layout_has_pricing_route',
    'tests/test_e4_4_config_ux.py::TestImportsResolvable::test_all_admin_routers_import',
    'tests/test_e4_4_config_ux.py::TestImportsResolvable::test_pay_router_imports',
    'tests/test_e4_4_config_ux.py::TestPaymentChannels::test_cred_specs_single_source',
    'tests/test_e4_4_config_ux.py::TestPaymentChannels::test_creds_encrypted',
    'tests/test_e4_4_config_ux.py::TestPaymentChannels::test_creds_not_echoed',
    'tests/test_e4_4_config_ux.py::TestPaymentChannels::test_dangerous_flag_needs_confirmation',
    'tests/test_e4_4_config_ux.py::TestPaymentChannels::test_empty_cred_means_no_change',
    'tests/test_e4_4_config_ux.py::TestPaymentChannels::test_must_have_creds_before_enable',
    'tests/test_e4_4_config_ux.py::TestPaymentChannels::test_public_providers_filters_disabled',
    'tests/test_e4_4_config_ux.py::TestPaymentChannels::test_write_endpoints_exist',
    'tests/test_e4_9_endpoint_smoke.py::TestExceptionHandlers::test_validation_error_still_422',
    'tests/test_e4_9_endpoint_smoke.py::TestSmokeScript::test_script_exists',
    'tests/test_e4_9_endpoint_smoke.py::TestSmokeScript::test_script_syntax',
    'tests/test_v027_main_domain.py::test_api_not_shadowed_by_catch_all',
    'tests/test_v027_main_domain.py::test_path_traversal_blocked',
    'tests/test_v180_t1_split.py::test_route_count_unchanged',
    'tests/test_v180_t1_split.py::test_routers_modules_exist',
    'tests/test_v190_stability.py::test_backup_retention',
    'tests/test_v190_stability.py::test_backup_script_has_safety',
    'tests/test_v190_stability.py::test_backup_script_runs',
    'tests/test_v190_stability.py::test_backup_script_syntax',
    'tests/test_v190_stability.py::test_backup_scripts_exist',
    'tests/test_v190_stability.py::test_logrotate_config_exists',
    'tests/test_v190_stability.py::test_restore_script_backs_up_before_overwrite',
    'tests/test_v208_admin_ws.py::test_bulk_adjust_credits',
    'tests/test_v208_admin_ws.py::test_me_plan_free_for_new',
    'tests/test_v208_admin_ws.py::test_me_usage_has_new_fields',
    'tests/test_v209_admin_security.py::test_credit_adjust_writes_audit_with_diff',
    'tests/test_v209_admin_security.py::test_pay_events_filter_and_total',
    'tests/test_v209_admin_security.py::test_pay_events_raw_returns_raw_body',
    'tests/test_v209_admin_security.py::test_refund_writes_audit_with_before_snapshot',
    'tests/test_v254_config.py::TestEnvFileSafety::test_deploy_runs_both_fixes',
    'tests/test_v254_config.py::TestEnvFileSafety::test_key_migration_script_exists',
    'tests/test_v254_config.py::TestEnvFileSafety::test_quoting_fixer_script_exists',
    'tests/test_v256_queue_concurrency.py::test_admin_has_queue_page',
    'tests/test_v256_queue_concurrency.py::test_user_frontend_has_queue_watch',
    'tests/test_v25_queue.py::test_setup_script_exists',
    'tests/test_v25_queue.py::test_setup_script_has_safety_configs',
    'tests/test_v25_queue.py::test_setup_script_idempotent_marker',
}


def pytest_collection_modifyitems(config, items):
    _n = 0
    for it in items:
        _base = it.nodeid.split("[", 1)[0]
        if _base in _PERSONAL_SKIP_CASES:
            it.add_marker(_personal_pytest.mark.skip(
                reason="个人版已剥离该用例依赖的商业/运维资源"))
            _n += 1

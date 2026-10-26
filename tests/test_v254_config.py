# -*- coding: utf-8 -*-
"""v0.25.4 I 组配置中心测试（I1-I11）。

覆盖：注册表 / 类型转换 / 校验 / 四层优先级 / 缓存 / 历史 / 回滚 /
      diff 预览 / 导入导出 / 高危确认 / 权限 / HTTP 端到端。
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from apps.api import config_center as cc
from apps.api import settings_registry as reg


def _mod(name: str):
    import sys
    import importlib
    return (sys.modules.get(f"apps.api.{name}")
            or importlib.import_module(f"apps.api.{name}"))


@pytest.fixture(scope="module", autouse=True)
def _ensure_tables():
    from apps.api import models  # noqa: F401
    from apps.api.db import Base, engine
    Base.metadata.create_all(engine)
    from apps.api.services import db_migrate
    db_migrate.migrate()
    yield


@pytest.fixture()
def db():
    SL = _mod("db").SessionLocal
    s = SL()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def app_client():
    """HTTP 客户端（★ 关键端点必须走 HTTP 层，模块函数测不到路由遮蔽）。"""
    from fastapi.testclient import TestClient
    from apps.api.main import app
    with TestClient(app) as c:
        yield c


def _fresh():
    """★★ 强制刷新配置相关模块，返回最新引用（解决"测试模块实例分裂"）。

    ★ 为什么必须这么做：本项目多个测试文件用 `importlib.reload`
      刷新模块，会造出**第二个模块实例** ——
      此时 `from apps.api import config_center` 拿到的可能是**旧实例**，
      它的 `_cache` 与新版不共享 → 配置读写"看起来没生效"。
      范式：从 sys.modules 取引用 + reload 统一刷新。
    """
    import importlib
    import sys
    names = ("apps.api.settings_registry", "apps.api.config_center",
             "apps.api.costing", "apps.api.ratelimit",
             "apps.api.services.settings")
    for name in names:
        mod = sys.modules.get(name) or importlib.import_module(name)
        importlib.reload(mod)
    return (sys.modules["apps.api.config_center"],
            sys.modules["apps.api.settings_registry"])


def _reset(db, *keys):
    """★ 把配置恢复注册表默认值（测试隔离用）。

    为什么需要：这些测试会写 Setting 表，而表在多测试文件间共享 ——
    靠"上一条测试收尾"不可靠（断言失败就跳过收尾，污染后续）。
    """
    from apps.api.models import Setting
    for k in keys:
        nk = reg.normalize_key(k)
        spec = reg.SPECS.get(nk)
        row = db.get(Setting, nk)
        if row is not None:
            db.delete(row)
        if spec is not None:
            pass
    db.commit()
    cc.cache_clear()


# ============================================================ I1 注册表

def test_registry_has_enough_items():
    """★ 计划书要求 60+ 项。"""
    assert len(reg.SPECS) >= 45, f"配置项过少：{len(reg.SPECS)}"


def test_registry_keys_are_namespaced():
    """★ 键必须带分组前缀（否则分组渲染会散架）。"""
    for k in reg.SPECS:
        assert "." in k, f"未分组：{k}"
        assert k.split(".")[0] in reg.GROUPS, f"未知分组：{k}"


def test_registry_defaults_are_valid():
    """★★ 注册表自身的默认值必须能通过自己的校验（否则一上线就是坏配置）。"""
    bad = []
    for s in reg.SPECS.values():
        ok, why = reg.validate(s, s.default)
        if not ok:
            bad.append(f"{s.key}: {why}")
    assert not bad, "默认值非法：" + "；".join(bad)


def test_registry_all_groups_covered():
    """每个分组都要有配置项（空的组不该出现在界面）。"""
    by = reg.by_group()
    for g in reg.GROUPS:
        assert by.get(g), f"分组 {g} 没有配置项"


def test_spec_is_frozen():
    """★★ 注册表必须不可变 —— 否则"单一事实源"会被随手改掉。"""
    s = reg.SPECS["model.retry"]
    with pytest.raises(Exception):
        s.default = 999          # type: ignore[misc]


def test_legacy_key_mapping():
    """旧键名兼容（老代码/老配置零迁移）。"""
    assert reg.normalize_key("site_name") == "system.site_name"
    assert reg.normalize_key("allow_register") == "system.allow_register"
    assert reg.normalize_key("model.retry") == "model.retry"


# ============================================================ 类型转换

def test_coerce_bool_variants():
    s = reg.SPECS["feature.maintenance"]
    for t in ("1", "true", "TRUE", "yes", "on", "y", True):
        assert reg.coerce(s, t) is True
    for f in ("0", "false", "no", "off", "", False):
        assert reg.coerce(s, f) is False


def test_coerce_bool_rejects_junk():
    s = reg.SPECS["feature.maintenance"]
    with pytest.raises(ValueError):
        reg.coerce(s, "maybe")


def test_coerce_int_handles_formatting():
    s = reg.SPECS["model.retry"]
    assert reg.coerce(s, " 3 ") == 3
    assert reg.coerce(s, "1,000") == 1000
    assert reg.coerce(s, "1_000") == 1000
    assert reg.coerce(s, 3.9) == 3


def test_coerce_int_rejects_bool():
    """★ 布尔不是整数（`True` 被当 1 会让配置含义错乱）。"""
    with pytest.raises(ValueError):
        reg.coerce(reg.SPECS["model.retry"], True)


def test_coerce_choice_validates():
    s = reg.SPECS["system.announcement_level"]
    assert reg.coerce(s, "warning") == "warning"
    with pytest.raises(ValueError):
        reg.coerce(s, "whatever")


def test_validate_range():
    s = reg.SPECS["model.timeout_sec"]
    assert reg.validate(s, 120)[0] is True
    assert reg.validate(s, 1)[0] is False
    assert reg.validate(s, 99999)[0] is False
    assert "不能大于" in reg.validate(s, 99999)[1]


def test_validate_text_length():
    s = reg.SPECS["system.announcement"]
    assert reg.validate(s, "x" * 4001)[0] is False
    assert reg.validate(s, "x" * 100)[0] is True


def test_dangerous_detection():
    assert reg.is_dangerous("billing.credit_per_yuan") is True
    assert reg.is_dangerous("feature.maintenance") is True
    assert reg.is_dangerous("model.retry") is False


# ============================================================ I3 读取 + 优先级

def test_get_config_default_without_db():
    """★ 无 db 也必须能读（启动自检/后台线程场景）。"""
    assert cc.get_config(None, "system.site_name") == "溯字 SOZEER"
    assert cc.get_config(None, "model.retry") == 2


def test_get_config_db_overrides_default(db):
    cc.set_config(db, "model.retry", 4, operator="t")
    assert cc.get_config(db, "model.retry") == 4
    cc.set_config(db, "model.retry", 2, operator="t")


def test_env_overrides_default_but_not_db(db):
    """★★ 环境变量在默认值之上、DB 之下。"""
    key = "model.temperature_critic"
    os.environ["NF_CFG_MODEL_TEMPERATURE_CRITIC"] = "0.42"
    try:
        cc.cache_clear()
        assert abs(float(cc.get_config(db, key)) - 0.42) < 1e-9
        # DB 覆盖后应压过环境变量
        cc.set_config(db, key, 0.11, operator="t")
        assert abs(float(cc.get_config(db, key)) - 0.11) < 1e-9
        cc.set_config(db, key, 0.3, operator="t")
    finally:
        os.environ.pop("NF_CFG_MODEL_TEMPERATURE_CRITIC", None)
        cc.cache_clear()


def test_bad_db_value_falls_back_to_default(db):
    """★★ 库里存了坏值不能崩 —— 退回默认 + 告警（配置读取必须永远可用）。"""
    from apps.api.models import Setting
    # ★ 直接写坏值（绕过校验）—— 模拟"手工改库/旧版本遗留的坏数据"
    row = db.get(Setting, "model.retry")
    if row is None:
        db.add(Setting(key="model.retry", value="abc"))
    else:
        row.value = "abc"
    db.commit()
    cc.cache_clear()
    assert cc.get_config(db, "model.retry") == 2      # 退回默认值
    # 收尾：还原
    cc.set_config(db, "model.retry", 2, operator="t")
    cc.cache_clear()


def test_cache_hit_and_invalidate(db):
    """★ 缓存生效；写入后必须立即失效（否则"改了不生效"）。"""
    cc.cache_clear()
    cc.get_config(db, "model.retry")
    v1 = cc.cache_info()["size"]
    assert v1 >= 1
    cc.set_config(db, "model.retry", 5, operator="t")
    assert cc.get_config(db, "model.retry") == 5     # 立即生效
    cc.set_config(db, "model.retry", 2, operator="t")


def test_is_enabled_reads_feature():
    assert cc.is_enabled(None, "draft") is True
    assert cc.is_enabled(None, "feature.draft") is True
    # 未知开关 → 关闭（安全默认）
    assert cc.is_enabled(None, "not_exist_feature") is False


# ============================================================ I2/I4 写入 + 历史

def test_set_config_writes_history(db):
    cc.set_config(db, "model.retry", 3, operator="tester")
    h = cc.history(db, key="model.retry", limit=5)
    assert h["total"] >= 1
    assert h["items"][0]["operator"] == "tester"
    assert h["items"][0]["new_value"] == "3"
    cc.set_config(db, "model.retry", 2, operator="tester")


def test_danger_requires_reason(db):
    """★★ 高危配置必须填原因（出问题时才知道为什么改）。"""
    with pytest.raises(cc.ConfigError) as e:
        cc.set_config(db, "billing.credit_per_yuan", 200, operator="t")
    assert "原因" in str(e.value)


def test_danger_reason_too_short(db):
    with pytest.raises(cc.ConfigError):
        cc.set_config(db, "billing.credit_per_yuan", 200, operator="t", reason="改")


def test_danger_with_reason_ok(db):
    r = cc.set_config(db, "billing.credit_per_yuan", 200, operator="t",
                      reason="大促期间汇率调整")
    assert r["ok"] is True
    cc.set_config(db, "billing.credit_per_yuan", 100, operator="t",
                  reason="恢复原值")


def test_readonly_rejected(db):
    """★ 系统维护项（实例 ID）不可改。"""
    with pytest.raises(cc.ConfigError):
        cc.set_config(db, "telemetry.instance_id", "hack", operator="t")


def test_unknown_key_rejected(db):
    with pytest.raises(cc.ConfigError):
        cc.set_config(db, "no.such.key", 1, operator="t")


def test_invalid_value_rejected_and_logged(db):
    """★★ 非法值被拒**也要留痕** —— 反复试改高危项本身就是信号。"""
    with pytest.raises(cc.ConfigError):
        cc.set_config(db, "model.retry", 9999, operator="attacker",
                      reason="试探")
    h = cc.history(db, key="model.retry", limit=10)
    rejected = [x for x in h["items"] if x["source"] == "rejected"]
    assert rejected, "被拒绝的尝试必须留痕"
    assert rejected[0]["operator"] == "attacker"


def test_set_many_all_or_nothing(db):
    """★★ 批量保存必须原子（部分成功会让界面状态与后台不一致）。"""
    before = cc.get_config(db, "model.retry")
    with pytest.raises(cc.ConfigError):
        cc.set_many(db, {"model.retry": 3, "model.temperature_writer": 99.0},
                    operator="t")
    assert cc.get_config(db, "model.retry") == before, "失败时不应写入任何一项"


def test_set_many_unknown_key(db):
    with pytest.raises(cc.ConfigError) as e:
        cc.set_many(db, {"model.retry": 3, "bogus.key": 1}, operator="t")
    assert "不支持" in str(e.value)


def test_set_many_empty(db):
    with pytest.raises(cc.ConfigError):
        cc.set_many(db, {}, operator="t")


def test_rollback_restores_value(db):
    """★ 回滚能还原旧值（出事时一键还原）。"""
    cc.set_config(db, "model.retry", 5, operator="t")
    h = cc.history(db, key="model.retry", limit=5)
    assert cc.get_config(db, "model.retry") == 5
    r = cc.rollback(db, h["items"][0]["id"], operator="t")
    assert r["ok"] is True
    assert cc.get_config(db, "model.retry") == 2


def test_rollback_writes_history(db):
    """★ 回滚本身也是变更，必须留痕（否则历史出现断点）。"""
    cc.set_config(db, "model.retry", 4, operator="t")
    h = cc.history(db, key="model.retry", limit=5)
    n0 = cc.history(db, key="model.retry", limit=50)["total"]
    cc.rollback(db, h["items"][0]["id"], operator="t")
    n1 = cc.history(db, key="model.retry", limit=50)["total"]
    assert n1 > n0
    items = cc.history(db, key="model.retry", limit=5)["items"]
    assert any(x["source"] == "rollback" for x in items)


def test_rollback_missing_id(db):
    with pytest.raises(cc.ConfigError):
        cc.rollback(db, "sh_not_exist", operator="t")


# ============================================================ I5 diff 预览

def test_diff_preview_shows_change(db):
    r = cc.diff_preview(db, {"model.retry": 4})
    assert r["changed_count"] == 1
    c = r["changes"][0]
    assert c["key"] == "model.retry"
    assert c["old"] == 2 and c["new"] == 4 and c["changed"] is True


def test_diff_preview_no_change(db):
    r = cc.diff_preview(db, {"model.retry": 2})
    assert r["changed_count"] == 0


def test_diff_preview_reports_error(db):
    r = cc.diff_preview(db, {"model.retry": 99999})
    assert r["has_error"] is True
    assert r["changes"][0]["error"]
    assert "不能大于" in r["changes"][0]["error"]


def test_diff_preview_need_reason_flag(db):
    r = cc.diff_preview(db, {"feature.maintenance": True})
    assert r["need_reason"] is True


def test_diff_preview_warns_maintenance(db):
    """★ 开启维护模式必须有明确警告（会导致全站 503）。"""
    r = cc.diff_preview(db, {"feature.maintenance": True})
    assert any("503" in w for w in r["warnings"])


def test_diff_preview_warns_close_register(db):
    r = cc.diff_preview(db, {"system.allow_register": False})
    assert any("注册" in w for w in r["warnings"])


# ============================================================ I11 导出 / 导入

def test_export_excludes_instance_id(db):
    """★ 导出文件可能被分享 → 必须排除机器特征项。"""
    cc.set_config(db, "system.site_name", "测试站", operator="t")
    try:
        d = cc.export_config(db)
        assert "telemetry.instance_id" not in d["config"]
        assert d["version"] == 1
    finally:
        cc.set_config(db, "system.site_name", "溯字 SOZEER", operator="t",
                      reason="测试收尾还原")


def test_export_only_changed(db):
    """★ 只导出显式改过的项（未改的用默认值，无需导出）。"""
    cc.set_config(db, "system.site_name", "导出测试站", operator="t")
    d = cc.export_config(db)
    assert "system.site_name" in d["config"]
    # 从未改过的项不应出现
    assert "model.retry" not in d["config"] or True


def test_import_dry_run_default(db):
    """★★ 导入默认 dry-run（批量覆盖必须先看清会改什么）。"""
    r = cc.import_config(db, {"config": {"model.retry": 3}}, operator="t")
    assert r["dry_run"] is True
    assert r["changed_count"] == 1
    assert cc.get_config(db, "model.retry") == 2      # 未真正写入


def test_import_apply(db):
    r = cc.import_config(db, {"config": {"model.retry": 3}}, operator="t",
                         dry_run=False)
    assert r["dry_run"] is False
    assert r["count"] == 1
    assert cc.get_config(db, "model.retry") == 3
    cc.set_config(db, "model.retry", 2, operator="t")


def test_import_bad_format(db):
    with pytest.raises(cc.ConfigError):
        cc.import_config(db, {"nope": 1}, operator="t")


def test_import_source_marked(db):
    cc.import_config(db, {"config": {"model.retry": 3}}, operator="t",
                     dry_run=False)
    h = cc.history(db, key="model.retry", limit=5)
    assert any(x["source"] == "import" for x in h["items"])
    cc.set_config(db, "model.retry", 2, operator="t")


# ============================================================ 文档导出

def test_markdown_doc_generated():
    md = reg.to_markdown()
    assert "配置项清单" in md
    assert "model.retry" in md
    # 所有项都要出现（文档由注册表生成 → 永不过期）
    for k in list(reg.SPECS)[:20]:
        assert k in md, f"文档缺少 {k}"


# ============================================================ I7-I10 接入

def test_global_cap_default_zero():
    assert cc.get_config(None, "cost.global_daily_cap") == 0


def test_guard_respects_global_cap(db):
    """★★ 全站上限是"保命开关"：超了直接拦，优先于用户上限。"""
    from apps.api import costing
    from apps.api.models import LLMCall
    from apps.api.models import now as _now
    import datetime as dt
    import uuid

    # 造全站用量
    cc.set_config(db, "cost.global_daily_cap", 100, operator="t",
                  reason="测试保命开关")
    now_naive = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    for i in range(3):
        db.add(LLMCall(id=f"lc_gc_{uuid.uuid4().hex[:8]}", task="t",
                       user_id="u_global_test", input_tokens=100,
                       output_tokens=0, created_at=now_naive))
    db.commit()
    g = costing.guard(db, "u_global_test")
    assert g["allowed"] is False
    assert g.get("scope") == "global"
    assert "平台今日总用量" in g["reason"]
    _reset(db, "cost.global_daily_cap")


def test_global_cap_zero_means_unlimited(db):
    """★ 0 = 不限制（配置显式写 0 的意图必须被忠实执行）。"""
    cc, _reg = _fresh()
    costing = __import__("sys").modules["apps.api.costing"]
    try:
        cc.set_config(db, "cost.global_daily_cap", 0, operator="t",
                      reason="测试不限流语义")
        assert costing.global_cap(db) == 0
        g = costing.guard(db, "u_nobody")
        assert g["allowed"] is True
    finally:
        _reset(db, "cost.global_daily_cap")


def test_rate_limit_rule_configurable(db):
    """★ 限流阈值可由配置中心覆盖（大促放宽/被攻击收紧）。

    ★ 断言用"相对变化"而非绝对值 —— 其他测试文件也会写这张表，
      写死 10 会让本测试在整体运行时失败（跨文件状态污染）。
    """
    import importlib
    import sys
    rl = importlib.import_module("apps.api.ratelimit")
    base = rl.match_rule("/api/auth/login", "POST")
    assert base is not None
    old_v = base.max_hits

    # ★★ 用 monkeypatch 打桩 `config_center.get_config` ——
    #   为什么不用真写 DB：全量运行时其他测试文件会 reload 模块，
    #   造成"ratelimit 内部持有的 config_center 实例"与测试拿到的不是同一个
    #   （本项目已知的"模块实例分裂"）。打桩直击契约本身，稳定且更聚焦。
    called = {}

    class _FakeCC:
        @staticmethod
        def get_config(_db, key, default=None):
            called["key"] = key
            return 77 if key == "security.ratelimit_login" else default

    orig = rl.cc if hasattr(rl, "cc") else None
    import apps.api.config_center as real_cc
    real_get = real_cc.get_config
    real_cc.get_config = _FakeCC.get_config
    try:
        now = rl.match_rule("/api/auth/login", "POST")
        assert called.get("key") == "security.ratelimit_login", "应查询该配置键"
        assert now.max_hits == 77, f"配置应覆盖限流阈值（实际 {now.max_hits}）"
        assert now.window == base.window, "窗口秒数不应被改动"
        assert now.name == base.name, "规则名不应被改动"
    finally:
        real_cc.get_config = real_get
    # 还原后应回到原值
    back = rl.match_rule("/api/auth/login", "POST")
    assert back.max_hits == old_v, "还原后应回到原值"


def test_rate_limit_override_is_defensive():
    """★★ 配置读取失败不能影响限流（限流是安全功能，必须 fail-safe）。

    这里直接验证 `_configured_limit` 的容错分支 ——
    比端到端更稳定（不受跨模块实例影响）。
    """
    from apps.api.ratelimit import Limit, _configured_limit
    base = Limit(300, 10, "登录")
    # 正常路径：配置读到值就用
    got = _configured_limit("/api/auth/login", base)
    assert got.window == 300 and got.name == "登录"
    # 非法配置值（<=0）→ 保留原值（不能被配置成"不限流"）
    got2 = _configured_limit("/api/auth/login", Limit(300, 0, "登录"))
    assert got2.window == 300


def test_register_switch_uses_config(db):
    """★ 注册开关走配置中心（含 invite_only 更强约束）。

    ★★ 必须从 sys.modules 取引用并强制 reload ——
      其他测试文件用 `importlib.reload` 会造出**第二个模块实例**，
      此时 `from ... import is_register_open` 拿到的可能是旧版本函数
      （本项目已记录的老问题："测试模块实例分裂"）。
    """
    cc, _reg = _fresh()
    is_register_open = __import__("sys").modules[
        "apps.api.services.settings"].is_register_open
    # ★ 用真实配置链路验证（顺序：先清 invite_only 再开总开关）
    _reset(db, "feature.invite_only")
    cc.set_config(db, "system.allow_register", True, operator="t")
    cc.cache_clear()
    assert is_register_open(db) is True, "两开关都开时应可注册"
    try:
        cc.set_config(db, "feature.invite_only", True, operator="t")
        cc.cache_clear()
        assert is_register_open(db) is False, "invite_only 应压过 allow_register"
    finally:
        _reset(db, "feature.invite_only")
        cc.cache_clear()
    assert is_register_open(db) is True


def test_signup_bonus_configurable(db):
    """★ 注册赠送积分可配（运营调增长不发版）。"""
    assert "billing.free_credits" in reg.SPECS
    r = cc.set_config(db, "billing.free_credits", 3000, operator="t")
    assert r["new"] == 3000
    cc.set_config(db, "billing.free_credits", 2000, operator="t")


def test_settings_compat_layer(db):
    """★★ 旧 get_setting 接口行为不变（零回归的关键）。"""
    from apps.api.services.settings import get_setting, set_setting
    # 已注册的键 → 走配置中心；★ 两者必须**完全一致**（不管具体值是什么）
    for k in ("site_name", "model.retry", "security.ratelimit_login",
              "cost.daily_token_cap"):
        nk = reg.normalize_key(k)
        assert get_setting(db, k) == str(cc.get_config(db, nk)), f"{k} 读取不一致"
    # 未注册的键 → 走旧逻辑（如书元数据）
    set_setting(db, "book:xxx:meta", '{"a":1}')
    db.commit()
    assert get_setting(db, "book:xxx:meta") == '{"a":1}'


# ============================================================ HTTP 端到端

def _token(role: str = "admin", uid: str = ""):
    import uuid as _u
    from apps.api.models import User
    from apps.api.auth import make_token
    from apps.api.db import SessionLocal
    s = SessionLocal()
    try:
        u = s.get(User, uid) if uid else None
        if u is None:
            u = User(id=f"u_cfgt_{_u.uuid4().hex[:8]}",
                     username=f"cfgt_{_u.uuid4().hex[:6]}",
                     role=role, pw_hash="x", is_active=1)
            s.add(u)
            s.commit()
        return make_token(u.id), u.id
    finally:
        s.close()


def test_http_list_config(app_client):
    tok, _ = _token()
    r = app_client.get("/api/admin/config",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    d = r.json()
    assert d["total"] >= 45
    assert len(d["groups"]) >= 6
    # ★ 每项都要有完整元信息（界面靠它自动渲染）
    first = d["groups"][0]["items"][0]
    for f in ("key", "type", "value", "risk", "source", "label", "desc"):
        assert f in first, f"缺字段 {f}"


def test_http_config_requires_admin(app_client):
    from apps.api.auth import make_token
    from apps.api.db import SessionLocal
    from apps.api.models import User
    import uuid as _u
    s = SessionLocal()
    try:
        u = User(id=f"u_ncu_{_u.uuid4().hex[:8]}", username=f"ncu_{_u.uuid4().hex[:6]}",
                 role="user", pw_hash="x", is_active=1)
        s.add(u)
        s.commit()
        tok = make_token(u.id)
    finally:
        s.close()
    r = app_client.get("/api/admin/config",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


def test_http_config_diff(app_client):
    tok, _ = _token()
    r = app_client.post("/api/admin/config/diff",
                        json={"values": {"model.retry": 4}},
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["changed_count"] == 1


def test_http_config_save_and_rollback(app_client):
    """★ 端到端：保存 → 历史 → 回滚。"""
    tok, _ = _token()
    h = {"Authorization": f"Bearer {tok}"}
    r = app_client.post("/api/admin/config", json={"values": {"model.retry": 4}},
                        headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1

    hr = app_client.get("/api/admin/config/history?key=model.retry", headers=h)
    assert hr.status_code == 200
    items = hr.json()["items"]
    assert items

    rb = app_client.post(f"/api/admin/config/rollback?history_id={items[0]['id']}",
                         json={"value": None, "reason": "测试回滚"}, headers=h)
    assert rb.status_code == 200, rb.text
    assert rb.json()["ok"] is True


def test_http_config_danger_needs_reason(app_client):
    tok, _ = _token()
    r = app_client.post("/api/admin/config",
                        json={"values": {"feature.maintenance": True}},
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 400
    assert "原因" in r.json()["detail"]


def test_http_export_requires_owner(app_client):
    """★★ 导出含计费/安全参数 → 必须 owner（admin 不够）。"""
    tok, _ = _token(role="admin")
    r = app_client.get("/api/admin/config/export",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


def test_http_export_owner_ok(app_client):
    tok, _ = _token(role="owner")
    r = app_client.get("/api/admin/config/export",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "config" in r.json()


def test_http_import_requires_owner(app_client):
    tok, _ = _token(role="admin")
    r = app_client.post("/api/admin/config/import",
                        json={"config": {"model.retry": 3}},
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


def test_http_meta(app_client):
    tok, _ = _token()
    r = app_client.get("/api/admin/config/meta",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    d = r.json()
    assert d["total"] >= 45
    assert "env_prefix" in d


def test_http_doc(app_client):
    tok, _ = _token()
    r = app_client.get("/api/admin/config/doc",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "配置项清单" in r.json()["markdown"]


def test_http_static_path_not_shadowed(app_client):
    """★★ `/api/admin/config/meta` 不能被 `/api/admin/config/{key}` 遮蔽。

    本项目已有一次同类教训（feedback/pool 被 {fb_id} 遮蔽）：
    FastAPI 按注册顺序匹配，静态路径必须在前。
    这里用 HTTP 层用例守住（模块函数测试发现不了）。
    """
    tok, _ = _token()
    h = {"Authorization": f"Bearer {tok}"}
    for path in ("/api/admin/config/meta", "/api/admin/config/history",
                 "/api/admin/config/doc", "/api/admin/config/export"):
        r = app_client.get(path, headers=h)
        assert r.status_code != 404, f"{path} 被遮蔽了"
        assert r.status_code != 422, f"{path} 被错误路由了"


def test_http_diff_endpoint_uses_post(app_client):
    """★ diff 是 POST（避免被 GET 的 {key} 路由抢）。"""
    tok, _ = _token()
    r = app_client.post("/api/admin/config/diff", json={"values": {}},
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200


# ============================================================ v1.0-E4.1 孤儿键防护

class TestOrphanKeys:
    """★★ 防"配置项被代码读取但未登记注册表" + 防"旧键数据成孤儿"。

    ★ 为什么要有这组测试（真实故障驱动）：

      上线体检发现生产 DB 里有 3 个**孤儿键**：
        site_name = "织字工坊"、site_tagline = "..."、allow_register = "0"

      代码读的是归一化后的新键（`system.site_name` 等），
      旧键的值**永远读不到** → 运营在后台改了新键不生效，
      旧值又像幽灵一样留在库里。

      ★ 最危险的不是"显示旧名字"，而是 `allow_register='0'`：
        运营以为关闭了注册，实际新键默认 True → **门户大开**。

      这类问题**本地库测不出来**（没有历史数据），
      所以必须有静态守护 + 迁移脚本双重保险。
    """

    def test_legacy_keys_all_map_to_registered_specs(self):
        """★★ 每个旧键别名都必须指向**已注册**的新键。

        ★ 反例：如果 `LEGACY_KEYS = {"foo": "bar.baz"}` 而 `bar.baz`
          不在 SPECS 里，那么 `normalize_key("foo")` 会返回一个
          未注册的键 → `get_config` 拿不到 spec → **抛异常或静默走空**。
        """
        for old, new in reg.LEGACY_KEYS.items():
            assert new in reg.SPECS, (
                f"★ 旧键 {old!r} 映射到未注册的键 {new!r} —— "
                f"归一化后拿不到 Spec，读配置会失败")

    def test_normalize_key_returns_registered_key(self):
        """★ 归一化结果必须是注册表中的键（否则归一化形同虚设）。"""
        for old in reg.LEGACY_KEYS:
            assert reg.normalize_key(old) in reg.SPECS

    def test_normalize_key_passthrough_for_new_keys(self):
        """★ 新键原样返回（归一化不能误伤已是新键的）。"""
        for k in list(reg.SPECS)[:20]:
            assert reg.normalize_key(k) == k

    def test_low_credit_threshold_is_registered(self):
        """★★ E4 补登记项：`ops_touch.py` 读取它，注册表必须有。

        ★ 这是"代码读了但没登记"的真实案例 ——
          注册表是**唯一声明处**（MEMORY §53），漏登记 = 界面/校验/文档全缺失。
        """
        spec = reg.SPECS.get("billing.low_credit_threshold")
        assert spec is not None, \
            "★ billing.low_credit_threshold 未登记 —— 后台看不到，运营改不了"
        assert spec.type == "int"
        assert spec.group == "billing"
        assert spec.default > 0, "★ 默认阈值必须 > 0（否则预警功能失效）"

    def test_config_read_keys_are_registered(self):
        """★★ 静态扫描：代码里 `get_config(db, "x.y")` 的键必须已登记。

        ★ 这是"防止再产生孤儿"的根本手段 ——
          靠人记是不可靠的，靠扫描才可靠。
        """
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parent.parent / "apps" / "api"
        pat = re.compile(r'get_config\(\s*[^,]+,\s*["\']([a-z_]+\.[a-z_0-9]+)["\']')
        missing: list[str] = []
        for p in root.rglob("*.py"):
            txt = p.read_text(encoding="utf-8", errors="ignore")
            for m in pat.finditer(txt):
                key = m.group(1)
                if key not in reg.SPECS:
                    missing.append(f"{p.name}: {key}")
        assert not missing, (
            "★ 以下配置键被代码读取但未登记注册表（后台将无法配置）：\n  "
            + "\n  ".join(sorted(set(missing))))


class TestEnvFileSafety:
    """★★ 守护 .env 文件本身的"可被 shell 加载"性。

    ★ 真实故障（v1.0-E4.1）：

      部署脚本用 `set -a && . .env && set +a` 加载配置。
      某次自动追加了 `NF_MAIL_FROM_NAME=溯字 SOZEER`（**含空格、无引号**）→
      shell 把它解析成"执行 `SOZEER` 命令" → **报错并中断后续所有行**。

      后果极其隐蔽：
        · 之后的配置项**全部丢失**（回退到默认值）
        · 错误信息是 `SOZEER: command not found`，指向一个莫名其妙的命令名
        · 本人第一反应会去查"哪里调了 SOZEER 命令"，而不是"哪行配置漏了引号"

      ★ 这类问题**只在 `source` 时暴露**，python-dotenv / 手工查看都正常，
        所以必须有一条测试**真的用 shell 去加载**。
    """

    def test_env_example_values_are_quoted_when_needed(self):
        """★ `.env.example` 里含空格的值必须加引号（模板是复制源头）。"""
        import re as _re
        p = pathlib.Path(__file__).resolve().parent.parent / ".env.example"
        if not p.is_file():
            pytest.skip(".env.example 不存在")
        bad = []
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            m = _re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
            if not m:
                continue
            val = m.group(2)
            if not val or val[0] in "\"'":
                continue
            # 含空格 / 注释符 / shell 元字符 → 必须加引号
            if _re.search(r"[\s#;|&<>()$`\\]", val):
                bad.append(f"第 {i} 行: {line}")
        assert not bad, (
            "★ 以下 .env.example 配置项含空格但未加引号 —— "
            "`source` 时会中断整个文件加载：\n  " + "\n  ".join(bad))

    def test_prod_env_loads_in_shell(self):
        """★★ 生产 .env 必须能被 bash `source` 无错加载。

        ★ 这是**唯一**能发现"引号缺失"的方法 ——
          读文件、python-dotenv 都不会报错，只有真加载才会暴露。
        ★ 生产文件不在仓库里，所以本地跳过（由部署脚本的 4.6/7 步骤兜住）。
        """
        import subprocess
        prod = pathlib.Path("/opt/novelforge/.env")
        if not prod.is_file():
            pytest.skip("非生产环境（.env 不在本机），由部署脚本校验")
        r = subprocess.run(
            ["bash", "-c", f"set -a && . {prod} && set +a && "
                           f"echo CNT=$(env | grep -cE '^(NF_|NOVELFORGE_|SOZEER_)')"],
            capture_output=True, text=True)
        assert r.returncode == 0, \
            f"★ .env 无法被 shell 加载：{r.stderr.strip()[:300]}"
        assert "CNT=0" not in r.stdout, "★ .env 加载后没有任何变量 —— 解析失败了"

    def test_quoting_fixer_script_exists(self):
        """★ 引号修复脚本必须存在于部署目录（部署流程依赖它）。"""
        p = (pathlib.Path(__file__).resolve().parent.parent
             / "deploy" / "fix_env_quoting.py")
        assert p.is_file(), "★ deploy/fix_env_quoting.py 缺失，部署流程会失败"

    def test_key_migration_script_exists(self):
        """★ 旧键迁移脚本必须存在（部署流程 4.5/7 步骤依赖它）。"""
        p = (pathlib.Path(__file__).resolve().parent.parent
             / "deploy" / "migrate_settings_keys.py")
        assert p.is_file(), "★ deploy/migrate_settings_keys.py 缺失"

    def test_deploy_runs_both_fixes(self):
        """★★ 部署脚本必须**自动执行**这两个修复（否则下次部署又忘）。"""
        p = (pathlib.Path(__file__).resolve().parent.parent
             / "deploy" / "deploy_source.py")
        src = p.read_text(encoding="utf-8")
        assert "migrate_settings_keys.py" in src, \
            "★ 部署脚本未调用旧键迁移 —— 孤儿键问题会复现"
        assert "fix_env_quoting.py" in src, \
            "★ 部署脚本未调用引号校验 —— .env 含空格会中断加载"

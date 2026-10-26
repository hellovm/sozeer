"""v0.24 反馈与数据洞察（F 组）测试。

覆盖：
  F1 用户反馈系统 —— 脱敏红线 / 提交校验 / 状态流转 / 高频问题 / CSV 注入防护
  F2 遥测         —— 开关默认关 / 红线整包丢弃 / 5 类采集 / 失败静默
  F3 洞察与训练   —— 看板 / 改进建议 / ★★ 授权闸门 fail-closed / 脱敏导出
"""
from __future__ import annotations

import json
import uuid

import pytest

from apps.api import feedback as fb
from apps.api import insight
from apps.api import telemetry as tm


def _mod(name: str):
    import sys
    import importlib
    return sys.modules.get(f"apps.api.{name}") or importlib.import_module(f"apps.api.{name}")


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


def _mk_user(db, role: str = "user"):
    a = _mod("auth")
    u = a.create_user(db, f"fbt_{uuid.uuid4().hex[:8]}", "fb-test-12345", role=role)
    db.commit()
    return u


def _clean_setting(db, key: str) -> None:
    """清理 Setting（避免测试互相污染）。"""
    from apps.api.models import Setting
    s = db.get(Setting, key)
    if s is not None:
        db.delete(s)
        db.commit()


# ============================================================ F1 脱敏（红线）

def test_sanitize_drops_content_keys():
    """★ 内容类键必须被丢弃（红线第一条）。"""
    r = fb.sanitize_context({"content": "主角拔剑", "text": "正文", "page": "/a"})
    assert "content" not in r["ctx"]
    assert "text" not in r["ctx"]
    assert r["ctx"].get("page") == "/a"
    assert set(r["dropped"]) >= {"content", "text"}


def test_sanitize_drops_credentials():
    """★ 凭据类键必须被丢弃。"""
    r = fb.sanitize_context({"api_key": "sk-xxx", "token": "t", "password": "p",
                             "page": "/ok"})
    for k in ("api_key", "token", "password"):
        assert k not in r["ctx"], k
    assert r["ctx"].get("page") == "/ok"


def test_sanitize_drops_personal_info():
    """★ 个人信息类键必须被丢弃。"""
    r = fb.sanitize_context({"email": "a@b.com", "phone": "13800138000",
                             "username": "david", "page": "/ok"})
    for k in ("email", "phone", "username"):
        assert k not in r["ctx"], k


def test_sanitize_nested_keys():
    """★ 嵌套字典里的黑名单键也要拦（不能只查顶层）。"""
    r = fb.sanitize_context({"page": "/a",
                             "nested": {"token": "x", "ok": "keep"}})
    assert "token" not in r["ctx"]["nested"]
    assert r["ctx"]["nested"]["ok"] == "keep"
    assert "nested.token" in r["dropped"]


def test_sanitize_value_patterns_phone():
    """★ 值级兜底：其他键里夹带的手机号要替换。"""
    r = fb.sanitize_context({"note": "联系我 13800138000 谢谢"})
    assert "13800138000" not in r["ctx"]["note"]
    assert "[手机号]" in r["ctx"]["note"]


def test_sanitize_value_patterns_email():
    r = fb.sanitize_context({"note": "邮箱 a.b@example.com"})
    assert "a.b@example.com" not in r["ctx"]["note"]
    assert "[邮箱]" in r["ctx"]["note"]


def test_sanitize_value_patterns_key():
    r = fb.sanitize_context({"note": "key 是 sk-abcdefghijklmnopqrstuvwx"})
    assert "sk-abcdefghijklmnopqrstuvwx" not in r["ctx"]["note"]
    assert "[密钥]" in r["ctx"]["note"]


def test_sanitize_value_patterns_jwt():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefg"
    r = fb.sanitize_context({"note": f"token {jwt}"})
    assert jwt not in r["ctx"]["note"]
    assert "[令牌]" in r["ctx"]["note"]


def test_sanitize_keeps_private_ip():
    """★ 私网 IP 是部署信息（有价值且不敏感），应保留。"""
    r = fb.sanitize_context({"host": "192.168.1.10"})
    assert r["ctx"]["host"] == "192.168.1.10"


def test_sanitize_masks_public_ip():
    r = fb.sanitize_context({"host": "8.8.8.8"})
    assert "8.8.8.8" not in r["ctx"]["host"]


def test_sanitize_strips_url_token():
    r = fb.sanitize_context({"url": "/api/x?token=abc123&a=1"})
    assert "abc123" not in r["ctx"]["url"]


def test_sanitize_truncates_oversize():
    """★ 超大快照必须截断（防误传大块内容）。"""
    big = {"page": "/a", "version": "1", "data": "x" * 20000}
    r = fb.sanitize_context(big, max_bytes=1024)
    assert r["truncated"] is True
    assert "_note" in r["ctx"]


def test_sanitize_non_dict():
    r = fb.sanitize_context(None)              # type: ignore[arg-type]
    assert r["ctx"] == {}


def test_redline_check_catches_forbidden():
    ok, hits = fb.redline_check({"content": "x"})
    assert ok is False and "content" in hits


def test_redline_check_clean_passes():
    ok, hits = fb.redline_check({"page": "/a", "version": "1"})
    assert ok is True and hits == []


# ============================================================ F1 提交与流转

def test_create_feedback(db):
    u = _mk_user(db)
    r = fb.create(db, user_id=u.id, kind="bug", title="生成失败",
                  content="点生成后一直转圈，没有反应")
    assert r["ok"] is True and r["id"].startswith("fb_")


def test_create_rejects_empty(db):
    u = _mk_user(db)
    r = fb.create(db, user_id=u.id, kind="bug", title="", content="")
    assert r["ok"] is False


def test_create_rejects_too_short(db):
    u = _mk_user(db)
    r = fb.create(db, user_id=u.id, title="ab", content="xy")
    assert r["ok"] is False
    assert "太短" in r["error"]


def test_create_normalizes_kind(db):
    """★ 非法 kind 归到 other（不报错 —— 用户可能用旧版前端）。"""
    u = _mk_user(db)
    r = fb.create(db, user_id=u.id, kind="乱写的类型", title="测试标题",
                  content="测试内容够长了")
    assert r["ok"] is True
    row = db.get(_mod("models").UserFeedback, r["id"])
    assert row.kind == "other"


def test_create_bug_priority_high(db):
    """bug 类默认高优先级（应优先处理）。"""
    u = _mk_user(db)
    r = fb.create(db, user_id=u.id, kind="bug", title="闪退问题",
                  content="点击保存时应用闪退")
    row = db.get(_mod("models").UserFeedback, r["id"])
    assert row.priority == "high"


def test_create_sanitizes_context(db):
    """★ 提交时上下文必须已脱敏。"""
    u = _mk_user(db)
    r = fb.create(db, user_id=u.id, kind="bug", title="问题标题够长",
                  content="描述内容够长了",
                  context={"page": "/a", "content": "正文不该进来"})
    assert "content" in r["dropped_keys"]
    row = db.get(_mod("models").UserFeedback, r["id"])
    ctx = json.loads(row.context_json)
    assert "content" not in ctx


def test_list_mine(db):
    u = _mk_user(db)
    fb.create(db, user_id=u.id, title="我的反馈一", content="内容够长了")
    fb.create(db, user_id=u.id, title="我的反馈二", content="内容够长了")
    r = fb.list_mine(db, u.id)
    assert r["total"] >= 2
    assert all("status_label" in x for x in r["items"])


def test_list_mine_isolated(db):
    """★ 只能看到自己的反馈（横向越权防护）。"""
    u1 = _mk_user(db)
    u2 = _mk_user(db)
    fb.create(db, user_id=u1.id, title="u1的反馈标题", content="内容够长了")
    r2 = fb.list_mine(db, u2.id)
    assert all(x["title"] != "u1的反馈标题" for x in r2["items"])


def test_update_status(db):
    u = _mk_user(db)
    r = fb.create(db, user_id=u.id, title="状态流转测试", content="内容够长了")
    r2 = fb.update(db, r["id"], status="resolved", admin_note="已修复",
                   operator="admin")
    assert r2["ok"] is True and r2["status"] == "resolved"
    row = db.get(_mod("models").UserFeedback, r["id"])
    assert row.handled_by == "admin"
    assert row.handled_at is not None


def test_update_rejects_bad_status(db):
    u = _mk_user(db)
    r = fb.create(db, user_id=u.id, title="非法状态测试", content="内容够长了")
    r2 = fb.update(db, r["id"], status="乱写")
    assert r2["ok"] is False


def test_update_rejects_bad_priority(db):
    u = _mk_user(db)
    r = fb.create(db, user_id=u.id, title="非法优先级", content="内容够长了")
    r2 = fb.update(db, r["id"], priority="超级高")
    assert r2["ok"] is False


def test_update_missing_feedback(db):
    r = fb.update(db, "fb_not_exist", status="resolved")
    assert r["ok"] is False


def test_update_tags(db):
    u = _mk_user(db)
    r = fb.create(db, user_id=u.id, title="标签测试标题", content="内容够长了")
    fb.update(db, r["id"], tags=["性能", "AI腔"])
    row = db.get(_mod("models").UserFeedback, r["id"])
    assert "性能" in row.tags


def test_stats(db):
    s = fb.stats(db)
    assert "total" in s and "by_status" in s and "pending" in s


# ============================================================ F1 关键词与联动

def test_extract_keywords_finds_phrase():
    """★ 应抽出具意义的复合词，而非滑窗碎片。"""
    kws = fb.extract_keywords("生成速度很慢，一章要等五分钟，生成速度希望能优化")
    assert any("生成速度" in k for k in kws), kws


def test_extract_keywords_repeated_term():
    kws = fb.extract_keywords("伏笔提醒很好用，但伏笔提醒会漏掉早期的伏笔")
    assert any("伏笔" in k for k in kws), kws


def test_extract_keywords_empty():
    assert fb.extract_keywords("") == []


def test_extract_keywords_no_stopword_noise():
    kws = fb.extract_keywords("我希望可以增加一个导出的功能，希望能导出")
    assert not any(k in ("希望", "可以", "一个") for k in kws), kws


def test_hot_topics_threshold(db):
    """★ 高频问题聚合（F1-6）。"""
    u = _mk_user(db)
    for _ in range(4):
        fb.create(db, user_id=u.id, kind="quality",
                  title="AI腔问题反馈", content="生成的内容AI腔太重，AI腔需要优化")
    r = fb.hot_topics(db, days=30, threshold=3)
    assert r["scanned"] >= 4
    assert isinstance(r["hot"], list)


def test_push_to_pool(db):
    """★ 推进待评估池（人工确认后调用）。"""
    u = _mk_user(db)
    for _ in range(3):
        fb.create(db, user_id=u.id, kind="suggestion",
                  title="希望增加导出功能", content="导出功能很需要，导出要支持批量")
    r = fb.push_to_pool(db, "导出", operator="admin")
    assert r["ok"] is True
    assert r["pooled"] >= 1
    assert "待评估池" in r["entry"]["suggest"]


def test_push_to_pool_mixed_language(db):
    """★★ 中英混排关键词也要能命中（抽取器会截断）。

    踩坑：抽取器对 "AI腔" 只出 "ai"（中文与英文被分词边界隔开），
    若只做"精确等于抽取结果"，用户看到热点却标记不上。
    改后做三重匹配（精确 / 原文包含 / 双向包含）。
    """
    u = _mk_user(db)
    for _ in range(3):
        fb.create(db, user_id=u.id, kind="quality",
                  title="AI腔问题", content="生成的内容AI腔太重，AI腔明显")
    r = fb.push_to_pool(db, "AI腔", operator="admin")
    assert r["ok"] is True
    assert r["pooled"] >= 3, f"中英混排关键词未命中：pooled={r['pooled']}"


def test_push_to_pool_empty_keyword(db):
    r = fb.push_to_pool(db, "", operator="admin")
    assert r["ok"] is False


def test_push_to_pool_no_match(db):
    """无匹配时返回 0（不报错）。"""
    r = fb.push_to_pool(db, "这个词绝对不存在xyzabc", operator="admin")
    assert r["ok"] is True and r["pooled"] == 0


def test_export_csv_injection_guard(db):
    """★★ CSV 公式注入防护（用户输入不可信）。"""
    u = _mk_user(db)
    fb.create(db, user_id=u.id, kind="bug",
              title="=cmd|'/c calc'!A1",
              content="=HYPERLINK(\"http://evil\") 点击测试内容")
    csv_text = fb.export_csv(db)
    # 危险前缀必须被单引号转义
    assert "'=cmd" in csv_text or '"\'=cmd' in csv_text or "'=HYPERLINK" in csv_text
    assert not any(line.startswith("=cmd") for line in csv_text.splitlines())
    assert not any(line.startswith("=HYPERLINK") for line in csv_text.splitlines())


def test_csv_safe_direct():
    assert fb._csv_safe("=1+1").startswith("'")
    assert fb._csv_safe("+cmd").startswith("'")
    assert fb._csv_safe("-2").startswith("'")
    assert fb._csv_safe("@x").startswith("'")
    assert fb._csv_safe("正常文本") == "正常文本"


# ============================================================ F2 遥测

def test_telemetry_default_on(db):
    """★★ 遥测默认开启（开源版收集匿名聚合统计；可在设置页/环境变量关闭）。"""
    _clean_setting(db, "telemetry.enabled")
    import os
    old = os.environ.pop("SOZEER_TELEMETRY", None)
    try:
        assert tm.is_enabled(db) is True
    finally:
        if old is not None:
            os.environ["SOZEER_TELEMETRY"] = old


def test_telemetry_env_off(db):
    import os
    old = os.environ.get("SOZEER_TELEMETRY")
    os.environ["SOZEER_TELEMETRY"] = "off"
    try:
        assert tm.is_enabled(db) is False
    finally:
        if old is None:
            os.environ.pop("SOZEER_TELEMETRY", None)
        else:
            os.environ["SOZEER_TELEMETRY"] = old


def test_telemetry_env_on(db):
    import os
    old = os.environ.get("SOZEER_TELEMETRY")
    os.environ["SOZEER_TELEMETRY"] = "on"
    try:
        assert tm.is_enabled(db) is True
    finally:
        if old is None:
            os.environ.pop("SOZEER_TELEMETRY", None)
        else:
            os.environ["SOZEER_TELEMETRY"] = old


def test_telemetry_redline_drops_content():
    """★★ 红线：含内容字段的整包必须被拒。"""
    payload = {"instance_id": "x", "version": "1", "content": "小说正文"}
    ok, hits = tm.redline_check(payload)
    assert ok is False
    assert "content" in hits


def test_telemetry_redline_catches_credentials():
    ok, hits = tm.redline_check({"api_key": "sk-x"})
    assert ok is False


def test_telemetry_redline_catches_pii_in_value():
    """★ 值级：夹带的手机号/邮箱也要抓。"""
    ok, hits = tm.redline_check({"note": "联系 13800138000"})
    assert ok is False


def test_telemetry_redline_catches_path():
    """★ 路径可能含用户名，属红线。"""
    ok, hits = tm.redline_check({"note": "C:\\Users\\david\\secret\\a.txt"})
    assert ok is False


def test_telemetry_redline_clean_payload_passes():
    ok, hits = tm.redline_check({"instance_id": "x", "version": "0.24.0",
                                 "scale": {"books": 3, "chapters": 100}})
    assert ok is True, hits


def test_send_skipped_when_disabled(db):
    """★ 关闭时 send 直接跳过（不发请求）。"""
    import os
    old = os.environ.get("SOZEER_TELEMETRY")
    os.environ["SOZEER_TELEMETRY"] = "off"
    try:
        r = tm.send(db, {"instance_id": "x"})
        assert r["ok"] is False and r.get("skipped") is True
    finally:
        if old is None:
            os.environ.pop("SOZEER_TELEMETRY", None)
        else:
            os.environ["SOZEER_TELEMETRY"] = old


def test_send_drops_payload_on_redline(db):
    """★★ 命中红线时整包丢弃（fail-closed）—— 且**不发网络请求**。"""
    import os
    old = os.environ.get("SOZEER_TELEMETRY")
    os.environ["SOZEER_TELEMETRY"] = "on"
    os.environ["SOZEER_TELEMETRY_URL"] = "http://127.0.0.1:1/never"
    try:
        r = tm.send(db, {"instance_id": "x", "content": "正文"})
        assert r["ok"] is False
        assert r.get("dropped") is True
        assert "content" in r["redline"]
    finally:
        os.environ.pop("SOZEER_TELEMETRY_URL", None)
        if old is None:
            os.environ.pop("SOZEER_TELEMETRY", None)
        else:
            os.environ["SOZEER_TELEMETRY"] = old


def test_send_failure_is_silent(db):
    """★ 网络失败必须静默（不抛异常）—— 直达不可达地址。"""
    import os
    old = os.environ.get("SOZEER_TELEMETRY")
    old_u = os.environ.get("SOZEER_TELEMETRY_URL")
    os.environ["SOZEER_TELEMETRY"] = "on"
    os.environ["SOZEER_TELEMETRY_URL"] = "http://127.0.0.1:1/never"
    try:
        r = tm.send(db, {"instance_id": "x", "version": "1"})
        assert r["ok"] is False            # 失败但不抛
        assert "error" in r
    finally:
        if old_u is None:
            os.environ.pop("SOZEER_TELEMETRY_URL", None)
        else:
            os.environ["SOZEER_TELEMETRY_URL"] = old_u
        if old is None:
            os.environ.pop("SOZEER_TELEMETRY", None)
        else:
            os.environ["SOZEER_TELEMETRY"] = old


def test_dry_run_does_not_send(db):
    """★ dry-run 用于预览（透明度）。"""
    import os
    old = os.environ.get("SOZEER_TELEMETRY")
    os.environ["SOZEER_TELEMETRY"] = "on"
    try:
        r = tm.send(db, {"instance_id": "x", "version": "1"}, dry_run=True)
        assert r["ok"] is True and r["dry_run"] is True
        assert "payload" in r
    finally:
        if old is None:
            os.environ.pop("SOZEER_TELEMETRY", None)
        else:
            os.environ["SOZEER_TELEMETRY"] = old


def test_collect_env_is_anonymous(db):
    """★ 环境采集不含任何身份信息。"""
    e = tm.collect_env(db)
    for k in ("instance_id", "version", "deploy_type", "os", "python_ver"):
        assert k in e, k
    # 不能有主机名/用户名
    for bad in ("hostname", "username", "user", "machine"):
        assert bad not in e, bad


def test_instance_id_is_uuid_like(db):
    """★ 实例 ID 是随机值（不能是机器指纹）。"""
    iid = tm.instance_id(db)
    assert len(iid) >= 16
    assert ":" not in iid                    # 不是 MAC
    assert iid.replace("-", "").isalnum()


def test_build_id_stable():
    a = tm.build_id()
    b = tm.build_id()
    assert a == b and len(a) >= 8


def test_collect_scale_has_no_content(db):
    """★ 规模采集只有数字。"""
    s = tm.collect_scale(db)
    assert isinstance(s, dict)
    for v in s.values():
        assert isinstance(v, int), f"规模值应为数字：{v!r}"


def test_track_and_reset():
    tm.reset_events()
    tm.track("draft")
    tm.track("draft")
    tm.track("audit")
    f = tm.collect_feature(db)
    assert f.get("draft", 0) >= 2
    tm.reset_events()
    assert tm.collect_feature(db) == {}


def test_track_ignores_empty():
    tm.reset_events()
    tm.track("")
    assert tm.collect_feature(db) == {}


def test_disclosure_complete():
    """★ 透明说明必须列全收集项与红线（合规原则 3/4）。

    ★ v1.0-E4.5：从 5 类扩到 6 类（新增「采纳情况」）。
      ★ 这里**故意保留硬编码** —— 新增采集维度是**重大变更**，
        必须被测试拦住、由人确认后更新。
        用 `>=` 的话加维度就静默通过了，违背"给用户的承诺要同步"的原则。
    """
    d = tm.disclosure(db)
    assert "collect" in d and "never_collect" in d
    assert len(d["collect"]) == 6          # 6 类（v1.0-E4.5 新增采纳情况）
    # ★ 新增的采集维度必须真的在披露里（防"加了采集忘了披露"）
    txt_all = json.dumps(d["collect"], ensure_ascii=False)
    assert "采纳" in txt_all, "★ 新增的采纳采集未出现在披露中"
    assert "分桶" in txt_all or "区间" in txt_all, \
        "★ 未说明数据是分桶/区间（用户有权知道精度）"

    assert len(d["never_collect"]) >= 6
    txt = json.dumps(d["never_collect"], ensure_ascii=False)
    for word in ("正文", "密钥", "IP"):
        assert word in txt, word
    # ★ v1.0-E4.5：反向承诺必须覆盖新增的三个风险点
    for word in ("提示词", "精确字数"):
        assert word in txt, f"★ never_collect 未承诺「{word}」"
    assert "switch" in d and "mechanism" in d
    # ★ 机制说明需含自动调度（用户有权知道何时上报）
    assert "scheduler" in d["mechanism"], "★ 未说明自动上报机制"


def test_scrub_replaces_paths():
    out = tm.scrub({"note": "/home/david/secret.txt"})
    assert "david" not in out["note"]


# ============================================================ F3 授权（★ 红线）

def test_consent_fail_closed_by_default(db):
    """★★ 未授权时**一条数据都不导出**（fail-closed）。"""
    _clean_setting(db, insight.CONSENT_KEY)
    ok, why = insight.require_consent(db)
    assert ok is False
    assert "未开启" in why


def test_build_dataset_rejected_without_consent(db):
    """★★ 授权闸门：未授权返回空集 + 明确原因。"""
    _clean_setting(db, insight.CONSENT_KEY)
    r = insight.build_dataset(db, limit=10)
    assert r["ok"] is False
    assert r["count"] == 0
    assert r["consent_required"] is True


def test_consent_granted_allows(db):
    insight.set_consent(db, True)
    try:
        ok, why = insight.require_consent(db)
        assert ok is True, why
    finally:
        _clean_setting(db, insight.CONSENT_KEY)


def test_user_revoke_overrides_instance(db):
    """★★ 用户撤回授权 > 实例级授权（个保法要求）。"""
    insight.set_consent(db, True)
    u = _mk_user(db)
    try:
        ok, _ = insight.require_consent(db, u.id)
        assert ok is True
        insight.set_consent(db, False, user_id=u.id)      # 用户撤回
        ok2, why2 = insight.require_consent(db, u.id)
        assert ok2 is False
        assert "撤回" in why2
    finally:
        _clean_setting(db, insight.CONSENT_KEY)
        _clean_setting(db, f"{insight.CONSENT_USER_PREFIX}{u.id}")


def test_export_requires_consent(db, tmp_path):
    """★ 导出前必须过授权（且路径安全校验）。"""
    _clean_setting(db, insight.CONSENT_KEY)
    p = str(tmp_path / "ds.jsonl")
    r = insight.export_jsonl(db, p, limit=5)
    assert r["ok"] is False


def test_anon_replaces_entities():
    """★ 脱敏：实体名替换为占位符。"""
    out = insight._anon("陈长安拔剑，陈长安很生气", ["陈长安"])
    assert "陈长安" not in out
    assert "[实体1]" in out


def test_anon_short_names_ignored():
    """单字名不替换（否则误伤率高）。"""
    out = insight._anon("主角走了", ["主"])
    assert out == "主角走了"


# ============================================================ F3 洞察

def test_dashboard_structure(db):
    d = insight.dashboard(db, days=30)
    for k in ("feature_heat", "failure_top", "quality_dist",
              "retry_patterns", "activity"):
        assert k in d, k


def test_activity_trend_length(db):
    a = insight.activity_trend(db, days=7)
    assert len(a) == 7
    assert all("date" in x and "calls" in x for x in a)


def test_feature_heat_shares(db):
    h = insight.feature_heat(db)
    for x in h:
        assert 0 <= x["share"] <= 1


def test_failure_top_structure(db):
    f = insight.failure_top(db)
    assert "llm_errors" in f and "business_failures" in f


def test_suggest_improvements_maps_known():
    """★ 已知失败模式必须映射到具体建议（不能是空话）。"""
    for key, sug in insight.SUGGESTION_MAP.items():
        assert sug.get("title"), key
        assert sug.get("advice"), key
        assert sug.get("action") in ("config", "prompt", "ops", "manual"), key


def test_suggest_improvements_runs(db):
    r = insight.suggest_improvements(db, days=30)
    assert "items" in r and "count" in r
    assert "待评估池" in r["note"]


def test_quality_suggestion_map_complete():
    for key, sug in insight.QUALITY_SUGGESTION.items():
        assert sug.get("advice"), key


def test_dataset_stats(db):
    s = insight.dataset_stats(db)
    for k in ("chapters_total", "chapters_trainable", "readiness", "consent"):
        assert k in s, k
    assert s["readiness"]["level"] in ("insufficient", "early", "ok", "ready")


def test_readiness_levels():
    assert insight._readiness(10)["level"] == "insufficient"
    assert insight._readiness(200)["level"] == "early"
    assert insight._readiness(1000)["level"] == "ok"
    assert insight._readiness(10000)["level"] == "ready"


def test_ab_framework_structure(db):
    r = insight.ab_framework(db, limit=50)
    assert "variants" in r and "note" in r


def test_ab_requires_min_samples(db):
    """★ 样本 <3 不纳入比较（避免误导）。"""
    r = insight.ab_framework(db, limit=50)
    assert all(v["n"] >= 3 for v in r["variants"])


# ============================================================ HTTP 层

@pytest.fixture()
def app_client():
    import sys
    import importlib
    m = sys.modules.get("apps.api.main") or importlib.import_module("apps.api.main")
    from fastapi.testclient import TestClient
    with TestClient(m.app) as c:
        yield c


def _token(role: str = "admin"):
    a = _mod("auth")
    SL = _mod("db").SessionLocal
    from apps.api.models import User
    import sqlalchemy as sa
    with SL() as db:
        if role == "admin":
            u = db.scalar(sa.select(User).where(User.role == "admin"))
            if u is None:
                u = a.create_user(db, f"fbadm_{uuid.uuid4().hex[:6]}",
                                  "fb-123456", role="admin")
                db.commit()
        else:
            u = a.create_user(db, f"fbusr_{uuid.uuid4().hex[:6]}",
                              "fb-123456", role="user")
            db.commit()
        return a.make_token(u.id)


def test_http_submit_feedback(app_client):
    tok = _token()
    r = app_client.post("/api/feedback",
                        json={"kind": "bug", "title": "接口测试反馈",
                              "content": "这是通过 HTTP 提交的测试反馈内容",
                              "context": {"page": "/x", "content": "不该进来"}},
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert "content" in d["dropped_keys"]


def test_http_my_feedback(app_client):
    tok = _token()
    r = app_client.get("/api/me/feedback",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "items" in r.json()


def test_http_feedback_meta(app_client):
    r = app_client.get("/api/me/feedback/meta")
    assert r.status_code == 200
    assert len(r.json()["kinds"]) == 4


def test_http_admin_feedback_list(app_client):
    tok = _token()
    r = app_client.get("/api/admin/feedback",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "items" in r.json()


def test_http_admin_feedback_stats(app_client):
    tok = _token()
    r = app_client.get("/api/admin/feedback/stats",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200


def test_http_admin_feedback_hot(app_client):
    tok = _token()
    r = app_client.get("/api/admin/feedback/hot?days=30&threshold=2",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "hot" in r.json()


def test_http_pool_not_shadowed_by_fb_id(app_client):
    """★★ 路由顺序守卫：静态路径不能被动态路径遮蔽。

    踩坑：`POST /api/admin/feedback/pool` 曾被
    `POST /api/admin/feedback/{fb_id}` 抢先匹配（fb_id="pool"），
    导致 pool 端点永远返回"反馈不存在"。
    FastAPI 按注册顺序匹配 → 静态路径必须在前。
    """
    tok = _token()
    r = app_client.post("/api/admin/feedback/pool",
                        json={"keyword": "测试关键词xyz"},
                        headers={"Authorization": f"Bearer {tok}"})
    # 关键：不能是"反馈不存在"（那说明被 {fb_id} 抢了）
    assert r.status_code == 200, r.text[:200]
    assert "pooled" in r.json(), r.text[:200]


def test_http_pool_empty_keyword(app_client):
    tok = _token()
    r = app_client.post("/api/admin/feedback/pool", json={"keyword": ""},
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 400


def test_http_admin_feedback_export(app_client):
    tok = _token()
    r = app_client.get("/api/admin/feedback/export",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")


def test_http_telemetry_disclosure_public(app_client):
    """★ 透明说明无需登录（用户有权先看再决定）。"""
    r = app_client.get("/api/telemetry/disclosure")
    assert r.status_code == 200
    d = r.json()
    assert "collect" in d and "never_collect" in d


def test_http_telemetry_preview(app_client):
    tok = _token()
    r = app_client.get("/api/admin/telemetry/preview",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "redline_ok" in r.json()


def test_http_insight_dashboard(app_client):
    tok = _token()
    r = app_client.get("/api/admin/insight/dashboard?days=7",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "feature_heat" in r.json()


def test_http_insight_suggestions(app_client):
    tok = _token()
    r = app_client.get("/api/admin/insight/suggestions",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200


def test_http_training_consent_get(app_client):
    tok = _token()
    r = app_client.get("/api/admin/training/consent",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "granted" in r.json()


def test_http_training_preview_blocked_without_consent(app_client):
    """★★ 未授权时预览也必须被拒（HTTP 层验证）。"""
    SL = _mod("db").SessionLocal
    with SL() as s:
        _clean_setting(s, insight.CONSENT_KEY)
    tok = _token()
    r = app_client.get("/api/admin/training/preview?limit=2",
                       headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["consent_required"] is True


def test_http_training_export_rejects_bad_path(app_client):
    tok = _token()
    SL = _mod("db").SessionLocal
    with SL() as s:
        insight.set_consent(s, True)
    try:
        r = app_client.post("/api/admin/training/export",
                            json={"path": "/etc/passwd"},
                            headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 400
    finally:
        with SL() as s:
            _clean_setting(s, insight.CONSENT_KEY)


def test_http_training_export_requires_consent(app_client, tmp_path):
    tok = _token()
    SL = _mod("db").SessionLocal
    with SL() as s:
        _clean_setting(s, insight.CONSENT_KEY)
    r = app_client.post("/api/admin/training/export",
                        json={"path": str(tmp_path / "d.jsonl")},
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


def test_http_anonymous_rejected(app_client):
    """★ 反馈与洞察属敏感数据，匿名必须被拦。"""
    for path in ("/api/me/feedback", "/api/admin/feedback",
                 "/api/admin/insight/dashboard", "/api/admin/training/consent",
                 "/api/me/telemetry"):
        r = app_client.get(path)
        assert r.status_code in (401, 403), f"{path} 未拦截匿名（{r.status_code}）"


def test_http_non_admin_blocked(app_client):
    """★ 普通用户不能看全局洞察与训练数据。"""
    tok = _token("user")
    for path in ("/api/admin/feedback", "/api/admin/insight/dashboard",
                 "/api/admin/training/consent",
                 "/api/admin/telemetry/preview"):
        r = app_client.get(path, headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code in (401, 403), f"{path} 未拦截普通用户"

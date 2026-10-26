# -*- coding: utf-8 -*-
"""v1.0-E4.11 功能守卫：协议页/导出 DOCX+PDF/日更趋势/云端视频通道。

覆盖调研报告差距 G1-G6 中落地的四项：
- G1 协议页（路由 + 注册勾选字段）
- G3 导出 DOCX/PDF
- G4/G5 日更趋势接口 + list_books updated_at
- G6 云端视频通道（注册表/折算/媒体分派）
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "apps" / "api"
WEB = ROOT / "apps" / "web2" / "src"


# ---------------------------------------------------------------- G1 协议

def test_terms_privacy_routes_registered():
    """/terms /privacy 前端路由存在且挂在营销布局下。"""
    txt = (WEB / "router" / "index.ts").read_text(encoding="utf-8")
    assert "path: 'terms'" in txt
    assert "path: 'privacy'" in txt
    # 必须是 intro 路由块（营销布局 children）之后、login 之前的兄弟节点
    intro_pos = txt.index("name: 'intro'")
    terms_pos = txt.index("name: 'terms'")
    login_pos = txt.index("path: '/login'")
    assert intro_pos < terms_pos < login_pos


def test_register_view_has_agreement_checkbox():
    """注册页有协议勾选 + 未勾选禁用提交。"""
    txt = (WEB / "views" / "auth" / "RegisterView.vue").read_text(encoding="utf-8")
    assert 'v-model="agreed"' in txt
    assert ":disabled=\"!agreed\"" in txt
    assert "/terms" in txt and "/privacy" in txt
    # 提交函数里有协议兜底校验
    assert "请先阅读并同意" in txt


def test_register_backend_records_agreed_terms():
    """注册端点支持 agreed_terms 字段；显式 False 被拒。"""
    from apps.api.routers.system import RegisterIn
    m = RegisterIn(username="ab", password="x" * 8)
    assert m.agreed_terms is True            # 缺省 True（老客户端兼容）
    m2 = RegisterIn(username="ab", password="x" * 8, agreed_terms=False)
    assert m2.agreed_terms is False


def test_user_model_has_agreed_terms_columns():
    """User 模型与迁移注册表都声明了 agreed_terms 列（★ create_all 不改老库）。"""
    from apps.api.models import User
    assert hasattr(User, "agreed_terms_at")
    assert hasattr(User, "agreed_terms_version")
    mig = (SRC / "services" / "db_migrate.py").read_text(encoding="utf-8")
    assert '"agreed_terms_at"' in mig
    assert '"agreed_terms_version"' in mig


def test_marketing_footer_renders_icp():
    """页脚渲染 ICP（有值才渲染，链接到工信部）。"""
    txt = (WEB / "layouts" / "MarketingLayout.vue").read_text(encoding="utf-8")
    assert 'v-if="icp"' in txt
    assert "beian.miit.gov.cn" in txt
    assert "/api/public/site" in txt


# ---------------------------------------------------------------- G3 导出

def _fake_book_chapters():
    from types import SimpleNamespace
    bk = SimpleNamespace(title="守卫测试书", author="作者", synopsis="简介")
    chs = [SimpleNamespace(idx=i, title=f"第{i}章", content=f"段落一{i}。\n段落二。")
           for i in (1, 2, 3)]
    return bk, chs


def test_export_docx_and_pdf():
    from apps.api import exporter
    bk, chs = _fake_book_chapters()
    for fmt, magic in (("docx", b"PK"), ("pdf", b"%PDF")):
        data, mime, fname = exporter.build(bk, chs, fmt)
        assert data[:len(magic)] == magic, f"{fmt} 魔数不对"
        assert len(data) > 500
        assert fname.endswith("." + fmt)


def test_export_unknown_format_still_rejected():
    from apps.api import exporter
    bk, chs = _fake_book_chapters()
    with pytest.raises(ValueError):
        exporter.build(bk, chs, "nosuch")


# ------------------------------------------------- G4/G5 日更与 updated_at

def test_daily_writes_endpoint_registered_and_guarded():
    """日更趋势端点存在（鉴权由全局依赖体系负责）。

    ★ app.routes 的扁平遍历拿不到 include_router 挂的子路由
      （_IncludedRouter 代理对象），所以用 TestClient 真实 HTTP 判定——
      401=已挂载且被鉴权保护；404=没挂上。
    """
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.get("/api/stats/daily-writes")
    assert r.status_code == 401, f"期望 401（未登录保护），实际 {r.status_code}"


def test_list_books_returns_updated_at():
    """list_books 响应含 updated_at（源级断言：序列化分支存在）。"""
    txt = (SRC / "main.py").read_text(encoding="utf-8")
    m = re.search(r"def list_books.*?return out", txt, re.S)
    assert m, "list_books 未找到"
    body = m.group(0)
    assert "func.max(Chapter.updated_at)" in body
    assert '"updated_at"' in body


def test_frontend_statsview_consumes_daily_writes():
    """StatsView 已接日更趋势（不再写"待后端补接口"）。"""
    txt = (WEB / "views" / "data" / "StatsView.vue").read_text(encoding="utf-8")
    assert "dailyWrites" in txt
    assert "待后端补充接口" not in txt


# ---------------------------------------------------------------- G6 云视频

def test_cloud_video_registry_and_pricing():
    """云端视频注册表 ≥1 家且折算结果为正（免费通道允许 0 元，如 Agnes flash）。"""
    from apps.api import cloud_video as cv
    assert len(cv.PROVIDERS) >= 1
    # ★ v1.0-E4.12：单价比对放宽到 ≥0——agnes flash 是免费额度通道（0 元/秒），
    #   其余付费通道折算必须为正。
    assert all(p.yuan_per_second >= 0 for p in cv.PROVIDERS.values())
    assert any(p.yuan_per_second == 0 for p in cv.PROVIDERS.values()), \
        "至少应有一家免费通道（agnes）"
    cr, yuan = cv.estimate_credits(10, "openai-video")
    assert cr > 0 and yuan > 0
    # 未知 provider 折算安全归零（不抛异常）
    cr2, yuan2 = cv.estimate_credits(10, "no-such")
    assert cr2 == 0 and yuan2 == 0


def test_cloud_video_templates_merged_into_admin_view():
    """后台模板清单合并云端通道（kind=cloud-video）。"""
    from apps.api import cloud_video as cv
    ts = cv.list_cloud_templates()
    assert ts and all(t["kind"] == "cloud-video" for t in ts)
    src = (SRC / "routers" / "ads_models.py").read_text(encoding="utf-8")
    assert "list_cloud_templates" in src


def test_media_dispatches_cloud_video_provider():
    """media.run_media 分派了 cloud-video 分支（接线守卫，防"建了没触发"）。"""
    src = (SRC / "media.py").read_text(encoding="utf-8")
    assert "_cloud_video.PROVIDERS" in src
    assert "cloudvideo:{call_id}" in src          # 计费幂等键
    assert "from apps.api import cloud_video as _cloud_video" in src


def test_cloud_video_generates_requires_key():
    """无 Key 时给明确报错（不是模糊 500）。"""
    import asyncio
    from types import SimpleNamespace
    from apps.api import cloud_video as cv
    m = SimpleNamespace(provider="openai-video", base_url="http://x",
                        model_id="m1", api_key="")
    with pytest.raises(cv.CloudVideoError):
        asyncio.run(cv.generate(m, "测试提示词"))


def test_plans_endpoints_use_config_update_not_model_manage():
    """★ 套餐管理全端点权限统一 config.update（E4.11 403 事故守卫）。

    事故：列表挂 model.manage（只给 owner），写接口却用 config.update
    （admin 有）——管理员打开 /admin/plans 列表必 403。读比写严=倒挂。
    """
    src = (SRC / "routers" / "plans_admin.py").read_text(encoding="utf-8")
    n_endpoints = src.count('@router.')
    n_config = src.count('require_perm("config.update")')
    assert n_config == n_endpoints, (
        f"plans_admin.py 有 {n_endpoints} 个端点，"
        f"只有 {n_config} 个用 config.update —— 存在权限口径不一致")
    assert 'require_perm("model.manage")' not in src


# ---------------------------------------------------------------- G7 Agnes AI

def _run(coro):
    import asyncio
    return asyncio.run(coro)


def _agnes_profile():
    from types import SimpleNamespace
    return SimpleNamespace(provider="agnes", base_url="https://apihub.agnes-ai.com/v1",
                           model_id="agnes-video-2.5-flash", name="Agnes 视频",
                           api_key="sk-test")


async def _fake_download(url):
    assert url == "https://cdn/v.mp4"
    return b"FAKE_MP4"


def test_agnes_registered_in_cloud_video_and_kinds():
    """Agnes 视频 provider 双声明一致：cloud_video.PROVIDERS + PROVIDER_KINDS。"""
    from apps.api import cloud_video as cv
    from apps.api import model_router as mr
    assert "agnes" in cv.PROVIDERS
    assert "agnes" in mr.PROVIDER_KINDS
    p = cv.PROVIDERS["agnes"]
    assert p.adapt_submit == "agnes"
    assert p.submit_path == "/videos" and p.query_path == "/videos/{id}"
    assert p.seconds_max == 8                      # Agnes duration 上限 8s
    assert p.yuan_per_second == 0.0                # flash 免费


def test_agnes_submit_body_uses_duration_and_clamps():
    """提交体用 duration（非 seconds/size），超长钳到 8s。"""
    from apps.api import cloud_video as cv
    p = cv.PROVIDERS["agnes"]
    body = cv._build_submit_body(p, "测试", seconds=30, width=1920, height=1080)
    assert body["duration"] == 8
    assert "seconds" not in body and "size" not in body


def test_agnes_free_channel_zero_credits_and_paid_regression():
    """免费通道 0 积分；付费通道（openai-video）计费不受影响。"""
    from apps.api import cloud_video as cv
    cr, yuan = cv.estimate_credits(10, "agnes")
    assert cr == 0 and yuan == 0.0
    cr2, yuan2 = cv.estimate_credits(10, "openai-video")
    assert cr2 > 0 and yuan2 > 0


def test_agnes_response_parse_chain():
    """Agnes 响应兼容：video_id 任务号 + metadata.url 结果直链。"""
    from apps.api import cloud_video as cv
    import apps.api.cloud_video as cvmod
    from types import SimpleNamespace

    class FakeResp:
        def __init__(self, payload, status=200):
            self._p, self.status_code = payload, status
            self.text = json.dumps(payload, default=str)[:100]
        def json(self): return self._p

    calls = {"n": 0}

    class FakeClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            calls["n"] += 1
            return FakeResp({"video_id": "vid_X", "status": "queued"})
        async def get(self, url, headers=None):
            return FakeResp({"status": "completed",
                             "metadata": {"url": "https://cdn/v.mp4"}})

    orig_httpx = cvmod.httpx
    orig_dl = cvmod._download
    orig_kv = cvmod.keyvault
    try:
        cvmod.httpx = SimpleNamespace(AsyncClient=FakeClient)
        cvmod._download = _fake_download
        cvmod.keyvault = SimpleNamespace(get_api_key=lambda p: "sk-test")
        data = _run(cv.generate(_agnes_profile(), "测试提示词"))
        assert data == b"FAKE_MP4"
    finally:
        cvmod.httpx = orig_httpx
        cvmod._download = orig_dl
        cvmod.keyvault = orig_kv


def test_media_skip_billing_for_free_cloud_channel():
    """免费云端通道（cr==0）不调用 consume（consume 要求 amount>0）。"""
    src = (SRC / "media.py").read_text(encoding="utf-8")
    assert "if _owner and cr > 0:" in src

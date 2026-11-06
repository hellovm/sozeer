"""v0.27 回归：agnes 裸 base_url（缺 /v1）被 WAF 403 拦截时自动回退 /v1。

复现用户线上缺陷：开书时模型调用 HTTP 403 + 一大段 [if IE] HTML 拦截页。
根因：实际调用 _endpoint 不补 /v1，而 test-model 端点补了 /v1 容错 → 测试绿、调用红。
修复：_complete_once / stream / stream_usage / act / act_stream 加 /v1 候选回退 + 错误脱敏。
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# 让 `apps` 包可被导入（项目根加入 sys.path）
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.api import llm  # noqa: E402


def test_v1_fallbacks_agnes_bare_url():
    # agnes 裸 base_url 漏 /v1 → 应补 /v1 候选
    got = llm._v1_fallbacks("https://apihub.agnes-ai.com/chat/completions")
    assert got == ["https://apihub.agnes-ai.com/v1/chat/completions"]


def test_v1_fallbacks_zero_regression_when_already_v1():
    # 正常已经带 /v1 → 不应追加任何候选（零回归）
    for u in (
        "https://api.openai.com/v1/chat/completions",
        "https://x.com/v1/chat/completions",
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    ):
        assert llm._v1_fallbacks(u) == [], u


def test_v1_fallbacks_zero_regression_full_endpoint():
    # 已是完整 /chat/completions 端点（base 已含 /v1 之外的路径）也不追加
    assert llm._v1_fallbacks("https://my.proxy/v1/chat/completions") == []


def test_looks_like_html_detects_waf_page():
    waf = "<!DOCTYPE html><html><head><title>Access Denied</title></head>" \
          "<body>[if IE] Please enable JavaScript</body></html>"
    assert llm._looks_like_html(waf) is True
    assert llm._looks_like_html('{"error":"invalid key"}') is False
    assert llm._looks_like_html("HTTP 401: unauthorized") is False


def test_err_body_msg_desensitizes_html():
    waf = "<html><body>[if IE] Please enable JavaScript</body></html>"
    msg = llm._err_body_msg(403, waf)
    assert "[if IE]" not in msg
    assert "WAF" in msg and "/v1" in msg

    # 真实 JSON 错误体应保留精简原文
    raw = llm._err_body_msg(401, '{"error":{"message":"Incorrect API key"}}')
    assert "Incorrect API key" in raw


class _FakeAsyncClient:
    """替代 httpx.AsyncClient：__aenter__ 返回自身，post 调用注入的函数。"""
    def __init__(self, post_fn):
        self._post_fn = post_fn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        return await self._post_fn(url, headers, json)


class _FakeResp:
    def __init__(self, status_code, text, json_data=None):
        self.status_code = status_code
        self._text = text
        self._json = json_data

    @property
    def text(self):
        return self._text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _make_client_that_falls_back_to_v1():
    """裸 URL 返回 403+WAF HTML，/v1 URL 返回正常 JSON。"""
    captured = {}

    async def post(url, headers=None, json=None):
        captured["url"] = url
        if url.endswith("/v1/chat/completions"):
            return _FakeResp(200, "{}", {
                "choices": [{"message": {"content": "沧海一声笑"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            })
        # 裸 URL（缺 /v1）→ WAF 403 + HTML 拦截页
        return _FakeResp(403, "<html><body>[if IE] Please enable JavaScript</body></html>")

    return _FakeAsyncClient(post), captured


def test_complete_falls_back_to_v1_on_waf_403(monkeypatch):
    fake_client, captured = _make_client_that_falls_back_to_v1()

    # 让 _pick_endpoint 返回 agnes 裸 URL（缺 /v1）
    monkeypatch.setattr(
        llm, "_pick_endpoint",
        lambda p: ({"Authorization": "Bearer k"},
                   "https://apihub.agnes-ai.com/chat/completions"),
    )
    # 用 fake_client 替换每次 new 的 AsyncClient
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: fake_client)

    prof = MagicMock()
    prof.model_id = "agnes-3.0-flash"
    prof.base_url = "https://apihub.agnes-ai.com"
    prof.temperature = 0.7
    prof.max_tokens = 4096

    async def _run():
        return await llm.complete(prof, [{"role": "user", "content": "你好"}],
                                  max_tokens=4096, timeout=30)
    r = asyncio.run(_run())
    # 必须回退到 /v1 并成功拿回正文
    assert r.error == "", f"应回退 /v1 成功，却报错: {r.error}"
    assert r.text == "沧海一声笑"
    assert captured["url"].endswith("/v1/chat/completions")


def test_complete_does_not_spin_on_real_api_error(monkeypatch):
    """真实 API 错误（JSON 401，非 HTML）不应无限回退重试，应直接报脱敏错误。"""
    async def post(url, headers=None, json=None):
        return _FakeResp(401, '{"error":{"message":"Incorrect API key"}}')

    fake_client = _FakeAsyncClient(post)
    monkeypatch.setattr(
        llm, "_pick_endpoint",
        lambda p: ({"Authorization": "Bearer bad"},
                   "https://api.x.com/v1/chat/completions"),
    )
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: fake_client)

    prof = MagicMock()
    prof.model_id = "m"; prof.base_url = "https://api.x.com/v1"
    prof.temperature = 0.7; prof.max_tokens = 4096

    async def _run():
        return await llm.complete(prof, [{"role": "user", "content": "hi"}],
                                  max_tokens=4096, timeout=30)
    r = asyncio.run(_run())
    assert r.error and "Incorrect API key" in r.error


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

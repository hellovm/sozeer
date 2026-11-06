"""模型网关：云端 API，OpenAI 兼容协议。

覆盖 DeepSeek / 智谱 / 通义 / Kimi / OpenAI / OpenRouter / 自建 vLLM / Ollama，
只要它们暴露 /v1/chat/completions。预留 provider 扩展点（Anthropic 走 messages 协议）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from apps.api.models import ModelProfile


@dataclass
class LLMResult:
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    model: str = ""
    duration_ms: int = 0
    error: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class AgentTurn:
    """一次带工具调用的模型回合。"""
    text: str = ""
    tool_calls: list = field(default_factory=list)   # [{id, name, arguments}]
    error: str = ""
    model: str = ""
    # 原来这三个字段不存在，调用方只能写 round((turn and 0 or 0) + ...) 这种恒零表达式，
    # 工具调用那一轮（最贵的一轮，带完整 system prompt + 状态快照 + 全部工具定义）
    # 的花费永远不计入账单。
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0


async def act(
    p: ModelProfile,
    messages: list[dict],
    tools: list[dict],
    tool_choice: str = "auto",
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float = 600.0,
) -> AgentTurn:
    """OpenAI 风格 tools 调用（DeepSeek / 智谱 / 通义 / Kimi 均支持）。

    返回自然回复 text 与待执行的 tool_calls。调用方负责执行工具并把结果回灌模型。
    """
    payload: dict[str, Any] = {
        "model": p.model_id,
        "messages": messages,
        "temperature": p.temperature if temperature is None else temperature,
        "max_tokens": max_tokens or p.max_tokens,
        "tools": tools,
        "tool_choice": tool_choice,
        "stream": False,
    }
    t0 = time.time()
    _ep_hdr, _ep_url = _pick_endpoint(p)   # v0.26.2 多供应商分流
    urls = [_ep_url, *_v1_fallbacks(_ep_url)]   # ★ /v1 回退候选（agnus 403 根治，已含 /v1 → 仅主端点）
    r = None
    async with httpx.AsyncClient(timeout=timeout) as cli:
        for idx, u in enumerate(urls):
            try:
                r = await cli.post(u, headers=_ep_hdr, json=payload)
            except httpx.HTTPError as e:
                return AgentTurn(error=f"网络错误: {e}", model=p.model_id)
            if r.status_code < 400:
                break
            if _looks_like_html(r.text or "") and idx < len(urls) - 1:
                continue        # WAF/网关拦截（返回 HTML）→ 试 /v1 候选
            break               # 真 API 错误（JSON 错误体）→ 不再重试
    if r.status_code >= 400:
        return AgentTurn(error=_err_body_msg(r.status_code, r.text or ""), model=p.model_id)
    data = r.json()
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    text = msg.get("content") or ""
    tcs: list[dict] = []
    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {}
        tcs.append({"id": tc.get("id"), "name": fn.get("name"), "arguments": args})
    # 与 complete() 同样口径解析 usage，拿不到就保持 0，不猜。
    usage = data.get("usage") or {}
    tin = int(usage.get("prompt_tokens") or 0)
    tout = int(usage.get("completion_tokens") or 0)
    return AgentTurn(text=text, tool_calls=tcs, model=p.model_id,
                    input_tokens=tin, output_tokens=tout,
                    cost=calc_cost(p, tin, tout) if tin or tout else 0.0,
                    error="" if (text or tcs) else "模型未返回内容")


class LLMError(RuntimeError):
    pass


def _headers(p: ModelProfile) -> dict[str, str]:
    from apps.api import keyvault
    h = {"Content-Type": "application/json"}
    key = keyvault.get_api_key(p)   # Key 库中是密文，请求时才解密
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _pick_endpoint(p: ModelProfile) -> tuple[dict[str, str], str]:
    """★ v0.26.2：**一次性**解析出 (headers, 完整端点 URL)。

    ★ 为什么必须一次性解析：headers 与 endpoint 若来自两次独立解析，
      可能选中不同端点 → 用 A 的 key 打 B → 401。
    ★ 零回归：无 ModelProvider 记录时回退 profile 自身，行为与升级前一致。
    """
    try:
        from apps.api import model_router as _mr
        from apps.api.db import SessionLocal as _SL
        with _SL() as _db:
            base, key = _mr.resolve_endpoint(_db, p)
        if base:
            h = {"Content-Type": "application/json"}
            if key:
                h["Authorization"] = f"Bearer {key}"
            return h, _endpoint_of(p, base)
    except Exception:                                   # noqa: BLE001
        pass                                            # 路由故障 → 单端点兜底
    return _headers(p), _endpoint(p)


def _endpoint(p: ModelProfile) -> str:
    base = (p.base_url or "").rstrip("/")
    if not base:
        raise LLMError("模型未配置 base_url")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _endpoint_of(p: ModelProfile, base: str) -> str:
    """由给定 base_url 拼出 chat completions 端点。"""
    b = (base or "").rstrip("/")
    if not b:
        raise LLMError("模型未配置 base_url")
    if b.endswith("/chat/completions"):
        return b
    return b + "/chat/completions"


def _v1_fallbacks(primary_url: str) -> list[str]:
    """★ 与测试端点 _openai_compat_checks 的 /v1 补探对称（agnus 403 根治）。

    base_url 漏 /v1（如 https://apihub.agnes-ai.com）时，主端点实际打到根路径
    /chat/completions → 供应商 WAF 把请求丢给 HTML 拦截页（403 + <html>/[if IE]）。
    这里补一个 {base}/v1/chat/completions 候选，主端点被 WAF 拦截时自动救回。

    ★ 零回归：正常 base_url（已含 /v1 或已是完整 /chat/completions 端点）返回 []，
      请求行为与升级前完全一致。只有"裸 base_url"才追加 /v1 候选。
    """
    base = (primary_url or "").rsplit("/chat/completions", 1)[0].rstrip("/")
    if not base or base.endswith("/v1"):
        return []
    return [base + "/v1/chat/completions"]


def _looks_like_html(body: str) -> bool:
    """判断响应体是否像 HTML/WAF 拦截页（而非 API 的 JSON/纯文本错误）。

    命中即说明打到了**非 API 端点**（网关把请求丢给了 WAF / 登录页 / 默认页）：
    此时响应是一大段 <html>，塞进错误信息对用户毫无价值 → 应转为可操作建议。
    """
    low = (body or "").strip()[:600].lower()
    return any(tok in low for tok in (
        "<html", "<!doctype", "[if ie]", "please enable javascript",
        "<script", "<body", "<!document"))


def _err_body_msg(code: int, body: str) -> str:
    """4xx/5xx 错误脱敏。

    · HTML/WAF 拦截页 → 给出"地址可能缺 /v1"的可操作建议（不再整段吐 <html>）
    · 真实 API 错误（JSON/短文本）→ 保留精简原文（截 200 字），便于定位
    """
    s = (body or "").strip()
    if _looks_like_html(s):
        return (f"HTTP {code}：请求被 API 网关/WAF 拦截（返回的是 HTML 页面而非接口响应）。"
                "多半是 API 地址缺少 /v1 或拼错（实际打到了根路径）。"
                "请到模型中心把 API 地址改为带 /v1 的形式（如 https://api.example.com/v1）后重试")
    return f"HTTP {code}: {s[:400]}"


async def complete(
    p: ModelProfile,
    messages: list[dict[str, str]],
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float = 600.0,
    json_mode: bool = False,
) -> LLMResult:
    """非流式补全。空响应自动重试一次（max_tokens 翻倍）。

    思考型模型偶发把 max_tokens 全耗在推理链上（finish_reason=length 且
    content 为空）。一次重试 + 预算翻倍能救回大多数这种失败，省得用户手动重跑。
    """
    r = await _complete_once(p, messages, temperature, max_tokens, timeout, json_mode)
    if r.error and "耗尽了 max_tokens" in r.error:
        doubled = (max_tokens or p.max_tokens) * 2
        r2 = await _complete_once(p, messages, temperature, doubled, timeout, json_mode)
        if not r2.error and (r2.text or "").strip():
            return r2
        # 重试仍失败：返回带可操作建议的原错误
        return LLMResult(
            error=(f"模型在输出正文前耗尽了 max_tokens（已自动重试翻倍至 {doubled} 仍失败）。"
                   "请到模型中心把该模型的 max_tokens 调大到 16384 以上，或换用非思考/非推理模式"),
            model=p.model_id)
    return r


async def _complete_once(
    p: ModelProfile,
    messages: list[dict[str, str]],
    temperature: float | None,
    max_tokens: int | None,
    timeout: float,
    json_mode: bool,
) -> LLMResult:
    payload: dict[str, Any] = {
        "model": p.model_id,
        "messages": messages,
        "temperature": p.temperature if temperature is None else temperature,
        "max_tokens": max_tokens or p.max_tokens,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    t0 = time.time()
    _ep_hdr, _ep_url = _pick_endpoint(p)   # v0.26.2 多供应商分流
    urls = [_ep_url, *_v1_fallbacks(_ep_url)]   # ★ /v1 回退候选（agnus 403 根治，已含 /v1 → 仅主端点）
    r = None
    async with httpx.AsyncClient(timeout=timeout) as cli:
        for idx, u in enumerate(urls):
            try:
                r = await cli.post(u, headers=_ep_hdr, json=payload)
            except httpx.HTTPError as e:
                return LLMResult(error=f"网络错误: {e}", model=p.model_id)
            if r.status_code < 400:
                break
            if _looks_like_html(r.text or "") and idx < len(urls) - 1:
                continue        # WAF/网关拦截（返回 HTML）→ 试 /v1 候选
            break               # 真 API 错误（JSON 错误体）→ 不再重试

    if r.status_code >= 400:
        return LLMResult(
            error=_err_body_msg(r.status_code, r.text or ""),
            model=p.model_id,
            duration_ms=int((time.time() - t0) * 1000),
        )

    data = r.json()
    usage = data.get("usage") or {}
    tin = int(usage.get("prompt_tokens") or 0)
    tout = int(usage.get("completion_tokens") or 0)
    text = ""
    finish = ""
    try:
        finish = data["choices"][0].get("finish_reason") or ""
        text = data["choices"][0]["message"]["content"] or ""
    except Exception:
        text = ""

    # 思考型模型（GLM / DeepSeek-R1 类）偶发把 max_tokens 全部耗在推理链上，
    # 正文 content 为空且 finish_reason=length。翻译成可操作的提示，
    # complete() 外层会自动重试一次。
    if not text.strip() and finish == "length":
        return LLMResult(
            error=(f"模型在输出正文前耗尽了 max_tokens（当前 {max_tokens or p.max_tokens}，"
                   "finish_reason=length，多为思考型模型思考过长）"),
            input_tokens=tin, output_tokens=tout,
            cost=calc_cost(p, tin, tout),
            model=p.model_id,
            duration_ms=int((time.time() - t0) * 1000),
            raw=data)

    return LLMResult(
        text=text,
        input_tokens=tin,
        output_tokens=tout,
        cost=calc_cost(p, tin, tout),
        model=p.model_id,
        duration_ms=int((time.time() - t0) * 1000),
        raw=data,
    )


async def stream(
    p: ModelProfile,
    messages: list[dict[str, str]],
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float = 900.0,
) -> AsyncIterator[str]:
    """按 SSE 增量产出文本。"""
    payload: dict[str, Any] = {
        "model": p.model_id,
        "messages": messages,
        "temperature": p.temperature if temperature is None else temperature,
        "max_tokens": max_tokens or p.max_tokens,
        "stream": True,
    }
    _ep_hdr, _ep_url = _pick_endpoint(p)   # v0.26.2 多供应商分流
    urls = [_ep_url, *_v1_fallbacks(_ep_url)]   # ★ /v1 回退候选（agnus 403 根治）
    async with httpx.AsyncClient(timeout=timeout) as cli:
        try:
            for cand_idx, u in enumerate(urls):
                async with cli.stream("POST", u, headers=_ep_hdr, json=payload) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        text = body.decode('utf-8', 'ignore')
                        if _looks_like_html(text) and cand_idx < len(urls) - 1:
                            continue       # WAF/网关拦截（返回 HTML）→ 试 /v1 候选
                        yield f"\n[模型错误 {_err_body_msg(resp.status_code, text)}]"
                        return
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            return
                        try:
                            d = json.loads(chunk)
                            delta = d["choices"][0].get("delta") or {}
                            if delta.get("content"):
                                yield delta["content"]
                        except Exception:
                            continue
                    return   # 该候选正常读完流，结束（不再试后续候选）
        except httpx.HTTPError as e:
            yield f"\n[网络错误] {e}"


async def stream_usage(
    p: ModelProfile,
    messages: list[dict[str, str]],
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float = 900.0,
) -> AsyncIterator[dict]:
    """★ v0.23.2 D2：带 usage 的流式补全。

    为什么不能复用 stream()：它只 yield 文本，拿不到 token 用量。
    而**正文生成是最大的成本项**（占单章 70%+ token），
    不记账等于成本看板少了大半数据。

    产出事件：{"kind": "token"|"done"|"error", ...}
    · token 事件带 text
    · done 事件带 input_tokens / output_tokens / cost / duration_ms

    ★ 兼容性：请求体里加 `stream_options: {include_usage: true}`
      （OpenAI 兼容协议标准做法）。**不支持该字段的服务端会忽略它**，
      此时 usage 缺失 → 用字符数估算兜底（宁可粗算也不要零）。
    """
    payload: dict[str, Any] = {
        "model": p.model_id,
        "messages": messages,
        "temperature": p.temperature if temperature is None else temperature,
        "max_tokens": max_tokens or p.max_tokens,
        "stream": True,
        # 标准做法；不支持的网关直接忽略
        "stream_options": {"include_usage": True},
    }
    t0 = time.time()
    tin = tout = 0
    n_chars = 0
    _ep_hdr, _ep_url = _pick_endpoint(p)   # v0.26.2 多供应商分流
    urls = [_ep_url, *_v1_fallbacks(_ep_url)]   # ★ /v1 回退候选（agnus 403 根治）
    async with httpx.AsyncClient(timeout=timeout) as cli:
        try:
            for cand_idx, u in enumerate(urls):
                async with cli.stream("POST", u, headers=_ep_hdr,
                                      json=payload) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        text = body.decode('utf-8', 'ignore')
                        if _looks_like_html(text) and cand_idx < len(urls) - 1:
                            continue       # WAF/网关拦截（返回 HTML）→ 试 /v1 候选
                        yield {"kind": "error",
                               "text": f"模型错误 {_err_body_msg(resp.status_code, text)}"}
                        return
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            d = json.loads(chunk)
                        except Exception:
                            continue
                        # usage 可能在任意 chunk 上（多数服务端放最后一个）
                        u = d.get("usage") or {}
                        if u:
                            tin = int(u.get("prompt_tokens") or tin or 0)
                            tout = int(u.get("completion_tokens") or tout or 0)
                        try:
                            delta = d["choices"][0].get("delta") or {}
                        except Exception:
                            continue
                        if delta.get("content"):
                            n_chars += len(delta["content"])
                            yield {"kind": "token", "text": delta["content"]}
                    break   # 该候选正常读完流，结束（不再试后续候选）
        except httpx.HTTPError as e:
            yield {"kind": "error", "text": f"网络错误: {e}"}
            return

    # ★ 兜底估算：网关不返回 usage 时按字符数推算
    #   中文约 1.5 字符/token（保守取 1.6），输入按 prompt 字符估
    if not tout and n_chars:
        tout = max(1, int(n_chars / 1.6))
    if not tin:
        tin = max(0, int(sum(len(m.get("content") or "") for m in messages) / 1.6))
    yield {"kind": "done", "input_tokens": tin, "output_tokens": tout,
           "cost": calc_cost(p, tin, tout),
           "duration_ms": int((time.time() - t0) * 1000),
           "estimated": not bool(tin and tout) or None,
           "model": p.model_id}


def calc_cost(p: ModelProfile, tin: int, tout: int) -> float:
    return round(tin / 1_000_000 * p.price_in + tout / 1_000_000 * p.price_out, 6)


@dataclass
class StreamActEvent:
    """流式工具轮的单个增量事件。"""
    kind: str = "token"          # token | tool_calls | done | error
    text: str = ""
    tool_calls: list = field(default_factory=list)   # 完整累积后的 tool_calls（仅 kind=tool_calls）
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    model: str = ""


async def act_stream(
    p: ModelProfile,
    messages: list[dict],
    tools: list[dict],
    tool_choice: str = "auto",
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float = 600.0,
) -> AsyncIterator[StreamActEvent]:
    """流式版 act()：OpenAI 风格 tools + stream=True。

    边生成边产出 token（前端即时可见）；若模型决定调工具，则在流结束时
    产出一条 kind="tool_calls" 事件（携带完整累积的 tool_calls）。
    最后一条事件固定是 kind="done"（带 usage/cost）或 kind="error"。

    为什么需要它：原先 chat 主循环用非流式 act()，模型读 3 章正文 +
    组织长回复时用户要干等 60–180 秒看不到一个字，超过网关/代理空闲
    超时就表现为"回复被中止"。流式后每几秒就有字节流动，连接不会
    被判空闲，体验也从"转圈等全文"变成"边写边看"。
    """
    payload: dict[str, Any] = {
        "model": p.model_id,
        "messages": messages,
        "temperature": p.temperature if temperature is None else temperature,
        "max_tokens": max_tokens or p.max_tokens,
        "tools": tools,
        "tool_choice": tool_choice,
        "stream": True,
    }
    # 流式协议下 usage 在最后一个 chunk（需 stream_options 显式开启），
    # 部分中转网关不支持该参数会报 400 —— 失败就降级无 usage。
    payload["stream_options"] = {"include_usage": True}

    text_parts: list[str] = []
    # tool_calls 增量累积：OpenAI 流式协议按 index 分片下发 arguments 字符串
    tc_acc: dict[int, dict] = {}
    tin = tout = 0
    finish = ""

    _ep_hdr, _ep_url = _pick_endpoint(p)   # v0.26.2 多供应商分流
    urls = [_ep_url, *_v1_fallbacks(_ep_url)]   # ★ /v1 回退候选（agnus 403 根治）
    async with httpx.AsyncClient(timeout=timeout) as cli:
        try:
            for cand_idx, u in enumerate(urls):
                async with cli.stream("POST", u, headers=_ep_hdr, json=payload) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        text = body.decode('utf-8', 'ignore')
                        if _looks_like_html(text) and cand_idx < len(urls) - 1:
                            continue       # WAF/网关拦截（返回 HTML）→ 试 /v1 候选
                        yield StreamActEvent(kind="error",
                                             text=f"模型错误 {_err_body_msg(resp.status_code, text)}",
                                             model=p.model_id)
                        return
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            d = json.loads(chunk)
                        except Exception:
                            continue
                        choice = (d.get("choices") or [{}])[0]
                        delta = choice.get("delta") or {}
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
                        usage = d.get("usage")
                        if usage:
                            tin = int(usage.get("prompt_tokens") or 0)
                            tout = int(usage.get("completion_tokens") or 0)
                        content = delta.get("content")
                        if content:
                            text_parts.append(content)
                            yield StreamActEvent(kind="token", text=content, model=p.model_id)
                        for tc in (delta.get("tool_calls") or []):
                            tc_idx = int(tc.get("index") or 0)
                            acc = tc_acc.setdefault(tc_idx, {"id": "", "name": "", "arguments": ""})
                            fn = tc.get("function") or {}
                            if tc.get("id"):
                                acc["id"] = tc["id"]
                            if fn.get("name"):
                                acc["name"] = fn.get("name")
                            if fn.get("arguments"):
                                acc["arguments"] += fn["arguments"]
                    break   # 该候选正常读完流，结束（不再试后续候选）
        except httpx.HTTPError as e:
            yield StreamActEvent(kind="error", text=f"网络错误: {e}", model=p.model_id)
            return

    text = "".join(text_parts)
    tool_calls: list[dict] = []
    for idx in sorted(tc_acc.keys()):
        acc = tc_acc[idx]
        try:
            args = json.loads(acc["arguments"]) if acc["arguments"].strip() else {}
        except Exception:
            args = {}
        tool_calls.append({"id": acc["id"] or f"tc_{idx}", "name": acc["name"], "arguments": args})

    cost = calc_cost(p, tin, tout) if tin or tout else 0.0
    if tool_calls:
        yield StreamActEvent(kind="tool_calls", text=text, tool_calls=tool_calls,
                             input_tokens=tin, output_tokens=tout, cost=cost, model=p.model_id)
    elif text:
        yield StreamActEvent(kind="done", text=text,
                             input_tokens=tin, output_tokens=tout, cost=cost, model=p.model_id)
    else:
        # 空响应分两种：思考超预算（length）与真异常。给出可区分的报错。
        hint = ("模型思考过长耗尽 max_tokens，未输出正文"
                if finish == "length" else "模型未返回内容（空响应，可能被网关拦截或模型过载）")
        yield StreamActEvent(kind="error", text=hint, model=p.model_id)


async def ping(p: ModelProfile) -> dict:
    """配置中心连通性测试：按模型角色选择探活方式。

    image/video/tts 角色走各自的真实生成端点（chat/completions 对生图模型
    会 404，导致"明明配好了却提示连通失败"的假阴性）。
    """
    if p.role == "image":
        try:
            from apps.api.media import gen_image_openai, gen_image_comfyui, MediaError
            if p.provider == "comfyui":
                data = await gen_image_comfyui(p, "a red circle on white background",
                                               size="256x256")
            else:
                data = await gen_image_openai(p, "a red circle on white background",
                                              size="256x256")
            return {"ok": True, "model": p.model_id,
                    "reply": f"生图成功（{len(data)} bytes）"}
        except (MediaError, Exception) as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    if p.role == "tts":
        try:
            from apps.api.media import gen_tts_openai, MediaError
            data = await gen_tts_openai(p, "连通性测试", voice="alloy")
            return {"ok": True, "model": p.model_id, "reply": f"语音合成成功（{len(data)} bytes）"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    if p.role == "video":
        return {"ok": False, "error": "视频适配器未接入该厂商，暂无法探活（配置仍会保存）"}
    # 探活预算：不硬编码（思考型模型 8 token 连推理链开头都不够，必然
    # finish_reason=length 空响应——用户把模型 max_tokens 调到多大都"不生效"）。
    # 取模型配置的 1/64（下限 256、上限 4096）：既真实测到"能出正文"，
    # 又不会因探活烧掉大量 token。
    probe_budget = max(256, min(4096, (p.max_tokens or 8192) // 64))
    r = await complete(p, [{"role": "user", "content": "hi"}],
                       max_tokens=probe_budget, timeout=60)
    if r.error:
        return {"ok": False, "error": r.error}
    return {"ok": True, "model": p.model_id, "reply": (r.text or "")[:60]}

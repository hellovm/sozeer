# -*- coding: utf-8 -*-
"""云端视频 API 适配层（v1.0-E4.11，调研报告差距 G6）。

★ 背景与定位（docs/调研_v1.0-E4.11_竞品与市场对比报告.md §三 G6）：
  平台此前只有 4 个本地 ComfyUI 模板（LTX-2.5×2 + MiniMax H3×2）——
  本地方案成本虽低（<16.2 元/分钟的市场锚点），但**要求用户自备 GPU**，
  大众用户走不了。本模块补齐「云端 API 通道」：有 Key 就能出片。

★ 设计（严格复用既有体系，不造第二套）：
  1. **provider 注册表**：`PROVIDERS` 集中声明（对标 MEMORY「集中定义映射」铁律——
     散落 if/elif 正是"模板建了但没人触发"事故的土壤）。
  2. **ModelProfile 复用**：云端视频 = `provider="cloud-video"` 的模型插槽，
     api_key 走 keyvault 密文、base_url 存厂商网关、model_id 存厂商模型名。
     管理后台**零新概念**——和配 LLM 插槽同一套操作。
  3. **协议**：优先 OpenAI 兼容（/v1/videos 或 Sora 式任务接口）——
     国内厂商（火山引擎/可灵网关/阿里百炼）均在向 OpenAI 兼容靠拢；
     不兼容的厂商经 `PROVIDERS[key].adapt` 挂差异化请求构造器，不再加分支。
  4. **计费**：credits.consume 按"元/秒 → 积分"折算，幂等键 ref_id=call_id。
     成本台账照走 MediaCall（est_cost 填预估，actual_cost 填厂商计费返回）。

★ 接线点（grep 调用点核对，v1.0-E4.11）：
  - media.run_media：kind=video 且 m.provider=="cloud-video" → generate()
  - routers/ads_models.py：模板列表透传 list_cloud_templates()（后台可选）
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field

import httpx

from apps.api import keyvault
from apps.api.models import ModelProfile


class CloudVideoError(Exception):
    pass


# ---------------------------------------------------------------- 注册表

@dataclass(frozen=True)
class CloudProvider:
    """一家云端视频厂商的声明（★ 集中注册，禁止散落分支）。"""
    key: str                     # provider key（model_profiles.provider 填这个）
    name: str                    # 后台显示名
    submit_path: str             # 提交任务路径（相对 base_url）
    query_path: str              # 查询任务路径，{id} 占位
    # 元/秒（按 720p 档估算；用于 credits 折算与 MediaCall.est_cost）
    yuan_per_second: float
    # 提交体构造器：把通用参数翻成厂商协议（默认 OpenAI 兼容）
    adapt_submit: str = "openai" # openai | jimeng | kling | agnes
    seconds_max: int = 15
    doc_url: str = ""


PROVIDERS: dict[str, CloudProvider] = {
    # ★ 默认网关：OpenAI 兼容视频任务协议（火山引擎/百炼等兼容网关均可用）。
    #   base_url 存网关根（如 https://ark.cn-beijing.volces.com/api/v3），
    #   model_id 存厂商模型名（如 doubao-seedance-1-0-lite-t2v）。
    "openai-video": CloudProvider(
        key="openai-video", name="OpenAI 兼容视频网关",
        submit_path="/videos/generations", query_path="/videos/{id}",
        yuan_per_second=0.30, adapt_submit="openai", seconds_max=15,
        doc_url="https://platform.volcengine.com/docs"),
    # 即梦（Seedance）直连网关：提交/查询分离的任务协议
    "jimeng": CloudProvider(
        key="jimeng", name="即梦 Seedance（火山引擎）",
        submit_path="/videos/generations", query_path="/videos/{id}",
        yuan_per_second=0.28, adapt_submit="openai", seconds_max=30,
        doc_url="https://www.volcengine.com/product/doubao"),
    # 可灵开放平台：任务制（submit → poll）
    "kling": CloudProvider(
        key="kling", name="可灵 Kling（快手开放平台）",
        submit_path="/v1/videos/text2video", query_path="/v1/videos/text2video/{id}",
        yuan_per_second=0.35, adapt_submit="kling", seconds_max=10,
        doc_url="https://app.klingai.com/docs"),
    # ★ v1.0-E4.12：Agnes AI（OpenAI 风格视频任务，flash 系列免费额度）。
    #   协议差异三处：提交体用 duration（秒，≤8）；结果直链在 metadata.url；
    #   视频限频 1 次/分钟（429 需退避重试）。base_url 填 https://apihub.agnes-ai.com/v1
    #   （国际站）或 https://api.agnes-ai.cn/v1（中国站），两站 Key 不通用。
    "agnes": CloudProvider(
        key="agnes", name="Agnes AI（agnes-video-2.5-flash，免费额度）",
        submit_path="/videos", query_path="/videos/{id}",
        yuan_per_second=0.0, adapt_submit="agnes", seconds_max=8,
        doc_url="https://agnes-ai.com/doc/overview"),
}


def list_cloud_templates() -> list[dict]:
    """云端视频"模板"清单（后台下拉用）。kind=cloud-video，与 ComfyUI 模板并排。"""
    return [{
        "key": p.key, "name": p.name,
        "desc": (f"云端视频 API · 约 {p.yuan_per_second:.2f} 元/秒 · "
                 f"单段 ≤{p.seconds_max}s。需在模型中心配置 provider={p.key} "
                 "的模型插槽并填入 API Key。"),
        "kind": "cloud-video", "needs": ["prompt"], "optional": ["seconds", "image"],
        "requires_node": "", "models": [],
    } for p in PROVIDERS.values()]


def is_cloud_video(m: ModelProfile) -> bool:
    return (m.provider or "") in PROVIDERS


def estimate_credits(seconds: float, provider_key: str,
                     cents_per_credit: float = 10.0) -> tuple[int, float]:
    """按 元/秒 × 时长 折算积分。

    返回 (credits, est_cost_yuan)。折算比例：1 积分 = 0.10 元
    （与 billing 现行口径对齐；cents_per_credit=10 即 0.1 元/积分）。
    ★ 计费铁律：金额/额度一律服务端重算，前端传值只作展示。
    """
    p = PROVIDERS.get(provider_key)
    if not p:
        return 0, 0.0
    yuan = round(p.yuan_per_second * max(0.0, float(seconds or 0)), 4)
    # ★ 免费通道（yuan_per_second=0，如 Agnes flash）：0 积分即真免费——
    #   但 media.py 的 consume 链路要求 amount>0，免费通道由调用方跳过扣费
    #   （见 run_media 的 cloud-video 分支：cr<=0 不调 consume）。
    credits = max(1, int(yuan / (cents_per_credit / 100.0) + 0.999)) if yuan > 0 else 0
    return credits, yuan


# ---------------------------------------------------------------- 适配器

def _headers(p: ModelProfile) -> dict:
    h = {"Content-Type": "application/json"}
    key = keyvault.get_api_key(p)
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _build_submit_body(p: CloudProvider, prompt: str, *, seconds=None,
                       width=None, height=None, ref_image: str = "") -> dict:
    """按厂商协议构造提交体（注册表 adapt_submit 分派，禁止散落 if/elif）。"""
    s = int(seconds or 5)
    w = int(width or 1280)
    h = int(height or 720)
    if p.adapt_submit == "kling":
        # 可灵文生视频：mode/aspect_ratio/duration
        ratio = "9:16" if h >= w else "16:9"
        return {"model_name": "kling-v1", "prompt": prompt,
                "mode": "pro", "aspect_ratio": ratio,
                "duration": min(s, p.seconds_max)}
    if p.adapt_submit == "agnes":
        # Agnes 视频：duration（秒，≤8，固定 720P）。
        # ★ 限频 1 次/分钟 → 429 交由 generate() 退避重试（不在构造层处理）。
        return {"model": None, "prompt": prompt,
                "duration": min(s, p.seconds_max)}
    # openai / jimeng 统一 OpenAI 兼容任务体
    body: dict = {"model": None, "prompt": prompt, "seconds": min(s, p.seconds_max),
                  "size": f"{w}x{h}"}
    if ref_image:
        # i2v：参考图走 b64（调用方已保证是本地路径读取后的 bytes 交给上层？）
        # ★ 云端通道先支持 t2v（大众路径）；i2v 在 query 侧标注不支持时明确报错。
        body["input_reference"] = ref_image
    return body


async def _download(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=300.0) as cli:
        r = await cli.get(url)
        if r.status_code >= 400:
            raise CloudVideoError(f"下载视频产物失败 HTTP {r.status_code}")
        return r.content


async def generate(p: ModelProfile, prompt: str, *, seconds=None,
                   width=None, height=None, ref_image: str = "") -> bytes:
    """云端视频生成主入口：提交 → 轮询 → 下载。

    ★ 轮询纪律（MEMORY 时区/并发教训的镜像）：轮询循环必须有**总超时上限**，
    绝不允许无限循环占住 worker；每次间隔 sleep 在 async 下用 asyncio.sleep。
    """
    import asyncio

    pdef = PROVIDERS.get(p.provider or "")
    if not pdef:
        raise CloudVideoError(f"未知云端视频 provider：{p.provider}")
    base = (p.base_url or "").rstrip("/")
    if not base:
        raise CloudVideoError("云端视频模型未配置 base_url（厂商网关地址）")
    key = keyvault.get_api_key(p)
    if not key:
        raise CloudVideoError("云端视频模型未配置 API Key（模型中心填写）")

    body = _build_submit_body(pdef, prompt, seconds=seconds, width=width,
                              height=height, ref_image=ref_image)
    body["model"] = p.model_id or ""

    url = f"{base}{pdef.submit_path}"
    headers = _headers(p)
    # ★ Agnes 视频限频 1 次/分钟：429 退避重试（最多等 70s 一轮，共 3 轮）。
    #   其他厂商 429 照常报错（它们没有此限频口径，报错更利于暴露配额问题）。
    submit_retries = 3 if pdef.adapt_submit == "agnes" else 1
    data = None
    async with httpx.AsyncClient(timeout=60.0) as cli:
        for attempt in range(submit_retries):
            try:
                r = await cli.post(url, json=body, headers=headers)
            except httpx.HTTPError as e:
                raise CloudVideoError(f"云端视频提交失败（网络）：{type(e).__name__}: {e}")
            if r.status_code == 429 and attempt < submit_retries - 1:
                import asyncio
                await asyncio.sleep(65.0)     # 官方限频口径 1/min，留 5s 余量
                continue
            if r.status_code >= 400:
                raise CloudVideoError(f"云端视频提交失败 HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            break
    if data is None:                          # 防御：循环耗尽仍未拿到响应
        raise CloudVideoError("云端视频提交失败：重试耗尽（限频未解除）")

    # 任务 id：兼容 {id} / {task_id} / {video_id}(Agnes) / data.id 四种常见形态
    # （外部结构不可信，逐层取）
    task_id = (data.get("id") or data.get("task_id") or data.get("video_id")
               or ((data.get("data") or {}) or {}).get("id") or "")
    if not task_id:
        # 同步返回视频内容的网关（少见，但兼容）
        vurl = ((data.get("data") or [{}]) or [{}])[0].get("url") \
            if isinstance(data.get("data"), list) else None
        if vurl:
            return await _download(vurl)
        raise CloudVideoError(f"云端视频未返回任务 id：{json.dumps(data, default=str)[:200]}")

    qurl = f"{base}{pdef.query_path.format(id=task_id)}"
    # 总超时：15s 片段最多 10 分钟（远大于厂商常规出片时间）
    deadline = time.monotonic() + 600.0
    async with httpx.AsyncClient(timeout=30.0) as cli:
        while time.monotonic() < deadline:
            await asyncio.sleep(6.0)
            try:
                r = await cli.get(qurl, headers=headers)
            except httpx.HTTPError:
                continue                      # 网络抖动：继续轮询，不中断
            if r.status_code >= 500:
                continue
            if r.status_code >= 400:
                raise CloudVideoError(f"云端视频查询失败 HTTP {r.status_code}: {r.text[:200]}")
            st = r.json()
            status = str(st.get("status") or "").lower()
            if status in ("succeeded", "success", "completed", "done"):
                # 视频地址：兼容 video_url / url / output[0] / data[0].url /
                # metadata.url（Agnes 口径：结果直链在 metadata.url）
                vurl = (st.get("video_url") or st.get("url") or "")
                if not vurl:
                    out = st.get("output") or {}
                    vurl = out.get("video_url") if isinstance(out, dict) else ""
                if not vurl and isinstance(st.get("data"), list) and st["data"]:
                    vurl = (st["data"][0] or {}).get("url", "")
                if not vurl:
                    _meta = st.get("metadata") or {}
                    if isinstance(_meta, dict):
                        vurl = _meta.get("url") or ""
                if not vurl:
                    raise CloudVideoError(f"任务完成但未找到视频地址：{json.dumps(st, default=str)[:200]}")
                return await _download(vurl)
            if status in ("failed", "error", "cancelled"):
                msg = st.get("error") or st.get("message") or status
                raise CloudVideoError(f"云端视频任务失败：{msg}")
            # pending/running → 继续轮询
        raise CloudVideoError("云端视频轮询超时（10 分钟）")


__all__ = ["PROVIDERS", "CloudProvider", "CloudVideoError",
           "list_cloud_templates", "is_cloud_video", "estimate_credits",
           "generate"]

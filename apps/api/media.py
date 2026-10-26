"""媒体生成适配器：图像 / 视频 / TTS。

设计（v0.13）：
- 织字工坊模型中心新增 role=image / video / tts 的模型插槽（provider 区分实现）；
- drama.generate_scene_media 按目标种类路由到 pick_model(db, "image"/"video"/"tts")，
  再按 provider 调用对应适配器；
- provider 一览：
  * image: openai-compatible（/v1/images/generations，支持智谱 CogView / 通义万相
    兼容端点 / 各类 OpenAI 兼容生图网关）、comfyui（本地 ComfyUI /prompt API）
  * video: 占位（生成提示词包，提示接入云端视频 API 或 ComfyUI 视频）；
    v1.0-E4.11 新增 provider=cloud-video（OpenAI 兼容/即梦/可灵云端通道）
  * tts: openai-compatible（/v1/audio/speech，兼容 Edge-TTS 网关等）
- 所有产物写到书籍仓库 drama_media/ 下，前端经 /api/files/ 静态取出（带鉴权）。
"""
from __future__ import annotations

import base64
import json
import re
import uuid

import httpx

from apps.api import keyvault
from apps.api.models import ModelProfile, jd
from apps.api import gitrepo
# ★ v1.0-E4.11（G6）：云端视频适配层（provider=cloud-video 的分派在 run_media）
from apps.api import cloud_video as _cloud_video

MEDIA_TYPES = ("image", "video", "audio")


class MediaError(Exception):
    pass


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------- 通用工具

def _clean_prompt(text: str, limit: int = 900) -> str:
    """压掉提示词里的控制符与超长（部分生图网关对长度敏感）。"""
    t = re.sub(r"\s+", " ", (text or "")).strip()
    return t[:limit]


def _save_media(book_id: str, kind: str, ext: str, data: bytes) -> str:
    """产物落库书籍仓库 drama_media/，返回相对路径（供 /api/files/ 使用）。"""
    rel = f"drama_media/{kind}_{new_id('m')}.{ext}"
    gitrepo.write_file(book_id, rel, "", commit_msg="")   # 先确保目录/仓库存在
    p = gitrepo.repo_path(book_id) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return rel


async def _post_json(url: str, payload: dict, headers: dict | None = None,
                     timeout: float = 300.0) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as cli:
        r = await cli.post(url, json=payload, headers=headers or {})
    if r.status_code >= 400:
        raise MediaError(f"HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


# ---------------------------------------------------------------- 图像

async def gen_image_openai(p: ModelProfile, prompt: str, size: str = "1024x1024") -> bytes:
    """OpenAI 兼容生图：POST {base}/images/generations → b64_json 或 url。"""
    base = (p.base_url or "").rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    url = f"{base}/images/generations"
    headers = {"Content-Type": "application/json"}
    _key = keyvault.get_api_key(p)   # Key 库中是密文，请求时才解密
    if _key:
        headers["Authorization"] = f"Bearer {_key}"
    data = await _post_json(url, {"model": p.model_id, "prompt": prompt,
                                  "size": size, "n": 1}, headers,
                            timeout=max(300.0, float(p.max_tokens) or 300.0))
    item = ((data.get("data") or [{}])[0])
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    if item.get("url"):
        async with httpx.AsyncClient(timeout=300.0) as cli:
            r = await cli.get(item["url"])
            if r.status_code >= 400:
                raise MediaError(f"下载生图产物失败 HTTP {r.status_code}")
            return r.content
    raise MediaError(f"生图响应无 b64_json/url 字段：{json.dumps(data)[:200]}")


async def gen_image_comfyui(p: ModelProfile, prompt: str, size: str = "1024x1024",
                            *, ref_image: str = "",
                            ref_images: list[str] | None = None,
                            ip_weight: float | None = None,
                            negative: str = "") -> bytes:
    """本地 ComfyUI /prompt API（Checkpoint 填法）。

    约定：model_id 存 ComfyUI 的 checkpoint 名（如 v1-5-pruned-emaonly.safetensors），
    base_url 存 ComfyUI 服务地址（如 http://127.0.0.1:8188）。

    ★ v0.16 A1（评估报告缺口 3）：支持 IP-Adapter 参考图，实现**跨镜头角色一致性**。
    修复前本函数是硬编码的最简文生图（只有一个 CLIPTextEncode），
    调用方传的 ref_images/ip_weight 被**完全忽略** —— 分镜出图的角色长相
    只能靠 prompt 文字描述，跨集必然漂移（正是报告指出的"根"）。

    参考图工作流要求 ComfyUI 装了 IPAdapter Plus 节点包：
      IPAdapterUnifiedLoader → IPAdapter（可串联多张）→ KSampler.model
    当 ref_images 为空时，**回落到原最简文生图**（零回归，老行为完全不变）。
    """
    host = (p.base_url or "").rstrip("/")
    headers = _comfy_headers(p)

    # 汇总参考图（主图 + 多角色图，去重保序，最多 9 张）
    refs: list[str] = []
    if ref_image:
        refs.append(ref_image)
    for r in (ref_images or []):
        if r and r not in refs:
            refs.append(r)
    refs = refs[:9]
    w8 = _clamp_ip_weight(ip_weight) if refs else None

    w, h = (size.split("x") + ["1024"])[:2]
    neg_text = negative or "low quality, blurry"

    if refs:
        # ---------- IP-Adapter 参考图工作流（A1 核心）----------
        # model 链：CheckpointLoaderSimple → IPAdapterUnifiedLoader → IPAdapter(链)
        # 每张参考图串一个 IPAdapter 节点，第 n 个的 model 输入接上一个的输出。
        workflow: dict = {
            "1": {"class_type": "CheckpointLoaderSimple",
                  "inputs": {"ckpt_name": p.model_id}},
            "2": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": prompt, "clip": ["1", 1]}},
            "3": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": neg_text, "clip": ["1", 1]}},
            "4": {"class_type": "EmptyLatentImage",
                  "inputs": {"width": int(w), "height": int(h), "batch_size": 1}},
            # IPAdapterUnifiedLoader：自动按模型挑合适的 IP-Adapter 权重
            "50": {"class_type": "IPAdapterUnifiedLoader",
                   "inputs": {"preset": "PLUS (high strength)", "model": ["1", 0]}},
        }
        prev_model: list = ["50", 0]          # 串联起点
        prev_ipa: list | None = None          # 上一张图（多图串联用）
        node = 51
        for idx, rp in enumerate(refs):
            ref_name = await _upload_comfy_image(host, headers, rp)
            load_id = str(node); node += 1
            ipa_id = str(node); node += 1
            workflow[load_id] = {"class_type": "LoadImage",
                                 "inputs": {"image": ref_name, "upload": "image"}}
            ipa_inputs = {
                "weight": float(w8 if w8 is not None else 0.8),
                "start_at": 0.0, "end_at": 1.0,
                "weight_type": "linear",
                "image": [load_id, 0],
                "ipadapter": ["50", 1],
                "model": prev_model if idx == 0 else prev_ipa,
            }
            workflow[ipa_id] = {"class_type": "IPAdapter", "inputs": ipa_inputs}
            prev_ipa = [ipa_id, 0]
        # 采样：model 接最后一张 IPAdapter 的输出
        workflow["5"] = {"class_type": "KSampler",
                         "inputs": {"seed": int(uuid.uuid4().hex[:8], 16),
                                    "steps": 20, "cfg": 7.0,
                                    "sampler_name": "euler", "scheduler": "normal",
                                    "denoise": 1.0, "model": prev_ipa,
                                    "positive": ["2", 0], "negative": ["3", 0],
                                    "latent_image": ["4", 0]}}
        workflow["6"] = {"class_type": "VAEDecode",
                         "inputs": {"samples": ["5", 0], "vae": ["1", 2]}}
        workflow["7"] = {"class_type": "SaveImage",
                         "inputs": {"images": ["6", 0],
                                    "filename_prefix": "novelforge_ipa"}}
    else:
        # ---------- 最简文生图（原行为，零回归）----------
        workflow = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": p.model_id}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt,
                  "clip": ["1", 1]}},
            "3": {"class_type": "CLIPTextEncode", "inputs": {"text": neg_text,
                  "clip": ["1", 1]}},
            "4": {"class_type": "EmptyLatentImage", "inputs": {"width": int(w),
                  "height": int(h), "batch_size": 1}},
            "5": {"class_type": "KSampler", "inputs": {"seed": int(uuid.uuid4().hex[:8], 16),
                  "steps": 20, "cfg": 7.0, "sampler_name": "euler", "scheduler": "normal",
                  "denoise": 1.0, "model": ["1", 0], "positive": ["2", 0],
                  "negative": ["3", 0], "latent_image": ["4", 0]}},
            "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0],
                  "vae": ["1", 2]}},
            "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0],
                  "filename_prefix": "novelforge"}},
        }

    data = await _post_json(f"{host}/prompt", {"prompt": workflow}, headers,
                            timeout=60.0)
    pid = data.get("prompt_id")
    if not pid:
        raise MediaError(f"ComfyUI 未返回 prompt_id：{json.dumps(data)[:200]}")
    # 轮询历史直到完成（最多 10 分钟）
    import asyncio
    async with httpx.AsyncClient(timeout=30.0) as cli:
        for _ in range(120):
            await asyncio.sleep(5)
            r = await cli.get(f"{host}/history/{pid}", headers=headers)
            if r.status_code >= 400:
                continue
            hist = r.json().get(pid) or {}
            if not hist:
                continue
            outs = (hist.get("outputs") or {})
            for node_out in outs.values():
                imgs = node_out.get("images") or []
                if imgs:
                    fn = imgs[0]
                    vr = await cli.get(
                        f"{host}/view?filename={fn.get('filename')}"
                        f"&subfolder={fn.get('subfolder', '')}"
                        f"&type={fn.get('type', 'output')}", headers=headers)
                    if vr.status_code < 400:
                        return vr.content
            if hist.get("status", {}).get("status_str") == "error":
                raise MediaError("ComfyUI 工作流执行失败")
    raise MediaError("ComfyUI 生成超时（10 分钟）")


# ---------------------------------------------------------------- TTS

async def gen_tts_openai(p: ModelProfile, text: str, voice: str = "alloy",
                         speed: float = 1.0, pitch: float = 0.0,
                         style: str = "") -> bytes:
    """OpenAI 兼容语音：POST {base}/audio/speech → mp3 二进制。

    v0.14.10 D19 音色资产化：新增 speed / pitch / style 三个可选参数。
    - speed 是 OpenAI 规范字段（1.0 = 原速），主流网关都认。
    - pitch / style 是**扩展字段**：OpenAI 官方 API 没有，但 Edge-TTS 网关
      （如各家用 /v1/audio/speech 包装 edge-tts 的服务）普遍支持。
      不认识它们的网关会直接忽略（多余字段按 JSON 语义无害），
      因此这里可以放心地无论如何都带上——牺牲一点兼容性换零配置成本。
    注意：过大的 pitch 偏移会破坏真人质感，调用方（voice_cast）已把范围
    钳制在 ±12 半音内。
    """
    base = (p.base_url or "").rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    url = f"{base}/audio/speech"
    headers = {"Content-Type": "application/json"}
    _key = keyvault.get_api_key(p)   # Key 库中是密文，请求时才解密
    if _key:
        headers["Authorization"] = f"Bearer {_key}"
    payload: dict = {"model": p.model_id, "input": text[:4000],
                     "voice": voice, "response_format": "mp3"}
    try:
        sp = float(speed)
    except (TypeError, ValueError):
        sp = 1.0
    if abs(sp - 1.0) > 1e-6:
        payload["speed"] = round(sp, 3)
    try:
        pt = float(pitch)
    except (TypeError, ValueError):
        pt = 0.0
    if abs(pt) > 1e-6:
        payload["pitch"] = round(pt, 3)
    if style:
        payload["style"] = style
    async with httpx.AsyncClient(timeout=300.0) as cli:
        r = await cli.post(url, headers=headers, json=payload)
    if r.status_code >= 400:
        raise MediaError(f"HTTP {r.status_code}: {r.text[:300]}")
    return r.content


async def gen_tts_multivoice(p: ModelProfile, lines: list[dict]) -> bytes:
    """D19：多角色逐行合成再拼接成单条音轨。

    参数 lines: [{"text": "...", "voice": "yunxi", "speed": 1.0,
                  "pitch": 0.0, "style": "", "speaker": "陈长安"}, ...]

    为什么要「逐行合成再拼」而不是「一次请求多角色」：
      OpenAI /audio/speech 一次只接受一个 voice，没有多说话人概念。
      跨镜头音色一致靠的是**同一角色每次都传同一 voice**，而不是请求合并。
      逐行合成的代价是 N 次 HTTP 往返，但短剧单镜台词量小（个位数），
      换来的是每个角色音色精确可控 —— 这正是 D18 结论要求的。

    拼接策略：
      * 优先 ffmpeg concat（若可用）——无损、快；
      * 不可用则纯 Python 拼接 mp3 帧（对 CBR mp3 有效）；
      * 都失败则只返回第一段（绝不让配音整体失败）。

    单行时直接返回该行音频，不做任何拼接（省一次 IO）。
    """
    from pathlib import Path as _P
    import shutil as _sh
    import subprocess as _sp
    import tempfile as _tf

    buf = []
    for ln in (lines or []):
        txt = (ln.get("text") or "").strip()
        if not txt:
            continue
        data = await gen_tts_openai(
            p, txt, voice=ln.get("voice") or "alloy",
            speed=ln.get("speed", 1.0), pitch=ln.get("pitch", 0.0),
            style=ln.get("style", ""))
        if data:
            buf.append(data)
    if not buf:
        return b""
    if len(buf) == 1:
        return buf[0]

    ffmpeg = _sh.which("ffmpeg")
    if ffmpeg:
        tmp = _P(_tf.mkdtemp(prefix="nf_tts_"))
        try:
            parts = []
            for i, d in enumerate(buf):
                fp = tmp / f"{i:03d}.mp3"
                fp.write_bytes(d)
                parts.append(fp)
            lst = tmp / "list.txt"
            lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts),
                           encoding="utf-8")
            out = tmp / "out.mp3"
            r = _sp.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                         "-f", "concat", "-safe", "0", "-i", str(lst),
                         "-c", "copy", str(out)],
                        capture_output=True, timeout=120)
            if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
                return out.read_bytes()
        except Exception:
            pass
        finally:
            _sh.rmtree(tmp, ignore_errors=True)

    # ffmpeg 不可用：直接字节拼接。对同为 CBR 的 mp3 段，多数播放器能连续播
    # （每段自带 ID3 头会有一丝杂音，但远好过整段丢失）。这是降级路径。
    return b"".join(buf)


# ---------------------------------------------------------------- ComfyUI workflow 通用适配器
# v0.13.11：checkpoint 填法只能跑内置的硬编码最小文生图骨架；真正的
# ArcReel 式能力（视频 / MiniMax H3 音频 / 自定义节点）必须走 workflow API——
# 用户在 ComfyUI 网页里搭好工作流，「导出（API 格式）」得到 JSON，把 JSON
# 填进模型中心的 model_id（或存成文件填路径），后端原样提交 /prompt，
# 只做占位符替换与产物回收。

# 占位符支持：{{prompt}} {{negative}} {{width}} {{height}} {{seed}} {{text}}
# （{{text}} 是音频/语音类节点用的文案槽，与图像 prompt 区分开）。
# v0.14.1 D3 新增：{{frames}}（视频帧数，LTX 帧数纪律换算）、
# {{image}}（参考图引用串 "filename[subfolder]"，经 /upload/image 上传后填入）。
# v0.14.1 D4 新增：{{image2}}…{{image9}} 多参考图槽（IP-Adapter Plus 多角色
# 一致性）；{{weight}} 为 IPAdapter 权重槽（不填保持 workflow 原值）。
_PLACEHOLDER_RE = re.compile(
    r"\{\{\s*(prompt|negative|width|height|width2|height2|seed|text|frames"
    r"|image\d*|weight)\s*\}\}")


def _extract_workflow_json(raw: str) -> dict:
    """从 model_id 字段内容提取 API 格式 workflow dict。

    兼容三种填法：
    1. 纯 API 格式 JSON：{"1": {"class_type": ..., "inputs": {...}}, ...}
    2. 带包装：{"workflow": {...}}（API 导出文件的完整结构）
    3. LiteGraph UI 导出格式：{"nodes": [...], "links": [...]} —— UI 格式
       的 nodes[i].widgets_values 常把 workflow 整包塞在最后一项里，尽力还原；
       还原不了就报错，提示用户改用「导出（API 格式）」。
    """
    try:
        data = json.loads(raw)
    except Exception as e:
        raise MediaError(f"workflow JSON 解析失败：{e}")
    if not isinstance(data, dict):
        raise MediaError("workflow 必须是 JSON 对象")
    if "workflow" in data and isinstance(data["workflow"], dict):
        data = data["workflow"]
    if "nodes" in data and isinstance(data.get("nodes"), list):
        # LiteGraph UI 格式 → 尝试转 API 格式（按 widgets_values 里的整包优先）
        wf = _ui_to_api(data)
        if wf:
            return wf
        raise MediaError(
            "检测到 UI 格式 workflow（含 nodes/links）。请在 ComfyUI 里用"
            "「导出（API 格式）/ Save (API Format)」重新导出后填入")
    if not all(isinstance(v, dict) and "class_type" in v for v in data.values()):
        raise MediaError("workflow 不是 API 格式（每个节点需含 class_type/inputs）")
    return data


def _ui_to_api(ui: dict) -> dict:
    """UI 导出文件里挖内嵌 API workflow。

    只认 widgets_values 里藏的整包 API JSON（ComfyUI UI 导出文件普遍带）。
    不做 links→inputs 的拓扑重建：widget 顺序没有稳定约定，重建出的工作流
    看似能跑、参数全错，比直接报错危险得多。
    """
    for n in ui.get("nodes") or []:
        for wv in (n.get("widgets_values") or []):
            if isinstance(wv, dict):
                cand = wv.get("workflow") if "workflow" in wv else (
                    wv if all(isinstance(v, dict) and "class_type" in v
                              for v in wv.values()) else None)
                if isinstance(cand, dict) and cand:
                    return cand
    return {}


def _apply_placeholders(workflow: dict, mapping: dict) -> dict:
    """深遍历 workflow，把 {{xxx}} 占位符替换为 mapping 里的值。

    只替换字符串值；数字槽位（width/height/seed）允许 mapping 里给数字。
    没出现在 workflow 里的占位符静默忽略（比如纯视频工作流没有 prompt 槽）。
    """
    def walk(v):
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, str):
            def rep(m):
                key = m.group(1)
                if key in mapping and mapping[key] is not None:
                    return str(mapping[key])
                return m.group(0)   # 未提供的占位符原样保留（ComfyUI 会报更清晰的错）
            return _PLACEHOLDER_RE.sub(rep, v)
        return v
    return walk(workflow)


async def _download_comfy_outputs_async(host: str, headers: dict, outputs: dict,
                                        prefer_audio: bool = False) -> bytes | None:
    """从 outputs 里挑产物并经 /view 下载（异步，唯一真实调用路径）。返回 bytes 或 None。

    探测顺序：音频类节点（SaveAudio 等 audio 字段）→ SaveImage images。
    prefer_audio 用于配音场景（工作流可能同时出图和出音频）。
    """
    audio_files, image_files, video_files = [], [], []
    for node_out in outputs.values():
        for key, val in (node_out or {}).items():
            if not isinstance(val, list):
                continue
            for item in val:
                if not isinstance(item, dict) or not item.get("filename"):
                    continue
                if key == "images":
                    image_files.append(item)
                elif "audio" in key or "gifi" in key:
                    audio_files.append(item)
                else:
                    # SaveVideo/PreviewVideo 等新 UI 输出键（videos/video 等）
                    # ——按扩展名归类，命中视频容器才算视频产物
                    ext = (item.get("filename") or "").rsplit(".", 1)[-1].lower()
                    if ext in ("mp4", "webm", "mkv", "mov", "gif"):
                        video_files.append(item)
    files = audio_files if (prefer_audio and audio_files) else (
        audio_files or video_files or image_files)
    async with httpx.AsyncClient(timeout=120.0) as cli:
        for fn in files:
            url = (f"{host}/view?filename={fn.get('filename')}"
                   f"&subfolder={fn.get('subfolder', '')}"
                   f"&type={fn.get('type', 'output')}")
            r = await cli.get(url, headers=headers)
            if r.status_code < 400 and r.content:
                return r.content
    return None


async def gen_media_comfyui_workflow(p: ModelProfile, workflow_raw: str,
                                     mapping: dict) -> bytes:
    """workflow API 通用生成：图像 / 视频 / 音频统一入口。

    workflow_raw：API 格式 JSON 字符串（可含 {"workflow": ...} 包装）。
    mapping：占位符取值，如 {"prompt": "...", "width": 1024}。
    返回二进制产物（图像 png / 音频由节点格式决定，经 outputs 探测）。
    """
    host = (p.base_url or "").rstrip("/")
    headers = {"Content-Type": "application/json"}
    _key = keyvault.get_api_key(p)
    if _key:
        headers["Authorization"] = f"Bearer {_key}"
    wf = _apply_placeholders(_extract_workflow_json(workflow_raw), mapping)
    data = await _post_json(f"{host}/prompt", {"prompt": wf}, headers, timeout=60.0)
    pid = data.get("prompt_id")
    if not pid:
        raise MediaError(f"ComfyUI 未返回 prompt_id：{json.dumps(data)[:200]}")
    import asyncio
    async with httpx.AsyncClient(timeout=30.0) as cli:
        for _ in range(240):          # 最多 20 分钟（视频生成偏慢）
            await asyncio.sleep(5)
            r = await cli.get(f"{host}/history/{pid}", headers=headers)
            if r.status_code >= 400:
                continue
            hist = r.json().get(pid) or {}
            if not hist:
                continue
            outs = (hist.get("outputs") or {})
            got = await _download_comfy_outputs_async(
                host, headers, outs,
                prefer_audio=bool(mapping.get("_prefer_audio")))
            if got:
                return got
            st = (hist.get("status") or {})
            if st.get("status_str") == "error":
                msgs = []
                for m in (st.get("messages") or []):
                    if m and m[0] == "execution_error":
                        msgs.append(json.dumps(m[1:], ensure_ascii=False)[:400])
                raise MediaError("ComfyUI 执行失败：" + ("; ".join(msgs) or "未知节点错误"))
    raise MediaError("ComfyUI 生成超时（20 分钟）")


# ---------------------------------------------------------------- 视频占位

# ---------------------------------------------------------------- 视频（D3 LTX-2.5 接真）

# LTX 视频模型帧数纪律：帧数必须 ≡1 (mod 8)。LTX-2.5 原生支持长镜头
# （25fps 下 10 秒 ≈ 249 帧），上限钳在 249 防止单镜失控拉爆显存。
LTX_FPS = 25
LTX_MAX_FRAMES = 249


def ltx_frames_for_duration(seconds: float | int | None, fps: int = LTX_FPS) -> int:
    """时长秒数 → 合规帧数（≡1 mod 8 就近取整，钳制 [9, LTX_MAX_FRAMES]）。

    duration 为空/非法 → 33 帧（约 1.3 秒，LTX 自动时长模式的稳妥默认）。
    """
    try:
        s = float(seconds) if seconds is not None else 0.0
    except (TypeError, ValueError):
        s = 0.0
    if s <= 0:
        return 33
    want = max(1, int(round(s * fps)))
    n = max(9, min(LTX_MAX_FRAMES, want))
    return ((n - 1 + 4) // 8) * 8 + 1   # 就近取 ≡1 (mod 8)


async def _upload_comfy_image(host: str, headers: dict, ref_path: str) -> str:
    """把参考图（/api/files/ 相对路径或绝对路径）上传到 ComfyUI /upload/image。

    返回 ComfyUI 引用串 "filename[subfolder]"（LoadImage image 槽格式）。
    上传失败抛 MediaError（i2v 无参考图就该失败，静默降级成 t2v 会产出
    与角色完全无关的视频，比报错更糟）。
    """
    from apps.api.config import BOOKS_DIR as _BOOKS_DIR
    from pathlib import Path as _P
    if ref_path.startswith("/api/files/"):
        local = _BOOKS_DIR / ref_path.split("/api/files/", 1)[-1]
    else:
        local = _P(ref_path)
    local = local.resolve()
    if not local.exists():
        raise MediaError(f"参考图不存在：{ref_path}")
    fn = local.name
    import asyncio
    # multipart 上传不能带 Content-Type: application/json（会破坏 boundary）
    auth = {k: v for k, v in (headers or {}).items() if k.lower() != "content-type"}
    async with httpx.AsyncClient(timeout=60.0) as cli:
        with open(local, "rb") as f:
            r = await cli.post(
                f"{host}/upload/image",
                files={"image": (fn, f, "application/octet-stream")},
                data={"overwrite": "true"}, headers=auth)
        if r.status_code >= 400:
            raise MediaError(f"参考图上传失败 HTTP {r.status_code}: {r.text[:200]}")
        info = r.json()
    name = info.get("name") or fn
    sub = info.get("subfolder") or ""
    return f"{name}[{sub}]" if sub else name


def _ltx_mapping(workflow: dict, prompt: str, *, seconds=None, width=None,
                 height=None, negative: str = "", ref_image: str = "") -> dict:
    """LTX/comfyui-workflow 视频的占位符 mapping 组装。

    frames：workflow 里出现 {{frames}} 时按帧数纪律换算（duration_seconds
    或缺省 33）；width/height：workflow 有槽才填（默认 720x1280 竖屏）；
    image：ref_image 非空时上传参考图得 "fn[sub]" 串（上传在 run_media 层做）。
    """
    # 词边界匹配：{{width2}} 不应误命中 width 检查（反之亦然）
    def has(k):
        if not workflow:
            return False
        pat = re.compile(r"\{\{\s*" + re.escape(k) + r"\s*\}\}")
        return any(
            pat.search(str(v))
            for n in workflow.values() for v in (n.get("inputs") or {}).values()
            if isinstance(v, str))
    mapping = {"prompt": prompt, "negative": negative,
               "seed": int(uuid.uuid4().hex[:8], 16)}
    if has("frames"):
        mapping["frames"] = ltx_frames_for_duration(seconds)
    if has("width"):
        mapping["width"] = int(width or 720)
    if has("height"):
        mapping["height"] = int(height or 1280)
    # LTX-2.5 双阶段工作流：{{width2}}/{{height2}} 是空 latent 的半分辨率槽
    # （首采在半分辨率做，LTXVLatentUpsampler x2 后精修到全分辨率）。
    # EmptyLTXVLatentVideo 内部按 //32 切 latent 格，半分辨率须 32 对齐
    # （720x1280 → 360x640 → 钳 352x640 → 上采样解码实际输出 704x1280）。
    if has("width2"):
        mapping["width2"] = max(64, round(int(width or 720) / 2 / 32) * 32)
    if has("height2"):
        mapping["height2"] = max(64, round(int(height or 1280) / 2 / 32) * 32)
    if has("image"):
        mapping["image"] = ref_image
    return mapping


def _workflow_has(workflow: dict, key: str) -> bool:
    """workflow 的字符串 inputs 里是否出现 {{key}} 占位符。"""
    for n in (workflow or {}).values():
        for v in (n.get("inputs") or {}).values():
            if isinstance(v, str) and "{{" in v and key in v:
                return True
    return False


def _clamp_ip_weight(v) -> float | None:
    """IP-Adapter 权重钳制到 [0, 2]（社区常用 0.6-1.0；>2 会过拟合出伪影）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(min(2.0, max(0.0, f)), 2)


def _comfy_headers(p: ModelProfile) -> dict:
    """ComfyUI 请求头：API Token 保护时带 Authorization Bearer（用户提供的
    $2b$12$... 哈希即 token 原文）；无密钥时不带（本地无保护模式）。"""
    headers = {"Content-Type": "application/json"}
    _key = keyvault.get_api_key(p)
    if _key:
        headers["Authorization"] = f"Bearer {_key}"
    return headers


async def gen_video_comfyui(p: ModelProfile, prompt: str, *, seconds=None,
                            width=None, height=None, negative: str = "",
                            ref_image: str = "", ref_images: list[str] | None = None,
                            ip_weight: float | None = None) -> bytes:
    """LTX-2.5 视频生成（comfyui provider，视频专用而非 workflow JSON 填法）。

    工作流假设（用户在 ComfyUI 网页搭好后存 API 格式 JSON 到模型 model_id
    或文件路径，与 comfyui-workflow 同一填法）：
      - 提示词槽 {{prompt}} / {{negative}}
      - 帧数槽 {{frames}}（LTXVBaseSampler 的 length，≡1 mod 8）
      - 宽高槽 {{width}}/{{height}}（i2v 时应与参考图比例一致）
      - 可选参考图槽 {{image}}（LoadImage.image，i2v 模式）
      - 可选多参考图槽 {{image2}}…{{image9}}（IP-Adapter 多角色，D4）
      - 可选权重槽 {{weight}}（IPAdapter weight，D4）
    """
    host = (p.base_url or "").rstrip("/")
    headers = _comfy_headers(p)
    raw = p.model_id or ""
    if raw.strip().startswith(("/", "~")) or "\\" in raw:
        from pathlib import Path as _P
        raw = _P(raw).expanduser().read_text(encoding="utf-8")
    wf = _extract_workflow_json(raw)
    mapping = _ltx_mapping(wf, prompt, seconds=seconds, width=width,
                           height=height, negative=negative)
    # i2v 单参考图：workflow 含 {{image}} 且给了 ref_image 才上传（缺图时
    # 静默跳过——LTX t2v 模式本来就不需要图）
    if ref_image and _workflow_has(wf, "image"):
        mapping["image"] = await _upload_comfy_image(host, headers, ref_image)
    # D4 多参考图（IP-Adapter Plus）：{{image2}}…{{image9}} 逐一上传
    for i, rp in enumerate((ref_images or [])[:9], 2):
        key = f"image{i}"
        if rp and _workflow_has(wf, key):
            mapping[key] = await _upload_comfy_image(host, headers, rp)
    w = _clamp_ip_weight(ip_weight)
    if w is not None and _workflow_has(wf, "weight"):
        mapping["weight"] = w
    data = await _post_json(f"{host}/prompt", {"prompt": _apply_placeholders(wf, mapping)},
                            headers, timeout=60.0)
    pid = data.get("prompt_id")
    if not pid:
        raise MediaError(f"ComfyUI 未返回 prompt_id：{json.dumps(data)[:200]}")
    import asyncio
    async with httpx.AsyncClient(timeout=30.0) as cli:
        for _ in range(240):          # 最多 20 分钟（视频生成偏慢）
            await asyncio.sleep(5)
            r = await cli.get(f"{host}/history/{pid}", headers=headers)
            if r.status_code >= 400:
                continue
            hist = r.json().get(pid) or {}
            if not hist:
                continue
            outs = (hist.get("outputs") or {})
            got = await _download_comfy_outputs_async(host, headers, outs)
            if got:
                return got
            st = (hist.get("status") or {})
            if st.get("status_str") == "error":
                msgs = []
                for m in (st.get("messages") or []):
                    if m and m[0] == "execution_error":
                        msgs.append(json.dumps(m[1:], ensure_ascii=False)[:400])
                raise MediaError("ComfyUI 执行失败：" + ("; ".join(msgs) or "未知节点错误"))
    raise MediaError("ComfyUI 视频生成超时（20 分钟）")


async def gen_video_placeholder(p: ModelProfile, prompt: str) -> str:
    """视频占位（provider 不明时的兜底）：视频 API 各家差异大，引导用户
    走 comfyui provider（LTX-2.5 workflow）或云端通道 cloud-video。"""
    keys = "、".join(sorted(_cloud_video.PROVIDERS))
    raise MediaError(
        "视频模型已配置但适配器未接入该厂商。当前视频支持两条通道："
        "① 本地 ComfyUI —— provider=comfyui/comfyui-workflow 并填 API 格式 "
        "workflow JSON；② 云端 API —— provider 填 " + keys +
        "（模型中心配 base_url + API Key 即可；Agnes flash 有免费额度，"
        "国际站 base_url=https://apihub.agnes-ai.com/v1）。请在模型中心调整。")


# ---------------------------------------------------------------- 路由入口

async def run_media(db, book_id: str, kind: str, prompt: str,
                    voice: str = "alloy", task_id: str = "",
                    seconds: float | None = None, width: int | None = None,
                    height: int | None = None, ref_image: str = "",
                    ref_images: list[str] | None = None,
                    ip_weight: float | None = None,
                    tts_lines: list[dict] | None = None,
                    shot_kind: str = "",
                    routes: dict | None = None) -> dict:
    """按模型 role 路由到适配器并落盘。kind: image|video|audio。

    返回 {"ok": True, "path": 相对路径} 或 {"ok": False, "error": ...}。
    v0.13.11：新增 provider=comfyui-workflow（workflow API JSON 填法），
    图像/视频/音频统一走同一适配器；视频仍无云端厂商实现，本地 ComfyUI
    视频工作流即从此入口生效。
    v0.13.15 C3：每次调用落一行 MediaCall 成本台账（成功/失败都记，
    失败行带 error_code 供归因；记账失败绝不阻断生成主流程）。
    v0.14.1 D3：视频接真——comfyui provider 走 LTX-2.5 专用适配器
    （帧数 ≡1 mod 8 纪律、{{frames}}/{{image}} 占位符、i2v 参考图上传）；
    秒数/宽高/参考图参数由调用方（drama 分镜）透传。
    v0.14.1 D4：多参考图 ref_images（{{image2}}…{{image9}}，IP-Adapter Plus
    多角色一致性）与 ip_weight（{{weight}}，0-2 钳制）贯通图像/视频两路。
    v0.14.10 D19：音频支持 tts_lines（逐行声纹）——给了就走多角色逐行合成再
    拼接（跨镜头音色一致的实现路径）；没给则沿用单 voice 老行为。
    v0.17 B1：视频支持 shot_kind/routes —— 走 VideoBackend 协议层按镜头类型
    选后端（战斗→FLF2V / 对话→原生音画 / 群演→LTX）。不传则保持原直连路径。
    """
    import time as _time
    from apps.api.models import MediaCall
    from apps.api.orchestrator.graph import pick_model
    role = {"image": "image", "video": "video", "audio": "tts"}.get(kind)
    if not role:
        return {"ok": False, "error": f"未知媒体类型 {kind}"}
    m = pick_model(db, role)
    if not m:
        return {"ok": False,
                "error": f"未配置 {role} 模型。请在模型中心添加 role={role} 的模型插槽"}
    prompt = _clean_prompt(prompt)
    t0 = _time.time()
    call_id = f"mc_{uuid.uuid4().hex[:12]}"

    def _ledger(status: str, err: str = "", err_code: str = "") -> None:
        """成本台账：ok/error 全记；记账自身失败静默（不影响生成）。"""
        try:
            db.add(MediaCall(id=call_id, book_id=book_id, task_id=task_id or "",
                             media_type=kind, provider=m.provider or "",
                             model_id=m.model_id or "", prompt_chars=len(prompt),
                             est_cost=0.0, actual_cost=0.0, status=status,
                             error_code=err_code[:64],
                             error_message=err[:512],
                             duration_ms=int((_time.time() - t0) * 1000)))
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

    try:
        if m.provider in ("comfyui-workflow", "comfyui") and kind == "video":
            # D3/D4：视频统一走 LTX 专用适配器（workflow 填法 + 帧数纪律 +
            # i2v/IP-Adapter 参考图）。provider=comfyui 与 comfyui-workflow
            # 的视频都按 workflow JSON 解释（comfyui 的 checkpoint 填法只对图像有意义）。
            # v0.17 B1：若调用方给了 shot_kind 且开启了后端路由，走 VideoBackend
            # 协议层（可按镜头类型换后端）；否则保持原直连路径（零回归）。
            data = None
            if shot_kind:
                try:
                    from apps.api import video_backend as _vb
                    req = _vb.VideoRequest(
                        prompt=prompt, seconds=seconds, width=width, height=height,
                        ref_image=ref_image, ref_images=list(ref_images or []),
                        ip_weight=ip_weight, shot_kind=shot_kind)
                    vr = await _vb.generate_video(m, req, routes=routes)
                    if vr.ok and vr.data:
                        data = vr.data
                    elif vr.error:
                        # 路由到的后端失败 → 记录但回落到直连路径，绝不因路由层故障断生成
                        import sys as _sys
                        print(f"[media] VideoBackend {vr.backend} 失败，回落直连：{vr.error}",
                              file=_sys.stderr, flush=True)
                except Exception as _e:
                    import sys as _sys
                    print(f"[media] VideoBackend 路由异常，回落直连：{type(_e).__name__}: {_e}",
                          file=_sys.stderr, flush=True)
            if data is None:
                data = await gen_video_comfyui(m, prompt, seconds=seconds,
                                               width=width, height=height,
                                               ref_image=ref_image,
                                               ref_images=ref_images,
                                               ip_weight=ip_weight)
            path = _save_media(book_id, "video", "mp4", data)
            _ledger("ok")
            return {"ok": True, "path": path, "model": m.name or m.model_id}
        # ★★ v1.0-E4.11（G6）：云端视频 API 通道（cloud_video.py 注册表）。
        #   在 ComfyUI 本地路径**之前**分派：provider=cloud-video 与本地工作流
        #   是互斥的两条通道，先判云端避免误入 ComfyUI 分支报workflow错。
        if m.provider in _cloud_video.PROVIDERS and kind == "video":
            from apps.api.models import Book as _Book
            # 计费归属：书的所有者（drama 链路无 user 上下文，以资源归属为准，
            # 与 can_access_book 的横向越权判定同一口径）。
            _owner = (db.get(_Book, book_id).owner_id
                      if db.get(_Book, book_id) else "") or ""
            data = await _cloud_video.generate(
                m, prompt, seconds=seconds, width=width, height=height,
                ref_image=ref_image)
            path = _save_media(book_id, "video", "mp4", data)
            _ledger("ok")
            return {"ok": True, "path": path, "model": m.name or m.model_id}
        if m.provider == "comfyui-workflow":
            # workflow 填法：model_id 存 API 格式 workflow JSON（或文件路径）。
            # 图像/视频用 {{prompt}}，音频用 {{text}}；音频场景优先收音频产物。
            #
            # ★★ v0.26.3：支持**内置模板键**（media.py 的"最后一公里"）。
            #   管理员现在只需在 model_id 填 `ltx25_i2v` / `minimax_h3_ref2v`
            #   这类模板名，不用自己去 ComfyUI 搭节点图导出 JSON。
            #   优先级：内置模板键 > 文件路径 > 内联 JSON（老行为不变）。
            wf = m.model_id or ""
            _wfd_tpl = None
            try:
                from apps.api import comfy_workflows as _cw
                if _cw.is_template_key(wf):
                    _wfd_tpl = _cw.get_workflow(wf)
            except Exception:                           # noqa: BLE001
                _wfd_tpl = None
            if _wfd_tpl is not None:
                wfd = _wfd_tpl
                wf = ""                                # 标记：已解析为 dict
            else:
                if wf.strip().startswith(("/", "~")) or "\\" in wf:
                    # 看起来像路径：从服务端磁盘读（工作流文件可放 /opt/novelforge/workflows/）
                    try:
                        from pathlib import Path as _P
                        wf = _P(wf).expanduser().read_text(encoding="utf-8")
                    except Exception as e:
                        return {"ok": False, "error": f"workflow 文件读取失败：{e}"}
                wfd = _extract_workflow_json(wf)
            mapping = {"prompt": prompt,
                       "text": prompt if kind == "audio" else prompt,
                       "negative": "", "seed": None, "width": None, "height": None}
            # ★ v0.26.3：{{seconds}} 占位符（H3 模板用）——字符串时长
            if seconds and _workflow_has(wfd, "seconds"):
                mapping["seconds"] = str(int(seconds))
            # ★ v0.26.3：{{audio}} 参考音频（H3 Ref2VA 音色锁定用）
            _aref = (locals().get("audio_ref") or "")
            if _aref and _workflow_has(wfd, "audio"):
                try:
                    mapping["audio"] = await _upload_comfy_image(host0, headers0, _aref)
                except Exception as _e:                 # noqa: BLE001
                    import sys as _s2
                    print(f"[media] 参考音频上传失败（忽略）：{_e}", file=_s2.stderr, flush=True)
            # D4 参考图链路：workflow 声明了 {{image}}/{{imageN}}/{{weight}}
            # 且调用方给了参考图/权重才上传填充；没给就保留原占位符由
            # ComfyUI 报更清晰的节点错（不静默降级成纯文生图）。
            host0 = (m.base_url or "").rstrip("/")
            headers0 = _comfy_headers(m)
            if ref_image and _workflow_has(wfd, "image"):
                mapping["image"] = await _upload_comfy_image(host0, headers0, ref_image)
            for i, rp in enumerate((ref_images or [])[:9], 2):
                key = f"image{i}"
                if rp and _workflow_has(wfd, key):
                    mapping[key] = await _upload_comfy_image(host0, headers0, rp)
            w0 = _clamp_ip_weight(ip_weight)
            if w0 is not None and _workflow_has(wfd, "weight"):
                mapping["weight"] = w0
            if kind == "image" and _workflow_has(wfd, "frames"):
                mapping["frames"] = None   # 图像工作流不该有帧数槽，防御性置空
            data = await gen_media_comfyui_workflow(
                m, wf, {**mapping, "_prefer_audio": kind == "audio"})
            if kind == "image":
                path = _save_media(book_id, "image", "png", data)
            elif kind == "audio":
                path = _save_media(book_id, "audio", "mp3", data)
            else:
                path = _save_media(book_id, "video", "mp4", data)
            _ledger("ok")
            return {"ok": True, "path": path, "model": m.name or m.model_id[:40]}
        if kind == "image":
            if m.provider == "comfyui":
                # ★ v0.16 A1：终于把参考图接进图像生成链（评估报告缺口 3）。
                # 修复前这里只传 (m, prompt)，分镜出图的角色长相纯靠 prompt 文字，
                # 跨集/跨镜必然漂移。现在把 ref_image/ref_images/ip_weight 透传，
                # 有参考图→走 IP-Adapter 工作流；无参考图→回落最简文生图（零回归）。
                data = await gen_image_comfyui(
                    m, prompt, ref_image=ref_image, ref_images=ref_images,
                    ip_weight=ip_weight)
            else:
                # OpenAI 兼容图像 API 不支持参考图（silent ignore 参数，保持兼容）
                data = await gen_image_openai(m, prompt)
            path = _save_media(book_id, "image", "png", data)
        elif kind == "audio":
            # D19：有逐行声纹作业 → 多角色逐行合成再拼接（跨镜头音色一致）；
            # 否则回落单 voice（老行为，兼容未做音色装配的调用方）。
            if tts_lines:
                data = await gen_tts_multivoice(m, tts_lines)
            else:
                data = await gen_tts_openai(m, prompt, voice=voice)
            path = _save_media(book_id, "audio", "mp3", data)
        else:
            # 视频：provider 不明，占位报错引导配置
            await gen_video_placeholder(m, prompt)
            path = ""
        _ledger("ok")
        return {"ok": True, "path": path, "model": m.name or m.model_id}
    except MediaError as e:
        _ledger("error", str(e), _err_code(str(e)))
        return {"ok": False, "error": str(e)}
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        _ledger("error", msg, _err_code(msg))
        return {"ok": False, "error": msg}


def _err_code(err: str) -> str:
    """错误归因分类（ArcReel CallFailure classify 的轻量版）。"""
    e = err.lower()
    if "429" in e or "rate" in e or "限流" in err:
        return "rate_limited"
    if "timeout" in e or "timed out" in e or "超时" in err:
        return "timeout"
    if "auth" in e or "401" in e or "403" in e or "api key" in e or "密钥" in err:
        return "auth"
    if "content" in e or "审查" in err or "敏感" in err or "policy" in e:
        return "content_policy"
    if "connection" in e or "connect" in e or "网络" in err:
        return "network"
    if "workflow" in e or "comfy" in e:
        return "comfyui_error"
    return "unknown"

"""内置 ComfyUI 视频工作流模板（v0.26.3）。

★ 解决的核心痛点（需求 4）：
   之前接 ComfyUI 视频，管理员必须自己去 ComfyUI 网页搭好节点图、
   导出 API 格式 JSON、再手工找占位符 —— 门槛极高。
   现在：**管理员只需选「模型」+ 填「IP:端口」+ 填「token」**，
   工作流由本模块提供，占位符已按规范埋好。

★ 占位符规范（与 media.py `_ltx_mapping` 对齐，不可随意改名）：
   {{prompt}}    正向提示词
   {{negative}}  负向提示词
   {{seed}}      随机种子（重拍时固定）
   {{frames}}    总帧数（LTX 纪律：≡1 mod 8）
   {{width}}/{{height}}     主分辨率
   {{width2}}/{{height2}}   半分辨率（LTX 双阶段空 latent）
   {{image}}     参考图（i2v 首帧 / IP-Adapter 参考）
   {{image2}}..{{image9}}   多参考图
   {{weight}}    IP-Adapter 权重（0-2）
   {{seconds}}   时长（秒，供自定义工作流换算）

★ 模型文件要求（LTX-2.5 官方）：
   diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors
   text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors
   vae/ltx-2.5-video-vae-bf16.safetensors
   vae/ltx-2.5-audio-vae-bf16.safetensors
"""

from __future__ import annotations

import json

# ============================================================ LTX-2.5 文生视频
# 结构：UNETLoader → CLIPLoader(gemma4) → CLIPTextEncode → EmptyLTXVLatentVideo
#       → LTXVConditioning → LTXVScheduler → KSamplerSelect → SamplerCustomAdvanced
#       → VAEDecode → SaveWEBM
# ★ 这是 ComfyUI 官方模板 video_ltx2_5_t2v 的**精简单阶段版**
#   （去掉 latent upsampler，降低显存门槛；需要两阶段的用户可自行导入完整 JSON）
LTX25_T2V: dict = {
    "1": {
        "class_type": "UNETLoader",
        "inputs": {
            "unet_name": "ltx-2.5-22b-distilled-transformer-bf16.safetensors",
            "weight_dtype": "default",
        },
    },
    "2": {
        "class_type": "CLIPLoader",
        "inputs": {
            "clip_name": "gemma4-12b-with-proj-ltx-2.5-bf16.safetensors",
            "type": "ltxv",
        },
    },
    "3": {
        "class_type": "VAELoader",
        "inputs": {"vae_name": "ltx-2.5-video-vae-bf16.safetensors"},
    },
    "4": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "{{prompt}}", "clip": ["2", 0]},
    },
    "5": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "{{negative}}", "clip": ["2", 0]},
    },
    "6": {
        "class_type": "EmptyLTXVLatentVideo",
        "inputs": {
            "width": "{{width}}",
            "height": "{{height}}",
            "length": "{{frames}}",
            "batch_size": 1,
        },
    },
    "7": {
        "class_type": "LTXVConditioning",
        "inputs": {"positive": ["4", 0], "negative": ["5", 0], "frame_rate": 24.0},
    },
    "8": {
        "class_type": "LTXVScheduler",
        "inputs": {
            "steps": 8,
            "max_shift": 2.05,
            "base_shift": 0.95,
            "stretch": True,
            "terminal": 0.1,
            "latent": ["6", 0],
        },
    },
    "9": {
        "class_type": "KSamplerSelect",
        "inputs": {"sampler_name": "euler"},
    },
    "10": {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": ["11", 0],
            "guider": ["12", 0],
            "sampler": ["9", 0],
            "sigmas": ["8", 0],
            "latent_image": ["6", 0],
        },
    },
    "11": {
        "class_type": "RandomNoise",
        "inputs": {"noise_seed": "{{seed}}"},
    },
    "12": {
        "class_type": "CFGGuider",
        "inputs": {"model": ["1", 0], "positive": ["7", 0],
                   "negative": ["7", 1], "cfg": 1.0},
    },
    "13": {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["10", 0], "vae": ["3", 0]},
    },
    "14": {
        "class_type": "SaveWEBM",
        "inputs": {
            "images": ["13", 0],
            "filename_prefix": "sozeer_ltx25_t2v",
            "codec": "vp9",
            "fps": 24.0,
            "crf": 20.0,
        },
    },
}

# ============================================================ LTX-2.5 图生视频
# 相对 T2V 只多一个 LoadImage + LTXVImgToVideo（首帧条件）
LTX25_I2V: dict = {
    **{k: v for k, v in LTX25_T2V.items() if k in ("1", "2", "3", "9")},
    "4": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "{{prompt}}", "clip": ["2", 0]},
    },
    "5": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "{{negative}}", "clip": ["2", 0]},
    },
    "6": {
        "class_type": "LoadImage",
        "inputs": {"image": "{{image}}", "upload": "image"},
    },
    "7": {
        "class_type": "LTXVImgToVideo",
        "inputs": {
            "positive": ["4", 0],
            "negative": ["5", 0],
            "vae": ["3", 0],
            "image": ["6", 0],
            "width": "{{width}}",
            "height": "{{height}}",
            "length": "{{frames}}",
            "batch_size": 1,
            "strength": 1.0,
        },
    },
    "8": {
        "class_type": "LTXVScheduler",
        "inputs": {
            "steps": 8, "max_shift": 2.05, "base_shift": 0.95,
            "stretch": True, "terminal": 0.1, "latent": ["7", 2],
        },
    },
    "10": {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": ["11", 0], "guider": ["12", 0], "sampler": ["9", 0],
            "sigmas": ["8", 0], "latent_image": ["7", 2],
        },
    },
    "11": {"class_type": "RandomNoise", "inputs": {"noise_seed": "{{seed}}"}},
    "12": {
        "class_type": "CFGGuider",
        "inputs": {"model": ["1", 0], "positive": ["7", 0],
                   "negative": ["7", 1], "cfg": 1.0},
    },
    "13": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["3", 0]}},
    "14": {
        "class_type": "SaveWEBM",
        "inputs": {"images": ["13", 0], "filename_prefix": "sozeer_ltx25_i2v",
                   "codec": "vp9", "fps": 24.0, "crf": 20.0},
    },
}

# ============================================================ MiniMax H3（ComfyUI 节点）
# ★ H3 走 ComfyUI 时需要 ComfyUI-MiniMax 节点包（社区实现）。
#   注意：H3 的强项是 Ref2VA（多参考图 + 参考音频），所以这里给了
#   images/audios 两组槽位 —— 音色锁定全靠 audios。
MINIMAX_H3_T2V: dict = {
    "1": {
        "class_type": "MiniMaxH3Loader",
        "inputs": {"model_name": "minimax-h3"},
    },
    "2": {
        "class_type": "MiniMaxH3TextToVideo",
        "inputs": {
            "model": ["1", 0],
            "prompt": "{{prompt}}",
            "negative_prompt": "{{negative}}",
            "seed": "{{seed}}",
            "duration": "{{seconds}}",
            "resolution": "1080p",
            "aspect_ratio": "9:16",
            "generate_audio": True,
        },
    },
    "3": {
        "class_type": "SaveWEBM",
        "inputs": {"images": ["2", 0], "filename_prefix": "sozeer_h3_t2v",
                   "codec": "vp9", "fps": 24.0, "crf": 18.0},
    },
}

MINIMAX_H3_REF2V: dict = {
    "1": {
        "class_type": "MiniMaxH3Loader",
        "inputs": {"model_name": "minimax-h3"},
    },
    "2": {
        # ★ Ref2VA：参考图（角色身份/服装/场景）+ 参考音频（音色锁定）
        "class_type": "MiniMaxH3ReferenceToVideo",
        "inputs": {
            "model": ["1", 0],
            "prompt": "{{prompt}}",
            "images": ["{{image}}"],
            "image2": "{{image2}}",
            "image3": "{{image3}}",
            "audio_ref": "{{audio}}",
            "seed": "{{seed}}",
            "duration": "{{seconds}}",
            "resolution": "1080p",
            "aspect_ratio": "9:16",
            "generate_audio": True,
        },
    },
    "3": {
        "class_type": "SaveWEBM",
        "inputs": {"images": ["2", 0], "filename_prefix": "sozeer_h3_ref2v",
                   "codec": "vp9", "fps": 24.0, "crf": 18.0},
    },
}

# ============================================================ 模板注册表

TEMPLATES: dict[str, dict] = {
    "ltx25_t2v": {
        "name": "LTX-2.5 · 文生视频（单阶段）",
        "desc": "从提示词直接生成视频。原生多镜头，可写整段叙事弧线。"
                "★ 提示词建议英文（文本编码器 Gemma 4 主要针对英文优化）。",
        "kind": "video",
        "workflow": LTX25_T2V,
        "needs": ["prompt"],
        "optional": ["negative", "seed", "frames", "width", "height", "seconds"],
        "models": ["diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors",
                   "text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors",
                   "vae/ltx-2.5-video-vae-bf16.safetensors"],
    },
    "ltx25_i2v": {
        "name": "LTX-2.5 · 图生视频（单阶段）",
        "desc": "从首帧图片生成视频 —— **跨镜一致性最稳的方式**（身份由首帧锁定）。"
                "推荐：先用图片模型出分镜图，再用它做首帧。",
        "kind": "video",
        "workflow": LTX25_I2V,
        "needs": ["prompt", "image"],
        "optional": ["negative", "seed", "frames", "width", "height"],
        "models": ["diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors",
                   "text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors",
                   "vae/ltx-2.5-video-vae-bf16.safetensors"],
    },
    "minimax_h3_t2v": {
        "name": "MiniMax H3 · 文生视频（含音频）",
        "desc": "H3 原生生成 32kHz 立体声 + 口型对齐。★ 支持中文提示词，"
                "对白台词可直接用中文写。",
        "kind": "video",
        "workflow": MINIMAX_H3_T2V,
        "needs": ["prompt"],
        "optional": ["negative", "seed", "seconds"],
        "requires_node": "ComfyUI-MiniMax",
        "models": [],
    },
    "minimax_h3_ref2v": {
        "name": "MiniMax H3 · 参考图/参考音频生视频（角色一致性推荐）",
        "desc": "最强一致性模式：最多 9 张参考图（角色+服装+场景分槽）+ 3 段"
                "参考音频（音色锁定）。★ 做对白镜头、跨集音色一致时首选。",
        "kind": "video",
        "workflow": MINIMAX_H3_REF2V,
        "needs": ["prompt", "image"],
        "optional": ["image2", "image3", "audio", "seed", "seconds"],
        "requires_node": "ComfyUI-MiniMax",
        "models": [],
    },
}


def list_templates(kind: str = "") -> list[dict]:
    """列出内置模板（供后台下拉选择）。不含完整 workflow（体积大）。"""
    out = []
    for key, t in TEMPLATES.items():
        if kind and t.get("kind") != kind:
            continue
        out.append({
            "key": key, "name": t["name"], "desc": t["desc"],
            "kind": t["kind"], "needs": t["needs"], "optional": t["optional"],
            "requires_node": t.get("requires_node", ""),
            "models": t.get("models", []),
        })
    return out


def get_workflow(key: str) -> dict | None:
    """取模板的完整 workflow（深拷贝，避免调用方改动污染模板）。"""
    t = TEMPLATES.get((key or "").strip())
    if not t:
        return None
    return json.loads(json.dumps(t["workflow"]))


def is_template_key(key: str) -> bool:
    return (key or "").strip() in TEMPLATES

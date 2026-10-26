"""D14-r 音频策略路由：按 utterance.kind 把声音分流到不同生成通道。

## 背景（D18 第二轮的实测硬结论）

| 通道 | 能力 | 实测数据 |
|---|---|---|
| LTX-2.5 i2v 原生台词 | 逐字复现 + 口型同步 | 8/8 逐字；音量 mean -15.9dB |
| LTX-2.5 t2v 原生台词 | 不可靠 | 音量塌陷 -33~-40dB、内容随机 |
| LTX 环境音 | 自动生成 | t2v 基线自带雪夜风声，白送 |
| **跨镜头音色一致性** | **不成立** | **F0 中位 250Hz → 163Hz，相对差 34.7%** |

关键推论：i2v 原生台词只在**单镜头内**可靠；跨镜头角色对白若直接用 i2v，
每个镜头都会重新抽一次「音色盲盒」，短剧不可用。
→ **对白必须走 TTS 音色克隆（D19 音色资产化）**，i2v 原生台词降级为
「单镜头快速出片」的可选加速档。

## 路由表（本模块的唯一事实来源）

| utterance.kind | 声音通道 | 理由 |
|---|---|---|
| `dialogue`（画面内对白） | `tts`（音色克隆，按角色声纹） | 跨镜头一致性刚需；口型靠 lipsync 后处理 |
| `voiceover`（旁白/画外音） | `tts`（叙述者音色） | 不需要口型；t2v 台词不可靠 |
| 环境音 / 无台词 | `native`（LTX 自动） | 白送，且 i2v 会自然带出 |
| `dialogue` + `mode=fast` | `native`（LTX i2v 原生台词） | 单镜头快速出片，牺牲跨镜头一致性换省两步 |

## 与既有代码的关系

- `drama.generate_scene_media(target="audio")` 此前只是把旁白文本拼起来丢 TTS，
  没有 kind 分流。本模块提供 `plan_scene_audio()` 给出**路由决策**，
  `split_utterances()` 把 utterances 拆成 tts 段与 native 段。
- 视频侧：`build_video_prompt()` 需要知道哪些台词由 LTX 原生说（进 prompt）、
  哪些留给 TTS（不进 prompt，避免模型自作主张配音）。见 `native_dialogue_lines()`。
"""
from __future__ import annotations

# ---- 路由枚举（字符串常量，便于 JSON 序列化与前端展示） ----
CH_TTS = "tts"          # 独立 TTS 合成（音色可控）
CH_NATIVE = "native"    # 交给视频模型原生生成（LTX-2.5）
CH_SKIP = "skip"        # 无需生成（纯环境音场景由视频自带）

# 生成模式：quality（默认，跨镜头一致优先）/ fast（单镜头快速出片）
MODE_QUALITY = "quality"
MODE_FAST = "fast"


def classify_utterance(u: dict) -> str:
    """把一条 utterance 归类为路由通道。

    - dialogue → tts（quality 模式）/ native（fast 模式由 plan 决定）
    - voiceover → tts（旁白不需要口型，且 t2v 台词不可靠）
    - 其他/空 → skip
    """
    kind = (u.get("kind") or "").strip().lower()
    text = (u.get("text") or "").strip()
    if not text:
        return CH_SKIP
    if kind == "dialogue":
        return CH_TTS
    if kind == "voiceover":
        return CH_TTS
    # 未标 kind 但确有台词文本：按对白兜底（宁可 TTS 可控，不可赌模型）
    if text:
        return CH_TTS
    return CH_SKIP


def split_utterances(utterances: list[dict], mode: str = MODE_QUALITY) -> dict:
    """把一场戏的 utterances 拆成三桶。

    返回：{
      "tts":    [utt, ...],     # 走独立 TTS 的台词
      "native": [utt, ...],     # 交给视频模型原生说（仅 fast 模式的 dialogue）
      "skip":   [utt, ...],     # 不生成（空文本等）
    }

    quality 模式下 native 恒为空——所有台词都走 TTS，保证跨镜头音色一致
    （D18 第二轮 F0 漂移 34.7% 的直接对策）。
    """
    out = {"tts": [], "native": [], "skip": []}
    for u in (utterances or []):
        kind = (u.get("kind") or "").strip().lower()
        text = (u.get("text") or "").strip()
        if not text:
            out["skip"].append(u)
            continue
        # fast 模式：画面内对白交给 LTX i2v 原生（省 TTS + 口型对齐两步），
        # 但旁白仍走 TTS（t2v 台词不可靠，实测 -33~-40dB 塌陷）。
        if mode == MODE_FAST and kind == "dialogue":
            out["native"].append(u)
        else:
            out["tts"].append(u)
    return out


def native_dialogue_lines(utterances: list[dict], mode: str = MODE_QUALITY) -> list[str]:
    """返回应当写进视频 prompt、由 LTX 原生念出的台词（仅 fast 模式的 dialogue）。

    quality 模式下返回空列表——视频 prompt 不应含台词，避免模型随机配音
    与 TTS 音轨叠成双声。
    """
    return [u.get("text", "").strip()
            for u in split_utterances(utterances, mode)["native"]
            if u.get("text", "").strip()]


def tts_lines(utterances: list[dict], mode: str = MODE_QUALITY) -> list[dict]:
    """返回应走 TTS 的台词（保留 speaker 供音色选择）。"""
    return split_utterances(utterances, mode)["tts"]


def plan_scene_audio(utterances: list[dict], mode: str = MODE_QUALITY) -> dict:
    """给出一场戏的完整音频路由计划（供生成流程与前端展示）。

    返回：{
      "mode": "quality",
      "channels": [
        {"utt": {...}, "channel": "tts"|"native"|"skip", "speaker": "角色名"},
        ...
      ],
      "has_tts": bool, "has_native": bool,
      "tts_text": "合并后的 TTS 文本（按行）",
      "native_text": "合并后的原生台词文本",
      "notes": ["..."]   # 人话解释，前端可直接展示
    }
    """
    buckets = split_utterances(utterances, mode)
    channels = []
    for u in (utterances or []):
        channels.append({
            "utt": u,
            "channel": classify_utterance(u) if mode != MODE_FAST
            else ("native" if (u.get("kind") or "").lower() == "dialogue"
                  and (u.get("text") or "").strip() else classify_utterance(u)),
            "speaker": u.get("speaker"),
        })
    tts_txt = "\n".join(u.get("text", "").strip() for u in buckets["tts"])
    nat_txt = "\n".join(u.get("text", "").strip() for u in buckets["native"])
    notes = []
    if buckets["tts"]:
        notes.append(f"{len(buckets['tts'])} 条台词走 TTS 音色克隆（跨镜头一致）")
    if buckets["native"]:
        notes.append(f"{len(buckets['native'])} 条对白交给 LTX i2v 原生（快速档，"
                     f"跨镜头音色可能漂移）")
    if not buckets["tts"] and not buckets["native"]:
        notes.append("本镜无台词，环境音由视频模型自动生成")
    return {
        "mode": mode,
        "channels": channels,
        "has_tts": bool(buckets["tts"]),
        "has_native": bool(buckets["native"]),
        "tts_text": tts_txt,
        "native_text": nat_txt,
        "notes": notes,
    }


def audio_plan_for_project(project_mode: str = "") -> str:
    """项目级默认档位校验：未知值一律回落 quality。"""
    m = (project_mode or "").strip().lower()
    return MODE_FAST if m == MODE_FAST else MODE_QUALITY

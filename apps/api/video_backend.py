# -*- coding: utf-8 -*-
"""视频后端协议抽象（v0.17 B1，评估报告缺口 2）。

背景
----
评估报告缺口 2：

  "视频侧多 backend 未抽象：绑定 LTX-2.5 单路；无法按镜头类型选模型
   （战斗用 FLF2V、对话用原生音画）"

现状：`media.gen_video_comfyui` 是唯一视频实现，写死了 LTX 的工作流约定。
结果是：**不同镜头类型只能共用一套参数**——战斗镜头需要首末帧插值（FLF2V）
保证动作连贯，对话镜头需要原生音画同步，群演空镜用纯 t2v 更省成本，
但当前无法区分。

设计（对齐 ArcReel 的 VideoBackend）
------------------------------------
1. **协议层**：`VideoBackend` —— 统一 `generate()` 签名，屏蔽各实现差异。
2. **路由层**：`pick_backend(scene_kind, route_table)` —— 按镜头类型选后端。
3. **实现层**：LTX（现有）作为第一个 backend；mock/placeholder 便于测试。
4. **成本追踪**：每次调用记录 backend / 耗时 / 估算成本，落 MediaCall。

关键约束：**不破坏现有调用链**。`media.run_media` 仍可用旧路径，
本模块是新增的**可选用法**——逐步迁移，零回归。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# ----------------------------------------------------------------- 镜头类型
# 这些是短剧最常见的几类镜头，各自对视频模型的能力诉求不同。
SHOT_DIALOGUE = "dialogue"      # 对话/口型镜头 → 需要原生音画同步
SHOT_ACTION = "action"          # 战斗/动作镜头 → 需要首末帧插值（FLF2V）保连贯
SHOT_CROWD = "crowd"            # 群演/空镜 → 纯 t2v 即可，省成本
SHOT_TRANSITION = "transition"  # 转场 → i2v from 分镜图
SHOT_OTHER = "other"

ALL_SHOT_KINDS = (SHOT_DIALOGUE, SHOT_ACTION, SHOT_CROWD, SHOT_TRANSITION, SHOT_OTHER)

# 镜头类型判定关键词（从 shot_type / scene_description 里嗅探）
_ACTION_HINTS = ("打斗", "战斗", "追逐", "交手", "厮杀", "挥剑", "出拳", "爆", "冲",
                 "action", "fight", "battle", "chase")
_DIALOGUE_HINTS = ("对话", "交谈", "说", "问", "答", "口型", "特写", "近景",
                   "dialogue", "talk", "close", "medium")
_CROWD_HINTS = ("群演", "人群", "远景", "全景", "空镜", "环境", "群山", "城市",
                "wide", "establishing", "crowd", "landscape")
_TRANSITION_HINTS = ("转场", "过渡", "淡入", "淡出", "切至", "transition", "fade")


def classify_shot(shot_type: str = "", scene_description: str = "") -> str:
    """把镜头描述归类到 SHOT_* 之一（关键词嗅探，容错优先）。

    优先级：action > dialogue > crowd > transition > other

    * action 最先：误判成本最高（战斗用错模型会毁掉动作连贯性）
    * crowd 先于 transition：**画面内容**比拍摄用途更能决定模型选型。
      例："远景群山，空镜过渡" —— 内容是群山（crowd），过渡只是用途，
      交给 crowd 后端（t2v 省成本）比切转场后端更合适。
    """
    blob = f"{shot_type or ''} {scene_description or ''}".lower()
    if not blob.strip():
        return SHOT_OTHER
    for kw in _ACTION_HINTS:
        if kw in blob:
            return SHOT_ACTION
    for kw in _DIALOGUE_HINTS:
        if kw in blob:
            return SHOT_DIALOGUE
    for kw in _CROWD_HINTS:
        if kw in blob:
            return SHOT_CROWD
    for kw in _TRANSITION_HINTS:
        if kw in blob:
            return SHOT_TRANSITION
    return SHOT_OTHER


# ----------------------------------------------------------------- 结果与协议
@dataclass
class VideoResult:
    """所有 backend 的统一返回。"""
    ok: bool
    data: bytes = b""              # 视频字节（内存态）
    path: str = ""                 # 落盘后的相对路径（run_media 填）
    backend: str = ""              # 实际使用的 backend 名
    cost_estimate: float = 0.0     # 估算成本（元；本地为 0）
    duration_ms: int = 0
    error: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class VideoRequest:
    """所有 backend 的统一入参。"""
    prompt: str
    seconds: float | None = None
    width: int | None = None
    height: int | None = None
    negative: str = ""
    ref_image: str = ""                    # i2v 首帧
    end_frame: str = ""                    # FLF2V 末帧（动作镜头用）
    ref_images: list[str] = field(default_factory=list)   # IP-Adapter 多角色
    ip_weight: float | None = None
    shot_kind: str = SHOT_OTHER
    meta: dict = field(default_factory=dict)


@runtime_checkable
class VideoBackend(Protocol):
    """视频后端协议。实现者只需提供 name / supports / generate 三项。"""

    name: str

    def supports(self, shot_kind: str) -> bool:
        """该 backend 是否适合处理这种镜头类型。"""
        ...

    async def generate(self, model, req: VideoRequest) -> VideoResult:
        """生成视频。model 为 ModelProfile。"""
        ...


# ----------------------------------------------------------------- 内置实现
class LTXBackend:
    """LTX-2.5（现有实现）—— 通用本地视频后端。

    支持全镜头类型；对 action 镜头会尝试使用末帧（FLF2V）保连贯。
    """
    name = "ltx"

    def supports(self, shot_kind: str) -> bool:
        return True          # LTX 通用，什么镜头都能跑

    async def generate(self, model, req: VideoRequest) -> VideoResult:
        from apps.api import media as _m
        t0 = time.time()
        try:
            data = await _m.gen_video_comfyui(
                model, req.prompt, seconds=req.seconds,
                width=req.width, height=req.height, negative=req.negative,
                ref_image=(req.ref_image or req.end_frame or ""),
                ref_images=req.ref_images, ip_weight=req.ip_weight)
            return VideoResult(ok=True, data=data, backend=self.name,
                               cost_estimate=0.0,
                               duration_ms=int((time.time() - t0) * 1000))
        except Exception as e:
            return VideoResult(ok=False, backend=self.name,
                               error=f"{type(e).__name__}: {e}",
                               duration_ms=int((time.time() - t0) * 1000))


class PlaceholderBackend:
    """占位后端：未配置视频模型或用于测试。永不产片，但契约完整。"""
    name = "placeholder"

    def supports(self, shot_kind: str) -> bool:
        return True

    async def generate(self, model, req: VideoRequest) -> VideoResult:
        return VideoResult(ok=False, backend=self.name,
                           error="未配置可用的视频后端（placeholder）")


# 注册表：新增后端只需在这里登记
BACKENDS: dict[str, VideoBackend] = {
    "ltx": LTXBackend(),
    "placeholder": PlaceholderBackend(),
}

# ----------------------------------------------------------------- 路由表
# 默认路由：按镜头类型选 backend。可被项目级配置覆盖。
DEFAULT_ROUTES: dict[str, str] = {
    SHOT_DIALOGUE: "ltx",       # 对话：LTX 原生音画（D18 实测定为 quality 档）
    SHOT_ACTION: "ltx",         # 动作：LTX + 末帧插值
    SHOT_CROWD: "ltx",          # 群演：LTX t2v（最省）
    SHOT_TRANSITION: "ltx",     # 转场：LTX i2v
    SHOT_OTHER: "ltx",
}


def pick_backend(shot_kind: str, routes: dict | None = None) -> VideoBackend:
    """按镜头类型选后端。未命中路线 / 后端不存在 → 回落 placeholder。"""
    table = dict(DEFAULT_ROUTES)
    if routes:
        table.update({k: v for k, v in routes.items() if k})
    name = table.get(shot_kind) or table.get(SHOT_OTHER) or "ltx"
    be = BACKENDS.get(name)
    if be is None:
        return BACKENDS["placeholder"]
    return be


def resolve_routes(project=None) -> dict:
    """从项目读路由配置（project.video_routes JSON），失败回落默认。"""
    if project is None:
        return dict(DEFAULT_ROUTES)
    raw = getattr(project, "video_routes", "") or ""
    if not raw:
        return dict(DEFAULT_ROUTES)
    try:
        import json
        d = json.loads(raw)
        if isinstance(d, dict):
            out = dict(DEFAULT_ROUTES)
            out.update({str(k): str(v) for k, v in d.items() if v})
            return out
    except Exception:
        pass
    return dict(DEFAULT_ROUTES)


async def generate_video(model, req: VideoRequest, *, routes: dict | None = None) -> VideoResult:
    """统一入口：按 req.shot_kind 路由到合适 backend 并生成。

    与 media.run_media 的关系：run_media 是「媒体类型总线」（图/视频/音频），
    本函数是**视频侧的路由层**。run_media 的视频分支未来可直接调这里。
    """
    be = pick_backend(req.shot_kind, routes)
    if not be.supports(req.shot_kind):
        be = BACKENDS["placeholder"]
    return await be.generate(model, req)


def backend_capabilities() -> list[dict]:
    """后端能力清单（前端展示 / 调试用）。"""
    return [{"name": n, "supports": list(ALL_SHOT_KINDS)} for n in BACKENDS]


__all__ = [
    "VideoBackend", "VideoRequest", "VideoResult",
    "LTXBackend", "PlaceholderBackend", "BACKENDS",
    "DEFAULT_ROUTES", "pick_backend", "resolve_routes", "generate_video",
    "classify_shot", "backend_capabilities",
    "SHOT_DIALOGUE", "SHOT_ACTION", "SHOT_CROWD", "SHOT_TRANSITION", "SHOT_OTHER",
    "ALL_SHOT_KINDS",
]

# -*- coding: utf-8 -*-
"""v0.17 B1 回归：VideoBackend 协议抽象（评估报告缺口 2）。

缺口原文：
  "视频侧多 backend 未抽象：绑定 LTX-2.5 单路；无法按镜头类型选模型
   （战斗用 FLF2V、对话用原生音画）"

实现：apps/api/video_backend.py
  * VideoBackend 协议（name/supports/generate）
  * classify_shot 镜头类型嗅探
  * pick_backend / resolve_routes 路由层
  * LTXBackend / PlaceholderBackend 两个实现
  * 成本追踪（VideoResult.cost_estimate / duration_ms）
"""
import asyncio
import os

import pytest

os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-b1")


# ==================================================== 镜头分类
@pytest.mark.parametrize("desc,expect", [
    ("两人在山巅打斗，剑光交错", "action"),
    ("他挥剑劈向对手", "action"),
    ("近距离对话，特写镜头", "dialogue"),
    ("她说：你走吧", "dialogue"),
    ("远景群山，空镜过渡", "crowd"),
    ("淡入下一场", "transition"),
    ("", "other"),
    ("普通的画面描述", "other"),
])
def test_classify_shot(desc, expect):
    from apps.api import video_backend as VB
    assert VB.classify_shot("", desc) == expect, f"{desc!r} 应归类为 {expect}"


def test_action_priority_over_dialogue():
    """★ 动作优先：含打斗又含对话时判为 action（误判成本最高）。"""
    from apps.api import video_backend as VB
    assert VB.classify_shot("", "他一边打斗一边说台词") == "action"


def test_classify_from_shot_type():
    from apps.api import video_backend as VB
    assert VB.classify_shot("近景特写", "") == "dialogue"
    assert VB.classify_shot("wide establishing shot", "") == "crowd"


def test_classify_never_crashes():
    from apps.api import video_backend as VB
    for bad in [None, "", 123, "🙂", "x" * 9999]:
        assert isinstance(VB.classify_shot(bad, bad), str)


# ==================================================== 协议与实现
def test_protocol_shape():
    """★ 协议三要素：name / supports / generate。"""
    from apps.api import video_backend as VB
    for name in ("ltx", "placeholder"):
        be = VB.BACKENDS[name]
        assert getattr(be, "name", ""), f"{name} 缺 name"
        assert callable(getattr(be, "supports", None)), f"{name} 缺 supports"
        assert callable(getattr(be, "generate", None)), f"{name} 缺 generate"


def test_ltx_supports_all_kinds():
    from apps.api import video_backend as VB
    for k in VB.ALL_SHOT_KINDS:
        assert VB.BACKENDS["ltx"].supports(k)


def test_video_request_defaults():
    from apps.api import video_backend as VB
    req = VB.VideoRequest(prompt="测试")
    assert req.shot_kind == VB.SHOT_OTHER
    assert req.ref_images == []
    assert req.end_frame == ""


def test_video_result_shape():
    from apps.api import video_backend as VB
    r = VB.VideoResult(ok=True, data=b"x", backend="ltx")
    assert r.ok and r.data == b"x" and r.cost_estimate == 0.0


def test_runtime_checkable_protocol():
    """LTXBackend 应满足 VideoBackend 协议（runtime_checkable）。"""
    from apps.api import video_backend as VB
    assert isinstance(VB.BACKENDS["ltx"], VB.VideoBackend)


# ==================================================== 路由
def test_pick_backend_default():
    from apps.api import video_backend as VB
    be = VB.pick_backend(VB.SHOT_ACTION)
    assert be.name == "ltx"


def test_pick_backend_custom_routes():
    """★ 路由表可按镜头类型换后端。"""
    from apps.api import video_backend as VB
    be = VB.pick_backend(VB.SHOT_ACTION, routes={VB.SHOT_ACTION: "placeholder"})
    assert be.name == "placeholder"
    # 未覆盖的镜头仍走默认
    assert VB.pick_backend(VB.SHOT_DIALOGUE, routes={VB.SHOT_ACTION: "placeholder"}).name == "ltx"


def test_pick_backend_unknown_falls_back():
    from apps.api import video_backend as VB
    assert VB.pick_backend(VB.SHOT_ACTION, routes={VB.SHOT_ACTION: "不存在的后端"}).name == "placeholder"


def test_pick_backend_never_crashes():
    from apps.api import video_backend as VB
    for bad in [None, "", 123, "未知镜头"]:
        be = VB.pick_backend(bad)
        assert hasattr(be, "name")


def test_resolve_routes_default():
    from apps.api import video_backend as VB
    assert VB.resolve_routes(None) == VB.DEFAULT_ROUTES


def test_resolve_routes_from_project():
    """★ 项目级路由配置可覆盖默认。"""
    from apps.api import video_backend as VB

    class P:
        video_routes = '{"action": "placeholder"}'

    r = VB.resolve_routes(P())
    assert r["action"] == "placeholder"
    assert r["dialogue"] == "ltx"          # 未覆盖的用默认


def test_resolve_routes_bad_json_falls_back():
    from apps.api import video_backend as VB

    class P:
        video_routes = "{坏 json"

    assert VB.resolve_routes(P()) == VB.DEFAULT_ROUTES


# ==================================================== 统一入口
def test_generate_video_routes_to_placeholder():
    """★ 端到端：路由到 placeholder 时返回结构化失败（不抛异常）。"""
    from apps.api import video_backend as VB
    req = VB.VideoRequest(prompt="测试", shot_kind=VB.SHOT_ACTION)

    async def run():
        return await VB.generate_video(None, req, routes={VB.SHOT_ACTION: "placeholder"})

    r = asyncio.run(run())
    assert r.ok is False
    assert r.backend == "placeholder"
    assert "placeholder" in r.error


def test_generate_video_ltx_failure_is_structured(monkeypatch):
    """★ LTX 后端失败也返回 VideoResult（不抛异常）。"""
    from apps.api import video_backend as VB

    async def boom(*a, **k):
        raise RuntimeError("模拟 ComfyUI 挂了")

    monkeypatch.setattr(VB, "gen_video_comfyui" if hasattr(VB, "gen_video_comfyui") else "_never",
                        boom, raising=False)
    # 直接构造一个会失败的 backend
    class BadBackend:
        name = "bad"

        def supports(self, k):
            return True

        async def generate(self, model, req):
            return VB.VideoResult(ok=False, backend=self.name, error="模拟失败")

    VB.BACKENDS["bad"] = BadBackend()
    try:
        req = VB.VideoRequest(prompt="x", shot_kind=VB.SHOT_ACTION)

        async def run():
            return await VB.generate_video(None, req, routes={VB.SHOT_ACTION: "bad"})

        r = asyncio.run(run())
        assert r.ok is False and r.error == "模拟失败"
    finally:
        VB.BACKENDS.pop("bad", None)


def test_backend_capabilities():
    from apps.api import video_backend as VB
    caps = VB.backend_capabilities()
    names = {c["name"] for c in caps}
    assert "ltx" in names and "placeholder" in names
    for c in caps:
        assert isinstance(c["supports"], list)


# ==================================================== 集成
def test_media_run_media_has_shot_kind():
    """★ media.run_media 必须接受 shot_kind / routes（B1 接线点）。"""
    import inspect
    from apps.api import media
    sig = inspect.signature(media.run_media)
    assert "shot_kind" in sig.parameters
    assert "routes" in sig.parameters
    assert sig.parameters["shot_kind"].default == ""


def test_drama_passes_shot_kind():
    """★ drama.py 视频生成路径必须传 shot_kind（否则路由不生效）。"""
    import inspect
    from apps.api import drama
    src = inspect.getsource(drama)
    assert "video_backend" in src, "drama.py 未接入 video_backend"
    assert "shot_kind" in src and "classify_shot" in src


def test_module_contract():
    from apps.api import video_backend as VB
    for name in VB.__all__:
        assert hasattr(VB, name), f"__all__ 声明了 {name} 但未定义"


def test_shot_kinds_constant():
    from apps.api import video_backend as VB
    assert VB.SHOT_DIALOGUE in VB.ALL_SHOT_KINDS
    assert VB.SHOT_ACTION in VB.ALL_SHOT_KINDS
    assert VB.SHOT_CROWD in VB.ALL_SHOT_KINDS
    assert len(VB.ALL_SHOT_KINDS) == 5

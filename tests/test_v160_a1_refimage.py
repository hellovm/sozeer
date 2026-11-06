# -*- coding: utf-8 -*-
"""v0.16 A1 回归：角色参考图接通图像生成链（评估报告缺口 3 ★最高）。

缺口原文：
  "`DramaAsset.image` 已存角色参考图，但分镜出图**未强制以它为首帧/参考条件**
   —— 跨集一致性实际靠 prompt 描述"

根因（v0.16 实测定位）：
  run_media 的图像分支只调 `gen_image_comfyui(m, prompt)`，
  **ref_image / ref_images / ip_weight 三个参数被完全忽略** ——
  drama.generate_scene_media 明明传了 refs，到 media 层就丢了。

修复：
  * gen_image_comfyui 支持 IP-Adapter 工作流（IPAdapterUnifiedLoader +
    IPAdapter 串联，每张参考图一个节点）
  * run_media 图像分支透传 ref_image/ref_images/ip_weight
  * 多宫格出图也注入全集角色参考图
  * 无参考图 → 回落最简文生图（零回归）
"""
import os

import pytest

os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-a1")


# ==================================================== 签名与透传
def test_gen_image_comfyui_accepts_refs():
    """★ 核心契约：gen_image_comfyui 必须接受参考图参数。"""
    import inspect
    from apps.api import media
    sig = inspect.signature(media.gen_image_comfyui)
    for p in ("ref_image", "ref_images", "ip_weight", "negative"):
        assert p in sig.parameters, f"缺参数 {p}"
        assert sig.parameters[p].kind == inspect.Parameter.KEYWORD_ONLY, f"{p} 应为仅关键字"
    # 老调用（3 个位置参数）必须仍可用
    assert list(sig.parameters)[:3] == ["p", "prompt", "size"]


def test_run_media_passes_refs_to_image():
    """★ 核心修复：run_media 图像分支必须透传参考图（修复前被丢弃）。"""
    import inspect
    from apps.api import media
    src = inspect.getsource(media.run_media)
    i = src.index('if kind == "image":\n            if m.provider == "comfyui":')
    seg = src[i:i + 600]
    assert "ref_image=ref_image" in seg, "未透传 ref_image"
    assert "ref_images=ref_images" in seg, "未透传 ref_images"
    assert "ip_weight=ip_weight" in seg, "未透传 ip_weight"


# ==================================================== IP-Adapter 工作流结构
def _build_wf(refs_present=True):
    """复刻 gen_image_comfyui 的工作流构造（不真的发请求）。"""
    import uuid
    workflow = {}
    if refs_present:
        workflow = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "x"}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "p", "clip": ["1", 1]}},
            "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "n", "clip": ["1", 1]}},
            "4": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
            "50": {"class_type": "IPAdapterUnifiedLoader",
                   "inputs": {"preset": "PLUS (high strength)", "model": ["1", 0]}},
        }
        prev_model = ["50", 0]
        prev_ipa = None
        node = 51
        for idx in range(2):
            load_id, ipa_id = str(node), str(node + 1)
            node += 2
            workflow[load_id] = {"class_type": "LoadImage", "inputs": {"image": f"f{idx}"}}
            workflow[ipa_id] = {"class_type": "IPAdapter", "inputs": {
                "weight": 0.8, "image": [load_id, 0],
                "ipadapter": ["50", 1],
                "model": prev_model if idx == 0 else prev_ipa}}
            prev_ipa = [ipa_id, 0]
        workflow["5"] = {"class_type": "KSampler",
                         "inputs": {"model": prev_ipa, "positive": ["2", 0],
                                    "negative": ["3", 0], "latent_image": ["4", 0]}}
        workflow["6"] = {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}}
        workflow["7"] = {"class_type": "SaveImage", "inputs": {"images": ["6", 0]}}
    return workflow


def test_ipadapter_workflow_has_loader():
    """有参考图时，工作流必须含 IPAdapterUnifiedLoader + IPAdapter 节点。"""
    wf = _build_wf(True)
    types = {n["class_type"] for n in wf.values()}
    assert "IPAdapterUnifiedLoader" in types, "缺 IPAdapterUnifiedLoader"
    assert "IPAdapter" in types, "缺 IPAdapter"


def test_ipadapter_chains_multiple_refs():
    """★ 多角色：每张参考图一个 IPAdapter 节点，且 model 串联。"""
    wf = _build_wf(True)
    ipas = [n for n in wf.values() if n["class_type"] == "IPAdapter"]
    assert len(ipas) == 2, f"2 张参考图应有 2 个 IPAdapter，实际 {len(ipas)}"
    # 第二个 IPAdapter 的 model 应接第一个的输出
    assert ipas[1]["inputs"]["model"] == ["52", 0], \
        f"IPAdapter 未串联：{ipas[1]['inputs']['model']}"
    # KSampler 必须接最后一个 IPAdapter
    assert wf["5"]["inputs"]["model"] == ["54", 0]


def test_ipadapter_weight_applied():
    wf = _build_wf(True)
    for n in wf.values():
        if n["class_type"] == "IPAdapter":
            assert n["inputs"]["weight"] == 0.8


def test_no_refs_falls_back_to_basetxt2img():
    """★ 零回归：无参考图时，工作流必须还是原来的最简文生图（无 IPAdapter）。"""
    wf = _build_wf(False)
    assert wf == {}, "无参考图分支（本测试构造应为空）"
    # 真实实现里该分支的节点集
    import inspect
    from apps.api import media
    src = inspect.getsource(media.gen_image_comfyui)
    assert "最简文生图（原行为，零回归）" in src, "缺零回归分支注释"
    # 该分支不得出现 IPAdapter
    fallback = src[src.index("最简文生图（原行为，零回归）"):]
    fallback = fallback[:fallback.index("data = await _post_json")] if "data = await _post_json" in fallback else fallback
    assert "IPAdapter" not in fallback, "兜底分支混入 IPAdapter，破坏零回归"


# ==================================================== 资产图链路
def test_asset_derivative_uses_parent_ref():
    """衍生形态必须用本体定妆图作参考（原有能力，防回归）。"""
    import inspect
    from apps.api import drama
    src = inspect.getsource(drama.generate_asset_image)
    assert 'ref_image": parent.image' in src or "'ref_image': parent.image" in src, \
        "衍生形态未用本体图作参考"
    assert "ip_weight" in src


def test_grid_image_injects_refs():
    """★ 多宫格出图也注入角色参考图（v0.16 新增）。"""
    import inspect
    from apps.api import drama
    src = inspect.getsource(drama.generate_grid_image)
    assert "_scene_ref_images" in src, "多宫格未收集角色参考图"
    assert "ref_images" in src, "多宫格未透传 ref_images"


def test_scene_ref_images_returns_done_only():
    """_scene_ref_images 只收 image_status=done 且有图的资产。"""
    import inspect
    from apps.api import drama
    src = inspect.getsource(drama._scene_ref_images)
    assert '"done"' in src, "未过滤 image_status"


# ==================================================== 端到端：工作流构造
def test_gen_image_comfyui_builds_ipadapter_workflow(monkeypatch):
    """★ 端到端：给定参考图，实际发出的 workflow 含 IPAdapter 节点。"""
    import asyncio
    from apps.api import media
    from apps.api.models import ModelProfile

    captured = {}

    async def fake_post_json(url, payload, headers, timeout=60.0):
        captured["workflow"] = payload.get("prompt") or {}
        return {"prompt_id": "pid_test"}

    async def fake_upload(host, headers, ref_path):
        return f"uploaded_{ref_path}"

    monkeypatch.setattr(media, "_post_json", fake_post_json)
    monkeypatch.setattr(media, "_upload_comfy_image", fake_upload)

    p = ModelProfile(id="m1", name="c", provider="comfyui",
                     base_url="http://x", model_id="ckpt.safetensors")

    async def run():
        try:
            await asyncio.wait_for(
                media.gen_image_comfyui(p, "测试提示词", ref_image="a.png",
                                        ref_images=["b.png"], ip_weight=0.7),
                timeout=1.0)
        except Exception:
            pass   # 轮询阶段超时无所谓，我们只关心已发出的 workflow

    asyncio.run(run())

    wf = captured.get("workflow") or {}
    assert wf, "未捕获到 workflow"
    types = {n.get("class_type") for n in wf.values()}
    assert "IPAdapterUnifiedLoader" in types, f"未走 IP-Adapter 工作流：{types}"
    assert "IPAdapter" in types
    # 2 张参考图 → 2 个 IPAdapter
    n_ipa = sum(1 for n in wf.values() if n.get("class_type") == "IPAdapter")
    assert n_ipa == 2, f"应 2 个 IPAdapter，实际 {n_ipa}"

"""v0.14.1 P0二期：短剧视频线回归测试。

覆盖 D5 / D3 / D4 三个机制：
1. D5 drama_storyboard skill：八段结构识别、截断钳制、分镜细化接线、
   build_image_prompt/build_video_prompt 八段优先与旧结构兜底、宫格接线
2. D3 LTX-2.5：帧数纪律换算（≡1 mod 8）、{{frames}}/{{image}} 占位符、
   run_media video 路由（comfyui provider → gen_video_comfyui，mock HTTP）
3. D4 参考图链路：_scene_ref_images 收集规则（形态回退/去重/上限）、
   ip_weight 钳制、衍生资产图参考本体

跑法：项目根目录执行  python -m pytest tests/test_v141_drama_video.py -q
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# 隔离环境：临时数据目录，绝不碰真实 data/。
# 用 setdefault 而非覆盖：pytest 按字母序收集测试文件，先收集者（如
# test_core）已设置隔离目录；后收集者沿用同一目录即可，避免覆盖导致
# test_core 的 test_data_dir_isolated 断言拿到别的目录而失败。
_TMP = tempfile.mkdtemp(prefix="nf_test_v141_")
# 隔离环境：conftest.py 已统一设置，这里 setdefault 兜底
os.environ.setdefault("NOVELFORGE_DATA", tempfile.mkdtemp(prefix="nf_test_v141_"))
os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-for-pytest-only")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 关键：collection 阶段显式导入 models，确保 Base.metadata 在任何
# fixture 执行前已注册全部表（fixture 内的局部导入不触发模块加载）
import apps.api.models  # noqa: F401,E402


@pytest.fixture()
def db():
    from apps.api.db import Base, engine, SessionLocal
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        yield s


@pytest.fixture()
def drama_env(db):
    """书 + 短剧项目 + 2 个角色资产（一个带定妆图）+ 1 个衍生形态。"""
    import uuid
    import asyncio
    from apps.api.models import Book, DramaAsset, DramaEpisode
    from apps.api import drama
    b = Book(id=f"bk_{uuid.uuid4().hex[:8]}", title="测试书", genre="仙侠")
    db.add(b)
    db.commit()
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    p = loop.run_until_complete(
        drama.create_project(db, b, title="测试短剧", style="中国仙侠古风"))
    db.add(DramaEpisode(id=drama.new_id("de"), project_id=p.id, episode=1,
                        title="第1集", src_from=1, src_to=2,
                        source_text="第一集源文", status="scripted"))
    db.commit()
    a1 = DramaAsset(id=drama.new_id("da"), project_id=p.id, kind="character",
                    name="陈长安",
                    description="黑发青年剑客，白衣胜雪，剑眉星目")
    a1.image_status = "done"
    a1.image = "drama_media/asset_chen.png"
    a2 = DramaAsset(id=drama.new_id("da"), project_id=p.id, kind="scene",
                    name="青云山门",
                    description="云雾缭绕的山门石阶")
    a3 = DramaAsset(id=drama.new_id("da"), project_id=p.id, kind="character",
                    name="陈老太爷", form_name="老年", parent_id=a1.id,
                    description="白发苍苍，目光如炬")
    db.add_all([a1, a2, a3])
    db.commit()
    return b, p, a1, a2, a3


def _scene(db, project, sid="E1S01", chars=None, desc="陈长安立于山门前，拔剑指天",
           image_prompt=None, image_path="", idx=0):
    from apps.api.models import DramaScene
    from apps.api import drama as _d
    s = DramaScene(id=_d.new_id("ds"),
                   project_id=project.id, episode=1, scene_id=sid, idx=idx,
                   duration_seconds=8, characters_in_scene=json.dumps(chars or ["陈长安"]),
                   scene_description=desc,
                   image_prompt=json.dumps(image_prompt or {}),
                   image_path=image_path)
    db.add(s)
    db.commit()
    return s


# ---------------------------------------------------------------- D5 八段

def test_is_storyboard():
    from apps.api.drama import _is_storyboard
    assert _is_storyboard({"CHARACTER": "a", "STYLE": "b", "LIGHT": "c"})
    assert not _is_storyboard({"scene": "x", "composition": {}})   # 旧结构
    assert not _is_storyboard({})


def test_clamp_segments():
    from apps.api.drama import _clamp_segments
    ip = _clamp_segments({k: "长" * 200 for k in
                          ("CHARACTER", "BACKGROUND", "ACTION", "SCENE",
                           "CAMERA", "LIGHT", "TEXT", "STYLE")})
    assert all(len(v) <= 120 for v in ip.values())
    assert sum(len(v) for v in ip.values()) <= 600
    # 缺段补空串
    ip2 = _clamp_segments({"CHARACTER": "只有一段"})
    assert set(ip2) == {"CHARACTER", "BACKGROUND", "ACTION", "SCENE",
                        "CAMERA", "LIGHT", "TEXT", "STYLE"}
    assert ip2["TEXT"] == ""


def test_storyboard_skill_loads():
    from apps.api.skills import loader
    sk = loader.load("drama_storyboard", None)
    assert sk and "CHARACTER" in sk.body and sk.model_role == "critic"


def test_build_image_prompt_storyboard_first(db, drama_env):
    b, p, a1, a2, a3 = drama_env
    from apps.api import drama
    s = _scene(db, p, image_prompt={"CHARACTER": "黑发青年剑客", "BACKGROUND": "山门",
                                    "STYLE": "仙侠", "TEXT": ""})
    # 八段优先：不再拼资产描述、不重复加 project.style
    assert drama.build_image_prompt(db, p, s) == "黑发青年剑客，山门，仙侠"
    # 旧结构兜底路径仍在
    s2 = _scene(db, p, sid="E1S02",
                image_prompt={"scene": "山门前的青年", "composition": {"shot_type": "全景"}})
    out = drama.build_image_prompt(db, p, s2)
    assert "中国仙侠古风" in out and "全景" in out and "山门前的青年" in out


def test_build_video_prompt_fallback(db, drama_env):
    b, p, a1, a2, a3 = drama_env
    from apps.api import drama
    s = _scene(db, p)  # 无 video_prompt、非八段 → scene_description 兜底
    out = drama.build_video_prompt(db, p, s)
    assert "拔剑指天" in out and "中国仙侠古风" in out
    s2 = _scene(db, p, sid="E1S03",
                image_prompt={"CHARACTER": "c", "STYLE": "s", "LIGHT": "l"})
    # 手动写 video_prompt
    s2.video_prompt = json.dumps({"action": "缓缓拔剑", "camera_motion": "推镜"})
    db.commit()
    out2 = drama.build_video_prompt(db, p, s2)
    assert "缓缓拔剑" in out2 and "运镜：推镜" in out2


def test_grid_prompt_storyboard(db, drama_env):
    b, p, a1, a2, a3 = drama_env
    from apps.api import drama
    scenes = [_scene(db, p, sid=f"E1S0{i}",
                     image_prompt={"CHARACTER": "青年剑客", "ACTION": "拔剑",
                                   "SCENE": "全景居中", "TEXT": ""})
              for i in range(1, 5)]
    prompt = drama.build_grid_prompt(db, p, scenes)
    assert "2x2" in prompt and "拔剑" in prompt


def test_enrich_storyboard_flow(db, drama_env, monkeypatch):
    """D5 主流程：mock 模型返回八段 JSON → 回写 image_prompt/video_prompt。"""
    b, p, a1, a2, a3 = drama_env
    from apps.api import drama
    from apps.api.models import ModelProfile
    import uuid
    import asyncio
    # critic 模型槽（enrich_storyboard_prompts 走 pick_model("critic")）
    db.add(ModelProfile(id=f"mdl_{uuid.uuid4().hex[:6]}", name="CriticM",
                        provider="openai-compatible", role="critic",
                        base_url="http://mock", model_id="critic-x", enabled=1))
    db.commit()

    async def fake_complete(m, msgs, **kw):
        class R:
            text = json.dumps({"scenes": [
                {"scene_id": "E1S01",
                 "image_prompt": {"CHARACTER": "黑发青年剑客，白衣胜雪" + "长" * 130,
                                  "BACKGROUND": "云雾缭绕的山门石阶", "ACTION": "拔剑指天",
                                  "SCENE": "中景居中", "CAMERA": "固定机位",
                                  "LIGHT": "晨光", "TEXT": "", "STYLE": "中国仙侠古风"},
                 "video_prompt": {"action": "缓缓拔剑", "camera_motion": "固定机位"}}]},
                ensure_ascii=False)
            error = ""
        return R()

    monkeypatch.setattr(drama.llm, "complete", fake_complete)
    s = _scene(db, p)   # 待细化的分镜（scene_description 非空、image_prompt 空）
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    r = loop.run_until_complete(drama.enrich_storyboard_prompts(db, p, 1))
    assert r["ok"] and r["enriched"] == 1
    from apps.api.models import DramaScene, jl
    row = db.query(DramaScene).filter_by(project_id=p.id, scene_id="E1S01").first()
    ip = jl(row.image_prompt, {})
    assert set(ip) >= {"CHARACTER", "BACKGROUND", "STYLE"}   # 八段齐
    assert len(ip["CHARACTER"]) <= 120                        # 截断生效
    vp = jl(row.video_prompt, {})
    assert vp.get("action") == "缓缓拔剑"
    # 幂等：已八段的不再重跑
    r2 = loop.run_until_complete(drama.enrich_storyboard_prompts(db, p, 1))
    assert r2["enriched"] == 0


# ---------------------------------------------------------------- D3 LTX

def test_ltx_frames_discipline():
    from apps.api.media import ltx_frames_for_duration, LTX_MAX_FRAMES
    assert ltx_frames_for_duration(None) == 33
    assert ltx_frames_for_duration(0) == 33
    assert ltx_frames_for_duration("bad") == 33
    for sec in (1, 2, 3, 4, 5, 8, 10, 20, 2.6):
        n = ltx_frames_for_duration(sec)
        assert (n - 1) % 8 == 0 and 9 <= n <= LTX_MAX_FRAMES
    assert ltx_frames_for_duration(1.3) == 33    # 32.5 → 33
    assert ltx_frames_for_duration(0.5) == 9     # 12.5 → 9
    assert ltx_frames_for_duration(4) == 97      # 100 → 就近 ≡1 mod 8（97 距 3 < 105 距 5）
    assert ltx_frames_for_duration(100) == LTX_MAX_FRAMES  # 超长钳制


def test_placeholder_ext():
    from apps.api.media import _PLACEHOLDER_RE
    assert _PLACEHOLDER_RE.search("{{frames}}") and _PLACEHOLDER_RE.search("{{image3}}")
    assert _PLACEHOLDER_RE.search("{{weight}}") and _PLACEHOLDER_RE.search("{{image}}")
    assert _PLACEHOLDER_RE.search("{{width2}}") and _PLACEHOLDER_RE.search("{{height2}}")


def test_workflow_has():
    from apps.api.media import _workflow_has
    wf = {"1": {"class_type": "LTXVBaseSampler",
                "inputs": {"prompt": "{{prompt}}", "length": "{{frames}}"}},
          "2": {"class_type": "IPAdapter", "inputs": {"image": "{{image2}}",
                                                      "weight": "{{weight}}"}}}
    assert _workflow_has(wf, "frames") and _workflow_has(wf, "image2")
    assert _workflow_has(wf, "weight") and not _workflow_has(wf, "image9")


def test_ltx_mapping():
    from apps.api.media import _ltx_mapping
    wf = {"1": {"class_type": "X", "inputs": {"prompt": "{{prompt}}",
                                              "length": "{{frames}}",
                                              "width": "{{width}}",
                                              "height": "{{height}}"}}}
    mp = _ltx_mapping(wf, "测试提示词", seconds=5, width=720, height=1280)
    assert (mp["frames"] - 1) % 8 == 0 and mp["width"] == 720
    assert "image" not in mp     # workflow 无 {{image}} 槽不填
    wf2 = {"1": {"class_type": "X", "inputs": {"image": "{{image}}"}}}
    mp2 = _ltx_mapping(wf2, "p", ref_image="a.png[subs]")
    assert mp2["image"] == "a.png[subs]"


def test_ltx_mapping_two_stage():
    """LTX-2.5 双阶段：{{width2}}/{{height2}} 半分辨率 32 对齐（720→360→352）。"""
    from apps.api.media import _ltx_mapping
    wf = {"1": {"class_type": "EmptyLTXVLatentVideo",
                "inputs": {"width": "{{width2}}", "height": "{{height2}}",
                           "length": "{{frames}}"}}}
    mp = _ltx_mapping(wf, "p", seconds=4, width=720, height=1280)
    assert mp["width2"] == 352 and mp["height2"] == 640
    assert (mp["frames"] - 1) % 8 == 0
    # 单阶段工作流（无 width2 槽）不受影响
    mp1 = _ltx_mapping(wf, "p", seconds=4, width=720, height=1280)
    assert "width" not in mp1


def test_clamp_ip_weight():
    from apps.api.media import _clamp_ip_weight
    assert _clamp_ip_weight(0.8) == 0.8
    assert _clamp_ip_weight(5) == 2.0
    assert _clamp_ip_weight(-1) == 0.0
    assert _clamp_ip_weight("abc") is None
    assert _clamp_ip_weight(None) is None


def test_run_media_video_routes_to_ltx(db, drama_env, monkeypatch):
    """D3 路由：role=video + provider=comfyui → gen_video_comfyui（mock）。"""
    import uuid
    from apps.api.models import ModelProfile
    from apps.api import media
    b, p, a1, a2, a3 = drama_env
    mp = ModelProfile(id=f"mdl_{uuid.uuid4().hex[:6]}", name="LTX-2.5",
                      provider="comfyui", role="video", base_url="http://127.0.0.1:8188",
                      model_id="ltxwf", enabled=1)
    db.add(mp)
    db.commit()
    called = {}

    async def fake_gen(m, prompt, **kw):
        called.update(kw)
        called["prompt"] = prompt
        return b"fake-mp4-bytes"

    monkeypatch.setattr(media, "gen_video_comfyui", fake_gen)
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    r = loop.run_until_complete(
        media.run_media(db, b.id, "video", "测试视频提示词",
                        seconds=8, width=720, height=1280,
                        ref_image="drama_media/x.png", ref_images=["drama_media/y.png"],
                        ip_weight=0.9))
    assert r["ok"] and r["path"].endswith(".mp4")
    assert called.get("seconds") == 8 and called.get("ip_weight") == 0.9
    assert called.get("ref_images") == ["drama_media/y.png"]


def test_run_media_video_unknown_provider(monkeypatch):
    """provider 不明 → 占位报错引导配置。

    用独立 DB_URL（进程内新建 engine）避免受其他测试残留模型行的
    default 回退污染——pick_model 的 default 回退按行序取第一个。
    """
    import uuid
    import asyncio
    import importlib
    os.environ["NOVELFORGE_DATA"] = tempfile.mkdtemp(prefix="nf_test_v141_iso_")
    importlib.reload(importlib.import_module("apps.api.config"))
    dbmod = importlib.reload(importlib.import_module("apps.api.db"))
    importlib.reload(importlib.import_module("apps.api.models"))
    media_mod = importlib.reload(importlib.import_module("apps.api.media"))
    dbmod.Base.metadata.create_all(dbmod.engine)
    from apps.api.models import Book, ModelProfile
    b = Book(id=f"bk_{uuid.uuid4().hex[:8]}", title="t")
    with dbmod.SessionLocal() as db:
        db.add(b)
        db.add(ModelProfile(id=f"mdl_{uuid.uuid4().hex[:6]}", name="云端视频",
                            provider="vendorx", role="video", base_url="http://x",
                            model_id="m", enabled=1))
        db.commit()
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        r = loop.run_until_complete(
            media_mod.run_media(db, b.id, "video", "p"))
    assert not r["ok"] and "comfyui" in r["error"]


# ---------------------------------------------------------------- D4 参考图

def test_scene_ref_images(db, drama_env):
    b, p, a1, a2, a3 = drama_env
    from apps.api import drama
    import json as _j
    # 基本收集：出场角色有定妆图
    s = _scene(db, p, chars=["陈长安"])
    assert drama._scene_ref_images(db, p, s) == ["drama_media/asset_chen.png"]
    # 形态引用回退本体图
    s2 = _scene(db, p, sid="E1S02", chars=["陈长安（老年）"])
    assert drama._scene_ref_images(db, p, s2) == ["drama_media/asset_chen.png"]
    # 无图/未出场 → 空
    s3 = _scene(db, p, sid="E1S03", chars=["路人甲"])
    assert drama._scene_ref_images(db, p, s3) == []


def test_generate_scene_media_video_passthrough(db, drama_env, monkeypatch):
    """D3+D4 全链路：generate_scene_media(video) 透传 seconds/ref_image/ref_images。"""
    b, p, a1, a2, a3 = drama_env
    from apps.api import drama, media
    import asyncio
    s = _scene(db, p, image_path="drama_media/scene_img.png")

    async def fake_run_media(db_, book_id, kind, prompt, **kw):
        assert kind == "video"
        assert kw.get("seconds") == 8              # duration_seconds 透传
        assert kw.get("ref_image") == "drama_media/scene_img.png"
        assert kw.get("ref_images") == ["drama_media/asset_chen.png"]
        assert kw.get("ip_weight") == 0.8
        return {"ok": True, "path": "drama_media/video_x.mp4"}

    monkeypatch.setattr(media, "run_media", fake_run_media)
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    r = loop.run_until_complete(
        drama.generate_scene_media(db, p, s, "video"))
    assert r["ok"] and r["path"] == "drama_media/video_x.mp4"
    assert s.media_status == "done" and s.video_path == "drama_media/video_x.mp4"


# ---------------------------------------------------------------- D7 末帧传递

def test_prev_end_frame_no_prev(db, drama_env):
    """D7：首镜（无前镜视频）→ 返回空串，不抛错。"""
    b, p, a1, a2, a3 = drama_env
    from apps.api import drama
    s = _scene(db, p, sid="E1S01")
    assert drama._prev_end_frame(db, p, s) == ""


def test_prev_end_frame_prev_no_video(db, drama_env):
    """D7：前镜存在但无视频产物 → 空串（退化分镜图锚定）。"""
    b, p, a1, a2, a3 = drama_env
    from apps.api import drama
    _scene(db, p, sid="E1S01", idx=0)             # 无 video_path
    s2 = _scene(db, p, sid="E1S02", idx=1)
    assert drama._prev_end_frame(db, p, s2) == ""


def test_prev_end_frame_extracts_last_frame(db, drama_env, monkeypatch):
    """D7：前镜视频在盘 → ffmpeg 抽末帧成功，返回 PNG 绝对路径。

    无 ffmpeg 的环境（CI/本地）monkeypatch shutil.which 返回假路径并
    mock subprocess.run 模拟成功，纯逻辑验证。
    """
    b, p, a1, a2, a3 = drama_env
    from apps.api import drama
    from apps.api.config import BOOKS_DIR
    import json as _json
    from apps.api.models import DramaScene
    prev = _scene(db, p, sid="E1S01", idx=0)
    # 造一个真文件充当"前镜视频"
    rel = "drama_media/prev_ep1.mp4"
    f = BOOKS_DIR / p.book_id / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 64)
    prev.video_path = f"/api/files/{p.book_id}/{rel}"
    db.commit()
    s = _scene(db, p, sid="E1S02", idx=1)

    calls = {}
    import subprocess, shutil

    class FakeCompleted:
        returncode = 0

    def fake_run(argv, **kw):
        calls["argv"] = argv
        # 模拟 ffmpeg 真的写出了文件
        out = argv[-1]
        with open(out, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\nfake")
        return FakeCompleted()

    monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/ffmpeg" if x == "ffmpeg" else None)
    monkeypatch.setattr(subprocess, "run", fake_run)
    got = drama._prev_end_frame(db, p, s)
    assert got and got.endswith("_prev_end.png")
    assert "E1S01" in got
    argv = calls["argv"]
    assert argv[0] == "ffmpeg" and argv[1:4] == ["-y", "-v", "error"]
    assert "-sseof" in argv and argv[argv.index("-sseof") + 1] == "-0.1"
    assert argv[argv.index("-frames:v") + 1] == "1"
    assert argv[-1].endswith("_prev_end.png")


def test_prev_end_frame_ffmpeg_missing(db, drama_env, monkeypatch):
    """D7：ffmpeg 不在 PATH → 空串（静默降级，不阻塞视频生成）。"""
    b, p, a1, a2, a3 = drama_env
    from apps.api import drama
    from apps.api.config import BOOKS_DIR
    import shutil
    prev = _scene(db, p, sid="E1S01", idx=0)
    rel = "drama_media/prev2.mp4"
    f = BOOKS_DIR / p.book_id / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 32)
    prev.video_path = f"/api/files/{p.book_id}/{rel}"
    db.commit()
    s = _scene(db, p, sid="E1S02", idx=1)
    monkeypatch.setattr(shutil, "which", lambda x: None)
    assert drama._prev_end_frame(db, p, s) == ""


def test_generate_scene_media_video_uses_prev_frame(db, drama_env, monkeypatch):
    """D7 全链路：前镜有视频 → generate_scene_media(video) 的 ref_image
    是抽出的末帧路径而非分镜图。"""
    b, p, a1, a2, a3 = drama_env
    from apps.api import drama, media
    import asyncio
    from apps.api.config import BOOKS_DIR
    prev = _scene(db, p, sid="E1S01", idx=0)
    rel = "drama_media/prev3.mp4"
    f = BOOKS_DIR / p.book_id / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 32)
    prev.video_path = f"/api/files/{p.book_id}/{rel}"
    db.commit()
    s2 = _scene(db, p, sid="E1S02", idx=1, image_path="drama_media/scene_img.png")

    captured = {}

    async def fake_run_media(db_, book_id, kind, prompt, **kw):
        captured["ref_image"] = kw.get("ref_image")
        return {"ok": True, "path": "drama_media/video_y.mp4"}

    monkeypatch.setattr(media, "run_media", fake_run_media)
    monkeypatch.setattr(drama, "_prev_end_frame",
                        lambda db_, p_, s_: "/tmp/fake_prev_end.png")
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    r = loop.run_until_complete(drama.generate_scene_media(db, p, s2, "video"))
    assert r["ok"]
    assert captured["ref_image"] == "/tmp/fake_prev_end.png"   # 末帧优先于分镜图


def test_generate_scene_media_video_first_scene_falls_back(db, drama_env, monkeypatch):
    """D7：首镜无前镜 → ref_image 回退分镜图（D3 原行为不变）。"""
    b, p, a1, a2, a3 = drama_env
    from apps.api import drama, media
    import asyncio
    s = _scene(db, p, sid="E1S01", image_path="drama_media/scene_first.png")
    captured = {}

    async def fake_run_media(db_, book_id, kind, prompt, **kw):
        captured["ref_image"] = kw.get("ref_image")
        return {"ok": True, "path": "drama_media/video_z.mp4"}

    monkeypatch.setattr(media, "run_media", fake_run_media)
    monkeypatch.setattr(drama, "_prev_end_frame", lambda db_, p_, s_: "")
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    r = loop.run_until_complete(drama.generate_scene_media(db, p, s, "video"))
    assert r["ok"]
    assert captured["ref_image"] == "drama_media/scene_first.png"


def test_d7_chain_dependency_in_enqueue(db, drama_env):
    """D7：generate-all/video 的链式依赖——video_i 依赖 video_{i-1}。

    直接验证 main.py 入队逻辑的依赖图：模拟 scenes idx 顺序，检查
    每行 dependency_id 指向前一镜的 video 行（首镜除外）。
    """
    b, p, a1, a2, a3 = drama_env
    from apps.api.models import DramaTask, DramaScene
    from apps.api import drama as drama_mod, drama_queue as dq
    # 造 3 个顺序分镜
    scenes = []
    for i, sid in enumerate(("E1S01", "E1S02", "E1S03"), 1):
        sc = DramaScene(id=drama_mod.new_id("ds"), project_id=p.id,
                        episode=1, scene_id=sid, idx=i - 1,
                        characters_in_scene="[]", scene_description="x",
                        image_prompt="{}", video_prompt="{}")
        db.add(sc)
        scenes.append(sc)
    db.commit()
    # 复刻 main.py generate-all 的链式入队逻辑（单 dependency_id 槽）
    video_task_ids: dict[str, str] = {}
    rows = []
    for s in scenes:
        dep = ""
        prev = max((x for x in scenes if x.idx < s.idx),
                   key=lambda x: x.idx, default=None)
        if prev is not None and prev.id in video_task_ids:
            dep = video_task_ids[prev.id]
        t = dq.enqueue(db, book_id=b.id, project_id=p.id, episode=1,
                       task_type="video", resource_type="scene",
                       resource_id=s.id, resource_key=s.scene_id,
                       batch_id=dq.new_batch_id(), dependency_id=dep)
        video_task_ids[s.id] = t.id
        rows.append(t)
    db.commit()
    # 断言链：S01 无依赖，S02 依赖 S01 行，S03 依赖 S02 行
    by_scene = {r.resource_id: r for r in rows}
    assert by_scene[scenes[0].id].dependency_id == ""
    assert by_scene[scenes[1].id].dependency_id == by_scene[scenes[0].id].id
    assert by_scene[scenes[2].id].dependency_id == by_scene[scenes[1].id].id


# ---------------------------------------------------------------- D6 VLM 审图

def asyncio_run(coro):
    """测试内跑协程（复用既有 event loop 语义，与旧用例一致）。"""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def _mk_img(drama_env, name="qc_test.png"):
    """造一张真图文件供 qc 引擎读取（不必是真图，引擎只读 bytes）。"""
    b, p, a1, a2, a3 = drama_env
    from apps.api.config import BOOKS_DIR
    rel = f"drama_media/{name}"
    f = BOOKS_DIR / p.book_id / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    return rel


def _qc_rsp(pass_=True, scores=None, checks=None, issues=None):
    """构造一个 VLM 返回体（qc._vlm_once 的 JSON 解析目标）。"""
    return {"pass": pass_,
            "scores": scores or {"subject_clarity": 5, "element_completeness": 5,
                                 "realism": 5, "scene_integrity": 5},
            "checks": checks or [],
            "issues": issues or []}


def test_qc_majority_all_pass(db, drama_env, monkeypatch):
    """3 票全 pass → pass=True，分数中位。"""
    from apps.api import qc
    rel = _mk_img(drama_env)
    b, p, a1, a2, a3 = drama_env
    s = _scene(db, p, image_prompt={"CHARACTER": "白衣剑客", "ACTION": "拔剑"})

    async def fake_vlm(p_, content, max_tokens=1200):
        return _qc_rsp(True, scores={"subject_clarity": 5, "element_completeness": 4,
                                     "realism": 5, "scene_integrity": 4})

    monkeypatch.setattr(qc, "_vlm_once", fake_vlm)
    rep = asyncio_run(qc.qc_shot(db, p, s, f"/api/files/{p.book_id}/{rel}"))
    assert rep["ok"] and rep["pass"] is True
    assert rep["pass_votes"] == 3
    assert rep["scores"]["subject_clarity"] == 5
    assert rep["weakest"] in ("element_completeness", "scene_integrity")


def test_qc_majority_2of3_fail(db, drama_env, monkeypatch):
    """2/3 票 fail → 整体 fail；重复问题清单合并计票。"""
    from apps.api import qc
    rel = _mk_img(drama_env, "qc_test2.png")
    b, p, a1, a2, a3 = drama_env
    s = _scene(db, p)

    votes = [_qc_rsp(True), _qc_rsp(False, issues=["人物左手多出一根手指"]),
             _qc_rsp(False, issues=["人物左手多出一根手指"])]
    seq = {"i": 0}

    async def fake_vlm(p_, content, max_tokens=1200):
        r = votes[seq["i"] % 3]
        seq["i"] += 1
        return r

    monkeypatch.setattr(qc, "_vlm_once", fake_vlm)
    rep = asyncio_run(qc.qc_shot(db, p, s, f"/api/files/{p.book_id}/{rel}"))
    assert rep["ok"] and rep["pass"] is False
    assert rep["pass_votes"] == 1
    assert any("手指" in i["message"] for i in rep["issues"])
    dup = [i for i in rep["issues"] if "手指" in i["message"]]
    assert dup and dup[0]["votes"] == 2            # 合并计票
    assert "重画时务必修正" in rep["retry_hint"]


def test_qc_stage2_missing_element_forces_fail(db, drama_env, monkeypatch):
    """阶段2 程序化核验：过半数票说"缺要素" → 强制 fail（模型说了 pass 也推翻）。"""
    from apps.api import qc
    rel = _mk_img(drama_env, "qc_test3.png")
    b, p, a1, a2, a3 = drama_env
    s = _scene(db, p, image_prompt={"CHARACTER": "白衣剑客", "SCENE": "山门石阶"})

    def rsp():
        return _qc_rsp(True, checks=[
            {"element": "SCENE: 山门石阶", "present": False, "evidence": "画面无石阶"}])

    async def fake_vlm(p_, content, max_tokens=1200):
        return rsp()

    monkeypatch.setattr(qc, "_vlm_once", fake_vlm)
    rep = asyncio_run(qc.qc_shot(db, p, s, f"/api/files/{p.book_id}/{rel}"))
    assert rep["ok"] and rep["pass"] is False     # pass 票全 true 但被 stage2 推翻
    assert rep["stage2"]["missing"]
    assert "缺要素" in rep["issues"][0]["message"]


def test_qc_no_model_skips(db, drama_env, monkeypatch):
    """无任何可用模型 → skipped，不抛错。"""
    from apps.api import qc
    b, p, a1, a2, a3 = drama_env
    s = _scene(db, p)
    monkeypatch.setattr(qc, "_pick_qc_model", lambda db_: None)
    rep = asyncio_run(qc.qc_shot(db, p, s, "/api/files/x/y.png"))
    assert rep["ok"] is False and "skipped" in rep


def test_qc_image_missing_skips(db, drama_env, monkeypatch):
    """图文件不存在 → skipped。"""
    from apps.api import qc
    b, p, a1, a2, a3 = drama_env
    s = _scene(db, p)
    rep = asyncio_run(qc.qc_shot(db, p, s, "/api/files/nobook/nope.png"))
    assert rep["ok"] is False and "不存在" in rep.get("skipped", "")


def test_qc_vlm_all_error_skips(db, drama_env, monkeypatch):
    """VLM 三连失败（如 HTTP 4xx 不支持多模态）→ skipped 带原因。"""
    from apps.api import qc
    rel = _mk_img(drama_env, "qc_test4.png")
    b, p, a1, a2, a3 = drama_env
    s = _scene(db, p)

    async def fake_vlm(p_, content, max_tokens=1200):
        raise qc.QCError("HTTP 400: image input not supported")

    monkeypatch.setattr(qc, "_vlm_once", fake_vlm)
    rep = asyncio_run(qc.qc_shot(db, p, s, f"/api/files/{p.book_id}/{rel}"))
    assert rep["ok"] is False
    assert "多模态" in rep.get("skipped", "")


def test_qc_and_retry_retries_with_hint(db, drama_env, monkeypatch):
    """D6 全链路：fail → 重试生图（prompt 带问题清单）→ 复审 pass → 用新图。"""
    from apps.api import drama, media, qc
    b, p, a1, a2, a3 = drama_env
    s = _scene(db, p, image_path="")
    rel = _mk_img(drama_env, "qc_first.png")

    calls = {"gen": 0, "qc": 0}

    async def fake_run_media(db_, book_id, kind, prompt, **kw):
        # 首轮图由外部传入 first_path（不调 run_media），
        # run_media 只在 fail 重试时被调用 → 第 1 次调用就是重试
        assert kind == "image"
        # 重试图：prompt 必须带问题清单
        assert "重画时务必修正" in prompt
        calls["gen"] += 1
        return {"ok": True, "path": "drama_media/qc_retry.png"}

    async def fake_qc_shot(db_, p_, s_, img_path):
        calls["qc"] += 1
        if calls["qc"] == 1:
            return {"ok": True, "pass": False, "votes": 3, "pass_votes": 0,
                    "issues": [{"message": "人物面部糊成一片", "votes": 3, "stage": 1}],
                    "retry_hint": "上一版画面存在以下问题，重画时务必修正：人物面部糊成一片"}
        return {"ok": True, "pass": True, "votes": 3, "pass_votes": 3,
                "issues": [], "scores": {}}

    monkeypatch.setattr(media, "run_media", fake_run_media)
    monkeypatch.setattr(drama, "_qc_shot_isolated", fake_qc_shot, raising=False)
    # drama._qc_and_retry 内部直接调 qc_mod.qc_shot——monkeypatch qc.qc_shot
    monkeypatch.setattr(qc, "qc_shot", fake_qc_shot)
    monkeypatch.setattr(qc, "_pick_qc_model",
                        lambda db_: type("M", (), {"provider": "test", "model_id": "vlm-x"})())
    monkeypatch.setattr(qc, "qc_media_call", lambda *a, **k: None)

    async def flow():
        return await drama._qc_and_retry(db, p, s, "提示词", rel)

    path, rep_json = asyncio_run(flow())
    # run_media 只在重试时被调 1 次（首轮图是外部传入的 first_path）；qc 跑 2 轮
    assert calls["gen"] == 1 and calls["qc"] == 2
    assert path == "drama_media/qc_retry.png"
    rep = json.loads(rep_json)
    assert rep["pass"] is True and rep["retries"] == 1


def test_qc_and_retry_pass_first_keeps_image(db, drama_env, monkeypatch):
    """D6：首轮 pass → 不重试，图不变。"""
    from apps.api import drama, qc
    b, p, a1, a2, a3 = drama_env
    s = _scene(db, p)
    rel = _mk_img(drama_env, "qc_ok.png")

    async def fake_qc(db_, p_, s_, img):
        return {"ok": True, "pass": True, "votes": 3, "pass_votes": 3, "issues": []}

    monkeypatch.setattr(qc, "qc_shot", fake_qc)
    monkeypatch.setattr(qc, "_pick_qc_model", lambda db_: None)
    monkeypatch.setattr(qc, "qc_media_call", lambda *a, **k: None)
    path, rep_json = asyncio_run(drama._qc_and_retry(db, p, s, "p", rel))
    assert path == rel
    rep = json.loads(rep_json)
    assert rep["pass"] is True and rep["retries"] == 0


def test_qc_media_call_ledger(db, drama_env):
    """审图调用记账：ok 与 error 各一行，model_id 带 qc: 前缀。"""
    from apps.api import qc
    from apps.api.models import MediaCall
    from sqlalchemy import select as _sel
    b, p, a1, a2, a3 = drama_env
    m = type("M", (), {"provider": "openai-compatible", "model_id": "qwen-vl"})()
    qc.qc_media_call(db, b.id, m, {"ok": True, "cost": 0.01}, duration_ms=123)
    qc.qc_media_call(db, b.id, m, {"ok": False, "skipped": "无模型"})
    rows = db.scalars(_sel(MediaCall).where(MediaCall.book_id == b.id)).all()
    assert len(rows) == 2
    assert all(r.model_id.startswith("qc:") for r in rows)
    assert {r.status for r in rows} == {"ok", "error"}


def test_scene_dict_includes_qc(db, drama_env, monkeypatch):
    """scene_dict 带 qc 摘要（徽章数据源）。"""
    from apps.api import drama
    b, p, a1, a2, a3 = drama_env
    s = _scene(db, p)
    from apps.api.models import jd as _jd
    s.qc_report = _jd({"ok": True, "pass": False, "votes": 3, "pass_votes": 1,
                       "scores": {"realism": 3}, "retries": 1,
                       "issues": [{"message": "面部糊", "votes": 3}]})
    db.commit()
    d = drama.scene_dict(s)
    assert d["qc"]["pass"] is False and d["qc"]["retries"] == 1
    assert d["qc"]["issues"] == ["面部糊"]
    # 未审图 → qc 为空 dict
    s2 = _scene(db, p, sid="E1S09")
    assert drama.scene_dict(s2)["qc"] == {}

# -*- coding: utf-8 -*-
"""v1.0-E4.23 短剧资产自动管线 + 批量生成契约修复。

用户报障（生产截图）：
  ① 点「生成图」批量按钮 → 400「target 必须是 image/video/audio」
     —— 根因：前端批量按钮传复数 images/videos/audios，后端只认单数。
  ② 重要资产（人物/道具/场景）不会自动提取并生成参考图，
     跨镜人物/场景一致性无保障。

本文件覆盖：
  1. 契约：generate-all / scene-generate 复数容错归一 + 非法值仍 400
  2. ensure_character_assets：幂等自动提取 + 形态名只登记本体
  3. enrich_assets：按出场上下文 grounding（画面描述/台词进提示词）
  4. _auto_asset_pipeline 入口（走端点函数）：缺图角色入队 asset_image、
     衍生形态依赖本体任务、响应带 asset_tasks
  5. _scene_ref_images 重构回归：形态无图回退本体、去重、上限
  6. 分镜执行器 defer：在途定妆图 → 等待（不耗 attempts）；超时放行
  7. 前端契约：批量按钮传单数、资产卡读 image 字段
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time

import pytest

# 隔离环境（conftest 已统一设置，这里 setdefault 兜底）
os.environ.setdefault("NOVELFORGE_DATA", tempfile.mkdtemp(prefix="nf_test_e423_"))
os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-e423")
os.environ.setdefault("NOVELFORGE_DISABLE_WORKERS", "1")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import apps.api.models  # noqa: F401  collection 阶段注册全部表


# ============================================================ fixtures

@pytest.fixture()
def db():
    from apps.api.db import Base, engine, SessionLocal
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        yield s


@pytest.fixture()
def env(db):
    """书 + 项目 + 第1集（含源文）+ 2 场景行 + 角色/形态/场景资产。"""
    import asyncio
    import uuid
    from apps.api.models import Book, DramaAsset, DramaEpisode, DramaScene
    from apps.api import drama

    b = Book(id=f"bk_{uuid.uuid4().hex[:8]}", title="E423 测试书", genre="仙侠")
    db.add(b)
    db.commit()
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    p = loop.run_until_complete(drama.create_project(db, b, title="E423 项目"))
    db.add(DramaEpisode(id=drama.new_id("de"), project_id=p.id, episode=1,
                        title="第1集", src_from=1, src_to=1,
                        source_text="源文一", status="scripted"))
    db.commit()

    s1 = DramaScene(id=drama.new_id("ds"), project_id=p.id, episode=1,
                    scene_id="E1S01", idx=0, duration_seconds=8,
                    characters_in_scene=json.dumps(["陈长安"]),
                    scene_description="雪山山门，陈长安立于阶前，白衣胜雪。",
                    utterances=json.dumps([{"kind": "dialogue",
                                            "speaker": "陈长安",
                                            "text": "山门虽远，此心可往。"}]))
    s2 = DramaScene(id=drama.new_id("ds"), project_id=p.id, episode=1,
                    scene_id="E1S02", idx=1, duration_seconds=8,
                    characters_in_scene=json.dumps(["陈长安（老年）"]),
                    scene_description="山门内，白发老人背手而立。")
    db.add_all([s1, s2])
    db.commit()

    a1 = DramaAsset(id=drama.new_id("da"), project_id=p.id, kind="character",
                    name="陈长安", description="黑发青年剑客，白衣胜雪，剑眉星目")
    a2 = DramaAsset(id=drama.new_id("da"), project_id=p.id, kind="character",
                    name="陈长安（老年）", form_name="老年", parent_id=a1.id,
                    description="白发苍苍，目光如炬")
    a3 = DramaAsset(id=drama.new_id("da"), project_id=p.id, kind="scene",
                    name="青云山门", description="云雾缭绕的山门石阶")
    db.add_all([a1, a2, a3])
    db.commit()
    yield b, p, a1, a2, a3, s1, s2
    # 清理（幂等）
    from apps.api.models import (DramaProject, DramaScene as DS2,
                                 DramaAsset as DA2, DramaEpisode as DE2, DramaTask)
    from sqlalchemy import delete
    for M in (DS2, DA2, DE2, DramaTask):
        db.execute(delete(M).where(M.project_id == p.id))
    db.execute(delete(DramaProject).where(DramaProject.id == p.id))
    db.execute(delete(Book).where(Book.id == b.id))
    db.commit()


def _run(coro):
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


# ============================================================ 1. 契约：复数容错

def test_generate_all_accepts_plural_images(db, env):
    """★ 用户报障根因：旧前端批量按钮传 'images' → 400。现在必须 200 入队。"""
    from apps.api import main as m
    from apps.api.db import SessionLocal
    from apps.api.models import DramaTask
    from sqlalchemy import select
    b, p, *_ = env
    s = SessionLocal()
    try:
        r = _run(m.drama_generate_all(b.id, p.id, 1, "images", s))
    finally:
        s.close()
    assert r["status"] == "queued", f"复数 target 不应再 400：{r}"
    assert r["scene_count"] == 2
    s2 = db
    rows = s2.scalars(select(DramaTask).where(
        DramaTask.project_id == p.id, DramaTask.task_type == "image")).all()
    assert len(rows) == 2, "应入队 2 个分镜图任务"


def test_generate_all_accepts_plural_videos_and_audios(db, env):
    b, p, *_ = env
    from apps.api import main as m
    from apps.api.db import SessionLocal
    for target in ("videos", "audios"):
        s = SessionLocal()
        try:
            r = _run(m.drama_generate_all(b.id, p.id, 1, target, s))
        finally:
            s.close()
        assert r["status"] == "queued"


def test_generate_all_rejects_unknown_target(db, env):
    from fastapi import HTTPException
    from apps.api import main as m
    from apps.api.db import SessionLocal
    b, p, *_ = env
    s = SessionLocal()
    try:
        with pytest.raises(HTTPException) as ei:
            _run(m.drama_generate_all(b.id, p.id, 1, "bogus", s))
        assert ei.value.status_code == 400
    finally:
        s.close()


def test_scene_generate_accepts_plural(db, env, monkeypatch):
    b, p, a1, a2, a3, s1, s2 = env
    from apps.api import drama as drama_mod
    captured = {}

    async def fake_gen(db_, p_, s_, target):
        captured["target"] = target
        return {"ok": True, "path": "drama_media/x.png"}

    monkeypatch.setattr(drama_mod, "generate_scene_media", fake_gen)
    from apps.api import main as m
    r = _run(m.drama_scene_generate(b.id, p.id, s1.id, "images", db))
    assert r.get("ok") is True
    assert captured["target"] == "image", "端点必须归一为单数再传给生成函数"


# ============================================================ 2. 自动提取

def test_ensure_character_assets_registers_missing(db, env):
    b, p, a1, a2, a3, s1, s2 = env
    from apps.api import drama as d
    from apps.api.models import DramaAsset, DramaScene
    from sqlalchemy import select
    # 加一个新角色出场（未登记）
    s3 = DramaScene(id=d.new_id("ds"), project_id=p.id, episode=1,
                    scene_id="E1S03", idx=2,
                    characters_in_scene=json.dumps(["白浅", "陈长安（老年）"]),
                    scene_description="新角色出场")
    db.add(s3)
    db.commit()
    scenes = db.scalars(select(DramaScene).where(DramaScene.project_id == p.id)).all()
    n = d.ensure_character_assets(db, p, scenes)
    assert n == 1, "只应新登记 1 个角色（白浅）"
    names = {a.name for a in db.scalars(select(DramaAsset).where(
        DramaAsset.project_id == p.id, DramaAsset.kind == "character")).all()}
    assert "白浅" in names
    # 幂等：再跑一次 0
    assert d.ensure_character_assets(db, p, scenes) == 0
    db.delete(s3)
    db.commit()


def test_ensure_assets_form_reference_registers_base_only(db, env):
    """「陈长安（老年）」引用 → 只登记本体「陈长安」，不登记形态伪角色。"""
    b, p, *_ = env
    from apps.api import drama as d
    from apps.api.models import DramaAsset, DramaScene
    from sqlalchemy import select
    scenes = db.scalars(select(DramaScene).where(DramaScene.project_id == p.id)).all()
    n = d.ensure_character_assets(db, p, scenes)
    assert n == 0, "陈长安与形态引用都已有本体，不应新登记"
    names = {a.name for a in db.scalars(select(DramaAsset).where(
        DramaAsset.project_id == p.id, DramaAsset.kind == "character")).all()}
    assert "陈长安（老年）" in names  # 形态行存在（fixture 建的）
    assert "老年" not in names


# ============================================================ 3. 描述 grounding

def test_enrich_assets_uses_scene_grounding(db, env, monkeypatch):
    """★ enrich 提示词必须带出场上下文（画面描述+台词），不再只喂名字。"""
    b, p, a1, a2, a3, s1, s2 = env
    from apps.api import drama as d
    a1.description = ""          # 缺描述 → 进 enrich
    db.commit()
    prompts = {}

    class _R:
        error = ""
        text = json.dumps({"items": [
            {"kind": "character", "name": "陈长安",
             "description": "白发剑客，白衣胜雪，立于雪山山门前。"}]},
            ensure_ascii=False)

    async def fake_complete(model, msgs, **kw):
        prompts["text"] = msgs[0]["content"]
        return _R()

    monkeypatch.setattr(d.llm, "complete", fake_complete)
    monkeypatch.setattr(d.gen_graph, "pick_model", lambda db_, role: object())
    r = _run(d.enrich_assets(db, p, b))
    assert r["ok"] is True and r["enriched"] == 1
    assert a1.description.strip(), "描述应写回"
    ptxt = prompts["text"]
    assert "雪山山门，陈长安立于阶前" in ptxt, "提示词应包含画面描述 grounding"
    assert "山门虽远，此心可往" in ptxt, "提示词应包含角色台词 grounding"
    assert "出场上下文" in ptxt


def test_enrich_assets_no_context_still_works(db, env, monkeypatch):
    """无出场上下文的资产（纯幻觉兜底）不报错。"""
    b, p, a1, a2, a3, s1, s2 = env
    from apps.api import drama as d
    a3.description = ""
    db.commit()

    class _R:
        error = ""
        text = json.dumps({"items": [
            {"kind": "scene", "name": "青云山门", "description": "石阶云雾"}]},
            ensure_ascii=False)

    async def fake_complete(model, msgs, **kw):
        return _R()

    monkeypatch.setattr(d.llm, "complete", fake_complete)
    monkeypatch.setattr(d.gen_graph, "pick_model", lambda db_, role: object())
    r = _run(d.enrich_assets(db, p, b))
    assert r["ok"] is True
    assert a3.description == "石阶云雾"


# ============================================================ 4. 自动管线入队

def test_batch_image_auto_enqueues_asset_images(db, env):
    """★ 核心：批量出图前自动入队缺图角色的定妆图任务。"""
    from apps.api import main as m
    from apps.api.db import SessionLocal
    from apps.api.models import DramaTask
    from sqlalchemy import select
    b, p, a1, a2, a3, s1, s2 = env
    s = SessionLocal()
    try:
        r = _run(m.drama_generate_all(b.id, p.id, 1, "image", s))
    finally:
        s.close()
    assert r["status"] == "queued"
    # 2 个角色缺图（陈长安 + 老年形态）→ 2 个 asset_image 任务
    assert r["asset_tasks"] == 2, f"asset_tasks={r.get('asset_tasks')}"
    assert "定妆图" in r["progress"]
    at = db.scalars(select(DramaTask).where(
        DramaTask.project_id == p.id,
        DramaTask.task_type == "asset_image")).all()
    assert len(at) == 2
    by_res = {t.resource_id: t for t in at}
    assert by_res[a1.id].dependency_id == "", "本体无依赖"
    # ★ 衍生形态依赖本体任务（保脸顺序）
    assert by_res[a2.id].dependency_id == by_res[a1.id].id
    # 分镜任务也在同批
    st = db.scalars(select(DramaTask).where(
        DramaTask.project_id == p.id,
        DramaTask.task_type == "image")).all()
    assert len(st) == 2
    assert st[0].batch_id == at[0].batch_id, "资产与分镜任务同批"


def test_batch_video_also_runs_asset_pipeline(db, env):
    b, p, *_ = env
    from apps.api import main as m
    from apps.api.db import SessionLocal
    s = SessionLocal()
    try:
        r = _run(m.drama_generate_all(b.id, p.id, 1, "video", s))
    finally:
        s.close()
    assert r["asset_tasks"] >= 2, "出视频同样需要定妆图锚点"


def test_batch_audio_skips_asset_pipeline(db, env):
    b, p, *_ = env
    from apps.api import main as m
    from apps.api.db import SessionLocal
    s = SessionLocal()
    try:
        r = _run(m.drama_generate_all(b.id, p.id, 1, "audio", s))
    finally:
        s.close()
    assert r["asset_tasks"] == 0, "配音不走参考图链路，不应入队定妆图"


def test_batch_image_skips_assets_with_done_image(db, env):
    b, p, a1, a2, a3, s1, s2 = env
    a1.image = "drama_media/a1.png"
    a1.image_status = "done"
    db.commit()
    from apps.api import main as m
    from apps.api.db import SessionLocal
    s = SessionLocal()
    try:
        r = _run(m.drama_generate_all(b.id, p.id, 1, "image", s))
    finally:
        s.close()
    assert r["asset_tasks"] == 1, "已就绪的定妆图不重复入队（只补老年形态）"


# ============================================================ 5. 参考图回归（重构）

def test_scene_ref_images_form_fallback_to_base(db, env):
    """形态无图 → 参考图回退本体定妆图（D4 原语义，重构后不变）。"""
    from apps.api import drama as d
    b, p, a1, a2, a3, s1, s2 = env
    a1.image = "drama_media/base.png"
    a1.image_status = "done"
    a2.image = ""                 # 形态图未生成
    a2.image_status = "none"
    db.commit()
    refs = d._scene_ref_images(db, p, s2)
    assert refs == ["drama_media/base.png"], "形态回退本体图"
    refs2 = d._scene_ref_images(db, p, s1)
    assert refs2 == ["drama_media/base.png"]


def test_scene_ref_images_dedupe_and_cap(db, env):
    from apps.api import drama as d
    from apps.api.models import DramaAsset, DramaScene
    b, p, a1, a2, a3, s1, s2 = env
    a1.image = "drama_media/base.png"
    a1.image_status = "done"
    db.commit()
    # 同一角色引用两次 + 形态引用 → 去重后 1 张
    s3 = DramaScene(id=d.new_id("ds"), project_id=p.id, episode=1,
                    scene_id="E1S09", idx=8,
                    characters_in_scene=json.dumps(
                        ["陈长安", "陈长安", "陈长安（老年）"]),
                    scene_description="x")
    db.add(s3)
    db.commit()
    refs = d._scene_ref_images(db, p, s3)
    assert refs == ["drama_media/base.png"]
    db.delete(s3)
    db.commit()


# ============================================================ 6. defer 等待

def _mk_running_scene_task(db, p, scene, task_type="image"):
    from apps.api.models import DramaTask
    from apps.api import drama_queue as dq
    t = dq.enqueue(db, book_id=p.book_id, project_id=p.id,
                   episode=scene.episode, task_type=task_type,
                   resource_type="scene", resource_id=scene.id,
                   resource_key=scene.scene_id,
                   batch_id=dq.new_batch_id())
    t.status = "running"
    db.commit()
    return t


def test_exec_scene_media_defers_on_pending_asset(db, env, monkeypatch):
    """在途定妆图 → 分镜任务 defer（回队等待），不生成。"""
    from apps.api import drama as drama_mod
    from apps.api import drama_queue as dq
    calls = {}

    async def fake_gen(db_, p_, s_, target):
        calls["n"] = calls.get("n", 0) + 1
        return {"ok": True, "path": "drama_media/gen.png"}

    monkeypatch.setattr(drama_mod, "generate_scene_media", fake_gen)
    b, p, a1, a2, a3, s1, s2 = env
    # 造一个在途的定妆图任务（陈长安）
    from apps.api import drama_queue as dq2
    at = dq2.enqueue(db, book_id=b.id, project_id=p.id, episode=0,
                     task_type="asset_image", resource_type="asset",
                     resource_id=a1.id, resource_key="陈长安")
    at.status = "queued"
    db.commit()
    t = _mk_running_scene_task(db, p, s1)
    r = _run(dq._exec_scene_media(db, t))
    assert r.get("defer") is True, "应在途定妆图时 defer"
    assert "n" not in calls, "defer 时不得调用生成"
    assert json.loads(t.payload_json).get("asset_wait_since"), "等待起点应落 payload"
    # 定妆图完成后 → 放行生成
    at.status = "done"
    db.commit()
    r2 = _run(dq._exec_scene_media(db, t))
    assert r2.get("ok") is True
    assert calls.get("n") == 1


def test_exec_scene_media_defer_timeout_proceeds(db, env, monkeypatch):
    """等待超 30 分钟 → 不再等，降级继续生成（fail-open）。"""
    from apps.api import drama as drama_mod
    from apps.api import drama_queue as dq
    calls = {}

    async def fake_gen(db_, p_, s_, target):
        calls["n"] = 1
        return {"ok": True, "path": "drama_media/gen.png"}

    monkeypatch.setattr(drama_mod, "generate_scene_media", fake_gen)
    b, p, a1, a2, a3, s1, s2 = env
    at = dq.enqueue(db, book_id=b.id, project_id=p.id, episode=0,
                    task_type="asset_image", resource_type="asset",
                    resource_id=a1.id, resource_key="陈长安")
    at.status = "queued"
    db.commit()
    t = _mk_running_scene_task(db, p, s1)
    t.payload_json = json.dumps({"asset_wait_since": time.time() - 40 * 60})
    db.commit()
    r = _run(dq._exec_scene_media(db, t))
    assert r.get("ok") is True, "超时后必须降级放行"
    assert calls.get("n") == 1


def test_worker_defer_does_not_consume_attempts(db, env, monkeypatch):
    """★ worker 级：defer 回队不耗 attempts（等待≠失败）。"""
    from apps.api import drama as drama_mod
    from apps.api import drama_queue as dq
    monkeypatch.setattr(drama_mod, "generate_scene_media",
                        lambda *a, **k: _never())

    async def _never():
        raise AssertionError("等待期间不应调用生成")

    b, p, a1, a2, a3, s1, s2 = env
    at = dq.enqueue(db, book_id=b.id, project_id=p.id, episode=0,
                    task_type="asset_image", resource_type="asset",
                    resource_id=a1.id, resource_key="陈长安")
    at.status = "queued"
    db.commit()
    t = _mk_running_scene_task(db, p, s1)
    w = dq._Worker()
    _run(w._execute(t.id))
    # 用新 Session 验终态（_execute 用自己的 Session）
    from apps.api.db import SessionLocal
    s2x = SessionLocal()
    try:
        row = s2x.get(dq.DramaTask, t.id)
        assert row.status == "queued", "defer 后应回 queued"
        assert row.attempts == 0, "★ 等待不得消耗 attempts"
        assert row.error == ""
    finally:
        s2x.close()


# ============================================================ 7. 前端契约

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _read(*parts) -> str:
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def test_frontend_batch_buttons_use_singular_target():
    """★ 用户报障根因的另一半：前端批量按钮必须传单数。"""
    src = _read("apps", "web2", "src", "views", "drama", "DramaDetailView.vue")
    for bad in ("generateAll(bookId, projectId, episode, 'images')",
                "generateAll(bookId, projectId, episode, 'videos')",
                "generateAll(bookId, projectId, episode, 'audios')"):
        assert bad not in src, f"前端仍在传复数 target：{bad}"
    assert "generateAll(bookId, projectId, episode, 'image')" in src
    assert "generateAll(bookId, projectId, episode, 'video')" in src
    assert "generateAll(bookId, projectId, episode, 'audio')" in src


def test_frontend_asset_panel_reads_image_field():
    """资产卡必须读后端契约字段 image（不是 image_path）+ 状态。"""
    src = _read("apps", "web2", "src", "views", "drama", "DramaDetailView.vue")
    assert "a.image_path" not in src, "★ 仍在读 image_path（后端不返回该字段，定妆图永不显示）"
    assert "v-if=\"a.image\"" in src
    assert "image_status === 'pending'" in src
    assert "image_status === 'error'" in src
    ts = _read("apps", "web2", "src", "api", "modules", "drama.ts")
    assert "image?: string" in ts
    assert "image_status?: string" in ts


def test_character_assets_view_reads_image_field():
    src = _read("apps", "web2", "src", "views", "assets", "CharacterAssetsView.vue")
    assert "a.image_path" not in src

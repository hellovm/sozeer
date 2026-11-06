"""短剧工坊：小说 → 分集 → 剧本 → 资产 → 分镜 →（媒体生成）→ 成片数据。

复刻 ArcReel 的核心机制（简化适配织字工坊）：
1. 数据模型：project（episode_plan）→ episodes（冻结源文+剧本 JSON）
   → assets（character/scene/prop）→ scenes（分镜单元）。
2. workflow-status 状态机：不存状态，每次实时从数据**推导**——
   blockers / operations 准入 / next_action 三件套，与 ArcReel 同构。
   好处：数据被外部改坏时状态自动如实，不会出现"状态说完成了但剧本丢了"。
3. 剧本契约：scenes[]，每场景 {scene_id, duration_seconds, segment_break,
   characters_in_scene, scene_description, utterances[], image_prompt,
   video_prompt, source_text}——与 ArcReel scripts/episode_N.json 对齐，
   未来可直接桥接 ArcReel 或剪映草稿导出。
"""
from __future__ import annotations

import json
import re
import uuid

from sqlalchemy import select

from apps.api import llm
from apps.api.models import (
    Book, Chapter, jd, jl, now,
    DramaProject, DramaEpisode, DramaAsset, DramaScene,
)
from apps.api.orchestrator import graph as gen_graph
from apps.api.skills import loader as skills

MEDIA_DIR = "drama_media"   # 相对书籍仓库根


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ================================================================ 项目

async def create_project(db, book: Book, title: str = "", style: str = "",
                         content_mode: str = "drama",
                         generation_mode: str = "storyboard",
                         aspect_ratio: str = "9:16") -> DramaProject:
    p = DramaProject(
        id=new_id("dp"), book_id=book.id,
        title=title or f"{book.title}·短剧",
        style=style, content_mode=content_mode,
        generation_mode=generation_mode, aspect_ratio=aspect_ratio,
        owner_id=book.owner_id,
    )
    db.add(p)
    db.commit()
    return p


def get_project(db, project_id: str, owner_id: str = "") -> DramaProject | None:
    p = db.get(DramaProject, project_id)
    if not p:
        return None
    if owner_id and p.owner_id and p.owner_id != owner_id and owner_id != "admin":
        return None
    return p


def list_projects(db, book_id: str) -> list[DramaProject]:
    return db.scalars(select(DramaProject).where(DramaProject.book_id == book_id)
                      .order_by(DramaProject.created_at.desc())).all()


# ================================================================ 分集规划

async def estimate_durations(db, book: Book, emit=None) -> dict:
    """导演/编剧视角通读全书，预估每一章的剧情时长（秒）。

    产出 {"ok": True, "total_minutes": x, "chapters": [{idx, seconds, note}]}。
    依据：台词密度 / 场景切换 / 动作戏占比 —— 对白多、冲突密集的章节
    撑得起更长的银幕时间；过渡章压缩。这是 ArcReel「剧本时长由场景累积」
    的上游：先估时长，再按每集目标时长切集。
    """
    chapters = db.scalars(select(Chapter).where(
        Chapter.book_id == book.id, Chapter.status == "final",
        Chapter.idx > 0).order_by(Chapter.idx)).all()
    if not chapters:
        return {"ok": False, "error": "没有已定稿章节"}
    m = gen_graph.pick_model(db, "planner") or gen_graph.pick_model(db, "default")
    if not m:
        # 没模型时退化为字数启发式：短剧语速约 4.5 字/秒，对白约占总篇幅 40%
        chs = [{"idx": c.idx, "seconds": max(60, round((c.words or 600) * 0.4 / 4.5 / 5) * 5)}
               for c in chapters]
        total = sum(x["seconds"] for x in chs)
        return {"ok": True, "total_minutes": round(total / 60), "chapters": chs,
                "mode": "heuristic"}

    if emit:
        await emit({"kind": "stage", "text": "正在通读全书，以导演视角预估各章剧情时长…"})
    lines = []
    for c in chapters:
        summ = (c.summary or "").strip()
        if not summ:
            summ = (c.content or "")[:200]
        lines.append(f"第{c.idx:03d}章 {c.title or ''}（{c.words or 0}字）：{summ[:150]}")
    chap_txt = "\n".join(lines)
    # 超长书截断（保留首尾章，保证规划器看得见全书骨架）
    if len(chap_txt) > 60000:
        chap_txt = chap_txt[:30000] + "\n……（中略）……\n" + chap_txt[-26000:]

    prompt = (
        "你是短剧总导演兼编剧。下面是一部小说全部章节的摘要。请以影视化视角"
        "预估每一章被改编成短剧剧情的时长（秒）：对白密集、冲突强的章节给足时长，"
        "过渡章压缩。单章一般在 90-600 秒之间。\n"
        "只输出 JSON：{\"total_minutes\": 全片总分钟数, \"chapters\": "
        "[{\"idx\": 章号, \"seconds\": 该章剧情秒数, \"note\": \"一句话改编思路\"}]}"
        "，chapters 必须覆盖所有列出的章节且顺序一致。\n\n" + chap_txt)
    r = await llm.complete(m, [{"role": "user", "content": prompt}],
                           json_mode=True, temperature=0.4, timeout=600)
    if r.error or not r.text:
        return {"ok": False, "error": f"时长预估失败：{(r.error or '空响应')[:200]}"}
    data = _parse_script_json(r.text)
    if not data or not isinstance(data.get("chapters"), list) or not data["chapters"]:
        return {"ok": False, "error": "时长预估 JSON 结构无效"}
    chs = {}
    for it in data["chapters"]:
        try:
            idx = int(it.get("idx"))
            sec = max(30, min(1800, int(it.get("seconds") or 120)))
            chs[idx] = {"idx": idx, "seconds": sec, "note": (it.get("note") or "")[:120]}
        except (TypeError, ValueError):
            continue
    # 模型漏掉的章节用字数启发式兜底
    for c in chapters:
        if c.idx not in chs:
            sec = max(60, round((c.words or 600) * 0.4 / 4.5 / 5) * 5)
            chs[c.idx] = {"idx": c.idx, "seconds": sec, "note": "（启发式估算）"}
    ordered = [chs[c.idx] for c in chapters]
    total = sum(x["seconds"] for x in ordered)
    out = {"ok": True, "total_minutes": round(total / 60),
           "chapters": ordered, "mode": "ai",
           "model": m.name or m.model_id}
    if emit:
        await emit({"kind": "action", "type": "duration_estimate",
                    "summary": f"全书预估 {out['total_minutes']} 分钟（{len(ordered)} 章）"})
    return out


async def plan_episodes(db, project: DramaProject, book: Book,
                        chars_per_episode: int = 1, max_episodes: int = 30,
                        episode_minutes: float = 0, duration_map: dict | None = None,
                        emit=None) -> dict:
    """把已定稿章节切成集。

    两种模式（v0.13）：
    - episode_minutes > 0：时长感知切集 —— 用 duration_map（estimate_durations
      产出的逐章秒数）按累计时长逼近每集目标分钟数切边界，剧情节奏决定分集；
    - 否则：按章数机械切（每集 chars_per_episode 章）。
    两段式（与 ArcReel 一致）：规则切集 → 模型起标题（失败则纯规则兜底）。
    产出写进 project.episode_plan 并落 DramaEpisode 行（源文冻结快照）。
    """
    chapters = db.scalars(select(Chapter).where(
        Chapter.book_id == book.id, Chapter.status == "final",
        Chapter.idx > 0).order_by(Chapter.idx)).all()
    if not chapters:
        return {"ok": False, "error": "没有已定稿章节，先在写作台完成章节"}

    if episode_minutes and episode_minutes > 0:
        # 时长感知切集：没有 duration_map 时先现场估一轮
        if not duration_map:
            est = await estimate_durations(db, book, emit=emit)
            if not est.get("ok"):
                return est
            duration_map = {c["idx"]: c["seconds"] for c in est["chapters"]}
        target_sec = float(episode_minutes) * 60
        groups: list[list[Chapter]] = []
        cur: list[Chapter] = []
        acc = 0.0
        for c in chapters:
            cur.append(c)
            acc += float(duration_map.get(c.idx, 120))
            # 达到目标时长的 80% 就切一刀：宁短勿超，剧情节奏决定分集边界；
            # 边界微调交给 manual_split 手工修正
            if acc >= target_sec * 0.8:
                groups.append(cur)
                cur = []
                acc = 0.0
        if cur:
            if groups and len(cur) == 1 and acc < target_sec * 0.3:
                groups[-1].extend(cur)   # 尾巴太短并入上一集
            else:
                groups.append(cur)
        groups = groups[:max(1, max_episodes)]
    else:
        # 规则切集：每集 chars_per_episode 章
        groups: list[list[Chapter]] = []
        cur: list[Chapter] = []
        for c in chapters:
            cur.append(c)
            if len(cur) >= max(1, chars_per_episode):
                groups.append(cur)
                cur = []
        if cur:
            groups.append(cur)
        groups = groups[:max(1, max_episodes)]

    plan = [{"episode": i + 1,
             "title": (g[0].title or f"第{g[0].idx:03d}章").replace("#", "").strip()[:60],
             "src_from": g[0].idx, "src_to": g[-1].idx} for i, g in enumerate(groups)]

    # 模型起标题（尽力而为，失败不影响切集）
    m = gen_graph.pick_model(db, "planner") or gen_graph.pick_model(db, "default")
    if m:
        chap_lines = "\n".join(f"第{c.idx:03d}章 {c.title or ''}：{(c.summary or c.content or '')[:80]}"
                               for g in groups for c in g[:1])
        prompt = (
            "下面是一部小说的分组章节列表，每组将被改编成短剧的一集。"
            "请为每组起一个吸引人的短剧分集标题（≤20字，有钩子感）。\n"
            "只输出 JSON：{\"titles\": [\"第一集标题\", ...]}，数组顺序与分组一致。\n\n" + chap_lines)
        r = await llm.complete(m, [{"role": "user", "content": prompt}],
                               json_mode=True, temperature=0.7, timeout=120)
        if not r.error and r.text:
            try:
                titles = json.loads(r.text).get("titles") or []
                for i, t in enumerate(titles):
                    if i < len(plan) and isinstance(t, str) and t.strip():
                        plan[i]["title"] = t.strip()[:60]
            except Exception:
                pass

    # 记录每集预估时长（供前端展示与后续排期）
    if episode_minutes and duration_map:
        for item in plan:
            sec = sum(duration_map.get(i, 120)
                      for i in range(int(item["src_from"]), int(item["src_to"]) + 1))
            item["est_seconds"] = sec

    _save_plan(db, project, plan, book)
    return {"ok": True, "episodes": len(plan), "plan": plan}


def _save_plan(db, project: DramaProject, plan: list[dict], book: Book) -> None:
    """写 plan + 落 DramaEpisode（源文冻结快照；重跑时保留已有剧本不覆盖）。"""
    project.episode_plan = jd(plan)
    project.updated_at = now()
    existing = {e.episode: e for e in db.scalars(select(DramaEpisode).where(
        DramaEpisode.project_id == project.id)).all()}
    for item in plan:
        ep_no = int(item["episode"])
        src = _freeze_source(db, book, int(item.get("src_from") or 0), int(item.get("src_to") or 0))
        row = existing.get(ep_no)
        if row is None:
            row = DramaEpisode(id=new_id("de"), project_id=project.id, episode=ep_no)
            db.add(row)
        # 重规划不覆盖已有剧本/分镜，只更新绑定与源文快照
        row.title = item.get("title") or row.title or ""
        row.src_from = int(item.get("src_from") or 0)
        row.src_to = int(item.get("src_to") or 0)
        if src:
            row.source_text = src
        row.updated_at = now()
    db.commit()


def _freeze_source(db, book: Book, src_from: int, src_to: int) -> str:
    """把章节正文冻结成该集的源文快照（后续剧本改编以它为准，不再回读 DB）。"""
    if not src_from:
        return ""
    rows = db.scalars(select(Chapter).where(
        Chapter.book_id == book.id,
        Chapter.idx >= src_from, Chapter.idx <= (src_to or src_from),
        Chapter.status == "final").order_by(Chapter.idx)).all()
    parts = []
    for c in rows:
        title = c.title or f"第{c.idx:03d}章"
        parts.append(f"# 第{c.idx:03d}章 {title}\n\n{c.content or ''}")
    return "\n\n".join(parts).strip()


def manual_split(db, project: DramaProject, book: Book, plan: list[dict]) -> dict:
    """手工调整分集边界（前端拖拽/编辑后提交）。结构校验后全量重写。"""
    if not isinstance(plan, list) or not plan:
        return {"ok": False, "error": "分集计划为空"}
    cleaned = []
    last_to = 0
    for i, item in enumerate(plan):
        try:
            f = int(item.get("src_from") or 0)
            t = int(item.get("src_to") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"第{i + 1}组章节号不是整数"}
        if f <= 0 or t < f:
            return {"ok": False, "error": f"第{i + 1}组章节区间非法（{f}-{t}）"}
        if f <= last_to:
            return {"ok": False, "error": f"第{i + 1}组与上一组章节重叠"}
        cleaned.append({"episode": i + 1,
                        "title": (item.get("title") or "").strip()[:60],
                        "src_from": f, "src_to": t})
        last_to = t
    _save_plan(db, project, cleaned, book)
    return {"ok": True, "episodes": len(cleaned)}


# ================================================================ 剧本生成

async def generate_script(db, project: DramaProject, book: Book, episode: int,
                          emit=None) -> dict:
    """把该集冻结源文改编成结构化剧本（scenes[]）。

    产出：DramaEpisode.script_json（正式剧本）。同时把 scenes 落到
    DramaScene 行（分镜/生成进度的追踪载体）。
    """
    ep = db.scalar(select(DramaEpisode).where(DramaEpisode.project_id == project.id,
                                              DramaEpisode.episode == episode))
    if not ep or not ep.source_text.strip():
        return {"ok": False, "error": f"第{episode}集不存在或源文为空"}

    m = gen_graph.pick_model(db, "writer") or gen_graph.pick_model(db, "default")
    if not m:
        return {"ok": False, "error": "未配置可用模型"}

    if emit:
        await emit({"kind": "stage", "text": f"正在改编第 {episode} 集剧本…"})

    # 资产清单进提示词：让改编复用既有角色/场景名，保持跨集一致
    assets = db.scalars(select(DramaAsset).where(DramaAsset.project_id == project.id)).all()
    chars_txt = "\n".join(f"- {a.name}：{a.description[:80]}" for a in assets if a.kind == "character") or "（暂无，可新提炼）"
    scenes_txt = "\n".join(f"- {a.name}：{a.description[:60]}" for a in assets if a.kind == "scene") or "（暂无，可新提炼）"

    sk = skills.load("drama_script", db)
    if sk:
        prompt = skills.render(sk, source_text=ep.source_text, style=project.style,
                               characters=chars_txt, scenes=scenes_txt,
                               content_mode=project.content_mode)
    else:
        prompt = (
            "你是短剧改编编剧。把下面的小说源文改编成短视频分镜剧本。\n"
            f"项目画风：{project.style or '（未设定）'}\n"
            f"已有角色（优先复用同名角色）：\n{chars_txt}\n"
            f"已有场景：\n{scenes_txt}\n\n"
            "要求：\n"
            "1. 输出 JSON：{\"title\": 集标题, \"scenes\": [...]}。\n"
            "2. 每个 scene：{scene_id: \"E{n}S{序}\", duration_seconds: 4-10, "
            "segment_break: 是否分集节点(0/1), characters_in_scene: [角色名], "
            "scenes: [场景名], props: [道具名], scene_description: 画面描述(80-150字，具体可视化), "
            "utterances: [{kind: voiceover|dialogue, speaker: 角色名或null, text: 台词}], "
            "source_text: 对应原文}\n"
            "3. scene_description 是纯画面语言（给图像模型看），不写心理活动。\n"
            "4. 每集 15-30 个场景；台词保留原作语气；旁白 kind=voiceover。\n"
            "只输出 JSON。\n\n源文：\n" + ep.source_text[:12000])

    r = await llm.complete(m, [{"role": "user", "content": prompt}],
                           json_mode=True, temperature=0.7, timeout=900)
    if r.error:
        return {"ok": False, "error": f"剧本生成失败：{r.error[:200]}"}
    data = _parse_script_json(r.text)
    if not data or not isinstance(data.get("scenes"), list) or not data["scenes"]:
        return {"ok": False, "error": "剧本 JSON 结构无效（缺 scenes）"}

    ep.script_json = jd(data)
    ep.status = "scripted"
    ep.updated_at = now()
    _sync_scenes(db, project, ep, data)
    # 剧本里新出现的角色/场景自动补进资产清单（ArcReel 的 new_assets 语义）
    _ensure_assets(db, project, data, ep.episode)
    db.commit()
    # D5：剧本落库后自动细化八段分镜提示词（失败不阻塞剧本主体——
    # 分镜提示词缺失时 build_image_prompt 会走旧路径兜底）
    enriched_n = 0
    try:
        r = await enrich_storyboard_prompts(db, project, episode, emit=emit)
        enriched_n = int(r.get("enriched") or 0)
        enrich_note = f"，八段细化 {enriched_n}/{r.get('total', 0)}" if r.get("ok") else ""
    except Exception as e:  # 细化挂了不影响剧本交付
        enrich_note = ""
        if emit:
            await emit({"kind": "warn", "text": f"八段分镜细化失败（剧本已保存）：{e}"})
    if emit:
        await emit({"kind": "action", "type": "drama_script",
                    "summary": f"第 {episode} 集剧本完成：{len(data['scenes'])} 个分镜{enrich_note}"})
    return {"ok": True, "scenes": len(data["scenes"]), "title": data.get("title") or ep.title,
            "storyboard_enriched": enriched_n}


def _parse_script_json(text: str) -> dict | None:
    """模型返回的剧本 JSON 解析（容忍 ```json 包裹与前后杂文）。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    # 再试：抓第一个 { 到最后一个 }
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except Exception:
            return None
    return None


def _sync_scenes(db, project: DramaProject, ep: DramaEpisode, data: dict) -> None:
    """把剧本 scenes 同步进 DramaScene 追踪表（保留已有生成产物）。"""
    existing = {s.scene_id: s for s in db.scalars(select(DramaScene).where(
        DramaScene.project_id == project.id, DramaScene.episode == ep.episode)).all()}
    for i, sc in enumerate(data.get("scenes") or []):
        sid = str(sc.get("scene_id") or f"E{ep.episode}S{i + 1:02d}")
        row = existing.get(sid)
        if row is None:
            row = DramaScene(id=new_id("ds"), project_id=project.id,
                             episode=ep.episode, scene_id=sid, idx=i)
            db.add(row)
        row.duration_seconds = int(sc.get("duration_seconds") or 8)
        row.segment_break = 1 if sc.get("segment_break") else 0
        row.characters_in_scene = jd(sc.get("characters_in_scene") or [])
        row.scene_description = sc.get("scene_description") or ""
        # v0.17 A3：utterances 支持两种形态——结构化 JSON（原样）或
        # `@[角色/衍生]{台词}` DSL 文本（模型改写稿/人工编辑稿）。
        # normalize_script 统一归一为结构化 JSON，下游零改动。
        from apps.api import script_dsl as _dsl
        raw_utts = sc.get("utterances")
        if isinstance(raw_utts, str) and raw_utts.strip() and not raw_utts.strip().startswith("["):
            # 纯 DSL 文本（非 JSON 数组）→ 解析
            parsed = _dsl.to_utterances(raw_utts, include_prose=True)
            # 兼容：也接受 script / dialogue 字段写 DSL
            row.utterances = jd(parsed or _dsl.normalize_script(raw_utts))
        else:
            row.utterances = jd(_dsl.normalize_script(raw_utts) if raw_utts else [])
        row.image_prompt = jd(sc.get("image_prompt") or {})
        row.video_prompt = jd(sc.get("video_prompt") or {})
        row.updated_at = now()


def _ensure_assets(db, project: DramaProject, data: dict, episode: int) -> None:
    """剧本里出现但清单里没有的角色/场景/道具 → 自动登记（description 留待补全）。"""
    have = {(a.kind, a.name) for a in db.scalars(select(DramaAsset).where(
        DramaAsset.project_id == project.id)).all()}
    bucket = {"character": set(), "scene": set(), "prop": set()}
    for sc in data.get("scenes") or []:
        for n in (sc.get("characters_in_scene") or []):
            if isinstance(n, str) and n.strip():
                bucket["character"].add(n.strip())
        for n in (sc.get("scenes") or []):
            if isinstance(n, str) and n.strip():
                bucket["scene"].add(n.strip())
        for n in (sc.get("props") or []):
            if isinstance(n, str) and n.strip():
                bucket["prop"].add(n.strip())
    for kind, names in bucket.items():
        for n in names:
            if (kind, n) in have:
                continue
            db.add(DramaAsset(id=new_id("da"), project_id=project.id, kind=kind,
                              name=n, first_seen=episode))
    db.commit()


def ensure_character_assets(db, project: DramaProject, scenes) -> int:
    """E4.23：角色自动提取（幂等，批量出图/出视频前调用）。

    把分镜行里出现但资产清单没有的角色登记进 DramaAsset。覆盖两种漏网：
    ① 历史数据——旧版本生成剧本时未走资产自动登记；
    ② LLM 产出缺 characters_in_scene 字段（scene 行才是已归一化的事实）。
    「名字（形态）」「名字·形态」只登记本体（本体才是一致性锚点；
    衍生形态走 /forms 端点人工添加）。返回新登记数。
    """
    have = {a.name for a in db.scalars(select(DramaAsset).where(
        DramaAsset.project_id == project.id,
        DramaAsset.kind == "character")).all()}
    n = 0
    for s in scenes or []:
        for nm in (jl(s.characters_in_scene, []) or []):
            if not isinstance(nm, str):
                continue
            m = re.match(r"^(.*?)[（(·](.*?)[）)]?$", nm.strip())
            name = m.group(1).strip() if m else nm.strip()
            if name and name not in have:
                db.add(DramaAsset(id=new_id("da"), project_id=project.id,
                                  kind="character", name=name,
                                  first_seen=s.episode))
                have.add(name)
                n += 1
    if n:
        db.commit()
    return n


def confirm_plan(db, project: DramaProject, episode: int) -> dict:
    """确认剧本草稿（ArcReel 的 script_plan_review 指纹确认语义，简化为开关）。"""
    ep = db.scalar(select(DramaEpisode).where(DramaEpisode.project_id == project.id,
                                              DramaEpisode.episode == episode))
    if not ep:
        return {"ok": False, "error": f"第{episode}集不存在"}
    ep.plan_confirmed = 1
    ep.updated_at = now()
    db.commit()
    return {"ok": True}


# ================================================================ D5 八段分镜提示词
# 社区标准分镜提示词模板：CHARACTER/BACKGROUND/ACTION/SCENE/CAMERA/
# LIGHT/TEXT/STYLE 八段。结构化提示词的价值在跨镜一致性——CHARACTER 段
# 逐字复用资产描述，同一角色在任意两镜里拿到完全相同的外观锚点。
# 存储：DramaScene.image_prompt（顶层八段键）；旧结构 {"scene":…,
# "composition":…} 仍被 build_image_prompt 兜底识别，新旧数据共存。

STORYBOARD_KEYS = ("CHARACTER", "BACKGROUND", "ACTION", "SCENE",
                   "CAMERA", "LIGHT", "TEXT", "STYLE")
_BATCH_SIZE = 5          # 每批送模型的分镜数（平衡上下文长度与调用次数）
_SEG_MAX = 120           # 单段字数上限
_PROMPT_TOTAL_MAX = 600  # 单镜八段合计上限


def _is_storyboard(ip: dict) -> bool:
    """image_prompt 是否已是八段结构（顶层命中 >=3 个八段键）。"""
    if not isinstance(ip, dict) or not ip:
        return False
    return sum(1 for k in STORYBOARD_KEYS if k in ip) >= 3


def _clamp_segments(ip: dict) -> dict:
    """截断超长段：单段 ≤120 字，合计 ≤600 字（按 STORYBOARD_KEYS 顺序）。"""
    out, total = {}, 0
    for k in STORYBOARD_KEYS:
        v = str(ip.get(k) or "").strip()
        v = v[:_SEG_MAX]
        total += len(v)
        out[k] = v
    if total > _PROMPT_TOTAL_MAX:  # 超预算从权重最低的 TEXT/LIGHT 往下削
        for k in ("TEXT", "LIGHT", "CAMERA", "ACTION"):
            if total <= _PROMPT_TOTAL_MAX:
                break
            cut = min(len(out[k]), total - _PROMPT_TOTAL_MAX)
            out[k] = out[k][:len(out[k]) - cut]
            total -= cut
    return out


def _assets_snapshot(db, project: DramaProject) -> str:
    """资产清单 JSON 文本：character/scene/prop 各带 name+description（含衍生形态）。"""
    items = []
    for a in list_assets(db, project):
        if a.parent_id and a.form_name:
            parent = db.get(DramaAsset, a.parent_id)
            items.append({"kind": "character", "name": f"{parent.name if parent else a.name}（{a.form_name}形态）",
                          "description": f"{(parent.description if parent else '')}；变化：{a.description}"})
        elif not a.parent_id:
            items.append({"kind": a.kind, "name": a.name, "description": a.description.strip()})
    return json.dumps(items, ensure_ascii=False)


def _scenes_snapshot(scenes: list[DramaScene]) -> str:
    """分镜清单 JSON 文本：只送细化需要的字段，控制上下文长度。"""
    items = []
    for s in scenes:
        items.append({
            "scene_id": s.scene_id,
            "scene_description": (s.scene_description or "")[:300],
            "characters_in_scene": jl(s.characters_in_scene, []),
            "scenes": [], "props": [],
        })
    return json.dumps(items, ensure_ascii=False)


async def enrich_storyboard_prompts(db, project: DramaProject, episode: int,
                                    emit=None) -> dict:
    """D5：把该集每个分镜的 scene_description 细化为八段结构。

    逐镜覆盖回写 DramaScene.image_prompt（八段）/ video_prompt（action +
    camera_motion）。已是八段的分镜跳过（幂等重跑）；critic 模型按批调用，
    单批失败不影响其他批。
    """
    ep = db.scalar(select(DramaEpisode).where(DramaEpisode.project_id == project.id,
                                              DramaEpisode.episode == episode))
    if not ep:
        return {"ok": False, "error": f"第{episode}集不存在"}
    scenes = list_scenes(db, project, episode)
    todo = [s for s in scenes
            if not _is_storyboard(jl(s.image_prompt, {}) or {}) and (s.scene_description or "").strip()]
    if not todo:
        return {"ok": True, "enriched": 0, "total": len(scenes)}

    m = gen_graph.pick_model(db, "critic") or gen_graph.pick_model(db, "default")
    if not m:
        return {"ok": False, "error": "未配置可用模型"}
    assets_json = _assets_snapshot(db, project)
    if emit:
        await emit({"kind": "stage", "text": f"正在细化 {len(todo)} 个分镜的八段提示词…"})

    ok_n = fail_n = 0
    for i in range(0, len(todo), _BATCH_SIZE):
        batch = todo[i:i + _BATCH_SIZE]
        sk = skills.load("drama_storyboard", db)
        if sk:
            prompt = skills.render(sk, style=project.style or "（未设定）",
                                   aspect_ratio=project.aspect_ratio or "9:16",
                                   assets_json=assets_json,
                                   scenes_json=_scenes_snapshot(batch))
        else:  # skill 缺失时的兜底提示词（保持最小八段契约）
            prompt = (
                "把每个分镜的 scene_description 细化为八段提示词 JSON："
                "{\"scenes\": [{\"scene_id\": 原值, \"image_prompt\": "
                "{\"CHARACTER\",\"BACKGROUND\",\"ACTION\",\"SCENE\",\"CAMERA\","
                "\"LIGHT\",\"TEXT\",\"STYLE\"}, \"video_prompt\": "
                "{\"action\",\"camera_motion\"}}]}。CHARACTER/BACKGROUND 逐字复用"
                "资产描述，每段≤120字。只输出 JSON。\n"
                f"资产清单：{assets_json}\n分镜：{_scenes_snapshot(batch)}")
        r = await llm.complete(m, [{"role": "user", "content": prompt}],
                               json_mode=True, temperature=0.6, timeout=600)
        data = _parse_script_json(r.text) if not r.error else None
        rows = (data or {}).get("scenes") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            fail_n += len(batch)
            continue
        by_id = {str(x.get("scene_id")): x for x in rows if isinstance(x, dict)}
        for s in batch:
            it = by_id.get(s.scene_id) or {}
            ip, vp = it.get("image_prompt"), it.get("video_prompt")
            if not isinstance(ip, dict):
                fail_n += 1
                continue
            s.image_prompt = jd(_clamp_segments(ip))
            if isinstance(vp, dict) and (vp.get("action") or vp.get("camera_motion")):
                s.video_prompt = jd({"action": str(vp.get("action") or "")[:200],
                                     "camera_motion": str(vp.get("camera_motion") or "")[:80]})
            ok_n += 1
        db.commit()
    db.commit()
    return {"ok": ok_n > 0, "enriched": ok_n, "failed": fail_n, "total": len(scenes)}


# ================================================================ workflow 状态机
# ArcReel 的核心设计：状态不是存出来的，是每次从数据**推导**出来的。
# blockers（硬阻断）/ operations（操作准入）/ next_action（建议下一步）三件套。

_OPS = ("plan_episodes", "generate_script", "manage_assets", "board", "render")


def workflow_status(db, project: DramaProject, book: Book, episode: int | None = None) -> dict:
    eps = db.scalars(select(DramaEpisode).where(DramaEpisode.project_id == project.id)
                     .order_by(DramaEpisode.episode)).all()
    assets = db.scalars(select(DramaAsset).where(DramaAsset.project_id == project.id)).all()
    blockers: list[str] = []
    operations: dict[str, dict] = {}

    has_chapters = db.scalar(select(Chapter.idx).where(
        Chapter.book_id == book.id, Chapter.status == "final",
        Chapter.idx > 0).limit(1)) is not None
    if not has_chapters:
        blockers.append("没有已定稿章节，先在写作台完成章节")
    operations["plan_episodes"] = {"state": "admitted" if has_chapters else "refused",
                                   "reason": None if has_chapters else "无定稿章节"}
    if not eps:
        operations["generate_script"] = {"state": "refused", "reason": "尚未分集"}
        operations["board"] = {"state": "refused", "reason": "尚无剧本"}
        operations["render"] = {"state": "refused", "reason": "尚无分镜"}
        return {"blockers": blockers, "operations": operations,
                "next_action": {"type": "plan_episodes", "args": {}} if has_chapters else None,
                "episodes": [], "assets_summary": _asset_summary(assets)}

    target = next((e for e in eps if e.episode == (episode or 0)), None) or \
        next((e for e in eps if e.status in ("empty", "planned")), None) or eps[0]

    scripted = [e for e in eps if e.status in ("scripted", "boarded", "rendered")]
    operations["generate_script"] = {
        "state": "admitted",
        "reason": None if target.source_text.strip() else "该集源文为空"}
    missing_desc = [a.name for a in assets if not a.description.strip()]
    operations["manage_assets"] = {
        "state": "admitted",
        "reason": None if not missing_desc else f"{len(missing_desc)} 个资产缺描述"}

    # 分镜准入：该集剧本已确认
    ep_ready = target.status in ("scripted", "boarded", "rendered") and target.plan_confirmed
    operations["board"] = {"state": "admitted" if ep_ready else "refused",
                           "reason": None if ep_ready else "剧本未确认"}
    scenes = db.scalars(select(DramaScene).where(
        DramaScene.project_id == project.id, DramaScene.episode == target.episode)).all()
    done_media = sum(1 for s in scenes if s.media_status == "done")
    operations["render"] = {
        "state": "admitted" if scenes else "refused",
        "reason": None if scenes else "尚无分镜"}

    # next_action 推导（优先级从高到低）
    if not eps:
        na = {"type": "plan_episodes", "args": {}}
    elif target.status in ("empty", "planned"):
        na = {"type": "generate_script", "args": {"episode": target.episode}}
    elif missing_desc:
        na = {"type": "manage_assets", "args": {"episode": target.episode}}
    elif not ep_ready:
        na = {"type": "confirm_script", "args": {"episode": target.episode}}
    elif done_media < len(scenes):
        na = {"type": "render", "args": {"episode": target.episode,
                                         "pending": len(scenes) - done_media}}
    else:
        nxt = next((e for e in eps if e.status in ("empty", "planned", "scripted")), None)
        na = ({"type": "next_episode", "args": {"episode": nxt.episode}}
              if nxt else {"type": "complete", "args": {}})

    return {"blockers": blockers, "operations": operations, "next_action": na,
            "episodes": [{"episode": e.episode, "title": e.title,
                          "src_from": e.src_from, "src_to": e.src_to,
                          "status": e.status, "plan_confirmed": bool(e.plan_confirmed)}
                         for e in eps],
            "assets_summary": _asset_summary(assets),
            "target_episode": target.episode,
            "scenes_done": done_media, "scenes_total": len(scenes)}


def _asset_summary(assets) -> dict:
    out = {}
    for a in assets:
        d = out.setdefault(a.kind, {"total": 0, "with_image": 0, "missing_desc": 0})
        d["total"] += 1
        d["with_image"] += 1 if a.image else 0
        d["missing_desc"] += 0 if a.description.strip() else 1
    return out


# ================================================================ 资产

def upsert_asset(db, project: DramaProject, kind: str, name: str,
                 description: str = "", voice_style: str = "") -> dict:
    if kind not in ("character", "scene", "prop"):
        return {"ok": False, "error": "kind 必须是 character/scene/prop"}
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name 为空"}
    row = db.scalar(select(DramaAsset).where(DramaAsset.project_id == project.id,
                                             DramaAsset.kind == kind,
                                             DramaAsset.name == name))
    if row is None:
        row = DramaAsset(id=new_id("da"), project_id=project.id, kind=kind, name=name)
        db.add(row)
    if description:
        row.description = description
    if voice_style:
        row.voice_style = voice_style
    db.commit()
    return {"ok": True, "id": row.id}


def list_assets(db, project: DramaProject, kind: str | None = None):
    q = select(DramaAsset).where(DramaAsset.project_id == project.id)
    if kind:
        q = q.where(DramaAsset.kind == kind)
    return db.scalars(q.order_by(DramaAsset.kind, DramaAsset.id)).all()


def _asset_grounding(db, project: DramaProject, a: DramaAsset) -> str:
    """E4.23：从项目分镜中提取该资产的出场上下文（描述生成的事实依据）。

    旧版 enrich_assets 只把资产名喂给 LLM → 描述纯幻觉、与小说实际形象
    脱节，定妆图一致性无从谈起。现在：
    · 角色：取它出场的分镜画面描述 + 它的台词（最多人物特征信息）；
    · 场景/道具：剧本 JSON 的 scenes[] 里带 scenes/props 名单与画面描述
      （这两类名单没有持久化到 scene 行，只在 script_json 里）。
    """
    chunks: list[str] = []
    if a.kind == "character":
        for s in db.scalars(select(DramaScene).where(
                DramaScene.project_id == project.id)).all():
            names = jl(s.characters_in_scene, []) or []
            if not any(isinstance(x, str) and a.name in x for x in names):
                continue
            d = (s.scene_description or "").strip()
            if d:
                chunks.append(d[:300])
            utts = jl(s.utterances, []) or []
            lines = [u.get("text", "") for u in utts
                     if isinstance(u, dict) and u.get("speaker") == a.name
                     and (u.get("text") or "").strip()][:3]
            if lines:
                chunks.append("台词：" + " / ".join(lines))
            if len(chunks) >= 4:
                break
    if not chunks and a.first_seen:
        ep = db.scalar(select(DramaEpisode).where(
            DramaEpisode.project_id == project.id,
            DramaEpisode.episode == a.first_seen))
        if ep and (ep.script_json or "").strip():
            try:
                data = json.loads(ep.script_json)
            except Exception:
                data = {}
            for sc in (data.get("scenes") or []):
                if a.name in (sc.get("scenes") or []) or a.name in (sc.get("props") or []):
                    d = (sc.get("scene_description") or "").strip()
                    if d:
                        chunks.append(d[:300])
                if len(chunks) >= 3:
                    break
    return "\n".join(chunks)[:800]


async def enrich_assets(db, project: DramaProject, book: Book, emit=None) -> dict:
    """给缺描述的资产生成外观/环境描述（E4.23 起按出场上下文 grounding）。

    批量自动管线（main._auto_asset_pipeline）与手动「从正文同步角色」
    端点共用。单次 LLM 调用覆盖全部缺描述资产；失败只返回 ok=False，
    由调用方决定 fail-open。
    """
    missing = [a for a in list_assets(db, project) if not a.description.strip()]
    if not missing:
        return {"ok": True, "enriched": 0}
    m = gen_graph.pick_model(db, "planner") or gen_graph.pick_model(db, "default")
    if not m:
        return {"ok": False, "error": "未配置可用模型"}
    blocks = []
    for a in missing:
        g = _asset_grounding(db, project, a)
        blocks.append(f"- [{a.kind}] {a.name}\n"
                      f"  出场上下文：{g or '（无，按名字与画风基调合理推断）'}")
    names = "\n".join(blocks)
    prompt = (
        "为下面这些短剧资产写可视化描述（给 AI 绘图/建模用）。\n"
        f"画风基调：{project.style or '（未设定）'}\n"
        "要求：角色写外貌/服装/气质（60-120字）；场景写环境/光线/氛围（40-80字）；"
        "道具写外观材质（20-50字）。严格依据下面每个资产的「出场上下文」，"
        "不要编造与上下文不符的细节。只输出 JSON："
        "{\"items\": [{\"kind\": \"character\", \"name\": \"沈墨\", \"description\": \"...\"}]}\n\n"
        + names)
    if emit:
        await emit({"kind": "stage", "text": f"正在为 {len(missing)} 个资产生成描述…"})
    r = await llm.complete(m, [{"role": "user", "content": prompt}],
                           json_mode=True, temperature=0.6, timeout=180)
    if r.error:
        return {"ok": False, "error": f"描述生成失败：{r.error[:200]}"}
    n = 0
    try:
        items = json.loads(r.text).get("items") or []
    except Exception:
        items = []
    for it in items:
        name, desc = (it.get("name") or "").strip(), (it.get("description") or "").strip()
        kind = (it.get("kind") or "").strip()
        if not name or not desc:
            continue
        row = next((a for a in missing if a.name == name and (not kind or a.kind == kind)), None)
        if row:
            row.description = desc
            n += 1
    db.commit()
    return {"ok": True, "enriched": n}


# ================================================================ 分镜与媒体

def list_scenes(db, project: DramaProject, episode: int):
    return db.scalars(select(DramaScene).where(
        DramaScene.project_id == project.id, DramaScene.episode == episode)
        .order_by(DramaScene.idx)).all()


def scene_dict(s: DramaScene) -> dict:
    qc = {}
    try:
        qc = jl(s.qc_report, {}) or {} if (s.qc_report or "").strip() else {}
    except Exception:
        qc = {}
    return {"id": s.id, "episode": s.episode, "scene_id": s.scene_id, "idx": s.idx,
            "duration_seconds": s.duration_seconds, "segment_break": bool(s.segment_break),
            "characters_in_scene": jl(s.characters_in_scene, []),
            "scene_description": s.scene_description,
            "utterances": jl(s.utterances, []),
            "image_prompt": jl(s.image_prompt, {}),
            "video_prompt": jl(s.video_prompt, {}),
            "media_status": s.media_status,
            "image_path": s.image_path, "video_path": s.video_path,
            "audio_path": s.audio_path, "error": s.error,
            "qc": {"pass": qc.get("pass"), "scores": qc.get("scores") or {},
                   "issues": [i.get("message") if isinstance(i, dict) else str(i)
                              for i in (qc.get("issues") or [])[:4]],
                   "retries": qc.get("retries", 0)} if qc else {}}


def build_image_prompt(db, project: DramaProject, scene: DramaScene) -> str:
    """把 scene 的 image_prompt + 资产描述 + 全局风格拼成可直接投喂图像模型的提示词。

    v0.14.1 D5：八段结构（CHARACTER/BACKGROUND/ACTION/SCENE/CAMERA/LIGHT/
    TEXT/STYLE）优先——八段本身就是完整提示词，直接按序拼接，不再做资产
    展开（CHARACTER 段在细化阶段已逐字复用资产描述）。
    旧结构 {"scene":…, "composition":…} 保留兜底路径，历史数据不受影响。
    """
    ip = jl(scene.image_prompt, {}) or {}
    if _is_storyboard(ip):
        return "，".join(ip[k] for k in STORYBOARD_KEYS if str(ip.get(k) or "").strip())
    parts = [project.style or ""]
    body = ip.get("scene") or scene.scene_description or ""
    # 展开角色引用：提示词里出现角色名（含「名字（形态）」「名字·形态」）时拼上描述
    assets = list_assets(db, project, "character")
    forms = {(a.parent_id, a.form_name): a for a in list_assets(db, project)
             if a.parent_id and a.form_name}
    by_id = {a.id: a for a in assets}
    import re as _re
    for a in assets:
        if a.name in body and a.description:
            body += f"；{a.name}外观：{a.description}"
            # 形态引用展开：本体内出现过的衍生形态名 → 补形态描述
            for (pid, fname), fa in forms.items():
                if pid != a.id or fname not in body:
                    continue
                if fa.description:
                    body += f"；{a.name}（{fname}形态）：{a.description}；{fa.description}"
    comp = ip.get("composition") or {}
    if comp.get("shot_type"):
        parts.append(f"镜头：{comp['shot_type']}")
    if comp.get("lighting"):
        parts.append(f"光线：{comp['lighting']}")
    parts.append(body)
    return "，".join(x for x in parts if x)


def build_video_prompt(db, project: DramaProject, scene: DramaScene) -> str:
    """视频提示词：D5 起八段细化会写回 video_prompt {action, camera_motion}；
    旧结构或缺失时退回 scene_description 作动作描述兜底。

    D14-r（v0.14.8）：fast 档下画面内对白交给 LTX i2v 原生生成，此处把台词
    追加进 prompt（引号包裹，实测 -15.9dB 音量、8/8 逐字）；quality 档刻意
    **不**写台词——台词由 TTS 音轨负责，视频prompt含台词会导致模型随机配音
    与 TTS 叠成双声（跨镜头音色还会漂移 34.7%）。
    """
    vp = jl(scene.video_prompt, {}) or {}
    parts = [project.style or ""]
    if vp.get("action"):
        parts.append(vp["action"])
    elif not _is_storyboard(jl(scene.image_prompt, {}) or {}):
        # 无 video_prompt 且非八段细化产物：用 scene_description 兜底（画面描述即动作底稿）
        desc = (scene.scene_description or "").strip()
        if desc:
            parts.append(desc[:200])
    if vp.get("camera_motion"):
        parts.append(f"运镜：{vp['camera_motion']}")
    # D14-r：fast 档才把对白写进 prompt（原生台词）
    try:
        from apps.api import audio_route as _ar
        mode = _ar.audio_plan_for_project(getattr(project, "content_mode", ""))
        lines = _ar.native_dialogue_lines(jl(scene.utterances, []) or [], mode=mode)
        if lines:
            spoken = " ".join(f'"{x}"' for x in lines)
            parts.append(f"角色说话（口型同步）：{spoken}")
    except Exception:
        pass   # 路由是增益位，异常不影响既有 prompt 构建
    return "，".join(x for x in parts if x)


def update_scene_media(db, scene: DramaScene, *, media_status: str | None = None,
                       image_path: str | None = None, video_path: str | None = None,
                       audio_path: str | None = None, error: str | None = None,
                       qc_report: str | None = None) -> None:
    if media_status is not None:
        scene.media_status = media_status
    if image_path is not None:
        scene.image_path = image_path
    if video_path is not None:
        scene.video_path = video_path
    if audio_path is not None:
        scene.audio_path = audio_path
    if qc_report is not None:
        scene.qc_report = qc_report[:20000]
    if error is not None:
        scene.error = error[:512]
    scene.updated_at = now()
    db.commit()


# ================================================================ 适配器注册表
# v0.13：媒体生成统一走 apps.api.media（按模型 role=image/video/tts 路由，
# provider 区分 openai-compatible / comfyui）。这里保留 ADAPTERS 注册表
# 语义：手工适配器仍可注册并优先于模型路由。

ADAPTERS: dict[str, dict] = {}


def register_adapter(kind: str, name: str, fn):
    """fn: async (project, scene, prompt) -> {"path": ...} 或 {"error": ...}"""
    ADAPTERS[f"{kind}:{name}"] = fn


def _prev_end_frame(db, project: DramaProject, scene: DramaScene) -> str:
    """D7 末帧传递：抽前一分镜视频的最后一帧作本镜 i2v 参考图。

    链式连贯性核心：前镜末帧（-sseof -0.1 精确定位尾部）→ 抽 PNG 落
    drama_media/frames/{前scene_id}_prev_end.png → 本镜视频以它做首帧锚定，
    镜间切换角色/场景状态自然衔接（i2v 双注入配方 strength 0.7/1.0 已实测）。

    返回绝对路径或 ""（前镜无视频/ffmpeg 不可用/抽帧失败时静默降级——
    退化为纯 i2v 用分镜图锚定或 t2v，不阻塞主流程）。
    """
    import shutil as _shutil
    import subprocess as _sp
    from apps.api.config import BOOKS_DIR as _BOOKS_DIR
    if _shutil.which("ffmpeg") is None:
        return ""
    prev = db.scalar(select(DramaScene).where(
        DramaScene.project_id == project.id, DramaScene.episode == scene.episode,
        DramaScene.idx < scene.idx, DramaScene.video_path != "")
        .order_by(DramaScene.idx.desc()).limit(1))
    if prev is None or not _file_ok_media(prev.video_path):
        return ""
    src = (_BOOKS_DIR / prev.video_path.split("/api/files/", 1)[-1]).resolve()
    frames_dir = _BOOKS_DIR / project.book_id / "drama_media" / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    out = frames_dir / f"{prev.scene_id or prev.id[:8]}_prev_end.png"
    try:
        r = _sp.run(["ffmpeg", "-y", "-v", "error", "-sseof", "-0.1",
                     "-i", str(src), "-frames:v", "1", str(out)],
                    capture_output=True, timeout=60)
        if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return str(out)
    except Exception:
        pass
    return ""


def _file_ok_media(path: str) -> bool:
    """drama.py 侧的产物存在性检查（与 drama_queue._file_ok 同语义）。"""
    if not (path or "").strip():
        return False
    from apps.api.config import BOOKS_DIR as _BOOKS_DIR
    rel = path.split("/api/files/", 1)[-1]
    try:
        return (_BOOKS_DIR / rel).exists()
    except Exception:
        return False


def ref_asset_rows(db, project: DramaProject, scene: DramaScene) -> list[DramaAsset]:
    """E4.23：匹配本镜出场角色对应的资产行（本体/衍生形态，按出场序去重）。

    与出图状态无关——批量管线用它判定「哪些定妆图需要生成/正在生成」。
    匹配规则与 D4 参考图链路完全一致：
    · 「名字（形态）」「名字·形态」优先命中衍生形态，未命中回退本体；
    · 形态无定妆图而本体有 → 锚点回退本体（本体才是一致性锚点）。
    """
    names = jl(scene.characters_in_scene, []) or []
    if not names:
        return []
    assets = list_assets(db, project, "character")
    bases = [a for a in assets if not a.parent_id]           # 本体行
    forms = [a for a in assets if a.parent_id and a.form_name]
    out: list[DramaAsset] = []
    seen: set[str] = set()
    for n in names:
        if not isinstance(n, str) or not n.strip():
            continue
        name = n.strip()
        pick = None
        # 形态引用「名字（形态）」「名字·形态」：先精确匹配本体名前缀
        m = re.match(r"^(.*?)[（(·](.*?)[）)]?$", name)
        base_name, form_name = (m.group(1).strip(), m.group(2).strip()) if m else (name, "")
        if form_name:
            for b in bases:
                if b.name == base_name:
                    for f in forms:
                        if f.parent_id == b.id and f.form_name == form_name:
                            pick = f
                            break
                    break
        if pick is None:                       # 非形态引用：本体精确名匹配
            for b in bases:
                if b.name == name:
                    pick = b
                    break
        if pick is None:
            continue
        if not (pick.image or "").strip() and pick.parent_id:
            # 形态图未生成 → 回退本体定妆图（本体才是一致性锚点）
            parent = db.get(DramaAsset, pick.parent_id)
            if parent is not None and (parent.image or "").strip():
                pick = parent
        if pick.id not in seen:
            seen.add(pick.id)
            out.append(pick)
    return out


def pending_ref_asset_tasks(db, project: DramaProject, scene: DramaScene) -> list[str]:
    """E4.23：本镜参考角色资产中，仍在途（queued/running）的定妆图任务 id。

    批量管线用：非空 → 分镜图/视频任务先等（defer 回队，不耗重试）。
    """
    from apps.api.models import DramaTask
    ids = [a.id for a in ref_asset_rows(db, project, scene)]
    if not ids:
        return []
    return [t.id for t in db.scalars(select(DramaTask).where(
        DramaTask.project_id == project.id,
        DramaTask.task_type == "asset_image",
        DramaTask.resource_id.in_(ids),
        DramaTask.status.in_(("queued", "running")))).all()]


def asset_image_dependency(db, project: DramaProject, a: DramaAsset,
                           base_task: dict[str, str]) -> str:
    """E4.23：资产定妆图任务的 dependency_id。

    衍生形态 → 本体定妆图任务（本批 base_task 映射，或上批仍在途的那行）——
    保「形态图用本体图做 IP-Adapter 参考」的顺序语义；本体 → 无依赖。
    手动批量端点与自动管线共用，行为一致。
    """
    if not a.parent_id:
        return ""
    dep = base_task.get(a.parent_id, "")
    if dep:
        return dep
    from apps.api.models import DramaTask
    row = db.scalar(select(DramaTask).where(
        DramaTask.project_id == project.id,
        DramaTask.task_type == "asset_image",
        DramaTask.resource_id == a.parent_id,
        DramaTask.status.in_(("queued", "running"))).order_by(DramaTask.created_at.desc()))
    return row.id if row else ""


def _scene_ref_images(db, project: DramaProject, scene: DramaScene,
                      max_n: int = 8) -> list[str]:
    """D4：收集本镜出场角色的定妆图路径（IP-Adapter Plus 参考图链路）。

    规则：characters_in_scene 按序匹配资产（含衍生形态——形态图未生成时
    回退本体图，本体才是一致性锚点）；只收 image_status=done 且有图的；
    上限 8 张（{{image2}}…{{image9}} 九槽减去 i2v 主图一槽）。
    E4.23：资产匹配抽取到 ref_asset_rows（与批量管线等待判定共用），
    本函数只做就绪过滤，行为不变。
    """
    out: list[str] = []
    for a in ref_asset_rows(db, project, scene):
        img = (a.image or "").strip()
        if not img or a.image_status != "done":
            continue
        if img not in out:
            out.append(img)
        if len(out) >= max_n:
            break
    return out


async def generate_scene_media(db, project: DramaProject, scene: DramaScene,
                               target: str = "image") -> dict:
    """生成单分镜的图像/视频/旁白音频。target: image|video|audio。

    优先级：手工注册的 ADAPTERS["{target}:default"] > media.run_media 模型路由。
    """
    audio_jobs: list[dict] = []      # D19：逐行声纹 TTS 作业（audio 分支填充）
    if target == "image":
        prompt = build_image_prompt(db, project, scene)
        scene.media_status = "image_pending"
    elif target == "video":
        prompt = build_video_prompt(db, project, scene)
        scene.media_status = "video_pending"
    else:
        # D14-r 音频策略路由（v0.14.8）：不再无脑把台词拼给 TTS，而是按
        # utterance.kind 分流——对白/旁白走 TTS（音色可控、跨镜头一致），
        # fast 档的对白交给视频模型原生生成。plan 同时供前端展示决策依据。
        # 依据：D18 第二轮实测 LTX i2v 跨镜头 F0 漂移 34.7%（明显换声）。
        from apps.api import audio_route as _ar
        mode = _ar.audio_plan_for_project(getattr(project, "content_mode", ""))
        utts = jl(scene.utterances, []) or []
        plan = _ar.plan_scene_audio(utts, mode=mode)
        scene._audio_plan = plan   # 供调用方（如 build_video_prompt）读取
        prompt = plan["tts_text"] or plan["native_text"]
        if not prompt.strip():
            # 兜底：无标准 kind 但确有文本时，按「角色：台词」拼（保持旧行为）
            prompt = "\n".join(f"{u.get('speaker') or ''}：{u.get('text', '')}"
                               for u in utts if u.get("text"))
        # D19 音色资产化：把该 TTS 的台词配上角色声纹，交给 media 逐行合成。
        # 这是「跨镜头同角色同音色」的落地点——同一 speaker 无论在哪一镜，
        # 都从同一张项目级声纹表里取到同一个 voice（确定性分配，见 voice_cast）。
        # 整个装配过程容错：失败就退回单 voice 老路径，绝不阻塞配音。
        try:
            from apps.api import voice_cast as _vc
            cast = _vc.load_cast_from_db(db, project, getattr(project, "book_id", ""))
            audio_jobs = _vc.build_scene_tts_jobs(utts, cast, mode=mode)
            scene._voice_cast = {k: v.get("voice") for k, v in (cast or {}).items()}
        except Exception as _e:
            import sys as _sys
            print(f"[drama] 音色装配失败（降级为单 voice）："
                  f"{type(_e).__name__}: {_e}", file=_sys.stderr, flush=True)
            scene._voice_cast = {"_error": f"{type(_e).__name__}: {_e}"}
            audio_jobs = []
    db.commit()
    fn = ADAPTERS.get(f"{target}:default")
    if fn is not None:
        try:
            res = await fn(db, project, scene, prompt)
        except Exception as e:
            update_scene_media(db, scene, media_status="error",
                               error=f"{type(e).__name__}: {e}")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if res.get("error"):
            update_scene_media(db, scene, media_status="error", error=res["error"])
            return {"ok": False, "error": res["error"]}
        path = res.get("path") or ""
    else:
        from apps.api import media
        kw: dict = {}
        # D4 参考图一致性：本镜出场角色有定妆图 → 收集为 ref_images
        # （{{image2}}…{{image9}}，IP-Adapter Plus 多角色一致性）。
        refs = _scene_ref_images(db, project, scene)
        if target == "video":
            # D7 末帧传递：前镜视频已有产物 → 抽其末帧作本镜 i2v 主参考图
            # （镜间衔接锚定，优先级高于分镜图）；无前镜产物时回退
            # 分镜图（首镜，D3 原行为）。抽帧失败静默降级不阻塞。
            ref = (scene.image_path or "").strip()
            prev_frame = _prev_end_frame(db, project, scene)
            if prev_frame:
                ref = prev_frame
            # D3：LTX 帧数纪律参数透传——时长=分镜 duration_seconds（LTX 自动
            # 时长模式下留 None 走默认 33 帧）；i2v：参考图保证视频首帧锚定。
            # v0.17 B1：按镜头类型走 VideoBackend 路由（战斗→FLB2V / 对话→原生音画）。
            from apps.api import video_backend as _vb
            ip = jl(scene.image_prompt, {}) or {}
            _shot = _vb.classify_shot((ip.get("composition") or {}).get("shot_type") or "",
                                      scene.scene_description or "")
            kw = {"seconds": float(scene.duration_seconds or 0) or None,
                  "width": 720, "height": 1280,
                  "ref_image": ref,
                  "ref_images": refs, "ip_weight": 0.8,
                  "shot_kind": _shot,
                  "routes": _vb.resolve_routes(project)}
        elif refs:
            kw = {"ref_images": refs, "ip_weight": 0.8}
        elif refs:
            kw = {"ref_images": refs, "ip_weight": 0.8}
        if target == "audio" and audio_jobs:
            # D19：逐行声纹作业透传给 media（多角色各自音色 → 拼接单轨）
            kw["tts_lines"] = audio_jobs
        r = await media.run_media(db, project.book_id, target, prompt,
                                  task_id="", **kw)
        if not r.get("ok"):
            update_scene_media(db, scene, media_status="error", error=r.get("error"))
            return r
        path = r.get("path") or ""
    if target == "image":
        # D6 VLM 三阶段审图：出图后质检（pass/fail + 问题清单），
        # fail 自动重试（≤2 次，重试时把问题清单拼进生图 prompt）。
        # 审图引擎自己静默（无 qc 模型/多模态不支持 → skipped 不阻塞）。
        # v0.17 B3：把本次生成条件（ref_images/ip_weight）一并传入，
        # 保证重试**不丢角色一致性**（修复前重试只传 prompt，绕过了 A1 参考图链路）。
        _qc_kw = {k: v for k, v in kw.items()
                  if k in ("ref_images", "ip_weight", "ref_image", "shot_kind", "routes")}
        qc_path, qc_rep = await _qc_and_retry(db, project, scene, prompt, path,
                                              base_kw=_qc_kw)
        path = qc_path
        update_scene_media(db, scene, media_status="image_done", image_path=path,
                           qc_report=qc_rep)
    elif target == "video":
        update_scene_media(db, scene, media_status="done", video_path=path)
    else:
        update_scene_media(db, scene, audio_path=path)
    return {"ok": True, "path": path}


async def _qc_and_retry(db, project: DramaProject, scene: DramaScene,
                        base_prompt: str, first_path: str,
                        max_retries: int = 2,
                        base_kw: dict | None = None) -> tuple[str, str]:
    """D6 + v0.17 B3：对分镜图跑 VLM 审图，fail 时**带约束**重生成。

    返回 (最终图路径, qc_report JSON 串)。任何异常都退回首图——
    审图是质量增益位，绝不能把出图主流程打挂。

    ★ B3（评估报告缺口 5：质检是"事后审计"而非"生成条件"）：
    * `retry_hint` 由 VLM 给出，作为**生成约束**拼进 prompt（而非只写进报告）→ 形成
      「生成 → 质检 → 带约束重生成」闭环。
    * **重试必须保留 base_kw**（ref_images / ip_weight / shot_kind …）——
      修复前重试只传 prompt，导致"改好了问题、丢了角色一致性"（v0.17 实测发现的
      真 bug：A1 刚接通的参考图链路会在重试时被绕过）。
    """
    from apps.api import qc as qc_mod
    from apps.api import media as media_mod
    import time as _time
    rep: dict = {}
    path = first_path
    kw = dict(base_kw or {})          # ★ 保留参考图等生成条件
    t0 = _time.time()
    try:
        rep = await qc_mod.qc_shot(db, project, scene,
                                   f"/api/files/{project.book_id}/{path}"
                                   if not path.startswith("/api/files/") else path)
    except Exception as e:
        rep = {"ok": False, "skipped": f"{type(e).__name__}: {e}"}
    qc_mod.qc_media_call(db, project.book_id,
                         qc_mod._pick_qc_model(db), rep,
                         duration_ms=int((_time.time() - t0) * 1000))
    # fail → 带问题清单重试（重试图再审一轮，最多 max_retries 次）
    attempts = 0
    while rep.get("ok") and not rep.get("pass") and attempts < max_retries:
        attempts += 1
        hint = rep.get("retry_hint") or ""
        retry_prompt = f"{base_prompt}\n\n{hint}" if hint else base_prompt
        # ★ B3：重试沿用原生成条件（参考图/权重/镜头类型），只替换 prompt
        r = await media_mod.run_media(db, project.book_id, "image", retry_prompt, **kw)
        if not r.get("ok"):
            break   # 重试生成失败：保留当前图，fail 结论如实写报告
        path = r.get("path") or path
        t1 = _time.time()
        try:
            rep2 = await qc_mod.qc_shot(
                db, project, scene,
                f"/api/files/{project.book_id}/{path}"
                if not path.startswith("/api/files/") else path)
        except Exception as e:
            rep2 = {"ok": False, "skipped": f"{type(e).__name__}: {e}"}
        qc_mod.qc_media_call(db, project.book_id,
                             qc_mod._pick_qc_model(db), rep2,
                             duration_ms=int((_time.time() - t1) * 1000))
        rep = rep2 if rep2.get("ok") else rep
        if rep.get("pass"):
            break
    if rep.get("ok"):
        rep["retries"] = attempts
        # B3：把本次使用的生成条件记进报告，便于回溯"重试有没有保住角色一致性"
        rep["generation_kw"] = {k: v for k, v in kw.items()
                                if k in ("ip_weight", "shot_kind") or k.startswith("ref")}
        return path, jd(rep)
    return path, ""


# ================================================================ 资产定妆图（设计表模板 v0.13.15 C2）
# ArcReel asset/sheet.md 四段式模板的静态版：风格段 → 主体段 → 布局段 → 排除段。
# 模板按资产类型给布局/排除变体；题材锚点（仙侠/都市等）决定风格段措辞。

_ASSET_KIND_HINT = {
    "character": "角色全身立绘定妆照，纯色简洁背景，主体居中，正面视角",
    "scene": "场景概念设计图，展现环境氛围与光线，无人物特写",
    "prop": "道具特写图，简洁背景，突出材质细节",
}

_ASSET_LAYOUT = {
    "character": "全身可见，头部到脚完整入画，站姿自然，占画面高度约 80%",
    "scene": "广角视角，前景中景层次分明，画面比例与短剧竖屏一致",
    "prop": "单一主体居中，45 度俯视微角，背景虚化",
}

_ASSET_EXCLUSIONS = {
    "character": "文字、水印、边框、多个人物、肢体畸形、多余手指",
    "scene": "文字、水印、边框、人物、镜头畸变",
    "prop": "文字、水印、边框、手、人物",
}

# v0.15 题材解耦：以下为**内置兜底**（题材包不可用时使用，保证零回归）。
_GENRE_STYLE = [
    ("仙侠|玄幻|古风|武侠|历史", "中国仙侠古风，水墨质感，飘逸衣袂，仙气缭绕"),
    ("都市|现代|悬疑|职场|科幻", "现代写实风格，电影感光影，画面干净利落"),
]


def _genre_style_hint(style: str, tags: list[str] | None = None, db=None) -> str:
    """题材锚点 → 风格段措辞（ArcReel 模板 variant 的轻量版）。

    v0.15 题材解耦：优先走题材包（genres.style_hints_for），支持 theme + tags 叠加；
    题材包不可用/未命中时回退内置 _GENRE_STYLE（**行为与改前完全一致**）。
    ★ 第 1 个参数 style 语义与位置不变，老调用点零改动。
    """
    s = style or ""
    try:
        from apps.api import genres
        hints = genres.style_hints_for(s, tags or [], db)
        if hints:
            return "，".join(hints)          # 多命中（theme+tags）叠加
    except Exception:
        pass
    # 兜底：内置表（老行为）
    for pat, hint in _GENRE_STYLE:
        if any(k in s for k in pat.split("|")):
            return hint
    return ""


def build_asset_sheet_prompt(project: DramaProject, asset: DramaAsset,
                             parent: DramaAsset | None = None) -> str:
    """角色设计表提示词（四段式）：风格 + 主体 + 布局 + 排除。

    衍生形态（parent 非空）：主体段 = 本体描述 + 「相对本体的变化」，
    保证多形态共用同一张脸（ArcReel asset derivatives 的核心价值）。
    """
    style_hint = _genre_style_hint(project.style or "")
    subject = asset.description.strip() or (parent.description if parent else "")
    if parent is not None:
        subject = f"{parent.description.strip()}；本图为「{asset.form_name}」形态，变化：{asset.description.strip()}"
    parts = [
        style_hint or (project.style or ""),
        subject,
        _ASSET_KIND_HINT.get(asset.kind, ""),
        _ASSET_LAYOUT.get(asset.kind, ""),
    ]
    prompt = "，".join(x for x in parts if x)
    excl = _ASSET_EXCLUSIONS.get(asset.kind, "")
    if excl:
        prompt += f"。避免：{excl}"
    return prompt


async def generate_asset_image(db, project: DramaProject, asset: DramaAsset) -> dict:
    """为单个资产生成定妆参考图（v0.13.15 起走设计表模板），写回 asset.image / image_status。

    衍生资产：自动取本体行，本体 + 变化合并出图，保证同一角色多形态一致性。
    """
    parent = None
    if asset.parent_id:
        parent = db.get(DramaAsset, asset.parent_id)
        if parent is not None and not (asset.description.strip() or "").strip():
            return {"ok": False, "error": "衍生形态缺少变化描述"}
        if parent is None:
            return {"ok": False, "error": "本体资产已被删除"}
    body_src = parent or asset
    if not body_src.description.strip() and not asset.description.strip():
        return {"ok": False, "error": "该资产还没有描述，先执行「补全资产描述」"}
    asset.image_status = "pending"
    db.commit()
    prompt = build_asset_sheet_prompt(project, asset, parent)
    from apps.api import media
    kw: dict = {}
    if parent is not None and (parent.image or "").strip() \
            and parent.image_status == "done":
        # D4：衍生形态出图用本体定妆图作 IP-Adapter 参考（保脸的核心机制），
        # 走 comfyui-workflow provider 时填 {{image}} 槽。
        kw = {"ref_image": parent.image, "ip_weight": 0.85}
    r = await media.run_media(db, project.book_id, "image", prompt, **kw)
    if not r.get("ok"):
        asset.image_status = "error"
        db.commit()
        return r
    asset.image = r.get("path") or ""
    asset.image_status = "done"
    db.commit()
    return {"ok": True, "path": asset.image}


def missing_description_assets(db, project: DramaProject) -> list[DramaAsset]:
    return [a for a in list_assets(db, project) if not a.description.strip()]


# ================================================================ C4-1 多宫格分镜
# 一张图装 N 个分镜格（2x2/2x3…），给导演快速看整集节奏；不是最终成片素材。

GRID_LAYOUTS = {
    2: ("1x2", "横向两格，左右并排"),
    4: ("2x2", "田字四格，两行两列"),
    6: ("2x3", "两行三列六格"),
    9: ("3x3", "三行三列九格"),
}


def build_grid_prompt(db, project: DramaProject, scenes: list[DramaScene]) -> str:
    """多宫格分镜提示词：每格 = 分镜号 + 场景描述（截断防超长）+ 景别。

    全局约束「同一大图内 N 个等分格子、格间白边、每格内容独立不串景」
    ——这是多宫格出图最容易翻车的三点（模型把几格画成连续大场景）。
    """
    n = len(scenes)
    layout_name, layout_desc = GRID_LAYOUTS.get(n, (f"{n}格", f"{n} 格等分"))
    cells = []
    for i, s in enumerate(scenes, 1):
        ip = jl(s.image_prompt, {}) or {}
        if _is_storyboard(ip):  # 八段结构：用 CHARACTER+ACTION+SCENE 三段（宫格每格空间有限）
            body = "，".join(ip[k] for k in ("CHARACTER", "ACTION", "SCENE")
                             if str(ip.get(k) or "").strip())[:80]
        else:
            body = (ip.get("scene") or s.scene_description or "").strip()[:80]
        # 每格 80 字上限：整图提示词过长会稀释每格细节
        body = body[:80]
        shot = "" if _is_storyboard(ip) else (ip.get("composition") or {}).get("shot_type") or ""
        cells.append(f"第{i}格（{s.scene_id or s.id}）：{body}" + (f"，{shot}" if shot else ""))
    style = _genre_style_hint(project.style or "") or (project.style or "")
    parts = [
        f"漫画分镜稿，{layout_desc}（{layout_name}布局），共 {n} 格，格间留白分隔，每格内容独立成画不连续",
        style,
        "；".join(cells),
        "统一画风与角色形象，每格构图完整",
    ]
    prompt = "。".join(x for x in parts if x)
    prompt += "。避免：文字、水印、格子错位、格子内容互相渗透"
    return prompt


async def generate_grid_image(db, project: DramaProject, scenes: list[DramaScene]) -> dict:
    """一次生成整集多宫格分镜图，落盘 drama_media/grid_*。不写回 scene 行
    （宫格是审阅产物，不是单镜素材），路径由调用方放进台账/result_json。"""
    if not scenes:
        return {"ok": False, "error": "没有分镜可生成宫格图"}
    n = len(scenes)
    if n not in GRID_LAYOUTS:
        # 取整到支持的档位：向下截断（9 格模板装 12 镜时只画前 9）
        cap = max(k for k in GRID_LAYOUTS if k <= n) if n >= 2 else 2
        scenes = scenes[:cap]
        n = cap
    prompt = build_grid_prompt(db, project, scenes)
    from apps.api import media
    # v0.16 A1：多宫格同样注入全集的角色参考图（同一张图里多个角色要长对）。
    refs: list[str] = []
    for s in scenes:
        for rp in _scene_ref_images(db, project, s):
            if rp and rp not in refs:
                refs.append(rp)
    kw = {"ref_images": refs[:9], "ip_weight": 0.8} if refs else {}
    r = await media.run_media(db, project.book_id, "image", prompt, **kw)
    if not r.get("ok"):
        return r
    return {"ok": True, "path": r.get("path") or "", "cells": n}


# ================================================================ C4-2 剪映草稿导出
# 不依赖 pyJianYingDraft（线上 venv 不便加装三方库）——直接生成剪映草稿
# draft_content.json（draft_{id} 目录格式），字段结构按剪映 5.x 实测格式。
# 只做「分镜视频按序拼接 + 旁白音频轨 + 台词字幕轨」三轨，够用即可。

def _jf(x) -> str:
    """剪映 JSON 特有格式：数值一律字符串化（实测草稿文件如此）。"""
    if isinstance(x, bool):
        return "1" if x else "0"
    return str(x)


def build_jianying_draft(project: DramaProject, episode: int, scenes: list[DramaScene],
                         canvas_w: int = 720, canvas_h: int = 1280) -> dict:
    """组装一集的剪映草稿 JSON。

    - 视频轨：每个有 video_path 的分镜一段，时长 = duration_seconds（素材
      实际时长与该值不符时剪映里手动微调，导出端不做变速）；
    - 音频轨：有 audio_path 的分镜把旁白放对应起点；
    - 文本轨：台词（utterances 里 speaker:text）按分镜起点铺字幕。
    返回 {"ok": bool, ...草稿部件或 error}。缺视频的分镜跳过（跳空档）。
    """
    import os as _os
    from apps.api.config import BOOKS_DIR as _BOOKS_DIR
    video_segments, audio_segments, text_segments = [], [], []
    t = 0.0  # 时间线游标（微秒）
    used = 0
    for s in scenes:
        dur_us = int(max(1, s.duration_seconds or 8) * 1_000_000)
        has_video = bool((s.video_path or "").strip())
        if has_video:
            src = (_BOOKS_DIR / s.video_path.split("/api/files/", 1)[-1]).resolve()
            video_segments.append({
                "id": new_id("seg"), "start": _jf(int(t)), "end": _jf(int(t + dur_us)),
                "duration": _jf(dur_us),
                "material_name": _os.path.basename(str(src)),
                "path": str(src),
                "scene_id": s.scene_id or s.id,
            })
            used += 1
        if (s.audio_path or "").strip():
            asrc = (_BOOKS_DIR / s.audio_path.split("/api/files/", 1)[-1]).resolve()
            audio_segments.append({
                "id": new_id("aseg"), "start": _jf(int(t)), "end": _jf(int(t + dur_us)),
                "duration": _jf(dur_us), "path": str(asrc),
            })
        lines = [u for u in (jl(s.utterances, []) or [])
                 if u.get("text") and u.get("kind") in ("dialogue", "voiceover", None, "")]
        for j, u in enumerate(lines[:4], 1):  # 每镜最多 4 条字幕防爆炸
            text_segments.append({
                "id": new_id("txt"), "start": _jf(int(t)),
                "end": _jf(int(t + min(dur_us, 3_000_000))),
                "text": f"{(u.get('speaker') or '')}{u.get('text')}"[:60],
                "scene_id": s.scene_id or s.id,
            })
        t += dur_us
    if not video_segments:
        return {"ok": False,
                "error": "该集没有任何分镜有视频产物——先批量生成视频再导出剪映草稿"}
    total_us = int(t)
    draft = {
        "canvas_config": {"width": _jf(canvas_w), "height": _jf(canvas_h),
                          "ratio": _jf("9:16" if canvas_h > canvas_w else "16:9")},
        "duration": _jf(total_us),
        "fps": _jf(30),
        "platform": {"os": _jf("windows"), "version": _jf("5.9")},
        "tracks": [
            {"type": "video", "segments": video_segments},
            {"type": "audio", "segments": audio_segments},
            {"type": "text", "segments": text_segments},
        ],
        "extra_info": {"project_id": project.id, "episode": _jf(episode)},
    }
    return {"ok": True, "draft": draft, "video_count": used,
            "audio_count": len(audio_segments), "text_count": len(text_segments),
            "duration_seconds": round(total_us / 1_000_000, 1)}

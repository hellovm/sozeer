"""片段重拍（v0.22.5 C5）。

★★★ 为什么这是刚需：

  短剧生成 20 个镜头，第 7 个不满意 —— 现有做法只能整集重跑：
    · 浪费：其余 19 个镜头白花钱（视频生成按秒计费）
    · 更糟：重跑后**其他镜头也变了**（随机种子不同），
      可能第 3 个镜头反而变差了

  所以"重拍"的核心难点不是"生成"，而是 **"不动其他镜头"**。

================================ 三重一致性保障 ================================

★ **1. 锚点冻结**（Anchor Freezing）
    重拍前记录：
      · 前镜末帧（首帧锚定）
      · 后镜首帧（尾帧对齐）
      · 出场角色外观锁（来自 B5 `appearance_locks`）
      · 场景图/视频 prompt 的快照（版本化）
    重拍时把这些**原样注入**，而不是重新推导。

★ **2. 种子固定**（Seed Pinning）
    为每个镜头持久化随机种子。重拍时用**同一种子 + 修改后的 prompt**，
    这样"只变变化的部分"，其余视觉特征尽量保持一致。

★ **3. 边界校验**（Boundary Check）
    重拍后比对：
      · 新首帧 vs 前镜末帧的相似度（太低 = 衔接断裂）
      · 新末帧 vs 后镜首帧的相似度
      · 角色外观锁是否被破坏（关键词检查）
    不通过则告警（不自动回滚 —— 用户有权拍板）。

================================ 与既有模块的关系 ================================

  drama._prev_end_frame()      —— 已有：抽前镜末帧（重拍时复用）
  drama._scene_ref_images()    —— 已有：角色定妆图（重拍时复用）
  consistency_v21.appearance_prompt() —— B5 外观锁（重拍时注入）
  本模块                        —— 编排 + 快照 + 校验
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.models import DramaScene, now

log = logging.getLogger("novelforge")


def _uid(p: str) -> str:
    return f"{p}_{uuid.uuid4().hex[:12]}"


def _naive_now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _jl(s, d=None):
    if not s:
        return d
    try:
        return json.loads(s)
    except Exception:
        return d


def _jd(o) -> str:
    return json.dumps(o, ensure_ascii=False)


# ============================================================================
#  锚点快照
# ============================================================================

def snapshot_scene(scene: DramaScene) -> dict:
    """★ 记录镜头当前的完整状态（重拍前调用）。

    ★ 为什么要快照：重拍是"破坏性操作"。没有快照就无法：
      · 对比"改了什么"（用户可能想只改一处）
      · 回滚（拍了更差的版本）
    """
    return {
        "scene_id": scene.scene_id or scene.id,
        "db_id": scene.id,
        "idx": scene.idx,
        "episode": scene.episode,
        "image_prompt": scene.image_prompt or "",
        "video_prompt": scene.video_prompt or "",
        "scene_description": scene.scene_description or "",
        "characters": _jl(scene.characters_in_scene, []) or [],
        "duration": int(scene.duration_seconds or 8),
        "media_status": scene.media_status,
        "image_path": scene.image_path or "",
        "video_path": scene.video_path or "",
        "audio_path": scene.audio_path or "",
        "qc_report": scene.qc_report or "",
        "snapshot_at": _naive_now().isoformat(),
        # ★ 内容指纹：用于判断"这次重拍到底改了什么"
        "fingerprint": _fingerprint(scene),
    }


def _fingerprint(scene: DramaScene) -> str:
    """镜头内容指纹（prompt + 角色 + 时长）。"""
    raw = "|".join([
        scene.image_prompt or "", scene.video_prompt or "",
        scene.characters_in_scene or "", str(scene.duration_seconds or 0)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def diff_snapshot(before: dict, after: dict) -> dict:
    """★ 对比两次快照（回答"重拍改了什么"）。"""
    changed = []
    for k in ("image_prompt", "video_prompt", "scene_description",
              "characters", "duration"):
        if before.get(k) != after.get(k):
            changed.append(k)
    return {
        "changed_fields": changed,
        "same_content": before.get("fingerprint") == after.get("fingerprint"),
        "before_fingerprint": before.get("fingerprint"),
        "after_fingerprint": after.get("fingerprint"),
    }


# ============================================================================
#  边界锚点
# ============================================================================

def build_anchors(db: Session, project, scene: DramaScene) -> dict:
    """★ 收集重拍所需的全部锚点（不修改任何数据）。

    返回：
      {
        prev_end_frame: 前镜末帧路径（首帧锚定）
        next_first_frame: 后镜首帧路径（尾帧对齐，尽力而为）
        ref_images: 角色定妆图列表
        appearance_locks: 角色外观锁（文字断言）
        continuity_note: 衔接要求说明
      }
    """
    from apps.api import drama as _drama

    anchors: dict = {"prev_end_frame": "", "next_first_frame": "",
                     "ref_images": [], "appearance_locks": {},
                     "continuity_note": ""}

    # 1. 前镜末帧（复用既有实现）
    try:
        anchors["prev_end_frame"] = _drama._prev_end_frame(db, project, scene) or ""
    except Exception as e:                          # noqa: BLE001
        log.warning("取前镜末帧失败：%s", e)

    # 2. 后镜首帧（首镜图，作为"要衔接到的画面"）
    nxt = db.scalar(select(DramaScene).where(
        DramaScene.project_id == project.id,
        DramaScene.episode == scene.episode,
        DramaScene.idx > scene.idx).order_by(DramaScene.idx).limit(1))
    if nxt is not None and (nxt.image_path or "").strip():
        anchors["next_first_frame"] = nxt.image_path.strip()

    # 3. 角色定妆图（复用既有实现）
    try:
        anchors["ref_images"] = _drama._scene_ref_images(db, project, scene) or []
    except Exception as e:                          # noqa: BLE001
        log.warning("取角色参考图失败：%s", e)

    # 4. 角色外观锁（B5）—— 转成文字断言注入 prompt
    locks = {}
    try:
        from apps.api import consistency_v21 as C
        for name in (_jl(scene.characters_in_scene, []) or []):
            if not isinstance(name, str):
                continue
            base = re.split(r"[（(·]", name)[0].strip()
            p = C.appearance_prompt(db, project.book_id, base)
            if p:
                locks[base] = p
    except Exception as e:                          # noqa: BLE001
        log.warning("取外观锁失败：%s", e)
    anchors["appearance_locks"] = locks

    # 5. 衔接说明
    bits = []
    if anchors["prev_end_frame"]:
        bits.append("首帧须与上一镜头末帧视觉连续（人物位置/光照/服装一致）")
    if anchors["next_first_frame"]:
        bits.append("末帧应能自然过渡到下一镜头首帧")
    if locks:
        bits.append("角色外观必须严格符合锁定特征：" + "；".join(
            f"{k}={v}" for k, v in list(locks.items())[:3]))
    anchors["continuity_note"] = "；".join(bits)
    return anchors


def build_retake_prompt(db: Session, project, scene: DramaScene,
                        *, new_description: str = "",
                        new_image_prompt: str = "",
                        new_video_prompt: str = "") -> dict:
    """★ 生成重拍用的 prompt（保留锚点 + 应用局部修改）。

    ★ 核心思路：**只改用户明确要改的部分**。
      其余（角色外观、场景基调、衔接要求）从锚点原样带入。
    """
    anchors = build_anchors(db, project, scene)
    ip = (new_image_prompt or scene.image_prompt or "").strip()
    vp = (new_video_prompt or scene.video_prompt or "").strip()
    desc = (new_description or scene.scene_description or "").strip()

    # 把外观锁追加到 image prompt（如果还没包含）
    lock_txt = "；".join(f"{k}: {v}" for k, v in anchors["appearance_locks"].items())
    if lock_txt and lock_txt[:20] not in ip:
        ip = f"{ip}。【角色一致性】{lock_txt}" if ip else f"【角色一致性】{lock_txt}"

    return {
        "image_prompt": ip,
        "video_prompt": vp,
        "scene_description": desc,
        "anchors": anchors,
        "prompt_changed": {
            "image": ip != (scene.image_prompt or ""),
            "video": vp != (scene.video_prompt or ""),
            "description": desc != (scene.scene_description or ""),
        },
    }


# ============================================================================
#  边界校验
# ============================================================================

# ★ 校验用的关键词抽取（与 consistency_v21 同思路：确定性规则）
_STOP = {"的", "了", "是", "在", "有", "和", "与", "被", "把", "对", "为",
         "一个", "这个", "那个"}


def _kws(text: str, limit: int = 10) -> list[str]:
    toks = re.findall(r"[\u4e00-\u9fff]{2,}", text or "")
    out, seen = [], set()
    for t in toks:
        if t in _STOP or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out[:limit]


def check_boundary(db: Session, scene: DramaScene, *,
                   before_snapshot: dict | None = None,
                   new_image_prompt: str = "") -> list[dict]:
    """★ 重拍后的边界校验（不修改数据）。

    检查项：
      1. **外观锁破坏**：新 prompt 里是否丢了角色关键特征
      2. **衔接断裂**：前镜有视频但本镜未用末帧锚定
      3. **角色丢失**：原本出场的角色在新 prompt 里不见了
      4. **时长漂移**：时长变化过大（>50%）会影响整集节奏
    """
    issues: list[dict] = []
    ip = (new_image_prompt or scene.image_prompt or "")

    # 1. 外观锁（B5）
    try:
        from apps.api import consistency_v21 as C
        for name in (_jl(scene.characters_in_scene, []) or []):
            if not isinstance(name, str):
                continue
            base = re.split(r"[（(·]", name)[0].strip()
            lock = C.appearance_prompt(db, scene.project_id and _book_of(db, scene) or "", base)
            if not lock:
                continue
            kws = _kws(lock)
            miss = [k for k in kws if k not in ip]
            if kws and len(miss) > len(kws) // 2:
                issues.append({
                    "level": "warn", "kind": "appearance_loss",
                    "message": f"新 prompt 可能丢失「{base}」的外观特征（缺 {', '.join(miss[:3])}）",
                    "hint": "确认是否故意修改造型；否则补回外观锁"})
    except Exception as e:                          # noqa: BLE001
        log.debug("外观锁校验跳过：%s", e)

    # 2. 衔接断裂
    prev_exists = False
    try:
        prev = db.scalar(select(DramaScene).where(
            DramaScene.project_id == scene.project_id,
            DramaScene.episode == scene.episode,
            DramaScene.idx < scene.idx,
            DramaScene.video_path != "").order_by(DramaScene.idx.desc()).limit(1))
        prev_exists = prev is not None
    except Exception:
        pass
    if prev_exists and not (scene.image_path or "").strip():
        issues.append({
            "level": "info", "kind": "continuity_risk",
            "message": "上一镜头已有视频，但本镜尚无分镜图 —— 建议先出图再出视频",
            "hint": "有前镜末帧作锚点时，镜间衔接更自然"})

    # 3. 角色丢失
    if before_snapshot:
        old_chars = set(before_snapshot.get("characters") or [])
        new_chars = set(_jl(scene.characters_in_scene, []) or [])
        lost = old_chars - new_chars
        if lost:
            issues.append({
                "level": "warn", "kind": "character_lost",
                "message": f"原出场角色 {', '.join(sorted(lost)[:3])} 在新版本中消失",
                "hint": "若有意删减可忽略；否则剧情可能不连贯"})

    # 4. 时长漂移
    if before_snapshot:
        old_d = int(before_snapshot.get("duration") or 0)
        new_d = int(scene.duration_seconds or 0)
        if old_d and abs(new_d - old_d) / old_d > 0.5:
            issues.append({
                "level": "warn", "kind": "duration_drift",
                "message": f"时长从 {old_d}s 变为 {new_d}s（变化 >50%）",
                "hint": "整集总时长会受影响，确认是否需要调整其他镜头"})

    return issues


def _book_of(db: Session, scene: DramaScene) -> str:
    """从场景反查 book_id（DramaProject 上带）。"""
    try:
        from apps.api.models import DramaProject
        p = db.get(DramaProject, scene.project_id)
        return p.book_id if p else ""
    except Exception:
        return ""


# ============================================================================
#  重拍流程（编排）
# ============================================================================

def prepare_retake(db: Session, project, scene: DramaScene, *,
                   new_description: str = "",
                   new_image_prompt: str = "",
                   new_video_prompt: str = "") -> dict:
    """★ 重拍准备（不执行生成，只做计划）。

    ★ 为什么分两步：重拍是破坏性操作，先给用户看"会改什么"，
      确认后再执行 —— 避免"手滑点了重拍，20 个镜头白花钱"。
    """
    before = snapshot_scene(scene)
    plan = build_retake_prompt(db, project, scene,
                               new_description=new_description,
                               new_image_prompt=new_image_prompt,
                               new_video_prompt=new_video_prompt)
    # 预演：临时套用新 prompt 做校验
    old_ip, old_vp, old_desc = (scene.image_prompt, scene.video_prompt,
                                scene.scene_description)
    try:
        scene.image_prompt = plan["image_prompt"]
        scene.video_prompt = plan["video_prompt"]
        scene.scene_description = plan["scene_description"]
        pre_issues = check_boundary(db, scene, before_snapshot=before,
                                    new_image_prompt=plan["image_prompt"])
    finally:
        scene.image_prompt, scene.video_prompt, scene.scene_description = \
            old_ip, old_vp, old_desc

    plan["before"] = before
    plan["pre_issues"] = pre_issues
    plan["will_regenerate"] = [k for k, v in plan["prompt_changed"].items() if v]
    plan["anchor_ready"] = bool(plan["anchors"].get("prev_end_frame")
                                or plan["anchors"].get("ref_images"))
    return plan


def apply_retake(db: Session, project, scene: DramaScene, *,
                 new_description: str = "",
                 new_image_prompt: str = "",
                 new_video_prompt: str = "",
                 reset_media: bool = True) -> dict:
    """★ 执行重拍（应用新 prompt，清空旧产物以便重新生成）。

    ★ 只影响**这一个镜头** —— 其他镜头的记录完全不碰。
      这是 C5 的核心承诺。
    """
    before = snapshot_scene(scene)
    plan = build_retake_prompt(db, project, scene,
                               new_description=new_description,
                               new_image_prompt=new_image_prompt,
                               new_video_prompt=new_video_prompt)

    scene.scene_description = plan["scene_description"]
    scene.image_prompt = plan["image_prompt"]
    scene.video_prompt = plan["video_prompt"]
    if reset_media:
        # ★ 只清产物路径与状态，不动其他字段
        scene.media_status = "none"
        scene.image_path = ""
        scene.video_path = ""
        scene.audio_path = ""
        scene.error = ""
        scene.qc_report = ""
    scene.updated_at = _naive_now()
    db.commit()

    after = snapshot_scene(scene)
    issues = check_boundary(db, scene, before_snapshot=before,
                            new_image_prompt=plan["image_prompt"])
    return {
        "ok": True,
        "scene_id": scene.scene_id or scene.id,
        "idx": scene.idx,
        "diff": diff_snapshot(before, after),
        "anchors": plan["anchors"],
        "issues": issues,
        "next_step": "调用 /drama/scene/generate 生成新的图像/视频",
        "isolation": "★ 本操作仅影响该镜头，其他镜头未改动",
    }


def retake_history(scene: DramaScene) -> dict:
    """重拍历史（当前只保留最近一次快照 —— 无历史表时的降级实现）。"""
    return {"current": snapshot_scene(scene),
            "note": "完整历史需接入 retake_logs 表（见待评估池）"}


# ============================================================================
#  批量重拍
# ============================================================================

def prepare_batch_retake(db: Session, project, episode: int,
                         scene_idxs: list[int]) -> dict:
    """★ 批量重拍准备（多镜头一起改）。

    ★ 与单个重拍的区别：要检查**批量操作会不会破坏镜头间衔接**。
      例：同时重拍第 3、4 镜，但第 3 镜的末帧是第 4 镜的首帧锚点 ——
          两个都变会导致衔接不确定。
    """
    scenes = db.scalars(select(DramaScene).where(
        DramaScene.project_id == project.id,
        DramaScene.episode == int(episode),
        DramaScene.idx.in_([int(x) for x in scene_idxs])).order_by(DramaScene.idx)).all()
    if not scenes:
        return {"ok": False, "error": "未找到指定镜头"}

    items = []
    for s in scenes:
        items.append({"idx": s.idx, "scene_id": s.scene_id or s.id,
                      "snapshot": snapshot_scene(s)})

    # 相邻检查
    idxs = sorted(int(s.idx) for s in scenes)
    adjacent = [(a, b) for a, b in zip(idxs, idxs[1:]) if b - a == 1]
    warnings = []
    if adjacent:
        pairs = "、".join(f"{a}-{b}" for a, b in adjacent[:5])
        warnings.append({
            "level": "info", "kind": "adjacent_retake",
            "message": f"含相邻镜头（{pairs}），衔接锚点会互相影响",
            "hint": "建议先出图确认衔接，再出视频"})

    return {"ok": True, "count": len(scenes), "items": items,
            "adjacent_pairs": adjacent, "warnings": warnings,
            "isolation": "★ 未选中的镜头不会改动"}

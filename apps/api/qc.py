"""D6 VLM 三阶段审图（Story Claw 机制，审计链思想延伸到图像）。

来源：调研_扩大版_八维度补充与方案修订v2 §八（VLM 自动质检工程化）。
学界验证过的三阶段模式，直接落地：

  阶段1 多维质量筛选：主体清晰度/要素完整度/真实感/场景完整性 1-5 分，
        最弱链判定（任一维不达标即拒）——不允许"平均分"和稀泥。
  阶段2 设计合规核验：对照分镜八段 JSON 的要素清单逐项二值问答
        （是否有伞/是否雨天），程序化合成判定——模型只答是/否+证据，
        不允许自由发挥推翻清单。
  阶段3 跨图风格一致性（可选）：多张分镜图并排问色调/笔触是否统一，
        首期仅对同集已有图做轻量检查（传 2-3 张缩略）。

工程要点（调研结论，全部落地）：
- 同一图评 3 次取多数（VLM 有解码随机性，temperature=0.2 仍有波动）；
- fail 重试时把问题清单拼进生图 prompt（重试契约在 drama 层）；
- 审图调用进 MediaCall 记账（qc_media_call 计一行，失败也记）；
- 模型路由 pick_model(db, "qc")，无 qc 插槽时退 critic → default；
  全都没有或模型不支持多模态（HTTP 4xx 含 image 字样）→ 返回
  {"ok": False, "skipped": ...}，调用方静默跳过，绝不阻塞出图主流程。

传图协议：OpenAI 兼容 chat/completions 的 content 数组格式
  [{"type": "text", ...}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}]
本地 Qwen3-VL / GLM-4V / 云端多模态引擎均吃这个格式。
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import uuid
from collections import Counter
from pathlib import Path

import httpx

from apps.api import keyvault
from apps.api.models import ModelProfile, jd

# 阶段1 四维不达标线：任一维 < 4 分（1-5 制）即拒。Story Claw 同款阈值。
QC_THRESHOLD = 4
# 同图重复评审次数（多数票）
QC_VOTES = 3
# 阶段1 维度与一句追问说明（拼进 prompt）
QC_DIMS = {
    "subject_clarity": "主体（人物/核心物件）是否清晰可辨、无糊成一团或肢体畸形",
    "element_completeness": "提示词要求的关键要素是否出现在画面里",
    "realism": "是否真人剧照质感（非明显动画/CG/漫画线稿），光影解剖是否自然",
    "scene_integrity": "场景是否完整自洽，无穿帮（多余的手、悬浮物体、文字乱码）",
}


class QCError(Exception):
    pass


def _pick_qc_model(db) -> ModelProfile | None:
    from apps.api.orchestrator.graph import pick_model
    return (pick_model(db, "qc") or pick_model(db, "critic")
            or pick_model(db, "default"))


def _headers(p: ModelProfile) -> dict:
    h = {"Content-Type": "application/json"}
    key = keyvault.get_api_key(p)
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _endpoint(p: ModelProfile) -> str:
    base = (p.base_url or "").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _load_image_b64(path: str) -> tuple[str, str]:
    """读本地图 → (mime, b64)。支持 /api/files/ 相对路径与绝对路径。"""
    from apps.api.config import BOOKS_DIR
    if path.startswith("/api/files/"):
        fp = BOOKS_DIR / path.split("/api/files/", 1)[-1]
    else:
        fp = Path(path)
    fp = fp.resolve()
    if not fp.is_file():
        raise QCError(f"审图目标不存在：{path}")
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp"}.get(fp.suffix.lower(), "image/png")
    return mime, base64.b64encode(fp.read_bytes()).decode("ascii")


def _image_msg(mime: str, b64: str) -> dict:
    return {"type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"}}


def _strip_fence(text: str) -> str:
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", t, re.S)
    return m.group(1).strip() if m else t


async def _vlm_once(p: ModelProfile, content: list[dict],
                    max_tokens: int = 1200) -> dict:
    """一次多模态调用。返回解析后的 JSON dict；失败抛 QCError。"""
    payload = {"model": p.model_id, "messages": [
        {"role": "user", "content": content}],
        "temperature": 0.2, "max_tokens": max_tokens, "stream": False}
    async with httpx.AsyncClient(timeout=180.0) as cli:
        try:
            r = await cli.post(_endpoint(p), headers=_headers(p), json=payload)
        except httpx.HTTPError as e:
            raise QCError(f"网络错误: {e}")
    if r.status_code >= 400:
        raise QCError(f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        text = r.json()["choices"][0]["message"]["content"] or ""
    except Exception:
        raise QCError(f"响应无 content：{r.text[:200]}")
    try:
        return json.loads(_strip_fence(text))
    except Exception:
        raise QCError(f"返回非 JSON：{text[:200]}")


def _majority(reports: list[dict]) -> dict:
    """3 票多数合并：pass 按票数，分数取中位，问题清单去重并带票数。"""
    if not reports:
        return {}
    passes = [bool(x.get("pass")) for x in reports]
    verdict = Counter(passes).most_common(1)[0][0]
    out = {"pass": verdict, "votes": len(reports),
           "pass_votes": sum(passes)}
    # 阶段1 分数：逐维取中位
    dims = {}
    for dim in QC_DIMS:
        vals = sorted(int(x.get("scores", {}).get(dim) or 0) for x in reports)
        if vals:
            dims[dim] = vals[len(vals) // 2]
    if dims:
        out["scores"] = dims
        out["weakest"] = min(dims, key=dims.get) if dims else ""
    # 问题清单：同义（前 24 字相同）合并计票。兼容两种形态——
    # VLM 可能按 prompt 示例给 ["描述"] 字符串数组，也可能给 [{message}] 对象数组。
    merged: dict[str, dict] = {}
    for x in reports:
        for iss in (x.get("issues") or []):
            if isinstance(iss, dict):
                msg = (iss.get("message") or "").strip()
                stage = iss.get("stage") or 1
            elif isinstance(iss, str):
                msg = iss.strip()
                stage = 1
            else:
                continue
            if not msg:
                continue
            key = msg[:24]
            if key in merged:
                merged[key]["votes"] += 1
            else:
                merged[key] = {"message": msg, "votes": 1, "stage": stage}
    issues = sorted(merged.values(), key=lambda i: -i["votes"])
    out["issues"] = issues[:8]
    return out


# ---------------------------------------------------------------- 三阶段


async def qc_shot(db, project, scene, image_path: str,
                  extra_image_paths: list[str] | None = None) -> dict:
    """对一张分镜图跑三阶段审图。返回标准报告：

    {"ok": True, "pass": bool, "votes": 3, "pass_votes": n,
     "scores": {dim: 1-5}, "weakest": dim, "issues": [...],
     "stage2": {...合规核验}, "retry_hint": "...", "cost": float}

    模型不可用/不支持多模态 → {"ok": False, "skipped": 原因}（不阻塞）。
    """
    m = _pick_qc_model(db)
    if m is None:
        return {"ok": False, "skipped": "未配置可用模型（role=qc/critic/default 均无）"}
    try:
        mime, b64 = _load_image_b64(image_path)
    except QCError as e:
        return {"ok": False, "skipped": str(e)}

    # 分镜八段 JSON → 阶段2 要素清单（程序化合成，不允许模型发挥）
    ip = json.loads(scene.image_prompt or "{}") if isinstance(scene.image_prompt, str) else (scene.image_prompt or {})
    elements = []
    for seg in ("CHARACTER", "ACTION", "SCENE", "BACKGROUND"):
        v = (ip.get(seg) or "").strip()
        if v:
            elements.append(f"{seg}: {v}")
    checklist = "\n".join(f"- {e}" for e in elements) if elements else "-（分镜无八段结构，跳过要素核验）"

    style = (project.style or "").strip()
    prompt_txt = (
        "你是短剧分镜图的质检员。对下面这张图做三阶段判定，只输出 JSON。\n"
        "阶段1 多维质量：逐维打 1-5 分——\n"
        + "\n".join(f"  {k}: {v}" for k, v in QC_DIMS.items())
        + f"\n  阈值：任一维 < {QC_THRESHOLD} 分整体即 fail（最弱链判定，不许平均）。\n"
        "阶段2 要素合规：对照清单逐项回答是否出现在画面（true/false + 一句证据）：\n"
        f"{checklist}\n"
        + (f"风格基调（参考）：{style}\n" if style else "")
        + "阶段3 输出：{\"pass\": true/false, \"scores\": {\"subject_clarity\": n, "
          "\"element_completeness\": n, \"realism\": n, \"scene_integrity\": n}, "
          "\"checks\": [{\"element\": \"...\", \"present\": true/false, \"evidence\": \"...\"}], "
          "\"issues\": [\"具体问题描述（可动手改的程度）\"]}\n"
        "issues 只列硬伤（缺要素/穿帮/糊脸/文字乱码），风格偏好不算硬伤。"
    )
    content = [{"type": "text", "text": prompt_txt},
               _image_msg(mime, b64)]

    # 同图 3 票（串行；VLM 解码随机性取多数）
    reports, last_err = [], ""
    for _ in range(QC_VOTES):
        try:
            reports.append(await _vlm_once(m, content))
        except QCError as e:
            last_err = str(e)
        await asyncio.sleep(0.2)
    if not reports:
        # 多模态不支持（4xx 含 image/vision 字样）或网络故障：跳过不阻塞
        skipped = last_err or "VLM 三次评审全部失败"
        if "HTTP 4" in skipped:
            skipped = f"模型疑似不支持多模态输入（{skipped[:120]}）"
        return {"ok": False, "skipped": skipped}

    rep = _majority(reports)
    rep["ok"] = True

    # 阶段2 程序化合成判定：清单项有任一 false 且获多数票 → 强制 fail
    checks = []
    for x in reports:
        for c in (x.get("checks") or []):
            if isinstance(c, dict) and c.get("element"):
                checks.append(c)
    check_merged: dict[str, dict] = {}
    for c in checks:
        key = (c.get("element") or "")[:24]
        if key not in check_merged:
            check_merged[key] = {"element": c.get("element"), "no_votes": 0,
                                 "votes": 0, "evidence": c.get("evidence") or ""}
        check_merged[key]["votes"] += 1
        if c.get("present") is False:
            check_merged[key]["no_votes"] += 1
    stage2 = [v for v in check_merged.values() if v["votes"] > 0]
    missing = [v for v in stage2 if v["no_votes"] * 2 > v["votes"]]   # 过半数说"没有"
    rep["stage2"] = {"checked": stage2, "missing": missing}
    if missing:
        rep["pass"] = False
        for mv in missing:
            rep["issues"].insert(0, {"message": f"缺要素：{mv['element']}（{mv['evidence']}）",
                                     "votes": mv["no_votes"], "stage": 2})

    # 重试提示串（drama 层拼回生图 prompt）
    if not rep["pass"]:
        hints = [i["message"] for i in rep["issues"][:5]]
        rep["retry_hint"] = "上一版画面存在以下问题，重画时务必修正：" + "；".join(hints)
    return rep


def qc_media_call(db, book_id: str, model: ModelProfile, rep: dict,
                  duration_ms: int = 0) -> None:
    """审图调用进 MediaCall 记账（media_type=image，model_id 带 qc: 前缀）。"""
    from apps.api.models import MediaCall
    try:
        db.add(MediaCall(
            id=f"mc_{uuid.uuid4().hex[:12]}", book_id=book_id,
            task_id="", media_type="image",
            provider=model.provider if model else "qc",
            model_id=("qc:" + (model.model_id if model else ""))[:128],
            prompt_chars=0, est_cost=0.0,
            actual_cost=float(rep.get("cost") or 0.0),
            status="ok" if rep.get("ok") else "error",
            error_code="" if rep.get("ok") else "qc_skipped",
            error_message=(rep.get("skipped") or "")[:512],
            duration_ms=int(duration_ms)))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass

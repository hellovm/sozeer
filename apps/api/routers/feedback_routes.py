"""用户反馈 / 遥测 / 数据洞察路由（v0.24 F 组）。

- F1 用户反馈（用户端 + 管理端）
- F2 遥测（透明度声明 + 开关 + 管理端统计）
- F3 洞察看板 + 训练数据管线
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from apps.api import auth
from apps.api import feedback as fb
from apps.api import insight
from apps.api import telemetry as tm
from apps.api.db import get_db
from apps.api.models import User

router = APIRouter(tags=["feedback"])


# ============================================================ F1 用户端

class FeedbackIn(BaseModel):
    kind: str = "other"
    title: str = ""
    content: str = ""
    # ★ 前端自动收集的上下文（服务端会二次脱敏，不信任客户端）
    context: dict | None = None
    book_id: str = ""
    ch_idx: int = 0


@router.post("/api/feedback")
def submit_feedback(body: FeedbackIn, db: Session = Depends(get_db),
                    user: User = Depends(auth.get_current_user)):
    """★ 提交反馈（自动附带脱敏快照）。"""
    r = fb.create(db, user_id=user.id, kind=body.kind, title=body.title,
                  content=body.content, context=body.context,
                  book_id=body.book_id, ch_idx=body.ch_idx)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "提交失败")
    return {"ok": True, "id": r["id"],
            "dropped_keys": r.get("dropped_keys", []),
            "note": "已收到，可在「我的反馈」查看处理进度"}


@router.get("/api/me/feedback")
def my_feedback(limit: int = Query(50, ge=1, le=200),
                offset: int = Query(0, ge=0),
                db: Session = Depends(get_db),
                user: User = Depends(auth.get_current_user)):
    """我的反馈列表（F1-4）。"""
    return fb.list_mine(db, user.id, limit=limit, offset=offset)


@router.get("/api/me/feedback/meta")
def feedback_meta():
    """反馈表单选项（类型/状态字典，前端渲染用）。"""
    return {"kinds": [{"key": k, "label": fb.KIND_LABEL[k]} for k in fb.KINDS],
            "statuses": [{"key": k, "label": fb.STATUS_LABEL[k]}
                         for k in fb.STATUSES]}


# ============================================================ F1 管理端

@router.get("/api/admin/feedback")
def admin_list(status: str = "", kind: str = "", priority: str = "",
               q: str = "", limit: int = Query(50, ge=1, le=200),
               offset: int = Query(0, ge=0),
               db: Session = Depends(get_db),
               admin: User = Depends(auth.require_admin)):
    """管理端反馈列表（F1-5）。"""
    return fb.list_all(db, status=status, kind=kind, priority=priority,
                       q=q, limit=limit, offset=offset)


@router.get("/api/admin/feedback/stats")
def admin_feedback_stats(db: Session = Depends(get_db),
                         admin: User = Depends(auth.require_admin)):
    """反馈统计。"""
    return fb.stats(db)


@router.get("/api/admin/feedback/hot")
def admin_hot(days: int = Query(30, ge=1, le=365),
              threshold: int = Query(fb.POOL_THRESHOLD, ge=1, le=100),
              db: Session = Depends(get_db),
              admin: User = Depends(auth.require_admin)):
    """★ 高频问题聚合（F1-6：自动进待评估池的候选）。"""
    return fb.hot_topics(db, days=days, threshold=threshold)


class PoolIn(BaseModel):
    keyword: str
    note: str = ""


@router.post("/api/admin/feedback/pool")
def admin_push_pool(body: PoolIn, db: Session = Depends(get_db),
                    admin: User = Depends(auth.require_admin)):
    """★ 把高频问题推进待评估池（人工确认后调用）。"""
    if not (body.keyword or "").strip():
        raise HTTPException(400, "缺少关键词")
    return fb.push_to_pool(db, body.keyword.strip(), note=body.note,
                           operator=admin.username)


@router.get("/api/admin/feedback/export")
def admin_export(limit: int = Query(200, ge=1, le=2000),
                 db: Session = Depends(get_db),
                 admin: User = Depends(auth.require_admin)):
    """导出 CSV（★ 已做公式注入防护）。"""
    from fastapi.responses import Response
    rows = fb.list_all(db, limit=limit)["items"]
    csv_text = fb.export_csv(db, rows)
    return Response(
        content="\ufeff" + csv_text,          # BOM：Excel 正确识别 UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="feedback.csv"'})


# ============================================================ F2 遥测


# ★★ 注意：本路由必须注册在**所有静态路径之后**！
#   原因：FastAPI 按注册顺序匹配，`/api/admin/feedback/pool` 会被
#   `/api/admin/feedback/{fb_id}` 抢先匹配（fb_id="pool"），
#   导致 pool 端点永远返回"反馈不存在"。踩过一次，加注释防复发。

class FeedbackUpdateIn(BaseModel):
    status: str = ""
    priority: str = ""
    admin_note: str = ""
    tags: list[str] | None = None


@router.post("/api/admin/feedback/{fb_id}")
def admin_update(fb_id: str, body: FeedbackUpdateIn,
                 db: Session = Depends(get_db),
                 admin: User = Depends(auth.require_admin)):
    """状态流转 / 优先级 / 备注 / 打标。"""
    r = fb.update(db, fb_id, status=body.status, priority=body.priority,
                  admin_note=body.admin_note, tags=body.tags,
                  operator=admin.username)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "更新失败")
    return r

@router.get("/api/telemetry/disclosure")
def telemetry_disclosure(db: Session = Depends(get_db)):
    """★ 数据收集透明说明（**无需登录** —— 用户有权先看再决定）。"""
    return tm.disclosure(db)


# ================================================================ 许可证与构建信息
# ★★ v1.0 E3 开源准备：许可 / 版本边界 / 构建来源（**全部无需登录**）
#
#   ★ 为什么公开：许可证是"你需要知道才能遵守"的信息，
#     藏在登录墙后等于没告知。评估_06 §4.1 明确要求"透明"。
#   ★ 安全性：只暴露版本号/许可证类型/限额说明，
#     **不含**任何密钥、实例 ID、内部配置。



@router.get("/api/build")
def build_meta():
    """构建信息（排障用：让用户贴一次就能定位版本）。

    ★ 这是 L2 埋点 ⑥ —— 与响应头/日志/DB 共用同一份 `build_info`，
    保证各处一致（否则"日志里的构建号 ≠ 接口返回的构建号"会让人迷惑）。
    """
    from apps.api import build_info as bi
    return {**bi.build_info(), "runtime": bi.runtime_summary(),
            "deploy_type": bi.deployment_type()}


# ================================================================ 新手引导（v1.0-E4）
# ★ 设计要点（见 apps/api/onboarding.py 模块头）：
#   · 不打断、可关闭、进度**基于真实数据**（不是"点没点过按钮"）
#   · 完成即自动消失（避免"我都写完了还提示去写第一章"）

@router.get("/api/me/onboarding")
def my_onboarding(db: Session = Depends(get_db),
                  user: User = Depends(auth.get_current_user)):
    """我的上手进度（工作台引导卡片用）。"""
    from apps.api import onboarding as ob
    return ob.progress(db, user.id)


@router.post("/api/me/onboarding/hide")
def hide_onboarding(db: Session = Depends(get_db),
                    user: User = Depends(auth.get_current_user)):
    """隐藏引导（★ 尊重用户选择 —— 这是防挨骂的关键）。"""
    from apps.api import onboarding as ob
    return ob.hide(db, user.id)


@router.post("/api/me/onboarding/show")
def show_onboarding(db: Session = Depends(get_db),
                    user: User = Depends(auth.get_current_user)):
    """重新显示引导（给"我误关了"的出口）。"""
    from apps.api import onboarding as ob
    return ob.show_again(db, user.id)


@router.post("/api/me/onboarding/celebrate")
def celebrate_onboarding(db: Session = Depends(get_db),
                         user: User = Depends(auth.get_current_user)):
    """标记"已完成"（★ 返回 first=True 时前端才弹庆祝，防重复打扰）。"""
    from apps.api import onboarding as ob
    first = ob.mark_celebrated(db, user.id)
    return {"ok": True, "first": first}


@router.get("/api/me/telemetry")
def my_telemetry(db: Session = Depends(get_db),
                 user: User = Depends(auth.get_current_user)):
    """我的遥测开关状态。"""
    return {"enabled": tm.is_enabled(db),
            "instance_id": tm.instance_id(db),
            "switch_hint": "设置 → 数据与隐私 → 匿名使用统计"}


class TelemetryIn(BaseModel):
    enabled: bool


@router.post("/api/me/telemetry")
def set_my_telemetry(body: TelemetryIn, db: Session = Depends(get_db),
                     user: User = Depends(auth.get_current_user)):
    """开关遥测（用户可随时关闭）。"""
    tm.set_enabled(db, bool(body.enabled))
    return {"ok": True, "enabled": tm.is_enabled(db)}


@router.get("/api/admin/telemetry/stats")
def admin_telemetry_stats(db: Session = Depends(get_db),
                          admin: User = Depends(auth.require_admin)):
    """遥测统计（本地库）。

    ★★ v1.0-E4.5：补三块 —— 让运维能一眼看出"链路是否是活的"：
      · `scheduler`：心跳调度器状态（此前完全没有，链路可能是死的都不知道）
      · `adoption`：当前进程的采纳计数（判断生成质量的实时数据）
      · `hint`：一句话诊断（"为什么没数据"）
    """
    r = tm.stats(db)
    r["local_enabled"] = tm.is_enabled(db)

    # ★ 调度器状态（运维自查：线程活着吗？上次心跳什么时候？）
    try:
        r["scheduler"] = tm.scheduler_status()
    except Exception:                                   # noqa: BLE001
        r["scheduler"] = {"running": False, "error": "读取失败"}

    # ★ 采纳计数（当前进程内存，重启清零 —— 只反映"这次运行"）
    try:
        from apps.api import adoption as _ad
        r["adoption"] = _ad.collect_adoption()
    except Exception:                                   # noqa: BLE001
        r["adoption"] = {}

    # ★ 一句话诊断（省得运维自己拼线索）
    if not r["local_enabled"]:
        r["hint"] = ("遥测已关闭 —— 打开后才会自动上报。"
                     "开启方式：环境变量 SOZEER_TELEMETRY=on 或后台设置页。")
    elif not r.get("scheduler", {}).get("running"):
        r["hint"] = ("★ 异常：遥测已开启但调度器未运行 —— "
                     "检查服务启动日志中「遥测调度器已启动」。")
    elif not r.get("metrics"):
        r["hint"] = "调度器运行中，首次上报将在启动后 90 秒内发生。"
    else:
        r["hint"] = "链路正常。"
    return r


@router.get("/api/admin/telemetry/preview")
def admin_telemetry_preview(db: Session = Depends(get_db),
                            admin: User = Depends(auth.require_admin)):
    """★ 预览上报内容（**透明度的关键** —— 运营能自己看到发了什么）。"""
    p = tm.build_payload(db)
    ok, hits = tm.redline_check(p)
    return {"payload": p, "redline_ok": ok, "redline_hits": hits,
            "bytes": len(str(p))}


@router.post("/api/admin/telemetry/send")
def admin_telemetry_send(force: bool = False,
                         db: Session = Depends(get_db),
                         admin: User = Depends(auth.require_admin)):
    """手动触发上报（dry-run 用 force=false + 未开启时看数据结构）。"""
    return tm.maybe_heartbeat(db, force=force)


# ============================================================ F3 洞察

@router.get("/api/admin/insight/dashboard")
def admin_insight(days: int = Query(30, ge=1, le=365),
                  db: Session = Depends(get_db),
                  admin: User = Depends(auth.require_admin)):
    """★ 洞察看板（F3-1）。"""
    return insight.dashboard(db, days=days)


@router.get("/api/admin/insight/lessons")
def admin_lessons(days: int = Query(90, ge=1, le=365),
                  db: Session = Depends(get_db),
                  admin: User = Depends(auth.require_admin)):
    """★ 避坑记录（#315）：用户在聊天里「指出问题 → 系统修正」的沉淀。

    ★ 与其它洞察的区别：这里**刻意给出可读原文** —— 避坑提示要能
      直接注入提示词，必须是具体指令，哈希/分桶做不到。
    ★ 边界：仅管理端可见，不参与遥测上报。
    """
    since = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
             - dt.timedelta(days=days))
    return {"range_days": days, **insight.lessons_digest(db, since=since)}

@router.get("/api/admin/insight/suggestions")
def admin_suggestions(days: int = Query(30, ge=1, le=365),
                      db: Session = Depends(get_db),
                      admin: User = Depends(auth.require_admin)):
    """★ 改进闭环建议（F3-2）。"""
    return insight.suggest_improvements(db, days=days)


@router.get("/api/admin/insight/ab")
def admin_ab(task: str = "chapter_write",
             limit: int = Query(200, ge=10, le=2000),
             db: Session = Depends(get_db),
             admin: User = Depends(auth.require_admin)):
    """★ A/B 实验框架（F3-5）：按**模型**分组比较。"""
    return insight.ab_framework(db, task=task, limit=limit)


@router.get("/api/admin/insight/prompt-ab")
def admin_prompt_ab(top: int = Query(10, ge=1, le=50),
                    db: Session = Depends(get_db),
                    admin: User = Depends(auth.require_admin)):
    """★★ 提示词版本对比（v1.0-E4.7）—— 按**提示词指纹**分组。

    ★ 与 `/insight/ab` 的区别：
      · `/insight/ab`        → 按**模型**分组（回答"哪个模型好"）
      · `/insight/prompt-ab` → 按**提示词版本**分组（回答"改了 prompt 有没有变好"）

    ★★ 判据：看**采纳率**，不是看生成量 ——
      生成量大可能只是"用户反复重试"（质量差的表现）。

    ★ 数据范围：运行时内存计数（服务重启清零），响应里带 `scope` 说明。
    """
    return insight.prompt_ab(db, top=top)


# ---------------- 训练数据（★ 授权闸门）

@router.get("/api/admin/training/consent")
def training_consent(db: Session = Depends(get_db),
                     admin: User = Depends(auth.require_admin)):
    """训练数据授权状态。"""
    ok, why = insight.require_consent(db)
    return {"granted": ok, "reason": why,
            "key": insight.CONSENT_KEY,
            "hint": "训练数据涉及版权与个人信息，必须显式开启并咨询法务"}


class ConsentIn(BaseModel):
    enabled: bool
    user_id: str = ""


@router.post("/api/admin/training/consent")
def set_training_consent(body: ConsentIn, db: Session = Depends(get_db),
                         admin: User = Depends(auth.require_admin)):
    """开启/关闭训练数据授权（实例级或用户级）。"""
    insight.set_consent(db, bool(body.enabled), user_id=body.user_id)
    return {"ok": True, "enabled": bool(body.enabled),
            "scope": ("user:" + body.user_id) if body.user_id else "instance"}


@router.get("/api/admin/training/stats")
def training_stats(db: Session = Depends(get_db),
                   admin: User = Depends(auth.require_admin)):
    """数据集体检（F3-4 就绪度）。"""
    return insight.dataset_stats(db)


@router.get("/api/admin/training/preview")
def training_preview(limit: int = Query(3, ge=1, le=20),
                     anonymous: bool = True,
                     book_id: str = "",
                     db: Session = Depends(get_db),
                     admin: User = Depends(auth.require_admin)):
    """★ 预览训练样本（**看脱敏效果**，不导出）。"""
    r = insight.build_dataset(db, book_id=book_id, limit=limit, anon=anonymous)
    if not r.get("ok"):
        return r
    r["samples"] = r["samples"][:limit]
    r["count"] = len(r["samples"])
    return r


class ExportIn(BaseModel):
    path: str = ""
    book_id: str = ""
    limit: int = 500
    anonymous: bool = True


@router.post("/api/admin/training/export")
def training_export(body: ExportIn, db: Session = Depends(get_db),
                    admin: User = Depends(auth.require_admin)):
    """★ 导出训练集（JSONL）。**授权校验失败即拒绝**。"""
    import os
    if not body.path:
        raise HTTPException(400, "请提供导出路径")
    # ★ 路径安全：禁止覆盖关键文件
    p = os.path.abspath(body.path)
    if any(p.startswith(x) for x in ("/etc", "/boot", "/sys", "/proc")):
        raise HTTPException(400, "非法导出路径")
    r = insight.export_jsonl(db, p, book_id=body.book_id,
                             limit=body.limit, anon=body.anonymous)
    if not r.get("ok"):
        raise HTTPException(403 if r.get("consent_required") else 400,
                            r.get("error") or "导出失败")
    try:
        r["bytes"] = os.path.getsize(p)
    except Exception:                                   # noqa: BLE001
        r["bytes"] = 0
    return r

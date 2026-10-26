"""成本与性能路由（v0.23 D 组）。

- D1 模型路由：解释 / 覆盖度 / 预览
- D2 成本看板与上限
- D3 性能审计
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from apps.api import auth
from apps.api import costing
from apps.api.db import get_db
from apps.api.models import User

router = APIRouter(tags=["costing"])


# ============================================================ D1 模型路由

@router.get("/api/costing/route/explain")
def route_explain(task: str = Query(..., description="任务类型，如 chapter_write"),
                  plan: str = Query("", description="套餐 key，留空按标准档"),
                  db: Session = Depends(get_db),
                  user: User = Depends(auth.get_current_user)):
    """★ 路由解释：这个任务会用哪个模型、为什么。"""
    return costing.explain(db, task, plan)


@router.get("/api/costing/route/coverage")
def route_coverage(db: Session = Depends(get_db),
                   user: User = Depends(auth.get_current_user)):
    """角色覆盖度体检（缺哪些角色档）。"""
    return costing.coverage(db)


class RoutePreviewIn(BaseModel):
    tasks: list[str] = []
    plan: str = ""


@router.post("/api/costing/route/preview")
def route_preview(body: RoutePreviewIn, db: Session = Depends(get_db),
                  user: User = Depends(auth.get_current_user)):
    """批量预览（运营配模型时看"这些任务会走哪"）。"""
    tasks = body.tasks or ["chapter_write", "chapter_summary", "consistency_check",
                           "chapter_brief", "title"]
    return {"items": [costing.explain(db, t, body.plan) for t in tasks]}


# ============================================================ D2 成本看板

@router.get("/api/me/cost")
def my_cost(days: int = Query(30, ge=1, le=180),
            db: Session = Depends(get_db),
            user: User = Depends(auth.get_current_user)):
    """★ 我的成本报告（用户中心展示 —— 让用户知道钱花在哪）。"""
    return costing.user_report(db, user.id, days=days)


@router.get("/api/me/quota")
def my_quota(db: Session = Depends(get_db),
             user: User = Depends(auth.get_current_user)):
    """我的今日额度（前端据此显示进度条 + 提前提示）。"""
    g = costing.guard(db, user.id)
    g["usage_pct"] = (round(g["used"] / g["cap"] * 100, 1)
                      if g.get("cap", 0) > 0 else 0)
    return g


@router.get("/api/admin/costing/dashboard")
def admin_dashboard(days: int = Query(7, ge=1, le=180),
                    db: Session = Depends(get_db),
                    admin: User = Depends(auth.require_admin)):
    """★ 成本看板（管理后台）。"""
    return costing.dashboard(db, days=days)


@router.get("/api/admin/costing/users/{user_id}")
def admin_user_cost(user_id: str, days: int = Query(30, ge=1, le=180),
                    db: Session = Depends(get_db),
                    admin: User = Depends(auth.require_admin)):
    """单用户成本明细（排查"谁在烧钱"）。"""
    return costing.user_report(db, user_id, days=days)


@router.get("/api/admin/costing/spikes")
def admin_spikes(hours: int = Query(1, ge=1, le=24),
                 db: Session = Depends(get_db),
                 admin: User = Depends(auth.require_admin)):
    """★ 异常消耗检测（突增 10x）。"""
    from sqlalchemy import select
    from apps.api.models import LLMCall, User as U
    # 只看近 N 小时有调用的用户（避免全表扫描所有历史用户）
    import datetime as dt
    # ★ 台账存 UTC，查询也必须用 UTC（否则 8 小时偏移）
    since = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(hours=hours)
    uids = [r for r in db.scalars(
        select(LLMCall.user_id).where(LLMCall.created_at >= since,
                                      LLMCall.user_id != "").distinct()).all()]
    items = []
    for uid in uids[:200]:                       # 上限保护
        s = costing.check_spike(db, uid, hours=hours)
        if s["is_spike"]:
            u = db.get(U, uid)
            items.append({"user_id": uid,
                          "username": (u.username if u else ""), **s})
    items.sort(key=lambda x: -x["ratio"])
    return {"hours": hours, "count": len(items), "items": items}


@router.post("/api/admin/costing/prune")
def admin_prune(keep_days: int = Query(180, ge=30, le=3650),
                db: Session = Depends(get_db),
                admin: User = Depends(auth.require_admin)):
    """清理老台账（管理操作）。"""
    n = costing.prune(db, keep_days=keep_days)
    return {"ok": True, "deleted": n, "keep_days": keep_days}


# ============================================================ D3 性能

@router.get("/api/admin/perf/report")
def admin_perf_report(db: Session = Depends(get_db),
                      admin: User = Depends(auth.require_admin)):
    """★ 性能综合报告（慢查询 / 索引 / N+1 / 表体积）。"""
    from apps.api import perf
    return perf.report(db)


@router.get("/api/admin/perf/indexes")
def admin_perf_indexes(db: Session = Depends(get_db),
                       admin: User = Depends(auth.require_admin)):
    """索引审计（缺哪些索引会导致全表扫描）。"""
    from apps.api import perf
    return perf.audit_indexes(db.get_bind())


@router.post("/api/admin/perf/reset")
def admin_perf_reset(db: Session = Depends(get_db),
                     admin: User = Depends(auth.require_admin)):
    """清空采样（改完优化后对比前后）。"""
    from apps.api import perf
    perf.collector().reset()
    perf.nplusone().reset()
    return {"ok": True}

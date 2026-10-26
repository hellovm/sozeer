"""任务队列路由（v0.25 J0 组）。

J0-1 Redis 状态 → 健康检查 + 配置
J0-2 降级重建   → 手动重建 + 队列监控 + 死信处理
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from apps.api import auth
from apps.api import queue as q
from apps.api.db import get_db
from apps.api.models import User

router = APIRouter(tags=["queue"])


# ============================================================ 健康

@router.get("/api/queue/health")
def queue_health():
    """★ 队列健康状态（**无需登录** —— 前端据此决定"排队"还是"直跑"）。

    为什么不鉴权：这只是"队列是否可用"的布尔信息，不含业务数据，
    且未登录页也要能显示正确的交互模式。
    """
    st = q.status()
    return {
        "available": st.get("available", False),
        "mode": st.get("mode", "degraded"),
        "hint": st.get("hint", ""),
        "queued": st.get("queued", 0),
        "running": st.get("running", 0),
    }


# ============================================================ 管理端

@router.get("/api/admin/queue/status")
def admin_status(db: Session = Depends(get_db),
                 admin: User = Depends(auth.require_admin)):
    """★ 队列状态（含 Redis 配置详情）。"""
    st = q.status()
    st["stats"] = q.stats()
    return st


@router.get("/api/admin/queue/peek")
def admin_peek(limit: int = Query(20, ge=1, le=100),
               db: Session = Depends(get_db),
               admin: User = Depends(auth.require_admin)):
    """查看队首任务（调试用）。"""
    return {"items": q.peek(limit=limit)}


@router.get("/api/admin/queue/dlq")
def admin_dlq(db: Session = Depends(get_db),
              admin: User = Depends(auth.require_admin)):
    """死信队列（重试耗尽的任务）。"""
    try:
        import json
        cli = q.client()
        if cli is None:
            return {"items": [], "note": "队列不可用"}
        raw = cli.lrange(q.K_DLQ, 0, 99) or []
        items = []
        for x in raw:
            try:
                items.append(json.loads(x))
            except Exception:                           # noqa: BLE001
                items.append({"raw": str(x)[:200]})
        return {"items": items, "count": len(items)}
    except Exception as e:                              # noqa: BLE001
        return {"items": [], "error": str(e)[:200]}


class ClearDlqIn(BaseModel):
    reason: str = ""


@router.post("/api/admin/queue/clear-dlq")
def admin_clear_dlq(body: ClearDlqIn | None = None,
                    db: Session = Depends(get_db),
                    admin: User = Depends(auth.require_admin)):
    """清空死信（★ 人工确认后调用，留痕原因）。

    ★ 为什么要记原因：死信清空是**不可恢复**操作。
      日后复盘"当时为什么清掉"时，原因比清了多少条更重要。
    """
    n = q.clear_dlq()
    reason = (body.reason if body else "") or ""
    # ★ 走唯一审计入口（admin_service.write_audit）—— 它会保证"失败也留痕"
    try:
        from apps.api import admin_service as _as
        _as.write_audit(db, operator=admin, action="queue.clear_dlq",
                        target_type="queue", target_id="dlq",
                        detail=f"清空 {n} 条死信", reason=reason)
    except Exception:                                   # noqa: BLE001
        pass
    return {"ok": True, "cleared": n, "operator": admin.username,
            "reason": reason}


class RebuildIn(BaseModel):
    limit: int = 5000
    dry_run: bool = True


@router.post("/api/admin/queue/rebuild")
def admin_rebuild(body: RebuildIn, db: Session = Depends(get_db),
                  admin: User = Depends(auth.require_admin)):
    """★★ 从 DB 重建队列（J0-2 核心能力）。

    ★ 默认 `dry_run=True` —— 重建会改任务状态，必须先让运维看清"会改什么"。
      确认无误后传 `dry_run=false` 真正执行。
    """
    r = q.rebuild_from_db(db, limit=body.limit, dry_run=body.dry_run)
    r["operator"] = admin.username
    return r


@router.post("/api/admin/queue/reap")
def admin_reap(timeout_seconds: int = Query(1800, ge=60, le=86400),
               db: Session = Depends(get_db),
               admin: User = Depends(auth.require_admin)):
    """★ 回收超时任务（worker 崩溃遗留的僵尸）。"""
    return q.reap_stale(timeout_seconds=timeout_seconds)


@router.post("/api/admin/queue/reset")
def admin_reset(db: Session = Depends(get_db),
                admin: User = Depends(auth.require_admin)):
    """重置连接状态（Redis 恢复后手动加速重连）。"""
    q.reset()
    return {"ok": True, "status": q.status(), "operator": admin.username}


# ============================================================ 限流后台

@router.get("/api/admin/ratelimit/status")
def admin_ratelimit_status(db: Session = Depends(get_db),
                           admin: User = Depends(auth.require_admin)):
    """★ 限流器状态（含后端类型 —— 运维必须能看到"计数是否共享"）。"""
    from apps.api import ratelimit as rl
    st = rl.stats()
    st["hint"] = ("Redis 后端：多 worker 下计数共享，可安全扩容"
                  if st.get("multi_worker_safe")
                  else "内存后端：★ 多 worker 下实际额度 = 配置 × worker 数")
    return st


class BackendIn(BaseModel):
    backend: str


@router.post("/api/admin/ratelimit/backend")
def admin_set_backend(body: BackendIn, db: Session = Depends(get_db),
                      admin: User = Depends(auth.require_admin)):
    """切换限流后端（redis / memory / auto）。

    ★ 为什么不重启服务就能切：Redis 临时故障时运维可强制切内存，
      避免限流器因 Redis 抖动而反复探测。
    """
    from apps.api import ratelimit as rl
    r = rl.set_backend(body.backend)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "切换失败")
    r["operator"] = admin.username
    return r


# ============================================================ v0.25.6 J5/J9/J10

@router.get("/api/admin/queue/concurrency")
def admin_concurrency(db: Session = Depends(get_db),
                      admin: User = Depends(auth.require_admin)):
    """★ 并发上限与实际占用（J5）。

    ★ 为什么单独一个接口：看板上"还有多少余量"是运维最常看的信息，
      和队列统计分开能让前端按需刷新（这个便宜，可高频）。
    """
    st = q.stats()
    lim = q.concurrency_limits(db)
    conc = st.get("concurrency") or {}
    return {
        "limits": lim,
        "in_flight": st.get("running", 0),
        "queued": st.get("queued", 0),
        "saturation": conc.get("saturation", 0),
        "top_users": conc.get("top_users") or [],
        "active_books": conc.get("active_books", 0),
        "hint": ("未限制并发（0 = 不限）" if lim["global"] == 0
                 else f"全局 {st.get('running', 0)}/{lim['global']}，"
                      f"单用户上限 {lim['per_user']}，单书上限 {lim['per_book']}"),
    }


@router.get("/api/admin/queue/dashboard")
def admin_queue_dashboard(limit: int = 30, db: Session = Depends(get_db),
                          admin: User = Depends(auth.require_admin)):
    """★ 队列监控看板数据（J9）—— 一次拿全，减少前端请求数。"""
    st = q.stats()
    return {
        **st,
        "peek": q.peek(limit=max(1, min(int(limit or 30), 100))),
        "limits": q.concurrency_limits(db),
        "dlq_items": _dlq_preview(20),
    }


def _dlq_preview(limit: int = 20) -> list[dict]:
    """死信预览（★ 解析失败也要返回原始串，便于排障）。"""
    cli = q.client()
    if cli is None:
        return []
    out: list[dict] = []
    try:
        for raw in (cli.lrange(q.K_DLQ, 0, max(0, limit - 1)) or []):
            try:
                out.append(json.loads(raw))
            except Exception:                           # noqa: BLE001
                out.append({"raw": str(raw)[:300]})
    except Exception:                                   # noqa: BLE001
        pass
    return out


@router.get("/api/admin/queue/alerts")
def admin_queue_alerts(db: Session = Depends(get_db),
                       admin: User = Depends(auth.require_admin)):
    """★ 队列告警检查（J10）—— 手动触发，也可由定时任务调用。"""
    return q.check_alerts(db)


@router.post("/api/admin/queue/alerts/reset")
def admin_alerts_reset(db: Session = Depends(get_db),
                       admin: User = Depends(auth.require_admin)):
    """清空告警节流（★ 排障后想立刻收到新告警时用）。"""
    q.alert_state_reset()
    return {"ok": True, "operator": admin.username,
            "note": "已重置节流，下次检查若仍有问题会立即重发"}


# ============================================================ v0.25.6 J8 用户端

@router.get("/api/queue/position")
def queue_position_api(task_id: str = Query("", description="任务 ID"),
                       db: Session = Depends(get_db)):
    """★ 查排队位置与 ETA（J8 用户端）。

    ★ 为什么不需要登录：任务 ID 本身是随机不可猜的凭据
      （`t_` + 12 位随机 hex），知道 ID 就说明是发起者。
      强制登录会让"排队进度条"在令牌过期时直接失效（体验很差）。
      ★ 且接口不返回任何敏感信息（只有位置与时间估算）。
    """
    tid = (task_id or "").strip()
    if not tid:
        raise HTTPException(400, "缺少 task_id")
    if len(tid) > 128:
        raise HTTPException(400, "task_id 过长")
    return q.queue_position(tid)


@router.get("/api/queue/mine")
async def my_queue_items(request: Request, db: Session = Depends(get_db)):
    """★ 我的排队中任务（J8：支持"后台运行"后回来查看）。

    ★ 为什么要这个：用户点了"后台运行"就离开了页面，
      回来时要能看到"还在排队/已完成"，否则会以为丢了。
    ★ 未登录返回空列表而非 401 —— 前端不必为"顺便查一下"处理异常。
    """
    from apps.api import auth as _auth
    uid = ""
    try:
        ah = request.headers.get("Authorization", "")
        tok = ah[7:] if ah.lower().startswith("bearer ") else ""
        uid = _auth.parse_token(tok) if tok else None or ""
    except Exception:                                   # noqa: BLE001
        uid = ""
    if not uid:
        return {"items": [], "count": 0, "note": "未登录"}
    from apps.api.models import User as _User
    u = db.get(_User, uid)
    if u is None or not u.is_active:
        return {"items": [], "count": 0, "note": "账号不可用"}
    cli = q.client()
    if cli is None:
        return {"items": [], "available": False}
    items: list[dict] = []
    try:
        for tid, sc in (cli.zrange(q.K_QUEUE, 0, -1, withscores=True) or []):
            raw = cli.hget(q.K_TASK, tid)
            if not raw:
                continue
            d = json.loads(raw)
            if d.get("user_id") != u.id:
                continue
            d["score"] = float(sc)
            items.append(d)
            if len(items) >= 20:
                break
    except Exception:                                   # noqa: BLE001
        pass
    out = []
    for d in items:
        pos = q.queue_position(d.get("task_id", ""))
        out.append({**d, "position": pos.get("position"),
                    "eta_text": pos.get("eta_text", "")})
    return {"items": out, "count": len(out)}


# ============================================================ v0.25.7 J11 全链路

@router.get("/api/admin/queue/admission")
def admin_admission(user_id: str = "", book_id: str = "",
                    db: Session = Depends(get_db),
                    admin: User = Depends(auth.require_admin)):
    """★ 准入预检（J11）—— 不占位，只看"现在能不能接新任务"。

    ★ 为什么需要"只查不占"的接口：
      运维/客服在用户报"一直排队"时，需要**只读**地看当前是否已满，
      而不是真的占一个位（那会让问题更糟）。

    ★ 与 `/api/admin/queue/concurrency` 的区别：
      那个看的是"全局余量"；这个可以**指定用户/书**做模拟预检，
      用于回答"这个用户现在提交会排队吗"。
    """
    lim = q.concurrency_limits(db)
    cli = q.client()
    if cli is None:
        return {"ok": True, "can_admit": True, "degraded": True,
                "reason": "队列不可用（将直接执行）", "limits": lim}

    running = int(cli.zcard(q.K_RUNNING) or 0)
    blockers: list[dict] = []
    if lim["global"] > 0 and running >= lim["global"]:
        blockers.append({"scope": "global", "current": running,
                         "limit": lim["global"],
                         "text": f"平台并发已满（{running}/{lim['global']}）"})
    if user_id and lim["per_user"] > 0:
        un = int(cli.hget(q.K_USER_RUN, user_id) or 0)
        if un >= lim["per_user"]:
            blockers.append({"scope": "user", "current": un,
                             "limit": lim["per_user"],
                             "text": f"该用户已有 {un} 个任务在跑"})
    if book_id and lim["per_book"] > 0:
        bn = int(cli.hget(q.K_BOOK_RUN, book_id) or 0)
        if bn >= lim["per_book"]:
            blockers.append({"scope": "book", "current": bn,
                             "limit": lim["per_book"],
                             "text": "该书籍还有任务在跑（同书串行）"})

    return {"ok": True, "can_admit": not blockers, "blockers": blockers,
            "limits": lim, "in_flight": running, "queued": int(cli.zcard(q.K_QUEUE) or 0)}


@router.get("/api/admin/queue/lanes")
def admin_lanes(db: Session = Depends(get_db),
                admin: User = Depends(auth.require_admin)):
    """★ 泳道配比（J3 加权轮询）说明与当前分布。

    ★ 为什么要暴露"配比"给运维看：
      配比是**隐式**实现的（靠时间偏移），不看代码看不出来。
      明确展示"fast 等 0 秒 / standard 等 8 秒 / batch 等 24 秒"，
      运维才能判断"standard 用户抱怨慢"是不是配比问题。
    """
    st = q.stats()
    return {
        "lanes": list(q.LANES),
        "round_seconds": q.LANE_ROUND_SECONDS,
        "offsets": q.LANE_WEIGHT,
        "ratio": "6:3:1（fast:standard:batch）",
        "explain": (
            f"空载时 fast 优先；当 fast 持续有任务时，"
            f"standard 等 {q.LANE_ROUND_SECONDS} 秒、"
            f"batch 等 {q.LANE_ROUND_SECONDS * 3} 秒后会被自动晋升，"
            f"从而逼近 6:3:1 的实际出队比例"
        ),
        "by_lane": st.get("by_lane") if st.get("available") else {},
        "kind_map": {
            "fast": ["title", "tag", "summary_short", "classify", "ping"],
            "standard": ["draft", "chat", "polish", "rewrite", "summary"],
            "batch": ["audit_book", "batch", "draft_batch", "drama_batch"],
        },
    }

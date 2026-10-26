"""成本路由与用量台账（v0.23 D 组）。

★ 核心目标：**毛利保卫** —— token 成本降 30%+，且绝不跑飞。

三个阶段：
  D1 按角色路由模型  → 便宜模型干便宜活（本模块 route/route_for_task）
  D2 成本看板与硬上限 → LLMCall 台账 + daily_cap 拦截（record/dashboard/guard）
  D3 性能优化        → 索引与慢查询（见 perf.py）

★ 为什么单独建模块而不是塞进 llm.py：
  llm.py 是**纯网关**（管协议、重试、流式），不该管业务策略。
  路由策略会经常变（模型降价、新模型上线），策略变更不应触碰网关代码。
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.models import LLMCall, ModelProfile
from apps.api.models import now as _now

_log = logging.getLogger("novelforge.costing")

# ----------------------------------------------------------------- 任务 → 角色

# ★ 任务类型 → 模型角色映射（D1 的核心表）
#
# 为什么这样分：
#   · writer  写正文 / 精修 / 对话生成 / 剧本 → **质量直接决定产品价值**，用强模型
#   · planner 大纲 / 任务单 / 分集规划 / 封面提示词 → 结构化输出，中等模型够
#   · critic  摘要 / 实体提取 / 一致性检查 / 打分 / 审核 → **量大且可容忍偏差**，便宜模型
#   · utility 标题 / 分类 / 打标签 / 短判断 → 极短输出，最便宜的够用
#
# ★ 预期收益：摘要/检查类调用占比 40%+，这部分降级就能省 30-50%
TASK_ROLE: dict[str, str] = {
    # ---- writer：质量优先 ----
    "chapter_write": "writer",
    "chapter_polish": "writer",
    "polish": "writer",
    "rewrite": "writer",
    "retake": "writer",
    "scene_write": "writer",
    "script_write": "writer",
    "dialogue": "writer",
    # ---- planner：结构化 ----
    "chapter_brief": "planner",
    "outline": "planner",
    "outline_expand": "planner",
    "episode_plan": "planner",
    "cover_prompt": "planner",
    "storyboard": "planner",
    "scene_split": "planner",
    # ---- critic：量大从简 ----
    "summary": "critic",
    "chapter_summary": "critic",
    "rolling_summary": "critic",
    "entity_extract": "critic",
    "consistency_check": "critic",
    "conflict_detect": "critic",
    "audit": "critic",
    "score": "critic",
    "gate": "critic",
    "deai_check": "critic",
    "knowledge_extract": "critic",
    "relation_extract": "critic",
    "emotion_analyze": "critic",
    # ---- utility：极短输出 ----
    "title": "utility",
    "tagging": "utility",
    "classify": "utility",
    "short_judge": "utility",
}

# 角色兜底链：找不到精确角色时依次回退
ROLE_FALLBACK: dict[str, list[str]] = {
    "utility": ["utility", "critic", "planner", "writer", "default"],
    "critic": ["critic", "planner", "writer", "default"],
    "planner": ["planner", "writer", "default"],
    "writer": ["writer", "planner", "default"],
    "default": ["default", "writer", "planner", "critic"],
}

# 用户套餐分层：免费版强制走便宜档（D1 的"分层走便宜模型"）
PLAN_TIER: dict[str, str] = {
    "free": "basic",      # 免费版：只给 critic/utility 档
    "basic": "std",       # 基础版：正常路由
    "pro": "std",
    "max": "std",
    "": "std",
}

# 免费版允许的角色（其余降级到 critic）
FREE_TIER_ROLES = {"critic", "utility", "default"}


def role_for_task(task: str) -> str:
    """任务类型 → 模型角色。未知任务给 default（不猜）。"""
    t = (task or "").strip().lower()
    if t in TASK_ROLE:
        return TASK_ROLE[t]
    # 前缀匹配兜底：chapter_write_v2 → chapter_write
    for key in sorted(TASK_ROLE, key=len, reverse=True):
        if t.startswith(key):
            return TASK_ROLE[key]
    return "default"


def _all_profiles(db: Session) -> list[ModelProfile]:
    return list(db.scalars(select(ModelProfile).where(ModelProfile.enabled == 1)).all())


def resolve(db: Session, role: str, *,
            tier: str = "std", plan_key: str = "") -> ModelProfile | None:
    """核心路由：角色 + 分层 → 具体模型。

    ★ 为什么"降级链"比"精确匹配"重要：
      运营可能只配了 writer + default 两个模型。
      此时 critic 任务必须能落到某个可用模型上，而不是报错。
      降级链保证**任何角色都有模型可用**（最差落到 default）。

    ★★ v0.26.2：接入 model_router 的**套餐路由**（实现替换，签名冻结 §1）。
      新增能力：不同套餐可指定不同模型（免费版走便宜模型）。
      兼容：套餐没配路由时行为与升级前**完全一致**（走原有 chain 逻辑）。
    """
    # ① v0.26.2：套餐级路由优先（运营在后台显式配置的意图最强）
    if plan_key:
        try:
            from apps.api import model_router as _mr
            _p = _mr.resolve_profile(db, role, plan_key=plan_key)
            if _p is not None and _p.enabled:
                return _p
        except Exception:                              # noqa: BLE001
            pass                                       # 路由异常不阻断，退回旧逻辑

    rows = _all_profiles(db)
    if not rows:
        return None

    # 免费版强制降级到便宜档
    if tier == "basic" and role not in FREE_TIER_ROLES:
        role = "critic"

    chain = ROLE_FALLBACK.get(role) or [role, "default"]
    by_role: dict[str, ModelProfile] = {}
    for r in rows:
        by_role.setdefault(r.role or "default", r)

    for candidate in chain:
        if candidate in by_role:
            return by_role[candidate]
    return rows[0]


def route_for_task(db: Session, task: str, *,
                   plan: str = "", user_id: str = "") -> ModelProfile | None:
    """业务侧唯一入口：给任务名，拿到该用的模型。"""
    role = role_for_task(task)
    tier = PLAN_TIER.get((plan or "").strip().lower(), "std")
    m = resolve(db, role, tier=tier, plan_key=(plan or "").strip().lower())
    if m is not None:
        return m
    # 兜底：完全没配模型时用既有 pick_model（保持旧行为）
    try:
        from apps.api.orchestrator.graph import pick_model
        return pick_model(db, role)
    except Exception:                                  # noqa: BLE001
        return None


def explain(db: Session, task: str, plan: str = "") -> dict:
    """路由解释（配置中心/调试用）。★ 让"为什么用这个模型"可追溯。

    ★ db 允许为 None（纯策略查询，不查库）—— 前端配置页常见"还没配模型
      就想看策略表"的场景，此时不该报 500。
    """
    role = role_for_task(task)
    tier = PLAN_TIER.get((plan or "").strip().lower(), "std")
    effective_role = role
    if tier == "basic" and role not in FREE_TIER_ROLES:
        effective_role = "critic"
    m = None
    if db is not None:
        try:
            m = resolve(db, role, tier=tier)
        except Exception:                                # noqa: BLE001
            m = None
    return {
        "task": task, "role": role, "tier": tier,
        "effective_role": effective_role,
        "downgraded": effective_role != role,
        "model_id": (m.model_id if m else ""),
        "model_name": (m.name if m else ""),
        "price_in": (m.price_in if m else 0),
        "price_out": (m.price_out if m else 0),
        "fallback_chain": ROLE_FALLBACK.get(effective_role, []),
    }


def coverage(db: Session) -> dict:
    """角色覆盖度：运营配了几个角色、缺哪些（配置体检）。"""
    rows = _all_profiles(db)
    have = {r.role or "default" for r in rows}
    roles = ["writer", "planner", "critic", "utility", "default"]
    missing = [r for r in roles if r not in have]
    # 各角色价差（省钱空间估算）
    prices = {}
    for r in rows:
        prices[r.role or "default"] = {
            "model": r.model_id, "in": r.price_in, "out": r.price_out}
    return {"total": len(rows), "have": sorted(have), "missing": missing,
            "roles": prices,
            "task_map": TASK_ROLE,
            "note": ("" if rows else "尚未配置任何模型"),
            }


# ----------------------------------------------------------------- D2 用量台账

def estimate_cost(m: ModelProfile | None, tin: int, tout: int) -> float:
    if m is None:
        return 0.0
    return round(int(tin or 0) / 1_000_000 * float(m.price_in or 0)
                 + int(tout or 0) / 1_000_000 * float(m.price_out or 0), 6)


def record(db: Session, *, task: str, role: str = "", user_id: str = "",
           book_id: str = "", model: ModelProfile | None = None,
           model_id: str = "", input_tokens: int = 0, output_tokens: int = 0,
           cost: float | None = None, duration_ms: int = 0,
           status: str = "ok", error: str = "", run_id: str = "",
           commit: bool = True) -> LLMCall:
    """写一行 LLM 调用台账。

    ★ 为什么必须落库（而不是只在内存计数）：
      1. 成本看板要按**日/用户/功能**聚合 → 必须可查询
      2. 硬上限要跨进程/跨重启生效 → 内存计数重启即归零（可被绕过）
      3. 异常消耗检测要**对比历史基线** → 需要历史数据
    """
    mid = model_id or (model.model_id if model else "")
    if cost is None:
        cost = estimate_cost(model, input_tokens, output_tokens)
    row = LLMCall(
        id=f"lc_{_new_id()}", task=(task or "")[:64], role=(role or "")[:32],
        user_id=user_id or "", book_id=book_id or "", model_id=mid[:128],
        input_tokens=int(input_tokens or 0), output_tokens=int(output_tokens or 0),
        cost=float(cost or 0.0), duration_ms=int(duration_ms or 0),
        status=status or "ok", error=(error or "")[:512], run_id=run_id or "",
        created_at=_now(),
    )
    # ★ 记账是**旁路**，任何失败（含 add 阶段的连接故障）都不得冒泡。
    #   最初只包了 commit，漏了 add —— 连接池耗尽时 add 也会抛，
    #   结果"记账失败"变成"生成失败"。这里统一兜住。
    try:
        db.add(row)
        if commit:
            db.commit()
    except Exception as e:                              # noqa: BLE001
        _log.warning("用量记账失败（不影响主流程）：%s: %s", type(e).__name__, e)
        try:
            db.rollback()
        except Exception:
            pass
    return row


def _new_id() -> str:
    import uuid
    return uuid.uuid4().hex[:16]


def _now_utc() -> dt.datetime:
    """★ 统一用 **UTC** 取当前时间。

    为什么必须统一：`models.now()` 存 UTC（`datetime.now(timezone.utc)`，
    但 PG/SQLite 的 DateTime 列是 naive，存进去就是 UTC 的墙钟值）。
    如果这里用 `datetime.now()`（本地时间 UTC+8）去比较，
    会**多算 8 小时** —— "最近 1 小时"查不到刚写入的数据，
    日上限也会在每天 08:00 而不是 00:00 重置。
    """
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _day_start(days_ago: int = 0) -> dt.datetime:
    d = _now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    return d - dt.timedelta(days=days_ago)


# ----------------------------------------------------------------- 硬上限

# ★ 默认日上限（token）。可通过 Setting 表覆盖：cost.daily_token_cap.<plan>
DEFAULT_DAILY_TOKEN_CAP = 2_000_000          # 200 万 token/日（约 20 本 3 千字章节）
CAP_BY_PLAN = {
    "free": 200_000,
    "basic": 2_000_000,
    "pro": 8_000_000,
    "max": 30_000_000,
}


def daily_usage(db: Session, user_id: str) -> dict:
    """当日用量（token + 成本 + 调用数）。"""
    since = _day_start()
    row = db.execute(
        select(func.coalesce(func.sum(LLMCall.input_tokens), 0),
               func.coalesce(func.sum(LLMCall.output_tokens), 0),
               func.coalesce(func.sum(LLMCall.cost), 0.0),
               func.count(LLMCall.id))
        .where(LLMCall.user_id == user_id, LLMCall.created_at >= since)
    ).one()
    tin, tout, cost, calls = int(row[0]), int(row[1]), float(row[2]), int(row[3])
    return {"input_tokens": tin, "output_tokens": tout, "tokens": tin + tout,
            "cost": round(cost, 4), "calls": calls,
            "date": since.strftime("%Y-%m-%d")}


def _cap_for(db: Session, user_id: str) -> int:
    """取该用户的日上限。优先级：套餐覆盖 > 全局配置 > 套餐默认。

    ★ v0.25.4 I7-I10：全局值改走**配置中心**（`cost.daily_token_cap`），
      这样界面改完立即生效、有历史可回滚；`cost.daily_token_cap.<plan>`
      仍保留（运营对单个套餐做差异化）。
    ★ 为什么保留两层而不是只留全局：
      免费版与付费版的成本承受力差一个数量级，一刀切会让免费版
      被刷爆（或付费版额度不够用）。
    """
    try:
        from apps.api.models import Setting, User
        from apps.api import config_center as cc
        u = db.get(User, user_id)
        plan = ""
        if u is not None:
            # 从最近订单/账户推套餐（无订单则视为 free）
            from apps.api.models import Order
            o = db.scalar(select(Order).where(Order.user_id == user_id,
                                              Order.status == "paid")
                          .order_by(Order.created_at.desc()))
            plan = (getattr(o, "plan_key", "") or "") if o else ""
        # ① 套餐专属覆盖（Setting 表，保留原行为）
        s = db.get(Setting, f"cost.daily_token_cap.{plan}") if plan else None
        if s is not None and str(s.value or "").strip() != "":
            try:
                return int(str(s.value).replace(",", ""))
            except (ValueError, TypeError):
                pass
        # ② 全局值：★ 兼容旧写法直接写 Setting 的场景（老代码/运维脚本/测试）
        #    ★★ 语义：**0 = 不限制**（保持 v0.23 原行为，不引入隐式兜底）。
        #    理由：运维写 0 的意图就是"关掉限制"，此时若偷偷给免费版套 20 万，
        #    会出现"我明明关了限制但用户还是被拦"的困惑 —— 显式值必须忠实执行。
        raw_global = db.get(Setting, "cost.daily_token_cap")
        if raw_global is not None and str(raw_global.value or "").strip() != "":
            try:
                return int(str(raw_global.value).replace(",", ""))
            except (ValueError, TypeError):
                pass
        # ③ 配置中心注册表值
        #    ★ 与 ② 的区别：注册表**默认值**（200 万）不是"运维显式设的"，
        #      所以这里允许"值被显式改成 0"时回落到套餐分层（保留免费版保护），
        #      而 ② 的 Setting 是显式写入 → 忠实执行（含 0 = 不限制）。
        from apps.api.models import Setting as _S
        raw_reg = db.get(_S, "cost.daily_token_cap")
        g = int(cc.get_config(db, "cost.daily_token_cap",
                              DEFAULT_DAILY_TOKEN_CAP) or 0)
        if g <= 0 and raw_reg is None:
            # 未显式配置 → 按套餐分层（免费版 20 万，专业版 800 万…）
            return CAP_BY_PLAN.get(plan or "free", DEFAULT_DAILY_TOKEN_CAP)
        return g
    except Exception:                                    # noqa: BLE001
        return DEFAULT_DAILY_TOKEN_CAP


def global_cap(db: Session) -> int:
    """全站日上限（配置中心 `cost.global_daily_cap`，0 = 不限制）。

    ★ 这是"保命开关"：成本异常飙升时，运维用它一键止血，
      而不是去停服务（停服影响所有正常用户）。
    """
    try:
        from apps.api import config_center as cc
        return int(cc.get_config(db, "cost.global_daily_cap", 0) or 0)
    except Exception:                                    # noqa: BLE001
        return 0


def platform_usage_today(db: Session) -> int:
    """全站当日 token 用量（用于全局上限判定）。"""
    since = _day_start()
    row = db.execute(
        select(func.coalesce(func.sum(LLMCall.input_tokens), 0),
               func.coalesce(func.sum(LLMCall.output_tokens), 0))
        .where(LLMCall.created_at >= since)).one()
    return int(row[0]) + int(row[1])


def guard(db: Session, user_id: str, *, need_tokens: int = 0) -> dict:
    """★ 生成前的硬上限检查。

    返回 {allowed, used, cap, remain, reason}

    ★ 设计取舍：
      · **预估需求**（need_tokens）也计入 → 防止"差一点就超"的边界反复触发
      · 超限不抛异常，返回结构化结果 → 调用方决定是拒绝还是降级
      · 上限为 0 或负数 = 不限制（自建/内部账号）
    ★ v0.25.4 I7-I10：先判**全站上限**（保命开关），再判用户上限。
      顺序很重要 —— 全站超了的话，逐个用户判也没意义。
    """
    # ① 全站上限（配置中心，0 = 不限制）★ 保命开关优先
    gcap = global_cap(db)
    if gcap > 0:
        gused = platform_usage_today(db)
        if gused >= gcap:
            return {"allowed": False, "used": gused, "cap": gcap,
                    "remain": 0, "unlimited": False, "scope": "global",
                    "reason": (f"平台今日总用量已达上限（{gused:,}/{gcap:,}），"
                               "生成已暂停以控制成本。请联系管理员。")}
    cap = _cap_for(db, user_id)
    if cap <= 0:
        return {"allowed": True, "used": 0, "cap": 0, "remain": -1,
                "reason": "", "unlimited": True, "scope": "user"}
    use = daily_usage(db, user_id)
    used = use["tokens"]
    remain = cap - used
    if remain <= 0:
        return {"allowed": False, "used": used, "cap": cap, "remain": 0,
                "scope": "user",
                "reason": (f"今日 token 用量已达上限（{used:,}/{cap:,}）。"
                           "请明日再试，或升级套餐提高额度。"),
                "unlimited": False}
    if need_tokens and need_tokens > remain:
        return {"allowed": False, "used": used, "cap": cap, "remain": remain,
                "scope": "user",
                "reason": (f"本次预估需 {need_tokens:,} token，"
                           f"今日仅剩 {remain:,}（上限 {cap:,}）。"
                           "请减少章节长度或明日再试。"),
                "unlimited": False}
    return {"allowed": True, "used": used, "cap": cap, "remain": remain,
            "reason": "", "unlimited": False, "scope": "user"}


def check_spike(db: Session, user_id: str, *, hours: int = 1,
                factor: float = 10.0) -> dict:
    """异常消耗检测（D2 要求：突增 10x 告警）。

    ★ 对比基线怎么选：
      用"过去 7 天的**同时段**均值"而不是"昨天总量"——
      用户白天写作、夜里不写，用总量算会把正常的高峰误判为突增。
      同时段（±1h）对比更贴近真实节奏。
    """
    now = _now_utc()
    cur = db.execute(
        select(func.coalesce(func.sum(LLMCall.input_tokens + LLMCall.output_tokens), 0))
        .where(LLMCall.user_id == user_id,
               LLMCall.created_at >= now - dt.timedelta(hours=hours))
    ).scalar() or 0
    # 基线：过去 7 天同一时段
    base_vals = []
    for d in range(1, 8):
        s = now - dt.timedelta(days=d, hours=hours)
        e = now - dt.timedelta(days=d)
        v = db.execute(
            select(func.coalesce(func.sum(LLMCall.input_tokens + LLMCall.output_tokens), 0))
            .where(LLMCall.user_id == user_id,
                   LLMCall.created_at >= s, LLMCall.created_at < e)
        ).scalar() or 0
        base_vals.append(int(v))
    base_vals = [v for v in base_vals if v > 0]
    base = (sum(base_vals) / len(base_vals)) if base_vals else 0
    cur = int(cur)
    spike = bool(base and cur >= base * factor)
    return {"current": cur, "baseline_avg": int(base), "factor": factor,
            "is_spike": spike,
            "ratio": round(cur / base, 2) if base else 0,
            "samples": len(base_vals)}


# ----------------------------------------------------------------- 看板

def dashboard(db: Session, *, days: int = 7, user_id: str = "") -> dict:
    """成本看板（D2 核心）。

    维度：
      · 按日趋势（成本 + token + 调用数）
      · 按功能（task）分布 —— ★ 这个维度决定"该降级哪个功能"
      · 按用户 TOP 消耗
      · 按模型分布（验证路由是否生效）
    """
    since = _day_start(max(0, days - 1))
    q = select(LLMCall).where(LLMCall.created_at >= since)
    if user_id:
        q = q.where(LLMCall.user_id == user_id)
    rows = list(db.scalars(q).all())

    by_day: dict[str, dict] = {}
    by_task: dict[str, dict] = {}
    by_user: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_role: dict[str, dict] = {}
    err = 0

    def _acc(d: dict, key: str, r) -> None:
        it = d.setdefault(key, {"tokens": 0, "cost": 0.0, "calls": 0})
        it["tokens"] += int(r.input_tokens or 0) + int(r.output_tokens or 0)
        it["cost"] += float(r.cost or 0.0)
        it["calls"] += 1

    for r in rows:
        day = r.created_at.strftime("%Y-%m-%d") if r.created_at else "?"
        _acc(by_day, day, r)
        _acc(by_task, r.task or "(未标注)", r)
        _acc(by_user, r.user_id or "(匿名)", r)
        _acc(by_model, r.model_id or "(未知)", r)
        _acc(by_role, r.role or "(未分角色)", r)
        if (r.status or "ok") != "ok":
            err += 1

    total_tokens = sum(v["tokens"] for v in by_task.values())
    total_cost = round(sum(v["cost"] for v in by_task.values()), 4)

    # ★ 省钱空间估算：如果 critic 档调用改用最便宜的可用模型能省多少
    saving = _saving_estimate(db, rows)

    return {
        "range_days": days,
        "total": {"tokens": total_tokens, "cost": total_cost,
                  "calls": len(rows), "errors": err},
        "by_day": _sorted_dict(by_day),
        "by_task": _sorted_dict(by_task, by="cost"),
        "by_user": _sorted_dict(by_user, by="cost", limit=20),
        "by_model": _sorted_dict(by_model, by="cost"),
        "by_role": _sorted_dict(by_role, by="cost"),
        "saving_estimate": saving,
    }


def _sorted_dict(d: dict, by: str = "cost", limit: int = 0) -> dict:
    items = sorted(d.items(), key=lambda kv: -kv[1].get(by, 0))
    if limit:
        items = items[:limit]
    return {k: {**v, "cost": round(v["cost"], 4)} for k, v in items}


def _saving_estimate(db: Session, rows: list) -> dict:
    """估算"非 writer 档全部走最便宜模型"能省多少。

    ★ 这是验收"token 成本降 ≥30%"的量化依据。
    """
    profiles = {p.model_id: p for p in _all_profiles(db)}
    cheapest = min(profiles.values(), key=lambda p: (p.price_in + p.price_out)) \
        if profiles else None
    if cheapest is None:
        return {"note": "无可用模型", "could_save": 0.0, "ratio": 0.0}
    actual = sum(float(r.cost or 0.0) for r in rows)
    optimal = 0.0
    cheap_calls = 0
    for r in rows:
        tin, tout = int(r.input_tokens or 0), int(r.output_tokens or 0)
        if (r.role or "") in ("critic", "utility"):
            optimal += estimate_cost(cheapest, tin, tout)
            cheap_calls += 1
        else:
            # writer/planner 保持原样（不降级，质量优先）
            optimal += float(r.cost or 0.0)
    saved = actual - optimal
    return {
        "could_save": round(saved, 4),
        "ratio": round(saved / actual, 3) if actual else 0.0,
        "cheap_model": cheapest.model_id,
        "cheap_calls": cheap_calls,
        "note": "假设 critic/utility 档全部改用最便宜模型（writer/planner 不变）",
    }


def user_report(db: Session, user_id: str, *, days: int = 30) -> dict:
    """单用户成本报告（用户中心展示）。"""
    since = _day_start(days - 1)
    rows = list(db.scalars(
        select(LLMCall).where(LLMCall.user_id == user_id,
                              LLMCall.created_at >= since)).all())
    by_task: dict[str, dict] = {}
    for r in rows:
        it = by_task.setdefault(r.task or "(未标注)",
                                {"tokens": 0, "cost": 0.0, "calls": 0})
        it["tokens"] += int(r.input_tokens or 0) + int(r.output_tokens or 0)
        it["cost"] += float(r.cost or 0.0)
        it["calls"] += 1
    today = daily_usage(db, user_id)
    cap = _cap_for(db, user_id)
    return {"days": days, "today": today, "daily_cap": cap,
            "cap_remain": (max(0, cap - today["tokens"]) if cap > 0 else -1),
            "by_task": _sorted_dict(by_task, by="cost"),
            "total_cost": round(sum(v["cost"] for v in by_task.values()), 4)}


def prune(db: Session, *, keep_days: int = 180) -> int:
    """清理过老台账（防止表无限膨胀）。

    ★ 保留 180 天：够做同比/环比，且按当前量级约几十万行（可控）。
    """
    cutoff = _now_utc() - dt.timedelta(days=keep_days)
    rows = list(db.scalars(select(LLMCall).where(LLMCall.created_at < cutoff)).all())
    for r in rows:
        db.delete(r)
    if rows:
        db.commit()
    return len(rows)

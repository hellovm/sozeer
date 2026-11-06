"""模型路由与负载分流（v0.26.2）。

解决三件事：
  ① **同一功能多模型**：一个 role 下挂多个 ModelProfile，按序降级。
  ② **同一模型多供应商/多 key**：一个 profile 下挂多个 ModelProvider，按权重分流。
  ③ **按套餐路由**：不同 plan 用不同模型（`PlanModelRoute`）。

★ 设计原则（沿用 §36 熔断 + §43 降级链）：
  - 权重是"相对比例"，不是百分比 —— 100/100 等价于 1:1，改动权重不影响总数。
  - **失败立刻熔断该 provider**（连续 N 次 → 停 X 秒），避免每次请求都等超时。
  - **全部不可用时返回 None**，由调用方决定兜底（绝不在这里抛异常打断业务）。
  - 挑选用**确定性哈希轮询**（按调用序号），不用随机 —— 便于复现问题。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid

from sqlalchemy import select

from . import keyvault
from .models import ModelProfile, ModelProvider, PlanModelRoute

# 连续失败多少次触发熔断
FAIL_THRESHOLD = 3
# 熔断持续秒数
OPEN_SECONDS = 60

# 支持的模型角色（与 media.py / llm.py 对齐）
#
# ★★ v1.0-E4.15：补登记 "utility" 档。
#   为什么必须补：costing.TASK_ROLE 把 title/tagging/classify/short_judge
#   四类任务路由到 utility，ROLE_FALLBACK 与 coverage() 体检也都认它 ——
#   但本表漏了它 → 管理端建档被 400 拒（"未知角色 utility"），
#   而注册表的 model.role_utility 却还把它当默认值。
#   这是"两处角色表漂移"的典型缺陷（MEMORY §注册表唯一声明处）。
ROLES: dict[str, str] = {
    "default": "通用（未指定角色时使用）",
    "planner": "规划（大纲 / 任务单）",
    "writer": "写作（正文生成）",
    "critic": "判定与摘要（质检 / 压缩）",
    "utility": "辅助（标题 / 标签 / 分类等极短输出）",
    "script": "剧本（小说改编短剧）",
    "storyboard": "分镜（八段提示词细化）",
    "image": "图片生成（参考图 / 分镜图）",
    "video": "视频生成（分镜视频 / 成片）",
    "tts": "语音合成（配音 / 音色）",
    "embedding": "向量嵌入（语义检索）",
}

# 媒体类角色（走 media.py 而非 llm.py）
MEDIA_ROLES = ("image", "video", "tts")

# provider 类型（影响 media.py 走哪个适配器）
PROVIDER_KINDS: dict[str, str] = {
    "openai-compatible": "OpenAI 兼容（文本 / 图片）",
    "openai-images": "OpenAI 兼容图片（/v1/images/generations）",
    "openai-videos": "OpenAI 兼容视频（/v1/videos 异步）",
    "agnes": "Agnes AI（图片 / 视频，OpenAI 兼容）",
    "comfyui": "ComfyUI 本地（内置 LTX-2.5 工作流）",
    "comfyui-workflow": "ComfyUI 自定义工作流（API JSON）",
    "minimax": "MiniMax H3（视频，原生音频）",
    "custom": "自定义 HTTP 适配器",
}


def new_id() -> str:
    return f"mp_{uuid.uuid4().hex[:12]}"


def _now() -> dt.datetime:
    """★ naive UTC（§7 时区铁律：DB 列是 naive，比较前必须统一）。"""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _jload(s: str, default):
    try:
        v = json.loads(s or "")
        return v if isinstance(v, type(default)) else default
    except Exception:                                   # noqa: BLE001
        return default


# ---------------------------------------------------------------- Provider 列表

def providers_for(db, profile_id: str, *, only_available: bool = True) -> list[ModelProvider]:
    """取某模型槽下**可用**的供应商端点（按权重降序，已过滤熔断）。"""
    rows = list(db.scalars(
        select(ModelProvider).where(ModelProvider.profile_id == profile_id)))
    rows = [r for r in rows if r.enabled]
    if only_available:
        now_ = _now()
        rows = [r for r in rows if not (r.open_until and r.open_until > now_)]
        rows = [r for r in rows if int(r.weight or 0) > 0]
    rows.sort(key=lambda r: (-int(r.weight or 0), r.created_at or _now()))
    return rows


def _pick_by_weight(rows: list[ModelProvider], seq: int) -> ModelProvider | None:
    """按权重做**确定性**轮询。

    ★ 为什么按 seq 哈希而不是随机：同一批请求可复现（便于排查）。
    ★ 实现坑：不能直接按"权重累加求和"落段 —— 权重是相对值，
      必须先归一化成整数份数（用最大公约数思路简化：除以最小权重），
      否则 100/100 会永远落在第一段。
      这里用「按最小权重归一化 → 每个端点占 w/min 个槽位」实现真轮询。
    """
    if not rows:
        return None
    ws = [max(0, int(r.weight or 0)) for r in rows]
    total = sum(ws)
    if total <= 0:
        return rows[0]
    # 归一化：把权重转成"份数"（保证 100/100 → 1:1，300/100 → 3:1）
    g = 0
    for w in ws:
        g = _gcd(g, w) if w else g
    g = g or 1
    units = [w // g for w in ws]
    total_units = sum(units) or 1
    pos = seq % total_units
    acc = 0
    for r, u in zip(rows, units):
        acc += u
        if pos < acc:
            return r
    return rows[-1]


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


# 进程内调用序号（用于确定性轮询；多 worker 下各进程独立，够用）
_SEQ = {"n": 0}


def next_seq() -> int:
    _SEQ["n"] = (_SEQ["n"] + 1) % 100000
    return _SEQ["n"]


def resolve_profile(db, role: str, *, plan_key: str = "") -> ModelProfile | None:
    """按「套餐路由 → 角色默认」两级解析出模型槽。

    ① 若该套餐为该角色配了有序列表 → 取第一个启用且存在的
    ② 否则取该角色下 is_default 的；再否则取该角色第一个启用的
    ③ 再否则回退到 default 角色
    ★ 全程返回 None 而非抛异常 —— 调用方负责兜底（§43 降级链）。
    """
    # ① 套餐路由
    if plan_key:
        try:
            stmt = (select(PlanModelRoute)
                    .where(PlanModelRoute.plan_key == plan_key,
                           PlanModelRoute.role == role))
            route = db.scalars(stmt).first()
            if route:
                for pid in _jload(route.profile_ids, []):
                    p = db.get(ModelProfile, pid)
                    if p and p.enabled:
                        return p
        except Exception:                               # noqa: BLE001
            pass

    # ② 角色默认
    try:
        rows = list(db.scalars(
            select(ModelProfile).where(ModelProfile.role == role,
                                       ModelProfile.enabled == 1)))
        for p in rows:
            if p.is_default:
                return p
        if rows:
            return rows[0]
    except Exception:                                   # noqa: BLE001
        pass
    return None


def resolve_endpoint(db, profile: ModelProfile) -> tuple[str, str]:
    """给一个模型槽挑一个具体端点，返回 (base_url, api_key)。

    ★ 无 Provider 记录时**回退到 profile 自身的 base_url/api_key** ——
      这是兼容旧数据的关键：老部署只有一个端点，行为与升级前完全一致。
    """
    rows = providers_for(db, profile.id)
    if not rows:
        return profile.base_url or "", keyvault.get_api_key(profile)
    p = _pick_by_weight(rows, next_seq())
    if not p:
        return profile.base_url or "", keyvault.get_api_key(profile)
    return p.base_url or profile.base_url or "", keyvault.get_api_key(p)


def mark_ok(db, profile: ModelProfile) -> None:
    """标记某次调用成功（清失败计数，解除熔断）。fail-open。"""
    try:
        rows = providers_for(db, profile.id, only_available=False)
        for r in rows:
            r.fail_count = 0
            r.open_until = None
            r.last_ok_at = _now()
        db.commit()
    except Exception:                                   # noqa: BLE001
        try:
            db.rollback()
        except Exception:                               # noqa: BLE001
            pass


def mark_fail(db, profile: ModelProfile, err: str = "") -> None:
    """标记某次调用失败；连续达阈值则熔断。fail-open。

    ★ 只对**权重最高的那个**（即刚被选中的）计数是不够的 ——
      我们不知道调用方用的是哪个。折中：对全部端点累计，
      但熔断只作用于"当前被选中权重段"的那个（由 open_until 单独控制）。
      实现上简化为：累计所有端点 fail_count，任一达阈值即熔断它自己。
    """
    try:
        rows = providers_for(db, profile.id, only_available=False)
        for r in rows:
            r.fail_count = int(r.fail_count or 0) + 1
            r.last_err = (err or "")[:500]
            if r.fail_count >= FAIL_THRESHOLD:
                r.open_until = _now() + dt.timedelta(seconds=OPEN_SECONDS)
                r.fail_count = 0        # 重置，避免熔断期结束后立刻再熔断
        db.commit()
    except Exception:                                   # noqa: BLE001
        try:
            db.rollback()
        except Exception:                               # noqa: BLE001
            pass


# ---------------------------------------------------------------- 后台视图

def admin_view(db) -> dict:
    """后台模型配置页一次性取全：模型槽 + 端点 + 套餐路由 + 元数据。"""
    profiles = list(db.scalars(select(ModelProfile)))
    providers = list(db.scalars(select(ModelProvider)))
    routes = list(db.scalars(select(PlanModelRoute)))

    prov_by_profile: dict[str, list[dict]] = {}
    for p in providers:
        prov_by_profile.setdefault(p.profile_id, []).append({
            "id": p.id, "name": p.name, "base_url": p.base_url,
            "weight": p.weight, "enabled": p.enabled,
            "fail_count": p.fail_count,
            "open_until": p.open_until.isoformat() if p.open_until else "",
            "circuit_open": bool(p.open_until and p.open_until > _now()),
            "last_ok_at": p.last_ok_at.isoformat() if p.last_ok_at else "",
            "last_err": p.last_err, "has_key": bool(p.api_key),
        })

    prof_list = [{
        "id": p.id, "name": p.name, "provider": p.provider,
        "base_url": p.base_url, "model_id": p.model_id, "role": p.role,
        "is_default": p.is_default, "enabled": p.enabled,
        "price_in": p.price_in, "price_out": p.price_out,
        "temperature": p.temperature, "max_tokens": p.max_tokens,
        "context_window": p.context_window,
        "has_key": bool(p.api_key),
        "providers": prov_by_profile.get(p.id, []),
    } for p in profiles]
    prof_list.sort(key=lambda x: (x["role"], -int(x["is_default"] or 0), x["name"]))

    route_list = [{
        "id": r.id, "plan_key": r.plan_key, "role": r.role,
        "profile_ids": _jload(r.profile_ids, []),
    } for r in routes]

    return {
        "profiles": prof_list,
        "routes": route_list,
        "roles": ROLES,
        "provider_kinds": PROVIDER_KINDS,
        "media_roles": list(MEDIA_ROLES),
        "circuit": {"fail_threshold": FAIL_THRESHOLD, "open_seconds": OPEN_SECONDS},
    }

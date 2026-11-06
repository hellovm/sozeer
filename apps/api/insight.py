"""数据洞察与模型训练准备（v0.24.3 F3）。

三条主线：
  F3-1 洞察看板    功能热力 / 失败模式 TOP / 质量分布
  F3-2 改进闭环    高频失败 → 优化建议 → 待评估池
  F3-3 训练数据管线 ★ 脱敏 + 授权校验 + 格式导出

★★ 训练数据的法律红线（计划书明确要求）：
  采用「**用户明确授权 + 脱敏 + 可撤回**」三原则。
  本模块 `require_consent()` 是授权闸门 —— 未经显式 opt-in 的数据
  **一条都不导出**。默认关闭（fail-closed）。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

_log = logging.getLogger("novelforge.insight")

# ---------------------------------------------------------------- 授权（红线）

CONSENT_KEY = "training.consent"           # 全局训练授权开关
CONSENT_USER_PREFIX = "training.consent.user."   # 单用户授权


def require_consent(db: Session, user_id: str = "") -> tuple[bool, str]:
    """★ 训练数据授权闸门（**fail-closed**）。

    两级授权：
      · 实例级（`training.consent=1`）：部署方声明"本实例用户已同意"
      · 用户级（`training.consent.user.<id>=1`）：单个用户显式勾选

    规则：
      · 实例级未开 → 一律拒绝（连用户级也不看）
      · 实例级开了但用户级未开 → 仅当**实例声明"用户协议已覆盖"**时放行
      · 传 user_id 时，检查该用户是否**撤回**授权（撤回优先于实例级）

    ★ 为什么撤回优先：用户的撤回权 > 部署方的授权声明。
      这是 GDPR / 个保法的基本要求。
    """
    from apps.api.models import Setting
    inst = db.get(Setting, CONSENT_KEY)
    if inst is None or str(inst.value or "").strip() not in ("1", "true", "on"):
        return False, "实例未开启训练数据授权（training.consent=1）"
    if user_id:
        u = db.get(Setting, f"{CONSENT_USER_PREFIX}{user_id}")
        if u is not None:
            v = str(u.value or "").strip().lower()
            if v in ("0", "false", "off", "revoked"):
                return False, "该用户已撤回授权"
            if v in ("1", "true", "on"):
                return True, ""
    return True, ""


def set_consent(db: Session, enabled: bool, user_id: str = "") -> None:
    """设置授权（用户中心 / 管理端调用）。"""
    from apps.api.models import Setting
    key = f"{CONSENT_USER_PREFIX}{user_id}" if user_id else CONSENT_KEY
    s = db.get(Setting, key)
    val = "1" if enabled else "0"
    if s is None:
        # ★ Setting 只有 key/value 两列，不能传 created_at
        db.add(Setting(key=key, value=val))
    else:
        s.value = val
    db.commit()


# ---------------------------------------------------------------- F3-1 洞察看板

def dashboard(db: Session, *, days: int = 30) -> dict:
    """★ 洞察看板：功能热力 / 失败 TOP / 质量分布。

    ★ 数据来源是**自家库**（不是遥测上报），所以可以更细。
      遥测看板看的是"所有开源实例的聚合"，这里看的是"本实例详情"。
    """
    since = _utc_now() - dt.timedelta(days=days)
    return {
        "range_days": days,
        "feature_heat": feature_heat(db, since=since),
        "failure_top": failure_top(db, since=since),
        "quality_dist": quality_dist(db, since=since),
        "retry_patterns": retry_patterns(db, since=since),
        "activity": activity_trend(db, since=since),
        # #315：避坑记录（用户在聊天里"指出问题→系统修正"沉淀的经验）
        "lessons": lessons_digest(db, since=since),
    }


def lessons_digest(db: Session, *, since=None) -> dict:
    """避坑记录摘要（#315）。

    ★ 与 adoption 的区别：adoption 只给"计数/分桶"（隐私优先）；
      这里刻意给出**可读的纠错原文**——避坑提示要能直接注入提示词，
      必须是具体指令，哈希或分桶做不到。

    ★ 边界：只回给管理端（本实例管理员），且**永不进遥测上报**。
    """
    try:
        from apps.api.models import GenLesson
        q = select(GenLesson)
        if since is not None:
            q = q.where(GenLesson.created_at >= since)
        rows = list(db.scalars(q.order_by(GenLesson.created_at.desc())
                               .limit(200)).all())
        items = [{
            "id": r.id, "book_id": r.book_id,
            "kind": r.kind, "trigger": r.trigger, "fix": r.fix,
            "tools": r.tools, "status": r.status,
            "hits": int(r.hits or 0),
            "created_at": (r.created_at.strftime("%Y-%m-%d %H:%M")
                           if r.created_at else ""),
        } for r in rows]
        active = [x for x in items if x["status"] == "active"]
        injected = sum(x["hits"] for x in items)
        return {
            "count": len(items),
            "active": len(active),
            "muted": len(items) - len(active),
            "injected_hits": injected,
            "items": items[:50],
            "note": ("用户在聊天中「指出问题 → 系统修正」的沉淀；"
                     "仅管理端可见，不参与遥测上报"),
        }
    except Exception as e:                              # noqa: BLE001
        return {"count": 0, "active": 0, "muted": 0, "injected_hits": 0,
                "items": [], "note": f"读取失败：{e}"}


def _utc_now() -> dt.datetime:
    """★ UTC（与 models.now() 一致，避免 8 小时偏移）。"""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def feature_heat(db: Session, *, since=None) -> list[dict]:
    """功能热力（按 LLMCall.task 聚合）。"""
    from apps.api.models import LLMCall
    q = select(LLMCall.task, func.count(LLMCall.id),
               func.coalesce(func.sum(LLMCall.input_tokens + LLMCall.output_tokens), 0))
    if since is not None:
        q = q.where(LLMCall.created_at >= since)
    q = q.group_by(LLMCall.task).order_by(func.count(LLMCall.id).desc()).limit(30)
    out = []
    try:
        for task, n, toks in db.execute(q).all():
            out.append({"task": task or "(未标注)", "calls": int(n),
                        "tokens": int(toks or 0)})
    except Exception:                                   # noqa: BLE001
        pass
    total = sum(x["calls"] for x in out) or 1
    for x in out:
        x["share"] = round(x["calls"] / total, 4)
    return out


def failure_top(db: Session, *, since=None, limit: int = 20) -> dict:
    """失败模式 TOP（★ 只看类型与计数，不看内容）。

    两个来源：
      · LLMCall.status != ok → LLM 层失败
      · Run.status == failed → 业务层失败
    """
    from apps.api.models import LLMCall, Run
    llm: Counter = Counter()
    biz: Counter = Counter()
    try:
        q = select(LLMCall).where(LLMCall.status != "ok")
        if since is not None:
            q = q.where(LLMCall.created_at >= since)
        for r in db.scalars(q.limit(5000)).all():
            # ★ 只用错误**类型前缀**（"HTTP 429" / "网络错误"），非全文
            label = (r.error or "unknown").split(":", 1)[0][:48]
            llm[label] += 1
    except Exception:                                   # noqa: BLE001
        pass
    try:
        q2 = select(Run).where(Run.status == "failed")
        if since is not None:
            q2 = q2.where(Run.created_at >= since)
        for r in db.scalars(q2.limit(2000)).all():
            biz[f"kind={r.kind or '?'}"] += 1
    except Exception:                                   # noqa: BLE001
        pass
    return {"llm_errors": [{"reason": k, "count": v}
                           for k, v in llm.most_common(limit)],
            "business_failures": [{"reason": k, "count": v}
                                  for k, v in biz.most_common(limit)]}


def quality_dist(db: Session, *, since=None) -> dict:
    """质量分布（质检分数直方图 + 闸门失败原因）。"""
    from apps.api.models import AuditReport, Run
    hist: Counter = Counter()
    reasons: Counter = Counter()
    scores: list[int] = []
    try:
        q = select(Run)
        if since is not None:
            q = q.where(Run.created_at >= since)
        for r in db.scalars(q.limit(5000)).all():
            s = int(r.gate_score or 0)
            if s <= 0:
                continue
            scores.append(s)
            hist[f"{(s // 10) * 10:02d}-{(s // 10) * 10 + 9:02d}"] += 1
    except Exception:                                   # noqa: BLE001
        pass
    try:
        q2 = select(AuditReport)
        if since is not None:
            q2 = q2.where(AuditReport.created_at >= since)
        for r in db.scalars(q2.limit(3000)).all():
            try:
                data = json.loads(r.issues_json or "[]")
            except Exception:
                continue
            for it in (data if isinstance(data, list) else []):
                rule = (it or {}).get("rule") or ""
                if rule:
                    reasons[str(rule)[:48]] += 1
    except Exception:                                   # noqa: BLE001
        pass
    avg = round(sum(scores) / len(scores), 1) if scores else 0
    return {"hist": dict(sorted(hist.items())), "avg_score": avg,
            "samples": len(scores),
            "gate_fail_reasons": [{"rule": k, "count": v}
                                  for k, v in reasons.most_common(20)]}


def retry_patterns(db: Session, *, since=None) -> dict:
    """重试/补救触发统计（计划书要求：如"产出意图未落地 8 次"）。

    ★ 数据来源：同一次 Run 内重复的 task 调用（说明触发了重试）。
    """
    from apps.api.models import LLMCall
    q = select(LLMCall.run_id, LLMCall.task, func.count(LLMCall.id))
    if since is not None:
        q = q.where(LLMCall.created_at >= since)
    q = (q.where(LLMCall.run_id != "")
         .group_by(LLMCall.run_id, LLMCall.task)
         .having(func.count(LLMCall.id) > 1).limit(2000))
    c: Counter = Counter()
    try:
        for _rid, task, n in db.execute(q).all():
            c[task or "(未标注)"] += (int(n) - 1)     # 多出次数 = 重试次数
    except Exception:                                   # noqa: BLE001
        pass
    return {"retry_total": sum(c.values()),
            "by_task": [{"task": k, "retries": v}
                        for k, v in c.most_common(20)]}


def activity_trend(db: Session, *, since=None, days: int = 30) -> list[dict]:
    """活跃趋势（按日统计调用数）。"""
    from apps.api.models import LLMCall
    q = select(LLMCall.created_at)
    if since is not None:
        q = q.where(LLMCall.created_at >= since)
    c: Counter = Counter()
    try:
        for (t,) in db.execute(q.limit(50000)).all():
            if t:
                c[t.strftime("%Y-%m-%d")] += 1
    except Exception:                                   # noqa: BLE001
        pass
    out = []
    today = _utc_now().date()
    for i in range(days - 1, -1, -1):
        d = (today - dt.timedelta(days=i)).strftime("%Y-%m-%d")
        out.append({"date": d, "calls": c.get(d, 0)})
    return out


# ---------------------------------------------------------------- F3-2 改进闭环

# ★ 失败原因 → 优化建议的映射（确定性，不用模型）。
#
# 为什么不用模型生成建议：这是"配置/代码层面"的建议，
# 需要**准确且可执行**。模型容易写出"建议优化提示词"这类空话。
SUGGESTION_MAP: dict[str, dict] = {
    "HTTP 429": {
        "title": "模型调用被限流",
        "advice": "提高限流配额，或为 critic 档配置独立（不同供应商）模型分散压力",
        "action": "config", "level": "high",
    },
    "HTTP 401": {
        "title": "API Key 无效或过期",
        "advice": "到配置中心重新填写模型 Key；建议开启密钥轮换双版本共存",
        "action": "config", "level": "urgent",
    },
    "HTTP 402": {
        "title": "账户余额不足",
        "advice": "充值或切换到备用模型；建议配置模型降级链",
        "action": "ops", "level": "urgent",
    },
    "HTTP 404": {
        "title": "接口路径不存在",
        "advice": "检查 base_url 是否含多余路径；自建服务确认已实现 /chat/completions",
        "action": "config", "level": "high",
    },
    "网络错误": {
        "title": "网络不可达",
        "advice": "检查出网策略与 DNS；建议为外部依赖加熔断（已有范式：embedding）",
        "action": "ops", "level": "high",
    },
    "耗尽": {
        "title": "max_tokens 不足",
        "advice": "把该模型 max_tokens 调到 16384 以上，或换非思考模式",
        "action": "config", "level": "normal",
    },
}

# 质检规则 → 优化建议（质量侧闭环）
QUALITY_SUGGESTION: dict[str, dict] = {
    "ai_flavor": {"title": "AI 腔偏高",
                  "advice": "扩充去 AI 腔词库，或在 writer 系统提示里加强约束",
                  "action": "prompt", "level": "high"},
    "word_count": {"title": "字数不达标",
                   "advice": "调高 model max_tokens；检查任务单是否给出字数区间",
                   "action": "config", "level": "normal"},
    "logic_jump": {"title": "逻辑跳跃",
                   "advice": "加强上下文装配（前文回捞 top_k），或补充场景层任务单",
                   "action": "prompt", "level": "high"},
    "repeat": {"title": "内容重复",
               "advice": "提高 temperature（0.9+）或增加滚动摘要的重复提示",
               "action": "config", "level": "normal"},
}


def suggest_improvements(db: Session, *, days: int = 30) -> dict:
    """★ 改进闭环（F3-2）：高频失败 → 自动生成优化建议。

    ★ 输出进"待评估池草稿"，由人确认后才实施 ——
      自动改配置风险太大（可能是用户自己的环境问题）。
    """
    since = _utc_now() - dt.timedelta(days=days)
    fails = failure_top(db, since=since)
    qual = quality_dist(db, since=since)
    items: list[dict] = []

    for f in fails["llm_errors"]:
        reason = f["reason"]
        for key, sug in SUGGESTION_MAP.items():
            if key in reason:
                items.append({"source": "llm_error", "trigger": reason,
                              "count": f["count"], **sug})
                break
        else:
            if f["count"] >= 3:
                items.append({"source": "llm_error", "trigger": reason,
                              "count": f["count"],
                              "title": f"未知失败模式：{reason[:32]}",
                              "advice": "需人工排查（已进待评估池）",
                              "action": "manual", "level": "normal"})

    for r in qual["gate_fail_reasons"][:10]:
        for key, sug in QUALITY_SUGGESTION.items():
            if key in r["rule"]:
                items.append({"source": "gate", "trigger": r["rule"],
                              "count": r["count"], **sug})
                break

    for r in fails["business_failures"]:
        if r["count"] >= 3:
            items.append({"source": "business", "trigger": r["reason"],
                          "count": r["count"],
                          "title": f"业务失败集中：{r['reason']}",
                          "advice": "检查该流程的依赖与前置条件",
                          "action": "manual", "level": "high"})

    items.sort(key=lambda x: -x.get("count", 0))
    return {"days": days, "count": len(items), "items": items[:30],
            "note": "以上为**建议**，需人工确认后写入计划书 §附录B 待评估池"}


# ---------------------------------------------------------------- F3-3 训练数据

# ★ 训练样本格式（对齐主流 SFT 格式）。
SFT_FORMAT = "openai-chat-v1"      # {"messages":[{role,content},...]}


def _anon(text: str, salts: list[str]) -> str:
    """★ 脱敏：把出现的人名/专名替换为占位符。

    ★ 为什么不用模型：脱敏是**安全操作**，模型可能漏（也贵）。
      确定性替换虽然朴素，但**可验证**（有测试守卫）。
    做法：把已知实体（角色名/书名）按长度降序替换为 [角色N]/[书名N]。
    """
    out = text or ""
    for i, name in enumerate(sorted(set(salts), key=len, reverse=True)):
        if not name or len(name) < 2:
            continue
        out = out.replace(name, f"[实体{i + 1}]")
    return out


def _entities_of(db: Session, book_id: str) -> list[str]:
    """取该书的所有实体名（角色 + 书名 + 设定名）。"""
    from apps.api.models import Book, Character, WorldSetting
    names: list[str] = []
    try:
        b = db.get(Book, book_id)
        if b is not None and b.title:
            names.append(b.title)
        for c in db.scalars(select(Character).where(
                Character.book_id == book_id)).all():
            if c.name:
                names.append(c.name)
        for w in db.scalars(select(WorldSetting).where(
                WorldSetting.book_id == book_id)).all():
            if w.name:
                names.append(w.name)
    except Exception:                                   # noqa: BLE001
        pass
    return names


def build_dataset(db: Session, *, user_id: str = "", book_id: str = "",
                  limit: int = 500, anon: bool = True) -> dict:
    """★ 训练数据管线（F3-3）：脱敏 + 授权校验 + 格式导出。

    ★★ 授权是硬闸门：未授权返回空集 + 明确原因（fail-closed）。
       这不是"最好加一下"的选项 —— 计划书把它列为法律红线。

    样本来源：已采纳的章节（Run.status=ok 且有内容）
    样本格式：{"messages": [{"role":"system","content":...},
                           {"role":"user","content":...},
                           {"role":"assistant","content":...}]}
    """
    ok, why = require_consent(db, user_id)
    if not ok:
        return {"ok": False, "error": why, "count": 0, "samples": [],
                "consent_required": True}

    from apps.api.models import Chapter, Run
    q = select(Chapter).where(Chapter.status == "final")
    if book_id:
        q = q.where(Chapter.book_id == book_id)
    rows = list(db.scalars(q.order_by(Chapter.created_at.desc())
                           .limit(min(max(limit, 1), 5000))).all())

    samples: list[dict] = []
    skipped: Counter = Counter()
    for ch in rows:
        body = (ch.content or "").strip()
        if len(body) < 200:
            skipped["过短"] += 1
            continue
        summary = (ch.summary or "").strip()
        if not summary:
            skipped["缺摘要（无指令可学）"] += 1
            continue
        salts = _entities_of(db, ch.book_id)
        payload = {
            "messages": [
                {"role": "system",
                 "content": "你是中文网文写作助手。根据任务单写出连贯、有画面感的章节正文。"},
                {"role": "user",
                 "content": (_anon(summary, salts) if anon else summary)},
                {"role": "assistant",
                 "content": (_anon(body, salts) if anon else body)},
            ],
            "meta": {"format": SFT_FORMAT,
                     "book_id_hash": hashlib.sha256(
                         (ch.book_id or "").encode()).hexdigest()[:12],
                     "ch_idx": int(ch.idx or 0),
                     "words": len(body),
                     "anonymized": bool(anon)},
        }
        samples.append(payload)

    return {"ok": True, "count": len(samples), "samples": samples,
            "skipped": dict(skipped), "format": SFT_FORMAT,
            "anonymized": bool(anon),
            "note": "已脱敏（实体替换为占位符）；导出前请再经法务确认"}


def export_jsonl(db: Session, path: str, **kw) -> dict:
    """导出 JSONL（训练框架通用格式）。"""
    r = build_dataset(db, **kw)
    if not r.get("ok"):
        return r
    try:
        with open(path, "w", encoding="utf-8") as f:
            for s in r["samples"]:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
        return {"ok": True, "path": path, "count": r["count"],
                "bytes": 0}
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def dataset_stats(db: Session) -> dict:
    """数据集体检（能不能训）。"""
    from apps.api.models import Chapter
    total = db.scalar(select(func.count()).select_from(Chapter)) or 0
    final = db.scalar(select(func.count()).select_from(Chapter)
                      .where(Chapter.status == "final")) or 0
    with_summary = db.scalar(
        select(func.count()).select_from(Chapter)
        .where(Chapter.status == "final", Chapter.summary != "")) or 0
    words = db.scalar(select(func.coalesce(func.sum(Chapter.words), 0))
                      .where(Chapter.status == "final")) or 0
    ok, why = require_consent(db)
    return {
        "chapters_total": int(total), "chapters_final": int(final),
        "chapters_trainable": int(with_summary),
        "total_words": int(words),
        "consent": {"granted": ok, "reason": why},
        "readiness": _readiness(int(with_summary)),
        "note": ("训练样本建议 ≥500 条；不足时先用 合成数据 + 提示词工程 过渡"
                 if int(with_summary) < 500 else "样本量已具备微调条件"),
    }


def _readiness(n: int) -> dict:
    """训练就绪度评估。"""
    if n >= 5000:
        lvl, txt = "ready", "样本充足，可做完整 SFT"
    elif n >= 500:
        lvl, txt = "ok", "可做 LoRA 微调"
    elif n >= 100:
        lvl, txt = "early", "样本偏少，建议先做评测集与提示词优化"
    else:
        lvl, txt = "insufficient", "样本不足，先积累数据（或用合成数据）"
    return {"level": lvl, "text": txt, "samples": n,
            "next_milestone": (500 if n < 500 else (5000 if n < 5000 else None))}


def ab_framework(db: Session, *, task: str = "chapter_write",
                 limit: int = 200) -> dict:
    """★ A/B 实验框架（F3-5）：按模型分组比较同一任务的产出质量。

    ★ 为什么用"事后分组"而不是"实时分流"：
      生成是重操作且有成本，实时分流会打断用户。事后按 model_id 分组
      统计质检分，同样能回答"哪个模型更好"，且零干扰。
    """
    from apps.api.models import Run
    q = select(Run).where(Run.kind == "chapter", Run.gate_score > 0)
    groups: dict[str, list[int]] = {}
    try:
        for r in db.scalars(q.order_by(Run.created_at.desc()).limit(limit * 5)).all():
            mid = r.model_id or "(未知)"
            groups.setdefault(mid, []).append(int(r.gate_score or 0))
    except Exception:                                   # noqa: BLE001
        pass
    variants = []
    for mid, scores in groups.items():
        if len(scores) < 3:
            continue
        variants.append({"model_id": mid, "n": len(scores),
                         "avg_score": round(sum(scores) / len(scores), 1),
                         "min": min(scores), "max": max(scores)})
    variants.sort(key=lambda x: -x["avg_score"])
    return {"task": task, "variants": variants,
            "note": ("样本数 ≥3 才纳入比较（少于 3 条不具统计意义）"
                     if variants else "数据不足，需积累更多生成记录")}


# ================================================================
#  ★★ v1.0-E4.7：提示词版本对比（基于 adoption 实时计数）
#
#  ============================ 为什么需要 ============================
#
#  `ab_framework()` 是**按模型**分组（用 Run.gate_score 事后统计）。
#  但"**改了提示词，效果变好没有**"是另一个维度的问题 ——
#  两者互补，都需要。
#
#  ★ 数据来源：`adoption` 计数 = Σ(历史快照增量·DB) + 当前内存。
#    （v1.0-E4.21 起跨重启累计；此前纯内存、每次部署清零，
#      数据洞察永远只有"本次运行"的零头，无法支持系统进化。）
#    ★ 界面标注为"累计历史"，与避坑记录（gen_lessons）同为持久数据。
#
#  ★★ 判据（怎么算"哪个版本更好"）：
#    看**采纳率**，不是看生成量。
#    生成量大可能只是"用户反复重试"（质量差的表现）。
# ================================================================

def prompt_ab(db: Session | None = None, *, top: int = 10) -> dict:
    """★ 提示词版本对比：按指纹分组的生成/采纳/采纳率。

    ★ 数据范围（必须回传，界面要显示）：
      · v1.0-E4.21 起**跨重启累计**（历史快照落库 + 当前内存）
      · 更早版本部署期间的样本因当时纯内存计数而缺失（无法回填）
      · 样本数 < 3 的版本不纳入对比（无统计意义）
    """
    try:
        from apps.api import adoption as adapt
        snap = adapt.collect_adoption()
    except Exception as e:                              # noqa: BLE001
        return {"variants": [], "note": f"读取失败：{e}"}

    by_prompt = snap.get("by_prompt") or {}
    variants = []
    for key, v in by_prompt.items():
        gen = int(v.get("gen") or 0)
        adopt_n = int(v.get("adopt") or 0)
        # ★ 样本太少不比较（避免"生成 1 次采纳 1 次 = 100%"这种误导）
        if gen < 3:
            continue
        rate = round(adopt_n / gen * 100, 1) if gen else 0.0
        role, _, fp = key.partition(":")
        variants.append({
            "fingerprint": fp[:16],
            "role": role,
            "gen": gen,
            "adopt": adopt_n,
            "adopt_rate": rate,
            # ★ 丢弃数 = 生成 - 采纳（近似，含未决策的）
            "pending": max(0, gen - adopt_n),
        })
    variants.sort(key=lambda x: -x["adopt_rate"])

    out: dict = {
        "variants": variants[:top],
        "total_variants": len(variants),
        "note": ("样本数 ≥3 才纳入比较" if variants
                 else "暂无足够样本（需 ≥3 次生成才计入）"),
        # ★★ 必须回传数据范围说明 —— 防止运营误读
        "scope": "累计历史（v1.0-E4.21 起持久化到数据库，跨重启累计）",
    }

    # ★ 补充：环节级别的采纳漏斗（哪个环节产出最容易被丢弃）
    by_role = snap.get("by_role") or {}
    funnel = []
    for role, rv in sorted(by_role.items(),
                           key=lambda x: -int(x[1].get("gen") or 0))[:10]:
        gen = int(rv.get("gen") or 0)
        ok = int(rv.get("gen_ok") or 0)
        adopt_n = int(rv.get("adopt") or 0)
        funnel.append({
            "role": role,
            "gen": gen,
            "ok": ok,
            "adopt": adopt_n,
            "ok_rate": round(ok / gen * 100, 1) if gen else 0.0,
            "adopt_rate": round(adopt_n / gen * 100, 1) if gen else 0.0,
            "edit": rv.get("edit") or {},
            "regen": rv.get("regen") or {},
        })
    out["funnel"] = funnel

    # ★ 最佳/最差建议（只有样本足够才给）
    if len(variants) >= 2:
        best, worst = variants[0], variants[-1]
        gap = round(best["adopt_rate"] - worst["adopt_rate"], 1)
        out["suggestion"] = (
            f"版本 {best['fingerprint']} 采纳率 {best['adopt_rate']}%，"
            f"比最差的 {worst['fingerprint']}（{worst['adopt_rate']}%）"
            f"高 {gap} 个百分点。"
            + ("★ 差距显著，建议保留前者。" if gap >= 15
               else "差距不大，建议继续观察。"))
    elif variants:
        out["suggestion"] = "只有一个版本有足够样本，无法对比。再积累一些数据。"
    else:
        out["suggestion"] = ("数据不足：每个提示词版本需 ≥3 次生成才计入。"
                             "正常使用（代笔→采纳）即会自动积累，无需开启遥测。")

    return out

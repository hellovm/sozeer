"""用户反馈系统（v0.24.1 F1）。

★ 设计核心：**自动快照要"够用但不越界"**。

够用 = 能复现问题：页面路径、操作序列、版本、浏览器、错误码。
越界 = 用户没打算给你看的东西：小说正文、对话原文、密钥、个人信息。

本模块的 `sanitize_context()` 是**写入前的唯一关口**（fail-closed），
任何进入 `context_json` 的数据都必须过它。
"""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.models import UserFeedback
from apps.api.models import now as _now

_log = logging.getLogger("novelforge.feedback")

# ---------------------------------------------------------------- 常量

KINDS = ("bug", "suggestion", "quality", "other")
KIND_LABEL = {
    "bug": "功能异常",
    "suggestion": "功能建议",
    "quality": "内容质量",
    "other": "其他",
}

STATUSES = ("open", "triaged", "in_progress", "resolved", "closed", "duplicate")
STATUS_LABEL = {
    "open": "待处理",
    "triaged": "已分类",
    "in_progress": "处理中",
    "resolved": "已解决",
    "closed": "已关闭",
    "duplicate": "重复",
}

PRIORITIES = ("low", "normal", "high", "urgent")
PRIORITY_LABEL = {
    "low": "低", "normal": "普通", "high": "高", "urgent": "紧急",
}

MAX_TITLE = 200
MAX_CONTENT = 5000
MAX_CTX_BYTES = 8 * 1024          # 快照上限 8KB（够用且防止塞正文）


# ---------------------------------------------------------------- 脱敏（红线）

# ★ 键名黑名单：这些 key 一律丢弃（无论值是什么）
#   为什么用键名而不是内容识别：内容识别必漏（用户可能贴任何东西），
#   键名是**确定性**的，能拦住"把正文塞进 content 字段"这类误传。
FORBIDDEN_KEYS = {
    # 内容类
    "content", "text", "body", "chapter_content", "draft", "passage",
    "prompt", "completion", "output", "input", "messages", "answer",
    "原文", "正文", "内容", "草稿", "对话",
    # 凭据类
    "api_key", "apikey", "key", "secret", "token", "password", "passwd",
    "authorization", "auth", "cookie", "session", "credential",
    "nf_secret", "secret_key", "private_key",
    # 个人信息类
    "email", "phone", "mobile", "id_card", "idcard", "realname",
    "real_name", "address", "ip", "ip_addr", "client_ip",
    "username", "user_name", "nickname",
}

# 值层面的兜底：命中即替换（防止通过其他 key 夹带）
_PATTERNS = [
    # 手机号（中国大陆）
    (re.compile(r"\b1[3-9]\d{9}\b"), "[手机号]"),
    # 邮箱
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[邮箱]"),
    # 身份证（18 位）
    (re.compile(r"\b\d{17}[\dXx]\b"), "[证件号]"),
    # 常见 API Key 形态
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), "[密钥]"),
    (re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}\b"), "[密钥]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[密钥]"),
    # JWT
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+"), "[令牌]"),
    # IPv4（保留私网段除外 —— 私网段是部署信息，有价值且不敏感）
    (re.compile(r"\b(?!10\.|127\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)"
                r"(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
    # URL 里的 query（可能含 token）
    (re.compile(r"[?&](token|key|access_token|api_key)=[^&\s]+", re.I), "?[已脱敏]"),
]


def _scrub_value(v):
    """对字符串值做模式替换（递归处理容器）。"""
    if isinstance(v, str):
        out = v
        for pat, rep in _PATTERNS:
            out = pat.sub(rep, out)
        return out
    if isinstance(v, dict):
        return {k: _scrub_value(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_scrub_value(x) for x in v]
    return v


def sanitize_context(ctx: dict | None, *, max_bytes: int = MAX_CTX_BYTES) -> dict:
    """★ 反馈快照脱敏（**写入前的唯一关口**）。

    做三件事：
      1. **键名黑名单**：命中即丢弃（确定性拦截）
      2. **值模式替换**：手机/邮箱/密钥/IP/JWT → 占位符
      3. **体积截断**：超 8KB 丢弃（正常快照只有几百字节，
         超限基本意味着误传了大块内容）

    返回结构：{"ctx": {...}, "dropped": [...], "truncated": bool}
    """
    if not isinstance(ctx, dict):
        return {"ctx": {}, "dropped": [], "truncated": False}

    dropped: list[str] = []
    clean: dict = {}

    for k, v in ctx.items():
        ks = str(k).strip().lower()
        if ks in FORBIDDEN_KEYS:
            dropped.append(str(k))
            continue
        # 嵌套字典：递归过滤键
        if isinstance(v, dict):
            sub, sub_dropped = {}, []
            for k2, v2 in v.items():
                if str(k2).strip().lower() in FORBIDDEN_KEYS:
                    sub_dropped.append(f"{k}.{k2}")
                    continue
                sub[k2] = _scrub_value(v2)
            dropped.extend(sub_dropped)
            clean[k] = sub
        else:
            clean[k] = _scrub_value(v)

    # 体积控制
    truncated = False
    try:
        raw = json.dumps(clean, ensure_ascii=False)
        if len(raw.encode("utf-8")) > max_bytes:
            clean = {"_note": "快照过大已省略",
                     "page": clean.get("page", ""),
                     "version": clean.get("version", "")}
            truncated = True
    except Exception:                                   # noqa: BLE001
        clean, truncated = {}, True

    return {"ctx": clean, "dropped": dropped, "truncated": truncated}


def redline_check(ctx: dict) -> tuple[bool, list[str]]:
    """红线自检（用于测试与监控）。

    返回 (是否干净, 命中列表)。**监控用** —— 若线上出现大量命中，
    说明上游有地方在往快照里塞敏感数据，需要排查。
    """
    hits: list[str] = []

    def walk(obj, path: str = "") -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                p = f"{path}.{k}" if path else str(k)
                if str(k).strip().lower() in FORBIDDEN_KEYS:
                    hits.append(p)
                walk(v, p)
        elif isinstance(obj, str):
            for pat, rep in _PATTERNS:
                if pat.search(obj):
                    hits.append(path or "(root)")

    walk(ctx)
    return (not hits), hits


# ---------------------------------------------------------------- 写入

def create(db: Session, *, user_id: str, kind: str = "other",
           title: str = "", content: str = "", context: dict | None = None,
           book_id: str = "", ch_idx: int = 0) -> dict:
    """提交反馈。

    ★ 校验策略：标题必填、内容至少 5 字（防止刷空反馈）。
      类型非法时归到 other（不报错 —— 用户可能用旧版前端）。
    """
    k = (kind or "").strip().lower()
    if k not in KINDS:
        k = "other"
    t = (title or "").strip()[:MAX_TITLE]
    c = (content or "").strip()
    if not t and not c:
        return {"ok": False, "error": "请填写反馈标题或内容"}
    if len(c) < 5 and len(t) < 5:
        return {"ok": False, "error": "描述太短了，请补充到 5 个字以上"}
    if len(c) > MAX_CONTENT:
        c = c[:MAX_CONTENT]

    s = sanitize_context(context)
    row = UserFeedback(
        id=f"fb_{_new_id()}", user_id=user_id or "", kind=k, title=t,
        content=c[:MAX_CONTENT], context_json=json.dumps(s["ctx"], ensure_ascii=False),
        status="open", priority=("high" if k == "bug" else "normal"),
        tags="[]", book_id=book_id or "", ch_idx=int(ch_idx or 0),
        handled_by="", pooled=0, created_at=_now(), updated_at=_now(),
    )
    db.add(row)
    db.commit()
    return {"ok": True, "id": row.id, "sanitize": s,
            "dropped_keys": s["dropped"]}


def _new_id() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------- 查询

def _row(r: UserFeedback, *, with_ctx: bool = False) -> dict:
    d = {
        "id": r.id, "kind": r.kind, "kind_label": KIND_LABEL.get(r.kind, r.kind),
        "title": r.title, "content": r.content,
        "status": r.status, "status_label": STATUS_LABEL.get(r.status, r.status),
        "priority": r.priority,
        "priority_label": PRIORITY_LABEL.get(r.priority, r.priority),
        "tags": _loads(r.tags, []), "book_id": r.book_id,
        "ch_idx": int(r.ch_idx or 0),
        "admin_note": r.admin_note,
        "created_at": _fmt(r.created_at), "updated_at": _fmt(r.updated_at),
    }
    if with_ctx:
        d["context"] = _loads(r.context_json, {})
        d["pooled"] = bool(r.pooled)
    return d


def _loads(s, default):
    try:
        return json.loads(s or "")
    except Exception:                                   # noqa: BLE001
        return default


def _fmt(t) -> str:
    return t.strftime("%Y-%m-%d %H:%M") if t else ""


def list_mine(db: Session, user_id: str, *, limit: int = 50,
              offset: int = 0) -> dict:
    """我的反馈列表（F1-4：用户可查历史与处理状态）。"""
    rows = list(db.scalars(
        select(UserFeedback).where(UserFeedback.user_id == user_id)
        .order_by(UserFeedback.created_at.desc())
        .limit(min(max(limit, 1), 200)).offset(max(offset, 0))).all())
    total = db.scalar(select(func.count()).select_from(UserFeedback)
                      .where(UserFeedback.user_id == user_id)) or 0
    return {"items": [_row(r) for r in rows], "total": int(total)}


def list_all(db: Session, *, status: str = "", kind: str = "",
             priority: str = "", q: str = "", limit: int = 50,
             offset: int = 0) -> dict:
    """管理端列表（支持筛选 + 关键词）。"""
    conds = []
    if status:
        conds.append(UserFeedback.status == status)
    if kind:
        conds.append(UserFeedback.kind == kind)
    if priority:
        conds.append(UserFeedback.priority == priority)
    if q:
        like = f"%{q.strip()}%"
        conds.append(UserFeedback.title.like(like) | UserFeedback.content.like(like))
    base = select(UserFeedback)
    cnt = select(func.count()).select_from(UserFeedback)
    for c in conds:
        base = base.where(c)
        cnt = cnt.where(c)
    rows = list(db.scalars(
        base.order_by(UserFeedback.created_at.desc())
        .limit(min(max(limit, 1), 200)).offset(max(offset, 0))).all())
    total = db.scalar(cnt) or 0
    return {"items": [_row(r, with_ctx=True) for r in rows], "total": int(total)}


# ---------------------------------------------------------------- 处理

def update(db: Session, fb_id: str, *, status: str = "", priority: str = "",
           admin_note: str = "", tags: list | None = None,
           operator: str = "") -> dict:
    """管理端更新（状态流转 / 优先级 / 备注 / 打标）。"""
    r = db.get(UserFeedback, fb_id)
    if r is None:
        return {"ok": False, "error": "反馈不存在"}
    if status:
        if status not in STATUSES:
            return {"ok": False, "error": f"非法状态：{status}"}
        r.status = status
        if status in ("resolved", "closed"):
            r.handled_by = operator
            r.handled_at = _now()
    if priority:
        if priority not in PRIORITIES:
            return {"ok": False, "error": f"非法优先级：{priority}"}
        r.priority = priority
    if admin_note:
        r.admin_note = admin_note
    if tags is not None:
        r.tags = json.dumps(list(tags)[:10], ensure_ascii=False)
    r.updated_at = _now()
    db.commit()
    return {"ok": True, "id": r.id, "status": r.status}


def stats(db: Session) -> dict:
    """反馈统计（管理端看板）。"""
    by_status: dict[str, int] = {}
    for s in STATUSES:
        n = db.scalar(select(func.count()).select_from(UserFeedback)
                      .where(UserFeedback.status == s)) or 0
        if n:
            by_status[s] = int(n)
    by_kind: dict[str, int] = {}
    for k in KINDS:
        n = db.scalar(select(func.count()).select_from(UserFeedback)
                      .where(UserFeedback.kind == k)) or 0
        if n:
            by_kind[k] = int(n)
    total = db.scalar(select(func.count()).select_from(UserFeedback)) or 0
    pending = (by_status.get("open", 0) + by_status.get("triaged", 0)
               + by_status.get("in_progress", 0))
    return {"total": int(total), "pending": pending,
            "by_status": by_status, "by_kind": by_kind,
            "status_label": STATUS_LABEL, "kind_label": KIND_LABEL}


# ---------------------------------------------------------------- F1-6 记忆联动

# ★ 高频问题阈值：同一关键词出现 ≥ N 次 → 建议进待评估池。
#   为什么要有这个：用户反馈的**聚合**价值远大于单条 ——
#   一个人说"AI 腔重"是抱怨，十个人说是路线问题。
POOL_THRESHOLD = 3

_STOPWORDS = {
    "的", "了", "是", "我", "你", "他", "在", "和", "就", "都", "也", "很",
    "有", "没", "不", "这", "那", "一个", "什么", "怎么", "为什么", "可以",
    "希望", "建议", "问题", "功能", "能否", "有点", "感觉", "时候",
}


def extract_keywords(text: str, *, top: int = 8) -> list[str]:
    """抽取关键词（**确定性**，不用模型）。

    ★ 为什么不用模型：这要跑在反馈提交路径上，模型调用有延迟与成本；
      而且关键词提取是"抽取"任务，规则足够（与 B6 同一思路）。

    ★ 算法（三步，避免滑窗产生无意义碎片）：
      1. 英文/数字词直接取
      2. 中文先算 2-gram 频次，**种子 = 频次 ≥2 的 2-gram**
      3. 从种子向左右扩展（最长 6 字），取**扩展后频次不降**的最长形态
         —— 这样 "AI腔" 会从种子 "AI"+"腔" 扩展出来，
         而 "腔太重了" 这种跨词碎片因为只出现 1 次被淘汰
    """
    t = (text or "").strip()
    if not t:
        return []

    words: dict[str, int] = {}
    # ① 英文/数字词
    for w in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{1,}", t):
        lw = w.lower()
        if lw not in _STOPWORDS and len(lw) >= 2:
            words[lw] = words.get(lw, 0) + 1

    # ② 中文 2-gram 频次
    zh = re.sub(r"[^\u4e00-\u9fff]+", " ", t)
    grams: dict[str, int] = {}
    segs = [s for s in zh.split() if len(s) >= 2]
    for seg in segs:
        for i in range(len(seg) - 1):
            g = seg[i:i + 2]
            if g in _STOPWORDS or any(c in _STOPWORDS for c in g):
                continue
            grams[g] = grams.get(g, 0) + 1

    # ③ 扩展：从每个种子向左/右加字，取频次不降的最长形态
    seeds = {g: c for g, c in grams.items() if c >= 2}
    by_name: dict[str, int] = {}

    def _count(sub: str) -> int:
        """sub 在原文中的出现次数（跨段连续匹配）。"""
        return sum(seg.count(sub) for seg in segs)

    for g, c in seeds.items():
        best = g
        best_c = c
        # 找该种子首次出现的位置做扩展
        for seg in segs:
            pos = seg.find(g)
            if pos < 0:
                continue
            # 向左扩展
            for left in range(1, 3):
                if pos - left < 0:
                    break
                cand = seg[pos - left:pos + len(g)]
                if len(cand) > 6:
                    break
                n = _count(cand)
                if n >= best_c:
                    best, best_c = cand, n
            # 向右扩展（在左扩展结果上继续）
            pos2 = seg.find(best)
            if pos2 < 0:
                continue
            for right in range(1, 3):
                end = pos2 + len(best) + right
                if end > len(seg):
                    break
                cand = seg[pos2:end]
                if len(cand) > 6:
                    break
                n = _count(cand)
                if n >= best_c:
                    best, best_c = cand, n
            break
        by_name[best] = max(by_name.get(best, 0), best_c)

    words.update(by_name)

    # ④ 去重：短词被长词包含则丢弃（"AI" 被 "AI腔" 包含）
    items = sorted(words.items(), key=lambda kv: (-kv[1], -len(kv[0])))
    picked: list[str] = []
    for w, c in items:
        if w in _STOPWORDS or len(w) < 2:
            continue
        if any(w != p and w in p for p in picked):
            continue
        picked.append(w)
        if len(picked) >= top:
            break
    return picked


def hot_topics(db: Session, *, days: int = 30, threshold: int = POOL_THRESHOLD,
               limit: int = 20) -> dict:
    """★ 高频问题聚合（F1-6：自动进待评估池）。

    方法：对近 N 天的反馈做关键词共现统计，出现 ≥ threshold 次的
    关键词即视为"高频问题"，附带原文样本供人工判断。
    """
    import datetime as dt
    since = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=days)
    rows = list(db.scalars(
        select(UserFeedback).where(UserFeedback.created_at >= since)
        .order_by(UserFeedback.created_at.desc())).all())

    hits: dict[str, dict] = {}
    for r in rows:
        kws = extract_keywords(f"{r.title} {r.content}")
        for kw in kws:
            it = hits.setdefault(kw, {"keyword": kw, "count": 0, "ids": [],
                                      "samples": [], "kinds": {}})
            it["count"] += 1
            if len(it["ids"]) < 20:
                it["ids"].append(r.id)
            if len(it["samples"]) < 3:
                it["samples"].append({"id": r.id, "title": r.title,
                                      "kind": r.kind})
            it["kinds"][r.kind] = it["kinds"].get(r.kind, 0) + 1

    hot = [v for v in hits.values() if v["count"] >= threshold]
    hot.sort(key=lambda x: -x["count"])
    return {"days": days, "threshold": threshold,
            "scanned": len(rows), "hot": hot[:limit],
            "pooled_count": int(db.scalar(
                select(func.count()).select_from(UserFeedback)
                .where(UserFeedback.pooled == 1)) or 0)}


def push_to_pool(db: Session, keyword: str, *, note: str = "",
                 operator: str = "") -> dict:
    """★ 把某个高频问题推进"待评估池"（F1-6 记忆联动）。

    ★ 为什么不是自动推：计划书 §附录B 是**人工决策**入口。
      自动推会污染待评估池（关键词误命中很常见）。
      本函数做的是"批量打标 + 生成条目草稿"，由人在后台确认。

    ★★ 匹配策略（踩坑修正）：
      最初只做"keyword ∈ 抽取结果"，但抽取器对中英混排会截断
      （"AI腔" → 只出 "ai"），导致用户明明看到热点却标记不上。
      改为**三重匹配**：
        ① 抽取结果精确等于 keyword
        ② keyword 出现在原始文本里（最直观，符合用户预期）
        ③ 抽取结果中包含 keyword 或反之（处理截断/扩展）
    """
    import datetime as dt
    kw = (keyword or "").strip()
    if not kw:
        return {"ok": False, "error": "缺少关键词"}

    since = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=30)
    rows = list(db.scalars(
        select(UserFeedback).where(UserFeedback.created_at >= since)).all())

    hit_ids: list[str] = []
    for r in rows:
        raw = f"{r.title} {r.content}"
        kws = extract_keywords(raw)
        hit = (kw in kws
               or kw in raw
               or any(kw in k or k in kw for k in kws if k))
        if hit:
            hit_ids.append(r.id)

    for r in rows:
        if r.id in hit_ids:
            r.pooled = 1
    db.commit()

    entry = {
        "keyword": kw, "count": len(hit_ids), "ids": hit_ids[:20],
        "note": note or f"用户反馈高频问题：{kw}",
        "operator": operator,
        "suggest": "已标记进待评估池，请在计划书 §附录B 人工确认后方可实施",
    }
    _log.info("高频问题进池：%s（%d 条反馈）", kw, len(hit_ids))
    return {"ok": True, "pooled": len(hit_ids), "entry": entry}


def export_csv(db: Session, rows: list[dict] | None = None) -> str:
    """导出 CSV（管理端）。★ 不含 context（可能含路径等环境信息）。"""
    import csv
    import io as _io
    if rows is None:
        rows = list_all(db, limit=200)["items"]
    buf = _io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "类型", "标题", "内容", "状态", "优先级", "备注",
                "标签", "关联书", "章节", "创建时间", "更新时间"])
    for r in rows:
        w.writerow([r["id"], r.get("kind_label", r["kind"]), r["title"],
                    _csv_safe(r["content"]), r.get("status_label", r["status"]),
                    r.get("priority_label", r["priority"]), r["admin_note"],
                    ",".join(r.get("tags") or []), r["book_id"], r["ch_idx"],
                    r["created_at"], r["updated_at"]])
    return buf.getvalue()


def _csv_safe(s: str) -> str:
    """★ CSV 注入防护：以 = + - @ 开头的单元格前置单引号。

    为什么：Excel 会把这些当公式执行（`=cmd|...`），
    用户反馈是**不可信输入**，必须转义。
    """
    t = str(s or "")
    if t and t[0] in ("=", "+", "-", "@"):
        return "'" + t
    return t

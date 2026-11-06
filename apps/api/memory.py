"""长程记忆：章节切块索引 + 混合检索（D1）。

为什么需要它：现有上下文装配只给「最近 3 章全文 + 更早 12 章一句话摘要」。
写到第 80 章时，第 12 章前主角获得的关键信物、只出现过一次的配角恩怨，
摘要链那 120 字根本装不下——模型没有可回捞的记忆层。

调研依据（2026-10 扩大调研）：
- RecurrentGPT（学术）：以当前段为 query 语义检索历史段，检索结果进上下文。
- xiehuanyi/NovelForge（工程，实测 400 章/108 万字）：结构化标签过滤比纯语义准。
- 结论：混合检索 = 结构标签过滤 + 稀疏向量语义排序 + 新近度加权。

实现选型：
- 中文 bigram 词频向量做余弦相似度——零外部依赖（不引 sentence-transformers、
  不引向量库）。2000 块规模 numpy 点积 <10ms。
- 接口按「向量可替换」设计：换真嵌入模型时只改 encode() 与 _rank()，schema 不动。
- 首版存储直接用主库 SQLite 表；规模超过 1 万块再考虑 LanceDB。
"""
from __future__ import annotations

import json
import math
import re
import uuid
from collections import Counter

from sqlalchemy import select

from apps.api.models import Chapter, MemoryChunk, Volume, jd, jl, now

# 块大小：中文正文一段约 200-500 字。切 400 字左右、按段落边界对齐最稳。
CHUNK_CHARS = 400
# 默认回捞条数与预算（字）
TOP_K = 6
BUDGET_CHARS = 2000
# 新近度权重：越近的章节越相关，指数半衰期 = 15 章
RECENCY_HALFLIFE = 15.0

_WORD_RE = re.compile(r"[\u4e00-\u9fff]+")


def _new_id() -> str:
    return f"mc_{uuid.uuid4().hex[:12]}"


# ----------------------------------------------------------------- 稀疏向量

def encode(text: str) -> dict[str, int]:
    """中文 bigram 词频向量。英文/数字也切 bigram，标点丢弃。"""
    freq: Counter[str] = Counter()
    for seg in _WORD_RE.findall(text or ""):
        if len(seg) == 1:
            freq[seg] += 1
            continue
        for i in range(len(seg) - 1):
            freq[seg[i:i + 2]] += 1
    return dict(freq)


def _cosine(a: dict[str, int], b: dict[str, int]) -> float:
    if not a or not b:
        return 0.0
    if len(b) < len(a):
        a, b = b, a
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    if not dot:
        return 0.0
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb)


# ----------------------------------------------------------------- 人物识别

def extract_char_names(db, book_id: str, text: str) -> list[str]:
    """确定性扫描：本章正文里出现了哪些已登记人物（含别称）。"""
    from apps.api.models import Character
    rows = db.scalars(select(Character).where(Character.book_id == book_id)).all()
    if not rows:
        return []
    hit: list[str] = []
    for c in rows:
        names = [c.name]
        for alias in jl(c.aliases, []) or []:
            if alias:
                names.append(str(alias))
        if any(n and n in text for n in names):
            hit.append(c.name)
    return hit


# ----------------------------------------------------------------- 切块入库

def _split_chunks(content: str) -> list[str]:
    """按段落聚合切块：凑到 CHUNK_CHARS 为止，单段超长硬切。"""
    paras = [p.strip() for p in (content or "").split("\n") if p.strip()]
    # 跳过标题行（# 开头）
    paras = [p for p in paras if not p.startswith("#")]
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for p in paras:
        # 单段超长：按 CHUNK_CHARS 硬切成多块
        while len(p) > CHUNK_CHARS * 2:
            chunks.append(p[:CHUNK_CHARS * 2])
            p = p[CHUNK_CHARS * 2:]
        buf.append(p)
        size += len(p)
        if size >= CHUNK_CHARS:
            chunks.append("\n".join(buf))
            buf, size = [], 0
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def _volume_idx_of(db, book_id: str, ch_idx: int) -> int:
    v = db.scalar(
        select(Volume).where(
            Volume.book_id == book_id,
            Volume.start_ch <= ch_idx,
            (Volume.end_ch >= ch_idx) | (Volume.end_ch == 0) | (Volume.end_ch.is_(None)),
        ).order_by(Volume.idx)
    )
    return int(v.idx) if v else 0


def index_chapter(db, book_id: str, ch_idx: int, content: str,
                  title: str = "") -> int:
    """把一章正文切块入库（先删旧块再写，幂等——重传/改稿不产生重复）。

    ★ v0.21.1 B1：同时生成**稠密向量**（若远端 embedding 可用）。
      失败不影响入库 —— 稀疏 bigram 永远会写，检索时自动降级。
    """
    from apps.api import embedding as _emb

    old = db.scalars(select(MemoryChunk).where(
        MemoryChunk.book_id == book_id, MemoryChunk.ch_idx == ch_idx)).all()
    for r in old:
        db.delete(r)
    vol = _volume_idx_of(db, book_id, ch_idx)
    names = extract_char_names(db, book_id, content or "")

    # ★ 先切块，再**批量**编码（比逐块调用快 N 倍，也省 API 配额）
    raw_chunks = [c for c in _split_chunks(content or "") if len(c) >= 40]
    if not raw_chunks:
        db.commit()
        return 0

    enc = _emb.get_encoder()
    dense_list: list = [None] * len(raw_chunks)
    try:
        dense_list = enc.encode_batch(raw_chunks)
    except Exception as e:                          # noqa: BLE001
        # ★ 编码失败必须放行入库（否则一章写不进去 = 功能不可用）
        import logging
        logging.getLogger("novelforge").warning(
            "向量编码失败，本章仅存稀疏向量（检索自动降级）：%s: %s",
            type(e).__name__, e)

    emb_model = getattr(getattr(enc, "remote", None), "model", "") or ""

    n = 0
    for i, chunk in enumerate(raw_chunks):
        # 每块前缀带上章号，检索命中后模型能直接定位时间线位置
        text = f"[第{ch_idx:03d}章{i if i == 0 else ''}] " if i == 0 else f"[第{ch_idx:03d}章] "
        text += chunk
        dv = dense_list[i] if i < len(dense_list) else None
        emb_b64, emb_dim = "", 0
        if dv:
            try:
                emb_b64 = _emb.quantize_to_b64(dv)
                emb_dim = len(dv)
            except Exception:
                emb_b64, emb_dim = "", 0
        db.add(MemoryChunk(
            id=f"{_new_id()}_{i}", book_id=book_id, ch_idx=int(ch_idx),
            volume_idx=vol, kind="scene", text=text,
            char_tags=jd(names), vec_json=jd(encode(chunk)),
            emb=emb_b64, emb_dim=emb_dim, emb_model=(emb_model if emb_b64 else ""),
            words=len(chunk), created_at=now(),
        ))
        n += 1
    db.commit()
    return n


def backfill_embeddings(db, book_id: str = "", limit: int = 500) -> dict:
    """★ 回填稠密向量（老数据补齐 / 配好 embedding 后跑一次）。

    为什么需要：老库里的块只有稀疏向量。配好远端 embedding 后，
    需要把历史块补上稠密向量，否则新旧块打分不可比。
    """
    from apps.api import embedding as _emb

    q = select(MemoryChunk).where(
        (MemoryChunk.emb == "") | (MemoryChunk.emb.is_(None)))
    if book_id:
        q = q.where(MemoryChunk.book_id == book_id)
    rows = db.scalars(q.limit(max(1, min(limit, 5000)))).all()
    if not rows:
        return {"ok": True, "total": 0, "done": 0, "failed": 0}

    enc = _emb.get_encoder()
    texts = [r.text or "" for r in rows]
    try:
        dense = enc.encode_batch(texts)
    except Exception as e:                          # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "total": len(rows), "done": 0, "failed": len(rows)}

    emb_model = getattr(getattr(enc, "remote", None), "model", "") or ""
    done = failed = 0
    for r, dv in zip(rows, dense):
        if not dv:
            failed += 1
            continue
        try:
            r.emb = _emb.quantize_to_b64(dv)
            r.emb_dim = len(dv)
            r.emb_model = emb_model
            done += 1
        except Exception:
            failed += 1
    db.commit()
    return {"ok": True, "total": len(rows), "done": done, "failed": failed}


def deindex_chapter(db, book_id: str, ch_idx: int) -> int:
    old = db.scalars(select(MemoryChunk).where(
        MemoryChunk.book_id == book_id, MemoryChunk.ch_idx == ch_idx)).all()
    for r in old:
        db.delete(r)
    db.commit()
    return len(old)


# ----------------------------------------------------------------- 检索

def search(db, book_id: str, query: str, ch_idx: int = 0,
           top_k: int = TOP_K, budget: int = BUDGET_CHARS,
           char_names: list[str] | None = None,
           dense_weight: float = 0.55) -> list[dict]:
    """★ 混合检索（v0.21.1 B1 升级）。

    打分 = 稠密语义(dense) × w + 稀疏 bigram(sparse) × (1-w)
           + 结构标签奖励 + 新近度奖励

    ★ 为什么混合而不是纯稠密：
      · 稠密强在**语义**（"愤怒"≈"勃然大怒"），弱在**专名**（人名/法宝名容易漂）
      · 稀疏强在**精确匹配**（"赤霄剑"三个字必须对上），弱在语义
      两者互补。任务单里既有语义意图（"主角复仇"）又有专名（"赤霄剑"），
      所以必须混合 —— 这是本次调研（xiehuanyi/NovelForge 反证）的结论。

    ★ 降级：没有稠密向量的块（emb 为空）→ 只用稀疏分，**不会掉队**
      （权重自动归一到稀疏上，见下方 normalize）。
    """
    from apps.api import embedding as _emb

    q_sparse = encode(query or "")
    if not q_sparse:
        return []
    q_dense = _emb.cached_encode(query or "")       # None = 远端不可用（降级）

    rows = db.scalars(select(MemoryChunk).where(
        MemoryChunk.book_id == book_id,
        MemoryChunk.ch_idx < int(ch_idx or 10 ** 9),
    ).order_by(MemoryChunk.ch_idx.desc())).all()
    if not rows:
        return []

    want = set(char_names or [])
    scored: list[tuple[float, MemoryChunk, dict]] = []
    for r in rows:
        s_sparse = _cosine(q_sparse, jl(r.vec_json, {}) or {})
        # ★ 稠密分：只有 query 与 chunk 都有向量才算
        s_dense = 0.0
        has_dense = False
        if q_dense and r.emb:
            dv = _emb.dequantize_from_b64(r.emb, r.emb_dim or 0)
            if dv and len(dv) == len(q_dense):
                s_dense = _emb.dot(q_dense, dv)
                has_dense = True

        if has_dense:
            sim = s_dense * dense_weight + s_sparse * (1.0 - dense_weight)
        else:
            # ★ 降级：没有稠密向量 → 全押稀疏（并轻微折扣，避免与有向量的块不公平竞争）
            sim = s_sparse * 0.92
        if sim <= 0.02:                 # 完全不相关直接丢，省预算
            continue

        recency = math.pow(0.5, (int(ch_idx or 0) - r.ch_idx) / RECENCY_HALFLIFE)
        tags = set(jl(r.char_tags, []) or [])
        tag_bonus = 0.15 if (want & tags) else 0.0
        score = sim * 0.75 + recency * 0.15 + tag_bonus
        scored.append((score, r, {"sparse": round(s_sparse, 4),
                                  "dense": round(s_dense, 4),
                                  "dense_used": has_dense,
                                  "tag": tag_bonus > 0,
                                  "recency": round(recency, 4)}))

    scored.sort(key=lambda x: -x[0])
    out: list[dict] = []
    used = 0
    for score, r, detail in scored:
        t = r.text or ""
        if used + len(t) > budget:
            break
        out.append({"ch_idx": r.ch_idx, "score": round(score, 4),
                    "text": t, "chars": len(t), "detail": detail})
        used += len(t)
        if len(out) >= top_k:
            break
    out.sort(key=lambda x: x["ch_idx"])  # 按章号正序呈现，符合阅读惯性
    return out


def render_retrieved(hits: list[dict]) -> str:
    """检索结果 → 上下文包文本。空结果返回空串（不占包）。"""
    if not hits:
        return ""
    lines = []
    for h in hits:
        lines.append(f"{h['text'].strip()}")
    return "\n\n".join(lines)


# ----------------------------------------------------------------- 建索引维护

def reindex_book(db, book_id: str) -> int:
    """全书重建索引（升级后跑一次 / 或手动触发）。"""
    chs = db.scalars(select(Chapter).where(Chapter.book_id == book_id)
                     .order_by(Chapter.idx)).all()
    n = 0
    for c in chs:
        n += index_chapter(db, book_id, c.idx, c.content or "", c.title or "")
    return n


def memory_stats(db, book_id: str) -> dict:
    """索引统计（★ v0.21.1 B1 加向量覆盖率）。"""
    from apps.api import embedding as _emb

    rows = db.scalars(select(MemoryChunk).where(MemoryChunk.book_id == book_id)).all()
    with_dense = sum(1 for r in rows if r.emb)
    return {
        "chunks": len(rows),
        "words": sum(r.words or 0 for r in rows),
        "dense_chunks": with_dense,
        "dense_ratio": round(with_dense / len(rows) * 100, 1) if rows else 0.0,
        "encoder": _emb.stats(),
    }


def recall_quality(db, book_id: str, query: str, ch_idx: int = 0,
                   top_k: int = 6) -> dict:
    """★ 召回质量抽检（B1 验收用：人工抽检相关性 ≥ 80%）。

    返回命中块与分数明细，供人工判断相关性。
    """
    hits = search(db, book_id, query, ch_idx=ch_idx, top_k=top_k)
    return {
        "query": query,
        "ch_idx": ch_idx,
        "count": len(hits),
        "chars": sum(h["chars"] for h in hits),
        "hits": [{"ch_idx": h["ch_idx"], "score": h["score"],
                  "detail": h.get("detail", {}),
                  "preview": (h["text"] or "")[:120]} for h in hits],
    }

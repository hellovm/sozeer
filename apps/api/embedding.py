"""向量嵌入层（v0.21.1 B1）。

★★★ 为什么需要独立一层：
  计划书 B1 要求「扩展 memory_chunks 加 embedding 列 + 自动向量化 + 语义召回
  + **降级兜底**」。当前 `memory.py` 用的是**中文 bigram 词频**稀疏向量 ——
  零依赖，但语义召回弱（"他很愤怒" vs "他勃然大怒" 几乎不相似）。

================================ 可插拔 Provider ================================

  EmbeddingProvider（抽象）
     ├─ RemoteEmbedding   ★ 远端 API（OpenAI 兼容 /v1/embeddings）
     ├─ LocalEmbedding    ★ 本地 bigram 稀疏向量（零依赖、零延迟、离线可用）
     └─ FallbackEmbedding ★ 组合器：远端优先，失败自动降级本地

★ 核心设计：**降级永不失败**
  B1 验收明确要求"embedding 服务不可用时自动降级，不报错"。
  做法：所有远端调用包 try/except，失败返回 None → 组合器切本地实现。
  **而且要在同一个进程里持续用降级结果**（不要每次都重试远端超时，
  否则一次故障会让每次检索都慢 3 秒）。

================================ 关键工程细节 ================================

  1. **向量归一化**：存 L2 归一化后的向量 → 余弦相似度退化为点积（快 3 倍）
  2. **int8 量化**：向量存为 int8 数组（Base64），体积降 4 倍，精度损失可忽略
     —— 1024 维 float32 = 4KB/块，量化后 1KB。10 万块 = 400MB → 100MB
  3. **批量 + 并发限制**：远端 API 一次最多 100 条（各家不同），串行分批
  4. **进程内缓存**：同一段文本重复编码直接命中（写作时同一 query 会多次检索）
  5. **熔断**：连续失败 N 次后**主动停止尝试远端**，避免每次都等超时
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import re
import struct
import threading
import time
from collections import Counter

log = logging.getLogger("novelforge")

# ★ 远端 embedding 的维度上限（不同模型不同，用于校验）
MAX_DIM = 4096
# ★ 单侧失败后就地熔断的阈值与冷却时间
FAIL_THRESHOLD = 3
COOLDOWN_SEC = 300


# ================================================================ 编码工具

def l2_normalize(vec: list[float]) -> list[float]:
    """L2 归一化 → 余弦相似度退化为点积。"""
    n = math.sqrt(sum(v * v for v in vec))
    if n <= 1e-12:
        return [0.0] * len(vec)
    return [v / n for v in vec]


def quantize_to_b64(vec: list[float]) -> str:
    """★ float 向量 → int8 → Base64（体积降 4 倍）。

    为什么用 int8 而不是 float16：
      int8 实现简单（乘法缩放），且精度足够 —— 向量检索对精度不敏感
      （本来就是近似召回），但存储/传输成本是 1/4。
    """
    arr = bytearray(len(vec))
    for i, v in enumerate(vec):
        # [-1, 1] → [-127, 127]，四舍五入并夹紧
        q = int(round(max(-1.0, min(1.0, float(v))) * 127))
        arr[i] = (q + 256) & 0xFF          # 存为无符号字节
    return base64.b64encode(bytes(arr)).decode("ascii")


def dequantize_from_b64(s: str, dim: int = 0) -> list[float]:
    """Base64 → int8 → float 向量（已归一化）。"""
    if not s:
        return []
    try:
        raw = base64.b64decode(s)
        out = []
        for b in raw:
            q = b if b < 128 else b - 256
            out.append(q / 127.0)
        return out
    except Exception:
        return []


def dot(a: list[float], b: list[float]) -> float:
    """点积（输入须已归一化 → 等价余弦相似度）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


# ================================================================ 本地实现

_WORD_RE = re.compile(r"[\u4e00-\u9fff]+")


class LocalEmbedding:
    """本地 bigram 稀疏向量（零依赖兜底）。

    ★ 产出的是**稀疏字典**（{bigram: count}），不是稠密向量。
      与远端稠密向量分数不可直接比较 —— 所以混合检索里
      两种分数分开算，最后加权融合（见 memory.rank_hybrid）。
    """

    name = "local"
    dim = 0            # 稀疏，维度不定

    def encode(self, text: str) -> dict[str, int]:
        freq: Counter[str] = Counter()
        for seg in _WORD_RE.findall(text or ""):
            if len(seg) == 1:
                freq[seg] += 1
                continue
            for i in range(len(seg) - 1):
                freq[seg[i:i + 2]] += 1
        return dict(freq)

    def encode_batch(self, texts: list[str]) -> list[dict]:
        return [self.encode(t) for t in texts]


def cosine_sparse(a: dict, b: dict) -> float:
    """稀疏向量余弦。"""
    if not a or not b:
        return 0.0
    if len(b) < len(a):
        a, b = b, a
    dot_v = sum(v * b.get(k, 0) for k, v in a.items())
    if not dot_v:
        return 0.0
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot_v / (na * nb) if na and nb else 0.0


# ================================================================ 远端实现

class RemoteEmbedding:
    """远端 embedding API（OpenAI 兼容 /v1/embeddings）。

    ★ 支持所有 OpenAI 协议兼容的服务：
      · OpenAI text-embedding-3-small（1536 维）
      · 硅基流动 BAAI/bge-m3（1024 维）
      · 阿里云 text-embedding-v3（1024 维）
      · 本地 Ollama / vLLM（任意维度）
    """

    name = "remote"

    def __init__(self, base_url: str, api_key: str, model: str = "text-embedding-3-small",
                 dim: int = 0, batch: int = 64, timeout: float = 20.0):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or "text-embedding-3-small"
        self.dim = int(dim or 0)
        self.batch = max(1, min(int(batch or 64), 128))
        self.timeout = timeout
        # ★ 熔断状态
        self._fails = 0
        self._down_until = 0.0
        self._lock = threading.Lock()

    def available(self) -> bool:
        """是否可用（配置齐全 + 未熔断）。"""
        if not self.base_url or not self.api_key:
            return False
        with self._lock:
            return time.time() >= self._down_until

    def _mark_fail(self) -> None:
        with self._lock:
            self._fails += 1
            if self._fails >= FAIL_THRESHOLD:
                self._down_until = time.time() + COOLDOWN_SEC
                log.warning("★ Embedding 远端连续失败 %d 次，熔断 %d 秒（自动降级本地）",
                            self._fails, COOLDOWN_SEC)

    def _mark_ok(self) -> None:
        with self._lock:
            self._fails = 0
            self._down_until = 0.0

    def encode_batch(self, texts: list[str]) -> list[list[float]] | None:
        """批量编码。失败返回 None（调用方降级）。"""
        if not texts:
            return []
        if not self.available():
            return None
        import httpx
        out: list[list[float]] = []
        try:
            for i in range(0, len(texts), self.batch):
                part = [t[:8000] for t in texts[i:i + self.batch]]   # 截断防超限
                r = httpx.post(
                    f"{self.base_url}/embeddings",
                    json={"model": self.model, "input": part},
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Content-Type": "application/json"},
                    timeout=self.timeout)
                if r.status_code >= 400:
                    log.warning("Embedding 远端返回 %s：%s", r.status_code, r.text[:200])
                    self._mark_fail()
                    return None
                j = r.json()
                data = sorted(j.get("data") or [], key=lambda x: x.get("index", 0))
                if len(data) != len(part):
                    log.warning("Embedding 返回条数不符：%d != %d", len(data), len(part))
                    self._mark_fail()
                    return None
                for d in data:
                    v = d.get("embedding") or []
                    if not v:
                        self._mark_fail()
                        return None
                    if not self.dim:
                        self.dim = len(v)
                    out.append(l2_normalize([float(x) for x in v]))
            self._mark_ok()
            return out
        except Exception as e:                      # noqa: BLE001
            log.warning("Embedding 远端异常：%s: %s", type(e).__name__, e)
            self._mark_fail()
            return None

    def encode(self, text: str) -> list[float] | None:
        r = self.encode_batch([text])
        return r[0] if r else None


# ================================================================ 组合器

class FallbackEmbedding:
    """★ 组合器：远端优先，失败自动降级本地（永不失败）。

    ★ 关键设计：**记住降级状态**。
      如果每次都先试远端（即使已知它挂了），一次故障会让每次检索都慢 3 秒。
      所以远端熔断后，直接走本地（见 RemoteEmbedding.available）。
    """

    def __init__(self, remote: RemoteEmbedding | None = None,
                 local: LocalEmbedding | None = None):
        self.remote = remote
        self.local = local or LocalEmbedding()
        self.name = "hybrid" if (remote and remote.available()) else "local"

    def encode(self, text: str) -> list[float] | None:
        """返回稠密向量（远端）；本地无稠密向量 → 返回 None。"""
        if self.remote and self.remote.available():
            v = self.remote.encode(text)
            if v is not None:
                return v
        return None

    def encode_batch(self, texts: list[str]) -> list[list[float] | None]:
        if self.remote and self.remote.available():
            v = self.remote.encode_batch(texts)
            if v is not None:
                return v
        return [None] * len(texts)

    def encode_sparse(self, text: str) -> dict:
        """本地稀疏向量（始终可用）。"""
        return self.local.encode(text)

    def describe(self) -> dict:
        r = self.remote
        # ★ 判定"实际生效的 provider"：
        #   远端配置了**且**未熔断**且**没有近期失败 → remote；否则 local。
        #   为什么不只看 available()：第一次失败后熔断计数是 1（未达阈值），
        #   此时 available() 仍为 True，但实际上已经在用本地兜底了。
        effective_remote = bool(r and r.available() and r._fails == 0)
        return {
            "provider": "remote" if effective_remote else "local",
            "remote_configured": bool(r and r.base_url and r.api_key),
            "remote_model": (r.model if r else ""),
            "remote_dim": (r.dim if r else 0),
            "remote_down": bool(r and not r.available()),
            "recent_fails": (r._fails if r else 0),
        }


# ================================================================ 单例与缓存

_encoder: FallbackEmbedding | None = None
_cache: dict[str, list[float]] = {}
_cache_lock = threading.Lock()
CACHE_MAX = 2000


def get_encoder() -> FallbackEmbedding:
    """取编码器单例（按配置组装）。"""
    global _encoder
    if _encoder is not None:
        return _encoder
    import os

    def _e(k: str, d: str = "") -> str:
        return os.environ.get(k, "") or d

    base, key = _e("NF_EMBEDDING_BASE"), _e("NF_EMBEDDING_KEY")
    # 未单独配置时，允许从默认模型槽（ModelProfile）借用
    if not base or not key:
        try:
            from apps.api.db import SessionLocal
            from apps.api.models import ModelProfile
            from apps.api import keyvault
            from sqlalchemy import select
            db = SessionLocal()
            try:
                p = db.scalars(select(ModelProfile)
                               .where(ModelProfile.enabled == 1)
                               .order_by(ModelProfile.is_default.desc())).first()
                if p is not None:
                    base = base or (p.base_url or "")
                    key = key or keyvault.get_api_key(p)
            finally:
                db.close()
        except Exception:
            pass

    remote = None
    if base and key:
        remote = RemoteEmbedding(
            base_url=base, api_key=key,
            model=_e("NF_EMBEDDING_MODEL", "text-embedding-3-small"),
            dim=int(_e("NF_EMBEDDING_DIM", "0") or 0),
            batch=int(_e("NF_EMBEDDING_BATCH", "64") or 64))
        log.info("向量检索：远端 embedding（%s @ %s）+ 本地 bigram 兜底",
                 remote.model, remote.base_url)
    else:
        log.info("向量检索：仅本地 bigram（未配置 NF_EMBEDDING_BASE/KEY，零依赖运行）")

    _encoder = FallbackEmbedding(remote=remote)
    return _encoder


def reset_encoder() -> None:
    """重置单例（测试用）。"""
    global _encoder
    _encoder = None
    with _cache_lock:
        _cache.clear()


def cached_encode(text: str) -> list[float] | None:
    """★ 带缓存的编码（写作时同一 query 会多次检索）。

    为什么必须缓存：一次章节生成会调 3-5 次检索（战略/战术/校验），
    每次都发网络请求既慢又费钱。
    """
    if not text:
        return None
    key = hashlib.md5(text[:2000].encode("utf-8")).hexdigest()
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    v = get_encoder().encode(text)
    if v is not None:
        with _cache_lock:
            if len(_cache) >= CACHE_MAX:
                # 简单淘汰：清 1/4
                for k in list(_cache.keys())[:CACHE_MAX // 4]:
                    _cache.pop(k, None)
            _cache[key] = v
    return v


def stats() -> dict:
    """编码器状态（后台观测）。"""
    e = get_encoder()
    d = e.describe()
    with _cache_lock:
        d["cache_size"] = len(_cache)
    d["cache_max"] = CACHE_MAX
    return d

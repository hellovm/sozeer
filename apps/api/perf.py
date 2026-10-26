"""性能审计工具（v0.23.3 D3）。

★ 三条主线：
  1. **索引审计**：找出"该建索引但没建"的列（外键、高频 where 列）
  2. **慢查询检测**：运行期采集超过阈值的 SQL（含调用位置）
  3. **N+1 检测**：同一请求内重复模式的查询计数

★ 为什么需要这个模块而不是"凭经验加索引"：
  经验加索引会漏（新表新列没人回头补），也会滥加（写放大）。
  做成**可执行的检查**才能进 CI —— 每次发版自动体检。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter, defaultdict

from sqlalchemy import event, inspect, text
from sqlalchemy.orm import Session

_log = logging.getLogger("novelforge.perf")

# ----------------------------------------------------------------- 慢查询采集

SLOW_MS = 200.0            # 超过即记录（200ms 对 OLTP 已经很慢）
_MAX_SAMPLES = 200         # 环形缓冲上限（防内存泄漏）


class _Collector:
    """线程安全的慢查询采集器。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.samples: list[dict] = []
        self.counts: Counter = Counter()
        self.total = 0
        self.slow_total = 0
        self.max_ms = 0.0

    def add(self, sql: str, ms: float, stack: str = "") -> None:
        with self.lock:
            self.total += 1
            if ms > self.max_ms:
                self.max_ms = ms
            if ms < SLOW_MS:
                return
            self.slow_total += 1
            norm = _normalize(sql)
            self.counts[norm] += 1
            self.samples.append({"sql": sql[:600], "ms": round(ms, 1),
                                 "at": time.strftime("%H:%M:%S"),
                                 "stack": stack[:300]})
            if len(self.samples) > _MAX_SAMPLES:
                del self.samples[:(_MAX_SAMPLES // 2)]

    def reset(self) -> None:
        with self.lock:
            self.samples.clear()
            self.counts.clear()
            self.total = 0
            self.slow_total = 0
            self.max_ms = 0.0

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "total_queries": self.total,
                "slow_queries": self.slow_total,
                "threshold_ms": SLOW_MS,
                "max_ms": round(self.max_ms, 1),
                "samples": list(reversed(self.samples[-30:])),
                "top_patterns": [{"sql": k, "count": v}
                                 for k, v in self.counts.most_common(15)],
            }


_col = _Collector()


def collector() -> _Collector:
    return _col


_INSTALLED = False


def install(engine) -> bool:
    """挂 SQLAlchemy 事件（幂等）。

    ★ 为什么用事件而不是中间件：
      中间件只能看到 HTTP 层总耗时，无法定位"是哪个 SQL 慢"。
      事件粒度到语句，且能拿到调用栈。
    """
    global _INSTALLED
    if _INSTALLED:
        return False
    import traceback as _tb

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        context._nf_t0 = time.perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        t0 = getattr(context, "_nf_t0", None)
        if t0 is None:
            return
        ms = (time.perf_counter() - t0) * 1000
        if ms >= SLOW_MS:
            # 只慢查询才抓栈（抓栈有成本）
            stack = "".join(_tb.format_stack(limit=8)[:-4])
            _col.add(statement, ms, stack)

    _INSTALLED = True
    return True


def _normalize(sql: str) -> str:
    """归一化 SQL（把字面量替换成 ?），便于按模式聚合。"""
    s = re.sub(r"'[^']*'", "?", sql or "")
    s = re.sub(r"\b\d+\b", "?", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:300]


# ----------------------------------------------------------------- 索引审计

# 期望索引：表 → [列名]。仅列"确认需要"的，避免滥加。
EXPECTED_INDEXES: dict[str, list[str]] = {
    "llm_calls": ["task", "user_id", "book_id", "created_at"],
    "media_calls": ["book_id"],
    "chapters": ["book_id"],
    "memory_chunks": ["book_id"],
    "book_scenes": ["book_id", "ch_idx"],
    "character_relations": ["book_id", "from_id", "to_id"],
    "chapter_emotion": ["book_id", "ch_idx"],
    "character_knowledge": ["book_id", "character_id"],
    "credit_ledger": ["user_id"],
    "login_records": ["user_id"],
    "admin_audit_logs": ["created_at"],
    "refresh_tokens": ["user_id", "token_hash", "expires_at"],
    "role_permissions": ["role"],
}

# 高频 where 列（来自代码审计）：这些列若无索引，查询会全表扫描
HOT_FILTER_COLUMNS: dict[str, list[str]] = {
    "books": ["owner_id"],
    "orders": ["user_id", "status"],
    "users": ["username"],
}


def audit_indexes(engine) -> dict:
    """索引审计：对比实际索引与期望，报缺失/冗余。

    ★ 覆盖 PG 与 SQLite（本地测试用 SQLite）。
    """
    insp = inspect(engine)
    is_pg = engine.dialect.name == "postgresql"
    tables = set(insp.get_table_names())
    missing: list[dict] = []
    present: list[dict] = []

    for table, cols in {**EXPECTED_INDEXES, **HOT_FILTER_COLUMNS}.items():
        if table not in tables:
            continue
        try:
            idx = insp.get_indexes(table)
            pk = insp.get_pk_constraint(table) or {}
        except Exception:                                # noqa: BLE001
            continue
        indexed: set[str] = set(pk.get("constrained_columns") or [])
        # 复合索引的首列也算（PG 与 SQLite 都能用首列做前缀查找）
        for i in idx:
            colseq = i.get("column_names") or []
            if colseq:
                indexed.add(colseq[0])
                for c in colseq:
                    indexed.add(c)
        for c in cols:
            rec = {"table": table, "column": c, "indexed": c in indexed}
            (present if rec["indexed"] else missing).append(rec)

    return {
        "dialect": engine.dialect.name,
        "checked_tables": len([t for t in EXPECTED_INDEXES if t in tables]),
        "missing": missing,
        "ok": present,
        "missing_count": len(missing),
        "note": ("无缺失" if not missing else
                 f"{len(missing)} 列缺索引，全表扫描风险"),
        "pg_hint": ("PG 可用 CREATE INDEX CONCURRENTLY 在线建（不锁表）"
                    if is_pg else "SQLite 索引较轻量"),
    }


# ----------------------------------------------------------------- N+1 检测

class NPlusOneDetector:
    """N+1 检测：统计一次请求内的查询模式。

    ★ 判定规则：
      归一化后的同一条 SQL 在**一次请求内**出现 ≥ N 次 → 疑似 N+1。
      为什么按"归一化后"：`WHERE id = 1` 与 `WHERE id = 2` 是同一条模式，
      循环里查 100 次正是 N+1 的典型形态。
    """

    def __init__(self, threshold: int = 10) -> None:
        self.threshold = threshold
        self.counter: Counter = Counter()
        self.enabled = True

    def record(self, sql: str) -> None:
        if not self.enabled:
            return
        self.counter[_normalize(sql)] += 1

    def suspects(self) -> list[dict]:
        return [{"sql": k, "count": v}
                for k, v in self.counter.most_common()
                if v >= self.threshold]

    def reset(self) -> None:
        self.counter.clear()


_np1 = NPlusOneDetector()


def nplusone() -> NPlusOneDetector:
    return _np1


def install_nplusone(engine) -> bool:
    """挂 N+1 检测（与慢查询共用事件，但只在开发/诊断开关下启用）。"""
    @event.listens_for(engine, "before_cursor_execute")
    def _rec(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        _np1.record(statement)
    return True


# ----------------------------------------------------------------- 采样计时

class timed:
    """上下文管理器：给任意代码块计时（性能分析用）。

    用法：
        with perf.timed("db_load") as t:
            ...
        print(t.ms)
    """

    __slots__ = ("label", "ms", "t0")

    def __init__(self, label: str = "") -> None:
        self.label = label
        self.ms = 0.0
        self.t0 = 0.0

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):  # noqa: ANN002
        self.ms = round((time.perf_counter() - self.t0) * 1000, 2)
        return False


# ----------------------------------------------------------------- 表体积

def table_sizes(db: Session, top: int = 15) -> list[dict]:
    """各表行数（找出"最大但最少用"的表）。"""
    out: list[dict] = []
    insp = inspect(db.get_bind())
    for t in insp.get_table_names():
        if t.startswith("sqlite_"):
            continue
        try:
            n = db.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar() or 0
            out.append({"table": t, "rows": int(n)})
        except Exception:                                # noqa: BLE001
            continue
    out.sort(key=lambda x: -x["rows"])
    return out[:top]


def report(db: Session) -> dict:
    """综合性能报告（管理后台用）。"""
    eng = db.get_bind()
    snap = _col.snapshot()
    return {
        "slow_query": snap,
        "indexes": audit_indexes(eng),
        "nplusone": {"suspects": _np1.suspects(),
                     "threshold": _np1.threshold,
                     "enabled": _np1.enabled},
        "table_sizes": table_sizes(db),
        "advice": _advice(snap),
    }


def _advice(snap: dict) -> list[str]:
    """把采样数据转成可操作建议。★ 只报问题不给方案 = 无用。"""
    out: list[str] = []
    if snap["slow_queries"] == 0:
        out.append("✅ 未采集到慢查询（阈值 %dms）" % SLOW_MS)
    else:
        out.append(f"⚠️ 采集到 {snap['slow_queries']} 次慢查询，"
                   f"最慢 {snap['max_ms']}ms")
    for p in snap.get("top_patterns", [])[:3]:
        out.append(f"· 高频慢 SQL（{p['count']} 次）：{p['sql'][:90]}")
    return out

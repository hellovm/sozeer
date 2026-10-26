"""Redis 客户端与降级（v0.25 J0-1 / J0-2）。

★★ 本模块存在的唯一理由：**Redis 挂了不能全站挂**。

三层保障：
  ① **连接层降级**：连不上 → 所有操作走"无 Redis"分支，功能降级但可用
  ② **启动重建**：DB 是**唯一真相源**，Redis 只做调度；启动时从 DB 回灌
  ③ **健康探测**：连续失败 N 次后进入"已知不可用"状态，避免每次都等超时

★ 为什么"宁可降级也不用内存队列"：
  内存队列在多 worker 下会各排各的（任务重复/丢失），
  比"没有队列"更糟。降级时直接**同步执行**，行为确定。

★ 依赖说明：`redis-py` 必须写进 requirements.txt。
  （★ v0.19.3 教训：依赖声明与依赖安装是两件事）
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

_log = logging.getLogger("novelforge.queue")

# ---------------------------------------------------------------- 配置

ENV_URL = "NF_REDIS_URL"
ENV_DISABLE = "NF_REDIS_DISABLE"        # 1 = 强制禁用（本地开发/测试）
DEFAULT_URL = "redis://127.0.0.1:6379/0"

# ★ 键前缀：多实例/多环境共用一台 Redis 时隔离
PREFIX = "sozeer"

# 队列相关键
K_QUEUE = f"{PREFIX}:queue"             # ZSET：member=task_id, score=优先级分
K_TASK = f"{PREFIX}:task"               # HASH：task_id → JSON 状态
K_RUNNING = f"{PREFIX}:running"         # ZSET：正在执行（含超时检测）
K_DLQ = f"{PREFIX}:dlq"                 # LIST：死信
K_STAT = f"{PREFIX}:stat"               # HASH：计数器
# ★ v0.25.6 J5：并发与配额
K_USER_RUN = f"{PREFIX}:userrun"        # HASH：user_id → 正在执行的数目
K_BOOK_RUN = f"{PREFIX}:bookrun"        # HASH：book_id → 正在执行的数目
K_QUOTA = f"{PREFIX}:quota"             # HASH：user_id → 当日已用（token 预扣）

# ★ v0.25.6 J5：并发/配额默认值（可由配置中心覆盖）
#   ★ 为什么并发要限：生成是重操作（每章几万 token + 几分钟 CPU），
#     不限并发的话一个用户开 10 个标签页就能把机器打满，**影响所有其他用户**。
MAX_GLOBAL_CONCURRENCY = 8              # 全站同时执行上限（受机器规格约束）
MAX_PER_USER = 2                        # 单用户同时执行上限
MAX_PER_BOOK = 1                        # ★ 同一本书串行（否则章节顺序会乱）

FAIL_THRESHOLD = 3                      # 连续失败 N 次 → 判定不可用
RECOVER_SECONDS = 30                    # 判定不可用后，N 秒内不再尝试连接

_lock = threading.Lock()
_client = None
_fails = 0
_down_until = 0.0
_disabled_reason = ""


def redis_url() -> str:
    return (os.environ.get(ENV_URL) or DEFAULT_URL).strip()


def is_forced_disabled() -> bool:
    """是否被显式禁用（测试/本地开发用）。"""
    return os.environ.get(ENV_DISABLE, "").strip() in ("1", "true", "on", "yes")


# ---------------------------------------------------------------- 连接

def _connect(url: str):
    """建立连接（延迟导入 redis，未安装时返回 None）。"""
    try:
        import redis  # type: ignore
    except ImportError:
        _log.info("未安装 redis-py，队列将降级为同步执行"
                  "（pip install redis 后启用）")
        return None
    try:
        cli = redis.Redis.from_url(
            url, decode_responses=True,
            socket_timeout=2.0, socket_connect_timeout=2.0,
            health_check_interval=30, retry_on_timeout=True,
            max_connections=32)
        cli.ping()
        return cli
    except Exception as e:                              # noqa: BLE001
        _log.info("Redis 连接失败：%s: %s", type(e).__name__, e)
        return None


def client(*, force: bool = False):
    """★ 获取 Redis 客户端（不健康时返回 None）。

    ★ 为什么要"熔断"：Redis 不可达时每次操作都要等 2 秒超时。
      不熔断的话，每个请求都慢 2 秒 —— 比"没有 Redis"糟得多。
    """
    global _client, _fails, _down_until, _disabled_reason

    if is_forced_disabled():
        _disabled_reason = f"{ENV_DISABLE}=1"
        return None

    now = time.time()
    if _client is not None:
        return _client

    # 熔断窗口内直接返回 None（不尝试连接）
    if now < _down_until:
        return None

    with _lock:
        if _client is not None:
            return _client
        if time.time() < _down_until:
            return None
        cli = _connect(redis_url())
        if cli is not None:
            _client, _fails, _down_until = cli, 0, 0.0
            _disabled_reason = ""
            _log.info("Redis 已连接：%s", _mask_url(redis_url()))
            return cli
        _fails += 1
        _down_until = time.time() + RECOVER_SECONDS
        _disabled_reason = f"连接失败 {_fails} 次，{RECOVER_SECONDS}s 内不再重试"
        if force:
            _log.warning("Redis 不可用（%s），本次强制探测", _disabled_reason)
        return None


def _mask_url(u: str) -> str:
    """掩码 URL 里的密码。"""
    import re
    return re.sub(r"://([^:@/]+):([^@/]+)@", r"://\1:***@", u or "")


def mark_fail(exc: Exception | None = None) -> None:
    """★ 标记一次操作失败（触发熔断）。"""
    global _client, _fails, _down_until, _disabled_reason
    with _lock:
        _fails += 1
        if exc is not None:
            _log.warning("Redis 操作失败：%s: %s", type(exc).__name__, exc)
        if _fails >= FAIL_THRESHOLD:
            _client = None
            _down_until = time.time() + RECOVER_SECONDS
            _disabled_reason = (f"连续失败 {_fails} 次，"
                                f"{RECOVER_SECONDS}s 内降级")


def mark_ok() -> None:
    global _fails
    with _lock:
        _fails = 0


def available() -> bool:
    """★ 队列是否可用（业务侧用它决定"排队还是同步执行"）。"""
    if is_forced_disabled():
        return False
    cli = client()
    if cli is None:
        return False
    try:
        cli.ping()
        mark_ok()
        return True
    except Exception as e:                              # noqa: BLE001
        mark_fail(e)
        return False


def reset() -> None:
    """重置状态（测试用 / 运维手动恢复）。"""
    global _client, _fails, _down_until, _disabled_reason
    with _lock:
        _client, _fails, _down_until, _disabled_reason = None, 0, 0.0, ""


# ---------------------------------------------------------------- 安全包装

def safe(fn, *args, default=None, **kw):
    """★ 执行 Redis 操作，任何异常都不冒泡。

    用法：
        n = q.safe(lambda c: c.zcard(q.K_QUEUE), default=0)

    ★ 为什么所有操作都要过它：队列是**旁路能力**，
      Redis 抖动不该让"生成小说"失败。
    """
    cli = client()
    if cli is None:
        return default
    try:
        r = fn(cli, *args, **kw)
        mark_ok()
        return r
    except Exception as e:                              # noqa: BLE001
        mark_fail(e)
        return default


# ---------------------------------------------------------------- 状态

def status() -> dict:
    """队列健康状态（后台展示 / 健康检查用）。"""
    cli = client()
    forced = is_forced_disabled()
    if cli is None:
        return {
            "available": False,
            "mode": "degraded",              # ★ 降级：同步执行
            "reason": _disabled_reason or (
                f"{ENV_DISABLE}=1（已禁用）" if forced else "未连接"),
            "url": _mask_url(redis_url()),
            "fails": _fails,
            "down_until": _down_until,
            "hint": ("队列不可用，任务将**同步执行**（功能正常，仅无排队）。"
                     "检查 Redis 服务：systemctl status redis-server"),
        }
    try:
        info = {
            "available": True,
            "mode": "queue",
            "url": _mask_url(redis_url()),
            "version": (cli.info("server") or {}).get("redis_version", ""),
            "used_memory_human": (cli.info("memory") or {}).get(
                "used_memory_human", ""),
            "aof_enabled": bool((cli.info("persistence") or {}).get(
                "aof_enabled", 0)),
            "queued": int(cli.zcard(K_QUEUE) or 0),
            "running": int(cli.zcard(K_RUNNING) or 0),
            "dlq": int(cli.llen(K_DLQ) or 0),
            "clients": (cli.info("clients") or {}).get("connected_clients", 0),
        }
        mark_ok()
        return info
    except Exception as e:                              # noqa: BLE001
        mark_fail(e)
        return {"available": False, "mode": "degraded",
                "reason": f"{type(e).__name__}: {e}",
                "url": _mask_url(redis_url()), "fails": _fails,
                "hint": "Redis 探测异常，任务将同步执行"}


def self_check() -> dict:
    """★ 启动自检（只报状态，不改动）。"""
    st = status()
    if st["available"]:
        _log.info("任务队列就绪：Redis %s（AOF %s，排队 %d，执行中 %d）",
                  st.get("version", "?"),
                  "开" if st.get("aof_enabled") else "★关（建议开启）",
                  st.get("queued", 0), st.get("running", 0))
        if not st.get("aof_enabled"):
            _log.warning("★ Redis 未开 AOF：重启会丢失队列数据"
                         "（任务可从 DB 重建，但会有延迟）")
    else:
        _log.info("任务队列降级：%s（任务将同步执行，功能不受影响）",
                  st.get("reason", ""))
    return st


# ---------------------------------------------------------------- J0-2 重建

def rebuild_from_db(db, *, limit: int = 5000, dry_run: bool = False) -> dict:
    """★★ 从 DB 回灌任务到 Redis（J0-2 核心）。

    ★ 为什么这是**必须**的：
      Redis 只是"调度器"，DB 才是**唯一真相源**。
      Redis 重启/数据丢失后，DB 里那些 `queued` / `running` 的任务
      会永远卡住 —— 用户看到"排队中"但永远不会开始。

    ★ 重建策略：
      · `queued`   → 重新入队（按原优先级）
      · `running`  → ★ 重置为 `queued`（因为执行进程已随 Redis 一起死了，
                     原 worker 不会再来认领）
      · 超过 `STALE_HOURS` 的僵死任务 → 标记失败（不无限重试）

    ★ 状态值对齐：Task.status 原为 `running|done|error|abandoned`，
      本版新增 `queued`（已入队未执行）。为兼容旧数据，
      `abandoned` 也纳入重建范围（服务重启遗留的"孤儿"任务）。
    """
    import datetime as dt

    STALE_HOURS = 6
    stats = {"scanned": 0, "requeued": 0, "reset": 0, "stale": 0,
             "skipped": 0, "dry_run": bool(dry_run)}

    cli = client()
    if cli is None:
        return {**stats, "ok": False,
                "reason": "Redis 不可用，无法回灌（任务保持 DB 状态，"
                          "恢复后自动生效）"}

    try:
        from sqlalchemy import select as _sel
        from apps.api.models import Task
    except Exception as e:                              # noqa: BLE001
        return {**stats, "ok": False, "reason": f"导入 Task 失败：{e}"}

    cutoff = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - \
        dt.timedelta(hours=STALE_HOURS)

    try:
        rows = list(db.scalars(
            _sel(Task).where(Task.status.in_(("queued", "running", "abandoned")))
            .order_by(Task.started_at.asc()).limit(limit)).all())
    except Exception as e:                              # noqa: BLE001
        return {**stats, "ok": False, "reason": f"查询任务失败：{e}"}

    for t in rows:
        stats["scanned"] += 1
        created = getattr(t, "started_at", None) or getattr(t, "enqueued_at", None)
        # ★ 僵死任务：不是"重新排队"，而是"判失败"
        #   （否则一个坏任务会被无限重试，永远占着队列）
        if created is not None and created < cutoff:
            stats["stale"] += 1
            if not dry_run:
                try:
                    t.status = "error"
                    t.error = (f"任务僵死超过 {STALE_HOURS} 小时，"
                               "已在启动重建时标记失败")
                except Exception:                       # noqa: BLE001
                    pass
            continue

        # running / abandoned 都已无 worker 认领 → 重置为 queued
        if t.status in ("running", "abandoned"):
            stats["reset"] += 1
            if not dry_run:
                try:
                    t.status = "queued"
                    t.worker_id = ""
                except Exception:                       # noqa: BLE001
                    pass
        else:
            stats["requeued"] += 1

        if dry_run:
            continue
        try:
            enqueue(db, task_id=t.id, kind=getattr(t, "kind", ""),
                    priority=getattr(t, "priority", 0) or 0,
                    lane=getattr(t, "lane", "standard") or "standard",
                    user_id=getattr(t, "owner_id", "") or "",
                    book_id=getattr(t, "book_id", ""), _rebuilt=True)
        except Exception:                               # noqa: BLE001
            stats["skipped"] += 1

    if not dry_run and stats["scanned"]:
        try:
            db.commit()
        except Exception as e:                          # noqa: BLE001
            _log.warning("重建提交失败：%s", e)
            try:
                db.rollback()
            except Exception:                           # noqa: BLE001
                pass

    result = {
        "ok": True, **stats,
        "reason": (f"扫描 {stats['scanned']} 个未完成任务；"
                   f"重新入队 {stats['requeued']}，"
                   f"重置 running {stats['reset']}，"
                   f"判僵死失败 {stats['stale']}"),
    }
    _log.info("队列重建完成：%s", result["reason"])
    return result


# ---------------------------------------------------------------- 队列原语

# ★ 优先级分档（J3 三级泳道的基础，此处先留结构）
LANES = ("fast", "standard", "batch")

# ★★ v0.25.7 J3：加权轮询（6:3:1）—— 从"固定优先级"改为"配额偏移"
#
# ★ 为什么不能只靠固定权重（原实现 weight=0/1e6/2e6）：
#   那等于"fast 绝对优先" —— 只要 fast 队列非空，standard 永远排不上。
#   真实场景：批量任务持续入 batch 时会饿死，或 fast 永远抢在 standard 前面
#   导致 standard（写正文，最重要）被大量快任务插队。
#
# ★ 做法：**按配额比例换算成时间偏移**
#   配比 6:3:1 → 权重差按比例放大到"秒"量级
#     fast     = 0
#     standard = ROUND      （3/6 的比例 → 等 ROUND 秒就能追平）
#     batch    = ROUND * 3  （1/6 的比例 → 等 3 倍时间）
#   这样：
#     · 空载时 fast 仍优先（偏移小）
#     · fast 持续有任务时，standard 等 ROUND 秒后会被"晋升"上来
#       → 实际出队比例接近 6:3:1
#
# ★ ROUND 取值：太小则配比不明显，太大则 standard 响应变慢。
#   取 8 秒 —— 既是"用户能接受的最长插队等待"，也让配比在
#   几秒内就能体现出来。
LANE_ROUND_SECONDS = 8
LANE_WEIGHT = {"fast": 0.0,
               "standard": float(LANE_ROUND_SECONDS),
               "batch": float(LANE_ROUND_SECONDS * 3)}
# ★ 兼容旧引用（部分代码/测试可能读这个常量）
LANE_WEIGHT_LEGACY = {"fast": 0, "standard": 1_000_000, "batch": 2_000_000}


def score_of(priority: int = 0, lane: str = "standard",
             ts: float | None = None) -> float:
    """★ 计算排序分（**越小越先出队**）。

    ★ 为什么这么设计：
      · 泳道偏移占低位（秒级）→ 空载时 fast 优先，但不绝对垄断
      · 泳道内按 `ts - priority` 排序：priority 越大越靠前
      · ts 是入队时间戳（秒），保证**同优先级 FIFO**，且等待越久越靠前
        —— 这天然实现了"防饥饿"（J4），无需额外机制
      · ★ 与泳道偏移叠加后，等待足够久的低优先泳道任务会**自然晋升**
        → 实现 6:3:1 加权轮询（J3）

    ★ 关键：偏移量从 1e6 降到 8 秒 —— 这是 J3 的实质改动。
      原来 1e6 秒（11.5 天）等于"永远轮不到"，现在 8 秒即可追平。
    """
    weight = LANE_WEIGHT.get(lane, LANE_WEIGHT["standard"])
    t = ts if ts is not None else time.time()
    # priority 范围 -1000~1000 → 换算成最多 1000 秒的时间提前量
    return weight + (t - max(-1000, min(1000, int(priority or 0))))


def enqueue(db, *, task_id: str, kind: str = "", priority: int = 0,
            lane: str = "standard", user_id: str = "", book_id: str = "",
            payload: dict | None = None, _rebuilt: bool = False) -> dict:
    """★ 入队（幂等 —— 同一 task_id 重复入队只生效一次）。"""
    l = lane if lane in LANES else "standard"
    cli = client()
    if cli is None:
        return {"ok": False, "queued": False, "degraded": True,
                "reason": "队列不可用，请同步执行"}

    data = {
        "task_id": task_id, "kind": kind, "priority": int(priority or 0),
        "lane": l, "user_id": user_id or "", "book_id": book_id or "",
        "payload": payload or {}, "rebuilt": bool(_rebuilt),
        "enqueued_at": time.time(),
    }
    try:
        pipe = cli.pipeline()
        # NX：只在 member 不存在时写入（幂等）
        pipe.zadd(K_QUEUE, {task_id: score_of(priority, l)}, nx=True)
        pipe.hset(K_TASK, task_id, json.dumps(data, ensure_ascii=False))
        pipe.hincrby(K_STAT, "enqueued", 1)
        pipe.execute()
        mark_ok()
        return {"ok": True, "queued": True, "task_id": task_id,
                "lane": l, "score": score_of(priority, l)}
    except Exception as e:                              # noqa: BLE001
        mark_fail(e)
        return {"ok": False, "queued": False, "degraded": True,
                "reason": f"入队失败：{type(e).__name__}"}


# ★★ v0.25.6 J5：原子出队脚本（并发控制 + 配额定在 Redis 侧判定）
#
# ★ 为什么必须用 Lua 而不是"读计数→判断→出队"：
#   三个 worker 同时检查"当前 7/8 < 8" → 都通过 → 变成 11 个并发。
#   Redis 单线程执行 Lua 才是真原子。
#
# ★ 为什么并发检查要在**出队时**而不是入队时：
#   入队时检查只能拒绝"提交"，无法限制"同时在跑几个"。
#   出队是真正的准入关口。
_LUA_DEQUEUE = """
local q = KEYS[1]          -- 待执行 ZSET
local running = KEYS[2]    -- 执行中 ZSET
local taskh = KEYS[3]      -- task_id -> JSON
local userrun = KEYS[4]    -- user_id -> 计数
local bookrun = KEYS[5]    -- book_id -> 计数
local stat = KEYS[6]       -- 计数器

local max_global = tonumber(ARGV[1])
local max_user = tonumber(ARGV[2])
local max_book = tonumber(ARGV[3])
local worker = ARGV[4]
local now = tonumber(ARGV[5])
local scan_limit = tonumber(ARGV[6])

-- 全局并发闸门
local global_n = redis.call('ZCARD', running)
if global_n >= max_global then
  return {'GLOBAL_FULL', tostring(global_n)}
end

-- ★ 扫前 N 个候选，找第一个"用户/书未超限"的
--   为什么扫描而不是直接取队首：队首可能属于已满额的用户，
--   直接跳过会让它一直占位（后面的任务永远出不来）。
local cand = redis.call('ZRANGE', q, 0, scan_limit - 1, 'WITHSCORES')
local i = 1
while i < #cand do
  local tid = cand[i]
  local sc = cand[i + 1]
  local raw = redis.call('HGET', taskh, tid)
  if raw then
    local ok, d = pcall(cjson.decode, raw)
    if ok then
      local uid = d['user_id'] or ''
      local bid = d['book_id'] or ''
      local u_ok = true
      local b_ok = true
      if uid ~= '' and max_user > 0 then
        local un = tonumber(redis.call('HGET', userrun, uid) or '0')
        u_ok = (un < max_user)
      end
      if bid ~= '' and max_book > 0 then
        local bn = tonumber(redis.call('HGET', bookrun, bid) or '0')
        b_ok = (bn < max_book)
      end
      if u_ok and b_ok then
        -- ★ 命中：取出并占位（全部在同一个原子脚本里）
        redis.call('ZREM', q, tid)
        redis.call('ZADD', running, now, tid)
        if uid ~= '' then
          redis.call('HINCRBY', userrun, uid, 1)
        end
        if bid ~= '' then
          redis.call('HINCRBY', bookrun, bid, 1)
        end
        redis.call('HINCRBY', stat, 'dequeued', 1)
        return {'OK', tid, sc, raw}
      end
    end
  else
    -- 状态丢失的孤儿 → 清掉（防止卡住队列）
    redis.call('ZREM', q, tid)
  end
  i = i + 2
end

-- ★ 队列非空但全都卡在限额上 → 明确区分（便于前端给准确提示）
if redis.call('ZCARD', q) > 0 then
  return {'THROTTLED', tostring(global_n)}
end
return {'EMPTY'}
"""


def concurrency_limits(db=None) -> dict:
    """并发上限（配置中心可覆盖，读不到用默认值）。

    ★ 读配置失败不能导致"无限并发" —— 必须回退到**安全默认值**。
    """
    g, u, b = MAX_GLOBAL_CONCURRENCY, MAX_PER_USER, MAX_PER_BOOK
    if db is not None:
        try:
            from apps.api import config_center as cc
            g = int(cc.get_config(db, "queue.max_global", g) or g)
            u = int(cc.get_config(db, "queue.max_per_user", u) or u)
            b = int(cc.get_config(db, "queue.max_per_book", b) or b)
        except Exception:                               # noqa: BLE001
            pass
    return {"global": max(0, g), "per_user": max(0, u), "per_book": max(0, b)}


def dequeue(*, worker_id: str = "", db=None,
            scan_limit: int = 50) -> dict | None:
    """★ 出队（原子：并发检查 + 配额检查 + 取任务一步完成）。

    ★ 为什么用 ZPOPMIN 不够：
      它只能保证"取一个"原子，不能保证"并发数没超"。
      多 worker 会各自取走一个 → 总并发失控。
      所以整段逻辑（检查 + 取 + 计数）必须在同一个 Lua 里。

    ★ scan_limit：最多扫前 N 个候选。
      太小 → 队首全满额时会误判"无任务可跑"；
      太大 → 单次脚本执行变长（阻塞 Redis）。
      50 是折中（正常并发下前几个就能命中）。
    """
    cli = client()
    if cli is None:
        return None
    lim = concurrency_limits(db)
    try:
        res = cli.eval(
            _LUA_DEQUEUE, 6,
            K_QUEUE, K_RUNNING, K_TASK, K_USER_RUN, K_BOOK_RUN, K_STAT,
            lim["global"], lim["per_user"], lim["per_book"],
            worker_id or "", time.time(), int(scan_limit))
        mark_ok()
    except Exception as e:                              # noqa: BLE001
        # ★ 脚本不支持（老 Redis）→ 退回非原子的 ZPOPMIN（功能可用优先）
        _log.warning("Lua 出队失败，退回简单出队：%s", e)
        try:
            r = cli.zpopmin(K_QUEUE, 1)
            if not r:
                return None
            tid, sc = r[0]
            raw = cli.hget(K_TASK, tid)
            d = json.loads(raw) if raw else {"task_id": tid}
            cli.zadd(K_RUNNING, {tid: time.time()})
            cli.hincrby(K_STAT, "dequeued", 1)
            d["score"] = sc
            d["worker_id"] = worker_id
            d["started_at"] = time.time()
            d["_degraded"] = True
            return d
        except Exception as e2:                         # noqa: BLE001
            mark_fail(e2)
            return None

    if not res:
        return None
    code = res[0]
    if code == "EMPTY":
        return None
    if code == "GLOBAL_FULL":
        return {"_blocked": True, "reason": "global_full",
                "current": int(res[1]), "limit": lim["global"],
                "text": f"平台并发已满（{res[1]}/{lim['global']}），请稍后重试"}
    if code == "THROTTLED":
        # ★ 队列有任务但都被限额挡住 —— 与"全局满"不同：
        #   全局还有余量，只是队首那些任务所属用户/书满了
        return {"_blocked": True, "reason": "throttled",
                "current": int(res[1]), "limit": lim["global"],
                "text": "队列中任务的用户或书籍已达并发上限，稍后会自动放行"}

    task_id, sc, raw = res[1], res[2], res[3]
    data = json.loads(raw) if raw else {"task_id": task_id}
    data["score"] = float(sc)
    data["worker_id"] = worker_id
    data["started_at"] = time.time()
    return data


def release(task_id: str) -> None:
    """释放并发计数（★ 内部用，正常路径由 finish/requeue 调用）。

    ★ 为什么单独抽出来：任务从 running 移除时必须**同时**减计数，
      漏了会导致计数只增不减 → 用户被永久锁死（再也排不上）。
    """
    cli = client()
    if cli is None:
        return
    try:
        raw = cli.hget(K_TASK, task_id)
        if raw:
            d = json.loads(raw)
            uid = d.get("user_id") or ""
            bid = d.get("book_id") or ""
            if uid:
                n = cli.hincrby(K_USER_RUN, uid, -1)
                if n is not None and int(n) <= 0:
                    cli.hdel(K_USER_RUN, uid)           # ★ 归零就删（防 HASH 膨胀）
            if bid:
                n2 = cli.hincrby(K_BOOK_RUN, bid, -1)
                if n2 is not None and int(n2) <= 0:
                    cli.hdel(K_BOOK_RUN, bid)
    except Exception as e:                              # noqa: BLE001
        _log.warning("释放并发计数失败：%s", e)


def finish(task_id: str, *, ok: bool = True) -> bool:
    """任务结束（从 running 移除 + 释放并发计数）。"""
    cli = client()
    if cli is None:
        return False
    try:
        # ★ 顺序很重要：先释放计数（要读 K_TASK），再删状态
        # ★ 顺便记录耗时（J8 的 ETA 靠它估算，比"位置"直观得多）
        try:
            raw = cli.hget(K_TASK, task_id)
            if raw:
                started = float(json.loads(raw).get("started_at") or 0)
                if started > 0:
                    cli.hincrby(K_STAT, "total_ms",
                                int(max(0, time.time() - started) * 1000))
        except Exception:                               # noqa: BLE001
            pass
        release(task_id)
        cli.zrem(K_RUNNING, task_id)
        cli.hdel(K_TASK, task_id)
        cli.hincrby(K_STAT, "done" if ok else "failed", 1)
        mark_ok()
        return True
    except Exception as e:                              # noqa: BLE001
        mark_fail(e)
        return False


def requeue(task_id: str, *, delay: int = 0, priority: int = 0,
            lane: str = "standard") -> bool:
    """★ 重试入队（delay 秒后可见）。

    ★ 为什么用"延迟"而不是立即重入：失败常因瞬时故障（限流/超时），
      立即重试会连续撞墙。延迟能让上游恢复。
    """
    cli = client()
    if cli is None:
        return False
    try:
        # ★ 重试也要释放并发计数 —— 否则重试几次后用户就被自己"锁死"了
        release(task_id)
        cli.zrem(K_RUNNING, task_id)
        score = score_of(priority, lane, ts=time.time() + max(0, int(delay)))
        cli.zadd(K_QUEUE, {task_id: score})
        cli.hincrby(K_STAT, "retried", 1)
        mark_ok()
        return True
    except Exception as e:                              # noqa: BLE001
        mark_fail(e)
        return False


def to_dlq(task_id: str, reason: str = "") -> bool:
    """★ 进死信队列（重试耗尽）。

    ★ 为什么需要死信：坏任务无限重试会一直占队列 + 刷日志。
      进死信后人工介入，且能在后台看到"有哪些任务彻底失败了"。
    """
    cli = client()
    if cli is None:
        return False
    try:
        cli.zrem(K_QUEUE, task_id)
        cli.zrem(K_RUNNING, task_id)
        cli.lpush(K_DLQ, json.dumps(
            {"task_id": task_id, "reason": reason[:500],
             "at": time.time()}, ensure_ascii=False))
        cli.ltrim(K_DLQ, 0, 999)               # 只留最近 1000 条
        cli.hdel(K_TASK, task_id)
        cli.hincrby(K_STAT, "dlq", 1)
        mark_ok()
        return True
    except Exception as e:                              # noqa: BLE001
        mark_fail(e)
        return False


def reap_stale(*, timeout_seconds: int = 1800) -> dict:
    """★ 回收超时任务（worker 崩溃后 running 里的僵尸）。

    ★ 为什么要做：worker 被 OOM kill 时不会执行 finally，
      任务会永远留在 running 里 —— 用户看到"执行中"但永远不结束。
    """
    cli = client()
    if cli is None:
        return {"ok": False, "reason": "Redis 不可用"}
    try:
        now = time.time()
        stale = cli.zrangebyscore(K_RUNNING, 0, now - timeout_seconds)
        for tid in stale:
            # ★★ 僵尸回收必须释放并发计数 —— 这是最容易被漏的一处：
            #   worker 被 OOM kill 时不会走 finish()，计数只增不减，
            #   几次之后该用户的 per_user 配额被"幽灵任务"占满，
            #   表现为"一直排队但永远不开始"（极难排查）。
            release(tid)
            cli.zrem(K_RUNNING, tid)
            cli.zadd(K_QUEUE, {tid: score_of(0, "standard")})
        if stale:
            cli.hincrby(K_STAT, "reaped", len(stale))
        mark_ok()
        return {"ok": True, "reaped": len(stale), "timeout": timeout_seconds}
    except Exception as e:                              # noqa: BLE001
        mark_fail(e)
        return {"ok": False, "reason": str(e)[:200]}


def stats() -> dict:
    """队列统计（管理端看板）。"""
    cli = client()
    if cli is None:
        return {"available": False, "mode": "degraded"}
    try:
        counters = cli.hgetall(K_STAT) or {}
        lanes: dict[str, int] = {}
        for tid, sc in (cli.zrange(K_QUEUE, 0, -1, withscores=True) or []):
            raw = cli.hget(K_TASK, tid)
            lane = "standard"
            if raw:
                try:
                    lane = json.loads(raw).get("lane", "standard")
                except Exception:                       # noqa: BLE001
                    pass
            lanes[lane] = lanes.get(lane, 0) + 1
        # ★ v0.25.6 J5：并发占用（运维要能看到"谁占着位置"）
        lim = concurrency_limits(None)
        running_n = int(cli.zcard(K_RUNNING) or 0)
        try:
            user_run = {k: int(v) for k, v in (cli.hgetall(K_USER_RUN) or {}).items()}
            book_run = {k: int(v) for k, v in (cli.hgetall(K_BOOK_RUN) or {}).items()}
        except Exception:                               # noqa: BLE001
            user_run, book_run = {}, {}
        mark_ok()
        return {
            "available": True, "mode": "queue",
            "queued": int(cli.zcard(K_QUEUE) or 0),
            "running": running_n,
            "dlq": int(cli.llen(K_DLQ) or 0),
            "by_lane": lanes,
            "counters": {k: int(v) for k, v in counters.items()},
            # ★ 并发信息：limit=0 表示不限
            "concurrency": {
                "limit_global": lim["global"], "limit_per_user": lim["per_user"],
                "limit_per_book": lim["per_book"], "in_flight": running_n,
                "saturation": (round(running_n / lim["global"], 3)
                               if lim["global"] > 0 else 0.0),
                "top_users": sorted(user_run.items(), key=lambda kv: -kv[1])[:5],
                "active_books": len(book_run),
            },
        }
    except Exception as e:                              # noqa: BLE001
        mark_fail(e)
        return {"available": False, "mode": "degraded",
                "reason": str(e)[:200]}


def peek(limit: int = 20) -> list[dict]:
    """查看队首（后台调试用）。"""
    cli = client()
    if cli is None:
        return []
    try:
        out = []
        for tid, sc in (cli.zrange(K_QUEUE, 0, limit - 1, withscores=True) or []):
            raw = cli.hget(K_TASK, tid)
            d = json.loads(raw) if raw else {"task_id": tid}
            d["score"] = sc
            out.append(d)
        mark_ok()
        return out
    except Exception as e:                              # noqa: BLE001
        mark_fail(e)
        return []


def clear_dlq() -> int:
    """清空死信（人工确认后调用）。"""
    cli = client()
    if cli is None:
        return 0
    try:
        n = int(cli.llen(K_DLQ) or 0)
        cli.delete(K_DLQ)
        mark_ok()
        return n
    except Exception as e:                              # noqa: BLE001
        mark_fail(e)
        return 0


# ---------------------------------------------------------------- J10 告警

# ★ 告警节流：同一告警 N 秒内只发一次
#   为什么必须节流：队列积压会持续触发告警，不节流会刷爆通知
#   （用户被刷烦了就会关掉通知 → 真出事时反而看不到）
_alert_last: dict[str, float] = {}
_ALERT_COOLDOWN = 1800          # 30 分钟


def _throttled(key: str, cooldown: int = _ALERT_COOLDOWN) -> bool:
    """是否处于节流窗口内（True = 应跳过）。"""
    now = time.time()
    last = _alert_last.get(key, 0)
    if now - last < cooldown:
        return True
    _alert_last[key] = now
    return False


def check_alerts(db=None) -> dict:
    """★ 队列健康检查 + 告警（J10）。

    ★ 检查项：
      · 队列积压超阈值（默认 100）
      · 失败率超阈值（默认 30%）
      · 并发饱和（>=90%）
      · 死信队列非空（有任务彻底失败，需要人看）

    ★ 为什么失败率要算"窗口内"而不是累计：
      累计失败率永远不会下降 —— 早期几次失败会让它长期高位，失去意义。
    """
    st = stats()
    if not st.get("available"):
        return {"ok": False, "reason": "队列不可用", "alerts": []}

    limits = {"depth": 100, "fail_rate": 30}
    if db is not None:
        try:
            from apps.api import config_center as cc
            limits["depth"] = int(cc.get_config(db, "queue.alert_depth", 100) or 100)
            limits["fail_rate"] = int(cc.get_config(
                db, "queue.alert_fail_rate", 30) or 30)
        except Exception:                               # noqa: BLE001
            pass

    alerts: list[dict] = []
    queued = int(st.get("queued") or 0)
    dlq = int(st.get("dlq") or 0)
    counters = st.get("counters") or {}
    done = int(counters.get("done") or 0)
    failed = int(counters.get("failed") or 0)
    total = done + failed
    fail_rate = (failed * 100.0 / total) if total else 0.0
    conc = st.get("concurrency") or {}
    sat = float(conc.get("saturation") or 0)

    if queued >= limits["depth"]:
        alerts.append({
            "key": "queue_depth", "level": "warning",
            "title": "队列积压",
            "message": f"待执行任务 {queued} 个（阈值 {limits['depth']}）",
            "suggest": "检查 worker 是否在跑；若持续增长需扩容或降低生成频率",
        })
    if total >= 20 and fail_rate >= limits["fail_rate"]:
        alerts.append({
            "key": "fail_rate", "level": "error",
            "title": "任务失败率偏高",
            "message": f"失败率 {fail_rate:.1f}%（{failed}/{total}，阈值 {limits['fail_rate']}%）",
            "suggest": "查看死信原因，优先排查最高频失败项",
        })
    if sat >= 0.9:
        alerts.append({
            "key": "saturation", "level": "warning",
            "title": "并发接近饱和",
            "message": f"当前 {conc.get('in_flight')}/{conc.get('limit_global')}"
                       f"（{sat * 100:.0f}%）",
            "suggest": "考虑上调 queue.max_global 或扩容机器",
        })
    if dlq > 0:
        alerts.append({
            "key": "dlq", "level": "error",
            "title": "死信队列非空",
            "message": f"{dlq} 个任务已彻底失败",
            "suggest": "需人工介入：查看失败原因，修复后重新入队或作废",
        })

    # ★ 发通知（节流；且失败不影响返回 —— 告警是旁路）
    sent = 0
    if db is not None and alerts:
        for a in alerts:
            if _throttled(f"queue:{a['key']}"):
                continue
            try:
                from apps.api import notify as _nf
                _nf.notify_admin(db, event="queue.alert",
                                 ctx={"title": a["title"], "message": a["message"],
                                      "suggest": a["suggest"]},
                                 dedup_key=f"qalert:{a['key']}")
                sent += 1
            except Exception as e:                      # noqa: BLE001
                _log.warning("队列告警通知失败（不影响检查）：%s", e)

    return {"ok": True, "alerts": alerts, "alert_count": len(alerts),
            "notified": sent, "limits": limits,
            "metrics": {"queued": queued, "dlq": dlq,
                        "fail_rate": round(fail_rate, 2),
                        "saturation": sat}}


def alert_state_reset() -> None:
    """清空告警节流状态（测试用）。"""
    _alert_last.clear()


# ---------------------------------------------------------------- J8 排队信息

def queue_position(task_id: str) -> dict:
    """★ 查任务在队列中的位置与 ETA（J8 用户端排队 UI 的数据源）。

    ★ 为什么要给 ETA 而不是只给位置：
      用户最关心的是"还要等多久"。只给"你是第 5 位"，
      用户还是要自己猜（而且猜不准，因为不知道前面任务多大）。
      ETA 用**平均耗时 × 前方任务数**估算，比位置直观得多。

    ★ 为什么 ETA 只能"估算"：
      生成耗时与章节长度、模型速度都相关，无法精确预测。
      所以给范围（如"约 3-6 分钟"），并明确标注是估算。
    """
    cli = client()
    if cli is None:
        return {"ok": False, "reason": "队列不可用（可能直接执行）"}
    try:
        rank = cli.zrank(K_QUEUE, task_id)
        running_n = int(cli.zcard(K_RUNNING) or 0)
        counters = cli.hgetall(K_STAT) or {}
        done = int(counters.get("done") or 0) + int(counters.get("failed") or 0)
        if rank is None:
            # 不在队列里 → 可能在执行中，或已完成
            in_run = cli.zscore(K_RUNNING, task_id) is not None
            return {"ok": True, "state": "running" if in_run else "unknown",
                    "position": 0, "ahead": 0,
                    "text": "正在生成…" if in_run else "任务不在队列中"}
        ahead = int(rank)
        # ★ 用历史均值估 ETA（无历史时给保守值 90s）
        avg = _avg_duration_seconds(cli)
        eta_lo = max(5, int(ahead * avg * 0.7))
        eta_hi = max(10, int(ahead * avg * 1.4))
        return {
            "ok": True, "state": "queued",
            "position": ahead + 1, "ahead": ahead,
            "running": running_n,
            "avg_seconds": round(avg, 1),
            "eta_seconds": [eta_lo, eta_hi],
            "eta_text": _fmt_eta(eta_lo, eta_hi),
            "done_total": done,
        }
    except Exception as e:                              # noqa: BLE001
        mark_fail(e)
        return {"ok": False, "reason": f"查询失败：{type(e).__name__}"}


def _avg_duration_seconds(cli) -> float:
    """平均任务耗时（★ 从累计计数估算，读不到给保守默认值）。

    ★ 为什么不用精确统计：那要额外存每个任务的耗时（写放大）。
      队列场景用累计均值足够 —— ETA 本来就是估算。
    """
    try:
        c = cli.hgetall(K_STAT) or {}
        total_ms = float(c.get("total_ms") or 0)
        done = float(c.get("done") or 0)
        if done >= 3 and total_ms > 0:
            return max(5.0, total_ms / done / 1000.0)
    except Exception:                                   # noqa: BLE001
        pass
    return 90.0                                         # 保守默认（1.5 分钟）


def _fmt_eta(lo: int, hi: int) -> str:
    def _u(s: int) -> str:
        if s < 60:
            return f"{s} 秒"
        m = s // 60
        return f"{m} 分钟" if m < 60 else f"{m // 60} 小时 {(m % 60)} 分"
    return f"约 {_u(lo)} ~ {_u(hi)}"


# ---------------------------------------------------------------- J11 全链路接入

# ★★ 准入检查脚本：判断"能否**立刻**执行"，能则占位。
#
# ★ 为什么需要它（而不是直接 enqueue + dequeue）：
#   HTTP 请求需要在**毫秒级**决定"是立刻开跑，还是告诉前端排队"。
#   走完整 enqueue→dequeue 流程会留下一个"没人认领"的任务（因为
#   请求线程不会去跑 worker），导致任务永远挂在队列里。
#
# ★ 语义：这是"轻量准入"，只做占位，不取任务。
_LUA_ADMIT = """
local running = KEYS[1]
local userrun = KEYS[2]
local bookrun = KEYS[3]

local max_global = tonumber(ARGV[1])
local max_user = tonumber(ARGV[2])
local max_book = tonumber(ARGV[3])
local uid = ARGV[4]
local bid = ARGV[5]
local token = ARGV[6]

local g = redis.call('ZCARD', running)
if max_global > 0 and g >= max_global then
  return {'FULL', tostring(g), tostring(max_global)}
end
if uid ~= '' and max_user > 0 then
  local un = tonumber(redis.call('HGET', userrun, uid) or '0')
  if un >= max_user then
    return {'USER_FULL', tostring(un), tostring(max_user)}
  end
end
if bid ~= '' and max_book > 0 then
  local bn = tonumber(redis.call('HGET', bookrun, bid) or '0')
  if bn >= max_book then
    return {'BOOK_FULL', tostring(bn), tostring(max_book)}
  end
end

-- ★ 占位：用 token 当 member（不占真实 task_id，避免与 worker 队列混淆）
redis.call('ZADD', running, tonumber(ARGV[7]), token)
if uid ~= '' then redis.call('HINCRBY', userrun, uid, 1) end
if bid ~= '' then redis.call('HINCRBY', bookrun, bid, 1) end
return {'OK', token}
"""


def admit(db, *, token: str = "", user_id: str = "", book_id: str = "") -> dict:
    """★ 准入检查（J11）—— 判断能否**立刻**执行，能则原子占位。

    返回 {allowed: bool, token: str, reason: str, text: str, limits: {...}}

    ★ 为什么"占位"和"准入"必须在同一个 Lua 里：
      否则是个 TOCTOU（检查时可用、使用时已被占）漏洞 ——
      10 个并发请求都看到"还有 1 个空位"，全部通过。

    ★ 为什么用 token 而非 task_id 占位：
      此时业务任务还没建（要先知道能不能跑）。用随机 token，
      执行结束调 `release_token()` 释放即可。
    """
    lim = concurrency_limits(db)
    cli = client()
    tk = token or f"adm_{os.urandom(8).hex()}"
    if cli is None:
        # ★ 队列不可用 → 放行（降级为同步执行，行为确定）
        return {"allowed": True, "token": "", "degraded": True,
                "reason": "queue_unavailable", "limits": lim,
                "text": "队列不可用，已降级为直接执行"}
    try:
        res = cli.eval(_LUA_ADMIT, 3, K_RUNNING, K_USER_RUN, K_BOOK_RUN,
                       lim["global"], lim["per_user"], lim["per_book"],
                       user_id or "", book_id or "", tk, time.time())
        mark_ok()
    except Exception as e:                              # noqa: BLE001
        _log.warning("准入检查失败，降级放行：%s", e)
        mark_fail(e)
        return {"allowed": True, "token": "", "degraded": True,
                "reason": "admit_error", "limits": lim,
                "text": "队列检查异常，已降级为直接执行"}

    code = res[0] if res else "OK"
    if code == "OK":
        return {"allowed": True, "token": tk, "limits": lim,
                "reason": "", "text": ""}

    if code == "FULL":
        cur, cap = res[1], res[2]
        return {"allowed": False, "token": "", "reason": "global_full",
                "limits": lim, "current": int(cur), "cap": int(cap),
                "text": f"平台忙碌中（{cur}/{cap} 个任务在跑），"
                        "已为你排队，稍后自动开始"}
    if code == "USER_FULL":
        return {"allowed": False, "token": "", "reason": "user_full",
                "limits": lim, "current": int(res[1]), "cap": int(res[2]),
                "text": f"你已有 {res[1]} 个任务在跑（上限 {res[2]}），"
                        "新任务将排队等待"}
    if code == "BOOK_FULL":
        return {"allowed": False, "token": "", "reason": "book_full",
                "limits": lim, "current": int(res[1]), "cap": int(res[2]),
                "text": "这本书还有任务在跑（同书串行，保证章节顺序），"
                        "已为你排队"}
    return {"allowed": True, "token": tk, "limits": lim, "reason": "",
            "text": ""}


def release_token(token: str, *, user_id: str = "", book_id: str = "") -> None:
    """释放准入占位（★ 与 `admit()` 配对，必须在 finally 里调）。

    ★ 为什么要显式传 user_id/book_id：
      占位时用的是随机 token（还没业务任务），
      无法从 task 状态反查归属 → 调用方必须把归属一起传回来。
    """
    if not token:
        return
    cli = client()
    if cli is None:
        return
    try:
        cli.zrem(K_RUNNING, token)
        if user_id:
            n = cli.hincrby(K_USER_RUN, user_id, -1)
            if n is not None and int(n) <= 0:
                cli.hdel(K_USER_RUN, user_id)
        if book_id:
            n2 = cli.hincrby(K_BOOK_RUN, book_id, -1)
            if n2 is not None and int(n2) <= 0:
                cli.hdel(K_BOOK_RUN, book_id)
    except Exception as e:                              # noqa: BLE001
        _log.warning("释放准入占位失败：%s", e)


def lane_of(task_kind: str = "") -> str:
    """按任务类型分泳道（J3/J11）。

    ★ 为什么用类型而不是"用户等级"：
      泳道描述的是**任务本身的轻重**（快问快答 vs 长文档生成），
      与谁发起无关。用用户等级会让同一个快任务因"免费用户"被拖慢，
      体验损失大而收益小。
    """
    k = (task_kind or "").lower()
    if k in ("title", "tag", "summary_short", "classify", "ping"):
        return "fast"
    if k in ("audit_book", "batch", "draft_batch", "drama_batch"):
        return "batch"
    return "standard"


def priority_of(task_kind: str = "", *, user_priority: int = 0) -> int:
    """任务优先级（泳道内排序用）。

    ★ 给"用户显式指定"留口子（user_priority），但**限制范围** ——
      否则用户填 99999 就能插队到所有人前面（不公平）。
    """
    base = {"fast": 50, "standard": 0, "batch": -20}.get(lane_of(task_kind), 0)
    return max(-1000, min(1000, base + int(user_priority or 0)))

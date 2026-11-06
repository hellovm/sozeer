"""限流与风控（v0.20.6 A6）。

★★★ 为什么必须有这一层：
  没有限流 = 攻击者可以：
    · 暴力破解密码（字典攻击，每秒几千次尝试）
    · 短信轰炸（刷验证码接口，一条短信几毛钱，能刷到你破产）
    · 撞库（拿泄露的账号密码批量试）
    · 刷生成接口（一次生成烧几毛钱模型费，跑一夜能烧到四位数）
  这些**不是理论风险** —— 是每个上线产品都会遇到的日常攻击。

================================ 设计取舍 ================================

★ 为什么用**进程内存**而不是 Redis（v0.20.6 原始决策）：
  当时是单 worker 部署（systemd 里 --workers 1），内存计数足够准确，
  且**零依赖、零延迟**。
  ⚠️ 限制：**多 worker 下每个 worker 各算各的**，
    实际额度 = 配置值 × worker 数。上多 worker 前必须切 Redis。

★★ v0.25.2：**已切换为 Redis 优先 + 内存兜底**（本文件下半部分）
  · Redis 可用 → 用 Redis（多 worker 下计数正确、跨进程共享）
  · Redis 不可用 → 自动降级内存（可用性优先，且**不报错**）
  · 开关：`NF_RATELIMIT_BACKEND=redis|memory|auto`（默认 auto）

★ 为什么用**滑动窗口**而不是固定窗口：
  固定窗口有边界突刺问题（59秒打满 + 1秒再打满 = 2 倍流量）。
  滑动窗口记录每次请求时间戳，精度更高。

★ 为什么限流要**失败开放**（fail-open）：
  限流器自身出错时应该放行而不是全站 503 ——
  可用性优先于防护（宁可被刷，不可让所有用户用不了）。
  但要在日志里 error 级别记录，便于发现限流器故障。

================================ 三层限流 ================================

  第 1 层：中间件全局限流（按 IP，所有请求）
  第 2 层：敏感端点限流（登录/注册/验证码，更严格）
  第 3 层：风控（同 IP 多账号、短时高频，行为分析）
"""
from __future__ import annotations

import ipaddress
import logging
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

log = logging.getLogger("novelforge")

ENV_BACKEND = "NF_RATELIMIT_BACKEND"      # redis | memory | auto（默认 auto）

# ★★ v1.0-E4.10：限流总开关（测试/冒烟/压测用）
#
#   ★ 为什么需要：全端点冒烟要发 1600+ 请求，同一账号必然触发
#     `gen:{user_id}` 限流 → 大量端点被 429 拦掉
#     → **冒烟产生假阴性**（看起来通过，实际没测）。
#
#   ★★ 判据：**压测/冒烟工具必须能绕开业务限流** ——
#     否则测的不是"端点是否正确"，而是"限流是否符合预期"。
#
#   ★ 安全：默认**不关闭**；只有显式设 `NF_RATELIMIT_OFF=1` 才关闭，
#     且启动时打 warning（见 main.py）。
ENV_OFF = "NF_RATELIMIT_OFF"
_RATELIMIT_OFF = (os.environ.get(ENV_OFF) or "").strip().lower() in (
    "1", "true", "on", "yes")
K_RL = "sozeer:rl"                        # Redis 键前缀


# ================================================================ 限流规则

@dataclass(frozen=True)
class Limit:
    """一条限流规则。

    window: 窗口秒数；max_hits: 窗口内允许的最大次数
    """
    window: int
    max_hits: int
    name: str = ""

    def describe(self) -> str:
        return f"{self.max_hits} 次 / {self.window} 秒"


# ★ 敏感端点规则（严格）
SENSITIVE_RULES: dict[str, Limit] = {
    # 登录：防暴力破解。10 次/5 分钟 —— 正常人不会连续输错 10 次
    "/api/auth/login": Limit(300, 10, "登录"),
    "/api/auth/register": Limit(3600, 5, "注册"),          # 5 次/小时
    "/api/auth/send-code": Limit(300, 5, "发送验证码"),      # ★ 防短信轰炸
    "/api/auth/login-by-code": Limit(300, 10, "验证码登录"),
    "/api/auth/reset-password": Limit(3600, 5, "重置密码"),
    # 支付：防刷单探测
    "/api/pay/create": Limit(60, 20, "下单"),
    # 用户中心敏感操作
    "/api/me/password": Limit(3600, 5, "修改密码"),
    "/api/me/phone": Limit(3600, 5, "换绑手机"),
}

# ★ 生成类规则（前缀匹配，耗钱接口）
GENERATE_PREFIXES: tuple[tuple[str, Limit], ...] = (
    ("/api/books/", None),          # 占位，见下方 _match_generate
)
# 生成相关路径关键词（命中即限流）
GENERATE_KEYWORDS = ("/draft", "/generate", "/audit", "/polish", "/drama/run",
                     "/batch", "/chat", "/continue", "/rewrite")
GENERATE_LIMIT = Limit(60, 30, "生成")       # 30 次/分钟（生成很贵，但要够用）

# ★ 全局 IP 限流（兜底）
GLOBAL_IP_LIMIT = Limit(60, 600, "全局")     # 600 次/分钟

# ★ 白名单（内网 / 本机，监控与健康检查用）
IP_WHITELIST = {
    "127.0.0.1", "::1", "localhost",
}


def client_ip(scope_or_headers) -> str:
    """从请求中取真实客户端 IP。

    ★ 优先级（重要）：
      1. CF-Connecting-IP —— Cloudflare 官方头，**不可伪造**（CF 会覆盖）
      2. X-Real-IP —— nginx 经 real_ip 模块写入的真实 IP
      3. 直连地址
    ★ 不用 X-Forwarded-For 的第一段：那是**客户端可伪造**的。
      攻击者伪造 XFF 就能绕过 IP 限流（经典漏洞）。
    """
    h = getattr(scope_or_headers, "headers", scope_or_headers)
    try:
        get = h.get if hasattr(h, "get") else (lambda k, d=None: None)
        for k in ("cf-connecting-ip", "CF-Connecting-IP",
                  "x-real-ip", "X-Real-IP"):
            v = get(k) if hasattr(h, "get") else None
            if v:
                return str(v).strip()
    except Exception:
        pass
    return ""


def is_private(ip: str) -> bool:
    """判断是否**本机/内网** IP（这类不限流 —— 监控与健康检查要稳定）。

    ★ 为什么不用 `ipaddress.is_private`：
      它把 RFC 5737 文档段（203.0.113.0/24 等）也算作 private，
      还包含大量保留段。豁免范围过宽意味着**边界模糊**，
      某些代理链能伪造出这类源 IP 从而绕过限流。
      这里只豁免明确的 RFC1918 内网 + 环回 + 链路本地。
    """
    if not ip:
        return True                     # 取不到 IP 时不限流（避免误伤）
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False                    # 非法 IP 视为不可信 → 照常限流
    if a.is_loopback or a.is_link_local:
        return True
    # 仅豁免 RFC1918 / RFC4193（ULA）真正的内网段
    _PRIVATE_NETS = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("fc00::/7"),
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("::1/128"),
    )
    return any(a in n for n in _PRIVATE_NETS)


# ================================================================ 滑动窗口计数器

class SlidingWindow:
    """滑动窗口计数器（线程安全）。

    实现：每个 key 存一个 deque[时间戳]，清理过期项后看长度。
    ★ 内存控制：单 key 最多存 max_hits 个时间戳（超了必然超限，不用再记）。
      这样即使被攻击（海量不同的 IP），单 key 内存也有上界。
    """

    __slots__ = ("_d", "_lock", "_max_keys")

    def __init__(self, max_keys: int = 100_000):
        self._d: dict[str, deque] = {}
        self._lock = threading.Lock()
        # ★ 防内存耗尽：超过上限时清理最旧的 key（攻击者可用海量 IP 撑爆内存）
        self._max_keys = max_keys

    def hit(self, key: str, window: int, max_hits: int, now: float | None = None
            ) -> tuple[bool, int, float]:
        """记一次请求。返回 (是否允许, 剩余次数, 距离最早请求过期的秒数)。"""
        now = now if now is not None else time.time()
        cutoff = now - window
        with self._lock:
            dq = self._d.get(key)
            if dq is None:
                if len(self._d) >= self._max_keys:
                    self._evict()
                dq = deque(maxlen=max_hits + 1)
                self._d[key] = dq
            # 清理过期项
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= max_hits:
                # 超限：计算还要等多久（最早那次过期时）
                retry_after = max(1.0, dq[0] + window - now) if dq else float(window)
                return False, 0, retry_after
            dq.append(now)
            return True, max_hits - len(dq), 0.0

    def count(self, key: str, window: int, now: float | None = None) -> int:
        """只读计数（不记一次）。风控分析用。"""
        now = now if now is not None else time.time()
        cutoff = now - window
        with self._lock:
            dq = self._d.get(key)
            if not dq:
                return 0
            return sum(1 for t in dq if t > cutoff)

    def reset(self, key: str) -> None:
        with self._lock:
            self._d.pop(key, None)

    def _evict(self) -> None:
        """内存满了：清掉一批（简单策略，够用）。"""
        n = max(1, len(self._d) // 10)
        for k in list(self._d.keys())[:n]:
            self._d.pop(k, None)
        log.warning("限流器内存达上限，已清理 %d 个 key", n)

    def size(self) -> int:
        with self._lock:
            return len(self._d)


_store = SlidingWindow()
# 登录失败计数（风控用，独立于限流）
_login_fails = SlidingWindow(max_keys=50_000)


# ================================================================ v0.25.2 Redis 后端
#
# ★★ 为什么必须切 Redis（而不是继续用内存）：
#   多 worker 下每个 worker 各有一份内存计数 → 实际额度 = 配置 × worker 数。
#   3 个 worker 时，"10 次/分钟"的登录限流实际是 30 次 —— 防暴力破解形同虚设。
#
# ★★ 实现要点：**Lua 脚本保证原子性**
#   滑动窗口需要"清理过期 + 计数 + 写入"三步。分开发命令会有竞态：
#   两个 worker 同时读到 count=9（上限 10）→ 都放行 → 实际放了 11 个。
#   Redis 单线程执行 Lua，天然原子。
#
# ★ 键设计：`{prefix}:{key}` —— 用 ZSET 存时间戳（score=时间，member=唯一ID）
#   为什么不只用 counter：固定窗口有边界突刺（见文件头说明）。

# KEYS[1] = 限流键
# ARGV[1] = 当前时间戳（秒，浮点）
# ARGV[2] = 窗口秒数
# ARGV[3] = 最大次数
# ARGV[4] = member 唯一后缀（防止同毫秒多次请求被 ZSET 去重）
# 返回：{allowed(0/1), remaining, retry_after_ms}
_LUA_HIT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local max_hits = tonumber(ARGV[3])
local member = ARGV[4]
local cutoff = now - window

-- ① 清理窗口外的记录
redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
-- ② 计数
local n = redis.call('ZCARD', key)
if n >= max_hits then
  -- 超限：算出最早那条何时过期
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  local retry_ms = window * 1000
  if oldest[2] then
    local t = tonumber(oldest[2])
    retry_ms = math.max(1000, math.ceil((t + window - now) * 1000))
  end
  -- ★ 仍然续期，防止被持续攻击时 key 永不过期（否则窗口会"滑走"）
  redis.call('EXPIRE', key, math.ceil(window) + 1)
  return {0, 0, retry_ms}
end
-- ③ 记录本次
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, math.ceil(window) + 1)
return {1, max_hits - n - 1, 0}
"""

# KEYS[1] = 键, ARGV[1] = 窗口秒数
_LUA_COUNT = """
local cutoff = tonumber(ARGV[1])
local now = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - cutoff)
return redis.call('ZCARD', KEYS[1])
"""


class RedisWindow:
    """Redis 滑动窗口（与 SlidingWindow 同接口）。

    ★ 接口完全一致 —— 这是"接口冻结、实现替换"的落地：
      `check_request` / `record_login_fail` 等调用方一行不用改。
    """

    def __init__(self, prefix: str = "main") -> None:
        self._prefix = f"{K_RL}:{prefix}"
        self._sha_hit: str | None = None
        self._sha_count: str | None = None
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._fallback = SlidingWindow()      # ★ Redis 故障时临时兜底
        self._fallback_hits = 0

    # ---- 内部

    def _key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    def _next_member(self) -> str:
        """★ 唯一 member：时间戳 + 自增序号。

        为什么需要：同一毫秒内多次请求，如果 member 只用时间戳，
        ZSET 会去重（同一 member 只保留一条）→ 计数偏少 → 限流失效。
        """
        with self._seq_lock:
            self._seq = (self._seq + 1) % 1_000_000
            n = self._seq
        return f"{time.time():.6f}-{os.getpid()}-{n}"

    def _cli(self):
        """取 Redis 客户端（复用 queue 模块的连接与熔断）。"""
        try:
            from apps.api import queue as _q
            return _q.client()
        except Exception:                            # noqa: BLE001
            return None

    def _eval(self, script: str, cache_attr: str, keys: list[str],
              args: list) -> list | None:
        """执行 Lua（带 SHA 缓存 + 故障降级）。"""
        cli = self._cli()
        if cli is None:
            return None
        try:
            sha = getattr(self, cache_attr)
            if sha is None:
                sha = cli.script_load(script)
                setattr(self, cache_attr, sha)
            try:
                return cli.evalsha(sha, len(keys), *keys, *args)
            except Exception as e:                   # noqa: BLE001
                # NOSCRIPT：脚本被 Redis 重启清掉了 → 重新加载
                if "NOSCRIPT" in str(e):
                    sha = cli.script_load(script)
                    setattr(self, cache_attr, sha)
                    return cli.evalsha(sha, len(keys), *keys, *args)
                raise
        except Exception as e:                       # noqa: BLE001
            try:
                from apps.api import queue as _q
                _q.mark_fail(e)
            except Exception:                        # noqa: BLE001
                pass
            self._fallback_hits += 1
            return None

    # ---- 对外（与 SlidingWindow 一致）

    def hit(self, key: str, window: int, max_hits: int,
            now: float | None = None) -> tuple[bool, int, float]:
        ts = now if now is not None else time.time()
        r = self._eval(_LUA_HIT, "_sha_hit", [self._key(key)],
                       [f"{ts:.6f}", str(window), str(max_hits),
                        self._next_member()])
        if r is None:
            # ★ Redis 不可用 → 降级内存（可用性优先）
            return self._fallback.hit(key, window, max_hits, now)
        allowed, remaining, retry_ms = int(r[0]), int(r[1]), int(r[2])
        return bool(allowed), remaining, (retry_ms / 1000.0 if retry_ms else 0.0)

    def count(self, key: str, window: int, now: float | None = None) -> int:
        ts = now if now is not None else time.time()
        r = self._eval(_LUA_COUNT, "_sha_count", [self._key(key)],
                       [str(window), f"{ts:.6f}"])
        if r is None:
            return self._fallback.count(key, window, now)
        return int(r)

    def reset(self, key: str) -> None:
        cli = self._cli()
        if cli is None:
            self._fallback.reset(key)
            return
        try:
            cli.delete(self._key(key))
        except Exception:                            # noqa: BLE001
            self._fallback.reset(key)
        self._fallback.reset(key)

    def size(self) -> int:
        """活跃键数（近似 —— SCAN 有成本，只取一批）。

        ★ 为什么用 SCAN 而不是 KEYS：KEYS 会阻塞 Redis 主线程，
          生产环境禁用。SCAN 是游标式，对服务影响可控。
        """
        cli = self._cli()
        if cli is None:
            return self._fallback.size()
        try:
            n = 0
            cursor = 0
            for _ in range(20):                      # 最多扫 20 批，避免长时间占用
                cursor, keys = cli.scan(cursor, match=f"{self._prefix}:*", count=500)
                n += len(keys)
                if cursor == 0:
                    break
            return n
        except Exception:                            # noqa: BLE001
            return self._fallback.size()

    def backend(self) -> str:
        return "redis" if self._cli() is not None else "memory(降级)"


def _pick_backend() -> str:
    """选择后端：环境变量 > auto（有 Redis 用 Redis）。"""
    v = (os.environ.get(ENV_BACKEND) or "auto").strip().lower()
    if v == "memory":
        return "memory"
    if v == "redis":
        return "redis"
    # auto：探测 Redis 可用性
    try:
        from apps.api import queue as _q
        return "redis" if _q.available() else "memory"
    except Exception:                                # noqa: BLE001
        return "memory"


def _build_store():
    if _pick_backend() == "redis":
        return RedisWindow("main")
    return SlidingWindow()


def _build_login_fails():
    if _pick_backend() == "redis":
        return RedisWindow("loginfail")
    return SlidingWindow(max_keys=50_000)


_store = _build_store()
# 登录失败计数（风控用，独立于限流）
_login_fails = _build_login_fails()


def backend() -> str:
    """当前限流后端（`redis` / `memory`）。"""
    b = getattr(_store, "backend", None)
    if callable(b):
        try:
            return b()
        except Exception:                            # noqa: BLE001
            pass
    return "memory"


def set_backend(name: str) -> dict:
    """切换后端（运维用；不重启服务）。

    ★ 为什么需要：Redis 临时故障后，运维可能想强制切内存跑一段时间。
      切回 redis 后计数从零开始（内存与 Redis 的窗口不共享）——
      这是可接受的取舍（限流本来就是"近似"）。
    """
    global _store, _login_fails
    n = (name or "").strip().lower()
    if n not in ("redis", "memory", "auto"):
        return {"ok": False, "error": f"未知后端：{name}"}
    os.environ[ENV_BACKEND] = n
    _store = _build_store()
    _login_fails = _build_login_fails()
    log.info("限流后端已切换为：%s（实际 %s）", n, backend())
    return {"ok": True, "requested": n, "actual": backend()}


# ================================================================ 规则匹配

# ★ v0.25.4 I7-I10：可由配置中心覆盖的规则（键 → 覆盖哪个 Limit）
#   为什么只把这两个放出来：它们是**运营真正需要按业务调**的
#   （大促要放宽生成、被攻击要收紧登录），
#   而 send-code 这类"防短信轰炸"的硬阈值不该被随手改。
CONFIG_OVERRIDES: dict[str, tuple[str, str]] = {
    # 配置键 → (目标路径, 字段)
    "security.ratelimit_login": ("/api/auth/login", "max_hits"),
}


def _configured_limit(path: str, base: Limit) -> Limit:
    """用配置中心的值覆盖规则（读不到就用原值 —— 配置读取永不影响限流）。"""
    try:
        from apps.api import config_center as cc
        from apps.api.db import SessionLocal
        key = ""
        for k, (p0, _f) in CONFIG_OVERRIDES.items():
            if p0 == path:
                key = k
                break
        if not key:
            return base
        _db = SessionLocal()
        try:
            v = int(cc.get_config(_db, key, base.max_hits) or base.max_hits)
        finally:
            _db.close()
        if v <= 0:
            return base
        return Limit(base.window, v, base.name)
    except Exception:                                    # noqa: BLE001
        return base


def match_rule(path: str, method: str = "GET") -> Limit | None:
    """匹配限流规则。返回 None = 不限流。"""
    # 精确匹配敏感端点
    if path in SENSITIVE_RULES:
        return _configured_limit(path, SENSITIVE_RULES[path])
    # 生成类（前缀 + 关键词）—— 只限写操作
    if method.upper() in ("POST", "PUT", "PATCH"):
        if any(kw in path for kw in GENERATE_KEYWORDS):
            return GENERATE_LIMIT
    return None


# ================================================================ 对外主入口

def check_request(path: str, method: str, ip: str,
                  user_id: str = "") -> dict | None:
    """检查请求是否超限。返回 None = 放行；否则返回拒绝信息 dict。

    ★ 为什么按 IP + user 双维度：
      纯 IP 限流会被 NAT（公司/学校出口）误伤；
      纯用户限流挡不住未登录的暴力破解。
      两者结合：登录用 IP（未登录），生成用 user（已登录）。
    """
    # ★★ v1.0-E4.10：测试/冒烟可显式关闭限流
    #
    #   ★ 为什么需要（踩坑记录）：
    #     全端点冒烟要发 1600+ 请求，**同一个管理员账号**必然触发
    #     `gen:{user_id}` 限流 → 993 次被 429 拦掉
    #     → **大量端点根本没进业务逻辑，冒烟产生假阴性**
    #     （看起来"通过"，实际什么都没测）。
    #
    #   ★★ 判据：**压测/冒烟类工具必须能绕开业务限流** ——
    #     否则测的不是"端点是否正确"，而是"限流是否符合预期"。
    #
    #   ★ 安全：只在**显式设置环境变量**时关闭，且启动时打 warning。
    #     不是"生产默认关闭"，所以不构成风险。
    if _RATELIMIT_OFF:
        return None

    if is_private(ip) or ip in IP_WHITELIST:
        return None                      # 内网不限流

    # ---- 第 1 层：全局 IP ----
    ok, left, retry = _store.hit(f"g:{ip}", GLOBAL_IP_LIMIT.window,
                                 GLOBAL_IP_LIMIT.max_hits)
    if not ok:
        return _deny("全局请求过于频繁，请稍后再试", retry, "global")

    # ---- 第 2 层：端点规则 ----
    rule = match_rule(path, method)
    if rule is None:
        return None

    # 生成类：已登录按用户，未登录按 IP
    if rule is GENERATE_LIMIT and user_id:
        key = f"gen:{user_id}"
    else:
        key = f"{rule.name}:{ip}"
        if user_id and rule is GENERATE_LIMIT:
            key = f"gen:{user_id}:{ip}"

    ok, left, retry = _store.hit(key, rule.window, rule.max_hits)
    if not ok:
        return _deny(
            f"{rule.name}过于频繁（{rule.describe()}），请稍后再试",
            retry, rule.name)
    return None


def _deny(message: str, retry_after: float, scope: str) -> dict:
    secs = int(max(1, retry_after))
    return {
        "status": 429,
        "message": message,
        "retry_after": secs,
        "scope": scope,
        "headers": {"Retry-After": str(secs)},
    }


def record_login_fail(ip: str, username: str = "") -> int:
    """记一次登录失败，返回该 IP 近期失败次数（风控用）。"""
    _login_fails.hit(f"lf:{ip}", 900, 10_000)
    n = _login_fails.count(f"lf:{ip}", 900)
    if username:
        _login_fails.hit(f"lfu:{ip}:{username}", 900, 10_000)
    return n


def login_fail_count(ip: str) -> int:
    return _login_fails.count(f"lf:{ip}", 900)


def reset_login_fail(ip: str) -> None:
    """登录成功后清零。"""
    _login_fails.reset(f"lf:{ip}")


def stats() -> dict:
    """限流器状态（后台观测用）。

    ★ v0.25.2：加 `backend` 字段 —— 运维必须能一眼看出
      "现在计数是共享的还是各算各的"（多 worker 下差别巨大）。
    """
    b = backend()
    return {
        "backend": b,
        "multi_worker_safe": b.startswith("redis"),
        "tracked_keys": _store.size(),
        "login_fail_keys": _login_fails.size(),
        "rules": len(SENSITIVE_RULES),
        "whitelist": sorted(IP_WHITELIST),
        "note": ("Redis 后端：多 worker 下计数共享（推荐）" if b.startswith("redis")
                 else "★ 内存后端：多 worker 下实际额度 = 配置 × worker 数"),
    }


def self_check() -> dict:
    """启动自检（log 一行结论）。"""
    b = backend()
    st = stats()
    if b.startswith("redis"):
        log.info("限流已启用：%d 条规则（IP 白名单 %d 个）· 后端 Redis（多 worker 安全）",
                 st["rules"], len(st["whitelist"]))
    else:
        log.info("限流已启用：%d 条规则（IP 白名单 %d 个）· 后端 内存",
                 st["rules"], len(st["whitelist"]))
        # ★ 仅在多 worker 时告警 —— 单 worker 下内存后端完全正确
        try:
            import os as _os
            w = _os.environ.get("WEB_CONCURRENCY") or ""
            if w.isdigit() and int(w) > 1:
                log.warning("★ 检测到多 worker（%s）但限流用内存后端："
                            "实际额度 = 配置 × %s。请设置 %s=redis",
                            w, w, ENV_BACKEND)
        except Exception:                            # noqa: BLE001
            pass
    return st

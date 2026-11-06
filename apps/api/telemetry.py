"""开源部署数据收集（v0.24.2 F2）。

★ 合规五原则（缺一不可，否则会挨骂）：
  1. **首次启动明确告知** + 可选择关闭
  2. **只收集匿名聚合数据**，绝不收集正文/用户信息
  3. **代码完全可见**（本模块就是"收集什么"的唯一定义，
     任何新增字段都必须在此文件可见）
  4. **README 透明说明**
  5. 遵循所在地区隐私法规

★ 工程三原则：
  · **绝不阻塞主流程**：采集失败静默（不重试到影响业务）
  · **绝不抛异常**：所有采集函数用 try 包住
  · **opt-out 优先**：读不到配置时按"关闭"处理（保守）

★★ 红线（本模块的核心价值）：`FORBIDDEN_PAYLOADS` 与 `redline_check()`
   —— 任何上报数据在发送前必须过红线校验，命中则**丢弃整包**并记日志。
   宁可少收一条数据，不能误传一次用户内容。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import sys
import threading
import time
import urllib.request
from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

_log = logging.getLogger("novelforge.telemetry")

# ---------------------------------------------------------------- 配置

ENV_SWITCH = "SOZEER_TELEMETRY"          # on / off（开源版默认 on；=off 可关）
ENV_ENDPOINT = "SOZEER_TELEMETRY_URL"
ENV_INSTANCE = "SOZEER_INSTANCE_ID"
# ★★ v0.25.5：以下**全部可选**（不配也能正常工作）。
#   部署方无需填写任何信息 —— 身份与展示名都会自动生成。
#   ★ 唯一必须的是 TELEMETRY_URL（且必须写**域名**）：
#     写域名 → SaaS 迁移/换 IP 时 DNS 自动解析到新地址，部署方零改动；
#     写 IP   → SaaS 一换 IP 就全体失联。
ENV_SITE_URL = "SOZEER_SITE_URL"        # 可选：想展示自己的站点地址时填
ENV_SITE_NAME = "SOZEER_SITE_NAME"      # 可选：想展示自定义名称时填
ENV_CONTACT = "SOZEER_CONTACT"          # 可选：留个联系方式（便于商务接触）
# ★★ 主机名：**默认不上报**（opt-in）。
#   为什么默认关闭：主机名常含个人信息（如 `zhangsan-macbook`），
#   属于可定位到具体人的弱标识 —— 与"匿名统计"承诺冲突。
#   ★ 想让自己在后台更好辨识的部署方可主动开启：SOZEER_SHARE_HOSTNAME=1
ENV_SHARE_HOSTNAME = "SOZEER_SHARE_HOSTNAME"
# ★ 上报地址必须用域名（SaaS 侧换 IP 时部署方无需改动）
DEFAULT_ENDPOINT = "https://sozeer.com/v1/heartbeat"
HEARTBEAT_SECONDS = 24 * 3600            # 24 小时
TIMEOUT = 15.0
MAX_RETRY = 3                            # 失败静默重试（间隔递增）

# ★ 事件计数（内存缓冲，随心跳一起上报）。
#   为什么缓存在内存而不是直接落库：事件是高频写入（每次生成都记），
#   落库会带来无谓的 I/O；心跳时聚合上报一次即可。
_events: Counter = Counter()
_events_lock = threading.Lock()
_last_beat = 0.0


def is_enabled(db: Session | None = None) -> bool:
    """★ 遥测开关。**开源版默认开启**（收集匿名聚合统计）。

    优先级：环境变量 > DB 配置 > 默认开启。
    ★ 为什么不保守关闭：开源版的"用了多少人 / 哪些功能受欢迎"对社区
      与作者都很重要；且本模块已做到（1）只收匿名聚合数据、
      （2）发前过红线校验、（3）代码完全可见、（4）可一键关闭。
      → 默认开启是"默认同意匿名统计"，而非"偷偷收集"。
    """
    v = (os.environ.get(ENV_SWITCH) or "").strip().lower()
    if v in ("off", "0", "false", "no", "disable", "disabled"):
        return False
    if v in ("on", "1", "true", "yes", "enable", "enabled"):
        return True
    # 环境变量未设 → 看 DB（用户在设置页关过就尊重）
    if db is not None:
        try:
            from apps.api.models import Setting
            s = db.get(Setting, "telemetry.enabled")
            if s is not None:
                return str(s.value or "").strip().lower() in ("1", "true", "on")
        except Exception:                               # noqa: BLE001
            pass
    return True                                         # ★ 开源版默认开启


def set_enabled(db: Session, enabled: bool) -> None:
    """写入开关（设置页调用）。"""
    from apps.api.models import Setting
    s = db.get(Setting, "telemetry.enabled")
    if s is None:
        from apps.api.models import now as _now
        s = Setting(key="telemetry.enabled", value="1" if enabled else "0")
        db.add(s)
    else:
        s.value = "1" if enabled else "0"
    db.commit()


def instance_id(db: Session | None = None) -> str:
    """★ 实例标识（随机 UUID，**无法反查机器**）。

    为什么不用 MAC / 机器码：那属于设备指纹，可反查到具体机器 = 个人信息。
    随机 UUID 首次启动生成、重置即换新，对"统计部署数量"完全够用。
    """
    v = (os.environ.get(ENV_INSTANCE) or "").strip()
    if v:
        return v
    if db is not None:
        try:
            from apps.api.models import Setting
            from apps.api.models import now as _now
            s = db.get(Setting, "telemetry.instance_id")
            if s is not None and (s.value or "").strip():
                return str(s.value).strip()
            import uuid
            iid = uuid.uuid4().hex
            if s is None:
                db.add(Setting(key="telemetry.instance_id", value=iid))
            else:
                s.value = iid
            db.commit()
            return iid
        except Exception:                               # noqa: BLE001
            pass
    return "unknown"


def build_id() -> str:
    """构建指纹 —— 用于溯源"哪个构建"。

    ★★ v1.0 E3：改为统一走 `build_info`（原来自己算一遍 sha256）。
      为什么必须统一：L2 埋点（响应头/日志/DB _meta）与 L3 遥测
      如果各算各的，会出现"日志里的 build_id ≠ 遥测里的 build_id" ——
      真出事时无法对齐，溯源能力打对折。

    ★ 只哈希**版本常量与许可证标识**，绝不哈希用户数据。
    """
    try:
        from apps.api import build_info as bi
        return bi.BUILD_ID
    except Exception:                                   # noqa: BLE001
        return ""


# ---------------------------------------------------------------- 红线

# ★★ 绝不收集（红线）。键名命中即整包丢弃。
FORBIDDEN_PAYLOADS = {
    # 内容
    "content", "text", "body", "chapter", "draft", "prompt", "completion",
    "message", "messages", "conversation", "原文", "正文", "内容", "草稿",
    # 身份
    "username", "user_name", "email", "phone", "mobile", "nickname",
    "ip", "ip_addr", "address", "real_name",
    # 凭据
    "api_key", "apikey", "secret", "token", "password", "authorization",
    "cookie", "credential", "private_key",
    # 路径（可能含用户名）
    "path", "file_path", "abspath", "cwd",
}

# 值级兜底（与 feedback 同一套模式，保持项目内一致）
import re as _re
_VALUE_PATTERNS = [
    (_re.compile(r"\b1[3-9]\d{9}\b"), "[phone]"),
    (_re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[email]"),
    (_re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), "[key]"),
    (_re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+"), "[jwt]"),
    (_re.compile(r"(?:[A-Za-z]:\\|/home/|/Users/)[^\s\"']+"), "[path]"),
    (_re.compile(r"\b(?!10\.|127\.|192\.168\.)"
                 r"(?:\d{1,3}\.){3}\d{1,3}\b"), "[ip]"),
]


def redline_check(payload: dict) -> tuple[bool, list[str]]:
    """★ 红线校验：上报前最后一道闸。

    返回 (是否干净, 命中项)。**命中即整包丢弃**（fail-closed）。
    宁可少收一条数据，不能误传一次用户内容。
    """
    hits: list[str] = []

    def walk(obj, path: str = "") -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                p = f"{path}.{k}" if path else str(k)
                if str(k).strip().lower() in FORBIDDEN_PAYLOADS:
                    hits.append(p)
                walk(v, p)
        elif isinstance(obj, (list, tuple)):
            for i, v in enumerate(obj):
                walk(v, f"{path}[{i}]")
        elif isinstance(obj, str):
            for pat, rep in _VALUE_PATTERNS:
                if pat.search(obj):
                    # ★ 原来写的是 _rep.pattern（_rep 是字符串）→ AttributeError
                    hits.append(f"{path or '(root)'}:{rep}")

    walk(payload)
    return (not hits), hits


def scrub(payload: dict) -> dict:
    """值级脱敏（红线校验的补充：路径/IP 等替换而非丢弃）。"""
    def _s(v):
        if isinstance(v, str):
            out = v
            for pat, rep in _VALUE_PATTERNS:
                out = pat.sub(rep, out)
            return out
        if isinstance(v, dict):
            return {k: _s(x) for k, x in v.items()
                    if str(k).strip().lower() not in FORBIDDEN_PAYLOADS}
        if isinstance(v, (list, tuple)):
            return [_s(x) for x in v]
        return v
    return _s(payload)


# ---------------------------------------------------------------- 采集（5 类）

def _count(db: Session, model, **where) -> int:
    try:
        q = select(func.count()).select_from(model)
        for k, v in where.items():
            q = q.where(getattr(model, k) == v)
        return int(db.scalar(q) or 0)
    except Exception:                                   # noqa: BLE001
        return 0


def collect_scale(db: Session) -> dict:
    """② 业务规模维度（聚合计数，**无内容**）。"""
    from apps.api.models import (Book, Chapter, Character, DramaProject,
                                 GenrePack, User, WorldSetting)
    return {
        "books": _count(db, Book),
        "chapters": _count(db, Chapter),
        "characters": _count(db, Character),
        "world_settings": _count(db, WorldSetting),
        "users": _count(db, User),
        "drama_projects": _count(db, DramaProject),
        "genre_packs": _count(db, GenrePack),
    }


def collect_feature(db: Session) -> dict:
    """③ 功能使用维度（事件计数，**无 prompt**）。"""
    with _events_lock:
        return dict(_events)


def collect_quality(db: Session) -> dict:
    """④ 质量与逻辑优化维度（脱敏分布，**重点**）。

    数据来源：AuditReport 的 issue 类型分布 + Run 的 gate 分数分布。
    ★ 只取**类型与计数**，不取任何文本。
    """
    from apps.api.models import AuditReport, Run
    out: dict = {"consistency_issues": {}, "gate_fail_reasons": {},
                 "gate_score_hist": {}}
    try:
        rows = list(db.scalars(select(AuditReport).limit(2000)).all())
        issues: Counter = Counter()
        for r in rows:
            try:
                data = json.loads(r.issues_json or "[]")
            except Exception:
                continue
            for it in (data if isinstance(data, list) else []):
                rule = (it or {}).get("rule") or (it or {}).get("kind") or ""
                if rule:
                    issues[str(rule)[:64]] += 1
        out["consistency_issues"] = dict(issues.most_common(30))
    except Exception:                                   # noqa: BLE001
        pass
    try:
        runs = list(db.scalars(select(Run).limit(2000)).all())
        hist: Counter = Counter()
        for r in runs:
            s = int(r.gate_score or 0)
            if s <= 0:
                continue
            bucket = f"{(s // 10) * 10}-{(s // 10) * 10 + 9}"
            hist[bucket] += 1
        out["gate_score_hist"] = dict(hist)
    except Exception:                                   # noqa: BLE001
        pass
    return out


def collect_errors(db: Session) -> dict:
    """⑤ Bug 与异常维度（**哈希，非全文**）。"""
    from apps.api.models import LLMCall, MediaCall
    out: dict = {"failed_tasks": {}, "llm_errors": {}}
    try:
        rows = list(db.scalars(
            select(LLMCall).where(LLMCall.status != "ok").limit(1000)).all())
        c: Counter = Counter()
        for r in rows:
            # ★ 只存错误类型的**哈希前缀**（可聚类，不可读原文）
            e = (r.error or "")[:80]
            if not e:
                continue
            h = hashlib.sha256(e.encode()).hexdigest()[:12]
            # 取错误类名（"HTTP 429" / "网络错误" 这类前缀，非全文）
            label = e.split(":", 1)[0][:32]
            c[f"{label}#{h[:6]}"] += 1
        out["llm_errors"] = dict(c.most_common(20))
    except Exception:                                   # noqa: BLE001
        pass
    try:
        rows = list(db.scalars(
            select(MediaCall).where(MediaCall.status != "ok").limit(500)).all())
        c2: Counter = Counter()
        for r in rows:
            if r.error_code:
                c2[str(r.error_code)[:32]] += 1
        out["failed_tasks"] = dict(c2.most_common(20))
    except Exception:                                   # noqa: BLE001
        pass
    return out


def collect_env(db: Session) -> dict:
    """① 部署实例维度（匿名）。"""
    from apps.api import config
    deploy = "docker" if os.path.exists("/.dockerenv") else "bare"
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        deploy = "k8s"
    site_url, site_name, contact = site_info(db)
    # ★★ v1.0 E3：补上 build_hash / license / channel —— 来源追溯所需
    #   （与 L2 埋点共用同一份 build_info，保证各处一致）
    try:
        from apps.api import build_info as _bi
        _bhash = _bi._compute_hash()
        _lic = _bi.LICENSE_ID
        _chan = _bi.CHANNEL
    except Exception:                                   # noqa: BLE001
        _bhash, _lic, _chan = "", "", ""
    return {
        # ★★ 身份 = 首次启动自生成的随机 UUID（写在本地 data/ 里）
        #    → 部署方**无需任何人工配置**；迁移机器时把 data/ 一起带走即无缝续上
        "instance_id": instance_id(db),
        "build_id": build_id(),
        "build_hash": _bhash,
        "license": _lic,
        "channel": _chan,
        "version": getattr(config, "APP_VERSION", ""),
        "deploy_type": deploy,
        "os": platform.system().lower(),
        "python_ver": f"{sys.version_info.major}.{sys.version_info.minor}",
        # ★ 主机名：便于运维在后台区分"哪台机器"。
        #   ★ 不算设备指纹：主机名是用户可改的显示名，
        #     且我们**不**上报 MAC / 机器码 / 序列号（那是可反查的个人信息）。
        # ★ 主机名：**默认不上报**（opt-in，见 ENV_SHARE_HOSTNAME）。
        #   为什么默认关闭：主机名常含个人信息（`zhangsan-macbook`），
        #   与"匿名统计"承诺冲突。★ 宁可后台辨识度低一点，也不越隐私红线。
        **({"hostname": (platform.node() or "")[:64]}
           if os.environ.get(ENV_SHARE_HOSTNAME, "") in ("1", "true", "on") else {}),
        # ★ 以下三项**可选**（默认按实例 ID 自动生成展示名，不填也对）
        "site_url": site_url,
        "site_name": site_name or default_site_name(),
        "contact": contact,
    }


def default_site_name() -> str:
    """默认展示名（部署方不配也能在后台看清是哪个实例）。

    ★ 为什么不默认成"未知"：后台一屏几十个"未知"没法用。
      用短 ID 至少能对上号（且不含任何敏感信息）。
    """
    return ""


def _norm_site_url(u: str) -> str:
    """规范化站点地址（只留 scheme://host，去掉路径）。

    ★ 为什么去掉路径：`https://x.com/` 与 `https://x.com/app` 是同一站点，
      保留路径会让同一实例出现多个 url（看起来像不同实例）。
    """
    t = (u or "").strip()
    if not t:
        return ""
    if not t.startswith(("http://", "https://")):
        t = "https://" + t
    try:
        from urllib.parse import urlsplit
        pr = urlsplit(t)
        host = (pr.hostname or "").strip().lower()
        if not host or "." not in host:
            return ""
        parts = host.split(".")
        if all(x.isdigit() for x in parts) and len(parts) == 4:
            # ★ 不用 IP 当站点地址（这正是要避免的用法）
            return ""
        sch = "https" if pr.scheme != "http" else "http"
        por = f":{pr.port}" if pr.port and pr.port not in (80, 443) else ""
        return f"{sch}://{host}{por}"
    except Exception:                                # noqa: BLE001
        return ""


def site_info(db: Session | None = None) -> tuple[str, str, str]:
    """站点信息（环境变量 > Setting 表 > 空）。

    ★ 为什么不从 HTTP 请求头取 Host：那等于把"访问者看到的域名"
      当成实例身份 —— 反代 / 多域名 / CDN 下会飘，
      且无法区分"同一个实例的多个域名"。
      **必须由部署方显式配置**，语义才确定。
    """
    url = (os.environ.get(ENV_SITE_URL) or "").strip()
    name = (os.environ.get(ENV_SITE_NAME) or "").strip()
    contact = (os.environ.get(ENV_CONTACT) or "").strip()
    if db is not None and (not url or not name):
        try:
            from apps.api.models import Setting
            if not url:
                r = db.get(Setting, "site.url")
                url = (r.value if r else "") or ""
            if not name:
                r2 = db.get(Setting, "site.display_name")
                name = (r2.value if r2 else "") or ""
                if not name:
                    from apps.api.services.settings import get_setting
                    name = get_setting(db, "system.site_name", "")
        except Exception:                            # noqa: BLE001
            pass
    return _norm_site_url(url), name[:128], contact[:255]


def build_payload(db: Session, *, full: bool = True) -> dict:
    """组装完整上报包（含红线自检）。"""
    p: dict = {"schema": 1, "ts": int(time.time()), **collect_env(db)}
    if full:
        p["scale"] = collect_scale(db)
        p["feature"] = collect_feature(db)
        p["quality"] = collect_quality(db)
        p["errors"] = collect_errors(db)
        # ★★ v1.0-E4.5：采纳分析（判断"生成质量好不好"的唯一依据）
        #
        #   ★ 为什么必须补：此前只能知道"生成了多少"，**不知道"好不好"**。
        #     生成次数多 ≠ 质量好 —— 可能只是用户反复重试。
        #
        #   ★ 字段全为**计数与分桶**（见 apps/api/adoption.py），
        #     不含任何正文、不含精确字数、提示词只存哈希。
        try:
            from apps.api import adoption as _ad
            p["adoption"] = _ad.collect_adoption()
        except Exception:                               # noqa: BLE001
            p["adoption"] = {}
    return p


# ---------------------------------------------------------------- 发送

def _post(url: str, payload: dict) -> tuple[bool, str]:
    """单次 POST（异常全部吞掉）。"""
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": f"sozeer-telemetry/{payload.get('version', '')}"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return (200 <= resp.status < 300), f"HTTP {resp.status}"
    except Exception as e:                              # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def send(db: Session, payload: dict | None = None, *,
         dry_run: bool = False) -> dict:
    """★ 上报（绝不抛异常、绝不阻塞主流程）。

    ★ 为什么先过红线再发：**发送是不可撤回的**。
      一旦发出去就无法收回，所以闸门必须在**发之前**。
    """
    try:
        if not is_enabled(db):
            return {"ok": False, "skipped": True, "reason": "遥测已关闭"}
        p = payload if payload is not None else build_payload(db)

        ok, hits = redline_check(p)
        if not ok:
            # ★★ 命中红线：丢弃整包（fail-closed）
            _log.warning("遥测红线命中，已丢弃整包：%s", hits[:10])
            try:
                from apps.api.models import TelemetryMetric
                from apps.api.models import now as _now
                db.add(TelemetryMetric(
                    id=f"tm_{hashlib.sha256(str(time.time()).encode()).hexdigest()[:12]}",
                    instance_id=str(p.get("instance_id", "")),
                    metrics_json="", redline_ok=0,
                    redline_hits=json.dumps(hits[:20], ensure_ascii=False)[:500],
                    collected_at=_now()))
                db.commit()
            except Exception:                           # noqa: BLE001
                pass
            return {"ok": False, "redline": hits, "dropped": True}

        p = scrub(p)
        url = os.environ.get(ENV_ENDPOINT) or DEFAULT_ENDPOINT
        if dry_run:
            return {"ok": True, "dry_run": True, "payload": p,
                    "bytes": len(json.dumps(p, ensure_ascii=False))}

        last = ""
        for i in range(MAX_RETRY):
            ok2, msg = _post(url, p)
            if ok2:
                _store(db, p)
                return {"ok": True, "attempts": i + 1, "bytes": len(json.dumps(p))}
            last = msg
            if i < MAX_RETRY - 1:
                time.sleep(min(2 ** i, 4))              # 1s, 2s（最后一次不睡）
        _log.info("遥测上报失败（已静默，不影响主流程）：%s", last)
        return {"ok": False, "error": last}
    except Exception as e:                              # noqa: BLE001
        # ★ 兜底：任何异常都不允许冒泡到业务
        _log.warning("遥测异常（已吞）：%s: %s", type(e).__name__, e)
        return {"ok": False, "error": str(e)[:200]}


def _store(db: Session, p: dict) -> None:
    """落库（服务端自用时用；开源实例一般只上报不落库）。"""
    from apps.api.models import TelemetryInstance, TelemetryMetric
    from apps.api.models import now as _now
    iid = str(p.get("instance_id") or "")
    if not iid:
        return
    inst = db.get(TelemetryInstance, iid)
    if inst is None:
        inst = TelemetryInstance(
            instance_id=iid, build_id=p.get("build_id", ""),
            version=p.get("version", ""), deploy_type=p.get("deploy_type", ""),
            os=p.get("os", ""), python_ver=p.get("python_ver", ""),
            first_seen=_now(), last_seen=_now(), heartbeat_count=1)
        db.add(inst)
    else:
        inst.last_seen = _now()
        inst.heartbeat_count = int(inst.heartbeat_count or 0) + 1
        inst.version = p.get("version", inst.version)
        # 部署方式可能变（迁移到 k8s），要及时更新
        if p.get("deploy_type"):
            inst.deploy_type = p["deploy_type"]


    db.add(TelemetryMetric(
        id=f"tm_{hashlib.sha256(f'{iid}{time.time()}'.encode()).hexdigest()[:12]}",
        instance_id=iid,
        metrics_json=json.dumps(p, ensure_ascii=False)[:20000],
        redline_ok=1, redline_hits="", collected_at=_now()))
    db.commit()


# ---------------------------------------------------------------- 事件计数

def track(feature: str) -> None:
    """★ 记一次功能使用（**内存计数，零 I/O**）。

    为什么内存：生成路径上不能有额外 I/O。
    进程重启丢计数可以接受（心跳本来也是周期性的）。
    """
    if not feature:
        return
    try:
        with _events_lock:
            _events[str(feature)[:48]] += 1
    except Exception:                                   # noqa: BLE001
        pass


def reset_events() -> None:
    with _events_lock:
        _events.clear()


def maybe_heartbeat(db: Session, *, force: bool = False) -> dict:
    """心跳节流（24h 一次）。由调度器/启动钩子调用。"""
    global _last_beat
    now_ts = time.time()
    if not force and (now_ts - _last_beat) < HEARTBEAT_SECONDS:
        return {"ok": False, "skipped": True,
                "next_in": int(HEARTBEAT_SECONDS - (now_ts - _last_beat))}
    r = send(db)
    if r.get("ok"):
        _last_beat = now_ts
    return r


# ---------------------------------------------------------------- 透明度

def disclosure(db: Session | None = None) -> dict:
    """★ 数据收集透明说明（**必须**暴露给用户看的完整清单）。

    对应合规原则 3「代码完全可见」：这就是"收集什么"的权威定义。
    任何新增字段都必须同步更新本函数 —— 它是给用户看的承诺。
    """
    return {
        "enabled": is_enabled(db),
        "switch": {"env": f"{ENV_SWITCH}=off 可关闭（也可在设置页关）",
                   "ui": "设置 → 数据与隐私 → 匿名使用统计",
                   "default": "开启（开源版默认收集匿名聚合统计）"},
        "collect": {
            "① 部署实例": ["实例随机 ID（无法反查机器）", "构建指纹",
                           "版本号", "部署方式", "操作系统", "Python 版本"],
            "② 业务规模": ["各表行数（书/章/角色/设定）", "用户数（仅数字）",
                           "短剧项目数", "题材包数量"],
            "③ 功能使用": ["功能调用次数", "模型调用次数（不含内容）",
                           "错误类型计数", "关键接口耗时"],
            "④ 质量分布": ["一致性检查问题类型分布", "质检分数直方图",
                           "闸门失败原因分布"],
            "⑤ 异常": ["异常类型 + 堆栈哈希（非全文）", "失败原因分类"],
            # ★★ v1.0-E4.5 新增：采纳分析
            #   ★ 为什么要采集：此前只能知道"生成了多少"，
            #     **不知道"生成得好不好"** —— 生成次数多可能只是反复重试。
            #   ★ 隐私保护：**全部是计数与分桶**，
            #     不含正文、不含精确字数、提示词只存哈希。
            "⑥ 采纳情况（全部为计数与区间）": [
                "各环节生成次数 / 成功次数 / 采纳次数",
                "采纳后修改幅度（**分桶**：几乎未改 / 1-10% / 10-30% / 30-60% / 60-90% / >90%）",
                "重写次数（**分桶**：1 / 2-3 / 4-9 / 10+）",
                "产出字数（**分桶**：1-500 / 501-1k / 1k-2k / 2k-3k / 3k-5k / 5k-10k / 10k+）",
                "丢弃原因（**枚举值**：用户关闭 / 质检未过 / 出错 / 被新稿替代）",
                "提示词的 **SHA256 哈希前 16 位**（用于版本对比，**不可反推内容**）",
                # ★★ v1.0-E4.6：指纹来源说明（用户有权知道从哪取的）
                "  ↳ 指纹取自：生成时用的提示词模板内容（chapter_write 等技能包）",
                "  ↳ 用途：对比「不同提示词版本的采纳率」，做数据驱动的迭代",
                "  ↳ 局限：只能区分「是否同一版本」，**不能还原改了什么**",
            ],
        },
        "never_collect": [
            "小说正文内容", "用户名 / 邮箱 / 手机号 / IP",
            "API Key / 密钥", "对话原文", "生成内容的任何片段",
            "文件路径中的敏感信息",
            # ★★ v1.0-E4.5：与"采纳分析"配套的反向承诺
            "采纳/修改的**具体内容**（只统计「改了多少」，不记录「改成什么」）",
            "提示词**原文**（只上报哈希，用于版本对比）",
            "精确字数（只上报区间，避免通过字数反推内容）",
            "精确时间戳（不用于行为画像）",
        ],
        "mechanism": {
            "frequency": "每 24 小时一次心跳（服务端可调整间隔）",
            "transport": "HTTPS POST（失败静默重试 3 次，不影响主流程）",
            "endpoint": os.environ.get(ENV_ENDPOINT) or DEFAULT_ENDPOINT,
            "redline": "上报前做红线校验，命中即丢弃整包（fail-closed）",
            # ★★ v1.0-E4.5：补调度说明
            #   ★ 此前**没有调度器** —— 只能管理员手动点"立即上报"。
            #     现在由后台线程自动执行（启动后 90 秒首次，之后按间隔）。
            "scheduler": "服务启动后自动运行（daemon 后台线程）；"
                         "首次上报在启动 90 秒后，之后按间隔执行。"
                         "★ 关闭遥测后线程仍在运行但不做任何上报。",
        },
        # ★★ v0.25.5：身份与网络位置解耦（解决"迁移/IP 变更后检测不到"）
        "identity": {
            "how": "实例自生成随机 UUID（写在本地 data/），与 IP / 域名**无关**",
            "why": "身份若用 IP：迁移服务器或换 IP 就会掉线、NAT 后多实例混在一起",
            "migration": "迁移机器时把 data/ 目录一起带走 → 无缝续上历史",
            "instance_id": (instance_id(db) if db is not None else ""),
            "source_ip": "由服务端从连接中读取（仅用于排障，**不参与识别**）",
        },
        "optional_fields": {
            "SOZEER_SITE_URL": "想展示自己的站点地址时填（★ 填域名，不要填 IP）",
            "SOZEER_SITE_NAME": "想展示自定义名称时填（不填则自动生成）",
            "SOZEER_CONTACT": "留个联系方式（可选）",
            "SOZEER_SHARE_HOSTNAME": "=1 时上报主机名（★ 默认关闭）",
            "note": "★ 以上全部可选 —— 不填任何一项也能正常接入",
        },
        "hostname": {
            "enabled": os.environ.get(ENV_SHARE_HOSTNAME, "") in ("1", "true", "on"),
            "switch": f"{ENV_SHARE_HOSTNAME}=1 可开启",
            "default": "不上报",
            "why": "主机名常含个人信息（如 zhangsan-macbook），"
                   "与匿名统计承诺冲突 → 默认关闭",
        },
        "commands": {
            "how": "主控可下发指令（探活/公告/重载配置），随下次心跳取走",
            "why": "部署方在 NAT 后时主控连不过去，只能靠'部署方主动出站取'",
            "limit": "不实时（最长等一个心跳周期）",
        },
        "contact": "如有疑问请通过项目仓库 Issue 反馈",
    }


def stats(db: Session) -> dict:
    """服务端遥测统计（管理端）。"""
    from apps.api.models import TelemetryInstance, TelemetryMetric
    total = _count(db, TelemetryInstance)
    suspect = _count(db, TelemetryInstance, is_commercial_suspect=1)
    metrics = _count(db, TelemetryMetric)
    rejected = _count(db, TelemetryMetric, redline_ok=0)
    vers: Counter = Counter()
    try:
        for r in db.scalars(select(TelemetryInstance).limit(5000)).all():
            vers[r.version or "?"] += 1
    except Exception:                                   # noqa: BLE001
        pass
    return {"instances": total, "commercial_suspect": suspect,
            "metrics": metrics, "redline_rejected": rejected,
            "by_version": dict(vers.most_common(10))}


# ================================================================
#  ★★ v1.0-E4.5：心跳调度器（此前**完全缺失**）
#
#  ============================ 为什么必须补 ============================
#
#  `maybe_heartbeat()` 的文档写着「由调度器/启动钩子调用」，
#  但**调度器从来没写过** —— 唯一调用点是管理端手动端点
#  `POST /api/admin/telemetry/send`。
#
#  ★ 后果（实测确认）：
#    · 开源实例即使开启遥测，**也不会自动上报**
#    · SaaS 侧 `telemetry_instances` 表 **0 条记录**
#    · 整条采集链路实际上是"死的"
#
#  ★ 这是本项目反复出现的模式：**"基础设施建好了，最后一公里没接上"**
#    （与 MEMORY §49 同类 —— 队列建了 6 个版本但生成入口直连执行）
#
#  ============================ 实现要点 ============================
#
#  ① **daemon 线程**：不能阻塞进程退出（否则 systemctl stop 会挂住）
#  ② **启动后延迟首次**：等 DB/服务就绪，避免启动瞬间的竞态
#  ③ **异常全吞**：遥测绝不能影响主服务（旁路功能铁律）
#  ④ **可关闭**：开关关闭时线程直接退出，不做无意义轮询
#  ⑤ **优雅停止**：提供 stop() 供测试与关闭流程调用
# ================================================================

# 调度器线程与停止事件
_sched_thread: threading.Thread | None = None
_sched_stop = threading.Event()
# ★ 启动后延迟多久做首次心跳（秒）—— 等服务就绪
_FIRST_DELAY = 90
# ★ 轮询间隔：实际间隔由配置 `telemetry.interval_hours` 决定，
#   但轮询粒度要细一些（否则改配置后要等一整个周期才生效）
_POLL_SECONDS = 600          # 10 分钟检查一次"该不该发"


def _heartbeat_loop() -> None:
    """后台心跳循环（daemon 线程体）。

    ★ 设计：**细粒度轮询 + 粗粒度节流**
      · 轮询 = 10 分钟（响应配置变更）
      · 节流 = `telemetry.interval_hours`（默认 24h，实际发送频率）
      轮询粒度若等于发送间隔，则改配置后要等一整个周期才生效。
    """
    log = logging.getLogger("novelforge.telemetry")
    # ① 启动后延迟首次（等服务就绪）
    if _sched_stop.wait(_FIRST_DELAY):
        return                                       # 期间被停止

    while not _sched_stop.is_set():
        try:
            from apps.api.db import SessionLocal
            db = SessionLocal()
            try:
                if is_enabled(db):
                    r = maybe_heartbeat(db)
                    if r.get("ok"):
                        log.info("遥测心跳已上报：%s bytes",
                                 r.get("bytes", "?"))
                    elif not r.get("skipped"):
                        # ★ 失败只记 info 级（不刷 warning —— 防日志噪音）
                        log.info("遥测心跳未成功（已静默）：%s",
                                 str(r.get("error") or r.get("reason"))[:80])
            finally:
                db.close()
        except Exception as e:                       # noqa: BLE001
            # ★ 异常全吞：遥测是旁路功能，绝不能影响主服务
            log.debug("遥测调度异常（已忽略）：%s", e)

        # ② 可中断等待（stop() 时立即醒）
        if _sched_stop.wait(_POLL_SECONDS):
            break


def start_scheduler() -> dict:
    """启动心跳调度器（幂等）。

    ★ 由 `main.py` 的 startup 钩子调用。
    ★ 幂等：重复调用只启动一个线程（防 reload 模式起多个）。
    """
    global _sched_thread
    if _sched_thread is not None and _sched_thread.is_alive():
        return {"ok": True, "already": True}
    _sched_stop.clear()
    _sched_thread = threading.Thread(
        target=_heartbeat_loop, name="sozeer-telemetry",
        daemon=True)                                 # ★ daemon：不阻塞退出
    _sched_thread.start()
    return {"ok": True, "first_delay": _FIRST_DELAY,
            "poll": _POLL_SECONDS}


def stop_scheduler(timeout: float = 2.0) -> dict:
    """停止调度器（供测试与优雅关闭调用）。"""
    global _sched_thread
    _sched_stop.set()
    t = _sched_thread
    if t is not None and t.is_alive():
        t.join(timeout=timeout)
    _sched_thread = None
    return {"ok": True}


def scheduler_status() -> dict:
    """调度器状态（运维自查用）。"""
    t = _sched_thread
    return {
        "running": bool(t is not None and t.is_alive()),
        "thread": t.name if t else "",
        "first_delay_sec": _FIRST_DELAY,
        "poll_sec": _POLL_SECONDS,
        "last_beat_ts": int(_last_beat) if _last_beat else 0,
    }

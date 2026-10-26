"""风控与审计（v0.20.6 A6-2 / A6-3）。

★★★ 与限流的区别：
  · **限流**（ratelimit.py）= 单点频率控制，"同一个 IP 每秒最多 N 次"
  · **风控**（本模块）  = 跨维度行为分析，"这个 IP 今天注册了 20 个号"、"这个账号 1 小时内登录了 8 个不同国家"

★ 风控的核心是"看得见"：
  检测到异常必须**留痕 + 可查 + 可处置**，否则等于没做。

================================ 检测规则 ================================

  R1 同 IP 多账号     —— 一个 IP 短时间注册/登录多个账号（工作室养号）
  R2 短时高频登录失败 —— 暴力破解（配合限流，但风控看得更长窗口）
  R3 异地登录         —— 同一账号短时间内从不同地区登录（账号被盗）
  R4 新设备敏感操作   —— 新 IP 上直接改密/换绑（盗号后立刻转移）
  R5 异常时段         —— 凌晨批量操作（爬虫/脚本特征）
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import json
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.models import LoginRecord, User, now

log = logging.getLogger("novelforge")


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _naive_now() -> dt.datetime:
    """统一 UTC naive（与 models.now() 存储格式一致，避免 8 小时偏差）。"""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


# ================================================================ 等级

RISK_LEVELS = ("none", "low", "medium", "high")

# 行动建议（前端/后台展示）
RISK_ACTIONS = {
    "none": "放行",
    "low": "记录观察",
    "medium": "要求二次验证 / 记录告警",
    "high": "阻断并要求人工复核",
}


@dataclass
class RiskHit:
    """一条风控命中。"""
    rule: str           # R1..R5
    level: str          # low | medium | high
    message: str
    detail: dict = None

    def to_dict(self) -> dict:
        return {"rule": self.rule, "level": self.level,
                "message": self.message, "detail": self.detail or {}}


# ================================================================ 登录记录

def record_login(db: Session, *, user_id: str, ip: str, ua: str,
                 method: str = "password", success: bool = True,
                 reason: str = "") -> str:
    """★ 写登录记录（成功与失败都要记）。

    为什么失败也要记：暴力破解的特征就是"大量失败"。
    只记成功等于把攻击证据扔了。
    """
    rid = _uid("lr")
    try:
        db.add(LoginRecord(id=rid, user_id=user_id or "", ip=ip[:64],
                           ua=(ua or "")[:256], method=method,
                           success=1 if success else 0, reason=(reason or "")[:128],
                           created_at=_naive_now()))
        db.commit()
    except Exception as e:                          # noqa: BLE001
        log.error("登录记录写入失败：%s: %s", type(e).__name__, e)
        try:
            db.rollback()
        except Exception:
            pass
    return rid


def recent_logins(db: Session, user_id: str, limit: int = 20) -> list[dict]:
    """最近登录记录（用户端"登录设备"页用）。"""
    rows = db.scalars(select(LoginRecord)
                      .where(LoginRecord.user_id == user_id)
                      .order_by(LoginRecord.created_at.desc())
                      .limit(limit)).all()
    return [{"id": r.id, "ip": _mask_ip(r.ip), "ua": r.ua, "method": r.method,
             "success": int(r.success or 0), "reason": r.reason,
             "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else ""}
            for r in rows]


def _mask_ip(ip: str) -> str:
    """IP 脱敏（用户端展示用，保留网段便于识别）。"""
    if not ip:
        return ""
    if ":" in ip:                       # IPv6 只留前 4 段
        parts = ip.split(":")
        return ":".join(parts[:4]) + "::***"
    parts = ip.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.*.*"
    return ip


# ================================================================ 风险检测

def assess_login(db: Session, *, ip: str, user_id: str = "",
                 ua: str = "") -> list[RiskHit]:
    """登录时风险评估。返回命中的规则列表（空 = 无风险）。"""
    hits: list[RiskHit] = []
    since_1h = _naive_now() - dt.timedelta(hours=1)
    since_1d = _naive_now() - dt.timedelta(days=1)

    # ---- R1：同 IP 多账号（1 天内该 IP 出现过多少个不同用户）----
    if ip:
        n_users = db.scalar(
            select(func.count(func.distinct(LoginRecord.user_id)))
            .where(LoginRecord.ip == ip,
                   LoginRecord.created_at >= since_1d,
                   LoginRecord.user_id != "")) or 0
        if n_users >= 8:
            hits.append(RiskHit("R1", "high",
                                f"同一 IP 24 小时内涉及 {n_users} 个账号（疑似工作室/养号）",
                                {"ip": _mask_ip(ip), "accounts": int(n_users)}))
        elif n_users >= 4:
            hits.append(RiskHit("R1", "medium",
                                f"同一 IP 24 小时内涉及 {n_users} 个账号",
                                {"ip": _mask_ip(ip), "accounts": int(n_users)}))

    # ---- R2：短时高频登录失败（15 分钟窗口）----
    since_15m = _naive_now() - dt.timedelta(minutes=15)
    n_fail = db.scalar(
        select(func.count()).select_from(LoginRecord)
        .where(LoginRecord.ip == ip, LoginRecord.success == 0,
               LoginRecord.created_at >= since_15m)) or 0
    if n_fail >= 10:
        hits.append(RiskHit("R2", "high",
                           f"IP {_mask_ip(ip)} 15 分钟内登录失败 {n_fail} 次（暴力破解特征）",
                           {"fails": int(n_fail)}))
    elif n_fail >= 5:
        hits.append(RiskHit("R2", "medium",
                           f"IP {_mask_ip(ip)} 15 分钟内登录失败 {n_fail} 次",
                           {"fails": int(n_fail)}))

    # ---- R3：异地登录（该账号 1 小时内出现过 ≥3 个不同 /24 网段）----
    if user_id:
        rows = db.scalars(
            select(LoginRecord.ip).where(LoginRecord.user_id == user_id,
                                         LoginRecord.success == 1,
                                         LoginRecord.created_at >= since_1h)).all()
        segments = {_seg24(x) for x in rows if x}
        segments.discard("")
        if len(segments) >= 3:
            hits.append(RiskHit("R3", "high",
                               f"账号 1 小时内从 {len(segments)} 个不同网段登录（疑似被盗）",
                               {"segments": sorted(segments)[:6]}))
        elif len(segments) == 2:
            hits.append(RiskHit("R3", "low",
                               "账号 1 小时内从 2 个不同网段登录",
                               {"segments": sorted(segments)}))

    # ---- R4/R5 由调用方按场景补充（见 assess_sensitive_action）----
    return hits


def _seg24(ip: str) -> str:
    """取 IP 的 /24 网段（IPv4）或 /48（IPv6），用于判断"地域"。"""
    try:
        a = ipaddress.ip_address(ip)
        if a.version == 4:
            return ".".join(str(a).split(".")[:3]) + ".0/24"
        return ":".join(str(a).split(":")[:3]) + "::/48"
    except ValueError:
        return ""


def assess_sensitive_action(db: Session, *, user_id: str, ip: str,
                            action: str) -> list[RiskHit]:
    """敏感操作（改密/换绑/2FA）风险评估。

    ★ R4：该 IP 首次为该用户出现 → 敏感操作可能是盗号后转移。
      正常用户改密通常在自己常用设备上。
    """
    hits: list[RiskHit] = []
    if not user_id or not ip:
        return hits
    n_seen = db.scalar(
        select(func.count()).select_from(LoginRecord)
        .where(LoginRecord.user_id == user_id, LoginRecord.ip == ip,
               LoginRecord.success == 1)) or 0
    if n_seen == 0:
        hits.append(RiskHit("R4", "medium",
                           f"新 IP（{_mask_ip(ip)}）上执行敏感操作：{action}",
                           {"action": action, "ip": _mask_ip(ip)}))

    # R5：凌晨 2-6 点敏感操作（脚本/爬虫特征，国内时区）
    try:
        hour = (_naive_now() + dt.timedelta(hours=8)).hour   # UTC → 东八区
    except Exception:
        hour = 12
    if 2 <= hour < 6:
        hits.append(RiskHit("R5", "low", f"凌晨 {hour} 点执行敏感操作：{action}",
                           {"action": action, "hour": hour}))
    return hits


def max_level(hits: list[RiskHit]) -> str:
    """取最高风险等级。"""
    order = {"none": 0, "low": 1, "medium": 2, "high": 3}
    lv = "none"
    for h in hits:
        if order.get(h.level, 0) > order.get(lv, 0):
            lv = h.level
    return lv


# ================================================================ 告警留痕

class RiskAlertHits:
    """风控告警（写审计日志，复用 AdminAuditLog 表）。

    ★ 为什么复用 AdminAuditLog 而不是新表：
      风控告警和管理审计是同一类东西（"发生了什么异常，谁看到的"），
      统一在一张表里便于关联分析（"封号前有没有风控告警"）。
      operator_id 留空表示系统自动产生。
    """
    pass


def write_risk_alert(db: Session, *, hits: list[RiskHit], ip: str,
                     user_id: str = "", action: str = "",
                     ua: str = "") -> str:
    """把风控命中写入审计日志（append-only）。返回日志 id。"""
    if not hits:
        return ""
    from apps.api.models import AdminAuditLog
    aid = _uid("risk")
    lv = max_level(hits)
    try:
        db.add(AdminAuditLog(
            id=aid, operator_id="", operator_name="系统风控",
            action=f"risk.{action or 'login'}", target_type="user",
            target_id=user_id or "", reason=f"风险等级：{lv}",
            detail=json.dumps([h.to_dict() for h in hits], ensure_ascii=False)[:8192],
            ip=ip[:64], user_agent=(ua or "")[:256],
            result="rejected" if lv == "high" else "ok",
            created_at=_naive_now()))
        db.commit()
    except Exception as e:                          # noqa: BLE001
        log.error("风控告警写入失败：%s: %s", type(e).__name__, e)
        try:
            db.rollback()
        except Exception:
            pass
    return aid


def list_risk_alerts(db: Session, *, level: str = "", limit: int = 50) -> dict:
    """查询风控告警（后台"风控"页用）。"""
    from apps.api.models import AdminAuditLog
    q = select(AdminAuditLog).where(AdminAuditLog.action.like("risk.%"))
    if level:
        q = q.where(AdminAuditLog.reason.like(f"%{level}%"))
    rows = db.scalars(q.order_by(AdminAuditLog.created_at.desc())
                      .limit(min(limit, 200))).all()
    return {"items": [
        {"id": r.id, "action": r.action, "user_id": r.target_id,
         "level": (r.reason or "").replace("风险等级：", ""),
         "detail": r.detail, "ip": r.ip,
         "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else ""}
        for r in rows]}


# ================================================================ 统计

def risk_overview(db: Session, *, hours: int = 24) -> dict:
    """风控总览（后台看板）。"""
    from apps.api.models import AdminAuditLog
    since = _naive_now() - dt.timedelta(hours=max(1, min(hours, 720)))

    alerts = db.scalars(select(AdminAuditLog)
                        .where(AdminAuditLog.action.like("risk.%"),
                               AdminAuditLog.created_at >= since)).all()
    by_level: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    for a in alerts:
        lv = (a.reason or "").replace("风险等级：", "")
        by_level[lv] = by_level.get(lv, 0) + 1
        try:
            for h in json.loads(a.detail or "[]"):
                r = h.get("rule", "?")
                by_rule[r] = by_rule.get(r, 0) + 1
        except Exception:
            pass

    # 登录统计
    n_ok = db.scalar(select(func.count()).select_from(LoginRecord)
                     .where(LoginRecord.success == 1,
                            LoginRecord.created_at >= since)) or 0
    n_fail = db.scalar(select(func.count()).select_from(LoginRecord)
                       .where(LoginRecord.success == 0,
                              LoginRecord.created_at >= since)) or 0

    return {
        "window_hours": hours,
        "alerts_total": len(alerts),
        "by_level": by_level,
        "by_rule": by_rule,
        "logins": {"success": int(n_ok), "failed": int(n_fail),
                   "fail_rate": round(n_fail / (n_ok + n_fail) * 100, 1)
                   if (n_ok + n_fail) else 0.0},
    }

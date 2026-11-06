"""内容审核（v0.20.6 A6-6）。

★★★ 为什么必须有：
  这是**生成式 AI 服务的法定合规要求**（《生成式人工智能服务管理暂行办法》
  第 14 条：发现违法内容应当及时采取停止生成、停止传输、消除等措施）。
  也是备案（E1）的必备能力 —— 没有内容审核，备案过不了。

================================ 可插拔 Provider ================================

  ContentModerator（抽象）
     ├─ LocalModerator      ← ★ 本版实现：本地敏感词 + 正则规则（零依赖、零延迟、免费）
     ├─ RemoteModerator     ← 预留：接第三方 API（阿里云内容安全 / 腾讯天御 / OpenAI Moderation）
     └─ ChainModerator      ← ★ 组合器：先跑本地，命中高危直接拦；可疑再送远端

★ 为什么本地优先：
  1. **零延迟**：生成流水线里每一章都要过，网络往返 200ms × 每天几千章 = 明显成本
  2. **零成本**：第三方 API 按调用量计费
  3. **离线可用**：模型服务挂了审核还能跑（合规不能依赖外部可用性）
  4. **兜底**：远端不可用时降级到本地，而不是"审核失败 = 放行"

================================ 四级处置 ================================

  pass    通过
  review  转人工复核（可疑，不阻断）
  warn    告警（记录但不拦，用于观察）
  block   阻断（拒绝生成/发布）

★ 分级依据：**误杀成本 vs 漏放成本**。
  漏放违法内容 → 法律风险；误杀正常创作 → 用户体验。
  所以：明确的违法词 block，模糊的转 review（人工判），
  文学创作中常见的"打架/情欲/犯罪"描写必须区分对待。
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid

from sqlalchemy.orm import Session

log = logging.getLogger("novelforge")

VERDICTS = ("pass", "warn", "review", "block")
_SEVERITY_ORDER = {"pass": 0, "warn": 1, "review": 2, "block": 3}


def worst(a: str, b: str) -> str:
    """取更严重的判定。"""
    return a if _SEVERITY_ORDER.get(a, 0) >= _SEVERITY_ORDER.get(b, 0) else b


# ================================================================ 规则库

# ★ 分级词库。注意：这里刻意**只放明确违规**的样本，
#   文学创作中大量"灰色"表达（打斗、暧昧、悬疑凶案）不进阻断库 ——
#   否则正常小说无法创作。灰色部分交给"观察词"转 review。
BLOCK_PATTERNS: list[tuple[str, str]] = [
    # (正则, 类别)
    (r"(\d{1,3}\s*[·]\s*\d{1,3})", "政治敏感"),          # 敏感数字组合
    (r"(制毒|製毒|冰毒配方|炸药制作|土制炸弹|枪支改装)", "违法教程"),
    (r"(开票|代开|包过|办证|假证|买卖身份证)", "违法服务"),
    (r"(幼女|萝莉{1,3}色情|儿童色情)", "未成年人色情"),
    (r"(杀.{0,4}全家|血洗|屠城.{0,3}教程)", "暴力教唆"),
    (r"(赌博平台|博彩网站|时时彩.{0,3}代理|洗钱)", "赌博洗钱"),
]

REVIEW_PATTERNS: list[tuple[str, str]] = [
    # 仅在**高密度**时转人工（见下方 density 判定）
    (r"(自杀|自残|割腕|跳楼)", "自伤风险"),
    (r"(毒品|吸毒|大麻|摇头丸)", "涉毒"),
    (r"(裸照|性器官|淫秽|做爱|性交)", "色情描写"),
    (r"(恐怖袭击|爆炸装置|制造炸弹)", "暴恐"),
]

# ★ 观察词（warn）：只记录，不拦。用于观察生成质量趋势。
WARN_PATTERNS: list[tuple[str, str]] = [
    (r"(习近平|毛泽东|邓小平|国家主席|总书记)", "政治人物提及"),
    (r"(台独|港独|疆独|藏独|法轮功)", "敏感政治表述"),
]

# ★ 白名单：命中这些上下文则**降级**（不是直接放行）为人工复核
#   例：「他在小说里写了一个制毒的反派」—— 创作讨论，不是教程
#   ★ 注意必须是**单个正则字符串**（用 | 连接），不是元组 —— 踩过坑
CONTEXT_WHITELIST = (
    r"小说|剧情|情节|角色|人物|反派|设定|架空|虚构|描写|桥段|剧本|"
    r"写作|创作|故事里|文中|书里|科幻|武侠|悬疑"
)

# ★ 审核自检：同一段文本里 review 类命中的**密度**阈值
#   单次出现「自杀」可能是剧情，出现 5 次以上可能是真的有害内容
DENSITY_THRESHOLD = 5


class ModerationResult:
    """审核结果。"""

    __slots__ = ("verdict", "categories", "hits", "provider", "elapsed_ms", "note")

    def __init__(self, verdict: str = "pass", categories: list[str] | None = None,
                 hits: list[dict] | None = None, provider: str = "local",
                 elapsed_ms: float = 0.0, note: str = ""):
        self.verdict = verdict
        self.categories = categories or []
        self.hits = hits or []
        self.provider = provider
        self.elapsed_ms = elapsed_ms
        self.note = note

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "categories": self.categories,
                "hits": self.hits, "provider": self.provider,
                "elapsed_ms": round(self.elapsed_ms, 2), "note": self.note}

    @property
    def blocked(self) -> bool:
        return self.verdict == "block"

    @property
    def needs_review(self) -> bool:
        return self.verdict in ("review", "block")


# ================================================================ Provider

class ContentModerator:
    """审核器抽象。"""

    name = "base"

    def moderate(self, text: str, *, stage: str = "generated",
                 context: dict | None = None) -> ModerationResult:
        """审核一段文本。stage: generated（生成后）| input（用户输入）| publish（发布前）"""
        raise NotImplementedError


class LocalModerator(ContentModerator):
    """本地规则审核（零依赖、零延迟、免费）。

    ★ 实现要点：
      1. 正则预编译（每章几千字，逐条 compile 会慢 10 倍以上）
      2. 上下文白名单降级（创作讨论不算违规）
      3. 密度判定（review 类单次出现可能是剧情，密集出现才转人工）
    """

    name = "local"

    def __init__(self):
        self._block = [(re.compile(p, re.I), c) for p, c in BLOCK_PATTERNS]
        self._review = [(re.compile(p, re.I), c) for p, c in REVIEW_PATTERNS]
        self._warn = [(re.compile(p, re.I), c) for p, c in WARN_PATTERNS]
        self._wl = re.compile(CONTEXT_WHITELIST, re.I)
        # 简单缓存（同一章重复审核时命中）
        self._cache: dict[str, ModerationResult] = {}
        self._cache_max = 500

    def _has_context(self, text: str, pos: int, span: int = 80) -> bool:
        """检查命中位置附近是否创作语境。"""
        lo = max(0, pos - span)
        hi = min(len(text), pos + span)
        return bool(self._wl.search(text[lo:hi]))

    def moderate(self, text: str, *, stage: str = "generated",
                 context: dict | None = None) -> ModerationResult:
        t0 = time.time()
        if not text:
            return ModerationResult("pass", provider=self.name)

        ck = f"{stage}:{hash(text)}"
        if ck in self._cache:
            return self._cache[ck]

        hits: list[dict] = []
        verdict = "pass"

        # ---- block 类：明确违规，直接拦（不受白名单影响太多）----
        for rx, cat in self._block:
            for m in rx.finditer(text):
                # ★ 创作讨论降级（例：小说里写反派制毒的情节）
                if self._has_context(text, m.start()):
                    hits.append({"category": cat, "level": "review",
                                 "sample": _safe_sample(text, m.start(), m.end()),
                                 "reason": "创作语境，降级为人工复核"})
                    verdict = worst(verdict, "review")
                else:
                    hits.append({"category": cat, "level": "block",
                                 "sample": _safe_sample(text, m.start(), m.end())})
                    verdict = worst(verdict, "block")

        # ---- review 类：密度判定 ----
        review_counts: dict[str, int] = {}
        for rx, cat in self._review:
            ms = list(rx.finditer(text))
            if not ms:
                continue
            review_counts[cat] = len(ms)
            lvl = "review" if len(ms) >= DENSITY_THRESHOLD else "warn"
            hits.append({"category": cat, "level": lvl, "count": len(ms),
                         "sample": _safe_sample(text, ms[0].start(), ms[0].end()),
                         "reason": (f"密度 {len(ms)} 次 ≥ 阈值 {DENSITY_THRESHOLD}，转人工"
                                    if lvl == "review" else f"出现 {len(ms)} 次（观察）")})
            verdict = worst(verdict, lvl)

        # ---- warn 类：只记录 ----
        for rx, cat in self._warn:
            ms = list(rx.finditer(text))
            if ms:
                hits.append({"category": cat, "level": "warn", "count": len(ms),
                             "sample": _safe_sample(text, ms[0].start(), ms[0].end())})
                verdict = worst(verdict, "warn")

        res = ModerationResult(verdict, sorted({h["category"] for h in hits}),
                               hits, self.name, (time.time() - t0) * 1000)
        if len(self._cache) < self._cache_max:
            self._cache[ck] = res
        return res


class RemoteModerator(ContentModerator):
    """第三方审核 API（**骨架** —— 拿到凭证后填 3 处）。

    支持目标（按需要实现其一）：
      · 阿里云内容安全（绿网）：https://help.aliyun.com/zh/live/...  国内合规首选
      · 腾讯天御：https://cloud.tencent.com/product/tms
      · OpenAI Moderation：https://platform.openai.com/docs/guides/moderation （免费）
    """

    name = "remote"

    def __init__(self, provider: str = "", endpoint: str = "", key: str = "",
                 secret: str = ""):
        self.provider = provider or ""
        self.endpoint = endpoint or ""
        self.key = key or ""
        self.secret = secret or ""

    def moderate(self, text: str, *, stage: str = "generated",
                 context: dict | None = None) -> ModerationResult:
        if not self.endpoint or not self.key:
            raise RuntimeError(
                "远端审核未配置（需 NF_MODERATION_ENDPOINT + NF_MODERATION_KEY）。"
                "配置方法见 docs/A6_安全加固方案.md §六")
        # TODO(拿到凭证后实现)：
        #   1. 构造请求（阿里云需 HMAC 签名，腾讯需 TC3 签名，OpenAI 只需 Bearer）
        #   2. POST 到 self.endpoint
        #   3. 把返回的 label/score 映射到 verdict
        raise RuntimeError("远端审核尚未实现（等待凭证）")


class ChainModerator(ContentModerator):
    """★ 组合审核器：本地优先，可疑再送远端。

    策略：
      · 本地判 block → 直接拦（不浪费远端调用）
      · 本地判 review/warn → 送远端复核（如果配了）
      · 本地判 pass → 直接过（省远端费用）
      · 远端异常 → 降级用本地结果（**可用性优先**，但记 error 日志）
    """

    name = "chain"

    def __init__(self, local: ContentModerator | None = None,
                 remote: ContentModerator | None = None):
        self.local = local or LocalModerator()
        self.remote = remote

    def moderate(self, text: str, *, stage: str = "generated",
                 context: dict | None = None) -> ModerationResult:
        local = self.local.moderate(text, stage=stage, context=context)
        if local.verdict == "block" or self.remote is None:
            return local
        if local.verdict == "pass":
            return local
        # 本地可疑 → 远端复核
        try:
            remote = self.remote.moderate(text, stage=stage, context=context)
            # 取更严重的
            v = worst(local.verdict, remote.verdict)
            cats = sorted(set(local.categories) | set(remote.categories))
            return ModerationResult(v, cats, local.hits + remote.hits,
                                    f"{self.local.name}+{self.remote.name}",
                                    local.elapsed_ms + remote.elapsed_ms,
                                    note="本地+远端联合判定")
        except Exception as e:                      # noqa: BLE001
            log.error("远端审核失败，降级用本地结果：%s: %s", type(e).__name__, e)
            return local


def _safe_sample(text: str, lo: int, hi: int, pad: int = 12) -> str:
    """截取命中片段（★ 脱敏：只留少量上下文，避免日志里存违规全文）。"""
    a = max(0, lo - pad)
    b = min(len(text), hi + pad)
    s = text[a:b].replace("\n", " ")
    return s[:60]


# ================================================================ 工厂

_moderator: ContentModerator | None = None


def get_moderator() -> ContentModerator:
    """取审核器单例（按配置组装）。"""
    global _moderator
    if _moderator is not None:
        return _moderator
    import os

    def _e(k: str, d: str = "") -> str:
        return os.environ.get(k, "") or d

    local = LocalModerator()
    ep, key = _e("NF_MODERATION_ENDPOINT"), _e("NF_MODERATION_KEY")
    if ep and key:
        remote = RemoteModerator(provider=_e("NF_MODERATION_PROVIDER", "generic"),
                                 endpoint=ep, key=key, secret=_e("NF_MODERATION_SECRET"))
        _moderator = ChainModerator(local, remote)
        log.info("内容审核：本地规则 + 远端 API（%s）", remote.provider)
    else:
        _moderator = local
        log.info("内容审核：仅本地规则（未配置远端 API）")
    return _moderator


def reset_moderator() -> None:
    """重置单例（测试用）。"""
    global _moderator
    _moderator = None


# ================================================================ 记录与查询

def record_review(db: Session, *, text_id: str, stage: str, result: ModerationResult,
                  user_id: str = "", book_id: str = "") -> str:
    """★ 审核记录落库（合规要求：审核过程可追溯）。

    用 ContentFlag 表存（复用，避免多一张表）：
      category 前缀 "mod." + 首个命中的类别
      note 存审核结果 JSON
    """
    if result.verdict in ("pass",):
        return ""
    from apps.api.models import ContentFlag, now as _now
    rid = f"mod_{uuid.uuid4().hex[:16]}"
    cat = (result.categories or ["unknown"])[0]
    try:
        db.add(ContentFlag(
            id=rid, book_id=book_id or "", category=f"mod.{cat}",
            severity="block" if result.blocked else "warn",
            note=json.dumps({"stage": stage, "text_id": text_id,
                             "verdict": result.verdict,
                             "categories": result.categories,
                             "provider": result.provider,
                             "hits": result.hits[:5]},
                            ensure_ascii=False)[:2048],
            status="open" if result.blocked else ("hidden" if result.needs_review else "cleared"),
            source="system", reporter_id=user_id or "", created_at=_now()))
        db.commit()
    except Exception as e:                          # noqa: BLE001
        log.error("审核记录写入失败：%s: %s", type(e).__name__, e)
        try:
            db.rollback()
        except Exception:
            pass
    return rid


def moderate_and_record(db: Session, text: str, *, stage: str = "generated",
                        user_id: str = "", book_id: str = "",
                        text_id: str = "") -> ModerationResult:
    """审核 + 记录（业务侧统一入口）。"""
    m = get_moderator()
    try:
        res = m.moderate(text, stage=stage)
    except Exception as e:                          # noqa: BLE001
        # ★ 审核器故障时**降级放行**而不是阻断（可用性优先），但必须告警
        log.error("★ 内容审核执行失败，已降级放行：%s: %s", type(e).__name__, e)
        res = ModerationResult("pass", provider="fallback",
                              note=f"审核器异常：{type(e).__name__}")
    if res.verdict != "pass":
        record_review(db, text_id=text_id or uuid.uuid4().hex[:12], stage=stage,
                      result=res, user_id=user_id, book_id=book_id)
    return res


def stats() -> dict:
    """审核器状态（后台观测）。"""
    m = get_moderator()
    return {"provider": m.name,
            "local_rules": {"block": len(BLOCK_PATTERNS),
                            "review": len(REVIEW_PATTERNS),
                            "warn": len(WARN_PATTERNS)},
            "density_threshold": DENSITY_THRESHOLD,
            "remote_configured": isinstance(m, ChainModerator)}

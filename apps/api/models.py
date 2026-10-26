"""状态中枢的数据模型。

设计原则：小说世界是「数据」，不是聊天记录。
workspace_id 为未来多租户预留，当前恒为 default。
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import DateTime, Float, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db import Base


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def jd(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def jl(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class ModelProfile(Base):
    """配置中心：模型插槽。走云端 API，OpenAI 兼容协议为主。"""
    __tablename__ = "model_profiles"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    provider: Mapped[str] = mapped_column(String(32), default="openai-compatible")
    base_url: Mapped[str] = mapped_column(String(512), default="https://api.deepseek.com/v1")
    api_key: Mapped[str] = mapped_column(Text, default="")
    model_id: Mapped[str] = mapped_column(String(128), default="deepseek-chat")
    context_window: Mapped[int] = mapped_column(Integer, default=128000)
    price_in: Mapped[float] = mapped_column(Float, default=2.0)   # 元 / 百万 input tokens
    price_out: Mapped[float] = mapped_column(Float, default=8.0)  # 元 / 百万 output tokens
    temperature: Mapped[float] = mapped_column(Float, default=0.85)
    max_tokens: Mapped[int] = mapped_column(Integer, default=8192)
    # 角色路由：planner(大纲/任务单) / writer(正文) / critic(判定与摘要)
    role: Mapped[str] = mapped_column(String(32), default="default")
    is_default: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Book(Base):
    __tablename__ = "books"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    author: Mapped[str] = mapped_column(String(128), default="")
    genre: Mapped[str] = mapped_column(String(256), default="")
    platform: Mapped[str] = mapped_column(String(64), default="generic")
    synopsis: Mapped[str] = mapped_column(Text, default="")
    # 0 = 未设定。目标字数/单章字数必须由用户（或立项确认）显式给出，
    # 不允许建书时就用写死的"100 万 / 2000-2200"冒充用户决策。
    target_words: Mapped[int] = mapped_column(Integer, default=0)
    chapter_min: Mapped[int] = mapped_column(Integer, default=0)
    chapter_max: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="active")  # active|archived|completed
    owner_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    quality_score: Mapped[int] = mapped_column(Integer, default=0)      # 仪表盘缓存评分
    # 质量技能开关：低于 polish_threshold 分时自动调用 chapter_polish 精修
    polish_enabled: Mapped[int] = mapped_column(Integer, default=1)
    polish_threshold: Mapped[int] = mapped_column(Integer, default=90)
    repo_path: Mapped[str] = mapped_column(String(512), default="")
    # v0.6：采纳章节时自动把高频关键人物补进人物清单
    auto_char_enabled: Mapped[int] = mapped_column(Integer, default=1)
    # v0.6：采纳章节后自动跑内容审计，低于 audit_threshold 分告警
    audit_enabled: Mapped[int] = mapped_column(Integer, default=1)
    audit_threshold: Mapped[int] = mapped_column(Integer, default=80)
    # v0.14 D2b 滚动剧情摘要：每章采纳后由 critic 模型压缩更新，
    # 作为写作上下文的第 10 包注入——解决「摘要链只有逐章流水、没有全局剧情走向」问题
    rolling_summary: Mapped[str] = mapped_column(Text, default="")
    # ★ v0.22.1 C1 三级大纲：小说级结构化大纲（JSON dict）。
    #   字段：{logline, theme, conflict, arc, acts:[{name,desc,ch_start,ch_end}]}
    #   为什么结构化而非纯文本：卷/章大纲要能"挂在"它下面做一致性检查，
    #   纯文本无法建立层级关系。
    outline_meta: Mapped[str] = mapped_column(Text, default="")
    # v0.15 题材解耦（用户已确认的产品决策）：
    #   theme = 主题材（**单选**）→ 决定度量衡与实力体系。必须唯一，
    #           两套度量衡冲突会让数值账本记账错乱（如"灵石"与"人民币"混记）。
    #           取值 = genres.GENRES 的 key（xianxia / urban / urban_suspense…）。
    #   tags  = 标签（**多选**，逗号分隔）→ 只影响提示词措辞（风格/封面/人设语气），
    #           一本书可以同时是"都市 + 悬疑"。
    # 注意：genre 字段保留为「自由文本分类」（用户随手写的类型），theme/tags 才是
    # 结构化的题材包引用；两者并存，避免破坏既有数据。
    theme: Mapped[str] = mapped_column(String(64), default="")
    tags: Mapped[str] = mapped_column(String(512), default="")
    # ★ v0.20.5 A5 管理后台：内容处置状态（与创作状态 status 语义分离）。
    #   normal = 正常；hidden = 违规隐藏（作者可见，读者不可见）
    content_status: Mapped[str] = mapped_column(String(16), default="normal", index=True)
    flagged: Mapped[int] = mapped_column(Integer, default=0)     # 是否有待处理标记
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Volume(Base):
    __tablename__ = "volumes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    idx: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(256), default="")
    outline: Mapped[str] = mapped_column(Text, default="")
    start_ch: Mapped[int] = mapped_column(Integer, default=0)
    end_ch: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    # ★ v0.22.1 C1 卷级结构化大纲（JSON）：
    #   {goal, conflict, turning_points:[...], key_events:[...], ends_with}
    outline_meta: Mapped[str] = mapped_column(Text, default="")


class Chapter(Base):
    __tablename__ = "chapters"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    volume_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    idx: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(256), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    words: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="final")  # draft|gated|final
    # ★ v0.22.1 C1 章节级大纲（写作前的大纲，与正文分离）
    outline: Mapped[str] = mapped_column(Text, default="")
    # ★ v0.22.1 C1 章节结构化大纲（JSON）：
    #   {purpose, conflict, twist, hook, scenes:[...]}
    outline_meta: Mapped[str] = mapped_column(Text, default="")
    gate_score: Mapped[int] = mapped_column(Integer, default=0)
    gate_report: Mapped[str] = mapped_column(Text, default="")
    model_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Character(Base):
    __tablename__ = "characters"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    role: Mapped[str] = mapped_column(String(64), default="")
    profile: Mapped[str] = mapped_column(Text, default="")
    voice: Mapped[str] = mapped_column(Text, default="")     # 口癖 / 说话方式
    arc: Mapped[str] = mapped_column(Text, default="")       # 成长弧
    first_chapter: Mapped[int] = mapped_column(Integer, default=0)
    state_now: Mapped[str] = mapped_column(Text, default="")  # 当前状态（写章时更新）
    # v0.6：写作推进中被自动识别补入的关键人物
    is_auto: Mapped[int] = mapped_column(Integer, default=0)
    mentions: Mapped[int] = mapped_column(Integer, default=0)  # 累计出现次数
    aliases: Mapped[str] = mapped_column(Text, default="")     # JSON list，别称/称呼
    # v0.14 D2 观察者证据链：state_now 最后一次有依据的更新来自哪一章（0=无来源）
    state_src_ch: Mapped[int] = mapped_column(Integer, default=0)
    # v0.14.10 D19 音色资产化：角色的声纹档案（JSON 串，见 voice_cast.VoiceProfile）。
    # 便于把一个角色的音色复用到其它项目/其它衍生形态。
    voice_profile: Mapped[str] = mapped_column(Text, default="")
    # ★ v0.21.5 B5 角色资产库：
    #   ref_images      = 多张参考图（JSON list，短剧生成时注入，解决"五官漂移"）
    #   appearance_locks= 跨镜锁定的外观特征（JSON dict：发型/瞳色/服饰…）
    #   为什么锁特征：纯靠参考图仍会漂（不同角度/光照），
    #   把关键特征写成**文字断言**同时注入 prompt，双保险。
    ref_images: Mapped[str] = mapped_column(Text, default="")        # JSON list
    appearance_locks: Mapped[str] = mapped_column(Text, default="")  # JSON dict


class WorldSetting(Base):
    __tablename__ = "world_settings"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    category: Mapped[str] = mapped_column(String(64), default="")  # 境界|势力|货币|规则|地理
    key: Mapped[str] = mapped_column(String(256), default="")
    value: Mapped[str] = mapped_column(Text, default="")
    # v0.14 D2 观察者证据链：该条设定最后修改时依据的章号（0=立项期设定，无章节依据）
    src_ch: Mapped[int] = mapped_column(Integer, default=0)


class Foreshadow(Base):
    __tablename__ = "foreshadows"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    planted_ch: Mapped[int] = mapped_column(Integer, default=0)
    target_vol: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="open")  # open|doing|done
    payoff_ch: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")
    # v0.14 D2c 伏笔债务：应付章号（0=按 target_vol*30 推导），提醒分级缓存
    due_ch: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[str] = mapped_column(String(16), default="normal")  # high|normal|low


class TimelineEvent(Base):
    """纪年表：把小说里的时间轴钉死，防止前后时间线打架。

    seq 为自增序号（同一 ch_idx 内按插入顺序），time_label 是可读时间标记，
    sort_key 是可排序的数值锚点（如「开元三年」→ 3，「十六岁那年」→ 16），
    用于做时间单调性校验。
    """
    __tablename__ = "timeline_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    ch_idx: Mapped[int] = mapped_column(Integer, default=0)      # 0 表示前置/背景设定
    era: Mapped[str] = mapped_column(String(128), default="")    # 纪元/年号，如 开元
    time_label: Mapped[str] = mapped_column(String(256), default="")  # 如 开元三年春
    sort_key: Mapped[float] = mapped_column(Float, default=0.0)  # 可排序锚点
    title: Mapped[str] = mapped_column(String(256), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(32), default="事件")  # 出生|晋级|战斗|转折|日常|事件
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class LedgerEntry(Base):
    """度量衡账本：金钱 / 经验 / 年龄 / 功法 / 级别 等数值的累计增减流水。

    balance 由 delta 累加推导（写入时重算），保证「上一笔余额 + 本次变动 = 本次余额」，
    一致性检查就是验证这条等式，并拿正文里扫出来的数值跟余额对账。
    """
    __tablename__ = "ledger_entries"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # seq 是真正的记账顺序。id 是「lg_随机hex」，按 id 排序等于按 uuid 排序，
    # 拿"最后一笔"会拿到随机一笔，余额直接算错 —— 必须按 seq 排。
    seq: Mapped[int] = mapped_column(Integer, default=0, index=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    ch_idx: Mapped[int] = mapped_column(Integer, default=0)
    subject: Mapped[str] = mapped_column(String(128), default="")  # 主体：人物名 / 势力名
    metric: Mapped[str] = mapped_column(String(64), default="")    # 金钱|经验|年龄|功法|级别|声望|其他
    delta: Mapped[float] = mapped_column(Float, default=0.0)       # 本次增减（可为负）
    balance: Mapped[float] = mapped_column(Float, default=0.0)     # 变动后余额
    unit: Mapped[str] = mapped_column(String(32), default="")      # 枚|两|年|级|层|点
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class AuditReport(Base):
    """内容审计留档：去AI味 / 逻辑 / 内容 / 世界观 / 大纲遵从。"""
    __tablename__ = "audit_reports"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    scope: Mapped[str] = mapped_column(String(32), default="chapter")  # chapter | book
    ch_idx: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    report: Mapped[str] = mapped_column(Text, default="")   # JSON
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class StyleProfile(Base):
    """风格 DNA + 写作纪律。一本书一条。"""
    __tablename__ = "style_profiles"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    pov: Mapped[str] = mapped_column(String(64), default="第一人称")
    tone: Mapped[str] = mapped_column(Text, default="")
    banned_words: Mapped[str] = mapped_column(Text, default="")   # JSON list
    max_para_chars: Mapped[int] = mapped_column(Integer, default=120)
    rules: Mapped[str] = mapped_column(Text, default="")          # 写作纪律正文
    sample: Mapped[str] = mapped_column(Text, default="")         # 范文片段（锚定文风）


class Run(Base):
    """每次执行的完整档案：可复现 + 成本可算。"""
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    chapter_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    kind: Mapped[str] = mapped_column(String(64), default="chapter")  # chapter|outline|brief|polish
    model_id: Mapped[str] = mapped_column(String(64), default="")
    prompt_version: Mapped[str] = mapped_column(String(64), default="")
    context_snapshot: Mapped[str] = mapped_column(Text, default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    gate_score: Mapped[int] = mapped_column(Integer, default=0)
    gate_report: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="ok")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class User(Base):
    """账号体系：取代 nginx 基础认证，支撑多用户与管理员后台。"""
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(256), default="")
    # 密码以 pbkdf2$<salt_b64>$<hash_b64> 形式存储，见 apps/api/auth.py
    pw_hash: Mapped[str] = mapped_column(String(256), default="")
    role: Mapped[str] = mapped_column(String(16), default="user")  # user | admin
    is_active: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    # ---- v0.20.4 A4 用户中心 ----
    nickname: Mapped[str] = mapped_column(String(64), default="")        # 昵称（显示名）
    avatar: Mapped[str] = mapped_column(String(256), default="")         # 头像路径（相对 data/）
    bio: Mapped[str] = mapped_column(Text, default="")                   # 个人简介
    phone: Mapped[str] = mapped_column(String(32), default="", index=True)  # 手机号
    phone_verified: Mapped[int] = mapped_column(Integer, default=0)
    email_verified: Mapped[int] = mapped_column(Integer, default=0)
    # ★ 二次验证密钥：**绝不存明文**。用 keyvault 加密（与 API Key 同机制）
    totp_secret: Mapped[str] = mapped_column(String(256), default="")
    totp_enabled: Mapped[int] = mapped_column(Integer, default=0)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_ip: Mapped[str] = mapped_column(String(64), default="")
    # ★ 软删除：注销后账号不可登录，但**行保留**（财务记录需关联）
    #   为什么不用硬删除：订单/支付事件属会计凭证，法律要求留存（通常 5 年）
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    # ★ 改密后旧令牌失效：记录"令牌生效起始时间"，早于它签发的令牌一律拒绝
    token_valid_after: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # ★ v1.0-E4.11（合规）：用户协议/隐私政策同意时间戳。
    #   E1 备案审查项：注册时必须记录"同意协议"的证明（谁、何时同意了哪一版）。
    #   老用户此列为 NULL —— 只约束新注册，不回填。
    agreed_terms_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    agreed_terms_version: Mapped[str] = mapped_column(String(32), default="")


class Conversation(Base):
    """对话线程：一本书可有多条独立对话（立项讨论 / 大纲脑暴 / 某角色深挖…）。

    会话是「聊天记录」，与小说世界数据（设定/人物/章节）解耦——
    删对话不影响正文，但刷新页面后能完整恢复上下文。
    """
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(256), default="新对话")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Message(Base):
    """对话中的单条消息，按 seq 顺序存储，保证回放与上下文一致。"""
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conv_id: Mapped[str] = mapped_column(String(64), index=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16), default="user")  # user | assistant
    content: Mapped[str] = mapped_column(Text, default="")
    seq: Mapped[int] = mapped_column(Integer, default=0)
    # v0.14.10：本条回复对应的后台任务（代笔任务落稿时写入）。
    # 用于刷新/切对话后的历史回放——之前草稿卡只活在实时流的 DOM 里，
    # 重放时无从得知「这条回复产出了哪一章的稿子」，于是草稿卡永远回不来。
    task_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Task(Base):
    """后台任务台账：代笔 / 全书审计 等长任务的状态中枢。

    设计意图（v0.9.1）：
    - 长任务（代笔一章、全书逐章审计）一旦跑起来，前端刷新不该"失忆"——
      进度、结果、是否中断都必须能从 DB 恢复，否则用户分不清"进行中"还是"已中断"。
    - status: running | done | error | abandoned
      * running：正在执行（刷新后若仍 running，说明进程还在跑，前端可重挂 SSE 流）；
      * abandoned：服务重启/进程消失后，启动时把遗留 running 行改成它（无法续跑，但留下证据）。
    - kind: draft | audit_book（预留其他长任务）。target 存对象（如章号 / 0=全书）。
    - progress：可读的进度文案（"第 3/15 章"）；events_json：逐条过程事件（供 SSE 回放 + 气泡活动流）；
      result_json：结果载荷（draft 存 gate 报告 + 正文指针；audit_book 存平均分 + 各章分数）。
    - 事件按 seq 追加，前端重挂流时带上「已看到的最大 seq」，服务端只补增量，不重放。
    """
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="draft")  # draft | audit_book
    target: Mapped[int] = mapped_column(Integer, default=0)         # 章号；0 表示全书
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|done|error|abandoned
    progress: Mapped[str] = mapped_column(String(128), default="")
    events_json: Mapped[str] = mapped_column(Text, default="")      # JSON list：过程事件
    seq: Mapped[int] = mapped_column(Integer, default=0)            # 事件计数器（增量回放用）
    result_json: Mapped[str] = mapped_column(Text, default="")      # 结果载荷
    error: Mapped[str] = mapped_column(String(512), default="")
    # ★ v0.25 J 组：排队系统字段
    #   priority：越大越先出队（-1000~1000）；lane：三级泳道
    #   worker_id：认领者（回收僵尸任务时用）；queued 状态 = 已入队未执行
    priority: Mapped[int] = mapped_column(Integer, default=0, index=True)
    lane: Mapped[str] = mapped_column(String(16), default="standard")
    worker_id: Mapped[str] = mapped_column(String(64), default="")
    owner_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    enqueued_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=True)


# ============================================================ 短剧任务队列（v0.13.15 C1）
# ArcReel lib/db/models/task.py 的精简移植：批量生成从 asyncio.spawn 升级为
# 数据库队列——行即任务，worker 分道认领执行；保留 dependency 依赖序与
# checkpoint 断点续跑两块 ArcReel 的精髓，去掉多 worker 租约（单进程够用）。

class DramaTask(Base):
    """短剧媒体生成任务行。status: queued|running|done|error|skipped|cancelled。

    - batch_id：同一次「批量生成」提交的所有行共享，用于批次级进度聚合与取消。
    - resource_type/resource_id：目标资源（scene:{id} / asset:{id}），与 task_type
      一起参与去重——同一资源的同一 target 活动中不允许重复入队。
    - dependency_id：前置任务（如视频依赖同分镜图像任务）；前置未 done 时不认领。
    - checkpoint_json：断点续跑快照（payload 冻结 + 重试计数）。
    - payload_json：执行参数（生成所需的上下文指针）。
    """
    __tablename__ = "drama_tasks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    episode: Mapped[int] = mapped_column(Integer, default=0, index=True)
    batch_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    task_type: Mapped[str] = mapped_column(String(16), default="image")  # image|video|audio|asset_image
    resource_type: Mapped[str] = mapped_column(String(16), default="scene")  # scene|asset
    resource_id: Mapped[str] = mapped_column(String(64), default="")
    resource_key: Mapped[str] = mapped_column(String(64), default="")   # 展示用（如 E1S01 / 角色名）
    dependency_id: Mapped[str] = mapped_column(String(64), default="")  # 前置任务 id（空=无依赖）
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued|running|done|error|skipped|cancelled
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)
    payload_json: Mapped[str] = mapped_column(Text, default="")
    checkpoint_json: Mapped[str] = mapped_column(Text, default="")
    result_json: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(String(512), default="")
    ledger_task_id: Mapped[str] = mapped_column(String(64), default="")  # 关联 Task 台账（SSE 进度用）
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class MediaCall(Base):
    """媒体调用成本台账（v0.13.15 C3 提前落表）：每次 run_media 一行。

    est_cost 调用前的模型单价预估；actual_cost 供应商返回的用量结算；
    失败行 error_code 供归因统计（429/超时/内容审查等）。
    """
    __tablename__ = "media_calls"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    task_id: Mapped[str] = mapped_column(String(64), default="")       # DramaTask 或 Task 台账 id
    media_type: Mapped[str] = mapped_column(String(16), default="image")  # image|video|audio
    provider: Mapped[str] = mapped_column(String(32), default="")
    model_id: Mapped[str] = mapped_column(String(128), default="")
    prompt_chars: Mapped[int] = mapped_column(Integer, default=0)
    est_cost: Mapped[float] = mapped_column(Float, default=0.0)
    actual_cost: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="ok")      # ok|error
    error_code: Mapped[str] = mapped_column(String(64), default="")
    error_message: Mapped[str] = mapped_column(String(512), default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


# ============================================================ 短剧工坊（v0.12）
# 复刻 ArcReel 的核心数据模型（简化版，贴合织字工坊既有 SQLAlchemy 风格）：
# - DramaProject：一部小说 → 一个短剧项目。episode_plan 按 ArcReel episodes[] 语义。
# - DramaEpisode：分集。每集绑定一本书的某章区间，持有剧本 JSON。
# - DramaAsset：角色/场景/道具三类资产（ArcReel characters/scenes/props 的合表），
#   description 即 ArcReel 的 description 字段，image 即 character_sheet 成品图。
# - DramaScene：分镜单元（ArcReel 的 script scenes[]），每条含台词/画面/视频提示词
#   与 generated_assets 产物状态。

class DramaProject(Base):
    __tablename__ = "drama_projects"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    style: Mapped[str] = mapped_column(Text, default="")            # 画风/风格串（进所有图像提示词）
    content_mode: Mapped[str] = mapped_column(String(16), default="drama")     # narration|drama|ad
    generation_mode: Mapped[str] = mapped_column(String(16), default="storyboard")  # storyboard|reference
    aspect_ratio: Mapped[str] = mapped_column(String(8), default="9:16")
    episode_plan: Mapped[str] = mapped_column(Text, default="")     # JSON list [{episode,title,src_from,src_to,units}]
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|archived
    owner_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class DramaEpisode(Base):
    __tablename__ = "drama_episodes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    episode: Mapped[int] = mapped_column(Integer, default=0)        # 集 ID（≥1，按播出顺序）
    title: Mapped[str] = mapped_column(String(256), default="")
    src_from: Mapped[int] = mapped_column(Integer, default=0)       # 起始章号（含）
    src_to: Mapped[int] = mapped_column(Integer, default=0)         # 结束章号（含）
    source_text: Mapped[str] = mapped_column(Text, default="")      # 冻结的源文（切集时快照）
    script_json: Mapped[str] = mapped_column(Text, default="")      # 剧本 JSON（scenes[]，ArcReel 结构）
    script_plan: Mapped[str] = mapped_column(Text, default="")      # 剧本草稿（未确认）
    plan_confirmed: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="empty")  # empty|planned|scripted|boarded|rendered
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class DramaAsset(Base):
    __tablename__ = "drama_assets"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="character")  # character|scene|prop
    name: Mapped[str] = mapped_column(String(128), default="")
    description: Mapped[str] = mapped_column(Text, default="")      # 外观/环境描述（提示词来源）
    voice_style: Mapped[str] = mapped_column(String(256), default="")
    image: Mapped[str] = mapped_column(Text, default="")            # 成品图路径/URL（空=未生成）
    image_status: Mapped[str] = mapped_column(String(16), default="none")  # none|pending|done|error
    first_seen: Mapped[int] = mapped_column(Integer, default=0)     # 首次出现集号
    # ---- v0.13.15 C2：资产衍生（ArcReel AssetDerivative 语义）----
    # 同一角色的多形态：青年/老年、神兽/黄狗。衍生行 parent_id 指向本体；
    # description 只写「相对本体的变化」，出图提示词 = 本体描述 + 衍生变化。
    parent_id: Mapped[str] = mapped_column(String(64), default="", index=True)  # 空=本体
    form_name: Mapped[str] = mapped_column(String(64), default="")  # 形态名（青年/老年/神兽/黄狗…）
    # v0.14.10 D19 音色资产化：声纹档案 JSON（voice/rate/pitch/style/traits/locked）。
    # 空 = 尚未分配，由 voice_cast.assign_voices 自动补。同一角色所有衍生形态
    # 默认共用本体的音色（见 voice_cast.load_cast_from_db 的「衍生归本体」逻辑）。
    voice_profile: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class DramaScene(Base):
    """分镜单元。对应 ArcReel 剧本 JSON 里 scenes[] 的一项，落库以便逐条生成与追踪。"""
    __tablename__ = "drama_scenes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    episode: Mapped[int] = mapped_column(Integer, default=0, index=True)
    scene_id: Mapped[str] = mapped_column(String(32), default="")   # E1S01
    idx: Mapped[int] = mapped_column(Integer, default=0)            # 集内顺序
    duration_seconds: Mapped[int] = mapped_column(Integer, default=8)
    segment_break: Mapped[int] = mapped_column(Integer, default=0)
    characters_in_scene: Mapped[str] = mapped_column(Text, default="")  # JSON list
    scene_description: Mapped[str] = mapped_column(Text, default="")
    utterances: Mapped[str] = mapped_column(Text, default="")       # JSON list [{kind,speaker,text}]
    image_prompt: Mapped[str] = mapped_column(Text, default="")     # JSON {scene,composition{shot_type,lighting,ambiance}}
    video_prompt: Mapped[str] = mapped_column(Text, default="")     # JSON {action,camera_motion,ambiance_audio}
    media_status: Mapped[str] = mapped_column(String(16), default="none")  # none|image_pending|image_done|video_pending|done|error
    image_path: Mapped[str] = mapped_column(Text, default="")
    video_path: Mapped[str] = mapped_column(Text, default="")
    audio_path: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(String(512), default="")
    # D6 VLM 审图报告（JSON：{pass, scores, issues, ...}；空串=未审）
    qc_report: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


# ============================================================ 长程记忆（v0.14 D1）
# 调研结论（2026-10）：xiehuanyi/NovelForge 实测 400 章/108 万字不崩靠的是
# 「结构化标签过滤」而非纯向量语义；RecurrentGPT 验证「语义检索回捞历史段」必要。
# 本表 = 混合检索的物化层：结构标签（人物/卷/章号）做过滤，稀疏 bigram 向量做语义排序。

class MemoryChunk(Base):
    """章节记忆块：一章正文切成若干块入库，供写作前语义回捞。

    - ch_idx / volume_idx / char_tags：结构标签，过滤比语义更准（人物视角的段落）。
    - vec_json：稀疏向量（中文 bigram 词频 JSON）。零外部依赖，2000 块内 numpy 余弦 <10ms。
      接口按「向量可替换」设计：将来接真嵌入模型时只换 encode/rank 实现，schema 不动。
    - kind：chunk 来源类型（scene=正文切块 / event=纪年事件 / state=人物状态变更），
      检索时可按需加权。
    """
    __tablename__ = "memory_chunks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    ch_idx: Mapped[int] = mapped_column(Integer, default=0, index=True)
    volume_idx: Mapped[int] = mapped_column(Integer, default=0, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="scene")  # scene|event|state
    text: Mapped[str] = mapped_column(Text, default="")      # 块正文（含章号前缀）
    char_tags: Mapped[str] = mapped_column(Text, default="")  # JSON list：出场人物名
    vec_json: Mapped[str] = mapped_column(Text, default="")   # JSON {bigram: count}
    # ★ v0.21.1 B1：稠密向量（int8 量化 + Base64，见 embedding.quantize_to_b64）。
    #   为什么另开一列而不是替换 vec_json：
    #     · vec_json 是**稀疏 bigram**（零依赖兜底），永远可用
    #     · emb 是**稠密语义向量**（需远端 API），配了才有
    #     两者融合排序比单用任一种都准，且任一缺失都能工作 → 真正的降级兜底
    emb: Mapped[str] = mapped_column(Text, default="")
    emb_dim: Mapped[int] = mapped_column(Integer, default=0)
    emb_model: Mapped[str] = mapped_column(String(64), default="")
    words: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


# ============================================================ 题材包（v0.15 题材解耦）
class GenrePack(Base):
    """题材包：把「题材规则与措辞」做成可配置项（配置中心可改，无需改代码）。

    为什么要有这张表（而不是纯代码常量）？
      用户明确要求「题材包放 DB 表，配置中心可改、需配套 UI」——
      新增一个题材（如"民国谍战"）不该走一次发版，运营/作者应能自助维护。

    与 genres.py 的关系（**代码为默认值、DB 为覆盖层**）：
      * genres.py 的 GENRES 是**内置出厂包**，保证系统开箱即用、零依赖启动；
      * 本表是可写覆盖层：key 命中内置包 → 覆盖字段；未命中 → 新增自定义题材。
      * 单条记录损坏 / 表为空 时，genres.py 的兜底逻辑照常工作（零回归）。

    字段语义
      key           题材标识（唯一，如 xianxia / urban）
      label         中文名（UI 显示）
      aliases       别名（JSON list），用于把自由文本 genre 子串匹配到本包
      config_json   完整配置（JSON）：metrics / scan_patterns / realm_dim /
                    realm_stops / style_hint / cover_anchor / prompt_hint
      builtin       1=内置出厂包（可改不可删）；0=自定义
      enabled       1=启用；0=停用（停用后 get_genre 不返回它）
    """
    __tablename__ = "genre_packs"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(128), default="")
    aliases: Mapped[str] = mapped_column(Text, default="")     # JSON list
    config_json: Mapped[str] = mapped_column(Text, default="")  # JSON dict
    builtin: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class GenrePackMarket(Base):
    """题材包市场条目（v0.19.7 F3）。

    为什么单独一张表（而不是给 genre_packs 加列）：
      * genre_packs 是**运行时生效的配置**（get_genre 每次都读）；
        市场是**内容目录**（可能列出一堆没安装的包），两者生命周期不同。
      * 卸载一个市场包 ≠ 删除 genre_packs 记录（可能要保留评分/评论）。
      * 内置包不进市场表（它们随代码发布，不需要"市场条目"）。

    与 genre_packs 的关系：
      market 表 → （安装）→ genre_packs 表 → （生效）→ get_genre

    字段语义
      pack_key     题材标识（对应 genre_packs.key）
      name         市场展示名（可与 label 不同，含版本/风格说明）
      author       作者（发布者标识）
      version      语义化版本，如 1.0.0
      description  一句话简介
      readme_md    README 正文（Markdown）
      tags         JSON list，用于分类检索
      config_json  完整包配置（安装时写入 genre_packs.config_json）
      aliases      JSON list
      rating_sum   评分累计（算平均分用 rating_sum/rating_count）
      rating_count 评分次数
      downloads    下载/安装次数
      installed    1=已安装到本机
      source       local(自建) | imported(导入)
      created_at / updated_at
    """
    __tablename__ = "genre_pack_market"
    pack_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    author: Mapped[str] = mapped_column(String(64), default="")
    version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    description: Mapped[str] = mapped_column(Text, default="")
    readme_md: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[str] = mapped_column(Text, default="")          # JSON list
    config_json: Mapped[str] = mapped_column(Text, default="")
    aliases: Mapped[str] = mapped_column(Text, default="")
    rating_sum: Mapped[int] = mapped_column(Integer, default=0)
    rating_count: Mapped[int] = mapped_column(Integer, default=0)
    downloads: Mapped[int] = mapped_column(Integer, default=0)
    installed: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(16), default="local")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

# ============================================================ 积分计费（v0.20.2 A2）
#
# 设计原则（★ 每一条都对应真实的资损场景）：
#   1. 金额用**分（INT）**，绝不用浮点 —— 浮点算钱必然出错（0.1+0.2≠0.3）
#   2. **流水 append-only**：财务数据只增不改，改余额必须留痕
#   3. **balance 只是缓存**，真相是 sum(ledger.delta) —— 对账靠这个等式
#   4. **ref_id 幂等键**：任务重试/网络重发靠它去重（否则重复扣费）
#   5. 不用 DB 外键（与项目既有约定一致：字符串关联）


class CreditAccount(Base):
    """积分账户（一个用户一个）。

    ★ `balance` 是**缓存值**，不是真相 —— 真相是 `sum(CreditLedger.delta)`。
      之所以冗余存一份，是为了避免每次查余额都扫全表流水。
      对账工具会校验两者的等式（见 credits.reconcile）。
    """
    __tablename__ = "credit_accounts"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    balance: Mapped[int] = mapped_column(Integer, default=0)        # 可用余额
    frozen: Mapped[int] = mapped_column(Integer, default=0)         # 冻结（任务进行中）
    total_earned: Mapped[int] = mapped_column(Integer, default=0)   # 累计获得（统计）
    total_spent: Mapped[int] = mapped_column(Integer, default=0)    # 累计消耗（统计）
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    @property
    def available(self) -> int:
        """可用余额 = 余额 - 冻结。扣费判断必须用这个，不能用 balance。"""
        return max(0, int(self.balance or 0) - int(self.frozen or 0))


class CreditLedger(Base):
    """积分流水（★ append-only，永不修改/删除）。

    kind 取值：
      signup        注册赠送
      recharge      充值到账
      consume       消耗
      refund        退款/返还
      freeze        冻结（不动 balance，只动 frozen）
      unfreeze      解冻
      admin_adjust  管理员调整（必须记 operator）
      expire        过期扣减

    ★ ref_id 是**幂等键**：同一 (user_id, kind, ref_id) 只允许有一条记录。

    ★★ v0.20.2 实测发现的关键坑：
      只靠「先 SELECT 检查是否存在，再 INSERT」**在并发下无效** ——
      多个请求会同时查不到、同时插入（检查与插入之间有窗口）。
      实测：同 ref_id 20 线程并发 → 扣了 10 次（只有 10 次被拦住）。

      正确做法：**加数据库唯一约束**，让并发插入在 DB 层被拒绝，
      应用层捕获 IntegrityError 后按"重复"处理。
      这是"数据库约束 > 应用层检查"的经典案例 —— 应用层永远有竞态窗口。
    """
    __tablename__ = "credit_ledger"
    __table_args__ = (
        # ★ 幂等唯一约束（partial index：ref_id 为空的不参与，因为很多流水没有 ref_id）
        Index("uq_credit_ledger_idem", "user_id", "kind", "ref_id",
              unique=True, sqlite_where=text("ref_id != ''"),
              postgresql_where=text("ref_id != ''")),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    delta: Mapped[int] = mapped_column(Integer, default=0)          # 正=入账 负=出账
    balance_after: Mapped[int] = mapped_column(Integer, default=0)  # 记账后余额（对账定位用）
    kind: Mapped[str] = mapped_column(String(32), default="consume", index=True)
    ref_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    operator: Mapped[str] = mapped_column(String(64), default="")   # 管理员操作留痕
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)


class Plan(Base):
    """套餐定义（★ 配置化，不硬编码在代码里）。

    price_cents 用**分**：¥39.00 → 3900。
    features 是 JSON：{"drama": 1, "api": 0, "concurrency": 2, ...}
    """
    __tablename__ = "plans"
    key: Mapped[str] = mapped_column(String(32), primary_key=True)   # free|basic|pro|max
    name: Mapped[str] = mapped_column(String(64), default="")
    price_cents: Mapped[int] = mapped_column(Integer, default=0)     # 单位：分
    period: Mapped[str] = mapped_column(String(16), default="monthly")  # once|monthly|yearly
    credits: Mapped[int] = mapped_column(Integer, default=0)         # 含积分数
    features: Mapped[str] = mapped_column(Text, default="")          # JSON
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class Order(Base):
    """订单（本版先建表 + 内部充值路径；真实支付在 A3 接入）。

    状态机：pending → paid → (refunded)
                    ↘ failed / canceled
    """
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    plan_key: Mapped[str] = mapped_column(String(32), default="")
    kind: Mapped[str] = mapped_column(String(16), default="plan")    # plan|topup
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    credits: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    pay_channel: Mapped[str] = mapped_column(String(16), default="manual")  # manual|alipay|wechat|stripe
    trade_no: Mapped[str] = mapped_column(String(128), default="")   # 第三方流水号
    note: Mapped[str] = mapped_column(Text, default="")
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)

class PaymentEvent(Base):
    """支付事件（v0.20.3 A3）。

    ★ 为什么必须存原始报文（raw_body）：
      支付纠纷时，**平台方推送的报文是唯一证据**。
      不存的话，事后无法自证"我收到的金额就是 ¥299"。
      这是财务系统的通行做法 —— 原始凭证不可丢。

    ★ signature_ok 也要存：验签失败的请求是**攻击痕迹**，
      保留下来可用于风控分析（同一 IP 反复伪造签名 = 恶意）。
    """
    __tablename__ = "payment_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    provider: Mapped[str] = mapped_column(String(16), default="sandbox", index=True)
    event_type: Mapped[str] = mapped_column(String(16), default="notify")  # notify|refund|query
    trade_no: Mapped[str] = mapped_column(String(128), default="", index=True)
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    raw_body: Mapped[str] = mapped_column(Text, default="")      # ★ 原始报文（唯一凭据）
    signature_ok: Mapped[int] = mapped_column(Integer, default=0)  # 验签结果（留痕）
    handled: Mapped[int] = mapped_column(Integer, default=0)       # 是否已处理
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)

# ============================================================ 用户中心（v0.20.4 A4）

class VerificationCode(Base):
    """验证码（手机短信 / 邮箱）。

    ★ 安全要点：
      · **存哈希不存明文**（防库被读后直接拿验证码登录）
      · 有**尝试次数上限**（防暴力猜 6 位码 —— 100 万空间可穷举）
      · 有**过期时间**（通常 5-10 分钟）
      · 有**频率限制**（同 target 60 秒内只能发一次）
    """
    __tablename__ = "verification_codes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target: Mapped[str] = mapped_column(String(160), index=True)   # 手机号或邮箱
    code_hash: Mapped[str] = mapped_column(String(128), default="")  # ★ 哈希，非明文
    purpose: Mapped[str] = mapped_column(String(16), default="login",
                                         index=True)  # login|reset|bind|verify
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    used: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)      # ★ 尝试次数
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)


class LoginRecord(Base):
    """登录记录（安全审计）。

    ★ 为什么必须记：用户能看到"谁在什么时候从哪里登录"，
      发现异常可及时改密 —— 这是账号安全的基础能力（也是合规要求）。
    """
    __tablename__ = "login_records"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    ua: Mapped[str] = mapped_column(String(256), default="")
    method: Mapped[str] = mapped_column(String(16), default="password")  # password|sms|email|token
    success: Mapped[int] = mapped_column(Integer, default=1, index=True)
    reason: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)


class AccountDeletion(Base):
    """注销申请（★ 冷静期机制）。

    为什么要有冷静期：
      · 用户误操作 → 作品全没了 → 投诉（不可逆）
      · 账号被盗后被恶意注销 → 无法追回
    做法：申请后 7 天冷静期，期间可撤销；到期才真正执行。
    """
    __tablename__ = "account_deletions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # pending（冷静期中）| completed（已注销）| canceled（已撤销）
    reason: Mapped[str] = mapped_column(Text, default="")
    keep_works: Mapped[int] = mapped_column(Integer, default=1)
    # ★ keep_works=1：保留作品但匿名化；0：一并删除
    requested_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    scheduled_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)  # 到期时间
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class AdminAuditLog(Base):
    """管理操作审计日志（v0.20.5 A5）。

    ★ 为什么管理操作必须留痕：
      · 管理员能封号、调积分、退款 —— 都是"能造成资损/不可逆"的操作
      · 出事时（用户投诉"我的积分没了"）必须能自证"谁在什么时候做了什么"
      · 这也是合规要求（等保/审计）

    ★ 关键设计：**只追加，不修改**（append-only）。
      任何"编辑日志"的能力都会让日志失去证明力。
    """
    __tablename__ = "admin_audit_logs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 操作者（管理员）
    operator_id: Mapped[str] = mapped_column(String(64), index=True)
    operator_name: Mapped[str] = mapped_column(String(64), default="")
    # 操作类型：user.ban / user.unban / credit.adjust / order.refund /
    #          book.flag / book.unflag / config.update / user.role_change ...
    action: Mapped[str] = mapped_column(String(64), index=True)
    # 目标对象
    target_type: Mapped[str] = mapped_column(String(32), default="")   # user|book|order|config
    target_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 变更详情（JSON：{"before": ..., "after": ...}）
    detail: Mapped[str] = mapped_column(Text, default="")
    # 操作原因（★ 强制填写 —— 无原因的敏感操作不可接受）
    reason: Mapped[str] = mapped_column(Text, default="")
    # 请求上下文（追责用）
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(256), default="")
    # ★ result：ok | rejected | error（失败也要记，失败尝试可能是攻击）
    result: Mapped[str] = mapped_column(String(16), default="ok")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)


class ContentFlag(Base):
    """内容标记 / 违规处理（v0.20.5 A5）。

    ★ 为什么单独一张表而不是给 books 加个字段：
      · 一本书可能被多次标记（不同原因、不同时间）
      · 需要记录"谁标了、为什么标、怎么处理的"—— 这是处置流程，不是状态
      · 保留历史便于复盘（"这本书上次为什么被隐藏"）

    ★ 隐私红线：管理员**不可读用户正文**。
      本表只存"处置动作"，不含章节内容。查看正文需用户授权或司法程序。
    """
    __tablename__ = "content_flags"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    # 标记原因：illegal(违法) / porn(色情) / violence(暴力) /
    #          plagiarism(抄袭) / spam(垃圾) / other
    category: Mapped[str] = mapped_column(String(32), default="other")
    severity: Mapped[str] = mapped_column(String(16), default="warn")   # warn|block
    note: Mapped[str] = mapped_column(Text, default="")
    # 处置状态：open（待处理）| hidden（已隐藏）| cleared（已澄清）| banned（封禁）
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    # 举报来源：system(自动) | user(举报) | admin(人工巡查)
    source: Mapped[str] = mapped_column(String(16), default="admin")
    reporter_id: Mapped[str] = mapped_column(String(64), default="")
    handled_by: Mapped[str] = mapped_column(String(64), default="")
    handled_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)


class RefreshToken(Base):
    """刷新令牌（v0.20.6 A6-4）。

    ★ 双令牌机制的服务端一半：
      access_token（短，2h，无状态 HMAC）+ refresh_token（长，30d，**有状态**）

    ★ 为什么 refresh 要有状态（存表）：
      因为要支持**轮换 + 重放检测** —— 需要记录"这个令牌用过没有"。
      无状态令牌做不到这一点（OAuth 2.1 明确推荐轮换 + 检测）。

    ★ 安全要点：
      · `token_hash`：**存哈希不存明文**（与密码同理，库泄露不送会话）
      · `family_id`：一次登录产生的令牌链，重放时**整族吊销**
      · `parent_id`：形成链，可追溯到"哪个令牌换了它"
      · `used_at`：非空 = 已用过；再次出现即视为**泄露信号**
    """
    __tablename__ = "refresh_tokens"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), index=True)
    family_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    parent_id: Mapped[str] = mapped_column(String(64), default="")
    revoked: Mapped[int] = mapped_column(Integer, default=0, index=True)
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    ua: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)


class RolePermission(Base):
    """角色 → 权限映射（v0.20.7 A7，**表驱动**）。

    ★ 为什么用表而不是代码里的字典：
      1. **加角色/权限不用发版**（运营在后台改，即时生效）
      2. **可审计**（谁在什么时候改了权限，配合 admin_audit_logs）
      3. **可回滚**（改错了改回来）

    ★ 与内置定义的关系：
      DB 表是**主**，rbac.BUILTIN_ROLES 是**兜底**（表空/表不存在时用）。
      为什么要有兜底：避免"表被清空 = 全站无权限"的雪崩事故。

    ★ permission 支持通配符：
      `*`（全部） / `book.*`（book 下所有） / `book.read`（精确）
    """
    __tablename__ = "role_permissions"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), index=True)
    permission: Mapped[str] = mapped_column(String(64), index=True)
    # ★ 是否内置：内置角色不允许删除（与题材包同一约定）
    builtin: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    updated_by: Mapped[str] = mapped_column(String(64), default="")


# ================================================= v0.21 一致性体系补齐（B 组）
class CharacterRelation(Base):
    """角色关系（v0.21.2 B2 关系图谱）。

    ★ 为什么单独建表而不是在 Character 里存 JSON：
      · 关系是**双向**的（A→B 与 B→A 可能不同，如"暗恋" vs "无感"）
      · 需要按关系类型查询（"所有仇敌"）、计算强度、做矛盾检测
      · 图算法（力导向布局、连通分量）需要边表结构

    ★ intensity 取 -100~100：
      负 = 敌对，正 = 亲近，0 = 中立。比单纯 "friends/enemies" 表达力强得多，
      且能做"关系变化曲线"（第 10 章 -80，第 50 章 +60 = 化敌为友）。
    """
    __tablename__ = "character_relations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    from_id: Mapped[str] = mapped_column(String(64), index=True)
    to_id: Mapped[str] = mapped_column(String(64), index=True)
    from_name: Mapped[str] = mapped_column(String(128), default="")
    to_name: Mapped[str] = mapped_column(String(128), default="")
    # 关系类型：师徒|仇敌|暧昧|血亲|同门|盟友|上下级|陌生
    relation_type: Mapped[str] = mapped_column(String(32), default="陌生")
    intensity: Mapped[int] = mapped_column(Integer, default=0)   # -100~100
    since_chapter: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")
    # ★ 来源：auto（自动抽取）| manual（人工设定）
    source: Mapped[str] = mapped_column(String(16), default="manual")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class ChapterEmotion(Base):
    """章节情绪（v0.21.3 B3 情绪弧线）。

    ★ intensity（强度 0-100）与 valence（正负 -100~100）分开：
      "激烈打斗"（高强度、负向）与"温馨日常"（低强度、正向）是两种不同的东西，
      只用一个维度无法区分。曲线可视化需要两个轴。
    """
    __tablename__ = "chapter_emotion"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    ch_idx: Mapped[int] = mapped_column(Integer, default=0, index=True)
    intensity: Mapped[int] = mapped_column(Integer, default=0)    # 0-100 情绪强度
    valence: Mapped[int] = mapped_column(Integer, default=0)      # -100~100 正负
    tags: Mapped[str] = mapped_column(Text, default="")           # JSON list：爆点|低谷|转折|日常
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class CharacterKnowledge(Base):
    """角色信息差（v0.21.6 B6）。

    ★ 解决什么问题："角色说了他不该知道的事" —— 一致性最硬的伤。
      例：第 5 章主角偷偷炼成神功（只有他和小师妹知道），
          第 8 章反派却"早就知道你的秘密" → 读者立刻发现穿帮。

    ★ 建模：三元组（角色, 事实, 何时知道）。
      fact_id 指向 WorldSetting / Foreshadow / TimelineEvent 等"真相对象"。
      chapter_learned = 0 表示"尚不知道"（潜在越界风险）。
    """
    __tablename__ = "character_knowledge"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    character_id: Mapped[str] = mapped_column(String(64), index=True)
    character_name: Mapped[str] = mapped_column(String(128), default="")
    # 事实标识（可用 "setting:<id>" / "foreshadow:<id>" / "event:<id>" 形式）
    fact_id: Mapped[str] = mapped_column(String(128), index=True)
    fact_desc: Mapped[str] = mapped_column(Text, default="")
    known: Mapped[int] = mapped_column(Integer, default=0)        # 1=知道
    chapter_learned: Mapped[int] = mapped_column(Integer, default=0)  # 0=尚未得知
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class BookScene(Base):
    """小说场景层（v0.22.2 C2）。

    ★ 层级：书 → 卷 → 章 → **场景**
      参考 yWriter / Bibisco 的做法：一章通常含 2-5 个场景
      （同一时间地点、连续动作的单元）。

    ★ 为什么要场景层：
      · **写作粒度**：一次生成一个场景（800-1500 字）比整章（3000 字）质量高
        —— 上下文更聚焦，模型不容易跑偏
      · **一致性检查**：场景是"时间地点人物"的最小单元，检查更精准
      · **重写粒度**：只需重写某个场景，不用动整章
      · **短剧映射**：一个场景天然对应 1-2 个分镜（镜头）

    ★ goal/conflict/turn 三要素（来自编剧理论）：
      没有目标 → 场景无方向；没有冲突 → 场景无聊；没有转折 → 场景无价值。
    """
    __tablename__ = "book_scenes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    ch_idx: Mapped[int] = mapped_column(Integer, default=0, index=True)
    idx: Mapped[int] = mapped_column(Integer, default=0)          # 章内顺序
    title: Mapped[str] = mapped_column(String(256), default="")
    # 场景三要素
    goal: Mapped[str] = mapped_column(Text, default="")           # 角色想达成什么
    conflict: Mapped[str] = mapped_column(Text, default="")       # 遇到什么阻碍
    turn: Mapped[str] = mapped_column(Text, default="")           # 结果如何反转
    # 场景要素
    location: Mapped[str] = mapped_column(String(256), default="")
    time_point: Mapped[str] = mapped_column(String(128), default="")   # 时间点（早晨/三日后…）
    characters: Mapped[str] = mapped_column(Text, default="")     # JSON list 出场人物
    pov: Mapped[str] = mapped_column(String(128), default="")     # 视角人物
    # 情绪（与 B3 情绪弧线呼应）
    emotion_intensity: Mapped[int] = mapped_column(Integer, default=50)
    emotion_valence: Mapped[int] = mapped_column(Integer, default=0)
    # 正文片段（可空 —— 先有大纲后写正文）
    body: Mapped[str] = mapped_column(Text, default="")
    words: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="planned")  # planned|draft|final
    # ★ 来源：manual（人工）/ split（从正文自动切分）/ outline（从大纲生成）
    source: Mapped[str] = mapped_column(String(16), default="manual")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class LLMCall(Base):
    """LLM 调用成本台账（v0.23.2 D2）。

    ★ 与 MediaCall 的关系：
      MediaCall 记**图像/视频/音频**（按次计价）；
      LLMCall 记**文本模型**（按 token 计价）。两者互补，不可合并
      —— 计价单位不同，合并会让聚合逻辑充满 if/else。

    ★ 为什么每行都存 model_id + role：
      role 是"应该走哪档"，model_id 是"实际走了哪个"。
      两者不一致 = 路由没生效（或降级了），看板上一眼能看出来。
    """
    __tablename__ = "llm_calls"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 功能标签（chapter_write / summary / audit ...），决定成本归属
    task: Mapped[str] = mapped_column(String(64), default="", index=True)
    role: Mapped[str] = mapped_column(String(32), default="")
    user_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    book_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    model_id: Mapped[str] = mapped_column(String(128), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="ok")   # ok|error
    error: Mapped[str] = mapped_column(String(512), default="")
    run_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)


# ================================================= v0.24 F 组：反馈与数据洞察
class UserFeedback(Base):
    """用户反馈（v0.24.1 F1）。

    ★ 为什么单独建表而不是复用 ContentFlag：
      ContentFlag 是**平台对内容**的处置（合规侧）；
      本表是**用户对产品**的反馈（产品侧）。两者主体与流程完全不同。

    ★ context_json 的设计红线：
      只存**可复现问题所需的最小信息**（页面/操作路径/版本/浏览器），
      **绝不存正文、对话原文、密钥**。
      写入前必须过 `feedback.sanitize_context()` 做黑名单剔除 + 长度截断。
    """
    __tablename__ = "user_feedback"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 类型：bug（功能异常）/ suggestion（建议）/ quality（内容质量）/ other
    kind: Mapped[str] = mapped_column(String(32), default="other", index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    # 自动快照（JSON，已脱敏）
    context_json: Mapped[str] = mapped_column(Text, default="")
    # 状态：open | triaged（已分类）| in_progress | resolved | closed | duplicate
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    # 优先级：low | normal | high | urgent
    priority: Mapped[str] = mapped_column(String(16), default="normal", index=True)
    admin_note: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[str] = mapped_column(String(512), default="")     # JSON list
    # 关联对象（便于定位："第 5 章生成失败"）
    book_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    ch_idx: Mapped[int] = mapped_column(Integer, default=0)
    # 处理人
    handled_by: Mapped[str] = mapped_column(String(64), default="")
    handled_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # ★ 是否已进入"待评估池"（F1-6 记忆联动）
    pooled: Mapped[int] = mapped_column(Integer, default=0, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class TelemetryInstance(Base):
    """遥测实例（v0.24.2 F2 服务端）。

    ★ 为什么 instance_id 用随机 UUID 而非机器指纹：
      机器指纹（MAC/CPU ID）可反查到具体机器 → 属于个人信息。
      随机 UUID 首次启动生成、重置即换新，**无法反查**，
      对"统计有多少部署实例"这个目的完全够用。

    ★ is_commercial_suspect：
      开源版被拿去做商业服务时，我们希望**能识别但不阻止**（合规姿态）。
      判据是"用户数 / 章节数异常大"，仅作提示，不用于封禁。
    """
    __tablename__ = "telemetry_instances"
    instance_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    build_id: Mapped[str] = mapped_column(String(64), default="")
    version: Mapped[str] = mapped_column(String(32), default="", index=True)
    deploy_type: Mapped[str] = mapped_column(String(16), default="bare")
    os: Mapped[str] = mapped_column(String(64), default="")
    python_ver: Mapped[str] = mapped_column(String(32), default="")
    first_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)
    heartbeat_count: Mapped[int] = mapped_column(Integer, default=0)
    is_commercial_suspect: Mapped[int] = mapped_column(Integer, default=0)
    # ★ v0.25.3 H6：商用嫌疑的规则命中明细（JSON list）+ 人工复核状态
    #   review: pending（待复核）| confirmed（确认商用）| dismissed（已排除）
    suspect_reasons: Mapped[str] = mapped_column(Text, default="")
    review_status: Mapped[str] = mapped_column(String(16), default="", index=True)
    review_note: Mapped[str] = mapped_column(Text, default="")
    reviewed_by: Mapped[str] = mapped_column(String(64), default="")
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # 最近一次心跳的规模快照（看板展示用，免去 join metrics）
    last_scale_json: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")

    # ================= ★★ v0.25.5：与网络位置解耦的身份体系 =================
    # ★ 为什么必须这么做（用户反馈的核心痛点）：
    #   实例的 IP / 域名会变（服务器迁移、换 IP、上 CDN、改域名）。
    #   若用 IP 或来源域名当身份 —— **一变就认不出**，历史数据全断。
    #   做法：身份 = 实例自生成的随机 UUID（永不随网络变化），
    #        网络位置（site_url / 最近来源）只作为**展示与回访**的辅助信息。
    #
    # 部署方**自报**的站点地址（开源版在设置里填自己的域名）。
    # ★ 仅用于展示与"人去访问验证"，**不参与身份判定**。
    site_url: Mapped[str] = mapped_column(String(255), default="")
    # 站点名称（★ 部署方零配置时由 SaaS 自动生成，便于后台辨识）
    site_name: Mapped[str] = mapped_column(String(128), default="")
    # 部署方主机名（★ 不是设备指纹：用户可改的显示名，且不上报 MAC/序列号）
    hostname: Mapped[str] = mapped_column(String(64), default="")
    # ★ 认领码：部署方在开源版本地后台看到，填到 SaaS 后台完成绑定。
    #   为什么需要：SaaS 无法信任"来源 IP"（可伪造/会变），
    #   用一次性码把"实例"与"认领人"绑起来，是**主动确认**而非被动推断。
    claim_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    # 认领状态："" 未认领 | claimed 已认领
    claimed: Mapped[int] = mapped_column(Integer, default=0, index=True)
    claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    claimed_by: Mapped[str] = mapped_column(String(128), default="")
    # 部署方联系方式（可选，便于商务接触；★ 由部署方自愿填写）
    contact: Mapped[str] = mapped_column(String(255), default="")
    # ★ 最近一次心跳的来源 IP —— **只记录不用于识别**（便于排障/风控）
    last_source_ip: Mapped[str] = mapped_column(String(64), default="")
    # 来源变化的次数（★ 用于识别"这个实例换过几次地址"，而非判定身份）
    source_change_count: Mapped[int] = mapped_column(Integer, default=0)
    # ★ 客户端签名（HMAC of instance_id + 服务端密钥）——
    #   防止有人伪造他人 instance_id 刷数据。★ 不是身份来源，是防伪。
    sig: Mapped[str] = mapped_column(String(64), default="")


class TelemetryMetric(Base):
    """遥测指标快照（v0.24.2 F2）。

    ★ metrics_json 结构（全部是**计数/分布**，无任何内容）：
      scale   {books, chapters, characters, users, ...}
      feature {draft: 1234, audit: 56, ...}
      quality {consistency_issues: {...}, gate_fail_reasons: {...}}
      errors  {exception_types: {...}}
    """
    __tablename__ = "telemetry_metrics"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(64), index=True)
    collected_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)
    metrics_json: Mapped[str] = mapped_column(Text, default="")
    # ★ 写入前的红线校验结果（可追溯"这条数据是否已脱敏"）
    redline_ok: Mapped[int] = mapped_column(Integer, default=1)
    redline_hits: Mapped[str] = mapped_column(String(512), default="")


class TelemetryEvent(Base):
    """遥测事件计数（v0.24.2 F2）。"""
    __tablename__ = "telemetry_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(64), default="", index=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    detail_json: Mapped[str] = mapped_column(Text, default="")
    collected_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)


# ================================================= v0.25 Xf 组：通知体系
class Notification(Base):
    """站内通知（v0.25 Xf-2）。

    ★ 为什么"站内信 + 邮件"双通道：
      邮件会进垃圾箱、用户可能没绑邮箱；站内信一定送得到。
      两者互补：站内信保可达，邮件保及时。

    ★ dedup_key 的作用：
      同一事件可能被多个路径触发（如"余额不足"在扣费与预检两处都会判）。
      用 `dedup_key + 时间窗` 去重，避免用户被同一件事刷屏。
    """
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 事件类型：system | order | credit | drama | security | feedback | quota
    kind: Mapped[str] = mapped_column(String(32), default="system", index=True)
    # 级别：info | success | warning | error
    level: Mapped[str] = mapped_column(String(16), default="info")
    title: Mapped[str] = mapped_column(String(200), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    # 跳转链接（站内相对路径）
    link: Mapped[str] = mapped_column(String(512), default="")
    # ★ 去重键（同一事件重复触发时覆盖而非新增）
    dedup_key: Mapped[str] = mapped_column(String(128), default="", index=True)
    read_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # 邮件投递状态："" 未尝试 | sent | failed | skipped
    mail_status: Mapped[str] = mapped_column(String(16), default="")
    mail_error: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)


class NotificationPref(Base):
    """通知偏好（v0.25 Xf-2）。

    ★ 为什么按 kind 细分而不是一个总开关：
      用户可能想收"订单"但不想收"营销"。一个总开关只能全开或全关，
      结果通常是"全关"（连重要通知也收不到）。
    """
    __tablename__ = "notification_prefs"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)   # user_id:kind
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="")
    # 站内信：★ 默认开启（不影响他人，且重要）
    inapp: Mapped[int] = mapped_column(Integer, default=1)
    # 邮件：★ 默认开启但受全局邮件开关约束
    email: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class SettingHistory(Base):
    """配置变更历史（v0.25.4 I2）。

    ★ 为什么必须单独建表而不是复用 AdminAuditLog：
      审计日志是**面向人的行为记录**（谁做了什么，自然语言描述）；
      本表是**面向配置的状态快照**（改前值/改后值，可机器回滚）。
      两者用途不同：审计要"可读"，历史要"可回放"。

    ★ 为什么要存 old_value（而不是只记"改过 XX"）：
      回滚需要旧值。只记"改过"的话，出事时无法一键还原。
    """
    __tablename__ = "setting_history"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    old_value: Mapped[str] = mapped_column(Text, default="")
    new_value: Mapped[str] = mapped_column(Text, default="")
    # ★ 高危配置必须填原因（有测试守卫）
    reason: Mapped[str] = mapped_column(Text, default="")
    operator: Mapped[str] = mapped_column(String(64), default="", index=True)
    operator_ip: Mapped[str] = mapped_column(String(64), default="")
    # 变更来源：ui（界面）/ api（脚本）/ rollback（回滚）/ import（导入）
    source: Mapped[str] = mapped_column(String(16), default="ui", index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)


class InstanceCommand(Base):
    """实例指令（v0.25.5）—— SaaS 下发给部署方的指令队列。

    ★★ 为什么需要它（用户场景的完整解法）：
      部署方在 NAT 后 / 无公网入口 → SaaS **无法主动连过去**。
      但运营有时需要"让某个实例做点什么"（换配置、清缓存、回传详情）。

      解法：**指令挂在队列上，部署方每次心跳时顺带拉取**。
      - 部署方零公网入口 ✅（永远是它主动出站）
      - SaaS 换 IP 无感 ✅（部署方连域名）
      - 天然异步、天然重试 ✅（没拉到就下次心跳再取）

    ★ 为什么不做成"长连接 WebSocket"：
      开源部署环境五花八门（可能有代理/防火墙拦 WS），
      而心跳是 24h 一次的低频场景 —— 用 HTTP 轮询足够且最可靠。
      ★ 简单可靠的方案 > 时髦但脆弱的方案。
    """
    __tablename__ = "instance_commands"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(64), index=True)
    # 指令类型：ping（探活）| rotate_key | reload_config | collect_now | notice
    kind: Mapped[str] = mapped_column(String(32), default="ping", index=True)
    # 参数（JSON，如 {"message": "..."}）
    payload_json: Mapped[str] = mapped_column(Text, default="")
    # pending → delivered（已下发）→ done / failed
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    result_json: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)
    delivered_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    done_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


# ============================================================ 发票（v0.25.8 H4）

class Invoice(Base):
    """发票申请单（H4 订单与财务 — 发票部分）。

    状态机：applied → approved → issued → (void)
                    ↘ rejected

    ★★ 为什么发票要"申请 → 审核 → 开具"三段而不是一步到位：
      · 开票是**不可逆的税务动作**（发票号一旦开出即占号），
        不能由用户自助触发 —— 必须有管理员审核关口。
      · 抬头/税号写错要作废重开（红冲），流程上必须留痕。

    ★★ 金额以**订单实际支付金额**为准，绝不信任前端传入：
      前端传 amount_cents 只是修 bug 用的兜底；
      服务层一律 `sum(Order.amount_cents WHERE status='paid')` 重算。
      否则用户可以自报 ¥0.01 开 ¥1000 的票。

    ★★ 一单一票（幂等）：`order_ids` 存 JSON 数组，
      申请时校验每个订单**未被其他有效发票占用**，
      否则同一笔订单能被反复开票 → 虚开风险。
    """
    __tablename__ = "invoices"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    # 关联订单（JSON 数组字符串）
    order_ids: Mapped[str] = mapped_column(Text, default="")
    order_count: Mapped[int] = mapped_column(Integer, default=0)
    # 开票金额（分）★ 服务层重算，不信前端
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    # 票种：normal(普通发票) | special(专用发票，需资质)
    kind: Mapped[str] = mapped_column(String(16), default="normal")
    # 抬头类型：personal(个人) | company(企业)
    title_type: Mapped[str] = mapped_column(String(16), default="personal")
    title: Mapped[str] = mapped_column(String(256), default="")        # 发票抬头
    tax_no: Mapped[str] = mapped_column(String(64), default="")        # 纳税人识别号
    address: Mapped[str] = mapped_column(String(256), default="")      # 注册地址（专票用）
    phone: Mapped[str] = mapped_column(String(64), default="")         # 注册电话（专票用）
    bank: Mapped[str] = mapped_column(String(128), default="")         # 开户行（专票用）
    bank_account: Mapped[str] = mapped_column(String(64), default="")  # 银行账号（专票用）
    email: Mapped[str] = mapped_column(String(256), default="")        # 接收邮箱
    # 状态：applied | approved | rejected | issued | void
    status: Mapped[str] = mapped_column(String(16), default="applied", index=True)
    # ★ 发票号（开具后生成，格式 YYYYMM + 8 位流水；作废后仍保留占号）
    invoice_no: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 附件路径（生成的发票文件，相对 data/）
    file_path: Mapped[str] = mapped_column(String(512), default="")
    # 备注 / 驳回原因 / 作废原因
    note: Mapped[str] = mapped_column(Text, default="")
    reject_reason: Mapped[str] = mapped_column(Text, default="")
    void_reason: Mapped[str] = mapped_column(Text, default="")
    # 操作留痕
    operator: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    issued_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


# ============================================================ 模型供应商与路由（v0.26.2）
class ModelProvider(Base):
    """模型供应商端点（v0.26.2）。

    ★ 一个"模型"可以有多个供应商端点（负载分流 / 故障转移）。
      例：agnes-image-2.5-flash 同时挂在两个中转站上，各带一个 key。

    ★ 与 ModelProfile 的分工：
      ModelProfile（旧）= "一个可调用的槽"（端点和模型混在一起）。
      本表把**端点**抽出来，让"多个 key 同一模型"变成 N 行记录，
      路由层按权重挑一个，失败自动换下一个。
    """
    __tablename__ = "model_providers"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 归属的模型槽（ModelProfile.id）；空 = 全局可用
    profile_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    name: Mapped[str] = mapped_column(String(128), default="")   # 展示名（如 "主线路"/"备用"）
    base_url: Mapped[str] = mapped_column(String(512), default="")
    api_key: Mapped[str] = mapped_column(Text, default="")       # 加密存储
    # 权重（负载分流比例）；0 表示不参与常规分流（仅手动/兜底）
    weight: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    # 健康状态：连续失败计数 + 熔断截止时间（见 §36 熔断范式）
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    open_until: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_ok_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_err: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class PlanModelRoute(Base):
    """套餐 → 模型路由（v0.26.2）。

    ★ 需求：不同用户套餐用不同模型（免费版用便宜模型，Pro 用强模型）。
      owner 可在后台按 (套餐, 功能角色) 指定优先模型，未配置则走全局默认。

    role: predict（正文）/ planner（大纲）/ critic（判定摘要）/
          image / video / tts / script / storyboard
    """
    __tablename__ = "plan_model_routes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_key: Mapped[str] = mapped_column(String(32), default="", index=True)
    role: Mapped[str] = mapped_column(String(32), default="", index=True)
    # 有序模型槽 id 列表（JSON 数组）—— 按序降级
    profile_ids: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


class GenLesson(Base):
    """生成避坑记录（v1.0-E4.19 · #315）。

    ★ 解决什么问题：用户在聊天里反复"指出问题 → 系统修正"，这些纠错过程
      以前**完全不落库** —— 下次生成同样的坑还会再踩一遍。

    ★ 与 adoption 的关系：adoption 只采"计数与分桶"（隐私优先，不含内容）；
      本表刻意存**可复用的经验**（用户要求什么、系统怎么改的），
      因为避坑提示必须是可读的具体指令才能注入提示词。

    ★ 隐私红线（沿用 insight 的脱敏思路）：
      · 只由**书籍 owner 自己的会话**产生，不外泄（按 book_id 隔离）
      · 管理端可见（本实例管理员），但**永不进遥测上报**（不参与 telemetry）
      · 长度硬截断（trigger/fix 各 ≤600 字），防爆表
      · status=muted 可由 owner/管理员停用（误采的坑要能关掉）
    """
    __tablename__ = "gen_lessons"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    book_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    user_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 触发类型：correction（用户纠错）/ preference（用户偏好声明）
    kind: Mapped[str] = mapped_column(String(32), default="correction")
    # 用户指出/要求的内容（原文截断）
    trigger: Mapped[str] = mapped_column(String(600), default="")
    # 系统实际做了什么修正（摘要）
    fix: Mapped[str] = mapped_column(String(600), default="")
    # 本轮涉及的变更类工具名（逗号分隔，如 "draft_chapter,save_character"）
    tools: Mapped[str] = mapped_column(String(256), default="")
    # active|muted
    status: Mapped[str] = mapped_column(String(16), default="active")
    # 被注入提示词的次数（衡量"这条坑到底有没有用"）
    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)


class AdoptionSnapshot(Base):
    """采纳计数增量快照（v1.0-E4.21）。

    ★ 解决什么问题：adoption 的计数原本**只存内存**，服务每次重启/部署
      就清零 —— 数据洞察页的「提示词版本对比 / 采纳漏斗」永远只能看到
      "本次运行以来"的零头，无法积累，更谈不上"支持系统进化"。

    ★ 设计（增量 delta，不是全量副本）：
      · 每次 flush 把**自上次 flush 以来新增**的计数写入一行，并清空内存
      · 读取时 = Σ(所有快照行) + 当前内存计数 → 精确且跨重启不丢
      · 若误存全量再求和会重复计数，所以必须是 delta

    ★ 隐私（与 adoption 模块同一红线）：
      · payload 只有「可枚举键名 → 计数」，键名已经过 _safe 护栏
        （非 ASCII 一律转下划线），不含任何正文/用户内容
      · 只在本实例管理端可见，**永不进遥测上报**

    ★ 可靠性：flush fail-open（写库失败不清内存，下次再试），
      数据丢失上界 = 阈值窗口内的增量。
    """
    __tablename__ = "adoption_snapshots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 键名 → 计数（JSON 对象，已脱敏）
    payload: Mapped[str] = mapped_column(Text, default="{}")
    # 本次快照覆盖的事件条数（所有计数之和，便于核对）
    total: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, index=True)

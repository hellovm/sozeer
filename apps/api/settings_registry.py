# -*- coding: utf-8 -*-
"""配置注册表（v0.25.4 I1）—— 全站配置的**单一事实源**。

================================ 为什么需要注册表 ================================

★ 问题：没有注册表时，配置散落在三处，且**互相不知道对方存在**
  · `SETTING_DEFAULTS` 里几个手写键
  · 代码里 `get_setting(db, "xx", "默认")` 的硬编码默认值
  · 界面上一堆 input 标签手写
  结果：加一个配置要改三处 → 必然漂移（界面写了后端不认识 / 后端读了没人能改）。

★ 方案：**注册表是唯一声明处**，其余全部自动派生：
  · 界面 → 由 `SPEC` 自动渲染（加配置只改这里）
  · 校验 → 由 `type` / `min` / `max` / `choices` 自动生成
  · 读取 → 由 `default` 兜底，调用方不用再写默认值
  · 文档 → 可导出成 Markdown

================================ 四层优先级 ================================

  ① DB（管理员在界面改的）        ← 最高
  ② 环境变量（部署时注入）
  ③ 注册表 default
  ④ 类型兜底（如 int 转不出来用 0）

★ 为什么环境变量在 DB 之下：
  环境变量是「部署时的初始值」，管理员在界面上改过之后应该以界面为准。
  否则运维改过环境变量就永远覆盖不了 —— 用户会以为"改了没生效"。

================================ 风险等级 ================================

  safe     普通配置（改错影响小）
  caution  需注意（影响计费/成本）
  danger   ★ 高危（影响计费正确性/安全/数据），必须填原因 + 二次确认

★ 为什么高危配置要「填原因」：
  出问题时第一个要回答的是「谁在什么时候为什么改的」。
  只记 old/new 不够 —— 不知道为什么改，就无法判断当时的意图是否正确。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

# ★★ v1.0-E4.15：档位配置的合法值来自 model_router.ROLES（唯一声明处）。
#   为什么模块级导入而不是延迟导入（MEMORY §能模块级导入就别延迟导入）：
#     已验证双向导入顺序无循环（model_router → keyvault → config，
#     均不回引 settings_registry）；模块级导入让 audit_imports 能静态校验，
#     延迟导入会把 ImportError 推迟到调用时（端点 500，最难查）。
from apps.api.model_router import ROLES as _MODEL_ROLES

_log = logging.getLogger("novelforge.settings")

# 环境变量前缀（部署方注入用）
ENV_PREFIX = "NF_CFG_"


# ============================================================ 配置项定义

@dataclass(frozen=True)
class Spec:
    """单个配置项的声明。

    ★ 为什么用 frozen dataclass 而不是 dict：
      dict 可以被任何地方 `spec["default"] = x` 悄悄改掉，
      注册表一旦被改就丧失了"单一事实源"的意义。frozen 从语法上禁止。
    """
    key: str
    type: str                                   # str|int|float|bool|choice|text
    default: Any
    label: str = ""                             # 界面显示名
    group: str = "system"                       # 分组（界面按组渲染）
    desc: str = ""                              # 说明（界面 tooltip）
    risk: str = "safe"                          # safe|caution|danger
    choices: tuple[str, ...] = ()               # type=choice 时必填
    min: float | None = None                    # 数值下界（含）
    max: float | None = None                    # 数值上界（含）
    unit: str = ""                              # 单位（显示用）
    # ★ 只读项：由系统维护（如实例 ID），界面可看不可改
    readonly: bool = False
    # ★★ 高级项（v1.0-E4.4）：默认**折叠**，面向前端运营者隐藏。
    #
    #   为什么需要它：运营打开配置中心，看到 `model.role_writer = writer`
    #   这类**实现细节**会一脸茫然 —— 它们不是"可调的业务参数"，
    #   而是"改错了会出事的技术常量"。
    #
    #   ★ 判据（怎么判断一个配置该不该折叠）：
    #     · 运营能说清"改成什么对业务更好"吗？不能 → advanced
    #     · 它有既定合理值、调它需要看代码/文档吗？是 → advanced
    #     · 改错了会静默出错（而不是立刻报错）吗？是 → advanced
    #
    #   例：`model.temperature_writer`（正文温度）→ 运营可理解（"更有创意/更稳"）
    #       `model.role_writer`（档位名）→ 需要先懂"档位"是什么概念 → advanced
    advanced: bool = False
    # ★ 热更新回调：改完立即生效（不需要重启）。None = 下次调用时自然生效
    on_change: Callable[[Any], None] | None = field(default=None, compare=False)


# ============================================================ 分组元信息

GROUPS: dict[str, dict[str, str]] = {
    "model":    {"name": "模型", "icon": "🤖",
                 "desc": "模型选择与调用参数。★ 按角色路由是降本的主要手段"},
    "billing":  {"name": "计费", "icon": "💰",
                 "desc": "积分单价与套餐规则。★ 改动影响收入，请谨慎"},
    "cost":     {"name": "成本", "icon": "📊",
                 "desc": "token 上限与告警。★ 上限为 0 表示不限制"},
    "feature":  {"name": "功能开关", "icon": "🎚️",
                 "desc": "模块级开关。关闭后入口隐藏且接口返回 403"},
    "security": {"name": "安全", "icon": "🔒",
                 "desc": "限流、令牌、密码策略"},
    "system":   {"name": "系统", "icon": "⚙️",
                 "desc": "站点信息与公告"},
    "telemetry": {"name": "遥测", "icon": "📡",
                  "desc": "开源实例数据收集。★ 默认开启（仅匿名聚合统计，可一键关闭）"},
    "queue": {"name": "任务队列", "icon": "⏳",
              "desc": "并发上限与告警。★ 并发不为 0 是为了保护机器与用户体验"},
    # ★★ v1.0-E4.4 新增：支付渠道（此前**完全没有**配置项）
    #
    #   修复前的问题：后台只有 `GET /api/pay/providers`（只读），
    #   **没有任何写端点** —— 运营想接微信支付只能改代码或环境变量。
    #
    #   ★ 判据：一个"可配置能力"必须同时具备 **读 + 写 + 校验 + 生效**。
    #     只有读端点 = 运营改不了 = 等于没有（MEMORY §24）。
    "payment": {"name": "支付渠道", "icon": "💳",
                "desc": "各支付渠道的开关与凭据。★ 密钥加密存储，界面不回显"},
}

RISK_LABEL = {"safe": "普通", "caution": "注意", "danger": "高危"}


def _specs() -> list[Spec]:
    """全部配置项（★ 加配置只改这里）。

    命名规范：`分组.名称`，如 `model.role_writer`。
    """
    return [
        # ---------------------------------------------- 模型 model.*
        # ★★ v1.0-E4.4：以下两项是**纯技术参数**，运营改它们没有业务判断依据
        #   （"超时该设 120 还是 180 秒"取决于网络与模型，不取决于运营策略）。
        #   → 标记 advanced，界面默认折叠。
        Spec("model.timeout_sec", "int", 120, "调用超时", "model",
             "单次模型调用超时（秒）。★ 太长会阻塞队列", min=5, max=900,
             unit="秒", advanced=True),
        Spec("model.retry", "int", 2, "失败重试次数", "model",
             "网络类错误的重试次数（不含 4xx）", min=0, max=5, unit="次",
             advanced=True),
        Spec("model.temperature_writer", "float", 0.85, "正文温度", "model",
             "写正文的 temperature。★ 越高越有创意也越易跑偏", min=0.0, max=2.0),
        Spec("model.temperature_critic", "float", 0.3, "检查温度", "model",
             "一致性检查/摘要的温度（要稳定，建议 ≤0.5）", min=0.0, max=2.0),
        # ★★ v1.0-E4.4：`role_*` 需要先理解"档位"这个概念（它映射到
        #   「模型配置」页里的模型槽）。运营直接在这里改名字**极易改错**
        #   （改成不存在的档位名 → 静默回退到默认）。
        #   → 标记 advanced；真正该去的地方是「模型配置 → 套餐路由」。
        #
        # ★★ v1.0-E4.15：从 str 改为 **choice**（合法值 = 模型路由的档位名）。
        #   修复前的问题：str 类型任意值都能保存（"writerr" 拼错也 200），
        #   运行时静默回退默认档 → "配了但没生效"这种最难查的缺陷。
        #   → choice 让「不合法的值在保存关口就被拒」（MEMORY §校验关口要报错）。
        #   ★ choices 引用 model_router.ROLES 的键（单一事实源），
        #     新增档位只需改 model_router，注册表自动跟随。
        Spec("model.role_writer", "choice", "writer", "写正文档位", "model",
             "写正文用的档位。★ 质量优先，建议用强模型；"
             "日常调整请到「模型配置 → 套餐路由」页",
             choices=tuple(_MODEL_ROLES.keys()), risk="caution", advanced=True),
        Spec("model.role_critic", "choice", "critic", "检查档位", "model",
             "摘要/一致性检查/打分用的档位。★ 调用量最大，用便宜模型省最多",
             choices=tuple(_MODEL_ROLES.keys()), risk="caution", advanced=True),
        Spec("model.role_planner", "choice", "planner", "规划档位", "model",
             "大纲/任务单等结构化任务用的档位",
             choices=tuple(_MODEL_ROLES.keys()), advanced=True),
        Spec("model.role_utility", "choice", "utility", "辅助档位", "model",
             "标题/标签/分类等极短输出用的档位",
             choices=tuple(_MODEL_ROLES.keys()), advanced=True),
        Spec("model.free_tier_role", "choice", "critic", "免费版降级档", "model",
             "免费用户写正文任务降级到哪一档。★ 建议 critic（便宜且稳定）",
             choices=tuple(_MODEL_ROLES.keys()), risk="caution"),

        # ---------------------------------------------- 计费 billing.*
        Spec("billing.credit_per_yuan", "float", 100.0, "每元积分数", "billing",
             "充值 1 元得多少积分。★ 改动影响所有新订单",
             risk="danger", min=1.0, max=100000.0, unit="积分/元"),
        Spec("billing.free_credits", "int", 2000, "注册赠送", "billing",
             "新用户注册赠送积分", risk="caution", min=0, max=10_000_000, unit="积分"),
        Spec("billing.sign_daily", "int", 100, "每日签到", "billing",
             "签到赠送积分", min=0, max=100_000, unit="积分"),
        Spec("billing.refund_window_days", "int", 7, "可退款天数", "billing",
             "订单支付后多少天内允许自助申请退款", risk="caution",
             min=0, max=90, unit="天"),
        Spec("billing.min_withdraw", "int", 10000, "最低提现", "billing",
             "提现门槛（积分）", risk="caution", min=0, max=100_000_000),
        Spec("billing.charge_on_fail", "bool", False, "失败也扣费", "billing",
             "★ 生成失败是否扣积分。建议关闭（失败扣费客诉最多）",
             risk="danger"),
        # ★★ v1.0-E4 补登记：`ops_touch.py` 早已在读这个键，但**注册表里没有** ——
        #   后果：后台配置页**看不到这一项**，运营想调额度预警阈值只能改代码。
        #   这是"代码读了但没登记"的典型缺口 —— 注册表是**唯一声明处**（MEMORY §53），
        #   漏登记 = 界面/校验/文档全部缺失。
        Spec("billing.low_credit_threshold", "int", 2000, "额度预警阈值", "billing",
             "余额低于该值时提醒用户充值。★ 采用**档位+边沿触发**："
             "只在「跌破 50% / 20%」时各提醒一次，不会每笔扣费都发（防刷屏）",
             risk="caution", min=0, max=10_000_000, unit="积分"),

        # ---------------------------------------------- 成本 cost.*
        Spec("cost.daily_token_cap", "int", 2_000_000, "单用户日上限", "cost",
             "★ 0 = 不限制。防止单账号刷爆成本", risk="danger",
             min=0, max=10_000_000_000, unit="token"),
        Spec("cost.global_daily_cap", "int", 0, "全站日上限", "cost",
             "★ 0 = 不限制。超过则暂停生成（保命开关）", risk="danger",
             min=0, max=100_000_000_000, unit="token"),
        Spec("cost.spike_ratio", "float", 10.0, "突增告警倍数", "cost",
             "用量超过同时段基线多少倍时告警", min=1.0, max=1000.0),
        Spec("cost.retention_days", "int", 180, "台账保留", "cost",
             "llm_calls 明细保留天数（超期自动清理）", min=7, max=3650, unit="天"),
        Spec("cost.warn_percent", "int", 80, "告警阈值", "cost",
             "用量达到日上限多少百分比时提醒用户", min=10, max=100, unit="%"),

        # ---------------------------------------------- 功能开关 feature.*
        Spec("feature.draft", "bool", True, "AI 续写", "feature", "章节正文生成"),
        Spec("feature.drama", "bool", True, "短剧改编", "feature", "小说转短剧流水线"),
        Spec("feature.batch_ghost", "bool", True, "批量代笔", "feature",
             "多章连续生成（消耗大，量大时建议关闭）", risk="caution"),
        Spec("feature.market", "bool", False, "作品市场", "feature",
             "公开作品市场（默认关闭，需内容储备）"),
        Spec("feature.feedback", "bool", True, "用户反馈", "feature", "反馈入口"),
        Spec("feature.telemetry_ui", "bool", True, "遥测开关界面", "feature",
             "是否向用户展示遥测开关"),
        Spec("feature.invite_only", "bool", False, "仅邀请注册", "feature",
             "★ 开启后关闭自助注册，只能由管理员建号", risk="caution"),
        Spec("feature.maintenance", "bool", False, "维护模式", "feature",
             "★ 开启后所有非管理员请求返回 503", risk="danger"),
        Spec("feature.readonly", "bool", False, "只读模式", "feature",
             "★ 开启后禁止生成（保留浏览与导出）", risk="danger"),
        Spec("feature.new_user_guide", "bool", True, "新手引导", "feature", "首次使用引导"),

        # ---------------------------------------------- 安全 security.*
        Spec("security.token_ttl_hours", "int", 336, "令牌时长", "security",
             "登录令牌有效时长（默认 14 天）", risk="caution",
             min=1, max=8760, unit="小时"),
        Spec("security.ratelimit_global", "int", 300, "全局限流", "security",
             "单 IP 每分钟请求上限", min=10, max=100000, unit="次/分"),
        Spec("security.ratelimit_login", "int", 10, "登录限流", "security",
             "单 IP 每分钟登录尝试上限", min=1, max=1000, unit="次/分"),
        Spec("security.password_min_len", "int", 8, "密码最短", "security",
             "密码最小长度", min=6, max=64, unit="位"),
        Spec("security.login_fail_lock", "int", 5, "失败锁定阈值", "security",
             "同账号连续失败多少次后触发风控", min=1, max=100, unit="次"),
        Spec("security.session_max", "int", 10, "并发会话数", "security",
             "单账号最多同时在线设备数，超出踢最早", min=1, max=100),
        Spec("security.audit_retention_days", "int", 365, "审计保留", "security",
             "操作审计日志保留天数（★ 合规要求 ≥ 6 个月）",
             risk="caution", min=180, max=3650, unit="天"),
        # ★★ v1.0-E4.14 后台安全加固（对标成熟 SaaS）
        Spec("security.admin_force_2fa", "bool", False, "后台强制 2FA", "security",
             "开启后，非 owner 的管理员必须已启用两步验证（TOTP）才能访问 /api/admin/*。"
             "★ owner 作为 break-glass 逃生角色不受限（防全体管理员被锁死）。默认关闭。",
             risk="danger"),
        Spec("security.admin_ip_whitelist", "str", "", "后台 IP 白名单", "security",
             "逗号分隔的 IP 或 CIDR 网段（如 203.0.113.5,198.51.100.0/24）。"
             "非空时，只有名单内的客户端能访问 /api/admin/*；留空 = 不限制（默认）。"
             "★ 自家出口 IP 可在百度搜「IP」查到；填错会把自己锁在后台外"
             "（届时需上服务器改库 recovery），建议先加一个备用 IP",
             risk="danger", advanced=True),

        # ---------------------------------------------- 系统 system.*
        Spec("system.site_name", "str", "溯字 SOZEER", "站点名称", "system",
             "浏览器标签页标题与前台页头显示的名字"),
        Spec("system.site_tagline", "str", "与创作主编一起，把一部小说从立项聊到成稿",
             "站点标语", "system",
             "前台首页大标题下方的一句话介绍（营销页 hero 区副标题）"),
        Spec("system.icp", "str", "", "ICP 备案号", "system",
             "如「京ICP备2026000000号-1」。★ 显示在页脚；在中国大陆提供网站服务"
             "属合规硬要求，完成备案后填写（E1 备案项，未备案前留空）"),
        Spec("system.contact_email", "str", "", "联系邮箱", "system",
             "页脚「联系我们」展示的邮箱，也作为用户反馈/商务合作的对外联系渠道"),
        Spec("system.announcement", "text", "", "全站公告", "system",
             "非空时在前台顶部展示横幅；留空则不显示。支持纯文本（不解析 HTML）"),
        # ★★ v1.0-E4.15：choice 的每个取值都写清楚视觉效果差异
        Spec("system.announcement_level", "choice", "info", "公告级别", "system",
             "公告横幅的颜色级别：info=蓝色（一般通知）/ warning=黄色（重要提醒）"
             "/ danger=红色（紧急故障或停服通知）",
             choices=("info", "warning", "danger")),
        Spec("system.allow_register", "bool", True, "开放注册", "system",
             "★ 关闭后前台不显示注册入口，只能由管理员在后台建号"
             "（邀请制运营时使用）", risk="caution"),
        # ★★ v1.0-E4.15：default_role 写清 editor 的能力边界
        Spec("system.default_role", "choice", "user", "默认角色", "system",
             "新注册用户的初始角色：user=普通用户（只用写作功能）/ "
             "editor=编辑（额外拥有内容审核等协作权限，慎选）",
             choices=("user", "editor"), risk="caution"),

        # ---------------------------------------------- 队列 queue.*（v0.25.6 J5）
        Spec("queue.max_global", "int", 8, "全局并发上限", "queue",
             "★ 全站同时执行的任务数。0 = 不限制（不建议）", risk="caution",
             min=0, max=128, unit="个"),
        Spec("queue.max_per_user", "int", 2, "单用户并发上限", "queue",
             "★ 防止一个用户开多个标签页把机器打满", risk="caution",
             min=0, max=32, unit="个"),
        Spec("queue.max_per_book", "int", 1, "单书并发上限", "queue",
             "★ 同一本书建议串行（否则章节顺序会乱）", risk="caution",
             min=0, max=8, unit="个"),
        Spec("queue.alert_depth", "int", 100, "队列深度告警", "queue",
             "积压超过此值时告警", risk="caution", min=1, max=100000, unit="个"),
        Spec("queue.alert_fail_rate", "int", 30, "失败率告警", "queue",
             "失败率超过此百分比时告警", risk="caution", min=1, max=100, unit="%"),

        # ---------------------------------------------- 遥测 telemetry.*
        Spec("telemetry.enabled", "bool", True, "启用遥测", "telemetry",
             "★ 默认开启。开源版默认收集**匿名聚合计数**（不含任何作品内容与"
             "用户信息），用于了解使用规模与功能受欢迎度；可在本页或环境变量"
             "关闭，关闭不影响任何功能", risk="caution"),
        Spec("telemetry.endpoint", "str", "", "上报地址", "telemetry",
             "★ 默认上报地址固定为 https://sozeer.com/v1/heartbeat（就在 sozeer.com"
             "主域上，**无需绑定任何子域名**）。仅当自建接收端才填；留空则沿用默认"
             "地址。★ 官方实例保持默认即可，自部署保持留空即可"),
        Spec("telemetry.interval_hours", "int", 24, "上报间隔", "telemetry",
             "每隔多少小时上报一次心跳", min=1, max=720, unit="小时"),
        Spec("telemetry.instance_id", "str", "", "实例 ID", "telemetry",
             "★ 系统自动生成的随机 UUID，不包含任何机器特征（不可反查）。"
             "由系统维护，不可修改", readonly=True),
        Spec("telemetry.share_metrics", "bool", True, "上报规模指标", "telemetry",
             "是否附带上报书籍数/章节数/用户数等**聚合计数**（关闭后只发心跳）"),

        # ---------------------------------------------- 支付渠道 payment.*
        #
        # ★★ v1.0-E4.4 新增（修复"支付渠道无法配置"）
        #
        #   修复前：后端只有 `GET /api/pay/providers`（列渠道），
        #   **没有任何写端点** —— 运营接微信/支付宝只能改代码。
        #
        #   ★ 设计要点：
        #     ① 密钥类走 `secret_store`（加密），不进通用配置表明文
        #     ② 渠道**开关**与**凭据**分开：先开开关，再填凭据
        #     ③ 沙箱渠道默认开启（开箱即用），真实渠道默认关闭
        #        （防"配了一半就收款"这种事故）
        Spec("payment.sandbox_enabled", "bool", True, "沙箱支付", "payment",
             "★ 开发/演示用：模拟支付成功，不产生真实交易。生产建议关闭"),
        Spec("payment.wechat_enabled", "bool", False, "微信支付", "payment",
             "★ 需先完成微信商户号申请与回调地址报备", risk="caution"),
        Spec("payment.alipay_enabled", "bool", False, "支付宝", "payment",
             "★ 需先完成支付宝商户签约", risk="caution"),
        Spec("payment.stripe_enabled", "bool", False, "Stripe", "payment",
             "海外收款。★ 需 Stripe 账号（支持个人）", risk="caution"),
        # ★ 回调地址：支付平台异步通知的地址（必须公网可达）
        Spec("payment.notify_base_url", "str", "", "回调地址前缀", "payment",
             "支付平台异步通知的地址前缀，填站点根地址（如 https://sozeer.com，"
             "不要带路径）。★ 留空则用请求 Host 推断；生产环境若站点在反代/"
             "CDN 之后，**必须显式配置**，否则回调地址可能被推断成内网地址",
             risk="caution"),
        # ★ 结算币种与最小金额
        # ★★ v1.0-E4.15：币种写清各自用途（免得"我该选哪个"）
        Spec("payment.currency", "choice", "CNY", "结算币种", "payment",
             "订单展示与结算使用的币种：CNY=人民币（国内收款）/ USD=美元 / "
             "HKD=港币。★ 改动只影响**新订单**，历史订单保持下单时的币种",
             choices=("CNY", "USD", "HKD")),
        Spec("payment.min_amount_cents", "int", 100, "最低支付金额", "payment",
             "低于此金额（单位：**分**，100 分 = 1 元）不允许下单。"
             "★ 防小额刷单/测试单打爆支付回调", 
             min=1, max=1_000_000, unit="分"),
        # ★★ 免密调试：仅开发用，生产必须关闭
        Spec("payment.skip_signature_check", "bool", False, "跳过签名校验",
             "payment",
             "★★ 危险：跳过支付回调的签名验证。**仅本地调试使用**，"
             "生产开启等于白送钱（任何人可伪造支付成功回调）",
             risk="danger"),
    ]


# ★ 注册表（模块级构建一次）
SPECS: dict[str, Spec] = {s.key: s for s in _specs()}

# 兼容旧接口（services/settings.py 的 SETTING_DEFAULTS）
LEGACY_KEYS = {
    "site_name": "system.site_name",
    "site_tagline": "system.site_tagline",
    "allow_register": "system.allow_register",
    "default_role": "system.default_role",
}


def spec_of(key: str) -> Spec | None:
    return SPECS.get(key)


def by_group() -> dict[str, list[Spec]]:
    """按分组返回（界面渲染用）。"""
    out: dict[str, list[Spec]] = {g: [] for g in GROUPS}
    for s in SPECS.values():
        out.setdefault(s.group, []).append(s)
    for g in out:
        out[g].sort(key=lambda x: x.key)
    return out


# ============================================================ 类型转换与校验

def coerce(spec: Spec, raw: Any) -> Any:
    """把原始值转成声明的类型。★ 转不出来就抛 ValueError（不静默兜底）。

    ★ 为什么不静默兜底：把 `"abc"` 当 `0` 会让"配置写错了"变成
      "配置看起来生效了但值是 0" —— 后者极难排查。
      校验关口要报错，读取兜底才用默认值（见 get_config）。
    """
    t = spec.type
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("1", "true", "yes", "on", "y", "t"):
            return True
        if s in ("0", "false", "no", "off", "n", "f", ""):
            return False
        raise ValueError(f"不是合法布尔值：{raw!r}")
    if t == "int":
        if isinstance(raw, bool):
            raise ValueError("布尔不是整数")
        if isinstance(raw, float):
            # ★ 浮点输入直接截断（HTML number 输入框可能给 3.9，
            #   报错会让"填了 3.9"变成保存失败 —— 截断更符合预期）
            return int(raw)
        txt = str(raw).strip().replace(",", "").replace("_", "")
        try:
            return int(txt)
        except ValueError:
            # 再试浮点（"3.9" 这种字符串）
            return int(float(txt))
    if t == "float":
        if isinstance(raw, bool):
            raise ValueError("布尔不是数值")
        return float(str(raw).strip().replace(",", "").replace("_", ""))
    if t == "choice":
        s = str(raw).strip()
        if spec.choices and s not in spec.choices:
            raise ValueError(f"必须是 {'/'.join(spec.choices)} 之一，收到 {s!r}")
        return s
    # str / text
    return str(raw)


def validate(spec: Spec, value: Any) -> tuple[bool, str]:
    """校验（返回 ok, 原因）。★ 在**写入前**调用，不合法直接拒。"""
    try:
        v = coerce(spec, value)
    except (ValueError, TypeError) as e:
        return False, f"{spec.label or spec.key}：{e}"
    if spec.type in ("int", "float"):
        if spec.min is not None and v < spec.min:
            return False, f"{spec.label or spec.key} 不能小于 {spec.min}{spec.unit}"
        if spec.max is not None and v > spec.max:
            return False, f"{spec.label or spec.key} 不能大于 {spec.max}{spec.unit}"
    if spec.type in ("str", "text"):
        n = len(str(v))
        if n > 4000:
            return False, f"{spec.label or spec.key} 过长（{n} 字符，上限 4000）"
    return True, ""


def is_dangerous(key: str) -> bool:
    s = SPECS.get(key)
    return bool(s and s.risk == "danger")


def normalize_key(key: str) -> str:
    """兼容旧键名（`site_name` → `system.site_name`）。"""
    k = (key or "").strip()
    return LEGACY_KEYS.get(k, k)


# ============================================================ 环境变量读取

def env_value(spec: Spec) -> Any | None:
    """环境变量值（未设置返回 None）。

    ★ 支持两种命名：
      `NF_CFG_MODEL_TIMEOUT_SEC`（点换下划线，大写）
      `NF_CFG_MODEL.TIMEOUT_SEC`（原样，某些平台不允许，仍尝试）
    """
    cands = [
        ENV_PREFIX + spec.key.upper().replace(".", "_"),
        ENV_PREFIX + spec.key.upper(),
    ]
    for name in cands:
        raw = os.environ.get(name)
        if raw is not None and raw != "":
            return raw
    return None


# ============================================================ 文档导出（I11 前置）

def to_markdown() -> str:
    """导出配置文档（★ 注册表是唯一事实源 → 文档自动同步，不会过期）。"""
    lines = ["# 溯字 SOZEER 配置项清单", "",
             f"共 **{len(SPECS)}** 项，分 {len(GROUPS)} 组。",
             "★ 本文件由 `settings_registry.py` 自动生成，请勿手工编辑。", ""]
    for g, name in GROUPS.items():
        items = by_group().get(g) or []
        if not items:
            continue
        lines.append(f"## {name['icon']} {name['name']}（{len(items)} 项）")
        lines.append("")
        lines.append("| 键 | 类型 | 默认值 | 风险 | 说明 |")
        lines.append("|---|---|---|---|---|")
        for s in items:
            d = str(s.default)
            if s.type == "bool":
                d = "开" if s.default else "关"
            if len(d) > 28:
                d = d[:25] + "…"
            rng = ""
            if s.min is not None or s.max is not None:
                rng = f"（{s.min if s.min is not None else '-∞'} ~ {s.max if s.max is not None else '∞'}）"
            lines.append(f"| `{s.key}` | {s.type}{rng} | {d} | "
                         f"{RISK_LABEL.get(s.risk, s.risk)} | {s.desc or s.label} |")
        lines.append("")
    return "\n".join(lines)


__all__ = ["Spec", "SPECS", "GROUPS", "RISK_LABEL", "spec_of", "by_group",
           "coerce", "validate", "is_dangerous", "normalize_key", "env_value",
           "to_markdown", "LEGACY_KEYS", "ENV_PREFIX"]

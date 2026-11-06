"""Agent 编排：创作主编。

一条多轮对话 + 工具调用的闭环：用户和「创作主编」聊天，主编边聊边把
立项 / 世界观 / 人物 / 大纲 / 伏笔 沉淀进「设定集」（复用状态中枢的 DB 模型），
并在用户要求时调用 draft_chapter 代笔起草章节（带质检闸，用户审核采纳）。

工具协议走 OpenAI 风格 tools；不支持 tools 的模型自动降级为纯对话（仍可给建议，
只是不持久化结构化设定）。
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid

from sqlalchemy import select

from apps.api import llm, quality
from apps.api import costing
from apps.api import lessons
from apps.api.models import (
    Book, Character, Foreshadow, Setting, StyleProfile, Volume, WorldSetting,
    jd, jl, now, Task, Run, CharacterKnowledge, CharacterRelation,
)
from apps.api.orchestrator import context as ctx_builder
from apps.api.orchestrator import gates
from apps.api.orchestrator import graph as gen_graph
from apps.api.skills import loader as skills

log = logging.getLogger("novelforge")

META_KEY = "project_meta"  # Setting 表中按 book 维度存的立项扩展字段


# ----------------------------------------------------------------- 人设
SYSTEM_PROMPT = """你是「溯字」创作平台里的一位资深网络小说创作主编，也是用户最信任的写作合伙人。

你的目标：陪用户把一部能稳定连载、适合投稿平台的网文，从 0 一点点聊出来、搭起来、写出来。
你不是“一键生成机器”，而是真正的搭档——会追问、会纠偏、会鼓励、会在关键处给专业判断。

# 工作流程（按这个顺序循序渐进，不要跳步，也不要一上来就写正文）
1) 立项：书名、类型（男频/女频/分类）、基调、核心卖点（3条）、一句话梗概、目标字数/章节数、单章字数（建议 2000–2200 字）、简介、标签、对标作品。先和用户敲定这些，再往下走。
2) 世界观：力量/等级体系、势力格局、地理、货币、规则、特殊设定。体系要自洽、能制造冲突与爽点。
3) 人物：主角（人设/动机/口癖/成长弧）、重要配角、反派、关系网。主角要有“反套路”的钩子和持续行动的理由。
4) 大纲/分卷：主线脉络、分卷规划、爽点节奏（每章一个正反馈）、章末钩子、伏笔排布。
5) 写章：在设定齐备后逐章起草。开篇“黄金三章”必须强钩子、快节奏、尽早给出第一个爽点。

# 题材适配（v0.15）
本书的**主题材**决定度量衡与实力体系，**软标签**只影响风格措辞。
若下方提供了「本书题材」段落，请严格按其中的体系与措辞创作，不要默认套用仙侠玄幻设定
（例如都市文不要写"灵石/功法"，应按现实规则写"金钱/职级"）。
立项时若用户表达了题材，请调用 set_project_meta 传入 theme（主题材，单选）与 theme_tags（软标签，多选）。

# 投稿平台的通用硬约束（写正文与简介时必须遵守）
- 章节标题格式统一为「第N章 标题」（例如：第001章 退婚那天，我觉醒了躺平系统）。
- 单章正文 2000–2200 字，段落短（手机端 ≤5 行），节奏快。
- 开篇 3 章内必须抛出核心冲突与钩子，第一句就要抓人。
- 简介：口语化、卖点前置、≤250 字、可直接用于投稿准备。
- 标签 3–5 个，分类路径清晰。
- 禁用 AI 腔（如“不得不说”“总而言之”“就在这时”等）、禁用空洞总结句。
- 玩梗必须有落点：谐音/双关在后文两三句内要有呼应对象（引号词复现或其语义场着落）；
  没有落点的梗一律不写，禁止“被‘X’醒→不是真X”式自拆台。幽默靠反差，不靠堆砌。

# 你的工具（必须主动调用以沉淀设定，别只停留在聊天）
- set_project_meta：写入/更新立项信息（书名、类型、卖点、梗概、简介、标签、单章字数等）。
- save_world：写入一条世界观设定（category + key + value），可多次调用。
- save_character：写入/更新一个人物（按 name 去重）。★ 用户要求给人物**改名**（如"主角换个名字"）时，必须传 replaces=旧人物名——否则会残留两条同类角色（真实事故：用户改主角名后列表里同时出现新旧两个"男主"）。
- delete_character：★ 删除人物专用。用户要「清理误入库的人物 / 删掉XX / 这个角色不要了」时，必须调它把人物真正删掉，不要只在对话里标注「误入库·待删除」。
- save_outline：写入/更新一卷大纲（按 idx 去重）。
- add_foreshadow：埋一条伏笔。
- draft_chapter：代笔起草一章（传入 instruction 与可选 ch_idx）。草稿会交给用户审核采纳，你不要在对话里把整章正文念出来。
- read_chapter：读取指定章节的全文或摘要（想知道某章写了什么时必须调用，不要凭印象回答）。
- rename_chapter：★ 改章节标题专用。用户要「改第N章标题 / 换个章名 / 这章叫XX吧」时，必须调它把新标题写进库并同步章节清单，不要只在对话里给名字。
- add_timeline_event：往「纪年表」里记一条时间线事件（纪元/时间标记/章节/事件）。
- add_ledger_entry：往「度量衡账本」里记一笔数值变动（金钱/经验/年龄/功法/级别/声望）。
- check_consistency：跑一遍一致性自检（时间线是否倒流、账本余额是否断层、正文数值与账本是否对不上）。
- audit_chapter：对某一章做七维审计（去AI味 / 逻辑一致性 / 内容一致性 / 世界观一致性 / 大纲遵从度 / 实力体系一致性 / 叙事健康度）。

# 关于「已有内容」——务必遵守
每轮对话都会附带一份「当前项目状态快照」，那是系统从数据库实时读出来的**唯一事实来源**。
用户问「写了几章」「现有内容是什么」「进度到哪了」「某章讲了什么」时：
1. 先看快照里的章节清单，如实回答章数、字数、最新章号。
2. 需要细节就调 read_chapter，不要凭印象编。
3. 快照说 0 章就说 0 章，说 14 章就说 14 章。禁止说「没有记录/无法查看/请用户自己确认」。

# 关于数值与时间线——务必遵守
- 凡是文中出现金钱、经验、年龄、功法层级、境界级别的变化，都要调 add_ledger_entry 记账，余额由系统累加。
- 凡是跨越时间、涉及年份/年纪的关键节点，都要调 add_timeline_event 记进纪年表。

# 关于工具调用时机——**最容易做错的一条，务必遵守**
check_consistency 与 audit_chapter 都是**按需工具，不是每轮的开场仪式**。
用户在跟你聊设定、问进度、讨论人物、让你改某句话的时候，跟数值账本、
跟七维审计**毫无关系**。此时调用它们只会：
  1) 白白烧 token 和时间；
  2) 在界面上弹出「一致性自检 100 分」「正在审计第 15 章」这种
     与当前对话毫无关系的噪音，让用户以为系统卡在某个固定动作上。
判定规则：
- 用户消息里出现「一致性 / 账本 / 时间线 / 对不上 / 冲突 / 断层」→ 才调 check_consistency。
- 用户消息里出现「审计 / 检查质量 / 评分 / 有没有硬伤 / 降AI味」并明确指向某章 → 才调 audit_chapter。
- 用户要写新章**且**明确提到数值或时间可能冲突 → 才调 check_consistency。
- 其他情况一律**不要**调用，直接回答用户真正问的东西。
拿不准的时候，默认不调用。宁可少调一次，也不要让噪音盖过回答。

# 关于改稿——务必遵守
- 用户指出逻辑问题时，先判根因（梗无落点 / 因果缺失 / 前后矛盾 / 数值冲突），再给改法；修法必须针对根因。
- 禁止只把句子接顺：表面通顺不等于逻辑成立。实例——「我是被“饿”醒的。不是真饿，是系统
  在我脑子里敲锣」补成「不是真饿，是被系统吵醒的」只是接顺，“饿”仍然悬空；正确修法是
  删掉无源梗，让“被系统吵醒”独立成立。改完后自查：改动是否消除了根因，而不只是读着顺了。

# 交互纪律
- 一次对话聚焦推进一个环节；用提问帮用户想清楚，而不是替他拍板。
- 调用工具后，用一两句话向用户确认“已记下”，并自然引出下一步。
- 用户没准备好写正文时，绝对不要调用 draft_chapter。
- 若用户给的设定和已有设定冲突，先指出来让用户定夺，再写入。
- 始终用中文、口语、像真人主编在微信里聊稿子。

# 关于「读了才能说」——**极易违反，务必遵守**
你说「我读一下」「我看过了」「读完了」时，**必须真的调用 read_chapter**。
这是在系统里留下记录的动作，不是你在脑子里想一下。
反例（真实发生）：模型说「我读一下第2章和第3章。**读完了**。根因清楚了」
—— 但它**一次 read_chapter 都没调**，纯属虚构。用户看到的是"它说读完了"，
但系统里没有任何读取记录，后续推论全是空中楼阁。
**规则：任何"读取/查看/核对/确认"的动作，都必须先调 read_chapter，再基于真实内容回答。**

# 关于「讨论了就要动手」——**用户最烦的一条**
当用户**指出问题**（如"第2章结尾埋了伏笔，第3章忘了""这里逻辑不通"）时，
**不要只分析表态就结束**。用户要的是**修改结果**，不是你的分析报告。
正确流程：**分析 → 立刻调用工具执行修改 → 最后用一两句话说明改了什么**。
反例（真实发生，用户连续吐槽多次）：
  用户："第2章结束的地方埋了个伏笔，第三章似乎完全忘了"
  模型："你说得对，这是内容一致性问题…我先读一下第2章和第3章。" ← 然后就没动静了
  用户："你的修改又没有生成草稿" ← 用户被迫人工催
**规则：只要你的分析指向"需要改/需要写"，那这一轮就必须产出草稿，不能只给分析。**

# 关于「说了就要做到」——**最容易犯的错，务必遵守**
你怎么说，就怎么调工具。**绝对禁止只口头承诺、不调工具**。
反例（都是真实发生过的）：「稍等，这就起草第2章」「现在补上，真正起草」「正在生成，请稍候」
—— 说完这些话，如果**没有真的发出 tool_call**，系统里什么都不会发生，
用户干等半天只看到一句空话，下一轮又来道歉重来，非常伤体验。
正确做法：
- 要起草 → **当场调 draft_chapter**，然后在同一轮里用一两句话说明你写了什么。**先调工具，再说话。**
- 要记设定 → **当场调 set_project_meta / save_world / save_character / save_outline**。
- 只是在聊天、讨论、给建议 → 那就不许说「稍等/正在生成/这就去」这类承诺语，正常把话说完即可。
判断标准：**如果这句话让用户期待"马上会看到某个产物"，那这一轮就必须真的产出那个产物。**

★ 另外：**调工具请用标准的 function call 机制**，不要把工具调用写成正文里的文本
（如 <tool_call>{"name":"draft_chapter",...}</tool_call>）。系统虽然能兜底识别，
但写成标准调用更可靠、更省 token。
"""


# ----------------------------------------------------------------- 工具定义
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "set_project_meta",
            "description": "写入或更新一本书的立项信息（书名/类型/基调/卖点/梗概/简介/标签/单章字数等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "书名"},
                    "genre": {"type": "string", "description": "类型，如 男频玄幻 / 女频言情 / 都市爽文"},
                    "platform": {"type": "string", "description": "目标发行渠道/平台（可留空，generic 表示通用连载格式）"},
                    "tone": {"type": "string", "description": "基调，如 轻松搞笑 / 热血 / 暗黑"},
                    "logline": {"type": "string", "description": "一句话梗概（≤60字）：主角+遭遇+反常态做法+结果"},
                    "selling_points": {"type": "array", "items": {"type": "string"}, "description": "核心卖点，3条"},
                    "synopsis": {"type": "string", "description": "简介（≤250字，口语、卖点前置）"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "标签 3-5 个"},
                    "category": {"type": "string", "description": "平台分类路径，如 男频-玄幻-东方玄幻"},
                    "chapter_min": {"type": "integer", "description": "单章最少字数；用户未确认前不要填写"},
                    "chapter_max": {"type": "integer", "description": "单章最多字数；用户未确认前不要填写"},
                    "target_words": {"type": "integer", "description": "全书目标字数"},
                    "theme": {"type": "string", "description":
                              "★主题材（单选，决定度量衡与实力体系）。取值：xianxia(仙侠玄幻)/"
                              "urban(都市)/urban_suspense(都市悬疑)/scifi(科幻)/game(游戏电竞)，"
                              "也可传中文名（如「都市」「悬疑」）。仅当用户明确表达题材时填写。"},
                    "theme_tags": {"type": "array", "items": {"type": "string"},
                                   "description": "题材软标签（多选，只影响风格/封面措辞，可叠加），"
                                                  "如 [\"悬疑\",\"职场\"]；与上面的 tags 不同，这是题材维度的标签"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_world",
            "description": "写入一条世界观设定。category 如 境界/势力/地理/货币/规则/特殊设定；可多次调用分批沉淀。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "类别"},
                    "key": {"type": "string", "description": "条目名，如 练气期 / 天剑宗"},
                    "value": {"type": "string", "description": "具体设定内容"},
                },
                "required": ["category", "key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_character",
            "description": "写入或更新一个人物（按 name 去重）。★ 给已有人物改名/换人设时必须传 replaces=旧人物名，系统会把旧条目替换掉，不会残留两条同类角色。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "人物名"},
                    "role": {"type": "string", "description": "角色定位，如 男主/女主/反派/师父"},
                    "profile": {"type": "string", "description": "小传/人设"},
                    "voice": {"type": "string", "description": "说话方式与口癖"},
                    "arc": {"type": "string", "description": "成长弧"},
                    "first_chapter": {"type": "integer", "description": "首次出场章号（可选）"},
                    "replaces": {"type": "string", "description": "（可选）要替换掉的旧人物名。用户要求改主角名/换人设时必填，旧人物会被本人物替换，不会残留"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_character",
            "description": "★ #311 修复：从人物清单里删除一个人物（按 name 定位）。"
                          "用户让你「清理误入库的人物 / 删掉XX / 这个角色不要了」时，必须调用本工具真正删除，"
                          "不要只在对话里标注「误入库·待删除」——那样人物会一直挂在清单里（真实事故）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "要删除的人物名（精确匹配）"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_outline",
            "description": "写入或更新一卷大纲（按 idx 去重）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "idx": {"type": "integer", "description": "卷号，从 1 开始"},
                    "name": {"type": "string", "description": "卷名"},
                    "outline": {"type": "string", "description": "本卷大纲与爽点节奏"},
                    "start_ch": {"type": "integer", "description": "起始章号"},
                    "end_ch": {"type": "integer", "description": "结束章号"},
                },
                "required": ["idx", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_foreshadow",
            "description": "埋一条伏笔，后续写章时会被自动提示回收。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "伏笔内容"},
                    "status": {"type": "string", "description": "open/doing/done，默认 open"},
                    "target_vol": {"type": "integer", "description": "预计在第几卷回收（可选）"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_chapter",
            "description": "代笔起草一章。会结合设定集与上下文生成 2000-2200 字草稿并过质检闸，交给用户审核采纳。不要在对话里复述全文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "instruction": {"type": "string", "description": "本章要写什么：情节/钩子/爽点/人物走向等"},
                    "ch_idx": {"type": "integer", "description": "章号（可选，默认下一章）"},
                },
                "required": ["instruction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_chapter",
            "description": "读取指定章节的全文或摘要。想知道某章写了什么、想核对细节时必须调用，不要凭印象回答。",
            "parameters": {
                "type": "object",
                "properties": {
                    "ch_idx": {"type": "integer", "description": "章号"},
                    "full": {"type": "boolean", "description": "true=全文，false=只看摘要与开头，默认 true"},
                },
                "required": ["ch_idx"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_chapter",
            "description": "★ #310 修复：把指定章节的标题改成新标题，并同步进章节清单（左栏树）。"
                          "用户让你「改第N章的标题 / 换个章节名 / 这章叫XX吧」时，必须调用本工具落地，"
                          "不要只在对话里给个新名字——那样章节清单不会更新（真实事故）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "ch_idx": {"type": "integer", "description": "要改名的章号"},
                    "title": {"type": "string", "description": "新章节标题（不含「第N章」前缀，系统会自动拼成「第N章 标题」存库）"},
                },
                "required": ["ch_idx", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_timeline_event",
            "description": "往纪年表记一条时间线事件，用于锁死时间线、防止前后打架。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "事件标题，如 陈长安入宗"},
                    "ch_idx": {"type": "integer", "description": "发生章号"},
                    "era": {"type": "string", "description": "纪元/年号，如 开元"},
                    "time_label": {"type": "string", "description": "时间标记，如 开元三年春 / 主角十六岁"},
                    "detail": {"type": "string", "description": "事件详情"},
                    "kind": {"type": "string", "description": "出生/晋级/战斗/转折/事件/日常"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_ledger_entry",
            "description": "往度量衡账本记一笔数值变动（金钱/经验/年龄/功法/级别/声望的增减），余额由系统累加推导。",
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "主体，如 陈长安 / 陈家"},
                    "metric": {"type": "string", "description": "维度：金钱/经验/年龄/功法/级别/声望/其他"},
                    "delta": {"type": "number", "description": "变动量，可正可负"},
                    "ch_idx": {"type": "integer", "description": "发生章号"},
                    "unit": {"type": "string", "description": "单位，如 枚/两/岁/层/级/点"},
                    "reason": {"type": "string", "description": "变动原因，如 卖出妖丹所得"},
                },
                "required": ["subject", "metric", "delta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_consistency",
            "description": "跑一遍一致性自检：时间线是否倒流、账本余额是否断层、正文数值与账本是否对不上。返回问题清单。只在用户问到数值/时间线/账本/冲突时才调用；闲聊、聊设定、问进度时不要调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "ch_idx": {"type": "integer", "description": "只查某一章（可选，不传则查全书）"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "audit_chapter",
            "description": "对某一章做七维内容审计：去AI味、逻辑一致性、内容一致性、世界观一致性、大纲遵从度、实力体系一致性、叙事健康度。返回评分与问题清单。只在用户明确要求审计/评分某章时调用；不要每轮都自动审计。",
            "parameters": {
                "type": "object",
                "properties": {
                    "ch_idx": {"type": "integer", "description": "章号"},
                },
                "required": ["ch_idx"],
            },
        },
    },
    # ---- v0.13.16 C4-3：短剧工坊工具 ----
    {
        "type": "function",
        "function": {
            "name": "drama_list_projects",
            "description": "列出某本书的短剧项目（含集数与工作流状态摘要）。用户问「短剧/分镜/剧集」相关情况时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drama_queue_status",
            "description": "查询短剧媒体生成队列的当前状态（各类型任务排队/运行/完成计数与最近任务明细）。用户问「生成到哪了/队列情况」时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drama_generate_batch",
            "description": "对某本书第一个短剧项目的指定集批量生成分镜媒体（image=图像/video=视频/audio=配音）。视频任务自动依赖图像。入队后立即返回任务 id，生成进度用户可在短剧工坊页看到。",
            "parameters": {
                "type": "object",
                "properties": {
                    "episode": {"type": "integer", "description": "集号，从 1 开始"},
                    "target": {"type": "string", "description": "image/video/audio，默认 image"},
                },
                "required": ["episode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drama_generate_grid",
            "description": "对某本书第一个短剧项目的指定集生成多宫格分镜图（一张图装 N 个分镜格，供导演快速审阅整集节奏）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "episode": {"type": "integer", "description": "集号"},
                    "cells": {"type": "integer", "description": "格数：2/4/6/9，默认 9"},
                },
                "required": ["episode"],
            },
        },
    },
]


# ----------------------------------------------------------------- 立项扩展字段存取
def get_meta(db, book_id: str) -> dict:
    row = db.get(Setting, f"book:{book_id}:{META_KEY}")
    return jl(row.value, {}) if row else {}


def set_meta(db, book_id: str, patch: dict) -> None:
    row = db.get(Setting, f"book:{book_id}:{META_KEY}")
    cur = jl(row.value, {}) if row else {}
    cur.update({k: v for k, v in patch.items() if v not in (None, "", []) or k in cur})
    if row is None:
        row = Setting(key=f"book:{book_id}:{META_KEY}", value=jd(cur))
        db.add(row)
    else:
        row.value = jd(cur)
    db.commit()


# ----------------------------------------------------------------- 工具执行
async def execute_tool(db, book: Book, tc: dict, emit) -> str:
    name = tc.get("name")
    args = tc.get("arguments") or {}
    try:
        if name == "set_project_meta":
            return await _t_set_meta(db, book, args, emit)
        if name == "save_world":
            return await _t_save_world(db, book, args, emit)
        if name == "save_character":
            return await _t_save_character(db, book, args, emit)
        if name == "delete_character":
            return await _t_delete_character(db, book, args, emit)
        if name == "save_outline":
            return await _t_save_outline(db, book, args, emit)
        if name == "add_foreshadow":
            return await _t_add_foreshadow(db, book, args, emit)
        if name == "draft_chapter":
            return await _t_draft_chapter(db, book, args, emit)
        if name == "read_chapter":
            return await _t_read_chapter(db, book, args, emit)
        if name == "rename_chapter":
            return await _t_rename_chapter(db, book, args, emit)
        if name == "add_timeline_event":
            return await _t_add_timeline_event(db, book, args, emit)
        if name == "add_ledger_entry":
            return await _t_add_ledger_entry(db, book, args, emit)
        if name == "check_consistency":
            return await _t_check_consistency(db, book, args, emit)
        if name == "audit_chapter":
            return await _t_audit_chapter(db, book, args, emit)
        if name == "drama_list_projects":
            return await _t_drama_list_projects(db, book, emit)
        if name == "drama_queue_status":
            return await _t_drama_queue_status(db, book, emit)
        if name == "drama_generate_batch":
            return await _t_drama_generate_batch(db, book, args, emit)
        if name == "drama_generate_grid":
            return await _t_drama_generate_grid(db, book, args, emit)
        return f"未知工具：{name}"
    except Exception as e:  # 工具出错不能打断对话
        return f"工具 {name} 执行出错：{type(e).__name__}: {e}"


async def _t_set_meta(db, book, a, emit) -> str:
    for k in ("title", "genre", "platform", "chapter_min", "chapter_max", "target_words"):
        if a.get(k) not in (None, ""):
            setattr(book, k, a[k])

    # v0.15 题材解耦（G9）：theme=主题材（单选，定度量衡/实力体系）；
    # tags=标签（多选，只影响提示词）。
    # 校验策略（计划书 §六 风险 8）：未知值不报错、留空/丢弃——
    # 因为老书 genre 是自由文本，可能没有对应题材包；此时走兜底即可。
    from apps.api import genres
    if a.get("theme") not in (None, ""):
        book.theme = genres.normalize_key(a["theme"], db)   # 未知 → ''，走兜底

    if a.get("theme_tags") not in (None, [], ""):
        raw = a["theme_tags"]
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        book.tags = ",".join(genres.normalize_keys(raw, db))

    if a.get("tone"):
        sp = db.scalar(select(StyleProfile).where(StyleProfile.book_id == book.id))
        if sp:
            sp.tone = a["tone"]
    db.commit()
    meta = {k: a[k] for k in ("logline", "selling_points", "synopsis", "tags", "category") if a.get(k) not in (None, "", [])}
    set_meta(db, book.id, meta)
    theme_txt = f"｜题材 {book.theme}" if book.theme else ""
    await emit({"kind": "action", "type": "meta",
                "summary": f"已记录立项：{a.get('title') or book.title}（{a.get('genre') or book.genre}）{theme_txt}"})
    return "立项信息已保存。"


async def _t_save_world(db, book, a, emit) -> str:
    cat, key, val = a.get("category", "其他"), a.get("key", ""), a.get("value", "")
    if not key:
        return "世界观条目缺少 key，未保存。"
    row = db.scalar(select(WorldSetting).where(
        WorldSetting.book_id == book.id, WorldSetting.category == cat, WorldSetting.key == key))
    if row:
        row.value = val
    else:
        db.add(WorldSetting(id=f"ws_{uuid.uuid4().hex[:12]}", book_id=book.id,
                            category=cat, key=key, value=val))
    db.commit()
    await emit({"kind": "action", "type": "world", "summary": f"世界观【{cat}】{key} 已记录"})
    return f"已保存世界观：{cat}/{key}。"


async def _t_save_character(db, book, a, emit) -> str:
    name = (a.get("name") or "").strip()
    if not name:
        return "人物缺少 name，未保存。"
    from apps.api.validators import invalid_character_reason, valid_character_name
    if not valid_character_name(name):
        # 大纲卖点/伏笔条目不是人物：明确告知原因，让模型改走 save_outline/save_world
        return (f"未保存：『{name[:40]}』不是合法人物名——{invalid_character_reason(name)}。"
                f"如果这是大纲/卖点/伏笔内容，请改用 save_outline / save_world / add_foreshadow。")
    replaces = (a.get("replaces") or "").strip()
    row = db.scalar(select(Character).where(Character.book_id == book.id, Character.name == name))
    # ★ v0.27.1 改名语义：传了 replaces（旧名）→ 把旧人物替换成新人物，
    #   不再残留两条同类角色（如两个"男主"）。
    old_row = None
    if replaces and replaces != name:
        old_row = db.scalar(select(Character).where(
            Character.book_id == book.id, Character.name == replaces))
    merged_from_id = ""
    if old_row is not None:
        merged_from_id = old_row.id
        if row is not None and row.id != old_row.id:
            # 新旧两条都存在 → 合并：以新名为准，删除旧行
            db.delete(old_row)
            db.flush()
        else:
            # 改名：沿用旧行（保留 id，章节出场记录等引用不悬空）
            row = old_row
            row.name = name
    if not row:
        row = Character(id=f"ch_{uuid.uuid4().hex[:12]}", book_id=book.id, name=name)
        db.add(row)
    for k in ("role", "profile", "voice", "arc"):
        if a.get(k):
            setattr(row, k, a[k])
    if a.get("first_chapter"):
        row.first_chapter = int(a["first_chapter"])
    db.commit()
    # ★ 衍生表同步：角色知识/关系图按 name 或 character_id 关联，改名/合并后要跟着改，
    #   否则出现悬空引用（一致性检查会误报"孤儿角色"）。
    if merged_from_id:
        try:
            final_id = row.id
            for kn in db.scalars(select(CharacterKnowledge).where(
                    CharacterKnowledge.book_id == book.id,
                    CharacterKnowledge.character_name == replaces)).all():
                kn.character_name = name
            for kn in db.scalars(select(CharacterKnowledge).where(
                    CharacterKnowledge.book_id == book.id,
                    CharacterKnowledge.character_id == merged_from_id)).all():
                kn.character_id = final_id
            for rel in db.scalars(select(CharacterRelation).where(
                    CharacterRelation.book_id == book.id)).all():
                if rel.from_id == merged_from_id:
                    rel.from_id = final_id
                    rel.from_name = name
                if rel.to_id == merged_from_id:
                    rel.to_id = final_id
                    rel.to_name = name
            db.commit()
        except Exception as e:                       # noqa: BLE001
            log.warning("角色衍生表同步失败（不影响主流程）：book=%s %s -> %s: %s",
                        book.id, replaces, name, e)
    if old_row is not None:
        await emit({"kind": "action", "type": "character",
                    "summary": f"人物《{replaces}》已更新为《{name}》{a.get('role','')}（旧条目已替换）"})
        return f"已把人物《{replaces}》更新为《{name}》（旧条目已替换，不会残留两条）。"
    await emit({"kind": "action", "type": "character", "summary": f"人物《{name}》{a.get('role','')} 已记录"})
    return f"已保存人物：{name}。"


async def _t_delete_character(db, book, a, emit) -> str:
    """★ #311 修复：按 name 删除人物（连带清理衍生表），让清单真正不再显示。

    之前缺这个工具，主编只能口头标注「误入库·待删除」，人物永远留在清单里。
    """
    from apps.api.models import Character as Ch, CharacterKnowledge, CharacterRelation
    name = (a.get("name") or "").strip()
    if not name:
        return "delete_character 缺少 name，未删除。"
    c = db.scalar(select(Ch).where(Ch.book_id == book.id, Ch.name == name))
    if not c:
        return f"未找到人物《{name}》，可能已删除或名称不匹配。"
    cid = c.id
    # 衍生表清理（与 delete_character 端点 / 改名合并清理口径一致）
    for kn in db.scalars(select(CharacterKnowledge).where(
            CharacterKnowledge.book_id == book.id,
            (CharacterKnowledge.character_id == cid)
            | (CharacterKnowledge.character_name == name))).all():
        db.delete(kn)
    for rel in db.scalars(select(CharacterRelation).where(
            CharacterRelation.book_id == book.id)).all():
        if rel.from_id == cid or rel.to_id == cid:
            db.delete(rel)
    db.delete(c)
    db.commit()
    await emit({"kind": "action", "type": "character_delete", "summary": f"人物《{name}》已删除"})
    return f"已从人物清单删除《{name}》。"


async def _t_save_outline(db, book, a, emit) -> str:
    idx = int(a.get("idx", 1))
    row = db.scalar(select(Volume).where(Volume.book_id == book.id, Volume.idx == idx))
    if not row:
        row = Volume(id=f"vo_{uuid.uuid4().hex[:12]}", book_id=book.id, idx=idx)
        db.add(row)
    row.name = a.get("name", f"第{idx}卷")
    if a.get("outline"):
        row.outline = a["outline"]
    if a.get("start_ch"):
        row.start_ch = int(a["start_ch"])
    if a.get("end_ch"):
        row.end_ch = int(a["end_ch"])
    db.commit()
    await emit({"kind": "action", "type": "outline", "summary": f"大纲·卷{idx}《{row.name}》已记录"})
    return f"已保存卷{idx}大纲。"


async def _t_add_foreshadow(db, book, a, emit) -> str:
    content = a.get("content", "").strip()
    if not content:
        return "伏笔内容为空，未保存。"
    fs = Foreshadow(id=f"fs_{uuid.uuid4().hex[:12]}", book_id=book.id, content=content,
                    status=a.get("status") or "open",
                    target_vol=int(a.get("target_vol") or 0))
    db.add(fs)
    db.commit()
    await emit({"kind": "action", "type": "foreshadow", "summary": f"伏笔已埋：{content[:30]}…"})
    return "伏笔已记录。"


# ----------------------------------------------------------------- 任务台账（v0.9.1）
# 代笔是长任务。把它的状态/进度/结果落到 DB 的 Task 表：
# - 前端刷新后能从 /books/{id}/tasks 恢复「进行中 or 已完成 or 被中断」；
# - 每走一步都追加一条过程事件，供气泡活动流展示 + SSE 增量回放。
# 不 import main.py（会循环依赖），直接操作 Task 模型。


def _task_new(db, book, kind: str, target: int, progress: str = "准备中…") -> Task:
    t = Task(id=f"tk_{uuid.uuid4().hex[:12]}", book_id=book.id, kind=kind,
             target=int(target or 0), status="running", progress=progress[:128])
    db.add(t)
    db.commit()
    return t


def _task_event(db, task: Task, ev: dict, progress: str | None = None,
                status: str | None = None, result: dict | None = None,
                error: str | None = None) -> None:
    """追一条任务事件（agent 侧版本）。

    v0.18 T1 说明：main.py 版本已抽到 `apps.api.services.tasks.task_event`。
    两者**行为存在细微差异**（本版本直接改内存中的 task 对象；service 版会
    重新查一次行再改），当前**刻意保留本版本**以维持 agent 侧既有行为不变
    —— 合并需要单独跑一轮回归验证，不在 T1 范围内。改这里请同步检查
    services/tasks.py 的对应实现，别让两份继续漂移。
    """
    events = jl(task.events_json, []) or []
    events.append(ev)
    task.events_json = jd(events)
    task.seq = int(task.seq or 0) + 1
    if progress is not None:
        task.progress = progress[:128]
    if status:
        task.status = status
    if result is not None:
        task.result_json = jd(result)
    if error is not None:
        task.error = error[:512]
    if status in ("done", "error", "abandoned"):
        task.finished_at = now()
    task.updated_at = now()
    db.commit()


def _record_llm_call(db, book, m, task: str, r) -> None:
    """#313：把一次 llm.complete 的用量落到 LLMCall 台账（fail-open）。

    ★ 为什么从 agent 链路补这一刀：之前只有 orchestrator/graph.py 的 Run 管线在记，
      chat_stream 的对话生成、draft_chapter 的代笔、整章精修这三处**从不写 LLMCall**，
      导致用户日常聊天/写作 0 行台账 → 用量与成本页 by_task 永远 0、洞察页 feature_heat 缺失。
    """
    try:
        owner = (getattr(book, "owner_id", "") or "").strip()
        if not owner:
            return
        err = getattr(r, "error", "") or ""
        costing.record(
            db,
            task=task,
            role=getattr(m, "role", "") or "writer",
            user_id=owner,
            book_id=getattr(book, "id", ""),
            model_id=getattr(m, "id", ""),
            input_tokens=getattr(r, "input_tokens", 0) or 0,
            output_tokens=getattr(r, "output_tokens", 0) or 0,
            cost=getattr(r, "cost", None),
            status=("error" if err else "ok"),
            error=err[:512],
        )
    except Exception:  # noqa: BLE001
        pass


async def _t_draft_chapter(db, book, a, emit) -> str:
    instruction = a.get("instruction", "")
    ch_idx = int(a.get("ch_idx") or 0)
    from apps.api.models import Chapter as Ch
    if not ch_idx:
        last = db.scalar(select(Ch.idx).where(Ch.book_id == book.id).order_by(Ch.idx.desc()))
        ch_idx = (last or 0) + 1
    await emit({"kind": "stage", "text": f"正在代笔起草第 {ch_idx} 章…"})
    m = gen_graph.pick_model(db, "writer") or gen_graph.pick_model(db, "default")
    if not m:
        await emit({"kind": "error", "text": "未配置可用模型，无法代笔。请先在配置中心添加模型并设为默认。"})
        return "无法代笔：缺少模型。"

    # 建任务台账：代笔是长任务，刷新后靠它恢复"进行中 / 已完成 / 被中断"
    task = _task_new(db, book, "draft", ch_idx, f"正在代笔第 {ch_idx} 章…")
    _task_event(db, task, {"kind": "stage", "text": f"正在代笔起草第 {ch_idx} 章…"},
                progress=f"正在代笔第 {ch_idx} 章…")
    # 注意：这里外层 kind="task" 是事件类型，内层任务类型必须换键名。
    # 原先两个都写 "kind"，Python 字典字面量后者覆盖前者，前端收到 kind="draft"，
    # 事件分流直接失效（对话框里看不到过程流）。
    await emit({"kind": "task", "task_id": task.id, "task_kind": "draft",
                "ch_idx": ch_idx, "status": "running", "progress": task.progress})
    await emit({"kind": "stage", "text": f"正在装配上下文与写作纪律…"})

    pack = ctx_builder.build_chapter_context(db, book, ch_idx, user_instruction=instruction)
    sk = skills.load("chapter_write", db)
    prompt = skills.render(
        sk,
        style_dna=pack.style_dna,
        world_view=pack.world_view,
        volume_outline=pack.volume_outline,
        brief="（由对话触发代笔）",
        characters=pack.characters,
        foreshadows=pack.foreshadows,
        early_summaries=pack.early_summaries,
        recent_chapters=pack.recent_chapters,
        user_instruction=instruction,
        ch_idx=f"{ch_idx:03d}",
        ch_title="",
        timeline=pack.timeline,
        ledger=pack.ledger,
        continuity=pack.continuity,
    )
    # #315：把本书历史避坑经验附在代笔提示词末尾（用户反复纠正过的问题）。
    #   ★ 为什么用「追加」而不是改 skills 模板：模板是全局共享资产，
    #     而避坑是**每本书自己的** —— 不能为了动态数据去改静态模板。
    _draft_lessons = lessons.render_block(lessons.recent_lessons(db, book.id))
    if _draft_lessons:
        prompt = prompt + "\n\n" + _draft_lessons
    await emit({"kind": "stage", "text": f"正在生成第 {ch_idx} 章正文（模型调用中，约 1–3 分钟）…"})
    _task_event(db, task, {"kind": "stage", "text": "模型生成正文中…"}, progress="模型生成正文中…")
    r = await llm.complete(m, [{"role": "user", "content": prompt}], timeout=900)
    _record_llm_call(db, book, m, "chapter_write", r)
    text = (r.text or "").strip()
    if not text:
        # 空响应自动重试一次：思考型模型偶发把预算耗在推理链上，重试多半能成。
        # 两次都空才算真失败，并把可操作的排查提示带给用户。
        await emit({"kind": "stage", "text": "模型返回为空，自动重试一次…"})
        _task_event(db, task, {"kind": "stage", "text": "模型返回为空，自动重试…"},
                    progress="空响应重试中…")
        r = await llm.complete(m, [{"role": "user", "content": prompt}], timeout=900)
        _record_llm_call(db, book, m, "chapter_write", r)
        text = (r.text or "").strip()
    if not text:
        hint = r.error or "空内容（可能为模型过载或网关拦截，稍后重试；若反复出现请检查模型 max_tokens 配置）"
        _task_event(db, task, {"kind": "error", "text": "模型未返回正文：" + hint},
                    status="error", error=hint)
        await emit({"kind": "task", "task_id": task.id, "task_kind": "draft",
                    "ch_idx": ch_idx, "status": "error"})
        await emit({"kind": "error", "text": "模型未返回正文：" + hint})
        return "代笔失败：模型无内容。"
    # ★ v1.0-E4.20 连续性护栏：新章与紧邻上一章高度重复（疑似重复同一场景/对话）→ 重生一次。
    #   ★ 为什么只重生一次：重复是"写偏了"不是"写坏了"，给一次纠正机会即可；
    #     重生仍失败则 fail-open 接受（不能为了去重把整章卡死）。
    #   ★ 判定用 gates.repeat_rate：与上一章 12-gram 重叠 > 15% 视为重复场景。
    _prev_full = pack.stats.get("_prev_texts", [])
    if _prev_full:
        try:
            _ov = gates.repeat_rate(text, _prev_full[-1])
        except Exception:                       # noqa: BLE001
            _ov = 0.0
        if _ov > 0.15:
            await emit({"kind": "stage", "text": "检测到与上一章高度重复，正在换写法重写…"})
            _task_event(db, task, {"kind": "stage", "text": "重生（避免重复上一章场景）"},
                        progress="避免重复，重写中…")
            r2 = await llm.complete(m, [{"role": "user", "content": prompt +
                "\n\n【纠正】上一版与上一章内容高度重复，疑似重复描写同一场景/对话。"
                "必须推进全新剧情进展，沿用上一章结尾的悬念继续，不得复述前文已发生的情节。"}],
                timeout=900)
            _record_llm_call(db, book, m, "chapter_write", r2)
            text2 = (r2.text or "").strip()
            if text2:
                try:
                    _ov2 = gates.repeat_rate(text2, _prev_full[-1])
                except Exception:               # noqa: BLE001
                    _ov2 = 0.0
                if _ov2 <= 0.15:
                    text = text2
    # 统一标题行
    lines = text.splitlines()
    if not (lines and re.match(r"^#\s*第\s*\d+\s*章", lines[0].strip())):
        body = text
        if lines and lines[0].lstrip().startswith("#"):
            body = "\n".join(lines[1:]).strip()
        text = f"# 第{ch_idx:03d}章 \n\n{body}".strip()
    content = text
    sp = db.scalar(select(StyleProfile).where(StyleProfile.book_id == book.id))
    prev = pack.stats.get("_prev_texts", [])
    await emit({"kind": "stage", "text": f"正在过质量闸门…"})
    _task_event(db, task, {"kind": "stage", "text": "质量闸门检查中…"}, progress="质量闸门检查中…")
    report = gates.run_gates(content, book, sp, prev,
                             realm_whitelist=gates.realm_whitelist_for(db, book.id))

    # ---- 质量技能自动调用：diagnose → polish → regate -------------------
    polished = None
    diag = None
    if int(getattr(book, "polish_enabled", 1)) == 1 and report.score < int(getattr(book, "polish_threshold", 90)):
        chars = [{"name": c.name} for c in db.scalars(
            select(Character).where(Character.book_id == book.id)).all()]
        diag = quality.diagnose(gates.strip_title(content), book, chars, sp)
        await emit({"kind": "stage", "text": f"正在整章精修（{report.score} 分 < 阈值 "
                                              f"{book.polish_threshold}）…"})
        _task_event(db, task, {"kind": "stage", "text": "整章精修中…"}, progress="整章精修中…")
        polished = await _polish(db, book, content, pack, sp, diag, ch_idx, emit)
        if polished:
            pre_score = report.score
            content = polished
            report = gates.run_gates(content, book, sp, prev,
                                     realm_whitelist=gates.realm_whitelist_for(db, book.id))
            await emit({"kind": "stage", "text": f"精修完成：质检 {pre_score} → {report.score} 分"})
            _task_event(db, task, {"kind": "stage", "text": f"精修完成：{pre_score} → {report.score} 分"},
                        progress=f"精修完成 {report.score} 分")
    else:
        chars = [{"name": c.name} for c in db.scalars(
            select(Character).where(Character.book_id == book.id)).all()]
        diag = quality.diagnose(gates.strip_title(content), book, chars, sp)

    # ★★ v1.0-E4.6：把本次生成所用提示词的**指纹**写进任务结果
    #
    #   ★ 为什么必须存：采纳发生在**生成之后**（可能在别的请求、甚至隔天），
    #     要算"提示词版本 → 采纳率/质量"，必须让指纹"活"到采纳那一刻。
    #     任务台账是唯一的持久化载体。
    #
    #   ★ 隐私：只存 SHA256 前 16 位（`prompt_fingerprint`），**原文不落地**。
    #
    #   ★ 为什么用 getattr 兜底：`_flow_ctx` 可能不存在（历史任务/测试替身），
    #     不能让埋点把生成搞挂。
    _p_hash = ""
    try:
        from apps.api import adoption as _ad
        _pc = getattr(_t_draft_chapter, "_last_flow_ctx", None)
        _raw = getattr(_pc, "prompt_hash", "") if _pc is not None else ""
        _p_hash = str(_raw or "")
        if not _p_hash:
            # 兜底：用提示词文件内容算指纹（比没有强，能区分"改没改过"）
            _sk = skills.load("chapter_write", db)
            if _sk is not None:
                _p_hash = _ad.prompt_fingerprint(
                    getattr(_sk, "body", "") or getattr(_sk, "raw", "") or "")
    except Exception:                                   # noqa: BLE001
        _p_hash = ""

    _task_event(db, task,
                {"kind": "chapter_draft", "ch_idx": ch_idx, "gate_score": report.score},
                status="done",
                result={"ch_idx": ch_idx, "gate_score": report.score,
                        "words": gates.count_words(gates.strip_title(content)),
                        "title": (lines[0].lstrip('# ').strip() if lines else ""),
                        "content": content,
                        # ★ 提示词指纹（哈希，非原文）—— 供采纳侧做 A/B 分析
                        "prompt_hash": _p_hash,
                        "gate_report": report.to_dict()})
    await emit({"kind": "task", "task_id": task.id, "task_kind": "draft",
                "ch_idx": ch_idx, "status": "done", "gate_score": report.score})
    # task_id 一并带给草稿卡：丢弃/恢复接口都要凭 task_id 定位台账行
    await emit({"kind": "chapter_draft", "ch_idx": ch_idx, "text": content,
          "gate_score": report.score, "gate_report": report.to_dict(),
          "task_id": task.id,
          "diagnosis": diag.to_dict() if diag else None})
    return f"已起草第{ch_idx}章（约 {gates.count_words(gates.strip_title(content))} 字，质检 {report.score} 分），请用户在草稿卡中审核采纳。"


async def draft_chapter_for_batch(db, book, ch_idx: int, *, instruction: str = "",
                                  emit=None) -> dict:
    """批量代笔用的单章入口（v0.19 F1）。

    与 `_t_draft_chapter`（对话工具版）走**完全相同的**上下文装配 + 质检闸 +
    草稿台账逻辑，区别只是：
      * 返回值从「给模型看的字符串」改成「结构化 dict」（批量编排要判断成败）
      * emit 可选（批量场景可能没有事件通道）

    ★ 刻意不复制实现：把 `_t_draft_chapter` 的核心包一层，避免两份逻辑漂移。
    """
    async def _noop(ev):
        pass

    a = {"instruction": instruction or "", "ch_idx": int(ch_idx or 0)}
    _emit = emit or _noop

    # 复用对话工具版的完整流程；它内部会发 chapter_draft 事件
    before = _count_drafts(db, book)
    try:
        await _t_draft_chapter(db, book, a, _emit)
    except Exception as e:
        return {"ok": False, "ch_idx": int(ch_idx or 0),
                "error": f"{type(e).__name__}: {e}"}

    # 从最近一条 draft 任务读回结果（判断成败 + 取字数/分数）
    info = _latest_draft_result(db, book, int(ch_idx or 0))
    if not info:
        return {"ok": False, "ch_idx": int(ch_idx or 0),
                "error": "未产出草稿（模型可能无响应）"}
    return {"ok": True, "ch_idx": info.get("ch_idx", ch_idx),
            "words": info.get("words", 0),
            "gate_score": info.get("gate_score", 0),
            "task_id": info.get("task_id", "")}


def _count_drafts(db, book) -> int:
    from apps.api.models import Task as _T
    from sqlalchemy import func as _f
    return int(db.scalar(_f.count(_T.id).where(
        _T.book_id == book.id, _T.kind == "draft")) or 0)


def _latest_draft_result(db, book, ch_idx: int) -> dict | None:
    """取该书最近一条 draft 任务的结果（批量判成败用）。"""
    from apps.api.models import Task as _T
    row = db.scalar(select(_T).where(_T.book_id == book.id, _T.kind == "draft")
                    .order_by(_T.started_at.desc()).limit(1))
    if row is None or row.status != "done":
        return None
    res = jl(row.result_json, {}) or {}
    if not res:
        return None
    return {"ch_idx": res.get("ch_idx", ch_idx),
            "words": res.get("words", 0),
            "gate_score": res.get("gate_score", 0),
            "task_id": row.id}


async def _t_read_chapter(db, book, a, emit) -> str:
    """读取某章：主编想核对细节时用，避免凭印象瞎答。"""
    from apps.api.models import Chapter as Ch
    try:
        ch_idx = int(a.get("ch_idx"))
    except (TypeError, ValueError):
        return "read_chapter 缺少有效 ch_idx。"
    c = db.scalar(select(Ch).where(Ch.book_id == book.id, Ch.idx == ch_idx))
    if not c:
        allc = db.scalars(select(Ch.idx).where(Ch.book_id == book.id).order_by(Ch.idx)).all()
        return (f"第{ch_idx}章不存在。本书现有章号："
                + ("、".join(str(x) for x in allc[:40]) if allc else "（暂无任何章节）"))
    full = a.get("full")
    full = True if full is None else bool(full)
    await emit({"kind": "action", "type": "read", "summary": f"已读取第 {ch_idx} 章"})
    head = f"第{c.idx:03d}章 《{c.title or '（无题）'}》｜{c.words or 0}字｜质检{c.gate_score}分"
    if c.summary:
        head += f"\n摘要：{c.summary.strip()}"
    if full:
        return head + "\n\n正文：\n" + (c.content or "")[:12000]
    return head + "\n\n开头 400 字：\n" + (c.content or "")[:400]


async def _t_rename_chapter(db, book, a, emit) -> str:
    """★ #310 修复：把指定章节标题写进库（并同步 git 文件），让左栏章节树真正刷新。

    之前没有这个工具，主编只能在聊天里给新名字，章节清单从不更新。
    """
    from apps.api.models import Chapter as Ch
    try:
        ch_idx = int(a.get("ch_idx"))
    except (TypeError, ValueError):
        return "rename_chapter 缺少有效 ch_idx。"
    title = (a.get("title") or "").strip()
    if not title:
        return "rename_chapter 缺少 title，未改名。"
    # 去掉可能误带过来的「第N章」前缀，统一由系统拼装
    title = re.sub(r"^第\s*\d+\s*章\s*", "", title).strip()
    c = db.scalar(select(Ch).where(Ch.book_id == book.id, Ch.idx == ch_idx))
    if not c:
        allc = db.scalars(select(Ch.idx).where(Ch.book_id == book.id).order_by(Ch.idx)).all()
        return (f"第{ch_idx}章不存在，无法改名。本书现有章号："
                + ("、".join(str(x) for x in allc[:40]) if allc else "（暂无任何章节）"))
    old_title = c.title or ""
    c.title = title
    c.updated_at = now()
    db.commit()
    # ★ 同步 git 文件里的标题行（与 patch_chapter 写路径一致：有正文才重写）
    if c.content:
        try:
            from apps.api import gitrepo
            gitrepo.write_file(book.id, f"正文/第{ch_idx:03d}章.md",
                              f"# 第{ch_idx:03d}章 {title}\n\n{c.content}\n",
                              commit_msg=f"重命名第{ch_idx:03d}章：{old_title} → {title}")
        except Exception as e:                       # noqa: BLE001
            log.warning("rename_chapter 同步 git 失败（不影响标题入库）：book=%s ch=%s: %s",
                        book.id, ch_idx, e)
    await emit({"kind": "action", "type": "rename", "ch_idx": ch_idx,
                "summary": f"第{ch_idx:03d}章已改名：《{old_title}》→《{title}》"})
    return f"已将第{ch_idx:03d}章标题改为《{title}》（章节清单已同步）。"


async def _t_add_timeline_event(db, book, a, emit) -> str:
    from apps.api import consistency
    title = (a.get("title") or "").strip()
    if not title:
        return "纪年事件缺少 title，未保存。"
    row = consistency.add_event(
        db, book.id, title=title, ch_idx=int(a.get("ch_idx") or 0),
        era=a.get("era", ""), time_label=a.get("time_label", ""),
        detail=a.get("detail", ""), kind=a.get("kind") or "事件")
    await emit({"kind": "action", "type": "timeline",
                "summary": f"纪年表已记：{row.time_label or row.title}"})
    return f"纪年表已记录：{row.era}{row.time_label}｜{row.title}（第{row.ch_idx}章）"


async def _t_add_ledger_entry(db, book, a, emit) -> str:
    from apps.api import consistency
    subject = (a.get("subject") or "").strip()
    if not subject:
        return "记账缺少 subject（主体），未保存。"
    try:
        delta = float(a.get("delta") or 0)
    except (TypeError, ValueError):
        return "记账 delta 不是数字，未保存。"
    row = consistency.add_ledger(
        db, book.id, subject=subject, metric=a.get("metric") or "其他", delta=delta,
        ch_idx=int(a.get("ch_idx") or 0), unit=a.get("unit") or "", reason=a.get("reason") or "")
    await emit({"kind": "action", "type": "ledger",
                "summary": f"账本：{row.subject}·{row.metric} {row.delta:+g}{row.unit} → 余 {row.balance}"})
    return (f"已记账：{row.subject}·{row.metric} {row.delta:+g}{row.unit or ''}，"
            f"变动后余额 {row.balance}{row.unit or ''}（第{row.ch_idx}章）")


async def _t_check_consistency(db, book, a, emit) -> str:
    from apps.api import consistency
    ch = a.get("ch_idx")
    rep = consistency.run_consistency(db, book.id, int(ch) if ch else None)
    await emit({"kind": "action", "type": "consistency",
                "summary": f"一致性自检 {rep['score']} 分（{rep['errors']} 硬伤 / {rep['warns']} 提醒）"})
    if not rep["issues"]:
        return f"一致性自检通过：100 分，时间线与账本均无异常。"
    lines = [f"一致性自检 {rep['score']} 分：{rep['errors']} 个硬伤、{rep['warns']} 个提醒。"]
    for it in rep["issues"][:12]:
        lines.append(f"- [{it['kind']}/{it['severity']}] 第{it.get('ch_idx', 0)}章 "
                     f"{it.get('scope', '')}：{it['message']}")
    if len(rep["issues"]) > 12:
        lines.append(f"- 另有 {len(rep['issues']) - 12} 条已省略")
    return "\n".join(lines)


async def _t_audit_chapter(db, book, a, emit) -> str:
    from apps.api import audit as audit_mod
    try:
        ch_idx = int(a.get("ch_idx"))
    except (TypeError, ValueError):
        return "audit_chapter 缺少有效 ch_idx。"
    rep = await audit_mod.audit_chapter(db, book, ch_idx, emit=emit)
    if rep.get("error"):
        return rep["error"]
    audit_mod.save_report(db, book.id, rep, ch_idx=ch_idx)
    await emit({"kind": "action", "type": "audit",
                "summary": f"第{ch_idx}章审计 {rep['score']} 分"})
    lines = [f"第{ch_idx}章审计得分：{rep['score']}（{rep.get('mode') == 'llm' and '模型+规则' or '仅规则'}）"]
    for d in rep.get("dimensions", []):
        if d["issues"]:
            lines.append(f"- {d['name']} {d['score']}分：" + "；".join(
                i["message"][:60] for i in d["issues"][:3]))
        else:
            lines.append(f"- {d['name']} {d['score']}分：无明显问题")
    if rep.get("summary"):
        lines.append("总评：" + rep["summary"])
    return "\n".join(lines)


# ---- v0.13.16 C4-3：短剧工坊工具 ----

def _first_drama_project(db, book: Book):
    from apps.api.models import DramaProject
    return db.scalar(select(DramaProject).where(
        DramaProject.book_id == book.id).order_by(DramaProject.created_at))


async def _t_drama_list_projects(db, book, emit) -> str:
    from apps.api.models import DramaProject, DramaEpisode
    from apps.api import drama as drama_mod
    rows = db.scalars(select(DramaProject).where(
        DramaProject.book_id == book.id).order_by(DramaProject.created_at)).all()
    if not rows:
        await emit({"kind": "action", "type": "drama",
                    "summary": "该书还没有短剧项目"})
        return "该书还没有短剧项目。可以在短剧工坊页创建。"
    lines = []
    for p in rows:
        eps = db.scalars(select(DramaEpisode).where(
            DramaEpisode.project_id == p.id).order_by(DramaEpisode.episode)).all()
        st = drama_mod.workflow_status(db, p, book)
        nxt = (st or {}).get("next_action") or {}
        lines.append(f"- 《{p.title}》共 {len(eps)} 集"
                     f"（{', '.join(f'EP{e.episode}:{e.status}' for e in eps[:5])}"
                     f"{'…' if len(eps) > 5 else ''}）"
                     + (f"；下一步：{nxt.get('label') or nxt.get('action') or ''}" if nxt else ""))
    await emit({"kind": "action", "type": "drama",
                "summary": f"查询短剧项目：{len(rows)} 个"})
    return "短剧项目：\n" + "\n".join(lines)


async def _t_drama_queue_status(db, book, emit) -> str:
    from apps.api.models import DramaTask
    rows = db.scalars(select(DramaTask).where(
        DramaTask.book_id == book.id).order_by(DramaTask.created_at.desc())).all()
    if not rows:
        await emit({"kind": "action", "type": "drama", "summary": "队列为空"})
        return "该书的媒体生成队列为空。"
    counts: dict[str, dict[str, int]] = {}
    for t in rows:
        d = counts.setdefault(t.task_type, {})
        d[t.status] = d.get(t.status, 0) + 1
    lines = [f"- {k}：{'/'.join(f'{s}={n}' for s, n in sorted(v.items()))}"
             for k, v in counts.items()]
    recent = [f"  · {t.resource_key} {t.status}"
              + (f"（{t.error[:60]}）" if t.error else "")
              for t in rows[:5]]
    await emit({"kind": "action", "type": "drama", "summary": "查询生成队列状态"})
    return "队列状态：\n" + "\n".join(lines + recent)


async def _t_drama_generate_batch(db, book, a, emit) -> str:
    from apps.api.models import DramaScene
    from apps.api import drama as drama_mod, drama_queue as dq
    p = _first_drama_project(db, book)
    if p is None:
        return "该书还没有短剧项目，无法批量生成。先在短剧工坊创建项目。"
    try:
        episode = int(a.get("episode"))
    except (TypeError, ValueError):
        return "缺少有效 episode（集号）。"
    target = a.get("target") if a.get("target") in ("image", "video", "audio") else "image"
    scenes = drama_mod.list_scenes(db, p, episode)
    if not scenes:
        return f"EP{episode} 还没有分镜（需要先分集、写剧本生成分镜）。"
    ledger = Task(id=f"tk_{uuid.uuid4().hex[:12]}", book_id=book.id,
                  kind=f"drama_batch_{target}", target=episode,
                  status="running", progress=f"排队 {len(scenes)} 个分镜…")
    db.add(ledger)
    batch_id = dq.new_batch_id()
    scene_img_task: dict[str, str] = {}
    if target == "video":
        for s in scenes:
            img = dq.enqueue(db, book_id=book.id, project_id=p.id, episode=episode,
                             task_type="image", resource_type="scene", resource_id=s.id,
                             resource_key=s.scene_id or s.id, batch_id=batch_id,
                             ledger_task_id=ledger.id)
            scene_img_task[s.id] = img.id
    for s in scenes:
        dq.enqueue(db, book_id=book.id, project_id=p.id, episode=episode,
                   task_type=target, resource_type="scene", resource_id=s.id,
                   resource_key=s.scene_id or s.id, batch_id=batch_id,
                   ledger_task_id=ledger.id,
                   dependency_id=scene_img_task.get(s.id, ""))
    db.commit()
    dq.ensure_worker()
    await emit({"kind": "action", "type": "drama",
                "summary": f"已排队 EP{episode} {len(scenes)} 个分镜的{ '图像' if target == 'image' else '视频' if target == 'video' else '配音'}生成（任务 {ledger.id[:12]}）"})
    return (f"已入队：EP{episode} 共 {len(scenes)} 个分镜的 {target} 生成"
            f"（批次 {batch_id}，台账任务 {ledger.id}）。"
            f"生成在后台队列进行，用户可在短剧工坊页看进度，"
            f"也可稍后用 drama_queue_status 查询。")


async def _t_drama_generate_grid(db, book, a, emit) -> str:
    from apps.api.models import DramaScene, DramaTask as DT
    from apps.api import drama as drama_mod, drama_queue as dq
    p = _first_drama_project(db, book)
    if p is None:
        return "该书还没有短剧项目，无法生成宫格图。"
    try:
        episode = int(a.get("episode"))
    except (TypeError, ValueError):
        return "缺少有效 episode（集号）。"
    cells = a.get("cells") if a.get("cells") in (2, 4, 6, 9) else 9
    scenes = drama_mod.list_scenes(db, p, episode)[:cells]
    if not scenes:
        return f"EP{episode} 还没有分镜。"
    busy = db.scalar(select(DT).where(
        DT.project_id == p.id, DT.task_type == "grid", DT.episode == episode,
        DT.status.in_(("queued", "running"))))
    if busy:
        return f"EP{episode} 的宫格图已在生成中（批次 {busy.batch_id}），无需重复提交。"
    ledger = Task(id=f"tk_{uuid.uuid4().hex[:12]}", book_id=book.id,
                  kind="drama_grid", target=episode,
                  status="running", progress=f"生成 {len(scenes)} 格宫格图…")
    db.add(ledger)
    batch_id = dq.new_batch_id()
    dq.enqueue(db, book_id=book.id, project_id=p.id, episode=episode,
               task_type="grid", resource_type="grid",
               resource_id=f"ep{episode}_grid", resource_key=f"EP{episode}宫格",
               batch_id=batch_id, ledger_task_id=ledger.id,
               payload={"scene_ids": [s.id for s in scenes], "cells": cells})
    db.commit()
    dq.ensure_worker()
    await emit({"kind": "action", "type": "drama",
                "summary": f"已排队 EP{episode} {len(scenes)} 格宫格图"})
    return (f"已入队：EP{episode} {len(scenes)} 格多宫格分镜图（批次 {batch_id}）。"
            f"完成后在短剧工坊页可查看整集节奏审阅图。")


async def _polish(db, book, content: str, pack, sp, diag, ch_idx: int, emit) -> str | None:
    """调用 chapter_polish 技能做整章精修（模型调用，失败静默降级）。"""
    sk = skills.load("chapter_polish", db)
    if not sk:
        return None
    m = gen_graph.pick_model(db, "writer") or gen_graph.pick_model(db, "default")
    if not m:
        return None
    style_dna = getattr(sp, "rules", "") or "（未设置，沿用默认：口语化、短段落、少形容词、多动作）"
    chars = [{"name": c.name, "role": c.role, "profile": c.profile, "voice": c.voice}
             for c in db.scalars(select(Character).where(Character.book_id == book.id)).all()]
    chars_txt = "\n".join(f"- {c['name']}（{c.get('role') or ''}）：{(c.get('profile') or '')[:120]}"
                          + (f"；口癖：{c['voice']}" if c.get("voice") else "") for c in chars) or "（暂无）"
    fs = db.scalars(select(Foreshadow).where(Foreshadow.book_id == book.id,
                                             Foreshadow.status != "done")).all()
    fs_txt = "\n".join(f"- {f.content}" for f in fs) or "（无）"
    prompt = skills.render(
        sk,
        draft=content,
        style_dna=style_dna,
        world_view=getattr(pack, "world_view", "")[:1500],
        characters=chars_txt,
        foreshadows=fs_txt,
        issue_notes=diag.notes() if diag else "",
        # 未设定时给精修一个宽松参考区间（仅生成期提示，不回写、不进 UI）
        chapter_min=book.chapter_min or 2000,
        chapter_max=book.chapter_max or 3000,
    )
    try:
        r = await llm.complete(m, [{"role": "user", "content": prompt}], timeout=900)
        _record_llm_call(db, book, m, "chapter_polish", r)
    except Exception as e:
        await emit({"kind": "warn", "text": f"精修模型调用失败，保留原稿：{type(e).__name__}"})
        return None
    if r.error or not (r.text or "").strip():
        await emit({"kind": "warn", "text": "精修未返回内容，保留原稿。"})
        return None
    out = r.text.strip()
    lines = out.splitlines()
    if not (lines and re.match(r"^#\s*第\s*\d+\s*章", lines[0].strip())):
        if lines and lines[0].lstrip().startswith("#"):
            out = f"# 第{ch_idx:03d}章 \n\n" + "\n".join(lines[1:]).strip()
    if not gates.strip_title(out).strip():
        return None
    return out


# ----------------------------------------------------------------- 主循环
# v0.11.0 重构要点（修复"回复被中止"与"假流式"）：
# 1) 原先主循环是非流式 act()：模型读3章正文+组织长回复时，前端干等 60-180s
#    无任何字节流动，超过网关/浏览器空闲判定即"中止"。现在 act_stream() 真流式，
#    边生成边吐 token，连接始终有数据在流动。
# 2) 原先工具执行完只有一轮 llm.complete 总结，长回复超 max_tokens 被截断、
#    且模型没法继续调工具（比如"批量改3章"只改了一半）。现在最多 MAX_TOOL_ROUNDS
#    轮循环：模型可以逐章 read_chapter -> draft_chapter -> 再 read 下一章，每轮
#    的自然语言部分都流式直达前端。
# 3) 工具轮之间发 stage 心跳：防止反代/浏览器把静默连接当死流掐断。

MAX_TOOL_ROUNDS = 6

# ----------------------------------------------------------------- 创作意图未落地兜底（v0.19.2）
#
# ★★ 这是同一个 bug 的**第三次修复**，前两次都失败，原因值得记录：
#
#   v0.18.1：用「承诺语关键词」枚举（稍等/这就去/正在生成…）
#            → 失败。模型说「**直接发起**草指令」「这轮让系统**生成**第2章草稿」，
#              都不在枚举里。**关键词枚举永远追不上模型的措辞变化。**
#   v0.19.1：用 XML 文本兜底（模型把 tool_call 写成正文）
#            → 部分有效，但本次模型**连 XML 都没写**，只输出了纯文本。
#
# ★ 正确思路：不再猜「它怎么说的」，而是判「它**想做什么** + 有没有落地」：
#
#   触发条件（三条同时满足）：
#     ① 正文表达了**创作产出意图**（要写第 N 章 / 起草 / 生成草稿 / 写入设定…）
#     ② 这一轮**没有任何工具被调用**（无标准 tool_calls，也无 XML 形式）
#     ③ 正文**不含"已完成"的自我描述**（避免把"已起草完成，请审阅"误判）
#
#   满足则给模型追加一条系统提醒，逼它真的调工具；最多补救 MAX_NUDGE_ROUNDS 次。
#
# 为什么这样更可靠：判断的是「**意图 vs 结果**」的不一致，而不是「字面措辞」。
# 只要模型承诺了产出、却没产出，就一定能命中 —— 无论它怎么措辞。
_INTENT_PATTERNS = [
    # 具体章号 + 动作（★ 不能只写「第N章」——"第2章写的是退婚"是复述，不是意图）
    r"第\s*\d+\s*章\s*(?:的)?(?:草稿|正文|初稿|稿子)",
    r"(?:写|改|重写|生成|起草|补|续|接下)\s*(?:第\s*\d+\s*章|下一章|本章|这章)",
    # ★ 负向断言 (?!的是|的) 排除"第2章**写的是**退婚"这类复述
    r"(?:第\s*\d+\s*章|下一章|本章|这章)\s*(?:要|得|该|来|去)?(?:写|改|重写|生成|起草|补)(?!的是|的)",
    r"下一章", r"本章草稿", r"这章草稿",
    # 写/起草类动词
    # ★ 注意「写」太宽泛 —— "第2章**写的是**退婚"（复述内容）会误命中，
    #   所以收紧为「要写/去写/写第N章/写一下/写出来」这类**动作意图**。
    r"(?:起|代|撰|动)草", r"(?:要|去|来|再|重|改)写", r"写(?:一下|出来|出个|第\s*\d+\s*章)",
    r"落笔", r"成稿",
    # 产出类名词（单独出现即视为产出意图，因为用户不会无缘无故提"草稿"）
    r"草稿", r"初稿", r"稿子",
    # 生成/产出类动词
    # ★ 注意「落进/落到」太宽泛 —— "没真正落进系统"（复盘）会误命中，所以去掉。
    #   真正的产出意图由「草稿 / 正文 / 第N章」这些具体名词保证。
    r"生成", r"产出", r"输出", r"补(?:上|齐)",
    # 设定写入意图
    r"(?:记|写|存|录)(?:入|进)", r"登记", r"入册",
    # 工具名直呼
    r"draft_chapter", r"set_project_meta", r"save_world",
    r"save_character", r"save_outline",
]
_INTENT_RE = re.compile("|".join(_INTENT_PATTERNS))

# ★ v0.19.4 新增：**分析型空转**检测（第四次修复的关键补充）
#
# 线上实录（seq=64~70）暴露了 v0.19.2 漏掉的场景：
#   seq=64 模型：「你说得对，这是个结构硬伤…**我先读一下**现有的第2、3章，
#                 把断点找准确，**再重写**。」→ 无任何工具调用 ❌
#   seq=68 模型：「我**读一下**第2章和第3章。**读完了**。」→ 假装读完，实际没调 ❌
#
# 共同点：用户的话是「**指出问题**」而非「下指令」时，模型倾向**先分析表态**，
# 把"计划"当成回复，而没执行计划的第一步（read_chapter / draft_chapter）。
#
# 所以除了「产出意图」，还要覆盖「**工具意图**」——模型说要做某个动作，
# 但那个动作在系统里必须靠工具完成。
_TOOL_INTENT_PATTERNS = [
    # 读取类（read_chapter）
    r"我(?:先|来|去)?读", r"让我(?:先)?读", r"(?:先|去)看(?:看|一下)",
    r"我(?:先)?看一?下", r"看一?眼前文", r"(?:先|再)看(?:看|一下)?",
    r"核对(?:一下)?(?:原文|正文|前文|内容)",
    r"(?:确认|核实)(?:一下)?(?:前文|伏笔|原文|设定)",
    # 生成类（draft_chapter）
    r"(?:我)?(?:先|马上|这就|立刻|立即)(?:去)?(?:写|改|重写|生成|起草)",
    r"(?:重写|改写|改一?下|修正)(?:第\s*\d+\s*章|正文|这章|本章)",
    # 写入类（save_* / set_project_meta）
    r"(?:我)?(?:把|将).{0,12}(?:记(?:下|入|进)|存(?:下|入|进)|写(?:入|进)|登记|入册)",
    # 自述"接下来要做什么"（计划式表述）
    r"(?:接下来|下一步|然后)(?:我)?(?:会|将|要|先)",
    r"(?:我)?(?:先|将).{0,8}(?:再|然后)(?:重写|改写|生成|起草|输出)",
]
_TOOL_INTENT_RE = re.compile("|".join(_TOOL_INTENT_PATTERNS))

# 「已完成」的自我描述（出现则视为**真的做了**，不再补救）
_DONE_HINTS = ("已起草", "已生成", "已完成", "已写入", "已记下", "已保存",
               "已登记", "已入册", "草稿卡里", "请审阅", "请审核", "采纳",
               "在草稿卡")
# 「只是聊天/讨论」的排除信号：模型在跟用户商量、提问、给选项时不该补救。
# ★ 注意：不能简单用「含问号」判定 —— 书名常带问号（如《退婚？退得好》），
#   会把正常回复误排除（实测踩坑）。这里只认**明确的征询措辞**。
_CHATTY_HINTS = ("要不要", "你想", "你定", "你选", "我建议", "可以吗",
                 "哪个", "定了吗", "你觉得呢", "怎么选", "选哪个")


def _looks_like_unfulfilled_intent(text: str) -> bool:
    """判断这段正文是不是「表达了产出/工具意图，但没真正动手」。

    ★ 演化史（四次修复，每次都覆盖了新的失败模式）：
      v0.18.1  枚举**承诺措辞**（稍等/这就去）           → 追不上措辞变化
      v0.19.1  兜底 **XML 文本**形式的工具调用            → 模型纯文本时不覆盖
      v0.19.2  检测**产出意图**（第N章/起草/生成）        → 漏"我先读一下"类
      v0.19.4  追加**工具意图**（读/看/核对/计划式表述）  → 本版

    判定：有产出意图 **或** 工具意图 **且** 无"已完成"描述 **且** 非征询。
    """
    t = (text or "").strip()
    if not t:
        return False
    # 征询意见/给选项（明确措辞，不用单纯问号）
    if any(h in t for h in _CHATTY_HINTS):
        return False
    # 已完成的自我描述 → 视为真的做了
    if any(h in t for h in _DONE_HINTS):
        return False
    # v0.19.4：产出意图 **或** 工具意图（"我先读一下""我看看原文""接下来先…再…"）
    if not (_INTENT_RE.search(t) or _TOOL_INTENT_RE.search(t)):
        return False
    return True


def _is_false_completion(text: str) -> bool:
    """★ v0.19.4：检测「**假装做完了**」。

    线上实录（seq=68）：模型说「我读一下第2章和第3章。**读完了**。」
    —— 但它**根本没调 read_chapter**，纯属虚构动作。

    这类比"只承诺"更隐蔽：它用了完成时态，让用户以为已经发生。
    判定：出现完成时 + 读取/写入类动词，但本轮无任何工具调用。

    ★ 入参防御：主循环传进来的可能是 None / 非字符串，不防御会抛
    AttributeError 把对话打挂（实测踩坑）。
    """
    if not isinstance(text, str):
        return False
    t = text.strip()
    if not t:
        return False
    fake = ("读完了", "看完了", "查完了", "核对完了", "确认完了",
            "已经读取", "已经查看", "已经核对", "我看过了", "查过了")
    return any(k in t for k in fake)


# 向后兼容别名（旧测试/调用点仍可用）
_looks_like_empty_promise = _looks_like_unfulfilled_intent

MAX_NUDGE_ROUNDS = 3                     # 补救上限（防死循环）



def _genre_block(db, book) -> str:
    """按书题材生成「本书题材」提示段（v0.15 G10）。

    * 仙侠书 → 与 v0.14.10 原有措辞等价（零回归）
    * 都市书 → 明确的现代规则措辞，不再被引导修仙
    * 无题材信息 → 返回空串（不注入，走原行为）
    """
    try:
        from apps.api import genres
        theme = (getattr(book, "theme", "") or getattr(book, "genre", "") or "").strip()
        if not theme:
            return ""
        g = genres.get_genre(theme, db)
        if not g:
            return ""
        tags_raw = (getattr(book, "tags", "") or "")
        tags = [t for t in tags_raw.split(",") if t.strip()]
        lines = [f"# 本书题材（{g.get('label', theme)}）"]
        metrics = g.get("metrics") or {}
        if metrics:
            lines.append("度量衡维度（记账只认这些）：" + "、".join(metrics.keys()))
        dim = g.get("realm_dim") or ""
        if dim:
            lines.append(f'实力体系维度：「{dim}」（世界观里用 category="{dim}" 登记阶梯）')
        else:
            lines.append("本书无实力等级体系，不要编造等级阶梯。")
        hints = genres.style_hints_for(theme, tags, db)
        if hints:
            lines.append("风格基调：" + "；".join(hints))
        prompts = genres.prompt_hints_for(theme, tags, db)
        if prompts:
            lines.append("创作要点：" + " ".join(prompts))
        return "\n".join(lines)
    except Exception:
        return ""


async def chat_stream(db, book_id: str, message: str, history: list[dict], emit):
    book = db.get(Book, book_id)
    if not book:
        await emit({"kind": "error", "text": "书籍不存在"})
        return
    model = gen_graph.pick_model(db, "planner") or gen_graph.pick_model(db, "default") or gen_graph.pick_model(db, "writer")
    if not model:
        await emit({"kind": "error", "text": "未配置模型：请先在配置中心添加模型并测试连通、设为默认。"})
        return

    # 关键：把书籍现状压成快照注入上下文。
    # 少了这一步，主编对「已有几章、写到哪、人物有谁」一无所知，只能编或说没有。
    try:
        state = ctx_builder.build_agent_state(db, book)
    except Exception as e:  # 快照生成失败不能阻断对话，但不能让它悄悄失忆
        state = f"（状态快照生成失败：{type(e).__name__}: {e}）"
        await emit({"kind": "warn", "text": f"读取项目状态失败：{type(e).__name__}"})

    msgs = [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": state}]
    # v0.15 G10：题材提示段（仙侠书等价原措辞；都市书明确现代规则；无题材则不注入）
    gb = _genre_block(db, book)
    if gb:
        msgs.append({"role": "system", "content": gb})
    # #315：注入本书历史避坑经验（用户在本书反复指出并要求修正的问题）。
    #   ★ fail-open：lessons 内部异常全吞，绝不阻断对话。
    _lessons_rows = lessons.recent_lessons(db, book.id)
    _lessons_block = lessons.render_block(_lessons_rows)
    if _lessons_block:
        msgs.append({"role": "system", "content": _lessons_block})
        lessons.bump_hits(db, _lessons_rows)
    msgs += [{"role": m["role"], "content": m["content"]} for m in (history or [])]
    msgs.append({"role": "user", "content": message})

    total_cost = 0.0
    total_in = total_out = 0
    _t0 = time.time()
    nudge_used = 0          # ★ v0.18.1：口头承诺补救次数（防死循环）
    # #315：本轮对话标识 —— 避坑采集用它做"同轮只记 1 条"的去重
    _turn_key = uuid.uuid4().hex[:12]

    def _record_run(status: str, note: str = "") -> None:
        """成本台账 + ★ 积分扣费（v0.20.2 A2 接入）。

        两件事一起做（同一处）：
          ① Run 记录 token/cost（原来就有，"可算账"）
          ② 按 token 折算扣积分（新增）

        ★ 为什么放在同一处：两者数据源完全一致（都是 total_in/total_out），
          分开写会有一处漏掉的风险。

        ★ 扣费失败绝不阻断对话（与 Run 记录同样的容错原则）：
          但失败原因要写日志，否则"扣费静默失效"会让商业化变成空转。

        ★ 幂等：ref_id 用 run_id —— 同一轮对话重试不会重复扣费。
        """
        run_id = f"rn_{uuid.uuid4().hex[:12]}"
        try:
            db.add(Run(id=run_id, book_id=book.id, chapter_id="",
                       kind="chat", model_id=model.id, prompt_version="agent-v1",
                       context_snapshot="", input_tokens=total_in, output_tokens=total_out,
                       cost=round(total_cost, 4), gate_score=0, gate_report="",
                       status=status, duration_ms=int((time.time() - _t0) * 1000)))
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass


        # ★ #313：聊天主循环用量落 LLMCall 台账（使 /api/me/cost 与洞察 feature_heat 有数据）。
        #   fail-open：record 内部已兜底，失败不影响对话。
        _cost_owner = (getattr(book, "owner_id", "") or "").strip()
        if _cost_owner:
            costing.record(
                db, task="chat", role=getattr(model, "role", "") or "planner",
                user_id=_cost_owner, book_id=book.id, model_id=model.id,
                input_tokens=total_in, output_tokens=total_out,
                cost=round(total_cost, 4), status=status, run_id=run_id,
            )


    for round_i in range(MAX_TOOL_ROUNDS):
        # --- 流式工具轮：边生成边吐 token ---
        final_text = ""
        tool_calls: list[dict] = []
        had_error = False
        got_any = False
        async for ev in llm.act_stream(model, msgs, TOOLS):
            if ev.kind == "token":
                got_any = True
                await emit({"kind": "token", "text": ev.text})
            elif ev.kind == "tool_calls":
                final_text = ev.text
                tool_calls = ev.tool_calls
                total_in += ev.input_tokens
                total_out += ev.output_tokens
                total_cost += ev.cost
            elif ev.kind == "done":
                final_text = ev.text
                total_in += ev.input_tokens
                total_out += ev.output_tokens
                total_cost += ev.cost
                # ★ v0.19.1 真根因修复：部分模型明明支持 tools，却**退化成把工具调用
                # 写成正文里的 XML 文本**（<tool_call>{"name":...}</tool_call>）。
                # act_stream 只认标准 delta.tool_calls，这类文本会被当成普通回复，
                # 于是主循环走「无工具调用 → 收尾」→ 用户看到"写了"但**草稿卡里啥也没有**
                # （v0.18.1 的承诺拦截治不了这个 —— 拦截虽触发，模型重试仍用文本形式）。
                # 这里把正文里 XML 形式的调用捞出来，转成标准 tool_calls 继续走工具分支。
                if not tool_calls and final_text:
                    xml_calls, cleaned = _parse_xml_tool_calls(final_text)
                    if xml_calls:
                        tool_calls = xml_calls
                        final_text = cleaned
                        await emit({"kind": "stage",
                                    "text": f"检测到文本形式的工具调用，已自动识别 "
                                            f"{len(xml_calls)} 个（{', '.join(c['name'] for c in xml_calls)}）"})
            elif ev.kind == "error":
                had_error = True
                _record_run("error", ev.text[:200])
                # 已吐过正文则降级为提醒；否则明确报错
                if got_any:
                    await emit({"kind": "warn", "text": "模型流中断：" + ev.text[:160]})
                    await emit({"kind": "done", "cost": round(total_cost, 4)})
                else:
                    await emit({"kind": "error", "text": "模型调用失败：" + ev.text})
                    await emit({"kind": "done", "cost": round(total_cost, 4)})
                return

        if tool_calls:
            # 本轮工具调用前的前言文本已流式吐出；追加 assistant 消息以便回灌
            assistant_msg = {"role": "assistant", "content": final_text or ""}
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                for tc in tool_calls
            ]
            msgs.append(assistant_msg)
            for tc in tool_calls:
                res = await execute_tool(db, book, tc, emit)
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": res})
            # #315：「用户纠错 → 系统修正」沉淀为避坑经验（每轮最多 1 条）。
            #   ★ 判据在 lessons.record_lesson 里：既要用户这句像纠错，
            #     又要本轮真执行了变更类工具 —— 两者缺一不可（防噪声）。
            try:
                lessons.record_lesson(
                    db, book_id=book.id,
                    user_id=(getattr(book, "owner_id", "") or "").strip(),
                    user_msg=message,
                    tool_names=[tc["name"] for tc in tool_calls],
                    fix_text=(final_text or "")[:300],
                    turn_key=_turn_key,
                )
            except Exception:  # noqa: BLE001
                pass
            # 工具执行可能耗时长（代笔 1-3 分钟）：发心跳防静默断流
            await emit({"kind": "stage",
                        "text": f"第 {round_i + 1} 轮：已执行 {len(tool_calls)} 个工具，继续…"})
            continue  # 下一轮：模型基于工具结果继续（可再调工具或直接回复）

        # ★ v0.19.2 「产出意图未落地」兜底（第三次修复，前两次见函数上方注释）。
        # 判据是「**意图 vs 结果**不一致」而非字面措辞 —— 只要模型表达了产出意图
        # （要写第N章 / 起草 / 生成草稿 / 写入设定…）却没调任何工具，就逼它动手。
        _need_nudge = (
            _looks_like_unfulfilled_intent(final_text)
            or _is_false_completion(final_text)      # v0.19.4：假装做完了
        )
        if nudge_used < MAX_NUDGE_ROUNDS and _need_nudge:
            nudge_used += 1
            # ★ 诊断留痕（v0.19.2）：把模型原文前 300 字写进日志。
            # 为什么重要：这个 bug 修了三次，前两次都因为**看不到模型原文**而误判根因。
            # 有了这条日志，下次再出问题可以直接看它到底说了什么、我们有没有拦住。
            _why = "假装完成" if _is_false_completion(final_text) else "意图未落地"
            logging.getLogger("novelforge").warning(
                "检测到 %s（第 %d/%d 次补救）| 模型原文：%s",
                _why, nudge_used, MAX_NUDGE_ROUNDS,
                (final_text or "")[:300].replace("\n", " "))
            await emit({"kind": "stage",
                        "text": f"检测到你说了要做但系统里没有产物，正在要求真正执行"
                                f"（第 {nudge_used}/{MAX_NUDGE_ROUNDS} 次）…"})
            msgs.append({"role": "assistant", "content": final_text or ""})
            msgs.append({"role": "system", "content":
                         "⚠️ **系统检测到你又只说了没做**。\n\n"
                         "你刚才的回复里表达要做某事（起草/生成/重写/读取/核对/写入…），"
                         "但这一轮**没有产生任何工具调用**，所以：\n"
                         "  · 用户看不到任何新草稿\n"
                         "  · 你声称的「读完了」「已生成」都是**假的**（系统里没有这个动作）\n"
                         "  · 用户已经被这个问题困扰很多次了，每次都要人工催你\n\n"
                         "**现在立刻做，不要再说话**：\n"
                         "  · 要看内容 → 调 **read_chapter**（传 ch_idx，可用逗号分隔多章）\n"
                         "  · 要写/重写正文 → 调 **draft_chapter**（传 instruction 与 ch_idx）\n"
                         "  · 要记设定 → 调 **set_project_meta** / **save_world** / "
                         "**save_character** / **save_outline**\n"
                         "  · 要埋线 → 调 **add_foreshadow**\n\n"
                         "禁止：道歉、解释、复述计划、「我这就去」。**第一个动作就必须是工具调用。**"})
            continue

        # 无工具调用：正文已全部流式吐完，收尾
        _record_run("ok")
        await emit({"kind": "done", "cost": round(total_cost, 4)})
        return

    # 轮数耗尽：给用户一个明确交代，而不是静默截断
    _record_run("ok", note="轮数耗尽")
    await emit({"kind": "warn",
                "text": f"已连续执行 {MAX_TOOL_ROUNDS} 轮工具调用，为控制成本先停在这里——"
                        f"需要继续请说「接着来」。"})
    await emit({"kind": "done", "cost": round(total_cost, 4)})


# 匹配 XML 风格工具调用的兜底解析：部分模型明明支持 tools，却会退化成
# 把工具调用写成正文里的 <tool_call {...} </tool_call> 文本。
#
# ★ v0.19.1：这个兜底**已接进 chat_stream 主循环**（原先只定义未调用，
# 导致「模型说要起草但草稿没落」的老问题一直存在 —— 见 chat_stream 的 done 分支）。
# 用户实录：同一本书 04:09 那次正常（进度显示"已执行 1 个工具"），
# 12:42 那次卡在"正在思考…"（模型把调用写成了正文文本，被当成普通回复收尾）。
# 正则用动态拼接标签字面量：源码里直接写尖括号标签容易被
# 文档工具/编辑器当成 HTML 吞掉，拼接形式既保留意图又保证运行时完整。
_TOOL_CALL_OPEN = "<" + "tool_call" + ">"
_TOOL_CALL_CLOSE = "<" + "/tool_call" + ">"


def _parse_xml_tool_calls(text: str) -> tuple[list[dict], str]:
    """从正文里捞出 XML 风格的工具调用，返回 (工具列表, 剔除后的正文)。

    覆盖两种模型习惯：
      A. <9个<包裹的 {"name":..., "arguments":{...}}
      B. arguments 是「JSON 字符串」而不是对象，必须二次解析。

    ★ 入参防御：主循环传进来的 final_text 可能是 None/空（流式为空），
    不防御会抛 TypeError 反而把对话打挂（实测踩坑）。
    """
    if not isinstance(text, str) or not text:
        return [], (text if isinstance(text, str) else "")
    open_tag = "<" + "tool_call" + ">"
    close_tag = "<" + "/tool_call" + ">"
    out: list[dict] = []
    for m in re.finditer(
            re.escape(open_tag) + r"\s*(.*?)\s*" + re.escape(close_tag),
            text, re.S):
        raw = m.group(1).strip()
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("name") or obj.get("tool_name") or obj.get("tool")
        if not name:
            continue
        args = obj.get("arguments", obj.get("parameters", obj.get("args", {})))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        if not isinstance(args, dict):
            args = {}
        out.append({"id": f"tc_xml_{len(out)}_{uuid.uuid4().hex[:6]}",
                    "name": name, "arguments": args})
    if not out:
        return [], text
    cleaned = re.sub(
        re.escape(open_tag) + r".*?" + re.escape(close_tag),
        "", text, flags=re.S).strip()
    return out, cleaned

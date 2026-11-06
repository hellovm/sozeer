"""生成避坑记录（v1.0-E4.19 · #315）。

============================ 解决什么问题 ============================

用户在创作聊天里反复"指出问题 → 让系统修正"：

    「第 3 章主角怎么管女二叫师姐？他俩同辈！」
    「别用'嘴角勾起一抹冷笑'，你每章都写」
    「时间线错了，决战应该在冬至之后」

这些纠错过程以前**只存在于对话里**，从不落库 ——
于是下一次生成（哪怕同一本书、同一个模型）还会踩同样的坑。

本模块把"用户纠错 → 系统修正"沉淀成可注入提示词的经验条目。

============================ 采集判据（★ 宁缺毋滥）============================

**同时满足**两个条件才算一次避坑：
  ① 用户这句话像"纠错/要求"（`looks_like_correction`）
  ② 本轮真的执行了**变更类工具**（`CHANGE_TOOLS`）

为什么必须两个都要：
  · 只有①没有② → 用户只是在聊天/提问，系统没改任何东西，
    记下来等于把闲聊当经验（噪声）。
  · 只有②没有① → 用户主动要求写新内容（"帮我写第 5 章"），
    这是正常创作不是纠错，记下来会让提示词越来越臃肿。

★ 每轮对话最多记 1 条（`_RECORDED` 去重）——
  防止一次长会话里反复落同一件事。

============================ 隐私与红线 ============================

· 条目**按 book_id 隔离**，只注入到同一本书的后续生成
· 管理端可见，但**永不进遥测上报**（不接 telemetry）
· 长度硬截断（trigger/fix ≤600 字）
· 采集全程 fail-open：任何异常都不得影响生成主流程
"""
from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

_log = logging.getLogger("novelforge.lessons")

# ---------------------------------------------------------------- 变更类工具
#
# ★ 判据：调用它会**改变书籍内容/设定**（而不是只读或只查）。
#   漏一个 → 该纠错不被记录（保守，可接受）；
#   错把一个只读工具算进来 → 记下噪声（不可接受）。所以宁少不多。
CHANGE_TOOLS = frozenset({
    "draft_chapter",       # 代笔/重写正文（最常见的纠错出口）
    "rename_chapter",
    "set_project_meta",
    "save_world",
    "save_character",
    "delete_character",
    "save_outline",
    "add_foreshadow",
    "add_timeline_event",
    "add_ledger_entry",
})

# ---------------------------------------------------------------- 纠错识别

# ★ 为什么用词典而不是模型判断：这是**生成路径上的同步判断**，
#   要求"确定 + 零成本 + 零延迟"。模型判断要一次额外调用，
#   既慢又可能误判（把正常要求当纠错）。
#   词典宁可漏判（少记几条），不可误判（记一堆噪声）。
_CORRECTION_PATTERNS: tuple[re.Pattern, ...] = tuple(re.compile(p) for p in (
    # —— 明确纠错（事实/设定/前后文）——
    r"不对", r"不对吧", r"错[了误]", r"有问题", r"别这", r"不要这",
    r"改成", r"改为", r"换[成掉]", r"重新[写改来做]", r"删除", r"去掉",
    r"删掉", r"应该是", r"应是", r"不该", r"怎么不",
    r"为什么没", r"为什么又", r"不是说过", r"前面说", r"上次说",
    r"说过", r"矛盾", r"冲突", r"崩了", r"崩坏", r"太[多长少频繁]",
    r"又[是写来用]", r"别[再重复用]", r"重复", r"出戏", r"违和",
    r"不符合", r"违背", r"搞错", r"弄错", r"时间线", r"设定是",
    r"人设是", r"同辈", r"辈分", r"称呼", r"称呼错",
    # —— 质量改进（E4.21 扩充：此类表达此前漏采，是"数据洞察为空"的根因之一）——
    #    噪声风险由条件②兜底（同轮必须真的调了变更类工具才落库）。
    r"更好", r"再好[一写点些]", r"写得好", r"提高[一写点些]质量",
    r"生硬", r"不自然", r"太直白", r"直白", r"干巴巴", r"没画面",
    r"不生动", r"没意思", r"无聊", r"节奏", r"拖沓", r"啰嗦",
    r"平淡", r"单调", r"老套", r"套话", r"模板", r"机械",
    r"[aA][iI]味", r"机器味", r"僵硬", r"不连贯", r"连贯", r"突兀",
    r"前后不一", r"不一致", r"没逻辑", r"逻辑不通", r"凑字数",
    r"太水", r"刻意", r"太夸张", r"夸张", r"口吻", r"语气",
    r"视角错", r"人称错", r"味道不对", r"不像",
))

# 上限：用户消息过长多半是粘贴素材，不是纠错指令
_MAX_MSG_LEN = 400


def looks_like_correction(text: str) -> bool:
    """判断用户这句话是否像"纠错/修正要求"。

    ★ 保守策略：命中任一模式才算。宁可漏判，不可误判 ——
      噪声条目会稀释真正有用的避坑提示。
    """
    t = (text or "").strip()
    if not t or len(t) > _MAX_MSG_LEN:
        return False
    return any(p.search(t) for p in _CORRECTION_PATTERNS)


def _truncate(s: str, limit: int = 600) -> str:
    s = (s or "").replace("\r", " ").strip()
    # 压掉连续空白，提示词里单行更好读
    s = re.sub(r"\s+", " ", s)
    return s[:limit]


def _new_id() -> str:
    return f"gl_{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------- 写入

# 每轮对话是否已记录（防同一轮 chat_stream 里重复落库）。
# ★ 为什么不用 DB 去重：DB 去重要多一次查询，且"同一轮"是**调用栈内**
#   的概念，进程内集合足够，也不需要跨进程精确。
# ★ 为什么键里带 turn_key：只按 (book, 消息哈希) 去重会把**后续轮次里
#   同一句纠错**也误杀（用户第 5 轮又说"称呼错了"是新事件）；
#   而完全不带上下文标识又会被并发会话互相清空。turn_key 由调用方
#   （一次 chat_stream 调用）生成，两者兼顾。
_RECORDED: set[str] = set()
_RECORDED_MAX = 500


def record_lesson(db: Session, *, book_id: str, user_id: str,
                  user_msg: str, tool_names: list[str],
                  fix_text: str = "", turn_key: str = "") -> bool:
    """记录一条避坑经验（fail-open）。

    :param book_id:    书籍 id（隔离边界）
    :param user_id:    owner id
    :param user_msg:   用户纠错原文（会截断）
    :param tool_names: 本轮执行的工具名（只取交集部分）
    :param fix_text:   系统修正摘要（可空）
    :param turn_key:   本轮对话标识（同一轮内只记 1 条）
    :return: 是否写入
    """
    try:
        if not book_id or not user_id:
            return False
        tools = sorted({t for t in (tool_names or []) if t in CHANGE_TOOLS})
        if not tools:
            return False                        # 没有实际改动 → 不算避坑
        if not looks_like_correction(user_msg):
            return False

        # 同轮去重（一次用户消息最多一条）
        key = f"{turn_key}:{book_id}:{hash(user_msg)}"
        if key in _RECORDED:
            return False
        if len(_RECORDED) >= _RECORDED_MAX:
            _RECORDED.clear()
        _RECORDED.add(key)

        from apps.api.models import GenLesson, now as _now
        row = GenLesson(
            id=_new_id(), book_id=book_id, user_id=user_id,
            kind="correction",
            trigger=_truncate(user_msg),
            fix=_truncate(fix_text),
            tools=",".join(tools)[:256],
            status="active", hits=0,
            created_at=_now(),
        )
        db.add(row)
        db.commit()
        return True
    except Exception as e:                                  # noqa: BLE001
        _log.warning("避坑记录失败（不影响主流程）：%s: %s", type(e).__name__, e)
        try:
            db.rollback()
        except Exception:
            pass
        return False


def reset_recorded() -> None:
    """清空同轮去重集合（测试用）。"""
    _RECORDED.clear()


# ---------------------------------------------------------------- 读取与注入

def recent_lessons(db: Session, book_id: str, *, limit: int = 12) -> list:
    """取该书最近的生效避坑条目（新的优先）。"""
    if not book_id:
        return []
    try:
        from apps.api.models import GenLesson
        return list(db.scalars(
            select(GenLesson)
            .where(GenLesson.book_id == book_id, GenLesson.status == "active")
            .order_by(GenLesson.created_at.desc())
            .limit(limit)
        ).all())
    except Exception as e:                                  # noqa: BLE001
        _log.warning("读取避坑条目失败：%s: %s", type(e).__name__, e)
        return []


def render_block(rows: list, *, max_items: int = 8) -> str:
    """把避坑条目渲染成可注入提示词的文本块。

    ★ 为什么限制条数：注入块越长，占用上下文越多、
      且稀释注意力（模型对第 9 条之后的"注意事项"基本无感）。
      只注入最近 8 条最相关的。
    """
    if not rows:
        return ""
    lines = [
        "【本书避坑经验】以下是用户在本书创作过程中反复指出并要求修正的问题，"
        "本次生成**必须避免重蹈覆辙**：",
    ]
    for i, r in enumerate(rows[:max_items], 1):
        trig = (getattr(r, "trigger", "") or "").strip()
        if not trig:
            continue
        lines.append(f"{i}. 用户指出：{trig}")
        fix = (getattr(r, "fix", "") or "").strip()
        if fix:
            lines.append(f"   → 修正方式：{fix}")
    if len(lines) == 1:
        return ""
    lines.append("（若上述要求与本次用户指令冲突，以本次用户指令为准。）")
    return "\n".join(lines)


def bump_hits(db: Session, rows: list) -> None:
    """记录这些条目被注入过（fail-open，用于衡量避坑提示有没有被用上）。"""
    if not rows:
        return
    try:
        from apps.api.models import GenLesson
        for r in rows:
            r.hits = int(getattr(r, "hits", 0) or 0) + 1
        db.commit()
    except Exception as e:                                  # noqa: BLE001
        _log.warning("避坑 hits 更新失败：%s: %s", type(e).__name__, e)
        try:
            db.rollback()
        except Exception:
            pass


def all_lessons(db: Session, *, days: int = 90, limit: int = 100) -> list[dict]:
    """管理端用：全站避坑条目（按时间倒序，不含正文内容之外的东西）。"""
    import datetime as dt
    try:
        from apps.api.models import GenLesson
        since = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) \
            - dt.timedelta(days=days)
        rows = list(db.scalars(
            select(GenLesson)
            .where(GenLesson.created_at >= since)
            .order_by(GenLesson.created_at.desc())
            .limit(min(max(limit, 1), 500))
        ).all())
        return [_to_dict(r) for r in rows]
    except Exception as e:                                  # noqa: BLE001
        _log.warning("读取避坑列表失败：%s: %s", type(e).__name__, e)
        return []


def _to_dict(r) -> dict:
    return {
        "id": r.id,
        "book_id": r.book_id,
        "user_id": r.user_id,
        "kind": r.kind,
        "trigger": r.trigger,
        "fix": r.fix,
        "tools": r.tools,
        "status": r.status,
        "hits": int(r.hits or 0),
        "created_at": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
    }

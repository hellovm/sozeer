# -*- coding: utf-8 -*-
"""新手引导与上手进度（v1.0 E4 运营准备）。

★★ 为什么需要引导（以及它不该长什么样）

  ★ 数据常识：新用户**第一小时决定留存**。而"面对一个空白工作台
    不知道先干什么"是流失的头号原因。

  ★ 但引导最容易做错的两件事：
    ① **弹窗轰炸**：一进来 5 个引导气泡，用户第一反应是找"跳过"。
    ② **强制流程**：不给跳过 = 直接劝退（尤其对老手）。
       ★ 对比 MEMORY §L 经验：被卡住的用户会去用别的产品，不会跟着走。

  ★ 所以本模块的设计是：
    · **不打断**：不弹窗、不遮罩、不阻断任何操作
    · **可关闭**：`SOZEER_DISABLE_ONBOARDING=1` 或前端一键隐藏
    · **进度驱动**：显示"已完成 2/4"，完成即**自动消失**
    · **基于事实而非标记**：进度来自**实际数据**
      （有没有建过书、有没有生成过章节），不是"用户点没点过引导按钮"。
      ★ 这一点很关键：基于事实的引导，用户即使跳过引导自己摸会了，
        进度也会自动打勾 —— 不会出现"我都写了三章了还提示我去写第一章"。

★ 存储

  用 `Setting` 表（复用现有基础设施，**零 schema 变更**）：
    · `ops.onboard.hidden.<uid>` = "1"   → 用户手动隐藏了引导
    · `ops.onboard.done.<uid>`   = "1"   → 弹过一次"全部完成"（防重复庆祝）
"""
from __future__ import annotations

import logging
import os

from sqlalchemy.orm import Session

_log = logging.getLogger("novelforge.onboard")

ENV_DISABLE = "SOZEER_DISABLE_ONBOARDING"

_HIDE_KEY = "ops.onboard.hidden.{uid}"
_DONE_KEY = "ops.onboard.done.{uid}"

# ---------------------------------------------------------------- 引导步骤定义
#
# ★★ 每一步都必须有 **可自动判定的完成条件**（用真实数据推导）。
#    为什么：如果某步只能靠"用户点了完成"来判定，那它一定不准 ——
#    用户跳过引导后自己做了，这里还显示未完成，体验很差。
#
# ★ 步骤顺序 = 真实上手路径：立项 → 出内容 → 保证质量 → 尝试进阶功能
STEPS = (
    {
        "key": "create_book",
        "title": "创建第一部作品",
        "desc": "填写题材与目标字数，系统会生成总纲与前三章大纲",
        "action": "新建作品",
        "link": "/app/books",
        "hint": "不知道写什么？可以先从「示例想法」开始",
    },
    {
        "key": "write_chapter",
        "title": "用 AI 写出第一章",
        "desc": "在写作台对话驱动代笔，草稿点「采纳」后进入正文",
        "action": "去写作台",
        "link": "/app/books",
        "hint": "生成的是草稿，采纳前可以随时重写或丢弃",
    },
    {
        "key": "check_quality",
        "title": "跑一次质检",
        "desc": "自动发现前后矛盾（境界越界、伏笔超期、人物状态漂移）",
        "action": "查看质检",
        "link": "/app/books",
        "hint": "质检给的是建议，不是硬性要求；但越界问题建议优先处理",
    },
    {
        "key": "try_drama",
        "title": "把小说变成短剧",
        "desc": "从章节生成分集剧本与分镜提示词，批量出图出视频",
        "action": "打开短剧工坊",
        "link": "/app/drama",
        "hint": "长任务会后台跑，完成后会通知你，不用守着页面",
    },
)


def _disabled() -> bool:
    """引导是否被部署方整体关闭。"""
    return (os.environ.get(ENV_DISABLE, "").strip().lower()
            in ("1", "true", "on", "yes"))


def _get_flag(db: Session, key: str) -> bool:
    try:
        from apps.api.models import Setting
        row = db.get(Setting, key)
        return bool(row and str(row.value).strip() == "1")
    except Exception:                                   # noqa: BLE001
        return False


def _set_flag(db: Session, key: str, on: bool = True) -> None:
    try:
        from apps.api.models import Setting
        row = db.get(Setting, key)
        if row is None:
            db.add(Setting(key=key, value="1" if on else "0"))
        else:
            row.value = "1" if on else "0"
        db.commit()
    except Exception as e:                              # noqa: BLE001
        _log.debug("写入引导标记失败：%s: %s", type(e).__name__, e)
        try:
            db.rollback()
        except Exception:                               # noqa: BLE001
            pass


# ---------------------------------------------------------------- 进度推导

def _facts(db: Session, user_id: str) -> dict:
    """★ 用**真实数据**推导进度（不是"点没点过按钮"）。

    这是本模块最重要的设计 —— 基于事实的引导永远不会"滞后于用户"。
    """
    f = {"books": 0, "chapters": 0, "quality": 0, "dramas": 0}
    try:
        from sqlalchemy import func, select
        from apps.api.models import Book, Chapter, DramaProject

        f["books"] = int(db.scalar(
            select(func.count(Book.id)).where(Book.owner_id == user_id)) or 0)

        # ★ 章节要限定在"自己的书"里 —— 直接 count(Chapter) 会把别人的算进来
        if f["books"]:
            f["chapters"] = int(db.scalar(
                select(func.count(Chapter.id))
                .join(Book, Book.id == Chapter.book_id)
                .where(Book.owner_id == user_id)) or 0)

        f["dramas"] = int(db.scalar(
            select(func.count(DramaProject.id))
            .join(Book, Book.id == DramaProject.book_id)
            .where(Book.owner_id == user_id)) or 0)

        # ★★ 质检：用 `quality_score > 0` 而不是 `is not None`（实测踩坑）
        #
        #   坑：`Book.quality_score` 的 DB 默认值是 **0**，不是 NULL。
        #   所以 `isnot(None)` **恒为真** —— 新建的书也会被判定为"跑过质检"，
        #   这一步永远自动打勾，引导失去意义。
        #   ★ 教训：判空之前先确认"空值到底是 NULL 还是 0"（对比 MEMORY §59：
        #     先确认"检查的是什么"）。
        try:
            f["quality"] = int(db.scalar(
                select(func.count(Book.id))
                .where(Book.owner_id == user_id,
                       Book.quality_score > 0)) or 0)
        except Exception:                               # noqa: BLE001
            f["quality"] = 0
    except Exception as e:                              # noqa: BLE001
        _log.debug("读取引导事实失败（按未完成处理）：%s: %s",
                   type(e).__name__, e)
    return f


def _is_done(step_key: str, f: dict) -> bool:
    """步骤完成判定（★ 纯函数，好测）。"""
    if step_key == "create_book":
        return f.get("books", 0) >= 1
    if step_key == "write_chapter":
        return f.get("chapters", 0) >= 1
    if step_key == "check_quality":
        return f.get("quality", 0) >= 1
    if step_key == "try_drama":
        return f.get("dramas", 0) >= 1
    return False


def progress(db: Session, user_id: str) -> dict:
    """计算上手进度（★ 只读，不改任何状态）。

    返回：
      {
        "steps": [{key,title,desc,action,link,hint,done}],
        "done_count": 2, "total": 4, "percent": 50,
        "all_done": False, "hidden": False, "show": True
      }
    """
    if _disabled():
        return {"steps": [], "done_count": 0, "total": 0, "percent": 0,
                "all_done": False, "hidden": True, "show": False,
                "reason": "部署方已关闭引导"}

    f = _facts(db, user_id)
    hidden = _get_flag(db, _HIDE_KEY.format(uid=user_id))

    steps = []
    for s in STEPS:
        steps.append({**s, "done": _is_done(s["key"], f)})

    done_count = sum(1 for s in steps if s["done"])
    total = len(steps)
    all_done = done_count >= total

    # ★ 完成即自动隐藏 —— 不需要用户手动关闭（也避免"我都写完了还提示"）
    #   ★ 但保留一点"庆祝"：首次完成时前端可以给一次提示（用 done 标记防重复）
    return {
        "steps": steps,
        "done_count": done_count,
        "total": total,
        "percent": int(done_count / total * 100) if total else 0,
        "all_done": all_done,
        "hidden": hidden,
        # ★ show = 该不该展示：未隐藏 且 未全部完成
        "show": (not hidden) and (not all_done),
        "facts": f,
    }


def hide(db: Session, user_id: str) -> dict:
    """用户手动隐藏引导（★ 尊重用户选择，这是防挨骂的关键）。"""
    _set_flag(db, _HIDE_KEY.format(uid=user_id), True)
    return {"ok": True, "hidden": True}


def show_again(db: Session, user_id: str) -> dict:
    """重新显示引导（给"我误关了"的出口）。"""
    _set_flag(db, _HIDE_KEY.format(uid=user_id), False)
    return {"ok": True, "hidden": False}


def mark_celebrated(db: Session, user_id: str) -> bool:
    """标记"已完成庆祝"（防重复弹恭喜）。

    返回 True = 本次是第一次（前端应弹庆祝），False = 已庆祝过。
    """
    key = _DONE_KEY.format(uid=user_id)
    if _get_flag(db, key):
        return False
    _set_flag(db, key, True)
    return True


__all__ = ["STEPS", "progress", "hide", "show_again", "mark_celebrated",
           "ENV_DISABLE"]

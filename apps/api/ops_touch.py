# -*- coding: utf-8 -*-
"""运营触达（v1.0 E4 运营准备）。

★★ 这个模块解决什么问题

  `notify.py`（Xf 组）已经建好了**完整的事件模板与投递通道**
  （7 类通知、站内信 + 邮件、安全类强制开、去重、审计）。

  但实测发现：**模板定义了却从未被触发** ——

    credit.low      0 处调用   ← 额度预警（运营最需要的能力）
    credit.granted  0 处调用
    drama.done      0 处调用

  ★ 这是 MEMORY §49 的经典教训：**"有基础设施 ≠ 已接入"**。
    又一次"最后一公里"问题 —— 建了 6 个版本的通知系统，
    关键事件却没人触发它。

  本模块的职责就是**补上这最后一公里**：集中定义"什么业务动作触发什么通知"，
  而不是把 `notify()` 调用散落到各业务文件里。

★ 三条设计铁律（继承 §19 旁路铁律）

  ① **fail-open**：任何运营通知失败都**不能影响主流程**
     （充值失败不能让用户看不到余额；扣费失败不能让生成中断）
  ② **放在核心业务之后**：先入账/扣费，再发通知。
     顺序反了会出现"通知说充值成功但余额没变"的事故。
  ③ **去重靠 notify 的 dedup_key**：同一事件多路径触发不刷屏
     （如"余额低"在连续扣费时会被反复判定）

★ 额度预警的触发策略（★ 关键设计）

  ★ 为什么不做"每次扣费都检查"：
    高频调用下会反复触发。靠 dedup 去重虽然能挡，但每次都要走一遍
    DB 查询 + 模板渲染，属于无谓开销。

  ★ 做法：**跨阈值才触发**（边沿触发，而非电平触发）
    · 记录"上次提醒时的档位"，只有**降档**才提醒
    · 档位：正常 → 偏低(≤30%) → 严重(≤10%) → 耗尽(0)
    · 这样从 30% 降到 10% 只提醒一次，不会每次扣费都提醒
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

_log = logging.getLogger("novelforge.ops")



def on_drama_done(db: Session, user_id: str, *, project_id: str,
                  title: str = "", episode: int = 0,
                  scenes: int = 0) -> bool:
    """短剧生成完成通知（★ 长任务结束必须告知，否则用户只能反复刷新）。"""
    try:
        from apps.api import notify
        notify.notify(
            db, user_id=user_id, event="drama.done",
            ctx={"title": title or "短剧项目", "episode": episode,
                 "scenes": scenes, "project_id": project_id},
            dedup_key=f"drama_done:{project_id}:{episode}",
        )
        return True
    except Exception as e:                              # noqa: BLE001
        _log.warning("短剧完成通知异常（已忽略）：%s: %s", type(e).__name__, e)
        return False


def on_drama_failed(db: Session, user_id: str, *, project_id: str,
                    title: str = "", episode: int = 0,
                    reason: str = "") -> bool:
    """短剧生成失败通知（★ 失败更要主动告知 —— 否则用户以为还在跑）。"""
    try:
        from apps.api import notify
        notify.notify(
            db, user_id=user_id, event="drama.failed",
            ctx={"title": title or "短剧项目", "episode": episode,
                 "reason": reason or "生成过程出错", "project_id": project_id},
            dedup_key=f"drama_fail:{project_id}:{episode}",
        )
        return True
    except Exception as e:                              # noqa: BLE001
        _log.warning("短剧失败通知异常（已忽略）：%s: %s", type(e).__name__, e)
        return False


def on_welcome(db: Session, user_id: str, *, username: str = "") -> bool:
    """注册欢迎（★ 运营第一触点：告诉用户"先做什么"）。

    ★ 为什么注册就该发：新用户的第一小时决定留存。
      一封"三步上手"的邮件比"欢迎使用"有用得多。
    """
    try:
        from apps.api import notify
        notify.notify(
            db, user_id=user_id, event="welcome",
            ctx={"username": username or "创作者"},
            dedup_key=f"welcome:{user_id}",
        )
        return True
    except Exception as e:                              # noqa: BLE001
        _log.warning("欢迎通知异常（已忽略）：%s: %s", type(e).__name__, e)
        return False


__all__ = [
    "on_drama_done", "on_drama_failed", "on_welcome",
]

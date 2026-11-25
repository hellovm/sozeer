"""对话历史 token 感知窗口（#62）。

长会话下把早期消息截掉、只保留最近的上下文，防 token 膨胀 + 历史理解衰减。

★ 设计原则（血泪「确定性 / 无副作用 / 可单测」）：
  - **纯函数**：`trim_history(history, max_tokens)` 只读输入、不写 DB、不触网、
    不调 LLM，同输入同输出。
  - **只截断、不生成**：本模块只做「窗口截断」（从最新往旧累加、超预算停），
    早期的「滚动摘要压缩」（把被截掉的消息压成摘要）是后续增强，此处不引入
    任何非确定性逻辑。
  - **保守估算**：token 数按「字符数」近似（中文 1 字 ≈ 1 token，略高估 →
    偏早触发截断 = 更安全，避免上下文溢出）。
"""
from __future__ import annotations

from typing import Any


def estimate_tokens(text: str) -> int:
    """粗略估算文本 token 数（按字符数，保守高估）。空串 → 0。"""
    t = (text or "").strip()
    return len(t)


def trim_history(history: list[dict] | None, max_tokens: int) -> list[dict]:
    """从**最新**往旧累加 token，超过 `max_tokens` 就截掉更早的。

    - `max_tokens` <= 0：不截断，原样返回（保持旧行为 / 关闭窗口）。
    - 输入顺序为「旧 → 新」，返回保持同序（旧 → 新），但可能更短。
    - **至少保留最后 1 条**（即使它自身超预算也要给模型当前上下文）。
    - 确定性：同输入同输出，无随机 / 无时间戳。

    血泪：纯函数、无副作用、pytest 可直接 import 验证（不依赖 LLM）。
    """
    if not history:
        return []
    if not max_tokens or max_tokens <= 0:
        # 关闭窗口（配置为 0 / 负数）→ 不截断，行为与旧版完全一致
        return list(history)

    kept: list[dict] = []
    acc = 0
    for m in reversed(history):          # 从最新往旧
        t = estimate_tokens(str((m or {}).get("content", "")))
        # ★ 预算尽量不超：已有内容且「加入这条会超」→ 停（这条及更早的都不要）。
        #   第一条（最新）无条件保留（kept 为空时），保证至少给模型 1 条上下文。
        #   边界：acc+t == budget 仍保留（不浪费刚好放得下的消息）。
        if kept and acc + t > max_tokens:
            break
        kept.insert(0, m)
        acc += t
    return kept


def window_stats(history: list[dict] | None, max_tokens: int) -> dict[str, Any]:
    """供 /debug 或日志观测：截断前后的条数与估算 token。确定性、无副作用。"""
    h = history or []
    kept = trim_history(h, max_tokens)
    return {
        "before": len(h),
        "after": len(kept),
        "est_tokens_before": sum(estimate_tokens(str((m or {}).get("content", ""))) for m in h),
        "est_tokens_after": sum(estimate_tokens(str((m or {}).get("content", ""))) for m in kept),
        "budget": max_tokens,
    }

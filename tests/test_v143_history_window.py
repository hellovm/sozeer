"""#62 对话历史 token 感知窗口（history_window.py）——纯函数不变量测试。

血泪「确定性 / 无副作用 / 可单测」：trim_history 只读输入、不写 DB、不调 LLM，
同输入同输出。这些测试直接 import 纯函数验证，不依赖 mcp/LLM 客户端 API。

钉死的核心不变量：
  1. 预算 <= 0 → 关窗口，原样返回（保持旧行为，零回归关键）。
  2. 永远至少保留最近 1 条（即使它自身超预算）。
  3. 保持「旧 → 新」顺序（截断的是更旧的，最新的永远留）。
  4. 确定性：同输入同输出、无随机/时间戳。
  5. 只截断不生成（返回的是输入的连续后缀，绝不改写消息内容）。
"""
import pytest

from apps.api import history_window as hw
from apps.api import config as nf_config


def _msgs(n: int, each_len: int) -> list[dict]:
    """造 n 条「旧→新」消息，每条 content 长 each_len 字符。"""
    return [{"role": "user" if i % 2 else "assistant",
             "content": "x" * each_len} for i in range(n)]


def test_empty_history():
    assert hw.trim_history([], 100) == []
    assert hw.trim_history(None, 100) == []


def test_zero_budget_disables_window():
    """预算 0 / 负 → 不截断（保持旧的全量透传行为，零回归关键）。"""
    h = _msgs(5, 10)
    assert hw.trim_history(h, 0) == h
    assert hw.trim_history(h, -1) == h
    assert hw.trim_history(None, 0) == []


def test_within_budget_keeps_all():
    h = _msgs(3, 10)            # 30 字符
    assert hw.trim_history(h, 100) == h


def test_trims_oldest_keeps_newest():
    """超预算时从「旧」往「新」丢：最新的永远留，更旧的被截。"""
    h = _msgs(5, 10)            # 每条 10 字符，总 50
    kept = hw.trim_history(h, 30)
    # 从最新往旧累加（尽量不超）：10,20,30 都 ≤30 保留 3 条；第4条 30+10=40>30 停
    assert kept == h[2:]
    assert kept[0] is h[2]      # 连续后缀，未改写


def test_at_least_one_kept_even_if_over_budget():
    """即使单条就超预算，也要保留最近 1 条（给模型最小上下文）。"""
    h = _msgs(1, 1000)
    kept = hw.trim_history(h, 10)
    assert len(kept) == 1
    assert kept[0] is h[0]


def test_preserves_order_old_to_new():
    h = _msgs(4, 8)
    kept = hw.trim_history(h, 8 * 3)          # 保留最近 3 条
    assert [len(m["content"]) for m in kept] == [8, 8, 8]
    assert kept == h[-3:]


def test_does_not_mutate_input():
    h = _msgs(5, 10)
    snapshot = [dict(m) for m in h]
    hw.trim_history(h, 20)
    assert h == snapshot


def test_deterministic_no_random():
    h = _msgs(10, 12)
    a = hw.trim_history(h, 30)
    b = hw.trim_history(h, 30)
    assert a == b
    assert a is not b          # 返回新列表，不共享可变引用


def test_window_stats_shape():
    h = _msgs(5, 10)
    s = hw.window_stats(h, 25)
    assert s["before"] == 5
    assert s["after"] == 2            # budget 25：装 2 条(20)，第 3 条 30>25 停
    assert s["budget"] == 25
    assert s["est_tokens_after"] <= s["est_tokens_before"]


def test_config_default_budget_positive():
    """默认预算 > 0（窗口开启）；这是行为开关，锁死默认值防误改。"""
    assert nf_config.CHAT_HISTORY_TOKENS > 0

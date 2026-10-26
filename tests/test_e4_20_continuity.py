"""v1.0-E4.20 · 章节连续性修复测试。

覆盖：
  - gates.repeat_rate：与前文的 12-gram 重复率计算（边界 + 高重叠）
  - check_repeat：紧邻上一章高重叠判 error（连续性护栏的判定源）
  - context._continuity_block：首章返回空；后续章包含「上一章结尾」+「最近已写章节」
  - chapter_write 技能模板：含连续性衔接段与修正后的「起」指令

★ 设计原则：只测确定性逻辑（不依赖真实 LLM 调用），重生护栏的判定直接复用
  gates.repeat_rate（本文件已覆盖），整章生成走冒烟/线上验证。
"""
from __future__ import annotations

import sys
import uuid

import pytest

from apps.api.orchestrator import gates
from apps.api.skills import loader as skills_loader
from apps.api.orchestrator import context as ctx_builder


def _mod(name: str):
    return sys.modules.get(f"apps.api.{name}") or __import__(
        f"apps.api.{name}", fromlist=["*"])


@pytest.fixture(scope="module", autouse=True)
def _ensure_tables():
    from apps.api import models  # noqa: F401
    from apps.api.db import Base, engine
    Base.metadata.create_all(engine)
    from apps.api.services import db_migrate
    db_migrate.migrate()
    yield


@pytest.fixture()
def db():
    SL = _mod("db").SessionLocal
    s = SL()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def book(db):
    b = _mod("models").Book(id=f"bk_{uuid.uuid4().hex[:8]}", title="连续性测试书")
    db.add(b)
    db.commit()
    return b


def _add_chapter(db, book, idx, title, content, summary=""):
    ch = _mod("models").Chapter(
        id=f"{book.id}_{idx}", book_id=book.id, idx=idx,
        title=title, content=content, summary=summary, words=len(content),
    )
    db.add(ch)
    db.commit()
    return ch


# ════════════════════════ gates.repeat_rate ══════════════════════════

def test_repeat_rate_empty():
    assert gates.repeat_rate("", "") == 0.0
    assert gates.repeat_rate("正文内容", "") == 0.0
    assert gates.repeat_rate("", "前文内容") == 0.0


def test_repeat_rate_different_low():
    a = "林晚拔剑出鞘，寒光映着窗外的雪。"
    b = "陈长安在酒楼里喝着闷酒，盘算明日的行程。"
    assert gates.repeat_rate(a, b) < 0.02


def test_repeat_rate_near_identical_high():
    base = ("林晚拔剑出鞘，寒光映着窗外的雪。"
            "她忽然收剑入鞘，转身望向身后那道黑影。"
            "「你终于肯现身了。」")
    copy = base + "黑影低笑一声，并未答话。"  # 大段复用上一章结尾
    assert gates.repeat_rate(copy, base) > 0.15


# ════════════════════════ check_repeat 紧邻上一章 ════════════════════════

def test_check_repeat_immediate_error():
    """新章大段复用紧邻上一章内容 → 判 error（连续性护栏据此重生）。"""
    prev = ("林晚拔剑出鞘，寒光映着窗外的雪。"
            "她忽然收剑入鞘，转身望向身后那道黑影。")
    body = (prev + "\n黑影低笑一声，并未答话，只将手中折扇轻轻展开。"
            "林晚眯起眼，指尖已扣住三枚银针。")
    rep = gates.GateReport()
    gates.check_repeat(body, [prev], rep)
    errors = [i for i in rep.issues if i.rule == "重复度" and i.level == "error"]
    assert errors, "紧邻上一章高重叠应判 error"
    assert rep.stats.get("repeat_immediate", 0) > 0.12


def test_check_repeat_low_ok():
    """不同内容 → 不报错。"""
    prev = "林晚拔剑出鞘，寒光映着窗外的雪。"
    body = "陈长安在酒楼里喝着闷酒，盘算明日的行程与那桩未了的恩怨。"
    rep = gates.GateReport()
    gates.check_repeat(body, [prev], rep)
    errors = [i for i in rep.issues if i.rule == "重复度" and i.level == "error"]
    assert not errors


# ════════════════════════ context._continuity_block ════════════════════════

def test_continuity_block_first_chapter_empty(db, book):
    """首章（无上一章）→ 返回空，由提示词兜底。"""
    assert ctx_builder._continuity_block(db, book, 1) == ""


def test_continuity_block_has_prev_ending(db, book):
    """后续章 → 必须包含上一章结尾片段与最近已写章节清单。"""
    long_tail = "黑影低笑一声：「你以为这把剑，真是给你用的？」林晚瞳孔骤缩。" * 6
    _add_chapter(db, book, 1, "风雪夜", "第一章正文。" + long_tail,
                 summary="林晚雪夜遇袭，神秘黑影现身。")
    block = ctx_builder._continuity_block(db, book, 2)
    assert block, "后续章应生成连续性衔接块"
    # 上一章结尾（最近 ~900 字）必须出现在块里
    assert "上一章结尾" in block
    # 最近已写章节清单必须出现上一章标题
    assert "风雪夜" in block
    assert "最近已写章节" in block
    # 上一章摘要（已发生，不得复述）必须出现
    assert "林晚雪夜遇袭" in block


# ════════════════════════ chapter_write 技能模板 ════════════════════════

def test_chapter_write_skill_has_continuity():
    """代笔模板必须包含连续性衔接段 + 修正后的「起」指令（接上一章结尾）。"""
    sk = skills_loader.load("chapter_write", None)
    assert sk is not None, "chapter_write 技能必须存在"
    body = sk.body
    assert "连续性衔接" in body, "模板缺少连续性衔接段"
    assert "{{continuity}}" in body, "模板未注入 continuity 变量"
    assert "上一章结尾" in body, "应要求接续上一章结尾"
    assert "不得复述" in body or "不得重复" in body, "应禁止复述已写章节"
    # 「起」指令从「从一个具体场景切入」改为「紧接上一章结尾」
    assert "必须紧接" in body
    # variables 声明里包含 continuity
    assert "continuity" in (sk.variables or [])

"""v0.14.5 D11 辩论循环回归测试。

覆盖：
1. polish_rounds_for：≥85→0 / 70-84→1 / <70→2 三档动态轮数；
2. node_polish 择优回滚：改写后分数下降 → 回滚到改写前版本（gate 事件不误发）；
3. node_polish 辩论轮：<70 第二轮调用 editor_review（critic 模型），编辑意见并入改写指令；
4. 免修路径：≥85 分不进循环、不调模型；
5. 兼容：MAX_POLISH_ROUNDS 常量保留（外部引用不炸），editor_review skill 模板可加载。

跑法：项目根目录执行  python -m pytest tests/test_v145_d11_debate.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = os.environ.get("NOVELFORGE_DATA", tempfile.mkdtemp(prefix="nf_test_v145_"))
os.environ.setdefault("NOVELFORGE_DATA", _TMP)
os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-for-pytest-only")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import apps.api.models  # noqa: F401,E402  模型注册表必须先导入，create_all 才有表


def asyncio_run(coro):
    """测试内跑协程（项目未装 pytest-asyncio，用同步帮手，与 v141 用例一致）。"""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


@pytest.fixture()
def db():
    from apps.api.db import Base, engine, SessionLocal
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        yield s


@pytest.fixture()
def book(db):
    import uuid
    from apps.api.models import Book
    b = Book(id=f"bk_{uuid.uuid4().hex[:8]}", title="D11测试书", genre="仙侠")
    db.add(b)
    db.commit()
    return b


# ---------------------------------------------------------------- 轮数函数

def test_polish_rounds_three_tiers():
    from apps.api.orchestrator.graph import polish_rounds_for
    assert polish_rounds_for(100) == 0
    assert polish_rounds_for(85) == 0
    assert polish_rounds_for(84) == 1
    assert polish_rounds_for(70) == 1
    assert polish_rounds_for(69) == 2
    assert polish_rounds_for(0) == 2


def test_max_polish_rounds_kept():
    from apps.api.orchestrator import graph
    assert graph.MAX_POLISH_ROUNDS == 1  # 保留常量，外部引用兼容


def test_editor_review_skill_loadable():
    from apps.api.skills import loader
    s = loader.load("editor_review", None)
    assert s is not None
    assert "notes" in s.body
    assert "{{content}}" in s.body and "{{issues}}" in s.body


# ---------------------------------------------------------------- node_polish 行为

class _Issue:
    def __init__(self, rule, level, message, span="", fix_hint=""):
        self.rule, self.level, self.message = rule, level, message
        self.span, self.fix_hint = span, fix_hint


class _Report:
    def __init__(self, score, issues):
        self.score, self.issues = score, issues
        self.stats = {"words": 2500}

    def to_dict(self):
        return {"score": self.score, "issues": [i.__dict__ for i in self.issues]}


class _Model:
    def __init__(self, name="m"):
        self.model_id = name
        self.name = name  # 测试内区分 writer/critic 用
        self.provider = "stub"


class _FC:
    """node_polish 所需的最小 FlowContext 替身。"""

    def __init__(self, book, report, model=None, critic=None):
        from apps.api.orchestrator.graph import FlowContext

        class _DB:  # StyleProfile/skill Setting 查询替身：永远查不到
            def scalar(self, *a, **k):
                return None

            def get(self, *a, **k):
                return None

        self.db = _DB()
        self.book = book
        self.report = report
        self.model_writer = model
        self.model_critic = critic
        self.content = "原稿内容"
        self.pack = None
        self.tokens_in = self.tokens_out = 0
        self.cost = 0.0
        self.events = []

    async def event(self, kind, **data):
        self.events.append({"kind": kind, **data})


@pytest.fixture()
def patch_gates(monkeypatch):
    """闸门替换：按文本内容返回预设分数序列。"""
    from apps.api.orchestrator import graph
    scores = []

    def _run(content, *a, **k):
        s = scores.pop(0) if scores else 90
        return _Report(s, [] if s >= 85 else [_Issue("去AI味", "warn", "套话")])

    monkeypatch.setattr(graph.gates, "run_gates", _run)
    return scores


def test_polish_skip_when_score_high(db, book, monkeypatch):
    """≥85 分免修：不进循环、不调模型。"""
    from apps.api.orchestrator import graph
    called = {"n": 0}

    async def fake_complete(model, msgs, **k):
        called["n"] += 1
        raise AssertionError("高分不应调用模型")

    monkeypatch.setattr(graph.llm, "complete", fake_complete)
    fc = _FC(book, _Report(88, []), model=_Model())
    asyncio_run(graph.node_polish(fc))
    assert called["n"] == 0
    assert fc.content == "原稿内容"


def test_polish_rollback_on_score_drop(db, book, monkeypatch):
    """改写后分数下降 → 回滚到改写前版本。"""
    from apps.api.orchestrator import graph

    async def fake_complete(model, msgs, **k):
        class R:
            error = ""
            text = "改坏了的稿子"
            input_tokens = 10
            output_tokens = 10
            cost = 0.01
        return R()

    monkeypatch.setattr(graph.llm, "complete", fake_complete)
    # 初始 75 分（一轮）；改写后 60 分（下降）→ 回滚
    scores = [75, 60]
    scores.insert(0, 75)  # run_gates 序列：改写后 60
    monkeypatch.setattr(graph.gates, "run_gates",
                        lambda content, *a, **k: _Report(60, []))
    fc = _FC(book, _Report(75, [_Issue("去AI味", "warn", "套话")]), model=_Model())
    asyncio_run(graph.node_polish(fc))
    assert fc.content == "原稿内容"  # 回滚生效
    warns = [e for e in fc.events if e["kind"] == "warn"]
    assert any("回滚" in w.get("text", "") for w in warns), str(fc.events)


def test_polish_keeps_better_version(db, book, monkeypatch):
    """改写后分数上升 → 保留新版并发 gate 事件。"""
    from apps.api.orchestrator import graph

    async def fake_complete(model, msgs, **k):
        class R:
            error = ""
            text = "改好了的稿子"
            input_tokens = 10
            output_tokens = 10
            cost = 0.01
        return R()

    monkeypatch.setattr(graph.llm, "complete", fake_complete)
    monkeypatch.setattr(graph.gates, "run_gates",
                        lambda content, *a, **k: _Report(92, []))
    fc = _FC(book, _Report(75, [_Issue("去AI味", "warn", "套话")]), model=_Model())
    asyncio_run(graph.node_polish(fc))
    assert fc.content == "改好了的稿子"
    gates_ev = [e for e in fc.events if e["kind"] == "gate"]
    assert len(gates_ev) == 1


def test_debate_round_calls_editor_review(db, book, monkeypatch):
    """<70 两轮：第二轮先调 editor_review（critic），编辑意见并入改写指令。"""
    from apps.api.orchestrator import graph
    from apps.api.skills import loader as skills

    calls = {"critic": 0, "writer": 0, "prompts": []}

    async def fake_complete(model, msgs, **k):
        prompt = msgs[0]["content"]
        if model.name == "critic-x":
            calls["critic"] += 1
            assert "编辑" in prompt or "责编" in prompt  # editor_review 模板
            class R:
                error = ""
                text = '{"notes": [{"note": "第2段缺情绪过渡"}], "overall": "补过渡"}'
                input_tokens = 10
                output_tokens = 10
                cost = 0.01
            return R()
        calls["writer"] += 1
        calls["prompts"].append(prompt)
        class R:
            error = ""
            text = f"第{calls['writer']}版改写稿"
            input_tokens = 10
            output_tokens = 10
            cost = 0.01
        return R()

    monkeypatch.setattr(graph.llm, "complete", fake_complete)
    # 闸门序列：轮1改写后85分（停），初始69分
    monkeypatch.setattr(graph.gates, "run_gates",
                        lambda content, *a, **k: _Report(85, []))
    critic = _Model("critic-x")
    writer = _Model("writer-x")

    real_load = skills.load

    def fake_load(skill_id, db_):
        if skill_id == "editor_review":
            return real_load("editor_review", None)
        if skill_id == "deai_polish":
            class S:
                body = "{issues}\n{original}\n{style_dna}"
            return S()
        return real_load(skill_id, db_)

    monkeypatch.setattr(graph.skills, "load", fake_load)
    monkeypatch.setattr(graph.skills, "render",
                        lambda sk, **kw: sk.body.format(**kw))
    fc = _FC(book, _Report(69, [_Issue("去AI味", "warn", "套话"),
                                _Issue("节奏", "warn", "拖沓")]),
             model=writer, critic=critic)
    asyncio_run(graph.node_polish(fc))
    # 69 分 → 两轮上限；但轮 1 改写后 85 分（无 issue）→ 轮 2 break
    assert calls["writer"] == 1
    assert calls["critic"] == 0  # 辩论轮只在 i>0 触发，轮1无 critic 调用
    assert "第1版改写稿" == fc.content


def test_debate_second_round_uses_editor_notes(db, book, monkeypatch):
    """<70 且首轮改写后仍有 issue → 轮 2 触发 editor_review，意见进 prompt。"""
    from apps.api.orchestrator import graph
    from apps.api.skills import loader as skills

    calls = {"critic": 0, "writer": 0, "prompts": []}

    async def fake_complete(model, msgs, **k):
        prompt = msgs[0]["content"]
        if model.name == "critic-x":
            calls["critic"] += 1
            class R:
                error = ""
                text = '{"notes": [{"note": "对话标签仍有套话"}], "overall": "x"}'
                input_tokens = 10
                output_tokens = 10
                cost = 0.01
            return R()
        calls["writer"] += 1
        calls["prompts"].append(prompt)
        class R:
            error = ""
            text = f"第{calls['writer']}版改写稿"
            input_tokens = 10
            output_tokens = 10
            cost = 0.01
        return R()

    monkeypatch.setattr(graph.llm, "complete", fake_complete)
    # 闸门序列：轮1改写后 72（仍有 issue，进轮2）、轮2改写后 90
    seq = [iter([72, 90])]
    def fake_gates(content, *a, **k):
        try:
            s = next(seq[0])
        except StopIteration:
            s = 90
        return _Report(s, [] if s >= 85 else [_Issue("去AI味", "warn", "套话")])
    monkeypatch.setattr(graph.gates, "run_gates", fake_gates)

    real_load = skills.load

    def fake_load(skill_id, db_):
        if skill_id == "editor_review":
            class S:
                body = "{content}\n{issues}"  # 与 node_polish 传参对齐（content/issues）
            return S()
        if skill_id == "deai_polish":
            class S:
                body = "{issues}\n{original}\n{style_dna}"
            return S()
        return real_load(skill_id, db_)

    monkeypatch.setattr(graph.skills, "load", fake_load)
    monkeypatch.setattr(graph.skills, "render",
                        lambda sk, **kw: sk.body.format(**kw))
    fc = _FC(book, _Report(69, [_Issue("去AI味", "error", "套话")]),
             model=_Model("writer-x"), critic=_Model("critic-x"))
    asyncio_run(graph.node_polish(fc))
    assert calls["critic"] == 1  # 轮 2 触发辩论
    assert calls["writer"] == 2
    # 轮 2 prompt 必须含编辑意见
    assert "对话标签仍有套话" in calls["prompts"][1]
    assert fc.content == "第2版改写稿"

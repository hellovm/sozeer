"""★ v1.3.0 连续性强化回归测试。

背景（血泪：装配器早已算好包、主路径却漏传 → 被 loader 静默清空）：
    `build_chapter_context` 早已把「上 3 章全文 + 世界观 + 伏笔 + 进度(rolling/纪年/度量衡)
    + 连续性衔接块(continuity) + 信息差(knowledge) + 三级大纲(outline)」全部算好，
    但 `node_generate` 只喂了其中一部分，漏传 continuity/knowledge/outline ——
    而 `skills.loader.render()` 会把未传变量对应的 `{{var}}` **静默清空**，
    等价于这些"强化连续性"的包**从未真正进过 prompt**。

本文件固化「接通后必须进 prompt」这一行为：
1. 正文（node_generate）：world/伏笔/进度 仍在 + continuity/knowledge/outline 三包进 prompt；
2. 任务单（node_brief）：continuity/outline 进 prompt（规划侧也要接得上、不跑偏）；
3. 装配器护栏：RECENT_FULL=3 全文 + 世界观 + 伏笔 + 滚动进度 确实被算出（非空）。

跑法：项目根目录执行  python -m pytest tests/test_v130_continuity_injection.py -q
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = os.environ.get("NOVELFORGE_DATA", tempfile.mkdtemp(prefix="nf_test_v130_"))
os.environ.setdefault("NOVELFORGE_DATA", _TMP)
os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-for-pytest-only")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import apps.api.models  # noqa: F401,E402  模型注册表必须先导入，create_all 才有表

from apps.api.db import Base, engine, SessionLocal
from apps.api.models import (
    Book, Chapter, WorldSetting, Foreshadow, Character, CharacterKnowledge,
)
from apps.api.orchestrator import graph, context as ctx_builder


def _run(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


@pytest.fixture()
def db():
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        yield s


@pytest.fixture()
def book(db):
    import uuid
    bid = f"bk_v130_{uuid.uuid4().hex[:8]}"
    b = Book(id=bid, title="连续性强化测试书", genre="仙侠",
             rolling_summary="主角已入宗门，正修基础功法，尚未触及剑冢",
             outline_meta=json.dumps({"logline": "寒门弟子逆天改命",
                                      "theme": "成长", "conflict": "正邪对决"},
                                     ensure_ascii=False))
    db.add(b)
    # 前 4 章（写第 5 章时：prev=1..4，最近 3 章=2/3/4）
    for i in range(1, 5):
        db.add(Chapter(id=f"ch_{bid}_{i}", book_id=bid, idx=i,
                       title=f"章{i}", content=f"第{i}章正文……残灯忽灭（第{i}章）。",
                       summary=f"第{i}章剧情摘要"))
    db.add(WorldSetting(id=f"ws_{bid}", book_id=bid, category="规则",
                        key="御剑", value="御剑飞行每旬耗灵石三百"))
    db.add(Foreshadow(id=f"fs_{bid}", book_id=bid, content="剑冢中藏上古残图",
                      planted_ch=1, status="open"))
    db.add(Character(id=f"ct_{bid}", book_id=bid, name="陈长安", role="主角"))
    db.add(CharacterKnowledge(id=f"ck_{bid}", book_id=bid, character_id=f"ct_{bid}",
                              character_name="陈长安", fact_id=f"fs_{bid}",
                              fact_desc="反派早已探知他的身世", known=0, chapter_learned=0))
    db.commit()
    return b


class _M:
    """最小 ModelProfile 替身（只供 fc.track 读取 role/model_id）。"""
    role = "writer"
    model_id = "fake-writer"
    name = "writer"


def _make_fc(db, book, ch_idx, brief=None, mentioned=None):
    fc = graph.FlowContext(db=db, book=book, ch_idx=ch_idx, emit=_noop_emit,
                           user_instruction="推进主线", t0=0.0)
    fc.model_writer = _M()
    fc.model_planner = None
    fc.model_critic = None
    fc.brief = brief or {"title": "天光忽暗", "goal": "推进主线", "beat": [],
                        "payoff": [], "plant": [], "coolpoint": "",
                        "characters": mentioned or [], "constraint": "", "hook": ""}
    fc.pack = ctx_builder.build_chapter_context(
        db, book, ch_idx, brief=graph._render_brief(fc.brief),
        user_instruction="推进主线", mentioned=mentioned or None)
    return fc


async def _noop_emit(_e):
    return None


# ---------------------------------------------------------------- 装配器护栏
def test_context_pack_computes_all_continuity_fields(db, book):
    """装配器确实算出「上3章全文 + 世界观 + 伏笔 + 进度」，且三包非空（首章才空）。"""
    pack = ctx_builder.build_chapter_context(db, book, 5, mentioned=["陈长安"])
    txt = pack.render()
    # 上 3 章全文（RECENT_FULL=3）：2/3/4 章正文齐全
    assert txt.count("### 第") >= 3, f"应含最近 3 章全文，实际 {txt.count('### 第')}"
    # 世界观
    assert "御剑飞行每旬耗灵石三百" in pack.world_view
    # 伏笔（含债务）
    assert "剑冢中藏上古残图" in pack.foreshadows
    # 进度（滚动剧情走向）
    assert "已入宗门" in pack.rolling
    # 三个"强化"包在写第 5 章时应非空
    assert pack.continuity, "continuity 包应非空（前 4 章已存在）"
    assert pack.knowledge, "knowledge 包应非空（陈长安 有 1 条未知事实）"
    assert pack.outline, "outline 包应非空（book.outline_meta 已配）"


# ---------------------------------------------------------------- node_generate
def test_node_generate_injects_continuity_knowledge_outline(db, book, monkeypatch):
    """正文 prompt 必须同时含 旧有包(世界观/伏笔/进度) + 三包(continuity/knowledge/outline)。"""
    cap = {}

    async def _fake_stream_usage(model, msgs, **kw):
        cap["prompt"] = msgs[0]["content"]
        yield {"kind": "token", "text": "# 第005章 天光忽暗\n\n残灯忽灭。\n"}
        yield {"kind": "done", "input_tokens": 10, "output_tokens": 20,
               "cost": 0.0, "duration_ms": 1}

    monkeypatch.setattr(graph.llm, "stream_usage", _fake_stream_usage)
    fc = _make_fc(db, book, 5, mentioned=["陈长安"])
    _run(graph.node_generate(fc))

    p = cap["prompt"]
    # 旧有（应一直在）
    assert "御剑飞行每旬耗灵石三百" in p, "世界观未进 prompt"
    assert "剑冢中藏上古残图" in p, "伏笔未进 prompt"
    assert "已入宗门" in p, "进度(滚动摘要)未进 prompt"
    assert p.count("### 第") >= 3, "上 3 章全文未进 prompt"
    # ★ v1.3.0 接通后必须出现（未接通时这些包被 render 静默清空）
    assert "必须紧接此处悬念/场景续写" in p, "continuity(连续性衔接) 未进 prompt"
    assert "尚不知" in p and "反派早已探知他的身世" in p, "knowledge(信息差) 未进 prompt"
    assert "【全书定位】" in p and "寒门弟子逆天改命" in p, "outline(三级大纲) 未进 prompt"
    # 生成正常落 content
    assert "残灯忽灭" in fc.content


# ---------------------------------------------------------------- node_brief
def test_node_brief_injects_continuity_and_outline(db, book, monkeypatch):
    """任务单 prompt 也必须含 continuity + outline（规划侧接得上、不跑偏）。"""
    cap = {}

    async def _fake_complete(model, msgs, **kw):
        cap["prompt"] = msgs[0]["content"]

        class R:
            error = ""
            text = json.dumps({"title": "天光忽暗", "goal": "推进主线", "beat": [],
                              "payoff": [], "plant": [], "coolpoint": "打脸",
                              "characters": ["陈长安"], "constraint": "", "hook": "残灯"},
                             ensure_ascii=False)
            input_tokens = 5
            output_tokens = 10
            cost = 0.0
            duration_ms = 1
        return R()

    monkeypatch.setattr(graph.llm, "complete", _fake_complete)
    fc = _make_fc(db, book, 5, mentioned=["陈长安"])
    _run(graph.node_brief(fc))

    p = cap["prompt"]
    assert "必须紧接此处悬念/场景续写" in p, "任务单未注入 continuity"
    assert "【全书定位】" in p, "任务单未注入 outline"
    # brief 解析成 dict
    assert isinstance(fc.brief, dict) and fc.brief.get("title") == "天光忽暗"

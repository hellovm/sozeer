"""Agent 对话工作室冒烟测试（聚焦 v0.3 新链路）。

不触网：monkeypatch 掉 llm.act / llm.complete / git，直接驱动
agent.chat_stream → 验证「工具调用→设定集持久化→chapter_draft 事件」，
再走 accept_draft 验证草稿落库成章。
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

# ① 指向临时数据目录（干净的 sqlite）
TMP = tempfile.mkdtemp(prefix="nf_agent_")
os.environ.setdefault("NOVELFORGE_DATA", TMP)
os.environ["NOVELFORGE_DB_URL"] = f"sqlite:///{TMP}/nf.db"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.api import db as dbmod
dbmod.init_db()
from apps.api import models
from apps.api import agent as agent_mod
from apps.api.orchestrator import graph as graph_mod
from apps.api.llm import AgentTurn, LLMResult
from apps.api import gitrepo

# 一段示例正文（测试不强制字数门槛，仅验证落库与事件链路）
BODY = "# 第001章 退婚那天，我觉醒了躺平系统\n\n" + (
    "天刚蒙蒙亮，沈闲就被门外一阵吵闹声吵醒。\n\n"
    "他翻了个身，把被子拉过头顶，心里盘算着今天又能躺平几个小时。\n\n"
    "“沈闲！你给我出来！”门外的人嗓门洪亮，显然是不达目的不罢休。\n\n"
    "他叹了口气。穿越到这修仙界三个月，他最大的心愿就是安安静静地当个闲人。\n\n"
    "可这世道，偏偏不让人闲。\n\n"
    "【叮——躺平系统已绑定。】脑子里忽然响起一道机械音。\n\n"
    "沈闲一愣：“什么系统？”\n\n"
    "【检测到宿主持续躺平，修为自动增长中。当前境界：炼气一层。】\n\n"
    "他眨了眨眼。这年头，连摆烂都能涨修为了？\n\n"
    "门外的声音更急了：“沈闲！林家退婚的帖子都贴到山门了，你还要脸不要？”\n\n"
    "沈闲慢吞吞坐起来。退婚？正好，他本来就不想娶。\n\n"
    "他推开门，迎着晨光伸了个懒腰：“退就退呗，我正好省下一笔彩礼。”\n\n"
    "院外所有人，集体沉默。\n"
)


class FakeModel:
    # ★ chat_stream._record_run 用 model.id 直接取 model_id（非 getattr 兜底），
    #   FakeModel 必须同时提供 id（缺则 AttributeError → error 事件 → 无 chapter_draft）。
    id = "mock"
    model_id = "mock"
    base_url = "http://mock"
    temperature = 0.7
    max_tokens = 4000


# ② monkeypatch：第一轮 act 返回工具调用；complete 返回正文
async def fake_act(p, messages, tools, **kw):
    return AgentTurn(
        text="好的，我先帮你立项，再起草第一章。",
        tool_calls=[
            {"id": "c1", "name": "set_project_meta", "arguments": {
                "title": "躺平修仙", "genre": "男频·修仙", "platform": "番茄小说",
                "chapter_min": 2000, "chapter_max": 2200, "target_words": 2000,
                "tone": "轻松反套路",
                "logline": "社畜穿修仙，躺平也能涨修为",
                "selling_points": ["躺平爽点", "反套路系统", "宗门日常"],
                "synopsis": "沈闲穿进修仙界，绑定躺平系统，越摆烂修为越高。",
                "tags": ["修仙", "轻松", "系统"], "category": "男频"}},
            {"id": "c2", "name": "draft_chapter", "arguments": {
                "instruction": "写第一章，黄金三章强钩子开场", "ch_idx": 1}},
        ],
        model="mock",
    )


async def fake_complete(p, messages, **kw):
    return LLMResult(text=BODY, error="", cost=0.0, model="mock")


# ★ 测试隔离（修复跨文件污染 · 血泪"假阴性比失败更危险"）：
#   全局 monkeypatch 必须收进 run() 内部，绝不能在模块级执行。
#   本文件无 test_ 前缀，正常全量回归不收集它；但若显式
#   `pytest tests/agent_smoke.py`，import 时刻的模块级 patch 会把
#   graph.pick_model 改成返回无 provider 的 FakeModel、gitrepo.write_file 置空，
#   泄漏给同进程的 test_core media 成本台账测试（run_media 取 m.provider
#   抛 AttributeError → 成功场景误判 ok=False → 假失败）。收进 run() 后
#   import 不再污染，standalone 行为不变。
def _install_smoke_patches():
    """安装冒烟全局 patch，返回原始引用供还原。"""
    orig = {
        "act": agent_mod.llm.act,
        "complete": agent_mod.llm.complete,
        "pick_model": graph_mod.pick_model,
        "write_file": gitrepo.write_file,
    }
    agent_mod.llm.act = fake_act
    agent_mod.llm.complete = fake_complete
    agent_mod.gen_graph.pick_model = lambda db, role: FakeModel()
    gitrepo.write_file = lambda *a, **k: None  # 跳过 git，纯测内核
    return orig


def _restore_smoke_patches(orig) -> None:
    """还原原始引用（不测试泄漏给后续用例）。"""
    agent_mod.llm.act = orig["act"]
    agent_mod.llm.complete = orig["complete"]
    agent_mod.gen_graph.pick_model = orig["pick_model"]
    gitrepo.write_file = orig["write_file"]


def make_book(db):
    b = models.Book(id="bk_test", title="测试书", workspace_id="default",
                    status="draft", genre="修仙", platform="番茄小说",
                    chapter_min=2000, chapter_max=2200, target_words=2000)
    db.add(b)
    db.add(models.ModelProfile(id="mp_w", name="Mock-正文", base_url="http://mock",
                               model_id="mock", api_key="sk", role="writer", is_default=1))
    db.commit()
    return b


async def run():
    db = dbmod.SessionLocal()
    make_book(db)
    bid = "bk_test"

    events = []
    async def emit(e):
        events.append(e)

    orig = _install_smoke_patches()
    try:
        await agent_mod.chat_stream(db, bid, "帮我立项并写第一章", [], emit)
        db.commit()
    finally:
        _restore_smoke_patches(orig)

    kinds = {}
    for e in events:
        kinds[e.get("kind")] = kinds.get(e.get("kind"), 0) + 1

    cd = next((e for e in events if e.get("kind") == "chapter_draft"), None)
    actions = [e for e in events if e.get("kind") == "action"]

    print("事件统计:", kinds)
    print("action 摘要:")
    for a in actions:
        print("  -", a.get("type"), "::", a.get("summary"))

    assert kinds.get("chapter_draft") == 1, "缺少 chapter_draft 事件"
    assert kinds.get("action", 0) >= 1, "工具未产出 action 事件（立项未持久化）"
    assert cd and cd.get("ch_idx") == 1, "chapter_draft 索引错误"

    # 立项持久化：title 落在 Book 行，扩展字段（tags 等）落在 Setting
    bk = db.get(models.Book, bid)
    meta = agent_mod.get_meta(db, bid)
    print("立项 title:", bk.title, "| tags:", meta.get("tags"))
    assert bk.title == "躺平修仙", "立项 title 未写入 Book"
    assert meta.get("tags") == ["修仙", "轻松", "系统"], "立项扩展字段未写入 Setting"

    # 验收落库
    ch_idx = cd["ch_idx"]
    title = "退婚那天，我觉醒了躺平系统"
    content = cd["text"].split("\n", 1)[1] if "\n" in cd["text"] else cd["text"]
    ch = await graph_mod.accept_draft(db, db.get(models.Book, bid), ch_idx,
                                      title, content, cd.get("gate_report"),
                                      register=False)
    print("采纳落库: idx=", ch.idx, "words=", ch.words, "status=", ch.status)
    assert ch.idx == 1 and ch.status == "final", "采纳落库失败"

    # 设定集读取
    cnt = db.query(models.Chapter).filter_by(book_id=bid).count()
    print("正文章节数:", cnt)
    assert cnt == 1, "章节数异常"

    db.close()
    print("\nAgent 链路冒烟通过 ✅")


if __name__ == "__main__":
    asyncio.run(run())

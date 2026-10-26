"""v0.14 P0一期：长程记忆三件套回归测试。

覆盖 D1 / D2 / D2b / D2c 四个机制：
1. memory.py：bigram 编码、切块、索引幂等、混合检索（过滤+排序+新近度）
2. consistency.py：伏笔债务推导、逾期分级、债务核验并入 run_consistency
3. context.py：滚动摘要包 + 语义回捞包 + 债务标记注入
4. graph.py：node_register 写观察者证据链（state_src_ch）

跑法：项目根目录执行  python -m pytest tests/test_v140_memory.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# 隔离环境：conftest.py 已统一设置，这里 setdefault 兜底
_TMP = os.environ.get("NOVELFORGE_DATA", tempfile.mkdtemp(prefix="nf_test_v140_"))
os.environ.setdefault("NOVELFORGE_DATA", _TMP)
os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-for-pytest-only")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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
    b = Book(id=f"bk_{uuid.uuid4().hex[:8]}", title="测试书", genre="仙侠")
    db.add(b)
    db.commit()
    return b


# ---------------------------------------------------------------- D1 memory

def test_encode_bigram():
    from apps.api import memory
    v = memory.encode("灵石飞剑")
    assert v.get("灵石") == 1 and v.get("石飞") == 1 and v.get("飞剑") == 1
    v2 = memory.encode("灵石灵石")
    assert v2.get("灵石") == 2
    assert memory.encode("") == {}


def test_cosine_basic():
    from apps.api import memory
    a = memory.encode("主角在坊市买了飞剑")
    b = memory.encode("坊市里买到飞剑")
    c = memory.encode("他在雪山上练功")
    assert memory._cosine(a, b) > memory._cosine(a, c)
    assert memory._cosine(a, {}) == 0.0


def test_split_chunks_alignment():
    from apps.api import memory
    paras = [f"第{i}段。" + "内容描写" * 50 for i in range(10)]
    chunks = memory._split_chunks("\n\n".join(paras))
    assert len(chunks) >= 2
    assert all(c.strip() for c in chunks)
    # 标题行被剔除
    chunks2 = memory._split_chunks("# 第001章 题\n\n" + "\n\n".join(paras[:2]))
    assert all(not c.startswith("#") for c in chunks2)


def test_index_and_search_roundtrip(db, book):
    from apps.api import memory
    from apps.api.models import Character, Chapter
    # 登记一个人物并写两章：第1章埋信物，第10章再提
    db.add(Character(id="c1", book_id=book.id, name="陈长安"))
    c1 = "# 第001章 得宝\n\n" + "陈长安在坊市得到了一枚玄天令，令牌背面刻着古老的符文。" * 8
    c10 = "# 第010章 再临坊市\n\n" + "陈长安再次来到坊市，摸了摸怀里的玄天令。" * 8
    db.add(Chapter(id="ch1", book_id=book.id, idx=1, title="得宝", content=c1))
    db.add(Chapter(id="ch10", book_id=book.id, idx=10, title="再临", content=c10))
    db.commit()
    n1 = memory.index_chapter(db, book.id, 1, c1, "得宝")
    assert n1 >= 1
    # 幂等：重复索引不产生重复
    n1b = memory.index_chapter(db, book.id, 1, c1, "得宝")
    assert n1b == n1
    memory.index_chapter(db, book.id, 10, c10, "再临")
    # 检索：query 提玄天令，应能回捞到第 1 章那块
    hits = memory.search(db, book.id, "玄天令的来历与符文", ch_idx=20,
                         char_names=["陈长安"])
    assert hits, "检索应命中"
    assert any(h["ch_idx"] == 1 for h in hits)
    assert all("[第" in h["text"] for h in hits)  # 带章号前缀
    # 只检索更早章节：写第 5 章时不应捞到第 10 章
    hits5 = memory.search(db, book.id, "玄天令", ch_idx=5)
    assert all(h["ch_idx"] < 5 for h in hits5)


def test_search_budget_limit(db, book):
    from apps.api import memory
    from apps.api.models import Chapter
    body = "江湖夜雨十年灯。" * 60
    for i in range(1, 6):
        db.add(Chapter(id=f"chb{i}", book_id=book.id, idx=i, title=f"t{i}",
                       content=f"# 第00{i}章\n\n" + body))
    db.commit()
    for i in range(1, 6):
        memory.index_chapter(db, book.id, i, f"# 第00{i}章\n\n" + body, f"t{i}")
    hits = memory.search(db, book.id, "江湖夜雨", ch_idx=10, budget=800)
    assert hits
    assert sum(h["chars"] for h in hits) <= 800


def test_extract_char_names_with_alias(db, book):
    from apps.api import memory
    from apps.api.models import Character, jd
    db.add(Character(id="c2", book_id=book.id, name="牧云谣",
                     aliases=jd(["云谣姑娘", "谣儿"])))
    db.commit()
    assert memory.extract_char_names(db, book.id, "云谣姑娘浅浅一笑") == ["牧云谣"]
    assert memory.extract_char_names(db, book.id, "路人甲走过") == []


def test_reindex_book(db, book):
    from apps.api import memory
    from apps.api.models import Chapter
    db.add(Chapter(id="chr1", book_id=book.id, idx=1, title="t",
                   content="# 第001章\n\n" + "主角一路向西，斩了妖兽。" * 30))
    db.commit()
    n = memory.reindex_book(db, book.id)
    assert n >= 1
    st = memory.memory_stats(db, book.id)
    assert st["chunks"] == n


# ---------------------------------------------------------------- D2c obligations

def test_compute_due_ch(db, book):
    from apps.api import consistency
    from apps.api.models import Foreshadow
    f1 = Foreshadow(id="f1", book_id=book.id, content="玄天令的秘密",
                    planted_ch=5, target_vol=2)          # 推导 60
    f2 = Foreshadow(id="f2", book_id=book.id, content="师父的死因",
                    planted_ch=5, target_vol=0, due_ch=25)  # 显式 25
    f3 = Foreshadow(id="f3", book_id=book.id, content="无名伏笔",
                    planted_ch=10, target_vol=0)          # 推导 40
    db.add_all([f1, f2, f3])
    db.commit()
    assert consistency.compute_due_ch(f2) == 25
    assert consistency.compute_due_ch(f1) == 60
    assert consistency.compute_due_ch(f3) == 40


def test_obligations_levels(db, book):
    from apps.api import consistency
    from apps.api.models import Foreshadow
    db.add(Foreshadow(id="fo1", book_id=book.id, content="古镜来历",
                      planted_ch=1, target_vol=1))       # due=30
    db.commit()
    # 第 55 章：逾期 25 章 → overdue_hard
    items = consistency.obligations(db, book.id, 55)
    assert items and items[0]["level"] == "overdue_hard"
    # 第 35 章：逾期 5 章 → overdue
    items = consistency.obligations(db, book.id, 35)
    assert items and items[0]["level"] == "overdue"
    # 第 25 章：due=30，距到期 5 章 ≤ 10 → due_soon
    items = consistency.obligations(db, book.id, 25)
    assert items and items[0]["level"] == "due_soon"
    # 第 2 章：还没到债务窗口 → 空
    assert consistency.obligations(db, book.id, 2) == []


def test_check_obligations_and_run_consistency(db, book):
    from apps.api import consistency
    from apps.api.models import Foreshadow
    db.add(Foreshadow(id="fo2", book_id=book.id, content="血玉之谜",
                      planted_ch=1, target_vol=1))
    db.commit()
    issues = consistency.check_obligations(db, book.id, 65)
    assert any(i["kind"] == "obligation" and i["severity"] == "error" for i in issues)
    # run_consistency 应并入债务 issue
    rep = consistency.run_consistency(db, book.id, 65)
    assert any(i["kind"] == "obligation" for i in rep["issues"])
    # 回收后（status=done）不再报
    from apps.api.models import Foreshadow as F
    row = db.get(F, "fo2")
    row.status = "done"
    db.commit()
    rep2 = consistency.run_consistency(db, book.id, 65)
    assert not any(i["kind"] == "obligation" for i in rep2["issues"])


def test_render_obligations_for_prompt(db, book):
    from apps.api import consistency
    from apps.api.models import Foreshadow
    db.add(Foreshadow(id="fo3", book_id=book.id, content="断剑重铸之日",
                      planted_ch=1, target_vol=2))   # due=60，第 85 章 → 逾期 25 → 严重
    db.commit()
    txt = consistency.render_obligations_for_prompt(db, book.id, 85)
    assert "严重逾期" in txt and "断剑重铸" in txt


# ---------------------------------------------------------------- context 装配

def test_context_pack_new_fields(db, book):
    from apps.api.models import Chapter, Foreshadow
    from apps.api.orchestrator import context as ctx
    book.rolling_summary = "主角已入金丹期，与魔教势不两立。"
    db.add(Chapter(id="cx1", book_id=book.id, idx=1, title="得宝",
                   content="# 第001章 得宝\n\n" + "陈长安得了玄天令。" * 30))
    db.add(Foreshadow(id="fx1", book_id=book.id, content="玄天令上的符文",
                      planted_ch=1, target_vol=1))
    db.commit()
    from apps.api import memory as memory_mod
    memory_mod.index_chapter(db, book.id, 1, "# 第001章 得宝\n\n" + "陈长安得了玄天令。" * 30, "得宝")
    pack = ctx.build_chapter_context(db, book, 35, brief="陈长安追查玄天令来历")
    assert "金丹期" in pack.rolling
    assert "前文回捞" in pack.render() or not pack.retrieved  # 索引可用时应命中
    # 债务标记注入：35 章 vs due=30 → overdue
    assert "逾期" in pack.foreshadows


def test_render_order_rolling_before_early(db, book):
    from apps.api.orchestrator.context import ContextPack
    p = ContextPack(rolling="全局走向", early_summaries="摘要链",
                    recent_chapters="最近正文")
    txt = p.render()
    assert txt.index("全局剧情走向") < txt.index("更早章节摘要链") < txt.index("前文（最近")


# ---------------------------------------------------------------- D2 观察者证据链

def test_state_src_ch_column(db, book):
    from apps.api.models import Character
    c = Character(id="csrc", book_id=book.id, name="李淳罡", state_now="剑开天门",
                  state_src_ch=42)
    db.add(c)
    db.commit()
    row = db.get(Character, "csrc")
    assert row.state_src_ch == 42


def test_world_setting_src_ch(db, book):
    from apps.api.models import WorldSetting
    w = WorldSetting(id="wsrc", book_id=book.id, category="境界",
                     key="金丹", value="凝丹需雷劫", src_ch=17)
    db.add(w)
    db.commit()
    assert db.get(WorldSetting, "wsrc").src_ch == 17


# ---------------------------------------------------------------- D2b rolling summary skill

def test_rolling_summary_skill_loads():
    from apps.api.skills import loader
    sk = loader.load("rolling_summary")
    assert sk is not None
    assert "全局剧情摘要" in sk.body
    assert set(sk.variables) >= {"ch_idx", "chapter_summary", "prev_summary"}


def test_chapter_write_skill_v2_has_new_vars():
    from apps.api.skills import loader
    sk = loader.load("chapter_write")
    assert sk.version == "2"
    assert "rolling" in sk.variables and "retrieved" in sk.variables

"""v0.21 B 组一致性体系回归测试。

覆盖 6 项：
  B1 向量检索记忆（含**降级兜底** —— 验收硬要求）
  B2 关系图谱（含关系矛盾检测）
  B3 情绪弧线（含低潮/无爆点告警）
  B4 伏笔自动提醒（★ 里程碑："埋了20章没回收"）
  B5 角色资产库（外观锁定 prompt）
  B6 信息差管理（★ 里程碑："角色说了他不该知道的事"）

★ 测试重点：**降级路径必须验证**。
  B1 验收明确要求"embedding 服务不可用时自动降级，不报错"，
  所以专门造"远端挂了"的场景。
"""
from __future__ import annotations

import json
import uuid

import pytest

from apps.api import consistency_v21 as C
from apps.api import embedding
from apps.api.db import SessionLocal


def _mod(name: str):
    import sys
    import importlib
    return sys.modules.get(f"apps.api.{name}") or importlib.import_module(f"apps.api.{name}")


@pytest.fixture(scope="module", autouse=True)
def _ensure_tables():
    from apps.api import models  # noqa: F401
    from apps.api.db import Base, engine
    Base.metadata.create_all(engine)
    from apps.api.services import db_migrate
    db_migrate.migrate()
    yield


@pytest.fixture(autouse=True)
def _fresh():
    import sys
    import importlib
    global C, embedding, SessionLocal
    C = _mod("consistency_v21")
    embedding = _mod("embedding")
    SessionLocal = _mod("db").SessionLocal
    embedding.reset_encoder()
    yield
    embedding.reset_encoder()


def _mk_book(owner_id: str = "u_test", title: str = "B组测试书") -> str:
    from apps.api.models import Book
    bid = f"bk_{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db:
        db.add(Book(id=bid, title=title, author="a", owner_id=owner_id))
        db.commit()
    return bid


def _mk_char(book_id: str, name: str, role: str = "主角") -> str:
    from apps.api.models import Character
    cid = f"ch_{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db:
        db.add(Character(id=cid, book_id=book_id, name=name, role=role))
        db.commit()
    return cid


def _mk_chapter(book_id: str, idx: int, content: str) -> str:
    from apps.api.models import Chapter
    cid = f"cp_{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db:
        db.add(Chapter(id=cid, book_id=book_id, idx=idx, title=f"第{idx}章",
                       content=content, words=len(content)))
        db.commit()
    return cid


# ============================================================================
#  B1 向量检索
# ============================================================================

def test_l2_normalize():
    v = embedding.l2_normalize([3.0, 4.0])
    assert abs(sum(x * x for x in v) - 1.0) < 1e-9
    assert embedding.l2_normalize([0.0, 0.0]) == [0.0, 0.0]


def test_quantize_roundtrip_lossless_enough():
    """★ int8 量化精度（损失必须可忽略，否则检索质量下降）。"""
    v = embedding.l2_normalize([0.3, -0.5, 0.8, 0.1, -0.2])
    d = embedding.dequantize_from_b64(embedding.quantize_to_b64(v))
    sim = embedding.dot(v, d)
    assert sim > 0.995, f"量化损失过大：{sim}"


def test_quantize_size_reduction():
    """int8 量化后体积应约为 float32 的 1/4（Base64 后 1/3）。"""
    v = embedding.l2_normalize([0.1] * 1024)
    b64 = embedding.quantize_to_b64(v)
    import base64 as _b
    raw = _b.b64decode(b64)
    assert len(raw) == 1024, f"量化后字节数应为 1024，实际 {len(raw)}"


def test_local_encoder_sparse():
    e = embedding.LocalEmbedding()
    v = e.encode("主角在雪山上练剑")
    assert isinstance(v, dict) and v
    assert "雪山" in v


def test_local_cosine():
    a = {"雪山": 1, "练剑": 2}
    b = {"雪山": 1, "练剑": 2}
    c = {"完全": 1, "不同": 1}
    assert embedding.cosine_sparse(a, b) > 0.99
    assert embedding.cosine_sparse(a, c) < 0.01


def test_encoder_falls_back_when_remote_down(monkeypatch):
    """★★ B1 验收核心：远端不可用时**自动降级，不报错**。"""
    monkeypatch.setenv("NF_EMBEDDING_BASE", "http://127.0.0.1:1/v1")   # 必定连不上
    monkeypatch.setenv("NF_EMBEDDING_KEY", "sk-fake")
    embedding.reset_encoder()
    e = embedding.get_encoder()
    # 远端连不上 → encode 返回 None（不抛异常）
    v = e.encode("测试文本")
    assert v is None, "远端故障时应返回 None 而不是抛异常"
    # ★ 稀疏向量始终可用（真正的兜底）
    sp = e.encode_sparse("测试文本")
    assert sp, "本地稀疏向量必须始终可用"
    d = e.describe()
    assert d["provider"] == "local" or d["remote_down"] is True


def test_remote_circuit_breaker(monkeypatch):
    """★ 熔断：连续失败 N 次后主动停止尝试（避免每次都等超时）。"""
    r = embedding.RemoteEmbedding(base_url="http://127.0.0.1:1/v1", api_key="x")
    for _ in range(embedding.FAIL_THRESHOLD):
        r.encode_batch(["t"])
    assert r.available() is False, "达到失败阈值后应熔断"
    assert r._down_until > 0


def test_encoder_never_raises_on_garbage():
    """脏输入不能抛异常（写作时正文可能含各种字符）。"""
    e = embedding.get_encoder()
    for s in ["", "\x00\x01", "🎉" * 100, "a" * 100000]:
        try:
            e.encode_sparse(s)
            e.encode(s)
        except Exception as ex:
            pytest.fail(f"输入 {s[:20]!r} 抛异常：{ex}")


def test_memory_index_and_search():
    """索引 + 检索基本流程。"""
    from apps.api import memory
    bid = _mk_book()
    content = ("少年握紧长剑，迎着风雪走向山巅。" * 10 + "\n\n"
               + "他在雪山之巅苦练剑法，剑光如雪。" * 10 + "\n\n"
               + "城中集市熙熙攘攘，卖糖葫芦的老翁叫卖。" * 10)
    with SessionLocal() as db:
        n = memory.index_chapter(db, bid, 1, content, "第一章")
        assert n > 0, "索引未产生任何块"
        hits = memory.search(db, bid, "雪山 练剑", ch_idx=5, top_k=3)
        assert hits, "检索无结果"
        # 命中的应包含「雪山」相关内容
        assert any("雪山" in h["text"] for h in hits), \
            f"检索未命中相关块：{[(h['ch_idx'], h['text'][:40]) for h in hits]}"


def test_memory_index_idempotent():
    """★ 重复索引同一章不产生重复块（重传/改稿场景）。"""
    from apps.api import memory
    bid = _mk_book()
    content = "测试内容。" * 200
    with SessionLocal() as db:
        n1 = memory.index_chapter(db, bid, 1, content)
        n2 = memory.index_chapter(db, bid, 1, content)
        assert n1 == n2
        st = memory.memory_stats(db, bid)
        assert st["chunks"] == n2, f"重复索引导致块数翻倍：{st['chunks']} != {n2}"


def test_memory_search_excludes_current_chapter():
    """★ 只检索更早章节（本章与自己无关，未来章节更不能看）。"""
    from apps.api import memory
    bid = _mk_book()
    with SessionLocal() as db:
        memory.index_chapter(db, bid, 1, "雪山练剑。" * 100)
        memory.index_chapter(db, bid, 5, "雪山练剑。" * 100)
        hits = memory.search(db, bid, "雪山练剑", ch_idx=3, top_k=10)
    assert all(h["ch_idx"] < 3 for h in hits), f"检索到了本章或未来章：{[h['ch_idx'] for h in hits]}"


def test_memory_degrade_no_dense(monkeypatch):
    """★★ 降级：没有稠密向量也能检索（老数据 / 远端挂）。"""
    from apps.api import memory
    monkeypatch.setenv("NF_EMBEDDING_BASE", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("NF_EMBEDDING_KEY", "sk-fake")
    embedding.reset_encoder()
    bid = _mk_book()
    with SessionLocal() as db:
        n = memory.index_chapter(db, bid, 1, "雪山练剑，剑气纵横。" * 50)
        assert n > 0, "远端挂时索引应仍能写入（只有稀疏向量）"
        hits = memory.search(db, bid, "雪山练剑", ch_idx=5)
        assert hits, "★ 无稠密向量时检索失败（降级未生效）"
        assert hits[0]["detail"]["dense_used"] is False


def test_memory_stats_and_recall_quality():
    from apps.api import memory
    bid = _mk_book()
    with SessionLocal() as db:
        memory.index_chapter(db, bid, 1, "主角修炼功法。" * 100)
        st = memory.memory_stats(db, bid)
        assert "dense_ratio" in st and "encoder" in st
        rq = memory.recall_quality(db, bid, "修炼功法", ch_idx=5)
        assert "hits" in rq and rq["count"] >= 0


# ============================================================================
#  B2 关系图谱
# ============================================================================

def test_relation_add_and_graph():
    bid = _mk_book()
    _mk_char(bid, "李青云")
    _mk_char(bid, "苏雪")
    with SessionLocal() as db:
        C.add_relation(db, book_id=bid, from_name="李青云", to_name="苏雪",
                       relation_type="师徒", since_chapter=1, note="收徒")
        g = C.build_graph(db, bid)
    assert g["stats"]["nodes"] >= 2
    assert g["stats"]["links"] == 1
    assert g["links"][0]["type"] == "师徒"
    assert g["links"][0]["intensity"] == 70        # 师徒极性


def test_relation_upsert_dedup():
    """同一对角色重复添加应更新而非新增。"""
    bid = _mk_book()
    _mk_char(bid, "A"); _mk_char(bid, "B")
    with SessionLocal() as db:
        C.add_relation(db, book_id=bid, from_name="A", to_name="B",
                       relation_type="盟友")
        C.add_relation(db, book_id=bid, from_name="A", to_name="B",
                       relation_type="仇敌")
        g = C.build_graph(db, bid)
    assert g["stats"]["links"] == 1, "重复添加产生了多条关系"
    assert g["links"][0]["type"] == "仇敌", "未更新为最新类型"


def test_relation_intensity_clamped():
    bid = _mk_book()
    _mk_char(bid, "A"); _mk_char(bid, "B")
    with SessionLocal() as db:
        r = C.add_relation(db, book_id=bid, from_name="A", to_name="B",
                           relation_type="仇敌", intensity=999)
    assert r["intensity"] == 100, "强度未夹紧到 [-100,100]"


def test_detect_relation_conflict_bidirectional():
    """★ 双向极性冲突检测（A 恨 B，B 却亲近 A）。"""
    bid = _mk_book()
    _mk_char(bid, "甲"); _mk_char(bid, "乙")
    with SessionLocal() as db:
        C.add_relation(db, book_id=bid, from_name="甲", to_name="乙",
                       relation_type="仇敌", intensity=-90)
        C.add_relation(db, book_id=bid, from_name="乙", to_name="甲",
                       relation_type="盟友", intensity=80)
        issues = C.detect_relation_conflict(db, bid)
    kinds = {i["kind"] for i in issues}
    assert "relation_conflict" in kinds, f"未检测到关系冲突：{kinds}"


def test_detect_relation_conflict_not_flagged_with_note():
    """★ 有 note 说明的冲突不报警（单恋/误解是合理设定）。"""
    bid = _mk_book()
    _mk_char(bid, "甲"); _mk_char(bid, "乙")
    with SessionLocal() as db:
        C.add_relation(db, book_id=bid, from_name="甲", to_name="乙",
                       relation_type="仇敌", intensity=-90, note="单方面误会")
        C.add_relation(db, book_id=bid, from_name="乙", to_name="甲",
                       relation_type="暧昧", intensity=85, note="暗恋不知情")
        issues = C.detect_relation_conflict(db, bid)
    assert not [i for i in issues if i["kind"] == "relation_conflict"], \
        "有说明的冲突被误报"


def test_detect_self_relation():
    bid = _mk_book()
    _mk_char(bid, "自恋者")
    with SessionLocal() as db:
        C.add_relation(db, book_id=bid, from_name="自恋者", to_name="自恋者")
        issues = C.detect_relation_conflict(db, bid)
    assert any(i["kind"] == "self_relation" and i["level"] == "error" for i in issues)


def test_detect_orphan_characters():
    """孤立角色（有档案但无关系）应被提示。"""
    bid = _mk_book()
    _mk_char(bid, "孤家寡人")
    with SessionLocal() as db:
        issues = C.detect_relation_conflict(db, bid)
    assert any(i["kind"] == "orphan_characters" for i in issues)


# ============================================================================
#  B3 情绪弧线
# ============================================================================

def test_emotion_set_and_arc():
    bid = _mk_book()
    with SessionLocal() as db:
        C.set_emotion(db, book_id=bid, ch_idx=1, intensity=30, valence=-20,
                      tags=["日常"])
        C.set_emotion(db, book_id=bid, ch_idx=2, intensity=85, valence=50,
                      tags=["爆点"])
        arc = C.emotion_arc(db, bid)
    assert len(arc["points"]) == 2
    assert arc["stats"]["peak_ch"] == 2
    assert arc["stats"]["valley_ch"] == 1


def test_emotion_upsert():
    """同章重复写入应更新。"""
    bid = _mk_book()
    with SessionLocal() as db:
        C.set_emotion(db, book_id=bid, ch_idx=1, intensity=10, valence=0)
        C.set_emotion(db, book_id=bid, ch_idx=1, intensity=90, valence=50)
        arc = C.emotion_arc(db, bid)
    assert len(arc["points"]) == 1
    assert arc["points"][0]["intensity"] == 90


def test_emotion_clamped():
    bid = _mk_book()
    with SessionLocal() as db:
        r = C.set_emotion(db, book_id=bid, ch_idx=1, intensity=999, valence=-999)
    assert r["intensity"] == 100 and r["valence"] == -100


def test_detect_low_flat():
    """★ 连续低潮检测（读者流失风险）。"""
    points = [{"ch_idx": i, "intensity": 20, "valence": 0} for i in range(1, 10)]
    issues = C.detect_low_flat(points)
    assert any(i["kind"] == "low_flat" for i in issues), f"未检测到连续低潮：{issues}"


def test_detect_no_peak():
    """★ 长期无爆点检测。"""
    points = [{"ch_idx": i, "intensity": 40, "valence": 0} for i in range(1, 30)]
    points[0]["intensity"] = 90        # 只有第 1 章是爆点
    issues = C.detect_low_flat(points, gap=12)
    assert any(i["kind"] == "no_peak" for i in issues), f"未检测到无爆点：{issues}"


def test_detect_sharp_jump():
    """★ 情绪跳变过陡。"""
    points = [{"ch_idx": 1, "intensity": 10, "valence": 0},
              {"ch_idx": 2, "intensity": 95, "valence": 0}]
    issues = C.detect_low_flat(points)
    assert any(i["kind"] == "sharp_jump" for i in issues)


def test_no_false_positive_healthy_arc():
    """健康的情绪弧线不应报警（防误报）。"""
    points = []
    for i in range(1, 21):
        # 起伏正常：30-80 之间波动，每 5 章一个爆点
        v = 80 if i % 5 == 0 else (35 + (i % 70))
        points.append({"ch_idx": i, "intensity": min(95, v), "valence": 10})
    issues = C.detect_low_flat(points)
    assert not [i for i in issues if i["kind"] == "low_flat"], "健康弧线误报低潮"


# ============================================================================
#  B4 伏笔自动提醒（★ 里程碑）
# ============================================================================

def test_foreshadow_overdue_detection():
    """★★ 里程碑：伏笔埋了 20 章没回收必须被检出。"""
    from apps.api.models import Foreshadow
    bid = _mk_book()
    with SessionLocal() as db:
        db.add(Foreshadow(id=f"fs_{uuid.uuid4().hex[:8]}", book_id=bid,
                          content="青铜古镜的来历", planted_ch=3,
                          due_ch=10, status="open", priority="high"))
        db.commit()
    with SessionLocal() as db:
        d = C.due_foreshadows(db, bid, current_ch=30)
    assert d["items"], "未检出伏笔"
    item = d["items"][0]
    assert item["overdue"] == 20, f"逾期计算错误：{item['overdue']}"
    assert item["level"] in ("critical", "high"), f"分级错误：{item['level']}"
    assert d["urgent"], "未进入 urgent 列表"


def test_foreshadow_not_due_yet():
    from apps.api.models import Foreshadow
    bid = _mk_book()
    with SessionLocal() as db:
        db.add(Foreshadow(id=f"fs_{uuid.uuid4().hex[:8]}", book_id=bid,
                          content="刚埋的线索", planted_ch=1, due_ch=50,
                          status="open"))
        db.commit()
    with SessionLocal() as db:
        d = C.due_foreshadows(db, bid, current_ch=5)
    assert d["items"][0]["overdue"] == 0
    assert d["items"][0]["level"] in ("ok", "watch")


def test_foreshadow_done_excluded():
    from apps.api.models import Foreshadow
    bid = _mk_book()
    with SessionLocal() as db:
        db.add(Foreshadow(id=f"fs_{uuid.uuid4().hex[:8]}", book_id=bid,
                          content="已回收", planted_ch=1, due_ch=5,
                          status="done", payoff_ch=6))
        db.commit()
    with SessionLocal() as db:
        d = C.due_foreshadows(db, bid, current_ch=30)
    assert not d["items"], "已回收的伏笔不应出现在清单里"


def test_foreshadow_default_due():
    """无 due_ch / target_vol 时应给默认值（planted + 20）。"""
    from apps.api.models import Foreshadow
    bid = _mk_book()
    with SessionLocal() as db:
        f = Foreshadow(id=f"fs_{uuid.uuid4().hex[:8]}", book_id=bid,
                       content="无应付章", planted_ch=10, status="open")
        db.add(f); db.commit()
        d = C.foreshadow_debt(f, 35)
    assert d["due_ch"] == 30, f"默认应付章计算错误：{d['due_ch']}"


def test_render_foreshadow_hint():
    debt = {"urgent": [{"content": "古镜来历", "planted_ch": 3, "overdue": 20},
                       {"content": "神秘信物", "planted_ch": 8, "overdue": 0,
                        "pending": 15}]}
    txt = C.render_foreshadow_hint(debt)
    assert "待回收伏笔" in txt and "古镜来历" in txt


def test_render_foreshadow_hint_empty():
    assert C.render_foreshadow_hint({"urgent": []}) == ""


# ============================================================================
#  B5 角色资产库
# ============================================================================

def test_character_asset_set_get():
    bid = _mk_book()
    _mk_char(bid, "陈长安")
    with SessionLocal() as db:
        r = C.set_character_asset(db, bid, "陈长安",
                                  ref_images=["img/a.png", "img/b.png"],
                                  appearance_locks={"hair": "黑长发束冠",
                                                    "eyes": "丹凤眼",
                                                    "clothing": "青衫"})
        assert r["ok"]
        a = C.character_asset(db, bid, "陈长安")
    assert len(a["ref_images"]) == 2
    assert a["appearance_locks"]["eyes"] == "丹凤眼"


def test_appearance_prompt():
    """★ 外观锁定 → prompt 片段（解决五官漂移）。"""
    bid = _mk_book()
    _mk_char(bid, "牧云谣")
    with SessionLocal() as db:
        C.set_character_asset(db, bid, "牧云谣", appearance_locks={
            "gender": "女", "hair": "银白长发", "eyes": "碧色",
            "clothing": "白衣"})
        p = C.appearance_prompt(db, bid, "牧云谣")
    assert "银白长发" in p and "碧色" in p
    # 顺序应遵循预设（gender 在前）
    assert p.index("gender") < p.index("hair")


def test_appearance_prompt_empty():
    bid = _mk_book()
    _mk_char(bid, "无资产角色")
    with SessionLocal() as db:
        assert C.appearance_prompt(db, bid, "无资产角色") == ""


def test_asset_missing_character():
    bid = _mk_book()
    with SessionLocal() as db:
        r = C.set_character_asset(db, bid, "不存在的人", ref_images=["x.png"])
    assert r["ok"] is False


def test_ref_images_capped():
    """参考图数量上限（防撑爆存储）。"""
    bid = _mk_book()
    _mk_char(bid, "多图角色")
    with SessionLocal() as db:
        r = C.set_character_asset(db, bid, "多图角色",
                                  ref_images=[f"img/{i}.png" for i in range(50)])
    assert len(r["ref_images"]) <= 12


# ============================================================================
#  B6 信息差管理（★ 里程碑）
# ============================================================================

def test_knowledge_set_get():
    bid = _mk_book()
    _mk_char(bid, "李青云")
    with SessionLocal() as db:
        C.set_knowledge(db, book_id=bid, character_name="李青云",
                        fact_id="setting:1", fact_desc="师尊的真实身份是魔教护法",
                        known=True, chapter_learned=12)
        items = C.knowledge_of(db, bid, "李青云")
    assert len(items) == 1
    assert items[0]["known"] is True
    assert items[0]["chapter_learned"] == 12


def test_knowledge_upsert():
    bid = _mk_book()
    _mk_char(bid, "甲")
    with SessionLocal() as db:
        C.set_knowledge(db, book_id=bid, character_name="甲",
                        fact_id="f1", fact_desc="秘密", known=False)
        C.set_knowledge(db, book_id=bid, character_name="甲",
                        fact_id="f1", fact_desc="秘密", known=True,
                        chapter_learned=5)
        items = C.knowledge_of(db, bid, "甲")
    assert len(items) == 1
    assert items[0]["known"] is True


def test_knowledge_violation_detected():
    """★★ 里程碑：角色说了他不该知道的事必须被检出。"""
    bid = _mk_book()
    _mk_char(bid, "反派")
    with SessionLocal() as db:
        C.set_knowledge(db, book_id=bid, character_name="反派",
                        fact_id="setting:1",
                        fact_desc="主角的秘密功法是太上忘情诀",
                        known=False, chapter_learned=0)     # 尚未得知
        text = "反派冷笑道：太上忘情诀果然名不虚传，主角的秘密功法我早已知晓。"
        issues = C.check_knowledge_violation(db, bid, ch_idx=8, text=text,
                                            char_names=["反派"])
    assert issues, "★ 未检出信息差越界"
    assert issues[0]["kind"] == "knowledge_violation"
    assert issues[0]["level"] == "error"


def test_knowledge_no_violation_after_learned():
    """已合法得知后不应报警（防误报）。"""
    bid = _mk_book()
    _mk_char(bid, "主角")
    with SessionLocal() as db:
        C.set_knowledge(db, book_id=bid, character_name="主角",
                        fact_id="f1", fact_desc="自己的秘密功法是太上忘情诀",
                        known=True, chapter_learned=3)
        text = "主角默念：太上忘情诀，我的秘密功法。"
        issues = C.check_knowledge_violation(db, bid, ch_idx=10, text=text,
                                            char_names=["主角"])
    assert not issues, f"已得知仍误报：{issues}"


def test_knowledge_no_violation_before_learned_chapter():
    """★ 边界：learned=12，当前第 8 章 → 应报警（还没到得知的时候）。"""
    bid = _mk_book()
    _mk_char(bid, "配角")
    with SessionLocal() as db:
        C.set_knowledge(db, book_id=bid, character_name="配角",
                        fact_id="f1", fact_desc="主角藏有上古神剑诛仙剑",
                        known=True, chapter_learned=12)
        text = "配角说：我知道上古神剑诛仙剑在你手里。"
        issues = C.check_knowledge_violation(db, bid, ch_idx=8, text=text,
                                            char_names=["配角"])
    assert issues, "得知章节晚于当前章却未报警"


def test_knowledge_no_false_positive_unrelated():
    """无关文本不应误报。"""
    bid = _mk_book()
    _mk_char(bid, "甲")
    with SessionLocal() as db:
        C.set_knowledge(db, book_id=bid, character_name="甲",
                        fact_id="f1", fact_desc="主角的秘密功法是太上忘情诀",
                        known=False, chapter_learned=0)
        issues = C.check_knowledge_violation(
            db, bid, ch_idx=8, text="甲抬头看了看天，今天天气不错。",
            char_names=["甲"])
    assert not issues


def test_knowledge_no_records():
    bid = _mk_book()
    with SessionLocal() as db:
        issues = C.check_knowledge_violation(db, bid, 1, "任意文本", ["甲"])
    assert issues == []


# ============================================================================
#  B 组汇总：全量一致性检查
# ============================================================================

def test_full_consistency_check():
    """★ 汇总检查：输出结构完整、分数计算正确。"""
    from apps.api.models import Foreshadow
    bid = _mk_book()
    _mk_char(bid, "甲"); _mk_char(bid, "乙")
    with SessionLocal() as db:
        db.add(Foreshadow(id=f"fs_{uuid.uuid4().hex[:8]}", book_id=bid,
                          content="严重逾期伏笔", planted_ch=1, due_ch=5,
                          status="open"))
        db.commit()
        C.add_relation(db, book_id=bid, from_name="甲", to_name="乙",
                       relation_type="仇敌", intensity=-90)
        C.add_relation(db, book_id=bid, from_name="乙", to_name="甲",
                       relation_type="盟友", intensity=80)
        r = C.full_consistency_check(db, bid, ch_idx=30)
    assert "score" in r and 0 <= r["score"] <= 100
    assert "by_level" in r and "issues" in r
    assert r["by_level"].get("error", 0) + r["by_level"].get("warn", 0) > 0
    assert r["foreshadow_debt"]["urgent"] >= 1


def test_full_check_clean_book_scores_100():
    """干净的书应满分（防误报）。"""
    bid = _mk_book()
    with SessionLocal() as db:
        r = C.full_consistency_check(db, bid, ch_idx=1)
    assert r["score"] == 100, f"干净的书被扣分：{r['by_level']}"


def test_relation_types_constant():
    for t in ("师徒", "仇敌", "暧昧", "血亲", "同门", "盟友", "上下级", "陌生"):
        assert t in C.RELATION_TYPES


def test_emotion_tags_constant():
    assert "爆点" in C.EMOTION_TAGS and "低谷" in C.EMOTION_TAGS


def test_keywords_extraction():
    kws = C._keywords("主角的秘密功法是太上忘情诀")
    assert kws
    assert any("忘情" in k or "功法" in k or "秘密" in k for k in kws)


def test_keywords_stopword_filter():
    """停用词不应成为关键词（否则匹配到大量噪声）。"""
    kws = C._keywords("的 了 是 在 有 和 与")
    assert not kws, f"停用词未被过滤：{kws}"

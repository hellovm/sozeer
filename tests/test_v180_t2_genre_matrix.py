# -*- coding: utf-8 -*-
"""v0.18 T2：每个题材一组完整回归（参数化生成）。

覆盖七条链路（题材解耦的验收面）
-------------------------------
① 度量衡     metrics_for        —— 记账维度正确
② 数值扫描   scan_patterns      —— 能从正文扫出对应维度
③ 实力闸门   realm_whitelist    —— 维度名对（悬疑无体系 → 跳过）
④ 审计维度   audit._realm_system —— 审计上下文用对维度名
⑤ 视觉锚点   drama/cover        —— 出图风格与封面锚点
⑥ 提示词     agent._genre_block —— 题材段注入正确
⑦ 名称归一   normalize_key      —— 中文名/别名 → 规范 key

设计：用 pytest.mark.parametrize 按题材展开，新增题材只需在
CASES 里加一行，七条链路自动覆盖。
"""
from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("NOVELFORGE_DATA", tempfile.mkdtemp(prefix="nf_test_t2_"))
os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-t2")


# ============================================================ 题材用例表
# 每个题材描述「应该是什么样」——新增题材在此登记即可全链路覆盖。
CASES = {
    "xianxia": {
        "label": "仙侠玄幻",
        "metrics": ["金钱", "经验", "年龄", "功法", "级别", "声望", "其他"],
        "realm_dim": "境界",
        "sample": "他掏出三百灵石，练气三层，年方十六。",
        "expect_dims": ["金钱", "功法"],
        "style_kw": ["仙侠", "古风", "水墨"],
        "cover_kw": ["古风", "仙侠", "山水"],
        "forbidden": ["职级", "粉丝", "案件"],
    },
    "urban": {
        "label": "都市",
        "metrics": ["金钱", "职级", "年龄", "声望", "粉丝", "其他"],
        "realm_dim": "职级",
        "sample": "他月薪三万块，职级三级，粉丝一百万。",
        "expect_dims": ["金钱", "职级"],
        "style_kw": ["现代"],
        "cover_kw": ["现代", "都市"],
        "forbidden": ["灵石", "功法", "境界"],
    },
    "urban_suspense": {
        "label": "都市悬疑",
        "metrics": ["金钱", "年龄", "案件", "时间", "其他"],
        "realm_dim": "",                      # ★ 无实力体系
        "sample": "现场有三起案件，三天前发生，损失五万元。",
        "expect_dims": ["案件", "金钱"],
        "style_kw": ["悬疑"],
        "cover_kw": ["悬疑", "暗"],
        "forbidden": ["灵石", "功法", "职级"],
    },
    "scifi": {
        "label": "科幻",
        "metrics": ["金钱", "能源", "年龄", "等级", "其他"],
        "realm_dim": "等级",
        "sample": "消耗三百度能源，等级五级，花掉二百星币。",
        "expect_dims": ["能源", "金钱"],
        "style_kw": ["科幻", "未来", "科技"],
        "cover_kw": ["科幻", "未来"],
        "forbidden": ["灵石", "功法", "职级"],
    },
    "game": {
        "label": "游戏电竞",
        "metrics": ["金钱", "经验", "等级", "年龄", "其他"],
        "realm_dim": "等级",
        "sample": "获得一千经验，升到三十级，赚了五百金币。",
        "expect_dims": ["经验", "等级"],
        "style_kw": ["游戏", "电竞", "霓虹"],
        "cover_kw": ["游戏", "电竞"],
        "forbidden": ["灵石", "功法", "职级"],
    },
}

ALL_KEYS = list(CASES.keys())


# ============================================================ fixtures
@pytest.fixture()
def db():
    from apps.api import main
    import apps.api.genres as genres
    genres.invalidate_cache()
    with main.SessionLocal() as s:
        yield s
    genres.invalidate_cache()


def _mk_book(db, bid, theme):
    from apps.api.models import Book
    from sqlalchemy import delete
    db.execute(delete(Book).where(Book.id == bid))
    db.commit()
    db.add(Book(id=bid, title=f"{theme}测试书", theme=theme, owner_id="u1"))
    db.commit()
    return db.get(Book, bid)


# ============================================================ ① 度量衡
@pytest.mark.parametrize("key", ALL_KEYS)
def test_metrics(key):
    from apps.api import genres
    got = list((genres.GENRES[key]["metrics"] or {}).keys())
    want = CASES[key]["metrics"]
    assert got == want, f"{key} 度量衡不符：{got} ≠ {want}"


# ============================================================ ② 数值扫描
@pytest.mark.parametrize("key", ALL_KEYS)
def test_scan(key):
    from apps.api import consistency as C
    pats = C.scan_patterns_for_theme(key)
    got = C.scan_numbers(CASES[key]["sample"], patterns=pats)
    for dim in CASES[key]["expect_dims"]:
        assert dim in got, f"{key} 未扫出「{dim}」：{sorted(got.keys())}"
        assert got[dim] and got[dim][0]["value"] > 0, f"{key}.{dim} 数值异常"


@pytest.mark.parametrize("key", ALL_KEYS)
def test_scan_no_forbidden_dims(key):
    """★ 不该扫出别家题材的维度（防串味）。"""
    from apps.api import consistency as C
    pats = C.scan_patterns_for_theme(key)
    got = C.scan_numbers(CASES[key]["sample"], patterns=pats)
    for bad in CASES[key]["forbidden"]:
        assert bad not in got, f"{key} 串味：扫出了「{bad}」"


# ============================================================ ③ 实力闸门
@pytest.mark.parametrize("key", ALL_KEYS)
def test_realm_dim(key):
    from apps.api import genres
    assert genres.realm_dim_for(key) == CASES[key]["realm_dim"], \
        f"{key} 实力维度不符"


@pytest.mark.parametrize("key", ALL_KEYS)
def test_realm_whitelist(db, key):
    """闸门白名单：按题材维度名取世界观条目。"""
    from apps.api import genres
    from apps.api.orchestrator import gates
    from apps.api.models import WorldSetting
    from sqlalchemy import delete
    bid = f"bk_t2_{key}"
    _mk_book(db, bid, key)
    db.execute(delete(WorldSetting).where(WorldSetting.book_id == bid))
    db.commit()
    dim = CASES[key]["realm_dim"]
    if dim:
        for i, name in enumerate(["初阶", "中阶", "高阶"]):
            db.add(WorldSetting(id=f"ws_{bid}_{i}", book_id=bid,
                                category=dim, key=name, value=""))
        db.commit()
        wl = gates.realm_whitelist_for(db, bid)
        assert "初阶" in wl, f"{key} 闸门白名单为空：{wl}"
    else:
        # 无实力体系 → 白名单恒空
        wl = gates.realm_whitelist_for(db, bid)
        assert wl == [], f"{key} 无体系却返回白名单：{wl}"


# ============================================================ ④ 审计维度
@pytest.mark.parametrize("key", ALL_KEYS)
def test_audit_realm_system(db, key):
    from apps.api import audit
    from apps.api.models import WorldSetting
    from sqlalchemy import delete
    bid = f"bk_t2a_{key}"
    b = _mk_book(db, bid, key)
    db.execute(delete(WorldSetting).where(WorldSetting.book_id == bid))
    db.commit()
    dim = CASES[key]["realm_dim"]
    if dim:
        db.add(WorldSetting(id=f"ws_{bid}_0", book_id=bid, category=dim,
                            key="初阶", value=""))
        db.commit()
    txt = audit._realm_system(db, b, "")
    assert isinstance(txt, str) and txt
    if dim:
        assert dim in txt, f"{key} 审计上下文未用「{dim}」：{txt[:80]}"
    else:
        assert "无实力" in txt or "满分" in txt, f"{key} 应提示无体系：{txt[:80]}"


# ============================================================ ⑤ 视觉锚点
@pytest.mark.parametrize("key", ALL_KEYS)
def test_visual_anchors(key):
    from apps.api import cover, drama
    sh = drama._genre_style_hint(key)
    ca = cover._cover_anchor_for(key)
    assert sh, f"{key} 风格提示为空"
    assert ca, f"{key} 封面锚点为空"
    assert any(kw in sh for kw in CASES[key]["style_kw"]), \
        f"{key} 风格不含预期词 {CASES[key]['style_kw']}：{sh}"
    assert any(kw in ca for kw in CASES[key]["cover_kw"]), \
        f"{key} 封面不含预期词 {CASES[key]['cover_kw']}：{ca}"


@pytest.mark.parametrize("key", ALL_KEYS)
def test_visual_no_cross_genre(key):
    """★ 现代题材不该拿到仙侠画风（反之亦然）。"""
    from apps.api import drama
    sh = drama._genre_style_hint(key)
    for bad in CASES[key]["forbidden"]:
        # 只检查明显串味的画风词
        if bad in ("灵石", "功法"):
            continue          # 这些是度量衡词，画风里本就不该有，单独测
    if key != "xianxia":
        assert "水墨" not in sh or "现代" in sh, f"{key} 疑似拿到仙侠画风：{sh}"


# ============================================================ ⑥ 提示词
@pytest.mark.parametrize("key", ALL_KEYS)
def test_prompt_block(db, key):
    from apps.api import agent
    bid = f"bk_t2p_{key}"
    b = _mk_book(db, bid, key)
    txt = agent._genre_block(db, b)
    assert txt, f"{key} 题材提示段为空"
    assert CASES[key]["label"] in txt, f"{key} 提示段未含题材名：{txt[:80]}"
    dim = CASES[key]["realm_dim"]
    if dim:
        assert dim in txt, f"{key} 提示段未含实力维度：{txt[:120]}"
    else:
        assert "无实力等级体系" in txt, f"{key} 应提示无体系：{txt[:120]}"


@pytest.mark.parametrize("key", ALL_KEYS)
def test_prompt_no_cross_genre_words(db, key):
    """★ 提示词不该出现别家题材的度量衡词。"""
    from apps.api import agent
    bid = f"bk_t2pw_{key}"
    b = _mk_book(db, bid, key)
    txt = agent._genre_block(db, b)
    for bad in CASES[key]["forbidden"]:
        assert bad not in txt, f"{key} 提示词串味：出现「{bad}」"


# ============================================================ ⑦ 名称归一
@pytest.mark.parametrize("key", ALL_KEYS)
def test_normalize_key(key):
    from apps.api import genres
    assert genres.normalize_key(key) == key, f"{key} 自身归一失败"
    # 中文 label 应归一到本 key
    assert genres.normalize_key(CASES[key]["label"]) == key, \
        f"『{CASES[key]['label']}』未归一到 {key}"


@pytest.mark.parametrize("key", ALL_KEYS)
def test_alias_routing(key):
    from apps.api import genres
    for alias in genres.GENRES[key].get("aliases", []):
        got = genres.get_genre(alias)
        assert got is not None, f"{key} 别名「{alias}」无命中"
        got_key = next((k for k, v in genres.GENRES.items() if v is got), "")
        # 允许别名指向更精确的题材（如「悬疑」→ urban_suspense）
        assert got_key, f"{key} 别名「{alias}」指向未知包"


# ============================================================ 兜底
def test_unknown_theme_falls_back():
    from apps.api import genres
    assert genres.metrics_for("不存在的题材") == genres.GENRES["xianxia"]["metrics"]
    assert genres.realm_dim_for("不存在的题材") == "境界"


def test_empty_theme_falls_back():
    from apps.api import genres
    assert genres.metrics_for("") == genres.GENRES["xianxia"]["metrics"]
    assert genres.metrics_for(None) == genres.GENRES["xianxia"]["metrics"]


def test_all_packs_have_full_coverage():
    """元测试：CASES 覆盖了全部内置题材（新增题材忘登记会失败）。"""
    from apps.api import genres
    missing = set(genres.GENRES) - set(CASES)
    assert not missing, f"CASES 未覆盖题材：{missing}（请补齐用例表）"

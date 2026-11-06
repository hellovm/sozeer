# -*- coding: utf-8 -*-
"""v0.15 G1–G3 题材包地基回归测试。

覆盖：
  G1 genres.py 加载器：get_genre 未命中返 None、别名子串匹配、兜底、查询接口
  G2 xianxia 包零回归铁证：内容与原 consistency.METRICS **逐字节一致**
  G3 GenrePack 表 + DB 覆盖层 + seed_builtin 幂等 + 缓存失效

设计原则验证：
  * 未命中返 None（opt-in），调用方走兜底 → 老书零影响
  * DB 为覆盖层、代码为默认 → DB 挂了也能跑
  * 硬规则（metrics/scan_patterns/realm_dim）单值；软措辞（style/cover/prompt）可叠加
"""
import os
import tempfile

import pytest

os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-genres")


# ==================================================== G1：加载器基础行为
def test_get_genre_none_input():
    """空输入必须返 None（不启用），不抛异常。"""
    from apps.api import genres
    assert genres.get_genre(None) is None
    assert genres.get_genre("") is None


def test_get_genre_unknown_returns_none():
    """未命中任何题材必须返 None —— 这是 opt-in 的核心契约。"""
    from apps.api import genres
    assert genres.get_genre("完全不存在的题材xyz") is None


def test_get_genre_exact_key():
    from apps.api import genres
    g = genres.get_genre("xianxia")
    assert g is not None
    assert g["label"] == "仙侠玄幻"
    g2 = genres.get_genre("urban")
    assert g2["label"] == "都市"


def test_get_genre_alias_substring_match():
    """实测 genre 是自由文本，必须子串匹配。

    真实数据：'系统流 + 轻松搞笑 + 修仙 + 反套路' 应命中 xianxia。
    """
    from apps.api import genres
    g = genres.get_genre("系统流 + 轻松搞笑 + 修仙 + 反套路")
    assert g is not None, "复合串中的'修仙'必须命中 xianxia"
    assert g["label"] == "仙侠玄幻"


def test_get_genre_longest_alias_wins():
    """'都市悬疑' 必须命中 urban_suspense，不能被 '都市' 抢先。"""
    from apps.api import genres
    g = genres.get_genre("都市悬疑")
    assert g is not None
    assert g["label"] == "都市悬疑", f"应命中 urban_suspense，实际 {g['label']}"


def test_get_genre_case_insensitive():
    from apps.api import genres
    assert genres.get_genre("XIANXIA") is not None
    assert genres.get_genre("Urban") is not None


def test_get_genre_by_key_strict():
    """by_key 不做别名匹配（内部调用语义）。"""
    from apps.api import genres
    assert genres.get_genre_by_key("xianxia") is not None
    assert genres.get_genre_by_key("修仙") is None, "by_key 不该做别名匹配"
    assert genres.get_genre_by_key(None) is None


# ==================================================== G2：零回归铁证
def test_xianxia_metrics_byte_identical():
    """★ 零回归铁证：xianxia 包的 metrics 必须与原 consistency.METRICS 逐字节一致。"""
    from apps.api import genres
    original = {
        "金钱": {"units": ["枚", "块", "两", "文", "灵石", "金币", "银两", "万"], "cumulative": True},
        "经验": {"units": ["点", "经验"], "cumulative": True},
        "年龄": {"units": ["岁", "年"], "cumulative": True},
        "功法": {"units": ["层", "重", "阶", "品"], "cumulative": True},
        "级别": {"units": ["级", "阶", "段", "星"], "cumulative": True},
        "声望": {"units": ["点"], "cumulative": True},
        "其他": {"units": [], "cumulative": True},
    }
    got = genres.GENRES["xianxia"]["metrics"]
    assert got == original, "xianxia 度量衡与现状不一致 → 违反零回归承诺"


def test_metrics_for_default_fallback():
    """未命中 / 空值时回退仙侠原表（老书行为不变）。"""
    from apps.api import genres
    xianxia_metrics = genres.GENRES["xianxia"]["metrics"]
    assert genres.metrics_for(None) == xianxia_metrics
    assert genres.metrics_for("") == xianxia_metrics
    assert genres.metrics_for("不存在的题材") == xianxia_metrics


def test_metrics_for_urban():
    """都市题材取到自己的度量衡（金钱/职级/粉丝），且不含仙侠的'功法'。"""
    from apps.api import genres
    m = genres.metrics_for("urban")
    assert "金钱" in m and "职级" in m
    assert "功法" not in m, "都市文不该有'功法'度量衡"
    assert "元" in m["金钱"]["units"]
    assert "灵石" not in m["金钱"]["units"], "都市文的金钱单位不该含灵石"


def test_scan_patterns_for_returns_templates():
    """扫描模式返回 (维度, 正则模板) 且模板含 {NUM} 占位。"""
    from apps.api import genres
    pats = genres.scan_patterns_for("urban")
    assert pats, "都市必须有扫描模式"
    for dim, tmpl in pats:
        assert "{NUM}" in tmpl, f"{dim} 的模式缺 {{NUM}} 占位：{tmpl}"


def test_scan_patterns_xianxia_has_realm_units():
    from apps.api import genres
    pats = dict(genres.scan_patterns_for("xianxia"))
    assert "金钱" in pats
    assert "灵石" in pats["金钱"], "仙侠金钱模式必须含灵石"


def test_realm_dim_for():
    """实力体系维度名：仙侠=境界，都市=职级，悬疑=空（闸门跳过）。"""
    from apps.api import genres
    assert genres.realm_dim_for("xianxia") == "境界"
    assert genres.realm_dim_for("urban") == "职级"
    assert genres.realm_dim_for("urban_suspense") == "", "悬疑无实力体系，必须是空串"
    # 未命中回退 '境界'（保持老书行为）
    assert genres.realm_dim_for(None) == "境界"
    assert genres.realm_dim_for("未知题材") == "境界"


def test_realm_stops_nonempty_for_realm_genres():
    from apps.api import genres
    assert genres.realm_stops_for("xianxia"), "仙侠需防误报词表"
    assert genres.realm_stops_for("urban"), "都市需防误报词表"


# ==================================================== G1：软措辞可叠加
def test_style_hints_single_theme():
    from apps.api import genres
    hints = genres.style_hints_for("urban")
    assert hints and "现代" in hints[0]


def test_style_hints_tags_stack():
    """★ 多标签叠加：urban + 悬疑 → 同时拿到两组风格。"""
    from apps.api import genres
    hints = genres.style_hints_for("urban", ["urban_suspense"])
    assert len(hints) == 2, f"主题材+标签应得 2 组风格，实际 {hints}"
    assert any("现代" in h for h in hints)
    assert any("悬疑" in h for h in hints)


def test_style_hints_dedup():
    """重复标签不产生重复措辞。"""
    from apps.api import genres
    hints = genres.style_hints_for("urban", ["urban", "urban"])
    assert len(hints) == 1, "重复标签必须去重"


def test_cover_anchors_stack():
    from apps.api import genres
    a = genres.cover_anchors_for("urban", ["urban_suspense"])
    assert len(a) == 2
    assert all(isinstance(x, str) and x for x in a)


def test_prompt_hints():
    from apps.api import genres
    h = genres.prompt_hints_for("urban")
    assert h and "现实规则" in h[0]


def test_hints_empty_input():
    from apps.api import genres
    assert genres.style_hints_for(None, None) == []
    assert genres.cover_anchors_for(None, []) == []
    assert genres.prompt_hints_for() == []


def test_hints_ignore_unknown():
    from apps.api import genres
    assert genres.style_hints_for("未知", ["也不存在"]) == []


# ==================================================== G1：catalog
def test_catalog_lists_all_builtin():
    from apps.api import genres
    cat = genres.catalog()
    keys = {c["key"] for c in cat}
    for k in ("xianxia", "urban", "urban_suspense", "scifi", "game"):
        assert k in keys, f"catalog 缺 {k}"
    # 每项必须有 UI 需要的最小字段
    for c in cat:
        assert c["label"] and isinstance(c["aliases"], list)
        assert isinstance(c["has_realm"], bool)


# ==================================================== G3：DB 覆盖层
@pytest.fixture()
def db():
    """用并发测试同源的 SessionLocal（conftest 已定死隔离目录，见其说明）。

    关键：必须用 apps.api.main 引用链上的 SessionLocal，而不是 db.py 的模块属性
    —— 与 test_v147_accept_consume 同一套路，保证 fixture 与生产代码同库。
    进/出都清缓存，避免测试间相互污染（缓存是模块级全局）。
    """
    from apps.api import main
    import apps.api.genres as genres
    genres.invalidate_cache()
    with main.SessionLocal() as s:
        yield s
    genres.invalidate_cache()


def _restore(db, *keys):
    """测试后还原内置包状态（enabled=1、label 复位），保证测试可重复运行。"""
    from apps.api import genres
    from apps.api.models import GenrePack
    for k in keys:
        r = db.get(GenrePack, k)
        if r:
            r.enabled = 1
            src = genres.GENRES.get(k)
            if src:
                r.label = src.get("label", r.label)
    db.commit()
    genres.invalidate_cache()


def test_genre_pack_table_exists(db):
    from apps.api.models import GenrePack
    from sqlalchemy import select
    # 表可查询（空表不报错）
    rows = db.scalars(select(GenrePack)).all()
    assert isinstance(rows, list)


def test_seed_builtin_idempotent(db):
    """种子写入幂等：跑两次不产生重复、不覆盖已改。

    注意：导入 apps.api.main 会触发启动期种子（main.py 顶部已 seed_builtin），
    所以首次调用可能已是 0（已存在）。本测试不断言"首次必写入 N 个"，
    而是断言"种子后内置包齐备" + "二次调用为 0"。
    """
    from apps.api import genres
    from apps.api.models import GenrePack
    from sqlalchemy import select, func

    genres.seed_builtin(db)                      # 幂等：已存在则跳过
    cnt1 = db.scalar(select(func.count(GenrePack.key)))

    n2 = genres.seed_builtin(db)
    assert n2 == 0, "二次种子必须为 0（已存在则跳过）"

    cnt2 = db.scalar(select(func.count(GenrePack.key)))
    assert cnt1 == cnt2, "重复种子不该改变记录数"

    # 内置包必须齐备
    keys = {r for (r,) in db.execute(select(GenrePack.key)).all()}
    for k in genres.GENRES:
        assert k in keys, f"内置包 {k} 未写入 DB"


def test_db_override_takes_effect(db):
    """★ DB 覆盖：改 label 后 effective_genres 读到新值。"""
    from apps.api import genres
    from apps.api.models import GenrePack

    genres.seed_builtin(db)
    genres.invalidate_cache()

    # 覆盖内置包的 label
    row = db.get(GenrePack, "urban")
    row.label = "都市（自定义）"
    db.commit()
    genres.invalidate_cache()

    g = genres.get_genre("urban", db)
    assert g["label"] == "都市（自定义）", "DB 覆盖未生效"

    _restore(db, "urban")
    assert genres.get_genre("urban", db)["label"] == "都市", "还原失败"


def test_db_can_add_custom_genre(db):
    """★ 新增自定义题材（不改代码，只加一条记录）。"""
    from apps.api import genres
    from apps.api.models import GenrePack, jd

    genres.seed_builtin(db)
    # 幂等清理：多次运行不冲突
    old = db.get(GenrePack, "republican_spy")
    if old:
        db.delete(old)
        db.commit()
    db.add(GenrePack(
        key="republican_spy", label="民国谍战",
        aliases=jd(["民国", "谍战", "特工"]),
        config_json=jd({
            "metrics": {"金钱": {"units": ["大洋", "法币"], "cumulative": True}},
            "scan_patterns": [("金钱", r"{NUM}\s*(?:块大洋|法币)")],
            "realm_dim": "军衔",
            "realm_stops": [],
            "style_hint": "民国复古质感，暖黄色调，老上海街景",
            "cover_anchor": "民国街景，复古色调，旗袍与长衫",
            "prompt_hint": "谍战核心是身份伪装与信息博弈。",
        }),
        builtin=0, enabled=1, sort_order=999,
    ))
    db.commit()
    genres.invalidate_cache()

    g = genres.get_genre("民国谍战", db)
    assert g is not None and g["label"] == "民国谍战"
    assert genres.realm_dim_for("republican_spy", db) == "军衔"
    assert "大洋" in genres.metrics_for("republican_spy", db)["金钱"]["units"]


def test_db_disabled_pack_invisible(db):
    """★ 停用语义：DB 里 enabled=0 必须能**压过内置包**（配置中心停用按钮要生效）。"""
    from apps.api import genres
    from apps.api.models import GenrePack

    genres.seed_builtin(db)
    row = db.get(GenrePack, "scifi")
    row.enabled = 0
    db.commit()
    genres.invalidate_cache()

    assert genres.get_genre_by_key("scifi", db) is None, "停用后不该可见"
    assert "scifi" not in genres.effective_genres(db), "停用项必须从合并表剔除"
    # 其他题材不受影响
    assert genres.get_genre_by_key("urban", db) is not None

    _restore(db, "scifi")


def test_db_none_falls_back_to_builtin():
    """db=None 时只读内置包（零依赖启动路径）。"""
    from apps.api import genres
    table = genres.effective_genres(None)
    assert "xianxia" in table and "urban" in table
    assert table is not genres.GENRES, "应返回副本，避免外部改动污染内置包"


def test_invalidate_cache_clears():
    from apps.api import genres
    genres._DB_CACHE = {"fake": {}}
    genres._DB_LOADED = True
    genres.invalidate_cache()
    assert genres._DB_CACHE is None
    assert genres._DB_LOADED is False


# ==================================================== 兜底健壮性（风险 7/8）
def test_broken_config_json_does_not_crash(db):
    """★ 风险 7：DB 里 config_json 损坏时，静默跳过而非崩溃。"""
    from apps.api import genres
    from apps.api.models import GenrePack

    genres.seed_builtin(db)
    old = db.get(GenrePack, "broken")
    if old:
        db.delete(old)
        db.commit()
    db.add(GenrePack(key="broken", label="坏包", aliases="[]",
                     config_json="{ this is not json", builtin=0, enabled=1))
    db.commit()
    genres.invalidate_cache()

    # 不该抛异常
    table = genres.effective_genres(db)
    assert "xianxia" in table, "坏包不该影响内置包可用性"
    assert "broken" not in table, "坏包应被跳过"

    # 清理
    row = db.get(GenrePack, "broken")
    if row:
        db.delete(row)
        db.commit()
    genres.invalidate_cache()


def test_unknown_theme_falls_back_no_crash():
    """★ 风险 8：任意未知 theme 都不抛异常、落兜底。"""
    from apps.api import genres
    for bad in ["", None, "🙂", "a" * 500, "'; DROP TABLE books; --"]:
        assert genres.metrics_for(bad) == genres.GENRES["xianxia"]["metrics"]
        assert genres.scan_patterns_for(bad) == genres.GENRES["xianxia"]["scan_patterns"]


# ==================================================== 模块契约
def test_all_exports_present():
    from apps.api import genres
    for name in genres.__all__:
        assert hasattr(genres, name), f"__all__ 声明了 {name} 但未定义"


def test_genres_have_required_fields():
    """每个内置包必须有 label/aliases/一个体系字段（realm_dim 可空）。"""
    from apps.api import genres
    for k, v in genres.GENRES.items():
        assert v.get("label"), f"{k} 缺 label"
        assert isinstance(v.get("aliases"), list) and v["aliases"], f"{k} 缺 aliases"
        assert "realm_dim" in v, f"{k} 缺 realm_dim（可为空串）"

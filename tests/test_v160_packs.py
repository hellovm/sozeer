# -*- coding: utf-8 -*-
"""v0.16 G11 回归：补齐 urban_suspense / scifi / game 三包。

核心验收（计划书 §五 v0.16 G11）：
  * 5 个题材包结构完整（label/aliases/metrics/scan_patterns/realm_*/视觉/提示词）
  * 每包独立记账、独立扫描、独立视觉、独立提示词（互不串味）
  * 悬疑无实力体系 → 闸门自动跳过
"""
import os

import pytest

os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-g11")

ALL_PACKS = ["xianxia", "urban", "urban_suspense", "scifi", "game"]
REQUIRED_FIELDS = ["label", "aliases", "metrics", "scan_patterns",
                   "realm_dim", "realm_stops", "style_hint", "cover_anchor", "prompt_hint"]


# ==================================================== 结构完整性
def test_all_five_packs_exist():
    from apps.api import genres
    for k in ALL_PACKS:
        assert k in genres.GENRES, f"缺题材包 {k}"


def test_all_packs_have_required_fields():
    from apps.api import genres
    for k in ALL_PACKS:
        g = genres.GENRES[k]
        missing = [f for f in REQUIRED_FIELDS if f not in g]
        assert not missing, f"{k} 缺字段：{missing}"


def test_all_packs_have_nonempty_visual():
    """每包必须有风格 + 封面锚点（否则出图会退化）。"""
    from apps.api import genres
    for k in ALL_PACKS:
        g = genres.GENRES[k]
        assert g["style_hint"].strip(), f"{k} 缺 style_hint"
        assert g["cover_anchor"].strip(), f"{k} 缺 cover_anchor"
        assert g["prompt_hint"].strip(), f"{k} 缺 prompt_hint"


def test_all_packs_have_metrics_and_patterns():
    from apps.api import genres
    for k in ALL_PACKS:
        g = genres.GENRES[k]
        assert g["metrics"], f"{k} 度量衡为空"
        assert g["scan_patterns"], f"{k} 扫描模式为空"


def test_scan_patterns_align_with_metrics():
    """扫描模式的维度必须是度量衡维度的子集（否则记账维度对不上）。"""
    from apps.api import genres
    for k in ALL_PACKS:
        g = genres.GENRES[k]
        metrics = set(g["metrics"].keys())
        dims = {d for d, _ in g["scan_patterns"]}
        extra = dims - metrics
        assert not extra, f"{k} 扫描维度 {extra} 不在度量衡 {metrics} 内"


# ==================================================== 独立性（互不串味）
def test_metrics_are_distinct():
    """各包度量衡必须有区分度（不能全一样，否则解耦没意义）。"""
    from apps.api import genres
    seen = {}
    for k in ALL_PACKS:
        key = tuple(sorted(genres.GENRES[k]["metrics"].keys()))
        seen.setdefault(key, []).append(k)
    # 至少 4 组不同（xianxia 独有功法；urban 独有职级/粉丝；悬疑有案件/时间；scifi 有能源）
    assert len(seen) >= 4, f"度量衡区分度不足，仅 {len(seen)} 组：{seen}"


def test_no_xianxia_words_in_modern_packs():
    """★ 现代题材不该出现仙侠词（灵石/功法），否则解耦失败。"""
    from apps.api import genres
    xianxia_words = ["灵石", "功法", "丹药", "元婴", "筑基"]
    for k in ["urban", "urban_suspense", "scifi", "game"]:
        g = genres.GENRES[k]
        blob = str(g["metrics"]) + str(g["scan_patterns"]) + g["style_hint"] + g["prompt_hint"]
        for w in xianxia_words:
            assert w not in blob, f"{k} 出现仙侠词「{w}」"


def test_realm_dim_values():
    """实力体系维度：仙侠=境界，都市=职级，悬疑=空（无体系）。"""
    from apps.api import genres
    assert genres.GENRES["xianxia"]["realm_dim"] == "境界"
    assert genres.GENRES["urban"]["realm_dim"] == "职级"
    assert genres.GENRES["urban_suspense"]["realm_dim"] == "", "悬疑必须无实力体系"
    assert genres.GENRES["scifi"]["realm_dim"] == "等级"
    assert genres.GENRES["game"]["realm_dim"] == "等级"


# ==================================================== 扫描器行为
def test_urban_suspense_scanner():
    from apps.api import consistency as C
    pats = C.scan_patterns_for_theme("urban_suspense")
    got = C.scan_numbers("现场有3起案件，三天前发生，损失5万元", patterns=pats)
    assert "案件" in got, f"悬疑未扫出案件：{got}"
    assert "金钱" in got
    assert any(h["value"] == 3 for h in got["案件"])


def test_scifi_scanner():
    from apps.api import consistency as C
    pats = C.scan_patterns_for_theme("scifi")
    got = C.scan_numbers("消耗300度能源，花掉200星币", patterns=pats)
    assert "能源" in got, f"科幻未扫出能源：{got}"
    assert "金钱" in got


def test_game_scanner():
    from apps.api import consistency as C
    pats = C.scan_patterns_for_theme("game")
    got = C.scan_numbers("获得1000经验，升到30级，赚了500金币", patterns=pats)
    assert "经验" in got, f"游戏未扫出经验：{got}"
    assert "等级" in got


def test_compile_handles_compiled_patterns():
    """★ 健壮性：已编译的 Pattern 透传，不崩（v0.16 修复的 TypeError）。"""
    import re
    from apps.api import consistency as C
    compiled = re.compile(r"(\d+)\s*级")
    out = C._compile_scan_patterns([("等级", compiled)])
    assert len(out) == 1 and out[0][1] is compiled


def test_compile_handles_dirty_input():
    """★ 脏数据不崩：None / 空 / 非元组 / 坏元组。"""
    from apps.api import consistency as C
    for bad in [None, [], [None], [("x",)], ["notatuple"], [("d", None)], [("d", "")],
                [("d", "(unclosed")], [(None, "x")]]:
        out = C._compile_scan_patterns(bad)
        assert isinstance(out, list), f"入参 {bad!r} 导致异常"


# ==================================================== 视觉锚点
def test_each_pack_has_distinct_style():
    from apps.api import genres
    styles = {k: genres.GENRES[k]["style_hint"] for k in ALL_PACKS}
    uniq = set(styles.values())
    assert len(uniq) == len(ALL_PACKS), f"风格锚点有重复：{styles}"


def test_visual_hint_via_api():
    from apps.api import drama, cover
    for k in ALL_PACKS:
        sh = drama._genre_style_hint(k)
        ca = cover._cover_anchor_for(k)
        assert sh, f"{k} 风格提示为空"
        assert ca, f"{k} 封面锚点为空"


# ==================================================== 闸门
def test_suspense_gate_skipped():
    """悬疑无实力体系 → realm_dim 空 → 闸门白名单恒空。"""
    from apps.api import genres
    from apps.api.orchestrator import gates
    assert genres.realm_dim_for("urban_suspense") == ""
    rep = gates.GateReport()
    gates.check_realms("他陷入了绝境，这是他人生的困境。", [], rep)
    assert not rep.issues


def test_scifi_gate_uses_dengji():
    from apps.api import genres
    assert genres.realm_dim_for("scifi") == "等级"


# ==================================================== catalog（UI 用）
def test_catalog_lists_five():
    from apps.api import genres
    cat = genres.catalog()
    keys = {c["key"] for c in cat}
    for k in ALL_PACKS:
        assert k in keys, f"catalog 缺 {k}"
    assert len(cat) >= 5
    for c in cat:
        assert c["label"] and c["style_hint"], f"{c['key']} catalog 信息不全"


# ==================================================== 别名匹配
def test_alias_routing_all_packs():
    """各包的典型中文名都能路由到正确包。"""
    from apps.api import genres
    cases = {
        "仙侠": "xianxia", "玄幻": "xianxia", "修仙": "xianxia",
        "都市": "urban", "职场": "urban", "商战": "urban",
        "悬疑": "urban_suspense", "推理": "urban_suspense", "刑侦": "urban_suspense",
        "科幻": "scifi", "星际": "scifi", "机甲": "scifi",
        "游戏": "game", "电竞": "game", "网游": "game",
    }
    for name, want in cases.items():
        g = genres.get_genre(name)
        assert g is not None, f"「{name}」未命中任何题材"
        got = next(k for k, v in genres.GENRES.items() if v is g)
        assert got == want, f"「{name}」应命中 {want}，实际 {got}"

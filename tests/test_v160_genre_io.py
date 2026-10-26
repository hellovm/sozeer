# -*- coding: utf-8 -*-
"""v0.16 G12 回归：题材包导入/导出（JSON 互转）。

核心验收（计划书 §五 v0.16 G12）：
  * 导出全部（含内置）/ 只导自定义
  * 导入三种冲突策略：skip / update / rename
  * 脏数据容错：坏 key / 坏 config / 非对象 → 记 errors 但不中断整批
  * 越界 on_conflict 被拒
"""
import os

import pytest

os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-g12")


@pytest.fixture()
def ctx():
    from apps.api import main
    import apps.api.genres as genres
    genres.invalidate_cache()
    with main.SessionLocal() as s:
        from apps.api.models import User
        u = s.get(User, "u_g12")
        if not u:
            u = User(id="u_g12", username="g12admin", role="admin", is_active=1)
            s.add(u)
            s.commit()
        yield main, s, u, genres
    genres.invalidate_cache()


def _cleanup(s, *keys):
    from apps.api.models import GenrePack
    for k in keys:
        r = s.get(GenrePack, k)
        if r:
            s.delete(r)
    s.commit()


# ==================================================== 导出
def test_export_all_includes_builtin(ctx):
    main, s, u, genres = ctx
    d = main.export_genres(include_builtin=1, db=s, _=u)
    assert d["schema"] == genres.EXPORT_SCHEMA
    keys = {p["key"] for p in d["packs"]}
    for k in genres.GENRES:
        assert k in keys, f"导出缺内置包 {k}"
    assert "exported_at" in d


def test_export_custom_only(ctx):
    main, s, u, genres = ctx
    d = main.export_genres(include_builtin=0, db=s, _=u)
    # 内置包不应出现
    for p in d["packs"]:
        assert p["key"] not in genres.GENRES, f"内置包 {p['key']} 不该出现在自定义导出里"


def test_export_pack_structure(ctx):
    main, s, u, genres = ctx
    d = main.export_genres(db=s, _=u)
    for p in d["packs"]:
        assert set(p.keys()) >= {"key", "label", "aliases", "config", "builtin"}
        assert isinstance(p["aliases"], list)
        assert isinstance(p["config"], dict)


# ==================================================== 导入：三种冲突策略
def test_import_skip(ctx):
    """★ skip：已存在则跳过（最安全，默认）。"""
    main, s, u, genres = ctx
    exp = main.export_genres(db=s, _=u)
    r = main.import_genres(exp, on_conflict="skip", db=s, _=u)
    assert r["ok"]
    assert r["created"] == 0 and r["updated"] == 0
    assert r["skipped"] == len(exp["packs"])


def test_import_update(ctx):
    main, s, u, genres = ctx
    from apps.api.models import GenrePack
    _cleanup(s, "upd_g12")
    genres.seed_builtin(s)
    r = main.import_genres({"packs": [
        {"key": "urban", "label": "都市（导入版）", "config": {"realm_dim": "职级"}}
    ]}, on_conflict="update", db=s, _=u)
    assert r["ok"] and r["updated"] == 1
    genres.invalidate_cache()
    assert genres.get_genre_by_key("urban", s)["label"] == "都市（导入版）"
    # 还原
    row = s.get(GenrePack, "urban")
    row.label = genres.GENRES["urban"]["label"]
    s.commit()
    genres.invalidate_cache()


def test_import_rename(ctx):
    """★ rename：冲突时自动加后缀，保留原包。"""
    main, s, u, genres = ctx
    _cleanup(s, "ren_g12", "ren_g12_2", "ren_g12_3")
    main.import_genres({"packs": [{"key": "ren_g12", "label": "原始"}]},
                       on_conflict="skip", db=s, _=u)
    r = main.import_genres({"packs": [{"key": "ren_g12", "label": "副本"}]},
                           on_conflict="rename", db=s, _=u)
    assert r["created"] == 1
    from apps.api.models import GenrePack
    assert s.get(GenrePack, "ren_g12") is not None, "原包应保留"
    assert s.get(GenrePack, "ren_g12_2") is not None, "应新建 _2"
    _cleanup(s, "ren_g12", "ren_g12_2", "ren_g12_3")
    genres.invalidate_cache()


def test_import_new_pack(ctx):
    main, s, u, genres = ctx
    _cleanup(s, "brand_new_g12")
    r = main.import_genres({"packs": [
        {"key": "brand_new_g12", "label": "全新题材", "aliases": ["全新"],
         "config": {"realm_dim": "段位", "metrics": {"金钱": {"units": ["币"], "cumulative": True}}}}
    ]}, on_conflict="skip", db=s, _=u)
    assert r["ok"] and r["created"] == 1
    genres.invalidate_cache()
    g = genres.get_genre("全新", s)
    assert g is not None and g["label"] == "全新题材"
    assert genres.realm_dim_for("brand_new_g12", s) == "段位"
    _cleanup(s, "brand_new_g12")
    genres.invalidate_cache()


# ==================================================== 脏数据容错
def test_import_dirty_data_does_not_abort(ctx):
    """★ 单条坏数据只记 errors，不中断整批（计划书 §六 风险 8）。"""
    main, s, u, genres = ctx
    _cleanup(s, "good_g12")
    dirty = {"packs": [
        {"key": "有中文", "label": "坏 key"},
        {"key": "good_g12", "label": "好的"},
        "不是对象",
        {"key": "bad_cfg_g12", "label": "坏配置", "config": "不是 dict"},
        {"key": "", "label": "空 key"},
    ]}
    r = main.import_genres(dirty, on_conflict="skip", db=s, _=u)
    assert r["ok"], "整批不该失败"
    assert r["created"] == 1, f"好数据应被导入，实际 created={r['created']}"
    assert len(r["errors"]) == 4, f"应记 4 条错误，实际 {r['errors']}"
    _cleanup(s, "good_g12", "bad_cfg_g12")
    genres.invalidate_cache()


def test_import_rejects_bad_payload(ctx):
    main, s, u, genres = ctx
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        main.import_genres({"nope": 1}, db=s, _=u)
    assert ei.value.status_code == 400


def test_import_rejects_bad_conflict_strategy(ctx):
    main, s, u, genres = ctx
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        main.import_genres({"packs": []}, on_conflict="drop_table", db=s, _=u)
    assert ei.value.status_code == 400


def test_import_accepts_bare_list(ctx):
    """允许直接传数组（不带 packs 包装）。"""
    main, s, u, genres = ctx
    _cleanup(s, "bare_g12")
    r = main.import_genres([{"key": "bare_g12", "label": "裸数组"}], db=s, _=u)
    assert r["ok"] and r["created"] == 1
    _cleanup(s, "bare_g12")
    genres.invalidate_cache()


def test_import_aliases_string_normalized(ctx):
    """aliases 传字符串也能处理（容错）。"""
    main, s, u, genres = ctx
    _cleanup(s, "ali_g12")
    r = main.import_genres({"packs": [
        {"key": "ali_g12", "label": "别名测试", "aliases": "甲，乙"}
    ]}, db=s, _=u)
    assert r["ok"] and r["created"] == 1
    from apps.api.models import GenrePack, jl
    row = s.get(GenrePack, "ali_g12")
    assert jl(row.aliases, []) == ["甲", "乙"], f"别名未正确拆分：{row.aliases}"
    _cleanup(s, "ali_g12")
    genres.invalidate_cache()


# ==================================================== round-trip 一致性
def test_round_trip_preserves_config(ctx):
    """★ 导出→导入 配置不丢（备份/迁移的核心保证）。"""
    main, s, u, genres = ctx
    _cleanup(s, "rt_g12")
    main.import_genres({"packs": [{
        "key": "rt_g12", "label": "往返测试", "aliases": ["往返"],
        "config": {"realm_dim": "段位",
                   "metrics": {"金钱": {"units": ["币"], "cumulative": True}},
                   "style_hint": "测试风格"},
    }]}, db=s, _=u)
    genres.invalidate_cache()
    exp = main.export_genres(db=s, _=u)
    pack = next(p for p in exp["packs"] if p["key"] == "rt_g12")
    assert pack["label"] == "往返测试"
    assert pack["aliases"] == ["往返"]
    assert pack["config"]["realm_dim"] == "段位"
    assert pack["config"]["style_hint"] == "测试风格"
    _cleanup(s, "rt_g12")
    genres.invalidate_cache()


def test_export_import_module_contract():
    from apps.api import genres
    for name in ("export_packs", "import_packs", "EXPORT_SCHEMA"):
        assert hasattr(genres, name), f"缺 {name}"
        assert name in genres.__all__


def test_export_includes_custom_pack(ctx):
    """★ 回归：自定义包（只在 DB、不在 GENRES）也必须导出。

    原实现以 GENRES.keys() 为全集，导致自定义题材导出时静默丢失
    —— 备份/分享会直接丢数据（v0.16 实测发现）。
    """
    main, s, u, genres = ctx
    _cleanup(s, "custom_only_g12")
    main.import_genres({"packs": [{
        "key": "custom_only_g12", "label": "仅DB题材", "aliases": ["仅DB"],
        "config": {"realm_dim": "段位"},
    }]}, db=s, _=u)
    genres.invalidate_cache()
    assert "custom_only_g12" not in genres.GENRES, "前提：该包不在内置表"

    exp = main.export_genres(include_builtin=1, db=s, _=u)
    keys = {p["key"] for p in exp["packs"]}
    assert "custom_only_g12" in keys, f"自定义包被漏导：{sorted(keys)}"

    # 只导自定义时也必须有它
    exp2 = main.export_genres(include_builtin=0, db=s, _=u)
    keys2 = {p["key"] for p in exp2["packs"]}
    assert "custom_only_g12" in keys2
    assert not (keys2 & set(genres.GENRES)), "只导自定义时混入了内置包"

    _cleanup(s, "custom_only_g12")
    genres.invalidate_cache()

# -*- coding: utf-8 -*-
"""v0.15 G8 回归：配置中心题材管理 API（D1 落地）。

核心验收（计划书 §五 G8）：
  * 可增删改题材包、启停、改完即生效（清缓存）
  * 内置包 builtin=1 **禁止删除**（计划书 §六 风险 9）
  * key 校验、重名校验
"""
import os

import pytest

os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-g8")


@pytest.fixture()
def ctx():
    from apps.api import main
    import apps.api.genres as genres
    genres.invalidate_cache()
    with main.SessionLocal() as s:
        from apps.api.models import User
        adm = s.get(User, "u_g8_adm")
        if not adm:
            adm = User(id="u_g8_adm", username="g8admin", role="admin", is_active=1)
            s.add(adm)
            s.commit()
        yield main, s, adm, genres
    genres.invalidate_cache()


def _cleanup(s, key):
    from apps.api.models import GenrePack
    r = s.get(GenrePack, key)
    if r:
        s.delete(r)
        s.commit()


# ==================================================== 清单
def test_list_genres_returns_items(ctx):
    main, s, adm, genres = ctx
    out = main.list_genres(db=s, _=adm)
    assert "items" in out and out["items"]
    assert out["builtin_count"] == len(genres.GENRES)
    for it in out["items"]:
        assert "enabled" in it and "builtin" in it


def test_public_list_requires_login_but_not_admin(ctx):
    """立项下拉用的公开清单，签名只要求登录（非 admin）。"""
    import inspect
    main, s, adm, genres = ctx
    sig = inspect.signature(main.list_genres_public)
    assert "db" in sig.parameters and "_" in sig.parameters


# ==================================================== 新增
def test_create_custom_genre(ctx):
    main, s, adm, genres = ctx
    _cleanup(s, "test_genre_g8")
    r = main.create_genre(main.GenrePackIn(
        key="test_genre_g8", label="测试题材", aliases=["测试"],
        config={"metrics": {"金钱": {"units": ["元"], "cumulative": True}},
                "realm_dim": "等级"}),
        db=s, _=adm)
    assert r["ok"] and r["key"] == "test_genre_g8"
    genres.invalidate_cache()
    assert genres.get_genre("测试", s) is not None
    _cleanup(s, "test_genre_g8")
    genres.invalidate_cache()


def test_create_rejects_builtin_key(ctx):
    """★ 不能重复创建内置题材的 key。"""
    main, s, adm, genres = ctx
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        main.create_genre(main.GenrePackIn(key="xianxia", label="假的仙侠"), db=s, _=adm)
    assert ei.value.status_code == 400


def test_create_rejects_bad_key(ctx):
    main, s, adm, genres = ctx
    from fastapi import HTTPException
    for bad in ["", "有中文", "has space", "a-b"]:
        with pytest.raises(HTTPException):
            main.create_genre(main.GenrePackIn(key=bad, label="x"), db=s, _=adm)


def test_create_rejects_empty_label(ctx):
    main, s, adm, genres = ctx
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        main.create_genre(main.GenrePackIn(key="nolebel", label=""), db=s, _=adm)


def test_create_rejects_duplicate(ctx):
    main, s, adm, genres = ctx
    from fastapi import HTTPException
    _cleanup(s, "dup_g8")
    main.create_genre(main.GenrePackIn(key="dup_g8", label="重复"), db=s, _=adm)
    with pytest.raises(HTTPException) as ei:
        main.create_genre(main.GenrePackIn(key="dup_g8", label="重复2"), db=s, _=adm)
    assert ei.value.status_code == 400
    _cleanup(s, "dup_g8")
    genres.invalidate_cache()


# ==================================================== 更新
def test_update_builtin_label_takes_effect(ctx):
    """★ 改内置包 label → 立即生效（清缓存后读到新值）。"""
    main, s, adm, genres = ctx
    from apps.api.models import GenrePack
    main.update_genre("urban", main.GenrePackIn(label="都市（改）"), db=s, _=adm)
    genres.invalidate_cache()
    assert genres.get_genre_by_key("urban", s)["label"] == "都市（改）"
    # 还原
    src = genres.GENRES["urban"]
    row = s.get(GenrePack, "urban")
    row.label = src["label"]
    s.commit()
    genres.invalidate_cache()


def test_update_missing_genre_seeds_first(ctx):
    """内置包若未落库，update 应先补种子再改（健壮性）。"""
    main, s, adm, genres = ctx
    from apps.api.models import GenrePack
    # 删掉 scifi 行模拟"未落库"
    row = s.get(GenrePack, "scifi")
    if row:
        s.delete(row)
        s.commit()
    r = main.update_genre("scifi", main.GenrePackIn(config={"realm_dim": "等级"}), db=s, _=adm)
    assert r["ok"], "应自动补种子后更新成功"
    _cleanup(s, "scifi")
    genres.invalidate_cache()


def test_toggle_disable_and_enable(ctx):
    main, s, adm, genres = ctx
    main.update_genre("game", main.GenrePackIn(enabled=0), db=s, _=adm)
    genres.invalidate_cache()
    assert genres.get_genre_by_key("game", s) is None, "停用后不可见"
    main.update_genre("game", main.GenrePackIn(enabled=1), db=s, _=adm)
    genres.invalidate_cache()
    assert genres.get_genre_by_key("game", s) is not None, "重新启用后可见"


# ==================================================== 删除（风险 9）
def test_delete_builtin_forbidden(ctx):
    """★ 内置包禁止删除（计划书 §六 风险 9）。"""
    main, s, adm, genres = ctx
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        main.delete_genre("xianxia", db=s, _=adm)
    assert ei.value.status_code == 400
    assert "内置" in str(ei.value.detail)
    # 确认还在
    genres.invalidate_cache()
    assert genres.get_genre_by_key("xianxia", s) is not None


def test_delete_custom_ok(ctx):
    main, s, adm, genres = ctx
    _cleanup(s, "del_g8")
    main.create_genre(main.GenrePackIn(key="del_g8", label="待删"), db=s, _=adm)
    r = main.delete_genre("del_g8", db=s, _=adm)
    assert r["ok"]
    genres.invalidate_cache()
    assert genres.get_genre_by_key("del_g8", s) is None


def test_delete_missing_404(ctx):
    main, s, adm, genres = ctx
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        main.delete_genre("never_existed_g8", db=s, _=adm)
    assert ei.value.status_code == 404


# ==================================================== reload
def test_reload_returns_counts(ctx):
    main, s, adm, genres = ctx
    r = main.reload_genres(db=s, _=adm)
    assert r["ok"] and r["total"] >= len(genres.GENRES)


# ==================================================== 即时生效（无重启）
def test_changes_take_effect_without_restart(ctx):
    """★ 核心验收：改完即生效，无需重启服务。"""
    main, s, adm, genres = ctx
    from apps.api.models import GenrePack
    _cleanup(s, "live_g8")
    main.create_genre(main.GenrePackIn(
        key="live_g8", label="即时题材",
        config={"metrics": {"金钱": {"units": ["信用点"], "cumulative": True}},
                "realm_dim": "权限等级"}),
        db=s, _=adm)
    # 不重启，直接读
    genres.invalidate_cache()
    g = genres.get_genre_by_key("live_g8", s)
    assert g is not None and g["label"] == "即时题材"
    assert genres.realm_dim_for("live_g8", s) == "权限等级"
    _cleanup(s, "live_g8")
    genres.invalidate_cache()

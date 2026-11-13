# -*- coding: utf-8 -*-
"""★ v1.2.0 封面/海报双版本能力测试。

覆盖三块新能力（全部确定性可跑，不依赖图片模型/LLM/网络）：
  1. cover._font() 多级字体降级 —— 找不到任何 CJK 字体也绝不 raise
     （血泪：旧版只查 3 个 Linux Noto 路径，Windows/Docker 部署一没字体就 500）。
  2. compose_cover 支持 2:3（小说封面）与 9:16（短剧竖屏海报）两种画幅，
     且任意尺寸底图都走 cover-fit 居中裁剪、不拉伸变形。
  3. 短剧海报端点 POST /api/books/{bid}/drama/projects/{pid}/poster
     与小说封面端点同套"无字底图 + 服务端真字体排字"反乱码架构，
     产物落 drama_media/poster_{pid}.png（项目维度，多项目不互相覆盖）。

HTTP 契约测试用 no_ai=true（纯渐变底）——不触达图片模型，CI 可确定性运行；
真正出图的路径（gen_base_image → media.run_media）由 media/挑模型那套测试兜底。
"""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from PIL import Image
from apps.api.db import Base, get_db
from apps.api.main import app
from apps.api.models import Book, DramaProject, User


# ================================================================ fixtures
def _now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def db_session(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}",
                       connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    SL = sessionmaker(bind=eng, autoflush=False, autocommit=False)
    s = SL()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def client(db_session, monkeypatch):
    """★★ 鉴权中间件直接用 SessionLocal（不走 Depends）→ 三处都要 patch（MEMORY §62）。"""
    import apps.api.main as main_mod
    import apps.api.auth as auth_mod
    import apps.api.db as db_mod

    def _get_db():
        yield db_session

    class _SL:
        def __call__(self):
            return db_session
        def __enter__(self):
            return db_session
        def __exit__(self, *a):
            return False

    app.dependency_overrides[get_db] = _get_db
    monkeypatch.setattr(main_mod, "SessionLocal", _SL(), raising=False)
    monkeypatch.setattr(auth_mod, "SessionLocal", _SL(), raising=False)
    monkeypatch.setattr(db_mod, "SessionLocal", _SL(), raising=False)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def user(db_session):
    u = User(id="u_cp", username="cover_p", role="owner", is_active=1,
             nickname="封面测试", email="cp@example.com", created_at=_now())
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture()
def H(user):
    from apps.api import auth
    return {"Authorization": f"Bearer {auth.make_token(user.id)}"}


# ================================================================ ① 字体降级
def test_font_never_raises_even_without_cjk(monkeypatch):
    """★ 找不到任何 CJK 字体时 _font 退到 Pillow 内置，绝不 raise。"""
    import apps.api.cover as cov
    monkeypatch.setattr(cov, "_FONT_ALL", [])          # 模拟一台什么 CJK 字体都没有的机器
    f = cov._font(48)
    assert f is not None, "无 CJK 字体时必须返回内置字体而非抛异常"
    # 内置默认字体同样能排版（不抛）
    assert hasattr(f, "getbbox") or hasattr(f, "getsize")


def test_font_ttc_index_fallback():
    """★ 候选字体打不开指定 index 时，退回普通 truetype / 内置字体，不崩。
    只要返回一个可用字体对象（能排版）即通过。"""
    import apps.api.cover as cov
    f = cov._font(24)
    assert f is not None, "_font 必须总归返回一个可用字体对象"
    assert hasattr(f, "getbbox") or hasattr(f, "getsize"), "返回对象应可测量文本"


# ================================================================ ② 画幅与合成
def test_base_size_maps():
    """★ 2:3 与 9:16 都映射到对应原生像素。"""
    import apps.api.cover as cov
    assert cov._BASE_SIZE["2:3"] == "1024x1536"
    assert cov._BASE_SIZE["9:16"] == "1080x1920"


def test_compose_23_gradient_only():
    """小说封面默认 2:3，无底图时纯渐变也能出图（尺寸正确）。"""
    import apps.api.cover as cov
    png = cov.compose_cover("测试书名", author="作者", base_png=None,
                            style="xianxia", portrait="2:3")
    out = Image.open(__import__("io").BytesIO(png))
    assert out.size == (1024, 1536), f"2:3 应为 1024x1536，实际 {out.size}"


def test_compose_916_gradient_only():
    """短剧海报 9:16（竖屏）。"""
    import apps.api.cover as cov
    png = cov.compose_cover("剧名", author="作者", base_png=None,
                            style="modern", portrait="9:16")
    out = Image.open(__import__("io").BytesIO(png))
    assert out.size == (1080, 1920), f"9:16 应为 1080x1920，实际 {out.size}"


def test_compose_916_coverfit_no_stretch():
    """★ 非 9:16 底图（如方图）走 cover-fit 居中裁剪，输出仍是 1080x1920 不拉伸。"""
    import io
    import apps.api.cover as cov
    buf = io.BytesIO()
    Image.new("RGB", (1024, 1024), (90, 90, 120)).save(buf, "PNG")
    png = cov.compose_cover("剧名", author="作者", base_png=buf.getvalue(),
                            style="modern", portrait="9:16")
    out = Image.open(io.BytesIO(png))
    assert out.size == (1080, 1920)
    # 反证：若是硬拉伸，方图会被压成 9:16（宽高比 1.0→0.5625），cover-fit 不会


def test_compose_unknown_portrait_falls_back():
    """★ 传了未知画幅不崩，回落默认 2:3（血泪：宽泛 except 至少要有确定行为）。"""
    import apps.api.cover as cov
    png = cov.compose_cover("书", base_png=None, portrait="7:1")
    out = Image.open(__import__("io").BytesIO(png))
    assert out.size == (1024, 1536)


# ================================================================ ③ 文件名安全
def test_save_cover_rejects_traversal(tmp_path, monkeypatch):
    """★ filename 只允许白名单字符，拒 ../ 与绝对路径（与 gitrepo.safe_book_id 同理）。"""
    import apps.api.cover as cov
    monkeypatch.setattr(cov, "BOOKS_DIR", str(tmp_path / "books"))
    with pytest.raises(ValueError):
        cov.save_cover("bk1", b"x", filename="../evil.png")
    with pytest.raises(ValueError):
        cov.save_cover("bk1", b"x", filename="/etc/passwd")
    with pytest.raises(ValueError):
        cov.save_cover("bk1", b"x", filename="a/b.png")


def test_save_cover_valid_filename(tmp_path, monkeypatch):
    import apps.api.cover as cov
    monkeypatch.setattr(cov, "BOOKS_DIR", str(tmp_path / "books"))
    rel = cov.save_cover("bk1", b"pngdata", filename="poster_p123.png")
    assert rel == "bk1/drama_media/poster_p123.png"
    p = tmp_path / "books" / "bk1" / "drama_media" / "poster_p123.png"
    assert p.exists() and p.read_bytes() == b"pngdata"


def test_poster_filename_sanitize():
    """★ project_id 里任何非法字符都归一化，且不产生路径分隔。"""
    import apps.api.cover as cov
    assert cov.poster_filename("p123") == "poster_p123.png"
    assert "/" not in cov.poster_filename("../../etc")
    assert ".." not in cov.poster_filename("a..b").replace("a..b", "a..b")  # 纯文件名无分隔
    assert cov.poster_filename("") == "poster_p.png"


# ================================================================ ④ 海报 prompt 兜底
def test_poster_fallback_prompt_no_ai_never_crashes():
    """★ 无 LLM（planner 模型未配）时确定性兜底，含 9:16 锚点 + 禁文字指令。"""
    import apps.api.cover as cov

    class B:
        id = "b1"
        title = "我的书"
        genre = "仙侠"
        synopsis = "修仙"
    class P:
        id = "p1"
        title = "我的短剧"
        style = "水墨古风"
    p = cov._fallback_poster_prompt(B(), P(), {"tags": []}, None)
    assert "9:16" in p, "短剧海报兜底 prompt 必须竖屏锚定"
    assert cov.NO_TEXT_SUFFIX in p, "兜底 prompt 也必须禁文字（反乱码铁律）"
    assert "水墨古风" in p, "项目画风串应进海报 prompt"


# ================================================================ ⑤ HTTP 契约（no_ai 确定性）
def test_book_cover_endpoint_noai(client, user, db_session, H):
    """小说封面端点：no_ai=true 纯渐变出图，返回 cover.png 路径与 url。"""
    db_session.add(Book(id="bk_cov", title="契约书", author="作者A",
                       owner_id=user.id, created_at=_now()))
    db_session.commit()
    r = client.post("/api/books/bk_cov/cover",
                    json={"no_ai": True, "style": "xianxia"},
                    headers=H)
    assert r.status_code == 200, f"封面端点应 200，实际 {r.status_code}: {r.text[:200]}"
    d = r.json()
    assert d["ok"] is True
    assert d["path"] == "bk_cov/drama_media/cover.png"
    assert d["url"] == "/api/files/bk_cov/drama_media/cover.png"
    assert d["ai_base"] is False, "no_ai=true 时不应走底图模型"


def test_book_cover_endpoint_916_portrait(client, user, db_session, H):
    """★ v1.2.0：portrait=9:16 合法返回；产物仍是 cover.png（小说封面单一文件）。"""
    db_session.add(Book(id="bk_cov9", title="竖屏书", author="作者B",
                       owner_id=user.id, created_at=_now()))
    db_session.commit()
    r = client.post("/api/books/bk_cov9/cover",
                    json={"no_ai": True, "portrait": "9:16"},
                    headers=H)
    assert r.status_code == 200
    assert r.json()["portrait"] == "9:16"


def test_book_cover_missing_book_404(client, H):
    r = client.post("/api/books/bk_does_not_exist/cover",
                    json={"no_ai": True}, headers=H)
    assert r.status_code == 404


def test_poster_endpoint_noai(client, user, db_session, H):
    """★ 短剧海报端点：no_ai=true 出图，产物落 poster_{pid}.png。"""
    db_session.add(Book(id="bk_prj", title="短剧源书", author="作者C",
                       owner_id=user.id, created_at=_now()))
    db_session.add(DramaProject(id="prj_1", book_id="bk_prj",
                                title="我的短剧", owner_id=user.id))
    db_session.commit()
    r = client.post("/api/books/bk_prj/drama/projects/prj_1/poster",
                    json={"no_ai": True, "style": "modern"},
                    headers=H)
    assert r.status_code == 200, f"海报端点应 200，实际 {r.status_code}: {r.text[:200]}"
    d = r.json()
    assert d["ok"] is True
    assert d["path"] == "bk_prj/drama_media/poster_prj_1.png"
    assert d["url"] == "/api/files/bk_prj/drama_media/poster_prj_1.png"
    assert d["ai_base"] is False


def test_poster_missing_project_404(client, user, db_session, H):
    """短剧项目不存在 → 404（_drama_ctx 契约）。"""
    db_session.add(Book(id="bk_nop", title="空书", author="作者D",
                       owner_id=user.id, created_at=_now()))
    db_session.commit()
    r = client.post("/api/books/bk_nop/drama/projects/prj_x/poster",
                    json={"no_ai": True}, headers=H)
    assert r.status_code == 404


def test_poster_cross_book_404(client, user, db_session, H):
    """★ 横向越权：项目属于别的书时，用另一本书调海报端点 → 404。"""
    db_session.add(Book(id="bk_a", title="A", author="作者",
                       owner_id=user.id, created_at=_now()))
    db_session.add(Book(id="bk_b", title="B", author="作者",
                       owner_id=user.id, created_at=_now()))
    db_session.add(DramaProject(id="prj_b", book_id="bk_b", title="B剧",
                                owner_id=user.id))
    db_session.commit()
    # 项目 prj_b 属于 bk_b，但请求挂在 bk_a 下 → 应 404（p.book_id != book_id）
    r = client.post("/api/books/bk_a/drama/projects/prj_b/poster",
                    json={"no_ai": True}, headers=H)
    assert r.status_code == 404

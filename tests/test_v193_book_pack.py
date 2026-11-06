# -*- coding: utf-8 -*-
"""v0.19.3 F2 回归：整书打包导出 / 导入（.nfbook）。

背景
----
项目已有**给人读的**导出（txt/md/epub/投稿稿），缺的是「机器可还原的完整快照」。
换服务器时无法把一本书连同设定/大纲/人物/伏笔/纪年表/账本/短剧工程一起搬走。

`.nfbook` 就是个 ZIP：
    manifest.json   元信息（格式版本 / 导出时间 / 各表条数）
    book.json       书籍主记录
    tables.json     关联表（卷/章/人物/世界观/伏笔/时间线/账本/审计/风格/对话）
    drama.json      短剧工程（项目/分集/资产/分镜）
    files/          书籍目录原始文件（正文 md / 人物卡 / 大纲）

★ 安全红线：包内**绝不能含** users / model_profiles / settings（含账号与 API Key）。
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile

import pytest

os.environ.setdefault("NOVELFORGE_DATA", tempfile.mkdtemp(prefix="nf_test_f2_"))
os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-f2")


# ============================================================ fixtures
@pytest.fixture()
def db():
    from apps.api import main
    with main.SessionLocal() as s:
        yield s


@pytest.fixture()
def sample_book(db):
    from apps.api.models import Book, Chapter, Character, WorldSetting, Foreshadow
    from sqlalchemy import delete
    bid = "bk_pack_t"
    for M in (Chapter, Character, WorldSetting, Foreshadow):
        db.execute(delete(M).where(M.book_id == bid))
    db.execute(delete(Book).where(Book.id == bid))
    db.commit()
    db.add(Book(id=bid, title="打包样本", theme="xianxia", owner_id="u1",
                genre="仙侠"))
    db.commit()
    for i in range(1, 4):
        db.add(Chapter(id=f"c_pk_{i}", book_id=bid, idx=i, title=f"第{i}章",
                       content=f"第{i}章正文。" * 30, words=300, gate_score=90))
    db.add(Character(id="ch_pk_1", book_id=bid, name="沈闲", role="主角",
                     profile="打工人穿越"))
    db.add(WorldSetting(id="ws_pk_1", book_id=bid, category="境界",
                        key="练气", value="一层"))
    db.add(Foreshadow(id="fs_pk_1", book_id=bid, content="大爷的身份", status="open"))
    db.commit()
    return db.get(Book, bid)


# ============================================================ 导出
def test_export_returns_zip(db, sample_book):
    from apps.api import book_pack
    data, fname = book_pack.export_book(db, sample_book)
    assert data[:2] == b"PK", "不是有效 ZIP"
    assert fname.endswith(".nfbook")


def test_export_structure(db, sample_book):
    from apps.api import book_pack
    data, _ = book_pack.export_book(db, sample_book)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
    assert {"manifest.json", "book.json", "tables.json"} <= names


def test_manifest_content(db, sample_book):
    from apps.api import book_pack
    data, _ = book_pack.export_book(db, sample_book)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        mf = json.loads(z.read("manifest.json"))
    assert mf["magic"] == book_pack.MAGIC
    assert mf["format_version"] == book_pack.FORMAT_VERSION
    assert mf["book_id"] == sample_book.id
    assert mf["stats"]["chapters"] == 3
    assert mf["stats"]["characters"] == 1


def test_export_includes_all_book_tables(db, sample_book):
    from apps.api import book_pack
    data, _ = book_pack.export_book(db, sample_book)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        tables = json.loads(z.read("tables.json"))
    for t in ("chapters", "characters", "world_settings", "foreshadows"):
        assert t in tables, f"缺表 {t}"
    assert len(tables["chapters"]) == 3


def test_export_empty_book_ok(db):
    """★ 空书也要能导出（不能因为没章节就报错）。"""
    from apps.api import book_pack
    from apps.api.models import Book
    from sqlalchemy import delete
    db.execute(delete(Book).where(Book.id == "bk_empty_pk")); db.commit()
    db.add(Book(id="bk_empty_pk", title="空书", owner_id="u1")); db.commit()
    data, _ = book_pack.export_book(db, db.get(Book, "bk_empty_pk"))
    assert len(data) > 0
    assert book_pack.inspect_package(data)["ok"] is True


# ============================================================ ★ 安全红线
def test_package_excludes_credentials(db, sample_book):
    """★★ 最重要：包内绝不能含账号 / API Key / 系统设置。

    ★ 注意断言方式：不能简单 `"settings" not in text` ——
    正常的 `world_settings` 表名也包含 "settings" 子串，会误报（实测踩坑）。
    正确做法是**检查 JSON 的顶层 key**（表名），而不是全文子串匹配。
    """
    from apps.api import book_pack
    data, _ = book_pack.export_book(db, sample_book)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        tables = json.loads(z.read("tables.json")) if "tables.json" in names else {}
        drama = json.loads(z.read("drama.json")) if "drama.json" in names else {}
        blob = b"".join(z.read(n) for n in names).decode("utf-8", "ignore")
    present = set(tables) | set(drama)
    for forbidden in ("model_profiles", "users", "settings", "runs", "media_calls"):
        assert forbidden not in present, f"包内不该含表：{forbidden}"
    # 关键字段级检查：全包里不该出现密钥字段
    for field in ("api_key", "apikey", "password_hash", "secret"):
        assert field not in blob, f"包内疑似含敏感字段：{field}"


def test_excluded_tables_documented():
    """排除清单要写清楚（防以后有人手滑加回去）。"""
    from apps.api import book_pack
    # runs / media_calls 是运行时台账，体积大且无迁移价值
    assert "runs" not in book_pack.BOOK_TABLES
    assert "media_calls" not in book_pack.BOOK_TABLES
    assert "model_profiles" not in book_pack.BOOK_TABLES
    assert "users" not in book_pack.BOOK_TABLES


# ============================================================ 校验
def test_inspect_valid(db, sample_book):
    from apps.api import book_pack
    data, _ = book_pack.export_book(db, sample_book)
    r = book_pack.inspect_package(data)
    assert r["ok"] and r["manifest"]["book_title"] == "打包样本"


def test_inspect_rejects_garbage():
    from apps.api import book_pack
    assert book_pack.inspect_package(b"not a zip")["ok"] is False
    assert book_pack.inspect_package(b"")["ok"] is False


def test_inspect_rejects_foreign_zip():
    """★ 是 ZIP 但不是 NovelForge 备份 → 应拒绝。"""
    from apps.api import book_pack
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "world")
    r = book_pack.inspect_package(buf.getvalue())
    assert r["ok"] is False
    assert "manifest" in r["error"]


def test_inspect_rejects_future_version(db, sample_book):
    """★ 未来版本（format_version 更高）必须拒绝，不能盲导。"""
    from apps.api import book_pack
    data, _ = book_pack.export_book(db, sample_book)
    # 篡改 manifest 版本号
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        items = {n: z.read(n) for n in z.namelist()}
    mf = json.loads(items["manifest.json"])
    mf["format_version"] = book_pack.FORMAT_VERSION + 99
    items["manifest.json"] = json.dumps(mf).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, b in items.items():
            z.writestr(n, b)
    r = book_pack.inspect_package(buf.getvalue())
    assert r["ok"] is False
    assert "版本" in r["error"]


def test_inspect_never_raises():
    from apps.api import book_pack
    for bad in (b"", b"x", b"PK\x03\x04broken", None):
        try:
            r = book_pack.inspect_package(bad)
            assert isinstance(r, dict)
        except Exception as e:
            pytest.fail(f"inspect 抛异常：{type(e).__name__}: {e}")


# ============================================================ 导入（copy）
def test_import_copy_roundtrip(db, sample_book):
    """★ 核心：导出→导入（copy）数据完整还原，且新 id 不撞车。"""
    from apps.api import book_pack
    from apps.api.models import Book, Chapter, Character
    data, _ = book_pack.export_book(db, sample_book)
    r = book_pack.import_book(db, data, mode="copy")
    assert r["ok"] is True
    assert r["book_id"] != sample_book.id, "copy 模式应生成新 id"

    nb = db.get(Book, r["book_id"])
    assert nb is not None
    assert "（导入）" in nb.title
    assert nb.theme == sample_book.theme
    chs = db.query(Chapter).filter(Chapter.book_id == r["book_id"]).all()
    assert len(chs) == 3
    assert sorted(c.idx for c in chs) == [1, 2, 3]
    chars = db.query(Character).filter(Character.book_id == r["book_id"]).all()
    assert [c.name for c in chars] == ["沈闲"]


def test_import_preserves_content(db, sample_book):
    """正文内容必须逐字还原（不能丢字/截断）。"""
    from apps.api import book_pack
    from apps.api.models import Chapter
    data, _ = book_pack.export_book(db, sample_book)
    r = book_pack.import_book(db, data, mode="copy")
    orig = db.query(Chapter).filter(Chapter.book_id == sample_book.id,
                                    Chapter.idx == 1).first()
    new = db.query(Chapter).filter(Chapter.book_id == r["book_id"],
                                   Chapter.idx == 1).first()
    assert new.content == orig.content
    assert new.words == orig.words
    assert new.gate_score == orig.gate_score


def test_import_copy_twice_no_conflict(db, sample_book):
    """★ 同一包连导两次不该主键冲突（copy 模式要换新 id）。"""
    from apps.api import book_pack
    data, _ = book_pack.export_book(db, sample_book)
    r1 = book_pack.import_book(db, data, mode="copy")
    r2 = book_pack.import_book(db, data, mode="copy")
    assert r1["ok"] and r2["ok"]
    assert r1["book_id"] != r2["book_id"]


def test_import_datetime_coerced(db, sample_book):
    """★ datetime 必须转回 Python 对象（否则 SQLite 报 TypeError）。

    这是实测踩的坑：导出时 datetime→ISO 字符串，导入不转回来会直接炸。
    """
    from apps.api import book_pack
    from apps.api.models import Book
    data, _ = book_pack.export_book(db, sample_book)
    r = book_pack.import_book(db, data, mode="copy")
    nb = db.get(Book, r["book_id"])
    assert nb is not None, "导入失败（很可能是 datetime 未转换）"
    import datetime as dt
    assert isinstance(nb.created_at, dt.datetime), \
        f"created_at 类型错误：{type(nb.created_at)}"


# ============================================================ 导入（merge）
def test_import_merge_same_id(db, sample_book):
    """★ merge 模式：沿用原 id，用于"恢复到同一位置"。"""
    from apps.api import book_pack
    data, _ = book_pack.export_book(db, sample_book)
    r = book_pack.import_book(db, data, mode="merge")
    assert r["ok"] is True
    assert r["book_id"] == sample_book.id, "merge 模式应保持原 id"


def test_import_rejects_bad_package(db, sample_book):
    from apps.api import book_pack
    r = book_pack.import_book(db, b"garbage", mode="copy")
    assert r["ok"] is False
    assert "error" in r


# ============================================================ API
def test_api_endpoints_registered():
    from apps.api import main
    for fn in ("book_pack_export", "book_pack_import", "book_pack_inspect"):
        assert hasattr(main, fn), f"缺端点函数 {fn}"


def test_api_export_response():
    from fastapi.testclient import TestClient
    from apps.api.main import app
    c = TestClient(app)
    # 未鉴权应 401（而不是 404，证明路由存在）
    r = c.get("/api/books/bk_pack_t/pack")
    assert r.status_code == 401


def test_model_helper_works():
    """模型反射必须能找到真实模型（否则导出静默丢表）。"""
    from apps.api import book_pack
    for t in ("chapters", "characters", "world_settings", "draма_projects".replace("ма", "ma")):
        assert book_pack._model_for(t) is not None, f"反射不到 {t}"
    assert book_pack._model_for("不存在的表") is None


def test_pk_new_generation():
    from apps.api import book_pack
    from apps.api.models import Chapter
    n1 = book_pack._new_pk(Chapter, "c_pk_1")
    n2 = book_pack._new_pk(Chapter, "c_pk_1")
    assert n1 != n2, "新主键应唯一"
    assert n1.startswith("c_"), f"应保持前缀风格：{n1}"

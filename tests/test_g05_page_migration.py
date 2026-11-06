"""v0.25.10 G0-5 核心页面迁移 —— 契约与产物回归测试。

★ 为什么这个测试重要（铁律 R-C「一页一验收」的技术保障）：
  页面迁移最大的风险是**前端假设的接口形状与后端不一致** ——
  这类 bug 不报错、只在运行时表现为"页面空白/undefined"。
  所以这里对**每个被迁移页面依赖的端点**都做真实调用，校验字段形状。

★ 覆盖：
  ① 写作台（StudioView）：book / chapters / state / conversations / messages
  ② 章节列表与编辑：chapter / patch / history / gate
  ③ 设定页：characters / patch / bible / consistency / memory
  ④ 产物完整性：所有已迁移页面都在构建产物中
  ⑤ 类型契约：后端返回的关键字段名与前端类型一致
     （★ 字段名拼错是最高频的"静默失效"来源）
"""
from __future__ import annotations

import json
import re
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module", autouse=True)
def _ensure_tables():
    from apps.api import models  # noqa: F401
    from apps.api.db import Base, engine
    Base.metadata.create_all(engine)
    from apps.api.services import db_migrate
    db_migrate.migrate()
    try:
        from apps.api import rbac as _rbac
        from apps.api.db import SessionLocal as _S
        with _S() as _db:
            _rbac.seed_role_permissions(_db)
    except Exception:
        pass
    yield


@pytest.fixture(scope="module")
def client():
    from apps.api.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _fresh():
    import importlib
    import sys
    global auth, SessionLocal, APP2_DIR
    auth = sys.modules.get("apps.api.auth") or importlib.import_module("apps.api.auth")
    _db = sys.modules.get("apps.api.db") or importlib.import_module("apps.api.db")
    SessionLocal = _db.SessionLocal
    m = sys.modules.get("apps.api.main") or importlib.import_module("apps.api.main")
    APP2_DIR = m.APP2_DIR
    yield


def _mk_user() -> tuple[str, str]:
    uname = f"g5_{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        u = auth.create_user(db, uname, "g5-test-12345", role="editor")
        u.is_active = 1
        db.commit()
        return u.id, auth.make_token(u.id)


def _h(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


def _mk_book(owner_id: str, n_chapters: int = 3) -> str:
    import datetime as dt
    from apps.api.models import Book, Chapter
    bid = f"bk_{uuid.uuid4().hex[:12]}"
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    with SessionLocal() as db:
        db.add(Book(id=bid, title="G0测试书", author="测试", genre="xianxia",
                    owner_id=owner_id, platform="起点", target_words=1000000,
                    chapter_min=2000, chapter_max=3000, created_at=now))
        for i in range(1, n_chapters + 1):
            db.add(Chapter(id=f"ch_{uuid.uuid4().hex[:10]}", book_id=bid, idx=i,
                           title=f"第{i}章 测试", content="测试正文内容" * 30,
                           words=180, status="draft", gate_score=70 + i))
        db.commit()
    return bid


# ============================================================ ① 写作台契约

def test_studio_book_detail_shape(client):
    """★ 写作台顶栏依赖：title / genre / platform"""
    uid, tok = _mk_user()
    bid = _mk_book(uid)
    r = client.get(f"/api/books/{bid}", headers=_h(tok))
    assert r.status_code == 200
    d = r.json()
    for k in ("id", "title", "author", "genre", "platform",
              "target_words", "chapter_min", "chapter_max", "style"):
        assert k in d, f"写作台依赖的字段缺失：{k}"
    assert isinstance(d["style"], dict)
    for k in ("pov", "tone", "rules", "max_para_chars", "banned_words"):
        assert k in d["style"], f"文风字段缺失：{k}"


def test_studio_chapters_returns_bare_array(client):
    """★★ 关键契约：`GET /chapters` 返回**裸数组**，不是 `{items}`。

    前端若写成 `r.items` 就会拿到 undefined → 章节树永远空
    （不报错，只是空白 —— 最典型的静默失效）。
    """
    uid, tok = _mk_user()
    bid = _mk_book(uid, 3)
    r = client.get(f"/api/books/{bid}/chapters", headers=_h(tok))
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d, list), f"应为裸数组，实际 {type(d).__name__}"
    assert len(d) == 3
    for k in ("idx", "title", "words", "status", "gate_score", "volume"):
        assert k in d[0], f"章节项字段缺失：{k}"


def test_studio_state_shape(client):
    """★ 写作台右栏依赖：volumes / characters / foreshadows / world / progress"""
    uid, tok = _mk_user()
    bid = _mk_book(uid)
    r = client.get(f"/api/books/{bid}/state", headers=_h(tok))
    assert r.status_code == 200
    d = r.json()
    for k in ("volumes", "characters", "foreshadows", "world", "progress"):
        assert k in d, f"状态中枢字段缺失：{k}"
        assert isinstance(d[k], (list, dict))
    for k in ("chapters", "words", "last_idx", "open_foreshadows"):
        assert k in d["progress"], f"progress 字段缺失：{k}"


def test_studio_conversations_and_messages(client):
    """★ 写作台对话流依赖：会话创建 → 消息列表"""
    uid, tok = _mk_user()
    bid = _mk_book(uid)
    r = client.post(f"/api/books/{bid}/conversations", headers=_h(tok),
                    json={"title": "测试对话"})
    assert r.status_code == 200, r.text
    # ★ 后端返回扁平 {"id": "..."} —— 断言真实契约
    body = r.json()
    assert "id" in body, f"创建会话应返回 id，实际 {list(body)}"
    cid = body["id"]

    r2 = client.get(f"/api/books/{bid}/conversations", headers=_h(tok))
    assert r2.status_code == 200
    d2 = r2.json()
    # ★ 真实契约：{conversations: [...], active} —— 不是 items、不是裸数组
    assert "conversations" in d2, f"会话列表应为 conversations 键，实际 {list(d2)}"
    assert "active" in d2
    assert any(c["id"] == cid for c in d2["conversations"])

    r3 = client.get(f"/api/books/{bid}/conversations/{cid}/messages", headers=_h(tok))
    assert r3.status_code == 200
    # ★ 真实契约：{messages: [...]}
    assert "messages" in r3.json(), f"消息列表应为 messages 键，实际 {list(r3.json())}"


# ============================================================ ② 章节契约

def test_chapter_detail_and_patch(client):
    """★ 章节编辑器依赖：读详情 → 保存 → 字数回传"""
    uid, tok = _mk_user()
    bid = _mk_book(uid, 1)

    r = client.get(f"/api/books/{bid}/chapters/1", headers=_h(tok))
    assert r.status_code == 200
    d = r.json()
    for k in ("idx", "title", "content", "words", "status", "gate_score", "gate_report"):
        assert k in d, f"章节详情字段缺失：{k}"

    new_content = "全新正文" * 50
    r2 = client.patch(f"/api/books/{bid}/chapters/1", headers=_h(tok),
                      json={"title": "改后标题", "content": new_content})
    assert r2.status_code == 200
    body = r2.json()
    assert body.get("ok") is True
    # ★ 后端会重算字数并回传（前端依赖它更新显示）
    assert "words" in body
    assert body["words"] > 0

    r3 = client.get(f"/api/books/{bid}/chapters/1", headers=_h(tok))
    assert r3.json()["title"] == "改后标题"


def test_chapter_history_and_gate(client):
    uid, tok = _mk_user()
    bid = _mk_book(uid, 1)
    r = client.get(f"/api/books/{bid}/chapters/1/history", headers=_h(tok))
    assert r.status_code == 200
    assert isinstance(r.json(), list)

    r2 = client.post(f"/api/books/{bid}/chapters/1/gate", headers=_h(tok), json={})
    assert r2.status_code == 200
    rep = r2.json()
    # ★ 质检报告必须带 score（前端色阶依赖）
    assert "score" in rep, f"质检报告缺少 score：{list(rep)[:8]}"


def test_chapter_404_for_missing(client):
    uid, tok = _mk_user()
    bid = _mk_book(uid, 1)
    r = client.get(f"/api/books/{bid}/chapters/999", headers=_h(tok))
    assert r.status_code == 404


def test_chapter_patch_upsert_creates_missing(client):
    """★ v1.0-E4.17：PATCH 章节对「尚未建行」的章节号应 upsert 建行（不再 404）。

    场景：待审草稿「在编辑器中查看」跳到 chapter-edit，新书首次起草时目标章节
    还没入库 → GET 仍 404（读契约不变），但用户在编辑器预填草稿后点「保存」
    走 PATCH，必须成功建行，否则**丢稿**。
    """
    uid, tok = _mk_user()
    bid = _mk_book(uid, 1)
    idx = 7  # 库里没有第 7 章
    # GET 仍 404（读路径契约保持）
    g = client.get(f"/api/books/{bid}/chapters/{idx}", headers=_h(tok))
    assert g.status_code == 404
    # PATCH 不 404，建行成功
    p = client.patch(f"/api/books/{bid}/chapters/{idx}", headers=_h(tok),
                     json={"title": "草稿第七章", "content": "正文内容" * 20})
    assert p.status_code == 200, f"PATCH 对不存在章节不应 404（upsert）：{p.status_code}"
    body = p.json()
    assert body.get("ok") is True
    assert body["words"] > 0
    # 建行后 GET 200，且内容落库
    g2 = client.get(f"/api/books/{bid}/chapters/{idx}", headers=_h(tok))
    assert g2.status_code == 200
    assert g2.json()["title"] == "草稿第七章"


# ============================================================ ③ 设定页契约

def test_characters_shape(client):
    """★ 人物管理依赖：id/name/role/profile/voice/arc/mentions/is_auto"""
    uid, tok = _mk_user()
    bid = _mk_book(uid)
    r = client.get(f"/api/books/{bid}/characters", headers=_h(tok))
    assert r.status_code == 200
    d = r.json()
    assert "items" in d
    assert isinstance(d["items"], list)
    # 空库也要能返回（前端空态依赖）
    if d["items"]:
        for k in ("id", "name", "role", "profile", "voice", "arc",
                  "mentions", "is_auto", "first_chapter", "state_now"):
            assert k in d["items"][0], f"人物字段缺失：{k}"


def test_bible_shape(client):
    """★ 大纲页依赖：meta / book / world / characters / volumes /
    foreshadows / chapters / progress"""
    uid, tok = _mk_user()
    bid = _mk_book(uid, 2)
    r = client.get(f"/api/books/{bid}/bible", headers=_h(tok))
    assert r.status_code == 200
    d = r.json()
    for k in ("meta", "book", "world", "characters", "volumes",
              "foreshadows", "chapters", "progress"):
        assert k in d, f"bible 字段缺失：{k}"
    for k in ("title", "genre", "platform", "chapter_min", "chapter_max",
              "target_words", "theme", "tags"):
        assert k in d["book"], f"bible.book 字段缺失：{k}"
    # ★ 章节点必须能对上前端类型
    if d["chapters"]:
        for k in ("idx", "title", "words", "gate_score", "status"):
            assert k in d["chapters"][0], f"bible.chapters 字段缺失：{k}"


def test_consistency_and_memory_endpoints(client):
    """★ 质检页依赖（失败不能 500，最多返回空结构）"""
    uid, tok = _mk_user()
    bid = _mk_book(uid)
    r = client.get(f"/api/books/{bid}/consistency", headers=_h(tok))
    assert r.status_code == 200, f"一致性检查失败：{r.status_code}"
    assert isinstance(r.json(), dict)

    r2 = client.get(f"/api/books/{bid}/memory", headers=_h(tok))
    assert r2.status_code == 200, f"记忆概况失败：{r2.status_code}"
    assert isinstance(r2.json(), dict)


def test_runs_shape(client):
    """★ 质检页运行记录依赖（★ 注意：/runs 返回裸数组）"""
    uid, tok = _mk_user()
    bid = _mk_book(uid)
    r = client.get(f"/api/books/{bid}/runs", headers=_h(tok))
    assert r.status_code == 200
    assert isinstance(r.json(), list), "runs 应为裸数组"


def test_audits_shape(client):
    uid, tok = _mk_user()
    bid = _mk_book(uid)
    r = client.get(f"/api/books/{bid}/audits", headers=_h(tok))
    assert r.status_code == 200
    assert "items" in r.json()


# ============================================================ ④ 产物完整性

MIGRATED_VIEWS = [
    "StudioView", "ChapterListView", "ChapterEditView",
    "CharacterListView", "OutlineView", "QualityView",
    "BookListView", "HomeView", "LoginView", "RegisterView",
]


@pytest.mark.parametrize("name", MIGRATED_VIEWS)
def test_view_built_into_bundle(name):
    """★ 每个已迁移页面都必须出现在构建产物里。

    ★ 为什么要测：路由是懒加载（`() => import(...)`），
      如果某个页面编译失败但构建仍"成功"（或漏配路由），
      线上表现是**点击即白屏**，而构建日志可能看不出问题。
    """
    assets = APP2_DIR / "assets"
    if not assets.is_dir():
        pytest.skip("新版未构建")
    hits = [f for f in assets.iterdir() if f.name.startswith(name + "-")]
    assert hits, f"{name} 未出现在构建产物中（路由/编译有问题？）"


def test_bundle_size_sane():
    """产物体积合理性（防止误打包 node_modules 之类）。"""
    assets = APP2_DIR / "assets"
    if not assets.is_dir():
        pytest.skip("新版未构建")
    js = [f for f in assets.iterdir() if f.suffix == ".js"]
    assert js, "没有 JS 产物"
    biggest = max(js, key=lambda f: f.stat().st_size)
    # vue vendor 约 112KB；超过 600KB 说明打包出了问题
    assert biggest.stat().st_size < 600_000, \
        f"最大 JS 产物 {biggest.name} = {biggest.stat().st_size} 字节，异常偏大"


def test_no_duplicate_vue_chunks():
    """★ 防止 Vue 被重复打包（会导致运行时两个实例、store 不共享）。"""
    assets = APP2_DIR / "assets"
    if not assets.is_dir():
        pytest.skip("新版未构建")
    vue_chunks = [f for f in assets.iterdir() if f.name.startswith("vue-")]
    assert len(vue_chunks) == 1, f"Vue 被打包了 {len(vue_chunks)} 次：{[f.name for f in vue_chunks]}"


def test_index_references_assets(client):
    r"""index.html 必须引用带 hash 的产物（否则缓存策略失效）。

    ★ 正则说明（v1.0-E4.1 修正）：Vite 的产物名是 `<名字>-<hash>.js`，
      而**名字本身可能以 `-` 结尾**（如 `index--Vx2bPrO.js`）
      → 出现连续两个横线。旧正则要求名字部分不含横线，
      会**漏匹配**（测试假失败）。

      ★ 教训：测试断言的"格式假设"要写宽一点 ——
        我们真正要验证的是"**文件名里带 hash**"，
        而不是"Vite 恰好用什么符号分隔"。用 `[-\w]+` + 捕捉 hash 更稳。
    """
    if not (APP2_DIR / "index.html").is_file():
        pytest.skip("新版未构建")
    html = (APP2_DIR / "index.html").read_text(encoding="utf-8")
    assert "/assets/" in html
    # ★ hash 通常是 8 位以上的 base64url 字符；名字部分允许含 `-`
    m = re.search(r"/assets/[-\w]+-([A-Za-z0-9_]{6,})\.js", html)
    assert m, "index.html 未引用带 hash 的 JS"


# ============================================================ ⑤ 静态资源可达

def test_app2_assets_reachable(client):
    """★ 构建产物可经 /assets/ 访问（部署漏传会白屏）。"""
    assets = APP2_DIR / "assets"
    if not assets.is_dir():
        pytest.skip("新版未构建")
    for f in list(assets.iterdir())[:5]:
        r = client.get(f"/assets/{f.name}")
        assert r.status_code == 200, f"{f.name} 不可访问：{r.status_code}"


def test_app2_view_routes_fallback(client):
    """★ 所有迁移页面的前端路由都要能直访（刷新不 404）。"""
    if not (APP2_DIR / "index.html").is_file():
        pytest.skip("新版未构建")
    bid = "bk_test123"
    paths = [
        "/books", f"/books/{bid}", f"/books/{bid}/chapters",
        f"/books/{bid}/chapters/1", f"/books/{bid}/characters",
        f"/books/{bid}/outline", f"/books/{bid}/quality",
        "/drama", "/account/profile", "/data/usage",
    ]
    for p in paths:
        r = client.get(p)
        assert r.status_code == 200, f"{p} -> {r.status_code}"
        assert '<div id="app">' in r.text, f"{p} 未回退到 index.html"

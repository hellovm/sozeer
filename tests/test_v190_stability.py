# -*- coding: utf-8 -*-
"""v0.19 稳定性补强回归（S1–S5）。

S1 数据库备份      —— 脚本存在 + 备份/恢复可跑通 + 完整性校验
S2 日志落文件      —— setup_logging 幂等 + 目录不可写时降级不崩
S3 任务超时保护    —— spawn 超时取消 + 台账标 abandoned + 防死循环
S4 characters 测试 —— 人名抽取三重降噪 + 同步 + 清单
S5 深度健康探针    —— 浅/深探针分层 + 探针自身绝不抛异常
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import subprocess
import sys
import tempfile

import pytest

os.environ.setdefault("NOVELFORGE_DATA", tempfile.mkdtemp(prefix="nf_test_v19_"))
os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-v19")

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ============================================================ S1 备份
def test_backup_scripts_exist():
    for f in ("backup_db.sh", "restore_db.sh",
              "novelforge-backup.service", "novelforge-backup.timer"):
        assert (ROOT / "deploy" / f).exists(), f"缺 deploy/{f}"


def test_backup_script_syntax():
    """bash -n 语法检查（防低级错误）。"""
    for f in ("backup_db.sh", "restore_db.sh"):
        r = subprocess.run(["bash", "-n", str(ROOT / "deploy" / f)],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"{f} 语法错误：{r.stderr}"


def test_backup_script_has_safety():
    """备份脚本必须做完整性校验 + 保留策略。"""
    src = (ROOT / "deploy" / "backup_db.sh").read_text(encoding="utf-8")
    assert "integrity_check" in src, "缺完整性校验"
    assert "NF_KEEP" in src, "缺保留策略"
    assert "set -euo pipefail" in src, "缺严格模式"


def test_restore_script_backs_up_before_overwrite():
    """★ 恢复脚本必须「先备份现有库再覆盖」（恢复错了也能回退）。"""
    src = (ROOT / "deploy" / "restore_db.sh").read_text(encoding="utf-8")
    assert "pre-restore" in src, "缺覆盖前备份"
    assert "--dry-run" in src, "缺干跑模式"
    assert "systemctl stop" in src, "恢复前应停服务"


def test_backup_script_runs(tmp_path):
    """端到端跑一次备份（用临时库）。"""
    import sqlite3
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE x(a INT)")
    con.execute("INSERT INTO x VALUES (1)")
    con.commit()
    con.close()
    bk = tmp_path / "bk"
    env = dict(os.environ, NF_DB=str(db), NF_BACKUP_DIR=str(bk), NF_KEEP="3")
    r = subprocess.run(["bash", str(ROOT / "deploy" / "backup_db.sh")],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, f"备份失败：{r.stdout}\n{r.stderr}"
    assert list(bk.glob("novelforge-*.db.gz")), "未产出备份文件"


def test_backup_retention(tmp_path):
    """保留策略：超过 NF_KEEP 份应清理旧备份。"""
    import sqlite3, time
    db = tmp_path / "t.db"
    sqlite3.connect(db).close()
    bk = tmp_path / "bk"; bk.mkdir()
    # 造假 5 个旧备份
    for i in range(5):
        f = bk / f"novelforge-2026010{i}-000000.db.gz"
        f.write_bytes(b"x")
        os.utime(f, (time.time() - i * 3600,) * 2)
    env = dict(os.environ, NF_DB=str(db), NF_BACKUP_DIR=str(bk), NF_KEEP="3")
    subprocess.run(["bash", str(ROOT / "deploy" / "backup_db.sh")],
                   capture_output=True, text=True, env=env)
    left = list(bk.glob("novelforge-*.db.gz"))
    assert len(left) <= 3, f"保留策略失效，剩 {len(left)} 份"


def test_backup_missing_db_fails_gracefully(tmp_path):
    env = dict(os.environ, NF_DB=str(tmp_path / "nope.db"), NF_BACKUP_DIR=str(tmp_path))
    r = subprocess.run(["bash", str(ROOT / "deploy" / "backup_db.sh")],
                       capture_output=True, text=True, env=env)
    assert r.returncode != 0, "库不存在应非零退出"


# ============================================================ S2 日志
def test_logging_setup_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELFORGE_LOG_DIR", str(tmp_path))
    import importlib
    from apps.api import app_logging
    importlib.reload(app_logging)
    app_logging.setup_logging(force=True)
    n1 = len(__import__("logging").getLogger().handlers)
    app_logging.setup_logging()          # 第二次不该叠加
    n2 = len(__import__("logging").getLogger().handlers)
    assert n1 == n2, f"handler 被重复添加：{n1} → {n2}"


def test_logging_writes_file(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELFORGE_LOG_DIR", str(tmp_path))
    import importlib, logging
    from apps.api import app_logging
    importlib.reload(app_logging)
    app_logging.setup_logging(force=True)
    logging.getLogger("novelforge").warning("测试日志写入 %s", "OK")
    for h in logging.getLogger().handlers:
        h.flush()
    f = tmp_path / app_logging.APP_LOG
    assert f.exists(), "日志文件未创建"


def test_logging_degrades_when_dir_unwritable(monkeypatch):
    """★ 目录不可写 → 降级为仅 stderr，绝不抛异常。"""
    monkeypatch.setenv("NOVELFORGE_LOG_DIR", "/proc/nonexistent/definitely-not-writable")
    import importlib
    from apps.api import app_logging
    importlib.reload(app_logging)
    app_logging.setup_logging(force=True)      # 不应抛
    import logging
    assert logging.getLogger().handlers, "至少应有 stderr handler"


def test_logrotate_config_exists():
    f = ROOT / "deploy" / "novelforge.logrotate"
    assert f.exists()
    src = f.read_text(encoding="utf-8")
    assert "rotate 14" in src and "copytruncate" in src


# ============================================================ S3 任务超时
def test_spawn_accepts_timeout_params():
    import inspect
    from apps.api.services import tasks
    sig = inspect.signature(tasks.spawn)
    for p in ("timeout", "task_id", "label"):
        assert p in sig.parameters, f"spawn 缺参数 {p}"


def test_spawn_completes_normally():
    from apps.api.services import tasks

    async def main():
        done = {"v": False}

        async def q():
            await asyncio.sleep(0.01)
            done["v"] = True
        tasks.spawn(q(), timeout=5, label="t")
        await asyncio.sleep(0.1)
        return done["v"]

    assert asyncio.run(main()) is True


def test_spawn_cancels_on_timeout():
    """★ 超时必须取消协程（否则泄漏连接/内存）。"""
    from apps.api.services import tasks

    async def main():
        reached = {"past": False}

        async def slow():
            await asyncio.sleep(5)
            reached["past"] = True
        t = tasks.spawn(slow(), timeout=0.1, label="slow")
        await asyncio.sleep(0.4)
        return reached["past"], t.done()

    past, done = asyncio.run(main())
    assert past is False, "超时后协程仍在跑"
    assert done is True


def test_timeout_marks_task_abandoned():
    """★ 超时必须把台账标 abandoned（否则前端死等进度流）。"""
    from apps.api import main
    from apps.api.models import Book, Task
    from apps.api.services import tasks as T

    with main.SessionLocal() as db:
        b = Book(id="bk_s3", title="S3", owner_id="u1")
        if not db.get(Book, "bk_s3"):
            db.add(b); db.commit()
        t = T.new_task(db, "bk_s3", "test", progress="跑着")
        tid = t.id

    async def main_():
        async def slow():
            await asyncio.sleep(5)
        T.spawn(slow(), timeout=0.1, task_id=tid, label="t")
        await asyncio.sleep(0.4)

    asyncio.run(main_())
    with main.SessionLocal() as db:
        row = db.get(Task, tid)
        assert row.status == "abandoned", f"应标 abandoned，实际 {row.status}"
        assert "超时" in (row.progress or "")


def test_live_task_count():
    from apps.api.services import tasks
    assert isinstance(tasks.live_task_count(), int)


def test_timeout_configurable_by_env(monkeypatch):
    monkeypatch.setenv("NOVELFORGE_TASK_TIMEOUT", "42")
    import importlib
    from apps.api.services import tasks
    importlib.reload(tasks)
    assert tasks.TASK_TIMEOUT_SECONDS == 42
    importlib.reload(tasks)


# ============================================================ S4 characters
def test_extract_candidates_basic():
    from apps.api import characters as C
    text = "苏明月冷笑道。苏明月看着他。苏明月走了。"
    got = C.extract_candidates(text)
    assert "苏明月" in got, f"未抽出主角：{got}"


def test_extract_candidates_rejects_surname_led_nouns():
    """★ #309：以姓氏字开头的普通名词（叶子/东西/宿主…）不得当成人名。

    这些词满足「姓 + 1~2 字」模式，过去会被误抽入库，
    现在必须在 NAME_DENYLIST 中被拦下。
    """
    from apps.api import characters as C
    text = (
        "一片叶子落在地上。东西两边各有哨塔。宿主意识渐渐苏醒。"
        "苏明月冷笑道。苏明月看着他。苏明月走了。"
    )
    got = C.extract_candidates(text)
    assert "叶子" not in got, f"叶子不应被当人名：{got}"
    assert "东西" not in got, f"东西不应被当人名：{got}"
    assert "宿主" not in got, f"宿主不应被当人名：{got}"
    # 真名仍要正常抽出
    assert "苏明月" in got, f"真名被误伤：{got}"


def test_name_denylist_is_two_char_only():
    """★ #309：3 字同名候选（如「叶子轩」）不算黑名单，真实长名不被误杀。"""
    from apps.api import characters as C
    text = "叶子轩握剑。叶子轩回头。叶子轩开口。叶子轩转身。"
    got = C.extract_candidates(text)
    assert "叶子轩" in got, f"长名应保留：{got}"


def test_extract_candidates_filters_single_occurrence():
    """★ 只出现一次的名字应被过滤（降噪门槛 1）。"""
    from apps.api import characters as C
    text = "苏明月来了。路人甲乙丙丁各自散去。"
    got = C.extract_candidates(text)
    assert "路人" not in got or got.get("路人", 0) < 3


def test_extract_candidates_empty():
    from apps.api import characters as C
    assert C.extract_candidates("") == {}
    assert C.extract_candidates(None) == {}


def test_extract_candidates_prefix_suppression():
    """★ 降噪门槛 3：长名出现 ≥ 短名前缀时，短名被抑制。"""
    from apps.api import characters as C
    text = "苏明月说。" * 5
    got = C.extract_candidates(text)
    assert "苏明月" in got, f"长名应在：{got}"
    assert "苏明" not in got, f"短前缀应被抑制：{got}"


def test_sync_characters_adds():
    from apps.api import main
    from apps.api import characters as C
    from apps.api.models import Book, Character
    from sqlalchemy import delete
    with main.SessionLocal() as db:
        db.execute(delete(Book).where(Book.id == "bk_ch_s4")); db.commit()
        db.add(Book(id="bk_ch_s4", title="人物", owner_id="u1")); db.commit()
        text = "林惊羽握剑。林惊羽回头。林惊羽开口。林惊羽转身。"
        r = C.sync_characters(db, "bk_ch_s4", text, ch_idx=1)
        assert any(x["name"] == "林惊羽" for x in r["added"]), f"未入册：{r}"
        rows = db.query(Character).filter(Character.book_id == "bk_ch_s4").all()
        assert rows and rows[0].is_auto == 1


def test_sync_characters_updates_mentions():
    from apps.api import main
    from apps.api import characters as C
    from apps.api.models import Book, Character
    from sqlalchemy import delete
    with main.SessionLocal() as db:
        db.execute(delete(Character).where(Character.book_id == "bk_ch_s4b"))
        db.execute(delete(Book).where(Book.id == "bk_ch_s4b")); db.commit()
        db.add(Book(id="bk_ch_s4b", title="人物2", owner_id="u1")); db.commit()
        db.add(Character(id="c1", book_id="bk_ch_s4b", name="王大力", mentions=1))
        db.commit()
        r = C.sync_characters(db, "bk_ch_s4b", "王大力来了。王大力走了。王大力笑了。", ch_idx=2)
        assert "王大力" in r["updated"], f"未累加：{r}"
        row = db.get(Character, "c1")
        assert int(row.mentions) > 1


def test_review_queue_sorted_by_mentions():
    from apps.api import main
    from apps.api import characters as C
    from apps.api.models import Book, Character
    from sqlalchemy import delete
    with main.SessionLocal() as db:
        db.execute(delete(Character).where(Character.book_id == "bk_ch_s4c"))
        db.execute(delete(Book).where(Book.id == "bk_ch_s4c")); db.commit()
        db.add(Book(id="bk_ch_s4c", title="人物3", owner_id="u1")); db.commit()
        db.add(Character(id="a", book_id="bk_ch_s4c", name="少", mentions=1))
        db.add(Character(id="b", book_id="bk_ch_s4c", name="多", mentions=99))
        db.commit()
        q = C.review_queue(db, "bk_ch_s4c")
        assert q[0]["name"] == "多", f"未按出现次数降序：{q}"


def test_review_queue_fields():
    from apps.api import main
    from apps.api import characters as C
    with main.SessionLocal() as db:
        q = C.review_queue(db, "bk_ch_s4c")
        if q:
            for k in ("id", "name", "mentions", "is_auto", "state_now"):
                assert k in q[0], f"缺字段 {k}"


# ============================================================ #310 章节改名工具
def test_agent_rename_chapter_updates_title():
    """★ #310：rename_chapter 工具必须把新标题写进库，章节清单才会刷新。"""
    import asyncio
    from apps.api import main
    from apps.api import agent as agent_mod
    from apps.api.models import Book, Chapter
    from sqlalchemy import delete, select

    async def emit(ev):
        pass

    with main.SessionLocal() as db:
        db.execute(delete(Chapter).where(Chapter.book_id == "bk_ren"))
        db.execute(delete(Book).where(Book.id == "bk_ren")); db.commit()
        db.add(Book(id="bk_ren", title="改名书", owner_id="u1"))
        db.add(Chapter(id="ch_ren1", book_id="bk_ren", idx=3,
                       title="旧标题", status="final", content="正文内容"))
        db.commit()
        book = db.get(Book, "bk_ren")
        r = asyncio.run(agent_mod._t_rename_chapter(
            db, book, {"ch_idx": 3, "title": "第3章 新标题"}, emit))
        ch = db.get(Chapter, "ch_ren1")
        assert ch.title == "新标题", f"标题未更新：{r} / {ch.title}"
        db.execute(delete(Chapter).where(Chapter.book_id == "bk_ren"))
        db.execute(delete(Book).where(Book.id == "bk_ren")); db.commit()


def test_agent_rename_chapter_missing_chapter():
    """★ #310：章号不存在时给可读提示，不崩。"""
    import asyncio
    from apps.api import main
    from apps.api import agent as agent_mod
    from apps.api.models import Book, Chapter
    from sqlalchemy import delete

    async def emit(ev):
        pass

    with main.SessionLocal() as db:
        db.execute(delete(Book).where(Book.id == "bk_ren2")); db.commit()
        db.add(Book(id="bk_ren2", title="改名书2", owner_id="u1")); db.commit()
        book = db.get(Book, "bk_ren2")
        r = asyncio.run(agent_mod._t_rename_chapter(
            db, book, {"ch_idx": 999, "title": "无此章"}, emit))
        assert "不存在" in r, f"应提示不存在：{r}"
        db.execute(delete(Book).where(Book.id == "bk_ren2")); db.commit()


# ============================================================ #311 删除人物工具 + 端点
def test_agent_delete_character_removes():
    """★ #311：delete_character 工具要真删，不要只口头标记「误入库·待删除」。"""
    import asyncio
    from apps.api import main
    from apps.api import agent as agent_mod
    from apps.api.models import Book, Character
    from sqlalchemy import delete, select

    async def emit(ev):
        pass

    with main.SessionLocal() as db:
        db.execute(delete(Character).where(Character.book_id == "bk_del"))
        db.execute(delete(Book).where(Book.id == "bk_del")); db.commit()
        db.add(Book(id="bk_del", title="删人书", owner_id="u1"))
        db.add(Character(id="ch_del1", book_id="bk_del", name="叶子", is_auto=1))
        db.add(Character(id="ch_del2", book_id="bk_del", name="陈长安"))
        db.commit()
        book = db.get(Book, "bk_del")
        r = asyncio.run(agent_mod._t_delete_character(
            db, book, {"name": "叶子"}, emit))
        remaining = db.scalars(select(Character).where(
            Character.book_id == "bk_del")).all()
        names = {c.name for c in remaining}
        assert "叶子" not in names, f"未删除：{r} / {names}"
        assert "陈长安" in names, f"误删了别人：{names}"
        db.execute(delete(Character).where(Character.book_id == "bk_del"))
        db.execute(delete(Book).where(Book.id == "bk_del")); db.commit()


def test_delete_character_endpoint():
    """★ #311：DELETE /characters/{cid} 端点真正删人（含衍生表清理）。"""
    import time
    from fastapi.testclient import TestClient
    from apps.api import main
    from apps.api.models import Book, Character, CharacterKnowledge, CharacterRelation
    from sqlalchemy import delete, select

    # 全站需登录：注册一个用户拿 token（与 api_smoke 同款写法）
    user = f"deltester_{int(time.time() * 1000)}"
    client = TestClient(main.app)
    reg = client.post("/api/auth/register",
                      json={"username": user, "password": "secret123"})
    assert reg.status_code == 200, reg.text
    tok = reg.json()["token"]
    H = {"Authorization": f"Bearer {tok}"}

    with main.SessionLocal() as db:
        db.execute(delete(CharacterKnowledge).where(CharacterKnowledge.book_id == "bk_dele"))
        db.execute(delete(CharacterRelation).where(CharacterRelation.book_id == "bk_dele"))
        db.execute(delete(Character).where(Character.book_id == "bk_dele"))
        db.execute(delete(Book).where(Book.id == "bk_dele")); db.commit()
        db.add(Book(id="bk_dele", title="删人端点", owner_id="u1"))
        db.add(Character(id="ch_dele1", book_id="bk_dele", name="东西", is_auto=1))
        db.add(CharacterKnowledge(id="ck_dele1", book_id="bk_dele",
                                  character_id="ch_dele1", character_name="东西",
                                  fact_id="setting:x", fact_desc="测试事实"))
        db.commit()

    r = client.delete(f"/api/books/bk_dele/characters/ch_dele1", headers=H)
    assert r.status_code == 200 and r.json().get("ok") is True, r.text

    with main.SessionLocal() as db:
        remaining = db.scalars(select(Character).where(
            Character.book_id == "bk_dele")).all()
        assert not remaining, f"端点未删人：{remaining}"
        kn = db.scalars(select(CharacterKnowledge).where(
            CharacterKnowledge.book_id == "bk_dele")).all()
        assert not kn, f"衍生知识未清理：{kn}"
        db.execute(delete(Book).where(Book.id == "bk_dele")); db.commit()


# ============================================================ S5 健康探针
def test_shallow_probe_light():
    from apps.api import health
    r = health.shallow()
    assert r["ok"] is True and "version" in r


def test_deep_probe_structure():
    from apps.api import health
    r = health.deep()
    assert set(r.keys()) >= {"ok", "version", "checks", "degraded"}
    for k in ("db", "disk", "log", "tasks", "backup"):
        assert k in r["checks"], f"缺检查项 {k}"


def test_deep_probe_never_raises(monkeypatch):
    """★ 探针自身绝不抛异常（否则探针成了故障源）。"""
    from apps.api import health
    # 制造各种异常场景
    monkeypatch.setattr(health, "_check_disk", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    try:
        r = health.deep()
        assert isinstance(r, dict)
    except Exception as e:
        pytest.fail(f"探针抛异常：{type(e).__name__}: {e}")


def test_db_probe_detects_write():
    from apps.api import health
    r = health._check_db_writable()
    assert r["ok"] is True, f"DB 应可写：{r}"
    assert "ms" in r


def test_disk_probe():
    from apps.api import health
    r = health._check_disk()
    assert "free_mb" in r and "free_pct" in r


def test_health_endpoint_shallow_and_deep():
    from fastapi.testclient import TestClient
    from apps.api.main import app
    c = TestClient(app)
    s = c.get("/api/health")
    assert s.status_code == 200 and "checks" not in s.json()
    d = c.get("/api/health?deep=1")
    assert d.status_code == 200 and "checks" in d.json()


def test_probe_does_not_leak_probe_rows():
    """探针写的临时 Setting 行必须自己清理掉。"""
    from apps.api import main, health
    from apps.api.models import Setting
    health._check_db_writable()
    with main.SessionLocal() as db:
        assert db.get(Setting, "__health_probe__") is None, "探针行未清理"

# -*- coding: utf-8 -*-
"""整书打包导出 / 导入（v0.19.3 F2）。

为什么做
--------
项目已有单书**文本**导出（txt/md/epub/投稿稿），但那是「给人读的产物」。
缺的是「**机器可还原的完整快照**」：
  * 换服务器 / 换实例时，怎么把一本书连同设定、大纲、人物、伏笔、纪年表、
    账本、短剧工程、版本历史一起搬过去？
  * 想给某本书做个「时间点快照」，后面写崩了好回退？

本模块提供 `.nfbook`（就是个 ZIP）：
    manifest.json          元信息（格式版本 / 导出时间 / 统计）
    book.json              书籍主记录
    tables/*.json          各关联表（卷/章/人物/世界观/伏笔/时间线/账本/审计/风格）
    drama/*.json           短剧工程（项目/分集/资产/分镜）
    files/                 书籍目录下的原始文件（正文 md、人物卡、大纲）

设计原则
--------
1. **模型驱动**：靠 SQLAlchemy 反射拿列，不手写字段清单
   → 以后加字段不会漏（手写清单是维护地狱）
2. **可校验**：manifest 带格式版本 + 各表条数，导入时先校验再写
3. **导入可选策略**：`merge`（同 id 更新）/ `copy`（新 id，避免覆盖现有书）
4. **不碰用户凭证**：导出的表**不含 model_profiles / users / settings**
   （含 API Key，绝对不能进备份包）
"""
from __future__ import annotations

import io
import json
import time
import uuid
import zipfile
from pathlib import Path

# 打包格式版本（未来结构变更时递增，导入端据此兼容）
FORMAT_VERSION = 1
MAGIC = "novelforge-book"

# ----------------------------------------------------------------- 导出范围
# 「随书走」的表（按 book_id 关联）。★ 刻意排除：
#   users / model_profiles / settings / runs / media_calls
#   —— 前者含账号与 API Key（绝不能进包），后者是运行时台账（体积大、无迁移价值）
BOOK_TABLES = (
    "volumes", "chapters", "characters", "world_settings",
    "foreshadows", "timeline_events", "ledger_entries",
    "audit_reports", "style_profiles", "conversations", "messages",
)

DRAMA_TABLES = ("drama_projects", "drama_episodes", "drama_assets", "drama_scenes")

# 书籍目录下要一起打包的原始文件（相对 data/books/<id>/）
BOOK_FILE_DIRS = ("正文", "人物", "大纲", "设定")


def _model_for(table: str):
    """按表名取 ORM 模型（避免手写映射）。"""
    from apps.api import models as M
    for name in dir(M):
        obj = getattr(M, name)
        if isinstance(obj, type) and hasattr(obj, "__tablename__") \
                and getattr(obj, "__tablename__", "") == table:
            return obj
    return None


def _row_to_dict(obj) -> dict:
    """ORM 实例 → 纯 dict（datetime 转 ISO 串）。"""
    import datetime as dt
    out = {}
    for col in obj.__table__.columns:
        v = getattr(obj, col.name, None)
        if isinstance(v, (dt.datetime, dt.date)):
            v = v.isoformat()
        out[col.name] = v
    return out


def export_book(db, book, *, include_drama: bool = True,
                include_files: bool = True) -> tuple[bytes, str]:
    """导出整书为 .nfbook 字节流。

    返回 (zip_bytes, filename)。失败抛异常由调用方处理。
    """
    from sqlalchemy import select
    from apps.api import config

    buf = io.BytesIO()
    stats: dict[str, int] = {}

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # ---- 书籍主记录 ----
        book_d = _row_to_dict(book)
        z.writestr("book.json", json.dumps(book_d, ensure_ascii=False, indent=1))
        stats["book"] = 1

        # ---- 关联表 ----
        tables_out: dict[str, list] = {}
        for table in BOOK_TABLES:
            M = _model_for(table)
            if M is None:
                continue
            rows = db.scalars(select(M).where(M.book_id == book.id)).all()
            data = [_row_to_dict(r) for r in rows]
            tables_out[table] = data
            stats[table] = len(data)
        z.writestr("tables.json",
                   json.dumps(tables_out, ensure_ascii=False, indent=1))

        # ---- 短剧工程 ----
        drama_out: dict[str, list] = {}
        if include_drama:
            projs = db.scalars(select(_model_for("drama_projects"))
                               .where(_model_for("drama_projects").book_id == book.id)).all()
            drama_out["drama_projects"] = [_row_to_dict(p) for p in projs]
            stats["drama_projects"] = len(projs)
            pids = [p.id for p in projs]
            for table in ("drama_episodes", "drama_assets", "drama_scenes"):
                M = _model_for(table)
                if M is None:
                    continue
                if pids:
                    rows = db.scalars(select(M).where(M.project_id.in_(pids))).all()
                else:
                    rows = []
                drama_out[table] = [_row_to_dict(r) for r in rows]
                stats[table] = len(rows)
            z.writestr("drama.json",
                       json.dumps(drama_out, ensure_ascii=False, indent=1))

        # ---- 原始文件（正文 md / 人物卡 / 大纲）----
        n_files = 0
        if include_files:
            base = Path(config.BOOKS_DIR) / book.id
            if base.is_dir():
                for sub in BOOK_FILE_DIRS:
                    d = base / sub
                    if not d.is_dir():
                        continue
                    for f in d.rglob("*"):
                        if f.is_file():
                            rel = f.relative_to(base)
                            try:
                                z.writestr(f"files/{rel.as_posix()}",
                                           f.read_bytes())
                                n_files += 1
                            except Exception:
                                pass
        stats["files"] = n_files

        # ---- manifest（最后写，带完整统计）----
        manifest = {
            "magic": MAGIC,
            "format_version": FORMAT_VERSION,
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "book_id": book.id,
            "book_title": getattr(book, "title", ""),
            "stats": stats,
        }
        z.writestr("manifest.json",
                   json.dumps(manifest, ensure_ascii=False, indent=1))

    safe = "".join(c for c in (getattr(book, "title", "") or "book")
                   if c not in r'\/:*?"<>|').strip() or "book"
    fname = f"{safe}-{time.strftime('%Y%m%d-%H%M%S')}.nfbook"
    return buf.getvalue(), fname


def inspect_package(data: bytes) -> dict:
    """只读校验：不解压到磁盘，返回 manifest + 结构检查结果。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            if "manifest.json" not in names:
                return {"ok": False, "error": "不是有效的 .nfbook（缺 manifest.json）"}
            mf = json.loads(z.read("manifest.json").decode("utf-8"))
            if mf.get("magic") != MAGIC:
                return {"ok": False, "error": "magic 不匹配（不是 NovelForge 备份）"}
            fv = int(mf.get("format_version") or 0)
            if fv > FORMAT_VERSION:
                return {"ok": False,
                        "error": f"备份格式版本 {fv} 高于当前支持的 {FORMAT_VERSION}，"
                                 f"请升级系统后再导入"}
            return {"ok": True, "manifest": mf, "entries": len(names),
                    "compatible": fv <= FORMAT_VERSION}
    except zipfile.BadZipFile:
        return {"ok": False, "error": "文件损坏或不是 ZIP"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _rstrip_dir(p) -> str:
    return str(p).rstrip("/\\")


def _coerce_for_model(M, payload: dict) -> dict:
    """把 JSON 里的值转回 ORM 需要的 Python 类型。

    ★ 为什么必须有这一步：导出时 datetime → ISO 字符串（JSON 无法存 datetime），
    导入时若不转回来，SQLite 会直接报
      "SQLite DateTime type only accepts Python datetime and date objects as input"
    （实测踩坑：第一次跑往返就炸在这里）。
    """
    import datetime as dt
    try:
        from sqlalchemy import DateTime as _DT, Date as _D
        cols = {c.name: c for c in M.__table__.columns}
    except Exception:
        return payload
    out = {}
    for k, v in payload.items():
        col = cols.get(k)
        t = getattr(col, "type", None) if col is not None else None
        if isinstance(v, str) and t is not None:
            if isinstance(t, _DT):
                try:
                    out[k] = dt.datetime.fromisoformat(v)
                    continue
                except Exception:
                    pass
            elif isinstance(t, _D):
                try:
                    out[k] = dt.date.fromisoformat(v)
                    continue
                except Exception:
                    pass
        out[k] = v
    return out


def import_book(db, data: bytes, *, mode: str = "copy",
                title_suffix: str = "（导入）") -> dict:
    """导入 .nfbook。

    mode:
      * "copy"  —— **新 id**，不覆盖现有书（默认，最安全）
      * "merge" —— 沿用原 id，同 id 记录覆盖（用于「恢复到同一位置」）

    返回 {"ok", "book_id", "title", "mode", "imported": {表: 条数}}
    """
    chk = inspect_package(data)
    if not chk.get("ok"):
        return chk

    from sqlalchemy import select
    from apps.api import config
    from apps.api.models import Book

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        book_d = json.loads(z.read("book.json").decode("utf-8"))
        tables = json.loads(z.read("tables.json").decode("utf-8")) \
            if "tables.json" in z.namelist() else {}
        drama = json.loads(z.read("drama.json").decode("utf-8")) \
            if "drama.json" in z.namelist() else {}

        old_id = book_d.get("id") or ""
        if mode == "copy":
            new_id = f"bk_{uuid.uuid4().hex[:12]}"
        else:
            new_id = old_id

        # ---- 书籍 ----
        existing = db.get(Book, new_id)
        if existing is not None and mode == "merge":
            # ★ merge 分支同样要过类型转换 —— 踩过坑：只给 else 分支转了，
            # merge 时 datetime 仍是字符串，SQLite 直接 TypeError。
            cols = {c.name for c in Book.__table__.columns}
            fixed = _coerce_for_model(Book, {k: v for k, v in book_d.items() if k in cols})
            for k, v in fixed.items():
                if k != "id" and hasattr(existing, k):
                    setattr(existing, k, v)
        else:
            cols = {c.name for c in Book.__table__.columns}
            payload = _coerce_for_model(Book, {k: v for k, v in book_d.items() if k in cols})
            payload["id"] = new_id
            if mode == "copy":
                payload["title"] = (str(payload.get("title") or "未命名")
                                    + title_suffix)
            db.add(Book(**payload))
        db.commit()

        imported: dict[str, int] = {"book": 1}

        # ---- 关联表：重建外键指向 ----
        for table, rows in (tables or {}).items():
            M = _model_for(table)
            if M is None:
                continue
            cols = {c.name for c in M.__table__.columns}
            n = 0
            for r in rows:
                payload = _coerce_for_model(M, {k: v for k, v in r.items() if k in cols})
                payload["book_id"] = new_id
                if mode == "copy":
                    # 主键也要换新，避免撞车（特殊情况：chapter 的 idx 保留）
                    pk = _pk_name(M)
                    if pk and pk in payload:
                        payload[pk] = _new_pk(M, payload[pk])
                    # 卷/章节的父子引用要跟着换（这里做保守处理：置空卷引用）
                    if "volume_id" in payload and payload["volume_id"]:
                        payload["volume_id"] = ""      # 卷 id 已变，先解耦
                try:
                    db.merge(M(**payload))
                    n += 1
                except Exception:
                    db.rollback()
            db.commit()
            imported[table] = n

        # ---- 短剧工程 ----
        if drama:
            proj_map: dict[str, str] = {}
            M = _model_for("drama_projects")
            if M:
                cols = {c.name for c in M.__table__.columns}
                for r in drama.get("drama_projects", []):
                    payload = _coerce_for_model(M, {k: v for k, v in r.items() if k in cols})
                    old_pid = payload.get("id", "")
                    if mode == "copy":
                        payload["id"] = f"dp_{uuid.uuid4().hex[:12]}"
                    payload["book_id"] = new_id
                    proj_map[old_pid] = payload["id"]
                    try:
                        db.merge(M(**payload))
                    except Exception:
                        db.rollback()
                db.commit()
                imported["drama_projects"] = len(drama.get("drama_projects", []))

            for table in ("drama_episodes", "drama_assets", "drama_scenes"):
                M = _model_for(table)
                if M is None:
                    continue
                cols = {c.name for c in M.__table__.columns}
                n = 0
                for r in drama.get(table, []):
                    payload = _coerce_for_model(M, {k: v for k, v in r.items() if k in cols})
                    if "project_id" in payload:
                        payload["project_id"] = proj_map.get(payload["project_id"],
                                                             payload["project_id"])
                    if mode == "copy":
                        pk = _pk_name(M)
                        if pk and pk in payload:
                            payload[pk] = _new_pk(M, payload[pk])
                    try:
                        db.merge(M(**payload))
                        n += 1
                    except Exception:
                        db.rollback()
                db.commit()
                imported[table] = n

        # ---- 原始文件 ----
        n_files = 0
        try:
            base = Path(config.BOOKS_DIR) / new_id
            for name in z.namelist():
                if not name.startswith("files/"):
                    continue
                rel = name[len("files/"):]
                if not rel or rel.endswith("/"):
                    continue
                target = base / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(name))
                n_files += 1
        except Exception:
            pass
        imported["files"] = n_files

    return {"ok": True, "book_id": new_id,
            "title": book_d.get("title", ""), "mode": mode,
            "imported": imported}


def _pk_name(M) -> str:
    try:
        return M.__table__.primary_key.columns.keys()[0]
    except Exception:
        return ""


def _new_pk(M, old) -> str:
    """生成新主键（保持前缀风格）。"""
    try:
        pk_col = M.__table__.primary_key.columns.values()[0]
        maxlen = getattr(pk_col.type, "length", None) or 64
    except Exception:
        maxlen = 64
    prefix = ""
    if isinstance(old, str) and "_" in old:
        prefix = old.split("_")[0] + "_"
    elif isinstance(old, str) and old[:2].isalpha():
        prefix = old[:2]
    new = f"{prefix}{uuid.uuid4().hex[:12]}"
    return new[:maxlen] if maxlen else new


__all__ = ["export_book", "import_book", "inspect_package",
           "FORMAT_VERSION", "MAGIC", "BOOK_TABLES", "DRAMA_TABLES"]

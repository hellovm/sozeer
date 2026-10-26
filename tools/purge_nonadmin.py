"""生产环境清理：删除所有非管理员用户及其全部业务数据。

★ 安全第一（项目铁律 R6：破坏性操作需先备份）：
  - 默认只做 SELECT 统计（--dry-run），不改动任何数据。
  - --execute 必须显式加 --yes，且执行前自动 pg_dump 全量备份（除非 --no-backup）。
  - 保留管理员的审计/合规记录（admin_audit_logs / setting_history / content_flags），
    这些不是"用户的数据"，属平台治理留痕。

★ 级联说明：全库无外键，删除必须应用层手工完成。本脚本复刻
  main.py:delete_book / drama_delete_project 的级联范围并补齐缺失子表。

用法（在生产服务器 /opt/novelforge/src 下，用 .venv 运行）：
  python tools/purge_nonadmin.py --dry-run            # 只读预览
  python tools/purge_nonadmin.py --execute --yes      # 备份后真正删除
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# 必须在导入 apps.api.* 之前把生产 .env 注入环境（config/db 在 import 时读取）
ENV_FILE = Path(os.environ.get("NF_ENV_FILE", "/opt/novelforge/.env"))


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        # 去掉可能的引号（dotenv 与 source 两套解析器差异，这里统一去引号）
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        os.environ.setdefault(k, v)


load_dotenv(ENV_FILE)

# 脚本可能放在 /tmp 运行，需把生产源码目录加入导入路径
sys.path.insert(0, os.environ.get("NF_SRC", "/opt/novelforge/src"))

from sqlalchemy import delete, make_url, select  # noqa: E402

from apps.api.config import BOOKS_DIR, DB_URL  # noqa: E402
from apps.api.db import SessionLocal, engine  # noqa: E402
import apps.api.models as M  # noqa: E402

KEEP_ROLES = ("admin", "owner")


def _book_children():
    """(模型类, 关联列) —— 通过 book_id 间接归属用户的子表。"""
    return [
        (M.Chapter, "book_id"), (M.Volume, "book_id"), (M.Character, "book_id"),
        (M.WorldSetting, "book_id"), (M.Foreshadow, "book_id"), (M.TimelineEvent, "book_id"),
        (M.LedgerEntry, "book_id"), (M.AuditReport, "book_id"), (M.StyleProfile, "book_id"),
        (M.Run, "book_id"), (M.Conversation, "book_id"), (M.Message, "book_id"),
        (M.MemoryChunk, "book_id"), (M.DramaTask, "book_id"), (M.MediaCall, "book_id"),
        (M.CharacterRelation, "book_id"), (M.ChapterEmotion, "book_id"),
        (M.CharacterKnowledge, "book_id"), (M.BookScene, "book_id"),
    ]


def _user_tables():
    """(模型类, user_id 列) —— 通过 user_id 直接归属用户的表（财务凭证一并硬删）。"""
    return [
        (M.CreditAccount, "user_id"), (M.CreditLedger, "user_id"), (M.Invoice, "user_id"),
        (M.LoginRecord, "user_id"), (M.AccountDeletion, "user_id"), (M.RefreshToken, "user_id"),
        (M.LLMCall, "user_id"), (M.UserFeedback, "user_id"), (M.Notification, "user_id"),
        (M.NotificationPref, "user_id"),
    ]


def _count(s, cls, col, val):
    from sqlalchemy import func
    return s.execute(select(func.count()).select_from(cls).where(getattr(cls, col) == val)).scalar_one()


def _purge_drama(s, dp):
    pid = dp.id
    s.execute(delete(M.DramaEpisode).where(M.DramaEpisode.project_id == pid))
    s.execute(delete(M.DramaAsset).where(M.DramaAsset.project_id == pid))
    s.execute(delete(M.DramaScene).where(M.DramaScene.project_id == pid))
    s.execute(delete(M.DramaTask).where(M.DramaTask.project_id == pid))
    s.delete(dp)


def collect_targets(s):
    users = s.execute(select(M.User).where(M.User.role.notin_(KEEP_ROLES))).scalars().all()
    return users


def dry_run(s):
    users = collect_targets(s)
    print(f"[dry-run] 待删除非管理员用户数: {len(users)}")
    if not users:
        print("[dry-run] 无需清理。")
        return
    total = 0
    for u in users:
        bid = u.id
        books = s.execute(select(M.Book).where(M.Book.owner_id == bid)).scalars().all()
        dproj = s.execute(select(M.DramaProject).where(M.DramaProject.owner_id == bid)).scalars().all()
        orders = s.execute(select(M.Order.id).where(M.Order.user_id == bid)).scalars().all()
        rows = {"user": 1, "books": len(books), "drama_projects": len(dproj), "orders": len(orders)}
        for cls, col in _book_children():
            n = _count(s, cls, col, bid)
            # 注意：book 级子表按 owner 计会重复，这里仅作量级提示，不累加 total
            rows.setdefault(f"child:{cls.__tablename__}", n)
        for cls, col in _user_tables():
            n = _count(s, cls, col, bid)
            rows.setdefault(f"{cls.__tablename__}", n)
            total += n
        total += 1 + len(books) + len(dproj) + len(orders)
        print(f"  - user={bid} role={u.role}: books={len(books)} drama={len(dproj)} orders={len(orders)}")
    # 总量级（财务凭证 + 用户行 + 书/短剧 + 子表按 owner 粗略合计）
    print(f"[dry-run] 估算待删业务行总量级 ≈ {total}（不含 book 子表逐行明细）")
    print("[dry-run] 以上为只读统计，未做任何修改。")


def backup_pg() -> Path | None:
    if not DB_URL.startswith("postgresql"):
        print(f"[backup] 非 PostgreSQL（{DB_URL.split(':',1)[0]}），跳过 pg_dump；请自行备份数据目录。")
        return None
    u = make_url(DB_URL)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path("/opt/novelforge/backups")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"purge_nonadmin_{ts}.dump"
    env = dict(os.environ)
    if u.password:
        env["PGPASSWORD"] = u.password
    cmd = [
        "pg_dump", "-h", str(u.host), "-p", str(u.port or 5432),
        "-U", str(u.username), "-d", str(u.database),
        "-Fc", "-f", str(out),
    ]
    print(f"[backup] pg_dump -> {out}")
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        print(f"[backup] 失败:\n{r.stderr}")
        raise RuntimeError("备份失败，已中止删除（绝不裸删）")
    print(f"[backup] 完成：{out}  （恢复：pg_restore -Fc -d <库> {out}）")
    return out


def execute(s):
    users = collect_targets(s)
    if not users:
        print("[execute] 无需清理。")
        return
    print(f"[execute] 开始清理 {len(users)} 个非管理员用户及其全部数据……")
    for u in users:
        bid = u.id
        # 1) 书籍及其全部子表 + 磁盘目录
        books = s.execute(select(M.Book).where(M.Book.owner_id == bid)).scalars().all()
        for b in books:
            for cls, col in _book_children():
                s.execute(delete(cls).where(getattr(cls, col) == b.id))
            # 该书关联的短剧项目
            for dp in s.execute(select(M.DramaProject).where(M.DramaProject.book_id == b.id)).scalars().all():
                _purge_drama(s, dp)
            s.delete(b)
            d = BOOKS_DIR / b.id
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
        # 2) 直接归属用户的短剧项目（无对应书）
        for dp in s.execute(select(M.DramaProject).where(M.DramaProject.owner_id == bid)).scalars().all():
            _purge_drama(s, dp)
        # 3) 任务
        s.execute(delete(M.Task).where(M.Task.owner_id == bid))
        # 4) 财务凭证：订单 → 支付事件 → 其余按 user_id
        order_ids = s.execute(select(M.Order.id).where(M.Order.user_id == bid)).scalars().all()
        s.execute(delete(M.PaymentEvent).where(M.PaymentEvent.order_id.in_(order_ids)))
        s.execute(delete(M.Order).where(M.Order.user_id == bid))
        for cls, col in _user_tables():
            s.execute(delete(cls).where(getattr(cls, col) == bid))
        # 5) 用户行本身
        s.delete(u)
        s.commit()
        print(f"  [execute] 已清理 user={bid}（含 {len(books)} 本书）")
    print("[execute] 全部完成。管理员审计/合规记录（admin_audit_logs/setting_history/content_flags）已保留。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只读统计待删数据（默认）")
    ap.add_argument("--execute", action="store_true", help="真正删除（需 --yes）")
    ap.add_argument("--yes", action="store_true", help="确认执行删除")
    ap.add_argument("--no-backup", action="store_true", help="跳过 pg_dump（不推荐）")
    args = ap.parse_args()

    if args.execute and not args.yes:
        print("错误：--execute 必须同时传 --yes 以确认不可逆删除。")
        sys.exit(2)
    if not args.dry_run and not args.execute:
        args.dry_run = True

    if args.execute and not args.no_backup:
        backup_pg()

    with SessionLocal() as s:
        if args.dry_run:
            dry_run(s)
        elif args.execute:
            execute(s)


if __name__ == "__main__":
    main()

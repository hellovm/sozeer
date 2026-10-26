#!/usr/bin/env python
"""A1 · SQLite → PostgreSQL 数据搬运工具（v0.20.1）。

用法
----
```bash
# 1) 预演（只读，不改任何数据）—— ★ 强烈建议先跑这个
python tools/migrate_sqlite_to_pg.py --dry-run

# 2) 正式搬运（会清空 PG 目标库后重建）
python tools/migrate_sqlite_to_pg.py --pg-url "postgresql+psycopg://..." --yes

# 3) 只校验（搬运后核对）
python tools/migrate_sqlite_to_pg.py --verify-only --pg-url "..."
```

设计要点（★ 每一条都对应一个真实事故）
--------------------------------------
1. **逐表校验行数**，任何一张表不一致立刻停止（不做"搬完再看"）
2. **自动序列修正**（setval）—— 显式带 id 插入后，PG sequence 不会跟进，
   不修就会在下次 INSERT 报 duplicate key（本方案 R2 风险）
3. **按外键依赖排序**搬运 —— 否则违反约束
4. **dry-run 模式**默认开启安全检查，必须显式 `--yes` 才真的写入
5. **保留 SQLite 原文件不动** —— 回滚的唯一凭据
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 允许直接运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _sqlite_url_of(db_path: Path) -> str:
    return f"sqlite:///{db_path}"


def table_order(models_mod) -> list[str]:
    """按外键依赖排序（被依赖的表在前）。

    用 SQLAlchemy metadata 的 sorted_tables（已做拓扑排序），
    比手写顺序可靠 —— 手写一定会在加新表时忘掉。
    """
    from apps.api.db import Base
    return [t.name for t in Base.metadata.sorted_tables]


def main() -> int:
    ap = argparse.ArgumentParser(description="SQLite → PostgreSQL 数据搬运")
    ap.add_argument("--sqlite", default=None,
                    help="源 SQLite 文件路径（默认取 config.DB_PATH）")
    ap.add_argument("--pg-url", default=None,
                    help="目标 PG URL；不传则读 NOVELFORGE_PG_URL 环境变量")
    ap.add_argument("--dry-run", action="store_true",
                    help="只检查，不写入（默认行为，除非给 --yes）")
    ap.add_argument("--yes", action="store_true", help="确认执行写入")
    ap.add_argument("--verify-only", action="store_true",
                    help="只做行数比对，不搬运")
    ap.add_argument("--batch", type=int, default=1000, help="每批插入行数")
    args = ap.parse_args()

    import os
    from sqlalchemy import create_engine, func, select, text

    # ---- 解析源 ----
    if args.sqlite:
        src_path = Path(args.sqlite)
    else:
        from apps.api.config import DB_PATH
        src_path = Path(DB_PATH)
    if not src_path.exists():
        print(f"✗ 源数据库不存在：{src_path}")
        return 2

    # ---- 解析目标 ----
    pg_url = args.pg_url or os.environ.get("NOVELFORGE_PG_URL", "")
    if not pg_url:
        print("✗ 未提供 --pg-url，也未设置 NOVELFORGE_PG_URL")
        return 2
    if pg_url.startswith("postgresql://"):
        # SQLAlchemy 2.x 需要显式 driver
        pg_url = pg_url.replace("postgresql://", "postgresql+psycopg://", 1)

    do_write = args.yes and not args.dry_run

    print("=" * 66)
    print("A1 · SQLite → PostgreSQL 数据搬运")
    print("=" * 66)
    print(f"源    ：{src_path}")
    print(f"目标  ：{pg_url.split('@')[-1] if '@' in pg_url else pg_url}")
    print(f"模式  ：{'★ 正式写入' if do_write else '只读预演（--dry-run）'}")
    print()

    # ---- 建引擎 ----
    src_engine = create_engine(_sqlite_url_of(src_path), future=True)
    pg_engine = create_engine(pg_url, future=True, pool_pre_ping=True)

    # ---- 连通性 ----
    try:
        with pg_engine.connect() as c:
            ver = c.execute(text("SELECT version()")).scalar()
            print(f"✓ 目标库连通：{str(ver)[:60]}…")
    except Exception as e:
        print(f"✗ 目标库连接失败：{type(e).__name__}: {e}")
        return 2
    print()

    # ---- 建表 ----
    from apps.api import models  # noqa: F401
    from apps.api.db import Base

    order = table_order(models)
    print(f"待搬运表（按外键依赖排序，共 {len(order)} 张）：")
    for i, t in enumerate(order, 1):
        print(f"  {i:2}. {t}")
    print()

    if not args.verify_only and do_write:
        print("→ 在目标库建表（create_all，幂等）…")
        Base.metadata.create_all(pg_engine)
        print("  ✓ 建表完成")
        print()

    # ---- 逐表搬运 ----
    report: list[tuple[str, int, int, str]] = []   # (表, 源行数, 目标行数, 状态)
    t0 = time.time()

    for tname in order:
        table = Base.metadata.tables[tname]
        cols = [c.name for c in table.columns]

        with src_engine.connect() as sc:
            src_n = sc.execute(select(func.count()).select_from(table)).scalar() or 0

        if args.verify_only:
            with pg_engine.connect() as pc:
                try:
                    dst_n = pc.execute(select(func.count()).select_from(table)).scalar() or 0
                except Exception:
                    dst_n = -1
            status = "✓" if src_n == dst_n else "✗"
            report.append((tname, src_n, dst_n, status))
            print(f"  {status} {tname:28} 源={src_n:6}  目标={dst_n:6}")
            continue

        if not do_write:
            # dry-run：只报告将要搬多少
            report.append((tname, src_n, src_n, "预演"))
            print(f"  · {tname:28} 将搬运 {src_n} 行")
            continue

        # 正式搬运
        with src_engine.connect() as sc:
            rows = sc.execute(select(table)).mappings().all()

        if rows:
            with pg_engine.begin() as pc:
                # ★ 先清空目标表（避免重复搬导致主键冲突）
                #   TRUNCATE ... CASCADE 会连带清空引用它的表，
                #   但因为我们按依赖顺序搬，这里用 DELETE 更安全可控
                pc.execute(text(f'DELETE FROM "{tname}"'))
                data = [dict(r) for r in rows]
                for i in range(0, len(data), args.batch):
                    pc.execute(table.insert(), data[i:i + args.batch])

        with pg_engine.connect() as pc:
            dst_n = pc.execute(select(func.count()).select_from(table)).scalar() or 0

        ok = src_n == dst_n
        report.append((tname, src_n, dst_n, "✓" if ok else "✗"))
        print(f"  {'✓' if ok else '✗'} {tname:28} 源={src_n:6}  目标={dst_n:6}")
        if not ok:
            print(f"\n✗ 行数不一致，已停止后续搬运（避免状态更难收拾）")
            break

    print()
    print("=" * 66)

    # ---- 汇总 ----
    total_src = sum(r[1] for r in report)
    total_dst = sum(r[2] for r in report)
    bad = [r for r in report if r[3] == "✗"]

    print(f"合计：源 {total_src} 行 → 目标 {total_dst} 行")
    print(f"耗时：{time.time() - t0:.1f}s")

    if args.verify_only:
        if bad:
            print(f"\n✗ 有 {len(bad)} 张表行数不一致：")
            for t, s, d, _ in bad:
                print(f"    {t}: 源 {s} vs 目标 {d}")
            return 1
        print("\n✅ 全部表行数一致")
        return 0

    if not do_write:
        print("\n（预演结束，未写入任何数据。加 --yes 执行正式搬运）")
        return 0

    if bad:
        print(f"\n✗ 搬运未完成（{len(bad)} 张表不一致），请勿切换流量")
        return 1

    # ---- ★ 序列修正（最关键的一步）----
    print("\n→ 修正自增序列（不修会在下次 INSERT 报 duplicate key）…")
    from apps.api.db import fix_sequences_engine
    try:
        fixed = fix_sequences_engine(pg_engine)
        for t, nxt in sorted(fixed.items()):
            print(f"  ✓ {t:28} nextval = {nxt}")
        print(f"  共修正 {len(fixed)} 个序列")
    except Exception as e:
        print(f"  ✗ 序列修正失败：{type(e).__name__}: {e}")
        print("    ⚠️ 请手工执行 db.py 里的 fix_sequences 逻辑，否则插入会失败")
        return 1

    print("\n" + "=" * 66)
    print("✅ 搬运完成")
    print("=" * 66)
    print("\n下一步：")
    print("  1. 用 PG 起测试实例：NOVELFORGE_DB_URL=<pg-url> uvicorn ... --port 8001")
    print("  2. 跑全量测试确认无回归")
    print("  3. 验证通过后再改 .env 切流")
    print("  4. ★ SQLite 文件保留 30 天（回滚唯一凭据）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

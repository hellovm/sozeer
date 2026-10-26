"""数据库会话。

v0.20.1 A1：SQLite → PostgreSQL 迁移。

设计原则（★ 关键）：
  * **DB_URL 是唯一开关** —— 通过环境变量 `NOVELFORGE_DB_URL` 切换，
    业务代码零改动（已实测：项目无裸 SQL、无 SQLite 专有类型、无全局锁假设）
  * **SQLite 分支完整保留** —— 本地开发与回滚都靠它，不是"过渡代码"
  * **方言差异集中在 _engine_kwargs / _pg_setup**，不外泄到业务层

为什么必须迁 PG（不是"更好"，是"必须"）：
  * SQLite 单写锁 → 多用户并发跑任务会互相阻塞
  * SQLite 无行级锁 → **积分扣费会竞态扣成负数**（v0.20.2 A2 的硬依赖）
  * SQLite 必须 --workers 1 → 无法水平扩展
"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from apps.api.config import DB_URL, ensure_dirs

IS_SQLITE = DB_URL.startswith("sqlite")


class Base(DeclarativeBase):
    pass


def _engine_kwargs(url: str) -> dict:
    """按方言决定引擎参数。

    ★ SQLite 与 PG 的差异都收敛在这里，业务代码看不到。
    """
    if url.startswith("sqlite"):
        # SQLite：单文件，禁止跨线程检查（FastAPI 多线程）
        return {"connect_args": {"check_same_thread": False}}

    # PostgreSQL / 其他网络数据库
    #
    # ★★★ v1.0 E2 压测修正（实测发现，线上真实故障）：
    #
    #   现象：100 并发压测 → `/api/public/site` 成功率崩到 32%、
    #         `/api/ads` **连 1 并发都超时 20s**；且**服务重启后立刻恢复**。
    #   排查：业务代码 77ms、限流器 0.3ms —— 都不是瓶颈。
    #   根因：**连接池耗尽**。原配置 pool_size=10 + max_overflow=20 = 最多 30 连接，
    #         而压测并发 100 → 超出部分在池上**无限等待**（默认 pool_timeout=30s），
    #         表现为"进程活着、探针正常、但业务请求全部挂住"，且**日志无异常**
    #         （因为没抛错，只是在等）—— 这类"静默雪崩"极难定位。
    #
    #   ★ 修复三点：
    #     ① pool_size 10→25、max_overflow 20→25：覆盖 100 并发下的峰值。
    #        ★★ 为什么不是"100 并发就配 100 连接"（重要认知）：
    #           **并发请求数 ≠ 同时持有的 DB 连接数**。
    #           一个请求的生命周期里，只有"查/写库"那一小段持有连接，
    #           其余时间（JSON 序列化、模板渲染、网络回包）连接已归还。
    #           实测本服务在 100 并发下实际峰值连接数远低于 100。
    #        ★★ 更关键的**硬约束**：线上 PG `max_connections = 100`，
    #           且它是**全区共享**的（还有备份、psql 运维、其它进程）。
    #           把池配到 100 会打满 PG → 连备份都连不上 → 从"慢"变成"全挂"。
    #           经验值：应用池总量 ≤ PG max_connections 的 50%~70%。
    #     ② pool_timeout 30→10：快速失败优于长时间挂住。
    #        30 秒里用户看到的是"转圈不知道发生了什么"，10 秒能更快触发重试/降级。
    #     ③ 新增 pool_use_lifo：连接复用改为 LIFO，**减少空闲连接被 PG 回收的概率**，
    #        实测能显著降低"取到死连接"的几率（配合 pool_pre_ping 双保险）。
    #
    #   ★ 教训（写入 MEMORY）：**压测的价值在于暴露"量变引起质变"的问题**。
    #     单请求 200ms 正常 ≠ 100 并发正常 —— 连接池、线程池、文件描述符
    #     这类**有限资源**只有压到上限才会暴露。
    #     而调参时**必须同时看下游约束**（PG max_connections），
    #     否则会把"应用层排队"变成"数据库层拒绝"，故障反而更严重。
    return {
        "pool_pre_ping": True,      # 取连接前 ping，避免用到失效连接
        "pool_size": 25,            # ★ 10→25（v1.0 E2 压测修正）
        "max_overflow": 25,         # ★ 20→25（池总量 50，留足 PG 余量）
        "pool_recycle": 1800,       # 30 分钟回收（防 PG 侧 idle timeout 断连）
        "pool_timeout": 10,         # ★ 30→10（快速失败优于长时间挂住）
        "pool_use_lifo": True,      # ★ 新增：LIFO 复用，减少死连接概率
        "connect_args": {
            # ★ 服务端超时保护：避免慢查询把连接池占满
            "options": "-c statement_timeout=300000"   # 单语句 5 分钟
        },
    }


ensure_dirs()
engine = create_engine(DB_URL, future=True, **_engine_kwargs(DB_URL))


if IS_SQLITE:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

else:
    # ★ PG 专用设置（v0.20.1）
    # 为什么要显式设：SQLAlchemy 默认 on_connect 只做 ping，
    # 而我们在意的是「连接被网络中断后能否自动恢复」与「时区语义」。
    @event.listens_for(engine, "connect")
    def _pg_setup(dbapi_conn, _):  # pragma: no cover
        cur = dbapi_conn.cursor()
        # 与应用层保持一致：全部按 UTC 存 naive datetime（不引入 timestamptz）
        # 理由：迁移前 SQLite 存的就是 naive，若这里改 tz 会造成数据语义漂移
        try:
            cur.execute("SET TIME ZONE 'UTC'")
        except Exception:
            pass
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from apps.api import models  # noqa: F401

    Base.metadata.create_all(engine)


def db_dialect() -> str:
    """当前数据库方言（health 探针用）。"""
    return "postgresql" if not IS_SQLITE else "sqlite"


def fix_sequences() -> dict:
    """★ PG 专用：修正自增序列。

    为什么必须做：数据搬运时我们**显式带 id 插入**，PG 的 sequence 不会自动跟进，
    导致后续 INSERT 报 `duplicate key value violates unique constraint`。
    这是 SQLite → PG 迁移最经典的坑（本方案 R2 风险项）。

    返回 {表名: 修正后的下一个值}。SQLite 下返回空 dict（无需处理）。
    """
    if IS_SQLITE:
        return {}
    return fix_sequences_engine(engine)


def fix_sequences_engine(eng) -> dict:
    """fix_sequences 的引擎参数版本（供搬运脚本用独立引擎调用）。

    拆出来是为了让 tools/migrate_sqlite_to_pg.py 能对**目标 PG 引擎**操作，
    而不是只能对全局 engine —— 全局 engine 在迁移场景下指向的还是源库。
    """
    from sqlalchemy import text

    out: dict = {}
    with eng.begin() as conn:
        # 找到所有「有序列且序列属于本表」的列
        rows = conn.execute(text("""
            SELECT c.table_name, c.column_name,
                   pg_get_serial_sequence(quote_ident(c.table_name), c.column_name)
            FROM information_schema.columns c
            WHERE c.table_schema = 'public'
              AND c.column_default LIKE 'nextval%'
        """)).fetchall()
        for table, column, seq in rows:
            if not seq:
                continue
            # 用 COALESCE 处理空表（max 为 NULL 时从 1 开始）
            nxt = conn.execute(
                text(f'SELECT COALESCE(MAX("{column}"), 0) + 1 FROM "{table}"')
            ).scalar()
            conn.execute(text("SELECT setval(:seq, :nxt, false)"),
                         {"seq": seq, "nxt": nxt})
            out[table] = nxt
    return out

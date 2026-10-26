# -*- coding: utf-8 -*-
"""数据库幂等迁移（v0.18 T1 从 main.py 抽出）。

为既有 SQLite 库补齐新列（ADD COLUMN 幂等）+ 历史数据回填。
抽出来的好处：迁移逻辑与路由定义解耦，修改表结构时不必翻 3000 行的 main.py。
"""
from __future__ import annotations

from apps.api.db import engine


def migrate() -> None:
    """为既有库补齐新列（SQLite ADD COLUMN，幂等）。"""
    from sqlalchemy import inspect, text as _sql_text

    new_cols = {
        "books": [
            ("owner_id", "VARCHAR(64) DEFAULT ''"),
            ("quality_score", "INTEGER DEFAULT 0"),
            ("polish_enabled", "INTEGER DEFAULT 1"),
            ("polish_threshold", "INTEGER DEFAULT 90"),
            ("auto_char_enabled", "INTEGER DEFAULT 1"),
            ("audit_enabled", "INTEGER DEFAULT 1"),
            ("audit_threshold", "INTEGER DEFAULT 80"),
            ("rolling_summary", "TEXT DEFAULT ''"),
            # v0.15 题材解耦：theme=主题材（单选，定度量衡/实力体系）；
            # tags=标签（多选，仅影响提示词）。老书留空 → 走仙侠兜底，零回归。
            ("theme", "VARCHAR(64) DEFAULT ''"),
            ("tags", "VARCHAR(512) DEFAULT ''"),
            # ★ v0.22.1 C1 三级大纲：小说级结构化大纲
            ("outline_meta", "TEXT DEFAULT ''"),
            # ★ v0.20.5 A5 管理后台：内容处置状态。
            #   content_status: normal | hidden（违规隐藏，作者可见但读者不可见）
            #   ★ 用独立字段而不是复用 status：status 是创作状态（连载/完结），
            #     语义完全不同 —— 混用会导致"隐藏一本书 = 把它标成完结"的荒唐结果。
            ("content_status", "VARCHAR(16) DEFAULT 'normal'"),
            ("flagged", "INTEGER DEFAULT 0"),
        ],
        "characters": [
            ("is_auto", "INTEGER DEFAULT 0"),
            ("mentions", "INTEGER DEFAULT 0"),
            ("aliases", "TEXT DEFAULT ''"),
            ("state_src_ch", "INTEGER DEFAULT 0"),
            ("voice_profile", "TEXT DEFAULT ''"),            # ★ v0.21.5 B5 角色资产库
            ("ref_images", "TEXT DEFAULT ''"),
            ("appearance_locks", "TEXT DEFAULT ''"),
        ],
        "world_settings": [
            ("src_ch", "INTEGER DEFAULT 0"),
        ],
        # ★ v0.22.1 C1 三级大纲
        "volumes": [
            ("outline_meta", "TEXT DEFAULT ''"),
        ],
        "chapters": [
            ("outline", "TEXT DEFAULT ''"),
            ("outline_meta", "TEXT DEFAULT ''"),
        ],
        "drama_scenes": [
            ("qc_report", "TEXT DEFAULT ''"),
        ],
        # D19 音色资产化：角色资产挂声纹档案（JSON），小说侧角色也留一份便于
        # 跨项目复用语色。两处都幂等 ADD COLUMN，老库平滑升级。
        # v0.14.10 顺带补齐 C2 的 parent_id/form_name——此前只靠线上手工加过列，
        # 本地/新库缺列会导致 select 直接报 no such column（实测踩坑）。
        "drama_assets": [
            ("parent_id", "VARCHAR(64) DEFAULT ''"),
            ("form_name", "VARCHAR(64) DEFAULT ''"),
            ("voice_profile", "TEXT DEFAULT ''"),
        ],
        "foreshadows": [
            ("due_ch", "INTEGER DEFAULT 0"),
            ("priority", "VARCHAR(16) DEFAULT 'normal'"),
        ],
        "ledger_entries": [
            ("seq", "INTEGER DEFAULT 0"),
        ],
        # ★ v0.25.3 H6：实例核查字段
        "telemetry_instances": [
            ("suspect_reasons", "TEXT DEFAULT ''"),
            ("review_status", "VARCHAR(16) DEFAULT ''"),
            ("review_note", "TEXT DEFAULT ''"),
            ("reviewed_by", "VARCHAR(64) DEFAULT ''"),
            ("reviewed_at", "TIMESTAMP NULL"),
            ("last_scale_json", "TEXT DEFAULT ''"),
            # ★ v0.25.5：站点信息（展示用，不参与身份判定）
            ("site_url", "VARCHAR(255) DEFAULT ''"),
            ("site_name", "VARCHAR(128) DEFAULT ''"),
            ("hostname", "VARCHAR(64) DEFAULT ''"),
            ("claim_code", "VARCHAR(32) DEFAULT ''"),
            ("claimed", "INTEGER DEFAULT 0"),
            ("claimed_at", "TIMESTAMP NULL"),
            ("claimed_by", "VARCHAR(128) DEFAULT ''"),
            ("contact", "VARCHAR(255) DEFAULT ''"),
            ("last_source_ip", "VARCHAR(64) DEFAULT ''"),
            ("source_change_count", "INTEGER DEFAULT 0"),
            ("sig", "VARCHAR(64) DEFAULT ''"),
        ],
        # ★ v0.25 J 组：排队字段（Task 原来只有 running/done/error/abandoned）
        "tasks": [
            ("priority", "INTEGER DEFAULT 0"),
            ("lane", "VARCHAR(16) DEFAULT 'standard'"),
            ("worker_id", "VARCHAR(64) DEFAULT ''"),
            ("owner_id", "VARCHAR(64) DEFAULT ''"),
            ("enqueued_at", "TIMESTAMP NULL"),
        ],
        # v0.14.10：消息挂任务 id，供历史回放重建草稿卡（见 get_messages）。
        "messages": [            ("task_id", "VARCHAR(64) DEFAULT ''"),
        ],
        # ★ v0.21.1 B1 向量检索记忆：memory_chunks 加稠密向量列。
        #   老数据 emb 为空 → 检索时自动只用稀疏向量（降级行为，零回归）。
        "memory_chunks": [
            ("emb", "TEXT DEFAULT ''"),
            ("emb_dim", "INTEGER DEFAULT 0"),
            ("emb_model", "VARCHAR(64) DEFAULT ''"),
        ],
        # ★ v0.20.4 A4 用户中心：users 表新增字段。
        #   为什么必须写在这里：`create_all` **只建表不改表** ——
        #   生产库 users 表已有数据，新列不会自动加，导致
        #   `column users.nickname does not exist` 直接 500。
        #   （实测踩坑：部署后查询立刻报错）
        "users": [
            ("nickname", "VARCHAR(64) DEFAULT ''"),
            ("avatar", "VARCHAR(256) DEFAULT ''"),
            ("bio", "TEXT DEFAULT ''"),
            ("phone", "VARCHAR(32) DEFAULT ''"),
            ("phone_verified", "INTEGER DEFAULT 0"),
            ("email_verified", "INTEGER DEFAULT 0"),
            ("totp_secret", "VARCHAR(256) DEFAULT ''"),
            ("totp_enabled", "INTEGER DEFAULT 0"),
            ("last_login_at", "TIMESTAMP"),
            ("last_login_ip", "VARCHAR(64) DEFAULT ''"),
            ("deleted_at", "TIMESTAMP"),
            ("token_valid_after", "TIMESTAMP"),
            # ★ v1.0-E4.11（合规/E1 备案）：协议同意时间戳 + 版本号。
            #   同 create_all 只建表不改表 —— 生产库必须在这里幂等补列。
            ("agreed_terms_at", "TIMESTAMP"),
            ("agreed_terms_version", "VARCHAR(32) DEFAULT ''"),
        ],
        # ★ v0.25.8 H4：发票表（新表由 create_all 建，这里只做兜底补列 ——
        #   若线上曾手工建过部分列，ADD COLUMN 幂等补齐）。
        "invoices": [
            ("order_ids", "TEXT DEFAULT ''"),
            ("order_count", "INTEGER DEFAULT 0"),
            ("amount_cents", "INTEGER DEFAULT 0"),
            ("kind", "VARCHAR(16) DEFAULT 'normal'"),
            ("title_type", "VARCHAR(16) DEFAULT 'personal'"),
            ("invoice_no", "VARCHAR(64) DEFAULT ''"),
            ("file_path", "VARCHAR(512) DEFAULT ''"),
            ("reject_reason", "TEXT DEFAULT ''"),
            ("void_reason", "TEXT DEFAULT ''"),
            ("operator", "VARCHAR(64) DEFAULT ''"),
            ("updated_at", "TIMESTAMP"),
            ("issued_at", "TIMESTAMP"),
        ],
    }
    insp = inspect(engine)
    # ★ v0.20.4：users.phone 需要索引（手机号登录查询）。幂等建索引。
    try:
        if "users" in insp.get_table_names():
            _idx = {i["name"] for i in insp.get_indexes("users")}
            if "ix_users_phone" not in _idx:
                with engine.begin() as conn:
                    conn.execute(_sql_text(
                        "CREATE INDEX IF NOT EXISTS ix_users_phone ON users (phone)"))
    except Exception:
        pass      # 索引失败不影响主流程

    for table, cols in new_cols.items():
        if table not in insp.get_table_names():
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        with engine.begin() as conn:
            for name, ddl in cols:
                if name not in have:
                    conn.execute(_sql_text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
    # 补号：历史行的 seq 为 0 时，按插入顺序（SQLite rowid）回填，
    # 否则「取最后一笔余额」会把它们当成一个批次、顺序错乱。
    if "ledger_entries" in insp.get_table_names():
        with engine.begin() as conn:
            n = conn.execute(_sql_text(
                "SELECT COUNT(*) FROM ledger_entries WHERE seq IS NULL OR seq = 0")).scalar() or 0
            if n:
                mx = conn.execute(_sql_text(
                    "SELECT COALESCE(MAX(seq), 0) FROM ledger_entries")).scalar() or 0
                conn.execute(_sql_text(
                    "UPDATE ledger_entries SET seq = :base + rowid "
                    "WHERE seq IS NULL OR seq = 0"), {"base": int(mx)})

    # ★ v0.23.3 D3：补齐性能关键索引（幂等）。
    #
    # 为什么放在迁移里而不是 migration 工具：项目没有 Alembic，
    # 且这些索引"业务上必需"（缺了就是全表扫描），不是可选优化。
    #
    # ★ 只补真需要的列（来自 perf.EXPECTED_INDEXES），
    #   不无脑给所有列加索引（写放大 + 存储）。
    try:
        tables = set(insp.get_table_names())
        wanted = [
            ("llm_calls", "task", "ix_llm_calls_task"),
            ("llm_calls", "user_id", "ix_llm_calls_user"),
            ("llm_calls", "created_at", "ix_llm_calls_created"),
            ("books", "owner_id", "ix_books_owner"),
            ("chapters", "book_id", "ix_chapters_book"),
            ("memory_chunks", "book_id", "ix_memchunk_book"),
            ("book_scenes", "book_id", "ix_bookscenes_book"),
            ("character_relations", "book_id", "ix_charrel_book"),
            ("chapter_emotion", "book_id", "ix_chapemo_book"),
            ("character_knowledge", "book_id", "ix_charknow_book"),
            ("orders", "status", "ix_orders_status"),
            # ★ v0.25.8 H4：发票
            ("invoices", "user_id", "ix_invoices_user"),
            ("invoices", "status", "ix_invoices_status"),
            ("invoices", "invoice_no", "ix_invoices_no"),
            ("invoices", "created_at", "ix_invoices_created"),
            # ★ v0.24 F 组
            ("user_feedback", "user_id", "ix_feedback_user"),
            ("user_feedback", "status", "ix_feedback_status"),
            ("user_feedback", "created_at", "ix_feedback_created"),
            ("user_feedback", "pooled", "ix_feedback_pooled"),
            ("telemetry_instances", "version", "ix_telinst_version"),
            ("telemetry_metrics", "instance_id", "ix_telmetric_inst"),
            ("telemetry_events", "instance_id", "ix_televent_inst"),
            # ★ v0.25 Xf 组
            ("notifications", "user_id", "ix_notif_user"),
            ("notifications", "created_at", "ix_notif_created"),
            ("notifications", "dedup_key", "ix_notif_dedup"),
            ("notification_prefs", "user_id", "ix_notifpref_user"),
            # ★ v0.25 J 组
            ("tasks", "status", "ix_tasks_status"),
            ("tasks", "priority", "ix_tasks_priority"),
            ("tasks", "owner_id", "ix_tasks_owner"),
            # ★ v0.25.3 H6
            ("telemetry_instances", "review_status", "ix_telinst_review"),
            ("telemetry_instances", "last_seen", "ix_telinst_seen"),
            # ★ v0.25.5
            ("telemetry_instances", "claimed", "ix_telinst_claimed"),
            ("instance_commands", "instance_id", "ix_instcmd_inst"),
            ("instance_commands", "status", "ix_instcmd_status"),
            ("instance_commands", "created_at", "ix_instcmd_created"),
            # ★ v0.25.4 I 组
            ("setting_history", "key", "ix_setth_key"),
            ("setting_history", "created_at", "ix_setth_created"),
            ("setting_history", "operator", "ix_setth_operator"),
            ("setting_history", "source", "ix_setth_source"),
        ]
        with engine.begin() as conn:
            for table, col, name in wanted:
                if table not in tables:
                    continue
                try:
                    conn.execute(_sql_text(
                        f'CREATE INDEX IF NOT EXISTS {name} ON "{table}" ("{col}")'))
                except Exception:            # noqa: BLE001 单列失败不影响其他
                    continue
    except Exception:
        pass      # 索引失败不影响主流程


__all__ = ["migrate"]

"""轻量级、幂等的列补齐。

项目没有引入迁移框架；create_all 只会建新表，不会给既有表补列。
这里只做“缺什么补什么”，保证旧卷升级后仍能启动。
"""

import logging

from sqlalchemy import text

from .database import engine

logger = logging.getLogger("manhole-settlement.schema")

# (表, 列, PostgreSQL 列定义)
COLUMNS = [
    ("survey_batches", "zone_revision", "INTEGER NOT NULL DEFAULT 0"),
    ("survey_points", "source", "VARCHAR(20) NOT NULL DEFAULT 'initial'"),
    ("survey_points", "supplement_key", "VARCHAR(100)"),
    ("survey_points", "published_revision", "INTEGER"),
    ("repair_zones", "revision", "INTEGER"),
    ("repair_zones", "superseded_at", "TIMESTAMP WITH TIME ZONE"),
]

INDEXES = [
    ("uq_survey_batch_supplement_key", "CREATE UNIQUE INDEX IF NOT EXISTS uq_survey_batch_supplement_key ON survey_points (batch_id, supplement_key)"),
]


def ensure_schema():
    with engine.begin() as conn:
        for table, column, ddl in COLUMNS:
            exists = conn.execute(
                text("SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c"),
                {"t": table, "c": column},
            ).scalar()
            if exists:
                continue
            conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))
            logger.info("schema: added %s.%s", table, column)
        for name, ddl in INDEXES:
            try:
                conn.execute(text(ddl))
            except Exception:  # noqa: BLE001 - 非致命，索引已存在或方言差异
                logger.warning("schema: index %s skipped", name)

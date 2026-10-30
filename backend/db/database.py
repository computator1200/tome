from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from config import DATABASE_URL

_is_sqlite = DATABASE_URL.startswith("sqlite")

# echo=False — SQL is noisy; pipeline logging covers what we care about.
# For SQLite, a busy timeout lets writers wait for the lock instead of failing
# with "database is locked" (important now that the worker, its concurrent
# writes, and the SSE poller all touch one file).
_connect_args = {"timeout": 30} if _is_sqlite else {}
engine = create_async_engine(DATABASE_URL, echo=False, future=True, connect_args=_connect_args)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


if _is_sqlite:
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")   # concurrent readers + one writer
        cur.execute("PRAGMA busy_timeout=30000")  # wait up to 30s for a lock
        cur.execute("PRAGMA foreign_keys=ON")    # enforce cascade deletes
        cur.close()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped session."""
    async with SessionLocal() as session:
        yield session


# Columns added to existing tables after the first release. create_all does not
# ALTER existing tables, so we add them by hand on startup (SQLite ADD COLUMN is
# cheap and idempotent once guarded). Keeps existing data intact — no wipe.
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "series": [
        ("source_language", "VARCHAR DEFAULT 'auto'"),
        ("detected_language", "VARCHAR DEFAULT ''"),
        ("target_language", "VARCHAR DEFAULT 'English'"),
    ],
    "volumes": [
        ("summary", "TEXT DEFAULT ''"),
        # 'scans' (image pipeline) | 'text' (digital documents / webnovels)
        ("kind", "VARCHAR DEFAULT 'scans'"),
        # object-store key of the original document (epub) used for repack
        ("source_key", "VARCHAR DEFAULT ''"),
    ],
    "pages": [
        # chapter heading for text volumes (a text "page" is one chapter)
        ("chapter_title", "TEXT DEFAULT ''"),
    ],
}


async def _migrate_columns(conn) -> None:
    from sqlalchemy import text

    for table, columns in _ADDED_COLUMNS.items():
        rows = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
        existing = {r[1] for r in rows.fetchall()}
        for name, ddl in columns:
            if name not in existing:
                await conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                logger_msg = f"migrated: added {table}.{name}"
                import logging
                logging.getLogger("db").warning(logger_msg)


async def _init_fts(conn) -> None:
    """Full-text index over region text for the series chat agent (SQLite only).

    External-content FTS5 table + triggers keep it in sync with `regions` no
    matter who writes (pipeline, deletes, cascades). Built once; on first
    creation we backfill from existing rows with a 'rebuild'.
    """
    rows = await conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='region_fts'"
    )
    exists = rows.first() is not None
    if not exists:
        await conn.exec_driver_sql(
            "CREATE VIRTUAL TABLE region_fts USING fts5("
            "english_text, japanese_text, content='regions', content_rowid='rowid')"
        )
        await conn.exec_driver_sql("INSERT INTO region_fts(region_fts) VALUES('rebuild')")
    await conn.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS regions_fts_ai AFTER INSERT ON regions BEGIN "
        "INSERT INTO region_fts(rowid, english_text, japanese_text) "
        "VALUES (new.rowid, new.english_text, new.japanese_text); END"
    )
    await conn.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS regions_fts_ad AFTER DELETE ON regions BEGIN "
        "INSERT INTO region_fts(region_fts, rowid, english_text, japanese_text) "
        "VALUES ('delete', old.rowid, old.english_text, old.japanese_text); END"
    )
    await conn.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS regions_fts_au AFTER UPDATE ON regions BEGIN "
        "INSERT INTO region_fts(region_fts, rowid, english_text, japanese_text) "
        "VALUES ('delete', old.rowid, old.english_text, old.japanese_text); "
        "INSERT INTO region_fts(rowid, english_text, japanese_text) "
        "VALUES (new.rowid, new.english_text, new.japanese_text); END"
    )


async def init_db() -> None:
    """Create tables for dev/first-run and apply lightweight column migrations.
    Production could use Alembic, but create_all + ADD COLUMN keeps the
    zero-config story working without losing data."""
    # Import models so they register on Base.metadata before create_all.
    from db import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_columns(conn)
        if _is_sqlite:
            await _init_fts(conn)

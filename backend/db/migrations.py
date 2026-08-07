"""Idempotent schema migrations.

Follows the TranscodeBot pattern (PRAGMA table_info + conditional ALTER) rather
than pulling in Alembic. Migrations are an ordered list of (version, callable);
each runs at most once and records itself in schema_migrations.

Adding a column later:

    async def _v2_add_foo(db):
        if "foo" not in await db.table_columns("items"):
            await db.execute("ALTER TABLE items ADD COLUMN foo TEXT")

    MIGRATIONS = [(1, _v1_base), (2, _v2_add_foo)]
"""
import time
from pathlib import Path

from backend.common.logging_config import get_logger
from backend.db.database import Database

logger = get_logger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


async def _v1_base(db: Database) -> None:
    """Create the v1 schema. Every statement is CREATE ... IF NOT EXISTS, so
    this is safe to re-run against a partially-created database."""
    await db.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


async def _v2_settings(db: Database) -> None:
    """UI-editable settings, one row per top-level section.

    Per-section rather than one blob so partial writes stay cheap and the table
    is readable with the sqlite3 CLI when something looks wrong.
    """
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
          key   TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """
    )


async def _v3_seen_run(db: Database) -> None:
    """Track which discovery run last saw each item.

    Replaces comparing `last_seen` against the run's start timestamp. That
    comparison silently breaks whenever two runs land in the same wall-clock
    second — rotation detection and soft-delete both no-op — and it made
    correctness depend on clock resolution. An explicit run id can't tie.
    """
    if "seen_run" not in await db.table_columns("items"):
        await db.execute("ALTER TABLE items ADD COLUMN seen_run INTEGER")
    await db.executescript(
        "CREATE INDEX IF NOT EXISTS idx_items_seen_run ON items(library_key, seen_run)"
    )


async def _v4_plex_duration(db: Database) -> None:
    """Store Plex's reported runtime.

    Discovery already receives it; without a column to keep it in, the
    plex.duration_min fact could never be emitted and anything derived from it
    would be dead. Kept as a column rather than a fact because, like title and
    year, it's identity metadata that discovery owns.
    """
    if "plex_duration_ms" not in await db.table_columns("items"):
        await db.execute("ALTER TABLE items ADD COLUMN plex_duration_ms INTEGER")


MIGRATIONS: list[tuple[int, callable]] = [
    (1, _v1_base),
    (2, _v2_settings),
    (3, _v3_seen_run),
    (4, _v4_plex_duration),
]


async def run_migrations(db: Database) -> None:
    await db.executescript(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
    )
    rows = await db.fetch_all("SELECT version FROM schema_migrations")
    applied = {r["version"] for r in rows}

    for version, fn in MIGRATIONS:
        if version in applied:
            continue
        logger.info("⬆️  Applying migration %d (%s)", version, fn.__name__)
        await fn(db)
        await db.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, int(time.time())),
        )

    current = max((v for v, _ in MIGRATIONS), default=0)
    logger.info("✅ Schema at version %d", current)

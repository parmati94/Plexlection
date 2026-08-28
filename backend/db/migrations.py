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


async def _v5_tv(db: Database) -> None:
    """Columns for TV.

    Episodes carry their show's ratingKey in `parent_key` — the show, not the
    season. Seasons aren't indexed: Plex can't put one in a collection, so a
    third tier would cost a join for nothing.

    `tvdb_id` is free from the show's guid list and is what Sonarr keys on, so
    it's captured now rather than needing a re-scan later.
    """
    cols = await db.table_columns("items")
    for name, decl in (
        ("parent_key", "TEXT"),        # episode -> show ratingKey
        ("season_number", "INTEGER"),
        ("episode_number", "INTEGER"),
        ("tvdb_id", "INTEGER"),
        ("child_count", "INTEGER"),    # show: seasons
        ("leaf_count", "INTEGER"),     # show: episodes Plex knows about
        ("viewed_leaf_count", "INTEGER"),
    ):
        if name not in cols:
            await db.execute(f"ALTER TABLE items ADD COLUMN {name} {decl}")

    await db.executescript(
        "CREATE INDEX IF NOT EXISTS idx_items_parent "
        "  ON items(parent_key) WHERE deleted_at IS NULL;"
        "CREATE INDEX IF NOT EXISTS idx_items_tvdb "
        "  ON items(tvdb_id) WHERE tvdb_id IS NOT NULL;"
    )


async def _v6_collection_identity(db: Database) -> None:
    """Track which Plex collection a rule owns, and unfreeze its title.

    The builder used to persist the rule's name into collection_title on first
    save, so renaming the rule silently stopped renaming the collection. Any
    stored title equal to the name is that accident rather than a choice —
    clear those so the title follows the name again.

    The rating key is what lets sync rename the existing collection instead of
    creating a sibling under the new title and stranding the old one. It's
    NULL here and adopted (by title lookup) on each rule's next sync.
    """
    if "collection_rating_key" not in await db.table_columns("rules"):
        await db.execute("ALTER TABLE rules ADD COLUMN collection_rating_key TEXT")
    await db.execute(
        "UPDATE rules SET collection_title = NULL WHERE collection_title = name"
    )


MIGRATIONS: list[tuple[int, callable]] = [
    (1, _v1_base),
    (2, _v2_settings),
    (3, _v3_seen_run),
    (4, _v4_plex_duration),
    (5, _v5_tv),
    (6, _v6_collection_identity),
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

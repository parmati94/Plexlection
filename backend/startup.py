"""Application lifecycle and global runtime state.

Module-level mutables live here, and routers reference them as `startup.db`
rather than importing the names directly — that way a rebind in the lifespan is
visible everywhere. This is the house pattern from palworld-lens/backend/startup.py.
"""
import time
from contextlib import asynccontextmanager

from backend.clients.plex_client import PlexClient
from backend.common.config import config
from backend.common.logging_config import get_logger
from backend.common.settings import SettingsStore
from backend.db import indexes
from backend.db.database import Database
from backend.db.migrations import run_migrations
from backend.facts.registry import FactRegistry, build_registry
from backend.providers import build_providers
from backend.scan.engine import ScanEngine
from backend.sync.engine import SyncEngine
from backend.utils.broadcast import Broadcaster
from backend.utils.scheduler import Scheduler

logger = get_logger(__name__)

# ── Global runtime state ──────────────────────────────────────────────────
db: Database | None = None
broadcaster: Broadcaster | None = None
settings_store: SettingsStore | None = None
registry: FactRegistry | None = None
providers: list = []
scan_engine: ScanEngine | None = None
sync_engine: SyncEngine | None = None
scheduler = None

# Derived clients are cached and rebuilt when the settings version changes,
# so callers never have to remember to invalidate them by hand.
_plex_client: PlexClient | None = None
_plex_version: int = -1


def get_plex() -> PlexClient:
    """Plex client for the current settings. Rebuilt on any settings change."""
    global _plex_client, _plex_version
    s = settings_store.get()
    if _plex_client is None or _plex_version != settings_store.version:
        _plex_client = PlexClient(
            url=s.plex.url,
            token=s.plex.token,
            timeout=s.plex.timeout_s,
            verify_ssl=s.plex.verify_ssl,
        )
        _plex_version = settings_store.version
    return _plex_client


@asynccontextmanager
async def lifespan(app):
    """Startup and shutdown."""
    global db, broadcaster, settings_store, registry, providers
    global scan_engine, sync_engine, scheduler

    logger.info("🚀 Starting Plexlection")

    config.ensure_dirs()
    logger.info("📁 Data directory: %s", config.DATA_DIR)

    if config.ENABLE_LOGIN and config.SESSION_SECRET.startswith("change-me"):
        logger.warning(
            "⚠️  ENABLE_LOGIN is on but SESSION_SECRET is still the default. "
            "Sessions can be forged — set SESSION_SECRET in your compose file."
        )

    db = Database()
    await db.start()
    await run_migrations(db)

    settings_store = SettingsStore(db)
    await settings_store.load()
    await settings_store.seed_from_env()

    configured = settings_store.configured()
    logger.info(
        "🔌 Configured: %s",
        ", ".join(k for k, v in configured.items() if v) or "nothing yet — open Settings",
    )

    broadcaster = Broadcaster()
    await broadcaster.start()

    # Providers declare their facts; the registry is what the rule builder UI
    # and the expression indexes are both generated from.
    providers = build_providers(settings_store.get(), db)
    registry = build_registry(providers)
    await indexes.reconcile(db, registry)
    await indexes.self_test(db, registry)

    scan_engine = ScanEngine(db, registry, providers, broadcaster)
    sync_engine = SyncEngine(db, registry, get_plex, settings_store, broadcaster)

    scheduler = Scheduler(settings_store)
    await scheduler.start()

    # A run left 'running' means the process died mid-scan. Provenance is
    # written per batch, so resuming re-plans and picks up where it stopped.
    await db.execute(
        "UPDATE scan_runs SET status='error', message='interrupted by restart', "
        "finished_at=? WHERE status='running'",
        (int(time.time()),),
    )

    item_count = await db.fetch_val(
        "SELECT COUNT(*) FROM items WHERE deleted_at IS NULL", default=0
    )
    unmapped = await db.fetch_val(
        "SELECT COUNT(*) FROM items WHERE deleted_at IS NULL AND path_status = 'unmapped'",
        default=0,
    )
    logger.info("📚 %d live items in the catalog", item_count)
    if unmapped:
        logger.warning(
            "⚠️  %d items have unmapped file paths — file-derived facts can't be "
            "computed for them. Add a mapping in Settings → Paths.", unmapped,
        )

    logger.info("✅ Plexlection ready")

    yield

    logger.info("👋 Shutting down Plexlection")
    if scheduler is not None:
        await scheduler.stop()
    if broadcaster is not None:
        await broadcaster.stop()
    if db is not None:
        await db.stop()


def rebuild_providers() -> None:
    """Re-instantiate providers after a settings change.

    A newly-entered TMDB key has to make that provider configured *now*, and the
    registry the rule builder reads is derived from the provider list — so both,
    plus the engines holding references to them, are rebuilt together.
    """
    global providers, registry, scan_engine, sync_engine
    if settings_store is None or db is None:
        return
    providers = build_providers(settings_store.get(), db)
    registry = build_registry(providers)
    scan_engine = ScanEngine(db, registry, providers, broadcaster)
    sync_engine = SyncEngine(db, registry, get_plex, settings_store, broadcaster)
    logger.info("♻️  Providers rebuilt (%d facts)", len(registry.all()))

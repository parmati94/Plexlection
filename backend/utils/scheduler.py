"""Scheduled scans and syncs.

APScheduler's AsyncIOScheduler rather than the `schedule`+daemon-thread pattern
used elsewhere in this code directory. That pattern is right for standalone
scripts, and wrong here: these jobs are already coroutines that must run on the
same event loop as the API, share the same SQLite write lock, the same
broadcaster and the same engine singletons. A thread-based scheduler would need
`run_coroutine_threadsafe` for every job and buy nothing.

What it gives us for free: cron expressions from the UI, `max_instances=1` so a
long deep scan can't stack, `coalesce=True` so a restart doesn't fire six missed
runs at once, and `next_run_time` for the "next run in 4h 12m" display.

Job *definitions* live in settings, so the default in-memory job store is right
— APScheduler has nothing worth persisting, which also sidesteps its
pickle-based stores entirely.
"""
from backend.common.logging_config import get_logger

logger = get_logger(__name__)

JOB_IDS = ("scan_incremental", "scan_deep", "sync_all")


class Scheduler:
    def __init__(self, settings_store):
        self.settings_store = settings_store
        self._scheduler = None

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def start(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        self._scheduler = AsyncIOScheduler(
            job_defaults={
                "max_instances": 1,   # a long scan must not stack on itself
                "coalesce": True,     # a restart fires one catch-up, not six
                "misfire_grace_time": 3600,
            }
        )
        self._scheduler.start()
        self.reconfigure()

    async def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    # ── job registration ──────────────────────────────────────────────────
    def reconfigure(self) -> None:
        """(Re)register jobs from settings. Called at startup and on any
        settings change, so editing a cron expression takes effect immediately."""
        if self._scheduler is None:
            return
        from apscheduler.triggers.cron import CronTrigger

        for job_id in JOB_IDS:
            existing = self._scheduler.get_job(job_id)
            if existing:
                existing.remove()

        schedule = self.settings_store.get().schedule
        if not schedule.enabled:
            logger.info("⏰ Scheduler is off")
            return

        plans = [
            ("scan_incremental", schedule.scan_incremental, _run_incremental_scan),
            ("scan_deep", schedule.scan_deep, _run_deep_scan),
            ("sync_all", schedule.sync_all, _run_sync_all),
        ]
        for job_id, expression, func in plans:
            if not expression:
                continue
            try:
                trigger = CronTrigger.from_crontab(expression)
            except ValueError as exc:
                logger.warning("⏰ Bad cron for %s (%r): %s", job_id, expression, exc)
                continue
            self._scheduler.add_job(func, trigger=trigger, id=job_id,
                                    name=job_id, replace_existing=True)
            logger.info("⏰ %s scheduled: %s", job_id, expression)

    def jobs(self) -> list[dict]:
        if self._scheduler is None:
            return []
        return [
            {
                "id": job.id,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            }
            for job in self._scheduler.get_jobs()
        ]


# ── jobs ──────────────────────────────────────────────────────────────────
# Each wraps everything in try/except: an unhandled exception inside an
# APScheduler job kills that job silently, with no trace anywhere.

async def _run_incremental_scan() -> None:
    from backend import startup
    from backend.models.rules import RulePreview  # noqa: F401  (keeps imports lazy)
    from backend.routers.scan import ScanRequest, _run_job

    try:
        if not _plex_ready():
            logger.info("⏰ Skipping scheduled scan — Plex isn't configured")
            return
        if startup.scan_engine.running:
            logger.info("⏰ Skipping scheduled scan — one is already running")
            return
        await _run_job(ScanRequest(max_cost="network", discover=True), "schedule")
    except Exception as exc:
        logger.error("⏰ Scheduled incremental scan failed: %s", exc, exc_info=True)


async def _run_deep_scan() -> None:
    from backend import startup
    from backend.routers.scan import ScanRequest, _run_job

    try:
        if not _plex_ready() or startup.scan_engine.running:
            return
        await _run_job(ScanRequest(max_cost="expensive", discover=True), "schedule")
    except Exception as exc:
        logger.error("⏰ Scheduled deep scan failed: %s", exc, exc_info=True)


async def _run_sync_all() -> None:
    from backend import startup
    from backend.routers.rules import _row_to_rule

    try:
        if not _plex_ready():
            return
        rows = await startup.db.fetch_all("SELECT * FROM rules WHERE enabled = 1")
        dry_run = startup.settings_store.get().safety.dry_run
        for row in rows:
            rule = _row_to_rule(row)
            try:
                await startup.sync_engine.sync_rule(
                    rule, dry_run=dry_run, trigger="schedule"
                )
            except Exception as exc:
                # A guard refusing one rule must not stop the rest.
                logger.warning("⏰ Scheduled sync skipped %s: %s", rule["name"], exc)
    except Exception as exc:
        logger.error("⏰ Scheduled sync-all failed: %s", exc, exc_info=True)


def _plex_ready() -> bool:
    from backend import startup
    settings = startup.settings_store.get()
    return bool(settings.plex.url and settings.plex.token and settings.plex.libraries)

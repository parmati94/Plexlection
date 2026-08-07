"""Scan control.

Discovery and fact computation share one lock, one run table and one progress
stream, so a scheduled run and a button press contend properly and the UI needs
no special-casing for either.
"""
import asyncio
import json
import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend import startup
from backend.common.auth import require_auth
from backend.common.errors import NotConfiguredError, ScanBusyError
from backend.common.logging_config import get_logger
from backend.facts.spec import CostTier
from backend.scan.discovery import run_discovery

logger = get_logger(__name__)
router = APIRouter(prefix="/api/scan", tags=["scan"])

_lock = asyncio.Lock()
_task: asyncio.Task | None = None


class ScanRequest(BaseModel):
    # None = every configured provider at or below max_cost.
    providers: list[str] | None = None
    # Recompute even where provenance says the result is current.
    force: bool = False
    # Expensive providers (frame decoding) must be asked for by name.
    max_cost: str = "cheap"
    # Refresh the catalog from Plex before computing facts.
    discover: bool = True


@router.get("/state", dependencies=[Depends(require_auth)])
async def scan_state():
    row = await startup.db.fetch_one(
        "SELECT * FROM scan_runs ORDER BY started_at DESC LIMIT 1"
    )
    return {
        "running": _lock.locked(),
        "last_run": dict(row) if row else None,
        "live": startup.broadcaster.state.get("scan") if startup.broadcaster else None,
        "coverage": await startup.scan_engine.coverage(),
    }


@router.get("/runs", dependencies=[Depends(require_auth)])
async def recent_runs(limit: int = 20):
    rows = await startup.db.fetch_all(
        "SELECT * FROM scan_runs ORDER BY started_at DESC LIMIT ?", (limit,)
    )
    return {"runs": [dict(r) for r in rows], "count": len(rows)}


@router.post("", dependencies=[Depends(require_auth)])
async def start_scan(request: ScanRequest):
    await _ensure_free()
    _require_plex()

    global _task
    _task = asyncio.create_task(_run_job(request, "manual"), name="scan")
    return {"success": True, "started": True}


@router.post("/discover", dependencies=[Depends(require_auth)])
async def start_discovery():
    """Discovery only — refresh the catalog without computing any facts."""
    await _ensure_free()
    _require_plex()

    global _task
    request = ScanRequest(providers=[], discover=True)
    _task = asyncio.create_task(_run_job(request, "manual"), name="discovery")
    return {"success": True, "started": True}


@router.post("/cancel", dependencies=[Depends(require_auth)])
async def cancel_scan():
    if not _lock.locked():
        return {"success": True, "cancelled": False, "detail": "No scan is running."}
    # Cooperative first, so in-flight work flushes at a clean boundary.
    startup.scan_engine.request_cancel()
    if _task:
        _task.cancel()
    return {"success": True, "cancelled": True}


# ── helpers ───────────────────────────────────────────────────────────────
async def _ensure_free() -> None:
    if _lock.locked():
        row = await startup.db.fetch_one(
            "SELECT id, kind, trigger, started_at, done, total FROM scan_runs "
            "WHERE status = 'running' ORDER BY started_at DESC LIMIT 1"
        )
        raise ScanBusyError("A scan is already running.", run=dict(row) if row else None)


def _require_plex() -> None:
    settings = startup.settings_store.get()
    if not (settings.plex.url and settings.plex.token):
        raise NotConfiguredError("Plex is not configured — set the server URL and token in Settings.")
    if not settings.plex.libraries:
        raise NotConfiguredError("No Plex libraries selected — choose at least one in Settings.")


async def _run_job(request: ScanRequest, trigger: str) -> None:
    async with _lock:
        db = startup.db
        bc = startup.broadcaster
        engine = startup.scan_engine
        settings = startup.settings_store.get()

        kind = "discover" if request.providers == [] else "facts"
        started = int(time.time())
        run_id = await db.execute(
            "INSERT INTO scan_runs (kind, trigger, providers, started_at, status) "
            "VALUES (?,?,?,?, 'running')",
            (kind, trigger, json.dumps(request.providers or []), started),
        )

        summary: list[str] = []
        try:
            # ── discovery ──────────────────────────────────────────────
            if request.discover:
                def progress(done: int, total: int, label: str) -> None:
                    if bc:
                        bc.set_state("scan", {
                            "run_id": run_id, "kind": "discover", "status": "running",
                            "provider": "plex", "provider_label": "Discovery",
                            "done": done, "total": total, "current": label,
                        })

                progress(0, 0, "connecting to Plex")
                if bc:
                    bc.flush_soon()

                result = await run_discovery(
                    db, settings, startup.get_plex(), progress, run_id=run_id
                )
                summary.append(
                    f"discovery: +{result.added} new, {result.updated} updated, "
                    f"{result.rotated} rotated, {result.removed} removed"
                )
                if result.unmapped:
                    summary.append(f"{result.unmapped} unmapped")

            # ── providers ──────────────────────────────────────────────
            outcome = None
            if request.providers != []:
                try:
                    max_cost = CostTier(request.max_cost)
                except ValueError:
                    max_cost = CostTier.CHEAP
                outcome = await engine.run(
                    run_id=run_id,
                    provider_ids=request.providers,
                    force=request.force,
                    max_cost=max_cost,
                    settings=settings,
                )
                for p in outcome.providers:
                    if p.processed or p.errors:
                        summary.append(f"{p.provider}: {p.ok} ok, {p.errors} err")
                    elif p.skip_reason:
                        # A provider that did nothing is the outcome most in need
                        # of explaining — silence here reads as "the button is
                        # broken", which is exactly how it was reported.
                        summary.append(
                            f"{p.provider}: 0 of {p.eligible + p.skipped} eligible "
                            f"— {p.skip_reason}"
                        )

            cancelled = bool(outcome and outcome.cancelled)
            message = "; ".join(summary) or "nothing to do"
            await db.execute(
                "UPDATE scan_runs SET status=?, finished_at=?, total=?, done=?, failed=?, "
                "skipped=?, message=? WHERE id=?",
                ("cancelled" if cancelled else "done", int(time.time()),
                 sum(p.eligible for p in outcome.providers) if outcome else 0,
                 outcome.total_processed if outcome else 0,
                 outcome.total_errors if outcome else 0,
                 sum(p.skipped for p in outcome.providers) if outcome else 0,
                 message[:1000], run_id),
            )
            if bc:
                bc.set_state("scan", None)
                bc.emit("scan_done", {
                    "run_id": run_id,
                    "cancelled": cancelled,
                    "done": outcome.total_processed if outcome else 0,
                    "skipped": sum(p.skipped for p in outcome.providers) if outcome else 0,
                    "summary": message,
                })

        except asyncio.CancelledError:
            # Results are flushed per batch, so a cancelled run has almost always
            # completed real work. Record it — reporting done=0 here would imply
            # the whole scan was discarded, and the next run would look like it
            # was starting over when it's actually resuming.
            partial = engine.partial
            done = partial.total_processed if partial else 0
            if partial:
                for p in partial.providers:
                    if p.processed:
                        summary.append(f"{p.provider}: {p.ok} ok before cancel")
            await db.execute(
                "UPDATE scan_runs SET status='cancelled', finished_at=?, done=?, message=? "
                "WHERE id=?",
                (int(time.time()), done,
                 ("; ".join(summary) or "cancelled before any work")[:1000], run_id),
            )
            if bc:
                bc.set_state("scan", None)
                bc.emit("scan_done", {
                    "run_id": run_id, "cancelled": True, "done": done,
                    "summary": f"Cancelled — {done} items completed and saved.",
                })
            raise

        except Exception as exc:
            logger.error("Scan %d failed: %s", run_id, exc, exc_info=True)
            await db.execute(
                "UPDATE scan_runs SET status='error', finished_at=?, message=? WHERE id=?",
                (int(time.time()), str(exc)[:500], run_id),
            )
            if bc:
                bc.set_state("scan", None)
                bc.emit("scan_done", {"run_id": run_id, "error": str(exc), "done": 0})

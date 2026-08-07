"""Path mapping diagnostics.

Getting Plex's paths to line up with the container's mounts is the single most
common setup failure, and the failure is silent — every file-derived fact just
never computes. So this endpoint does more than report a count: it groups the
unmatched paths by prefix so the UI can offer a one-click mapping.
"""
import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend import startup
from backend.common.auth import require_auth
from backend.common.logging_config import get_logger
from backend.models.settings import PathMapping
from backend.utils import path_mapper

logger = get_logger(__name__)
router = APIRouter(prefix="/api/paths", tags=["paths"])

# Directory browsing is confined to these roots. Without this the endpoint is a
# filesystem reader for anyone who can reach the UI.
BROWSE_ROOTS = ("/media", "/mnt", "/data", "/app/data", "/volumes")


@router.get("", dependencies=[Depends(require_auth)])
async def path_health():
    db = startup.db
    rows = await db.fetch_all(
        "SELECT path_status, COUNT(*) AS n FROM items WHERE deleted_at IS NULL "
        "GROUP BY path_status"
    )
    counts = {"mapped": 0, "unmapped": 0, "missing": 0, "unknown": 0}
    for row in rows:
        counts[row["path_status"]] = row["n"]

    unmapped_rows = await db.fetch_all(
        "SELECT plex_path FROM items "
        "WHERE deleted_at IS NULL AND path_status = 'unmapped' AND plex_path IS NOT NULL"
    )
    prefixes = path_mapper.unmapped_prefixes(
        [r["plex_path"] for r in unmapped_rows],
        startup.settings_store.get().path_mappings,
    )

    samples: dict[str, str] = {}
    for row in unmapped_rows:
        for prefix in prefixes:
            if row["plex_path"].startswith(prefix) and prefix not in samples:
                samples[prefix] = row["plex_path"]

    missing_rows = await db.fetch_all(
        "SELECT rating_key, title, local_path FROM items "
        "WHERE deleted_at IS NULL AND path_status = 'missing' LIMIT 25"
    )

    return {
        "counts": counts,
        "unmapped": [
            {"prefix": p, "count": n, "sample": samples.get(p)}
            for p, n in sorted(prefixes.items(), key=lambda kv: -kv[1])
        ],
        "missing": [dict(r) for r in missing_rows],
    }


class PathTestRequest(BaseModel):
    """Unsaved mappings, so the user can check them before committing —
    the same instinct as previewing a rule before syncing it."""
    mappings: list[PathMapping] = []
    samples: list[str] = []


@router.post("/test", dependencies=[Depends(require_auth)])
async def test_mappings(request: PathTestRequest):
    samples = request.samples
    if not samples:
        rows = await startup.db.fetch_all(
            "SELECT DISTINCT plex_path FROM items "
            "WHERE deleted_at IS NULL AND plex_path IS NOT NULL LIMIT 5"
        )
        samples = [r["plex_path"] for r in rows]

    def _run() -> list[dict]:
        out = []
        for sample in samples:
            local = path_mapper.translate(sample, request.mappings)
            out.append({
                "plex_path": sample,
                "local_path": local,
                "mapped": local is not None,
                "exists": bool(local and os.path.exists(local)),
            })
        return out

    results = await asyncio.to_thread(_run)
    return {
        "results": results,
        "count": len(results),
        "ok": sum(1 for r in results if r["exists"]),
    }


@router.get("/browse", dependencies=[Depends(require_auth)])
async def browse(path: str = Query("/media")):
    """List subdirectories, so the container side of a mapping can be picked
    rather than typed. Directories only — never file contents."""
    target = Path(path).resolve()

    if not any(str(target) == root or str(target).startswith(root + "/") for root in BROWSE_ROOTS):
        raise HTTPException(
            status_code=403,
            detail=f"Browsing is limited to {', '.join(BROWSE_ROOTS)}",
        )

    def _run() -> list[dict]:
        if not target.is_dir():
            raise HTTPException(status_code=404, detail=f"{target} is not a directory")
        entries = []
        try:
            for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
                if child.name.startswith("."):
                    continue
                try:
                    if child.is_dir():
                        entries.append({"name": child.name, "path": str(child)})
                except OSError:
                    continue
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return entries[:500]

    dirs = await asyncio.to_thread(_run)
    return {
        "path": str(target),
        "parent": str(target.parent) if str(target) not in BROWSE_ROOTS else None,
        "directories": dirs,
        "count": len(dirs),
    }

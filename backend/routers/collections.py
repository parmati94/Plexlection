"""Collection sync: dry-run diffs, applying, history, posters."""
import hashlib
import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from backend import startup
from backend.common.auth import require_auth
from backend.common.config import config
from backend.common.logging_config import get_logger
from backend.routers.rules import _row_to_rule

logger = get_logger(__name__)
router = APIRouter(prefix="/api/collections", tags=["collections"])

MAX_POSTER_BYTES = 16 * 1024 * 1024


async def _get_rule(rule_id: int) -> dict:
    row = await startup.db.fetch_one("SELECT * FROM rules WHERE id = ?", (rule_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    return _row_to_rule(row)


@router.get("", dependencies=[Depends(require_auth)])
async def list_collections():
    """Every rule with its sync state, for the Collections tab."""
    rows = await startup.db.fetch_all(
        "SELECT r.*, "
        "  (SELECT COUNT(*) FROM sync_membership m WHERE m.rule_id = r.id) AS synced_count "
        "FROM rules r ORDER BY r.name COLLATE NOCASE"
    )
    out = []
    for row in rows:
        rule = _row_to_rule(row)
        label, pin, veto = startup.sync_engine.labels_for(rule["slug"])
        rule["labels"] = {"main": label, "pin": pin, "veto": veto}
        out.append(rule)
    return {
        "collections": out,
        "count": len(out),
        "dry_run_default": startup.settings_store.get().safety.dry_run,
    }


@router.post("/{rule_id}/diff", dependencies=[Depends(require_auth)])
async def diff_collection(rule_id: int):
    """What a sync would change. Never writes to Plex."""
    rule = await _get_rule(rule_id)
    diff = await startup.sync_engine.sync_rule(rule, dry_run=True, force=True)
    return diff.as_dict()


@router.post("/{rule_id}/sync", dependencies=[Depends(require_auth)])
async def sync_collection(rule_id: int, dry_run: bool | None = None, force: bool = False):
    """Apply a rule to Plex.

    `dry_run` defaults to the global safety setting, which starts on — nothing
    reaches Plex until it's deliberately turned off. `force` overrides the
    guards and is what the UI's second-click "sync anyway" sends.
    """
    rule = await _get_rule(rule_id)
    if dry_run is None:
        dry_run = startup.settings_store.get().safety.dry_run

    # SyncGuardError -> 409 with the diff attached (see main.py's handler),
    # so the UI can show exactly what it refused and offer the override.
    diff = await startup.sync_engine.sync_rule(
        rule, dry_run=dry_run, force=force, trigger="manual"
    )

    if startup.broadcaster and diff.applied:
        startup.broadcaster.emit("sync_done", {
            "rule_id": rule_id, "rule_name": rule["name"],
            "added": len(diff.add), "removed": len(diff.remove),
        })
    return diff.as_dict()


@router.post("/sync-all", dependencies=[Depends(require_auth)])
async def sync_all(dry_run: bool | None = None):
    rows = await startup.db.fetch_all("SELECT * FROM rules WHERE enabled = 1")
    if dry_run is None:
        dry_run = startup.settings_store.get().safety.dry_run

    results = []
    for row in rows:
        rule = _row_to_rule(row)
        try:
            diff = await startup.sync_engine.sync_rule(
                rule, dry_run=dry_run, trigger="manual"
            )
            results.append({"rule": rule["name"], "ok": True, **diff.as_dict()})
        except Exception as exc:
            # One guarded or failing rule must not stop the rest.
            results.append({"rule": rule["name"], "ok": False, "detail": str(exc)})
    return {"results": results, "count": len(results), "dry_run": dry_run}


@router.post("/{rule_id}/unsync", dependencies=[Depends(require_auth)])
async def unsync_collection(rule_id: int, strip_all: bool = False):
    """Remove our labels and delete the collection. Leaves :pin/:veto alone.

    `strip_all=true` removes the label from every item in Plex carrying it,
    not just the ones we recorded applying — the reset for when our records and
    Plex have diverged.
    """
    rule = await _get_rule(rule_id)
    result = await startup.sync_engine.unsync_rule(rule, strip_all=strip_all)
    return {"success": True, **result}


@router.get("/{rule_id}/history", dependencies=[Depends(require_auth)])
async def collection_history(rule_id: int, limit: int = 20):
    rows = await startup.db.fetch_all(
        "SELECT * FROM sync_history WHERE rule_id = ? ORDER BY started_at DESC LIMIT ?",
        (rule_id, limit),
    )
    history = []
    for row in rows:
        entry = dict(row)
        try:
            entry["detail"] = json.loads(entry.pop("detail_json") or "{}")
        except json.JSONDecodeError:
            entry["detail"] = {}
        history.append(entry)
    return {"history": history, "count": len(history)}


@router.get("/{rule_id}/poster", dependencies=[Depends(require_auth)])
async def get_poster(rule_id: int):
    """The card image: the uploaded poster when one exists, else Plex's own
    art for the collection — Plex composites one automatically, so every
    synced collection has something to show."""
    rule = await _get_rule(rule_id)

    ref = rule.get("poster_ref")
    if ref and Path(ref).exists():
        media = "image/jpeg" if ref.endswith(".jpg") else "image/png"
        # The frontend cache-busts on poster_fp, so long caching is safe.
        return FileResponse(ref, media_type=media,
                            headers={"Cache-Control": "private, max-age=86400"})

    if rule.get("collection_rating_key"):
        data = await startup.get_plex().collection_poster(rule["collection_rating_key"])
        if data:
            return Response(content=data, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=3600"})

    raise HTTPException(status_code=404, detail="No poster available.")


@router.post("/{rule_id}/poster", dependencies=[Depends(require_auth)])
async def upload_poster(rule_id: int, file: UploadFile = File(...)):
    """Store a poster for the collection.

    Hashed on save so a nightly sync doesn't re-upload an unchanged image —
    every Plex write bumps updatedAt and cascades into client refreshes.
    """
    rule = await _get_rule(rule_id)
    data = await file.read()
    if len(data) > MAX_POSTER_BYTES:
        raise HTTPException(status_code=413, detail="Poster must be 16MB or smaller.")
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=415, detail="That doesn't look like an image.")

    config.poster_dir().mkdir(parents=True, exist_ok=True)
    suffix = ".jpg" if "jpeg" in (file.content_type or "") else ".png"
    path = config.poster_dir() / f"{rule['slug']}{suffix}"
    path.write_bytes(data)

    await startup.db.execute(
        "UPDATE rules SET poster_ref = ?, poster_fp = ?, updated_at = ? WHERE id = ?",
        (str(path), hashlib.sha1(data).hexdigest(), int(time.time()), rule_id),
    )
    return {"success": True, "poster_ref": str(path), "bytes": len(data)}


@router.delete("/{rule_id}/poster", dependencies=[Depends(require_auth)])
async def remove_poster(rule_id: int):
    rule = await _get_rule(rule_id)
    if rule.get("poster_ref"):
        Path(rule["poster_ref"]).unlink(missing_ok=True)
    await startup.db.execute(
        "UPDATE rules SET poster_ref = NULL, poster_fp = NULL WHERE id = ?", (rule_id,)
    )
    return {"success": True}

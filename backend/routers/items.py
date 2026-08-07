"""Library browsing."""
import json

from fastapi import APIRouter, Depends, HTTPException, Query

from backend import startup
from backend.common.auth import require_auth
from backend.common.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/items", tags=["items"])

SORTS = {
    "title": "sort_title COLLATE NOCASE",
    "year": "year",
    "added": "plex_added_at",
    "size": "file_size",
}


@router.get("", dependencies=[Depends(require_auth)])
async def list_items(
    q: str = Query("", description="Title search"),
    path_status: str = Query("", description="mapped|unmapped|missing"),
    sort: str = Query("title"),
    direction: str = Query("asc"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    where = ["deleted_at IS NULL"]
    params: list = []

    if q:
        where.append("title LIKE ? ESCAPE '\\'")
        params.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    if path_status:
        if path_status not in ("mapped", "unmapped", "missing", "unknown"):
            raise HTTPException(status_code=400, detail=f"Unknown path_status {path_status!r}")
        where.append("path_status = ?")
        params.append(path_status)

    order = SORTS.get(sort, SORTS["title"])
    order += " DESC" if direction.lower() == "desc" else " ASC"
    clause = " AND ".join(where)

    total = await startup.db.fetch_val(
        f"SELECT COUNT(*) FROM items WHERE {clause}", params, default=0
    )
    rows = await startup.db.fetch_all(
        f"SELECT id, rating_key, title, year, item_type, path_status, local_path, "
        f"       plex_path, file_size, plex_added_at, facts "
        f"FROM items WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )

    items = []
    for row in rows:
        item = dict(row)
        item["facts"] = json.loads(item.pop("facts") or "{}")
        item["fact_count"] = _count_leaves(item["facts"])
        items.append(item)

    return {"items": items, "count": len(items), "total": total, "offset": offset}


@router.get("/stats", dependencies=[Depends(require_auth)])
async def item_stats():
    db = startup.db
    rows = await db.fetch_all(
        "SELECT path_status, COUNT(*) AS n FROM items WHERE deleted_at IS NULL "
        "GROUP BY path_status"
    )
    by_status = {r["path_status"]: r["n"] for r in rows}
    return {
        "total": sum(by_status.values()),
        "by_path_status": by_status,
        "deleted": await db.fetch_val(
            "SELECT COUNT(*) FROM items WHERE deleted_at IS NOT NULL", default=0
        ),
        "with_facts": await db.fetch_val(
            "SELECT COUNT(*) FROM items WHERE deleted_at IS NULL AND facts != '{}'", default=0
        ),
    }


@router.get("/{item_id}", dependencies=[Depends(require_auth)])
async def get_item(item_id: int):
    """Full detail plus per-provider provenance.

    This is the debugging surface: when a fact looks wrong, it shows which
    provider produced it, when, and whether it's now stale.
    """
    row = await startup.db.fetch_one("SELECT * FROM items WHERE id = ?", (item_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Item not found")

    item = dict(row)
    item["facts"] = json.loads(item.pop("facts") or "{}")

    prov = await startup.db.fetch_all(
        "SELECT provider, schema_version, input_fp, computed_at, status, reason, duration_ms "
        "FROM fact_provenance WHERE item_id = ? ORDER BY provider",
        (item_id,),
    )
    provenance = []
    for p in prov:
        entry = dict(p)
        # A fingerprint that no longer matches the file means these facts
        # describe a version of the file that isn't there any more.
        entry["stale"] = bool(
            entry["input_fp"] and item.get("file_fp") and entry["input_fp"] != item["file_fp"]
        )
        provenance.append(entry)

    return {"item": item, "provenance": provenance}


def _count_leaves(obj) -> int:
    if isinstance(obj, dict):
        return sum(_count_leaves(v) for v in obj.values())
    return 1

"""Discovery: reconcile the local item catalog against Plex.

Runs before any fact provider. Establishes which items exist, where their files
live from this container's point of view, and whether those files changed.

Two behaviours that matter more than they look:

* **rating_key rotation.** Plex reassigns ratingKey when an item is deleted and
  re-added — a routine consequence of an upgrade, a rename, or a library
  refresh. Matching on rating_key alone would orphan every fact and silently
  re-run hours of scanning. Falling back to the (stable) guid keeps the row.

* **Soft delete.** Items that vanish are marked, not dropped. A Plex outage, an
  unmounted library or a mid-scan restart must not destroy months of expensive
  results, and sync history has to stay referentially intact.
"""
import asyncio
import time
from dataclasses import dataclass, field

from backend.clients.plex_client import PlexClient, PlexItem
from backend.common.logging_config import get_logger
from backend.models.settings import Settings
from backend.utils import path_mapper

logger = get_logger(__name__)


@dataclass
class DiscoveryResult:
    sections: list[str] = field(default_factory=list)
    seen: int = 0
    added: int = 0
    updated: int = 0
    rotated: int = 0
    removed: int = 0
    changed_files: int = 0
    mapped: int = 0
    unmapped: int = 0
    missing: int = 0

    def as_dict(self) -> dict:
        return {
            "sections": self.sections,
            "seen": self.seen,
            "added": self.added,
            "updated": self.updated,
            "rotated": self.rotated,
            "removed": self.removed,
            "changed_files": self.changed_files,
            "mapped": self.mapped,
            "unmapped": self.unmapped,
            "missing": self.missing,
        }


_INSERT_SQL = """
INSERT INTO items (
  library_key, rating_key, guid, tmdb_id, imdb_id, item_type, title, sort_title, year,
  plex_added_at, plex_updated_at, plex_duration_ms, part_id, plex_path, local_path,
  path_status, file_size, file_mtime, file_fp, facts, first_seen, last_seen, seen_run,
  deleted_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'{}',?,?,?,NULL)
"""

_UPDATE_SQL = """
UPDATE items SET
  rating_key = ?, guid = ?, tmdb_id = ?, imdb_id = ?, item_type = ?, title = ?,
  sort_title = ?, year = ?, plex_added_at = ?, plex_updated_at = ?,
  plex_duration_ms = ?, part_id = ?, plex_path = ?, local_path = ?, path_status = ?,
  file_size = ?, file_mtime = ?, file_fp = ?, last_seen = ?, seen_run = ?,
  deleted_at = NULL
WHERE id = ?
"""


async def _resolve_page(items: list[PlexItem], settings: Settings) -> list[path_mapper.MappedFile]:
    """Translate and stat a page of paths off the event loop.

    One to_thread hop for the whole page rather than per item: on a 98%-full
    mergerfs pool each stat is cheap but the hop is not.
    """
    mappings = settings.path_mappings
    deep = settings.scan.deep_fingerprint

    def _run() -> list[path_mapper.MappedFile]:
        return [path_mapper.resolve(item.plex_path, mappings, deep) for item in items]

    return await asyncio.to_thread(_run)


async def run_discovery(
    db,
    settings: Settings,
    plex: PlexClient,
    progress=None,
    run_id: int | None = None,
) -> DiscoveryResult:
    """Reconcile every configured library section.

    `run_id` identifies this run; rows stamped with it were seen, everything
    else in the scanned sections was not. `progress(done, total, label)` is
    called as pages complete.
    """
    result = DiscoveryResult()
    run_at = int(time.time())
    if run_id is None:
        # Standalone/test invocation: any value distinct from stored ones works.
        run_id = await db.fetch_val(
            "SELECT COALESCE(MAX(seen_run), 0) + 1 FROM items", default=1
        )

    sections = settings.plex.libraries
    if not sections:
        logger.warning("No Plex libraries selected — nothing to discover")
        return result

    for section_key in sections:
        result.sections.append(section_key)
        try:
            total = await plex.section_size(section_key)
        except Exception as exc:
            logger.warning("Could not size section %s: %s", section_key, exc)
            total = 0

        logger.info("🔎 Discovering section %s (%d items)", section_key, total)

        async for page in plex.iter_items(section_key):
            resolved = await _resolve_page(page, settings)
            await _upsert_page(db, section_key, page, resolved, run_at, run_id, result)
            result.seen += len(page)
            if progress:
                progress(result.seen, total, f"section {section_key}")

    # Anything in the scanned sections this run didn't stamp is gone from Plex.
    placeholders = ",".join("?" * len(sections))
    stale_clause = (
        f"deleted_at IS NULL AND (seen_run IS NULL OR seen_run != ?) "
        f"AND library_key IN ({placeholders})"
    )
    result.removed = await db.fetch_val(
        f"SELECT COUNT(*) FROM items WHERE {stale_clause}", (run_id, *sections), default=0
    )
    if result.removed:
        await db.execute(
            f"UPDATE items SET deleted_at = ? WHERE {stale_clause}",
            (run_at, run_id, *sections),
        )

    counts = await db.fetch_all(
        "SELECT path_status, COUNT(*) AS n FROM items "
        "WHERE deleted_at IS NULL GROUP BY path_status"
    )
    for row in counts:
        if row["path_status"] == path_mapper.MAPPED:
            result.mapped = row["n"]
        elif row["path_status"] == path_mapper.UNMAPPED:
            result.unmapped = row["n"]
        elif row["path_status"] == path_mapper.MISSING:
            result.missing = row["n"]

    logger.info(
        "✅ Discovery: %d seen, +%d new, %d updated, %d rotated, %d removed "
        "(%d mapped / %d unmapped / %d missing)",
        result.seen, result.added, result.updated, result.rotated, result.removed,
        result.mapped, result.unmapped, result.missing,
    )
    return result


async def _upsert_page(
    db,
    section_key: str,
    page: list[PlexItem],
    resolved: list[path_mapper.MappedFile],
    run_at: int,
    run_id: int,
    result: DiscoveryResult,
) -> None:
    if not page:
        return

    rating_keys = [item.rating_key for item in page]
    placeholders = ",".join("?" * len(rating_keys))
    rows = await db.fetch_all(
        f"SELECT id, rating_key, guid, file_fp FROM items "
        f"WHERE library_key = ? AND rating_key IN ({placeholders})",
        (section_key, *rating_keys),
    )
    by_rating_key = {r["rating_key"]: r for r in rows}

    # Candidates for rotation: same guid, not seen in this run.
    unmatched_guids = [
        item.guid for item in page
        if item.rating_key not in by_rating_key and item.guid
    ]
    by_guid: dict[str, dict] = {}
    if unmatched_guids:
        gph = ",".join("?" * len(unmatched_guids))
        grows = await db.fetch_all(
            f"SELECT id, rating_key, guid, file_fp FROM items "
            f"WHERE library_key = ? AND (seen_run IS NULL OR seen_run != ?) "
            f"AND guid IN ({gph})",
            (section_key, run_id, *unmatched_guids),
        )
        by_guid = {r["guid"]: r for r in grows}

    statements: list[tuple[str, tuple]] = []

    for item, mapped in zip(page, resolved):
        existing = by_rating_key.get(item.rating_key)
        rotated = False
        if existing is None and item.guid:
            existing = by_guid.get(item.guid)
            if existing is not None:
                rotated = True
                logger.debug(
                    "🔁 ratingKey rotated for %s: %s -> %s",
                    item.title, existing["rating_key"], item.rating_key,
                )

        # Column order here must match both _INSERT_SQL and _UPDATE_SQL.
        common = (
            item.guid, item.tmdb_id, item.imdb_id, item.item_type, item.title,
            item.sort_title, item.year, item.added_at, item.updated_at,
            item.duration_ms, item.part_id, item.plex_path, mapped.local_path,
            mapped.status, mapped.size, mapped.mtime, mapped.fingerprint,
        )

        if existing is None:
            statements.append((
                _INSERT_SQL,
                (section_key, item.rating_key, *common, run_at, run_at, run_id),
            ))
            result.added += 1
        else:
            if existing["file_fp"] != mapped.fingerprint and mapped.fingerprint is not None:
                # Inputs to every file-derived provider changed. Facts are left in
                # place — a 3am file swap must not empty a collection — but the
                # fingerprint mismatch marks them stale for the next scan.
                result.changed_files += 1
            statements.append((
                _UPDATE_SQL,
                (item.rating_key, *common, run_at, run_id, existing["id"]),
            ))
            if rotated:
                result.rotated += 1
            else:
                result.updated += 1

    await db.transaction(statements)

"""Plex client.

plexapi is synchronous and chatty, so every call is wrapped in to_thread and
section listing is paged rather than pulled in one shot — section.all() on a
2,000-item library blocks a thread for a long time and holds the whole response
in memory.

Connect with a server token (X-Plex-Token), never a plex.tv account login: an
account login adds a MyPlex round-trip to every connection and can rate-limit.
"""
import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from backend.common.errors import NotConfiguredError
from backend.common.logging_config import get_logger

logger = get_logger(__name__)

PAGE_SIZE = 200

# Plex libtype -> our item_type
LIBTYPE_MOVIE = "movie"
LIBTYPE_SHOW = "show"
LIBTYPE_EPISODE = "episode"

# What a section of each type contributes to the catalogue. Seasons are
# deliberately absent: Plex can't put one in a collection.
SECTION_LIBTYPES = {
    "movie": (LIBTYPE_MOVIE,),
    "show": (LIBTYPE_SHOW, LIBTYPE_EPISODE),
}


@dataclass
class PlexItem:
    """Flattened Plex item. Only the fields discovery persists."""
    rating_key: str
    guid: str | None
    item_type: str
    title: str
    sort_title: str | None
    year: int | None
    added_at: int | None
    updated_at: int | None
    tmdb_id: int | None
    imdb_id: str | None
    part_id: str | None
    plex_path: str | None
    plex_size: int | None
    duration_ms: int | None
    # ── TV ────────────────────────────────────────────────────────────────
    tvdb_id: int | None = None
    parent_key: str | None = None      # episode -> its show's ratingKey
    season_number: int | None = None
    episode_number: int | None = None
    child_count: int | None = None     # show -> seasons
    leaf_count: int | None = None      # show -> episodes Plex knows about
    viewed_leaf_count: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlexSection:
    key: str
    title: str
    type: str
    item_count: int | None = None


def _parse_guids(video) -> tuple[int | None, str | None, int | None]:
    """Pull tmdb/imdb/tvdb ids out of the Plex agent's guid list.

    Shows carry all three; tvdb is the one Sonarr keys on.
    """
    tmdb_id: int | None = None
    imdb_id: str | None = None
    tvdb_id: int | None = None
    for guid in getattr(video, "guids", None) or []:
        gid = getattr(guid, "id", "") or ""
        try:
            if gid.startswith("tmdb://"):
                tmdb_id = int(gid.split("://", 1)[1])
            elif gid.startswith("tvdb://"):
                tvdb_id = int(gid.split("://", 1)[1])
            elif gid.startswith("imdb://"):
                imdb_id = gid.split("://", 1)[1]
        except ValueError:
            continue
    return tmdb_id, imdb_id, tvdb_id


def _primary_part(video) -> tuple[str | None, str | None, int | None]:
    """(part_id, file path, size) of the largest media part.

    Multi-version items (Plex 'editions') have several Media entries; the largest
    is the one worth probing.
    """
    best = None
    for media in getattr(video, "media", None) or []:
        for part in getattr(media, "parts", None) or []:
            size = getattr(part, "size", None) or 0
            if best is None or size > best[2]:
                best = (str(getattr(part, "id", "")), getattr(part, "file", None), size)
    return best if best else (None, None, None)


class PlexClient:
    def __init__(self, url: str, token: str, timeout: int = 30, verify_ssl: bool = True):
        self.url = (url or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self._server = None

    def _require(self) -> None:
        if not self.url or not self.token:
            raise NotConfiguredError("Plex is not configured — set the server URL and token in Settings.")

    def _connect_sync(self):
        if self._server is not None:
            return self._server
        from plexapi.server import PlexServer  # imported lazily: heavy module
        import requests

        session = requests.Session()
        session.verify = self.verify_ssl
        self._server = PlexServer(self.url, self.token, session=session, timeout=self.timeout)
        return self._server

    async def connect(self):
        self._require()
        return await asyncio.to_thread(self._connect_sync)

    # ── health ────────────────────────────────────────────────────────────
    async def test(self) -> dict:
        """Used by the Settings 'Test connection' button."""
        self._require()

        def _run() -> dict:
            server = self._connect_sync()
            return {
                "ok": True,
                "name": server.friendlyName,
                "version": server.version,
                "platform": server.platform,
                "machine_identifier": server.machineIdentifier,
            }

        return await asyncio.to_thread(_run)

    # ── libraries ─────────────────────────────────────────────────────────
    async def sections(self) -> list[PlexSection]:
        self._require()

        def _run() -> list[PlexSection]:
            server = self._connect_sync()
            out = []
            for section in server.library.sections():
                count = None
                try:
                    count = section.totalSize
                except Exception:
                    pass
                out.append(PlexSection(
                    key=str(section.key),
                    title=section.title,
                    type=section.type,
                    item_count=count,
                ))
            return out

        return await asyncio.to_thread(_run)

    async def section_size(self, section_key: str, libtype: str = LIBTYPE_MOVIE) -> int:
        self._require()

        def _run() -> int:
            server = self._connect_sync()
            section = server.library.sectionByID(int(section_key))
            return int(section.totalViewSize(libtype=libtype))

        return await asyncio.to_thread(_run)

    async def iter_items(
        self, section_key: str, libtype: str = LIBTYPE_MOVIE, page_size: int = PAGE_SIZE
    ) -> AsyncIterator[list[PlexItem]]:
        """Yield pages of items. Paged so a large library never blocks a thread
        for more than one page's worth of work."""
        self._require()

        def _fetch_page(offset: int) -> list:
            server = self._connect_sync()
            section = server.library.sectionByID(int(section_key))
            return section.search(
                libtype=libtype,
                container_start=offset,
                container_size=page_size,
                maxresults=page_size,
            )

        offset = 0
        while True:
            videos = await asyncio.to_thread(_fetch_page, offset)
            if not videos:
                break

            page: list[PlexItem] = []
            for video in videos:
                tmdb_id, imdb_id, tvdb_id = _parse_guids(video)
                part_id, path, size = _primary_part(video)

                # Episodes hang off their show. grandparentRatingKey is the show;
                # parentRatingKey is the season, which we don't index.
                parent_key = None
                if libtype == LIBTYPE_EPISODE:
                    gp = getattr(video, "grandparentRatingKey", None)
                    parent_key = str(gp) if gp is not None else None

                page.append(PlexItem(
                    rating_key=str(video.ratingKey),
                    guid=getattr(video, "guid", None),
                    item_type=libtype,
                    title=video.title or "(untitled)",
                    sort_title=getattr(video, "titleSort", None) or video.title,
                    year=getattr(video, "year", None),
                    added_at=int(video.addedAt.timestamp()) if getattr(video, "addedAt", None) else None,
                    updated_at=int(video.updatedAt.timestamp()) if getattr(video, "updatedAt", None) else None,
                    tmdb_id=tmdb_id,
                    imdb_id=imdb_id,
                    tvdb_id=tvdb_id,
                    part_id=part_id,
                    plex_path=path,
                    plex_size=size,
                    duration_ms=getattr(video, "duration", None),
                    parent_key=parent_key,
                    season_number=getattr(video, "parentIndex", None) if libtype == LIBTYPE_EPISODE else None,
                    episode_number=getattr(video, "index", None) if libtype == LIBTYPE_EPISODE else None,
                    child_count=getattr(video, "childCount", None) if libtype == LIBTYPE_SHOW else None,
                    leaf_count=getattr(video, "leafCount", None) if libtype == LIBTYPE_SHOW else None,
                    viewed_leaf_count=getattr(video, "viewedLeafCount", None) if libtype == LIBTYPE_SHOW else None,
                ))

            yield page

            if len(videos) < page_size:
                break
            offset += page_size

    # ── labels ────────────────────────────────────────────────────────────
    async def rating_keys_with_label(self, section_key: str, label: str,
                                     libtype: str = LIBTYPE_MOVIE) -> set[str]:
        """Every item in the section carrying `label`."""
        self._require()

        def _run() -> set[str]:
            server = self._connect_sync()
            section = server.library.sectionByID(int(section_key))
            try:
                found = section.search(label=label, libtype=libtype)
            except Exception:
                # An unused label isn't a filter option yet on some PMS versions.
                return set()
            return {str(v.ratingKey) for v in found}

        return await asyncio.to_thread(_run)

    async def apply_labels(
        self, section_key: str, rating_keys: list[str], label: str, add: bool
    ) -> tuple[int, list[str]]:
        """Add or remove one label across many items.

        Prefers plexapi's multi-edit path, which is roughly an order of magnitude
        fewer round-trips than per-item edits on a large collection, and falls
        back per item when that surface isn't available or errors — it varies
        across plexapi and PMS versions.

        Returns (changed, errors).
        """
        self._require()
        if not rating_keys:
            return 0, []

        def _run() -> tuple[int, list[str]]:
            server = self._connect_sync()
            section = server.library.sectionByID(int(section_key))
            items = []
            errors: list[str] = []
            for key in rating_keys:
                try:
                    items.append(server.fetchItem(int(key)))
                except Exception as exc:
                    errors.append(f"{key}: {exc}")
            if not items:
                return 0, errors

            try:
                edit = section.batchMultiEdits(items)
                (edit.addLabel(label) if add else edit.removeLabel(label))
                edit.saveMultiEdits()
                return len(items), errors
            except Exception as exc:
                logger.debug("batchMultiEdits unavailable (%s); falling back per item", exc)

            changed = 0
            for item in items:
                try:
                    item.addLabel(label) if add else item.removeLabel(label)
                    changed += 1
                except Exception as exc:
                    errors.append(f"{item.ratingKey}: {exc}")
            return changed, errors

        return await asyncio.to_thread(_run)

    # ── collections ───────────────────────────────────────────────────────
    async def find_collection(self, section_key: str, title: str):
        self._require()

        def _run():
            server = self._connect_sync()
            section = server.library.sectionByID(int(section_key))
            for collection in section.collections():
                if collection.title == title:
                    return collection
            return None

        return await asyncio.to_thread(_run)

    def _collection_by_key(self, server, rating_key) -> object | None:
        """The collection a rule remembers owning, if Plex still has it.

        A missing or repurposed rating key (collection deleted by hand) is a
        normal answer, not an error — the caller falls back to a title lookup.
        """
        if not rating_key:
            return None
        try:
            candidate = server.fetchItem(int(rating_key))
        except Exception:
            return None
        return candidate if getattr(candidate, "type", None) == "collection" else None

    async def ensure_smart_collection(
        self, section_key: str, title: str, labels: list[str],
        libtype: str = LIBTYPE_MOVIE, rating_key: str | None = None,
    ) -> dict:
        """Create (or verify) a smart collection filtered on `labels`.

        `rating_key` is the collection this rule already owns. When its title no
        longer matches, the collection is *renamed* — looking up by title alone
        here would create a sibling under the new name and strand the old one.

        `createCollection(smart=True, filters=...)` is the most version-sensitive
        call in this client, so the result is read back and its size compared to
        what the filter should produce. A mismatch is reported rather than
        assumed away, letting the caller fall back to static membership.
        """
        self._require()

        def _run() -> dict:
            server = self._connect_sync()
            section = server.library.sectionByID(int(section_key))

            renamed = False
            existing = self._collection_by_key(server, rating_key)
            if existing is not None and existing.title != title:
                existing.editTitle(title)
                renamed = True

            if existing is None:
                for collection in section.collections():
                    if collection.title == title:
                        existing = collection
                        break

            created = False
            if existing is None:
                existing = section.createCollection(
                    title=title, smart=True, libtype=libtype, filters={"label": labels}
                )
                created = True

            try:
                size = len(existing.items())
            except Exception:
                size = None

            return {
                "created": created,
                "renamed": renamed,
                "smart": bool(getattr(existing, "smart", False)),
                "rating_key": str(existing.ratingKey),
                "size": size,
            }

        return await asyncio.to_thread(_run)

    async def create_static_collection(self, section_key: str, title: str,
                                       rating_keys: list[str]) -> dict:
        """Fallback when the smart filter can't be trusted."""
        self._require()

        def _run() -> dict:
            server = self._connect_sync()
            section = server.library.sectionByID(int(section_key))
            items = []
            for key in rating_keys:
                try:
                    items.append(server.fetchItem(int(key)))
                except Exception:
                    continue
            collection = section.createCollection(title=title, items=items)
            return {"created": True, "smart": False,
                    "rating_key": str(collection.ratingKey), "size": len(items)}

        return await asyncio.to_thread(_run)

    async def update_collection(
        self, section_key: str, title: str,
        sort_title: str | None = None, summary: str | None = None,
        poster_path: str | None = None, sort_mode: str | None = None,
        rating_key: str | None = None,
    ) -> list[str]:
        """Apply presentation metadata. Returns the fields actually changed.

        Only writes what differs — every edit bumps Plex's updatedAt and cascades
        into client refreshes, so a nightly no-op sync shouldn't touch anything.
        """
        self._require()

        def _run() -> list[str]:
            server = self._connect_sync()
            section = server.library.sectionByID(int(section_key))
            collection = self._collection_by_key(server, rating_key)
            if collection is None:
                for candidate in section.collections():
                    if candidate.title == title:
                        collection = candidate
                        break
            if collection is None:
                return []

            changed: list[str] = []
            if sort_title and getattr(collection, "titleSort", None) != sort_title:
                collection.editSortTitle(sort_title)
                changed.append("sort_title")
            if summary and getattr(collection, "summary", None) != summary:
                collection.editSummary(summary)
                changed.append("summary")
            if sort_mode:
                try:
                    collection.sortUpdate(sort=sort_mode)
                    changed.append("sort_mode")
                except Exception as exc:
                    logger.debug("sortUpdate failed: %s", exc)
            if poster_path:
                collection.uploadPoster(filepath=poster_path)
                changed.append("poster")
            return changed

        return await asyncio.to_thread(_run)

    async def delete_collection(self, section_key: str, title: str,
                                rating_key: str | None = None) -> bool:
        self._require()

        def _run() -> bool:
            server = self._connect_sync()
            owned = self._collection_by_key(server, rating_key)
            if owned is not None:
                owned.delete()
                return True
            section = server.library.sectionByID(int(section_key))
            for collection in section.collections():
                if collection.title == title:
                    collection.delete()
                    return True
            return False

        return await asyncio.to_thread(_run)

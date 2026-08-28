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

# libtype -> (Plex's `type` query parameter, the element tag it comes back as).
# Shows are Directory elements; movies and episodes are Video.
_LIBTYPE_QUERY = {
    LIBTYPE_MOVIE: (1, "Video"),
    LIBTYPE_SHOW: (2, "Directory"),
    LIBTYPE_EPISODE: (4, "Video"),
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


def _int(value: str | None) -> int | None:
    """XML attributes are always strings, and absent ones are None."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_guids(element) -> tuple[int | None, str | None, int | None]:
    """Pull tmdb/imdb/tvdb ids out of the Plex agent's <Guid> children.

    Shows carry all three; tvdb is the one Sonarr keys on. These only appear
    when the listing was requested with includeGuids=1 — see iter_items.
    """
    tmdb_id: int | None = None
    imdb_id: str | None = None
    tvdb_id: int | None = None
    for guid in element.findall("Guid"):
        gid = guid.get("id") or ""
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


def _primary_part(element) -> tuple[str | None, str | None, int]:
    """(part_id, file path, size) of the largest media part.

    Multi-version items (Plex 'editions') have several Media entries; the largest
    is the one worth probing.
    """
    best = None
    for part in element.findall("./Media/Part"):
        size = _int(part.get("size")) or 0
        if best is None or size > best[2]:
            best = (part.get("id"), part.get("file"), size)
    return best if best else (None, None, 0)


def _backfill_guids(server, elements: list) -> None:
    """Fill in <Guid> children the section listing left out.

    Items Plex couldn't match to an agent get a `local://` guid, and their
    listing entry carries no <Guid> children even with includeGuids=1 — but the
    per-item metadata endpoint often still has them. plexapi hides this by
    silently reloading each such item the moment you touch `.guids`, which is
    both invisible and one request per item.

    Doing it explicitly costs one batched request per page instead: measured at
    36 requests and 1.4s to recover the ids for 835 unmatched episodes, against
    835 requests the lazy way. Without it those items reach the fact providers
    with no tmdb/imdb/tvdb id and quietly drop out of everything keyed on one.

    Best effort — the scan is still valid without these, so a failure here is
    logged and skipped rather than raised.
    """
    missing = [el for el in elements if not el.findall("Guid")]
    keys = [el.get("ratingKey") for el in missing if el.get("ratingKey")]
    if not keys:
        return

    try:
        found = server.query("/library/metadata/" + ",".join(keys))
    except Exception as exc:
        logger.debug("Guid backfill failed for %d item(s): %s", len(keys), exc)
        return

    by_key = {
        el.get("ratingKey"): guids
        for el in found
        if el.get("ratingKey") and (guids := el.findall("Guid"))
    }
    for el in missing:
        for guid in by_key.get(el.get("ratingKey"), ()):
            el.append(guid)


def _item_from_xml(element, libtype: str) -> PlexItem:
    """One <Video>/<Directory> element from a section listing.

    Field for field what plexapi's own `_loadData` does for these types — it
    reads the same attributes off the same element and casts them. Kept
    deliberately literal rather than clever, because `test_plex_parity.py`
    asserts it against plexapi over a live library and any divergence here
    shows up there as a diff.
    """
    is_episode = libtype == LIBTYPE_EPISODE
    is_show = libtype == LIBTYPE_SHOW
    tmdb_id, imdb_id, tvdb_id = _parse_guids(element)
    part_id, path, size = _primary_part(element)
    title = element.get("title")

    return PlexItem(
        rating_key=element.get("ratingKey"),
        guid=element.get("guid"),
        item_type=libtype,
        title=title or "(untitled)",
        sort_title=element.get("titleSort") or title,
        year=_int(element.get("year")),
        added_at=_int(element.get("addedAt")),
        updated_at=_int(element.get("updatedAt")),
        tmdb_id=tmdb_id,
        imdb_id=imdb_id,
        tvdb_id=tvdb_id,
        part_id=part_id,
        plex_path=path,
        plex_size=size,
        duration_ms=_int(element.get("duration")),
        # Episodes hang off their show. grandparentRatingKey is the show;
        # parentRatingKey is the season, which we don't index.
        parent_key=element.get("grandparentRatingKey") if is_episode else None,
        season_number=_int(element.get("parentIndex")) if is_episode else None,
        episode_number=_int(element.get("index")) if is_episode else None,
        child_count=_int(element.get("childCount")) if is_show else None,
        leaf_count=_int(element.get("leafCount")) if is_show else None,
        viewed_leaf_count=_int(element.get("viewedLeafCount")) if is_show else None,
    )


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
        """How many items of `libtype` the section holds, as Plex counts them.

        Must agree with what iter_items yields, because discovery compares the
        two to decide whether a pass read the whole section. plexapi's
        totalViewSize is not usable here: it defaults to includeCollections=True,
        so a movie library with 7 collections reports 2131 for 2124 movies —
        which stalls the scan progress bar short of 100% and reads to discovery
        as 7 items it failed to fetch.
        """
        self._require()
        type_id, _ = _LIBTYPE_QUERY.get(libtype, _LIBTYPE_QUERY[LIBTYPE_MOVIE])

        def _run() -> int:
            server = self._connect_sync()
            element = server.query(
                f"/library/sections/{int(section_key)}/all"
                f"?type={type_id}&includeCollections=0"
                f"&X-Plex-Container-Start=0&X-Plex-Container-Size=0"
            )
            return int(element.get("totalSize") or 0)

        return await asyncio.to_thread(_run)

    async def iter_items(
        self, section_key: str, libtype: str = LIBTYPE_MOVIE, page_size: int = PAGE_SIZE
    ) -> AsyncIterator[list[PlexItem]]:
        """Yield pages of items.

        Reads the listing as XML instead of through plexapi's object layer.
        Same request, same fields — but plexapi spends ~2.4ms per item building
        a `Video`, which on a large TV library is most of discovery's wall
        clock (20k episodes: ~48s of object construction against ~13s here).
        `server.query` is still plexapi, so its session, auth, timeout and
        retry handling all still apply; only the parsing is ours.

        Paged rather than fetched whole: the response for a large library is
        measured in gigabytes, and paging costs nothing (measured identical).
        """
        self._require()
        type_id, tag = _LIBTYPE_QUERY.get(libtype, _LIBTYPE_QUERY[LIBTYPE_MOVIE])

        def _fetch_page(offset: int) -> list[PlexItem]:
            server = self._connect_sync()
            # includeGuids is off by default and is not optional for us: without
            # it Plex omits the <Guid> children entirely, every tmdb/imdb/tvdb id
            # silently becomes NULL, and the TMDB and *arr providers have nothing
            # left to key on. Parsed in this thread so the event loop never wears
            # the deserialisation cost.
            element = server.query(
                f"/library/sections/{int(section_key)}/all"
                f"?type={type_id}&includeGuids=1"
                f"&X-Plex-Container-Start={offset}&X-Plex-Container-Size={page_size}"
            )
            elements = element.findall(tag)
            _backfill_guids(server, elements)
            return [_item_from_xml(el, libtype) for el in elements]

        offset = 0
        while True:
            page = await asyncio.to_thread(_fetch_page, offset)
            if not page:
                break

            yield page

            if len(page) < page_size:
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

    async def collection_poster(self, rating_key: str,
                                width: int = 240, height: int = 360) -> bytes | None:
        """The collection's poster, transcoded to card size.

        Plex composites art automatically for collections with no custom
        poster, so this answers for any collection that exists. None (not an
        error) when it doesn't — the card just shows its placeholder.
        """
        self._require()

        def _run() -> bytes | None:
            server = self._connect_sync()
            try:
                item = server.fetchItem(int(rating_key))
            except Exception:
                return None
            thumb = getattr(item, "thumb", None)
            if not thumb:
                return None
            try:
                url = server.transcodeImage(thumb, height=height, width=width)
            except Exception:
                url = server.url(thumb, includeToken=True)
            try:
                response = server._session.get(url, timeout=15)
                response.raise_for_status()
                return response.content
            except Exception as exc:
                logger.debug("Poster fetch failed for %s: %s", rating_key, exc)
                return None

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

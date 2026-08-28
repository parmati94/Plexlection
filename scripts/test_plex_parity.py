#!/usr/bin/env python3
"""Assert our XML parsing matches plexapi, field for field, against live Plex.

Discovery reads section listings as XML rather than through plexapi's object
layer, because building a `Video` per item costs ~2.4ms and dominates the scan
on a large TV library. The saving is only safe while the two agree exactly —
a field we parse differently becomes a wrong row in `items`, and a field we
drop becomes a NULL that quietly strands a fact provider. (That is not
hypothetical: the first draft of the parser omitted `includeGuids=1` and lost
every tmdb/imdb/tvdb id.)

So this is not a unit test with a fixture. It runs against the real server and
diffs every field of every item, which is what makes it able to catch a PMS
upgrade changing the response shape. Run it after upgrading Plex.

    python scripts/test_plex_parity.py            # sample each section
    python scripts/test_plex_parity.py --full     # every item, slow

Reads connection details from the app's own settings, so it needs no
configuration of its own.
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.clients.plex_client import SECTION_LIBTYPES, PlexClient  # noqa: E402
from backend.common.config import config  # noqa: E402
from backend.common.settings import SettingsStore  # noqa: E402
from backend.db.database import Database  # noqa: E402

PASS, FAIL = "\033[32m✓\033[0m", "\033[31m✗\033[0m"

# Everything discovery persists. Compared individually so a failure names the
# field rather than just saying two objects differ.
FIELDS = (
    "rating_key", "guid", "item_type", "title", "sort_title", "year",
    "added_at", "updated_at", "tmdb_id", "imdb_id", "tvdb_id",
    "part_id", "plex_path", "plex_size", "duration_ms",
    "parent_key", "season_number", "episode_number",
    "child_count", "leaf_count", "viewed_leaf_count",
)


def via_plexapi(server, section_key: str, libtype: str, limit: int) -> dict:
    """The same fields, built the way discovery used to build them.

    Deliberately duplicated from the old implementation rather than imported:
    this is the reference we are testing against, so it must not change when
    the parser does.
    """
    section = server.library.sectionByID(int(section_key))
    out = {}
    for video in section.search(libtype=libtype, container_start=0,
                                container_size=limit, maxresults=limit):
        tmdb_id = imdb_id = tvdb_id = None
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

        best = None
        for media in getattr(video, "media", None) or []:
            for part in getattr(media, "parts", None) or []:
                size = getattr(part, "size", None) or 0
                if best is None or size > best[2]:
                    best = (str(getattr(part, "id", "")),
                            getattr(part, "file", None), size)
        part_id, path, size = best if best else (None, None, 0)

        is_ep, is_show = libtype == "episode", libtype == "show"
        gp = getattr(video, "grandparentRatingKey", None)
        out[str(video.ratingKey)] = {
            "rating_key": str(video.ratingKey),
            "guid": getattr(video, "guid", None),
            "item_type": libtype,
            "title": video.title or "(untitled)",
            "sort_title": getattr(video, "titleSort", None) or video.title,
            "year": getattr(video, "year", None),
            "added_at": int(video.addedAt.timestamp()) if getattr(video, "addedAt", None) else None,
            "updated_at": int(video.updatedAt.timestamp()) if getattr(video, "updatedAt", None) else None,
            "tmdb_id": tmdb_id, "imdb_id": imdb_id, "tvdb_id": tvdb_id,
            "part_id": part_id, "plex_path": path, "plex_size": size,
            "duration_ms": getattr(video, "duration", None),
            "parent_key": (str(gp) if gp is not None else None) if is_ep else None,
            "season_number": getattr(video, "parentIndex", None) if is_ep else None,
            "episode_number": getattr(video, "index", None) if is_ep else None,
            "child_count": getattr(video, "childCount", None) if is_show else None,
            "leaf_count": getattr(video, "leafCount", None) if is_show else None,
            "viewed_leaf_count": getattr(video, "viewedLeafCount", None) if is_show else None,
        }
    return out


async def via_xml(client: PlexClient, section_key: str, libtype: str, limit: int) -> dict:
    """What discovery actually uses — the real iter_items, not a copy of it.

    Driven through the shipping code path deliberately: a reimplementation here
    would pass while the real one was broken, which is the exact failure this
    test exists to prevent (the guid backfill lives in iter_items, and an
    inlined query would silently skip it).
    """
    out = {}
    async for page in client.iter_items(section_key, libtype):
        for item in page:
            out[item.rating_key] = {f: getattr(item, f) for f in FIELDS}
            if len(out) >= limit:
                return out
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true",
                    help="every item in every section, not a sample")
    ap.add_argument("--limit", type=int, default=800,
                    help="items per libtype when not --full (default 800)")
    args = ap.parse_args()

    db = Database(config.db_path())
    await db.start()
    store = SettingsStore(db)
    await store.load()
    settings = store.get()

    if not (settings.plex.url and settings.plex.token):
        print("Plex is not configured — nothing to compare against.")
        return 1

    client = PlexClient(settings.plex.url, settings.plex.token,
                        settings.plex.timeout_s, settings.plex.verify_ssl)
    sections = {s.key: s for s in await client.sections()}
    server = await client.connect()

    failures = total_items = 0
    for key in settings.plex.libraries:
        section = sections.get(key)
        if section is None:
            print(f"\n{FAIL} section {key} is selected but Plex doesn't have it")
            failures += 1
            continue

        print(f"\n── section {key}: {section.title} ({section.type}) ──")
        for libtype in SECTION_LIBTYPES.get(section.type, ()):
            limit = await client.section_size(key, libtype) if args.full else args.limit

            t = time.time()
            want = await asyncio.to_thread(via_plexapi, server, key, libtype, limit)
            t_api = time.time() - t
            t = time.time()
            got = await via_xml(client, key, libtype, limit)
            t_xml = time.time() - t

            total_items += len(want)
            speedup = f"{t_api / t_xml:.1f}x" if t_xml else "—"
            print(f"  {libtype:8} n={len(want):<6} plexapi {t_api:6.2f}s  "
                  f"xml {t_xml:5.2f}s  {speedup:>6}")

            missing = set(want) - set(got)
            extra = set(got) - set(want)
            if missing or extra:
                failures += 1
                print(f"    {FAIL} key sets differ: {len(missing)} missing, "
                      f"{len(extra)} unexpected")

            by_field: dict[str, list] = {}
            for rating_key in want.keys() & got.keys():
                for f in FIELDS:
                    if want[rating_key][f] != got[rating_key][f]:
                        by_field.setdefault(f, []).append(
                            (rating_key, want[rating_key][f], got[rating_key][f]))

            if by_field:
                failures += 1
                for f, examples in by_field.items():
                    key_, w, g = examples[0]
                    print(f"    {FAIL} {f}: {len(examples)} differ — "
                          f"ratingKey {key_} plexapi={w!r} xml={g!r}")
            elif not (missing or extra):
                print(f"    {PASS} all {len(FIELDS)} fields match on {len(want)} items")

    await db.stop()
    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'} "
          f"— {total_items} items compared")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

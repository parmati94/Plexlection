#!/usr/bin/env python3
"""Discovery logic tests against a stub Plex, no server required.

Covers the cases that are easy to get wrong and expensive to get wrong:
new items, updates, ratingKey rotation, soft delete, resurrection, and
file-change detection.

    python scripts/test_discovery.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.clients.plex_client import PlexItem  # noqa: E402
from backend.db.database import Database  # noqa: E402
from backend.db.migrations import run_migrations  # noqa: E402
from backend.models.settings import PathMapping, Settings  # noqa: E402
from backend.scan.discovery import run_discovery  # noqa: E402

PASS, FAIL = "\033[32m✓\033[0m", "\033[31m✗\033[0m"
failures = 0


def check(label: str, got, want) -> None:
    global failures
    ok = got == want
    if not ok:
        failures += 1
    print(f"  {PASS if ok else FAIL} {label:<46} got={got!r} want={want!r}")


class StubPlex:
    """Stands in for PlexClient. Returns whatever items the test hands it.

    `claims` overrides what section_size reports, so a pass can be made to come
    up short of what Plex says the section holds — the truncated-read case.
    """

    def __init__(self, items: list[PlexItem], claims: int | None = None):
        self.items = items
        self.claims = claims

    async def section_size(self, section_key, libtype="movie"):
        return len(self.items) if self.claims is None else self.claims

    async def iter_items(self, section_key, libtype="movie", page_size=200):
        for i in range(0, len(self.items), page_size):
            yield self.items[i:i + page_size]


def make_item(rating_key, guid, title, path, **kw) -> PlexItem:
    return PlexItem(
        rating_key=str(rating_key), guid=guid, item_type="movie", title=title,
        sort_title=title, year=kw.get("year", 2020),
        added_at=kw.get("added_at", 1700000000), updated_at=kw.get("updated_at", 1700000000),
        tmdb_id=kw.get("tmdb_id"), imdb_id=None, part_id=str(kw.get("part_id", 1)),
        plex_path=path, plex_size=kw.get("size", 1000), duration_ms=7200000,
    )


async def main() -> None:
    tmp = tempfile.mkdtemp(prefix="pxl-test-")
    media = Path(tmp) / "media"
    media.mkdir()

    # Real files so path resolution and fingerprinting exercise the stat path.
    for name in ("a.mkv", "b.mkv"):
        (media / name).write_bytes(b"x" * 1024)

    db = Database(Path(tmp) / "test.db")
    await db.start()
    await run_migrations(db)

    settings = Settings()
    settings.plex.libraries = ["1"]
    settings.path_mappings = [PathMapping(plex="/data/movies", local=str(media))]

    async def discover(items, claims=None):
        return await run_discovery(db, settings, StubPlex(items, claims), None)

    async def row(rating_key):
        return await db.fetch_one(
            "SELECT * FROM items WHERE library_key='1' AND rating_key=?", (rating_key,)
        )

    # ── 1. first run ───────────────────────────────────────────────────
    print("\n1. First discovery")
    r = await discover([
        make_item(101, "plex://movie/aaa", "Alpha", "/data/movies/a.mkv"),
        make_item(102, "plex://movie/bbb", "Beta", "/data/movies/b.mkv"),
        make_item(103, "plex://movie/ccc", "Gamma", "/data/movies/missing.mkv"),
        make_item(104, "plex://movie/ddd", "Delta", "/elsewhere/x.mkv"),
    ])
    check("added", r.added, 4)
    check("mapped", r.mapped, 2)
    check("missing (prefix matched, no file)", r.missing, 1)
    check("unmapped (no prefix matched)", r.unmapped, 1)
    alpha = await row("101")
    check("fingerprint computed", bool(alpha["file_fp"]), True)

    # ── 2. idempotent re-run ───────────────────────────────────────────
    print("\n2. Re-run with no changes")
    r = await discover([
        make_item(101, "plex://movie/aaa", "Alpha", "/data/movies/a.mkv"),
        make_item(102, "plex://movie/bbb", "Beta", "/data/movies/b.mkv"),
        make_item(103, "plex://movie/ccc", "Gamma", "/data/movies/missing.mkv"),
        make_item(104, "plex://movie/ddd", "Delta", "/elsewhere/x.mkv"),
    ])
    check("no new rows", r.added, 0)
    check("all updated", r.updated, 4)
    check("nothing removed", r.removed, 0)
    check("no file changes", r.changed_files, 0)

    # ── 3. facts survive a ratingKey rotation ──────────────────────────
    print("\n3. ratingKey rotation (delete + re-add in Plex)")
    await db.execute(
        "UPDATE items SET facts = json_patch(facts, ?) WHERE rating_key='101'",
        ('{"video":{"dar":2.39}}',),
    )
    before = await row("101")
    r = await discover([
        make_item(999, "plex://movie/aaa", "Alpha", "/data/movies/a.mkv"),  # new key, same guid
        make_item(102, "plex://movie/bbb", "Beta", "/data/movies/b.mkv"),
        make_item(103, "plex://movie/ccc", "Gamma", "/data/movies/missing.mkv"),
        make_item(104, "plex://movie/ddd", "Delta", "/elsewhere/x.mkv"),
    ])
    after = await row("999")
    check("detected as rotation, not new", r.added, 0)
    check("rotated count", r.rotated, 1)
    check("row id preserved", after["id"], before["id"])
    check("facts preserved", after["facts"], '{"video":{"dar":2.39}}')
    check("old ratingKey gone", await row("101"), None)

    # ── 4. file replaced ───────────────────────────────────────────────
    print("\n4. File replaced on disk")
    (media / "a.mkv").write_bytes(b"y" * 4096)
    os.utime(media / "a.mkv", (1800000000, 1800000000))
    r = await discover([
        make_item(999, "plex://movie/aaa", "Alpha", "/data/movies/a.mkv"),
        make_item(102, "plex://movie/bbb", "Beta", "/data/movies/b.mkv"),
    ])
    check("file change detected", r.changed_files, 1)
    changed = await row("999")
    check("new fingerprint stored", changed["file_fp"] != before["file_fp"], True)
    check("facts NOT wiped on change", changed["facts"], '{"video":{"dar":2.39}}')

    # ── 5. soft delete ─────────────────────────────────────────────────
    print("\n5. Items vanish from Plex")
    check("gamma+delta soft-deleted", r.removed, 2)
    gone = await db.fetch_one("SELECT deleted_at, facts FROM items WHERE rating_key='103'")
    check("row kept, marked deleted", gone["deleted_at"] is not None, True)
    live = await db.fetch_val("SELECT COUNT(*) FROM items WHERE deleted_at IS NULL")
    check("live count", live, 2)

    # ── 6. resurrection ────────────────────────────────────────────────
    print("\n6. A deleted item comes back")
    r = await discover([
        make_item(999, "plex://movie/aaa", "Alpha", "/data/movies/a.mkv"),
        make_item(102, "plex://movie/bbb", "Beta", "/data/movies/b.mkv"),
        make_item(103, "plex://movie/ccc", "Gamma", "/data/movies/missing.mkv"),
    ])
    revived = await row("103")
    check("un-deleted in place", revived["deleted_at"], None)
    check("not counted as new", r.added, 0)

    # ── 7. a short read must not be mistaken for deletions ─────────────
    # Plex's own count already reflects real deletions, so seeing fewer items
    # than it claims means we failed to read — a truncated response, a parse
    # that dropped rows. Soft-deleting the difference would empty the catalogue
    # silently, since nothing about it errors.
    print("\n7. Section returns fewer items than Plex claims")
    live_before = await db.fetch_val(
        "SELECT COUNT(*) FROM items WHERE deleted_at IS NULL", default=0)
    r = await discover(
        [make_item(101, "plex://movie/aaa", "Alpha", "/data/movies/a.mkv")],
        claims=4,
    )
    check("section flagged incomplete", r.incomplete, ["1"])
    check("no soft deletes attempted", r.removed, 0)
    live_after = await db.fetch_val(
        "SELECT COUNT(*) FROM items WHERE deleted_at IS NULL", default=0)
    check("nothing was deleted", live_after, live_before)

    # ── 8. and a complete pass still deletes ───────────────────────────
    print("\n8. A complete pass still reconciles deletions")
    r = await discover([make_item(101, "plex://movie/aaa", "Alpha", "/data/movies/a.mkv")])
    check("not flagged", r.incomplete, [])
    check("the rest soft-deleted", r.removed, live_before - 1)

    await db.stop()
    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())

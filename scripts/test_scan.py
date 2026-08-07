#!/usr/bin/env python3
"""End-to-end scan-engine test against real media files.

Seeds the catalog directly from files on disk (no Plex required), runs the
provider pipeline, and checks that facts, provenance, staleness and incremental
re-scanning all behave.

    docker exec plexlection-dev python3 /app/scripts/test_scan.py [count]
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, "/app" if Path("/app/backend").exists() else
                str(Path(__file__).resolve().parent.parent))

from backend.db.database import Database  # noqa: E402
from backend.db.indexes import reconcile, self_test  # noqa: E402
from backend.db.migrations import run_migrations  # noqa: E402
from backend.facts.registry import build_registry  # noqa: E402
from backend.facts.spec import CostTier  # noqa: E402
from backend.models.settings import Settings  # noqa: E402
from backend.providers import build_providers  # noqa: E402
from backend.scan.engine import ScanEngine  # noqa: E402
from backend.utils.path_mapper import fingerprint  # noqa: E402

def _find_media_root() -> Path:
    """Locate a Movies directory rather than hardcoding one.

    The recommended mount maps media at the *same* path the host uses, so the
    location differs per install — a fixed path here breaks the moment someone
    follows that advice.
    """
    env = os.getenv("PLEXLECTION_TEST_MEDIA")
    if env and Path(env).is_dir():
        return Path(env)
    for base in ("/media", "/mnt", "/data"):
        root = Path(base)
        if not root.is_dir():
            continue
        for candidate in root.rglob("Movies"):
            if candidate.is_dir() and any(candidate.iterdir()):
                return candidate
    raise SystemExit(
        "No Movies directory found under /media, /mnt or /data. "
        "Set PLEXLECTION_TEST_MEDIA to point at one."
    )


MEDIA = _find_media_root()
PASS, FAIL = "\033[32m✓\033[0m", "\033[31m✗\033[0m"
failures = 0


def check(label, got, want=True, op="=="):
    global failures
    ok = (got == want) if op == "==" else (got >= want)
    if not ok:
        failures += 1
    print(f"  {PASS if ok else FAIL} {label:<48} got={got!r}")


async def seed(db, limit: int) -> int:
    """Insert real files as catalog items, bypassing Plex."""
    now = int(time.time())
    rows = []
    for i, movie_dir in enumerate(sorted(p for p in MEDIA.iterdir() if p.is_dir())):
        if len(rows) >= limit:
            break
        videos = sorted(movie_dir.glob("*.mkv")) + sorted(movie_dir.glob("*.mp4"))
        if not videos:
            continue
        path = max(videos, key=lambda p: p.stat().st_size)
        st = path.stat()
        rows.append((
            "1", str(1000 + i), f"plex://movie/{i}", "movie", movie_dir.name,
            movie_dir.name, 2020, now, now, 7_200_000, str(path), str(path),
            "mapped", st.st_size, int(st.st_mtime),
            fingerprint(str(path), st.st_size, int(st.st_mtime)),
            now, now, 1,
        ))

    await db.execute_many(
        "INSERT INTO items (library_key, rating_key, guid, item_type, title, sort_title,"
        " year, plex_added_at, plex_updated_at, plex_duration_ms, plex_path, local_path,"
        " path_status, file_size, file_mtime, file_fp, first_seen, last_seen, seen_run,"
        " facts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'{}')",
        rows,
    )
    return len(rows)


async def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    tmp = tempfile.mkdtemp(prefix="pxl-scan-")

    db = Database(Path(tmp) / "scan.db")
    await db.start()
    await run_migrations(db)

    settings = Settings()
    settings.scan.concurrency["ffprobe"] = 6

    providers = build_providers(settings)
    # Look providers up by id — positional indexing breaks the moment
    # the registration list gains an entry.
    ffprobe_provider = next(p for p in providers if p.id == 'ffprobe')
    registry = build_registry(providers)
    await reconcile(db, registry)

    print(f"\nSeeding {limit} real files from {MEDIA}")
    n = await seed(db, limit)
    check("items seeded", n, limit)

    engine = ScanEngine(db, registry, providers, None)

    # ── 1. first scan ──────────────────────────────────────────────────
    print("\n1. First scan (all providers)")
    t0 = time.perf_counter()
    outcome = await engine.run(run_id=1, max_cost=CostTier.CHEAP, settings=settings)
    elapsed = time.perf_counter() - t0
    for p in outcome.providers:
        print(f"     {p.provider:<10} eligible={p.eligible:<4} ok={p.ok:<4} err={p.errors:<3} skip={p.skipped}")
    ffprobe = next(p for p in outcome.providers if p.provider == "ffprobe")
    check("ffprobe processed every item", ffprobe.ok, n)
    check("no ffprobe errors", ffprobe.errors, 0)
    print(f"     {elapsed:.1f}s for {n} files "
          f"({elapsed / max(n, 1) * 1000:.0f}ms each, concurrency 6)")

    # ── 2. facts landed ────────────────────────────────────────────────
    print("\n2. Facts stored")
    with_dar = await db.fetch_val(
        "SELECT COUNT(*) FROM items WHERE json_extract(facts,'$.video.dar') IS NOT NULL")
    check("video.dar present", with_dar, n)
    with_derived = await db.fetch_val(
        "SELECT COUNT(*) FROM items WHERE json_extract(facts,'$.derived.is_scope') IS NOT NULL")
    check("derived facts computed from ffprobe", with_derived, n)

    row = await db.fetch_one(
        "SELECT title, facts FROM items WHERE json_extract(facts,'$.video.dar') >= 2.3 LIMIT 1")
    if row:
        f = json.loads(row["facts"])
        print(f"     sample: {row['title'][:40]}")
        print(f"       dar={f['video']['dar']} bucket={f['video']['aspect_bucket']} "
              f"hdr={f['video']['hdr_format']} res={f['derived']['resolution_class']}")

    # ── 3. the genesis query ───────────────────────────────────────────
    print("\n3. The ultrawide query")
    scope = await db.fetch_all(
        "SELECT title, json_extract(facts,'$.video.dar') AS dar FROM items "
        "WHERE deleted_at IS NULL AND CAST(json_extract(facts,'$.video.dar') AS REAL) >= 2.3 "
        "ORDER BY dar DESC")
    print(f"     {len(scope)} of {n} at DAR >= 2.3")
    for r in scope[:5]:
        print(f"       {r['dar']:>6}  {r['title'][:46]}")
    check("found some scope films", len(scope) > 0)

    plan = await db.fetch_all(
        "EXPLAIN QUERY PLAN SELECT id FROM items WHERE deleted_at IS NULL "
        "AND CAST(json_extract(facts, '$.video.dar') AS REAL) > ?", (0,))
    detail = " ".join(str(r["detail"]) for r in plan)
    check("expression index used (not a table scan)", "idx_fact_video_dar" in detail)

    # ── 4. incremental ─────────────────────────────────────────────────
    print("\n4. Re-scan is incremental")
    t0 = time.perf_counter()
    outcome2 = await engine.run(run_id=2, max_cost=CostTier.CHEAP, settings=settings)
    ff2 = next(p for p in outcome2.providers if p.provider == "ffprobe")
    check("nothing re-probed", ff2.eligible, 0)
    print(f"     {time.perf_counter() - t0:.2f}s (vs {elapsed:.1f}s cold)")

    # ── 5. staleness on file change ────────────────────────────────────
    print("\n5. Changed file marks only that item stale")
    await db.execute("UPDATE items SET file_fp = 'CHANGED' WHERE id = 1")
    stale = await engine.plan(ffprobe_provider)
    check("exactly one item stale", len(stale), 1)

    # ── 6. schema_version bump invalidates everything ──────────────────
    print("\n6. Provider schema_version bump")
    ffprobe_provider.__class__.schema_version = 99
    stale_all = await engine.plan(ffprobe_provider)
    check("every item stale", len(stale_all), n)
    ffprobe_provider.__class__.schema_version = 1

    # ── 7. coverage ────────────────────────────────────────────────────
    print("\n7. Coverage reporting")
    cov = await engine.coverage()
    check("ffprobe coverage known", cov["ffprobe"]["known"], n)
    check("stale count surfaced", cov["ffprobe"]["stale"], 1)

    unused = await self_test(db, registry)
    check("no unused expression indexes", len(unused), 0)

    await db.stop()
    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())

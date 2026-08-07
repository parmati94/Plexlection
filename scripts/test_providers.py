#!/usr/bin/env python3
"""TMDB / Tautulli / derived extraction tests — no network required.

These exercise the parsing and the cross-provider logic against recorded-shape
payloads, so the interesting failure modes (a 0 budget dividing, TMDB reporting
0 revenue for a film it simply has no data on, a never-played item) are covered
without needing credentials.

    docker exec plexlection-dev python3 /app/scripts/test_providers.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, "/app" if Path("/app/backend").exists() else
                str(Path(__file__).resolve().parent.parent))

from backend.models.settings import Settings  # noqa: E402
from backend.providers.base import EnrichContext, ItemRow  # noqa: E402
from backend.providers.derived import DerivedProvider  # noqa: E402
from backend.providers.tautulli import TautulliProvider  # noqa: E402
from backend.providers.tmdb import TmdbProvider  # noqa: E402

PASS, FAIL = "\033[32m✓\033[0m", "\033[31m✗\033[0m"
failures = 0


def check(label, got, want=True):
    global failures
    ok = got == want
    if not ok:
        failures += 1
    print(f"  {PASS if ok else FAIL} {label:<50} got={got!r}")


TMDB_PAYLOAD = {
    "title": "Blade Runner 2049",
    "runtime": 164,
    "budget": 150_000_000,
    "revenue": 259_000_000,
    "vote_average": 7.573,
    "vote_count": 12000,
    "original_language": "en",
    "release_date": "2017-10-04",
    "adult": False,
    "tagline": "There's a storm coming.",
    "belongs_to_collection": {"id": 422837, "name": "Blade Runner Collection"},
    "genres": [{"name": "Science Fiction"}, {"name": "Drama"}],
    "production_countries": [{"iso_3166_1": "US"}, {"iso_3166_1": "GB"}],
    "keywords": {"keywords": [{"name": "Dystopia"}, {"name": "Android"}, {"name": "dystopia"}]},
}


class FakeTautulli:
    def __init__(self, data):
        self.data = data

    async def watch_map(self):
        return self.data


def ctx():
    return EnrichContext(settings=Settings(), cancel=asyncio.Event(),
                         semaphore=asyncio.Semaphore(1))


async def main():
    # ── TMDB extraction ────────────────────────────────────────────────
    print("\n1. TMDB extraction")
    tmdb = TmdbProvider(Settings())
    f = tmdb._extract(TMDB_PAYLOAD)
    check("keywords lowercased and deduped", f["tmdb.keywords"], ["android", "dystopia"])
    check("franchise", f["tmdb.collection"], "Blade Runner Collection")
    check("official runtime", f["tmdb.runtime"], 164)
    check("roi computed", f["tmdb.roi"], round(259_000_000 / 150_000_000, 3))
    check("genres", f["tmdb.genres"], ["Science Fiction", "Drama"])
    check("countries", f["tmdb.countries"], ["US", "GB"])
    check("release date -> epoch", isinstance(f["tmdb.release_date"], int))

    print("\n2. TMDB missing-data handling")
    sparse = tmdb._extract({"title": "Obscure", "budget": 0, "revenue": 0})
    check("no roi when budget is 0", "tmdb.roi" in sparse, False)
    check("empty keywords is still a fact", sparse["tmdb.keywords"], [])
    check("no collection key when absent", "tmdb.collection" in sparse, False)

    # ── Tautulli ───────────────────────────────────────────────────────
    print("\n3. Tautulli watch facts")
    history = {
        "100": {"play_count": 3, "last_played": 1700000000,
                "users": {"paul", "guest"}, "completed": 2, "started": 3},
        "101": {"play_count": 1, "last_played": 1600000000,
                "users": {"paul"}, "completed": 0, "started": 1},
    }
    provider = TautulliProvider(Settings(), client=FakeTautulli(history))
    items = [
        ItemRow(id=1, rating_key="100", library_key="1", item_type="movie", title="Watched"),
        ItemRow(id=2, rating_key="101", library_key="1", item_type="movie", title="Abandoned"),
        ItemRow(id=3, rating_key="102", library_key="1", item_type="movie", title="Never"),
    ]
    results = {r.item_id: r.facts async for r in provider.enrich(items, ctx())}
    check("play count", results[1]["watch.play_count"], 3)
    check("distinct users", results[1]["watch.unique_users"], 2)
    check("finished isn't abandoned", results[1]["watch.abandoned"], False)
    check("started but never finished IS abandoned", results[2]["watch.abandoned"], True)
    check("never played flagged", results[3]["watch.never_played"], True)
    check("never played has no last_played", "watch.last_played" in results[3], False)

    # ── derived cross-provider ─────────────────────────────────────────
    print("\n4. Extended-cut detection (the cross-provider payoff)")
    derived = DerivedProvider(Settings())

    async def facts_for(video_facts):
        item = ItemRow(id=1, rating_key="1", library_key="1", item_type="movie",
                       title="X", facts=video_facts)
        return [r.facts async for r in derived.enrich([item], ctx())][0]

    # Theatrical 164 min; the file runs 175 -> an extended cut.
    out = await facts_for({
        "file": {"duration_s": 175 * 60}, "tmdb": {"runtime": 164},
        "video": {"width": 3840, "height": 1600, "dar": 2.4},
    })
    check("runtime delta vs TMDB", out["derived.runtime_vs_tmdb_min"], 11.0)
    check("flagged as extended cut", out["derived.is_extended_cut"], True)

    out = await facts_for({
        "file": {"duration_s": 164 * 60 + 30}, "tmdb": {"runtime": 164},
        "video": {"width": 1920, "height": 800, "dar": 2.39},
    })
    check("30s over is not an extended cut", out["derived.is_extended_cut"], False)
    check("scope flag from dar", out["derived.is_scope"], True)
    check("scope film buckets on width, not height",
          out["derived.resolution_class"], "1080p")

    print("\n5. Derived flags with partial data")
    out = await facts_for({"tmdb": {"original_language": "ja", "budget": 0, "revenue": 0},
                           "video": {"width": 1920, "height": 1080, "dar": 1.78}})
    check("foreign detected", out["derived.is_foreign"], True)
    # TMDB reporting 0 revenue means "unrecorded", not "made nothing" — flagging
    # it as a bomb would sweep in half the library.
    check("no bomb verdict without both figures",
          "derived.is_box_office_bomb" in out, False)

    out = await facts_for({"tmdb": {"budget": 200_000_000, "revenue": 90_000_000},
                           "video": {"width": 1920, "height": 1080, "dar": 1.78}})
    check("bomb detected when both are known", out["derived.is_box_office_bomb"], True)

    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())

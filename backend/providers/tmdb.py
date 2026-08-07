"""TMDB provider.

The facts here are the ones Plex either doesn't index or doesn't expose to its
filter UI at all: keyword tags, budget/revenue, the official runtime, and the
franchise a film belongs to.

Two of these are worth the API key on their own:

* `tmdb.keywords` — far richer than genres. "christmas", "heist", "time-loop",
  "one-location", "post-credits-scene". Plex has no keyword filter.
* `tmdb.runtime` — compared against the file's actual duration by the derived
  provider, which is how you find the extended cuts you didn't know you had.
"""
import asyncio
import time
from typing import Any, AsyncIterator

from backend.common.logging_config import get_logger
from backend.facts.spec import CostTier, FactSpec, FactType
from backend.providers.base import (
    STATUS_ERROR,
    STATUS_OK,
    EnrichContext,
    Eligibility,
    FactProvider,
    FactResult,
    ItemRow,
)

logger = get_logger(__name__)


class TmdbProvider(FactProvider):
    id = "tmdb"
    label = "TMDB"
    cost = CostTier.NETWORK
    schema_version = 1
    depends_on = ()
    batch_size = 1
    # Metadata drifts slowly; the client also caches responses for 30 days.
    max_age_s = 30 * 24 * 3600
    default_concurrency = 4

    facts = (
        FactSpec("tmdb.keywords", "TMDB keywords", FactType.LIST,
                 "Free-form keyword tags — much richer than genres. christmas, "
                 "heist, time-loop, one-location, post-credits-scene.",
                 group="External", element_type=FactType.STRING,
                 example=["heist", "las vegas"]),
        FactSpec("tmdb.collection", "Franchise", FactType.STRING,
                 "TMDB's official collection, e.g. 'Star Wars Collection'. Note "
                 "these are conservative — the Star Wars one excludes Rogue One "
                 "and Solo, which the keywords do catch.",
                 group="External", indexed=True, example="The Dark Knight Collection"),
        FactSpec("tmdb.runtime", "Official runtime", FactType.NUMBER,
                 "TMDB's runtime in minutes. Compared against the file's actual "
                 "duration to spot extended and director's cuts.",
                 group="External", unit="min", aggregatable=True, example=152),
        FactSpec("tmdb.budget", "Budget", FactType.NUMBER,
                 "Production budget in USD. 0 when TMDB doesn't know.",
                 group="External", unit="USD", aggregatable=True, example=185000000),
        FactSpec("tmdb.revenue", "Box office", FactType.NUMBER,
                 "Worldwide gross in USD. 0 when unknown.",
                 group="External", unit="USD", aggregatable=True, example=1006000000),
        FactSpec("tmdb.roi", "Return on budget", FactType.NUMBER,
                 "Revenue divided by budget. Below 1 means it lost money — the "
                 "basis for a 'box office bombs' collection.",
                 group="External", aggregatable=True, example=5.44),
        FactSpec("tmdb.vote_average", "TMDB rating", FactType.NUMBER,
                 "Average user rating out of 10.",
                 group="External", indexed=True, aggregatable=True, example=8.5),
        FactSpec("tmdb.vote_count", "TMDB votes", FactType.NUMBER,
                 "How many people voted — useful for filtering out obscure "
                 "titles with a misleadingly high average.",
                 group="External", aggregatable=True, example=31000),
        FactSpec("tmdb.original_language", "Original language", FactType.STRING,
                 "ISO code of the original language. 'not English' is the basis "
                 "of a foreign-film collection.",
                 group="External", indexed=True, example="en"),
        FactSpec("tmdb.genres", "TMDB genres", FactType.LIST,
                 "Genres as TMDB has them, which often differ from Plex's.",
                 group="External", element_type=FactType.STRING,
                 example=["Action", "Thriller"]),
        FactSpec("tmdb.countries", "Production countries", FactType.LIST,
                 "ISO country codes of the production companies.",
                 group="External", element_type=FactType.STRING, example=["US", "GB"]),
        FactSpec("tmdb.release_date", "Release date", FactType.DATE,
                 "Theatrical release date.",
                 group="External", format="date", aggregatable=True),
        FactSpec("tmdb.adult", "Adult", FactType.BOOL,
                 "TMDB's adult flag.", group="External", example=False),
        FactSpec("tmdb.tagline", "Tagline", FactType.STRING,
                 "Marketing tagline.", group="External"),
    )

    def __init__(self, settings, client=None):
        super().__init__(settings)
        self.client = client

    def is_configured(self) -> bool:
        return bool(self.settings.tmdb.api_key)

    def not_configured_reason(self) -> str:
        return "no TMDB API key"

    def selector(self) -> tuple[str, list]:
        return "tmdb_id IS NOT NULL", []

    def can_enrich(self, item: ItemRow) -> Eligibility:
        if not item.tmdb_id:
            # Usually means Plex matched the film with a different agent.
            return Eligibility.skip("no TMDB id from Plex")
        return Eligibility.yes()

    def fingerprint(self, item: ItemRow) -> str | None:
        return f"tmdb:{item.tmdb_id}"

    async def enrich(self, items: list[ItemRow], ctx: EnrichContext) -> AsyncIterator[FactResult]:
        for item in items:
            if ctx.cancelled():
                return
            async with ctx.semaphore:
                if ctx.cancelled():
                    return
                if ctx.progress:
                    ctx.progress(item.title)
                yield await self._fetch(item)

    async def _fetch(self, item: ItemRow) -> FactResult:
        started = time.perf_counter()
        try:
            data = await self.client.movie(item.tmdb_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return FactResult(item.id, STATUS_ERROR, reason=f"{type(exc).__name__}: {exc}",
                              input_fp=self.fingerprint(item),
                              duration_ms=int((time.perf_counter() - started) * 1000))

        elapsed = int((time.perf_counter() - started) * 1000)
        if not data:
            return FactResult(item.id, STATUS_ERROR, reason=f"TMDB has no movie {item.tmdb_id}",
                              input_fp=self.fingerprint(item), duration_ms=elapsed)

        return FactResult(item.id, STATUS_OK, facts=self._extract(data),
                          input_fp=self.fingerprint(item), duration_ms=elapsed)

    def _extract(self, data: dict) -> dict[str, Any]:
        facts: dict[str, Any] = {}

        keywords = ((data.get("keywords") or {}).get("keywords")) or []
        facts["tmdb.keywords"] = sorted({k["name"].lower() for k in keywords if k.get("name")})

        collection = data.get("belongs_to_collection")
        if collection and collection.get("name"):
            facts["tmdb.collection"] = collection["name"]

        if data.get("runtime"):
            facts["tmdb.runtime"] = data["runtime"]

        budget = data.get("budget") or 0
        revenue = data.get("revenue") or 0
        facts["tmdb.budget"] = budget
        facts["tmdb.revenue"] = revenue
        # Only meaningful when both are known; a 0 budget would divide by zero
        # and a 0 revenue usually means "TMDB doesn't know", not "it made nothing".
        if budget > 0 and revenue > 0:
            facts["tmdb.roi"] = round(revenue / budget, 3)

        if data.get("vote_average") is not None:
            facts["tmdb.vote_average"] = round(float(data["vote_average"]), 2)
        if data.get("vote_count") is not None:
            facts["tmdb.vote_count"] = int(data["vote_count"])
        if data.get("original_language"):
            facts["tmdb.original_language"] = data["original_language"]

        facts["tmdb.genres"] = [g["name"] for g in (data.get("genres") or []) if g.get("name")]
        facts["tmdb.countries"] = [
            c["iso_3166_1"] for c in (data.get("production_countries") or [])
            if c.get("iso_3166_1")
        ]

        release = data.get("release_date")
        if release:
            try:
                from datetime import datetime, timezone
                facts["tmdb.release_date"] = int(
                    datetime.fromisoformat(release).replace(tzinfo=timezone.utc).timestamp()
                )
            except ValueError:
                pass

        facts["tmdb.adult"] = bool(data.get("adult"))
        if data.get("tagline"):
            facts["tmdb.tagline"] = data["tagline"]

        return facts

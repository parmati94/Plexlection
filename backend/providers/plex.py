"""Plex metadata facts.

Free — everything here was already fetched during discovery, so this provider
does no IO at all. It exists so Plex-sourced values live in the same fact
namespace as everything else and can be combined with them in one rule
("scope films added in the last 30 days").
"""
from typing import Any, AsyncIterator

from backend.facts.spec import CostTier, FactSpec, FactType
from backend.providers.base import (
    STATUS_OK,
    EnrichContext,
    FactProvider,
    FactResult,
    ItemRow,
)


class PlexFactProvider(FactProvider):
    id = "plex"
    label = "Plex metadata"
    cost = CostTier.FREE
    schema_version = 1
    depends_on = ()
    batch_size = 0  # whole set at once; there's no per-item cost to amortise
    max_age_s = None
    default_concurrency = 1

    facts = (
        FactSpec("plex.title", "Title", FactType.STRING,
                 "Title as Plex has it.", group="Identity", example="Dune"),
        FactSpec("plex.year", "Year", FactType.NUMBER,
                 "Release year.", group="Identity", indexed=True,
                 aggregatable=True, example=2021),
        FactSpec("plex.added_at", "Added to Plex", FactType.DATE,
                 "When the item first appeared in your library. Combine with "
                 "watch facts for 'added a year ago and never played'.",
                 group="Identity", format="date", indexed=True,
                 aggregatable=True, example=1700000000),
        FactSpec("plex.updated_at", "Updated in Plex", FactType.DATE,
                 "Plex's own last-modified stamp for the item.",
                 group="Identity", format="date", aggregatable=True),
        FactSpec("plex.duration_min", "Runtime (Plex)", FactType.NUMBER,
                 "Runtime in minutes as Plex reports it. Compared against TMDB's "
                 "official runtime to spot extended cuts.",
                 group="Identity", unit="min", aggregatable=True, example=155.0),
        FactSpec("plex.has_tmdb_id", "Matched to TMDB", FactType.BOOL,
                 "Plex resolved a TMDB id. Without one, TMDB facts can't be "
                 "fetched for this item.", group="Identity", example=True),
    )

    def fingerprint(self, item: ItemRow) -> str | None:
        # Plex's own updatedAt is the cheapest possible change signal.
        return f"{item.plex_updated_at}|{item.title}|{item.year}|{item.plex_duration_ms}"

    async def enrich(self, items: list[ItemRow], ctx: EnrichContext) -> AsyncIterator[FactResult]:
        for item in items:
            if ctx.cancelled():
                return
            facts: dict[str, Any] = {
                "plex.title": item.title,
                "plex.has_tmdb_id": item.tmdb_id is not None,
            }
            if item.year is not None:
                facts["plex.year"] = item.year
            if item.plex_added_at:
                facts["plex.added_at"] = item.plex_added_at
            if item.plex_updated_at:
                facts["plex.updated_at"] = item.plex_updated_at
            if item.plex_duration_ms:
                facts["plex.duration_min"] = round(item.plex_duration_ms / 60000, 2)

            yield FactResult(item.id, STATUS_OK, facts=facts,
                             input_fp=self.fingerprint(item))

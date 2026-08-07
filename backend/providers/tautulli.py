"""Tautulli provider — what you've actually watched.

Batched deliberately. Tautulli exposes per-item stats, but at library scale the
efficient shape is one paged sweep of `get_history` joined in memory, so this
provider takes every eligible item at once (`batch_size = 0`) and emits results
from a single fetch.

`fingerprint` returns None and `max_age_s` is set: watch history has no stable
input to hash against, it just goes out of date.
"""
import time
from typing import Any, AsyncIterator

from backend.common.logging_config import get_logger
from backend.facts.spec import CostTier, FactSpec, FactType
from backend.providers.base import (
    STATUS_ERROR,
    STATUS_OK,
    EnrichContext,
    FactProvider,
    FactResult,
    ItemRow,
)

logger = get_logger(__name__)


class TautulliProvider(FactProvider):
    id = "tautulli"
    label = "Tautulli"
    cost = CostTier.NETWORK
    schema_version = 1
    depends_on = ()
    batch_size = 0          # the whole library in one call
    max_age_s = 6 * 3600    # history always drifts
    default_concurrency = 1

    facts = (
        FactSpec("watch.play_count", "Play count", FactType.NUMBER,
                 "How many times it's been started, across all users.",
                 group="Watch", indexed=True, aggregatable=True, example=3),
        FactSpec("watch.last_played", "Last played", FactType.DATE,
                 "When it was last watched. Null means never — the basis for "
                 "'added a year ago and never played'.",
                 group="Watch", format="date", indexed=True, aggregatable=True),
        FactSpec("watch.never_played", "Never played", FactType.BOOL,
                 "No play history at all.",
                 group="Watch", indexed=True, example=True),
        FactSpec("watch.unique_users", "Distinct viewers", FactType.NUMBER,
                 "How many different users have watched it.",
                 group="Watch", aggregatable=True, example=2),
        FactSpec("watch.completed_count", "Completed plays", FactType.NUMBER,
                 "Plays Tautulli marked as finished.",
                 group="Watch", aggregatable=True, example=1),
        FactSpec("watch.abandoned", "Started but abandoned", FactType.BOOL,
                 "Started at least once and never finished. The 'I bounced off "
                 "this' collection.",
                 group="Watch", indexed=True, example=True),
    )

    def __init__(self, settings, client=None):
        super().__init__(settings)
        self.client = client

    def is_configured(self) -> bool:
        return bool(self.settings.tautulli.url and self.settings.tautulli.api_key)

    def not_configured_reason(self) -> str:
        return "no Tautulli URL or API key"

    def fingerprint(self, item: ItemRow) -> str | None:
        return None  # nothing stable to hash; freshness is max_age_s only

    async def enrich(self, items: list[ItemRow], ctx: EnrichContext) -> AsyncIterator[FactResult]:
        if not items:
            return

        if ctx.progress:
            ctx.progress("fetching watch history")
        started = time.perf_counter()
        try:
            watch = await self.client.watch_map()
        except Exception as exc:
            logger.error("Tautulli history fetch failed: %s", exc)
            for item in items:
                yield FactResult(item.id, STATUS_ERROR, reason=f"{type(exc).__name__}: {exc}")
            return

        elapsed = int((time.perf_counter() - started) * 1000)
        # One fetch amortised across every item, so the per-item cost is ~0.
        per_item = max(1, elapsed // max(1, len(items)))

        for index, item in enumerate(items):
            if ctx.cancelled():
                return
            if ctx.progress and index % 100 == 0:
                ctx.progress(item.title)

            entry = watch.get(item.rating_key)
            facts: dict[str, Any] = {}

            if entry is None:
                # Absence of history is itself a fact, and the most useful one here.
                facts["watch.play_count"] = 0
                facts["watch.never_played"] = True
                facts["watch.unique_users"] = 0
                facts["watch.completed_count"] = 0
                facts["watch.abandoned"] = False
            else:
                facts["watch.play_count"] = entry["play_count"]
                facts["watch.never_played"] = False
                facts["watch.unique_users"] = len(entry["users"])
                facts["watch.completed_count"] = entry["completed"]
                facts["watch.abandoned"] = entry["started"] > 0 and entry["completed"] == 0
                if entry["last_played"]:
                    facts["watch.last_played"] = int(entry["last_played"])

            yield FactResult(item.id, STATUS_OK, facts=facts, duration_ms=per_item)

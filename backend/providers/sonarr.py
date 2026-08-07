"""Sonarr provider — series-level facts your automation already knows.

Sonarr answers questions about a *show* that neither Plex nor the episode files
can. The two that earn their keep:

* `sonarr.percent_of_episodes` — how complete the show is. "Ended series I'm
  missing episodes of" is a genuinely useful collection and there is no way to
  express it from Plex's side, because Plex has no idea how many episodes are
  supposed to exist.
* `sonarr.series_type` — Sonarr is the only component in the stack that knows a
  show is anime, which is otherwise a genre guess.

Everything here is series-level, from the single `/api/v3/series` response, and
joins on tvdb_id. Episode-level facts (per-file custom formats, like Radarr's)
would need a call per series and are deliberately left out for now — the show
rollup provider already aggregates the episode facts we do compute.

Sonarr is also, at the moment, the only metadata source for shows at all: the
TMDB provider is movie-only, so `sonarr.genres` and `sonarr.rating` are the
show equivalents of facts movies have had since Phase 6.
"""
import time
from typing import Any, AsyncIterator

from backend.common.logging_config import get_logger
from backend.facts.spec import CostTier, FactSpec, FactType
from backend.providers.base import (
    STATUS_ERROR,
    STATUS_OK,
    STATUS_SKIPPED,
    EnrichContext,
    Eligibility,
    FactProvider,
    FactResult,
    ItemRow,
)

logger = get_logger(__name__)


class SonarrProvider(FactProvider):
    id = "sonarr"
    label = "Sonarr"
    cost = CostTier.NETWORK
    schema_version = 1
    depends_on = ()
    batch_size = 0
    max_age_s = 6 * 3600
    default_concurrency = 1
    default_applies_to = ("show",)

    facts = (
        FactSpec("sonarr.managed", "In Sonarr", FactType.BOOL,
                 "Sonarr tracks this series. False means Plex has it but your "
                 "automation doesn't know about it.",
                 group="Sonarr", indexed=True, example=True),
        FactSpec("sonarr.percent_of_episodes", "Episodes present", FactType.NUMBER,
                 "Percentage of the episodes Sonarr expects that you actually "
                 "have. Below 100 means gaps — the basis for a 'finish these' "
                 "collection.",
                 group="Sonarr", unit="%", format="percent", indexed=True,
                 aggregatable=True, example=100),
        FactSpec("sonarr.incomplete", "Has missing episodes", FactType.BOOL,
                 "At least one expected episode is missing.",
                 group="Sonarr", indexed=True, example=True),
        FactSpec("sonarr.missing_episodes", "Missing episodes", FactType.NUMBER,
                 "How many expected episodes have no file.",
                 group="Sonarr", aggregatable=True, example=3),
        FactSpec("sonarr.series_type", "Series type", FactType.ENUM,
                 "How Sonarr treats the show's numbering. The only reliable way "
                 "to identify anime in the library.",
                 group="Sonarr", indexed=True,
                 enum_values=("standard", "daily", "anime"), example="anime"),
        FactSpec("sonarr.series_status", "Series status", FactType.ENUM,
                 "Whether the show is still running.",
                 group="Sonarr", indexed=True,
                 enum_values=("continuing", "ended", "upcoming", "deleted"),
                 example="ended"),
        FactSpec("sonarr.ended", "Ended", FactType.BOOL,
                 "The show has finished airing — no more episodes are coming.",
                 group="Sonarr", indexed=True, example=True),
        FactSpec("sonarr.network", "Network", FactType.STRING,
                 "Broadcaster or streamer, e.g. Netflix, HBO, Apple TV.",
                 group="Sonarr", indexed=True, example="HBO"),
        FactSpec("sonarr.quality_profile", "Quality profile", FactType.STRING,
                 "The Sonarr profile assigned to this series.",
                 group="Sonarr", indexed=True, example="HD-1080p"),
        FactSpec("sonarr.monitored", "Monitored", FactType.BOOL,
                 "Sonarr is watching for new episodes and upgrades.",
                 group="Sonarr", indexed=True, example=True),
        FactSpec("sonarr.tags", "Sonarr tags", FactType.LIST,
                 "Tags applied to the series in Sonarr.",
                 group="Sonarr", element_type=FactType.STRING,
                 example=["hdr-required"]),
        FactSpec("sonarr.season_count", "Seasons", FactType.NUMBER,
                 "How many seasons Sonarr counts, excluding specials.",
                 group="Sonarr", aggregatable=True, example=5),
        FactSpec("sonarr.episode_count", "Episodes expected", FactType.NUMBER,
                 "Episodes Sonarr expects you to have, excluding unaired.",
                 group="Sonarr", aggregatable=True, example=62),
        FactSpec("sonarr.episode_file_count", "Episode files", FactType.NUMBER,
                 "Episode files actually on disk.",
                 group="Sonarr", aggregatable=True, example=62),
        FactSpec("sonarr.size_on_disk", "Series size", FactType.NUMBER,
                 "Total size of the series folder as Sonarr measured it.",
                 group="Sonarr", unit="bytes", format="bytes",
                 aggregatable=True, example=67166080597),
        FactSpec("sonarr.release_groups", "Release groups", FactType.LIST,
                 "Every group represented among the show's files. More than one "
                 "means a mixed-source season.",
                 group="Sonarr", element_type=FactType.STRING, example=["NTb"]),
        FactSpec("sonarr.root_folder", "Root folder", FactType.STRING,
                 "Which Sonarr root folder the series lives under.",
                 group="Sonarr", indexed=True),
        FactSpec("sonarr.genres", "Series genres", FactType.LIST,
                 "Genres from Sonarr's metadata. Shows have no TMDB coverage "
                 "yet, so this is the genre source for TV.",
                 group="Sonarr", element_type=FactType.STRING,
                 example=["Drama", "Crime"]),
        FactSpec("sonarr.rating", "Series rating", FactType.NUMBER,
                 "Average rating from Sonarr's metadata source, out of 10.",
                 group="Sonarr", indexed=True, aggregatable=True, example=8.6),
        FactSpec("sonarr.certification", "Certification", FactType.STRING,
                 "Age rating, e.g. TV-MA.",
                 group="Sonarr", indexed=True, example="TV-MA"),
        FactSpec("sonarr.runtime", "Episode runtime", FactType.NUMBER,
                 "Nominal runtime of one episode.",
                 group="Sonarr", unit="min", aggregatable=True, example=60),
        FactSpec("sonarr.original_language", "Original language", FactType.STRING,
                 "Language the show was made in.",
                 group="Sonarr", indexed=True, example="English"),
        FactSpec("sonarr.added", "Added to Sonarr", FactType.DATE,
                 "When the series was added to Sonarr.",
                 group="Sonarr", format="date", aggregatable=True),
        FactSpec("sonarr.first_aired", "First aired", FactType.DATE,
                 "Air date of the first episode.",
                 group="Sonarr", format="date", aggregatable=True),
        FactSpec("sonarr.last_aired", "Last aired", FactType.DATE,
                 "Air date of the most recent episode. Combined with 'continuing' "
                 "this finds shows that have quietly stopped.",
                 group="Sonarr", format="date", aggregatable=True),
    )

    def __init__(self, settings, client=None):
        super().__init__(settings)
        self.client = client

    def is_configured(self) -> bool:
        return bool(self.settings.sonarr.url and self.settings.sonarr.api_key)

    def not_configured_reason(self) -> str:
        return "no Sonarr URL or API key"

    def can_enrich(self, item: ItemRow) -> Eligibility:
        if not item.tvdb_id:
            return Eligibility.skip("no TVDB id — can't match to Sonarr")
        return Eligibility.yes()

    def fingerprint(self, item: ItemRow) -> str | None:
        return None  # episode counts drift as things air; age is the only signal

    async def options(self) -> dict[str, list[str]]:
        if not self.is_configured():
            return {}
        try:
            return {
                "sonarr.quality_profile": sorted((await self.client.quality_profiles()).values()),
                "sonarr.tags": sorted((await self.client.tags()).values()),
                "sonarr.root_folder": await self.client.root_folders(),
            }
        except Exception as exc:
            logger.warning("Sonarr vocabulary unavailable: %s", exc)
            return {}

    async def enrich(self, items: list[ItemRow], ctx: EnrichContext) -> AsyncIterator[FactResult]:
        if not items:
            return

        if ctx.progress:
            ctx.progress("fetching Sonarr series")
        started = time.perf_counter()
        try:
            by_tvdb = await self.client.by_external_id()
            profiles = await self.client.quality_profiles()
            tags = await self.client.tags()
        except Exception as exc:
            logger.error("Sonarr fetch failed: %s", exc)
            for item in items:
                yield FactResult(item.id, STATUS_ERROR, reason=f"{type(exc).__name__}: {exc}")
            return

        elapsed = int((time.perf_counter() - started) * 1000)
        per_item = max(1, elapsed // max(1, len(items)))

        for index, item in enumerate(items):
            if ctx.cancelled():
                return
            if ctx.progress and index % 50 == 0:
                ctx.progress(item.title)

            if not item.tvdb_id:
                yield FactResult(item.id, STATUS_SKIPPED,
                                 reason="no TVDB id — can't match to Sonarr")
                continue

            entry = by_tvdb.get(item.tvdb_id)
            if entry is None:
                yield FactResult(item.id, STATUS_OK,
                                 facts={"sonarr.managed": False}, duration_ms=per_item)
                continue

            yield FactResult(item.id, STATUS_OK,
                             facts=self._extract(entry, profiles, tags),
                             duration_ms=per_item)

    def _extract(self, entry: dict, profiles: dict[int, str],
                 tags: dict[int, str]) -> dict[str, Any]:
        facts: dict[str, Any] = {"sonarr.managed": True}

        profile = profiles.get(entry.get("qualityProfileId"))
        if profile:
            facts["sonarr.quality_profile"] = profile

        if entry.get("seriesType"):
            facts["sonarr.series_type"] = entry["seriesType"]
        status = entry.get("status")
        if status:
            facts["sonarr.series_status"] = status
        # `ended` is its own boolean in the payload and does not always agree
        # with status == "ended" for shows that were cancelled mid-run, so take
        # Sonarr's explicit flag rather than deriving it.
        facts["sonarr.ended"] = bool(entry.get("ended"))
        facts["sonarr.monitored"] = bool(entry.get("monitored"))

        for src, key in (
            ("network", "sonarr.network"),
            ("rootFolderPath", "sonarr.root_folder"),
            ("certification", "sonarr.certification"),
        ):
            if entry.get(src):
                facts[key] = entry[src]

        if entry.get("originalLanguage", {}).get("name"):
            facts["sonarr.original_language"] = entry["originalLanguage"]["name"]
        if entry.get("runtime"):
            facts["sonarr.runtime"] = int(entry["runtime"])

        facts["sonarr.genres"] = list(entry.get("genres") or [])
        facts["sonarr.tags"] = sorted(
            tags[t] for t in (entry.get("tags") or []) if t in tags
        )

        rating = (entry.get("ratings") or {}).get("value")
        if rating is not None:
            facts["sonarr.rating"] = round(float(rating), 2)

        for src, key in (
            ("added", "sonarr.added"),
            ("firstAired", "sonarr.first_aired"),
            ("lastAired", "sonarr.last_aired"),
        ):
            stamp = _epoch(entry.get(src))
            if stamp is not None:
                facts[key] = stamp

        # ── statistics ────────────────────────────────────────────────────
        stats = entry.get("statistics") or {}
        expected = int(stats.get("episodeCount") or 0)
        have = int(stats.get("episodeFileCount") or 0)

        facts["sonarr.season_count"] = int(stats.get("seasonCount") or 0)
        facts["sonarr.episode_count"] = expected
        facts["sonarr.episode_file_count"] = have
        facts["sonarr.size_on_disk"] = int(stats.get("sizeOnDisk") or 0)
        facts["sonarr.release_groups"] = sorted(stats.get("releaseGroups") or [])

        percent = stats.get("percentOfEpisodes")
        if percent is not None:
            facts["sonarr.percent_of_episodes"] = round(float(percent), 1)
        # A show with nothing aired yet has 0 expected episodes; calling that
        # "incomplete" would sweep every upcoming series into the gaps
        # collection, which is the opposite of useful.
        facts["sonarr.incomplete"] = expected > 0 and have < expected
        facts["sonarr.missing_episodes"] = max(0, expected - have)

        return facts


def _epoch(value: str | None) -> int | None:
    if not value:
        return None
    from datetime import datetime, timezone
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())

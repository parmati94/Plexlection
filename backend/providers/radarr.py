"""Radarr provider — what your automation thinks about each movie.

This is the first provider whose facts describe the *acquisition* of a file
rather than the file itself. ffprobe can tell you a movie is 2160p HDR; only
Radarr knows it was grabbed from a WEBDL when your profile asks for Bluray, or
that it scores 1705 against your Trash Guides custom formats while the one next
to it scores 5.

That gap is the interesting one. `radarr.cutoff_unmet` alone answers "what
should I upgrade next", which is a question the media file cannot answer and
Plex has never heard of.

Batched like Tautulli: `/api/v3/movie` returns the whole library in one
response, so the provider takes every eligible item at once (`batch_size = 0`)
and joins in memory on tmdb_id. Custom formats need a second sweep of
`/api/v3/moviefile` — see the client for why they aren't on the first one.

`fingerprint` is None with `max_age_s` set, matching Tautulli's reasoning:
Radarr state drifts on its own schedule. An upgrade changes the file (which
would show up in file_fp), but changing a quality profile or editing custom
format scores changes the answer with no file change at all, so age is the only
honest freshness signal.
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


class RadarrProvider(FactProvider):
    id = "radarr"
    label = "Radarr"
    cost = CostTier.NETWORK
    schema_version = 1
    depends_on = ()
    batch_size = 0          # whole library in one sweep
    max_age_s = 6 * 3600
    default_concurrency = 1
    default_applies_to = ("movie",)

    facts = (
        FactSpec("radarr.managed", "In Radarr", FactType.BOOL,
                 "Radarr tracks this movie. False means Plex has it but your "
                 "automation doesn't know about it — usually a manual import "
                 "that never got added.",
                 group="Radarr", indexed=True, example=True),
        FactSpec("radarr.quality_profile", "Quality profile", FactType.STRING,
                 "The Radarr profile assigned to this movie, e.g. Ultra-HD.",
                 group="Radarr", indexed=True, example="Ultra-HD"),
        FactSpec("radarr.quality", "Grabbed quality", FactType.STRING,
                 "The quality Radarr recorded for the file it has, e.g. "
                 "Bluray-2160p or WEBDL-1080p.",
                 group="Radarr", indexed=True, example="Bluray-2160p"),
        FactSpec("radarr.source", "Source", FactType.ENUM,
                 "Where the release came from. 'bluray' and 'webdl' are the two "
                 "that matter for quality arguments.",
                 group="Radarr", indexed=True,
                 enum_values=("bluray", "webdl", "webrip", "dvd", "tv",
                              "telecine", "telesync", "cam", "workprint", "unknown"),
                 example="bluray"),
        FactSpec("radarr.resolution", "Radarr resolution", FactType.NUMBER,
                 "Vertical resolution as Radarr classified the release. This is "
                 "the release's claimed resolution, which is not always what "
                 "ffprobe measures in the file.",
                 group="Radarr", unit="p", indexed=True, aggregatable=True, example=2160),
        FactSpec("radarr.remux", "Remux", FactType.BOOL,
                 "The release is an untouched disc remux.",
                 group="Radarr", indexed=True, example=True),
        FactSpec("radarr.custom_formats", "Custom formats", FactType.LIST,
                 "Trash Guides (or your own) custom formats that matched this "
                 "release — HDR, DV Boost, UHD Bluray Tier 01, and so on.",
                 group="Radarr", element_type=FactType.STRING,
                 example=["HDR", "UHD Bluray Tier 01"]),
        FactSpec("radarr.custom_format_score", "Custom format score", FactType.NUMBER,
                 "Total score of all matched custom formats. Ranks releases "
                 "against each other under your scoring rules — a low score on a "
                 "film you care about is an upgrade candidate.",
                 group="Radarr", indexed=True, aggregatable=True, example=1705),
        FactSpec("radarr.cutoff_unmet", "Below cutoff", FactType.BOOL,
                 "Radarr considers this file below the cutoff of its quality "
                 "profile — it would upgrade it given the chance. The direct "
                 "answer to 'what should I re-grab'.",
                 group="Radarr", indexed=True, example=True),
        FactSpec("radarr.release_group", "Release group", FactType.STRING,
                 "Group that produced the release, e.g. FraMeSToR or BHDStudio.",
                 group="Radarr", indexed=True, example="BHDStudio"),
        FactSpec("radarr.edition", "Edition", FactType.STRING,
                 "Edition tag parsed from the filename, lowercased — extended, "
                 "unrated, director's cut. Empty for a standard release.",
                 group="Radarr", indexed=True, example="extended"),
        FactSpec("radarr.proper_repack", "Proper or repack", FactType.BOOL,
                 "The file is a proper or a repack rather than the first release.",
                 group="Radarr", example=False),
        FactSpec("radarr.tags", "Radarr tags", FactType.LIST,
                 "Tags applied to the movie in Radarr.",
                 group="Radarr", element_type=FactType.STRING),
        FactSpec("radarr.root_folder", "Root folder", FactType.STRING,
                 "Which Radarr root folder the movie lives under — the practical "
                 "way to tell a normal library apart from a remux one.",
                 group="Radarr", indexed=True,
                 example="/media/paul/PLEXPOOL/Videos/Movies"),
        FactSpec("radarr.monitored", "Monitored", FactType.BOOL,
                 "Radarr is watching for upgrades to this movie.",
                 group="Radarr", indexed=True, example=True),
        FactSpec("radarr.movie_status", "Release status", FactType.ENUM,
                 "Radarr's view of where the film is in its release cycle.",
                 group="Radarr", indexed=True,
                 enum_values=("tba", "announced", "inCinemas", "released", "deleted"),
                 example="released"),
        FactSpec("radarr.size_on_disk", "Radarr file size", FactType.NUMBER,
                 "Size of the movie folder as Radarr measured it.",
                 group="Radarr", unit="bytes", aggregatable=True, example=67166080597),
        FactSpec("radarr.added", "Added to Radarr", FactType.DATE,
                 "When the movie was added to Radarr, which predates when Plex "
                 "saw the file.",
                 group="Radarr", format="date", aggregatable=True),
        FactSpec("radarr.studio", "Studio", FactType.STRING,
                 "Production studio as Radarr has it.",
                 group="Radarr", example="Warner Bros. Pictures"),
        FactSpec("radarr.certification", "Certification", FactType.STRING,
                 "Age rating from Radarr's metadata source.",
                 group="Radarr", indexed=True, example="PG-13"),
    )

    def __init__(self, settings, client=None):
        super().__init__(settings)
        self.client = client

    def is_configured(self) -> bool:
        return bool(self.settings.radarr.url and self.settings.radarr.api_key)

    def not_configured_reason(self) -> str:
        return "no Radarr URL or API key"

    def can_enrich(self, item: ItemRow) -> Eligibility:
        if not item.tmdb_id:
            # tmdbId is Radarr's own primary external key; without one there is
            # nothing to join on and "not in Radarr" would be a guess.
            return Eligibility.skip("no TMDB id — can't match to Radarr")
        return Eligibility.yes()

    def fingerprint(self, item: ItemRow) -> str | None:
        return None  # freshness is max_age_s only; see module docstring

    async def options(self) -> dict[str, list[str]]:
        """Authoritative vocabulary, so the rule builder offers real profiles and
        custom formats before anything has been scanned."""
        if not self.is_configured():
            return {}
        try:
            return {
                "radarr.quality_profile": sorted((await self.client.quality_profiles()).values()),
                "radarr.custom_formats": await self.client.custom_format_names(),
                "radarr.tags": sorted((await self.client.tags()).values()),
                "radarr.root_folder": await self.client.root_folders(),
            }
        except Exception as exc:
            logger.warning("Radarr vocabulary unavailable: %s", exc)
            return {}

    async def enrich(self, items: list[ItemRow], ctx: EnrichContext) -> AsyncIterator[FactResult]:
        if not items:
            return

        if ctx.progress:
            ctx.progress("fetching Radarr library")
        started = time.perf_counter()
        try:
            by_tmdb = await self.client.by_external_id()
            profiles = await self.client.quality_profiles()
            tags = await self.client.tags()
        except Exception as exc:
            logger.error("Radarr fetch failed: %s", exc)
            for item in items:
                yield FactResult(item.id, STATUS_ERROR, reason=f"{type(exc).__name__}: {exc}")
            return

        # Second sweep for custom formats, restricted to the files we actually
        # care about rather than the whole of Radarr.
        wanted_files = [
            entry["movieFileId"]
            for item in items
            if item.tmdb_id and (entry := by_tmdb.get(item.tmdb_id)) and entry.get("movieFileId")
        ]
        if ctx.progress:
            ctx.progress(f"fetching custom formats for {len(wanted_files)} files")
        try:
            files = await self.client.moviefiles(wanted_files)
        except Exception as exc:
            # Custom formats are the richest facts here but not the only ones;
            # losing them shouldn't cost you quality profiles and cutoff flags.
            logger.warning("Radarr custom formats unavailable: %s", exc)
            files = {}

        elapsed = int((time.perf_counter() - started) * 1000)
        per_item = max(1, elapsed // max(1, len(items)))

        for index, item in enumerate(items):
            if ctx.cancelled():
                return
            if ctx.progress and index % 100 == 0:
                ctx.progress(item.title)

            if not item.tmdb_id:
                yield FactResult(item.id, STATUS_SKIPPED,
                                 reason="no TMDB id — can't match to Radarr")
                continue

            entry = by_tmdb.get(item.tmdb_id)
            if entry is None:
                # Absence is a fact: these are the manual imports Radarr never
                # picked up, and they're worth being able to collect.
                yield FactResult(item.id, STATUS_OK,
                                 facts={"radarr.managed": False}, duration_ms=per_item)
                continue

            facts = self._extract(entry, files, profiles, tags)
            yield FactResult(item.id, STATUS_OK, facts=facts, duration_ms=per_item)

    def _extract(self, entry: dict, files: dict[int, dict],
                 profiles: dict[int, str], tags: dict[int, str]) -> dict[str, Any]:
        facts: dict[str, Any] = {"radarr.managed": True}

        profile = profiles.get(entry.get("qualityProfileId"))
        if profile:
            facts["radarr.quality_profile"] = profile

        facts["radarr.monitored"] = bool(entry.get("monitored"))
        if entry.get("status"):
            facts["radarr.movie_status"] = entry["status"]
        if entry.get("sizeOnDisk") is not None:
            facts["radarr.size_on_disk"] = int(entry["sizeOnDisk"])
        if entry.get("rootFolderPath"):
            facts["radarr.root_folder"] = entry["rootFolderPath"]
        if entry.get("studio"):
            facts["radarr.studio"] = entry["studio"]
        if entry.get("certification"):
            facts["radarr.certification"] = entry["certification"]

        facts["radarr.tags"] = sorted(
            tags[t] for t in (entry.get("tags") or []) if t in tags
        )

        added = _epoch(entry.get("added"))
        if added is not None:
            facts["radarr.added"] = added

        # ── file-level ────────────────────────────────────────────────────
        # `movieFile` is embedded in the list response but arrives with empty
        # custom formats; the moviefile sweep supplies the real ones.
        embedded = entry.get("movieFile") or {}
        detail = files.get(entry.get("movieFileId") or -1) or embedded
        if not detail:
            return facts

        quality = ((detail.get("quality") or {}).get("quality")) or {}
        if quality.get("name"):
            facts["radarr.quality"] = quality["name"]
        if quality.get("source"):
            facts["radarr.source"] = quality["source"]
        if quality.get("resolution") is not None:
            facts["radarr.resolution"] = int(quality["resolution"])
        facts["radarr.remux"] = quality.get("modifier") == "remux"

        revision = ((detail.get("quality") or {}).get("revision")) or {}
        facts["radarr.proper_repack"] = bool(
            revision.get("isRepack") or (revision.get("version") or 1) > 1
        )

        facts["radarr.custom_formats"] = sorted(
            c["name"] for c in (detail.get("customFormats") or []) if c.get("name")
        )
        # Explicit 0 rather than absent: "scored nothing" is a real answer and
        # the knownness guard would otherwise drop these from negative filters.
        facts["radarr.custom_format_score"] = int(detail.get("customFormatScore") or 0)
        facts["radarr.cutoff_unmet"] = bool(detail.get("qualityCutoffNotMet"))

        if detail.get("releaseGroup"):
            facts["radarr.release_group"] = detail["releaseGroup"]
        # Editions arrive as UNRATED, Unrated and unrated in the same library,
        # which makes an equality filter a coin toss. Lowercase is the fix.
        facts["radarr.edition"] = (detail.get("edition") or "").strip().lower()

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

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

## Multiple instances

More than one Radarr (a main library plus a remux library, say) can track the
same movie, and a merged Plex item can hold both files. Joining on tmdb_id
alone would then describe whichever instance answered — possibly a file Plex
isn't even serving. So the primary instance for an item is chosen by matching
file basename + size against the item's catalogued file (basename sidesteps
every path-mapping disagreement between the apps), falling back to instance
order. File-level facts merge across instances as any/union/best: `remux` is
"any copy is a remux", `custom_format_score` is the best copy's score. Identity
and movie-level facts (release_group, quality_profile, monitored…) come from
the primary alone.
"""
import os.path
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


# How facts combine when several instances track one movie. Everything not
# listed here is taken from the primary (file-matched) instance alone.
_MERGE_ANY = ("radarr.remux", "radarr.cutoff_unmet", "radarr.proper_repack")
_MERGE_UNION = ("radarr.custom_formats", "radarr.tags")
_MERGE_MAX = ("radarr.custom_format_score", "radarr.resolution", "radarr.size_on_disk")


class RadarrProvider(FactProvider):
    id = "radarr"
    label = "Radarr"
    cost = CostTier.NETWORK
    # v2: multi-instance merge — remux/score/formats now mean "any/best copy",
    # not "whatever the one instance said".
    schema_version = 2
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
        FactSpec("radarr.instance", "Radarr instance", FactType.STRING,
                 "The instance whose file backs this Plex item — with several "
                 "Radarrs, the one that actually owns the copy Plex serves.",
                 group="Radarr", indexed=True, example="remuxes"),
        FactSpec("radarr.instances", "Radarr instances", FactType.LIST,
                 "Every configured instance tracking this movie. 'main' without "
                 "'remuxes' is a movie you haven't remuxed yet.",
                 group="Radarr", element_type=FactType.STRING,
                 example=["main", "remuxes"]),
    )

    def __init__(self, settings, clients: list | None = None):
        super().__init__(settings)
        self.clients = clients or []

    def _configured_clients(self) -> list:
        return [c for c in self.clients if c.url and c.api_key]

    def is_configured(self) -> bool:
        return bool(self._configured_clients())

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
        custom formats before anything has been scanned. Union across instances."""
        merged: dict[str, set[str]] = {
            "radarr.quality_profile": set(), "radarr.custom_formats": set(),
            "radarr.tags": set(), "radarr.root_folder": set(),
        }
        names = [c.name for c in self._configured_clients()]
        for client in self._configured_clients():
            try:
                merged["radarr.quality_profile"] |= set((await client.quality_profiles()).values())
                merged["radarr.custom_formats"] |= set(await client.custom_format_names())
                merged["radarr.tags"] |= set((await client.tags()).values())
                merged["radarr.root_folder"] |= set(await client.root_folders())
            except Exception as exc:
                logger.warning("Radarr %r vocabulary unavailable: %s", client.name, exc)
        if not any(merged.values()):
            return {}
        out = {key: sorted(values) for key, values in merged.items()}
        out["radarr.instance"] = names
        out["radarr.instances"] = names
        return out

    async def enrich(self, items: list[ItemRow], ctx: EnrichContext) -> AsyncIterator[FactResult]:
        if not items:
            return

        started = time.perf_counter()
        # One sweep per instance. Any instance failing fails the batch: a merge
        # over half the instances would store confidently wrong facts (remux
        # False because only the non-remux Radarr answered) and cache them for
        # max_age_s.
        sweeps: list[tuple[str, dict, dict, dict, dict]] = []
        for client in self._configured_clients():
            if ctx.progress:
                ctx.progress(f"fetching Radarr library ({client.name})")
            try:
                by_tmdb = await client.by_external_id()
                profiles = await client.quality_profiles()
                tags = await client.tags()
            except Exception as exc:
                logger.error("Radarr %r fetch failed: %s", client.name, exc)
                for item in items:
                    yield FactResult(item.id, STATUS_ERROR,
                                     reason=f"{client.name}: {type(exc).__name__}: {exc}")
                return

            # Second sweep for custom formats, restricted to the files we
            # actually care about rather than the whole of Radarr.
            wanted_files = [
                entry["movieFileId"]
                for item in items
                if item.tmdb_id and (entry := by_tmdb.get(item.tmdb_id))
                and entry.get("movieFileId")
            ]
            if ctx.progress:
                ctx.progress(f"fetching custom formats for {len(wanted_files)} "
                             f"files ({client.name})")
            try:
                files = await client.moviefiles(wanted_files)
            except Exception as exc:
                # Custom formats are the richest facts here but not the only
                # ones; losing them shouldn't cost profiles and cutoff flags.
                logger.warning("Radarr %r custom formats unavailable: %s",
                               client.name, exc)
                files = {}
            sweeps.append((client.name, by_tmdb, profiles, tags, files))

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

            candidates = [
                (name, entry, files)
                for name, by_tmdb, _, _, files in sweeps
                if (entry := by_tmdb.get(item.tmdb_id))
            ]
            if not candidates:
                # Absence is a fact: these are the manual imports Radarr never
                # picked up, and they're worth being able to collect.
                yield FactResult(item.id, STATUS_OK,
                                 facts={"radarr.managed": False}, duration_ms=per_item)
                continue

            facts = self._merge(item, candidates, sweeps)
            yield FactResult(item.id, STATUS_OK, facts=facts, duration_ms=per_item)

    # ── multi-instance merge ──────────────────────────────────────────────
    def _merge(self, item: ItemRow,
               candidates: list[tuple[str, dict, dict]],
               sweeps: list[tuple[str, dict, dict, dict, dict]]) -> dict[str, Any]:
        vocab = {name: (profiles, tags) for name, _, profiles, tags, _ in sweeps}

        primary = next(
            (c for c in candidates if self._file_matches(item, self._file_detail(*c[1:]))),
            candidates[0],
        )
        name, entry, files = primary
        profiles, tags = vocab[name]
        facts = self._extract(entry, files, profiles, tags)
        facts["radarr.instance"] = name
        facts["radarr.instances"] = [c[0] for c in candidates]

        for other_name, other_entry, other_files in candidates:
            if other_name == name:
                continue
            profiles, tags = vocab[other_name]
            other = self._extract(other_entry, other_files, profiles, tags)
            for key in _MERGE_ANY:
                facts[key] = bool(facts.get(key)) or bool(other.get(key))
            for key in _MERGE_UNION:
                facts[key] = sorted(set(facts.get(key) or []) | set(other.get(key) or []))
            for key in _MERGE_MAX:
                values = [v for v in (facts.get(key), other.get(key)) if v is not None]
                if values:
                    facts[key] = max(values)
        return facts

    @staticmethod
    def _file_detail(entry: dict, files: dict[int, dict]) -> dict:
        return files.get(entry.get("movieFileId") or -1) or entry.get("movieFile") or {}

    @staticmethod
    def _file_matches(item: ItemRow, detail: dict) -> bool:
        """Is this Radarr file record the file Plex catalogued for the item?

        Basename + size, not full path: Radarr and Plex routinely disagree
        about mount points, but never about the file itself.
        """
        path = detail.get("path") or ""
        if not path or not item.plex_path:
            return False
        if os.path.basename(path) != os.path.basename(item.plex_path):
            return False
        size = detail.get("size")
        if size and item.file_size and int(size) != int(item.file_size):
            return False
        return True

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

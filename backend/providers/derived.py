"""Derived facts — pure functions over facts other providers already stored.

No network, no disk. Runs last in every scan, and is cheap enough to recompute
for every touched item rather than tracking fine-grained dependencies.

This is where cross-provider insight lives: the interesting facts are often
comparisons between two sources, not anything either source reports on its own.
"""
from typing import Any, AsyncIterator

from backend.facts.spec import CostTier, FactSpec, FactType
from backend.providers.base import (
    STATUS_OK,
    STATUS_SKIPPED,
    EnrichContext,
    FactProvider,
    FactResult,
    ItemRow,
)


def _get(facts: dict, dotted: str) -> Any:
    cur: Any = facts
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


class DerivedProvider(FactProvider):
    id = "derived"
    label = "Derived"
    cost = CostTier.FREE
    schema_version = 1
    depends_on = ("plex", "ffprobe", "tmdb")
    batch_size = 0
    max_age_s = None
    default_concurrency = 1
    # Reads file-derived facts, so it follows ffprobe's scope. TMDB-sourced
    # derivations simply stay unset on episodes, which have no TMDB id.
    default_applies_to = ("movie", "episode")

    facts = (
        FactSpec("derived.bitrate_per_pixel", "Encode efficiency", FactType.NUMBER,
                 "Bitrate divided by frame area, in bits per pixel per second. "
                 "Low values for the resolution are the signature of a stretched "
                 "or upscaled encode; high values flag bloated remuxes.",
                 group="Derived", aggregatable=True, example=0.0031),
        FactSpec("derived.size_per_minute_mb", "Size per minute", FactType.NUMBER,
                 "Megabytes per minute of runtime — a length-independent way to "
                 "compare how much disk a title costs.",
                 group="Derived", unit="MB/min", aggregatable=True, example=290.0),
        FactSpec("derived.resolution_class", "Resolution", FactType.ENUM,
                 "SD, 720p, 1080p or 4K, bucketed by frame WIDTH. Width is the "
                 "right discriminator because matting a scope film reduces its "
                 "height but not its width — a 1920x798 file is 1080p content, "
                 "not 720p.",
                 group="Derived", indexed=True,
                 enum_values=("sd", "720p", "1080p", "4k", "other"), example="4k"),
        FactSpec("derived.is_scope", "Scope (ultrawide)", FactType.BOOL,
                 "Aspect ratio at or above 2.3:1 — the anamorphic scope family. "
                 "Based on the container's declared ratio, so it catches "
                 "hard-matted files but not black bars baked into the frame.",
                 group="Derived", indexed=True, example=True),
        # Plex usually derives its runtime from the file, so this is near zero
        # for a healthy library — which is what makes an outlier meaningful:
        # Plex's metadata is stale relative to the file actually on disk.
        # The high-value comparison, file runtime vs TMDB's *official* runtime
        # (extended-cut detection), needs the TMDB provider and lands in v2.
        FactSpec("derived.runtime_mismatch_min", "Runtime drift vs Plex", FactType.NUMBER,
                 "File runtime minus the runtime Plex reports, in minutes. Normally "
                 "~0; a large gap means Plex's metadata describes a different cut "
                 "than the file you actually have.",
                 group="Derived", unit="min", aggregatable=True, example=11.5),

        # The headline cross-provider fact: neither source reports this on its
        # own, it only exists as a comparison between them.
        FactSpec("derived.runtime_vs_tmdb_min", "Runtime vs TMDB", FactType.NUMBER,
                 "File runtime minus TMDB's official runtime, in minutes. A large "
                 "positive value means you have an extended or director's cut; a "
                 "large negative one usually means a truncated or mismatched file.",
                 group="Derived", unit="min", indexed=True, aggregatable=True, example=11.0),
        FactSpec("derived.is_extended_cut", "Extended cut", FactType.BOOL,
                 "The file runs 5+ minutes longer than TMDB's official runtime. "
                 "Plex has no idea which version of a film you actually hold.",
                 group="Derived", indexed=True, example=True),
        FactSpec("derived.is_foreign", "Non-English original", FactType.BOOL,
                 "TMDB's original language isn't English.",
                 group="Derived", indexed=True, example=False),
        FactSpec("derived.is_box_office_bomb", "Box office bomb", FactType.BOOL,
                 "Grossed less than its production budget. Only set when TMDB "
                 "knows both figures.",
                 group="Derived", indexed=True, example=False),
    )

    def selector(self) -> tuple[str, list]:
        return "facts != '{}'", []

    def fingerprint(self, item: ItemRow) -> str | None:
        """Inputs are the facts this provider reads, so a change in any of them
        marks it stale — no dependency graph needed."""
        inputs = (
            _get(item.facts, "video.dar"),
            _get(item.facts, "video.width"),
            _get(item.facts, "video.height"),
            _get(item.facts, "file.bitrate_kbps"),
            _get(item.facts, "file.duration_s"),
            _get(item.facts, "file.size_bytes"),
            _get(item.facts, "plex.duration_min"),
            _get(item.facts, "tmdb.runtime"),
            _get(item.facts, "tmdb.original_language"),
            _get(item.facts, "tmdb.budget"),
            _get(item.facts, "tmdb.revenue"),
        )
        return "|".join(str(v) for v in inputs)

    async def enrich(self, items: list[ItemRow], ctx: EnrichContext) -> AsyncIterator[FactResult]:
        for item in items:
            if ctx.cancelled():
                return

            f = item.facts
            out: dict[str, Any] = {}

            width = _get(f, "video.width")
            height = _get(f, "video.height")
            bitrate = _get(f, "file.bitrate_kbps")
            duration = _get(f, "file.duration_s")
            size = _get(f, "file.size_bytes")
            dar = _get(f, "video.dar")

            if width:
                # Bucket on width, not height: a 2.39:1 film stored as 1920x798
                # is 1080p content, and a height-based test would call it 720p.
                out["derived.resolution_class"] = (
                    "4k" if width >= 3000 else
                    "1080p" if width >= 1800 else
                    "720p" if width >= 1200 else
                    "sd" if width > 0 else "other"
                )

            if dar is not None:
                out["derived.is_scope"] = dar >= 2.3

            if width and height and bitrate:
                out["derived.bitrate_per_pixel"] = round(
                    (bitrate * 1000) / (width * height), 6
                )

            if size and duration and duration > 0:
                out["derived.size_per_minute_mb"] = round(
                    size / 1_000_000 / (duration / 60), 2
                )

            plex_min = _get(f, "plex.duration_min")
            if plex_min and duration:
                out["derived.runtime_mismatch_min"] = round(duration / 60 - plex_min, 2)

            # Extended-cut detection. Prefer the file's own duration; fall back
            # to Plex's when ffprobe hasn't run.
            tmdb_min = _get(f, "tmdb.runtime")
            actual_min = (duration / 60) if duration else plex_min
            if tmdb_min and actual_min:
                delta = round(actual_min - tmdb_min, 2)
                out["derived.runtime_vs_tmdb_min"] = delta
                out["derived.is_extended_cut"] = delta >= 5

            language = _get(f, "tmdb.original_language")
            if language:
                out["derived.is_foreign"] = language != "en"

            budget = _get(f, "tmdb.budget")
            revenue = _get(f, "tmdb.revenue")
            # Only when both are known — a 0 revenue in TMDB means "unrecorded",
            # not "made nothing", and would flag half the library.
            if budget and revenue and budget > 0 and revenue > 0:
                out["derived.is_box_office_bomb"] = revenue < budget

            if not out:
                yield FactResult(item.id, STATUS_SKIPPED,
                                 reason="no inputs computed yet",
                                 input_fp=self.fingerprint(item))
                continue

            yield FactResult(item.id, STATUS_OK, facts=out,
                             input_fp=self.fingerprint(item))

"""Show-level facts, rolled up from episodes.

Plex collections of TV contain **shows**, but every technical fact lives on an
episode file. This is the bridge.

It's `derived` with a different join: instead of computing from one item's own
facts, it aggregates its children's. That keeps aggregation out of the rule
language entirely — the compiler, the registry, the builder and the sync engine
never learn what a "show" is, they just see more facts.

It also disposes of an ambiguity by naming it. "Shows that are 4K" is
unanswerable the moment one season is an upscale; `show.all_4k` and
`show.mixed_resolutions` each mean exactly one thing.
"""
from typing import Any, AsyncIterator

from backend.common.logging_config import get_logger
from backend.facts.spec import CostTier, FactSpec, FactType
from backend.providers.base import (
    STATUS_OK,
    STATUS_SKIPPED,
    EnrichContext,
    FactProvider,
    FactResult,
    ItemRow,
)

logger = get_logger(__name__)

# Worst-to-best, so "worst_resolution" has a defined answer.
RESOLUTION_ORDER = ("sd", "720p", "1080p", "4k", "other")

# One grouped pass over every episode. 21k rows in, 508 out — a per-show query
# would be 508 round-trips for the same answer.
_ROLLUP_SQL = """
SELECT
  e.parent_key                                                     AS show_key,
  COUNT(*)                                                         AS episodes,
  SUM(COALESCE(e.file_size, 0))                                    AS total_size,
  SUM(CAST(json_extract(e.facts,'$.file.duration_s') AS REAL))     AS total_seconds,
  MIN(CAST(json_extract(e.facts,'$.file.bitrate_kbps') AS REAL))   AS min_bitrate,
  MAX(CAST(json_extract(e.facts,'$.file.bitrate_kbps') AS REAL))   AS max_bitrate,
  AVG(CAST(json_extract(e.facts,'$.file.bitrate_kbps') AS REAL))   AS avg_bitrate,
  group_concat(DISTINCT json_extract(e.facts,'$.derived.resolution_class')) AS resolutions,
  group_concat(DISTINCT json_extract(e.facts,'$.video.hdr_format'))         AS hdr_formats,
  group_concat(DISTINCT json_extract(e.facts,'$.video.aspect_bucket'))      AS aspect_buckets,
  SUM(CASE WHEN json_extract(e.facts,'$.video.dar') IS NOT NULL THEN 1 ELSE 0 END) AS scanned
FROM items e
WHERE e.deleted_at IS NULL AND e.item_type = 'episode' AND e.parent_key IS NOT NULL
GROUP BY e.parent_key
"""

# Languages are a LIST fact, so they need json_each rather than group_concat.
_LANG_SQL = """
SELECT e.parent_key AS show_key, je.value AS lang
FROM items e, json_each(e.facts, '$.audio.languages') je
WHERE e.deleted_at IS NULL AND e.item_type = 'episode' AND e.parent_key IS NOT NULL
GROUP BY e.parent_key, je.value
"""


def _split(value: str | None) -> list[str]:
    """group_concat gives a comma-joined string, or NULL when nothing matched."""
    if not value:
        return []
    return sorted({v for v in value.split(",") if v and v != "null"})


class ShowRollupProvider(FactProvider):
    id = "show_rollup"
    label = "Show rollup"
    cost = CostTier.FREE
    schema_version = 1
    depends_on = ("plex", "ffprobe", "derived")
    batch_size = 0            # every show in one pass
    default_concurrency = 1
    default_applies_to = ("show",)
    # Always recompute. There's no per-show input to fingerprint without a query
    # per show, and the whole rollup is two grouped queries — cheaper than
    # working out whether it was needed.
    max_age_s = 0

    facts = (
        FactSpec("show.episode_count", "Episodes on disk", FactType.NUMBER,
                 "Episode files Plexlection has indexed for this show. May be "
                 "lower than Plex's own count if some episodes aren't scanned.",
                 group="Show", indexed=True, aggregatable=True, example=62),

        FactSpec("show.mixed_resolutions", "Mixed resolutions", FactType.BOOL,
                 "Episodes are not all the same resolution class — typically an "
                 "early season at a lower quality than the rest. The upgrade "
                 "shortlist.",
                 group="Show", indexed=True, example=True),
        FactSpec("show.worst_resolution", "Worst resolution", FactType.ENUM,
                 "The lowest resolution class present in the show. Finds series "
                 "still holding SD episodes.",
                 group="Show", indexed=True,
                 enum_values=RESOLUTION_ORDER, example="720p"),
        FactSpec("show.all_4k", "Entirely 4K", FactType.BOOL,
                 "Every indexed episode is 4K.", group="Show", indexed=True, example=False),
        FactSpec("show.any_4k", "Has 4K episodes", FactType.BOOL,
                 "At least one episode is 4K.", group="Show", example=True),

        FactSpec("show.mixed_hdr", "Mixed HDR", FactType.BOOL,
                 "Episodes disagree on HDR format — some Dolby Vision, some SDR. "
                 "Looks visibly inconsistent during a binge.",
                 group="Show", indexed=True, example=True),
        FactSpec("show.any_dolby_vision", "Has Dolby Vision", FactType.BOOL,
                 "At least one episode carries a Dolby Vision layer.",
                 group="Show", example=False),
        FactSpec("show.hdr_formats", "HDR formats present", FactType.LIST,
                 "Every distinct HDR format across the show's episodes.",
                 group="Show", element_type=FactType.STRING, example=["sdr", "hdr10"]),

        FactSpec("show.total_size_bytes", "Total size", FactType.NUMBER,
                 "Disk used by every indexed episode.",
                 group="Show", unit="B", format="bytes",
                 indexed=True, aggregatable=True, example=180_000_000_000),
        FactSpec("show.size_per_episode_mb", "Size per episode", FactType.NUMBER,
                 "Average megabytes per episode — comparable across shows of "
                 "wildly different lengths.",
                 group="Show", unit="MB", aggregatable=True, example=2900.0),
        FactSpec("show.total_runtime_min", "Total runtime", FactType.NUMBER,
                 "Combined runtime of every indexed episode.",
                 group="Show", unit="min", aggregatable=True, example=2790.0),

        FactSpec("show.max_bitrate_kbps", "Highest bitrate", FactType.NUMBER,
                 "Bitrate of the richest episode.",
                 group="Show", unit="kbps", format="kbps", aggregatable=True, example=38000.0),
        FactSpec("show.min_bitrate_kbps", "Lowest bitrate", FactType.NUMBER,
                 "Bitrate of the poorest episode.",
                 group="Show", unit="kbps", format="kbps", aggregatable=True, example=1800.0),
        FactSpec("show.mean_bitrate_kbps", "Average bitrate", FactType.NUMBER,
                 "Mean across episodes.",
                 group="Show", unit="kbps", format="kbps", aggregatable=True, example=9400.0),

        FactSpec("show.audio_languages", "Audio languages", FactType.LIST,
                 "Union of audio languages across every episode.",
                 group="Show", element_type=FactType.STRING, example=["eng", "jpn"]),
        FactSpec("show.aspect_buckets", "Aspect ratios present", FactType.LIST,
                 "Distinct aspect ratios across the show. More than one means "
                 "the framing changes mid-run.",
                 group="Show", element_type=FactType.STRING, example=["1.78:1"]),

        # ── straight from Plex's own show record, no rollup needed ─────────
        FactSpec("show.leaf_count", "Episodes (Plex)", FactType.NUMBER,
                 "Episodes Plex knows about, which can exceed what's on disk.",
                 group="Show", aggregatable=True, example=62),
        FactSpec("show.watched_count", "Episodes watched", FactType.NUMBER,
                 "From Plex's viewedLeafCount. This is per-account — it reflects "
                 "the account whose token Plexlection uses, not every user.",
                 group="Show", aggregatable=True, example=14),
        FactSpec("show.percent_watched", "Percent watched", FactType.NUMBER,
                 "Watched episodes as a percentage of what Plex knows about.",
                 group="Show", unit="%", indexed=True, aggregatable=True, example=22.6),
        FactSpec("show.unwatched", "Never started", FactType.BOOL,
                 "No episode has been watched.", group="Show", indexed=True, example=True),
        FactSpec("show.abandoned", "Started, not finished", FactType.BOOL,
                 "Between one episode and 90% watched — the shows you bounced "
                 "off partway.",
                 group="Show", indexed=True, example=True),
    )

    def __init__(self, settings, db=None):
        super().__init__(settings)
        self.db = db

    def is_configured(self) -> bool:
        return self.db is not None

    def not_configured_reason(self) -> str:
        return "no database handle"

    def fingerprint(self, item: ItemRow) -> str | None:
        return None  # see max_age_s

    async def enrich(self, items: list[ItemRow], ctx: EnrichContext) -> AsyncIterator[FactResult]:
        if not items:
            return

        if ctx.progress:
            ctx.progress("aggregating episodes")
        rollup = await self._aggregate()

        for item in items:
            if ctx.cancelled():
                return

            facts: dict[str, Any] = {}

            # Watch state comes from the show record itself — Plex already
            # tracks it, so there's nothing to roll up.
            leaf = item.leaf_count or 0
            viewed = item.viewed_leaf_count or 0
            if leaf:
                pct = round(viewed / leaf * 100, 1)
                facts["show.leaf_count"] = leaf
                facts["show.watched_count"] = viewed
                facts["show.percent_watched"] = pct
                facts["show.unwatched"] = viewed == 0
                facts["show.abandoned"] = 0 < pct < 90

            agg = rollup.get(item.rating_key)
            if not agg:
                # A show with no indexed episodes yet. Watch facts still apply.
                if not facts:
                    yield FactResult(item.id, STATUS_SKIPPED, reason="no episodes indexed")
                    continue
                yield FactResult(item.id, STATUS_OK, facts=facts)
                continue

            facts.update(agg)
            yield FactResult(item.id, STATUS_OK, facts=facts)

    async def _aggregate(self) -> dict[str, dict]:
        rows = await self.db.fetch_all(_ROLLUP_SQL)
        lang_rows = await self.db.fetch_all(_LANG_SQL)

        languages: dict[str, set] = {}
        for r in lang_rows:
            if r["lang"]:
                languages.setdefault(r["show_key"], set()).add(r["lang"])

        out: dict[str, dict] = {}
        for r in rows:
            key = r["show_key"]
            episodes = r["episodes"] or 0
            resolutions = _split(r["resolutions"])
            hdrs = _split(r["hdr_formats"])
            buckets = _split(r["aspect_buckets"])

            facts: dict[str, Any] = {
                "show.episode_count": episodes,
                "show.aspect_buckets": buckets,
                "show.audio_languages": sorted(languages.get(key, ())),
            }

            if r["total_size"]:
                facts["show.total_size_bytes"] = int(r["total_size"])
                if episodes:
                    facts["show.size_per_episode_mb"] = round(
                        r["total_size"] / 1_000_000 / episodes, 1
                    )
            if r["total_seconds"]:
                facts["show.total_runtime_min"] = round(r["total_seconds"] / 60, 1)
            if r["min_bitrate"] is not None:
                facts["show.min_bitrate_kbps"] = round(r["min_bitrate"], 1)
                facts["show.max_bitrate_kbps"] = round(r["max_bitrate"], 1)
                facts["show.mean_bitrate_kbps"] = round(r["avg_bitrate"], 1)

            if resolutions:
                facts["show.mixed_resolutions"] = len(resolutions) > 1
                facts["show.all_4k"] = resolutions == ["4k"]
                facts["show.any_4k"] = "4k" in resolutions
                facts["show.worst_resolution"] = next(
                    (r_ for r_ in RESOLUTION_ORDER if r_ in resolutions), "other"
                )

            if hdrs:
                facts["show.hdr_formats"] = hdrs
                facts["show.mixed_hdr"] = len(hdrs) > 1
                facts["show.any_dolby_vision"] = any(h.startswith("dv") for h in hdrs)

            out[key] = facts

        logger.info("📺 Rolled up %d shows from episode facts", len(out))
        return out

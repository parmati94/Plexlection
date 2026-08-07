"""Radarr / Sonarr client.

One class for both, because the v3 APIs are the same API with different nouns.
The flavour supplies the noun (`movie` vs `series`) and the external id field
(`tmdbId` vs `tvdbId`); everything else — auth header, vocabulary endpoints,
error shape — is identical. Two near-identical files would drift.

Fetching is deliberately whole-library. Both apps answer `/api/v3/movie` and
`/api/v3/series` with the entire collection in one response (2.2k movies is a
few MB, under a second on a LAN), so the provider does a single sweep and joins
in memory rather than issuing thousands of per-item round-trips.

The one wrinkle is Radarr custom formats. `/api/v3/movie` returns `movieFile`
with `customFormats: []` and `customFormatScore: null` — the fields exist but
are never populated on that endpoint. The real values only come from
`/api/v3/moviefile`, which requires an explicit id list and takes it as
*repeated* query params (`movieFileIds=1&movieFileIds=2`); a comma-joined
string is rejected with a 400. Since custom formats are most of the reason to
integrate Radarr at all, `moviefiles()` chunks the ids and reassembles.
"""
import asyncio
import time
from typing import Any

import httpx

from backend.common.errors import NotConfiguredError
from backend.common.logging_config import get_logger

logger = get_logger(__name__)

# Radarr accepts repeated query params happily, but a 2000-id URL would blow
# past what some reverse proxies allow. 250 ids is ~3KB of query string and
# came back in under a second in testing.
MOVIEFILE_CHUNK = 250

# Quality profiles, custom formats and tags are edited by hand a few times a
# year. Caching them for the process lifetime would be fine; ten minutes just
# means a new Trash Guides sync shows up without a restart.
VOCAB_TTL_S = 600


class ArrFlavour:
    """The handful of names that differ between the two apps."""

    def __init__(self, app: str, noun: str, id_field: str):
        self.app = app          # "radarr" | "sonarr"
        self.noun = noun        # "movie"  | "series"
        self.id_field = id_field  # "tmdbId" | "tvdbId"


RADARR = ArrFlavour("radarr", "movie", "tmdbId")
SONARR = ArrFlavour("sonarr", "series", "tvdbId")


class ArrClient:
    def __init__(self, flavour: ArrFlavour, url: str, api_key: str, timeout_s: float = 60.0):
        self.flavour = flavour
        self.url = (url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout_s = timeout_s
        self._client: httpx.AsyncClient | None = None
        self._vocab: dict[str, tuple[float, Any]] = {}

    # ── plumbing ──────────────────────────────────────────────────────────
    def _require(self) -> None:
        if not self.url or not self.api_key:
            raise NotConfiguredError(
                f"{self.flavour.app.title()} is not configured — add its URL and "
                f"API key in Settings."
            )

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout_s,
                headers={"X-Api-Key": self.api_key},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, params: Any = None, timeout: float | None = None) -> Any:
        self._require()
        client = await self._http()
        response = await client.get(
            f"{self.url}/api/v3/{path}", params=params, timeout=timeout or self.timeout_s
        )
        response.raise_for_status()
        return response.json()

    # ── connection test ───────────────────────────────────────────────────
    async def test(self) -> dict:
        data = await self._get("system/status")
        name = data.get("appName") or self.flavour.app.title()
        version = data.get("version") or "?"
        count = len(await self.items())
        return {
            "ok": True,
            "detail": f"Connected to {name} {version} — {count} {self.flavour.noun} entries.",
        }

    # ── the library ───────────────────────────────────────────────────────
    async def items(self) -> list[dict]:
        """Every movie / series, in one call."""
        data = await self._get(self.flavour.noun, timeout=max(self.timeout_s, 90.0))
        return data if isinstance(data, list) else []

    async def by_external_id(self) -> dict[int, dict]:
        """The library keyed on tmdbId / tvdbId — the join key against `items`.

        Duplicates are possible (two Radarr entries for one tmdbId happens after
        a bad manual import). Last one wins, which matches Radarr's own UI
        behaviour of showing the most recently added.
        """
        out: dict[int, dict] = {}
        for entry in await self.items():
            external = entry.get(self.flavour.id_field)
            if external:
                out[int(external)] = entry
        return out

    async def moviefiles(self, file_ids: list[int]) -> dict[int, dict]:
        """Radarr only. File records keyed on movieFileId, with custom formats.

        See the module docstring: this endpoint is the only source of populated
        `customFormats` / `customFormatScore`, and it wants repeated params.
        """
        if self.flavour is not RADARR or not file_ids:
            return {}

        out: dict[int, dict] = {}
        unique = sorted({int(i) for i in file_ids if i})
        for start in range(0, len(unique), MOVIEFILE_CHUNK):
            chunk = unique[start:start + MOVIEFILE_CHUNK]
            # A list value makes httpx emit one `movieFileIds=` per element,
            # which is the form Radarr's model binder accepts.
            rows = await self._get("moviefile", params={"movieFileIds": chunk})
            for row in rows if isinstance(rows, list) else []:
                if row.get("id"):
                    out[int(row["id"])] = row
            await asyncio.sleep(0)  # yield between chunks
        return out

    # ── vocabularies, for pre-populating the rule builder ─────────────────
    async def _vocab_fetch(self, path: str) -> list[dict]:
        cached = self._vocab.get(path)
        now = time.monotonic()
        if cached and now - cached[0] < VOCAB_TTL_S:
            return cached[1]
        try:
            rows = await self._get(path)
        except Exception as exc:
            # A missing vocabulary degrades the typeahead; it must never break
            # a scan or blank the rule builder.
            logger.warning("%s %s fetch failed: %s", self.flavour.app, path, exc)
            rows = []
        rows = rows if isinstance(rows, list) else []
        self._vocab[path] = (now, rows)
        return rows

    async def quality_profiles(self) -> dict[int, str]:
        return {p["id"]: p["name"] for p in await self._vocab_fetch("qualityprofile")
                if p.get("id") is not None and p.get("name")}

    async def custom_format_names(self) -> list[str]:
        return sorted({f["name"] for f in await self._vocab_fetch("customformat") if f.get("name")})

    async def tags(self) -> dict[int, str]:
        return {t["id"]: t["label"] for t in await self._vocab_fetch("tag")
                if t.get("id") is not None and t.get("label")}

    async def root_folders(self) -> list[str]:
        return [r["path"] for r in await self._vocab_fetch("rootfolder") if r.get("path")]

"""TMDB client.

Rate-limited and cached. Movie metadata is close to immutable, so responses are
kept in `provider_cache` for 30 days by default — a re-scan after a settings
change shouldn't re-fetch two thousand records.
"""
import asyncio
import json
import time

import httpx

from backend.common.errors import NotConfiguredError
from backend.common.logging_config import get_logger

logger = get_logger(__name__)

BASE_URL = "https://api.themoviedb.org/3"
CACHE_TTL_S = 30 * 24 * 3600
MAX_RETRIES = 3


class TokenBucket:
    """Simple rate limiter. TMDB no longer publishes a hard cap, but hammering
    it earns a 429 and everything stalls, so pace it."""

    def __init__(self, rate_per_s: float = 20.0, burst: int = 20):
        self.rate = rate_per_s
        self.capacity = burst
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def take(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / self.rate
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1


class TmdbClient:
    def __init__(self, api_key: str, language: str = "en-US", db=None):
        self.api_key = api_key or ""
        self.language = language
        self.db = db
        self._bucket = TokenBucket()
        self._client: httpx.AsyncClient | None = None

    def _require(self) -> None:
        if not self.api_key:
            raise NotConfiguredError("TMDB is not configured — add an API key in Settings.")

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=20.0)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── cache ─────────────────────────────────────────────────────────────
    async def _cached(self, key: str) -> dict | None:
        if self.db is None:
            return None
        row = await self.db.fetch_one(
            "SELECT payload, fetched_at, ttl_s FROM provider_cache "
            "WHERE provider = 'tmdb' AND cache_key = ?", (key,),
        )
        if row is None:
            return None
        if time.time() - row["fetched_at"] > row["ttl_s"]:
            return None
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:
            return None

    async def _store(self, key: str, payload: dict) -> None:
        if self.db is None:
            return
        await self.db.execute(
            "INSERT INTO provider_cache (provider, cache_key, payload, fetched_at, ttl_s) "
            "VALUES ('tmdb', ?, ?, ?, ?) "
            "ON CONFLICT(provider, cache_key) DO UPDATE SET "
            "  payload=excluded.payload, fetched_at=excluded.fetched_at, ttl_s=excluded.ttl_s",
            (key, json.dumps(payload), int(time.time()), CACHE_TTL_S),
        )

    # ── requests ──────────────────────────────────────────────────────────
    async def _get(self, path: str, **params) -> dict:
        self._require()
        client = await self._http()
        params.update({"api_key": self.api_key, "language": self.language})

        for attempt in range(MAX_RETRIES):
            await self._bucket.take()
            response = await client.get(path, params=params)

            if response.status_code == 429:
                # Honour Retry-After rather than guessing.
                delay = float(response.headers.get("Retry-After", 2 ** attempt))
                logger.warning("TMDB rate limited; waiting %.1fs", delay)
                await asyncio.sleep(delay)
                continue
            if response.status_code == 404:
                return {}
            response.raise_for_status()
            return response.json()

        raise RuntimeError("TMDB rate limit did not clear after retries")

    async def movie(self, tmdb_id: int) -> dict:
        """Full movie record with keywords and credits, cached.

        The cache key carries a version because the append_to_response list is
        part of the payload's shape: bumping the append list without bumping
        the key would serve credit-less cached payloads for another 30 days.
        """
        key = f"movie:{tmdb_id}:{self.language}:v2"
        cached = await self._cached(key)
        if cached is not None:
            return cached
        data = await self._get(f"/movie/{tmdb_id}", append_to_response="keywords,credits")
        if data:
            await self._store(key, data)
        return data

    async def test(self) -> dict:
        """Connection test — a cheap, always-present record (Fight Club)."""
        self._require()
        data = await self._get("/movie/550")
        if not data:
            return {"ok": False, "detail": "TMDB returned no data for the test lookup."}
        return {"ok": True, "detail": f"Connected — resolved {data.get('title')!r}."}

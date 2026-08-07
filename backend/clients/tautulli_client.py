"""Tautulli client.

Ported from MediaCleanup/backend/api/tautulli.py, which is the best of the three
implementations in the code directory — a shared requests.Session, apikey as a
query param, and the response unwrapped from `response.data`.

The history pager below is the important part: Tautulli's `media_info` table
goes stale (MediaCleanup abandoned it for exactly this reason), so watch state
is rebuilt from `get_history`, which is always current.
"""
import asyncio
from typing import Any

import httpx

from backend.common.errors import NotConfiguredError
from backend.common.logging_config import get_logger

logger = get_logger(__name__)

PAGE_SIZE = 10_000
MAX_ROWS = 500_000  # safety cap, matching MediaCleanup's


class TautulliClient:
    def __init__(self, url: str, api_key: str):
        self.url = (url or "").rstrip("/")
        self.api_key = api_key or ""
        self._client: httpx.AsyncClient | None = None

    def _require(self) -> None:
        if not self.url or not self.api_key:
            raise NotConfiguredError("Tautulli is not configured — add its URL and API key in Settings.")

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _call(self, cmd: str, timeout: float = 30.0, **params) -> Any:
        self._require()
        client = await self._http()
        params.update({"apikey": self.api_key, "cmd": cmd})
        response = await client.get(f"{self.url}/api/v2", params=params, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        if payload.get("response", {}).get("result") != "success":
            raise RuntimeError(f"Tautulli returned an error for {cmd}: {payload}")
        return payload.get("response", {}).get("data", {})

    async def test(self) -> dict:
        data = await self._call("get_server_friendly_name")
        name = data if isinstance(data, str) else "connected"
        return {"ok": True, "detail": f"Connected to {name}."}

    async def watch_map(self) -> dict[str, dict]:
        """Aggregate watch state for the whole library, keyed on rating_key.

        One paged sweep of get_history rather than a call per item: at library
        scale the per-item form is thousands of round-trips for data that
        arrives in a handful of pages.
        """
        self._require()
        out: dict[str, dict] = {}
        start = 0
        total: int | None = None

        while start < MAX_ROWS:
            data = await self._call(
                "get_history", timeout=90.0,
                length=PAGE_SIZE, start=start,
                order_column="date", order_dir="desc",
            )
            rows = data.get("data", []) if isinstance(data, dict) else (data or [])
            if not rows:
                break
            if total is None and isinstance(data, dict):
                total = data.get("recordsFiltered") or data.get("recordsTotal") or 0

            for row in rows:
                key = str(row.get("rating_key") or "")
                if not key:
                    continue
                entry = out.setdefault(key, {
                    "play_count": 0, "last_played": None,
                    "users": set(), "completed": 0, "started": 0,
                })
                entry["play_count"] += 1
                entry["started"] += 1
                stopped = row.get("date") or row.get("stopped")
                if stopped and (entry["last_played"] is None or stopped > entry["last_played"]):
                    entry["last_played"] = stopped
                if row.get("user"):
                    entry["users"].add(row["user"])
                # watched_status: 1 = finished, 0.5 = partial, 0 = barely started
                if float(row.get("watched_status") or 0) >= 1:
                    entry["completed"] += 1

            start += len(rows)
            if total and start >= total:
                break
            if len(rows) < PAGE_SIZE:
                break
            await asyncio.sleep(0)  # yield to the loop between pages

        logger.info("📺 Tautulli history: %d items with plays", len(out))
        return out

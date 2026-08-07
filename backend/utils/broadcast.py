"""Coalescing SSE broadcaster.

Workers mutate shared state; a single task ticks at a fixed interval and pushes
to clients only when the state actually changed.

Why not push directly from the workers: a scan produces thousands of updates a
second. Written straight into per-client `Queue(maxsize=10)` buffers, every
queue fills and progress becomes lossy in an undefined way — the UI would freeze
at an arbitrary percentage. Coalescing decouples worker speed from client count.

It also removes any special-casing for scheduled runs: a scan that starts at
03:00 with nobody watching just mutates state, and a browser opening at 03:20
receives the current snapshot in its `init` event and picks up live from there.
"""
import asyncio
import json
from typing import Any

from backend.common.logging_config import get_logger

logger = get_logger(__name__)

TICK_SECONDS = 0.25
CLIENT_QUEUE_SIZE = 10


class Broadcaster:
    def __init__(self, tick: float = TICK_SECONDS):
        self.tick = tick
        self.clients: list[asyncio.Queue] = []
        self.state: dict[str, Any] = {"scan": None, "sync": None}
        self._last_hash: int | None = None
        self._task: asyncio.Task | None = None
        self._urgent: asyncio.Event = asyncio.Event()

    # ── client registration ───────────────────────────────────────────────
    def register(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE)
        self.clients.append(q)
        return q

    def unregister(self, q: asyncio.Queue) -> None:
        if q in self.clients:
            self.clients.remove(q)

    def snapshot(self) -> dict[str, Any]:
        """Current state, sent to a client in its `init` event."""
        return dict(self.state)

    # ── producers ─────────────────────────────────────────────────────────
    def set_state(self, key: str, value: Any) -> None:
        """Update coalesced state. Cheap — safe to call per item."""
        self.state[key] = value

    def emit(self, event: str, data: Any) -> None:
        """Push a discrete event immediately, bypassing coalescing.

        For things that happen once and must not be dropped: scan_done,
        sync_done, settings_changed, toasts.
        """
        self._push({"event": event, "data": json.dumps(data, default=str)})

    def flush_soon(self) -> None:
        """Ask the ticker to publish on its next loop without waiting out the
        full interval. Used at phase boundaries so the UI feels responsive."""
        self._urgent.set()

    def _push(self, message: dict[str, str]) -> None:
        for q in list(self.clients):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # A client too slow to keep up loses this frame. Coalesced state
                # is re-sent on the next tick, so it self-heals.
                pass

    # ── ticker ────────────────────────────────────────────────────────────
    async def _run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._urgent.wait(), timeout=self.tick)
            except asyncio.TimeoutError:
                pass
            self._urgent.clear()

            if not self.clients:
                continue
            payload = json.dumps(self.state, default=str, sort_keys=True)
            h = hash(payload)
            if h == self._last_hash:
                continue
            self._last_hash = h
            self._push({"event": "state", "data": payload})

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="sse-broadcaster")
            logger.info("📡 SSE broadcaster started (%.0fms tick)", self.tick * 1000)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

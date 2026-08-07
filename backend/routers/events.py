"""Server-sent events.

One stream for everything: scan progress, sync progress, settings changes, toasts.

Shape matches palworld-lens/backend/routers/watch.py — bounded per-client queue,
`init` on connect, 30s `ping` from the wait_for timeout (which is what keeps
nginx from severing a quiet stream), disconnect polling, deregistration in a
`finally`. The difference is that producers write to the coalescing broadcaster
rather than to these queues directly.
"""
import asyncio
import json

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from backend import startup
from backend.common.auth import require_auth
from backend.common.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["events"])

PING_INTERVAL_S = 30.0


@router.get("/events", dependencies=[Depends(require_auth)])
async def events(request: Request):
    broadcaster = startup.broadcaster
    if broadcaster is None:
        return EventSourceResponse(iter(()))

    queue = broadcaster.register()

    async def event_generator():
        try:
            # A client connecting mid-scan needs the current picture before deltas.
            yield {
                "event": "init",
                "data": json.dumps(broadcaster.snapshot(), default=str),
            }
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=PING_INTERVAL_S)
                    yield message
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": ""}
                except asyncio.CancelledError:
                    break
                except Exception as exc:  # pragma: no cover
                    logger.error("SSE stream error: %s", exc)
                    break
        finally:
            broadcaster.unregister(queue)

    return EventSourceResponse(event_generator())

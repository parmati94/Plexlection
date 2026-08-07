"""Readiness endpoint.

This is what the container HEALTHCHECK curls — unlike nginx's /health, reaching
this proves the backend is actually alive and the database is answering.
"""
from fastapi import APIRouter

from backend import startup
from backend.common.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health_check():
    db_ok = False
    item_count = 0
    if startup.db is not None:
        try:
            item_count = await startup.db.fetch_val(
                "SELECT COUNT(*) FROM items WHERE deleted_at IS NULL", default=0
            )
            db_ok = True
        except Exception as exc:  # pragma: no cover
            logger.error("Health check DB query failed: %s", exc)

    return {
        "status": "healthy" if db_ok else "degraded",
        "database": db_ok,
        "items": item_count,
        "sse_clients": len(startup.broadcaster.clients) if startup.broadcaster else 0,
    }

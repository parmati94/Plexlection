"""The fact registry, served to the UI.

This endpoint is the extensibility mechanism made concrete: the rule builder
renders itself entirely from this payload, so adding a provider adds filter
options with no frontend change.
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from backend import startup
from backend.common.auth import require_auth
from backend.common.logging_config import get_logger
from backend.facts.spec import FactType
from backend.rules.sql import json_path

logger = get_logger(__name__)
router = APIRouter(prefix="/api/facts", tags=["facts"])


@router.get("/registry", dependencies=[Depends(require_auth)])
async def get_registry():
    coverage = await startup.scan_engine.coverage()
    facts = startup.registry.to_wire(coverage)
    return {
        "facts": facts,
        "count": len(facts),
        "groups": startup.registry.groups(),
        "providers": [
            {**p.describe(), **{"coverage": coverage.get(p.id, {})}}
            for p in startup.providers
        ],
    }


@router.get("/coverage", dependencies=[Depends(require_auth)])
async def get_coverage():
    return {"coverage": await startup.scan_engine.coverage()}


@router.get("/{key}/values", dependencies=[Depends(require_auth)])
async def distinct_values(key: str, q: str = Query(""), limit: int = Query(50, ge=1, le=200)):
    """Typeahead for a fact's observed values.

    Backs the `suggest` value editor — for something like tmdb.keywords, listing
    what's actually in your library beats making the user guess.
    """
    spec = startup.registry.get(key)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Unknown fact key {key!r}")

    path = json_path(spec.key)
    needle = f"%{q.lower()}%" if q else "%"

    if spec.type is FactType.LIST:
        sql = (
            f"SELECT je.value AS value, COUNT(*) AS n "
            f"FROM items, json_each(items.facts, '{path}') je "
            f"WHERE items.deleted_at IS NULL AND lower(je.value) LIKE ? "
            f"GROUP BY je.value ORDER BY n DESC, value LIMIT ?"
        )
    else:
        sql = (
            f"SELECT json_extract(facts, '{path}') AS value, COUNT(*) AS n "
            f"FROM items WHERE deleted_at IS NULL "
            f"AND json_extract(facts, '{path}') IS NOT NULL "
            f"AND lower(CAST(json_extract(facts, '{path}') AS TEXT)) LIKE ? "
            f"GROUP BY value ORDER BY n DESC, value LIMIT ?"
        )

    rows = await startup.db.fetch_all(sql, (needle, limit))
    values = [{"value": r["value"], "count": r["n"]} for r in rows]

    # Fold in the provider's controlled vocabulary. Observed values come first
    # and keep their counts — what's actually in your library is the more useful
    # suggestion — with the rest of the vocabulary listed behind them at count 0
    # so a custom format you own but haven't matched yet is still selectable.
    seen = {str(v["value"]).lower() for v in values}
    for candidate in await _vocabulary(spec):
        if len(values) >= limit:
            break
        if candidate.lower() in seen or (q and q.lower() not in candidate.lower()):
            continue
        seen.add(candidate.lower())
        values.append({"value": candidate, "count": 0})

    return {"key": key, "values": values, "count": len(values)}


async def _vocabulary(spec) -> list[str]:
    """Authoritative values the owning provider publishes for this key."""
    provider = next((p for p in startup.providers if p.id == spec.provider), None)
    if provider is None:
        return []
    try:
        return (await provider.options()).get(spec.key) or []
    except Exception as exc:
        logger.warning("vocabulary lookup for %s failed: %s", spec.key, exc)
        return []

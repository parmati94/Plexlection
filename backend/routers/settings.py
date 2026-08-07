"""Settings CRUD and per-service connection testing."""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend import startup
from backend.common.auth import require_auth
from backend.common.errors import NotConfiguredError
from backend.common.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsPatch(BaseModel):
    """A partial settings tree. Deep-merged over what's stored, so a payload
    carrying only {"plex": {"url": ...}} never drops the token beside it."""
    model_config = {"extra": "allow"}


@router.get("", dependencies=[Depends(require_auth)])
async def get_settings():
    store = startup.settings_store
    return {
        "settings": store.redacted(),
        "configured": store.configured(),
        "version": store.version,
    }


@router.put("", dependencies=[Depends(require_auth)])
async def put_settings(patch: dict[str, Any]):
    store = startup.settings_store
    try:
        await store.update(patch)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # A new API key must make its provider live immediately, which means
    # rebuilding the provider list, the registry the UI reads, and the engines
    # holding references to them.
    startup.rebuild_providers()
    if startup.scheduler:
        startup.scheduler.reconfigure()
    if startup.broadcaster:
        startup.broadcaster.emit("settings_changed", {"version": store.version})

    return {
        "success": True,
        "settings": store.redacted(),
        "configured": store.configured(),
        "version": store.version,
    }


@router.post("/test/{service}", dependencies=[Depends(require_auth)])
async def test_service(service: str):
    """Probe a service with the currently-saved credentials.

    Always 200 with {ok: bool} — a failed connection test is a normal answer to
    the question, not an HTTP error.
    """
    if service == "plex":
        try:
            info = await startup.get_plex().test()
            return {"ok": True, "detail": f"{info['name']} (Plex {info['version']})", "info": info}
        except NotConfiguredError as exc:
            return {"ok": False, "detail": str(exc)}
        except Exception as exc:
            logger.warning("Plex connection test failed: %s", exc)
            return {"ok": False, "detail": str(exc)}

    if service in ("tmdb", "tautulli", "radarr", "sonarr"):
        # Test through the provider's own client, so a passing test means the
        # provider will work — not merely that the host answers.
        provider = next((p for p in startup.providers if p.id == service), None)
        if provider is None or provider.client is None:
            return {"ok": False, "detail": f"{service} provider is not available."}
        if not provider.is_configured():
            return {"ok": False, "detail": provider.not_configured_reason()}
        try:
            return await provider.client.test()
        except NotConfiguredError as exc:
            return {"ok": False, "detail": str(exc)}
        except Exception as exc:
            logger.warning("%s connection test failed: %s", service, exc)
            return {"ok": False, "detail": str(exc)}

    raise HTTPException(status_code=404, detail=f"Unknown service {service!r}")


@router.get("/schedule", dependencies=[Depends(require_auth)])
async def get_schedule():
    """Configured jobs and when each next fires."""
    return {
        "schedule": startup.settings_store.get().schedule.model_dump(),
        "jobs": startup.scheduler.jobs() if startup.scheduler else [],
    }


@router.get("/plex/sections", dependencies=[Depends(require_auth)])
async def plex_sections():
    """Libraries available on the server, for the picker in Settings."""
    try:
        sections = await startup.get_plex().sections()
    except NotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Plex: {exc}") from exc

    selected = set(startup.settings_store.get().plex.libraries)
    return {
        "sections": [
            {
                "key": s.key,
                "title": s.title,
                "type": s.type,
                "item_count": s.item_count,
                "selected": s.key in selected,
                "supported": s.type in ("movie", "show"),
            }
            for s in sections
        ],
        "count": len(sections),
    }

"""Plexlection — FastAPI backend.

Plex is the render target, not the query engine: this service computes facts Plex
does not have, evaluates user-defined rules over them, and materialises the
results as Plex collections.
"""
# setup_logging() runs before any other project import so that module-level
# `logger = get_logger(__name__)` calls inherit the configured root handler.
# The import order here is deliberate (E402).
from backend.common.logging_config import setup_logging, get_logger  # isort: skip

setup_logging()
logger = get_logger(__name__)

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from backend import startup  # noqa: E402
from backend.common.errors import (  # noqa: E402
    NotConfiguredError,
    RuleError,
    ScanBusyError,
    SyncGuardError,
)
from backend.routers import (  # noqa: E402
    auth,
    collections,
    events,
    facts,
    health,
    items,
    paths,
    rules,
    scan,
    settings,
)

app = FastAPI(
    title="Plexlection",
    description="Rule-driven Plex collections built from facts Plex doesn't have",
    version="0.1.0",
    lifespan=startup.lifespan,
)

# No CORS middleware: nginx serves the frontend and proxies /api from the same
# origin. palworld-lens sets allow_origins=["*"] with allow_credentials=True,
# which browsers reject anyway — it's dead configuration.

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(events.router)
app.include_router(settings.router)
app.include_router(paths.router)
app.include_router(items.router)
app.include_router(facts.router)
app.include_router(rules.router)
app.include_router(collections.router)
app.include_router(scan.router)


@app.get("/")
async def root():
    return {"message": "Plexlection API", "version": app.version}


# ── Domain exception handlers ─────────────────────────────────────────────
@app.exception_handler(RuleError)
async def _rule_error(request: Request, exc: RuleError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(SyncGuardError)
async def _sync_guard(request: Request, exc: SyncGuardError):
    """409 carries the diff so the UI can render an explicit 'sync anyway'."""
    return JSONResponse(
        status_code=409,
        content={"detail": str(exc), "guard": True, "diff": exc.diff},
    )


@app.exception_handler(ScanBusyError)
async def _scan_busy(request: Request, exc: ScanBusyError):
    return JSONResponse(
        status_code=409,
        content={"detail": str(exc), "busy": True, "run": exc.run},
    )


@app.exception_handler(NotConfiguredError)
async def _not_configured(request: Request, exc: NotConfiguredError):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    logger.error("Unhandled exception on %s: %s", request.url.path, exc, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=False)

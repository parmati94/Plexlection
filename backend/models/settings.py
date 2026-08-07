"""The UI-editable settings tree.

Deployment config (log level, auth, data dir) is env-only and lives in
backend/common/config.py. Everything here is edited in the Settings tab and
persisted to SQLite; env vars only seed it on first run.
"""
from pydantic import BaseModel, Field


class PlexSettings(BaseModel):
    url: str = ""
    token: str = ""
    # Section keys to index. Empty means "ask the user to choose" rather than
    # silently indexing every library on the server.
    libraries: list[str] = Field(default_factory=list)
    # Label namespace. Configurable because label-driven sync writes visible
    # metadata onto your items and not everyone wants our name on it.
    label_prefix: str = "plexlection"
    verify_ssl: bool = True
    timeout_s: int = 30


class TmdbSettings(BaseModel):
    api_key: str = ""
    language: str = "en-US"


class TautulliSettings(BaseModel):
    url: str = ""
    api_key: str = ""


class PathMapping(BaseModel):
    """Translates a path as Plex reports it to one this container can open."""
    plex: str
    local: str


class ScanSettings(BaseModel):
    # Per-provider concurrency, keyed on provider id. Unknown keys are ignored
    # and missing ones fall back to the provider's own default.
    concurrency: dict[str, int] = Field(
        default_factory=lambda: {"plex": 1, "ffprobe": 4, "tmdb": 4, "tautulli": 1}
    )
    ffprobe_timeout_s: int = 60
    # Mixes a hash of the first/last 1MiB into the file fingerprint, catching
    # replacements that preserve both size and mtime (cp --preserve=timestamps).
    # Costs a seek plus 2MiB per item per scan. Off by default.
    deep_fingerprint: bool = False
    resume_on_start: bool = True


class SafetySettings(BaseModel):
    # Nothing writes to Plex until this is deliberately turned off.
    dry_run: bool = True
    refuse_empty_result: bool = True
    # A rule that suddenly drops most of its members is far more likely to be a
    # provider regression than an intentional change.
    max_removal_fraction: float = 0.25
    min_removal_alarm: int = 20
    max_stale_fraction: float = 0.10
    max_changes_per_sync: int = 2000


class ScheduleSettings(BaseModel):
    enabled: bool = False
    scan_incremental: str = "0 4 * * *"
    scan_deep: str = "0 3 * * 0"
    sync_all: str = "30 4 * * *"


class Settings(BaseModel):
    plex: PlexSettings = Field(default_factory=PlexSettings)
    tmdb: TmdbSettings = Field(default_factory=TmdbSettings)
    tautulli: TautulliSettings = Field(default_factory=TautulliSettings)
    path_mappings: list[PathMapping] = Field(default_factory=list)
    scan: ScanSettings = Field(default_factory=ScanSettings)
    safety: SafetySettings = Field(default_factory=SafetySettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)


# Dotted paths whose values are never returned to the client in the clear.
SECRET_PATHS: frozenset[str] = frozenset({
    "plex.token",
    "tmdb.api_key",
    "tautulli.api_key",
})

# Top-level sections, used for per-section persistence.
SECTIONS: tuple[str, ...] = tuple(Settings.model_fields.keys())

"""The UI-editable settings tree.

Deployment config (log level, auth, data dir) is env-only and lives in
backend/common/config.py. Everything here is edited in the Settings tab and
persisted to SQLite; env vars only seed it on first run.
"""
from pydantic import BaseModel, Field, field_validator


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


class ArrInstance(BaseModel):
    """One Radarr or Sonarr server. Both v3 APIs take the same two fields.

    The name is an identifier, not a caption: it appears in facts
    (`radarr.instance`, `radarr.instances`) and therefore in rules, so renaming
    an instance orphans any rule that filters on the old name.
    """
    name: str = "main"
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
        default_factory=lambda: {
            "plex": 1, "ffprobe": 4, "tmdb": 4, "tautulli": 1,
            # Both arr providers fetch the whole library in one sweep, so
            # concurrency above 1 would buy nothing.
            "radarr": 1, "sonarr": 1,
        }
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
    radarr: list[ArrInstance] = Field(default_factory=list)
    sonarr: list[ArrInstance] = Field(default_factory=list)
    path_mappings: list[PathMapping] = Field(default_factory=list)
    scan: ScanSettings = Field(default_factory=ScanSettings)
    safety: SafetySettings = Field(default_factory=SafetySettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)

    @field_validator("radarr", "sonarr", mode="before")
    @classmethod
    def _migrate_single_instance(cls, value):
        """Stored settings from before multi-instance support hold one object,
        not a list. An unconfigured one becomes an empty list rather than a
        blank row the UI would have to explain."""
        if isinstance(value, dict):
            return [{"name": "main", **value}] if (value.get("url") or value.get("api_key")) else []
        return value

    @field_validator("radarr", "sonarr", mode="after")
    @classmethod
    def _require_distinct_names(cls, value: list[ArrInstance]) -> list[ArrInstance]:
        for inst in value:
            inst.name = inst.name.strip() or "main"
        names = [inst.name for inst in value]
        if len(names) != len(set(names)):
            raise ValueError("Instance names must be unique — they identify "
                             "each server in facts and rules.")
        return value


# Dotted paths whose values are never returned to the client in the clear.
# A `*` segment means "every element of the list at that key".
SECRET_PATHS: frozenset[str] = frozenset({
    "plex.token",
    "tmdb.api_key",
    "tautulli.api_key",
    "radarr.*.api_key",
    "sonarr.*.api_key",
})

# Top-level sections, used for per-section persistence.
SECTIONS: tuple[str, ...] = tuple(Settings.model_fields.keys())

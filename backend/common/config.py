"""Deployment configuration.

Env-only, read once at import. This layer covers things that must be known before
anything boots and that a user never edits at runtime.

Everything operational — Plex URL/token, TMDB key, path mappings, scan tuning —
lives in the UI-editable settings store (backend/common/settings.py), NOT here.
The env vars listed under "first-run seeding" in docker-compose.yml are read
exactly once, to seed that store, and ignored forever after.
"""
import os
from pathlib import Path


def _bool(name: str, default: str) -> bool:
    return os.getenv(name, default).lower() in ("true", "1", "yes")


class Config:
    """Application configuration."""

    # ── Logging ───────────────────────────────────────────────────────────
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

    # ── Paths ─────────────────────────────────────────────────────────────
    DATA_DIR: Path = Path(os.getenv("DATA_DIR", "/app/data"))

    # ── External binaries ─────────────────────────────────────────────────
    # Always the container's own binaries. A bind mount shares the filesystem,
    # not the executables, so the host's ffmpeg is never involved.
    FFMPEG_BIN: str = os.getenv("FFMPEG_BIN", "ffmpeg")
    FFPROBE_BIN: str = os.getenv("FFPROBE_BIN", "ffprobe")

    # ── Authentication ────────────────────────────────────────────────────
    ENABLE_LOGIN: bool = _bool("ENABLE_LOGIN", "false")
    USERNAME: str = os.getenv("USERNAME", "admin")
    PASSWORD: str = os.getenv("PASSWORD", "admin")
    SESSION_SECRET: str = os.getenv("SESSION_SECRET", "change-me-in-production-please")

    # ── Login rate limiting ───────────────────────────────────────────────
    LOGIN_MAX_ATTEMPTS: int = int(os.getenv("LOGIN_MAX_ATTEMPTS", "10"))
    LOGIN_WINDOW_S: int = int(os.getenv("LOGIN_WINDOW_S", "300"))

    @classmethod
    def db_path(cls) -> Path:
        return cls.DATA_DIR / "plexlection.db"

    @classmethod
    def config_path(cls) -> Path:
        return cls.DATA_DIR / "config.yaml"

    @classmethod
    def poster_dir(cls) -> Path:
        return cls.DATA_DIR / "posters"

    @classmethod
    def ensure_dirs(cls) -> None:
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)
        cls.poster_dir().mkdir(parents=True, exist_ok=True)


config = Config()

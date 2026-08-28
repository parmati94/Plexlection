"""Settings store — SQLite-backed, UI-editable.

Three behaviours worth calling out:

* **Secret round-trip.** GET returns secrets masked. A PUT that sends the mask
  straight back means "unchanged", not "set it to a row of dots". Without this,
  the obvious UI implementation (load form, edit one field, save whole form)
  silently wipes every credential the user couldn't see.
* **Deep merge.** A PATCH carrying only `{"plex": {"url": "..."}}` must not drop
  the token stored alongside it.
* **Version bump.** Clients that cache derived objects (the Plex connection, the
  path mapper) rebuild when the version changes, rather than being invalidated
  by hand at every call site.
"""
import json
import os
from typing import Any

from backend.common.logging_config import get_logger
from backend.models.settings import SECRET_PATHS, SECTIONS, Settings

logger = get_logger(__name__)

MASK = "\u2022" * 8  # ••••••••


def _deep_merge(base: dict, patch: dict) -> dict:
    """Recursive dict merge. Lists are replaced wholesale — a path-mapping list
    edit is a replacement, not an append."""
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _get_path(data: dict, path: str) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _set_path(data: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _split_wildcard(path: str) -> tuple[str, str] | None:
    """('radarr', 'api_key') for 'radarr.*.api_key'; None for a plain path."""
    if ".*." not in path:
        return None
    prefix, field = path.split(".*.", 1)
    return prefix, field


def _resolve_masked_list(patch: dict, current: dict, prefix: str, field: str) -> None:
    """Restore masked secrets inside a list of named instances.

    Elements are matched to their stored counterpart by name first — so a
    reordered list keeps every credential — and by position as the fallback for
    an element renamed and saved in one edit.
    """
    patch_list = patch.get(prefix)
    if not isinstance(patch_list, list):
        return
    current_list = current.get(prefix) or []
    by_name = {e.get("name"): e for e in current_list if isinstance(e, dict)}
    for i, element in enumerate(patch_list):
        if not (isinstance(element, dict) and element.get(field) == MASK):
            continue
        stored = by_name.get(element.get("name"))
        if stored is None and i < len(current_list):
            stored = current_list[i]
        element[field] = (stored or {}).get(field, "")


class SettingsStore:
    def __init__(self, db):
        self.db = db
        self._cache: Settings | None = None
        self.version: int = 0

    # ── load / persist ────────────────────────────────────────────────────
    async def load(self) -> Settings:
        rows = await self.db.fetch_all("SELECT key, value FROM settings")
        raw: dict[str, Any] = {}
        for row in rows:
            if row["key"] in SECTIONS:
                try:
                    raw[row["key"]] = json.loads(row["value"])
                except json.JSONDecodeError:
                    logger.warning("Settings section %r is corrupt; using defaults", row["key"])
        self._cache = Settings.model_validate(raw)
        return self._cache

    def get(self) -> Settings:
        if self._cache is None:
            raise RuntimeError("SettingsStore.load() has not been called")
        return self._cache

    async def _persist(self, settings: Settings) -> None:
        dumped = settings.model_dump()
        await self.db.execute_many(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(section, json.dumps(dumped[section])) for section in SECTIONS],
        )

    async def update(self, patch: dict) -> Settings:
        """Deep-merge a partial patch, resolve masked secrets, validate, persist."""
        current = self.get().model_dump()

        # A secret arriving as the mask means the user never edited it.
        for path in SECRET_PATHS:
            wildcard = _split_wildcard(path)
            if wildcard:
                _resolve_masked_list(patch, current, *wildcard)
            elif _get_path(patch, path) == MASK:
                _set_path(patch, path, _get_path(current, path))

        merged = _deep_merge(current, patch)
        settings = Settings.model_validate(merged)  # raises ValidationError -> 422
        await self._persist(settings)
        self._cache = settings
        self.version += 1
        logger.info("⚙️  Settings updated (version %d)", self.version)
        return settings

    # ── presentation ──────────────────────────────────────────────────────
    def redacted(self) -> dict:
        """The settings tree with secrets masked, for GET /api/settings."""
        data = self.get().model_dump()
        for path in SECRET_PATHS:
            wildcard = _split_wildcard(path)
            if wildcard:
                prefix, field = wildcard
                for element in data.get(prefix) or []:
                    element[field] = MASK if element.get(field) else ""
            else:
                value = _get_path(data, path)
                _set_path(data, path, MASK if value else "")
        return data

    def configured(self) -> dict[str, bool]:
        """Which integrations have enough credentials to attempt a call."""
        s = self.get()
        return {
            "plex": bool(s.plex.url and s.plex.token),
            "tmdb": bool(s.tmdb.api_key),
            "tautulli": bool(s.tautulli.url and s.tautulli.api_key),
            "radarr": any(i.url and i.api_key for i in s.radarr),
            "sonarr": any(i.url and i.api_key for i in s.sonarr),
        }

    # ── first-run seeding ─────────────────────────────────────────────────
    async def seed_from_env(self) -> None:
        """Populate empty fields from env vars, once.

        Keeps one-shot docker-compose provisioning working while leaving the UI
        as the ongoing source of truth: only fields that are still empty are
        touched, so this never clobbers something set in the app.
        """
        patch: dict[str, Any] = {}

        def maybe(section: str, field: str, env: str) -> None:
            value = os.getenv(env, "").strip()
            if value and not getattr(getattr(self.get(), section), field):
                patch.setdefault(section, {})[field] = value

        maybe("plex", "url", "PLEX_URL")
        maybe("plex", "token", "PLEX_TOKEN")
        maybe("tmdb", "api_key", "TMDB_API_KEY")
        maybe("tautulli", "url", "TAUTULLI_URL")
        maybe("tautulli", "api_key", "TAUTULLI_API_KEY")

        # Arr sections are lists of instances; env vars seed the first one.
        for section, env_prefix in (("radarr", "RADARR"), ("sonarr", "SONARR")):
            url = os.getenv(f"{env_prefix}_URL", "").strip()
            key = os.getenv(f"{env_prefix}_API_KEY", "").strip()
            if url and key and not getattr(self.get(), section):
                patch[section] = [{"name": "main", "url": url, "api_key": key}]

        # MEDIA_PATH_MAP="/data/movies:/media/videos/Movies,/data/tv:/media/videos/Shows"
        raw_map = os.getenv("MEDIA_PATH_MAP", "").strip()
        if raw_map and not self.get().path_mappings:
            mappings = []
            for pair in raw_map.split(","):
                if ":" not in pair:
                    continue
                # rsplit so Windows drive letters ("Z:\Movies:/media") survive.
                plex, local = pair.rsplit(":", 1)
                if plex.strip() and local.strip():
                    mappings.append({"plex": plex.strip(), "local": local.strip()})
            if mappings:
                patch["path_mappings"] = mappings

        if patch:
            logger.info("🌱 Seeding settings from env: %s", ", ".join(sorted(patch)))
            await self.update(patch)

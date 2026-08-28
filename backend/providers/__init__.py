"""Fact providers.

**Adding a capability to Plexlection means adding a module here and listing it
in `build_providers`.** Registry collection, expression indexes, the /api/facts
wire format, the rule-builder filter options, the provider card on the Scan tab
and the coverage statistics all follow from that one edit — no UI changes.
"""
from backend.clients.arr_client import RADARR, SONARR, ArrClient
from backend.clients.tautulli_client import TautulliClient
from backend.clients.tmdb_client import TmdbClient
from backend.providers.base import FactProvider
from backend.providers.derived import DerivedProvider
from backend.providers.ffprobe import FFprobeProvider
from backend.providers.plex import PlexFactProvider
from backend.providers.radarr import RadarrProvider
from backend.providers.show_rollup import ShowRollupProvider
from backend.providers.sonarr import SonarrProvider
from backend.providers.tautulli import TautulliProvider
from backend.providers.tmdb import TmdbProvider


def build_providers(settings, db=None) -> list[FactProvider]:
    """Instantiate every provider for the current settings.

    Rebuilt whenever settings change, so a newly-entered API key takes effect
    without a restart. Ordering here is informative only — the scan engine
    topologically sorts on `depends_on`.
    """
    return [
        PlexFactProvider(settings),
        FFprobeProvider(settings),
        TmdbProvider(settings, client=TmdbClient(
            settings.tmdb.api_key, settings.tmdb.language, db=db
        )),
        TautulliProvider(settings, client=TautulliClient(
            settings.tautulli.url, settings.tautulli.api_key
        )),
        RadarrProvider(settings, clients=[
            ArrClient(RADARR, i.url, i.api_key, name=i.name) for i in settings.radarr
        ]),
        SonarrProvider(settings, clients=[
            ArrClient(SONARR, i.url, i.api_key, name=i.name) for i in settings.sonarr
        ]),
        # Last: these read what the others produced.
        DerivedProvider(settings),
        ShowRollupProvider(settings, db=db),
    ]


__all__ = ["FactProvider", "build_providers"]

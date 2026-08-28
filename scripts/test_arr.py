#!/usr/bin/env python3
"""Radarr / Sonarr extraction tests — no network required.

Payload shapes are recorded from a live Radarr 6.2 and Sonarr 4.0, so the cases
that actually bit during development are pinned here:

  * `/api/v3/movie` embeds a `movieFile` whose `customFormats` is always empty
    and whose `customFormatScore` is always null. The real values only exist on
    `/api/v3/moviefile`. A regression that silently drops back to the embedded
    copy would leave every custom format fact blank while still reporting `ok`,
    which is the worst kind of failure — a rule that quietly matches nothing.
  * A movie Radarr doesn't track must produce `managed: false` rather than an
    error, because "in Plex but not in Radarr" is a collection people want.
  * Editions arrive as UNRATED / Unrated / unrated in one library.
  * A series with nothing aired yet has 0 expected episodes, and must not be
    reported as incomplete.

    docker exec plexlection-dev python3 /app/scripts/test_arr.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, "/app" if Path("/app/backend").exists() else
                str(Path(__file__).resolve().parent.parent))

from backend.models.settings import Settings  # noqa: E402
from backend.providers.base import EnrichContext, ItemRow  # noqa: E402
from backend.providers.radarr import RadarrProvider  # noqa: E402
from backend.providers.sonarr import SonarrProvider  # noqa: E402

PASS, FAIL = "\033[32m✓\033[0m", "\033[31m✗\033[0m"
failures = 0


def check(label, got, want=True):
    global failures
    ok = got == want
    if not ok:
        failures += 1
    print(f"  {PASS if ok else FAIL} {label:<52} got={got!r}")


# ── recorded payloads ─────────────────────────────────────────────────────
MOVIE = {
    "id": 42, "tmdbId": 335984, "title": "Blade Runner 2049",
    "qualityProfileId": 7, "monitored": True, "status": "released",
    "sizeOnDisk": 64_000_000_000, "rootFolderPath": "/media/Movies",
    "studio": "Alcon Entertainment", "certification": "R",
    "tags": [1, 99], "added": "2023-05-31T00:24:47Z", "movieFileId": 900,
    # As Radarr actually returns it: the fields exist but are never filled in.
    "movieFile": {
        "id": 900, "edition": "", "releaseGroup": "FraMeSToR",
        "customFormats": [], "customFormatScore": None,
        "qualityCutoffNotMet": False,
        "quality": {"quality": {"name": "Remux-2160p", "source": "bluray",
                                "resolution": 2160, "modifier": "remux"},
                    "revision": {"version": 1, "isRepack": False}},
    },
}

MOVIEFILE = {
    "id": 900, "edition": "UNRATED", "releaseGroup": "FraMeSToR",
    "customFormats": [{"name": "HDR"}, {"name": "DV Boost"},
                      {"name": "UHD Bluray Tier 01"}],
    "customFormatScore": 1705, "qualityCutoffNotMet": True,
    "quality": {"quality": {"name": "Remux-2160p", "source": "bluray",
                            "resolution": 2160, "modifier": "remux"},
                "revision": {"version": 2, "isRepack": True}},
}

SERIES = {
    "id": 7, "tvdbId": 76107, "title": "Cowboy Bebop",
    "qualityProfileId": 4, "monitored": True, "status": "ended", "ended": True,
    "seriesType": "anime", "network": "TV Tokyo", "runtime": 25,
    "certification": "TV-14", "rootFolderPath": "/media/Shows",
    "genres": ["Animation", "Action"], "tags": [3],
    "ratings": {"value": 8.6}, "originalLanguage": {"name": "Japanese"},
    "added": "2022-01-02T03:04:05Z", "firstAired": "1998-04-03T00:00:00Z",
    "lastAired": "1999-04-24T00:00:00Z",
    "statistics": {"seasonCount": 1, "episodeCount": 26, "episodeFileCount": 24,
                   "sizeOnDisk": 30_000_000_000, "releaseGroups": ["NTb", "CtrlHD"],
                   "percentOfEpisodes": 92.3},
}

UNAIRED = {
    "id": 8, "tvdbId": 99999, "title": "Announced Show",
    "qualityProfileId": 4, "monitored": True, "status": "upcoming", "ended": False,
    "seriesType": "standard",
    "statistics": {"seasonCount": 1, "episodeCount": 0, "episodeFileCount": 0,
                   "sizeOnDisk": 0, "releaseGroups": [], "percentOfEpisodes": 0},
}

PROFILES = {7: "Ultra-HD", 4: "1080p/4k"}
TAGS = {1: "4k", 3: "dual", 99: "keep"}


class FakeArr:
    """Stands in for ArrClient. Records whether the moviefile sweep happened."""

    def __init__(self, by_id, files=None, fail_files=False, name="main"):
        self._by_id, self._files = by_id, files or {}
        self.fail_files = fail_files
        self.asked_for = None
        # The provider filters its clients on these, as build_providers only
        # constructs clients for instances that have both.
        self.name, self.url, self.api_key = name, "http://arr", "k"

    async def by_external_id(self):
        return self._by_id

    async def quality_profiles(self):
        return PROFILES

    async def tags(self):
        return TAGS

    async def root_folders(self):
        return ["/media/Movies"]

    async def custom_format_names(self):
        return ["DV Boost", "HDR", "UHD Bluray Tier 01"]

    async def moviefiles(self, ids):
        self.asked_for = list(ids)
        if self.fail_files:
            raise RuntimeError("moviefile endpoint down")
        return self._files


def ctx():
    return EnrichContext(settings=Settings(), cancel=asyncio.Event(),
                         semaphore=asyncio.Semaphore(1))


def settings_with(**kw):
    return Settings()


def movie_item(tmdb_id=335984, plex_path=None, file_size=None):
    return ItemRow(id=1, rating_key="1", library_key="1", item_type="movie",
                   title="Blade Runner 2049", tmdb_id=tmdb_id,
                   plex_path=plex_path, file_size=file_size)


def show_item(tvdb_id=76107, title="Cowboy Bebop"):
    return ItemRow(id=2, rating_key="2", library_key="2", item_type="show",
                   title=title, tvdb_id=tvdb_id)


async def run(provider, items):
    return [r async for r in provider.enrich(items, ctx())]


async def main():
    print("1. Radarr: custom formats come from the moviefile sweep")
    client = FakeArr({335984: MOVIE}, {900: MOVIEFILE})
    p = RadarrProvider(settings_with(), clients=[client])
    f = (await run(p, [movie_item()]))[0].facts

    check("asked for the right movieFileId", client.asked_for, [900])
    check("custom formats populated",
          f["radarr.custom_formats"], ["DV Boost", "HDR", "UHD Bluray Tier 01"])
    check("custom format score", f["radarr.custom_format_score"], 1705)
    # The embedded copy says False; only the moviefile record says True.
    check("cutoff flag taken from the file record", f["radarr.cutoff_unmet"], True)
    check("proper/repack from revision", f["radarr.proper_repack"], True)
    check("edition lowercased", f["radarr.edition"], "unrated")
    check("quality profile resolved by id", f["radarr.quality_profile"], "Ultra-HD")
    check("remux from modifier", f["radarr.remux"], True)
    check("source", f["radarr.source"], "bluray")
    check("resolution", f["radarr.resolution"], 2160)
    check("release group", f["radarr.release_group"], "FraMeSToR")
    check("tags resolved by id", f["radarr.tags"], ["4k", "keep"])
    check("added parsed to epoch", f["radarr.added"], 1685492687)  # 2023-05-31T00:24:47Z
    check("managed", f["radarr.managed"], True)

    print("\n2. Radarr: a dead moviefile endpoint degrades, it doesn't fail")
    p = RadarrProvider(settings_with(),
                       clients=[FakeArr({335984: MOVIE}, fail_files=True)])
    r = (await run(p, [movie_item()]))[0]
    check("still ok", r.status, "ok")
    check("profile survives", r.facts["radarr.quality_profile"], "Ultra-HD")
    # Falls back to the embedded copy, which is empty but not wrong.
    check("formats empty rather than absent", r.facts["radarr.custom_formats"], [])
    check("score defaults to 0 not null", r.facts["radarr.custom_format_score"], 0)

    print("\n3. Radarr: untracked and unmatchable movies")
    p = RadarrProvider(settings_with(), clients=[FakeArr({}, {})])
    r = (await run(p, [movie_item()]))[0]
    check("absent from Radarr is ok, not an error", r.status, "ok")
    check("managed is false", r.facts, {"radarr.managed": False})

    r = (await run(p, [movie_item(tmdb_id=None)]))[0]
    check("no tmdb id is skipped", r.status, "skipped")
    check("skip carries a reason", bool(r.reason), True)

    print("\n4. Radarr: vocabulary for the rule builder")
    p = RadarrProvider(settings_with(), clients=[FakeArr({}, {})])
    opts = await p.options()
    check("profiles offered", opts["radarr.quality_profile"], ["1080p/4k", "Ultra-HD"])
    check("custom formats offered",
          opts["radarr.custom_formats"], ["DV Boost", "HDR", "UHD Bluray Tier 01"])
    check("unconfigured provider offers nothing",
          await RadarrProvider(Settings(), clients=[]).options(), {})

    print("\n5. Sonarr: series facts")
    p = SonarrProvider(settings_with(), clients=[FakeArr({76107: SERIES})])
    f = (await run(p, [show_item()]))[0].facts
    check("anime detected", f["sonarr.series_type"], "anime")
    check("ended", f["sonarr.ended"], True)
    check("percent of episodes", f["sonarr.percent_of_episodes"], 92.3)
    check("incomplete", f["sonarr.incomplete"], True)
    check("missing count", f["sonarr.missing_episodes"], 2)
    check("network", f["sonarr.network"], "TV Tokyo")
    check("profile resolved", f["sonarr.quality_profile"], "1080p/4k")
    check("release groups sorted", f["sonarr.release_groups"], ["CtrlHD", "NTb"])
    check("genres", f["sonarr.genres"], ["Animation", "Action"])
    check("rating", f["sonarr.rating"], 8.6)
    check("original language", f["sonarr.original_language"], "Japanese")
    check("tags resolved", f["sonarr.tags"], ["dual"])
    check("first aired parsed", f["sonarr.first_aired"], 891561600)

    print("\n6. Sonarr: a show with nothing aired is not 'incomplete'")
    p = SonarrProvider(settings_with(), clients=[FakeArr({99999: UNAIRED})])
    f = (await run(p, [show_item(99999, "Announced Show")]))[0].facts
    check("zero expected episodes is not a gap", f["sonarr.incomplete"], False)
    check("missing count is 0", f["sonarr.missing_episodes"], 0)

    print("\n7. Sonarr: unmatchable series")
    p = SonarrProvider(settings_with(), clients=[FakeArr({})])
    check("absent from Sonarr", (await run(p, [show_item()]))[0].facts,
          {"sonarr.managed": False})
    check("no tvdb id is skipped",
          (await run(p, [show_item(tvdb_id=None)]))[0].status, "skipped")

    print("\n8. Scope declarations")
    check("Radarr is movie-only", RadarrProvider(Settings()).default_applies_to, ("movie",))
    check("Sonarr is show-only", SonarrProvider(Settings()).default_applies_to, ("show",))

    print("\n9. Radarr: two instances — the file match picks the owner")
    # The Oppenheimer case: main has a 2160p encode, the remux instance has the
    # actual remux, and the Plex item's file is the remux. The tmdb join alone
    # would hand the item main's facts (remux: False, MainFrame).
    remux_size = 60_000_000_000
    main_movie = {
        "id": 7, "tmdbId": 872585, "movieFileId": 100, "qualityProfileId": 4,
        "monitored": True, "status": "released", "tags": [],
        "rootFolderPath": "/media/Movies", "sizeOnDisk": 41_000_000_000,
    }
    main_file = {
        "id": 100, "path": "/data/Movies/Oppenheimer (2023)/Oppenheimer.2023.2160p.x265-MainFrame.mkv",
        "size": 41_000_000_000, "releaseGroup": "MainFrame", "edition": "",
        "customFormats": [{"name": "HDR"}], "customFormatScore": 1805,
        "qualityCutoffNotMet": True,
        "quality": {"quality": {"name": "Bluray-2160p", "source": "bluray",
                                "resolution": 2160, "modifier": "none"},
                    "revision": {"version": 1, "isRepack": False}},
    }
    remux_movie = {
        "id": 3, "tmdbId": 872585, "movieFileId": 200, "qualityProfileId": 7,
        "monitored": True, "status": "released", "tags": [],
        "rootFolderPath": "/media/Movies-REMUX", "sizeOnDisk": remux_size,
    }
    remux_file = {
        "id": 200, "path": "/data/Movies-REMUX/Oppenheimer (2023)/Oppenheimer.2023.REMUX-FraMeSToR.mkv",
        "size": remux_size, "releaseGroup": "FraMeSToR", "edition": "",
        "customFormats": [{"name": "HDR"}, {"name": "DV Boost"}],
        "customFormatScore": 1500, "qualityCutoffNotMet": False,
        "quality": {"quality": {"name": "Remux-2160p", "source": "bluray",
                                "resolution": 2160, "modifier": "remux"},
                    "revision": {"version": 1, "isRepack": False}},
    }
    p = RadarrProvider(settings_with(), clients=[
        FakeArr({872585: main_movie}, {100: main_file}, name="main"),
        FakeArr({872585: remux_movie}, {200: remux_file}, name="remuxes"),
    ])
    # Plex catalogued the remux under a different mount point — basename+size
    # is what has to carry the match.
    item = movie_item(872585,
                      plex_path="/media/paul/PLEXPOOL/Videos/Movies-REMUX/"
                                "Oppenheimer (2023)/Oppenheimer.2023.REMUX-FraMeSToR.mkv",
                      file_size=remux_size)
    f = (await run(p, [item]))[0].facts
    check("primary is the file's owner", f["radarr.instance"], "remuxes")
    check("both instances recorded", f["radarr.instances"], ["main", "remuxes"])
    check("remux true (any copy)", f["radarr.remux"], True)
    check("cutoff unmet true (any copy)", f["radarr.cutoff_unmet"], True)
    check("release group from the primary", f["radarr.release_group"], "FraMeSToR")
    check("root folder from the primary", f["radarr.root_folder"], "/media/Movies-REMUX")
    check("profile from the primary", f["radarr.quality_profile"], "Ultra-HD")
    check("score is the best copy's", f["radarr.custom_format_score"], 1805)
    check("formats unioned", f["radarr.custom_formats"], ["DV Boost", "HDR"])

    print("\n10. Radarr: no file match falls back to instance order")
    f = (await run(p, [movie_item(872585)]))[0].facts
    check("primary is the first instance", f["radarr.instance"], "main")
    check("merged remux still true", f["radarr.remux"], True)

    print("\n11. Sonarr: two instances union list facts, first one wins the rest")
    second = dict(SERIES, qualityProfileId=7,
                  statistics=dict(SERIES["statistics"], releaseGroups=["FLUX"]))
    p = SonarrProvider(settings_with(), clients=[
        FakeArr({76107: SERIES}, name="main"),
        FakeArr({76107: second}, name="4k"),
    ])
    f = (await run(p, [show_item()]))[0].facts
    check("primary is the first instance", f["sonarr.instance"], "main")
    check("both instances recorded", f["sonarr.instances"], ["main", "4k"])
    check("profile from the primary", f["sonarr.quality_profile"], "1080p/4k")
    check("release groups unioned", f["sonarr.release_groups"], ["CtrlHD", "FLUX", "NTb"])

    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())

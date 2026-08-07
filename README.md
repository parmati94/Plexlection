# Plexlection

Rule-driven Plex collections built from facts Plex doesn't have.

**Plex is the render target, not the query engine.** Plex Smart Collections can only
filter on metadata Plex indexes, which makes a whole class of interesting groupings
impossible: *movies cropped to ultrawide*, *Dolby Vision Profile 5 files that render
green on my Apple TV*, *films where my file is 10 minutes longer than TMDB's runtime*,
*started and never finished*.

Plexlection computes those facts — from the media files themselves via `ffprobe`, from
TMDB, from Tautulli watch history, and from library-relative aggregates — lets you
compose boolean rules over them in a GUI, and materialises the matches as Plex
collections.

```
Plex API ──┐
TMDB API ──┼─► providers ──► facts JSON in SQLite ──► rule compiler ──► SQL ──► matched set
Tautulli ──┤                        ▲                                              │
ffprobe  ──┘                        │                                              ▼
  (reads mounted media)      fact registry ──────► rule builder UI          label sync → Plex
```

## Why it's extensible

Everything routes through **pluggable fact providers**. A provider declares the fact
keys it emits; the rule builder UI is generated from that registry; rules compile to
SQL over a JSON facts column. Adding a capability means adding one provider file —
the UI and the rule engine need zero changes.

## Status

| Phase | State |
|---|---|
| 1 — Container scaffold, app skeleton, SQLite schema, SSE | ✅ Done |
| 2 — Plex client, path mapping, discovery scan, Library tab | ✅ Done |
| 3 — Fact registry, ffprobe provider, scan engine | ✅ Done |
| 4 — Rule engine + generated builder UI | ✅ Done |
| 5 — Label-driven Plex sync with dry-run | ✅ Done |
| 6 — TMDB, Tautulli, scheduling | ✅ Done |

**v1 is complete.** Deferred to v2: cropdetect (true aspect ratio behind baked-in
letterboxing, and variable-AR/IMAX detection), collection templates, TV support.

**58 facts** across 5 providers:

| Provider | Cost | Facts |
|---|---|---|
| `plex` | free | title, year, added/updated, runtime, TMDB match |
| `ffprobe` | cheap | aspect ratio + bucket, HDR/Dolby Vision profile, codec, bit depth, frame rate, interlacing, audio layout/languages/commentary, subtitle tracks + forced, duration, bitrate, container |
| `tmdb` | network | keywords, franchise, official runtime, budget/revenue/ROI, rating, original language, genres, countries, release date |
| `tautulli` | network | play count, last played, never played, distinct viewers, abandoned |
| `derived` | free | encode efficiency, size/minute, resolution class, scope flag, **runtime vs TMDB → extended-cut detection**, foreign, box-office bomb |

The `tmdb` and `tautulli` providers were added as two files and one line in
`build_providers`. That produced 24 new facts and 24 new rule-builder filters with
**zero frontend changes** — which is the whole architectural claim, made concrete.

Measured on a real library: **~42ms per file** at concurrency 6, so a full
2,084-movie ffprobe pass takes roughly **90 seconds**. Re-scans are effectively
instant — nothing is recomputed unless its fingerprint changed.

Rules compile to SQL, so library-relative predicates are cheap: *"scope films in
the top quartile of bitrate"* is one query with a CTE, and narrows a real 60-film
sample from 26 matches to 5.

### Tests

```bash
docker exec plexlection-dev python3 /app/scripts/test_discovery.py   # 21 checks, stubbed Plex
docker exec plexlection-dev python3 /app/scripts/test_scan.py 40     # 14 checks, real files
docker exec plexlection-dev python3 /app/scripts/test_rules.py       # 28 checks, compiler
docker exec plexlection-dev python3 /app/scripts/test_sync.py        # 30 checks, fake Plex
docker exec plexlection-dev python3 /app/scripts/test_providers.py   # 23 checks, TMDB/Tautulli/derived
```

**Not covered:** anything that talks to a real Plex server. The sync engine is
tested against a fake that records every label write, and the Plex client's own
calls (`batchMultiEdits`, `createCollection(smart=True, ...)`) are the most
version-sensitive surface in the project. Expect to shake those out on first
contact with a live server — the code reads the collection back and falls back to
static membership when the smart filter doesn't take.

## Quick start

```bash
cp docker-compose.yml docker-compose.override.yml   # edit your media path
docker compose up -d
```

Open <http://localhost:5182>, then set your Plex URL and token in **Settings**.

## Development

**The backend hot-reloads. The frontend does not.** `./backend` is bind-mounted and
supervisord runs `uvicorn --reload`, so editing a `.py` restarts the app in place.
nginx serves `frontend/dist/`, so any change to `js/` or `partials/` needs a Vite
build before it reaches the browser.

Three ways to work, pick one:

Copy `docker-compose.dev.yml` to `docker-compose.local.yml` and set your real
media path there — the local file is gitignored, so machine-specific paths never
reach the repo. Then use it in place of the dev file below.

```bash
# 1. Docker + manual rebuild — simplest
docker compose -f docker-compose.local.yml up --build    # http://localhost:5183
cd frontend && npm run build                            # after each frontend edit

# 2. Docker + Vite watcher — rebuilds dist/ automatically (still no HMR;
#    refresh the browser yourself)
cd frontend && npm run watch                            # second terminal

# 3. Docker backend + Vite dev server — true HMR
cd frontend && npm run dev                              # http://localhost:5184
```

Mode 3 proxies `/api` to `http://localhost:5183` (the dev container's published
port) — **not** to `:8000`, because uvicorn binds `127.0.0.1:8000` *inside* the
container and isn't reachable from the host. If you run uvicorn directly on the
host instead, set `PLEXLECTION_API=http://localhost:8000`.

Asset filenames are content-hashed, so **hard-refresh** after a rebuild — a cached
`index.html` points at assets that no longer exist.

## Configuration

Two layers, deliberately:

- **Env vars** (in `docker-compose.yml`) cover container concerns only: `LOG_LEVEL`,
  `TZ`, and the optional login (`ENABLE_LOGIN`, `USERNAME`, `PASSWORD`, `SESSION_SECRET`).
- **Everything operational** — Plex URL/token, TMDB key, Tautulli key, path mappings,
  scan tuning — lives in the database and is edited in the Settings tab. The
  commented-out env vars under *first-run seeding* populate it once on first start
  and are ignored thereafter.

### Media access

`ffprobe` runs **inside the container**, so ffmpeg only needs to exist in the image —
your host's copy is never executed. The image carries a static ffmpeg 7.x, which
reports Dolby Vision profiles that older distro builds get wrong.

Mount your media read-only, **at the same path inside the container**:

```yaml
volumes:
  - /srv/media/Videos:/srv/media/Videos:ro
```

Matching the paths means everything Plex reports resolves as-is and no mapping is
needed — for that library or any you add later. Only when the two genuinely can't
match (Plex in its own container with a different path scheme) do you need
**Settings → Paths**, where the UI reports how many items are unmapped and
pre-fills the prefix for you.

## Design notes

- **Facts** live in a JSON column, one row per item, written with `json_patch` so two
  providers can update the same row atomically and a removed key can be patched to
  `null`.
- **Provenance** is per `(item, provider)`. Staleness compares the provider's schema
  version and an input fingerprint — for file providers, `sha1(path|size|mtime)`, never
  the inode, because mergerfs doesn't keep inodes stable across a rebalance.
- **Rules compile to SQL** rather than filtering in Python. That's what makes
  library-relative predicates — "top 10% by runtime", "above the median bitrate" —
  three lines of emitter code instead of a query engine.
- **Every negative operator carries a knownness guard.** In a sparse fact store,
  `not_contains` would otherwise match every item that was never scanned.
- **Syncing is dry-run by default** and only ever removes labels it recorded applying.

## Relationship to Kometa

[Kometa](https://kometa.wiki/) (formerly Plex-Meta-Manager) covers the adjacent space
and covers it well, but it's YAML+CLI and can only filter on what Plex and TMDB already
expose. Plexlection differs in three ways: a GUI-first rule builder, file-derived facts
nothing else computes, and library-relative predicates. Kometa YAML export is planned
so the two interoperate rather than compete.

Note that Plex's own *"Use collection info from The Movie Database"* setting already
auto-creates official franchise collections. Plexlection doesn't reinvent that — its
value there is franchise **intersected with** other facts ("Star Wars films I've never
watched"), and the fuzzy franchises TMDB doesn't model (its Star Wars collection
excludes *Rogue One* and *Solo*).

## License

Personal project.

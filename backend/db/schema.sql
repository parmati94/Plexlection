-- Plexlection v1 schema.
--
-- Design notes that matter:
--   * One row per media item; all computed facts live in the `facts` JSON column.
--     Writes go through json_patch (RFC 7386 merge-patch) so two providers can
--     update one row atomically without a read-modify-write race, and so a key
--     can be deleted by patching it to null.
--   * Provenance is per (item, provider), not per fact. Staleness is decided by
--     comparing a provider's schema_version and input fingerprint.
--   * Items are soft-deleted, never dropped: a Plex outage or an unmounted
--     library must not destroy months of expensive scan results.

CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  applied_at INTEGER NOT NULL
);

-- ── Media items ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS items (
  id              INTEGER PRIMARY KEY,
  library_key     TEXT    NOT NULL,          -- Plex section key
  rating_key      TEXT    NOT NULL,          -- Plex ratingKey; rotates on delete+re-add
  guid            TEXT,                      -- plex://movie/5d77… stable across rotation
  tmdb_id         INTEGER,
  imdb_id         TEXT,
  item_type       TEXT    NOT NULL,          -- movie | show | season | episode
  title           TEXT    NOT NULL,
  sort_title      TEXT,
  year            INTEGER,
  plex_added_at   INTEGER,
  plex_updated_at INTEGER,                   -- Plex's own updatedAt: cheap change signal

  -- Primary media part. Drives invalidation of every file-derived fact.
  part_id         TEXT,
  plex_path       TEXT,                      -- path exactly as Plex reports it
  local_path      TEXT,                      -- after mapping; set only when it exists
  path_status     TEXT    NOT NULL DEFAULT 'unknown',  -- mapped|unmapped|missing|unknown
  file_size       INTEGER,
  file_mtime      INTEGER,
  file_fp         TEXT,                      -- sha1(local_path|size|mtime)

  facts           TEXT    NOT NULL DEFAULT '{}',

  first_seen      INTEGER NOT NULL,
  last_seen       INTEGER NOT NULL,
  deleted_at      INTEGER,                   -- NULL = live

  UNIQUE (library_key, rating_key)
);

CREATE INDEX IF NOT EXISTS idx_items_guid       ON items(library_key, guid);
CREATE INDEX IF NOT EXISTS idx_items_live       ON items(library_key, item_type) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_items_pathstatus ON items(path_status) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_items_sort       ON items(sort_title COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_items_tmdb       ON items(tmdb_id) WHERE tmdb_id IS NOT NULL;

-- ── Fact provenance ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS fact_provenance (
  item_id        INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  provider       TEXT    NOT NULL,
  schema_version INTEGER NOT NULL,   -- bump in a provider => every row instantly stale
  input_fp       TEXT,               -- provider.fingerprint(item); NULL = uncacheable
  computed_at    INTEGER NOT NULL,
  status         TEXT    NOT NULL,   -- ok | skipped | error
  reason         TEXT,
  duration_ms    INTEGER,
  PRIMARY KEY (item_id, provider)
);

CREATE INDEX IF NOT EXISTS idx_prov_provider ON fact_provenance(provider, status);

-- ── Rules ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS collection_templates (
  id              INTEGER PRIMARY KEY,
  name            TEXT NOT NULL,
  group_by_key    TEXT NOT NULL,
  min_count       INTEGER NOT NULL DEFAULT 3,
  max_collections INTEGER NOT NULL DEFAULT 50,
  title_template  TEXT NOT NULL,
  slug_template   TEXT NOT NULL,
  base_rule_json  TEXT,
  library_keys    TEXT NOT NULL,
  enabled         INTEGER NOT NULL DEFAULT 1,
  created_at      INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
  id                    INTEGER PRIMARY KEY,
  slug                  TEXT NOT NULL UNIQUE,   -- label suffix: plexlection:<slug>
  name                  TEXT NOT NULL,
  description           TEXT,
  rule_json             TEXT NOT NULL,
  library_keys          TEXT NOT NULL,          -- JSON array
  item_types            TEXT NOT NULL DEFAULT '["movie"]',
  order_by_key          TEXT,
  order_dir             TEXT NOT NULL DEFAULT 'desc',
  limit_n               INTEGER,
  enabled               INTEGER NOT NULL DEFAULT 1,
  sync_mode             TEXT NOT NULL DEFAULT 'label',  -- label | static | none
  collection_title      TEXT,
  collection_sort_title TEXT,
  collection_summary    TEXT,
  collection_sort       TEXT NOT NULL DEFAULT 'release',
  poster_ref            TEXT,
  poster_fp             TEXT,                   -- skip re-upload when unchanged
  template_id           INTEGER REFERENCES collection_templates(id) ON DELETE CASCADE,
  template_value        TEXT,
  last_sync_at          INTEGER,
  last_match_count      INTEGER,
  created_at            INTEGER NOT NULL,
  updated_at            INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rules_enabled  ON rules(enabled);
CREATE INDEX IF NOT EXISTS idx_rules_template ON rules(template_id);

-- ── Sync bookkeeping ────────────────────────────────────────────────────────
-- What WE applied. The difference between this and Plex's live label set is
-- exactly the drift a human introduced, which we report but never silently fix.
CREATE TABLE IF NOT EXISTS sync_membership (
  rule_id    INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
  rating_key TEXT    NOT NULL,
  added_at   INTEGER NOT NULL,
  PRIMARY KEY (rule_id, rating_key)
);

CREATE TABLE IF NOT EXISTS sync_history (
  id            INTEGER PRIMARY KEY,
  rule_id       INTEGER REFERENCES rules(id) ON DELETE SET NULL,
  rule_name     TEXT    NOT NULL,      -- denormalised: survives rule deletion
  started_at    INTEGER NOT NULL,
  finished_at   INTEGER,
  trigger       TEXT    NOT NULL,      -- manual | schedule
  dry_run       INTEGER NOT NULL,
  matched_count INTEGER,
  added_count   INTEGER,
  removed_count INTEGER,
  kept_count    INTEGER,
  pinned_count  INTEGER,
  vetoed_count  INTEGER,
  drifted_count INTEGER,
  status        TEXT    NOT NULL,      -- ok | guarded | error
  error         TEXT,
  detail_json   TEXT
);

CREATE INDEX IF NOT EXISTS idx_synchist_rule ON sync_history(rule_id, started_at DESC);

-- ── Scan bookkeeping ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scan_runs (
  id          INTEGER PRIMARY KEY,
  kind        TEXT    NOT NULL,        -- discover | incremental | deep | scoped
  trigger     TEXT    NOT NULL,        -- manual | schedule
  providers   TEXT    NOT NULL,        -- JSON array of provider ids
  started_at  INTEGER NOT NULL,
  finished_at INTEGER,
  status      TEXT    NOT NULL,        -- running | done | cancelled | error
  total       INTEGER NOT NULL DEFAULT 0,
  done        INTEGER NOT NULL DEFAULT 0,
  failed      INTEGER NOT NULL DEFAULT 0,
  skipped     INTEGER NOT NULL DEFAULT 0,
  message     TEXT
);

CREATE INDEX IF NOT EXISTS idx_scanruns_status ON scan_runs(status, started_at DESC);

-- The persisted work list. A crash or cancel resumes by re-reading state='pending',
-- which is why a half-finished deep scan costs nothing to restart.
CREATE TABLE IF NOT EXISTS scan_tasks (
  run_id   INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
  item_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  provider TEXT    NOT NULL,
  state    TEXT    NOT NULL DEFAULT 'pending',  -- pending|running|done|failed|skipped
  attempts INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, item_id, provider)
);

CREATE INDEX IF NOT EXISTS idx_tasks_pending ON scan_tasks(run_id, provider, state);

-- ── External API response cache ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS provider_cache (
  provider   TEXT    NOT NULL,
  cache_key  TEXT    NOT NULL,
  payload    TEXT    NOT NULL,
  fetched_at INTEGER NOT NULL,
  ttl_s      INTEGER NOT NULL,
  PRIMARY KEY (provider, cache_key)
);

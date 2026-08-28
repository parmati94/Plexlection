#!/usr/bin/env python3
"""Sync engine tests against a fake Plex.

The properties worth proving here are the safety ones — that a hand-applied
label survives, that :pin and :veto are honoured and never written by us, and
that the guards refuse the shapes that usually mean something upstream broke
rather than that the user meant it.

    docker exec plexlection-dev python3 /app/scripts/test_sync.py
"""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, "/app" if Path("/app/backend").exists() else
                str(Path(__file__).resolve().parent.parent))

from backend.common.errors import SyncGuardError  # noqa: E402
from backend.db.database import Database  # noqa: E402
from backend.db.indexes import reconcile  # noqa: E402
from backend.db.migrations import run_migrations  # noqa: E402
from backend.facts.registry import build_registry  # noqa: E402
from backend.models.settings import Settings  # noqa: E402
from backend.providers import build_providers  # noqa: E402
from backend.sync.engine import SyncEngine  # noqa: E402

PASS, FAIL = "\033[32m✓\033[0m", "\033[31m✗\033[0m"
failures = 0


def check(label, got, want=True):
    global failures
    ok = got == want
    if not ok:
        failures += 1
    print(f"  {PASS if ok else FAIL} {label:<52} got={got!r}")


class FakePlex:
    """Records label writes so the tests can assert on exactly what we touched."""

    def __init__(self):
        self.labels: dict[str, set[str]] = {}   # rating_key -> labels
        self.writes: list[tuple[str, str, bool]] = []
        self.collections: dict[str, dict] = {}
        self.fail_smart = False

    def _keys_with(self, label):
        return {k for k, labs in self.labels.items() if label in labs}

    async def rating_keys_with_label(self, section_key, label, libtype="movie"):
        return self._keys_with(label)

    async def apply_labels(self, section_key, rating_keys, label, add):
        for key in rating_keys:
            self.writes.append((key, label, add))
            bucket = self.labels.setdefault(key, set())
            bucket.add(label) if add else bucket.discard(label)
        return len(rating_keys), []

    async def ensure_smart_collection(self, section_key, title, labels,
                                      libtype="movie", rating_key=None):
        if self.fail_smart:
            raise RuntimeError("createCollection(smart=True) not supported")
        size = len(set().union(*(self._keys_with(x) for x in labels)) if labels else set())
        # Mirror the real client: a known rating key means rename-in-place.
        renamed = False
        if rating_key:
            for old_title, entry in list(self.collections.items()):
                if entry.get("rating_key") == rating_key and old_title != title:
                    self.collections[title] = self.collections.pop(old_title)
                    renamed = True
        self.collections.setdefault(title, {})
        self.collections[title].update(
            {"labels": labels, "size": size, "rating_key": rating_key or "9001"})
        return {"created": not renamed, "renamed": renamed, "smart": True,
                "rating_key": self.collections[title]["rating_key"], "size": size}

    async def update_collection(self, section_key, title, rating_key=None, **kw):
        return [k for k, v in kw.items() if v]

    async def delete_collection(self, section_key, title, rating_key=None):
        return self.collections.pop(title, None) is not None


async def make_db(tmp, n_scope=10, n_unscanned=0):
    db = Database(Path(tmp) / "sync.db")
    await db.start()
    await run_migrations(db)
    providers = build_providers(Settings())
    registry = build_registry(providers)
    await reconcile(db, registry)

    rows = []
    for i in range(n_scope):
        facts = {"video": {"dar": 2.4 if i < 6 else 1.78}}
        rows.append(("1", str(i), "movie", f"Film {i:02d}", f"Film {i:02d}", 2020, 1,
                     json.dumps(facts)))
    for i in range(n_unscanned):
        rows.append(("1", f"u{i}", "movie", f"Unscanned {i}", f"Unscanned {i}", 2020, 1, "{}"))

    await db.execute_many(
        "INSERT INTO items (library_key, rating_key, item_type, title, sort_title, year,"
        " seen_run, facts, first_seen, last_seen, path_status)"
        " VALUES (?,?,?,?,?,?,?,?,0,0,'mapped')",
        rows,
    )
    # Mark ffprobe as having run, so stale-fact guards don't fire spuriously.
    ids = await db.fetch_all("SELECT id FROM items WHERE facts != '{}'")
    now = int(time.time())
    await db.execute_many(
        "INSERT INTO fact_provenance (item_id, provider, schema_version, computed_at, status)"
        " VALUES (?, 'ffprobe', 1, ?, 'ok')",
        [(r["id"], now) for r in ids],
    )
    return db, registry


_RULES_DB = None


def make_rule(rule_id=1, **kw):
    base = {
        "id": rule_id, "slug": "scope", "name": "Scope",
        "rule": {"version": 1, "root": {"type": "cmp", "key": "video.dar",
                                        "op": "gte", "value": 2.3}},
        "library_keys": ["1"], "item_types": ["movie"],
        "order_by_key": None, "order_dir": "desc", "limit_n": None,
        "sync_mode": "label", "collection_title": "Scope",
        "collection_sort_title": None, "collection_summary": None,
        "collection_sort": "release", "poster_ref": None,
    }
    base.update(kw)
    return base


async def persist_rule(db, rule: dict) -> dict:
    """sync_history and sync_membership carry real foreign keys to rules, so the
    row has to exist — the same constraint that keeps history consistent in
    production."""
    now = int(time.time())
    await db.execute(
        "INSERT OR REPLACE INTO rules (id, slug, name, rule_json, library_keys,"
        " item_types, order_dir, enabled, sync_mode, collection_title,"
        " collection_sort, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,1,?,?,?,?,?)",
        (rule["id"], rule["slug"], rule["name"], json.dumps(rule["rule"]),
         json.dumps(rule["library_keys"]), json.dumps(rule["item_types"]),
         rule["order_dir"], rule["sync_mode"], rule["collection_title"],
         rule["collection_sort"], now, now),
    )
    return rule


async def main():
    tmp = tempfile.mkdtemp(prefix="pxl-sync-")
    db, registry = await make_db(tmp, n_scope=10)
    settings = Settings()
    settings.plex.libraries = ["1"]
    settings.safety.dry_run = False

    class Store:
        def get(self):
            return settings
    plex = FakePlex()
    engine = SyncEngine(db, registry, lambda: plex, Store(), None)
    rule = await persist_rule(db, make_rule())

    # ── 1. dry run writes nothing ──────────────────────────────────────
    print("\n1. Dry run")
    diff = await engine.sync_rule(rule, dry_run=True)
    check("matched the 6 scope films", diff.matched, 6)
    check("would add 6", len(diff.add), 6)
    check("wrote nothing to Plex", len(plex.writes), 0)
    check("recorded no membership",
          await db.fetch_val("SELECT COUNT(*) FROM sync_membership"), 0)
    check("history row written even for a dry run",
          await db.fetch_val("SELECT COUNT(*) FROM sync_history WHERE dry_run = 1"), 1)

    # ── 2. apply ───────────────────────────────────────────────────────
    print("\n2. Apply")
    diff = await engine.sync_rule(rule, dry_run=False)
    check("applied", diff.applied)
    check("6 labels written", len([w for w in plex.writes if w[2]]), 6)
    check("membership recorded",
          await db.fetch_val("SELECT COUNT(*) FROM sync_membership"), 6)
    check("smart collection created", "Scope" in plex.collections)
    check("collection filters on main + pin",
          plex.collections["Scope"]["labels"], ["plexlection:scope", "plexlection:scope:pin"])

    # ── 3. idempotent ──────────────────────────────────────────────────
    print("\n3. Re-sync with nothing changed")
    plex.writes.clear()
    diff = await engine.sync_rule(rule, dry_run=False)
    check("no writes", len(plex.writes), 0)
    check("all kept", diff.kept, 6)

    # ── 3b. rename follows the owned collection ────────────────────────
    print("\n3b. A title change renames the collection, not duplicates it")
    adopted = await db.fetch_val(
        "SELECT collection_rating_key FROM rules WHERE id = ?", (rule["id"],))
    check("collection rating key adopted on sync", adopted, "9001")
    renamed_rule = {**rule, "collection_title": "Scope Renamed",
                    "collection_rating_key": adopted}
    diff = await engine.sync_rule(renamed_rule, dry_run=False)
    check("new title present", "Scope Renamed" in plex.collections)
    check("old title gone — no stranded sibling", "Scope" not in plex.collections)
    check("same underlying collection",
          plex.collections["Scope Renamed"]["rating_key"], "9001")
    # Rename back so the remaining sections see the original title.
    await engine.sync_rule({**rule, "collection_rating_key": adopted}, dry_run=False)
    check("renamed back for the rest of the suite", "Scope" in plex.collections)

    # ── 4. a hand-applied label survives ───────────────────────────────
    print("\n4. Someone labels an item by hand")
    plex.labels.setdefault("9", set()).add("plexlection:scope")  # Film 09 is 1.78, not scope
    plex.writes.clear()
    diff = await engine.sync_rule(rule, dry_run=False)
    check("reported as drift", diff.drifted, 1)
    check("NOT removed — we never recorded adding it",
          any(w[0] == "9" and not w[2] for w in plex.writes), False)
    check("still labelled in Plex", "plexlection:scope" in plex.labels["9"], True)
    check("warning surfaced", any("carry this label" in w for w in diff.warnings), True)

    # ── 5. veto ────────────────────────────────────────────────────────
    print("\n5. User vetoes a match")
    plex.labels.setdefault("0", set()).add("plexlection:scope:veto")
    plex.writes.clear()
    diff = await engine.sync_rule(rule, dry_run=False)
    check("vetoed item counted", diff.vetoed, 1)
    check("our label removed from it",
          ("0", "plexlection:scope", False) in plex.writes, True)
    check("veto label untouched by us",
          any(w[1].endswith(":veto") for w in plex.writes), False)
    check("membership shrank to 5",
          await db.fetch_val("SELECT COUNT(*) FROM sync_membership"), 5)

    # ── 6. pin ─────────────────────────────────────────────────────────
    print("\n6. User pins a non-match")
    plex.labels.setdefault("8", set()).add("plexlection:scope:pin")  # 1.78, not a match
    plex.writes.clear()
    diff = await engine.sync_rule(rule, dry_run=False)
    check("pin counted", diff.pinned, 1)
    check("pin label never written by us",
          any(w[1].endswith(":pin") for w in plex.writes), False)
    check("collection filter still includes pins",
          "plexlection:scope:pin" in plex.collections["Scope"]["labels"], True)

    # ── 7. guards ──────────────────────────────────────────────────────
    print("\n7. Safety guards")

    async def guarded(label, r, **kw):
        try:
            await engine.sync_rule(r, dry_run=False, **kw)
            check(label, "allowed", "refused")
        except SyncGuardError as exc:
            print(f"  {PASS} {label:<52} {str(exc)[:46]}")

    empty = await persist_rule(db, make_rule(
        rule_id=2, slug="empty", name="Empty", collection_title="Empty",
        rule={"version": 1, "root": {"type": "cmp", "key": "video.dar",
                                     "op": "gte", "value": 99}}))
    await guarded("refuses an empty match set", empty)

    # A provider regression: the rule now matches almost nothing, so a sync would
    # strip most of the collection.
    shrunk = await persist_rule(db, make_rule(
        rule_id=1, slug="scope", name="Scope",
        rule={"version": 1, "root": {"type": "cmp", "key": "video.dar",
                                     "op": "gte", "value": 2.7}}))
    settings.safety.min_removal_alarm = 1
    await guarded("refuses a mass removal", shrunk)

    print("  … and with force=True")
    diff = await engine.sync_rule(shrunk, dry_run=False, force=True)
    check("force overrides the guard", diff.applied, True)
    settings.safety.min_removal_alarm = 20

    # ── 8. stale facts ─────────────────────────────────────────────────
    print("\n8. Stale-fact guard")
    db2, registry2 = await make_db(tempfile.mkdtemp(), n_scope=10, n_unscanned=10)
    plex2 = FakePlex()
    engine2 = SyncEngine(db2, registry2, lambda: plex2, Store(), None)
    await guarded_engine(engine2, await persist_rule(db2, make_rule()))

    # ── 9. teardown ────────────────────────────────────────────────────
    print("\n9. Unsync")
    # Step 7's forced sync of the dar>=2.7 variant emptied membership, so
    # re-apply the real rule first — otherwise there's nothing to tear down.
    rule = await persist_rule(db, make_rule())
    await engine.sync_rule(rule, dry_run=False, force=True)
    check("membership repopulated before teardown",
          await db.fetch_val("SELECT COUNT(*) FROM sync_membership WHERE rule_id = 1") > 0, True)

    plex.writes.clear()
    result = await engine.unsync_rule(rule)
    check("labels removed", result["removed"] > 0, True)
    check("collection deleted", result["collection_deleted"], True)
    check("membership cleared",
          await db.fetch_val("SELECT COUNT(*) FROM sync_membership WHERE rule_id = 1"), 0)
    check("user's pin left behind", "plexlection:scope:pin" in plex.labels["8"], True)

    # ── 10. smart-collection failure falls back gracefully ─────────────
    print("\n10. Smart collection unsupported")
    plex.fail_smart = True
    fb = await persist_rule(db, make_rule(rule_id=3, slug="fb", name="Fallback",
                                          collection_title="Fallback"))
    diff = await engine.sync_rule(fb, dry_run=False, force=True)
    check("labels still written", diff.applied, True)
    check("failure surfaced as a warning",
          any("smart collection" in w for w in diff.warnings), True)

    # A collection lives in exactly one Plex library, and a `show` collection
    # created inside a movie library matches nothing — silently, forever. The
    # old code picked library_keys[0] and the libtype independently, so nothing
    # stopped that pairing. It has to be refused, not merely survived.
    print("\n11. Library / item-type mismatch")
    plex.fail_smart = False
    tv = await persist_rule(db, make_rule(rule_id=4, slug="tv", name="TV",
                                          item_types=["show"],
                                          collection_title="TV"))
    try:
        await engine.sync_rule(tv, dry_run=True)
        check("refuses a show rule pointed at a movie library", "allowed", "refused")
    except SyncGuardError as exc:
        check("refuses a show rule pointed at a movie library",
              "hold shows" in str(exc), True)

    # Teardown must never be the thing that refuses — it's the escape hatch for
    # a rule that's already in a bad state.
    result = await engine.unsync_rule(tv)
    check("unsync still runs on a mismatched rule", result["collection_deleted"] is not None, True)

    # Same rule, pointed at a library that does hold shows: allowed through.
    await db.execute(
        "INSERT INTO items (library_key, rating_key, item_type, title, sort_title,"
        " year, seen_run, facts, first_seen, last_seen, path_status)"
        " VALUES ('2','s1','show','Show','Show',2020,1,'{}',0,0,'mapped')"
    )
    tv2 = await persist_rule(db, make_rule(rule_id=5, slug="tv2", name="TV2",
                                           library_keys=["1", "2"],
                                           item_types=["show"],
                                           collection_title="TV2"))
    diff = await engine.sync_rule(tv2, dry_run=True, force=True)
    check("picks the library that holds shows", diff.matched, 0)

    await db.stop()
    await db2.stop()
    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)


async def guarded_engine(engine, rule):
    try:
        await engine.sync_rule(rule, dry_run=False)
        check("refuses when facts are missing", "allowed", "refused")
    except SyncGuardError as exc:
        print(f"  {PASS} {'refuses when facts are missing':<52} {str(exc)[:46]}")


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""Rule compiler tests.

Exercises the parts that fail silently rather than loudly: parameter ordering
around CTEs, the knownness guards that keep unscanned items out of negative
predicates, index usage, and rejection of anything not on the allowlist.

    docker exec plexlection-dev python3 /app/scripts/test_rules.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/app" if Path("/app/backend").exists() else
                str(Path(__file__).resolve().parent.parent))

from backend.common.errors import RuleError  # noqa: E402
from backend.db.database import Database  # noqa: E402
from backend.db.indexes import reconcile  # noqa: E402
from backend.db.migrations import run_migrations  # noqa: E402
from backend.facts.registry import build_registry  # noqa: E402
from backend.models.settings import Settings  # noqa: E402
from backend.providers import build_providers  # noqa: E402
from backend.rules.compiler import Scope, compile_rule  # noqa: E402
from backend.rules.validate import validate_tree  # noqa: E402

PASS, FAIL = "\033[32m✓\033[0m", "\033[31m✗\033[0m"
failures = 0


def check(label, got, want=True):
    global failures
    ok = got == want
    if not ok:
        failures += 1
    print(f"  {PASS if ok else FAIL} {label:<54} got={got!r}")


def cmp(key, op, value=None):
    return {"type": "cmp", "key": key, "op": op, "value": value}


# Items chosen so each guard has something to catch. audio.languages stands in
# for the general LIST case (tmdb.keywords arrives with the TMDB provider).
FIXTURES = [
    # title,          dar,  bitrate, runtime_s, hdr,      languages
    ("Scope Cheap",   2.40,   3000,   7000,  "sdr",     ["eng"]),
    ("Scope Rich",    2.39,  40000,   9000,  "dv_p5",   ["eng", "fra"]),
    ("Flat Rich",     1.78,  38000,   6000,  "hdr10",   ["deu"]),
    ("Flat Cheap",    1.85,   2000,   5000,  "sdr",     []),
    ("Wide Mid",      2.76,  12000,   8000,  "dv_p7",   ["jpn"]),
    ("Unscanned",     None,   None,   None,   None,     None),  # no facts at all
]


async def seed(db):
    rows = []
    for i, (title, dar, bitrate, runtime, hdr, languages) in enumerate(FIXTURES):
        facts = {}
        if dar is not None:
            facts["video"] = {"dar": dar, "hdr_format": hdr, "width": 1920, "height": 800}
            facts["file"] = {"bitrate_kbps": bitrate, "duration_s": runtime}
            facts["derived"] = {"is_scope": dar >= 2.3}
        if languages is not None:
            facts.setdefault("audio", {})["languages"] = languages
        rows.append(("1", str(i), "movie", title, title, 2020, 1, json.dumps(facts)))

    await db.execute_many(
        "INSERT INTO items (library_key, rating_key, item_type, title, sort_title,"
        " year, seen_run, facts, first_seen, last_seen, path_status)"
        " VALUES (?,?,?,?,?,?,?,?,0,0,'mapped')",
        rows,
    )


async def main():
    tmp = tempfile.mkdtemp(prefix="pxl-rules-")
    db = Database(Path(tmp) / "rules.db")
    await db.start()
    await run_migrations(db)

    providers = build_providers(Settings())
    registry = build_registry(providers)
    await reconcile(db, registry)
    await seed(db)

    scope = Scope(library_keys=["1"], item_types=["movie"])

    async def titles(tree_root):
        tree = validate_tree({"version": 1, "root": tree_root}, registry)
        sql, params = compile_rule(tree, registry, scope, order_by_key="title", order_dir="asc")
        rows = await db.fetch_all(sql, params)
        return [r["title"] for r in rows]

    # ── 1. the genesis query ───────────────────────────────────────────
    print("\n1. Basic predicates")
    check("dar >= 2.3", await titles(cmp("video.dar", "gte", 2.3)),
          ["Scope Cheap", "Scope Rich", "Wide Mid"])
    check("AND", await titles({"type": "and", "children": [
        cmp("video.dar", "gte", 2.3), cmp("file.bitrate_kbps", "gte", 10000)]}),
        ["Scope Rich", "Wide Mid"])
    check("OR", await titles({"type": "or", "children": [
        cmp("video.dar", "gte", 2.7), cmp("file.bitrate_kbps", "lte", 2500)]}),
        ["Flat Cheap", "Wide Mid"])
    check("empty AND matches all", len(await titles({"type": "and", "children": []})), 6)
    check("empty OR matches none", await titles({"type": "or", "children": []}), [])

    # ── 2. knownness guards ────────────────────────────────────────────
    print("\n2. Knownness guards (the sparse-store trap)")
    check("ne excludes unscanned", await titles(cmp("video.hdr_format", "ne", "sdr")),
          ["Flat Rich", "Scope Rich", "Wide Mid"])
    check("not_in excludes unscanned",
          await titles(cmp("video.hdr_format", "not_in", ["sdr", "hdr10"])),
          ["Scope Rich", "Wide Mid"])
    check("list not_contains excludes unscanned",
          await titles(cmp("audio.languages", "not_contains", "eng")),
          ["Flat Cheap", "Flat Rich", "Wide Mid"])
    check("is_false excludes unscanned",
          await titles(cmp("derived.is_scope", "is_false")), ["Flat Cheap", "Flat Rich"])
    check("is_null finds the unscanned one",
          await titles(cmp("video.dar", "is_null")), ["Unscanned"])
    # NOT must exclude unknowns too, for scalars AND lists. SQL NULL propagation
    # gives this for free on scalars; list predicates only get it because their
    # emitters use CASE (a bare `json_type=... AND EXISTS(...)` collapses to
    # false for an unscanned item, and NOT false would let it through).
    check("NOT over a scalar excludes unscanned",
          "Unscanned" in await titles(
              {"type": "not", "child": cmp("video.dar", "gte", 2.3)}), False)
    check("NOT over a list excludes unscanned",
          "Unscanned" in await titles(
              {"type": "not", "child": cmp("audio.languages", "contains", "eng")}), False)

    # ── 3. lists ───────────────────────────────────────────────────────
    print("\n3. List operators")
    check("contains", await titles(cmp("audio.languages", "contains", "eng")),
          ["Scope Cheap", "Scope Rich"])
    check("contains_any", await titles(cmp("audio.languages", "contains_any", ["fra", "jpn"])),
          ["Scope Rich", "Wide Mid"])
    check("contains_all", await titles(cmp("audio.languages", "contains_all", ["eng", "fra"])),
          ["Scope Rich"])
    check("is_empty (empty array, not missing)",
          await titles(cmp("audio.languages", "is_empty")), ["Flat Cheap"])
    check("length_gte 2", await titles(cmp("audio.languages", "length_gte", 2)), ["Scope Rich"])

    # ── 4. library-relative ────────────────────────────────────────────
    print("\n4. Library-relative predicates")
    # bitrates: 2000, 3000, 12000, 38000, 40000 -> median 12000
    check("above median bitrate", await titles(
        {"type": "agg_cmp", "key": "file.bitrate_kbps", "op": "gt", "agg": "median"}),
        ["Flat Rich", "Scope Rich"])
    check("top 50% by bitrate", await titles(
        {"type": "agg_cmp", "key": "file.bitrate_kbps", "op": "gte",
         "agg": "percentile", "agg_arg": 50}),
        ["Flat Rich", "Scope Rich", "Wide Mid"])
    # mean of 2000, 3000, 12000, 38000, 40000 = 19000
    check("below mean bitrate", await titles(
        {"type": "agg_cmp", "key": "file.bitrate_kbps", "op": "lt", "agg": "mean"}),
        ["Flat Cheap", "Scope Cheap", "Wide Mid"])

    print("\n5. Aggregate + predicate together (parameter ordering)")
    # If CTE params and WHERE params were mis-ordered this returns the wrong set
    # rather than erroring — the exact failure mode worth a dedicated test.
    check("scope AND above-median bitrate", await titles({"type": "and", "children": [
        cmp("video.dar", "gte", 2.3),
        {"type": "agg_cmp", "key": "file.bitrate_kbps", "op": "gt", "agg": "median"}]}),
        ["Scope Rich"])
    check("two aggregates in one rule", await titles({"type": "and", "children": [
        {"type": "agg_cmp", "key": "file.bitrate_kbps", "op": "gt", "agg": "median"},
        {"type": "agg_cmp", "key": "file.duration_s", "op": "gt", "agg": "median"}]}),
        ["Scope Rich"])

    # ── 6. rejection ───────────────────────────────────────────────────
    print("\n6. Invalid input is rejected before it reaches SQL")

    def rejects(label, tree_root):
        """Passes when validation raises RuleError, printing the message so the
        wording stays reviewable."""
        global failures
        try:
            validate_tree({"version": 1, "root": tree_root}, registry)
        except RuleError as exc:
            print(f"  {PASS} {label:<54} {str(exc)[:60]}")
            return
        failures += 1
        print(f"  {FAIL} {label:<54} ACCEPTED (should have been rejected)")

    rejects("unknown fact key", cmp("video.nope", "eq", 1))
    rejects("operator wrong for type", cmp("video.dar", "contains", "x"))
    rejects("SQL injection in key", cmp("video.dar'); DROP TABLE items;--", "eq", 1))
    rejects("aggregate on a non-aggregatable fact",
            {"type": "agg_cmp", "key": "video.codec", "op": "gt", "agg": "median"})
    rejects("bad enum value", cmp("video.hdr_format", "eq", "not-a-format"))

    deep = cmp("video.dar", "gte", 2.3)
    for _ in range(12):
        deep = {"type": "not", "child": deep}
    rejects("excessive nesting", deep)

    check("items table survived injection attempt",
          await db.fetch_val("SELECT COUNT(*) FROM items"), 6)

    # ── 7. index usage ─────────────────────────────────────────────────
    print("\n7. Query plan")
    tree = validate_tree({"version": 1, "root": cmp("video.dar", "gte", 2.3)}, registry)
    sql, params = compile_rule(tree, registry, scope)
    plan = await db.fetch_all(f"EXPLAIN QUERY PLAN {sql}", params)
    detail = " ".join(str(r["detail"]) for r in plan)
    # Which index wins depends on selectivity — with a scope predicate present
    # SQLite often prefers idx_items_live (library_key, item_type) over the
    # expression index. Either is fine; a full table scan is not.
    check("uses an index rather than scanning", "USING INDEX" in detail or "USING COVERING" in detail)
    print(f"     plan: {detail[:100]}")

    # ── 8. ordering and limit ──────────────────────────────────────────
    print("\n8. Ordering and limit")
    tree = validate_tree({"version": 1, "root": {"type": "and", "children": []}}, registry)
    sql, params = compile_rule(tree, registry, scope,
                               order_by_key="file.bitrate_kbps", order_dir="desc", limit_n=3)
    rows = await db.fetch_all(sql, params)
    check("top 3 by bitrate", [r["title"] for r in rows],
          ["Scope Rich", "Flat Rich", "Wide Mid"])
    check("NULLs sort last, not first", "Unscanned" not in [r["title"] for r in rows])

    await db.stop()
    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())

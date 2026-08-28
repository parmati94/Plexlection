"""Rule CRUD, live preview, and match listing."""
import json
import re
import time

from fastapi import APIRouter, Depends, HTTPException

from backend import startup
from backend.common.auth import require_auth
from backend.common.errors import RuleError
from backend.common.logging_config import get_logger
from backend.models.rules import RuleCreate, RulePreview, RuleUpdate
from backend.rules.compiler import Scope, compile_rule
from backend.rules.validate import collect_keys, validate_order_by, validate_tree

logger = get_logger(__name__)
router = APIRouter(prefix="/api/rules", tags=["rules"])

EMPTY_TREE = {"version": 1, "root": {"type": "and", "children": []}}


def slugify(text: str) -> str:
    """Slug becomes the Plex label suffix, so keep it conservative."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:60] or "rule"


async def _unique_slug(base: str, exclude_id: int | None = None) -> str:
    slug = base
    n = 2
    while True:
        row = await startup.db.fetch_one(
            "SELECT id FROM rules WHERE slug = ?", (slug,)
        )
        if row is None or (exclude_id is not None and row["id"] == exclude_id):
            return slug
        slug = f"{base}-{n}"
        n += 1


def _row_to_rule(row) -> dict:
    rule = dict(row)
    for field_name in ("rule_json", "library_keys", "item_types"):
        raw = rule.pop(field_name, None)
        key = "rule" if field_name == "rule_json" else field_name
        try:
            rule[key] = json.loads(raw) if raw else ([] if field_name != "rule_json" else EMPTY_TREE)
        except json.JSONDecodeError:
            rule[key] = EMPTY_TREE if field_name == "rule_json" else []
    rule["enabled"] = bool(rule.get("enabled"))
    return rule


def _scope_of(library_keys: list[str], item_types: list[str]) -> Scope:
    # An empty library list means "every library the user selected for indexing"
    # rather than "no libraries" — a rule scoped to nothing is never what's meant.
    keys = library_keys or startup.settings_store.get().plex.libraries
    return Scope(library_keys=list(keys), item_types=list(item_types or ["movie"]))


async def _stale_count(keys: set[str], scope: Scope) -> int:
    """Items missing any fact this rule depends on.

    Surfaced in the preview because a rule quietly matching fewer items than it
    should — because half the library hasn't been scanned — looks identical to a
    rule that's simply wrong.
    """
    providers = {
        startup.registry.require(k).provider for k in keys if startup.registry.get(k)
    }
    if not providers:
        return 0
    scope_sql, scope_params = scope.sql()
    placeholders = ",".join("?" * len(providers))
    return await startup.db.fetch_val(
        f"SELECT COUNT(*) FROM items i WHERE {scope_sql} AND NOT EXISTS ("
        f"  SELECT 1 FROM fact_provenance p WHERE p.item_id = i.id "
        f"  AND p.provider IN ({placeholders}) AND p.status = 'ok')",
        (*scope_params, *providers),
        default=0,
    )


# ── read ──────────────────────────────────────────────────────────────────
@router.get("", dependencies=[Depends(require_auth)])
async def list_rules():
    rows = await startup.db.fetch_all("SELECT * FROM rules ORDER BY name COLLATE NOCASE")
    return {"rules": [_row_to_rule(r) for r in rows], "count": len(rows)}


@router.get("/{rule_id}", dependencies=[Depends(require_auth)])
async def get_rule(rule_id: int):
    row = await startup.db.fetch_one("SELECT * FROM rules WHERE id = ?", (rule_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"rule": _row_to_rule(row)}


# ── preview ───────────────────────────────────────────────────────────────
@router.post("/preview", dependencies=[Depends(require_auth)])
async def preview_rule(request: RulePreview):
    """Evaluate an unsaved tree.

    Called on every edit in the builder, so it must stay cheap — which it is,
    being one compiled query plus a small sample.
    """
    tree = validate_tree(request.rule, startup.registry)
    order_by = validate_order_by(request.order_by_key, startup.registry)
    scope = _scope_of(request.library_keys, request.item_types)

    count_sql, count_params = compile_rule(
        tree, startup.registry, scope, count_only=True
    )
    matched = await startup.db.fetch_val(count_sql, count_params, default=0)

    sample = []
    if request.sample_size:
        sql, params = compile_rule(
            tree, startup.registry, scope,
            order_by_key=order_by, order_dir=request.order_dir,
            limit_n=request.sample_size,
            select="id, rating_key, title, year, sort_title, facts",
        )
        rows = await startup.db.fetch_all(sql, params)
        for row in rows:
            item = dict(row)
            try:
                item["facts"] = json.loads(item.get("facts") or "{}")
            except json.JSONDecodeError:
                item["facts"] = {}
            sample.append(item)

    keys = collect_keys(tree["root"])
    scope_sql, scope_params = scope.sql()
    in_scope = await startup.db.fetch_val(
        f"SELECT COUNT(*) FROM items WHERE {scope_sql}", scope_params, default=0
    )

    # A limit caps what actually reaches the collection; report both so
    # "matched 200" next to "collection 25" isn't a surprise later.
    collection_size = min(matched, request.limit_n) if request.limit_n else matched

    return {
        "matched": matched,
        "collection_size": collection_size,
        "in_scope": in_scope,
        "sample": sample,
        "depends_on": sorted(keys),
        "stale": await _stale_count(keys, scope),
    }


@router.post("/explain", dependencies=[Depends(require_auth)])
async def explain_rule(request: RulePreview):
    """The generated SQL and its query plan.

    A debugging aid, and the fastest way to confirm an expression index is
    actually being used rather than silently full-scanned.
    """
    tree = validate_tree(request.rule, startup.registry)
    order_by = validate_order_by(request.order_by_key, startup.registry)
    scope = _scope_of(request.library_keys, request.item_types)
    sql, params = compile_rule(
        tree, startup.registry, scope,
        order_by_key=order_by, order_dir=request.order_dir, limit_n=request.limit_n,
    )
    plan = await startup.db.fetch_all(f"EXPLAIN QUERY PLAN {sql}", params)
    return {
        "sql": sql,
        "params": params,
        "plan": [dict(r) for r in plan],
    }


@router.get("/{rule_id}/matches", dependencies=[Depends(require_auth)])
async def rule_matches(rule_id: int, limit: int = 200):
    row = await startup.db.fetch_one("SELECT * FROM rules WHERE id = ?", (rule_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    rule = _row_to_rule(row)

    tree = validate_tree(rule["rule"], startup.registry)
    scope = _scope_of(rule["library_keys"], rule["item_types"])
    sql, params = compile_rule(
        tree, startup.registry, scope,
        order_by_key=validate_order_by(rule["order_by_key"], startup.registry),
        order_dir=rule["order_dir"],
        limit_n=rule["limit_n"] or limit,
        select="id, rating_key, title, year, sort_title",
    )
    rows = await startup.db.fetch_all(sql, params)
    return {"items": [dict(r) for r in rows], "count": len(rows)}


# ── write ─────────────────────────────────────────────────────────────────
@router.post("", dependencies=[Depends(require_auth)])
async def create_rule(request: RuleCreate):
    tree = validate_tree(request.rule, startup.registry)
    order_by = validate_order_by(request.order_by_key, startup.registry)
    slug = await _unique_slug(slugify(request.slug or request.name))
    now = int(time.time())

    rule_id = await startup.db.execute(
        "INSERT INTO rules (slug, name, description, rule_json, library_keys, item_types,"
        " order_by_key, order_dir, limit_n, enabled, sync_mode, collection_title,"
        " collection_sort_title, collection_summary, collection_sort, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (slug, request.name, request.description, json.dumps(tree),
         json.dumps(request.library_keys), json.dumps(request.item_types),
         order_by, request.order_dir, request.limit_n, int(request.enabled),
         # Stored as given, NOT defaulted to the name: an empty title follows
         # the rule name forever, a persisted one freezes against renames.
         request.sync_mode, request.collection_title or None,
         request.collection_sort_title, request.collection_summary,
         request.collection_sort, now, now),
    )
    logger.info("Created rule %d (%s)", rule_id, slug)
    return await get_rule(rule_id)


@router.put("/{rule_id}", dependencies=[Depends(require_auth)])
async def update_rule(rule_id: int, request: RuleUpdate):
    row = await startup.db.fetch_one("SELECT * FROM rules WHERE id = ?", (rule_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Rule not found")

    sets: list[str] = []
    params: list = []

    def put(column: str, value) -> None:
        sets.append(f"{column} = ?")
        params.append(value)

    if request.rule is not None:
        put("rule_json", json.dumps(validate_tree(request.rule, startup.registry)))
    if request.order_by_key is not None:
        put("order_by_key", validate_order_by(request.order_by_key, startup.registry))
    if request.name is not None:
        put("name", request.name)
    if request.description is not None:
        put("description", request.description)
    if request.library_keys is not None:
        put("library_keys", json.dumps(request.library_keys))
    if request.item_types is not None:
        put("item_types", json.dumps(request.item_types))
    if request.order_dir is not None:
        put("order_dir", request.order_dir)
    if request.limit_n is not None:
        put("limit_n", request.limit_n)
    if request.enabled is not None:
        put("enabled", int(request.enabled))
    if request.sync_mode is not None:
        put("sync_mode", request.sync_mode)
    if request.collection_title is not None:
        put("collection_title", request.collection_title or None)
    if request.collection_sort_title is not None:
        put("collection_sort_title", request.collection_sort_title)
    if request.collection_summary is not None:
        put("collection_summary", request.collection_summary)
    if request.collection_sort is not None:
        put("collection_sort", request.collection_sort)

    if not sets:
        return await get_rule(rule_id)

    put("updated_at", int(time.time()))
    params.append(rule_id)
    await startup.db.execute(f"UPDATE rules SET {', '.join(sets)} WHERE id = ?", params)
    return await get_rule(rule_id)


@router.delete("/{rule_id}", dependencies=[Depends(require_auth)])
async def delete_rule(rule_id: int):
    row = await startup.db.fetch_one("SELECT slug FROM rules WHERE id = ?", (rule_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    # Labels already written to Plex are cleaned up by the sync engine in Phase 5;
    # for now deleting a rule only removes it here.
    await startup.db.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    logger.info("Deleted rule %d (%s)", rule_id, row["slug"])
    return {"success": True, "deleted": rule_id}

"""Sync engine — rule matches to Plex.

## The three-label protocol

A Plex *smart* collection derives its membership from a filter, which means
there is no "add to collection" action for a human to perform. So curation has
to happen one level down, at the label layer:

    plexlection:<slug>        ours, written and removed by us
    plexlection:<slug>:pin    yours — we never write or remove it. Force-include.
    plexlection:<slug>:veto   yours. Force-exclude.

The smart collection filters on `label is any of [<slug>, <slug>:pin]`, and
matching subtracts anything carrying `:veto`. Manual curation is therefore both
possible *and* structurally impossible for us to clobber.

## What we're allowed to remove

Only rating keys recorded in `sync_membership` — the set we put there ourselves.
Anything else carrying our label was applied by hand or by an older version, and
is reported as *drift* rather than silently corrected.
"""
import json
import time
from dataclasses import dataclass, field

from backend.common.errors import SyncGuardError
from backend.common.logging_config import get_logger
from backend.rules.compiler import Scope, compile_rule
from backend.rules.validate import validate_order_by, validate_tree

logger = get_logger(__name__)

BATCH = 50


@dataclass
class SyncDiff:
    rule_id: int
    rule_name: str
    label: str
    matched: int = 0
    add: list[dict] = field(default_factory=list)
    remove: list[dict] = field(default_factory=list)
    kept: int = 0
    pinned: int = 0
    vetoed: int = 0
    drifted: int = 0
    stale: int = 0
    in_scope: int = 0
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = True
    applied: bool = False
    collection: dict | None = None

    @property
    def stale_fraction(self) -> float:
        return (self.stale / self.in_scope) if self.in_scope else 0.0

    def as_dict(self) -> dict:
        return {
            "rule_id": self.rule_id, "rule_name": self.rule_name, "label": self.label,
            "matched": self.matched, "add": self.add, "remove": self.remove,
            "add_count": len(self.add), "remove_count": len(self.remove),
            "kept": self.kept, "pinned": self.pinned, "vetoed": self.vetoed,
            "drifted": self.drifted, "stale": self.stale, "in_scope": self.in_scope,
            "warnings": self.warnings, "dry_run": self.dry_run,
            "applied": self.applied, "collection": self.collection,
        }


class SyncEngine:
    def __init__(self, db, registry, plex_factory, settings_store, broadcaster=None):
        self.db = db
        self.registry = registry
        self._plex = plex_factory
        self.settings_store = settings_store
        self.broadcaster = broadcaster

    # ── labels ────────────────────────────────────────────────────────────
    def labels_for(self, slug: str) -> tuple[str, str, str]:
        prefix = self.settings_store.get().plex.label_prefix or "plexlection"
        return f"{prefix}:{slug}", f"{prefix}:{slug}:pin", f"{prefix}:{slug}:veto"

    # ── main entry point ──────────────────────────────────────────────────
    async def sync_rule(
        self, rule: dict, *, dry_run: bool = True, force: bool = False,
        trigger: str = "manual",
    ) -> SyncDiff:
        settings = self.settings_store.get()
        plex = self._plex()
        label, pin_label, veto_label = self.labels_for(rule["slug"])

        section_keys = rule["library_keys"] or settings.plex.libraries
        if not section_keys:
            raise SyncGuardError("This rule isn't scoped to any Plex library.")
        section_key = section_keys[0]

        diff = SyncDiff(rule_id=rule["id"], rule_name=rule["name"], label=label,
                        dry_run=dry_run)

        # ── what should be in it ──────────────────────────────────────────
        tree = validate_tree(rule["rule"], self.registry)
        scope = Scope(library_keys=list(section_keys),
                      item_types=list(rule["item_types"] or ["movie"]))
        sql, params = compile_rule(
            tree, self.registry, scope,
            order_by_key=validate_order_by(rule.get("order_by_key"), self.registry),
            order_dir=rule.get("order_dir", "desc"),
            limit_n=rule.get("limit_n"),
            select="rating_key, title, year",
        )
        rows = await self.db.fetch_all(sql, params)
        matched = {r["rating_key"]: dict(r) for r in rows}
        diff.matched = len(matched)

        scope_sql, scope_params = scope.sql()
        diff.in_scope = await self.db.fetch_val(
            f"SELECT COUNT(*) FROM items WHERE {scope_sql}", scope_params, default=0
        )
        diff.stale = await self._stale_for(tree, scope)

        # ── what's in it now ──────────────────────────────────────────────
        current = await plex.rating_keys_with_label(section_key, label)
        pinned = await plex.rating_keys_with_label(section_key, pin_label)
        vetoed = await plex.rating_keys_with_label(section_key, veto_label)
        diff.pinned, diff.vetoed = len(pinned), len(vetoed)

        ours = await self._membership(rule["id"])
        diff.drifted = len(current - ours)
        if diff.drifted:
            diff.warnings.append(
                f"{diff.drifted} items carry this label but aren't in our records — "
                f"applied by hand, or by an earlier version. They won't be removed."
            )

        target = set(matched) - vetoed
        to_add = target - current
        # Only ever remove what we put there.
        to_remove = (current & ours) - target
        diff.kept = len(target & current)

        titles = await self._titles(to_add | to_remove)
        diff.add = [{"rating_key": k, **titles.get(k, {})} for k in sorted(to_add)]
        diff.remove = [{"rating_key": k, **titles.get(k, {})} for k in sorted(to_remove)]

        self._guard(diff, current, settings, force)

        if dry_run:
            await self._record(rule, diff, trigger)
            return diff

        # ── apply ─────────────────────────────────────────────────────────
        # Membership is recorded per batch, immediately after the labels land.
        # Recording it once at the end instead would mean a crash or cancel
        # midway leaves labels in Plex that our records don't know about — and
        # since unsync only removes what we recorded, those would be orphaned
        # and only removable by hand, across potentially thousands of items.
        errors: list[str] = []
        for start in range(0, len(diff.add), BATCH):
            chunk = [d["rating_key"] for d in diff.add[start:start + BATCH]]
            _, errs = await plex.apply_labels(section_key, chunk, label, add=True)
            errors += errs
            await self._add_membership(rule["id"], chunk)
        for start in range(0, len(diff.remove), BATCH):
            chunk = [d["rating_key"] for d in diff.remove[start:start + BATCH]]
            _, errs = await plex.apply_labels(section_key, chunk, label, add=False)
            errors += errs
            await self._drop_membership(rule["id"], chunk)

        if errors:
            diff.warnings.append(f"{len(errors)} label writes failed: {errors[0]}")

        # Reconcile to the exact target, in case a batch partly failed.
        await self._replace_membership(rule["id"], target)

        if rule.get("sync_mode", "label") == "label":
            diff.collection = await self._ensure_collection(
                plex, section_key, rule, label, pin_label, target, pinned, diff
            )

        diff.applied = True
        await self.db.execute(
            "UPDATE rules SET last_sync_at = ?, last_match_count = ? WHERE id = ?",
            (int(time.time()), diff.matched, rule["id"]),
        )
        await self._record(rule, diff, trigger)

        logger.info(
            "🏷️  %s: +%d / -%d (kept %d, pinned %d, vetoed %d)",
            rule["name"], len(diff.add), len(diff.remove), diff.kept,
            diff.pinned, diff.vetoed,
        )
        return diff

    # ── collection upkeep ─────────────────────────────────────────────────
    async def _ensure_collection(self, plex, section_key, rule, label, pin_label,
                                 target, pinned, diff) -> dict:
        title = rule.get("collection_title") or rule["name"]
        try:
            info = await plex.ensure_smart_collection(section_key, title, [label, pin_label])
        except Exception as exc:
            diff.warnings.append(
                f"Couldn't create the smart collection ({exc}). The labels were "
                f"still written — you can build the collection in Plex by hand, "
                f"or switch this rule to static membership."
            )
            return {"error": str(exc)}

        # Read back and compare. createCollection(smart=True, filters=...) is
        # version-sensitive, so a silently-empty collection is a real outcome.
        expected = len(target | pinned)
        if info.get("size") is not None and expected and info["size"] == 0:
            diff.warnings.append(
                f"The smart collection reports 0 items but {expected} carry the "
                f"label. Plex may not have indexed the label yet, or the smart "
                f"filter didn't take."
            )

        changed = await plex.update_collection(
            section_key, title,
            sort_title=rule.get("collection_sort_title"),
            summary=rule.get("collection_summary"),
            poster_path=rule.get("poster_ref"),
            sort_mode=rule.get("collection_sort"),
        )
        info["updated_fields"] = changed
        return info

    # ── guards ────────────────────────────────────────────────────────────
    def _guard(self, diff: SyncDiff, current: set, settings, force: bool) -> None:
        if force:
            return
        safety = settings.safety

        if diff.matched == 0 and safety.refuse_empty_result:
            raise SyncGuardError(
                "This rule matches nothing. That's usually a typo or a missing "
                "scan rather than an intentionally empty collection.",
                diff=diff.as_dict(),
            )

        if diff.stale_fraction > safety.max_stale_fraction:
            raise SyncGuardError(
                f"{diff.stale} of {diff.in_scope} items are missing facts this rule "
                f"depends on. Syncing now would produce a short collection.",
                diff=diff.as_dict(),
            )

        removals = len(diff.remove)
        if (current and removals > safety.min_removal_alarm
                and removals / len(current) > safety.max_removal_fraction):
            raise SyncGuardError(
                f"This would remove {removals} of {len(current)} items. A drop that "
                f"large is more often a provider regression than an intended change.",
                diff=diff.as_dict(),
            )

        total_changes = len(diff.add) + removals
        if total_changes > safety.max_changes_per_sync:
            raise SyncGuardError(
                f"{total_changes} changes exceeds the per-sync cap of "
                f"{safety.max_changes_per_sync}.",
                diff=diff.as_dict(),
            )

    # ── bookkeeping ───────────────────────────────────────────────────────
    async def _membership(self, rule_id: int) -> set[str]:
        rows = await self.db.fetch_all(
            "SELECT rating_key FROM sync_membership WHERE rule_id = ?", (rule_id,)
        )
        return {r["rating_key"] for r in rows}

    async def _add_membership(self, rule_id: int, keys: list[str]) -> None:
        now = int(time.time())
        await self.db.execute_many(
            "INSERT OR IGNORE INTO sync_membership (rule_id, rating_key, added_at) "
            "VALUES (?,?,?)",
            [(rule_id, key, now) for key in keys],
        )

    async def _drop_membership(self, rule_id: int, keys: list[str]) -> None:
        await self.db.execute_many(
            "DELETE FROM sync_membership WHERE rule_id = ? AND rating_key = ?",
            [(rule_id, key) for key in keys],
        )

    async def _replace_membership(self, rule_id: int, keys: set[str]) -> None:
        now = int(time.time())
        await self.db.transaction(
            [("DELETE FROM sync_membership WHERE rule_id = ?", (rule_id,))]
            + [("INSERT INTO sync_membership (rule_id, rating_key, added_at) VALUES (?,?,?)",
                (rule_id, key, now)) for key in sorted(keys)]
        )

    async def _titles(self, keys: set[str]) -> dict[str, dict]:
        if not keys:
            return {}
        placeholders = ",".join("?" * len(keys))
        rows = await self.db.fetch_all(
            f"SELECT rating_key, title, year FROM items WHERE rating_key IN ({placeholders})",
            tuple(keys),
        )
        return {r["rating_key"]: {"title": r["title"], "year": r["year"]} for r in rows}

    async def _stale_for(self, tree: dict, scope: Scope) -> int:
        from backend.rules.validate import collect_keys
        keys = collect_keys(tree["root"])
        providers = {
            self.registry.require(k).provider for k in keys if self.registry.get(k)
        }
        if not providers:
            return 0
        scope_sql, scope_params = scope.sql()
        placeholders = ",".join("?" * len(providers))
        return await self.db.fetch_val(
            f"SELECT COUNT(*) FROM items i WHERE {scope_sql} AND NOT EXISTS ("
            f"  SELECT 1 FROM fact_provenance p WHERE p.item_id = i.id "
            f"  AND p.provider IN ({placeholders}) AND p.status = 'ok')",
            (*scope_params, *providers), default=0,
        )

    async def _record(self, rule: dict, diff: SyncDiff, trigger: str) -> None:
        now = int(time.time())
        await self.db.execute(
            "INSERT INTO sync_history (rule_id, rule_name, started_at, finished_at,"
            " trigger, dry_run, matched_count, added_count, removed_count, kept_count,"
            " pinned_count, vetoed_count, drifted_count, status, error, detail_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rule["id"], rule["name"], now, now, trigger, int(diff.dry_run),
             diff.matched, len(diff.add), len(diff.remove), diff.kept,
             diff.pinned, diff.vetoed, diff.drifted,
             "ok" if diff.applied or diff.dry_run else "error", None,
             json.dumps({"add": diff.add[:200], "remove": diff.remove[:200],
                         "warnings": diff.warnings})),
        )

    # ── teardown ──────────────────────────────────────────────────────────
    async def unsync_rule(self, rule: dict, strip_all: bool = False) -> dict:
        """Strip our label from everything and remove the collection.

        By default only touches rating keys in sync_membership, so a label a
        human applied by hand survives. `strip_all` widens it to every item in
        Plex carrying the label — the escape hatch for when our records and
        Plex have diverged and you just want the label gone.

        Neither mode ever touches :pin or :veto. Those are the user's.
        """
        plex = self._plex()
        label, _, _ = self.labels_for(rule["slug"])
        section_keys = rule["library_keys"] or self.settings_store.get().plex.libraries
        if not section_keys:
            return {"removed": 0, "collection_deleted": False}
        section_key = section_keys[0]

        if strip_all:
            ours = sorted(await plex.rating_keys_with_label(section_key, label))
        else:
            ours = sorted(await self._membership(rule["id"]))
        removed = 0
        for start in range(0, len(ours), BATCH):
            changed, _ = await plex.apply_labels(
                section_key, ours[start:start + BATCH], label, add=False
            )
            removed += changed

        deleted = False
        title = rule.get("collection_title") or rule["name"]
        try:
            deleted = await plex.delete_collection(section_key, title)
        except Exception as exc:
            logger.warning("Could not delete collection %r: %s", title, exc)

        await self.db.execute("DELETE FROM sync_membership WHERE rule_id = ?", (rule["id"],))

        leftover = len(await plex.rating_keys_with_label(section_key, label))
        return {
            "removed": removed,
            "collection_deleted": deleted,
            "strip_all": strip_all,
            # Non-zero after a normal unsync means hand-applied labels remain,
            # which is correct — strip_all is how you clear those deliberately.
            "still_labelled": leftover,
        }

"""The scan engine.

Runs providers in dependency order, cheapest tier first, over the items each one
considers stale. Persists results incrementally so a cancel or crash never
throws away completed work.

Staleness for an (item, provider) pair, any of:
  1. no provenance row            — never computed
  2. schema_version differs       — the provider's logic changed
  3. input_fp differs             — the inputs changed (usually the file)
  4. status='error'               — retried on the next explicit scan
  5. computed_at + max_age_s past — for time-varying sources
"""
import asyncio
import json
import time
from dataclasses import dataclass, field

from backend.common.logging_config import get_logger
from backend.facts.spec import CostTier
from backend.providers.base import (
    STATUS_ERROR,
    STATUS_OK,
    STATUS_SKIPPED,
    EnrichContext,
    FactResult,
    ItemRow,
)

logger = get_logger(__name__)

FLUSH_EVERY = 25
FLUSH_SECONDS = 2.0

COST_ORDER = {
    CostTier.FREE: 0,
    CostTier.CHEAP: 1,
    CostTier.NETWORK: 2,
    CostTier.EXPENSIVE: 3,
}

ITEM_COLUMNS = (
    "id, rating_key, library_key, item_type, title, year, tmdb_id, imdb_id, guid, "
    "plex_path, local_path, path_status, file_size, file_mtime, file_fp, "
    "plex_added_at, plex_updated_at, plex_duration_ms, "
    "tvdb_id, parent_key, season_number, episode_number, "
    "child_count, leaf_count, viewed_leaf_count, facts"
)


@dataclass
class ProviderOutcome:
    provider: str
    eligible: int = 0
    processed: int = 0
    ok: int = 0
    errors: int = 0
    skipped: int = 0
    fresh: int = 0  # already up to date, not re-run
    skip_reason: str | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class ScanOutcome:
    run_id: int
    providers: list[ProviderOutcome] = field(default_factory=list)
    cancelled: bool = False

    @property
    def total_processed(self) -> int:
        return sum(p.processed for p in self.providers)

    @property
    def total_errors(self) -> int:
        return sum(p.errors for p in self.providers)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "cancelled": self.cancelled,
            "processed": self.total_processed,
            "errors": self.total_errors,
            "providers": [p.as_dict() for p in self.providers],
        }


def _row_to_item(row) -> ItemRow:
    data = dict(row)
    facts_raw = data.pop("facts", "{}")
    try:
        facts = json.loads(facts_raw or "{}")
    except json.JSONDecodeError:
        facts = {}
    return ItemRow(**data, facts=facts)


def nest_facts(flat: dict) -> dict:
    """Turn {'video.dar': 2.39} into {'video': {'dar': 2.39}} for json_patch."""
    out: dict = {}
    for dotted, value in flat.items():
        parts = dotted.split(".")
        cur = out
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value
    return out


def order_providers(providers: list) -> list:
    """Topological sort on depends_on, then by cost tier.

    Dependencies are hard: `derived` reads what ffprobe, plex and tmdb produce,
    so it has to run after all of them.
    """
    by_id = {p.id: p for p in providers}
    ordered: list = []
    seen: set[str] = set()
    visiting: set[str] = set()

    def visit(provider) -> None:
        if provider.id in seen:
            return
        if provider.id in visiting:
            raise ValueError(f"Circular provider dependency at {provider.id!r}")
        visiting.add(provider.id)
        for dep in provider.depends_on:
            if dep in by_id:
                visit(by_id[dep])
        visiting.discard(provider.id)
        seen.add(provider.id)
        ordered.append(provider)

    for provider in sorted(providers, key=lambda p: COST_ORDER.get(p.cost, 9)):
        visit(provider)
    return ordered


class ScanEngine:
    def __init__(self, db, registry, providers, broadcaster=None):
        self.db = db
        self.registry = registry
        self.providers = {p.id: p for p in providers}
        self.broadcaster = broadcaster
        self.lock = asyncio.Lock()
        self._cancel = asyncio.Event()
        self._current: dict | None = None
        # Kept up to date as the run proceeds so a cancellation — which unwinds
        # through CancelledError and never returns an outcome — can still report
        # what was completed. Work is flushed per batch, so "cancelled, done=0"
        # would wrongly suggest it was all thrown away.
        self.partial: ScanOutcome | None = None

    @property
    def running(self) -> bool:
        return self.lock.locked()

    @property
    def current(self) -> dict | None:
        return self._current

    def request_cancel(self) -> None:
        self._cancel.set()

    def with_dependents(self, provider_ids: list[str]) -> list[str]:
        """Add cheap providers that read what the named ones produce.

        Recomputing a provider in isolation leaves its dependents describing
        inputs that no longer exist. That's how `derived` ended up with no
        is_foreign or is_box_office_bomb: it ran before TMDB was configured,
        then TMDB was recomputed on its own and nothing re-derived from it.

        Only FREE and CHEAP dependents are pulled in. An EXPENSIVE dependent
        must never be started as a side effect of recomputing something it reads
        — it's reported as stale and left for an explicit run.
        """
        selected = set(provider_ids)
        deferred: set[str] = set()
        changed = True
        while changed:
            changed = False
            for p in self.providers.values():
                if p.id in selected or not any(d in selected for d in p.depends_on):
                    continue
                if COST_ORDER[p.cost] <= COST_ORDER[CostTier.CHEAP]:
                    selected.add(p.id)
                    changed = True
                else:
                    deferred.add(p.id)

        added = selected - set(provider_ids)
        if added:
            logger.info("↳ also running dependents: %s", ", ".join(sorted(added)))
        if deferred - selected:
            logger.warning(
                "↳ %s depend(s) on this and are now stale, but are too expensive "
                "to run automatically — start them explicitly.",
                ", ".join(sorted(deferred - selected)),
            )
        return sorted(selected)

    # ── planning ──────────────────────────────────────────────────────────
    async def plan(self, provider, force: bool = False) -> list[ItemRow]:
        """Items this provider should run against.

        Filtered by the provider's declared item types, so ffprobe never even
        sees the 508 shows it would only skip — and doesn't record 508 skip rows
        on every scan saying so.
        """
        where, params = provider.selector()
        types = provider.default_applies_to
        type_clause = ""
        if types:
            type_clause = f" AND item_type IN ({','.join('?' * len(types))})"
        rows = await self.db.fetch_all(
            f"SELECT {ITEM_COLUMNS} FROM items "
            f"WHERE deleted_at IS NULL{type_clause} AND ({where})",
            (*types, *params),
        )
        items = [_row_to_item(r) for r in rows]
        if not items:
            return []

        prov_rows = await self.db.fetch_all(
            "SELECT item_id, schema_version, input_fp, computed_at, status "
            "FROM fact_provenance WHERE provider = ?",
            (provider.id,),
        )
        provenance = {r["item_id"]: r for r in prov_rows}

        now = int(time.time())
        stale: list[ItemRow] = []
        for item in items:
            if force or self._is_stale(provider, item, provenance.get(item.id), now):
                stale.append(item)
        return stale

    def _is_stale(self, provider, item: ItemRow, prov, now: int) -> bool:
        if prov is None:
            return True
        if prov["schema_version"] != provider.schema_version:
            return True
        if prov["status"] == STATUS_ERROR:
            return True
        if prov["status"] == STATUS_SKIPPED:
            # Re-evaluate skips: a path that was unmapped may now be mapped.
            return True
        fp = provider.fingerprint(item)
        if fp is not None and prov["input_fp"] != fp:
            return True
        if provider.max_age_s is not None and now - prov["computed_at"] >= provider.max_age_s:
            return True
        return False

    # ── execution ─────────────────────────────────────────────────────────
    async def run(
        self,
        run_id: int,
        provider_ids: list[str] | None = None,
        force: bool = False,
        max_cost: CostTier = CostTier.CHEAP,
        settings=None,
    ) -> ScanOutcome:
        async with self.lock:
            self._cancel.clear()
            outcome = ScanOutcome(run_id=run_id)
            self.partial = outcome

            if provider_ids is not None:
                provider_ids = self.with_dependents(provider_ids)

            selected = [
                p for p in self.providers.values()
                if (provider_ids is None or p.id in provider_ids)
                and p.is_configured()
                and (provider_ids is not None or COST_ORDER[p.cost] <= COST_ORDER[max_cost])
            ]
            ordered = order_providers(selected)
            logger.info(
                "🔬 Scan %d: providers %s", run_id, ", ".join(p.id for p in ordered) or "(none)"
            )

            for provider in ordered:
                if self._cancel.is_set():
                    outcome.cancelled = True
                    break
                # Attached before it runs, and mutated in place, so a cancellation
                # mid-provider still finds accurate counts on self.partial.
                result = ProviderOutcome(provider=provider.id)
                outcome.providers.append(result)
                await self._run_provider(run_id, provider, force, settings, result)

            if self._cancel.is_set():
                outcome.cancelled = True

            self._current = None
            return outcome

    async def _run_provider(self, run_id: int, provider, force: bool, settings,
                            out: ProviderOutcome) -> ProviderOutcome:
        candidates = await self.plan(provider, force=force)
        eligible: list[ItemRow] = []
        skips: list[FactResult] = []
        for item in candidates:
            verdict = provider.can_enrich(item)
            if verdict.ok:
                eligible.append(item)
            else:
                skips.append(FactResult(
                    item.id, STATUS_SKIPPED, reason=verdict.reason,
                    input_fp=provider.fingerprint(item),
                ))

        out.eligible = len(eligible)
        out.skipped = len(skips)
        if skips:
            await self._flush(provider, skips)

        if not eligible:
            if out.skipped:
                # Say *why*, once. "0 eligible" with no explanation is the most
                # confusing possible outcome for a scan the user just triggered.
                reason = skips[0].reason or "not eligible"
                out.skip_reason = reason
                logger.info("   %s: nothing eligible — %d skipped (%s)",
                            provider.id, out.skipped, reason)
            else:
                logger.info("   %s: already up to date", provider.id)
            return out

        concurrency = 1
        if settings is not None:
            concurrency = settings.scan.concurrency.get(provider.id, provider.default_concurrency)
        semaphore = asyncio.Semaphore(max(1, concurrency))

        current_label = {"item": ""}

        def set_current(label: str) -> None:
            current_label["item"] = label

        ctx = EnrichContext(
            settings=settings, cancel=self._cancel, semaphore=semaphore, progress=set_current
        )

        self._publish(run_id, provider, 0, len(eligible), "")

        pending: list[FactResult] = []
        last_flush = time.monotonic()
        chunk_size = provider.batch_size or len(eligible)

        try:
            for start in range(0, len(eligible), chunk_size):
                if self._cancel.is_set():
                    break
                chunk = eligible[start:start + chunk_size]

                async for result in provider.enrich(chunk, ctx):
                    pending.append(result)
                    out.processed += 1
                    if result.status == STATUS_OK:
                        out.ok += 1
                    elif result.status == STATUS_ERROR:
                        out.errors += 1
                    else:
                        out.skipped += 1

                    self._publish(
                        run_id, provider, out.processed, len(eligible), current_label["item"]
                    )

                    now = time.monotonic()
                    if len(pending) >= FLUSH_EVERY or (now - last_flush) >= FLUSH_SECONDS:
                        # shield: a cancel landing mid-flush must still commit
                        # what's already been computed.
                        await asyncio.shield(self._flush(provider, pending))
                        pending = []
                        last_flush = now
        finally:
            if pending:
                await asyncio.shield(self._flush(provider, pending))

        logger.info(
            "   %s: %d ok, %d error, %d skipped",
            provider.id, out.ok, out.errors, out.skipped,
        )
        return out

    async def _flush(self, provider, results: list[FactResult]) -> None:
        """Persist a batch: facts, provenance and run counters in one transaction."""
        if not results:
            return
        now = int(time.time())
        statements: list[tuple[str, tuple]] = []

        for result in results:
            if result.facts:
                statements.append((
                    "UPDATE items SET facts = json_patch(facts, ?) WHERE id = ?",
                    (json.dumps(nest_facts(result.facts)), result.item_id),
                ))
            statements.append((
                "INSERT INTO fact_provenance "
                "(item_id, provider, schema_version, input_fp, computed_at, status, "
                " reason, duration_ms) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(item_id, provider) DO UPDATE SET "
                "  schema_version=excluded.schema_version, input_fp=excluded.input_fp, "
                "  computed_at=excluded.computed_at, status=excluded.status, "
                "  reason=excluded.reason, duration_ms=excluded.duration_ms",
                (result.item_id, provider.id, provider.schema_version, result.input_fp,
                 now, result.status, result.reason, result.duration_ms),
            ))

        await self.db.transaction(statements)

    def _publish(self, run_id: int, provider, done: int, total: int, current: str) -> None:
        self._current = {
            "run_id": run_id,
            "kind": "facts",
            "status": "running",
            "provider": provider.id,
            "provider_label": provider.label,
            "done": done,
            "total": total,
            "current": current,
        }
        if self.broadcaster:
            self.broadcaster.set_state("scan", self._current)

    # ── coverage ──────────────────────────────────────────────────────────
    async def coverage(self) -> dict[str, dict]:
        """Per-provider counts, for the Scan tab and the rule builder's
        "known for N of M items" hint.

        Each provider's denominator is the items it can actually apply to, not
        the whole catalogue. Dividing ffprobe by everything made it read
        23,473 / 23,981 — permanently 508 short, that being the number of shows,
        which have no file for it to probe.
        """
        rows_by_type = await self.db.fetch_all(
            "SELECT item_type, COUNT(*) AS n FROM items WHERE deleted_at IS NULL "
            "GROUP BY item_type"
        )
        per_type = {r["item_type"]: r["n"] for r in rows_by_type}

        def applicable(provider) -> int:
            types = getattr(provider, "default_applies_to", ()) or per_type.keys()
            return sum(per_type.get(t, 0) for t in types)
        # Staleness is only comparable in SQL for providers that fingerprint the
        # file. Everything else hashes API ids or other facts, so input_fp never
        # equals file_fp and they'd report 100% stale forever.
        file_fp_providers = [
            p.id for p in self.providers.values() if getattr(p, "file_fingerprinted", False)
        ]
        placeholders = ",".join("?" * len(file_fp_providers)) or "NULL"
        rows = await self.db.fetch_all(
            f"SELECT p.provider, p.status, COUNT(*) AS n, "
            f"       SUM(CASE WHEN p.provider IN ({placeholders}) "
            f"                AND p.input_fp IS NOT NULL AND i.file_fp IS NOT NULL "
            f"                AND p.input_fp != i.file_fp THEN 1 ELSE 0 END) AS stale "
            f"FROM fact_provenance p JOIN items i ON i.id = p.item_id "
            f"WHERE i.deleted_at IS NULL GROUP BY p.provider, p.status",
            tuple(file_fp_providers),
        )

        catalogue = sum(per_type.values())
        out: dict[str, dict] = {}
        for provider_id, provider in self.providers.items():
            out[provider_id] = {
                "known": 0, "errors": 0, "skipped": 0, "stale": 0,
                "total": applicable(provider),
                # The whole catalogue, so the UI can say "of 23,473 applicable"
                # rather than implying 508 items went missing.
                "catalogue": catalogue,
                "applies_to": list(getattr(provider, "default_applies_to", ()) or ()),
            }
        for row in rows:
            entry = out.setdefault(
                row["provider"],
                {"known": 0, "errors": 0, "skipped": 0, "stale": 0,
                 "total": catalogue, "catalogue": catalogue, "applies_to": []},
            )
            if row["status"] == STATUS_OK:
                entry["known"] += row["n"]
                entry["stale"] += row["stale"] or 0
            elif row["status"] == STATUS_ERROR:
                entry["errors"] += row["n"]
            else:
                entry["skipped"] += row["n"]
        return out

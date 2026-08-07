"""Expression indexes for hot fact keys.

SQLite only uses an expression index when the query text renders the expression
*identically* to the index definition. Both sides go through
`backend/rules/sql.py::value_expr`, and this module verifies at startup that the
planner actually picks the index up — otherwise a drift between the two would
silently degrade every rule to a full table scan with no visible symptom.

At ~2,000 movies a JSON scan is a few milliseconds and these are a nicety. They
start mattering for the aggregate CTEs, which sort a whole column, and for TV
scale later.
"""
from backend.common.logging_config import get_logger
from backend.facts.spec import FactType
from backend.rules.sql import value_expr

logger = get_logger(__name__)

PREFIX = "idx_fact_"


def index_name(key: str) -> str:
    return PREFIX + key.replace(".", "_")


async def reconcile(db, registry) -> dict:
    """Create indexes for keys marked indexed; drop ones no longer in the
    registry (a provider was removed or a key stopped being hot)."""
    wanted: dict[str, str] = {}
    for spec in registry.indexed_specs():
        if spec.type is FactType.LIST:
            # Addressed via json_each; a scalar index would never be consulted.
            continue
        wanted[index_name(spec.key)] = (
            f"CREATE INDEX IF NOT EXISTS {index_name(spec.key)} "
            f"ON items ({value_expr(spec)}) WHERE deleted_at IS NULL"
        )

    rows = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE ?",
        (PREFIX + "%",),
    )
    existing = {r["name"] for r in rows}

    created, dropped = [], []
    for name, ddl in wanted.items():
        if name not in existing:
            await db.execute(ddl)
            created.append(name)
    for name in existing - set(wanted):
        await db.execute(f"DROP INDEX IF EXISTS {name}")
        dropped.append(name)

    if created:
        logger.info("🔑 Created %d expression index(es)", len(created))
    if dropped:
        logger.info("🗑️  Dropped %d stale expression index(es)", len(dropped))

    return {"created": created, "dropped": dropped, "total": len(wanted)}


async def self_test(db, registry) -> list[str]:
    """Confirm the planner uses each expression index.

    A warning here means the index DDL and the compiled WHERE clause have
    drifted — the exact failure this design is arranged to prevent.
    """
    unused: list[str] = []
    for spec in registry.indexed_specs():
        if spec.type is FactType.LIST:
            continue
        expr = value_expr(spec)
        try:
            plan = await db.fetch_all(
                f"EXPLAIN QUERY PLAN SELECT id FROM items "
                f"WHERE deleted_at IS NULL AND {expr} > ?", (0,)
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("Index self-test failed for %s: %s", spec.key, exc)
            continue

        detail = " ".join(str(row["detail"]) for row in plan)
        if index_name(spec.key) not in detail:
            unused.append(spec.key)

    if unused:
        logger.warning(
            "⚠️  Expression index unused for: %s — the index DDL and the compiled "
            "WHERE clause may have drifted apart.", ", ".join(unused),
        )
    return unused

"""Rule tree -> parameterized SQL.

Compiling to SQL rather than filtering in Python is what makes library-relative
predicates cheap: "longer than the library's 90th percentile" is a CTE, not a
second pass over every row in application code.

**Safety.** Fact keys are looked up in the registry and rejected if unknown, so
the JSON path is always built from a registry key and never from user text.
Operators come from a fixed per-type allowlist. Values are bound parameters.
No user input is ever interpolated into SQL text.

**Parameter ordering is the landmine here.** SQLite binds `?` strictly
left-to-right across the finished statement, and CTEs are textually first — so
params must be assembled CTEs, then the main scope, then the predicates. Getting
this wrong doesn't error; it silently compares the wrong values.
"""
from dataclasses import dataclass, field
from typing import Any

from backend.common.errors import RuleError
from backend.rules.operators import AGG_SQL_OP, OPERATORS
from backend.rules.sql import json_path, known, value_expr
from backend.rules.validate import COLUMN_SORTS

# Non-fact sort keys -> SQL expressions.
COLUMN_SORT_SQL = {
    "title": "sort_title COLLATE NOCASE",
    "year": "year",
    "added_at": "plex_added_at",
    "size": "file_size",
    "random": "RANDOM()",
}


@dataclass
class Scope:
    """Which items a rule is allowed to see."""
    library_keys: list[str] = field(default_factory=list)
    item_types: list[str] = field(default_factory=lambda: ["movie"])

    def sql(self) -> tuple[str, list]:
        parts = ["deleted_at IS NULL"]
        params: list[Any] = []
        if self.item_types:
            parts.append(f"item_type IN ({','.join('?' * len(self.item_types))})")
            params += self.item_types
        if self.library_keys:
            parts.append(f"library_key IN ({','.join('?' * len(self.library_keys))})")
            params += self.library_keys
        return " AND ".join(parts), params


@dataclass
class Compiled:
    ctes: list[str] = field(default_factory=list)
    cte_params: list[Any] = field(default_factory=list)
    where: str = "1=1"
    where_params: list[Any] = field(default_factory=list)


class RuleCompiler:
    def __init__(self, registry, scope: Scope):
        self.registry = registry
        self.scope = scope
        self._ctes: list[str] = []
        self._cte_params: list[Any] = []
        self._agg_n = 0

    def compile(self, tree: dict) -> Compiled:
        where, params = self._node(tree["root"])
        return Compiled(self._ctes, self._cte_params, where, params)

    # ── nodes ─────────────────────────────────────────────────────────────
    def _node(self, node: dict) -> tuple[str, list]:
        ntype = node["type"]

        if ntype in ("and", "or"):
            children = node.get("children") or []
            if not children:
                # An empty AND matches everything; an empty OR matches nothing.
                return ("1=1" if ntype == "and" else "1=0"), []
            parts, params = [], []
            for child in children:
                sql, child_params = self._node(child)
                parts.append(sql)
                params += child_params
            return "(" + f" {ntype.upper()} ".join(parts) + ")", params

        if ntype == "not":
            sql, params = self._node(node["child"])
            return f"(NOT {sql})", params

        if ntype == "const":
            return ("1=1" if node.get("value") else "1=0"), []

        if ntype == "cmp":
            return self._cmp(node)

        if ntype == "agg_cmp":
            return self._agg(node)

        raise RuleError(f"Unknown node type {ntype!r}")

    def _cmp(self, node: dict) -> tuple[str, list]:
        spec = self.registry.require(node["key"])
        table = OPERATORS[spec.type]
        op = node.get("op")
        if op not in table:
            raise RuleError(
                f"'{op}' is not a valid operator for {spec.key!r} ({spec.type.value})."
            )
        return table[op](spec, node.get("value"))

    def _agg(self, node: dict) -> tuple[str, list]:
        spec = self.registry.require(node["key"])
        if not spec.aggregatable:
            raise RuleError(f"{spec.key!r} cannot be compared against a library aggregate.")

        op = node["op"]
        agg = node["agg"]
        name = f"agg_{self._agg_n}"
        self._agg_n += 1

        expr = value_expr(spec)
        scope_sql, scope_params = self.scope.sql()

        # The aggregate is over the whole library scope, NOT over the rule's other
        # predicates: "top 10% longest" means longest in the library, not longest
        # among the items that already matched. Stated in the UI help text,
        # because the other reading is defensible.
        base = (
            f"SELECT {expr} AS v FROM items "
            f"WHERE {scope_sql} AND json_extract(facts, '{json_path(spec.key)}') IS NOT NULL"
        )

        if agg in ("percentile", "median"):
            # SQLite has no percentile_cont. Nearest-rank via ORDER BY/OFFSET is
            # exact, needs no extension, and uses the expression index for the sort.
            quantile = 0.5 if agg == "median" else float(node["agg_arg"]) / 100.0
            cte = (
                f"{name} AS (WITH s AS ({base}) "
                f"SELECT v AS threshold FROM s ORDER BY v "
                f"LIMIT 1 OFFSET (SELECT MAX(0, CAST(ROUND((COUNT(*) - 1) * ?) AS INTEGER)) FROM s))"
            )
            params = list(scope_params) + [quantile]
        elif agg in ("mean", "min", "max"):
            fn = {"mean": "AVG", "min": "MIN", "max": "MAX"}[agg]
            cte = f"{name} AS (SELECT {fn}(v) AS threshold FROM ({base}))"
            params = list(scope_params)
        else:
            raise RuleError(f"Unknown aggregate {agg!r}")

        self._ctes.append(cte)
        self._cte_params += params

        # Knownness guard again: an item with no value must not pass a comparison
        # against the library's threshold.
        return (
            f"({known(spec.key)} AND {expr} {AGG_SQL_OP[op]} "
            f"(SELECT threshold FROM {name}))"
        ), []


def order_clause(order_by_key: str | None, direction: str, registry) -> str:
    if not order_by_key:
        return "sort_title COLLATE NOCASE ASC"
    dir_sql = "DESC" if str(direction).lower() == "desc" else "ASC"
    if order_by_key in COLUMN_SORTS:
        expr = COLUMN_SORT_SQL[order_by_key]
        return expr if order_by_key == "random" else f"{expr} {dir_sql}"
    spec = registry.require(order_by_key)
    # NULLs last in both directions: items missing the sort fact belong at the
    # bottom, not scattered by SQLite's default NULL ordering.
    return f"({value_expr(spec)}) IS NULL, {value_expr(spec)} {dir_sql}"


def build_query(
    compiled: Compiled,
    scope: Scope,
    select: str = "id, rating_key, title, year, sort_title",
    order_by: str | None = None,
    limit: int | None = None,
    count_only: bool = False,
) -> tuple[str, list]:
    """Assemble the final statement.

    Parameter order must mirror the text: CTEs, then the main WHERE's scope,
    then the predicates, then LIMIT.
    """
    params: list[Any] = []
    sql = ""

    if compiled.ctes:
        sql += "WITH " + ",\n     ".join(compiled.ctes) + "\n"
        params += compiled.cte_params

    scope_sql, scope_params = scope.sql()
    projection = "COUNT(*) AS n" if count_only else select
    sql += f"SELECT {projection} FROM items WHERE {scope_sql} AND {compiled.where}"
    params += scope_params
    params += compiled.where_params

    if not count_only:
        if order_by:
            sql += f"\nORDER BY {order_by}"
        if limit:
            sql += "\nLIMIT ?"
            params.append(int(limit))

    return sql, params


def compile_rule(
    tree: dict,
    registry,
    scope: Scope,
    order_by_key: str | None = None,
    order_dir: str = "desc",
    limit_n: int | None = None,
    select: str = "id, rating_key, title, year, sort_title",
    count_only: bool = False,
) -> tuple[str, list]:
    """Validated tree -> (sql, params). The one entry point callers should use."""
    compiler = RuleCompiler(registry, scope)
    compiled = compiler.compile(tree)
    order = None if count_only else order_clause(order_by_key, order_dir, registry)
    return build_query(
        compiled, scope, select=select, order_by=order,
        limit=None if count_only else limit_n, count_only=count_only,
    )

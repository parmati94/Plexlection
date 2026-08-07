"""Rule tree validation.

Runs before compilation and produces messages a person can act on, because this
structure is edited directly in the builder and a bad node should say which node
and why — not fail somewhere inside SQL generation.

Everything the compiler later interpolates into SQL is checked here: the key
exists in the registry, the operator is in that fact type's allowlist, the
aggregate is permitted for that fact.
"""
from typing import Any

from backend.common.errors import RuleError
from backend.facts.spec import FactType
from backend.rules.operators import AGG_OPERATORS, AGGREGATES, OPERATORS

MAX_DEPTH = 8
MAX_NODES = 200

GROUP_TYPES = ("and", "or")
LEAF_TYPES = ("cmp", "agg_cmp", "const")
ALL_TYPES = GROUP_TYPES + ("not",) + LEAF_TYPES

# Sortable columns that aren't facts.
COLUMN_SORTS = ("title", "year", "added_at", "size", "random")


def validate_tree(tree: Any, registry) -> dict:
    """Validate and return the normalized tree. Raises RuleError with a path."""
    if not isinstance(tree, dict):
        raise RuleError("A rule must be an object with a 'root' node.")

    root = tree.get("root")
    if root is None:
        raise RuleError("A rule must have a 'root' node.")

    counter = {"n": 0}
    normalized = _node(root, registry, depth=0, path="root", counter=counter)
    return {"version": int(tree.get("version", 1)), "root": normalized}


def _node(node: Any, registry, depth: int, path: str, counter: dict) -> dict:
    counter["n"] += 1
    if counter["n"] > MAX_NODES:
        raise RuleError(f"Rule is too large (over {MAX_NODES} nodes).")
    if depth > MAX_DEPTH:
        raise RuleError(f"Rule is nested too deeply at {path} (over {MAX_DEPTH} levels).")
    if not isinstance(node, dict):
        raise RuleError(f"{path}: expected an object, got {type(node).__name__}.")

    ntype = node.get("type")
    if ntype not in ALL_TYPES:
        raise RuleError(f"{path}: unknown node type {ntype!r}. Expected one of {', '.join(ALL_TYPES)}.")

    if ntype in GROUP_TYPES:
        children = node.get("children")
        if children is None:
            children = []
        if not isinstance(children, list):
            raise RuleError(f"{path}: '{ntype}' needs a list of children.")
        return {
            "type": ntype,
            "children": [
                _node(child, registry, depth + 1, f"{path}.{ntype}[{i}]", counter)
                for i, child in enumerate(children)
            ],
        }

    if ntype == "not":
        child = node.get("child")
        if child is None:
            raise RuleError(f"{path}: 'not' needs a 'child'.")
        return {"type": "not", "child": _node(child, registry, depth + 1, f"{path}.not", counter)}

    if ntype == "const":
        return {"type": "const", "value": bool(node.get("value"))}

    # ── leaves ────────────────────────────────────────────────────────────
    key = node.get("key")
    if not key or not isinstance(key, str):
        raise RuleError(f"{path}: a condition needs a fact 'key'.")

    spec = registry.get(key)
    if spec is None:
        raise RuleError(f"{path}: unknown fact {key!r}. It may belong to a provider that isn't installed.")

    op = node.get("op")

    if ntype == "cmp":
        allowed = OPERATORS[spec.type]
        if op not in allowed:
            raise RuleError(
                f"{path}: '{op}' is not a valid operator for {key!r} "
                f"({spec.type.value}). Valid: {', '.join(sorted(allowed))}."
            )
        value = node.get("value")
        _check_value(spec, op, value, path)
        return {"type": "cmp", "key": key, "op": op, "value": value}

    # agg_cmp
    if not spec.aggregatable:
        raise RuleError(
            f"{path}: {key!r} can't be compared against a library aggregate. "
            f"Only numeric and date facts can."
        )
    if op not in AGG_OPERATORS:
        raise RuleError(
            f"{path}: '{op}' is not valid with an aggregate. Use one of {', '.join(AGG_OPERATORS)}."
        )
    agg = node.get("agg")
    if agg not in AGGREGATES:
        raise RuleError(f"{path}: unknown aggregate {agg!r}. Use one of {', '.join(AGGREGATES)}.")

    agg_arg = node.get("agg_arg")
    if agg == "percentile":
        try:
            pct = float(agg_arg)
        except (TypeError, ValueError):
            raise RuleError(f"{path}: 'percentile' needs a number between 0 and 100.") from None
        if not 0 <= pct <= 100:
            raise RuleError(f"{path}: percentile must be between 0 and 100, got {pct}.")
        agg_arg = pct

    return {"type": "agg_cmp", "key": key, "op": op, "agg": agg, "agg_arg": agg_arg}


def _check_value(spec, op: str, value: Any, path: str) -> None:
    """Validate the operand here rather than leaving it to the compiler.

    The compiler would also reject a bad enum, but only once it's generating
    SQL — by which point the error can't say which node it came from, and the
    builder has no useful place to show it.
    """
    if op in ("is_null", "is_not_null", "is_true", "is_false", "is_empty", "is_not_empty"):
        return

    if spec.type is FactType.ENUM and spec.enum_values:
        allowed = {v.lower() for v in spec.enum_values}
        candidates = value if isinstance(value, (list, tuple, set)) else [value]
        for candidate in candidates:
            if candidate is None:
                continue
            if str(candidate).lower() not in allowed:
                raise RuleError(
                    f"{path}: {candidate!r} is not a valid value for {spec.key!r}. "
                    f"Valid: {', '.join(sorted(spec.enum_values))}."
                )

    if spec.type is FactType.NUMBER and op != "between":
        candidates = value if isinstance(value, (list, tuple, set)) else [value]
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                float(candidate)
            except (TypeError, ValueError):
                raise RuleError(
                    f"{path}: {spec.key!r} expects a number, got {candidate!r}."
                ) from None

    if op == "between":
        values = value if isinstance(value, (list, tuple)) else []
        if len(values) != 2:
            raise RuleError(f"{path}: 'between' needs exactly two values.")


def validate_order_by(key: str | None, registry) -> str | None:
    """Ordering is a materialisation concern, not a predicate, so it lives on the
    rule rather than in the tree — which also keeps `and` commutative."""
    if not key:
        return None
    if key in COLUMN_SORTS:
        return key
    spec = registry.get(key)
    if spec is None:
        raise RuleError(f"Unknown sort key {key!r}.")
    if spec.type is FactType.LIST:
        raise RuleError(f"Can't sort by {key!r} — it's a list.")
    return key


def collect_keys(node: dict, out: set[str] | None = None) -> set[str]:
    """Every fact key a tree references.

    Used to warn "this rule depends on facts that haven't been computed for 412
    items" before a sync quietly produces a short collection.
    """
    out = set() if out is None else out
    ntype = node.get("type")
    if ntype in GROUP_TYPES:
        for child in node.get("children", []):
            collect_keys(child, out)
    elif ntype == "not":
        collect_keys(node["child"], out)
    elif ntype in ("cmp", "agg_cmp"):
        out.add(node["key"])
    return out

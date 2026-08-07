"""The single SQL expression renderer.

**Invariant:** the expression used in `CREATE INDEX` and the one used in a
compiled `WHERE` clause must be byte-identical, or SQLite quietly ignores the
index and full-scans. Both call `value_expr()`, and it always emits an
unqualified `facts` (never `items.facts`) so there is no qualification mismatch
to get wrong. `backend/db/indexes.py` runs an EXPLAIN QUERY PLAN self-test at
startup to catch any drift.
"""
from backend.common.errors import RuleError
from backend.facts.spec import KEY_RE, FactSpec, FactType


def json_path(key: str) -> str:
    """JSON path for a fact key.

    Safe to interpolate into SQL: KEY_RE is the allowlist, and the key comes
    from the registry rather than from user input in the first place.
    """
    if not KEY_RE.fullmatch(key):
        raise RuleError(f"Illegal fact key: {key!r}")
    return "$." + key


def raw_extract(key: str) -> str:
    """Uncast extraction. Used for knownness guards, where the point is to
    distinguish JSON null / absent from a value."""
    return f"json_extract(facts, '{json_path(key)}')"


def value_expr(spec: FactSpec) -> str:
    """Comparable expression for a fact.

    Strings and enums are lowercased so comparisons are case-insensitive by
    default; the matching index is built on lower(...) too.
    """
    path = json_path(spec.key)
    if spec.type in (FactType.NUMBER, FactType.DATE):
        return f"CAST(json_extract(facts, '{path}') AS REAL)"
    if spec.type is FactType.BOOL:
        return f"CAST(json_extract(facts, '{path}') AS INTEGER)"
    if spec.type in (FactType.STRING, FactType.ENUM):
        return f"lower(json_extract(facts, '{path}'))"
    raise RuleError(
        f"{spec.key}: LIST facts are addressed with json_each, not value_expr"
    )


def known(key: str) -> str:
    """Guard asserting the fact has actually been computed."""
    return f"{raw_extract(key)} IS NOT NULL"


def is_array(key: str) -> str:
    """Guard asserting a LIST fact is present and really an array.

    Required as well as `known`: json_each against a missing path raises on some
    SQLite builds, and 'never scanned' must not read as 'empty list'.
    """
    return f"json_type(facts, '{json_path(key)}') = 'array'"


def escape_like(value: str) -> str:
    """Escape LIKE wildcards. Paired with ESCAPE '\\' at the call site."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

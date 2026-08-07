"""Operator allowlist and SQL emitters.

Every operator a rule can use is here. The compiler looks up
`OPERATORS[spec.type][op]` and refuses anything absent, so an unknown or
type-inappropriate operator is a 400 rather than something that reaches SQL.

**The knownness rule.** The fact store is sparse — an unscanned item simply has
no value for a key. SQL's three-valued logic will happily let such an item pass
a *negative* predicate, so `tmdb.keywords not_contains "documentary"` would match
every movie that has no TMDB data at all and quietly fill the collection with
unscanned items. Therefore every negative operator ANDs in a knownness guard.
This is the subtlest correctness issue in the whole compiler.
"""
from datetime import datetime, timezone
from typing import Any, Callable

from backend.common.errors import RuleError
from backend.facts.spec import FactSpec, FactType
from backend.rules.sql import escape_like, is_array, json_path, known, value_expr

Emitter = Callable[[FactSpec, Any], tuple[str, list]]

MAX_LIST_VALUES = 200


# ── helpers ───────────────────────────────────────────────────────────────
def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _num(value: Any, spec: FactSpec) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise RuleError(f"{spec.key}: expected a number, got {value!r}") from None


def _date(value: Any, spec: FactSpec) -> float:
    """Accept an epoch or an ISO-8601 string; store/compare as epoch seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return float(text)
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            raise RuleError(f"{spec.key}: {value!r} is not a date") from None
    raise RuleError(f"{spec.key}: expected a date, got {value!r}")


def _coerce(spec: FactSpec, value: Any) -> Any:
    if spec.type is FactType.NUMBER:
        return _num(value, spec)
    if spec.type is FactType.DATE:
        return _date(value, spec)
    return value


def _enum_value(spec: FactSpec, value: Any) -> str:
    text = str(value).lower()
    allowed = {v.lower() for v in (spec.enum_values or ())}
    if text not in allowed:
        raise RuleError(
            f"{spec.key}: {value!r} is not one of {sorted(spec.enum_values or ())}"
        )
    return text


# ── scalar comparisons ────────────────────────────────────────────────────
def _cmp(op: str, *, negative: bool = False) -> Emitter:
    def emit(spec: FactSpec, value: Any) -> tuple[str, list]:
        sql = f"({value_expr(spec)} {op} ?)"
        if negative:
            sql = f"({known(spec.key)} AND {sql})"
        return sql, [_coerce(spec, value)]
    return emit


def _between(spec: FactSpec, value: Any) -> tuple[str, list]:
    values = _as_list(value)
    if len(values) != 2:
        raise RuleError(f"{spec.key}: 'between' needs exactly two values")
    lo, hi = (_coerce(spec, v) for v in values)
    if lo > hi:
        lo, hi = hi, lo
    return f"({value_expr(spec)} BETWEEN ? AND ?)", [lo, hi]


def _in(negative: bool) -> Emitter:
    def emit(spec: FactSpec, value: Any) -> tuple[str, list]:
        values = _as_list(value)[:MAX_LIST_VALUES]
        if not values:
            return ("1=0" if not negative else "1=1"), []
        if spec.type is FactType.ENUM:
            coerced = [_enum_value(spec, v) for v in values]
        elif spec.type in (FactType.STRING,):
            coerced = [str(v).lower() for v in values]
        else:
            coerced = [_coerce(spec, v) for v in values]
        placeholders = ",".join("?" * len(coerced))
        sql = f"({value_expr(spec)} {'NOT ' if negative else ''}IN ({placeholders}))"
        if negative:
            sql = f"({known(spec.key)} AND {sql})"
        return sql, coerced
    return emit


def _is_null(spec: FactSpec, _value: Any) -> tuple[str, list]:
    from backend.rules.sql import raw_extract
    return f"({raw_extract(spec.key)} IS NULL)", []


def _is_not_null(spec: FactSpec, _value: Any) -> tuple[str, list]:
    return f"({known(spec.key)})", []


# ── strings ───────────────────────────────────────────────────────────────
def _like(pattern: str, negative: bool = False) -> Emitter:
    def emit(spec: FactSpec, value: Any) -> tuple[str, list]:
        needle = pattern.format(escape_like(str(value).lower()))
        sql = f"({value_expr(spec)} LIKE ? ESCAPE '\\')"
        if negative:
            sql = f"({known(spec.key)} AND NOT {sql})"
        return sql, [needle]
    return emit


def _enum_eq(spec: FactSpec, value: Any) -> tuple[str, list]:
    return f"({value_expr(spec)} = ?)", [_enum_value(spec, value)]


def _enum_ne(spec: FactSpec, value: Any) -> tuple[str, list]:
    return f"({known(spec.key)} AND {value_expr(spec)} <> ?)", [_enum_value(spec, value)]


def _str_eq(spec: FactSpec, value: Any) -> tuple[str, list]:
    return f"({value_expr(spec)} = ?)", [str(value).lower()]


def _str_ne(spec: FactSpec, value: Any) -> tuple[str, list]:
    return f"({known(spec.key)} AND {value_expr(spec)} <> ?)", [str(value).lower()]


# ── booleans ──────────────────────────────────────────────────────────────
def _bool(want: int) -> Emitter:
    def emit(spec: FactSpec, _value: Any) -> tuple[str, list]:
        # is_false is a negative: an unscanned item is not "false".
        guard = f"{known(spec.key)} AND " if want == 0 else ""
        return f"({guard}{value_expr(spec)} = {want})", []
    return emit


# ── lists ─────────────────────────────────────────────────────────────────
#
# Every list predicate is wrapped in `CASE WHEN <is array> THEN <test> END`, with
# no ELSE, so an unknown fact evaluates to NULL rather than false.
#
# This is not cosmetic. The obvious form, `json_type(...) = 'array' AND EXISTS(...)`,
# evaluates to **false** for an item that was never scanned, because SQL says
# `NULL AND false` is false. Wrapping that in NOT then yields true, and every
# unscanned item silently lands in the collection — the same sparse-store trap the
# dedicated negative operators guard against, reached instead through an explicit
# NOT node. NULL propagation makes NOT safe for lists exactly as it already is for
# scalar comparisons.
def _list_case(spec: FactSpec, test: str) -> str:
    return f"(CASE WHEN {is_array(spec.key)} THEN ({test}) END)"


def _list_contains(spec: FactSpec, value: Any) -> tuple[str, list]:
    path = json_path(spec.key)
    test = f"EXISTS (SELECT 1 FROM json_each(facts, '{path}') WHERE lower(value) = ?)"
    return _list_case(spec, test), [str(value).lower()]


def _list_not_contains(spec: FactSpec, value: Any) -> tuple[str, list]:
    """An item with no data at all must not count as 'does not contain X'."""
    path = json_path(spec.key)
    test = f"NOT EXISTS (SELECT 1 FROM json_each(facts, '{path}') WHERE lower(value) = ?)"
    return _list_case(spec, test), [str(value).lower()]


def _list_contains_any(spec: FactSpec, value: Any) -> tuple[str, list]:
    values = [str(v).lower() for v in _as_list(value)][:MAX_LIST_VALUES]
    if not values:
        return "1=0", []
    path = json_path(spec.key)
    placeholders = ",".join("?" * len(values))
    test = (
        f"EXISTS (SELECT 1 FROM json_each(facts, '{path}') "
        f"WHERE lower(value) IN ({placeholders}))"
    )
    return _list_case(spec, test), values


def _list_contains_all(spec: FactSpec, value: Any) -> tuple[str, list]:
    values = sorted({str(v).lower() for v in _as_list(value)})[:MAX_LIST_VALUES]
    if not values:
        return "1=1", []
    path = json_path(spec.key)
    placeholders = ",".join("?" * len(values))
    test = (
        f"(SELECT COUNT(DISTINCT lower(value)) FROM json_each(facts, '{path}') "
        f"WHERE lower(value) IN ({placeholders})) = {len(values)}"
    )
    return _list_case(spec, test), values


def _list_len(op: str) -> Emitter:
    def emit(spec: FactSpec, value: Any) -> tuple[str, list]:
        path = json_path(spec.key)
        return _list_case(spec, f"json_array_length(facts, '{path}') {op} ?"), \
            [int(_num(value, spec))]
    return emit


def _list_empty(negative: bool) -> Emitter:
    def emit(spec: FactSpec, _value: Any) -> tuple[str, list]:
        path = json_path(spec.key)
        op = ">" if negative else "="
        return _list_case(spec, f"json_array_length(facts, '{path}') {op} 0"), []
    return emit


# ── dates ─────────────────────────────────────────────────────────────────
def _in_last_days(spec: FactSpec, value: Any) -> tuple[str, list]:
    """Relative to evaluation time, so a saved rule's meaning shifts daily.
    That's intended, but the UI says so — otherwise a nightly sync quietly
    dropping items looks like a bug."""
    days = _num(value, spec)
    return (
        f"({known(spec.key)} AND {value_expr(spec)} >= (strftime('%s','now') - ?))"
    ), [days * 86400]


def _not_in_last_days(spec: FactSpec, value: Any) -> tuple[str, list]:
    days = _num(value, spec)
    return (
        f"({known(spec.key)} AND {value_expr(spec)} < (strftime('%s','now') - ?))"
    ), [days * 86400]


# ── the allowlist ─────────────────────────────────────────────────────────
OPERATORS: dict[FactType, dict[str, Emitter]] = {
    FactType.NUMBER: {
        "eq": _cmp("="), "ne": _cmp("<>", negative=True),
        "gt": _cmp(">"), "gte": _cmp(">="),
        "lt": _cmp("<"), "lte": _cmp("<="),
        "between": _between,
        "in": _in(False), "not_in": _in(True),
        "is_null": _is_null, "is_not_null": _is_not_null,
    },
    FactType.DATE: {
        "before": _cmp("<"), "after": _cmp(">"),
        "between": _between,
        "in_last_days": _in_last_days, "not_in_last_days": _not_in_last_days,
        "is_null": _is_null, "is_not_null": _is_not_null,
    },
    FactType.STRING: {
        "eq": _str_eq, "ne": _str_ne,
        "contains": _like("%{}%"), "not_contains": _like("%{}%", negative=True),
        "starts_with": _like("{}%"), "ends_with": _like("%{}"),
        "in": _in(False), "not_in": _in(True),
        "is_null": _is_null, "is_not_null": _is_not_null,
    },
    FactType.ENUM: {
        "eq": _enum_eq, "ne": _enum_ne,
        "in": _in(False), "not_in": _in(True),
        "is_null": _is_null, "is_not_null": _is_not_null,
    },
    FactType.BOOL: {
        "is_true": _bool(1), "is_false": _bool(0),
        "is_null": _is_null, "is_not_null": _is_not_null,
    },
    FactType.LIST: {
        "contains": _list_contains, "not_contains": _list_not_contains,
        "contains_any": _list_contains_any, "contains_all": _list_contains_all,
        "length_gte": _list_len(">="), "length_lte": _list_len("<="),
        "is_empty": _list_empty(False), "is_not_empty": _list_empty(True),
    },
}

# Aggregate comparisons, valid only where spec.aggregatable is True.
AGG_OPERATORS = ("gt", "gte", "lt", "lte")
AGGREGATES = ("percentile", "median", "mean", "min", "max")

AGG_SQL_OP = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


# ── UI metadata ───────────────────────────────────────────────────────────
# arity: 0 = no value, 1 = one, 2 = a pair, "n" = a list.
_LABELS: dict[str, tuple[str, Any, str]] = {
    "eq": ("is", 1, "value"), "ne": ("is not", 1, "value"),
    "gt": ("is greater than", 1, "number"), "gte": ("is at least", 1, "number"),
    "lt": ("is less than", 1, "number"), "lte": ("is at most", 1, "number"),
    "between": ("is between", 2, "number"),
    "before": ("is before", 1, "date"), "after": ("is after", 1, "date"),
    "in_last_days": ("is within the last N days", 1, "number"),
    "not_in_last_days": ("is older than N days", 1, "number"),
    "contains": ("contains", 1, "value"), "not_contains": ("does not contain", 1, "value"),
    "starts_with": ("starts with", 1, "value"), "ends_with": ("ends with", 1, "value"),
    "in": ("is any of", "n", "value"), "not_in": ("is none of", "n", "value"),
    "contains_any": ("includes any of", "n", "value"),
    "contains_all": ("includes all of", "n", "value"),
    "length_gte": ("has at least N entries", 1, "number"),
    "length_lte": ("has at most N entries", 1, "number"),
    "is_empty": ("is empty", 0, None), "is_not_empty": ("is not empty", 0, None),
    "is_true": ("is true", 0, None), "is_false": ("is false", 0, None),
    "is_null": ("has not been computed", 0, None),
    "is_not_null": ("has been computed", 0, None),
}


def operators_for(spec: FactSpec) -> list[dict]:
    """Operator descriptors for the rule builder UI."""
    out = []
    for op in OPERATORS[spec.type]:
        label, arity, kind = _LABELS.get(op, (op, 1, "value"))
        if kind == "value":
            if spec.type is FactType.ENUM:
                kind = "enum"
            elif spec.type is FactType.LIST:
                kind = "suggest"
            elif spec.type is FactType.NUMBER:
                kind = "number"
            else:
                kind = "text"
        out.append({"op": op, "label": label, "arity": arity, "value_kind": kind})
    return out


def aggregates_for(spec: FactSpec) -> list[dict]:
    if not spec.aggregatable:
        return []
    return [
        {"agg": "percentile", "label": "the library's Nth percentile", "arg": "percent"},
        {"agg": "median", "label": "the library median", "arg": None},
        {"agg": "mean", "label": "the library average", "arg": None},
        {"agg": "min", "label": "the library minimum", "arg": None},
        {"agg": "max", "label": "the library maximum", "arg": None},
    ]

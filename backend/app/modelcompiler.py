"""Pure measure compiler for the Phase 16 semantic model (MODEL-04).

compile_measure(model, request) turns an approved measure plus dimensions, a
grain, filters and a date range into ONE read-only SELECT. Everything here is
deterministic plumbing: no database, network, clock or LLM. Identical inputs
(filters in any order, model rows in any order) give byte-identical SQL.

Rules that keep the numbers honest (each has a unit test):
  * joins follow approved relationships only, many side to one side, as LEFT
    JOINs, at most MAX_HOPS hops; fan-out, ambiguity and unknown paths raise
    NotModeled instead of guessing;
  * a simple measure's expression may add up schema.table.column of a relation reached that way
    (Quick 054), never one on the many side;
  * ratio and period-over-period measures compose AFTER aggregation: one CTE
    per input, a key spine joined with IS NOT DISTINCT FROM;
  * semi-additive measures aggregate only the rows at the max/min snapshot;
  * statements are built from the sqlglot AST (values are literal nodes,
    identifiers are quoted) and every result passes
    guardrails.validate_readonly_sql before it is returned.

Field names follow MetricFlow / Open Semantic Interchange so a later export is
a mapping, not a redesign.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

import sqlglot
from sqlglot import exp

from .guardrails import SQLGuardrailError, validate_readonly_sql

GRAINS = ("day", "week", "month", "quarter", "year")
AGGS = ("sum", "count", "count_distinct", "average", "min", "max", "sum_boolean")
FILTER_OPS = ("=", "!=", "in", "not_in", ">", ">=", "<", "<=", "is_null", "is_not_null")
OBJECTS = ("table", "relationship", "measure", "view", "materialized_view", "index")
OFFSET_UNITS = ("day", "week", "month", "quarter", "year")
MAX_HOPS = 3
MAX_DIMS = 5
MAX_FILTERS = 10
MAX_VALUES = 50
MAX_VALUE_CHARS = 200
MAX_WINDOW_DAYS = 3660
NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_PART = r"[^\s.;\"'()]+"
_REL_RE = re.compile(rf"^{_PART}\.{_PART}$")
_DIM_RE = re.compile(rf"^{_PART}\.{_PART}\.{_PART}$")
_COL_RE = re.compile(rf"^{_PART}$")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_OFFSET_RE = re.compile(r"^(\d{1,2}) (day|week|month|quarter|year)s?$")
_ADDITIVITY = ("additive", "semi_additive", "non_additive")
_KINDS = ("base", "view", "matview")
_ROLES = ("fact", "dimension")
_MAX_EXPR_CHARS = 500
_MAX_EXPR_NODES = 200


class NotModeled(Exception):
    """The question cannot be answered from approved model objects."""


class InvalidRequest(ValueError):
    """Malformed input (bad op, bad date, a semicolon in a value, ...)."""


# --------------------------------------------------------------------------- dataclasses

@dataclass(frozen=True)
class Filter:
    dimension: str
    op: str
    values: tuple = ()


@dataclass(frozen=True)
class ModelTable:
    name: str
    relation: str
    object_kind: str = "base"
    role: str | None = None
    grain: str | None = None
    primary_key: tuple = ()
    agg_time_dimension: str | None = None


@dataclass(frozen=True)
class Relationship:
    name: str
    from_relation: str
    from_columns: tuple
    to_relation: str
    to_columns: tuple
    cardinality: str = "many_to_one"


@dataclass(frozen=True)
class MetricInput:
    name: str
    alias: str | None = None
    offset_window: str | None = None
    filter: tuple = ()


@dataclass(frozen=True)
class NonAdditive:
    name: str
    window_choice: str = "max"
    window_groupings: tuple = ()


@dataclass(frozen=True)
class Measure:
    name: str
    type: str
    base_relation: str | None = None
    agg: str | None = None
    expr: str | None = None
    agg_time_dimension: str | None = None
    filter: tuple = ()
    numerator: MetricInput | None = None
    denominator: MetricInput | None = None
    input_metrics: tuple = ()
    non_additive_dimension: NonAdditive | None = None
    additivity: str = "additive"
    unit: str | None = None
    format: str | None = None
    label: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class Model:
    tables: tuple = ()
    relationships: tuple = ()
    measures: tuple = ()
    columns: Mapping[str, Mapping[str, str]] = field(default_factory=dict, hash=False)

    def measure(self, name: str) -> Measure | None:
        for m in self.measures:
            if m.name == name:
                return m
        return None

    def with_measure(self, m: Measure) -> "Model":
        kept = tuple(x for x in self.measures if x.name != m.name)
        return replace(self, measures=tuple(sorted(kept + (m,), key=lambda x: x.name)))

    def with_relationship(self, r: Relationship) -> "Model":
        kept = tuple(x for x in self.relationships if x.name != r.name)
        return replace(self, relationships=tuple(sorted(kept + (r,), key=lambda x: x.name)))

    def with_table(self, t: ModelTable) -> "Model":
        kept = tuple(x for x in self.tables if x.name != t.name)
        return replace(self, tables=tuple(sorted(kept + (t,), key=lambda x: x.name)))


@dataclass(frozen=True)
class Request:
    measure: str
    by: tuple = ()
    grain: str | None = None
    filters: tuple = ()
    start: date | None = None
    end: date | None = None
    window_days: int | None = None


@dataclass(frozen=True)
class Compiled:
    sql: str
    columns: tuple
    measure: str
    joins: tuple
    notes: tuple


@dataclass(frozen=True)
class JoinStep:
    relationship: str
    from_alias: str
    to_alias: str
    to_relation: str
    from_columns: tuple = ()
    to_columns: tuple = ()


# --------------------------------------------------------------------------- small helpers

def _rel_of(dim: str) -> str:
    return dim.rsplit(".", 1)[0]


def _col_of(dim: str) -> str:
    return dim.rsplit(".", 1)[1]


def _check_text(v: Any, what: str, limit: int = 1000) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise InvalidRequest(f"{what} must be text")
    if len(v) > limit:
        raise InvalidRequest(f"{what} is longer than {limit} characters")
    return v


def _check_name(v: Any, what: str = "name") -> str:
    if not isinstance(v, str) or not NAME_RE.match(v):
        raise InvalidRequest(f"{what} must match {NAME_RE.pattern}")
    return v


def _check_relation(v: Any, what: str) -> str:
    if not isinstance(v, str) or not _REL_RE.match(v):
        raise InvalidRequest(f"{what} must look like schema.table")
    return v


def _check_dim(v: Any, what: str = "dimension") -> str:
    if not isinstance(v, str) or not _DIM_RE.match(v):
        raise InvalidRequest(f"{what} must look like schema.table.column")
    return v


def _check_columns(v: Any, what: str) -> tuple:
    if not isinstance(v, (list, tuple)) or not 1 <= len(v) <= 4:
        raise InvalidRequest(f"{what} needs 1 to 4 column names")
    for c in v:
        if not isinstance(c, str) or not _COL_RE.match(c):
            raise InvalidRequest(f"{what} has an invalid column name")
    return tuple(v)


def _value_key(v: Any) -> tuple:
    return (type(v).__name__, str(v))


def _parse_date(v: Any, what: str) -> date | None:
    if v is None:
        return None
    if type(v) is date:
        return v
    if isinstance(v, str) and _ISO_RE.match(v):
        try:
            return date.fromisoformat(v)
        except ValueError:
            pass
    raise InvalidRequest(f"{what} is not a valid ISO date (YYYY-MM-DD)")


def _norm_filter(item: Any) -> Filter:
    if isinstance(item, Filter):
        dim, op, values = item.dimension, item.op, item.values
    elif isinstance(item, Mapping):
        dim, op, values = item.get("dimension"), item.get("op"), item.get("values", ())
    else:
        raise InvalidRequest("a filter must be {dimension, op, values}")
    _check_dim(dim, "filter dimension")
    if op not in FILTER_OPS:
        raise InvalidRequest(f"filter op must be one of {', '.join(FILTER_OPS)}")
    if values is None:
        values = ()
    if not isinstance(values, (list, tuple)):
        raise InvalidRequest("filter values must be a list")
    if len(values) > MAX_VALUES:
        raise InvalidRequest(f"a filter takes at most {MAX_VALUES} values")
    for v in values:
        if isinstance(v, bool) or isinstance(v, (int, float)):
            continue
        if not isinstance(v, str):
            raise InvalidRequest("filter values must be text, numbers or booleans")
        if ";" in v:
            raise InvalidRequest("a filter value may not contain a semicolon")
        if len(v) > MAX_VALUE_CHARS:
            raise InvalidRequest(f"a filter value is longer than {MAX_VALUE_CHARS} characters")
    if op in ("is_null", "is_not_null"):
        if values:
            raise InvalidRequest(f"{op} takes no values")
    elif op in ("in", "not_in"):
        if not values:
            raise InvalidRequest(f"{op} needs at least one value")
        values = tuple(sorted(set(values), key=_value_key))
    else:
        if len(values) != 1:
            raise InvalidRequest(f"{op} takes exactly one value")
    return Filter(dim, op, tuple(values))


def _norm_filters(items: Any, what: str = "filters") -> tuple:
    if items is None:
        return ()
    if not isinstance(items, (list, tuple)):
        raise InvalidRequest(f"{what} must be a list")
    if len(items) > MAX_FILTERS:
        raise InvalidRequest(f"at most {MAX_FILTERS} {what}")
    fs = {_norm_filter(i) for i in items}
    return tuple(sorted(fs, key=lambda f: (f.dimension, f.op, tuple(_value_key(v) for v in f.values))))


def _out_name(dim: str) -> str:
    rel = _rel_of(dim)
    return f"{rel.split('.')[-1]}__{_col_of(dim)}"


def make_request(measure, by=None, grain=None, filters=None, start=None, end=None,
                 window_days=None) -> Request:
    """Validate and canonicalise a request (filters deduped and sorted)."""
    _check_name(measure, "measure")
    by = tuple(by or ())
    if len(by) > MAX_DIMS:
        raise InvalidRequest(f"at most {MAX_DIMS} dimensions")
    seen: list[str] = []
    for d in by:
        _check_dim(d, "dimension")
        if d not in seen:
            seen.append(d)
    names: dict[str, str] = {}
    for d in seen:
        n = _out_name(d)
        if n in names:
            raise InvalidRequest(f"dimensions {names[n]} and {d} would both be named {n}")
        names[n] = d
    if grain is not None and grain not in GRAINS:
        raise InvalidRequest(f"grain must be one of {', '.join(GRAINS)}")
    fs = _norm_filters(filters)
    s = _parse_date(start, "start")
    e = _parse_date(end, "end")
    if s is not None and e is not None and s >= e:
        raise InvalidRequest("start must be before end (end is exclusive)")
    if window_days is not None:
        if isinstance(window_days, bool) or not isinstance(window_days, int) \
                or not 1 <= window_days <= MAX_WINDOW_DAYS:
            raise InvalidRequest(f"window_days must be 1 to {MAX_WINDOW_DAYS}")
        if s is not None or e is not None:
            raise InvalidRequest("window_days cannot be combined with start or end")
    return Request(measure, tuple(seen), grain, fs, s, e, window_days)


# --------------------------------------------------------------------------- expressions

_EXPR_OK = (
    exp.Column, exp.Identifier, exp.Literal, exp.Paren, exp.Add, exp.Sub, exp.Mul, exp.Div,
    exp.Mod, exp.Neg, exp.Case, exp.If, exp.Coalesce, exp.Nullif, exp.Cast, exp.DataType,
    exp.DataTypeParam, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.And, exp.Or,
    exp.Not, exp.Is, exp.Null, exp.Boolean, exp.In, exp.Abs, exp.Round, exp.Greatest,
    exp.Least, exp.Between,
)
_CAST_TYPES = {"INT", "BIGINT", "SMALLINT", "DECIMAL", "DOUBLE", "FLOAT", "DATE", "BOOLEAN"}


def _col_relation(node: exp.Column) -> str | None:
    """'schema.table' a column names, or None when it is unqualified (Quick 054)."""
    if not (node.table or node.args.get("db")):
        return None
    return f"{node.db}.{node.table}"


def _validate_expr(expr: str, columns: Mapping[str, str] | None, allow_star: bool, *,
                   linked: bool = False, catalog: Mapping[str, Mapping[str, str]] | None = None
                   ) -> exp.Expression:
    """linked=True (a simple measure, Quick 054) also accepts schema.table.column; catalog, when
    given, must hold that column. Whether the table is reachable is the join planner's call."""
    if not isinstance(expr, str) or not expr.strip():
        raise InvalidRequest("expression is empty")
    if len(expr) > _MAX_EXPR_CHARS:
        raise InvalidRequest(f"expression is longer than {_MAX_EXPR_CHARS} characters")
    if ";" in expr:
        raise InvalidRequest("expression may not contain a semicolon")
    try:
        tree = sqlglot.parse_one(expr, dialect="postgres")
    except Exception as e:  # sqlglot ParseError / TokenError
        raise InvalidRequest(f"expression could not be parsed: {str(e)[:80]}") from None
    if isinstance(tree, exp.Star):
        if allow_star:
            return tree
        raise InvalidRequest("* is only allowed for count")
    count = 0
    for node in tree.walk():
        count += 1
        if count > _MAX_EXPR_NODES:
            raise InvalidRequest("expression is too large")
        if not isinstance(node, _EXPR_OK):
            raise InvalidRequest(f"{type(node).__name__} is not allowed in an expression")
        if isinstance(node, exp.Column):
            qualified = node.table or node.args.get("db") or node.args.get("catalog")
            if qualified and not linked:
                raise InvalidRequest("qualified columns are not allowed in an expression")
            if qualified:
                if node.args.get("catalog") or not (node.table and node.db):
                    raise InvalidRequest(
                        "write a column from another table as schema.table.column, "
                        f"for example public.products.unit_cost (got {node.sql(dialect='postgres')})")
                rel = _col_relation(node)
                if catalog is not None and node.name not in (catalog.get(rel) or {}):
                    raise InvalidRequest(f"unknown column {rel}.{node.name} (schema, table and column "
                                         "are matched exactly as get_model shows them)")
                continue
            if columns is not None and node.name not in columns:
                raise InvalidRequest(f"unknown column {node.name}")
        elif isinstance(node, exp.DataType):
            if node.this.name not in _CAST_TYPES:
                raise InvalidRequest("only numeric, boolean and date casts are allowed")
    return tree


def validate_expr(expr: str, columns: Mapping[str, str], *, allow_star: bool = False) -> exp.Expression:
    """Parse a measure expression and refuse anything outside the allowlist."""
    return _validate_expr(expr, columns, allow_star)


def _qualify(tree: exp.Expression, base: str, amap: Mapping[str, str]) -> exp.Expression:
    """Each column on its join alias: unqualified -> the base (t0), schema.table.column -> that table."""
    def fn(node):
        if isinstance(node, exp.Column):
            return exp.column(node.name, table=amap[_col_relation(node) or base])
        return node
    return tree.transform(fn)


def measure_relations(defn: Mapping) -> set:
    """Every relation a stored measure row reads: base, expression (Quick 054), time column, filters
    and semi-additive dimension. The retire guards use it, so a view a measure reads stays."""
    out = set()
    if defn.get("base_relation"):
        out.add(defn["base_relation"])
    if defn.get("type") == "simple":
        out.update(expr_relations(defn.get("expr")))
    dims = [defn.get("agg_time_dimension")]
    dims += [f.get("dimension") for f in (defn.get("filter") or []) if isinstance(f, Mapping)]
    nad = defn.get("non_additive_dimension")
    if isinstance(nad, Mapping):
        dims.append(nad.get("name"))
        dims += list(nad.get("window_groupings") or [])
    for d in dims:
        if isinstance(d, str) and d.count(".") >= 2:
            out.add(_rel_of(d))
    return out


def _expr_rels(tree: exp.Expression) -> set:
    return {r for r in (_col_relation(c) for c in tree.find_all(exp.Column)) if r}


def expr_relations(expr: str | None) -> tuple:
    """Relations a simple measure's expression names as schema.table.column (Quick 054), sorted.
    Meant for expressions normalize_definition already accepted (stored or validated ones); on other
    text it is a best effort. Never raises: an unparsable expression names nothing."""
    if not isinstance(expr, str) or not expr.strip() or ";" in expr:
        return ()
    try:
        tree = sqlglot.parse_one(expr, dialect="postgres")
    except Exception:
        return ()
    return tuple(sorted({r for r in (_col_relation(c) for c in tree.find_all(exp.Column))
                         if r and not r.startswith(".")}))


# --------------------------------------------------------------------------- definitions

def _norm_offset(v: Any) -> str | None:
    if v is None:
        return None
    m = _OFFSET_RE.match(v) if isinstance(v, str) else None
    if not m or not 1 <= int(m.group(1)) <= 24:
        raise InvalidRequest("offset_window must look like '1 month' (1 to 24 of day, week, month, quarter, year)")
    return f"{int(m.group(1))} {m.group(2)}"


def _norm_input(item: Any) -> dict:
    if not isinstance(item, Mapping):
        raise InvalidRequest("a metric input must be {name, alias, offset_window, filter}")
    alias = item.get("alias")
    if alias is not None:
        _check_name(alias, "alias")
    return {
        "name": _check_name(item.get("name"), "input measure name"),
        "alias": alias,
        "offset_window": _norm_offset(item.get("offset_window")),
        "filter": [dict(dimension=f.dimension, op=f.op, values=list(f.values))
                   for f in _norm_filters(item.get("filter"), "input filters")],
    }


def _alias_of(inp: dict) -> str:
    return inp.get("alias") or inp["name"]


def _filters_json(fs: tuple) -> list:
    return [dict(dimension=f.dimension, op=f.op, values=list(f.values)) for f in fs]


def _normalize_measure(item: Mapping) -> dict:
    name = _check_name(item.get("name"))
    typ = item.get("type")
    if typ not in ("simple", "ratio", "derived"):
        raise InvalidRequest("measure type must be simple, ratio or derived")
    out: dict = {
        "name": name, "type": typ,
        "label": _check_text(item.get("label"), "label", 120),
        "description": _check_text(item.get("description"), "description", 1000),
        "unit": _check_text(item.get("unit"), "unit", 40),
        "format": _check_text(item.get("format"), "format", 40),
        "base_relation": None, "agg": None, "expr": None, "agg_time_dimension": None,
        "filter": [], "numerator": None, "denominator": None, "input_metrics": None,
        "non_additive_dimension": None, "additivity": "additive",
    }
    if typ == "simple":
        out["base_relation"] = _check_relation(item.get("base_relation"), "base_relation")
        agg = item.get("agg")
        if agg not in AGGS:
            raise InvalidRequest(f"agg must be one of {', '.join(AGGS)}")
        out["agg"] = agg
        expr = item.get("expr")
        if expr is None and agg == "count":
            out["expr"] = None
        else:
            _validate_expr(expr, None, allow_star=(agg == "count"), linked=True)
            out["expr"] = expr.strip()
        atd = item.get("agg_time_dimension")
        out["agg_time_dimension"] = _check_dim(atd, "agg_time_dimension") if atd is not None else None
        out["filter"] = _filters_json(_norm_filters(item.get("filter"), "measure filters"))
        nad = item.get("non_additive_dimension")
        additivity = item.get("additivity") or "additive"
        if additivity not in _ADDITIVITY:
            raise InvalidRequest("additivity must be additive, semi_additive or non_additive")
        if nad is not None:
            if not isinstance(nad, Mapping):
                raise InvalidRequest("non_additive_dimension must be {name, window_choice, window_groupings}")
            choice = nad.get("window_choice") or "max"
            if choice not in ("max", "min"):
                raise InvalidRequest("window_choice must be max or min")
            groupings = nad.get("window_groupings") or []
            if not isinstance(groupings, (list, tuple)) or len(groupings) > 3:
                raise InvalidRequest("window_groupings takes at most 3 dimensions")
            out["non_additive_dimension"] = {
                "name": _check_dim(nad.get("name"), "non_additive_dimension name"),
                "window_choice": choice,
                "window_groupings": [_check_dim(g, "window grouping") for g in groupings],
            }
            additivity = "semi_additive"
        elif additivity == "semi_additive":
            raise InvalidRequest("semi_additive needs a non_additive_dimension")
        out["additivity"] = additivity
    elif typ == "ratio":
        out["numerator"] = _norm_input(item.get("numerator"))
        out["denominator"] = _norm_input(item.get("denominator"))
        out["additivity"] = "non_additive"
    else:
        inputs = item.get("input_metrics")
        if not isinstance(inputs, (list, tuple)) or not 1 <= len(inputs) <= 5:
            raise InvalidRequest("a derived measure needs 1 to 5 input_metrics")
        norm = [_norm_input(i) for i in inputs]
        aliases = [_alias_of(i) for i in norm]
        if len(set(aliases)) != len(aliases):
            raise InvalidRequest("input_metrics need distinct aliases")
        out["input_metrics"] = norm
        expr = item.get("expr")
        _validate_expr(expr, {a: "numeric" for a in aliases}, allow_star=False)
        out["expr"] = expr.strip()
        out["additivity"] = "non_additive"
    return out


def _normalize_relationship(item: Mapping) -> dict:
    fc = _check_columns(item.get("from_columns"), "from_columns")
    tc = _check_columns(item.get("to_columns"), "to_columns")
    if len(fc) != len(tc):
        raise InvalidRequest("from_columns and to_columns must have the same length")
    card = item.get("cardinality") or "many_to_one"
    if card not in ("many_to_one", "one_to_one"):
        raise InvalidRequest("cardinality must be many_to_one or one_to_one")
    origin = item.get("origin") or "agent"
    if origin not in ("foreign_key", "agent"):
        raise InvalidRequest("origin must be foreign_key or agent")
    mr = item.get("match_rate")
    if mr is not None and (isinstance(mr, bool) or not isinstance(mr, (int, float)) or not 0 <= mr <= 1):
        raise InvalidRequest("match_rate must be a number from 0 to 1")
    return {
        "name": _check_name(item.get("name")),
        "from_relation": _check_relation(item.get("from_relation"), "from_relation"),
        "from_columns": list(fc),
        "to_relation": _check_relation(item.get("to_relation"), "to_relation"),
        "to_columns": list(tc),
        "cardinality": card, "origin": origin, "match_rate": mr,
        "description": _check_text(item.get("description"), "description"),
    }


def _normalize_table(item: Mapping) -> dict:
    kind = item.get("object_kind") or "base"
    if kind not in _KINDS:
        raise InvalidRequest("object_kind must be base, view or matview")
    role = item.get("role")
    if role is not None and role not in _ROLES:
        raise InvalidRequest("role must be fact or dimension")
    pk = item.get("primary_key") or []
    if not isinstance(pk, (list, tuple)) or len(pk) > 6:
        raise InvalidRequest("primary_key takes up to 6 column names")
    for c in pk:
        if not isinstance(c, str) or not _COL_RE.match(c):
            raise InvalidRequest("primary_key has an invalid column name")
    atd = item.get("agg_time_dimension")
    return {
        "name": _check_name(item.get("name")),
        "relation": _check_relation(item.get("relation"), "relation"),
        "object_kind": kind, "role": role,
        "grain": _check_text(item.get("grain"), "grain", 300),
        "primary_key": list(pk),
        "agg_time_dimension": _check_dim(atd, "agg_time_dimension") if atd is not None else None,
        "label": _check_text(item.get("label"), "label", 120),
        "description": _check_text(item.get("description"), "description", 1000),
    }


def _normalize_view(obj: str, item: Mapping) -> dict:
    name = _check_name(item.get("name"))
    sql = item.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise InvalidRequest("a view needs sql")
    tree, refs = _parse_derived(sql)
    return {
        "name": name, "object_kind": "view" if obj == "view" else "matview",
        "sql": tree.sql(dialect="postgres"), "refs": list(refs),
        "description": _check_text(item.get("description"), "description", 1000),
    }


def _normalize_index(item: Mapping) -> dict:
    name = _check_name(item.get("name"), "index name")
    rel = _check_relation(item.get("relation"), "relation")
    if rel.split(".")[0] != "model":
        raise InvalidRequest("indexes are only allowed on relations in schema model")
    return {"name": name, "relation": rel, "columns": list(_check_columns(item.get("columns"), "columns"))}


def normalize_definition(object: str, item: dict) -> dict:  # noqa: A002 - contract name
    """Validate and canonicalise one proposal item into a plain JSON-able dict."""
    if object not in OBJECTS:
        raise InvalidRequest(f"object must be one of {', '.join(OBJECTS)}")
    if not isinstance(item, Mapping):
        raise InvalidRequest("an item must be an object")
    if object == "table":
        return _normalize_table(item)
    if object == "relationship":
        return _normalize_relationship(item)
    if object == "measure":
        return _normalize_measure(item)
    if object in ("view", "materialized_view"):
        return _normalize_view(object, item)
    return _normalize_index(item)


def _filters_from(items: Any) -> tuple:
    out = []
    for f in items or ():
        out.append(Filter(f["dimension"], f["op"], tuple(f.get("values") or ())))
    return tuple(out)


def _input_from(d: Mapping | None) -> MetricInput | None:
    if not d:
        return None
    return MetricInput(d["name"], d.get("alias"), d.get("offset_window"), _filters_from(d.get("filter")))


def measure_from_definition(defn: Mapping) -> Measure:
    nad = defn.get("non_additive_dimension")
    return Measure(
        name=defn["name"], type=defn["type"],
        base_relation=defn.get("base_relation"), agg=defn.get("agg"), expr=defn.get("expr"),
        agg_time_dimension=defn.get("agg_time_dimension"),
        filter=_filters_from(defn.get("filter")),
        numerator=_input_from(defn.get("numerator")),
        denominator=_input_from(defn.get("denominator")),
        input_metrics=tuple(_input_from(i) for i in (defn.get("input_metrics") or ())),
        non_additive_dimension=(NonAdditive(nad["name"], nad.get("window_choice") or "max",
                                            tuple(nad.get("window_groupings") or ())) if nad else None),
        additivity=defn.get("additivity") or "additive",
        unit=defn.get("unit"), format=defn.get("format"),
        label=defn.get("label"), description=defn.get("description"),
    )


def relationship_from_definition(defn: Mapping) -> Relationship:
    return Relationship(defn["name"], defn["from_relation"], tuple(defn["from_columns"]),
                        defn["to_relation"], tuple(defn["to_columns"]),
                        defn.get("cardinality") or "many_to_one")


def table_from_definition(defn: Mapping) -> ModelTable:
    return ModelTable(defn["name"], defn["relation"], defn.get("object_kind") or "base",
                      defn.get("role"), defn.get("grain"), tuple(defn.get("primary_key") or ()),
                      defn.get("agg_time_dimension"))


def model_from_rows(tables: list, relationships: list, measures: list, columns: dict) -> Model:
    """Build a Model from model_tables / model_relationships / model_measures rows.

    Only status 'active' rows are used; row order never matters.
    """
    def active(rows):
        return [r for r in rows if (r.get("status") or "active") == "active"]
    ts = sorted((table_from_definition(r) for r in active(tables)), key=lambda x: x.name)
    rs = sorted((relationship_from_definition(r) for r in active(relationships)), key=lambda x: x.name)
    ms = sorted((measure_from_definition(r) for r in active(measures)), key=lambda x: x.name)
    cols = {rel: dict(c) for rel, c in (columns or {}).items()}
    return Model(tuple(ts), tuple(rs), tuple(ms), cols)


# --------------------------------------------------------------------------- derived SELECT check

_FUNC_OK = (
    exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Coalesce, exp.Nullif, exp.Round, exp.Abs,
    exp.Greatest, exp.Least, exp.DateTrunc, exp.TimestampTrunc, exp.Extract, exp.Lower,
    exp.Upper, exp.Trim, exp.Length, exp.Cast, exp.Case, exp.If, exp.Concat, exp.CurrentDate,
    exp.Floor, exp.Ceil,
    # ── Quick 032: operators and syntax that sqlglot models as Func subclasses (AND/OR are
    # Connector -> Binary -> Func), so they were refused as "function AND()" ──
    exp.And, exp.Or, exp.Exists, exp.Array, exp.Collate, exp.Pow, exp.RegexpLike, exp.RegexpILike,
    exp.JSONExtract, exp.JSONExtractScalar, exp.JSONBExtract, exp.JSONBExtractScalar,
    exp.ArrayContainsAll, exp.ArrayContainedBy, exp.ArrayOverlaps, exp.JSONBContains,
    exp.JSONBContainsAnyTopKeys, exp.JSONBContainsAllTopKeys,
    # ── Quick 032: text cleaning and conversion (pg_catalog built-ins, no side effects; every
    # one's regenerated SQL was checked against the original on PostgreSQL 16) ──
    exp.Substring, exp.SplitPart, exp.StrPosition, exp.Replace, exp.Translate, exp.RegexpReplace,
    exp.RegexpCount, exp.RegexpInstr, exp.Left, exp.Right, exp.Pad, exp.Initcap, exp.ConcatWs,
    exp.StrToDate, exp.StrToTime, exp.UnixToTime, exp.ToNumber, exp.TimeToStr, exp.Date,
    exp.CurrentTimestamp, exp.Trunc, exp.Sign,
)
# ── Quick 032: argument rules that keep the stored (regenerated) SQL identical in meaning and the
# cost bounded. Built-ins sqlglot has no node for parse as Anonymous; only these names, unqualified
# and unquoted, are allowed. ──
_ANON_OK = frozenset({"age", "make_date", "btrim", "regexp_substr", "regexp_match"})
_ANON_REGEX = frozenset({"regexp_substr", "regexp_match"})
_REGEX_NODES = (exp.RegexpLike, exp.RegexpILike, exp.RegexpReplace, exp.RegexpCount, exp.RegexpInstr)
_FORMAT_NODES = (exp.StrToDate, exp.StrToTime, exp.TimeToStr, exp.ToNumber)
_REGEX_MAX_CHARS = 200
_PAD_MAX = 1000
VIEW_FUNCTIONS_HELP = (
    "Views may use: AND/OR/NOT, CASE, CAST, EXISTS, IN, LIKE, ||, count, sum, avg, min, max, "
    "coalesce, nullif, greatest, least, abs, round, trunc, sign, floor, ceil, mod/%, power/^, "
    "lower, upper, initcap, trim, ltrim, rtrim, btrim, length, concat, concat_ws, left, right, lpad, "
    "rpad, substring/substr, split_part, position/strpos, replace, translate, regexp_replace, "
    "regexp_count, regexp_instr, regexp_substr, regexp_match, ~, ~*, to_date, to_timestamp, "
    "to_number, to_char, date, make_date, age, date_trunc, extract/date_part, current_date, now, "
    "+ INTERVAL '1 day', JSON ->, ->>, #>, #>>. Regex patterns are plain '...' literals of at most "
    f"{_REGEX_MAX_CHARS} characters; formats are literals such as 'MM/DD/YYYY'."
)
# ── End Quick 032 ──


def _cte_visible(node, name: str) -> bool:
    """True when an enclosing WITH makes `name` visible to this table node (scope-aware).

    A CTE body sees only the earlier siblings of its own WITH; everything else under
    that WITH's owner sees all of them (16-VERIFICATION gap 2, WR-03).
    """
    parent = node.parent
    while parent is not None:
        w = parent.args.get("with_") or parent.args.get("with")
        if w is not None:
            aliases = [c.alias.lower() for c in w.expressions if c.alias]
            cte = None
            walk = node
            while walk is not None and walk is not parent:
                if walk.parent is w:
                    cte = walk
                    break
                walk = walk.parent
            if cte is not None:
                idx = next((i for i, c in enumerate(w.expressions) if c is cte), 0)
                visible = {c.alias.lower() for c in w.expressions[:idx] if c.alias}
            else:
                visible = set(aliases)
            if name in visible:
                return True
        parent = parent.parent
    return False


def _parse_derived(sql: str):
    if not isinstance(sql, str) or ";" in sql:
        raise InvalidRequest("a view is one SELECT with no semicolon")
    try:
        validate_readonly_sql(sql)
        tree = sqlglot.parse_one(sql, dialect="postgres")
    except SQLGuardrailError as e:
        raise InvalidRequest(f"view SQL refused: {e}") from None
    except Exception as e:  # sqlglot errors
        raise InvalidRequest(f"view SQL could not be parsed: {str(e)[:80]}") from None
    if tree.find(exp.Lock) is not None:
        raise InvalidRequest("row-locking clauses (FOR UPDATE / FOR SHARE) are not allowed in a view")
    seen_ctes: set[str] = set()
    for w in tree.find_all(exp.With):
        if w.args.get("recursive"):
            raise InvalidRequest("recursive CTEs are not allowed in a view")
        for c in w.expressions:
            a = (c.alias or "").lower()
            if a in seen_ctes:
                raise InvalidRequest(f"CTE name {a} is defined twice; use unique names")
            seen_ctes.add(a)
            if a.startswith(("pg_", "ic_")) or a == "information_schema":
                raise InvalidRequest(f"CTE name {a} is reserved")
    # Quick 032: collect every problem in one walk and refuse once, so one retry is enough.
    refs: set[str] = set()
    funcs: list[str] = []
    args: list[str] = []
    problems: list[str] = []
    lits = _Literals(sql)

    def add(bucket: list, text: str) -> None:
        if text not in bucket:
            bucket.append(text)

    for node in tree.walk():
        if isinstance(node, exp.Window):
            add(problems, "window functions are not allowed")
        if isinstance(node, (exp.Func, exp.Anonymous, exp.ByteString, exp.Interval, exp.SimilarTo)):
            bad_func, problem = _node_problem(node, lits)
            if bad_func:
                add(funcs, bad_func)
            if problem:
                add(args, problem)
        if isinstance(node, exp.Table):
            if not node.name:
                add(problems, "this table reference is not allowed")
                continue
            if isinstance(node.this, exp.Func):
                add(problems, "table functions are not allowed")
                continue
            schema = (node.db or "").lower()
            if not schema:
                if not _cte_visible(node, node.name.lower()):
                    add(problems, f"table {node.name} must be schema-qualified")
                continue
            if schema.startswith("pg_") or schema == "information_schema" or schema.startswith("ic_"):
                add(problems, f"schema {schema} is not allowed in a view")
                continue
            refs.add(f"{node.db}.{node.name}")
    head = (["functions not allowed in a view: " + ", ".join(funcs)] if funcs else []) + args
    if head or problems:
        msg = "view refused: " + "; ".join(head + problems) + "."
        if head:
            msg += " " + VIEW_FUNCTIONS_HELP
        raise InvalidRequest(msg)
    return tree, tuple(sorted(refs))


# ── Quick 032: per-node checks for the derived SELECT ──


def _norm_space(s: str) -> str:
    return " ".join(s.lower().split())


class _Literals:
    """String literals exactly as the submitted SQL spells them (tokenized once, lazily)."""

    def __init__(self, sql: str):
        self._sql = sql
        self._exact: set | None = None
        self._loose: set = set()

    def _load(self) -> None:
        toks = sqlglot.tokenize(self._sql, read="postgres")
        self._exact = set()
        for i, t in enumerate(toks):
            if t.token_type.name != "STRING":
                continue
            self._exact.add(t.text)
            self._loose.add(_norm_space(t.text))
            nxt = toks[i + 1].text if i + 1 < len(toks) else ""
            if nxt.isalpha():  # INTERVAL '1' DAY
                self._loose.add(_norm_space(f"{t.text} {nxt}"))

    def exact(self, s: str) -> bool:
        if self._exact is None:
            self._load()
        return s in self._exact

    def loose(self, s: str) -> bool:
        if self._exact is None:
            self._load()
        return _norm_space(s) in self._loose


def _emitted_strings(node) -> list:
    """String literals in the SQL sqlglot will emit for this node (what Postgres will run)."""
    return [t.text for t in sqlglot.tokenize(node.sql(dialect="postgres"), read="postgres")
            if t.token_type.name == "STRING"]


def _func_label(node) -> str:
    if isinstance(node, exp.Anonymous):
        return f"{node.name.lower()}()"
    text = node.sql(dialect="postgres")
    m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(", text)
    if m:
        return f"{m.group(1).lower()}()"
    return f"the operator in {text[:60]}"


def _pattern_problem(p, what: str) -> str | None:
    if isinstance(p, exp.Escape):
        p = p.this
    if isinstance(p, exp.Literal) and p.is_string:
        if len(p.this) > _REGEX_MAX_CHARS:
            return f"the {what} pattern is longer than {_REGEX_MAX_CHARS} characters"
        return None
    return f"the {what} pattern must be a plain '...' string literal of at most {_REGEX_MAX_CHARS} characters"


def _node_problem(node, lits: _Literals) -> tuple:
    """(refused function label or None, other problem text or None) for one node."""
    if isinstance(node, exp.ByteString):
        return None, ("E'...' strings are not allowed (they would be rewritten): write a plain '...' "
                      "string, where backslashes are literal")
    if isinstance(node, exp.Interval):
        emitted = _emitted_strings(node)
        if emitted and not lits.loose(emitted[-1]):
            return None, (f"an INTERVAL literal would run as INTERVAL '{emitted[-1]}', which may not "
                          "mean what you wrote: use plain intervals such as INTERVAL '-1 day'")
        return None, None
    if isinstance(node, exp.SimilarTo):
        return None, _pattern_problem(node.expression, "SIMILAR TO")
    if isinstance(node, exp.Anonymous):
        if isinstance(node.parent, exp.Dot) and node.arg_key == "expression":
            return f"{node.parent.sql(dialect='postgres')[:60]} (schema-qualified)", None
        if not isinstance(node.this, str):
            return f'"{node.name}"() (quoted function name)', None
        name = node.name.lower()
        if name not in _ANON_OK:
            return f"{name}()", None
        if name in _ANON_REGEX:
            args = node.expressions
            return None, _pattern_problem(args[1] if len(args) > 1 else None, name)
        return None, None
    if isinstance(node, exp.TryCast):
        return None, ("TRY_CAST is not Postgres and would run as CAST, which fails on bad rows: guard "
                      "the cast with CASE WHEN col ~ '<pattern>' THEN CAST(col AS ...) END")
    if not isinstance(node, _FUNC_OK):
        return _func_label(node), None
    if isinstance(node, _REGEX_NODES):
        if isinstance(node, (exp.RegexpLike, exp.RegexpILike)) and node.args.get("flag") is not None:
            return None, ("regexp_like with a flags argument would lose the flags: use ~* for a "
                          "case-insensitive match")
        what = {exp.RegexpLike: "~", exp.RegexpILike: "~*"}.get(type(node)) or _func_label(node)[:-2]
        return None, _pattern_problem(node.expression, what)
    if isinstance(node, exp.Substring):
        start = node.args.get("start")
        if isinstance(start, (exp.Literal, exp.RawString, exp.ByteString)) and \
                not (isinstance(start, exp.Literal) and not start.is_string):
            return None, _pattern_problem(start, "substring regex")
        return None, None
    if isinstance(node, exp.Pad):
        n = node.expression
        if not (isinstance(n, exp.Literal) and not n.is_string and n.this.isdigit()
                and int(n.this) <= _PAD_MAX):
            return None, f"lpad/rpad length must be an integer literal from 0 to {_PAD_MAX}"
        return None, None
    if isinstance(node, _FORMAT_NODES):
        label = _func_label(node)
        fmt = node.args.get("format")
        if not (isinstance(fmt, exp.Literal) and fmt.is_string):
            return None, f"{label} format must be a string literal"
        emitted = _emitted_strings(node)
        if not emitted or not lits.exact(emitted[-1]):
            shown = emitted[-1] if emitted else ""
            return None, (f"{label} format would run as '{shown}', which may not mean what you wrote: "
                          f"write the format exactly as '{shown}'")
        return None, None
    if isinstance(node, (exp.JSONExtract, exp.JSONExtractScalar)) and \
            not isinstance(node.expression, exp.JSONPath):
        return None, "-> and ->> need a literal key, such as payload ->> 'id'"
    return None, None
# ── End Quick 032 ──


def derived_select_refs(sql: str) -> tuple:
    """Schema-qualified relations a derived SELECT reads; raises InvalidRequest on risk."""
    return _parse_derived(sql)[1]


# --------------------------------------------------------------------------- join planner

@dataclass(frozen=True)
class _Edge:
    name: str
    to_relation: str
    from_columns: tuple
    to_columns: tuple


@dataclass(frozen=True)
class _Node:
    depth: int
    parent: str | None
    edge: _Edge | None
    ambiguous: bool


def _adjacency(model: Model) -> dict:
    adj: dict[str, list] = {}
    for r in model.relationships:
        adj.setdefault(r.from_relation, []).append(
            _Edge(r.name, r.to_relation, r.from_columns, r.to_columns))
        if r.cardinality == "one_to_one":
            adj.setdefault(r.to_relation, []).append(
                _Edge(r.name, r.from_relation, r.to_columns, r.from_columns))
    return adj


def _tree(model: Model, base: str, max_depth: int = 12) -> dict:
    adj = _adjacency(model)
    nodes = {base: _Node(0, None, None, False)}
    frontier = [base]
    depth = 0
    while frontier and depth < max_depth:
        depth += 1
        cand: dict[str, list] = {}
        for p in sorted(frontier):
            for e in sorted(adj.get(p, ()), key=lambda x: x.name):
                if e.to_relation in nodes:
                    continue
                cand.setdefault(e.to_relation, []).append((p, e))
        new = []
        for to in sorted(cand):
            lst = cand[to]
            if len(lst) > 1:
                nodes[to] = _Node(depth, None, None, True)
            else:
                p, e = lst[0]
                nodes[to] = _Node(depth, p, e, nodes[p].ambiguous)
            new.append(to)
        frontier = new
    return nodes


def plan_joins(model: Model, base_relation: str, targets) -> tuple:
    """Join steps from base_relation to every target relation (tree paths, shared prefixes once)."""
    nodes = _tree(model, base_relation)
    reverse_reach: set | None = None
    needed: dict[str, _Node] = {}
    for target in sorted(set(targets)):
        if target == base_relation:
            continue
        node = nodes.get(target)
        if node is None:
            if reverse_reach is None:
                reverse_reach = _reverse_reach(model, base_relation)
            if target in reverse_reach:
                raise NotModeled(
                    f"{target} sits on the many side of {base_relation}; joining it would repeat rows "
                    f"and inflate the number, so this breakdown is refused")
            raise NotModeled(f"no approved relationship path from {base_relation} to {target}")
        if node.ambiguous:
            raise NotModeled(f"ambiguous path to {target} from {base_relation}: two approved relationships "
                             f"reach it equally fast")
        if node.depth > MAX_HOPS:
            raise NotModeled(f"{target} is {node.depth} hops from {base_relation}; the limit is {MAX_HOPS}")
        cur = target
        while cur != base_relation:
            n = nodes[cur]
            if n.ambiguous:
                raise NotModeled(f"ambiguous path to {cur} from {base_relation}")
            needed[cur] = n
            cur = n.parent
    order = sorted(needed, key=lambda r: (needed[r].depth, needed[r].edge.name))
    alias = {base_relation: "t0"}
    for i, rel in enumerate(order, start=1):
        alias[rel] = f"t{i}"
    steps = []
    for rel in order:
        n = needed[rel]
        steps.append(JoinStep(n.edge.name, alias[n.parent], alias[rel], rel,
                              n.edge.from_columns, n.edge.to_columns))
    return tuple(steps)


def _reverse_reach(model: Model, base: str) -> set:
    """Relations that reach `base` through approved edges (the one-to-many direction)."""
    rev = Model(relationships=tuple(
        Relationship(r.name, r.to_relation, r.to_columns, r.from_relation, r.from_columns, r.cardinality)
        for r in model.relationships))
    return set(_tree(rev, base))


def _alias_map(base: str, steps: tuple) -> dict:
    out = {base: "t0"}
    for s in steps:
        out[s.to_relation] = s.to_alias
    return out


# --------------------------------------------------------------------------- time dimension

def measure_time_dimension(model: Model, measure: Measure) -> str | None:
    if measure.type != "simple":
        return None
    if measure.agg_time_dimension:
        return measure.agg_time_dimension
    for t in model.tables:
        if t.relation == measure.base_relation and t.agg_time_dimension:
            return t.agg_time_dimension
    return None


def supports_time(model: Model, measure: Measure) -> bool:
    if measure.type == "simple":
        return measure_time_dimension(model, measure) is not None
    inputs = _inputs_of(measure)
    if any(i.offset_window for i in inputs):
        return True
    if not inputs:
        return False
    for i in inputs:
        sub = model.measure(i.name)
        if sub is None or not supports_time(model, sub):
            return False
    return True


def canonical_request(model: Model, measure: Measure, grain: str, start: date, end: date) -> Request:
    if supports_time(model, measure):
        return make_request(measure.name, grain=grain, start=start, end=end)
    return make_request(measure.name)


def _inputs_of(m: Measure) -> tuple:
    if m.type == "ratio":
        return tuple(i for i in (m.numerator, m.denominator) if i is not None)
    if m.type == "derived":
        return tuple(m.input_metrics)
    return ()


# --------------------------------------------------------------------------- SQL building blocks

def _tbl(relation: str, alias: str | None = None) -> exp.Table:
    schema, name = relation.split(".", 1)
    t = exp.Table(this=exp.to_identifier(name), db=exp.to_identifier(schema))
    if alias:
        t.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))
    return t


def _cte_tbl(name: str) -> exp.Table:
    return exp.Table(this=exp.to_identifier(name))


def _col(name: str, table: str) -> exp.Column:
    return exp.column(name, table=table)


def _and(conds: list) -> exp.Expression:
    out = conds[0]
    for c in conds[1:]:
        out = exp.And(this=out, expression=c)
    return out


def _is_date(t: str) -> bool:
    return t == "date"


def _is_ts(t: str) -> bool:
    return t.startswith("timestamp")


def _is_num(t: str) -> bool:
    return t.startswith(("integer", "bigint", "smallint", "numeric", "decimal", "real", "double"))


def _literal(value: Any, ctype: str) -> exp.Expression:
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float)):
        return exp.Literal.number(str(value)) if _is_num(ctype) or not ctype else exp.Literal.string(str(value))
    if ctype == "boolean":
        low = value.lower()
        if low in ("true", "false"):
            return exp.Boolean(this=(low == "true"))
        raise InvalidRequest("value is not a boolean")
    if _is_num(ctype):
        try:
            float(value)
        except ValueError:
            raise InvalidRequest("value is not a number") from None
        return exp.Literal.number(value)
    if _is_date(ctype) or _is_ts(ctype):
        if not _ISO_RE.match(value):
            raise InvalidRequest("value is not an ISO date (YYYY-MM-DD)")
        return exp.cast(exp.Literal.string(value), "date")
    return exp.Literal.string(value)


def _filter_cond(f: Filter, col: exp.Column, ctype: str) -> exp.Expression:
    if f.op == "is_null":
        return exp.Is(this=col, expression=exp.Null())
    if f.op == "is_not_null":
        return exp.Not(this=exp.Is(this=col, expression=exp.Null()))
    lits = [_literal(v, ctype) for v in f.values]
    if f.op == "in":
        return exp.In(this=col, expressions=lits)
    if f.op == "not_in":
        return exp.Not(this=exp.In(this=col, expressions=lits))
    cls = {"=": exp.EQ, "!=": exp.NEQ, ">": exp.GT, ">=": exp.GTE, "<": exp.LT, "<=": exp.LTE}[f.op]
    return cls(this=col, expression=lits[0])


def _agg(agg: str, v: exp.Expression) -> exp.Expression:
    if agg == "sum":
        return exp.Sum(this=v)
    if agg == "count":
        return exp.Count(this=v)
    if agg == "count_distinct":
        return exp.Count(this=exp.Distinct(expressions=[v]))
    if agg == "average":
        return exp.Avg(this=v)
    if agg == "min":
        return exp.Min(this=v)
    if agg == "max":
        return exp.Max(this=v)
    if agg == "sum_boolean":
        case = exp.Case(ifs=[exp.If(this=v, true=exp.Literal.number(1))], default=exp.Literal.number(0))
        return exp.Sum(this=case)
    raise InvalidRequest(f"unknown aggregation {agg}")


def _shift_back(d: date, n: int, unit: str) -> date:
    if unit == "day":
        return d - timedelta(days=n)
    if unit == "week":
        return d - timedelta(days=7 * n)
    months = n * {"month": 1, "quarter": 3, "year": 12}[unit]
    total = d.year * 12 + (d.month - 1) - months
    y, m = divmod(total, 12)
    m += 1
    dim = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28, 31, 30, 31, 30,
           31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, dim))


def _interval(n: int, unit: str) -> exp.Interval:
    if unit == "quarter":
        n, unit = n * 3, "month"
    return exp.Interval(this=exp.Literal.string(str(n)), unit=exp.var(unit.upper()))


def _parse_offset(s: str) -> tuple:
    m = _OFFSET_RE.match(s)
    if not m:
        raise NotModeled("offset_window is malformed")
    return int(m.group(1)), m.group(2)


# --------------------------------------------------------------------------- compile: simple

class _Ctx:
    def __init__(self):
        self.joins: list = []
        self.notes: list = []
        self.naive_utc: bool = False
        self.numeric_inputs: bool = False  # Phase 22 tile path only; v1 SQL stays byte-identical

    def note(self, text: str):
        if text not in self.notes:
            self.notes.append(text)

    def join(self, name: str):
        if name not in self.joins:
            self.joins.append(name)


def _col_type(model: Model, dim: str) -> tuple:
    rel, col = _rel_of(dim), _col_of(dim)
    cols = model.columns.get(rel)
    if cols is None or col not in cols:
        raise NotModeled(f"unknown column {dim}")
    return col, cols[col]


def _check_expr_reach(model: Model, name: str, base: str, rels: set) -> None:
    """Quick 054: a column the expression adds up must sit along approved many-to-one links, so
    every base row meets at most one row of it (no fan-out). Its value is still counted once per
    base row, so it suits per-row values (the cost of the roll on each line), not a total that
    belongs to the linked row; the compile note says so."""
    for rel in sorted(rels):
        try:
            plan_joins(model, base, [rel])
        except NotModeled as e:
            if rel not in _tree(model, base) and rel in _reverse_reach(model, base):
                raise NotModeled(
                    f"{name} adds up a column of {rel}, which sits on the many side of {base}: "
                    f"each {base} row would repeat once per {rel} row and inflate the total. "
                    f"Base the measure on {rel} instead, or on a view that joins them") from None
            raise NotModeled(f"{name} adds up a column of {rel}: {e}") from None


def _simple_core(model, m, req, prefix, internal, offset, extra_filters, ctx, bounds=None):
    base = m.base_relation
    if not base:
        raise NotModeled(f"measure {m.name} has no base relation")
    cols = model.columns.get(base)
    if cols is None:
        raise NotModeled(f"no column catalog for {base}")
    if m.expr is None and m.agg == "count":
        vexpr = exp.Star()
    else:
        try:
            vexpr = _validate_expr(m.expr, cols, allow_star=(m.agg == "count"),
                                   linked=True, catalog=model.columns)
        except InvalidRequest as e:
            raise NotModeled(f"measure {m.name} is no longer valid: {e}") from None
    vrels = _expr_rels(vexpr) - {base}
    _check_expr_reach(model, m.name, base, vrels)
    for rel in sorted(vrels):
        ctx.note(f"{m.name} counts {rel} values once per {base} row")
    nad = m.non_additive_dimension
    need_time = bool(req.grain or req.start or req.end or req.window_days or bounds)
    tdim = measure_time_dimension(model, m)
    if (need_time or offset) and not tdim:
        raise NotModeled(f"{m.name} has no time dimension, so grain and date ranges do not apply")
    mfilters = tuple(m.filter) + tuple(extra_filters)
    allf = mfilters + tuple(req.filters)
    dims = list(req.by) + [f.dimension for f in allf]
    if tdim and (need_time or offset):
        dims.append(tdim)
    if nad:
        dims.append(nad.name)
        dims.extend(nad.window_groupings)
    steps = plan_joins(model, base, {_rel_of(d) for d in dims} | vrels)
    for s in steps:
        ctx.join(s.relationship)
    amap = _alias_map(base, steps)

    def ref(dim):
        col, ctype = _col_type(model, dim)
        return _col(col, amap[_rel_of(dim)]), ctype

    keys: list = []  # (internal alias, public name, expression)
    tcol = None
    if tdim and (need_time or offset):
        tcol, ttype = ref(tdim)
        if not (_is_date(ttype) or _is_ts(ttype)):
            raise NotModeled(f"time column {tdim} is not a date or timestamp")
        if _is_naive_ts(ttype):
            if ctx.naive_utc:
                ctx.note(f"time column {tdim} has no time zone; it is read as UTC")
                tcol = exp.AtTimeZone(this=tcol, zone=exp.Literal.string("UTC"))
            else:
                ctx.note(f"time column {tdim} has no time zone; days follow its stored clock")
    if req.grain:
        shifted = tcol
        if offset:
            shifted = exp.Add(this=tcol.copy(), expression=_interval(*offset))
        inner = exp.cast(shifted, "timestamp") if _is_date(ttype) else shifted
        period = exp.cast(exp.func("DATE_TRUNC", exp.Literal.string(req.grain), inner), "date")
        keys.append(("kp", f"metric_time__{req.grain}", period))
    for i, d in enumerate(req.by):
        c, _t = ref(d)
        keys.append((f"k{i}", _out_name(d), c))

    conds = []
    for f in allf:
        c, ctype = ref(f.dimension)
        conds.append(_filter_cond(f, c, ctype))
    if tcol is not None:
        lo = hi = None
        if bounds is not None:
            lo, hi = bounds[0].copy(), bounds[1].copy()
        elif req.window_days:
            ctx.note("window_days uses CURRENT_DATE arithmetic, which is not pushed down to the source")
            # Quick 045: N days ending today, inclusive: today and the N-1 days before it.
            lo = exp.Sub(this=exp.CurrentDate(), expression=exp.Literal.number(req.window_days - 1))
            hi = exp.Add(this=exp.CurrentDate(), expression=exp.Literal.number(1))
            if offset:
                lo = exp.Sub(this=lo, expression=_interval(*offset))
                hi = exp.Sub(this=hi, expression=_interval(*offset))
        else:
            s_, e_ = req.start, req.end
            if offset:
                s_ = _shift_back(s_, *offset) if s_ else None
                e_ = _shift_back(e_, *offset) if e_ else None
            if s_:
                lo = exp.cast(exp.Literal.string(s_.isoformat()), "date")
            if e_:
                hi = exp.cast(exp.Literal.string(e_.isoformat()), "date")
        if lo is not None:
            conds.append(exp.GTE(this=tcol.copy(), expression=lo))
        if hi is not None:
            conds.append(exp.LT(this=tcol.copy(), expression=hi))

    def with_from(sel):
        sel = sel.from_(_tbl(base, "t0"))
        for s in steps:
            on = _and([exp.EQ(this=_col(fc, s.from_alias), expression=_col(tc, s.to_alias))
                       for fc, tc in zip(s.from_columns, s.to_columns)])
            sel = sel.join(_tbl(s.to_relation, s.to_alias), on=on, join_type="left")
        if conds:
            sel = sel.where(_and(conds))
        return sel

    def alias_of(k):
        return k[0] if internal else k[1]

    vname = "v" if internal else m.name
    extra: list = []
    if not nad:
        q = exp.select(*[exp.alias_(k[2], alias_of(k), quoted=True) for k in keys],
                       exp.alias_(_agg(m.agg, _qualify(vexpr, base, amap)), vname, quoted=True))
        q = with_from(q)
        if keys:
            q = q.group_by(*[exp.Literal.number(i + 1) for i in range(len(keys))])
            if not internal:
                q = q.order_by(*[exp.Literal.number(i + 1) for i in range(len(keys))])
        return extra, q

    # semi-additive: rows at the max/min snapshot per period (and window_groupings)
    base_name, pick_name = f"{prefix}base", f"{prefix}pick"
    ncol, _nt = ref(nad.name)
    groups = [ref(g)[0] for g in nad.window_groupings]
    val = exp.Literal.number(1) if isinstance(vexpr, exp.Star) else _qualify(vexpr, base, amap)
    base_cols = [exp.alias_(k[2], k[0], quoted=True) for k in keys]
    base_cols.append(exp.alias_(ncol, "nad", quoted=True))
    base_cols.extend(exp.alias_(g, f"g{i}", quoted=True) for i, g in enumerate(groups))
    base_cols.append(exp.alias_(val, "val", quoted=True))
    extra.append((base_name, with_from(exp.select(*base_cols))))
    pick_keys = ([exp.column("kp", table=base_name)] if req.grain else []) + \
        [exp.column(f"g{i}", table=base_name) for i in range(len(groups))]
    pick_sel = exp.select(*[exp.alias_(k.copy(), k.name, quoted=True) for k in pick_keys],
                          exp.alias_(exp.Max(this=exp.column("nad", table=base_name))
                                     if nad.window_choice == "max"
                                     else exp.Min(this=exp.column("nad", table=base_name)), "nad", quoted=True)
                          ).from_(_cte_tbl(base_name))
    if pick_keys:
        pick_sel = pick_sel.group_by(*[k.copy() for k in pick_keys])
    extra.append((pick_name, pick_sel))
    on = []
    if req.grain:
        on.append(exp.NullSafeEQ(this=_col("kp", base_name), expression=_col("kp", pick_name)))
    for i in range(len(groups)):
        on.append(exp.NullSafeEQ(this=_col(f"g{i}", base_name), expression=_col(f"g{i}", pick_name)))
    on.append(exp.EQ(this=_col("nad", base_name), expression=_col("nad", pick_name)))
    outer_keys = [exp.alias_(_col(k[0], base_name), alias_of(k), quoted=True) for k in keys]
    q = exp.select(*outer_keys,
                   exp.alias_(_agg(m.agg, _col("val", base_name)), vname, quoted=True))
    q = q.from_(_cte_tbl(base_name)).join(_cte_tbl(pick_name), on=_and(on), join_type="inner")
    if keys:
        q = q.group_by(*[exp.Literal.number(i + 1) for i in range(len(keys))])
        if not internal:
            q = q.order_by(*[exp.Literal.number(i + 1) for i in range(len(keys))])
    return extra, q


# --------------------------------------------------------------------------- compile: composed

def _compose(model, m, req, prefix, internal, offset, extra_filters, ctx, depth=0):
    if m.type == "simple":
        return _simple_core(model, m, req, prefix, internal, offset, extra_filters, ctx)
    inputs = _inputs_of(m)
    if m.type == "ratio" and len(inputs) != 2:
        raise NotModeled(f"ratio {m.name} needs a numerator and a denominator")
    if not inputs:
        raise NotModeled(f"derived measure {m.name} has no inputs")
    ctes: list = []
    names: list = []
    for i, mi in enumerate(inputs):
        inm = model.measure(mi.name)
        if inm is None:
            raise NotModeled(f"{m.name} needs measure {mi.name}, which is not approved")
        if m.type == "ratio" and inm.type != "simple":
            raise NotModeled(f"ratio {m.name} can only combine simple measures ({mi.name} is {inm.type})")
        if m.type == "derived" and inm.type == "derived":
            raise NotModeled(f"derived {m.name} cannot build on derived measure {mi.name} (one level only)")
        in_offset = _parse_offset(mi.offset_window) if mi.offset_window else None
        if in_offset and offset:
            raise NotModeled(f"{m.name} would stack two period offsets")
        in_offset = in_offset or offset
        if in_offset and not req.grain:
            raise NotModeled(f"{m.name} compares periods and needs a grain (day, week, month, quarter or year)")
        name = f"{prefix}m{i}"
        sub_ctes, sel = _compose(model, inm, req, f"{name}_", True, in_offset,
                                 tuple(extra_filters) + tuple(mi.filter), ctx, depth + 1)
        ctes.extend(sub_ctes)
        ctes.append((name, sel))
        names.append(name)
    nkeys = (1 if req.grain else 0) + len(req.by)
    key_in = (["kp"] if req.grain else []) + [f"k{i}" for i in range(len(req.by))]
    key_pub = ([f"metric_time__{req.grain}"] if req.grain else []) + [_out_name(d) for d in req.by]
    if nkeys == 0:
        src = exp.select().from_(_cte_tbl(names[0]))
        for n in names[1:]:
            src = src.join(_cte_tbl(n), join_type="cross")
        key_tables = []
    else:
        keys_name = f"{prefix}keys"
        parts = [exp.select(*[_col(k, n) for k in key_in]).from_(_cte_tbl(n)) for n in names]
        spine = parts[0]
        for p in parts[1:]:
            spine = exp.union(spine, p, distinct=True)
        ctes.append((keys_name, spine))
        src = exp.select().from_(_cte_tbl(keys_name))
        for n in names:
            on = _and([exp.NullSafeEQ(this=_col(k, n), expression=_col(k, keys_name)) for k in key_in])
            src = src.join(_cte_tbl(n), on=on, join_type="left")
        key_tables = [keys_name] * nkeys
    vals = {n: _col("v", n) for n in names}
    if m.type == "ratio":
        value = exp.Div(this=exp.cast(vals[names[0]], "numeric"),
                        expression=exp.Nullif(this=vals[names[1]], expression=exp.Literal.number(0)))
    else:
        amap = {}
        for mi, n in zip(inputs, names):
            amap[mi.alias or mi.name] = n
        try:
            tree = _validate_expr(m.expr, {a: "numeric" for a in amap}, allow_star=False)
        except InvalidRequest as e:
            raise InvalidRequest(f"derived expression of {m.name} is invalid: {e}") from None

        def fn(node):
            if isinstance(node, exp.Column):
                col = _col("v", amap[node.name])
                return exp.cast(col, "numeric") if ctx.numeric_inputs else col
            return node
        value = tree.transform(fn)
    sel_cols = []
    for idx in range(nkeys):
        alias = key_in[idx] if internal else key_pub[idx]
        sel_cols.append(exp.alias_(_col(key_in[idx], key_tables[idx]), alias, quoted=True))
    sel_cols.append(exp.alias_(value, "v" if internal else m.name, quoted=True))
    out = _set_select(src, sel_cols)
    if nkeys and not internal:
        out = out.order_by(*[exp.Literal.number(i + 1) for i in range(nkeys)])
    return ctes, out


def _set_select(sel: exp.Select, cols: list) -> exp.Select:
    sel.set("expressions", cols)
    return sel


def compile_measure(model: Model, request: Request, *, naive_utc: bool = False,
                    numeric_inputs: bool = False) -> Compiled:
    m = model.measure(request.measure)
    if m is None:
        raise NotModeled(f"measure {request.measure} is not modeled")
    req = make_request(request.measure, by=request.by, grain=request.grain, filters=request.filters,
                       start=request.start, end=request.end, window_days=request.window_days)
    if (req.grain or req.start or req.end or req.window_days) and not supports_time(model, m):
        raise NotModeled(f"{m.name} has no time dimension, so grain and date ranges do not apply")
    ctx = _Ctx()
    ctx.naive_utc = naive_utc
    ctx.numeric_inputs = numeric_inputs
    ctes, sel = _compose(model, m, req, "", False, None, (), ctx)
    for name, body in ctes:
        sel = sel.with_(name, as_=body)
    sql = sel.sql(dialect="postgres", identify=True)
    try:
        validate_readonly_sql(sql)
    except SQLGuardrailError as e:  # would be a compiler bug
        raise InvalidRequest(f"compiled SQL was refused by the guardrails: {e}") from None
    columns = ((f"metric_time__{req.grain}",) if req.grain else ()) + \
        tuple(_out_name(d) for d in req.by) + (m.name,)
    return Compiled(sql, columns, m.name, tuple(ctx.joins), tuple(ctx.notes))


# ---- Phase 22: date control support ----

DASH_PRESETS = ("today", "yesterday", "last_7_days", "last_30_days", "last_90_days",
                "this_month", "last_month", "this_quarter", "this_fy")
DASH_PERIODS = ("current", "previous", "last_year")
_ROLLING_DAYS = {"last_7_days": 7, "last_30_days": 30, "last_90_days": 90}
_TODAY = "(SELECT d FROM _today)"


def _is_naive_ts(t: str) -> bool:
    """A timestamp column that carries no time zone (timestamp without time zone)."""
    t = (t or "").lower()
    if t == "timestamptz" or "with time zone" in t:
        return False
    return t.startswith("timestamp")


def _preset_sql(preset: str, period: str, fy_start_month: int) -> tuple:
    """(from, to) inclusive bounds as SQL text over the one _today CTE (C-04)."""
    if preset not in DASH_PRESETS or period not in DASH_PERIODS:
        raise InvalidRequest("unknown preset or period")
    if isinstance(fy_start_month, bool) or not isinstance(fy_start_month, int) or not 1 <= fy_start_month <= 12:
        raise InvalidRequest("fy_start_month must be 1 to 12")
    t = _TODAY
    ts = f"CAST({t} AS TIMESTAMP)"
    fy = str(fy_start_month)
    if preset == "today":
        cf, ct, n = t, t, 1
    elif preset == "yesterday":
        cf, ct, n = f"{t} - 1", f"{t} - 1", 1
    elif preset in _ROLLING_DAYS:
        n = _ROLLING_DAYS[preset]
        cf, ct = f"{t} - {n - 1}", t
    elif preset == "this_month":
        cf, ct, n = f"CAST(DATE_TRUNC('month', {ts}) AS DATE)", t, None
    elif preset == "last_month":
        cf = f"CAST(DATE_TRUNC('month', {ts}) - INTERVAL '1 month' AS DATE)"
        ct, n = f"CAST(DATE_TRUNC('month', {ts}) AS DATE) - 1", None
    elif preset == "this_quarter":
        cf, ct, n = f"CAST(DATE_TRUNC('quarter', {ts}) AS DATE)", t, None
    else:  # this_fy
        cf = (f"MAKE_DATE(CAST(EXTRACT(YEAR FROM {t}) AS INT) - "
              f"CASE WHEN EXTRACT(MONTH FROM {t}) < {fy} THEN 1 ELSE 0 END, {fy}, 1)")
        ct, n = t, None
    if period == "current":
        return cf, ct
    if period == "last_year":
        return f"CAST(({cf}) - INTERVAL '1 year' AS DATE)", f"CAST(({ct}) - INTERVAL '1 year' AS DATE)"
    # previous
    if n is not None:
        return f"({cf}) - {n}", f"({ct}) - {n}"
    if preset == "last_month":
        return f"CAST(({cf}) - INTERVAL '1 month' AS DATE)", f"({cf}) - 1"
    months = {"this_month": 1, "this_quarter": 3, "this_fy": 12}[preset]
    pf = f"CAST(({cf}) - INTERVAL '{months} month' AS DATE)"
    return pf, f"LEAST({pf} + (({ct}) - ({cf})), ({cf}) - 1)"


def _preset_range_exprs(preset: str, period: str, fy_start_month: int) -> tuple:
    lo, hi = _preset_sql(preset, period, fy_start_month)
    return (sqlglot.parse_one(lo, read="postgres"), sqlglot.parse_one(hi, read="postgres"))


def _preset_bounds(preset: str, period: str, fy_start_month: int) -> tuple:
    """(lo, hi_exclusive) expressions for a preset range: from, to + 1."""
    lo, hi = _preset_range_exprs(preset, period, fy_start_month)
    return lo, exp.Add(this=exp.Paren(this=hi), expression=exp.Literal.number(1))


def _today_cte() -> exp.Expression:
    return exp.select(exp.alias_(exp.CurrentDate(), "d", quoted=True))


def preset_bounds_sql(presets, fy_start_month: int = 4) -> str:
    """One SELECT of (preset, period, from_date, to_date) over a single _today CTE."""
    presets = tuple(presets)
    if not presets or any(p not in DASH_PRESETS for p in presets):
        raise InvalidRequest("unknown preset")
    branches = []
    for p in presets:
        for per in DASH_PERIODS:
            lo, hi = _preset_range_exprs(p, per, fy_start_month)
            branches.append(exp.select(
                exp.alias_(exp.Literal.string(p), "preset", quoted=True),
                exp.alias_(exp.Literal.string(per), "period", quoted=True),
                exp.alias_(lo, "from_date", quoted=True),
                exp.alias_(hi, "to_date", quoted=True)))
    q = branches[0]
    for b in branches[1:]:
        q = exp.union(q, b, distinct=False)
    q = q.with_("_today", as_=_today_cte())
    return q.sql(dialect="postgres", identify=True)


# ---- Phase 22: kinds, formula tree, multi-measure tiles, preset values ----

_AGG_KIND = {"sum": "sum", "count": "sum", "sum_boolean": "sum", "min": "min", "max": "max",
             "average": "avg", "count_distinct": "distinct"}
_FORMULA_OPS = {exp.Add: "+", exp.Sub: "-", exp.Mul: "*", exp.Div: "/"}
FORMULA_MAX_NODES = 50
FORMULA_MAX_DEPTH = 12
TILE_MAX_MEASURES = 6
_PART_NAME_MAX = 63


class _NotAFormula(Exception):
    pass


def formula_tree(m) -> dict | None:
    """C-06 tree of a derived measure's approved expression, or None when it is not plain arithmetic.

    Accepts a Measure or, for convenience, the expression text itself (then any column is an input).
    """
    if isinstance(m, str):
        expr, aliases = m, None
    else:
        if m.type != "derived" or not m.expr:
            return None
        expr = m.expr
        aliases = {i.alias or i.name for i in m.input_metrics}
    try:
        tree = sqlglot.parse_one(expr, dialect="postgres")
    except Exception:
        return None
    count = [0]

    def node() -> None:
        count[0] += 1
        if count[0] > FORMULA_MAX_NODES:
            raise _NotAFormula

    def walk(n, depth):
        while isinstance(n, exp.Paren):
            n = n.this
        if depth > FORMULA_MAX_DEPTH:
            raise _NotAFormula
        node()
        if isinstance(n, exp.Literal) and not n.is_string:
            txt = n.name
            try:
                return {"num": int(txt)} if txt.isdigit() else {"num": float(txt)}
            except ValueError:
                raise _NotAFormula from None
        if isinstance(n, exp.Column):
            if n.table or n.args.get("db") or n.args.get("catalog"):
                raise _NotAFormula
            if aliases is not None and n.name not in aliases:
                raise _NotAFormula
            return {"in": n.name}
        if isinstance(n, exp.Neg):
            node()  # the synthetic zero
            return {"op": "-", "a": {"num": 0}, "b": walk(n.this, depth + 1)}
        for cls, op in _FORMULA_OPS.items():
            if type(n) is cls:
                return {"op": op, "a": walk(n.this, depth + 1), "b": walk(n.expression, depth + 1)}
        raise _NotAFormula

    try:
        return walk(tree, 1)
    except _NotAFormula:
        return None


def measure_kind(model: Model, m: Measure) -> str:
    """C-03 kind, decided from the definition and never from the additivity default."""
    if not supports_time(model, m):
        return "now"
    nad = m.non_additive_dimension
    if nad is not None:
        return "closing" if nad.window_choice == "max" else "opening"
    if m.type == "simple":
        if m.additivity == "non_additive":
            return "fixed"
        return _AGG_KIND.get(m.agg or "", "fixed")
    inputs = _inputs_of(m)
    if not inputs or any(i.offset_window for i in inputs):
        return "fixed"
    for i in inputs:
        inm = model.measure(i.name)
        if inm is None or inm.type != "simple" or measure_kind(model, inm) != "sum":
            return "fixed"
    if m.type == "ratio":
        return "ratio" if len(inputs) == 2 else "fixed"
    return "formula" if formula_tree(m) is not None else "fixed"


@dataclass(frozen=True)
class TileCompiled:
    sql: str
    columns: tuple
    metas: tuple
    notes: tuple
    joins: tuple


def _date_basis(model: Model, m: Measure) -> str | None:
    cur = m
    for _ in range(3):
        tdim = measure_time_dimension(model, cur)
        if tdim:
            return _col_of(tdim).replace("_", " ")
        inputs = _inputs_of(cur)
        sub = model.measure(inputs[0].name) if inputs else None
        if sub is None:
            return None
        cur = sub
    return None


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _tile_plan(model: Model, names, components: bool) -> tuple:
    """Per measure: kind, public columns with the measure to compile for each, and the meta dict."""
    counter = [0]
    box = [model]

    def synth(base: Measure) -> str:
        name = f"_ic_p{counter[0]}"
        counter[0] += 1
        box[0] = box[0].with_measure(replace(base, name=name))
        return name

    plan = []
    for n in names:
        m = model.measure(n)
        if m is None:
            raise NotModeled(f"measure {n} is not modeled")
        plan.append((n, m, measure_kind(model, m)))
    kinds = [k for _n, _m, k in plan]
    if "now" in kinds and any(k != "now" for k in kinds):
        bad = next(n for n, _m, k in plan if k == "now")
        raise NotModeled(f"measure {bad} has no date; put it in its own tile")
    out = []
    for n, m, kind in plan:
        if components and kind == "fixed":
            raise NotModeled(f"{n} compares periods or is not additive, so it cannot follow the date control")
        cols = [(n, n)]  # (public column, measure name to compile)
        parts = None
        if components and kind in ("avg", "ratio", "formula"):
            parts = {}
            if kind == "avg":
                for role, agg in (("sum", "sum"), ("count", "count")):
                    parts[role] = f"{n}__{role}"
                    cols.append((parts[role], synth(replace(m, agg=agg))))
            else:
                for idx, mi in enumerate(_inputs_of(m)):
                    inm = model.measure(mi.name)
                    if kind == "ratio":
                        role = ("num", "den")[idx]
                        col = f"{n}__{role}"
                    else:
                        role = mi.alias or mi.name
                        col = f"{n}__in_{role}"
                    parts[role] = col
                    cols.append((col, synth(replace(inm, filter=tuple(inm.filter) + tuple(mi.filter)))))
            for col, _src in cols[1:]:
                if len(col) > _PART_NAME_MAX:
                    raise InvalidRequest(f"measure {n} is too long to store its parts")
        nad = m.non_additive_dimension
        meta = {
            "column": n, "kind": kind, "parts": parts,
            "formula": formula_tree(m) if kind == "formula" else None,
            "choice": ("closing" if nad.window_choice == "max" else "opening") if nad else None,
            "label": m.label or n, "description": m.description or None,
            "date_basis": _date_basis(model, m) if kind != "now" else None,
        }
        out.append((n, kind, cols, meta))
    return box[0], out


def compile_tile(model: Model, names, *, by=(), grain=None, filters=(), window_days=None, start=None,
                 end=None, components: bool = False, naive_utc: bool = False, top: int | None = None,
                 other: bool = False) -> TileCompiled:
    """Up to 6 measures (plus component parts) as ONE read-only SELECT on a key spine."""
    names = tuple(names)
    if not 1 <= len(names) <= TILE_MAX_MEASURES:
        raise InvalidRequest(f"a tile takes 1 to {TILE_MAX_MEASURES} measures")
    if len(set(names)) != len(names):
        raise InvalidRequest("a measure can only be listed once in a tile")
    if top is not None and (isinstance(top, bool) or not isinstance(top, int) or not 1 <= top <= 100):
        raise InvalidRequest("top must be 1 to 100")
    if other and top is None:
        raise InvalidRequest("other needs top")
    if other and (grain is not None or len(tuple(by)) != 1):
        raise InvalidRequest("other needs exactly one by dimension and no grain")
    model2, plan = _tile_plan(model, names, components)
    if other:
        for n, kind, _cols, _meta in plan:
            if kind != "sum":
                raise InvalidRequest(f"other needs measures that add up; {n} does not")
    notes: list = []
    joins: list = []
    ctes: list = []  # (cte name, parsed select)
    part_tables: list = []  # (cte name, value column inside the cte, public column)
    keys_pub: list = []
    for _n, _kind, cols, _meta in plan:
        for public, src in cols:
            req = make_request(src, by=by, grain=grain, filters=filters, start=start, end=end,
                               window_days=window_days)
            c = compile_measure(model2, req, naive_utc=naive_utc, numeric_inputs=True)
            keys_pub = list(c.columns[:-1])
            cte = f"p{len(ctes)}"
            ctes.append((cte, sqlglot.parse_one(c.sql, read="postgres")))
            part_tables.append((cte, c.columns[-1], public))
            notes.extend(x for x in c.notes if x not in notes)
            joins.extend(x for x in c.joins if x not in joins)
    if keys_pub:
        spine = None
        for cte, _v, _p in part_tables:
            part = exp.select(*[_col(k, cte) for k in keys_pub]).from_(_cte_tbl(cte))
            spine = part if spine is None else exp.union(spine, part, distinct=True)
        ctes.append(("_keys", spine))
        src_sel = exp.select().from_(_cte_tbl("_keys"))
        for cte, _v, _p in part_tables:
            on = _and([exp.NullSafeEQ(this=_col(k, cte), expression=_col(k, "_keys")) for k in keys_pub])
            src_sel = src_sel.join(_cte_tbl(cte), on=on, join_type="left")
        sel_cols = [exp.alias_(_col(k, "_keys"), k, quoted=True) for k in keys_pub]
    else:
        src_sel = exp.select().from_(_cte_tbl(part_tables[0][0]))
        for cte, _v, _p in part_tables[1:]:
            src_sel = src_sel.join(_cte_tbl(cte), join_type="cross")
        sel_cols = []
    for cte, v, public in part_tables:
        sel_cols.append(exp.alias_(_col(v, cte), public, quoted=True))
    sel = _set_select(src_sel, sel_cols)
    value_cols = [p for _c, _v, p in part_tables]
    columns = tuple(keys_pub) + tuple(value_cols)
    if top is not None and not other:
        sel = sel.order_by(exp.Ordered(this=exp.column(value_cols[0], quoted=True), desc=True, nulls_first=False))
        sel = sel.limit(top)
    elif keys_pub and not other:
        sel = sel.order_by(*[exp.Literal.number(i + 1) for i in range(len(keys_pub))])
    for name, body in ctes:
        sel = sel.with_(name, as_=body)
    sql = sel.sql(dialect="postgres", identify=True)
    if other:
        sql = _wrap_other(sql, keys_pub[0], value_cols, top)
    try:
        validate_readonly_sql(sql)
    except SQLGuardrailError as e:  # would be a compiler bug
        raise InvalidRequest(f"compiled SQL was refused by the guardrails: {e}") from None
    return TileCompiled(sql, columns, tuple(p[3] for p in plan), tuple(notes), tuple(joins))


def _wrap_other(spine_sql: str, by_col: str, value_cols: list, top: int) -> str:
    """C-07: the top N rows by the first measure plus one 'Other' row summing the rest."""
    b = _q(by_col)
    vals = ", ".join(_q(v) for v in value_cols)
    sums = ", ".join(f"SUM({_q(v)})" for v in value_cols)
    n = int(top)
    return (
        f'WITH "_t" AS ({spine_sql}), '
        f'"_r" AS (SELECT "_t".*, ROW_NUMBER() OVER (ORDER BY {_q(value_cols[0])} DESC NULLS LAST, {b} ASC) AS "_rn" '
        f'FROM "_t") '
        f'SELECT {b}, {vals} FROM ('
        f'SELECT CAST({b} AS TEXT) AS {b}, {vals}, 0 AS "_o", "_rn" FROM "_r" WHERE "_rn" <= {n} '
        f'UNION ALL '
        f"SELECT 'Other' AS {b}, {sums}, 1 AS \"_o\", {n + 1} AS \"_rn\" FROM \"_r\" WHERE \"_rn\" > {n} "
        f'HAVING COUNT(*) > 0) AS "_u" ORDER BY "_o", "_rn"'
    )


def compile_presets(model: Model, name: str, *, filters=(), presets, fy_start_month: int = 4,
                    naive_utc: bool = False) -> Compiled:
    """C-05: one row per (preset, period) with the count-distinct value, bounds from one _today CTE."""
    m = model.measure(name)
    if m is None:
        raise NotModeled(f"measure {name} is not modeled")
    if measure_kind(model, m) != "distinct":
        raise InvalidRequest(f"{name} is not a count-distinct measure, so it needs no presets query")
    presets = tuple(presets)
    if not presets or any(p not in DASH_PRESETS for p in presets):
        raise InvalidRequest("unknown preset")
    req = make_request(name, filters=filters)
    ctx = _Ctx()
    ctx.naive_utc = naive_utc
    extra_ctes: list = [("_today", _today_cte())]
    branches = []
    # C-05 deviation: TO_CHAR, not ::text, so a non-ISO session DateStyle cannot change the format
    today_text = exp.Anonymous(
        this="TO_CHAR",
        expressions=[exp.Subquery(this=exp.select(exp.column("d")).from_(_cte_tbl("_today"))),
                     exp.Literal.string("YYYY-MM-DD")])
    for p in presets:
        for per in DASH_PERIODS:
            bounds = _preset_bounds(p, per, fy_start_month)
            extra, q = _simple_core(model, m, req, f"b{len(branches)}_", True, None, (), ctx, bounds=bounds)
            extra_ctes.extend(extra)
            branches.append(exp.select(
                exp.alias_(exp.Literal.string(p), "preset", quoted=True),
                exp.alias_(exp.Literal.string(per), "period", quoted=True),
                exp.alias_(exp.Subquery(this=q), "value", quoted=True),
                exp.alias_(today_text.copy(), "today", quoted=True)))
    out = branches[0]
    for b in branches[1:]:
        out = exp.union(out, b, distinct=False)
    for cname, body in extra_ctes:
        out = out.with_(cname, as_=body)
    sql = out.sql(dialect="postgres", identify=True)
    try:
        validate_readonly_sql(sql)
    except SQLGuardrailError as e:
        raise InvalidRequest(f"compiled SQL was refused by the guardrails: {e}") from None
    return Compiled(sql, ("preset", "period", "value", "today"), name, tuple(ctx.joins), tuple(ctx.notes))

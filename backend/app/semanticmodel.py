"""Semantic model store and the model_change executor (Phase 16, MODEL-01 / MODEL-02 / MODEL-03).

The ONLY writer of model_tables / model_relationships / model_measures / model_indexes and
the only caller of the whlink Phase 16 DDL functions. It is called solely by the
``model_change`` approval executor in ``actionsvc._execute`` (an approved ``actions`` row;
``approve()`` already claimed it single-winner). Tools only ever write ``actions`` rows.

The payload is untrusted: ``apply`` re-validates the definition with
``modelcompiler.normalize_definition``, recompiles measures from the definition (never
from any SQL in the payload), re-checks that relations, columns and inputs exist, recomputes
relationship statistics through ``guardrails``, and only then writes. Rows are upserted by
name and never deleted; ``retire`` sets ``status = 'retired'``.

Leaf module: must not import the agent graph.
"""

import json
import logging
from datetime import date, timedelta
from decimal import Decimal

from . import modelcompiler, schemadiscovery, warehouse, whlink
from .db import get_pool
from .guardrails import run_readonly_sql

log = logging.getLogger("semanticmodel")

# A fixed literal 30-day range for the approval-time compile check: nothing is executed, so
# apply stays clock-free and deterministic.
CHECK_START = date(2026, 1, 1)
CHECK_END = date(2026, 1, 31)

OPS = ("create", "update", "retire", "refresh")
_WAREHOUSE_OBJECTS = ("view", "materialized_view", "index")
_NAME_RE = modelcompiler.NAME_RE
_JSON_COLS = ("filter", "numerator", "denominator", "input_metrics", "non_additive_dimension", "meta")
_INTERNAL = ("id", "status", "action_id", "approved_at", "updated_at", "meta")
_DEFS = {
    "table": ("model_tables", "base"),
    "view": ("model_tables", "view"),
    "materialized_view": ("model_tables", "matview"),
    "relationship": ("model_relationships", None),
    "measure": ("model_measures", None),
    "index": ("model_indexes", None),
}


class _Refuse(Exception):
    """An approval-time check failed. The message is fixed text written in this module."""


def _decode(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _row(r) -> dict:
    d = dict(r)
    for k in _JSON_COLS:
        if k in d:
            d[k] = _decode(d[k])
    for k in ("primary_key", "from_columns", "to_columns", "columns"):
        if k in d and d[k] is not None:
            d[k] = list(d[k])
    return d


# ── Loading ────────────────────────────────────────────────────────────────
async def load_rows(include_retired: bool = False) -> dict:
    pool = await get_pool()
    where = "" if include_retired else " WHERE status = 'active'"
    out = {}
    for key, table in (("tables", "model_tables"), ("relationships", "model_relationships"),
                       ("measures", "model_measures"), ("indexes", "model_indexes")):
        rows = await pool.fetch(f"SELECT * FROM {table}{where} ORDER BY name")
        out[key] = [_row(r) for r in rows]
    return out


async def catalog_columns() -> dict:
    """{"schema.table": {column: type}} from the schema catalog, plus active model views."""
    snap = await schemadiscovery.introspect()
    cols: dict = {}
    for t in snap.get("tables") or []:
        cols[f"{t['schema']}.{t['table']}"] = {c["name"]: c["type"] for c in t.get("columns") or []}
    pool = await get_pool()
    # Phase 17 (CONS-01): active registered sources, so measures over src_<name> tables compile
    if warehouse.warehouse_mode():
        try:
            srcs = await pool.fetch(
                "SELECT name FROM sources WHERE status = 'active' AND name <> 'business_db' "
                "AND warehouse_schema IS NOT NULL")
        except Exception as e:
            log.warning("registered sources unreadable: %s", type(e).__name__)
            srcs = []
        for sr in srcs:
            try:
                ssnap = await schemadiscovery.introspect(sr["name"])
            except Exception as e:
                log.warning("source catalog skipped: %s", type(e).__name__)
                continue
            for t in ssnap.get("tables") or []:
                cols[f"{t['schema']}.{t['table']}"] = {c["name"]: c["type"] for c in t.get("columns") or []}
    rows = await pool.fetch(
        "SELECT name, relation, meta FROM model_tables "
        "WHERE object_kind IN ('view', 'matview') AND status = 'active'")
    for r in rows:
        meta = _decode(r["meta"]) or {}
        c = meta.get("columns") if isinstance(meta, dict) else None
        if c:
            cols[f"model.{r['name']}"] = dict(c)
    return cols


async def load_model() -> modelcompiler.Model:
    rows = await load_rows()
    return modelcompiler.model_from_rows(
        rows["tables"], rows["relationships"], rows["measures"], await catalog_columns())


async def current_definition(object: str, name: str) -> dict | None:  # noqa: A002 - contract name
    """The active row for that object and name as a plain dict, or None."""
    spec = _DEFS.get(object)
    if spec is None:
        return None
    table, kind = spec
    pool = await get_pool()
    if kind is not None:
        r = await pool.fetchrow(
            f"SELECT * FROM {table} WHERE name = $1 AND object_kind = $2 AND status = 'active'",
            name, kind)
    else:
        r = await pool.fetchrow(f"SELECT * FROM {table} WHERE name = $1 AND status = 'active'", name)
    if r is None:
        return None
    d = _row(r)
    return {k: v for k, v in d.items() if k not in _INTERNAL}


async def warehouse_objects() -> dict:
    """relname -> relkind for schema model in the warehouse; {} outside warehouse mode or on error."""
    if not warehouse.warehouse_mode():
        return {}
    try:
        rows = await run_readonly_sql(
            "SELECT c.relname AS relname, c.relkind::text AS relkind FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'model'")
        return {r["relname"]: r["relkind"] for r in rows}
    except Exception as e:
        log.warning("model objects listing failed: %s", type(e).__name__)
        return {}


# ── Relationship statistics (proposal evidence and approval share this) ───────
def _q(ident: str) -> str:
    return '"' + str(ident).replace('"', '""') + '"'


def _qrel(rel: str) -> str:
    schema, table = rel.split(".", 1)
    return f"{_q(schema)}.{_q(table)}"


async def relationship_stats(defn: dict) -> dict:
    fc, tc = list(defn["from_columns"]), list(defn["to_columns"])
    frel, trel = _qrel(defn["from_relation"]), _qrel(defn["to_relation"])
    to_list = ", ".join(_q(c) for c in tc)
    on = " AND ".join(f"f.{_q(a)} = t.{_q(b)}" for a, b in zip(fc, tc))
    match_sql = (
        f"WITH t AS (SELECT DISTINCT {to_list} FROM {trel}) "
        f"SELECT count(*) AS from_rows, count(f.{_q(fc[0])}) AS from_nonnull, "
        f"count(t.{_q(tc[0])}) AS matched FROM {frel} AS f LEFT JOIN t ON {on}")
    uniq_sql = (
        f"SELECT count(*) - count(DISTINCT ({to_list})) AS duplicate_keys, "
        f"count(*) FILTER (WHERE {_q(tc[0])} IS NULL) AS null_keys FROM {trel}")
    from_list = ", ".join(_q(c) for c in fc)
    from_nn = " AND ".join(f"{_q(c)} IS NOT NULL" for c in fc)
    from_uniq_sql = (
        f"SELECT count(*) - count(DISTINCT ({from_list})) AS duplicate_keys "
        f"FROM {frel} WHERE {from_nn}")
    m = (await run_readonly_sql(match_sql))[0]
    u = (await run_readonly_sql(uniq_sql))[0]
    fu = (await run_readonly_sql(from_uniq_sql))[0] if defn.get("cardinality") == "one_to_one" else {}
    nonnull, matched = int(m["from_nonnull"]), int(m["matched"])
    return {
        "sql": match_sql,
        "from_rows": int(m["from_rows"]), "from_nonnull": nonnull, "matched": matched,
        "match_rate": round(matched / nonnull, 4) if nonnull else None,
        "duplicate_keys": int(u["duplicate_keys"] or 0), "null_keys": int(u["null_keys"] or 0),
        "from_duplicate_keys": int(fu.get("duplicate_keys") or 0),
    }


# ── The executor ──────────────────────────────────────────────────────────
def _j(v):
    return None if v is None else json.dumps(v)


def _is_time_type(t: str) -> bool:
    t = (t or "").lower()
    return t.startswith("date") or t.startswith("timestamp")


def _split_dim(dim: str) -> tuple:
    rel, _, col = dim.rpartition(".")
    return rel, col


def _check_time_dimension(model, own_relation: str, dim: str) -> None:
    rel, col = _split_dim(dim)
    cols = model.columns.get(rel)
    if cols is None or col not in cols:
        raise _Refuse("the time dimension column does not exist")
    if not _is_time_type(cols[col]):
        raise _Refuse("the time dimension must be a date or timestamp column")
    if rel != own_relation:
        # reachable many-to-one through approved relationships (raises NotModeled otherwise)
        modelcompiler.plan_joins(model, own_relation, [rel])


def _check_columns(model, relation: str, columns, what: str) -> None:
    cols = model.columns.get(relation)
    if cols is None:
        raise _Refuse(f"{what} relation is not in the schema catalog")
    for c in columns:
        if c not in cols:
            raise _Refuse(f"{what} column does not exist")


def _dependency_cycle(model, name: str) -> bool:
    seen: set = set()

    def walk(n: str, path: tuple) -> bool:
        if n in path:
            return True
        if n in seen:
            return False
        seen.add(n)
        m = model.measure(n)
        if m is None:
            return False
        return any(walk(i.name, path + (n,)) for i in modelcompiler._inputs_of(m))
    return walk(name, ())


async def _check_object(obj: str, op: str, name: str, defn: dict, payload: dict) -> dict:
    """Object-specific checks; returns extras the write needs."""
    extra: dict = {}
    if op in ("retire", "refresh") and obj not in ("measure", "view", "materialized_view"):
        return extra
    if obj == "table":
        model = await load_model()
        if defn["object_kind"] != "base":
            raise _Refuse("a table item must be a base relation; views are separate items")
        _check_columns(model, defn["relation"], defn["primary_key"], "primary key")
        if defn.get("agg_time_dimension"):
            _check_time_dimension(model, defn["relation"], defn["agg_time_dimension"])
    elif obj == "relationship":
        model = await load_model()
        origin = payload.get("origin")
        if origin not in ("foreign_key", "agent"):
            raise _Refuse("origin must be foreign_key or agent")
        _check_columns(model, defn["from_relation"], defn["from_columns"], "from")
        _check_columns(model, defn["to_relation"], defn["to_columns"], "to")
        stats = await relationship_stats(defn)
        if stats["duplicate_keys"] > 0:
            raise _Refuse("the to-side columns are not unique, so the join would repeat rows")
        if defn.get("cardinality") == "one_to_one" and stats["from_duplicate_keys"] > 0:
            raise _Refuse("the from-side columns are not unique, so this is not one-to-one")
        extra = {"origin": origin, "match_rate": stats["match_rate"]}
    elif obj == "measure":
        model = await load_model()
        if op == "retire":
            rows = await load_rows()
            for m in rows["measures"]:
                for k in ("numerator", "denominator"):
                    if (m.get(k) or {}).get("name") == name:
                        raise _Refuse("an active measure uses this one as an input")
                for i in m.get("input_metrics") or []:
                    if i.get("name") == name:
                        raise _Refuse("an active measure uses this one as an input")
            return extra
        candidate = modelcompiler.measure_from_definition(defn)
        for i in modelcompiler._inputs_of(candidate):
            if i.name == name or model.measure(i.name) is None:
                raise _Refuse("an input measure is not active")
        m2 = model.with_measure(candidate)
        if _dependency_cycle(m2, name):
            raise _Refuse("measure inputs form a cycle")
        modelcompiler.compile_measure(
            m2, modelcompiler.canonical_request(m2, candidate, "month", CHECK_START, CHECK_END))
    elif obj in ("view", "materialized_view"):
        if op == "retire":
            rows = await load_rows()
            rel = f"model.{name}"
            if any(rel in modelcompiler.measure_relations(m) for m in rows["measures"]):  # Quick 054
                raise _Refuse("an active measure is built on this view")
        elif op == "update" and obj == "materialized_view":
            raise _Refuse("a materialized view cannot be changed in place; retire it and create a new one")
    elif obj == "index":
        if op == "create" or op == "update":
            mv = defn["relation"].split(".", 1)[1]
            if await current_definition("materialized_view", mv) is None:
                raise _Refuse("indexes need an active materialized view")
    return extra


async def _run_ddl(obj: str, op: str, name: str, defn: dict, cur: dict | None) -> dict:
    """Model-schema DDL as the owner role, under the link lock. Returns {'columns': ...}."""
    out: dict = {}
    async with warehouse._lock():
        conn = await warehouse.connect("owner")
        try:
            if op == "retire":
                await whlink.drop_model_object(conn, obj, name)
            elif op == "refresh":
                await whlink.refresh_model_matview(conn, name)
            else:
                await whlink.ensure_model_schema(conn)
                if obj == "view":
                    await whlink.check_reader_refs(conn, defn["refs"])
                    out["columns"] = await whlink.create_model_view(conn, name, defn["sql"])
                elif obj == "materialized_view":
                    await whlink.check_reader_refs(conn, defn["refs"])
                    out["columns"] = await whlink.create_model_matview(conn, name, defn["sql"])
                else:
                    mv = defn["relation"].split(".", 1)[1]
                    await whlink.create_model_index(conn, name, mv, defn["columns"])
        finally:
            await conn.close()
    return out


_TABLE_UPSERT = (
    "INSERT INTO model_tables (name, relation, object_kind, role, grain, primary_key, "
    "agg_time_dimension, label, description, status, action_id, approved_at, updated_at) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'active', $10, now(), now()) "
    "ON CONFLICT (name) DO UPDATE SET relation = EXCLUDED.relation, object_kind = EXCLUDED.object_kind, "
    "role = EXCLUDED.role, grain = EXCLUDED.grain, primary_key = EXCLUDED.primary_key, "
    "agg_time_dimension = EXCLUDED.agg_time_dimension, label = EXCLUDED.label, "
    "description = EXCLUDED.description, status = 'active', action_id = EXCLUDED.action_id, "
    "approved_at = now(), updated_at = now() "
    "WHERE model_tables.object_kind = EXCLUDED.object_kind")

_VIEW_UPSERT = (
    "INSERT INTO model_tables (name, relation, object_kind, derived_sql, description, meta, "
    "status, action_id, approved_at, updated_at) "
    "VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'active', $7, now(), now()) "
    "ON CONFLICT (name) DO UPDATE SET relation = EXCLUDED.relation, object_kind = EXCLUDED.object_kind, "
    "derived_sql = EXCLUDED.derived_sql, description = EXCLUDED.description, meta = EXCLUDED.meta, "
    "status = 'active', action_id = EXCLUDED.action_id, approved_at = now(), updated_at = now() "
    "WHERE model_tables.object_kind = EXCLUDED.object_kind")

_REL_UPSERT = (
    "INSERT INTO model_relationships (name, from_relation, from_columns, to_relation, to_columns, "
    "cardinality, origin, match_rate, description, status, action_id, approved_at, updated_at) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'active', $10, now(), now()) "
    "ON CONFLICT (name) DO UPDATE SET from_relation = EXCLUDED.from_relation, "
    "from_columns = EXCLUDED.from_columns, to_relation = EXCLUDED.to_relation, "
    "to_columns = EXCLUDED.to_columns, cardinality = EXCLUDED.cardinality, origin = EXCLUDED.origin, "
    "match_rate = EXCLUDED.match_rate, description = EXCLUDED.description, status = 'active', "
    "action_id = EXCLUDED.action_id, approved_at = now(), updated_at = now()")

_MEASURE_UPSERT = (
    "INSERT INTO model_measures (name, label, description, type, base_relation, agg, expr, "
    "agg_time_dimension, filter, numerator, denominator, input_metrics, non_additive_dimension, "
    "additivity, unit, format, status, action_id, approved_at, updated_at) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11::jsonb, $12::jsonb, $13::jsonb, "
    "$14, $15, $16, 'active', $17, now(), now()) "
    "ON CONFLICT (name) DO UPDATE SET label = EXCLUDED.label, description = EXCLUDED.description, "
    "type = EXCLUDED.type, base_relation = EXCLUDED.base_relation, agg = EXCLUDED.agg, "
    "expr = EXCLUDED.expr, agg_time_dimension = EXCLUDED.agg_time_dimension, "
    "filter = EXCLUDED.filter, numerator = EXCLUDED.numerator, denominator = EXCLUDED.denominator, "
    "input_metrics = EXCLUDED.input_metrics, non_additive_dimension = EXCLUDED.non_additive_dimension, "
    "additivity = EXCLUDED.additivity, unit = EXCLUDED.unit, format = EXCLUDED.format, "
    "status = 'active', action_id = EXCLUDED.action_id, approved_at = now(), updated_at = now()")

_INDEX_UPSERT = (
    "INSERT INTO model_indexes (name, relation, columns, status, action_id, approved_at, updated_at) "
    "VALUES ($1, $2, $3, 'active', $4, now(), now()) "
    "ON CONFLICT (name) DO UPDATE SET relation = EXCLUDED.relation, columns = EXCLUDED.columns, "
    "status = 'active', action_id = EXCLUDED.action_id, approved_at = now(), updated_at = now()")


async def _write(obj: str, op: str, name: str, defn: dict, action_id, extra: dict, ddl: dict) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            table, kind = _DEFS[obj]
            if op == "retire":
                if kind is not None:
                    await conn.execute(
                        f"UPDATE {table} SET status = 'retired', action_id = $2, updated_at = now() "
                        "WHERE name = $1 AND object_kind = $3", name, action_id, kind)
                else:
                    await conn.execute(
                        f"UPDATE {table} SET status = 'retired', action_id = $2, updated_at = now() "
                        "WHERE name = $1", name, action_id)
                if obj == "materialized_view":
                    await conn.execute(
                        "UPDATE model_indexes SET status = 'retired', action_id = $2, updated_at = now() "
                        "WHERE relation = $1 AND status = 'active'", f"model.{name}", action_id)
                return
            if op == "refresh":
                await conn.execute(
                    "UPDATE model_tables SET action_id = $2, updated_at = now() "
                    "WHERE name = $1 AND object_kind = 'matview'", name, action_id)
                return
            if obj == "table":
                await conn.execute(
                    _TABLE_UPSERT, name, defn["relation"], defn["object_kind"], defn.get("role"),
                    defn.get("grain"), list(defn.get("primary_key") or []), defn.get("agg_time_dimension"),
                    defn.get("label"), defn.get("description"), action_id)
            elif obj in ("view", "materialized_view"):
                await conn.execute(
                    _VIEW_UPSERT, name, f"model.{name}", defn["object_kind"], defn["sql"],
                    defn.get("description"), json.dumps({"columns": ddl.get("columns") or {}}), action_id)
            elif obj == "relationship":
                rate = extra["match_rate"]
                await conn.execute(
                    _REL_UPSERT, name, defn["from_relation"], list(defn["from_columns"]),
                    defn["to_relation"], list(defn["to_columns"]), defn.get("cardinality") or "many_to_one",
                    extra["origin"], None if rate is None else Decimal(str(rate)),
                    defn.get("description"), action_id)
            elif obj == "measure":
                await conn.execute(
                    _MEASURE_UPSERT, name, defn.get("label"), defn.get("description"), defn["type"],
                    defn.get("base_relation"), defn.get("agg"), defn.get("expr"),
                    defn.get("agg_time_dimension"), json.dumps(defn.get("filter") or []),
                    _j(defn.get("numerator")), _j(defn.get("denominator")), _j(defn.get("input_metrics")),
                    _j(defn.get("non_additive_dimension")), defn.get("additivity") or "additive",
                    defn.get("unit"), defn.get("format"), action_id)
            else:
                await conn.execute(_INDEX_UPSERT, name, defn["relation"], list(defn["columns"]), action_id)


async def _apply(payload: dict) -> bool:
    obj, op, name = payload.get("object"), payload.get("op"), payload.get("name")
    if obj not in modelcompiler.OBJECTS or op not in OPS:
        raise _Refuse("unknown object or op")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise _Refuse("invalid name")
    if op == "refresh" and obj != "materialized_view":
        raise _Refuse("refresh applies only to materialized views")
    if obj in _WAREHOUSE_OBJECTS and not warehouse.warehouse_mode():
        raise _Refuse("this item needs warehouse mode")

    defn: dict = {}
    if op in ("create", "update"):
        raw = payload.get("definition")
        if not isinstance(raw, dict):
            raise _Refuse("definition must be an object")
        # the payload is untrusted: re-validate; origin and match_rate never come from the definition
        defn = modelcompiler.normalize_definition(obj, {**raw, "name": name})

    if obj in ("table", "view", "materialized_view"):
        pool = await get_pool()
        other = await pool.fetchrow(
            "SELECT object_kind FROM model_tables WHERE name = $1", name)
        if other is not None and other["object_kind"] != _DEFS[obj][1]:
            raise _Refuse("that name belongs to another model object; pick a different name")
    cur = await current_definition(obj, name)
    if op == "create":
        if cur is not None:
            recreate = (obj in _WAREHOUSE_OBJECTS and name not in await warehouse_objects())
            if not recreate:
                raise _Refuse("an active object of that name exists; use update")
    elif cur is None:
        raise _Refuse("no active object of that name")

    extra = await _check_object(obj, op, name, defn, payload)

    ddl: dict = {}
    if obj in _WAREHOUSE_OBJECTS:
        ddl = await _run_ddl(obj, op, name, defn, cur)

    pool = await get_pool()
    action_id = await pool.fetchval(
        "SELECT id FROM actions WHERE kind = 'model_change' AND status = 'approved' "
        "AND payload->>'object' = $1 AND payload->>'name' = $2 ORDER BY id DESC LIMIT 1", obj, name)
    await _write(obj, op, name, defn, action_id, extra, ddl)
    return True


async def apply(payload: dict) -> bool:
    """The single writer. Never raises: any failure is logged by type name and returns False."""
    label = "?"
    try:
        label = f"{payload.get('object')}/{payload.get('name')}"
        return await _apply(payload)
    except _Refuse as e:
        log.warning("model_change refused (%s): %s", label, e)
    except Exception as e:
        log.warning("model_change failed (%s): %s", label, type(e).__name__)
    return False


# -- evidence (16-04): read-only, through guardrails -----------------------------
SAMPLE_DAYS = 30
PREVIEW_ROWS = 30


def _plain(v):
    """JSON-friendly scalar: Decimal -> float, date -> ISO text."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, date):
        return v.isoformat()
    return v


async def source_snapshots() -> list:
    """Quick 027: catalog snapshots of the active registered sources (warehouse mode only).
    Never raises: an unreadable registry or one bad source is skipped."""
    if not warehouse.warehouse_mode():
        return []
    try:
        pool = await get_pool()
        srcs = await pool.fetch(
            "SELECT name FROM sources WHERE status = 'active' AND name <> 'business_db' "
            "AND warehouse_schema IS NOT NULL")
    except Exception as e:
        log.warning("registered sources unreadable: %s", type(e).__name__)
        return []
    out = []
    for sr in srcs:
        try:
            out.append(await schemadiscovery.introspect(sr["name"]))
        except Exception as e:
            log.warning("source catalog skipped: %s", type(e).__name__)
    return out


async def foreign_keys() -> list:
    """One dict per foreign-key column in the live catalog (works with an empty context store).
    Quick 027: also the registered sources' catalogs in warehouse mode, so source FKs seed too."""
    out: list = []
    for snap in [await schemadiscovery.introspect(), *await source_snapshots()]:
        for t in snap.get("tables") or []:
            for c in t.get("columns") or []:
                fk = c.get("fk")
                if fk and fk.get("table") and fk.get("column"):
                    out.append({"schema": t["schema"], "table": t["table"], "column": c["name"],
                                "to_table": fk["table"], "to_column": fk["column"]})
    return out


async def measure_evidence(model, measure, today: date) -> dict:
    """Compiled SQL plus 30 days of daily values (one sample_value when the measure has no time)."""
    m2 = model.with_measure(measure)
    start, end = today - timedelta(days=SAMPLE_DAYS), today
    req = modelcompiler.canonical_request(m2, measure, "day", start, end)
    compiled = modelcompiler.compile_measure(m2, req)
    rows = await run_readonly_sql(compiled.sql)
    if req.grain:
        tcol, vcol = compiled.columns[0], compiled.columns[-1]
        samples = [{"day": str(_plain(r.get(tcol)))[:10], "value": _plain(r.get(vcol))}
                   for r in rows[:SAMPLE_DAYS + 1]]
        return {"sql": compiled.sql, "samples": samples,
                "sample_range": {"start": start.isoformat(), "end": end.isoformat()}}
    value = _plain(rows[0].get(compiled.columns[-1])) if rows else None
    return {"sql": compiled.sql, "samples": [], "sample_value": value, "sample_range": None}


async def relationship_evidence(rel: dict) -> dict:
    """Same match-rate and uniqueness statements the executor runs at approval."""
    return await relationship_stats(rel)


async def table_evidence(relation: str, primary_key, time_column, today: date) -> dict:
    pk = list(primary_key or [])
    rel = _qrel(relation)
    if pk:
        keys = ", ".join(_q(c) for c in pk)
        sql = f"SELECT count(*) AS row_count, count(DISTINCT ({keys})) AS distinct_keys FROM {rel}"
    else:
        sql = f"SELECT count(*) AS row_count, NULL AS distinct_keys FROM {rel}"
    r = (await run_readonly_sql(sql))[0]
    rows_n = int(r["row_count"])
    distinct = None if r.get("distinct_keys") is None else int(r["distinct_keys"])
    out = {"row_count": rows_n, "distinct_keys": distinct,
           "key_unique": (distinct == rows_n) if distinct is not None else None}
    if time_column:
        trel, tcol = _split_dim(time_column)
        if trel == relation:
            start, end = today - timedelta(days=SAMPLE_DAYS), today + timedelta(days=1)
            col = _q(tcol)
            per = await run_readonly_sql(
                f"SELECT CAST({col} AS date) AS day, count(*) AS rows FROM {rel} "
                f"WHERE {col} >= '{start.isoformat()}' AND {col} < '{end.isoformat()}' "
                "GROUP BY 1 ORDER BY 1")
            out["rows_per_day"] = [{"day": str(_plain(x["day"]))[:10], "rows": int(x["rows"])} for x in per]
        else:
            out["note"] = f"rows_per_day omitted: time column is on {trel}"
    return out


async def derived_evidence(sql: str) -> dict:
    rows = await run_readonly_sql(f"SELECT * FROM ({sql}) AS q LIMIT {PREVIEW_ROWS}")
    preview = [{k: _plain(v) for k, v in r.items()} for r in rows[:PREVIEW_ROWS]]
    return {"columns": list(rows[0].keys()) if rows else [], "preview_rows": preview}

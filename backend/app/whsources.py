"""Phase 15 (SRC-04, SRC-07, SRC-09): warehouse objects for registered sources, created only on a
wh_owner connection. With warehouse.py and whlink.py, the only code that creates warehouse objects.
Never CASCADE, never IMPORT FOREIGN SCHEMA.

Layout per source <name>:
  src_<name>               public schema, one view per table (the name an import or a live read uses)
  ic_imp_<name>[_staging]  loader-writable import dataset (no reader access)
  stage_<name>[_staging]   loader-writable test copies (no reader access)
  ic_fdw_src_<name>_*      private schemas holding foreign tables (live mode)
  server src_<name>        one foreign server with a single user mapping, for the owner only
"""
import hashlib
import json
import logging
import re

import asyncpg

from . import sourcesvc, sourcevault, warehouse, whlink
from .config import get_settings
from .db import get_pool
from .warehouse import DEFAULT_ROLES, exec_fmt

log = logging.getLogger("whsources")

MSSQL_LIVE_REFUSAL = ("SQL Server tables can be live only when they are small lookup tables "
                      "(at most mssql_live_max_rows rows); use import")
MYSQL_LIVE_REFUSAL = ("MySQL tables can be live only when they are small lookup tables "
                      "(at most mysql_live_max_rows rows); use import")
# Quick 028 (WR-04): neither mysql_fdw 2.9.3 nor tds_fdw 2.0.5 can enforce TLS, so a credential
# that asks for TLS never gets a live link (no silent plaintext downgrade)
LIVE_TLS_REFUSAL = {
    "mysql": "this login asks for SSL/TLS and the live MySQL link cannot enforce TLS; use import",
    "mssql": ("this login asks for encryption and the live SQL Server link cannot enforce TLS; "
              "use import, or set encrypt to no on the credential to accept an unencrypted live link"),
}
MSSQL_TDS_MISSING = "the warehouse has no tds_fdw; use import"
MYSQL_FDW_MISSING = "the warehouse has no mysql_fdw; use import"
NO_MAPPABLE = "no column has a type the warehouse can read"


# ── Naming ──────────────────────────────────────────────────────────────────
def src_schema(name: str) -> str:
    return f"src_{name}"


def imp_schema(name: str) -> str:
    return f"ic_imp_{name}"


def stage_schema(name: str) -> str:
    return f"stage_{name}"


def server_name(name: str) -> str:
    return f"src_{name}"


def tds_wrapper_name(remote_table: str) -> str:
    return "ic_rows_" + hashlib.md5((remote_table or "").encode()).hexdigest()[:16]


def tds_wrapper_templates() -> tuple[str, str, str, str]:
    # tds_fdw resolves the current user's mapping; the owner-owned SECURITY DEFINER wrapper makes
    # that the owner (SRC-07, 15-08 revision 2). No reader or PUBLIC mapping is ever created.
    return (
        "CREATE FUNCTION %I.%I() RETURNS SETOF %I.%I LANGUAGE sql STABLE SECURITY DEFINER "
        "SET search_path = pg_catalog, pg_temp AS %L",
        "REVOKE ALL ON FUNCTION %I.%I() FROM PUBLIC",
        "GRANT EXECUTE ON FUNCTION %I.%I() TO %I",
        "CREATE VIEW %I.%I AS SELECT {cols} FROM %I.%I()",
    )


# ── Type maps ───────────────────────────────────────────────────────────────
_MY_RE = re.compile(r"^([a-z]+)\s*(?:\(([^)]*)\))?\s*(.*)$")
_INT_ARGS = re.compile(r"^\d{1,4}$")
_DEC_ARGS = re.compile(r"^(\d{1,3})\s*,\s*(\d{1,3})$")


def _fsp(args: str | None) -> str:
    if args and _INT_ARGS.match(args.strip()) and 0 <= int(args) <= 6:
        return f"({int(args)})"
    return ""


def mysql_pg_type(column_type: str) -> str | None:
    t = (column_type or "").strip().lower()
    m = _MY_RE.match(t)
    if not m:
        return None
    base, args, rest = m.group(1), (m.group(2) or "").strip(), m.group(3)
    unsigned = "unsigned" in rest
    if base in ("tinyint", "smallint"):
        return "integer" if (base == "smallint" and unsigned) else "smallint"
    if base in ("mediumint", "int", "integer"):
        return "bigint" if (base != "mediumint" and unsigned) else "integer"
    if base == "bigint":
        return "numeric(20,0)" if unsigned else "bigint"
    if base in ("decimal", "numeric", "dec", "fixed"):
        d = _DEC_ARGS.match(args)
        if d:
            return f"numeric({int(d.group(1))},{int(d.group(2))})"
        if _INT_ARGS.match(args):
            return f"numeric({int(args)},0)"
        return "numeric(10,0)" if not args else None
    if base in ("double", "real"):
        return "double precision"
    if base == "float":
        return "real"
    if base in ("varchar", "char"):
        if not _INT_ARGS.match(args):
            return None
        return (f"character varying({int(args)})" if base == "varchar"
                else f"character({int(args)})")
    if base in ("tinytext", "text", "mediumtext", "longtext", "enum", "set"):
        return "text"
    if base == "date":
        return "date"
    if base in ("datetime", "timestamp"):
        f = _fsp(args)
        return f"timestamp{f} without time zone"
    if base == "time":
        f = _fsp(args)
        return f"time{f} without time zone"
    if base == "year":
        return "smallint"
    if base == "json":
        return "json"
    if base in ("tinyblob", "blob", "mediumblob", "longblob", "varbinary", "binary"):
        return "bytea"
    return None


def mssql_pg_type(canonical: str) -> str | None:
    t = (canonical or "").strip().lower()
    m = _MY_RE.match(t)
    if not m:
        return None
    base, args = m.group(1), (m.group(2) or "").strip()
    simple = {"int": "integer", "bigint": "bigint", "smallint": "smallint", "tinyint": "smallint",
              "bit": "boolean", "money": "numeric(19,4)", "smallmoney": "numeric(10,4)",
              "float": "double precision", "real": "real", "text": "text", "ntext": "text",
              "date": "date", "datetime": "timestamp without time zone",
              "smalldatetime": "timestamp without time zone", "uniqueidentifier": "uuid",
              "rowversion": "bytea", "timestamp": "bytea", "image": "bytea", "xml": "text"}
    if base in simple:
        return simple[base]
    if base in ("decimal", "numeric"):
        d = _DEC_ARGS.match(args)
        return f"numeric({int(d.group(1))},{int(d.group(2))})" if d else None
    if base in ("nvarchar", "varchar"):
        if args == "max":
            return "text"
        return f"character varying({int(args)})" if _INT_ARGS.match(args) else None
    if base in ("nchar", "char"):
        return f"character({int(args)})" if _INT_ARGS.match(args) else None
    if base in ("varbinary", "binary"):
        return "bytea"
    if base == "datetime2":
        f = _fsp(args)
        return f"timestamp{f} without time zone"
    if base == "datetimeoffset":
        f = _fsp(args)
        return f"timestamp{f} with time zone"
    if base == "time":
        f = _fsp(args)
        return f"time{f} without time zone"
    return None


def mssql_live_allowed(est_rows) -> bool:
    return est_rows is not None and est_rows <= get_settings().mssql_live_max_rows


def mysql_live_allowed(est_rows) -> bool:
    return est_rows is not None and est_rows <= get_settings().mysql_live_max_rows


def live_rows_allowed(kind: str, est_rows) -> bool:
    """Row cap for live tables read in full per query (Quick 028, WR-03). Postgres has none."""
    if kind == "mysql":
        return mysql_live_allowed(est_rows)
    if kind == "mssql":
        return mssql_live_allowed(est_rows)
    return True


def live_rows_refusal(kind: str) -> str:
    return MYSQL_LIVE_REFUSAL if kind == "mysql" else MSSQL_LIVE_REFUSAL


def tls_requested(kind: str, options) -> bool:
    """Does this credential ask for TLS? Mirrors icsync/conn.py: MySQL only when ssl is true;
    SQL Server unless encrypt is explicitly no (ic-sync defaults to yes)."""
    o = options if isinstance(options, dict) else {}
    if kind == "mysql":
        return o.get("ssl") is True
    if kind == "mssql":
        return o.get("encrypt", "yes") != "no"
    return False


def live_tls_refusal(kind: str, options) -> str | None:
    return LIVE_TLS_REFUSAL.get(kind) if tls_requested(kind, options) else None


# ── Hashes, view columns, catalog rows ──────────────────────────────────────
def mapped_col_hash(columns: list[dict]) -> str:
    """Link hash for MySQL and SQL Server live tables: name and mapped Postgres type, in order."""
    core = [[c.get("name"), c.get("type")] for c in columns]
    return hashlib.sha256(json.dumps(core, separators=(",", ":")).encode()).hexdigest()


def import_view_columns(attrs: list[dict]) -> list[str]:
    """Column names for an import view: attnum order, no dropped columns, no dlt bookkeeping."""
    live = [a for a in attrs if not a.get("dropped")]
    live.sort(key=lambda a: a.get("attnum", 0))
    return [a["name"] for a in live if not str(a["name"]).startswith("_dlt_")]


def _find_probe_table(probe: dict | None, t: dict) -> dict | None:
    tabs = (probe or {}).get("tables") or []
    for pt in tabs:
        if pt.get("warehouse_table") and pt["warehouse_table"] == t.get("warehouse_table"):
            return pt
    for pt in tabs:
        if pt.get("table") == t.get("remote_table") and \
                t.get("remote_schema") in (None, pt.get("schema")):
            return pt
    return None


def _fk_for(pt: dict, col: str, by_remote: dict) -> dict | None:
    for fk in pt.get("fks") or []:
        cols = fk.get("columns") or []
        if col not in cols:
            continue
        target = by_remote.get((fk.get("ref_schema"), fk.get("ref_table"))) \
            or by_remote.get((None, fk.get("ref_table")))
        refs = fk.get("ref_columns") or []
        if target is None or cols.index(col) >= len(refs):
            return None
        return {"table": target, "column": refs[cols.index(col)]}
    return None


def catalog_rows(name: str, tables: list[dict], probe: dict | None,
                 extra_columns: dict | None = None) -> list[dict]:
    """v1.0-shaped discovery rows for every table passed (the full registered set)."""
    by_remote: dict = {}
    for t in tables:
        by_remote[(t.get("remote_schema"), t.get("remote_table"))] = t["warehouse_table"]
        by_remote.setdefault((None, t.get("remote_table")), t["warehouse_table"])
    out = []
    for t in tables:
        pt = _find_probe_table(probe, t) or {}
        pkset = set(pt.get("pk") or [])
        cols = []
        for c in pt.get("columns") or []:
            cols.append({"name": c["name"], "type": c.get("type"),
                         "nullable": bool(c.get("nullable", True)),
                         "pk": bool(c.get("pk")) or c["name"] in pkset,
                         "fk": _fk_for(pt, c["name"], by_remote), "comment": None,
                         "type_builtin": True})
        if t.get("mode") == "import":
            have = {c["name"] for c in cols}
            for c in (extra_columns or {}).get(t["warehouse_table"], []):
                if c["name"] not in have:
                    cols.append({"name": c["name"], "type": c.get("type"), "nullable": True,
                                 "pk": False, "fk": None, "comment": None, "type_builtin": True})
        est = pt.get("est_rows") if pt.get("est_rows") is not None else t.get("est_rows")
        row = {"schema_name": src_schema(name), "table_name": t["warehouse_table"],
               "relkind": "f" if t.get("mode") == "live" else "r", "table_comment": None,
               "row_estimate": est, "columns": cols, "checks": []}
        row["col_hash"] = whlink.col_hash(cols)
        row["row_hash"] = whlink.catalog_row_hash(row)
        out.append(row)
    return out


# ── DDL helpers ─────────────────────────────────────────────────────────────
async def _schema_exists(conn, schema: str) -> bool:
    return bool(await conn.fetchval("SELECT 1 FROM pg_namespace WHERE nspname = $1", schema))


async def _ext_present(conn, ext: str) -> bool:
    return bool(await conn.fetchval("SELECT 1 FROM pg_extension WHERE extname = $1", ext))


async def ensure_public_schema(conn, name: str, roles=DEFAULT_ROLES) -> None:
    sch = src_schema(name)
    if not await _schema_exists(conn, sch):
        await exec_fmt(conn, "CREATE SCHEMA %I AUTHORIZATION CURRENT_USER", sch)
    if not await conn.fetchval("SELECT has_schema_privilege($1, $2, 'USAGE')", roles.reader, sch):
        await exec_fmt(conn, "GRANT USAGE ON SCHEMA %I TO %I", sch, roles.reader)


async def _ensure_loader_schema(conn, sch: str, roles) -> None:
    if not await _schema_exists(conn, sch):
        await exec_fmt(conn, "CREATE SCHEMA %I AUTHORIZATION CURRENT_USER", sch)
        await exec_fmt(conn, "REVOKE ALL ON SCHEMA %I FROM PUBLIC", sch)
    if not (await conn.fetchval("SELECT has_schema_privilege($1, $2, 'USAGE')", roles.loader, sch)
            and await conn.fetchval("SELECT has_schema_privilege($1, $2, 'CREATE')",
                                    roles.loader, sch)):
        await exec_fmt(conn, "GRANT USAGE, CREATE ON SCHEMA %I TO %I", sch, roles.loader)


async def ensure_import_schemas(conn, name: str, roles=DEFAULT_ROLES) -> None:
    for sch in (imp_schema(name), imp_schema(name) + "_staging"):
        await _ensure_loader_schema(conn, sch, roles)


async def _owner_conn(roles):
    return await warehouse.connect("owner", roles=roles, timeout=15.0)


async def _lock(conn, name: str) -> bool:
    return bool(await conn.fetchval(
        "SELECT pg_try_advisory_lock(hashtext($1))", f"ic_src:{name}"))


async def _unlock(conn, name: str) -> None:
    try:
        await conn.fetchval("SELECT pg_advisory_unlock(hashtext($1))", f"ic_src:{name}")
    except Exception:
        pass


async def ensure_stage(name: str, roles=DEFAULT_ROLES) -> dict:
    """stage_<name> and stage_<name>_staging for test copies (no reader grant). Idempotent."""
    conn = await _owner_conn(roles)
    try:
        for sch in (stage_schema(name), stage_schema(name) + "_staging"):
            await _ensure_loader_schema(conn, sch, roles)
        return {"status": "ok", "schemas": [stage_schema(name), stage_schema(name) + "_staging"]}
    finally:
        await conn.close()


async def import_table_columns(conn, name: str) -> dict:
    """{table: [{name, type}]} for every table in ic_imp_<name> (no dlt bookkeeping)."""
    rows = await conn.fetch(
        "SELECT c.relname AS t, a.attname AS n, a.attnum AS num, a.attisdropped AS dropped, "
        "format_type(a.atttypid, a.atttypmod) AS ty "
        "FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid = c.oid "
        "WHERE s.nspname = $1 AND c.relkind IN ('r','p') AND a.attnum > 0 "
        "AND c.relname NOT LIKE '\\_dlt\\_%' ORDER BY c.relname, a.attnum", imp_schema(name))
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["t"], []).append(
            {"name": r["n"], "attnum": r["num"], "dropped": r["dropped"], "type": r["ty"]})
    out = {}
    for t, attrs in by.items():
        keep = set(import_view_columns(attrs))
        out[t] = [{"name": a["name"], "type": a["type"]} for a in attrs
                  if a["name"] in keep and not a["dropped"]]
    return out


async def _view_signature(conn, schema: str, view: str) -> list | None:
    rows = await conn.fetch(
        "SELECT a.attname n, format_type(a.atttypid, a.atttypmod) ty FROM pg_class c "
        "JOIN pg_namespace s ON s.oid = c.relnamespace JOIN pg_attribute a ON a.attrelid = c.oid "
        "WHERE s.nspname = $1 AND c.relname = $2 AND c.relkind = 'v' AND a.attnum > 0 "
        "AND NOT a.attisdropped ORDER BY a.attnum", schema, view)
    return [[r["n"], r["ty"]] for r in rows] if rows else None


# ── Phase 17 (CONS-05): signature-preserving views ──────────────────────────
class ViewColumnsVanished(ValueError):
    """A column of the existing view is gone from the new source relation."""


_VIEW_NAME = r'(?:"[^"%\x00]+"|[a-z_][a-z0-9_ ,()]*)'
_VIEW_TYPE_RE = re.compile(rf"^{_VIEW_NAME}(?:\.{_VIEW_NAME})?(?:\[\])*$")
_TS, _TSTZ = "timestamp without time zone", "timestamp with time zone"


def _safe_view_type(t: str) -> str:
    # types come from the warehouse catalog (format_type), never from a payload
    if not isinstance(t, str) or not _VIEW_TYPE_RE.match(t):
        raise ValueError("unsupported column type")
    return t


def cast_select_list(view_cols: list[dict], src_cols: list[dict]) -> tuple[str, list[str]]:
    """format() template fragment + %I values: every view column kept in place with its
    current type, then source-only columns appended. Never adds COLLATE."""
    by = {c["name"]: c["type"] for c in src_cols}
    parts: list[str] = []
    vals: list[str] = []
    for vc in view_cols:
        n, vt = vc["name"], _safe_view_type(vc["type"])
        if n not in by:
            raise ViewColumnsVanished(n)
        st = by[n]
        if st == vt:
            parts.append("%I")
            vals.append(n)
        elif vt == _TS and st == _TSTZ:
            parts.append("(%I AT TIME ZONE 'UTC')::timestamp without time zone AS %I")
            vals += [n, n]
        elif vt == _TSTZ and st == _TS:
            parts.append("(%I AT TIME ZONE 'UTC')::timestamp with time zone AS %I")
            vals += [n, n]
        else:
            parts.append(f"CAST(%I AS {vt}) AS %I")
            vals += [n, n]
    have = {vc["name"] for vc in view_cols}
    for sc in src_cols:
        if sc["name"] not in have and not sc["name"].startswith("_dlt_"):
            parts.append("%I")
            vals.append(sc["name"])
    return ", ".join(parts), vals


async def view_columns(conn, schema: str, view: str) -> list[dict] | None:
    rows = await conn.fetch(
        "SELECT a.attname n, format_type(a.atttypid, a.atttypmod) ty FROM pg_class c "
        "JOIN pg_namespace s ON s.oid = c.relnamespace JOIN pg_attribute a ON a.attrelid = c.oid "
        "WHERE s.nspname = $1 AND c.relname = $2 AND c.relkind = 'v' AND a.attnum > 0 "
        "AND NOT a.attisdropped ORDER BY a.attnum", schema, view)
    return [{"name": r["n"], "type": r["ty"]} for r in rows] if rows else None


async def relation_columns(conn, schema: str, rel: str) -> list[dict]:
    rows = await conn.fetch(
        "SELECT a.attname n, format_type(a.atttypid, a.atttypmod) ty FROM pg_class c "
        "JOIN pg_namespace s ON s.oid = c.relnamespace JOIN pg_attribute a ON a.attrelid = c.oid "
        "WHERE s.nspname = $1 AND c.relname = $2 AND c.relkind IN ('r','p','f','v','m') "
        "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum", schema, rel)
    return [{"name": r["n"], "type": r["ty"]} for r in rows]


async def check_casts(conn, src_schema_: str, src_rel: str, template: str, values: list[str],
                      *, function: bool = False) -> None:
    """Run every cast over the whole source before cutover. OFFSET 0 fences the subquery so the
    planner cannot drop the cast columns; any value the old type cannot hold raises here.
    function=True reads a set-returning wrapper function (Phase 17 mode flips, mysql/tds live)."""
    frm = "%I.%I()" if function else "%I.%I"
    stmt = await conn.fetchval(
        "SELECT format($1, VARIADIC $2::text[])",
        f"SELECT count(*) FROM (SELECT {template} FROM {frm} OFFSET 0) s WHERE s IS NOT NULL",
        [*values, src_schema_, src_rel])
    await conn.fetchval(stmt)


async def replace_view_preserving(conn, schema: str, view: str, src_schema_: str, src_rel: str,
                                  roles=DEFAULT_ROLES, *, check: bool = True,
                                  src_function: bool = False,
                                  cols_rel: tuple | None = None) -> dict:
    """CREATE OR REPLACE a view over a different relation keeping every existing column's
    name, type and position. Never DROP, never CASCADE. src_function=True reads
    src_schema_.src_rel() (an owner wrapper function); cols_rel then names the foreign table
    (schema, table) whose columns the function returns."""
    if src_function and not cols_rel:
        raise ValueError("cols_rel is required with src_function")
    src_cols = await relation_columns(conn, *(cols_rel if src_function else (src_schema_, src_rel)))
    frm = "%I.%I()" if src_function else "%I.%I"
    cur = await view_columns(conn, schema, view)
    if cur is None:
        shown = [c for c in src_cols if not c["name"].startswith("_dlt_")]
        tpl = ", ".join("%I" for _ in shown)
        vals = [c["name"] for c in shown]
        appended: list[str] = []
        cast: list[str] = []
        create = "CREATE VIEW"
    else:
        try:
            tpl, vals = cast_select_list(cur, src_cols)
        except ViewColumnsVanished as e:
            return {"status": "refused", "reason": f"column {e.args[0]} is gone"}
        have = {c["name"] for c in cur}
        appended = [c["name"] for c in src_cols
                    if c["name"] not in have and not c["name"].startswith("_dlt_")]
        sty = {c["name"]: c["type"] for c in src_cols}
        cast = [c["name"] for c in cur if sty[c["name"]] != c["type"]]
        create = "CREATE OR REPLACE VIEW"
        if check and cast:
            await check_casts(conn, src_schema_, src_rel, tpl, vals, function=src_function)
    async with conn.transaction():
        await conn.execute("SET LOCAL lock_timeout = '5s'")
        await exec_fmt(conn, f"{create} %I.%I AS SELECT {tpl} FROM {frm}",
                       schema, view, *vals, src_schema_, src_rel)
    if not await conn.fetchval("SELECT has_table_privilege($1, format('%I.%I', $2::text, "
                               "$3::text), 'SELECT')", roles.reader, schema, view):
        await exec_fmt(conn, "GRANT SELECT ON %I.%I TO %I", schema, view, roles.reader)
    return {"status": "ok", "appended": appended, "cast": cast}


async def _refresh_views(conn, name: str, tables: list[dict], only, roles) -> dict:
    # Phase 17 (CONS-05): an existing view is only ever replaced signature-preserving, never dropped
    res = {"refreshed": 0, "stale": [], "missing": []}
    cols_by = await import_table_columns(conn, name)
    imp = imp_schema(name)
    for t in tables:
        if t.get("mode") != "import" or (only is not None and t["warehouse_table"] not in only):
            continue
        wt = t["warehouse_table"]
        sch = t.get("warehouse_schema") or src_schema(name)
        cols = cols_by.get(wt)
        if not cols:
            res["missing"].append(wt)
            continue
        sig = await _view_signature(conn, sch, wt)
        if sig == [[c["name"], c["type"]] for c in cols]:
            continue  # unchanged: no DDL
        if sig is not None:
            try:
                out = await replace_view_preserving(conn, sch, wt, imp, wt, roles, check=True)
            except (asyncpg.PostgresError, ValueError) as e:
                log.warning("view refresh of %s.%s blocked: %s", sch, wt, type(e).__name__)
                res["stale"].append(wt)
                continue
            if out.get("status") != "ok":
                log.warning("view refresh of %s.%s refused", sch, wt)
                res["stale"].append(wt)
                continue
        else:
            sel = ", ".join("%I" for _ in cols)
            names = [c["name"] for c in cols]
            try:
                async with conn.transaction():
                    await conn.execute("SET LOCAL lock_timeout = '5s'")
                    await exec_fmt(conn, f"CREATE VIEW %I.%I AS SELECT {sel} FROM %I.%I",
                                   sch, wt, *names, imp, wt)
            except asyncpg.PostgresError as e:
                log.warning("view refresh of %s.%s blocked: %s", sch, wt, type(e).__name__)
                res["stale"].append(wt)
                continue
            if not await conn.fetchval("SELECT has_table_privilege($1, format('%I.%I', $2::text, "
                                       "$3::text), 'SELECT')", roles.reader, sch, wt):
                await exec_fmt(conn, "GRANT SELECT ON %I.%I TO %I", sch, wt, roles.reader)
        res["refreshed"] += 1
    return res


async def refresh_import_views(name: str, tables=None, roles=DEFAULT_ROLES) -> dict:
    """Create or replace src_<name>.<t> over the explicit columns of ic_imp_<name>.<t>."""
    try:
        src = await sourcesvc.get_source(name)
        if src is None:
            return {"status": "error", "detail": "unknown source"}
        conn = await _owner_conn(roles)
    except Exception as e:
        return {"status": "error", "detail": whlink._error_detail(e)}
    try:
        if not await _lock(conn, name):
            return {"status": "busy"}
        try:
            r = await _refresh_views(conn, name, src.get("tables") or [],
                                     set(tables) if tables is not None else None, roles)
        finally:
            await _unlock(conn, name)
        return {"status": "stale" if r["stale"] else "ok", **r}
    except Exception as e:
        return {"status": "error", "detail": whlink._error_detail(e)}
    finally:
        await conn.close()


# ── Catalog ─────────────────────────────────────────────────────────────────
async def _load_probe(src: dict) -> dict | None:
    cfg = src.get("config") or {}
    pid = cfg.get("test_id") if src.get("kind") == "csv" else cfg.get("probe_id")
    if not pid:
        return None
    pool = await get_pool()
    snap = await pool.fetchval("SELECT snapshot FROM source_probes WHERE id = $1", int(pid))
    if isinstance(snap, (str, bytes)):
        snap = json.loads(snap)
    return snap


async def write_catalog(conn, name: str, probe=None) -> int:
    """Rewrite the catalog rows of the source from the FULL registered set. There is deliberately
    no table-subset parameter: whlink.refresh_catalog deletes every stored row it is not given,
    so a caller that passed only the tables it loaded would erase the others' rows."""
    src = await sourcesvc.get_source(name)
    if src is None:
        return 0
    tables = [t for t in src.get("tables") or [] if t.get("status") != "missing"]
    if probe is None:
        probe = await _load_probe(src)
    extra = await import_table_columns(conn, name) if any(
        t.get("mode") == "import" for t in tables) else {}
    rows = catalog_rows(name, tables, probe, extra)
    linked = {(r["schema_name"], r["table_name"]) for r in rows}
    return await whlink.refresh_catalog(conn, name, rows, linked)


# ── Live tables ─────────────────────────────────────────────────────────────
def _pg_extra(cred: dict) -> tuple:
    ssl = (cred.get("options") or {}).get("sslmode")
    return (("sslmode", ssl),) if ssl else ()


def _link_opts(cred: dict) -> "whlink.LinkOptions":
    return whlink.LinkOptions(cred["host"], str(cred["port"]), cred["database"], cred["user"],
                              cred.get("password") or "", extra=_pg_extra(cred))


async def _ensure_fdw_server(conn, srv: str, wrapper: str, sopts: dict, mopts: dict) -> None:
    """Server plus exactly one user mapping, for the owner. ALTER only on change."""
    if wrapper not in ("mysql_fdw", "tds_fdw"):
        raise ValueError("unsupported foreign data wrapper")
    row = await conn.fetchrow(
        "SELECT s.srvoptions, w.fdwname FROM pg_foreign_server s "
        "JOIN pg_foreign_data_wrapper w ON w.oid = s.srvfdw WHERE s.srvname = $1", srv)
    if row is None:
        tpl = (f"CREATE SERVER %I FOREIGN DATA WRAPPER {wrapper} OPTIONS ("
               + ", ".join("%I %L" for _ in sopts) + ")")
        vals = [srv]
        for k, v in sopts.items():
            vals += [k, str(v)]
        await exec_fmt(conn, tpl, *vals)
    else:
        if row["fdwname"] != wrapper:
            raise ValueError("server exists with another wrapper")
        tpl, vals = whlink._alter_template("ALTER SERVER %I", whlink._opt_dict(row["srvoptions"]),
                                           {k: str(v) for k, v in sopts.items()}, True)
        if tpl:
            await exec_fmt(conn, tpl, srv, *vals)
    mrow = await conn.fetchrow(
        "SELECT umoptions FROM pg_user_mappings WHERE srvname = $1 AND usename = current_user",
        srv)
    if mrow is None:
        await exec_fmt(conn, "CREATE USER MAPPING FOR CURRENT_USER SERVER %I OPTIONS "
                             "(username %L, password %L)", srv, mopts["username"], mopts["password"])
    else:
        tpl, vals = whlink._alter_template("ALTER USER MAPPING FOR CURRENT_USER SERVER %I",
                                           whlink._opt_dict(mrow["umoptions"]), mopts, False)
        if tpl:
            await exec_fmt(conn, tpl, srv, *vals)
    for r in await conn.fetch(
            "SELECT usename FROM pg_user_mappings WHERE srvname = $1 AND usename <> current_user",
            srv):
        if r["usename"] == "public":
            await exec_fmt(conn, "DROP USER MAPPING FOR PUBLIC SERVER %I", srv)
        else:
            await exec_fmt(conn, "DROP USER MAPPING FOR %I SERVER %I", r["usename"], srv)
        log.warning("dropped an unexpected user mapping on %s", srv)


async def _tx(conn, fn):
    async with conn.transaction():
        await conn.execute("SET LOCAL lock_timeout = '5s'")
        return await fn()


def _live_result() -> dict:
    return {"live": 0, "created": 0, "replaced": 0, "dropped": 0, "stale": [], "unlinked": []}


async def _live_postgres(conn, name: str, cred: dict, tables: list[dict], roles) -> dict:
    srv, sch = server_name(name), src_schema(name)
    res = _live_result()
    opts = _link_opts(cred)
    await whlink.ensure_server(conn, srv, opts)
    await whlink.ensure_catalog_tables(conn, srv, roles)
    snap = await whlink.fetch_snapshot(conn, srv, opts)
    want = {}
    for t in tables:
        key = (t.get("remote_schema") or "public", t["remote_table"])
        want[key] = t
    by = {(r["schema_name"], r["table_name"]): r for r in snap["tables"]
          if (r["schema_name"], r["table_name"]) in want}
    for key, t in want.items():
        if key not in by:
            res["unlinked"].append(f"{t['warehouse_table']}: not visible to the source login")
    # Phase 15 gap 2 (WR-14): the view carries the warehouse name; the foreign table keeps the
    # remote name. A recorded view name that differs from the desired one is a replace.
    rows = [dict(r) for r in await conn.fetch(
        "SELECT remote_schema, remote_table, col_hash, private_schema, "
        "COALESCE(view_name, remote_table) AS view_name FROM ic_meta.link_objects "
        "WHERE source = $1", srv)]
    cur = {(r["remote_schema"], r["remote_table"]): r for r in rows}
    plan = whlink.plan_changes(list(by.values()), rows)
    for key in by:
        if (key in cur and key not in plan["replace"] and key not in plan["create"]
                and (cur[key].get("view_name") or key[1]) != want[key]["warehouse_table"]):
            plan["replace"].append(key)

    for key in plan["drop"]:
        priv = cur[key]["private_schema"]

        async def remove(key=key, priv=priv):
            await whlink._drop_objects(conn, sch, priv, key[1], view=cur[key].get("view_name") or key[1])
            await whlink._delete_link_object(conn, srv, key[0], key[1])
        try:
            await _tx(conn, remove)
            res["dropped"] += 1
        except asyncpg.PostgresError as e:
            log.warning("drop of live table %s blocked: %s", key[1], type(e).__name__)
            res["stale"].append(key[1])
    for key in plan["create"] + plan["replace"]:
        r = by[key]
        priv = (cur.get(key) or {}).get("private_schema") or whlink.private_schema(srv, key[0])

        async def make(r=r, key=key, priv=priv):
            wt = want[key]["warehouse_table"]
            await whlink._drop_objects(conn, sch, priv, key[1],
                                       view=(cur.get(key) or {}).get("view_name"))
            p, linked_cols, unl = await whlink._create_foreign_table(
                conn, srv, r, roles, mirror_names=False)
            await whlink._create_view(conn, p, sch, key[1], linked_cols, roles, view=wt)
            await whlink._upsert_link_object(conn, srv, key[0], key[1], p, r["col_hash"], unl,
                                             view_name=wt)
        try:
            await _tx(conn, make)
            res["replaced" if key in plan["replace"] else "created"] += 1
        except (asyncpg.PostgresError, ValueError) as e:
            log.warning("live table %s not applied: %s", key[1], type(e).__name__)
            res["stale"].append(key[1])
    try:
        await whlink.drop_orphan_hash_enums(conn, srv)
    except asyncpg.PostgresError:
        pass
    res["live"] = len([k for k in by if k in {(r["remote_schema"], r["remote_table"])
                                              for r in await conn.fetch(
        "SELECT remote_schema, remote_table FROM ic_meta.link_objects WHERE source = $1", srv)}])
    return res


_SAFE_TYPE = re.compile(r"^[a-z][a-z0-9 ,()]*$")


async def _create_public_view(conn, private: str, schema: str, table: str, cols: list[str],
                              roles, *, source_relation: tuple | None = None) -> None:
    sel = ", ".join("%I" for _ in cols)
    rel = source_relation or (private, table)
    await exec_fmt(conn, f"CREATE VIEW %I.%I AS SELECT {sel} FROM %I.%I",
                   schema, table, *cols, *rel)
    await exec_fmt(conn, "GRANT SELECT ON %I.%I TO %I", schema, table, roles.reader)


async def _create_tds_wrapper(conn, private: str, foreign_table: str, table: str,
                              cols: list[str], roles) -> None:
    """The owner SECURITY DEFINER wrapper function and its privileges (no view)."""
    wrapper = tds_wrapper_name(table)
    t_fn, t_revoke, t_grant, _ = tds_wrapper_templates()
    body = await conn.fetchval(
        "SELECT format('SELECT %s FROM %I.%I', (SELECT string_agg(quote_ident(c), ', ' "
        "ORDER BY n) FROM unnest($1::text[]) WITH ORDINALITY AS u(c, n)), $2::text, $3::text)",
        cols, private, foreign_table)
    await exec_fmt(conn, t_fn, private, wrapper, private, foreign_table, body)
    await exec_fmt(conn, t_revoke, private, wrapper)
    await exec_fmt(conn, t_grant, private, wrapper, roles.reader)


async def _create_tds_lookup(conn, private: str, foreign_table: str, schema: str, table: str,
                             cols: list[str], roles) -> None:
    # table is the public VIEW name (the warehouse name); foreign_table keeps the remote name
    """Wrapper function, privileges, then the public view over it (four templates, in order)."""
    wrapper = tds_wrapper_name(table)
    await _create_tds_wrapper(conn, private, foreign_table, table, cols, roles)
    t_view = tds_wrapper_templates()[3]
    sel = ", ".join("%I" for _ in cols)
    await exec_fmt(conn, t_view.replace("{cols}", sel), schema, table, *cols, private, wrapper)
    await exec_fmt(conn, "GRANT SELECT ON %I.%I TO %I", schema, table, roles.reader)


async def _drop_live(conn, kind: str, schema: str, private: str, table: str, *,
                     view: str | None = None) -> None:
    await exec_fmt(conn, "DROP VIEW IF EXISTS %I.%I", schema, view or table)
    # both tds_fdw and mysql_fdw tables are read through the owner's wrapper (see _create_tds_lookup)
    await exec_fmt(conn, "DROP FUNCTION IF EXISTS %I.%I()", private,
                   tds_wrapper_name(view or table))
    await exec_fmt(conn, "DROP FOREIGN TABLE IF EXISTS %I.%I", private, table)


async def _live_remote(conn, name: str, cred: dict, tables: list[dict], probe, roles,
                       kind: str) -> dict:
    """MySQL (mysql_fdw) and SQL Server (tds_fdw, small lookups through the wrapper)."""
    srv, sch = server_name(name), src_schema(name)
    res = _live_result()
    wrapper = "mysql_fdw" if kind == "mysql" else "tds_fdw"
    mapper = mysql_pg_type if kind == "mysql" else mssql_pg_type
    desired: dict = {}
    if tables and not await _ext_present(conn, wrapper):
        why = MYSQL_FDW_MISSING if kind == "mysql" else MSSQL_TDS_MISSING
        res["unlinked"] += [f"{t['warehouse_table']}: {why}" for t in tables]
        tables = []
    default_schema = cred["database"] if kind == "mysql" else "dbo"
    tls_why = live_tls_refusal(kind, cred.get("options"))  # Quick 028 (WR-04)
    if tls_why and tables:
        res["unlinked"] += [f"{t['warehouse_table']}: {tls_why}" for t in tables]
        tables = []
    for t in tables:
        wt = t["warehouse_table"]
        pt = _find_probe_table(probe, t) or {}
        est = pt.get("est_rows") if pt.get("est_rows") is not None else t.get("est_rows")
        if not live_rows_allowed(kind, est):  # Quick 028 (WR-03): MySQL capped like SQL Server
            res["unlinked"].append(f"{wt}: {live_rows_refusal(kind)}")
            continue
        rschema = t.get("remote_schema") or pt.get("schema") or default_schema
        mapped = [{"name": c["name"], "type": mapper(c.get("type"))} for c in pt.get("columns") or []]
        usable = [c for c in mapped if c["type"] and _SAFE_TYPE.match(c["type"])]
        if not usable:
            res["unlinked"].append(f"{wt}: {NO_MAPPABLE}")
            continue
        skipped = [{"column": c["name"], "reason": "type not mapped"} for c in mapped
                   if c not in usable]
        desired[(rschema, t["remote_table"])] = {
            "t": t, "cols": usable, "skipped": skipped, "hash": mapped_col_hash(mapped),
            "private": whlink.private_schema(srv, rschema)}
    rows = {(r["remote_schema"], r["remote_table"]): dict(r) for r in await conn.fetch(
        "SELECT remote_schema, remote_table, col_hash, private_schema, "
        "COALESCE(view_name, remote_table) AS view_name FROM ic_meta.link_objects "
        "WHERE source = $1", srv)}
    if desired or rows:
        if kind == "mysql":
            await _ensure_fdw_server(conn, srv, wrapper, {"host": cred["host"],
                                                           "port": str(cred["port"])},
                                     {"username": cred["user"], "password": cred.get("password") or ""})
        else:
            await _ensure_fdw_server(conn, srv, wrapper, {
                "servername": cred["host"], "port": str(cred["port"]),
                "database": cred["database"], "tds_version": "7.4"},
                {"username": cred["user"], "password": cred.get("password") or ""})
    for key in sorted(k for k in rows if k not in desired):
        priv = rows[key]["private_schema"]

        async def remove(key=key, priv=priv):
            await _drop_live(conn, kind, sch, priv, key[1], view=rows[key].get("view_name") or key[1])
            await whlink._delete_link_object(conn, srv, key[0], key[1])
        try:
            await _tx(conn, remove)
            res["dropped"] += 1
        except asyncpg.PostgresError as e:
            log.warning("drop of live table %s blocked: %s", key[1], type(e).__name__)
            res["stale"].append(key[1])
    for key, d in sorted(desired.items()):
        have = rows.get(key)
        wt = d["t"]["warehouse_table"]
        if have and have["col_hash"] == d["hash"] and (have.get("view_name") or key[1]) == wt:
            continue
        priv, table = d["private"], key[1]
        names = [c["name"] for c in d["cols"]]

        async def make(d=d, key=key, priv=priv, table=table, names=names, wt=wt, have=have):
            if have:
                await _drop_live(conn, kind, sch, have["private_schema"], table,
                                 view=have.get("view_name") or key[1])
            if not await _schema_exists(conn, priv):
                await exec_fmt(conn, "CREATE SCHEMA %I AUTHORIZATION CURRENT_USER", priv)
            defs = ", ".join("%I " + c["type"] for c in d["cols"])
            vals = [priv, table] + [c["name"] for c in d["cols"]]
            if kind == "mysql":
                await exec_fmt(conn, f"CREATE FOREIGN TABLE %I.%I ({defs}) SERVER %I OPTIONS "
                                     "(dbname %L, table_name %L, truncatable 'false')",
                               *vals, srv, key[0], table)
                # mysql_fdw, like tds_fdw, resolves the mapping of the CURRENT user (observed on
                # 2.9.3: a plain owner view fails for wh_reader with "user mapping not found"),
                # so the view reads through the owner's SECURITY DEFINER wrapper as well
                await _create_tds_lookup(conn, priv, table, sch, wt, names, roles)
            else:
                await exec_fmt(conn, f"CREATE FOREIGN TABLE %I.%I ({defs}) SERVER %I OPTIONS "
                                     "(schema_name %L, table_name %L)", *vals, srv, key[0], table)
                await _create_tds_lookup(conn, priv, table, sch, wt, names, roles)
            await whlink._upsert_link_object(conn, srv, key[0], table, priv, d["hash"], d["skipped"],
                                             view_name=wt)
        try:
            await _tx(conn, make)
            res["replaced" if have else "created"] += 1
        except (asyncpg.PostgresError, ValueError) as e:
            log.warning("live table %s not applied: %s", table, type(e).__name__)
            res["stale"].append(table)
    res["live"] = await conn.fetchval(
        "SELECT count(*) FROM ic_meta.link_objects WHERE source = $1", srv)
    return res


async def _live_mysql(conn, name, cred, tables, probe, roles) -> dict:
    return await _live_remote(conn, name, cred, tables, probe, roles, "mysql")


async def _live_mssql(conn, name, cred, tables, probe, roles) -> dict:
    return await _live_remote(conn, name, cred, tables, probe, roles, "mssql")


# ── Provision ───────────────────────────────────────────────────────────────
def _cred_name(src: dict) -> str | None:
    c = src.get("credential")
    return c.removeprefix("cred:") if c else None


async def provision(name: str, roles=DEFAULT_ROLES) -> dict:
    """Create or reconcile every warehouse object of a registered source. Never raises."""
    pw = ""
    try:
        if not warehouse.warehouse_mode():
            return {"status": "error", "detail": "warehouse not configured"}
        src = await sourcesvc.get_source(name)
        if src is None:
            return {"status": "error", "detail": "unknown source"}
        probe = await _load_probe(src)
        cred = None
        if src["kind"] != "csv":
            cred = await sourcevault.load(_cred_name(src))
            pw = cred.get("password") or ""
        conn = await _owner_conn(roles)
    except Exception as e:
        return {"status": "error", "detail": whlink._error_detail(e, (pw,))}
    try:
        if not await _lock(conn, name):
            return {"status": "busy"}
        try:
            return await _provision(conn, name, src, probe, cred, roles)
        finally:
            await _unlock(conn, name)
    except Exception as e:
        log.warning("provision of %s failed: %s", name, type(e).__name__)
        return {"status": "error", "detail": whlink._error_detail(e, (pw,)),
                "live": 0, "import_views": 0, "stale": [], "unlinked": []}
    finally:
        try:
            await conn.close()
        except Exception:
            pass


async def _provision(conn, name, src, probe, cred, roles) -> dict:
    await whlink.ensure_meta(conn, roles)
    await ensure_public_schema(conn, name, roles)
    tables = [t for t in src.get("tables") or [] if t.get("status") != "missing"]
    imports = [t for t in tables if t.get("mode") == "import"]
    lives = [t for t in tables if t.get("mode") == "live"]
    if imports:
        await ensure_import_schemas(conn, name, roles)
    live = _live_result()
    kind = src["kind"]
    if lives or await conn.fetchval("SELECT 1 FROM ic_meta.link_objects WHERE source = $1",
                                    server_name(name)):
        if kind == "postgres":
            live = await _live_postgres(conn, name, cred, lives, roles)
        elif kind == "mysql":
            live = await _live_mysql(conn, name, cred, lives, probe, roles)
        elif kind == "mssql":
            live = await _live_mssql(conn, name, cred, lives, probe, roles)
        else:
            live["unlinked"] = [f"{t['warehouse_table']}: live is not available for {kind}"
                                for t in lives]
    views = await _refresh_views(conn, name, tables, None, roles) if imports else \
        {"refreshed": 0, "stale": [], "missing": []}
    await write_catalog(conn, name, probe)
    stale = live["stale"] + views["stale"]
    unlinked = live["unlinked"]
    return {"status": "partial" if (stale or unlinked) else "ok", "live": live["live"],
            "import_views": views["refreshed"], "stale": stale, "unlinked": unlinked,
            "detail": ("; ".join(unlinked + [f"stale: {s}" for s in stale]) or None)}


# ── Mapping refresh and re-provision checks ─────────────────────────────────
async def refresh_mapping(credential: str, roles=DEFAULT_ROLES) -> dict:
    """Update the user mapping of every active source with live tables that uses `credential`."""
    cname = (credential or "").removeprefix("cred:")
    done, failed = [], []
    try:
        srcs = [s for s in await sourcesvc.list_sources()
                if s.get("status") == "active" and _cred_name(s) == cname
                and any(t.get("mode") == "live" and t.get("status") != "missing"
                        for t in s.get("tables") or [])]
        if not srcs or not warehouse.warehouse_mode():
            return {"status": "ok", "updated": []}
        cred = await sourcevault.load(cname)
    except Exception as e:
        return {"status": "error", "detail": whlink._error_detail(e), "updated": []}
    pw = cred.get("password") or ""
    conn = await _owner_conn(roles)
    try:
        for s in srcs:
            srv = server_name(s["name"])
            try:
                if live_tls_refusal(s["kind"], cred.get("options")):  # Quick 028 (WR-04)
                    failed.append(s["name"])
                    continue
                if s["kind"] == "postgres":
                    await whlink.ensure_server(conn, srv, _link_opts(cred))
                elif s["kind"] == "mysql":
                    await _ensure_fdw_server(
                        conn, srv, "mysql_fdw",
                        {"host": cred["host"], "port": str(cred["port"])},
                        {"username": cred["user"], "password": pw})
                elif s["kind"] == "mssql":
                    await _ensure_fdw_server(
                        conn, srv, "tds_fdw",
                        {"servername": cred["host"], "port": str(cred["port"]),
                         "database": cred["database"], "tds_version": "7.4"},
                        {"username": cred["user"], "password": pw})
                done.append(s["name"])
            except Exception as e:
                log.warning("mapping refresh of %s failed: %s", s["name"], type(e).__name__)
                failed.append(s["name"])
    finally:
        await conn.close()
    return {"status": "partial" if failed else "ok", "updated": done, "failed": failed}


async def missing_objects(names: list[str], roles=DEFAULT_ROLES) -> list[str]:
    """Source names whose src_<name> schema is missing in the warehouse (lost volume)."""
    if not names or not warehouse.warehouse_mode():
        return []
    try:
        conn = await _owner_conn(roles)
    except Exception:
        return []
    try:
        have = {r["nspname"] for r in await conn.fetch(
            "SELECT nspname FROM pg_namespace WHERE nspname = ANY($1::text[])",
            [src_schema(n) for n in names])}
        return [n for n in names if src_schema(n) not in have]
    except Exception:
        return []
    finally:
        await conn.close()


# ── Phase 17 (CONS-05): live relations for table mode flips ─────────────────
# A flip never touches the view's name: these helpers create or reuse only the foreign table
# (and wrapper function) a live view reads, and remove them without ever dropping the view.
class LiveRel(tuple):
    """(schema, relation, is_function, cols_rel): the FROM target of a live view."""
    __slots__ = ()

    def __new__(cls, schema, relation, is_function=False, cols_rel=None):
        return super().__new__(cls, (schema, relation, bool(is_function), cols_rel))

    schema = property(lambda s: s[0])
    relation = property(lambda s: s[1])
    is_function = property(lambda s: s[2])
    cols_rel = property(lambda s: s[3])


async def link_keys(source: str) -> set:
    """(remote_schema, remote_table) pairs with a live link object (read only, owner login)."""
    conn = await _owner_conn(DEFAULT_ROLES)
    try:
        srv = source if source == warehouse.SOURCE else server_name(source)
        return {(r["remote_schema"], r["remote_table"]) for r in await conn.fetch(
            "SELECT remote_schema, remote_table FROM ic_meta.link_objects WHERE source = $1", srv)}
    finally:
        await conn.close()


async def extensions_present(names) -> set:
    """Which of the named extensions exist in the warehouse; empty when it cannot be asked."""
    try:
        conn = await _owner_conn(DEFAULT_ROLES)
    except Exception:
        return set()
    try:
        return {n for n in names if await _ext_present(conn, n)}
    except Exception:
        return set()
    finally:
        await conn.close()


async def _bdb_live_relation(conn, name: str, t: dict, roles) -> LiveRel:
    key = (t.get("remote_schema"), t["remote_table"])
    row = await conn.fetchrow(
        "SELECT private_schema, col_hash FROM ic_meta.link_objects WHERE source = $1 "
        "AND remote_schema = $2 AND remote_table = $3", name, *key)
    if row is None:
        raise ValueError("the table has no live link")
    cat = await conn.fetchrow(
        "SELECT col_hash FROM ic_meta.source_catalog WHERE source = $1 AND schema_name = $2 "
        "AND table_name = $3", name, *key)
    if cat is None or cat["col_hash"] == row["col_hash"]:
        return LiveRel(row["private_schema"], key[1])
    # The remote table changed while it was imported (keep-keys stop the link sweep from
    # following it). Only the unused kept foreign table is recreated, never the view.
    opts = whlink.parse_source_url(warehouse._business_url())
    snap = await whlink.fetch_snapshot(conn, name, opts)
    cur = next((r for r in snap["tables"] if (r["schema_name"], r["table_name"]) == key), None)
    if cur is None:
        raise ValueError("the table is not visible to the business database login")
    priv = row["private_schema"]

    async def make():
        await exec_fmt(conn, "DROP FOREIGN TABLE IF EXISTS %I.%I", priv, key[1])
        p, _cols, unl = await whlink._create_foreign_table(conn, name, cur, roles, mirror_names=True)
        await whlink._upsert_link_object(conn, name, key[0], key[1], p, cur["col_hash"], unl)
        return p
    return LiveRel(await _tx(conn, make), key[1])


async def _pg_live_relation(conn, name: str, t: dict, cred: dict, roles) -> LiveRel:
    srv = server_name(name)
    opts = _link_opts(cred)
    await whlink.ensure_server(conn, srv, opts)
    await whlink.ensure_catalog_tables(conn, srv, roles)
    snap = await whlink.fetch_snapshot(conn, srv, opts)
    key = (t.get("remote_schema") or "public", t["remote_table"])
    cur = next((r for r in snap["tables"] if (r["schema_name"], r["table_name"]) == key), None)
    if cur is None:
        raise ValueError("the table is not visible to the source login")
    have = await conn.fetchrow(
        "SELECT private_schema, col_hash FROM ic_meta.link_objects WHERE source = $1 "
        "AND remote_schema = $2 AND remote_table = $3", srv, *key)
    if have is not None and have["col_hash"] == cur["col_hash"]:
        return LiveRel(have["private_schema"], key[1])
    wt = t["warehouse_table"]
    priv = (have or {}).get("private_schema") or whlink.private_schema(srv, key[0])

    async def make():
        await exec_fmt(conn, "DROP FOREIGN TABLE IF EXISTS %I.%I", priv, key[1])
        p, _cols, unl = await whlink._create_foreign_table(conn, srv, cur, roles, mirror_names=False)
        await whlink._upsert_link_object(conn, srv, key[0], key[1], p, cur["col_hash"], unl,
                                         view_name=wt)
        return p
    return LiveRel(await _tx(conn, make), key[1])


async def _remote_live_relation(conn, name: str, t: dict, cred: dict, probe, roles,
                                kind: str) -> LiveRel:
    srv = server_name(name)
    wrapper = "mysql_fdw" if kind == "mysql" else "tds_fdw"
    mapper = mysql_pg_type if kind == "mysql" else mssql_pg_type
    if not await _ext_present(conn, wrapper):
        raise ValueError(MYSQL_FDW_MISSING if kind == "mysql" else MSSQL_TDS_MISSING)
    wt = t["warehouse_table"]
    pt = _find_probe_table(probe, t) or {}
    tls_why = live_tls_refusal(kind, cred.get("options"))  # Quick 028 (WR-04)
    if tls_why:
        raise ValueError(tls_why)
    est = pt.get("est_rows") if pt.get("est_rows") is not None else t.get("est_rows")
    if not live_rows_allowed(kind, est):  # Quick 028 (WR-03)
        raise ValueError(live_rows_refusal(kind))
    rschema = t.get("remote_schema") or pt.get("schema") or (
        cred["database"] if kind == "mysql" else "dbo")
    mapped = [{"name": c["name"], "type": mapper(c.get("type"))} for c in pt.get("columns") or []]
    usable = [c for c in mapped if c["type"] and _SAFE_TYPE.match(c["type"])]
    if not usable:
        raise ValueError(NO_MAPPABLE)
    skipped = [{"column": c["name"], "reason": "type not mapped"} for c in mapped if c not in usable]
    h = mapped_col_hash(mapped)
    priv, table = whlink.private_schema(srv, rschema), t["remote_table"]
    if kind == "mysql":
        await _ensure_fdw_server(conn, srv, wrapper, {"host": cred["host"], "port": str(cred["port"])},
                                 {"username": cred["user"], "password": cred.get("password") or ""})
    else:
        await _ensure_fdw_server(conn, srv, wrapper, {
            "servername": cred["host"], "port": str(cred["port"]),
            "database": cred["database"], "tds_version": "7.4"},
            {"username": cred["user"], "password": cred.get("password") or ""})
    have = await conn.fetchrow(
        "SELECT private_schema, col_hash FROM ic_meta.link_objects WHERE source = $1 "
        "AND remote_schema = $2 AND remote_table = $3", srv, rschema, table)
    fn = tds_wrapper_name(wt)
    if have is not None and have["col_hash"] == h:
        return LiveRel(have["private_schema"], fn, True, (have["private_schema"], table))
    names = [c["name"] for c in usable]

    async def make():
        if have is not None:
            await exec_fmt(conn, "DROP FUNCTION IF EXISTS %I.%I()", have["private_schema"], fn)
            await exec_fmt(conn, "DROP FOREIGN TABLE IF EXISTS %I.%I", have["private_schema"], table)
        if not await _schema_exists(conn, priv):
            await exec_fmt(conn, "CREATE SCHEMA %I AUTHORIZATION CURRENT_USER", priv)
        defs = ", ".join("%I " + c["type"] for c in usable)
        vals = [priv, table] + names
        if kind == "mysql":
            await exec_fmt(conn, f"CREATE FOREIGN TABLE %I.%I ({defs}) SERVER %I OPTIONS "
                                 "(dbname %L, table_name %L, truncatable 'false')",
                           *vals, srv, rschema, table)
        else:
            await exec_fmt(conn, f"CREATE FOREIGN TABLE %I.%I ({defs}) SERVER %I OPTIONS "
                                 "(schema_name %L, table_name %L)", *vals, srv, rschema, table)
        await _create_tds_wrapper(conn, priv, table, wt, names, roles)
        await whlink._upsert_link_object(conn, srv, rschema, table, priv, h, skipped, view_name=wt)
    await _tx(conn, make)
    return LiveRel(priv, fn, True, (priv, table))


async def live_relation(conn, name: str, table_row: dict, cred: dict | None, roles=DEFAULT_ROLES,
                        *, kind: str | None = None, probe: dict | None = None) -> LiveRel:
    """Create or reuse the live relation a view over this table reads, WITHOUT touching the
    view. business_db reuses its kept foreign table (recreated first when the remote table's
    columns changed while it was imported)."""
    if name == warehouse.SOURCE:
        return await _bdb_live_relation(conn, name, table_row, roles)
    if cred is None:
        raise ValueError("no credential for the source")
    if kind == "postgres":
        return await _pg_live_relation(conn, name, table_row, cred, roles)
    if kind in ("mysql", "mssql"):
        return await _remote_live_relation(conn, name, table_row, cred, probe, roles, kind)
    raise ValueError("live is not available for this source kind")


async def drop_live_relation(conn, name: str, table_row: dict, roles=DEFAULT_ROLES) -> None:
    """Drop the wrapper function and foreign table of a registered source's table and its link
    object. Never the view, never CASCADE. business_db keeps its foreign table."""
    if name == warehouse.SOURCE:
        return
    srv = server_name(name)
    key = (table_row.get("remote_schema"), table_row["remote_table"])
    row = await conn.fetchrow(
        "SELECT private_schema FROM ic_meta.link_objects WHERE source = $1 "
        "AND remote_table = $3 AND ($2::text IS NULL OR remote_schema = $2)", srv, *key)
    if row is None:
        return

    async def go():
        await exec_fmt(conn, "DROP FUNCTION IF EXISTS %I.%I()", row["private_schema"],
                       tds_wrapper_name(table_row["warehouse_table"]))
        await exec_fmt(conn, "DROP FOREIGN TABLE IF EXISTS %I.%I", row["private_schema"], key[1])
        await conn.execute(
            "DELETE FROM ic_meta.link_objects WHERE source = $1 AND remote_table = $3 "
            "AND ($2::text IS NULL OR remote_schema = $2)", srv, *key)
    await _tx(conn, go)
# ── End Phase 17 (CONS-05) live relations ──


# ── Phase 17 (CONS-06): abandoned stage_* schema pruning ────────────────────

def _stage_owner_names(schema: str) -> list[str]:
    """stage_<name> or stage_<name>_staging -> every possible owner source name.

    A source may itself be called <x>_staging, so a trailing _staging is ambiguous.
    """
    if not schema.startswith("stage_"):
        return []
    name = schema[len("stage_"):]
    out = [name] if name else []
    if name.endswith("_staging") and name[: -len("_staging")]:
        out.append(name[: -len("_staging")])
    return out


async def prune_stage_schemas(dry_run: bool, keep_days: int = 7,
                              roles=DEFAULT_ROLES) -> dict:
    """Drop abandoned stage_<name> / stage_<name>_staging test-copy schemas.

    Kept while a proposed source_change exists for <name> or a source test probe
    for <name> is newer than keep_days. Dry run lists and counts only. Drops run
    as the schema owner under the per-source advisory lock, never with CASCADE.
    """
    out: dict = {"schemas": [], "stage_schemas": 0, "stage_tables": 0, "busy": []}
    if not warehouse.warehouse_mode():
        return out
    pool = await get_pool()
    pending = {r["n"] for r in await pool.fetch(
        "SELECT payload->>'name' AS n FROM actions "
        "WHERE kind='source_change' AND status='proposed'")}
    recent = {r["n"] for r in await pool.fetch(
        "SELECT DISTINCT COALESCE(name, target) AS n FROM source_probes "
        "WHERE kind='test' AND created_at > now() - make_interval(days => $1)",
        int(keep_days))}
    keep = pending | recent
    conn = await _owner_conn(roles)
    try:
        schemas = [r["nspname"] for r in await conn.fetch(
            "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'stage\_%' "
            "ORDER BY nspname")]
        locked: dict[str, bool] = {}
        for sch in schemas:
            names = _stage_owner_names(sch)
            if not names or any(n in keep for n in names):
                continue
            rels = await conn.fetch(
                "SELECT c.relname, c.relkind::text AS relkind FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = $1 AND c.relkind IN ('v','m','r','p','f')", sch)
            ntables = sum(1 for r in rels if r["relkind"] in ("r", "p"))
            if not dry_run:
                busy = False
                for name in names:
                    if name not in locked:
                        locked[name] = await _lock(conn, name)
                    if not locked[name]:
                        busy = True
                        if name not in out["busy"]:
                            out["busy"].append(name)
                if busy:
                    continue
                for kind_set, verb in ((("v",), "VIEW"), (("m",), "MATERIALIZED VIEW"),
                                       (("r", "p"), "TABLE"), (("f",), "FOREIGN TABLE")):
                    for r in rels:
                        if r["relkind"] in kind_set:
                            await exec_fmt(conn, f"DROP {verb} IF EXISTS %I.%I",
                                           sch, r["relname"])
                for r in await conn.fetch(
                        "SELECT c.relname FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = $1 AND c.relkind = 'S'", sch):
                    await exec_fmt(conn, "DROP SEQUENCE IF EXISTS %I.%I", sch, r["relname"])
                await exec_fmt(conn, "DROP SCHEMA IF EXISTS %I", sch)
            out["schemas"].append(sch)
            out["stage_schemas"] += 1
            out["stage_tables"] += ntables
        for name, got in locked.items():
            if got:
                await _unlock(conn, name)
    finally:
        await conn.close()
    return out
# ── End Phase 17 (CONS-06) ──

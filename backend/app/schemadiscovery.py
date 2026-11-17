"""Read-only schema discovery for the business database.

Every tenant query goes through guardrails.run_readonly_sql; nothing in this
module writes anywhere. Introspection reads pg_catalog (not information_schema)
because information_schema hides PK/FK constraints from a SELECT-only role.
Writes happen only via propose_context_change -> actions row and owner approval.
"""

import hashlib
import json
import logging

from .config import get_settings
from .db import get_pool
from .guardrails import run_readonly_sql
from .guardrails import BusinessDbUnreachable  # Phase 14 (WH-05)
from . import warehouse  # Phase 14 (WH-05)
from . import guardrails  # Quick 031: business_db_configured()

log = logging.getLogger("schemadiscovery")

SOURCE = "business_db"
SAMPLE_ROWS = 3
SAMPLE_CHARS = 60
DEFAULT_MAX_TABLES = 15

CATALOG_SQL = r"""
SELECT n.nspname AS schema_name, c.relname AS table_name, c.relkind,
       obj_description(c.oid, 'pg_class') AS table_comment,
       c.reltuples::bigint AS row_estimate,
       (SELECT json_agg(json_build_object(
                 'name', a.attname,
                 'type', format_type(a.atttypid, a.atttypmod),
                 'nullable', NOT a.attnotnull,
                 'pk', EXISTS (SELECT 1 FROM pg_constraint p
                               WHERE p.conrelid = c.oid AND p.contype = 'p'
                                 AND a.attnum = ANY (p.conkey)),
                 'fk', (SELECT json_build_object('table', rt.relname, 'column', ra.attname)
                        FROM pg_constraint f
                        JOIN pg_class rt ON rt.oid = f.confrelid
                        JOIN pg_attribute ra ON ra.attrelid = f.confrelid
                             AND ra.attnum = f.confkey[array_position(f.conkey, a.attnum)]
                        WHERE f.conrelid = c.oid AND f.contype = 'f'
                          AND a.attnum = ANY (f.conkey) LIMIT 1),
                 'comment', col_description(c.oid, a.attnum))
               ORDER BY a.attnum)
        FROM pg_attribute a
        WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped) AS columns,
       (SELECT json_agg(pg_get_constraintdef(k.oid))
        FROM pg_constraint k WHERE k.conrelid = c.oid AND k.contype = 'c') AS checks
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\_%'
  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND NOT c.relispartition
  AND has_table_privilege(c.oid, 'SELECT')
ORDER BY n.nspname, c.relname
"""

# ── Phase 14 (WH-05, D-17): in warehouse mode discovery reads the link-time snapshot of the SOURCE catalog (views would lose NOT NULL and report object_kind view) ──
WAREHOUSE_CATALOG_SQL = r"""
SELECT s.schema_name, s.table_name, s.relkind, s.table_comment, s.row_estimate,
       s.columns, s.checks
FROM ic_meta.source_catalog s
WHERE s.source = 'business_db'
  AND EXISTS (SELECT 1 FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = s.schema_name AND c.relname = s.table_name
                AND has_table_privilege(c.oid, 'SELECT'))
ORDER BY s.schema_name, s.table_name
"""
# only the column wh_reader may read of ic_meta.link_state
WAREHOUSE_LINK_STATUS_SQL = "SELECT status FROM ic_meta.link_state WHERE source = 'business_db'"
# ── End Phase 14 (WH-05) ──

# ── Phase 15 (SRC-09): discovery over registered sources ──
import re as _re

_SOURCE_RE = _re.compile(r"^[a-z][a-z0-9_]{1,30}$")


def _source_catalog_sql(source: str) -> str:
    """WAREHOUSE_CATALOG_SQL for a registered source. The name is validated before it is
    formatted into the literal (regex excludes quotes and every other special character)."""
    if not isinstance(source, str) or not _SOURCE_RE.match(source):
        raise ValueError(f"invalid source name {source!r}")
    return WAREHOUSE_CATALOG_SQL.replace("s.source = 'business_db'", f"s.source = '{source}'")


async def _registered(source: str) -> bool:
    if not isinstance(source, str) or not _SOURCE_RE.match(source):
        return False
    from . import sourcesvc
    src = await sourcesvc.get_source(source)
    return bool(src) and src.get("status") == "active"


def _ref_source(ref) -> str | None:
    if not isinstance(ref, str) or "." not in ref:
        return None
    return ref.split(".", 1)[0]
# ── End Phase 15 (SRC-09) ──

_KIND = {"r": "table", "p": "table", "v": "view", "m": "matview"}
# Phase 14 (WH-05, D-05): foreign tables; object_kind never enters signature()
_KIND["f"] = "foreign"
_SYSTEM_SCHEMAS = {"pg_catalog", "information_schema", "pg_toast"}


def table_ref(source: str, schema: str, table: str) -> str:
    return f"{source}.{schema}.{table}"


def column_ref(source: str, schema: str, table: str, column: str) -> str:
    return f"{source}.{schema}.{table}.{column}"


def structure_of(col: dict, object_kind: str) -> dict:
    return {"type": col.get("type"), "nullable": bool(col.get("nullable")),
            "pk": bool(col.get("pk")), "fk": col.get("fk"), "object_kind": object_kind}


def signature(structure: dict) -> str:
    core = {k: structure.get(k) for k in ("type", "nullable", "pk", "fk")}
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]


def _decode(v):
    if isinstance(v, str):
        return json.loads(v)
    return v


# ── Quick 031: sources-only installs ──
NO_BUSINESS_DB = ("No business database is configured on this install, so business_db has nothing "
                  "to discover. Discover a registered source instead (get_sync_status lists them), "
                  "or set the business database on the Setup page.")


def business_db_configured() -> bool:
    """True when a business database URL is set (guardrails is the one reader of that rule)."""
    return guardrails._business_url_set()
# ── End Quick 031 ──


async def introspect(source: str = SOURCE) -> dict:
    if source != SOURCE:  # Phase 15 (SRC-09): registered source, read from its catalog rows
        if not warehouse.warehouse_mode():
            raise ValueError("registered sources need the warehouse")
        if not await _registered(source):
            raise ValueError(f"unknown source {source!r}")
        return await _introspect_sql(source, _source_catalog_sql(source))
    # Phase 14 (WH-05, D-19): a never-linked or failing link never serves a snapshot
    if warehouse.warehouse_mode():
        # Quick 031: with no business database configured (a sources-only install) the business_db
        # catalog is empty, so model and context tools still see the registered sources' src_
        # tables. A configured business database whose link is missing or failing still raises.
        if not business_db_configured():
            return {"source": SOURCE, "tables": [], "truncated": False}
        st = await run_readonly_sql(WAREHOUSE_LINK_STATUS_SQL)
        if not st or st[0].get("status") == "error":
            raise BusinessDbUnreachable()
    sql = WAREHOUSE_CATALOG_SQL if warehouse.warehouse_mode() else CATALOG_SQL  # Phase 14 (WH-05)
    return await _introspect_sql(source, sql)


async def _introspect_sql(source: str, sql: str) -> dict:
    rows = await run_readonly_sql(sql)
    tables = []
    for r in rows:
        schema = r["schema_name"]
        if schema in _SYSTEM_SCHEMAS or schema.startswith("pg_"):
            continue
        tables.append({
            "schema": schema,
            "table": r["table_name"],
            "object_kind": _KIND.get(r["relkind"], "table"),
            "comment": r.get("table_comment"),
            "row_estimate": r.get("row_estimate"),
            "columns": _decode(r.get("columns")) or [],
            "checks": _decode(r.get("checks")) or [],
        })
    truncated = len(rows) == get_settings().sql_row_limit
    return {"source": source, "tables": tables, "truncated": truncated}


def _q(ident: str) -> str:
    return '"' + str(ident).replace('"', '""') + '"'


async def sample_values(schema: str, table: str) -> dict[str, list[str]]:
    """Identifiers must come from introspect output, never from tool arguments."""
    try:
        rows = await run_readonly_sql(
            f"SELECT * FROM {_q(schema)}.{_q(table)} LIMIT {SAMPLE_ROWS}")
        out: dict[str, list[str]] = {}
        for row in rows:
            for k, v in row.items():
                if v is None:
                    continue
                out.setdefault(k, []).append(str(v)[:SAMPLE_CHARS])
        return out
    except Exception:
        return {}


async def resolve(ref: str, cat: dict | None = None) -> dict | None:
    """Match a ref against the live catalog in Python; ref text never reaches SQL.
    Pass `cat` (an introspect result) to reuse one catalog snapshot."""
    src = SOURCE
    if isinstance(ref, str) and not ref.startswith(SOURCE + "."):
        # Phase 15 (SRC-09): refs of a registered source; unregistered prefixes never resolve
        src = _ref_source(ref)
        if src is None or not _SOURCE_RE.match(src) or not await _registered(src):
            return None
        if cat is None or cat.get("source") != src:
            cat = await introspect(src)
    if not isinstance(ref, str) or not ref.startswith(src + "."):
        return None
    if cat is None:
        cat = await introspect()
    for t in cat["tables"]:
        tref = table_ref(src, t["schema"], t["table"])
        if ref == tref:
            return {"kind": "table", "source": src, "schema": t["schema"],
                    "table": t["table"], "column": None,
                    "object_kind": t["object_kind"],
                    "structure": {"object_kind": t["object_kind"]},
                    "signature": None, "references_ref": None}
        for c in t["columns"]:
            if ref == column_ref(src, t["schema"], t["table"], c["name"]):
                st = structure_of(c, t["object_kind"])
                fk = c.get("fk")
                return {"kind": "column", "source": src, "schema": t["schema"],
                        "table": t["table"], "column": c["name"],
                        "object_kind": t["object_kind"],
                        "structure": {k: st[k] for k in ("type", "nullable", "pk", "fk")},
                        "signature": signature(st),
                        "references_ref": column_ref(src, t["schema"], fk["table"],
                                                     fk["column"]) if fk else None}
    return None


# -- known state + pure diff ---------------------------------------------

async def known_state(source: str = SOURCE) -> dict:
    pool = await get_pool()
    trows = await pool.fetch(
        """SELECT t.ref, t.description, t.status FROM context_tables t
           JOIN context_sources s ON s.id = t.source_id WHERE s.name = $1""", source)
    crows = await pool.fetch(
        """SELECT c.ref, c.description, c.signature, c.status FROM context_columns c
           JOIN context_tables t ON t.id = c.table_id
           JOIN context_sources s ON s.id = t.source_id WHERE s.name = $1""", source)
    prows = await pool.fetch(
        """SELECT payload->>'kind' AS kind, payload->>'ref' AS ref FROM actions
           WHERE kind = 'context_change' AND status = 'proposed'""")
    return {
        "tables": {r["ref"]: dict(r) for r in trows},
        "columns": {r["ref"]: dict(r) for r in crows},
        "pending": {(r["kind"], r["ref"]) for r in prows},
    }


def diff(live: dict, known: dict) -> dict:
    """Pure: classify live objects as new/changed (listed) or pending/known/hidden
    (counted only); approved rows with no live object are listed as missing."""
    src = live.get("source", SOURCE)
    ktab, kcol, pending = known["tables"], known["columns"], known["pending"]
    counts = {"new": 0, "changed": 0, "pending": 0, "known": 0, "hidden": 0, "missing": 0}
    out_tables = []
    live_refs: set[str] = set()
    for t in live["tables"]:
        tref = table_ref(src, t["schema"], t["table"])
        live_refs.add(tref)
        for c in t["columns"]:
            live_refs.add(column_ref(src, t["schema"], t["table"], c["name"]))
        krow = ktab.get(tref)
        if krow and krow["status"] == "hidden":
            counts["hidden"] += 1
            continue
        approved_t = bool(krow and krow.get("description"))
        needs_table = False
        if approved_t:
            counts["known"] += 1
        elif ("table", tref) in pending:
            counts["pending"] += 1
        else:
            needs_table = True
            counts["new"] += 1
        cols = []
        for c in t["columns"]:
            cref = column_ref(src, t["schema"], t["table"], c["name"])
            kc = kcol.get(cref)
            if kc and kc["status"] == "hidden":
                counts["hidden"] += 1
                continue
            sig = signature(structure_of(c, t["object_kind"]))
            entry = {"ref": cref, "name": c["name"], "type": c.get("type"),
                     "nullable": c.get("nullable"), "pk": c.get("pk"),
                     "fk": c.get("fk"), "comment": c.get("comment")}
            if kc and kc.get("description"):
                if kc.get("signature") == sig:
                    counts["known"] += 1
                elif ("column", cref) in pending:
                    counts["pending"] += 1
                else:
                    counts["changed"] += 1
                    cols.append({**entry, "status": "changed",
                                 "current_description": kc["description"]})
            elif ("column", cref) in pending:
                counts["pending"] += 1
            else:
                counts["new"] += 1
                cols.append({**entry, "status": "new", "current_description": None})
        if needs_table or cols:
            out_tables.append({
                "ref": tref, "schema": t["schema"], "table": t["table"],
                "object_kind": t["object_kind"], "row_estimate": t.get("row_estimate"),
                "comment": t.get("comment"), "checks": t.get("checks") or [],
                "needs_table_description": needs_table, "columns": cols})
    missing = sorted(
        r for r, v in list(ktab.items()) + list(kcol.items())
        if r not in live_refs and v.get("description") and v["status"] != "hidden")
    counts["missing"] = len(missing)
    return {"tables": out_tables, "counts": counts, "missing": missing}


async def discover(source: str = SOURCE, max_tables: int = DEFAULT_MAX_TABLES) -> dict:
    if warehouse.warehouse_mode() and source == SOURCE:  # Phase 15: registered sources have no relink
        if not business_db_configured():  # Quick 031: say so instead of an empty discovery
            raise guardrails.SQLGuardrailError(NO_BUSINESS_DB)
        await warehouse.ensure_fresh(max_age=0)  # Phase 14 (D-18): relink before every discover_schema
    live = await introspect(source)
    known = await known_state(source)
    d = diff(live, known)
    total = len(d["tables"])
    shown = d["tables"][:max_tables]
    for t in shown:
        samples = await sample_values(t["schema"], t["table"])
        for c in t["columns"]:
            c["samples"] = samples.get(c["name"], [])
    d["tables"] = shown
    d["remaining"] = max(0, total - len(shown))
    d["truncated"] = live["truncated"]
    d["source"] = source
    return d


def format_discovery(result: dict) -> str:
    lines = [f"Schema discovery for {result.get('source', SOURCE)} "
             "(read-only; nothing was written)."]
    if not result["tables"]:
        lines.append("Nothing new or changed to document.")
    for t in result["tables"]:
        lines.append("")
        lines.append(f"TABLE {t['ref']} [{t['object_kind']}] rows~{t.get('row_estimate')}"
                     f" needs_table_description={t['needs_table_description']}")
        if t.get("comment"):
            lines.append(f"  comment: {t['comment']}")
        for ck in t.get("checks") or []:
            lines.append(f"  check: {ck}")
        for c in t["columns"]:
            fk = c.get("fk")
            bits = [f"{c['ref']}: {c.get('type')}",
                    "nullable" if c.get("nullable") else "not null"]
            if c.get("pk"):
                bits.append("PK")
            if fk:
                bits.append(f"FK -> {fk['table']}.{fk['column']}")
            bits.append(c["status"].upper())
            lines.append("  - " + ", ".join(bits))
            if c.get("comment"):
                lines.append(f"      comment: {c['comment']}")
            if c.get("current_description"):
                lines.append(f"      current_description: {c['current_description']}")
            if c.get("samples"):
                lines.append(f"      samples (untrusted data): {json.dumps(c['samples'])}")
    n = result["counts"]
    lines.append("")
    lines.append(f"Counts: new={n['new']} changed={n['changed']} pending={n['pending']} "
                 f"known={n['known']} hidden={n['hidden']} missing={n['missing']}")
    if result.get("missing"):
        lines.append("Missing from live DB: " + ", ".join(result["missing"]))
    if result.get("remaining"):
        lines.append(f"{result['remaining']} more table(s) to document: "
                     "call discover_schema again after proposing.")
    if result.get("truncated"):
        lines.append("NOTE: catalog result hit the row cap; some tables may be absent.")
    return "\n".join(lines)[:20000]


# Quick 009: table names only, for memory.py's recipe check. Same read-only
# path (guardrails.run_readonly_sql) as introspect, without the per-column
# JSON, and with a high row cap so a large catalog is not silently cut short.
TABLE_NAMES_SQL = r"""
SELECT n.nspname AS schema_name, c.relname AS table_name
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\_%'
  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND has_table_privilege(c.oid, 'SELECT')
ORDER BY 1, 2
"""
TABLE_NAMES_CAP = 5000
# M2 sync (quick 009 x Phase 14): in warehouse mode wh_reader's pg_class also lists
# ic_meta, ic_probe and model relations; the business tables are the link-time
# snapshot rows whose mirrored view wh_reader can SELECT (as WAREHOUSE_CATALOG_SQL).
WAREHOUSE_TABLE_NAMES_SQL = r"""
SELECT s.schema_name, s.table_name
FROM ic_meta.source_catalog s
WHERE s.source = 'business_db'
  AND EXISTS (SELECT 1 FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = s.schema_name AND c.relname = s.table_name
                AND has_table_privilege(c.oid, 'SELECT'))
ORDER BY 1, 2
"""


async def table_names() -> tuple[list[tuple[str, str]], bool]:
    """(schema, table) pairs the read-only role can SELECT, plus whether the
    listing hit TABLE_NAMES_CAP (incomplete). Raises when the tenant DB is
    unreachable or not configured; callers decide what that means."""
    sql = WAREHOUSE_TABLE_NAMES_SQL if warehouse.warehouse_mode() else TABLE_NAMES_SQL
    rows = await run_readonly_sql(sql, limit=TABLE_NAMES_CAP)
    pairs = [(str(r["schema_name"]), str(r["table_name"])) for r in rows]
    return pairs, len(rows) >= TABLE_NAMES_CAP


# Quick 012: approximate table sizes, so the agents can tell a quick lookup on a
# small table from a heavy scan of a large one. pg_class.reltuples is the
# planner's estimate (kept current by autovacuum/ANALYZE): no table is scanned.
# Same read-only path as everything else here; largest first so a row cap keeps
# the tables that matter.
ROW_ESTIMATE_SQL = r"""
SELECT c.relname AS table_name, max(c.reltuples)::bigint AS row_estimate
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\_%'
  AND c.relkind IN ('r', 'p', 'm')
  AND has_table_privilege(c.oid, 'SELECT')
GROUP BY c.relname
ORDER BY 2 DESC
"""
# M2 sync (quick 012 x Phase 14): in warehouse mode the business tables are views
# over foreign tables, so the warehouse's own pg_class.reltuples knows nothing of
# them. The link-time snapshot carries the SOURCE reltuples (whlink refreshes it
# when it changes); same relkinds and privilege rule as the direct path.
WAREHOUSE_ROW_ESTIMATE_SQL = r"""
SELECT s.table_name, max(s.row_estimate)::bigint AS row_estimate
FROM ic_meta.source_catalog s
WHERE s.source = 'business_db'
  AND s.relkind IN ('r', 'p', 'm')
  AND EXISTS (SELECT 1 FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = s.schema_name AND c.relname = s.table_name
                AND has_table_privilege(c.oid, 'SELECT'))
GROUP BY s.table_name
ORDER BY 2 DESC
"""
ROW_ESTIMATE_TTL = 600      # seconds a size map is reused
ROW_ESTIMATE_RETRY = 60     # seconds before retrying after a failed read
LARGE_TABLE_ROWS = 100_000  # at or above this a table is called large
_row_estimates: tuple[float, dict[str, tuple[str, int]]] | None = None


def reset_row_estimates() -> None:
    global _row_estimates
    _row_estimates = None


async def row_estimates() -> dict[str, tuple[str, int]]:
    """{bare lowercase table name: (name as stored, approximate rows)} for the
    tables the read-only role can SELECT. Never-analysed tables (reltuples
    below 0) are left out. Cached ROW_ESTIMATE_TTL s; {} on any error (cached
    ROW_ESTIMATE_RETRY s, so an unreachable DB is not asked on every search)."""
    import time
    global _row_estimates
    now = time.monotonic()
    if _row_estimates is not None and now < _row_estimates[0]:
        return _row_estimates[1]
    try:
        sql = WAREHOUSE_ROW_ESTIMATE_SQL if warehouse.warehouse_mode() else ROW_ESTIMATE_SQL
        rows = await run_readonly_sql(sql, limit=TABLE_NAMES_CAP)
        out: dict[str, tuple[str, int]] = {}
        for r in rows:
            name, n = str(r["table_name"]), r.get("row_estimate")
            if n is None or int(n) < 0:
                continue
            out.setdefault(name.lower(), (name, int(n)))
        _row_estimates = (now + ROW_ESTIMATE_TTL, out)
    except Exception:
        log.debug("row estimates unavailable", exc_info=True)
        out = {}
        _row_estimates = (now + ROW_ESTIMATE_RETRY, out)
    return out


# Phase 11: WR-04 -- resolve with the catalog's truncation flag.
async def resolve_checked(ref: str) -> tuple[dict | None, bool]:
    """Introspect once; return (resolved, catalog_truncated_at_sql_row_limit)."""
    cat = await introspect(_ref_source(ref) if _ref_source(ref) and not ref.startswith(SOURCE + ".")
                           and await _registered(_ref_source(ref)) else SOURCE)
    return await resolve(ref, cat), bool(cat.get("truncated"))

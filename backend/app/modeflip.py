"""Phase 17 (CONS-05): table storage mode switches. Agents propose; the source_change executor
(op mode) applies; advance() cuts over after the backfill. Warehouse DDL only through
whsources/whlink.

A switch never changes the warehouse name or the column signature of a table's view, and no
view is ever dropped: queries, dashboard versions, watches, insights and model objects keep
working while a table moves between import and live.
"""
import contextlib
import json
import logging
import re

import asyncpg

from . import freshness, modelcompiler, schemadiscovery, sourcesvc, whlink, whsources
from .config import get_settings
from .db import get_pool
from .warehouse import DEFAULT_ROLES

log = logging.getLogger("modeflip")

MODES = ("import", "live")
MAX_TABLES = 20
CURSOR_TYPES = ("date", "timestamp", "timestamptz", "integer", "bigint", "smallint", "numeric")
KEEP_LIVE_NOTE = ("The table keeps reading live until the first full copy finishes; then it "
                  "switches to the copy under the same name.")
OFFHOURS_NOTE = ("Large first copies read the client's production database; prefer a schedule "
                 "window outside business hours.")
_REQ_KEYS = {"table", "schema", "mode", "cursor", "merge_key", "reconcile"}
_CURSOR_RE = re.compile(
    r"^(date|datetime2?|smalldatetime|timestamp(tz)?|timestamp\s*(\(\d+\))?\s*(with|without)\s+time\s+zone|"
    r"int|integer|bigint|smallint|mediumint|tinyint|serial|bigserial|number)\b", re.I)
_NUMERIC_RE = re.compile(r"^(numeric|decimal)\s*(\(\s*\d+\s*(,\s*0\s*)?\))?$", re.I)
BUSY_TEXT = "busy; ask the agent to propose the switch again"


def _cursor_type_ok(t) -> bool:
    t = (t or "").strip()
    return bool(_CURSOR_RE.match(t) or _NUMERIC_RE.match(t))


def _str(v) -> bool:
    return isinstance(v, str) and 0 < len(v) <= 128 and "\x00" not in v


def validate_mode_change(source, tables_req, catalog, link_keys, settings, *,
                         has_ext=(), read_only=True, tls=False) -> tuple[dict | None, str | None]:
    """Pure. source is the get_source dict (with tables); catalog the introspect result;
    link_keys the (remote_schema, remote_table) pairs with a live link object. Returns the
    facts (the card payload without notes) or a fixed error text. Names are looked up in the
    registry and the catalog, never trusted as identifiers."""
    if not isinstance(source, dict) or source.get("status") != "active":
        return None, "unknown or inactive source"
    name, kind = source.get("name"), source.get("kind")
    if not isinstance(tables_req, list) or not tables_req:
        return None, "tables must be a non-empty list"
    if len(tables_req) > MAX_TABLES:
        return None, f"at most {MAX_TABLES} tables per request"
    cat = {(t.get("schema"), t.get("table")): t for t in (catalog or {}).get("tables") or []}
    rows = source.get("tables") or []
    is_bdb = name == "business_db"
    out, seen = [], set()
    for req in tables_req:
        if not isinstance(req, dict) or set(req) - _REQ_KEYS:
            return None, "each table is {table, mode, cursor?, merge_key?, reconcile?, schema?}"
        tname, mode = req.get("table"), req.get("mode")
        if not _str(tname):
            return None, "each table needs a table name"
        if mode not in MODES:
            return None, f"table {tname}: mode must be import or live"
        sch = req.get("schema")
        if sch is not None and not _str(sch):
            return None, f"table {tname}: schema must be text"
        cands = [r for r in rows if r.get("warehouse_table") == tname
                 and (sch is None or r.get("warehouse_schema") == sch)] or \
                [r for r in rows if r.get("remote_table") == tname
                 and (sch is None or r.get("warehouse_schema") == sch)]
        if not cands:
            return None, f"unknown table {tname} on source {name}"
        if len(cands) > 1:
            return None, f"table {tname} is ambiguous; name its schema"
        r = cands[0]
        label = f"{r['warehouse_schema']}.{r['warehouse_table']}"
        if r["id"] in seen:
            return None, f"table {label} is listed twice"
        seen.add(r["id"])
        if r.get("status") == "missing":
            return None, f"table {label} is missing from the source"
        cur = r.get("mode")
        if r.get("pending_mode"):
            return None, f"table {label} already has a switch to {r['pending_mode']} pending"
        if mode == cur:
            return None, f"table {label} is already {cur}"
        c = cat.get((r["warehouse_schema"], r["warehouse_table"])) or {}
        cols = {x.get("name"): x for x in c.get("columns") or []}
        est = c.get("row_estimate") if c.get("row_estimate") is not None else r.get("est_rows")
        item = {"table": r["warehouse_table"], "schema": r["warehouse_schema"],
                "remote_schema": r.get("remote_schema"), "remote_table": r["remote_table"],
                "from": cur, "to": mode, "cursor": None, "merge_key": [], "reconcile": False,
                "est_rows": est, "est_mb": c.get("est_mb")}
        if mode == "live":
            if kind == "csv":
                return None, sourcesvc.CSV_IMPORT_ONLY
            if not is_bdb and not read_only:
                return None, f"table {label}: live tables need a read-only login; this login can write"
            if kind in ("mysql", "mssql"):  # Quick 028 (WR-03, WR-04)
                if tls is True:
                    return None, f"table {label}: {whsources.LIVE_TLS_REFUSAL[kind]}"
                if not whsources.live_rows_allowed(kind, est):
                    return None, f"table {label}: {whsources.live_rows_refusal(kind)}"
            if kind == "mssql":
                if "tds_fdw" not in has_ext:
                    return None, f"table {label}: {whsources.MSSQL_TDS_MISSING}"
            if kind == "mysql" and "mysql_fdw" not in has_ext:
                return None, f"table {label}: {whsources.MYSQL_FDW_MISSING}"
            if is_bdb and (r.get("remote_schema"), r["remote_table"]) not in link_keys:
                return None, f"table {label} has no live link to switch back to"
            out.append(item)
            continue
        # to import
        if is_bdb and c.get("object_kind", "table") != "table":
            return None, f"table {label}: only base tables can be imported, not {c.get('object_kind')}"
        cursor = req.get("cursor")
        if cursor is not None:
            if not _str(cursor) or cursor not in cols:
                return None, f"table {label}: cursor column {cursor!r} is not a column of the table"
            if not _cursor_type_ok(cols[cursor].get("type")):
                return None, (f"table {label}: cursor column {cursor} must be a date, timestamp "
                              "or integer column")
        mk = req.get("merge_key")
        if mk is not None:
            if not isinstance(mk, list) or not mk or not all(_str(x) for x in mk):
                return None, f"table {label}: merge_key must be a list of column names"
            bad = [x for x in mk if x not in cols]
            if bad:
                return None, f"table {label}: merge_key column {bad[0]} is not a column of the table"
            key = list(mk)
        else:
            key = [n for n, x in cols.items() if x.get("pk")]
        if not key:
            return None, (f"table {label}: a merge key is needed (it has no primary key); "
                          "name the column(s) that identify a row")
        if cursor is None and est is not None and est > settings.sync_reconcile_max_rows:
            return None, f"table {label}: large tables need a cursor column"
        reconcile = req.get("reconcile", False)
        if not isinstance(reconcile, bool):
            return None, f"table {label}: reconcile must be true or false"
        if reconcile and est is not None and est > settings.sync_reconcile_max_rows:
            return None, (f"table {label}: reconcile is only for tables up to "
                          f"{settings.sync_reconcile_max_rows} rows")
        item.update({"cursor": cursor, "merge_key": key, "reconcile": reconcile})
        out.append(item)
    imp_schemas = {r.get("remote_schema") for r in rows if r.get("status") != "missing"
                   and (r.get("mode") == "import" or r.get("pending_mode") == "import")
                   and r["id"] not in seen}
    new_imp = [t for t in out if t["to"] == "import"]
    imp_schemas |= {t["remote_schema"] for t in new_imp}
    if len(imp_schemas) > 1:
        return None, ("a source imports from one remote schema; these span "
                      + ", ".join(sorted(map(str, imp_schemas))))
    facts = {"name": name, "kind": kind, "tables": out, "schedule": None,
             "freshness_minutes": None}
    if new_imp and not _has_schedule(source):
        sched = sourcesvc.normalize_schedule(None)
        facts["schedule"] = sched
        facts["freshness_minutes"] = 3 * sched["interval_minutes"]
    return facts, None


def _has_schedule(source: dict) -> bool:
    s = source.get("schedule")
    return isinstance(s, dict) and bool(s.get("interval_minutes"))


def _search_path() -> list[str]:
    from . import warehouse
    raw = warehouse.reader_server_settings().get("search_path", "public")
    path = [p.strip() for p in whlink._split_search_path(raw) if p.strip()]
    return path or ["public"]


def _reads(rels, schema: str, table: str, path_folded: set) -> bool:
    want = freshness.fold(schema, table)
    for s, t in rels:
        if t.casefold() != want[1]:
            continue
        if s:
            if s.casefold() == want[0]:
                return True
        elif want[0] in path_folded:
            return True
    return False


async def dependents(schema: str, table: str) -> dict:
    """What reads schema.table: watches, published dashboard versions, exploration insights and
    active model objects, with up to 5 titles each. agent_memory reads only; one bad SQL text
    or failed read never fails the preview."""
    pool = await get_pool()
    path = {p.casefold() for p in _search_path()}

    via_model: set = set()  # Quick 027: casefolded model.<view> relations that depend on the table

    def hit(sql) -> bool:
        try:
            rels = freshness.relations_of(sql)
        except Exception:
            return False
        return _reads(rels, schema, table, path) or any(
            f"{s}.{t}".casefold() in via_model for s, t in rels if s)

    out = {"watches": 0, "dashboards": 0, "insights": 0, "model_objects": 0, "titles": {}}

    def add(kind, title):
        out[kind] += 1
        ts = out["titles"].setdefault(kind, [])
        if len(ts) < 5:
            ts.append(str(title)[:120])

    try:
        rels = set()
        mrows = list(await pool.fetch(
            "SELECT name, relation, derived_sql FROM model_tables WHERE status = 'active'"))
        counted: set = set()
        changed = True
        while changed:  # a model view over a dependent model view depends on the table too
            changed = False
            for r in mrows:
                if r["name"] in counted:
                    continue
                direct = r["derived_sql"] is None and _reads(
                    {tuple((r["relation"] or "").rpartition(".")[::2])}, schema, table, path)
                if direct or (r["derived_sql"] and hit(r["derived_sql"])):
                    counted.add(r["name"])
                    changed = True
                    add("model_objects", r["name"])
                    rels.add((r["relation"] or "").casefold())
                    if r["derived_sql"]:
                        via_model.add((r["relation"] or "").casefold())
        for r in await pool.fetch(
                "SELECT name, base_relation, expr FROM model_measures WHERE status = 'active' "
                "AND base_relation IS NOT NULL"):
            # Quick 054: a measure also reads the tables its expression names (schema.table.column)
            for br in [(r["base_relation"] or "").casefold()] + [
                    x.casefold() for x in modelcompiler.expr_relations(r.get("expr"))]:
                if br in rels or _reads({tuple(br.rpartition(".")[::2])}, schema, table, path):
                    add("model_objects", r["name"])
                    break
    except Exception as e:
        log.warning("dependents: model objects not read: %s", type(e).__name__)
    try:
        for r in await pool.fetch(
                "SELECT id, title, compiled_sql FROM watches WHERE compiled_sql IS NOT NULL "
                "AND status <> 'done'"):
            if hit(r["compiled_sql"]):
                add("watches", r["title"])
    except Exception as e:
        log.warning("dependents: watches not read: %s", type(e).__name__)
    try:
        for r in await pool.fetch(
                "SELECT d.slug, v.title, v.queries FROM dashboard_versions v "
                "JOIN dashboards d ON d.id = v.dashboard_id WHERE v.status = 'published'"):
            qs = r["queries"]
            if isinstance(qs, (str, bytes)):
                qs = json.loads(qs)
            if any(isinstance(q, dict) and isinstance(q.get("sql"), str) and hit(q["sql"])
                   for q in qs or []):
                add("dashboards", r["title"] or r["slug"])
    except Exception as e:
        log.warning("dependents: dashboards not read: %s", type(e).__name__)
    try:
        for r in await pool.fetch(
                "SELECT id, meta->>'title' AS title, meta->>'evidence_sql' AS sql FROM events "
                "WHERE meta->>'origin' = 'exploration' AND meta->>'evidence_sql' IS NOT NULL "
                "ORDER BY id DESC LIMIT 500"):
            if hit(r["sql"]):
                add("insights", r["title"] or f"insight {r['id']}")
    except Exception as e:
        log.warning("dependents: insights not read: %s", type(e).__name__)
    return out


async def _context(name: str) -> dict | None:
    """Everything validate_mode_change needs, read from the registry, catalog and warehouse."""
    from . import sourcevault
    source = await sourcesvc.get_source(name)
    if source is None:
        return None
    catalog = await schemadiscovery.introspect(name)
    keys = await whsources.link_keys(name)
    exts = await whsources.extensions_present(("tds_fdw", "mysql_fdw"))
    read_only, tls = True, False
    if name != "business_db" and source.get("kind") != "csv":
        h = next((x for x in await sourcevault.list_handles()
                  if x.get("handle") == source.get("credential")), None)
        read_only = bool(h and h.get("read_only") is True)
        tls = bool(h and h.get("tls") is True)  # Quick 028 (WR-04)
    return {"source": source, "catalog": catalog, "keys": keys, "exts": exts,
            "read_only": read_only, "tls": tls}


def _freshness_line(facts: dict, source: dict) -> str:
    sched = facts.get("schedule") or source.get("schedule") or {}
    n = sched.get("interval_minutes")
    every = f"every {n} minutes" if n else "on the source schedule"
    return (f"Imported tables are as fresh as their last sync ({every}); tables read live are "
            "current at query time.")


async def propose(source_name: str, tables, schedule=None, freshness_minutes=None,
                  task_run_id=None) -> str:
    """Validate and write one source_change action (op mode). Returns the tool result text."""
    ctx = await _context(source_name) if _str(source_name) else None
    if ctx is None:
        return "REFUSED: unknown source"
    source = ctx["source"]
    if schedule is not None or freshness_minutes is not None:
        try:
            sched = sourcesvc.normalize_schedule(schedule) if schedule is not None else None
        except ValueError as e:
            return f"REFUSED: {e}"
    else:
        sched = None
    facts, err = validate_mode_change(source, tables, ctx["catalog"], ctx["keys"], get_settings(),
                                      has_ext=ctx["exts"], read_only=ctx["read_only"], tls=ctx.get("tls", False))
    if facts is None:
        return f"REFUSED: {err}"
    if facts["schedule"] is not None:  # the source had none: the owner's choice, else the default
        if sched is not None:
            facts["schedule"] = sched
        if freshness_minutes is not None:
            if isinstance(freshness_minutes, bool) or not isinstance(freshness_minutes, int) \
                    or freshness_minutes <= 0:
                return "REFUSED: freshness_minutes must be a positive whole number"
            facts["freshness_minutes"] = freshness_minutes
        else:
            facts["freshness_minutes"] = 3 * facts["schedule"]["interval_minutes"]
    deps = {}
    for t in facts["tables"]:
        deps[f"{t['schema']}.{t['table']}"] = await dependents(t["schema"], t["table"])
    notes = []
    to_imp = [t for t in facts["tables"] if t["to"] == "import"]
    if to_imp:
        notes.append(KEEP_LIVE_NOTE)
        big = get_settings().sync_reconcile_max_rows
        if source["name"] == "business_db" or any((t["est_rows"] or 0) > big for t in to_imp):
            notes.append(OFFHOURS_NOTE)
    n_imp, n_live = len(to_imp), len(facts["tables"]) - len(to_imp)
    payload = {
        "_summary": (f"Switch {len(facts['tables'])} table(s) of {source['name']}: "
                     f"{n_imp} to import, {n_live} to live"),
        "op": "mode", "name": source["name"], "tables": facts["tables"],
        "schedule": facts["schedule"], "freshness_minutes": facts["freshness_minutes"],
        "dependents": deps, "notes": notes, "freshness": _freshness_line(facts, source)}
    new_id, pending = await sourcesvc.insert_proposal(payload, task_run_id)
    if new_id is None:
        return (f"REFUSED: a source_change for {source['name']} is already waiting for approval "
                f"(#{pending})")
    return (f"proposed table mode switch (action #{new_id}): {payload['_summary']}. "
            + " ".join(notes) + " awaiting owner approval on the Approvals page; "
            "nothing changes before that.")


# ── executor (op mode) ───────────────────────────────────────────────────────
@contextlib.asynccontextmanager
async def _locks(conn, name: str):
    """Yield True when every lock a flip needs is held, else False (busy). business_db also
    takes the link locks so the link sweep cannot race a flip."""
    from . import warehouse
    held: list = []
    try:
        if not await whsources._lock(conn, name):
            yield False
            return
        held.append("src")
        if name == warehouse.SOURCE:
            lk = warehouse._lock()
            if lk.locked():
                yield False
                return
            await lk.acquire()
            held.append(lk)
            if not await conn.fetchval("SELECT pg_try_advisory_lock(hashtext($1))",
                                       f"ic_link:{name}"):
                yield False
                return
            held.append("link")
        yield True
    finally:
        for h in reversed(held):
            try:
                if h == "src":
                    await whsources._unlock(conn, name)
                elif h == "link":
                    await conn.fetchval("SELECT pg_advisory_unlock(hashtext($1))", f"ic_link:{name}")
                else:
                    h.release()
            except Exception:
                pass


async def _set_error(pool, row_id: int, text: str, *, clear_pending: bool = False) -> None:
    await pool.execute(
        "UPDATE source_tables SET last_error = $2, "
        "pending_mode = CASE WHEN $3 THEN NULL ELSE pending_mode END, "
        "import_reset = CASE WHEN $3 THEN false ELSE import_reset END, updated_at = now() "
        "WHERE id = $1", row_id, text[:300], clear_pending)


def _fail_text(what: str, e: Exception) -> str:
    return f"switch to {what} stopped: {type(e).__name__}: {whlink._error_detail(e)[:200]}"


async def _credentials(source: dict, need_live: bool):
    """(cred, probe) for building a live relation; (None, None) when none is needed."""
    from . import sourcevault
    if not need_live or source["name"] == "business_db":
        return None, None
    cred = await sourcevault.load(whsources._cred_name(source))
    probe = await whsources._load_probe(source) if source["kind"] in ("mysql", "mssql") else None
    return cred, probe


async def _view_to_live(conn, source: dict, r: dict, cred, probe) -> dict:
    rel = await whsources.live_relation(conn, source["name"], r, cred, kind=source["kind"], probe=probe)
    kw = {"src_function": True, "cols_rel": rel.cols_rel} if rel.is_function else {}
    return await whsources.replace_view_preserving(
        conn, r["warehouse_schema"], r["warehouse_table"], rel.schema, rel.relation, **kw)


async def apply(payload: dict) -> bool:
    """The op 'mode' executor. approve() has already claimed the action single-winner. Facts are
    recomputed from the registry and catalog; the payload only names the owner's choices."""
    name = payload.get("name") if isinstance(payload, dict) else None
    try:
        ctx = await _context(name) if _str(name) else None
        if ctx is None:
            log.warning("mode change rejected at execution: unknown source")
            return False
        reqs = []
        for t in payload.get("tables") or []:
            if not isinstance(t, dict):
                log.warning("mode change rejected at execution: invalid table entry")
                return False
            q = {"table": t.get("table"), "schema": t.get("schema"), "mode": t.get("to")}
            if t.get("to") == "import":
                q["cursor"] = t.get("cursor")
                if t.get("merge_key"):
                    q["merge_key"] = t["merge_key"]
                q["reconcile"] = bool(t.get("reconcile"))
            reqs.append(q)
        facts, err = validate_mode_change(ctx["source"], reqs, ctx["catalog"], ctx["keys"],
                                          get_settings(), has_ext=ctx["exts"],
                                          read_only=ctx["read_only"], tls=ctx.get("tls", False))
        if facts is None:
            log.warning("mode change rejected at execution: %s", err)
            return False
        if facts["schedule"] is not None:
            try:
                facts["schedule"] = sourcesvc.normalize_schedule(payload.get("schedule"))
            except ValueError:
                pass
            fm = payload.get("freshness_minutes")
            facts["freshness_minutes"] = fm if isinstance(fm, int) and not isinstance(fm, bool) \
                and fm > 0 else 3 * facts["schedule"]["interval_minutes"]
        pool = await get_pool()
        action_id = await pool.fetchval(
            "SELECT id FROM actions WHERE kind='source_change' AND status='approved' "
            "AND payload->>'name' = $1 ORDER BY id DESC LIMIT 1", name)
        source = ctx["source"]
        rows = {(r["warehouse_schema"], r["warehouse_table"]): r for r in source["tables"]}
        picked = [(t, rows[(t["schema"], t["table"])]) for t in facts["tables"]]
        conn = await whsources._owner_conn(DEFAULT_ROLES)
    except Exception as e:
        log.warning("mode change failed before execution: %s", type(e).__name__)
        return False
    ok = True
    try:
        async with _locks(conn, name) as got:
            if not got:
                for _, r in picked:
                    await _set_error(pool, r["id"], BUSY_TEXT)
                return False
            try:
                cred, probe = await _credentials(
                    source, any(t["to"] == "live" for t, _ in picked))
                cred_err = None
            except Exception as e:
                cred, probe, cred_err = None, None, e
            started_import = 0
            for t, r in picked:
                if t["to"] == "import":
                    try:
                        if not started_import:
                            await whsources.ensure_import_schemas(conn, name)
                        await pool.execute(
                            "UPDATE source_tables SET pending_mode = 'import', cursor_column = $2, "
                            "merge_key = $3::text[], reconcile = $4, backfill_done = false, "
                            "backfill_progress = 0, import_reset = true, mode_action_id = $5, "
                            "last_error = NULL, updated_at = now() WHERE id = $1",
                            r["id"], t["cursor"], t["merge_key"], t["reconcile"], action_id)
                        started_import += 1
                    except Exception as e:
                        ok = False
                        log.warning("switch of %s to import not started: %s", t["table"],
                                    type(e).__name__)
                        await _set_error(pool, r["id"], _fail_text("import", e))
                    continue
                try:
                    if cred_err is not None:
                        raise cred_err
                    res = await _view_to_live(conn, source, r, cred, probe)
                    if res.get("status") != "ok":
                        raise ValueError(res.get("reason") or "view could not be replaced")
                    await pool.execute(
                        "UPDATE source_tables SET mode = 'live', pending_mode = NULL, "
                        "import_reset = false, mode_action_id = $2, last_error = NULL, "
                        "updated_at = now() WHERE id = $1", r["id"], action_id)
                except (asyncpg.PostgresError, ValueError, LookupError, OSError) as e:
                    ok = False
                    log.warning("switch of %s to live stopped: %s", t["table"], type(e).__name__)
                    await _set_error(pool, r["id"], _fail_text("live", e))
            if started_import:
                if facts["schedule"] is not None:
                    await pool.execute(
                        "UPDATE sources SET schedule = $2::jsonb, freshness_minutes = $3, "
                        "next_sync_at = now(), updated_at = now() WHERE id = $1",
                        source["id"], json.dumps(facts["schedule"]), facts["freshness_minutes"])
                else:
                    await pool.execute(
                        "UPDATE sources SET next_sync_at = now(), updated_at = now() WHERE id = $1",
                        source["id"])
                from . import syncsched
                await syncsched.enqueue_now(source["id"], "backfill")
            if name != "business_db":  # Pitfall 6: never rewrite the business_db catalog
                try:
                    await whsources.write_catalog(conn, name)
                except Exception as e:
                    log.warning("catalog rewrite of %s failed: %s", name, type(e).__name__)
        sourcesvc._reset_cache_for_tests()
    except Exception as e:
        log.warning("mode change of %s failed: %s", name, type(e).__name__)
        return False
    finally:
        try:
            await conn.close()
        except Exception:
            pass
    return ok


# ── cutover (called by the scheduler sweep) ──────────────────────────────────
_PENDING_SQL = (
    "SELECT t.*, s.name AS source_name, s.kind AS source_kind FROM source_tables t "
    "JOIN sources s ON s.id = t.source_id "
    "WHERE t.pending_mode = 'import' AND t.backfill_done AND t.status = 'active' "
    "AND s.status = 'active' ORDER BY s.name, t.id")


async def _record(status: str, label: str, summary: str, error=None) -> None:
    from . import jobruns
    await jobruns.record("mode_switch", label, status=status, summary=summary, error=error)


async def _cutover_one(conn, pool, r: dict, out: dict) -> None:
    name, sch, wt = r["source_name"], r["warehouse_schema"], r["warehouse_table"]
    label = f"{sch}.{wt}"
    try:
        res = await whsources.replace_view_preserving(conn, sch, wt, whsources.imp_schema(name), wt,
                                                      DEFAULT_ROLES, check=True)
        if res.get("status") != "ok":
            raise ValueError(res.get("reason") or "the copy lacks columns the view exposes")
    except Exception as e:
        if isinstance(e, (asyncpg.PostgresConnectionError, ConnectionError)):
            raise
        # nothing was replaced (the cast check runs first): the view stays on live
        text = (f"switch to import stopped: {type(e).__name__}: values do not fit the current "
                "column types")
        await _set_error(pool, r["id"], text, clear_pending=True)
        await _record("failed", label, f"Switch of {label} to import stopped", error=text)
        out["failed"].append(label)
        return
    try:
        stmt = await conn.fetchval(
            "SELECT format('SELECT 1 FROM %I.%I LIMIT 1', $1::text, $2::text)", sch, wt)
        await conn.fetchval(stmt)
    except Exception as e:
        if isinstance(e, (asyncpg.PostgresConnectionError, ConnectionError)):
            raise
        text = (f"switch to import stopped: {type(e).__name__}: the copy could not be read; "
                "the table is back on live")
        try:  # point the view back at the live relation
            srcd = {"name": name, "kind": r["source_kind"], **(await _src_ref(pool, r))}
            cred, prb = await _credentials(srcd, True)
            back = await _view_to_live(conn, srcd, r, cred, prb)
            if back.get("status") != "ok":
                text += " (could not restore the live view)"
        except Exception as e2:
            log.warning("revert of %s to live failed: %s", label, type(e2).__name__)
            text += " (could not restore the live view)"
        await _set_error(pool, r["id"], text, clear_pending=True)
        await _record("failed", label, f"Switch of {label} to import stopped", error=text)
        out["failed"].append(label)
        return
    if name != "business_db":
        try:
            await whsources.drop_live_relation(conn, name, r, DEFAULT_ROLES)
        except Exception as e:
            log.warning("live relation of %s not dropped: %s", label, type(e).__name__)
    await pool.execute(
        "UPDATE source_tables SET mode = 'import', pending_mode = NULL, import_reset = false, "
        "last_error = NULL, updated_at = now() WHERE id = $1", r["id"])
    await _record("done", label, f"Switched {label} to import")
    out["switched"].append(label)


async def _src_ref(pool, r: dict) -> dict:
    """The source row fields _credentials needs (credential handle and kind)."""
    row = await pool.fetchrow("SELECT credential, config FROM sources WHERE id = $1", r["source_id"])
    cfg = row["config"] if row else None
    if isinstance(cfg, (str, bytes)):  # asyncpg returns jsonb as text (no codec registered)
        try:
            cfg = json.loads(cfg)
        except ValueError:
            cfg = {}
    return {"credential": row["credential"] if row else None,
            "config": dict(cfg) if isinstance(cfg, dict) else {}}


async def advance() -> dict:
    """Cut pending imports over to the copy once the first full copy finished and a fenced cast
    check passed. Never raises (the sweep keeps running); a database blip on the initial read
    propagates so the sweep's own blip handling applies."""
    out = {"switched": [], "failed": [], "busy": []}
    pool = await get_pool()
    rows = [dict(r) for r in await pool.fetch(_PENDING_SQL)]
    by_source: dict = {}
    for r in rows:
        by_source.setdefault(r["source_name"], []).append(r)
    for name, group in by_source.items():
        conn = None
        try:
            conn = await whsources._owner_conn(DEFAULT_ROLES)
            async with _locks(conn, name) as got:
                if not got:
                    out["busy"] += [f"{r['warehouse_schema']}.{r['warehouse_table']}" for r in group]
                    continue
                for r in group:
                    try:
                        await _cutover_one(conn, pool, r, out)
                    except Exception as e:
                        log.warning("cutover of %s.%s failed: %s", r["warehouse_schema"],
                                    r["warehouse_table"], type(e).__name__)
                        out["busy"].append(f"{r['warehouse_schema']}.{r['warehouse_table']}")
                if name != "business_db":
                    try:
                        await whsources.write_catalog(conn, name)
                    except Exception as e:
                        log.warning("catalog rewrite of %s failed: %s", name, type(e).__name__)
        except Exception as e:
            log.warning("cutover for source %s skipped: %s", name, type(e).__name__)
            out["busy"] += [f"{r['warehouse_schema']}.{r['warehouse_table']}" for r in group]
        finally:
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
    return out

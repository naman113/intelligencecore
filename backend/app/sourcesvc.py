"""Phase 15 registry plumbing: the only writer of sources, source_tables and sync_runs rows
outside the source_change executor, which also lives here (plan 15-10)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta

from .config import get_settings
from .db import get_pool

log = logging.getLogger(__name__)

SOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
RESERVED_NAMES = frozenset({"business_db", "public", "model", "warehouse", "information_schema"})
RESERVED_PREFIXES = ("ic_", "pg_", "src_", "stage_")
NAME_ERROR = ("Source names are 2 to 31 characters: a lowercase letter first, then lowercase "
              "letters, digits or underscores, and not a reserved name or prefix.")

MIN_INTERVAL = 15
MAX_INTERVAL = 10080
_SCHEDULE_KEYS = {"interval_minutes", "window", "reconcile_hour"}


def validate_name(name: str) -> str | None:
    """None when the name is acceptable, else a fixed error message."""
    if not isinstance(name, str) or not SOURCE_NAME_RE.match(name):
        return NAME_ERROR
    if name in RESERVED_NAMES or name.startswith(RESERVED_PREFIXES):
        return NAME_ERROR
    if name.endswith("_staging"):  # collides with another source's dlt staging dataset
        return NAME_ERROR
    return None


def _hour(v) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 23:
        raise ValueError("Schedule hours must be whole numbers from 0 to 23.")
    return v


def normalize_schedule(raw: dict | None) -> dict:
    s = get_settings()
    raw = raw or {}
    if not isinstance(raw, dict) or set(raw) - _SCHEDULE_KEYS:
        raise ValueError("A schedule may only hold interval_minutes, window and reconcile_hour.")
    interval = raw.get("interval_minutes", s.sync_default_interval_minutes)
    if isinstance(interval, bool) or not isinstance(interval, int) \
            or not MIN_INTERVAL <= interval <= MAX_INTERVAL:
        raise ValueError(f"interval_minutes must be a whole number from {MIN_INTERVAL} to {MAX_INTERVAL}.")
    window = raw.get("window")
    if window is not None:
        if not isinstance(window, (list, tuple)) or len(window) != 2:
            raise ValueError("window must be [start_hour, end_hour] or null.")
        a, b = _hour(window[0]), _hour(window[1])
        if a == b:
            raise ValueError("window start and end hours must differ.")
        window = [a, b]
    rh = _hour(raw.get("reconcile_hour", s.sync_reconcile_hour))
    return {"interval_minutes": interval, "window": window, "reconcile_hour": rh}


def _in_window(hour: int, window) -> bool:
    a, b = window
    return a <= hour < b if a < b else (hour >= a or hour < b)


def next_due(schedule: dict, now_local: datetime) -> datetime:
    """Next time a sync is due after now_local, honouring the local window (may wrap midnight)."""
    interval = int(schedule.get("interval_minutes") or get_settings().sync_default_interval_minutes)
    cand = now_local + timedelta(minutes=interval)
    window = schedule.get("window")
    if not window or _in_window(cand.hour, window):
        return cand
    nxt = cand.replace(hour=window[0], minute=0, second=0, microsecond=0)
    if nxt <= cand:
        nxt += timedelta(days=1)
    return nxt


def local_now() -> datetime:
    from .expectations import _local_now
    return _local_now()


def _j(v):
    if isinstance(v, (str, bytes)):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _decode(row) -> dict:
    d = dict(row)
    for k in ("config", "schedule", "display", "tables"):
        if k in d:
            d[k] = _j(d[k])
    return d


_SOURCES_SQL = "SELECT * FROM sources WHERE status <> 'removed' ORDER BY (name <> 'business_db'), id"


async def _with_tables(pool, srcs: list[dict]) -> list[dict]:
    if not srcs:
        return srcs
    rows = await pool.fetch(
        "SELECT * FROM source_tables WHERE source_id = ANY($1::bigint[]) "
        "ORDER BY source_id, warehouse_schema, warehouse_table", [s["id"] for s in srcs])
    by: dict[int, list[dict]] = {}
    for r in rows:
        by.setdefault(r["source_id"], []).append(dict(r))
    for s in srcs:
        s["tables"] = by.get(s["id"], [])
    return srcs


async def list_sources() -> list[dict]:
    pool = await get_pool()
    srcs = [_decode(r) for r in await pool.fetch(_SOURCES_SQL)]
    return await _with_tables(pool, srcs)


async def get_source(name: str) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM sources WHERE name = $1", name)
    if not row:
        return None
    return (await _with_tables(pool, [_decode(row)]))[0]


_ACTIVE_TTL = 30.0
_active_cache: tuple[float, bool] | None = None


def _reset_cache_for_tests() -> None:
    global _active_cache
    _active_cache = None


async def has_active_sources() -> bool:
    """True when an active source other than business_db exists (cached for 30 s)."""
    global _active_cache
    now = time.monotonic()
    if _active_cache and now - _active_cache[0] < _ACTIVE_TTL:
        return _active_cache[1]
    pool = await get_pool()
    v = bool(await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM sources WHERE status = 'active' AND name <> 'business_db')"))
    _active_cache = (now, v)
    return v


async def sync_business_db_tables() -> dict:
    """Keep source_tables for business_db equal to the discovery catalog (live rows). Writes only
    differences; vanished tables become 'missing', never deleted. Never raises."""
    from . import schemadiscovery
    if not schemadiscovery.business_db_configured():  # Quick 031: an empty catalog is not "tables gone"
        return {"skipped": "not_configured"}
    try:
        cat = await schemadiscovery.introspect("business_db")
        pool = await get_pool()
        sid = await pool.fetchval("SELECT id FROM sources WHERE name = 'business_db'")
        if sid is None:
            return {"error": "NoBusinessDbSource"}
        have = {(r["warehouse_schema"], r["warehouse_table"]): r["status"]
                for r in await pool.fetch(
                    "SELECT warehouse_schema, warehouse_table, status FROM source_tables WHERE source_id = $1", sid)}
        want = {(t["schema"], t["table"]) for t in cat.get("tables", [])}
        out = {"inserted": 0, "missing": 0, "restored": 0, "unchanged": 0}
        for sch, tbl in sorted(want):
            st = have.get((sch, tbl))
            if st is None:
                await pool.execute(
                    "INSERT INTO source_tables (source_id, remote_schema, remote_table, warehouse_schema, "
                    "warehouse_table, mode) VALUES ($1,$2,$3,$2,$3,'live') ON CONFLICT DO NOTHING", sid, sch, tbl)
                out["inserted"] += 1
            elif st == "missing":
                await pool.execute(
                    "UPDATE source_tables SET status='active', updated_at=now() WHERE source_id=$1 "
                    "AND warehouse_schema=$2 AND warehouse_table=$3", sid, sch, tbl)
                out["restored"] += 1
            else:
                out["unchanged"] += 1
        for (sch, tbl), st in sorted(have.items()):
            if (sch, tbl) not in want and st != "missing":
                await pool.execute(
                    "UPDATE source_tables SET status='missing', updated_at=now() WHERE source_id=$1 "
                    "AND warehouse_schema=$2 AND warehouse_table=$3", sid, sch, tbl)
                out["missing"] += 1
        return out
    except Exception as e:
        log.warning("business_db table sync failed: %s", type(e).__name__)
        return {"error": type(e).__name__}


# ── Phase 15 (SRC-05, SRC-06): sync runs ──
NO_PROGRESS_ERROR = ("backfill made no progress in a capped run: a cursor value repeats more often "
                     "than one run can load, or the source query is slower than the run time budget")
_ERR_CLIP = 300
_TIMEOUT_TEXT = "sync container timed out"


def _redact(text) -> str:
    from . import secretbox
    return (secretbox.redact(str(text)) or "").strip()[:_ERR_CLIP]


def _caps(source: dict) -> dict:
    from . import worker
    s = get_settings()
    approved = ((source.get("config") or {}).get("caps")) or {}
    rows = min(int(approved.get("max_rows_per_run") or s.sync_max_rows_per_run), s.sync_max_rows_per_run)
    secs = min(int(approved.get("max_seconds_per_run") or s.sync_max_seconds_per_run),
               s.sync_max_seconds_per_run, worker._sync_timeout() - 300)
    return {"max_rows": max(1, rows), "max_seconds": max(1, secs),
            "chunk_size": max(1, min(s.sync_chunk_size, rows))}


def _csv_job(source: dict, tables: list[dict]):
    # Phase 15 (SRC-08): CSV run job; the file travels on stdin
    from . import csvsource
    name = source["name"]
    csv = (source.get("config") or {}).get("csv") or {}
    imp = [t for t in tables if t.get("mode") == "import"]
    if not csv.get("current_sha") or not imp:
        raise ValueError("nothing to sync")
    t = imp[0]
    disposition = csv.get("on_reupload") or "replace"
    if not t.get("backfill_done"):
        disposition = "replace"
    job = {"v": 1, "source": name, "kind": "csv", "dataset": f"ic_imp_{name}", "pipeline": f"ic_{name}",
           "caps": {"chunk_size": get_settings().sync_chunk_size},
           "csv": {"mode": "load", "table": t["warehouse_table"],
                   "column_types": csv.get("column_types") or None, "disposition": disposition,
                   "delimiter": csv.get("delimiter", ","), "encoding": csv.get("encoding", "utf-8")}}
    return "csv", job, csvsource.source_file(name, csv["current_sha"])


# Phase 17 (CONS-05): business_db and pending imports on the sync lane
def business_db_cred() -> dict:
    """Source credential built from the decrypted tenant URL; it only ever goes into the container env."""
    from . import warehouse, whlink
    o = whlink.parse_source_url(warehouse._business_url())  # warehouse.py is the allowed URL reader
    if "," in o.host:
        raise ValueError("the business database URL lists several hosts; import needs exactly one")
    cred = {"host": o.host, "port": o.port, "database": o.dbname, "user": o.user,
            "password": o.password, "options": {}}
    ssl = dict(o.extra).get("sslmode")
    if ssl:
        cred["options"] = {"sslmode": ssl}
    return cred


def _import_rows(tables: list[dict]) -> list[dict]:
    return [t for t in tables if t.get("status") != "missing"
            and (t.get("mode") == "import" or t.get("pending_mode") == "import")]
# End Phase 17 (CONS-05)


def build_sync_job(source: dict, tables: list[dict], run_kind: str) -> tuple[str, dict, object]:
    """The ic-sync command, job and stdin file (None except for CSV) for the active import tables,
    least recently synced first."""
    name = source["name"]
    if source.get("kind") == "csv" and name != "business_db":
        return _csv_job(source, tables)
    imp = _import_rows(tables)  # Phase 17 (CONS-05): includes pending imports and business_db
    if not imp:
        raise ValueError("nothing to sync")
    remote_schema = (source.get("config") or {}).get("remote_schema")
    if name == "business_db":
        schemas = {t.get("remote_schema") for t in imp}
        if len(schemas) > 1:
            raise ValueError("business_db imports must stay in one schema; flip tables from one schema at a time")
        remote_schema = next(iter(schemas))

    def order(t):
        ls = t.get("last_success_at")
        return (ls is not None, ls.timestamp() if ls is not None else 0.0, t.get("warehouse_table") or "")
    imp.sort(key=order)
    cfg = source.get("config") or {}
    reconcile = run_kind == "reconcile"
    job = {
        "v": 1, "source": name, "kind": source["kind"],
        "remote_schema": remote_schema,
        "dataset": f"ic_imp_{name}", "pipeline": f"ic_{name}",
        "reconcile": reconcile,
        "tables": [{"remote_table": t["remote_table"], "warehouse_table": t["warehouse_table"],
                    "cursor": t.get("cursor_column"), "merge_key": list(t.get("merge_key") or []),
                    "reconcile": bool(reconcile and t.get("reconcile")),
                    "initial_value": cfg.get("backfill_start"),
                    "reset": bool(t.get("import_reset"))} for t in imp],
        "caps": _caps(source),
    }
    return "sync", job, None


def table_progress(rt: dict) -> tuple[str, bool]:
    """(effective status, counts as capped). A capped 0-row ok result and a CursorStalled failure
    made no progress and wait for the schedule instead of an immediate re-run."""
    status = rt.get("status")
    if status == "ok" and rt.get("capped") and not rt.get("rows"):
        return "failed", False
    if status == "failed":
        return "failed", False
    if status == "skipped":
        return "skipped", True
    return "ok", bool(rt.get("capped"))


async def fail_open_run(task_run_id: int, error: str) -> int:
    pool = await get_pool()
    r = await pool.execute(
        "UPDATE sync_runs SET status='failed', ended_at=now(), error=$2 "
        "WHERE task_run_id=$1 AND status IN ('queued','running')", task_run_id, _redact(error))
    try:
        return int(str(r).rsplit(" ", 1)[-1])
    except ValueError:
        return 0


def next_after_run(schedule: dict, any_capped: bool, now_local: datetime) -> datetime:
    if any_capped:
        window = (schedule or {}).get("window")
        if not window or _in_window(now_local.hour, window):
            return now_local
        nxt = now_local.replace(hour=window[0], minute=0, second=0, microsecond=0)
        return nxt if nxt > now_local else nxt + timedelta(days=1)
    return next_due(schedule or {}, now_local)


async def _estimate_rows(name: str, tables: list[str]) -> dict:
    """pg_class.reltuples of ic_imp_<name>.<t> on an owner connection; {} when unavailable."""
    try:
        from . import warehouse
        conn = await warehouse.connect("owner", timeout=15.0)
    except Exception:
        return {}
    try:
        rows = await conn.fetch(
            "SELECT c.relname, c.reltuples::bigint AS n FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = $1 AND c.relname = ANY($2::text[]) "
            "AND c.reltuples >= 0", f"ic_imp_{name}", tables)
        return {r["relname"]: int(r["n"]) for r in rows}
    except Exception:
        return {}
    finally:
        try:
            await conn.close()
        except Exception:
            pass


async def record_sync_result(run_id: int, source: dict, tables: list[dict], result: dict) -> dict:
    """Write sync_runs and source_tables from a container result, refresh views and the catalog."""
    from . import whsources
    pool = await get_pool()
    name = source["name"]
    is_bdb = name == "business_db"  # Phase 17 (CONS-05)
    by_wh = {t["warehouse_table"]: t for t in _import_rows(tables)}
    rts = [r for r in (result.get("tables") or []) if isinstance(r, dict)]
    eff = [(r, *table_progress(r)) for r in rts]
    ok_names = [r["table"] for r, st, _ in eff if st == "ok"]
    est = await _estimate_rows(name, ok_names) if ok_names else {}
    any_capped = False
    for r, st, capped in eff:
        t = by_wh.get(r.get("table"))
        if t is None:
            continue
        any_capped = any_capped or capped
        if st == "ok":
            total = est.get(r["table"], t.get("rows_total"))
            er = t.get("est_rows")
            prog = min(1.0, total / er) if (total is not None and er and er > 0) \
                else t.get("backfill_progress")
            if not capped:
                prog = 1.0 if prog is None else prog
            await pool.execute(
                "UPDATE source_tables SET last_success_at=now(), rows_total=$3, backfill_done=$4, "
                "backfill_progress=$5, last_error=NULL, status='active', import_reset=false, "
                "updated_at=now() WHERE source_id=$1 AND warehouse_table=$2 AND warehouse_schema=$6 "
                "AND (mode = 'import' OR pending_mode = 'import')",
                source["id"], r["table"], total, not capped, prog, t.get("warehouse_schema"))
        elif st == "failed":
            msg = NO_PROGRESS_ERROR if r.get("status") == "ok" else _redact(
                f"{r.get('error_class') or 'Error'}: {r.get('error') or 'failed'}")
            await pool.execute(
                "UPDATE source_tables SET status='failed', last_error=$3, updated_at=now() "
                "WHERE source_id=$1 AND warehouse_table=$2 AND warehouse_schema=$4 "
                "AND (mode = 'import' OR pending_mode = 'import')",
                source["id"], r["table"], msg, t.get("warehouse_schema"))
    n_ok = sum(1 for _, st, _ in eff if st == "ok")
    n_bad = sum(1 for _, st, _ in eff if st == "failed")
    if result.get("status") == "failed" and not n_ok:
        status = "failed"
    elif n_bad:
        status = "partial" if n_ok else "failed"
    else:
        status = "ok"
    err = None
    if status != "ok":
        err = _redact(result.get("error") or next(
            (f"{r.get('error_class') or 'Error'}: {r.get('error') or ''}" for r, st, _ in eff
             if st == "failed" and r.get("status") != "ok"), "") or "no table made progress")
    rows = sum(int(r.get("rows") or 0) for r in rts)
    summary = [{"table": r.get("table"), "status": st, "rows": int(r.get("rows") or 0), "capped": capped,
                "error_class": r.get("error_class"), "reconciled_deleted": r.get("reconciled_deleted")}
               for r, st, capped in eff]
    await pool.execute(
        "UPDATE sync_runs SET status=$2, ended_at=now(), duration_ms=$3, rows_loaded=$4, tables=$5::jsonb, "
        "error=$6, peak_memory_mb=$7 WHERE id=$1",
        run_id, status, result.get("duration_ms"), rows, json.dumps(summary), err,
        result.get("peak_memory_mb"))
    ok_imp = [n for n in ok_names if (by_wh.get(n) or {}).get("mode") == "import"]
    if ok_imp:
        try:
            await whsources.refresh_import_views(name, ok_imp)
        except Exception as e:
            log.warning("view refresh of %s failed: %s", name, type(e).__name__)
    try:
        if not is_bdb:  # Phase 17 (CONS-05, Pitfall 6): never rewrite the business_db catalog
            from . import warehouse
            conn = await warehouse.connect("owner", timeout=15.0)
            try:
                await whsources.write_catalog(conn, name)
            finally:
                await conn.close()
    except Exception as e:
        log.warning("catalog rewrite of %s failed: %s", name, type(e).__name__)
    now = local_now()
    nxt = next_after_run(source.get("schedule") or {}, any_capped, now)
    kind = await pool.fetchval("SELECT kind FROM sync_runs WHERE id=$1", run_id)
    if status == "ok" and not any_capped and kind == "reconcile":
        await pool.execute("UPDATE sources SET next_sync_at=$2, last_reconcile_on=$3, updated_at=now() "
                           "WHERE id=$1", source["id"], nxt, now.date())
    else:
        await pool.execute("UPDATE sources SET next_sync_at=$2, updated_at=now() WHERE id=$1",
                           source["id"], nxt)
    return {"status": status, "any_capped": any_capped, "rows": rows, "error": err}


async def _end_task(pool, tid: int, status: str, error: str | None) -> None:
    await pool.execute(
        "UPDATE task_runs SET status=$2, ended_at=now(), error=$3 WHERE id=$1 AND status='running'",
        tid, status, error)


async def run_sync_task(task: dict) -> None:
    """Run the sync_runs row of a kind='sync' task_run. Whatever ends it, the row ends finished."""
    from . import sourcevault, syncrunner, worker
    pool = await get_pool()
    tid = task["id"]
    err = None
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                run = await conn.fetchrow(
                    "SELECT * FROM sync_runs WHERE task_run_id=$1 AND status IN ('queued','running') "
                    "ORDER BY id DESC LIMIT 1 FOR UPDATE", tid)
                if run is not None:
                    await conn.execute(
                        "UPDATE sync_runs SET status='running', started_at=now() WHERE id=$1", run["id"])
        if run is None:
            await _end_task(pool, tid, "failed", "sync failed: no run")
            return
        src_name = await pool.fetchval("SELECT name FROM sources WHERE id=$1", run["source_id"])
        source = await get_source(src_name)
        if source is None or source.get("status") != "active":
            await fail_open_run(tid, "source is not active")
            await _end_task(pool, tid, "failed", "sync failed: source is not active")
            return
        try:
            cmd, job, stdin_path = build_sync_job(source, source["tables"], run["kind"])
            if stdin_path is not None:  # Phase 15 (SRC-08): CSV, loader env only
                secret_env, extra = syncrunner.loader_env(), {"stdin_path": stdin_path}
            else:
                if source["name"] == "business_db":  # Phase 17 (CONS-05)
                    cred = business_db_cred()
                else:
                    cred = await sourcevault.load(str(source["credential"]).removeprefix("cred:"))
                secret_env, extra = {**syncrunner.source_env(cred), **syncrunner.loader_env()}, {}
            result = await syncrunner.run(
                cmd, job, secret_env,
                name=f"run-{run['id']}", network=await syncrunner.network_for(cmd),
                timeout=worker._sync_timeout(), **extra)
        except syncrunner.SyncImageMissing:
            await fail_open_run(tid, worker.SYNC_RUNNER_MISSING)
            await _end_task(pool, tid, "failed", worker.SYNC_RUNNER_MISSING)
            return
        out = await record_sync_result(run["id"], source, source["tables"], result)
        if out["status"] == "failed":
            cls = next((r.get("error_class") for r in result.get("tables") or []
                        if r.get("status") == "failed" and r.get("error_class")), None) or "Error"
            await _end_task(pool, tid, "failed", f"sync failed: {cls}")
        else:
            await _end_task(pool, tid, "done", None)
    except BaseException as e:
        err = _TIMEOUT_TEXT if isinstance(e, (asyncio.CancelledError, asyncio.TimeoutError)) \
            else type(e).__name__
        raise
    finally:
        try:
            await asyncio.shield(fail_open_run(tid, err or "sync ended without a result"))
        except BaseException as e2:
            log.warning("could not finalize sync run of task %s: %s", tid, type(e2).__name__)
# ── End Phase 15 sync runs ──


# ── Phase 15 (SRC-04, SRC-07): proposals and the source_change executor ──
MAX_PROPOSAL_TABLES = 200
_WH_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
# ── Phase 15 gap 2 (WR-14): warehouse names for real-world table names ──
# Keywords with catcode R or T in PostgreSQL 16, captured with:
#   SELECT string_agg(word, ' ' ORDER BY word) FROM pg_get_keywords() WHERE catcode IN ('R','T')
PG_KEYWORDS = frozenset((
    "all", "analyse", "analyze", "and", "any", "array", "as", "asc", "asymmetric",
    "authorization", "binary", "both", "case", "cast", "check", "collate", "collation",
    "column", "concurrently", "constraint", "create", "cross", "current_catalog",
    "current_date", "current_role", "current_schema", "current_time", "current_timestamp",
    "current_user", "default", "deferrable", "desc", "distinct", "do", "else", "end", "except",
    "false", "fetch", "for", "foreign", "freeze", "from", "full", "grant", "group", "having",
    "ilike", "in", "initially", "inner", "intersect", "into", "is", "isnull", "join",
    "lateral", "leading", "left", "like", "limit", "localtime", "localtimestamp", "natural",
    "not", "notnull", "null", "offset", "on", "only", "or", "order", "outer", "overlaps",
    "placing", "primary", "references", "returning", "right", "select", "session_user",
    "similar", "some", "symmetric", "system_user", "table", "tablesample", "then", "to",
    "trailing", "true", "union", "unique", "user", "using", "variadic", "verbose", "when",
    "where", "window", "with",
))
WH_NAME_MAX_BASE = 55
_CAMEL_1 = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CAMEL_2 = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def warehouse_table_name(remote: str) -> str:
    """Pure: the lowercase snake_case warehouse name of a remote table name."""
    if _WH_TABLE_RE.match(remote) and remote not in PG_KEYWORDS:
        return remote
    s = _CAMEL_2.sub("_", _CAMEL_1.sub("_", remote)).lower()
    s = _NON_ALNUM.sub("_", s).strip("_") or "t"
    if s[0].isdigit():
        s = "t_" + s
    s = s[:WH_NAME_MAX_BASE].rstrip("_") or "t"
    if s in PG_KEYWORDS:
        s += "_tbl"
    return s


def warehouse_names(snapshot: dict, schema) -> dict[str, str]:
    """Pure: remote table name -> unique warehouse name for the probed tables of one schema.

    A remote name that already equals its base keeps it; the others take the base, then _2, _3
    in sorted remote-name order, never reusing a name another table has as its base."""
    remotes = sorted({t.get("table") for t in (snapshot or {}).get("tables") or []
                      if isinstance(t, dict) and isinstance(t.get("table"), str)
                      and (schema is None or t.get("schema") == schema)})
    bases = {r: warehouse_table_name(r) for r in remotes}
    all_bases = set(bases.values())
    out: dict[str, str] = {}
    used: set[str] = set()
    for r in remotes:
        if bases[r] == r:
            out[r] = r
            used.add(r)
    for r in remotes:
        if r in out:
            continue
        b = bases[r]
        if b not in used:
            out[r] = b
        else:
            n = 2
            while f"{b}_{n}" in used or f"{b}_{n}" in all_bases:
                n += 1
            out[r] = f"{b}_{n}"
        used.add(out[r])
    return out


def table_name_problem(remote) -> str | None:
    """Fixed refusal text for a remote name the plumbing cannot carry; never echoes the name."""
    if not isinstance(remote, str) or not remote:
        return "a table name is empty"
    if any(ord(c) < 32 or ord(c) == 127 for c in remote):
        return "a table name contains control characters"
    if len(remote.encode("utf-8")) > 63:
        return ("table names longer than 63 bytes cannot be synced; expose the table "
                "through a shorter view on the source")
    return None
# ── End Phase 15 gap 2 ──
_TABLE_KEYS = {"table", "schema", "cursor", "merge_key", "reconcile", "mode"}
_CONFIG_KEYS = {"target", "tables", "backfill_start", "freshness_minutes"}
PROVISION_FAILED = "warehouse provisioning failed"


def _snap(v):
    return _j(v) if isinstance(v, (str, bytes)) else v


def _probe_row(row) -> dict | None:
    if row is None:
        return None
    return {"id": int(row["id"]), "snapshot": _snap(row["snapshot"]) or {}}


async def latest_probe(target: str) -> dict | None:
    pool = await get_pool()
    return _probe_row(await pool.fetchrow(
        "SELECT id, snapshot FROM source_probes WHERE kind='probe' AND target=$1 "
        "ORDER BY id DESC LIMIT 1", target))


async def merged_test(pool, name: str, target: str, probe_id: int, max_id: int | None = None) -> dict | None:
    """Every test of (name, target) newer than the probe, merged table by table (newest wins).

    test_source copies at most a handful of tables per call, so a source with many import
    tables is tested in several calls; each must count towards the proposal."""
    rows = await pool.fetch(
        "SELECT id, snapshot FROM source_probes WHERE kind='test' AND target=$1 AND name=$2 "
        "AND id > $3 AND ($4::bigint IS NULL OR id <= $4) ORDER BY id", target, name, probe_id, max_id)
    if not rows:
        return None
    tables: dict = {}
    base: dict = {}
    for r in rows:
        snap = _snap(r["snapshot"]) or {}
        base = snap
        for t in snap.get("tables") or []:
            tables[t.get("table")] = t
    return {"id": int(rows[-1]["id"]), "snapshot": {**base, "tables": list(tables.values())}}


async def latest_test(name: str, target: str) -> dict | None:
    pool = await get_pool()
    if isinstance(target, str) and target.startswith("cred:"):
        probe = await latest_probe(target)
        if probe is not None:
            return await merged_test(pool, name, target, probe["id"])
    return _probe_row(await pool.fetchrow(
        "SELECT id, snapshot FROM source_probes WHERE kind='test' AND target=$1 AND name=$2 "
        "ORDER BY id DESC LIMIT 1", target, name))


def find_probe_table(snapshot: dict, table: str, schema: str | None = None) -> tuple[dict | None, str | None]:
    """(probe table, error): a bare name must be unique across the probed schemas."""
    hits = [t for t in (snapshot or {}).get("tables") or []
            if t.get("table") == table and (schema is None or t.get("schema") == schema)]
    if not hits:
        return None, f"table {table} is not in the latest probe"
    if len(hits) > 1:
        return None, f"table {table} exists in several schemas; name the schema"
    return hits[0], None


def _entry(raw) -> tuple[dict | None, str | None]:
    if isinstance(raw, str):
        raw = {"table": raw}
    if not isinstance(raw, dict) or not isinstance(raw.get("table"), str) or not raw["table"]:
        return None, "every table entry needs a table name"
    if set(raw) - _TABLE_KEYS:
        return None, f"table {raw['table']}: unknown option(s) {sorted(set(raw) - _TABLE_KEYS)}"
    mk = raw.get("merge_key")
    if mk is not None and (not isinstance(mk, list) or not all(isinstance(c, str) for c in mk)):
        return None, f"table {raw['table']}: merge_key must be a list of column names"
    return raw, None


def _parse_start(v) -> tuple[str | None, str | None]:
    if v in (None, ""):
        return None, None
    try:
        d = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    except ValueError:
        return None, "backfill_start must be an ISO date or date-time such as 2025-01-01"
    return d.isoformat(), None


def validate_proposal(name, config, schedule, modes, cred, probe, test, settings) -> tuple[dict | None, str | None]:
    """Pure. cred is the public credential view; probe and test are {"id", "snapshot"} or None.
    Returns the normalized facts (the card payload without _summary) or a fixed error text."""
    err = validate_name(name)
    if err:
        return None, err
    if isinstance(config, dict) and str(config.get("target") or "").startswith("csv:"):
        return validate_csv_proposal(name, config, schedule, modes, probe, test, settings)
    if not isinstance(config, dict) or set(config) - _CONFIG_KEYS:
        return None, "config may only hold target, tables, backfill_start and freshness_minutes"
    if cred is None:
        return None, "unknown credential; ask the owner to add it on the credentials form"
    handle = cred["handle"]
    if config.get("target") != handle:
        return None, "the credential does not match the target"
    if cred.get("verify_status") != "ok":
        return None, f"credential {handle} is not verified; ask the owner to re-check it on the credentials form"
    if probe is None:
        return None, f"no probe of {handle}; call probe_source first"
    raw_tables = config.get("tables")
    if not isinstance(raw_tables, list) or not raw_tables:
        return None, "tables must be a non-empty list"
    if len(raw_tables) > MAX_PROPOSAL_TABLES:
        return None, f"at most {MAX_PROPOSAL_TABLES} tables per source"
    modes = modes or {}
    if not isinstance(modes, dict):
        return None, "modes must map table names to import or live"
    try:
        sched = normalize_schedule(schedule)
    except ValueError as e:
        return None, str(e)
    start, err = _parse_start(config.get("backfill_start"))
    if err:
        return None, err
    kind = cred["kind"]
    read_only = cred.get("read_only") is True
    tested = {r.get("remote_table") or r.get("table"): r
              for r in ((test or {}).get("snapshot") or {}).get("tables") or []
              if isinstance(r, dict)}
    name_maps: dict = {}
    seen_remote: set = set()
    out, seen, schemas, warnings = [], set(), set(), []
    for raw in raw_tables:
        e, err = _entry(raw)
        if err:
            return None, err
        tname = e["table"]
        pt, err = find_probe_table(probe["snapshot"], tname, e.get("schema"))
        if err:
            return None, err
        problem = table_name_problem(tname)
        if problem:
            return None, problem
        sch = pt.get("schema")
        if sch not in name_maps:
            name_maps[sch] = warehouse_names(probe["snapshot"], sch)
        wname = name_maps[sch].get(tname)
        if not wname or not _WH_TABLE_RE.match(wname):
            return None, "internal: no valid warehouse name for this table"
        if tname in seen_remote:
            return None, f"table {tname} is listed twice (two tables cannot share a warehouse name)"
        seen_remote.add(tname)
        if wname in seen:
            return None, f"two tables map to warehouse name {wname}"
        seen.add(wname)
        schemas.add(pt.get("schema"))
        mode = modes.get(tname, modes.get(wname, e.get("mode", "import")))
        if mode not in ("import", "live"):
            return None, f"table {tname}: mode must be import or live"
        cols = {c["name"] for c in pt.get("columns") or []}
        est = pt.get("est_rows")
        t = {"remote_schema": pt.get("schema"), "remote_table": tname, "warehouse_table": wname,
             "mode": mode, "cursor": None, "merge_key": [], "est_rows": est,
             "est_mb": float(pt.get("est_mb") or 0), "reconcile": False, "cursor_indexed": None}
        if mode == "live":
            if not read_only:
                return None, "live tables need a read-only login; this login can write"
            if kind in ("mysql", "mssql"):  # Quick 028 (WR-03, WR-04)
                from . import whsources
                if cred.get("tls") is True:
                    return None, whsources.LIVE_TLS_REFUSAL[kind]
                if not whsources.live_rows_allowed(kind, est):
                    return None, whsources.live_rows_refusal(kind)
            out.append(t)
            continue
        if test is None or test["id"] <= probe["id"]:
            return None, (f"no test copy of {name} newer than the probe; call test_source "
                          "for the import tables first")
        if tname not in tested or tested[tname].get("status") not in (None, "ok"):
            return None, f"table {tname} was not copied in the latest test; call test_source for it first"
        cursor = e.get("cursor")
        if cursor is not None and cursor not in cols:
            return None, f"cursor column {cursor} is not a column of {tname}"
        mk = e.get("merge_key")
        if mk is not None:
            bad = [c for c in mk if c not in cols]
            if bad:
                return None, f"merge_key column {bad[0]} is not a column of {tname}"
        key = list(mk) if mk else list(pt.get("pk") or [])
        if cursor is not None:
            if not key:
                return None, (f"table {tname} has a cursor but no primary key; give merge_key "
                              "or load it without a cursor")
            t["cursor"], t["merge_key"] = cursor, key
            cand = next((c for c in pt.get("cursor_candidates") or [] if c.get("column") == cursor), None)
            t["cursor_indexed"] = bool(cand and cand.get("indexed"))
            if not t["cursor_indexed"]:
                warnings.append(f"cursor {cursor} on {tname} is not indexed: each sync scans the "
                                "table on the source; prefer an indexed column or an off-hours window")
        else:
            if est is not None and est > settings.sync_reconcile_max_rows:
                return None, f"table {tname}: large tables need a cursor column"
            if est is None:
                warnings.append(f"row count of {tname} is unknown; it is replaced in full every run")
            t["merge_key"] = key
        if bool(e.get("reconcile", False)):
            if not key:
                return None, f"table {tname}: reconcile needs a primary key or merge_key"
            if est is not None and est > settings.sync_reconcile_max_rows:
                return None, (f"table {tname}: reconcile is only for tables up to "
                              f"{settings.sync_reconcile_max_rows} rows")
            t["reconcile"] = True
        out.append(t)
    if len(schemas) > 1:
        return None, "a source reads one remote schema; these tables span " + ", ".join(sorted(map(str, schemas)))
    imports = [t for t in out if t["mode"] == "import"]
    interval = sched["interval_minutes"]
    fm = config.get("freshness_minutes", 3 * interval)
    if isinstance(fm, bool) or not isinstance(fm, int) or fm <= 0:
        return None, "freshness_minutes must be a positive whole number"
    caps = {"max_rows_per_run": settings.sync_max_rows_per_run,
            "max_seconds_per_run": settings.sync_max_seconds_per_run}
    rows = sum(int(t["est_rows"] or 0) for t in imports)
    disp = cred.get("display") or {}
    return {
        "name": name, "kind": kind, "credential": handle,
        "display": {"host": disp.get("host"), "port": disp.get("port"), "database": disp.get("database")},
        "warehouse_schema": f"src_{name}", "remote_schema": next(iter(schemas)),
        "tables": out, "backfill_start": start, "schedule": sched, "caps": caps,
        "estimated": {"rows": rows, "storage_mb": round(sum(t["est_mb"] for t in imports), 2),
                      "backfill_runs": (max(1, -(-rows // caps["max_rows_per_run"])) if imports else 0)},
        "schema_policy": {"new_columns": "evolve", "type_change": "fail table"},
        "freshness_minutes": fm, "probe_id": probe["id"],
        "test_id": test["id"] if imports and test else None, "warnings": warnings,
    }, None


def build_payload(facts: dict) -> dict:
    if facts.get("kind") == "csv":
        return {**facts, "_summary": (f"Load CSV file {facts['csv']['file']} as source {facts['name']} "
                                      f"({facts['estimated']['rows']} rows, re-upload: "
                                      f"{facts['csv']['on_reupload']})")}
    tabs = facts["tables"]
    n_imp = sum(1 for t in tabs if t["mode"] == "import")
    return {**facts, "_summary": (f"Connect {facts['kind']} source {facts['name']}: "
                                  f"{n_imp} import, {len(tabs) - n_imp} live tables")}


async def insert_proposal(payload: dict, task_run_id) -> tuple[int | None, int | None]:
    """(new id, None), or (None, existing pending id) when the name already waits for approval."""
    pool = await get_pool()
    new_id = await pool.fetchval(
        """INSERT INTO actions (kind, payload, tier, status, task_run_id)
           VALUES ('source_change', $1::jsonb, 'draft', 'proposed', $2)
           ON CONFLICT ((payload->>'name')) WHERE kind = 'source_change' AND status = 'proposed'
           DO NOTHING RETURNING id""", json.dumps(payload), task_run_id)
    if new_id is not None:
        return int(new_id), None
    pending = await pool.fetchval(
        "SELECT id FROM actions WHERE kind='source_change' AND status='proposed' "
        "AND payload->>'name' = $1 ORDER BY id DESC LIMIT 1", payload.get("name"))
    return None, (int(pending) if pending is not None else None)


async def _revalidate(payload: dict) -> tuple[dict | None, str | None]:
    """Facts recomputed from the stored probe, test and credential; the payload only names choices."""
    from . import sourcevault
    if not isinstance(payload, dict):
        return None, "payload is not an object"
    name, handle = payload.get("name"), payload.get("credential")
    if payload.get("kind") == "csv":
        return await _revalidate_csv(payload)
    if not isinstance(handle, str) or not handle.startswith("cred:"):
        return None, "payload has no credential handle"
    pool = await get_pool()
    cred = next((h for h in await sourcevault.list_handles() if h["handle"] == handle), None)

    async def snap(pid, kind):
        if not isinstance(pid, int) or isinstance(pid, bool):
            return None
        row = await pool.fetchrow(
            "SELECT id, snapshot, kind, target, name FROM source_probes WHERE id=$1", pid)
        if row is None or row["kind"] != kind or row["target"] != handle:
            return None
        if kind == "test" and row["name"] != name:
            return None
        return _probe_row(row)

    probe = await snap(payload.get("probe_id"), "probe")
    test = await snap(payload.get("test_id"), "test")
    if probe is not None and test is not None:
        test = await merged_test(pool, name, handle, probe["id"], test["id"]) or test
    tabs = []
    for t in payload.get("tables") or []:
        if not isinstance(t, dict):
            return None, "invalid table entry"
        tabs.append({"table": t.get("remote_table"), "schema": t.get("remote_schema"),
                     "cursor": t.get("cursor"), "merge_key": t.get("merge_key") or None,
                     "reconcile": bool(t.get("reconcile")), "mode": t.get("mode", "import")})
    cfg = {"target": handle, "tables": tabs, "backfill_start": payload.get("backfill_start"),
           "freshness_minutes": payload.get("freshness_minutes", 180)}
    sched = payload.get("schedule")
    if isinstance(sched, dict):
        sched = {k: v for k, v in sched.items() if k in _SCHEDULE_KEYS}
    return validate_proposal(name, cfg, sched, None, cred, probe, test, get_settings())


async def apply_source_change(payload: dict) -> bool:
    """The source_change executor. approve() has already claimed the action single-winner."""
    from . import syncsched, whsources
    # Phase 17 (CONS-05): mode switches
    if isinstance(payload, dict) and payload.get("op") == "mode":
        from . import modeflip
        return await modeflip.apply(payload)
    try:
        facts, err = await _revalidate(payload)
    except Exception as e:
        log.warning("source_change re-validation crashed: %s", type(e).__name__)
        return False
    if facts is None:
        log.warning("source_change rejected at execution: %s", err)
        return False
    if facts.get("kind") == "csv":
        return await _apply_csv(facts)
    name = facts["name"]
    pool = await get_pool()
    action_id = await pool.fetchval(
        "SELECT id FROM actions WHERE kind='source_change' AND status='approved' "
        "AND payload->>'name' = $1 ORDER BY id DESC LIMIT 1", name)
    cfg = {"probe_id": facts["probe_id"], "test_id": facts["test_id"],
           "backfill_start": facts["backfill_start"], "caps": facts["caps"],
           "remote_schema": facts["remote_schema"], "display": facts["display"]}
    wh_schema = facts["warehouse_schema"]
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow(
                    "SELECT id, status, config FROM sources WHERE name=$1 FOR UPDATE", name)
                args = (facts["kind"], facts["credential"], wh_schema, json.dumps(cfg),
                        json.dumps(facts["schedule"]), facts["freshness_minutes"], action_id)
                if existing is not None:
                    ec = _j(existing["config"]) or {}
                    if existing["status"] != "paused" or ec.get("last_error") != PROVISION_FAILED:
                        log.warning("source_change: source %s already exists", name)
                        return False
                    sid = existing["id"]
                    await conn.execute("DELETE FROM source_tables WHERE source_id=$1", sid)
                    await conn.execute(
                        "UPDATE sources SET kind=$2, credential=$3, warehouse_schema=$4, status='active', "
                        "config=$5::jsonb, schedule=$6::jsonb, freshness_minutes=$7, next_sync_at=now(), "
                        "action_id=$8, updated_at=now() WHERE id=$1", sid, *args)
                else:
                    sid = await conn.fetchval(
                        "INSERT INTO sources (name, kind, credential, warehouse_schema, status, config, "
                        "schedule, freshness_minutes, next_sync_at, action_id) "
                        "VALUES ($8,$1,$2,$3,'active',$4::jsonb,$5::jsonb,$6,now(),$7) RETURNING id",
                        *args, name)
                for t in facts["tables"]:
                    await conn.execute(
                        "INSERT INTO source_tables (source_id, remote_schema, remote_table, warehouse_schema, "
                        "warehouse_table, mode, cursor_column, merge_key, reconcile, est_rows) "
                        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8::text[],$9,$10) ON CONFLICT DO NOTHING",
                        sid, t["remote_schema"], t["remote_table"], wh_schema, t["warehouse_table"],
                        t["mode"], t["cursor"], t["merge_key"], t["reconcile"], t["est_rows"])
                await conn.execute(
                    "INSERT INTO context_sources (name, kind) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    name, facts["kind"])
    except Exception as e:
        log.warning("source_change registry write failed: %s", type(e).__name__)
        return False
    _reset_cache_for_tests()
    prov = await whsources.provision(name)
    if prov.get("status") == "error":
        await pool.execute(
            "UPDATE sources SET status='paused', config = config || $2::jsonb, updated_at=now() "
            "WHERE id=$1", sid, json.dumps({"last_error": PROVISION_FAILED}))
        log.warning("source_change: provisioning of %s failed", name)
        return False
    if any(t["mode"] == "import" for t in facts["tables"]):
        await syncsched.enqueue_now(sid, "backfill")
    return True
# ── End Phase 15 (SRC-04, SRC-07) ──

# ── Phase 15 (SRC-08): CSV proposals, executor and re-upload ──
_CSV_CONFIG_KEYS = {"target", "csv_on_reupload", "freshness_minutes"}
CSV_IMPORT_ONLY = "CSV sources are import only"


def validate_csv_proposal(name, config, schedule, modes, probe, test, settings) -> tuple[dict | None, str | None]:
    """Pure. probe and test are {"id", "snapshot"} records of the csv:<sha256> target."""
    from . import csvsource
    err = validate_name(name)
    if err:
        return None, err
    if not isinstance(config, dict) or set(config) - _CSV_CONFIG_KEYS:
        return None, "a CSV config may only hold target, csv_on_reupload and freshness_minutes"
    if any(v != "import" for v in (modes or {}).values()):
        return None, CSV_IMPORT_ONLY
    if schedule:
        return None, "CSV sources have no schedule; they load when the owner re-uploads a file"
    policy = config.get("csv_on_reupload", "replace")
    if policy not in ("replace", "append"):
        return None, "csv_on_reupload must be replace or append"
    fm = config.get("freshness_minutes")
    if fm is not None and (isinstance(fm, bool) or not isinstance(fm, int) or fm <= 0):
        return None, "freshness_minutes must be a positive whole number"
    target = config.get("target")
    if probe is None:
        return None, f"no probe of {target}; call probe_source first"
    snap = probe["snapshot"]
    if snap.get("kind") != "csv" or target != "csv:" + str(snap.get("sha256")):
        return None, "the probe does not match the target"
    if test is None or test["id"] <= probe["id"]:
        return None, f"no test copy of {name} newer than the probe; call test_source first"
    ts = test["snapshot"]
    trow = next((r for r in ts.get("tables") or [] if isinstance(r, dict)), None)
    if trow is None or trow.get("status") not in (None, "ok") or ts.get("status") == "failed":
        return None, "the latest test of this file did not load; fix the file and test again"
    ctypes = ts.get("column_types")
    if not isinstance(ctypes, dict) or not ctypes:
        return None, "the test did not report column types; call test_source again"
    table = ts.get("csv_table") or csvsource.table_name_from_file(snap.get("filename"))
    if not _WH_TABLE_RE.match(str(table)):
        return None, "invalid table name for the CSV"
    rows, mb = int(snap.get("rows") or 0), round(int(snap.get("bytes") or 0) / 1048576, 2)
    caps = {"max_rows_per_run": settings.sync_max_rows_per_run,
            "max_seconds_per_run": settings.sync_max_seconds_per_run}
    return {
        "name": name, "kind": "csv", "credential": None,
        "display": {"file": snap.get("filename"), "rows": rows},
        "warehouse_schema": f"src_{name}", "remote_schema": None,
        "tables": [{"remote_schema": None, "remote_table": table, "warehouse_table": table,
                    "mode": "import", "cursor": None, "merge_key": [], "est_rows": rows,
                    "est_mb": mb, "reconcile": False, "cursor_indexed": None}],
        "csv": {"file": snap.get("filename"), "sha256": snap["sha256"], "on_reupload": policy,
                "column_types": ctypes, "delimiter": snap.get("delimiter", ","),
                "encoding": snap.get("encoding", "utf-8")},
        "backfill_start": None, "schedule": None, "caps": caps,
        "estimated": {"rows": rows, "storage_mb": mb, "backfill_runs": 1},
        "schema_policy": {"new_columns": "evolve", "type_change": "fail run"},
        "freshness_minutes": fm, "probe_id": probe["id"], "test_id": test["id"], "warnings": [],
    }, None


async def _revalidate_csv(payload: dict) -> tuple[dict | None, str | None]:
    name, c = payload.get("name"), payload.get("csv")
    if not isinstance(c, dict) or not isinstance(c.get("sha256"), str):
        return None, "payload has no csv section"
    target = "csv:" + c["sha256"]
    pool = await get_pool()

    async def snap(pid, kind):
        if not isinstance(pid, int) or isinstance(pid, bool):
            return None
        row = await pool.fetchrow(
            "SELECT id, snapshot, kind, target, name FROM source_probes WHERE id=$1", pid)
        if row is None or row["kind"] != kind or row["target"] != target:
            return None
        if kind == "test" and row["name"] != name:
            return None
        return _probe_row(row)

    cfg = {"target": target, "csv_on_reupload": c.get("on_reupload", "replace")}
    if payload.get("freshness_minutes") is not None:
        cfg["freshness_minutes"] = payload["freshness_minutes"]
    return validate_csv_proposal(name, cfg, None, None, await snap(payload.get("probe_id"), "probe"),
                                 await snap(payload.get("test_id"), "test"), get_settings())


async def _apply_csv(facts: dict) -> bool:
    import asyncio
    from . import csvsource, syncsched, whsources
    name, c = facts["name"], facts["csv"]
    pool = await get_pool()
    action_id = await pool.fetchval(
        "SELECT id FROM actions WHERE kind='source_change' AND status='approved' "
        "AND payload->>'name' = $1 ORDER BY id DESC LIMIT 1", name)
    inc = csvsource.incoming_path(c["sha256"])
    try:
        if not inc.exists():
            log.warning("source_change: incoming CSV for %s is gone", name)
            return False
        stored = await asyncio.to_thread(csvsource.store_for_source, name, inc)
        if stored["sha256"] != c["sha256"]:
            return False
    except Exception as e:
        log.warning("source_change CSV store failed: %s", type(e).__name__)
        return False
    t = facts["tables"][0]
    cfg = {"probe_id": facts["probe_id"], "test_id": facts["test_id"], "caps": facts["caps"],
           "display": facts["display"],
           "csv": {**{k: v for k, v in c.items() if k != "sha256"}, "current_sha": c["sha256"],
                   "table": t["warehouse_table"]}}
    wh_schema = facts["warehouse_schema"]
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow(
                    "SELECT id, status, config FROM sources WHERE name=$1 FOR UPDATE", name)
                if existing is not None:
                    ec = _j(existing["config"]) or {}
                    if existing["status"] != "paused" or ec.get("last_error") != PROVISION_FAILED:
                        log.warning("source_change: source %s already exists", name)
                        return False
                    sid = existing["id"]
                    await conn.execute("DELETE FROM source_tables WHERE source_id=$1", sid)
                    await conn.execute(
                        "UPDATE sources SET kind='csv', credential=NULL, warehouse_schema=$2, status='active', "
                        "config=$3::jsonb, schedule='{}'::jsonb, freshness_minutes=$4, next_sync_at=NULL, "
                        "action_id=$5, updated_at=now() WHERE id=$1",
                        sid, wh_schema, json.dumps(cfg), facts["freshness_minutes"], action_id)
                else:
                    sid = await conn.fetchval(
                        "INSERT INTO sources (name, kind, credential, warehouse_schema, status, config, "
                        "schedule, freshness_minutes, next_sync_at, action_id) "
                        "VALUES ($1,'csv',NULL,$2,'active',$3::jsonb,'{}'::jsonb,$4,NULL,$5) RETURNING id",
                        name, wh_schema, json.dumps(cfg), facts["freshness_minutes"], action_id)
                await conn.execute(
                    "INSERT INTO source_tables (source_id, remote_schema, remote_table, warehouse_schema, "
                    "warehouse_table, mode, cursor_column, merge_key, reconcile, est_rows) "
                    "VALUES ($1,NULL,$2,$3,$4,'import',NULL,'{}'::text[],false,$5) ON CONFLICT DO NOTHING",
                    sid, t["remote_table"], wh_schema, t["warehouse_table"], t["est_rows"])
                await conn.execute(
                    "INSERT INTO context_sources (name, kind) VALUES ($1, 'csv') ON CONFLICT DO NOTHING", name)
    except Exception as e:
        log.warning("source_change CSV registry write failed: %s", type(e).__name__)
        return False
    _reset_cache_for_tests()
    prov = await whsources.provision(name)
    if prov.get("status") == "error":
        await pool.execute(
            "UPDATE sources SET status='paused', config = config || $2::jsonb, updated_at=now() "
            "WHERE id=$1", sid, json.dumps({"last_error": PROVISION_FAILED}))
        log.warning("source_change: provisioning of %s failed", name)
        return False
    await syncsched.enqueue_now(sid, "csv")
    try:
        inc.unlink()
    except OSError:
        pass
    return True


async def accept_reupload(source: dict, path) -> dict:
    """Store a newly uploaded file for an approved CSV source and queue its load. Raises ValueError
    with a fixed message when the file is refused."""
    import asyncio
    from . import csvsource, syncsched
    c = dict((source.get("config") or {}).get("csv") or {})
    if source.get("kind") != "csv" or not c.get("current_sha"):
        raise ValueError(f"{source.get('name')} is not a CSV source")
    probed = await asyncio.to_thread(csvsource.probe_csv, path)
    if probed["sha256"] == c["current_sha"]:
        raise ValueError("this file is already loaded")
    if sorted(probed["columns"]) != sorted((c.get("column_types") or {}).keys()):  # jsonb reorders keys
        raise ValueError("the file's columns differ from the approved ones; propose a new source instead")
    stored = await asyncio.to_thread(csvsource.store_for_source, source["name"], path)
    c.update(current_sha=stored["sha256"], file=stored["filename"], delimiter=probed["delimiter"])
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            run_id = await syncsched.enqueue_now(source["id"], "csv", conn=conn)
            if run_id is None:
                raise ValueError("a load of this source is already queued or running")
            await conn.execute("UPDATE sources SET config = config || $2::jsonb, updated_at=now() WHERE id=$1",
                               source["id"], json.dumps({"csv": c}))
    _reset_cache_for_tests()
    return {"run_id": int(run_id), "policy": c.get("on_reupload", "replace"),
            "sha256": stored["sha256"], "file": stored["filename"]}
# ── End Phase 15 (SRC-08) ──

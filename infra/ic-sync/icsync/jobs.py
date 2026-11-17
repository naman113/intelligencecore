"""test and sync commands: dlt pipelines, one run per table, resumable chunks, schema contract."""
from __future__ import annotations

import os
import time

STALL_ERROR = ("backfill cannot advance: more rows share one cursor value than one run can load; "
               "raise the per-run row cap or choose a cursor column with fewer repeated values")
NO_KEY_ERROR = "a table with a cursor needs a merge key"
BUDGET_NOTE = "run time budget spent"
SCHEMA_CONTRACT = {"tables": "evolve", "columns": "evolve", "data_type": "freeze"}


def dlt_env() -> None:
    """Set before dlt is imported. LOAD__WORKERS=2 keeps one sync inside the loader role connection limit of 5."""
    os.environ["SCHEMA__NAMING"] = "direct"
    os.environ["DESTINATION__POSTGRES__REPLACE_STRATEGY"] = "insert-from-staging"
    os.environ["RUNTIME__DLTHUB_TELEMETRY"] = "false"
    os.environ["LOAD__WORKERS"] = "2"


dlt_env()


# ---- pure rules -------------------------------------------------------------------------------

def stalled(capped: bool, rows: int, cursor_before, cursor_after) -> bool:
    return bool(capped) and rows == 0 and cursor_after == cursor_before


def effective_chunk(caps: dict) -> int:
    return max(1, min(int(caps["chunk_size"]), int(caps["max_rows"])))


def max_items_for(max_rows: int, chunk_size: int) -> int:
    return max(1, int(max_rows) // max(1, int(chunk_size)))


def table_hints(t: dict) -> dict:
    cursor = t.get("cursor")
    if not cursor:
        return {"write_disposition": "replace", "primary_key": None, "incremental": None}
    keys = list(t.get("merge_key") or [])
    if not keys:
        raise ValueError(NO_KEY_ERROR)
    return {"write_disposition": {"disposition": "merge", "strategy": "delete-insert"},
            "primary_key": keys,
            "incremental": {"cursor_path": cursor, "initial_value": t.get("initial_value"),
                            "row_order": "asc"}}


def classify_error(exc: BaseException) -> str:
    seen = set()
    e = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if type(e).__name__ == "DataValidationError":
            return "DataValidationError"
        e = e.__cause__ or e.__context__
    return type(exc).__name__


def run_status(results: list[dict]) -> str:
    st = [r["status"] for r in results if r.get("status") != "skipped"]
    if not st or all(s == "ok" for s in st):
        return "ok"
    if all(s == "failed" for s in st):
        return "failed"
    return "partial"


def run_with_budget(tables, max_seconds, run_one, clock=time.monotonic) -> list[dict]:
    start = clock()
    out = []
    for t in tables:
        remaining = max_seconds - (clock() - start)
        if remaining <= 0:
            out.append({"table": t["warehouse_table"], "status": "skipped", "rows": 0, "cursor_max": None,
                        "capped": True, "reconciled_deleted": None, "reconcile_note": BUDGET_NOTE,
                        "error_class": None, "error": None})
            continue
        out.append(run_one(t, remaining))
    return out


# ---- dlt plumbing -----------------------------------------------------------------------------

def _row(t: dict, **kw) -> dict:
    r = {"table": t["warehouse_table"], "status": "ok", "rows": 0, "cursor_max": None, "capped": False,
         "reconciled_deleted": None, "reconcile_note": None, "error_class": None, "error": None}
    r.update(kw)
    return r


def _initial(v):
    if isinstance(v, str):
        from datetime import datetime
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return v
    return v


def _pipeline(job: dict, env):
    import dlt

    from . import conn
    c = conn.warehouse_credentials(env)
    dest = dlt.destinations.postgres(credentials={
        "host": c["host"], "port": c["port"], "database": c["database"],
        "username": c["username"], "password": c["password"]})
    return dlt.pipeline(pipeline_name=job["pipeline"], destination=dest, dataset_name=job["dataset"])


def _wh_connect(env):
    import psycopg2

    from . import conn
    c = conn.warehouse_credentials(env)
    return psycopg2.connect(host=c["host"], port=c["port"], dbname=c["database"], user=c["username"],
                            password=c["password"], connect_timeout=15)


def _cursor_state(pipe, resource: str):
    """last_value of the incremental for a resource, read from the pipeline state, as text."""
    try:
        for s in pipe.state.get("sources", {}).values():
            inc = (s.get("resources", {}).get(resource, {}) or {}).get("incremental", {})
            for st in inc.values():
                if isinstance(st, dict) and st.get("last_value") is not None:
                    return str(st["last_value"])
    except Exception:  # noqa: BLE001
        pass
    return None


def _row_count(pipe, wh_table: str) -> int:
    try:
        return int(pipe.last_trace.last_normalize_info.row_counts.get(wh_table, 0))
    except Exception:  # noqa: BLE001
        return 0


def mssql_rowversion_adapters():
    """SQL Server rowversion/timestamp columns are 8 opaque bytes that the csv loader refuses; read
    them as BIGINT so they can be a cursor (A3 fallback). Returns (table_adapter, query_adapter)."""
    import sqlalchemy as sa
    from sqlalchemy.dialects import mssql
    rv = tuple(t for t in (getattr(mssql, "ROWVERSION", None), getattr(mssql, "TIMESTAMP", None)) if t)

    def table_adapter(table):
        for c in table.c:
            if rv and isinstance(c.type, rv):
                c.info["ic_rowversion"] = True
                c.type = sa.BigInteger()
        return table

    def query_adapter(query, table):
        if not any(c.info.get("ic_rowversion") for c in table.c):
            return query
        cols = [sa.cast(c, sa.BigInteger).label(c.name) if c.info.get("ic_rowversion") else c
                for c in table.c]
        return query.with_only_columns(*cols)

    return table_adapter, query_adapter


def _resource(job: dict, t: dict, engine, chunk: int, hints: dict, limit_items, max_time):
    import dlt
    from dlt.sources.sql_database import sql_table

    inc = None
    if hints["incremental"]:
        h = hints["incremental"]
        inc = dlt.sources.incremental(h["cursor_path"], initial_value=_initial(h["initial_value"]),
                                      row_order=h["row_order"])
    extra = {}
    if job.get("kind") == "mssql":
        extra["table_adapter_callback"], extra["query_adapter_callback"] = mssql_rowversion_adapters()
    r = sql_table(engine, table=t["remote_table"], schema=job.get("remote_schema"), backend="pyarrow",
                  chunk_size=chunk, reflection_level="full_with_precision", incremental=inc,
                  write_disposition=hints["write_disposition"], primary_key=hints["primary_key"], **extra)
    r.apply_hints(table_name=t["warehouse_table"], schema_contract=SCHEMA_CONTRACT)
    r.max_table_nesting = 0
    seen = {"chunks": 0, "last": 0, "chunk": chunk}

    def count(item):
        seen["chunks"] += 1
        seen["last"] = getattr(item, "num_rows", None) or len(item)
        return item

    r.add_map(count)
    if limit_items is not None:
        r.add_limit(max_items=limit_items, max_time=max_time)
    return r, seen


def _analyze(env, dataset: str, tables: list[str]) -> None:
    from psycopg2 import sql
    if not tables:
        return
    c = _wh_connect(env)
    try:
        c.autocommit = True
        cur = c.cursor()
        for t in tables:
            cur.execute(sql.SQL("ANALYZE {}.{}").format(sql.Identifier(dataset), sql.Identifier(t)))
    finally:
        c.close()


def table_columns(env, dataset: str, tables: list[str]) -> dict[str, list[dict]]:
    c = _wh_connect(env)
    out: dict[str, list[dict]] = {}
    try:
        cur = c.cursor()
        for t in tables:
            cur.execute(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
                "JOIN pg_class k ON k.oid = a.attrelid JOIN pg_namespace n ON n.oid = k.relnamespace "
                "WHERE n.nspname = %s AND k.relname = %s AND a.attnum > 0 AND NOT a.attisdropped "
                "ORDER BY a.attnum", (dataset, t))
            out[t] = [{"name": n, "type": ty} for n, ty in cur.fetchall()]
    finally:
        c.close()
    return out


def run_test(job: dict, env) -> dict:
    from . import conn
    from .redact import error_text
    dlt_env()
    limit = min(int(job.get("row_limit") or 1000), 1000)
    engine = conn.source_engine(job["kind"], env)
    pipe = _pipeline(job, env)
    results = []
    for t in job["tables"]:
        try:
            hints = {"write_disposition": "replace", "primary_key": None, "incremental": None}
            r, _ = _resource(job, t, engine, limit, hints, 1, None)
            pipe.run(r, loader_file_format="csv")
            results.append(_row(t, rows=_row_count(pipe, t["warehouse_table"])))
        except Exception as e:  # noqa: BLE001
            results.append(_row(t, status="failed", error_class=classify_error(e), error=error_text(e, [])))
    ok = [r["table"] for r in results if r["status"] == "ok"]
    try:
        cols = table_columns(env, job["dataset"], ok)
        for r in results:
            if r["table"] in cols:
                r["columns"] = cols[r["table"]]
    except Exception:  # noqa: BLE001
        pass
    return {"status": run_status(results), "tables": results,
            "rows_total": sum(r["rows"] for r in results)}


def run_sync(job: dict, env) -> dict:
    from . import conn
    from .redact import error_text
    dlt_env()
    caps = job["caps"]
    chunk = effective_chunk(caps)
    items = max_items_for(caps["max_rows"], chunk)
    engine = conn.source_engine(job["kind"], env)
    pipe = _pipeline(job, env)
    deadline = time.monotonic() + caps["max_seconds"]

    def run_one(t: dict, remaining: float) -> dict:
        t0 = time.monotonic()
        try:
            hints = table_hints(t)
        except ValueError as e:
            return _row(t, status="failed", error_class="ValueError", error=str(e))
        cursor = t.get("cursor")
        before = None
        reset = bool(t.get("reset"))
        if cursor and not reset:
            try:
                pipe.sync_destination()
            except Exception:  # noqa: BLE001
                pass
            before = _cursor_state(pipe, t["remote_table"])
        try:
            r, seen = _resource(job, t, engine, chunk, hints, items if cursor else None,
                                remaining if cursor else None)
            if reset:
                # Phase 17 (CONS-05, D10): a live-to-import flip re-imports from scratch; drop_resources drops this table's landing table and its resource state only
                pipe.run(r, loader_file_format="csv", refresh="drop_resources")
            else:
                pipe.run(r, loader_file_format="csv")
        except Exception as e:  # noqa: BLE001
            return _row(t, status="failed", error_class=classify_error(e), error=error_text(e, []))
        rows = _row_count(pipe, t["warehouse_table"])
        after = _cursor_state(pipe, t["remote_table"]) if cursor else None
        # Hit the chunk limit with a (nearly) full last chunk: more rows probably remain. A short last
        # chunk means the source was exhausted. (-1: the boundary row is dropped by dlt's dedupe.)
        hit_limit = seen["chunks"] >= items and seen["last"] >= chunk - 1
        capped = bool(cursor) and (hit_limit or (time.monotonic() - t0) >= remaining)
        res = _row(t, rows=rows, cursor_max=after, capped=capped)
        if cursor and stalled(capped, rows, before, after):
            res.update(status="failed", error_class="CursorStalled", error=STALL_ERROR, capped=False)
        return res

    results = run_with_budget(job["tables"], caps["max_seconds"], run_one)
    by_wh = {t["warehouse_table"]: t for t in job["tables"]}
    if job.get("reconcile"):
        from .reconcile import reconcile_table
        wc = _wh_connect(env)
        try:
            for r in results:
                t = by_wh[r["table"]]
                if r["status"] != "ok" or not t.get("reconcile"):
                    continue
                if time.monotonic() >= deadline:
                    r["reconcile_note"] = "reconcile skipped: run time budget spent"
                    continue
                try:
                    out = reconcile_table(engine, wc, job["dataset"], t["warehouse_table"],
                                          job.get("remote_schema"), t["remote_table"], t["merge_key"])
                    r["reconciled_deleted"] = out["deleted"]
                    r["reconcile_note"] = out["note"]
                except Exception as e:  # noqa: BLE001
                    r["reconcile_note"] = f"reconcile skipped: {type(e).__name__}"
        finally:
            wc.close()
    try:
        _analyze(env, job["dataset"], [r["table"] for r in results if r["status"] == "ok"])
    except Exception:  # noqa: BLE001
        pass
    return {"status": run_status(results), "tables": results,
            "rows_total": sum(r["rows"] for r in results)}

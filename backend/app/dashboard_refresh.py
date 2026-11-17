"""Dashboard snapshot refresh loop (SERVE-05).

This is the only code that fills dashboard snapshots, from two sources. SQL
entries go through guardrails.run_readonly_sql (the one tenant seam, _execute)
against the tenant DB. kpi_history entries (Phase 6) go through
kpihistory.daily_rows against the working-memory DB, never the tenant DB. The
file never opens a pool of its own. Snapshots live in agent_memory; the tenant
DB is never written. It is
plumbing, not a second executor: no task_runs, no agent work. It follows the
never-die convention from worker.py and sentinel.py. The loop also runs at most one
due forecast definition per sweep (Phase 13.1), still without any LLM, and records each
definition run for System > Runs (Quick 013).
"""
import asyncio
import datetime
import decimal
import json
import logging
import math
import uuid
from contextvars import ContextVar

import asyncpg

from . import dashsettings, db, freshness, guardrails, heartbeat, jobruns, kpihistory, warehouse  # Quick 008: jobruns; Phase 17: freshness, warehouse

log = logging.getLogger("dashboard_refresh")

# Module constants (no settings entries; tests monkeypatch them).
POLL_INTERVAL = 15
BATCH = 10
QUERY_TIMEOUT = 45
FAIL_RETRY_MAX_MINUTES = 5
OPERATOR_ERROR_CAP = 500

VIEWER_MESSAGES = {
    "rejected": "query rejected by the read-only guardrail",
    "timeout": "query timed out",
    "unavailable": "data source unavailable",
    "failed": "query failed",
    "error": "refresh error",
    "too_large": "too much data for one tile",  # Phase 22: the text is built from the caps
}

# ── Phase 22 (DCAP-08): v2 snapshots, operator timezone ──
V2_RENDERER = "ic-render-2.0.0"
TZ_SQL = ("UPDATE dashboard_snapshots SET day_timezone = $3 "
          "WHERE version_id = $1 AND query_id = $2")


def too_large_message() -> str:
    rows, size = dashsettings.snapshot_caps()
    if size % 1048576 == 0:
        return f"too much data for one tile (over {rows:,} rows or {size // 1048576} MB)"
    return f"too much data for one tile (over {rows:,} rows or {size // 1024} KB)"


# The zone one refresh pair runs in: read once in _refresh_pair, used by _execute for
# SET LOCAL TimeZone and by the TZ_SQL stamp, so the stamp is the zone the rows were bucketed in.
_LIVE = object()
_PAIR_ZONE: ContextVar = ContextVar("dashboard_refresh_zone", default=_LIVE)
# ── End Phase 22 ──

# Research C6: a memory-Postgres restart raises CannotConnectNowError /
# AdminShutdownError, which are OperatorInterventionError, not PostgresConnectionError.
BLIPS = (asyncpg.PostgresConnectionError, asyncpg.CannotConnectNowError,
         asyncpg.AdminShutdownError, ConnectionError, OSError)

UNAVAILABLE_ERRORS = (
    asyncpg.PostgresConnectionError, asyncpg.CannotConnectNowError,
    asyncpg.AdminShutdownError, asyncpg.CrashShutdownError,
    asyncpg.TooManyConnectionsError, asyncpg.InvalidAuthorizationSpecificationError,
    asyncpg.InvalidCatalogNameError, asyncpg.InterfaceError,
    ConnectionError, OSError,
)

CLAIM_SQL = (
    "WITH due AS MATERIALIZED (SELECT s.version_id, s.query_id FROM dashboard_snapshots s "
    "JOIN dashboard_versions v ON v.id = s.version_id "
    # Phase 17 (CONS-03): a sync wakes a sync_aligned pair once. GREATEST of the last
    # synced inputs and the last attempt keeps FAIL_SQL / DEFER_SQL backoff intact.
    # Known edge: a sync that stamped last_success_at before a claim but committed after
    # the facts were read is caught by the next timer claim (_should_confirm re-runs it).
    "WHERE (s.next_refresh_at <= now() OR (s.sync_aligned AND s.error IS NULL AND EXISTS ("
    "SELECT 1 FROM source_tables t WHERE t.mode = 'import' AND t.last_success_at > "
    "GREATEST(COALESCE(s.inputs_synced_at, '-infinity'::timestamptz), "
    "COALESCE(s.attempted_at, '-infinity'::timestamptz)) "
    "AND (t.warehouse_schema || '.' || t.warehouse_table) = ANY(s.reads)))) "
    "AND (v.status = 'published' OR "
    "(v.status = 'draft' AND v.created_at > now() - interval '24 hours')) "
    "ORDER BY s.next_refresh_at LIMIT $1 FOR UPDATE OF s SKIP LOCKED) "
    "UPDATE dashboard_snapshots s SET attempted_at = now(), "
    "next_refresh_at = now() + make_interval(mins => s.refresh_minutes) "
    "FROM due, dashboard_versions v WHERE s.version_id = due.version_id "
    "AND s.query_id = due.query_id AND v.id = s.version_id "
    "RETURNING s.version_id, s.query_id, v.version, v.dashboard_id, s.refresh_minutes, "
    "(SELECT q->>'sql' FROM jsonb_array_elements(v.queries) q "
    "WHERE q->>'id' = s.query_id LIMIT 1) AS sql, "
    "(SELECT q FROM jsonb_array_elements(v.queries) q "
    "WHERE q->>'id' = s.query_id LIMIT 1) AS query, "
    "s.sync_aligned, s.inputs_synced_at, s.refreshed_at, s.error, s.reads, "
    "v.renderer"  # Phase 22
)

# Phase 17 (CONS-03): freshness columns on every successful write; confirm path
FRESHNESS_SQL = (
    "UPDATE dashboard_snapshots SET data_as_of = $3, reads = $4::text[], "
    "sync_aligned = $5, inputs_synced_at = $6 WHERE version_id = $1 AND query_id = $2"
)

CONFIRM_SQL = (
    "UPDATE dashboard_snapshots SET refreshed_at = now() "
    "WHERE version_id = $1 AND query_id = $2 AND error IS NULL"
)

# Phase 13.1: the seed also admits the forecast source
SEED_SQL = (
    "INSERT INTO dashboard_snapshots (version_id, query_id, refresh_minutes) "
    "SELECT v.id, q->>'id', greatest(5, least(1440, CASE WHEN q->>'refresh_minutes' ~ "
    "'^[0-9]{1,4}$' THEN (q->>'refresh_minutes')::int ELSE 15 END)) "
    "FROM dashboard_versions v CROSS JOIN LATERAL jsonb_array_elements(v.queries) q "
    "WHERE (v.status = 'published' OR (v.status = 'draft' AND "
    "v.created_at > now() - interval '24 hours')) "
    "AND jsonb_typeof(q) = 'object' AND q->>'id' ~ '^[a-z][a-z0-9_]{0,47}$' "
    "AND (q->>'sql' IS NOT NULL OR q->>'source' = 'kpi_history' OR q->>'source' = 'forecast') ON CONFLICT (version_id, query_id) DO NOTHING"
)

UPSERT_OK_SQL = (
    "INSERT INTO dashboard_snapshots (version_id, query_id, columns, data, row_count, "
    "refreshed_at, error, attempted_at) VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, now(), "
    "NULL, now()) ON CONFLICT (version_id, query_id) DO UPDATE SET "
    "columns = CASE WHEN EXCLUDED.row_count = 0 THEN dashboard_snapshots.columns "
    "ELSE EXCLUDED.columns END, data = EXCLUDED.data, row_count = EXCLUDED.row_count, "
    "refreshed_at = now(), error = NULL"
)

CLEAR_VERSION_ERROR_SQL = (
    "UPDATE dashboard_versions SET last_error = NULL WHERE id = $1 "
    "AND last_error IS NOT NULL AND NOT EXISTS (SELECT 1 FROM dashboard_snapshots "
    "WHERE version_id = $1 AND error IS NOT NULL)"
)

FAIL_SQL = (
    "UPDATE dashboard_snapshots SET error = $3, "
    "next_refresh_at = now() + make_interval(mins => least(refresh_minutes, $4)) "
    "WHERE version_id = $1 AND query_id = $2"
)

VERSION_ERROR_SQL = (
    "UPDATE dashboard_versions SET last_error = $2 WHERE id = $1 "
    "AND last_error IS DISTINCT FROM $2"
)

DEFER_SQL = (
    "UPDATE dashboard_snapshots SET next_refresh_at = now() + interval '1 minute' "
    "WHERE version_id = $1 AND query_id = $2"
)


def _clean(s: str) -> str:
    return s.replace("\x00", "")


def encode_value(v):
    """Make a tenant value jsonb-safe (no NaN, no \\u0000, no bytes, no Decimal)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, decimal.Decimal):
        if not v.is_finite():
            return None
        if v == v.to_integral_value() and abs(v) < 2 ** 53:
            return int(v)
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, datetime.timedelta):
        return v.total_seconds()
    if isinstance(v, str):
        return _clean(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return None
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (list, tuple)):
        return [encode_value(x) for x in v]
    if isinstance(v, dict):
        return {_clean(str(k)): encode_value(x) for k, x in v.items()}
    return _clean(str(v))


def encode_rows(rows: list[dict]) -> tuple[list[str], str]:
    columns = [_clean(str(k)) for k in rows[0].keys()] if rows else []
    text = json.dumps([encode_value(dict(r)) for r in rows], allow_nan=False)
    return columns, text


def categorize(exc) -> str:
    if isinstance(exc, guardrails.SQLGuardrailError):
        return "rejected"
    if isinstance(exc, (TimeoutError, asyncpg.QueryCanceledError)):
        return "timeout"
    if isinstance(exc, UNAVAILABLE_ERRORS):
        return "unavailable"
    if isinstance(exc, asyncpg.PostgresError):
        return "failed"
    return "error"


def operator_error(qid: str, category: str, exc) -> str:
    msg = f"query {qid}: {too_large_message() if category == 'too_large' else VIEWER_MESSAGES[category]}"
    if category in ("failed", "rejected"):
        msg += f": {exc}"
    return _clean(msg)[:OPERATOR_ERROR_CAP]


def _tenant_configured() -> bool:
    """Quick 033: tenant reads can run (a business database, or the warehouse on a
    sources-only install). No warehouse and no business URL: the loop stays quiet."""
    return guardrails.reads_configured()


async def _execute(sql: str, limit: int | None = None) -> list[dict]:
    """The one tenant seam. The kpi_history branch lives in _refresh_pair so tests
    can keep faking _execute(sql). This file never opens a pool."""
    # Phase 22: keyword arguments only for non-None values, so the defaults call
    # run_readonly_sql(sql) exactly as before.
    kw = {}
    if limit is not None:
        kw["limit"] = limit
    tz = _PAIR_ZONE.get()
    if tz is _LIVE:
        tz = dashsettings.day_timezone()
    if tz is not None:
        kw["timezone"] = tz
    return await asyncio.wait_for(guardrails.run_readonly_sql(sql, **kw), QUERY_TIMEOUT)


# Phase 6: kpi_history source (D1)
KPI_SOURCE = "kpi_history"


async def _kpi_rows(q: dict) -> list[dict]:
    watch_id = int(q["watch_id"])
    days = q.get("days", 30)
    return await asyncio.wait_for(kpihistory.daily_rows(watch_id, days), QUERY_TIMEOUT)


# Quick 008: the operator error of each failed pair in this batch, for Runs.
_batch_errors: dict = {}


async def _record_failure(pool, pair, category, exc):
    vid, qid = pair["version_id"], pair["query_id"]
    _batch_errors[(vid, qid)] = operator_error(qid, category, exc)  # Quick 008
    viewer = too_large_message() if category == "too_large" else VIEWER_MESSAGES[category]
    await pool.execute(FAIL_SQL, vid, qid, viewer,
                       FAIL_RETRY_MAX_MINUTES)
    await pool.execute(VERSION_ERROR_SQL, vid, operator_error(qid, category, exc))
    detail = f"{type(exc).__name__}: {exc}" if category in ("failed", "rejected") \
        else type(exc).__name__
    log.warning("dashboard refresh v%s query %s failed: %s %s", vid, qid, category,
                detail)


def _should_confirm(pair, facts, now_local) -> bool:
    """Phase 17 (CONS-03): a sync_aligned pair is only confirmed (refreshed_at moves,
    no tenant query) while nothing it reads has synced since inputs_synced_at.
    Gap closure (17-15): and while every read is still an imported table with no
    switch pending; a live, mid-switch or missing read (or no reads) re-runs."""
    if not _get(pair, "sync_aligned") or _get(pair, "error") is not None or facts is None:
        return False
    refreshed, inputs = _get(pair, "refreshed_at"), _get(pair, "inputs_synced_at")
    if refreshed is None or inputs is None:
        return False
    if now_local - refreshed > datetime.timedelta(hours=24):
        return False
    if refreshed.astimezone(now_local.tzinfo).date() != now_local.date():
        return False
    reads = _get(pair, "reads") or []
    if not reads:  # Phase 17 gap closure (17-15)
        return False
    for name in reads:
        schema, _, table = str(name).partition(".")
        fact = facts.tables.get(freshness.fold(schema, table))
        # Phase 17 gap closure (17-15): only confirm while every read is imported
        if fact is None or fact.get("mode") != "import" or fact.get("pending_mode"):
            return False
        last = fact.get("last_success_at")
        if last is not None and last > inputs:
            return False
    return True


async def _freshness_columns(pair, q, sql_run: bool, facts, now):
    """(data_as_of, reads, sync_aligned, inputs_synced_at). Never raises."""
    default = (now, [], False, None)
    try:
        if not sql_run:
            if isinstance(q, dict) and q.get("source") == KPI_SOURCE:
                return (await kpihistory.latest_as_of(int(q["watch_id"])), [], False, None)
            return (None, [], False, None)
        if not warehouse.warehouse_mode():
            return default
        f = await freshness.for_sql(pair["sql"], cache=facts)
        if f.failed:
            return default
        aligned = (bool(f.reads) and not f.live_reads
                   and freshness.clock_kind(pair["sql"]) != "clock")
        as_of = f.as_of if (aligned or f.import_reads) else now
        return (as_of, list(f.reads), aligned, f.inputs_synced_at)
    except Exception as e:  # noqa: BLE001 - freshness never fails a pair
        log.debug("freshness step skipped: %s", type(e).__name__)
        return default


async def _refresh_pair(pool, pair, facts=None) -> str | None:
    vid, qid = pair["version_id"], pair["query_id"]
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        import zoneinfo
        now_local = now.astimezone(zoneinfo.ZoneInfo(kpihistory._tz()))
        if _should_confirm(pair, facts, now_local):
            await pool.execute(CONFIRM_SQL, vid, qid)
            return None
    except BLIPS:
        raise
    except Exception as e:  # noqa: BLE001 - fall through to a normal re-run
        log.debug("confirm check skipped: %s", type(e).__name__)
    q = pair.get("query")
    if isinstance(q, str):
        try:
            q = json.loads(q)
        except ValueError:
            q = None
    sql_run = True
    if isinstance(q, dict) and q.get("source") == KPI_SOURCE and pair["sql"] is None:
        sql_run = False
        try:
            rows = await _kpi_rows(q)
        except Exception as e:  # noqa: BLE001 - per-pair isolation
            category = "failed" if isinstance(e, LookupError) else categorize(e)
            await _record_failure(pool, pair, category, e)
            return category
    elif isinstance(q, dict) and q.get("source") == FORECAST_SOURCE and pair["sql"] is None:
        sql_run = False
        # Phase 13.1: forecast source, working-memory DB only
        try:
            rows = await _forecast_rows(q)
        except Exception as e:  # noqa: BLE001 - per-pair isolation
            category = "failed" if isinstance(e, LookupError) else categorize(e)
            await _record_failure(pool, pair, category, e)
            return category
    else:
        if pair["sql"] is None:
            exc = ValueError("query is not declared in the version")
            await _record_failure(pool, pair, "failed", exc)
            return "failed"
        v2 = _get(pair, "renderer") == V2_RENDERER  # Phase 22
        pair_tz = dashsettings.day_timezone()  # read once: the zone run AND stamped
        zone_token = _PAIR_ZONE.set(pair_tz)
        try:
            if v2:
                cap_rows, _cap_bytes = dashsettings.snapshot_caps()
                rows = await _execute(pair["sql"], cap_rows + 1)
            else:
                rows = await _execute(pair["sql"])
        except Exception as e:  # noqa: BLE001 - per-pair isolation
            category = categorize(e)
            await _record_failure(pool, pair, category, e)
            return category
        finally:
            _PAIR_ZONE.reset(zone_token)
        if v2:
            cap_rows, cap_bytes = dashsettings.snapshot_caps()
            too_big = len(rows) > cap_rows
            if not too_big:
                try:
                    too_big = len(encode_rows(rows)[1].encode("utf-8")) > cap_bytes
                except Exception:  # noqa: BLE001 - the write step below reports encode errors
                    too_big = False
            if too_big:
                await _record_failure(pool, pair, "too_large",
                                      ValueError("snapshot over the dashboard cap"))
                return "too_large"
    try:
        columns, text = encode_rows(rows)
        await pool.execute(UPSERT_OK_SQL, vid, qid, json.dumps(columns), text, len(rows))
        await pool.execute(CLEAR_VERSION_ERROR_SQL, vid)
        cols = await _freshness_columns(pair, q, sql_run, facts, now)
        await pool.execute(FRESHNESS_SQL, vid, qid, *cols)  # Phase 17 (CONS-03)
        if sql_run:  # Phase 22: the zone the day buckets were computed in (NULL clears it)
            await pool.execute(TZ_SQL, vid, qid, pair_tz)
    except BLIPS:
        raise  # memory DB blip: run_forever logs one line and retries
    except Exception as e:  # noqa: BLE001 - encode/write failure is per-pair
        await _record_failure(pool, pair, "error", e)
        return "error"
    return None


async def refresh_due() -> bool:
    if not _tenant_configured():
        return False
    pool = await db.get_pool()
    try:
        await pool.execute(SEED_SQL)
    except BLIPS:
        raise
    except asyncpg.PostgresError as e:
        log.warning("dashboard refresh seed skipped: %s", type(e).__name__)
    pairs = await pool.fetch(CLAIM_SQL, BATCH)
    facts = None  # Phase 17 (CONS-03): one fact load per batch, fail-soft
    try:
        if pairs and warehouse.warehouse_mode():
            facts = await freshness.load_facts()
    except Exception as e:  # noqa: BLE001
        log.debug("freshness facts unavailable: %s", type(e).__name__)
    started = datetime.datetime.now(datetime.timezone.utc)  # Quick 008
    outcomes = []
    _batch_errors.clear()
    async with jobruns.track("dashboard_refresh", trigger="refresh schedule"):  # in flight only
        for i, pair in enumerate(pairs):
            category = await _refresh_pair(pool, pair, facts=facts)
            outcomes.append((pair, category))
            if category == "unavailable":
                for rest in pairs[i + 1:]:
                    await pool.execute(DEFER_SQL, rest["version_id"], rest["query_id"])
                    outcomes.append((rest, "deferred"))
                break
    await _record_runs(outcomes, started)
    return len(pairs) == BATCH


def _get(pair, key):
    try:
        return pair[key]
    except (KeyError, IndexError):
        return None


async def _record_runs(outcomes: list, started) -> None:
    """Quick 008: one Runs record per dashboard refreshed in this batch. All
    queries fine is routine (collapses into the previous ok row); any failed
    query makes it a failed record naming the queries. jobruns never raises."""
    by_dash: dict = {}
    for pair, category in outcomes:
        by_dash.setdefault(str(_get(pair, "dashboard_id")), []).append((pair, category))
    for did, items in by_dash.items():
        failed = [(p, c) for p, c in items if c not in (None, "deferred")]
        deferred = sum(1 for _, c in items if c == "deferred")
        ok = len(items) - len(failed) - deferred
        version = _get(items[0][0], "version")
        minutes = _get(items[0][0], "refresh_minutes")
        took = int((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds() * 1000)
        kw = dict(counts={"queries": len(items), "ok": ok, "failed": len(failed),
                          "deferred": deferred, "version": version}, duration_ms=took,
                  trigger=jobruns.every(minutes * 60) if isinstance(minutes, int)
                  else "refresh schedule",
                  ref=("dashboard", did), started_at=started)
        note = f", {deferred} put off (data source unavailable)" if deferred else ""
        if failed:
            errors = "; ".join(
                _batch_errors.get((_get(p, "version_id"), _get(p, "query_id")))
                or f"query {_get(p, 'query_id')}: {VIEWER_MESSAGES.get(c, c)}"
                for p, c in failed)
            await jobruns.record("dashboard_refresh", status="failed", error=errors,
                                 summary=f"{ok} of {len(items)} queries refreshed{note}",
                                 collapse="replace", **kw)
        else:
            await jobruns.record("dashboard_refresh",
                                 summary=f"Refreshed {ok} {'query' if ok == 1 else 'queries'}"
                                         f" (version {version}){note}",
                                 routine=not deferred, **kw)
# ── Phase 13.1: nightly forecast definitions (FCST-02) ──
FORECAST_SOURCE = "forecast"
# Quick 013: a safety net above the step's own budgets (query 45 s, sandbox 120 s plus
# container start, two memory-DB writes); the sandbox is shielded from it (WR-01)
FORECAST_STEP_TIMEOUT = 240
FORECAST_KIND = "forecast"  # Quick 013: job_runs kind (runsapi labels it "Forecast")


async def _forecast_rows(q: dict) -> list[dict]:
    from . import forecasts
    return await asyncio.wait_for(
        forecasts.dashboard_rows(int(q["forecast_id"])), QUERY_TIMEOUT)


async def forecast_due() -> None:
    """Run at most one due forecast definition and record it for Runs (Quick 013).
    Never raises (except cancellation)."""
    if not _tenant_configured():
        return
    info: dict = {}
    started = datetime.datetime.now(datetime.timezone.utc)
    async with jobruns.track(FORECAST_KIND, trigger="forecast schedule"):  # in flight only
        try:
            from . import forecasts
            await asyncio.wait_for(forecasts.run_one_due(info=info), FORECAST_STEP_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            # before BLIPS: on Python 3.12 TimeoutError is an OSError (WR-01)
            log.warning("forecast step timed out after %ss (forecast %s)",
                        FORECAST_STEP_TIMEOUT, info.get("id"))
            await _forecast_timed_out(info)
        except BLIPS as e:
            log.warning("forecast step DB connection blip: %s", type(e).__name__)
        except Exception:
            log.exception("forecast step error")
    await _record_forecast(info, started)


async def _forecast_timed_out(info: dict) -> None:
    """The step timeout cut off a claimed definition: record its failure so the lease
    does not retry every 30 minutes. Never raises."""
    if info.get("id") is None or info.get("status"):
        return
    try:
        from . import forecasts
        info.update(status="failed", error=forecasts.FAIL_TEXT["step_timeout"])
        await forecasts.fail_claimed(info["id"], info["error"])
    except Exception as e:  # noqa: BLE001 - the step never raises
        log.warning("forecast %s: could not record the step timeout: %s", info["id"],
                    type(e).__name__)


def _miss(wape) -> str:
    if isinstance(wape, (int, float)) and not isinstance(wape, bool) and math.isfinite(wape):
        return f", typical miss {round(wape)}% (WAPE)"
    return ""


async def _record_forecast(info: dict, started) -> None:
    """Quick 013: one Runs record per definition run, the way dashboard refreshes are
    recorded: kind forecast, ref forecast:<id>, counts. A success or a deferral is routine
    (collapses into the previous routine row, routine prune rules); a failure names the
    definition and collapses only into the same error. jobruns never raises."""
    fid = info.get("id")
    if fid is None:
        return  # nothing was due (or the claim itself failed): a no-op tick
    name = str(info.get("name") or f"forecast {fid}")
    status = info.get("status")  # ok | failed | deferred | None (stopped before recording)
    ok, deferred = status == "ok", status == "deferred"
    failed = not ok and not deferred
    counts = {"definitions": 1, "ok": int(ok), "failed": int(failed), "deferred": int(deferred)}
    if ok:
        wape = info.get("wape")
        counts.update(method=info.get("method"),
                      wape=round(wape, 1) if _miss(wape) else None)
    took = int((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds() * 1000)
    kw = dict(counts=counts, duration_ms=took, started_at=started, ref=("forecast", str(fid)),
              trigger="nightly" if info.get("nightly") else "run now or new forecast")
    if failed:
        why = info.get("error") or "stopped before the result was recorded"
        await jobruns.record(FORECAST_KIND, name, status="failed",
                             error=f"forecast '{name}' (#{fid}): {why}",
                             summary="Not refreshed; the previous forecast is kept",
                             collapse="replace", **kw)
    elif deferred:
        await jobruns.record(FORECAST_KIND, name,
                             summary="Put off: the business database is unavailable; "
                                     "it retries in 15 minutes",
                             routine=True, collapse="replace", **kw)
    else:
        horizon, grain = info.get("horizon"), info.get("grain")
        ahead = f"{horizon} {grain}s ahead" if horizon and grain else "refreshed"
        await jobruns.record(FORECAST_KIND, name,
                             summary=f"Forecast refreshed: {ahead}, method "
                                     f"{info.get('method') or 'unknown'}{_miss(info.get('wape'))}",
                             routine=True, collapse="replace", **kw)


async def run_forever() -> None:
    log.info("dashboard refresh started")
    while True:
        busy = False
        try:
            heartbeat.busy("dashboard_refresh", BATCH * QUERY_TIMEOUT + FORECAST_STEP_TIMEOUT + 60)  # Phase 11: GOLIVE-05 heartbeat; Phase 13.1: covers the forecast step
            busy = await refresh_due()
            await forecast_due()  # Phase 13.1: one forecast definition per sweep, never raises
            heartbeat.beat("dashboard_refresh", POLL_INTERVAL + 60)  # Phase 11: GOLIVE-05 heartbeat
        except BLIPS as e:
            log.warning("dashboard refresh DB connection blip, retrying: %s",
                        type(e).__name__)
        except Exception as e:
            log.exception("dashboard refresh sweep error")
            await jobruns.loop_error("dashboard_refresh", e)  # Quick 008 (never raises)
        await asyncio.sleep(0 if busy else POLL_INTERVAL)

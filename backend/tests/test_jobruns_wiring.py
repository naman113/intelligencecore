"""Quick 008: the loops record what they did through jobruns, and a broken
recorder never changes what a loop does.

Fakes only. The job_runs fake is test_jobruns.FakePool, installed as the app's
open pool (jobruns writes only through an already-open pool)."""
import asyncio
import json
from datetime import datetime, timezone

import pytest

from app import consolidation, db, expectations, jobruns, sentinel
from app import dashboard_refresh as dr
from test_jobruns import BrokenPool, FakePool

TRIG = {"type": "threshold", "column": "stale", "op": ">", "value": 5}


@pytest.fixture
def jobs(monkeypatch):
    p = FakePool()
    monkeypatch.setattr(db, "_pool", p)
    monkeypatch.setattr(jobruns, "_last_prune", float("inf"))
    return p


# ── sentinel ──────────────────────────────────────────────────────────────

def _watch(**kw):
    w = {"id": 7, "thread_id": "ui:owner", "kind": "kpi", "title": "Stale orders",
         "condition": "stale orders", "one_shot": False, "status": "active",
         "last_state": {"stale": 2}, "consecutive_errors": 0, "cadence_seconds": 900,
         "compiled_sql": "SELECT 3 AS stale", "trigger_rule": dict(TRIG)}
    w.update(kw)
    return w


class WatchPool:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append(sql)

    async def fetchrow(self, sql, *args):
        return _watch()


def _wire(monkeypatch, rows=None, raises=None):
    pool, fired = WatchPool(), []

    async def get_pool():
        return pool

    async def run(sql):
        if raises:
            raise raises
        return rows

    async def fire(*a, **k):
        fired.append(a)

    async def no_history(*a, **k):
        return None

    monkeypatch.setattr(sentinel, "get_pool", get_pool)
    monkeypatch.setattr(sentinel, "run_readonly_sql", run)
    monkeypatch.setattr(sentinel, "_fire", fire)
    monkeypatch.setattr(sentinel.kpihistory, "record", no_history)
    return pool, fired


async def test_a_quiet_check_is_a_routine_row_that_collapses(monkeypatch, jobs):
    _wire(monkeypatch, rows=[{"stale": 3}])
    await sentinel._check_one(_watch())
    await sentinel._check_one(_watch())
    assert len(jobs.rows) == 1
    row = jobs.rows[0]
    assert (row["kind"], row["name"], row["ref_type"], row["ref_id"]) == \
        ("watch_check", "Stale orders", "watch", "7")
    assert row["summary"] == "stale = 3 · alerts when > 5"
    assert row["trigger"] == "every 15 min" and row["routine"] and row["repeats"] == 2
    assert row["counts"] == {"value": 3}


async def test_a_fire_is_a_notable_row(monkeypatch, jobs):
    _, fired = _wire(monkeypatch, rows=[{"stale": 9}])
    await sentinel._check_one(_watch())
    assert fired
    row = jobs.rows[0]
    assert row["summary"] == "Fired (critical): stale = 9 · alerts when > 5"
    assert row["routine"] is False and row["status"] == "done"


async def test_a_failed_query_is_a_failed_row(monkeypatch, jobs):
    pool, _ = _wire(monkeypatch, raises=RuntimeError("relation does not exist"))
    await sentinel._check_one(_watch())
    assert any("SET compiled_sql = NULL" in c for c in pool.calls)
    row = jobs.rows[0]
    assert row["status"] == "failed" and "relation does not exist" in row["error"]


async def test_a_failed_compile_says_why(monkeypatch, jobs):
    _wire(monkeypatch, rows=[{"stale": 1}])

    async def comp(w):
        w["_compile_error"] = "compiler ran out of steps"
        return None

    monkeypatch.setattr(sentinel, "_compile_watch", comp)
    await sentinel._check_one(_watch(compiled_sql=None, consecutive_errors=2))
    row = jobs.rows[0]
    assert row["status"] == "failed"
    assert row["error"] == "Could not compile the check: compiler ran out of steps (paused after 3 tries)"


async def test_a_first_compile_is_not_routine(monkeypatch, jobs):
    _wire(monkeypatch, rows=[{"stale": 1}])

    async def comp(w):
        return {"sql": "SELECT 1 AS stale", "trigger": dict(TRIG), "label": "x"}

    monkeypatch.setattr(sentinel, "_compile_watch", comp)
    await sentinel._check_one(_watch(compiled_sql=None))
    assert jobs.rows[0]["summary"].startswith("Compiled the check (first run).")
    assert jobs.rows[0]["routine"] is False


async def test_a_broken_recorder_does_not_change_the_check(monkeypatch):
    monkeypatch.setattr(db, "_pool", BrokenPool())
    pool, fired = _wire(monkeypatch, rows=[{"stale": 9}])
    await sentinel._check_one(_watch())
    assert fired and any("SET last_state" in c for c in pool.calls)


async def test_the_sentinel_loop_records_its_error_and_keeps_going(monkeypatch, jobs):
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise ValueError("bad row")

    monkeypatch.setattr(sentinel, "_due_watches", boom)
    monkeypatch.setattr(sentinel, "POLL_INTERVAL", 0.01)
    task = asyncio.create_task(sentinel.run_forever())
    await asyncio.sleep(0.08)
    alive = not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert alive and calls["n"] >= 2
    loops = [r for r in jobs.rows if r["kind"] == "loop"]
    assert len(loops) == 1 and loops[0]["repeats"] >= 2
    assert loops[0]["name"] == "Watch sweep" and loops[0]["error"] == "ValueError: bad row"


async def test_rate_limited_investigation_is_recorded_as_skipped(monkeypatch, jobs):
    import time
    monkeypatch.setattr(sentinel, "_escalation_times", [time.time()] * sentinel.MAX_ESCALATIONS_PER_HOUR)
    assert await sentinel._maybe_escalate(_watch(), {"severity": "critical"}) is False
    row = jobs.rows[0]
    assert row["kind"] == "watch_investigation" and row["summary"].startswith("Skipped")
    assert row["trigger"] == "critical alert"


async def test_an_investigation_is_a_live_row_with_its_trace(monkeypatch, jobs):
    from app.agents import graph
    delivered = []

    async def run(history, brief, **kw):
        assert jobs.rows[0]["status"] == "running"
        return "Orders stuck at the South depot since Monday."

    async def deliver(thread, msg):
        delivered.append(msg)

    async def not_paused():
        return None

    monkeypatch.setattr(sentinel, "_escalation_times", [])
    monkeypatch.setattr(graph, "run", run)
    monkeypatch.setattr(sentinel, "_deliver", deliver)
    monkeypatch.setattr(sentinel.usage, "background_pause_reason", not_paused)
    assert await sentinel._maybe_escalate(_watch(), {"severity": "critical", "summary": "x"}) is True
    row = jobs.rows[0]
    assert row["status"] == "done" and row["summary"].startswith("Orders stuck")
    assert row["trace_id"].startswith("watch_investigation:7:")
    assert delivered


# ── dashboard refresh ─────────────────────────────────────────────────────

def _pair(did, qid, version=3, minutes=15):
    return {"version_id": 10 + did, "query_id": qid, "version": version,
            "dashboard_id": did, "refresh_minutes": minutes}


async def test_refresh_records_one_row_per_dashboard(jobs):
    dr._batch_errors.clear()
    dr._batch_errors[(12, "b")] = 'query b: query failed: relation "x" does not exist'
    started = datetime.now(timezone.utc)
    await dr._record_runs([(_pair(1, "a"), None), (_pair(1, "c"), None),
                           (_pair(2, "b"), "failed"), (_pair(2, "d"), "deferred")], started)
    by_ref = {r["ref_id"]: r for r in jobs.rows}
    ok, bad = by_ref["1"], by_ref["2"]
    assert ok["status"] == "done" and ok["routine"] and ok["summary"] == "Refreshed 2 queries (version 3)"
    assert ok["trigger"] == "every 15 min" and ok["counts"]["ok"] == 2
    assert bad["status"] == "failed" and 'relation "x" does not exist' in bad["error"]
    assert bad["counts"] == {"queries": 2, "ok": 0, "failed": 1, "deferred": 1, "version": 3}


async def test_refresh_batches_collapse_per_dashboard(jobs):
    for _ in range(3):
        await dr._record_runs([(_pair(1, "a"), None)], datetime.now(timezone.utc))
    assert len(jobs.rows) == 1 and jobs.rows[0]["repeats"] == 3


# ── scheduler ─────────────────────────────────────────────────────────────

def _settings(**kw):
    import types
    base = dict(anthropic_api_key="k", openai_api_key="", consolidation_enabled=True,
                consolidation_hour=3, timezone="UTC")
    base.update(kw)
    return types.SimpleNamespace(**base)


def _wire_cron(monkeypatch, playbooks, nightly=None):
    class P:
        async def fetch(self, sql, *a):
            return list(playbooks)

    async def gp():
        return P()

    async def state():
        return {}

    async def save(s):
        return None

    async def enqueue(pid, **kw):
        return 41, True

    async def nightly_run():
        return nightly or {}

    monkeypatch.setattr(expectations, "get_settings", lambda: _settings())
    monkeypatch.setattr(expectations, "get_pool", gp)
    monkeypatch.setattr(expectations, "_load_cron_state", state)
    monkeypatch.setattr(expectations, "_save_cron_state", save)
    monkeypatch.setattr(expectations, "_local_now",
                        lambda: datetime(2026, 10, 26, 3, 5, tzinfo=timezone.utc))
    monkeypatch.setattr(consolidation, "run_nightly", nightly_run)
    import app.playbooks
    monkeypatch.setattr(app.playbooks, "enqueue_playbook_run_once", enqueue)


async def test_cron_dispatch_and_nightly_are_recorded(monkeypatch, jobs):
    _wire_cron(monkeypatch, [{"id": 5, "name": "explore_data",
                              "trigger": json.dumps({"type": "cron", "hour": 3})}],
               nightly={"learnings_merged": 2, "communities_rebuilt": True, "episodes_pruned": 0,
                        "retention": {"dry_run": True, "cutoff": "x", "events": 4, "messages": 6},
                        "history_retention": "error"})
    await expectations._dispatch_cron()
    disp = next(r for r in jobs.rows if r["kind"] == "playbook_dispatch")
    assert (disp["name"], disp["task_run_id"], disp["ref_id"], disp["trigger"]) == \
        ("explore_data", 41, "5", "daily at 03:00")
    night = next(r for r in jobs.rows if r["kind"] == "consolidation")
    assert night["status"] == "failed" and night["error"] == "some steps failed: history retention"
    assert night["summary"].startswith("Merged 2 learning cluster(s)")
    assert "would delete 10 old rows (dry run)" in night["summary"]
    assert night["trigger"] == "nightly at 03:00"


def test_summarize_nightly_all_good():
    text, counts, failed = consolidation.summarize_nightly(
        {"learnings_merged": 0, "communities_rebuilt": False, "episodes_pruned": 0,
         "retention": {"dry_run": False, "events": 3}, "history_retention": {"dry_run": False}})
    assert failed == [] and text.startswith("Merged 0 learning cluster(s)")
    assert "deleted 3 old rows" in text and counts["retention_rows"] == 3


async def test_violation_sweep_without_a_handler_is_a_failure(monkeypatch, jobs):
    class Conn:
        async def fetch(self, sql, *a):
            return [{"id": 1, "case_id": 2, "playbook_id": None}]

        async def fetchval(self, sql, *a):
            return 1 if "UPDATE expectations" in sql else None

    class Acq:
        async def __aenter__(self):
            return Conn()

        async def __aexit__(self, *a):
            return False

    class P:
        def acquire(self):
            return Acq()

    async def gp():
        return P()

    monkeypatch.setattr(expectations, "get_pool", gp)
    assert await expectations._sweep_violations() == 0
    row = jobs.rows[0]
    assert row["kind"] == "followup_sweep" and row["status"] == "failed"
    assert row["counts"] == {"queued": 0, "no_handler": 1}


# ── data intake ───────────────────────────────────────────────────────────

async def test_poller_records_new_rows_and_failures(jobs):
    from app import connectors
    for new in ({"Orders": 2}, {"Orders": 1, "Machines": 4}):
        async with jobruns.track("db_poll", "Database poller") as job:
            connectors._note_poll(job, new, [])
    async with jobruns.track("db_poll", "Database poller") as job:
        connectors._note_poll(job, {}, [])          # nothing new: no row
    assert len(jobs.rows) == 1
    assert jobs.rows[0]["counts"] == {"rows": 7, "Orders": 3, "Machines": 4}
    async with jobruns.track("db_poll", "Database poller") as job:
        connectors._note_poll(job, {}, ["Orders: UndefinedTableError: gone"])
    assert jobs.rows[1]["status"] == "failed" and "UndefinedTableError" in jobs.rows[1]["error"]


# ── Quick 006 case lifecycle hook ─────────────────────────────────────────

async def test_finding_case_open_and_close_show_on_runs(jobs):
    from app import findingcases
    await findingcases._record_run(5, "KPI alert: Stale orders", "Opened from a critical alert", "critical alert")
    row = jobs.rows[0]
    assert (row["kind"], row["ref_type"], row["ref_id"], row["name"]) == \
        ("case", "case", "5", "KPI alert: Stale orders")


def test_every_case_open_and_close_path_records():
    import inspect
    from app import findingcases
    for fn in (findingcases.on_watch_fire, findingcases.close_case, findingcases.insight_case,
               findingcases.open_chat_case):
        assert "_record_run(" in inspect.getsource(fn), fn.__name__

"""Quick 008: jobruns, the one writer of job_runs (background runs on the Runs page).

Pure tests with an in-memory fake pool. The rules that matter most: a no-op
tick writes nothing, routine successes collapse, the recorder never raises into
a loop (broken pool, hung pool), and an exception from the tracked work is
recorded and re-raised unchanged.
"""
import asyncio
import json
from datetime import datetime, timezone

import pytest

from app import db, jobruns


class FakePool:
    """Just enough of job_runs for the SQL jobruns sends."""

    def __init__(self):
        self.rows: list[dict] = []
        self.sql: list[str] = []
        self.stale: set[int] = set()

    def _row(self, rid):
        return next(r for r in self.rows if r["id"] == rid)

    async def fetchval(self, sql, *a):
        self.sql.append(sql)
        if sql == jobruns._START_SQL:
            kind, name, trigger, rtype, rid, started = a
            row = dict(kind=kind, name=name, trigger=trigger, status="running", summary="",
                       error=None, counts={}, ref_type=rtype, ref_id=rid, task_run_id=None,
                       trace_id=None, routine=False, repeats=1, duration_ms=None,
                       started_at=started)
        elif sql == jobruns._INSERT_SQL:
            (kind, name, trigger, status, summary, error, counts, rtype, rid, trid, trace,
             routine, ms, started) = a
            row = dict(kind=kind, name=name, trigger=trigger, status=status, summary=summary,
                       error=error, counts=json.loads(counts), ref_type=rtype, ref_id=rid,
                       task_run_id=trid, trace_id=trace, routine=routine, repeats=1,
                       duration_ms=ms, started_at=started)
        else:
            raise AssertionError(f"unexpected fetchval: {sql[:60]}")
        row["id"] = len(self.rows) + 1
        self.rows.append(row)
        return row["id"]

    async def fetchrow(self, sql, *a):
        self.sql.append(sql)
        assert sql == jobruns._LAST_SQL
        kind, rtype, rid, _idle, _span = a
        mine = [r for r in self.rows if (r["kind"], r["ref_type"], r["ref_id"]) == (kind, rtype, rid)]
        if not mine:
            return None
        last = mine[-1]
        return {**last, "counts": json.dumps(last["counts"]), "fresh": last["id"] not in self.stale}

    async def execute(self, sql, *a):
        self.sql.append(sql)
        if sql == jobruns._BUMP_SQL:
            rid, summary, error, counts, ms, trigger, name, trid, trace = a
            r = self._row(rid)
            r.update(repeats=r["repeats"] + 1, summary=summary, error=error,
                     counts=json.loads(counts), duration_ms=ms, trigger=trigger, name=name)
            r["task_run_id"] = trid if trid is not None else r["task_run_id"]
            r["trace_id"] = trace or r["trace_id"]
            return "UPDATE 1"
        if sql == jobruns._FINISH_SQL:
            (rid, status, summary, error, counts, name, trigger, rtype, ref_id, trid, trace,
             routine, ms) = a
            self._row(rid).update(status=status, summary=summary, error=error,
                                  counts=json.loads(counts), name=name, trigger=trigger,
                                  ref_type=rtype, ref_id=ref_id, task_run_id=trid,
                                  trace_id=trace, routine=routine, duration_ms=ms)
            return "UPDATE 1"
        if "SET status = 'failed'" in sql:
            n = 0
            for r in self.rows:
                if r["status"] == "running":
                    r.update(status="failed", error=r["error"] or a[0])
                    n += 1
            return f"UPDATE {n}"
        if sql == jobruns._PRUNE_SQL:
            return "DELETE 0"
        raise AssertionError(f"unexpected execute: {sql[:60]}")


class BrokenPool:
    async def fetchval(self, *a):
        raise ConnectionRefusedError("db down")

    fetchrow = execute = fetchval


class HungPool:
    async def fetchval(self, *a):
        await asyncio.sleep(10)

    fetchrow = execute = fetchval


@pytest.fixture
def pool(monkeypatch):
    p = FakePool()
    monkeypatch.setattr(db, "_pool", p)
    monkeypatch.setattr(jobruns, "_last_prune", float("inf"))  # no background prune
    return p


# ── no pool / no outcome ──────────────────────────────────────────────────

async def test_no_open_pool_means_no_writes_and_no_errors(monkeypatch):
    monkeypatch.setattr(db, "_pool", None)
    async with jobruns.track("watch_check", "W") as job:
        job.ok("fine", routine=True)
    assert await jobruns.record("forecast", "F", summary="x") is None
    assert await jobruns.recover() == 0


async def test_a_block_without_an_outcome_writes_nothing(pool):
    async with jobruns.track("db_poll", "Database poller"):
        pass
    assert pool.rows == [] and pool.sql == []


# ── collapse ──────────────────────────────────────────────────────────────

async def test_routine_successes_collapse_into_one_row(pool):
    for v in (3, 4, 5):
        async with jobruns.track("watch_check", "Stale orders", ref=("watch", 7)) as job:
            job.ok(f"stale = {v}", {"value": v}, routine=True)
    assert len(pool.rows) == 1
    row = pool.rows[0]
    assert row["repeats"] == 3 and row["summary"] == "stale = 5" and row["counts"] == {"value": 5}
    assert row["ref_type"] == "watch" and row["ref_id"] == "7" and row["routine"] is True


async def test_sum_collapse_adds_counts(pool):
    for n in (2, 3):
        async with jobruns.track("db_poll", "Database poller") as job:
            job.ok(f"{n} new row(s)", {"rows": n, "Orders": n}, routine=True, collapse="sum")
    assert len(pool.rows) == 1
    assert pool.rows[0]["counts"] == {"rows": 5, "Orders": 5}


async def test_a_notable_outcome_breaks_the_run_of_routine_rows(pool):
    ref = ("watch", 1)
    for outcome in ("ok", "fired", "ok", "ok"):
        async with jobruns.track("watch_check", "W", ref=ref) as job:
            if outcome == "fired":
                job.ok("Fired (critical): x = 9")
            else:
                job.ok("x = 1", routine=True)
    assert [r["repeats"] for r in pool.rows] == [1, 1, 2]
    assert pool.rows[1]["summary"].startswith("Fired")


async def test_a_stale_row_is_not_collapsed_into(pool):
    async with jobruns.track("watch_check", "W", ref=("watch", 1)) as job:
        job.ok("x = 1", routine=True)
    pool.stale.add(1)
    async with jobruns.track("watch_check", "W", ref=("watch", 1)) as job:
        job.ok("x = 2", routine=True)
    assert len(pool.rows) == 2


async def test_repeated_failures_collapse_only_on_the_same_error(pool):
    for err in ("boom", "boom", "other"):
        async with jobruns.track("watch_check", "W", ref=("watch", 2)) as job:
            job.fail(err)
    assert [(r["error"], r["repeats"]) for r in pool.rows] == [("boom", 2), ("other", 1)]
    async with jobruns.track("watch_check", "W", ref=("watch", 2)) as job:
        job.ok("x = 1", routine=True)
    assert len(pool.rows) == 3   # a success after a failure is a new row


async def test_loop_errors_collapse_whatever_the_message(pool):
    await jobruns.loop_error("sentinel", ValueError("watch 1"))
    await jobruns.loop_error("sentinel", ValueError("watch 2"))
    await jobruns.loop_error("triage", ValueError("x"))
    assert [(r["name"], r["repeats"]) for r in pool.rows] == [("Watch sweep", 2), ("Triage loop", 1)]
    assert pool.rows[0]["error"] == "ValueError: watch 2" and pool.rows[0]["status"] == "failed"


# ── exceptions and failure modes ──────────────────────────────────────────

async def test_an_exception_from_the_work_is_recorded_and_re_raised(pool):
    with pytest.raises(KeyError):
        async with jobruns.track("dashboard_refresh", ref=("dashboard", 3)):
            raise KeyError("version_id")
    assert pool.rows[0]["status"] == "failed"
    assert pool.rows[0]["error"].startswith("KeyError")


@pytest.mark.parametrize("bad", [BrokenPool(), HungPool()])
async def test_the_recorder_never_raises_into_a_loop(monkeypatch, bad):
    monkeypatch.setattr(db, "_pool", bad)
    monkeypatch.setattr(jobruns, "WRITE_TIMEOUT", 0.05)
    async with jobruns.track("triage", "Triage", live=True) as job:
        await job.go_live("3 new message(s)")
        job.ok("3 triaged", routine=True)
    assert await jobruns.record("forecast", "F", status="failed", error="x") is None
    await jobruns.loop_error("sentinel", RuntimeError("x"))
    assert await jobruns.recover() == 0
    # The work's own exception still comes out unchanged.
    with pytest.raises(ZeroDivisionError):
        async with jobruns.track("watch_check", "W"):
            1 / 0


# ── live jobs ─────────────────────────────────────────────────────────────

async def test_a_live_job_has_a_running_row_then_finishes_done(pool):
    async with jobruns.track("consolidation", "Nightly consolidation", live=True,
                             trigger="nightly at 03:00") as job:
        assert pool.rows[0]["status"] == "running"
        assert job.id == 1
    row = pool.rows[0]
    assert row["status"] == "done" and row["trace_id"] == "job:1" and row["trigger"] == "nightly at 03:00"


async def test_live_jobs_route_model_calls_to_their_usage_trace(pool):
    from app import usage
    async with jobruns.track("watch_investigation", "W", live=True):
        assert usage.current().trace_id == "job:1"
    assert usage.current().trace_id != "job:1"


async def test_go_live_turns_a_quiet_job_into_a_running_row(pool):
    async with jobruns.track("triage", "Triage") as job:
        assert pool.rows == []
        await job.go_live("4 new message(s)")
        assert pool.rows[0]["status"] == "running" and pool.rows[0]["name"] == "4 new message(s)"
        job.fail("model declined")
    assert pool.rows[0]["status"] == "failed" and pool.rows[0]["error"] == "model declined"
    assert len(pool.rows) == 1


async def test_a_cancelled_live_job_is_marked_stopped(pool):
    started = asyncio.Event()

    async def work():
        async with jobruns.track("watch_investigation", "W", live=True):
            started.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(work())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.rows[0]["status"] == "failed" and pool.rows[0]["error"] == jobruns.STOPPED


async def test_recover_fails_rows_left_running(pool):
    await jobruns._start_row(jobruns.Job(kind="triage"))
    assert await jobruns.recover() == 1
    assert pool.rows[0]["status"] == "failed" and pool.rows[0]["error"] == jobruns.INTERRUPTED


# ── in flight, record, prune, helpers ─────────────────────────────────────

async def test_active_lists_jobs_in_flight_only(pool):
    async with jobruns.track("dashboard_refresh", trigger="refresh schedule"):
        now = jobruns.active()
        assert len(now) == 1 and now[0]["kind"] == "dashboard_refresh" and now[0]["id"] is None
    assert jobruns.active() == []


async def test_record_one_shot_links_the_queued_run(pool):
    rid = await jobruns.record("playbook_dispatch", "explore_data", summary="Queued agent run #9",
                               ref=("playbook", 3), task_run_id=9, trigger="daily at 02:00")
    assert rid == 1
    row = pool.rows[0]
    assert (row["status"], row["task_run_id"], row["ref_id"], row["routine"]) == ("done", 9, "3", False)


async def test_text_is_clipped(pool):
    await jobruns.record("forecast", "n" * 500, summary="s" * 2000, status="failed", error="e" * 2000)
    row = pool.rows[0]
    assert len(row["name"]) <= jobruns.NAME_CHARS
    assert len(row["summary"]) <= jobruns.SUMMARY_CHARS and len(row["error"]) <= jobruns.ERROR_CHARS


async def test_prune_runs_at_most_hourly_in_the_background(monkeypatch, pool):
    calls = []

    async def fake_prune():
        calls.append(1)
        return 0

    monkeypatch.setattr(jobruns, "prune", fake_prune)
    monkeypatch.setattr(jobruns, "_last_prune", None)
    await jobruns.record("forecast", "a")
    await jobruns.record("forecast", "b")
    await asyncio.sleep(0)
    assert calls == [1]


async def test_prune_deletes_in_batches(monkeypatch):
    statuses = iter(["DELETE 5000", "DELETE 12"])
    sent = []

    class P:
        async def execute(self, sql, *a):
            sent.append(a)
            return next(statuses)

    monkeypatch.setattr(db, "_pool", P())
    assert await jobruns.prune() == 5012
    assert sent[0] == (jobruns.ROUTINE_KEEP_DAYS, jobruns.KEEP_DAYS, jobruns.PRUNE_BATCH)


@pytest.mark.parametrize("seconds,text", [
    (30, "every 30 s"), (300, "every 5 min"), (900, "every 15 min"), (3600, "every hour"),
    (7200, "every 2 hours"), (86400, "every day"), (None, "on a schedule"), (0, "on a schedule"),
])
def test_every(seconds, text):
    assert jobruns.every(seconds) == text


def test_started_at_is_utc():
    assert jobruns.Job(kind="x").started_at.tzinfo == timezone.utc
    assert isinstance(jobruns.Job(kind="x").started_at, datetime)

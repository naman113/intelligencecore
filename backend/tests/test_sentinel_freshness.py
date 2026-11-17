"""Phase 17 plan 05 (CONS-02): sentinel freshness gate, stale episodes, as_of stamping."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import freshness, kpihistory, sentinel

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(hours=9)
TRIG = {"type": "threshold", "column": "n", "op": ">", "value": 5}


def _watch(**kw):
    w = {"id": 7, "thread_id": "t", "kind": "kpi", "title": "Orders", "condition": "c",
         "one_shot": False, "status": "active", "last_state": None,
         "consecutive_errors": 0, "stale_since": None,
         "compiled_sql": "SELECT 9 AS n", "trigger_rule": dict(TRIG)}
    w.update(kw)
    return w


class Job:
    def __init__(self):
        self.oks, self.fails = [], []

    def ok(self, text, counts=None, routine=False):
        self.oks.append((text, counts, routine))

    def fail(self, text):
        self.fails.append(text)


class Pool:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *a):
        self.calls.append((sql, a))

    async def fetchrow(self, sql, *a):
        return _watch()

    def events(self, kind):
        return [c for c in self.calls if "INSERT INTO watch_events" in c[0] and kind in c[0]]


def _fr(stale=False, failed=False, as_of=NOW, tables=()):
    return freshness.Freshness(as_of=as_of, stale=stale, stale_tables=tuple(tables),
                               failed=failed, last_sync={t: OLD for t in tables})


def _wire(monkeypatch, fr, rows=({"n": 9},)):
    pool, log = Pool(), {"order": [], "rec": [], "stale": [], "fired": [], "cases": []}

    async def get_pool():
        return pool

    async def for_sql(sql, **k):
        log["order"].append("fresh")
        if isinstance(fr, Exception):
            raise fr
        return fr

    async def run(sql):
        log["order"].append("query")
        return list(rows)

    async def rec(p, wid, trig, cur, as_of=None):
        log["rec"].append(as_of)
        return True

    async def rec_stale(p, wid, trig, as_of, stale_tables=()):
        log["stale"].append((as_of, tuple(stale_tables)))
        return True

    async def fire(*a, **k):
        log["fired"].append(a)

    async def after(*a, **k):
        log["cases"].append(a)

    async def facts():
        return None

    from app import findingcases
    monkeypatch.setattr(sentinel, "get_pool", get_pool)
    monkeypatch.setattr(freshness, "for_sql", for_sql)
    monkeypatch.setattr(sentinel, "run_readonly_sql", run)
    monkeypatch.setattr(kpihistory, "record", rec)
    monkeypatch.setattr(kpihistory, "record_stale", rec_stale)
    monkeypatch.setattr(sentinel, "_fire", fire)
    monkeypatch.setattr(sentinel, "_sweep_facts", facts)
    monkeypatch.setattr(findingcases, "after_check", after)
    return pool, log


async def test_fresh_check_runs_and_stamps_as_of(monkeypatch):
    pool, log = _wire(monkeypatch, _fr(as_of=OLD))
    job = Job()
    await sentinel._run_check(_watch(), job)
    assert log["order"] == ["fresh", "query"]
    assert log["rec"] == [OLD]
    assert any("data_as_of" in c[0] for c in pool.calls)
    assert not log["stale"]


async def test_live_only_as_of_is_check_time(monkeypatch):
    pool, log = _wire(monkeypatch, _fr(as_of=NOW))
    await sentinel._run_check(_watch(), Job())
    assert log["rec"] == [NOW]


async def test_stale_skips_query_and_trigger_and_marks(monkeypatch):
    pool, log = _wire(monkeypatch, _fr(stale=True, as_of=OLD, tables=("src_x.orders",)))
    job = Job()
    await sentinel._run_check(_watch(), job)
    assert "query" not in log["order"]
    assert log["rec"] == [] and log["fired"] == [] and log["cases"] == []
    assert log["stale"] == [(OLD, ("src_x.orders",))]
    assert len(pool.events("'stale_data'")) == 1
    assert any("stale_since = now()" in c[0] for c in pool.calls)
    assert not any("last_state" in c[0] for c in pool.calls)
    text, _c, routine = job.oks[0]
    assert text.startswith("Skipped: stale data") and routine is True


async def test_second_stale_check_same_episode_no_new_event_but_marker(monkeypatch):
    pool, log = _wire(monkeypatch, _fr(stale=True, as_of=OLD, tables=("t",)))
    await sentinel._run_check(_watch(stale_since=OLD), Job())
    assert pool.events("'stale_data'") == []
    assert len(log["stale"]) == 1


async def test_fresh_after_stale_logs_data_fresh_once_and_evaluates(monkeypatch):
    pool, log = _wire(monkeypatch, _fr(as_of=NOW))
    w = _watch(stale_since=OLD)
    await sentinel._run_check(w, Job())
    assert len(pool.events("'data_fresh'")) == 1
    assert any("stale_since = NULL" in c[0] for c in pool.calls)
    assert log["order"] == ["fresh", "query"] and log["rec"] == [NOW]
    assert log["fired"]  # 9 > 5 evaluates normally again


async def test_threshold_that_would_fire_does_not_fire_while_stale(monkeypatch):
    pool, log = _wire(monkeypatch, _fr(stale=True, as_of=OLD, tables=("t",)), rows=({"n": 999},))
    await sentinel._run_check(_watch(), Job())
    assert log["fired"] == []


async def test_fire_inserts_kind_fire(monkeypatch):
    import inspect
    assert "'fire'" in inspect.getsource(sentinel._fire)


@pytest.mark.parametrize("fr", [_fr(failed=True, stale=True), RuntimeError("boom")])
async def test_failed_lookup_counts_as_fresh(monkeypatch, fr):
    pool, log = _wire(monkeypatch, fr)
    await sentinel._run_check(_watch(), Job())
    assert log["order"][-1] == "query" and len(log["rec"]) == 1
    assert log["rec"][0] is not None and not log["stale"]


async def test_sweep_facts_loaded_once(monkeypatch):
    calls = []

    async def lf():
        calls.append(1)
        return "F"
    monkeypatch.setattr(freshness, "load_facts", lf)
    sentinel._begin_sweep()
    a = await sentinel._sweep_facts()
    b = await sentinel._sweep_facts()
    assert a == b == "F" and len(calls) == 1
    sentinel._begin_sweep()


# ── routes (Task 2) ──
async def test_watches_routes_expose_fields(monkeypatch):
    from app import db, main
    seen = []

    class RoutePool:
        async def fetch(self, sql, *a):
            seen.append(sql)
            if "FROM watches" in sql:
                return [{"id": 1, "measure_ref": json.dumps({"measure": "net_sales"}),
                         "stale_since": OLD, "data_as_of": OLD}]
            return [{"severity": "info", "summary": "s", "escalated": False,
                     "created_at": OLD, "kind": "stale_data"}]

    async def gp():
        return RoutePool()
    monkeypatch.setattr(db, "get_pool", gp)
    rows = await main.list_watches()
    assert rows[0]["measure_ref"] == {"measure": "net_sales"}
    assert "stale_since" in seen[0] and "data_as_of" in seen[0] and "measure_ref" in seen[0]
    ev = await main.watch_events(1)
    assert ev[0]["kind"] == "stale_data" and "kind FROM watch_events" in seen[1]

"""Phase 13 (GOLIVE-08 gap): a KPI watch records the measure the owner asked for
(trigger.value_column), while firing still uses trigger.column only."""

import json
from types import SimpleNamespace

import pytest

from app import kpihistory, sentinel

COMPOUND = {"sql": "SELECT 84 AS open_orders_count, 0 AS stale_open_order_count",
            "trigger": {"type": "threshold", "column": "stale_open_order_count",
                        "op": ">", "value": 0, "value_column": "open_orders_count"},
            "label": "Open orders"}


# ── pick_value ──

def test_pick_value_prefers_value_column():
    t = {"type": "threshold", "column": "stale_open_order_count", "op": ">",
         "value": 0, "value_column": "open_orders_count"}
    assert kpihistory.pick_value(
        t, {"open_orders_count": 84, "stale_open_order_count": 0}) == ("open_orders_count", 84.0)


def test_pick_value_non_numeric_value_column_stays_null():
    assert kpihistory.pick_value({"column": "n", "value_column": "label"},
                                 {"n": 5, "label": "abc"}) == ("label", None)


def test_pick_value_missing_value_column_falls_back():
    assert kpihistory.pick_value({"column": "n", "value_column": "gone"}, {"n": 5}) == ("n", 5.0)


@pytest.mark.parametrize("vc", [None, 5, ""])
def test_pick_value_ignores_bad_value_column(vc):
    assert kpihistory.pick_value({"column": "n", "value_column": vc}, {"n": 5}) == ("n", 5.0)


# ── harness (copied from test_sentinel_history) ──

def _watch(**kw):
    w = {"id": 7, "thread_id": "t", "kind": "kpi", "title": "Open orders",
         "condition": "record the open count; alert when stale > 0", "one_shot": False,
         "status": "active", "last_state": None, "consecutive_errors": 0,
         "compiled_sql": None, "trigger_rule": None}
    w.update(kw)
    return w


class Pool:
    def __init__(self, watch=None):
        self.calls = []
        self.watch = watch or _watch()

    async def execute(self, sql, *args):
        self.calls.append((sql, args))

    async def fetchrow(self, sql, *args):
        return self.watch

    def inserts(self):
        return [c for c in self.calls if "INSERT INTO metric_history" in c[0]]


def _wire(monkeypatch, pool, rows):
    fired = []

    async def get_pool():
        return pool

    async def run(sql):
        return rows

    async def fire(*a, **k):
        fired.append(a)

    monkeypatch.setattr(sentinel, "get_pool", get_pool)
    monkeypatch.setattr(sentinel, "run_readonly_sql", run)
    monkeypatch.setattr(sentinel, "_fire", fire)
    return fired


class FakeAgent:
    def __init__(self, spec):
        self.spec = spec
        self.task = None

    async def ainvoke(self, payload, config=None):
        self.task = payload["messages"][0][1]
        return {"messages": [SimpleNamespace(content=json.dumps(self.spec))]}


def _agent(monkeypatch, spec):
    a = FakeAgent(spec)
    monkeypatch.setattr(sentinel, "_compiler", lambda: a)
    return a


def _spec(**trig):
    s = json.loads(json.dumps(COMPOUND))
    s["trigger"].update(trig)
    return s


ROW = [{"open_orders_count": 84, "stale_open_order_count": 0}]


# ── prompt + compile ──

def test_compile_prompt_mentions_value_column():
    p = sentinel.COMPILE_PROMPT
    assert p.startswith("You compile a monitoring condition")
    assert "value_column" in p and "KIND: kpi" in p
    assert '{"type":"increase","column":"<col>"}' in p


async def test_compile_task_carries_kind(monkeypatch):
    a = _agent(monkeypatch, COMPOUND)
    _wire(monkeypatch, Pool(), ROW)
    await sentinel._compile_watch(_watch())
    assert "CONDITION: " in a.task and "KIND: kpi" in a.task
    await sentinel._compile_watch({"id": 1, "condition": "c"})
    assert "KIND: user" in a.task


async def test_compile_keeps_valid_value_column(monkeypatch):
    _agent(monkeypatch, COMPOUND)
    _wire(monkeypatch, Pool(), ROW)
    spec = await sentinel._compile_watch(_watch())
    assert spec["trigger"]["value_column"] == "open_orders_count"


@pytest.mark.parametrize("bad", ["no_such_col", 7, ""])
async def test_compile_drops_bad_value_column(monkeypatch, bad):
    _agent(monkeypatch, _spec(value_column=bad))
    _wire(monkeypatch, Pool(), ROW)
    spec = await sentinel._compile_watch(_watch())
    assert "value_column" not in spec["trigger"]


# ── _check_one ──

async def test_compound_kpi_condition_records_open_count(monkeypatch):
    pool = Pool()
    _agent(monkeypatch, COMPOUND)
    fired = _wire(monkeypatch, pool, ROW)
    await sentinel._check_one(_watch())
    upd = [c for c in pool.calls if "SET compiled_sql" in c[0]]
    assert upd and any('"value_column": "open_orders_count"' in str(a) for a in upd[0][1])
    assert pool.inserts()[0][1][:3] == (7, "open_orders_count", 84.0)
    assert not fired


@pytest.mark.parametrize("stale,fires", [(3, 1), (0, 0)])
async def test_cached_compound_fires_on_alert_column_records_value(monkeypatch, stale, fires):
    pool = Pool()
    row = [{"open_orders_count": 84, "stale_open_order_count": stale}]
    fired = _wire(monkeypatch, pool, row)
    w = _watch(compiled_sql=COMPOUND["sql"], trigger_rule=COMPOUND["trigger"],
               last_state={"stale_open_order_count": 0, "open_orders_count": 84})
    await sentinel._check_one(w)
    assert len(fired) == fires
    assert pool.inserts()[0][1][:3] == (7, "open_orders_count", 84.0)

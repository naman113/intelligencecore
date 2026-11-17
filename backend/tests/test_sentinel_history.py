"""KPI-01 through the real sentinel._check_one: exactly one metric_history row per
successful check, none on failed compile/query/empty result, and a broken history
insert never stops last_state persistence or the fire path."""

import pytest

from app import sentinel

TRIG = {"type": "change", "column": "south_orders_30d"}


def _watch(**kw):
    w = {"id": 7, "thread_id": "t", "kind": "kpi", "title": "South orders",
         "condition": "south orders", "one_shot": False, "status": "active",
         "last_state": None, "consecutive_errors": 0,
         "compiled_sql": "SELECT 73 AS south_orders_30d", "trigger_rule": dict(TRIG)}
    w.update(kw)
    return w


class Pool:
    def __init__(self, insert_raises=False):
        self.calls = []
        self.insert_raises = insert_raises

    async def execute(self, sql, *args):
        if "INSERT INTO metric_history" in sql and self.insert_raises:
            raise RuntimeError("insert failed")
        self.calls.append((sql, args))

    async def fetchrow(self, sql, *args):
        return _watch()

    def inserts(self):
        return [c for c in self.calls if "INSERT INTO metric_history" in c[0]]

    def index(self, needle):
        return next(i for i, c in enumerate(self.calls) if needle in c[0])


def _wire(monkeypatch, pool, rows=None, raises=None):
    fired = []

    async def get_pool():
        return pool

    async def run(sql):
        if raises:
            raise raises
        return rows

    async def fire(*a, **k):
        fired.append(a)

    monkeypatch.setattr(sentinel, "get_pool", get_pool)
    monkeypatch.setattr(sentinel, "run_readonly_sql", run)
    monkeypatch.setattr(sentinel, "_fire", fire)
    return fired


async def test_success_writes_exactly_one_row(monkeypatch):
    pool = Pool()
    _wire(monkeypatch, pool, rows=[{"south_orders_30d": 73}])
    await sentinel._check_one(_watch())
    assert len(pool.inserts()) == 1
    assert pool.inserts()[0][1][:3] == (7, "south_orders_30d", 73.0)


async def test_history_insert_follows_last_state_update(monkeypatch):
    pool = Pool()
    _wire(monkeypatch, pool, rows=[{"south_orders_30d": 73}])
    await sentinel._check_one(_watch())
    assert pool.index("INSERT INTO metric_history") > pool.index("SET last_state")


async def test_sql_failure_writes_none(monkeypatch):
    pool = Pool()
    _wire(monkeypatch, pool, raises=RuntimeError("relation does not exist"))
    await sentinel._check_one(_watch())
    assert pool.inserts() == []
    assert any("SET compiled_sql = NULL" in c[0] for c in pool.calls)


async def test_compile_failure_writes_none(monkeypatch):
    pool = Pool()
    _wire(monkeypatch, pool, rows=[{"south_orders_30d": 1}])

    async def comp(w):
        return None
    monkeypatch.setattr(sentinel, "_compile_watch", comp)
    await sentinel._check_one(_watch(compiled_sql=None))
    assert pool.inserts() == []


async def test_compile_then_execute_writes_exactly_one(monkeypatch):
    pool = Pool()
    _wire(monkeypatch, pool, rows=[{"south_orders_30d": 73}])

    async def comp(w):
        return {"sql": "SELECT 73 AS south_orders_30d", "trigger": dict(TRIG),
                "label": "South orders"}
    monkeypatch.setattr(sentinel, "_compile_watch", comp)
    await sentinel._check_one(_watch(compiled_sql=None, trigger_rule=None))
    assert len(pool.inserts()) == 1


async def test_empty_result_writes_none(monkeypatch):
    pool = Pool()
    _wire(monkeypatch, pool, rows=[])
    await sentinel._check_one(_watch())
    assert pool.inserts() == []
    assert any("SET last_state" in c[0] for c in pool.calls)


async def test_insert_failure_still_fires(monkeypatch):
    pool = Pool(insert_raises=True)
    fired = _wire(monkeypatch, pool, rows=[{"south_orders_30d": 73}])
    trig = {"type": "threshold", "column": "south_orders_30d", "op": ">", "value": 10}
    await sentinel._check_one(_watch(trigger_rule=trig))
    assert len(fired) == 1

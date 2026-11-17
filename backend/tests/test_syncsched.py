"""Phase 15 (SRC-05): the scheduler sweep only enqueues and reaps (recording fake pool)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app import sourcesvc, syncsched, warehouse, whsources

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
IMP_DONE = {"mode": "import", "status": "active", "backfill_done": True}


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class Conn:
    def __init__(self, pool):
        self.pool = pool

    def transaction(self):
        return _Tx()

    async def execute(self, sql, *a):
        self.pool.log.append((sql, a))
        return "UPDATE 1"

    async def fetchval(self, sql, *a):
        self.pool.log.append((sql, a))
        if "INSERT INTO sync_runs" in sql:
            return None if self.pool.inflight else 11
        if "INSERT INTO task_runs" in sql:
            return 22


class _Acq:
    def __init__(self, c):
        self.c = c

    async def __aenter__(self):
        return self.c

    async def __aexit__(self, *a):
        return False


class Pool:
    def __init__(self, sources=(), tables=(), inflight=False, schema_names=()):
        self.sources, self.tables, self.inflight = list(sources), list(tables), inflight
        self.schema_names = list(schema_names)
        self.log = []
        self.conn = Conn(self)

    def acquire(self):
        return _Acq(self.conn)

    async def fetch(self, sql, *a):
        self.log.append((sql, a))
        if "warehouse_schema IS NOT NULL" in sql:
            return [{"name": n} for n in self.schema_names]
        if "FROM sources s" in sql:
            return self.sources
        if "FROM source_tables" in sql:
            return self.tables
        return []

    async def execute(self, sql, *a):
        self.log.append((sql, a))
        return "UPDATE 2"

    def sqls(self, needle):
        return [(s, a) for s, a in self.log if needle in s]


def _src(**kw):
    d = {"id": 1, "name": "shop", "kind": "mysql", "schedule": {"interval_minutes": 60, "reconcile_hour": 2},
         "last_reconcile_on": date(2026, 5, 1), "next_sync_at": None}
    d.update(kw)
    return d


@pytest.fixture
def wire(monkeypatch):
    def go(pool):
        async def gp():
            return pool
        monkeypatch.setattr(syncsched, "get_pool", gp)
        return pool
    monkeypatch.setattr(sourcesvc, "_decode", lambda r: dict(r))

    async def _no_alerts(*a, **k):  # Phase 15 (15-13): alerts have their own tests
        return {}
    from app import sourcealerts
    monkeypatch.setattr(sourcealerts, "evaluate", _no_alerts)

    async def _no_cutover(*a, **k):  # Phase 17 (CONS-05): cutover has its own tests
        return {"switched": [], "failed": [], "busy": []}
    from app import modeflip
    monkeypatch.setattr(modeflip, "advance", _no_cutover)
    syncsched._sweep_n = 0
    syncsched._reprovision.clear()
    return go


async def test_enqueue_inserts_run_task_and_advances(wire):
    pool = wire(Pool([_src()], [IMP_DONE]))
    assert await syncsched.enqueue_due(NOW) == [11]
    ins = pool.sqls("INSERT INTO sync_runs")[0]
    assert "ON CONFLICT (source_id) WHERE status IN ('queued','running') DO NOTHING" in ins[0]
    assert ins[1] == (1, "sync")
    assert "'sync', NULL, 'sync'" in pool.sqls("INSERT INTO task_runs")[0][0]
    assert pool.sqls("INSERT INTO threads")
    assert pool.sqls("SET task_run_id")[0][1] == (11, 22)
    assert pool.sqls("SET next_sync_at")[0][1][1] == NOW + timedelta(minutes=60)


async def test_in_flight_skips_without_advancing(wire):
    pool = wire(Pool([_src()], [IMP_DONE], inflight=True))
    assert await syncsched.enqueue_due(NOW) == []
    assert not pool.sqls("INSERT INTO task_runs") and not pool.sqls("SET next_sync_at")


@pytest.mark.parametrize("backfill_done,last,hour,kind", [
    (False, date(2026, 5, 1), 12, "backfill"),
    (True, date(2026, 4, 30), 12, "reconcile"),
    (True, date(2026, 4, 30), 1, "sync"),
    (True, date(2026, 5, 1), 12, "sync")])
async def test_run_kind(wire, backfill_done, last, hour, kind):
    t = [{"mode": "import", "status": "active", "backfill_done": backfill_done}]
    pool = wire(Pool([_src(last_reconcile_on=last)], t))
    await syncsched.enqueue_due(NOW.replace(hour=hour))
    assert pool.sqls("INSERT INTO sync_runs")[0][1][1] == kind


def test_due_query_excludes_unsyncable_sources():
    q = syncsched._DUE_SQL
    assert "s.status = 'active'" in q and "s.kind <> 'csv'" in q and "business_db" not in q  # Phase 17 (CONS-05)
    assert "next_sync_at IS NULL" in q and "t.mode = 'import'" in q
    assert "t.pending_mode = 'import'" in q


async def test_enqueue_now_returns_none_when_in_flight(wire):
    wire(Pool(inflight=True))
    assert await syncsched.enqueue_now(1, "sync") is None
    wire(Pool())
    assert await syncsched.enqueue_now(1, "sync") == 11


async def test_reaper_sql_and_count(wire):
    pool = wire(Pool())
    assert await syncsched.reap_stuck_runs() == 2
    sql = pool.sqls("sync run ended without a result")[0][0]
    assert "task_run_id IN (SELECT id FROM task_runs WHERE status IN ('done','failed'))" in sql


async def test_sweep_reaps_first_even_when_warehouse_off(wire, monkeypatch):
    pool = wire(Pool())
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: False)
    calls = []

    async def biz():
        calls.append("biz")
        return {}
    monkeypatch.setattr(sourcesvc, "sync_business_db_tables", biz)
    out = await syncsched.sweep()
    assert "sync run ended without a result" in pool.log[0][0]
    assert out == {"reaped": 2} and calls == []  # v1.0 path: no business_db sync
    assert not pool.sqls("FROM sources s")  # nothing enqueued with warehouse mode off


async def test_business_db_not_synced_without_warehouse(wire, monkeypatch):
    wire(Pool())
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: False)
    n = []

    async def biz():
        n.append(1)
        return {}
    monkeypatch.setattr(sourcesvc, "sync_business_db_tables", biz)
    for _ in range(12):
        await syncsched.sweep()
    assert n == []  # warehouse off: the business DB is never queried (WR-15)


async def test_lost_schema_reprovisioned_and_reset(wire, monkeypatch):
    pool = wire(Pool(schema_names=["shop"]))
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: True)
    prov = []

    async def mo(names):
        return ["shop"]

    async def pv(name):
        prov.append(name)
        return {"status": "ok"}
    monkeypatch.setattr(whsources, "missing_objects", mo)
    monkeypatch.setattr(whsources, "provision", pv)
    syncsched._sweep_n = 9
    out = await syncsched.sweep()
    assert prov == ["shop"] and out["reprovisioned"] == ["shop"]
    assert pool.sqls("backfill_done=false, backfill_progress=0")
    assert pool.sqls("SET next_sync_at=now()")


async def test_request_reprovision_forces_next_sweep(wire, monkeypatch):
    pool = wire(Pool(schema_names=["shop"]))
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: True)
    prov = []

    async def pv(name):
        prov.append(name)
        return {"status": "ok"}
    monkeypatch.setattr(whsources, "provision", pv)
    syncsched.request_reprovision("shop")
    await syncsched.sweep()
    assert prov == ["shop"] and not pool.sqls("backfill_done=false")


# Phase 17 (CONS-05): the cutover step of the sweep
async def test_sweep_runs_mode_cutover_in_warehouse_mode_only(wire, monkeypatch):
    from app import modeflip
    wire(Pool())
    seen = []

    async def adv():
        seen.append(1)
        return {"switched": ["public.orders"], "failed": [], "busy": []}
    monkeypatch.setattr(modeflip, "advance", adv)
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: False)
    out = await syncsched.sweep()
    assert seen == [] and "mode_switches" not in out
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: True)
    out = await syncsched.sweep()
    assert seen == [1] and out["mode_switches"]["switched"] == ["public.orders"]


async def test_sweep_survives_cutover_error_but_not_blips(wire, monkeypatch):
    from app import modeflip
    wire(Pool())
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: True)

    async def bad():
        raise RuntimeError("x")
    monkeypatch.setattr(modeflip, "advance", bad)
    out = await syncsched.sweep()
    assert "mode_switches" not in out

    async def blip():
        raise ConnectionError("down")
    monkeypatch.setattr(modeflip, "advance", blip)
    with pytest.raises(ConnectionError):
        await syncsched.sweep()

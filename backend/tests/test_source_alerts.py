"""Phase 15 (SRC-10): source freshness, failure and unreachable alerts."""
import asyncio
import datetime

import pytest

from app import guardrails, healthwatch, sourcealerts, sourcesvc, syncsched, warehouse

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def imp(done=True, last=NOW - datetime.timedelta(minutes=10), status="active"):
    return {"mode": "import", "status": status, "backfill_done": done, "last_success_at": last,
            "warehouse_table": "orders"}


def live(name="customers"):
    return {"mode": "live", "status": "active", "backfill_done": True, "last_success_at": None,
            "warehouse_table": name}


def src(tables=None, fm=60, status="active", name="erp", **kw):
    return dict({"id": 2, "name": name, "kind": "mysql", "status": status, "freshness_minutes": fm,
                 "tables": tables if tables is not None else [imp()]}, **kw)


def runs(*st):
    return [{"status": s} for s in st]


def test_decide_fresh_and_stale():
    assert not sourcealerts.decide(src(), [], NOW)["stale"]
    old = src([imp(last=NOW - datetime.timedelta(minutes=90))])
    d = sourcealerts.decide(old, [], NOW)
    assert d["stale"] and d["minutes"] == 90


def test_decide_first_backfill_never_stale():
    s = src([imp(done=False, last=NOW - datetime.timedelta(days=3))])
    assert not sourcealerts.decide(s, [], NOW)["stale"]
    assert not sourcealerts.decide(src([imp(last=None)]), [], NOW)["stale"]


def test_decide_no_freshness_target():
    s = src([imp(last=NOW - datetime.timedelta(days=3))], fm=None)
    assert not sourcealerts.decide(s, [], NOW)["stale"]


def test_decide_failing_needs_two_failed():
    assert sourcealerts.decide(src(), runs("failed", "failed"), NOW)["failing"]
    assert not sourcealerts.decide(src(), runs("failed", "ok"), NOW)["failing"]
    assert not sourcealerts.decide(src(), runs("failed"), NOW)["failing"]
    assert not sourcealerts.decide(src(), runs("failed", "partial"), NOW)["failing"]


def test_decide_exclusions():
    bad = runs("failed", "failed")
    # Phase 17 (CONS-05): all-live business_db stays silent; with import tables it alerts like any source
    assert sourcealerts.decide(src([live()], name="business_db"), bad, NOW) == {
        "stale": False, "failing": False, "minutes": None}
    assert sourcealerts.decide(src(name="business_db"), bad, NOW)["failing"]
    old = src([imp(last=NOW - datetime.timedelta(minutes=90))], name="business_db")
    assert sourcealerts.decide(old, [], NOW)["stale"]
    assert not sourcealerts.decide(src([live()]), bad, NOW)["failing"]
    assert not sourcealerts.decide(src(status="paused"), bad, NOW)["failing"]


def test_titles():
    t, r = healthwatch.title, healthwatch.recovered
    assert t("source_stale:x") == "A data source is out of date"
    assert r("source_stale:x") == "A data source is up to date again"
    assert t("source_failing:x") == "A data source failed two syncs in a row"
    assert r("source_failing:x") == "A data source is syncing again"
    assert t("source_unreachable:x") == "A data source cannot be reached"
    assert r("source_unreachable:x") == "A data source is reachable again"
    assert t("worker") == "The task worker has stopped making progress"


class Pool:
    def __init__(self, runs=None, ok=None, open_rows=()):
        self.runs, self.ok = runs or [], ok or []
        self.open = set(open_rows)
        self.alerts = {}
        self._id = 0

    async def fetch(self, sql, *a):
        if "row_number" in sql:
            return self.runs
        if "max(ended_at)" in sql:
            return self.ok
        return [{"component": c} for c in self.open]

    async def fetchval(self, sql, *a):
        comp = a[0]
        if sql.startswith("INSERT"):
            if comp in self.open:
                return None
            self.open.add(comp)
            self.alerts[comp] = a[1]
            self._id += 1
            return self._id
        if comp in self.open:
            self.open.discard(comp)
            self._id += 1
            return self._id
        return None


@pytest.fixture
def wire(monkeypatch):
    sent = []

    async def notify(component, aid, failing, detail=""):
        sent.append((component, failing, detail))

    monkeypatch.setattr(healthwatch, "_notify", notify)
    sourcealerts._last_probe.clear()

    def go(pool, sources, probe=None):
        async def gp():
            return pool

        async def ls():
            return sources

        monkeypatch.setattr(sourcealerts, "get_pool", gp)
        monkeypatch.setattr(sourcesvc, "list_sources", ls)
        if probe is not None:
            async def pl(s):
                return probe.pop(0) if probe else None
            monkeypatch.setattr(sourcealerts, "probe_live", pl)
        return sent
    return go


async def test_stale_once_per_change(wire):
    stale = src([imp(last=NOW - datetime.timedelta(minutes=90))])
    fresh = src([imp()])
    pool = Pool(ok=[{"source_id": 2, "t": NOW - datetime.timedelta(minutes=90)}])
    sent = wire(pool, [stale])
    await sourcealerts.evaluate(NOW)
    await sourcealerts.evaluate(NOW)
    assert sent == [("source_stale:erp", True,
                     "Source erp: last successful sync 90 minutes ago (target 60)")]
    wire(pool, [fresh])
    pool.ok = [{"source_id": 2, "t": NOW}]
    await sourcealerts.evaluate(NOW)
    await sourcealerts.evaluate(NOW)
    assert sent[1:] == [("source_stale:erp", False, "")]


async def test_failing_once_per_change(wire):
    pool = Pool(runs=[{"source_id": 2, "status": "failed"}, {"source_id": 2, "status": "failed"}],
                ok=[{"source_id": 2, "t": NOW}])
    sent = wire(pool, [src()])
    await sourcealerts.evaluate(NOW)
    await sourcealerts.evaluate(NOW)
    assert sent == [("source_failing:erp", True, "Source erp: the last two syncs failed")]
    pool.runs = [{"source_id": 2, "status": "ok"}, {"source_id": 2, "status": "failed"}]
    await sourcealerts.evaluate(NOW)
    assert sent[1:] == [("source_failing:erp", False, "")]


async def test_paused_or_removed_resolves(wire):
    pool = Pool(open_rows=["source_failing:erp", "source_stale:gone"])
    sent = wire(pool, [src(status="paused")])
    await sourcealerts.evaluate(NOW)
    assert {s[0] for s in sent} == {"source_failing:erp", "source_stale:gone"}
    assert all(s[1] is False for s in sent)


async def test_probe_live_outcomes(monkeypatch):
    seen = []

    def fake(exc=None):
        async def run(sql):
            seen.append(sql)
            if exc:
                raise exc
            return [{"?column?": 1}]
        monkeypatch.setattr(guardrails, "run_readonly_sql", run)

    s = src([live("zeta"), live("alpha")], name="crm")
    fake()
    assert await sourcealerts.probe_live(s) is False
    assert seen == ['SELECT 1 FROM "src_crm"."alpha" LIMIT 1']
    fake(guardrails.SourceUnreachable())
    assert await sourcealerts.probe_live(s) is True
    fake(asyncio.TimeoutError())
    assert await sourcealerts.probe_live(s) is None  # Quick 028: slow is unknown
    fake(guardrails.WarehouseUnreachable())
    assert await sourcealerts.probe_live(s) is None
    fake(RuntimeError("password=x"))
    assert await sourcealerts.probe_live(s) is None
    assert await sourcealerts.probe_live(src([imp()])) is None


async def test_unreachable_open_resolve_and_interval(wire):
    pool = Pool()
    s = src([live()], name="crm")
    sent = wire(pool, [s], probe=[True, True, False, None])
    clock = {"t": 1000.0}

    def c():
        return clock["t"]

    await sourcealerts.evaluate(NOW, clock=c)
    assert sent == [("source_unreachable:crm", True, "Source crm: its live tables could not be read")]
    clock["t"] += 10
    await sourcealerts.evaluate(NOW, clock=c)  # inside the interval: no probe consumed
    clock["t"] += 301
    await sourcealerts.evaluate(NOW, clock=c)  # True again: nothing new
    assert len(sent) == 1
    clock["t"] += 301
    await sourcealerts.evaluate(NOW, clock=c)  # False: resolve once
    assert sent[1:] == [("source_unreachable:crm", False, "")]
    clock["t"] += 301
    await sourcealerts.evaluate(NOW, clock=c)  # None: unchanged
    assert len(sent) == 2


async def test_no_probe_for_import_csv_business(wire, monkeypatch):
    calls = []
    pool = Pool()
    sent = wire(pool, [src([imp()]), src([live()], kind="csv", name="c"), src(name="business_db")])

    async def pl(s):
        calls.append(s)
    monkeypatch.setattr(sourcealerts, "probe_live", pl)
    await sourcealerts.evaluate(NOW)
    assert calls == [] and sent == []


async def test_sweep_survives_raising_evaluate(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("x")

    async def noop(*a, **k):
        return 0

    async def lst(*a, **k):
        return []
    monkeypatch.setattr(sourcealerts, "evaluate", boom)
    monkeypatch.setattr(syncsched, "reap_stuck_runs", noop)
    monkeypatch.setattr(syncsched, "enqueue_due", lst)
    monkeypatch.setattr(syncsched, "_reprovision_lost", lst)
    monkeypatch.setattr(sourcesvc, "sync_business_db_tables", noop)
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: True)
    from app import modeflip  # Phase 17 (CONS-05): the cutover step has its own tests
    monkeypatch.setattr(modeflip, "advance", noop)
    out = await syncsched.sweep()
    assert "alerts" not in out and out["enqueued"] == []


async def test_business_db_alerts_but_never_probed(wire, monkeypatch):  # Phase 17 (CONS-05)
    calls = []

    async def pl(s):
        calls.append(s)
    monkeypatch.setattr(sourcealerts, "probe_live", pl)
    old = imp(last=NOW - datetime.timedelta(minutes=90))
    sent = wire(Pool(), [src([old, live()], name="business_db")])
    out = await sourcealerts.evaluate(NOW)
    assert calls == [] and out["stale"] == ["business_db"]
    assert sent and sent[0][0] == "source_stale:business_db"

"""Phase 11 (GOLIVE-05): healthwatch evaluate, sweep and notify."""
import json

import pytest

from app import configservice, healthwatch, heartbeat, people, whatsapp

DB_OK = {"configured": True, "failures": 0, "error": ""}


def snap(deadline=100.0, alive=True, seen=True):
    return {"deadline": deadline, "alive": alive, "seen": seen}


def test_evaluate_loop_states():
    ev = healthwatch.evaluate
    m = ("worker",)
    assert ev({"worker": snap(100)}, 120, 0, DB_OK, m)["worker"]["state"] == "ok"
    r = ev({"worker": snap(100)}, 200, 0, DB_OK, m)["worker"]
    assert r["state"] == "failing" and r["detail"].startswith("no progress for")
    r = ev({"worker": snap(100, alive=False)}, 101, 0, DB_OK, m)["worker"]
    assert r == {"state": "failing", "detail": "loop task is not running"}
    assert ev({}, 100, 0, DB_OK, m)["worker"]["state"] == "starting"
    r = ev({}, 500, 0, DB_OK, m)["worker"]
    assert r == {"state": "failing", "detail": "never started"}


def test_evaluate_business_db():
    ev = healthwatch.evaluate
    off = ev({}, 0, 0, {"configured": False}, ())["business_db"]
    assert off["state"] == "ok"
    one = ev({}, 0, 0, {"configured": True, "failures": 1, "error": "X"}, ())
    assert one["business_db"]["state"] == "ok"
    two = ev({}, 0, 0, {"configured": True, "failures": 2, "error": "OSError"}, ())
    assert two["business_db"] == {"state": "failing", "detail": "OSError"}


class FakePool:
    def __init__(self):
        self.open_returns = [1]
        self.resolve_returns = [9]
        self.calls = []

    async def fetchval(self, sql, *a):
        self.calls.append((sql, a))
        if "ON CONFLICT (component) WHERE resolved_at IS NULL DO NOTHING" in sql:
            return self.open_returns.pop(0) if self.open_returns else None
        if sql.startswith("UPDATE health_alerts SET resolved_at"):
            return self.resolve_returns.pop(0) if self.resolve_returns and a[0] == "worker" else None
        if "INSERT INTO actions" in sql:
            return 77
        return None

    async def execute(self, sql, *a):
        self.calls.append((sql, a))


@pytest.fixture
def env(monkeypatch):
    heartbeat.reset()
    pool = FakePool()
    notes = []

    async def gp():
        return pool

    async def probe():
        pass

    async def notify(comp, aid, failing, detail=""):
        notes.append((comp, aid, failing))

    monkeypatch.setattr(healthwatch, "get_pool", gp)
    monkeypatch.setattr(healthwatch, "_probe_business_db", probe)
    monkeypatch.setattr(healthwatch, "_notify", notify)
    monkeypatch.setattr(healthwatch, "_db_configured", lambda: False)
    monkeypatch.setattr(healthwatch, "_monitored", ("worker",))
    monkeypatch.setattr(healthwatch, "_started_at", -10_000.0)
    yield pool, notes
    heartbeat.reset()


async def test_sweep_opens_once_and_resolves(env):
    pool, notes = env
    await healthwatch.sweep()  # worker never started -> failing
    assert notes == [("worker", 1, True)]
    await healthwatch.sweep()  # insert conflicts -> None -> no notify
    assert notes == [("worker", 1, True)]
    heartbeat.beat("worker", 30)
    await healthwatch.sweep()  # ok -> resolve -> recovery notify
    assert notes[-1] == ("worker", 9, False) and len(notes) == 2
    await healthwatch.sweep()  # nothing open -> no more notify
    assert len(notes) == 2


async def test_probe_hysteresis_and_class_name_only(monkeypatch):
    from app import guardrails
    monkeypatch.setattr(healthwatch, "_db_configured", lambda: True)
    monkeypatch.setattr(healthwatch, "_db_failures", 0)

    async def boom(sql):
        raise OSError("postgres://user:secret@host/db refused")

    monkeypatch.setattr(guardrails, "run_readonly_sql", boom)
    await healthwatch._probe_business_db()
    assert healthwatch._db_failures == 1 and healthwatch._db_error == "OSError"
    assert "secret" not in healthwatch._db_error

    async def ok(sql):
        return [{"?column?": 1}]

    monkeypatch.setattr(guardrails, "run_readonly_sql", ok)
    await healthwatch._probe_business_db()
    assert healthwatch._db_failures == 0


@pytest.fixture
def npool(monkeypatch):
    pool = FakePool()

    async def gp():
        return pool

    monkeypatch.setattr(healthwatch, "get_pool", gp)
    return pool


async def test_notify_not_configured_sends_nothing(monkeypatch, npool):
    sent = []
    monkeypatch.setattr(configservice, "_group_configured", lambda g: False)

    async def send(p, m):
        sent.append(m)
        return True

    monkeypatch.setattr(whatsapp, "send_message", send)
    await healthwatch._notify("worker", 1, True, "x")
    assert sent == [] and npool.calls == []


@pytest.mark.parametrize("result", [True, False])
async def test_notify_sends_once_and_records(monkeypatch, npool, result):
    sent = []
    monkeypatch.setattr(configservice, "_group_configured", lambda g: True)

    async def pp():
        return {"whatsapp": "15550001"}

    async def send(p, m):
        sent.append((p, m))
        return result

    monkeypatch.setattr(people, "primary_person", pp)
    monkeypatch.setattr(whatsapp, "send_message", send)
    await healthwatch._notify("worker", 5, True, "no progress for 90 s")
    assert len(sent) == 1 and sent[0][0] == "15550001"
    ins = [c for c in npool.calls if "INSERT INTO actions" in c[0]]
    assert len(ins) == 1 and "'notify'" in ins[0][0]
    payload = json.loads(ins[0][1][0])
    assert payload["health_alert_id"] == 5 and payload["component"] == "worker"
    assert payload["phone"] == "15550001" and payload["body"]
    assert ins[0][1][1] is result
    upd = [c for c in npool.calls if "notify_action_id" in c[0]]
    assert upd and upd[0][1] == (5, 77)


async def test_notify_raising_send_is_caught(monkeypatch, npool):
    monkeypatch.setattr(configservice, "_group_configured", lambda g: True)

    async def pp():
        return {"whatsapp": "1"}

    async def send(p, m):
        raise RuntimeError("down")

    monkeypatch.setattr(people, "primary_person", pp)
    monkeypatch.setattr(whatsapp, "send_message", send)
    await healthwatch._notify("worker", 5, False)
    ins = [c for c in npool.calls if "INSERT INTO actions" in c[0]]
    assert len(ins) == 1 and ins[0][1][1] is False
    assert any("resolve_action_id" in c[0] for c in npool.calls)


def test_titles_cover_components():
    for c in healthwatch.COMPONENTS:
        assert c in healthwatch.TITLES and c in healthwatch.RECOVERED


# ── Phase 14 (WH-08): warehouse component, FDW business_db, unknown state ──
from app import guardrails, warehouse  # noqa: E402
from app.config import get_settings  # noqa: E402

WH_OFF = {"configured": False}


def whs(failures=0, error=""):
    return {"configured": True, "failures": failures, "error": error}


def test_wh_constants():
    assert "warehouse" in healthwatch.COMPONENTS
    assert healthwatch.TITLES["warehouse"] == "The warehouse is unreachable"
    assert healthwatch.RECOVERED["warehouse"] == "The warehouse is reachable again"


def test_wh_evaluate_default_has_no_warehouse_key():
    out = healthwatch.evaluate({}, 0, 0, DB_OK, ())
    assert "warehouse" not in out and out["business_db"]["state"] == "ok"


def test_wh_evaluate_not_configured():
    ev = healthwatch.evaluate
    out = ev({}, 0, 0, {"configured": True, "failures": 2, "error": "OSError"}, (), wh_state=WH_OFF)
    assert out["warehouse"] == {"state": "ok", "detail": "not configured"}
    assert out["business_db"] == {"state": "failing", "detail": "OSError"}


def test_wh_evaluate_unknown_and_failing():
    ev = healthwatch.evaluate
    db2 = {"configured": True, "failures": 2, "error": "OSError"}
    one = ev({}, 0, 0, db2, (), wh_state=whs(1, "OSError"))
    assert one["warehouse"]["state"] == "ok"
    assert one["business_db"] == {"state": "unknown", "detail": "waiting for the warehouse"}
    two = ev({}, 0, 0, db2, (), wh_state=whs(2, "OSError"))
    assert two["warehouse"] == {"state": "failing", "detail": "OSError"}
    assert two["business_db"]["state"] == "unknown"


def test_wh_evaluate_not_configured_beats_unknown():
    out = healthwatch.evaluate({}, 0, 0, {"configured": False}, (), wh_state=whs(2, "OSError"))
    assert out["warehouse"]["state"] == "failing"
    assert out["business_db"] == {"state": "ok", "detail": "not configured"}


def test_wh_evaluate_business_failing_through_warehouse():
    out = healthwatch.evaluate({}, 0, 0, {"configured": True, "failures": 2, "error": "OSError"},
                               (), wh_state=whs(0))
    assert out["business_db"] == {"state": "failing",
                                  "detail": "unreachable through the warehouse (OSError)"}


def test_wh_titles_mode_aware(monkeypatch):
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: True)
    assert healthwatch.title("business_db") == "The business database is unreachable through the warehouse"
    assert "again" in healthwatch.recovered("business_db")
    assert healthwatch.title("worker") == healthwatch.TITLES["worker"]
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: False)
    assert healthwatch.title("business_db") == healthwatch.TITLES["business_db"]
    assert healthwatch.recovered("business_db") == healthwatch.RECOVERED["business_db"]
    assert healthwatch.title("zzz") == "System health alert"
    assert healthwatch.recovered("zzz") == "A health check is working again"


async def test_wh_business_probe_sql_by_mode(monkeypatch):
    seen = []

    async def run(sql):
        seen.append(sql)
        return []
    monkeypatch.setattr(guardrails, "run_readonly_sql", run)
    monkeypatch.setattr(healthwatch, "_db_configured", lambda: True)
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: True)
    await healthwatch._probe_business_db()
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: False)
    await healthwatch._probe_business_db()
    assert seen == ["SELECT n FROM ic_probe.business_db_ping", "SELECT 1"]


class FakeWhConn:
    def __init__(self):
        self.sql, self.closed = [], False

    async def fetchval(self, q):
        self.sql.append(q)

    async def close(self):
        self.closed = True


async def test_wh_probe_noop_outside_warehouse_mode(monkeypatch):
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: False)
    monkeypatch.setattr(healthwatch, "_wh_failures", 3)

    async def boom(*a, **k):
        raise AssertionError("must not connect")
    monkeypatch.setattr(warehouse, "connect", boom)
    await healthwatch._probe_warehouse()
    assert healthwatch._wh_failures == 0


async def test_wh_probe_counts_class_name_and_closes(monkeypatch):
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: True)
    monkeypatch.setattr(healthwatch, "_wh_failures", 0)
    conn = FakeWhConn()

    async def good(role, **kw):
        assert role == "reader" and kw["timeout"] == 10
        return conn
    monkeypatch.setattr(warehouse, "connect", good)

    async def no_run(*a, **k):
        raise guardrails.SQLGuardrailError("Business database is not configured yet")
    monkeypatch.setattr(guardrails, "run_readonly_sql", no_run)
    await healthwatch._probe_warehouse()
    assert healthwatch._wh_failures == 0 and conn.sql == ["SELECT 1"] and conn.closed

    async def bad(role, **kw):
        raise OSError("postgres://u:secret@h/db")
    monkeypatch.setattr(warehouse, "connect", bad)
    await healthwatch._probe_warehouse()
    await healthwatch._probe_warehouse()
    assert healthwatch._wh_failures == 2 and healthwatch._wh_error == "OSError"


class TrackPool:
    """Tracks open alerts per component; records every open and resolve."""
    def __init__(self):
        self.open, self.calls, self.n = set(), [], 0

    async def fetchval(self, sql, *a):
        if "ON CONFLICT (component) WHERE resolved_at IS NULL DO NOTHING" in sql:
            self.calls.append(("open", a[0]))
            if a[0] in self.open:
                return None
            self.open.add(a[0])
            self.n += 1
            return self.n
        if sql.startswith("UPDATE health_alerts SET resolved_at"):
            if a[0] in self.open:
                self.open.discard(a[0])
                self.calls.append(("resolve", a[0]))
                return 100
        return None

    async def execute(self, sql, *a):
        pass


@pytest.fixture
def wh_env(monkeypatch):
    heartbeat.reset()
    pool, notes = TrackPool(), []

    async def gp():
        return pool

    async def notify(comp, aid, failing, detail=""):
        notes.append((comp, failing))
    monkeypatch.setattr(healthwatch, "get_pool", gp)
    monkeypatch.setattr(healthwatch, "_notify", notify)
    monkeypatch.setattr(healthwatch, "_monitored", ())
    monkeypatch.setattr(healthwatch, "_db_failures", 0)
    monkeypatch.setattr(healthwatch, "_db_error", "")
    monkeypatch.setattr(healthwatch, "_wh_failures", 0)
    monkeypatch.setattr(healthwatch, "_wh_error", "")
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: True)
    monkeypatch.setattr(healthwatch, "_db_configured", lambda: True)
    yield pool, notes
    heartbeat.reset()


async def test_wh_sweep_unknown_race(wh_env, monkeypatch):
    pool, notes = wh_env
    probes = {"business": 0}
    script = iter([(0, 1), (1, None), (2, None), (0, 0)])  # (wh failures, db failures)
    step = {}

    async def pw():
        step["wh"], step["db"] = next(script)
        healthwatch._wh_failures = step["wh"]
        healthwatch._wh_error = "OSError" if step["wh"] else ""

    async def pb():
        probes["business"] += 1
        healthwatch._db_failures = step["db"]
        healthwatch._db_error = "OSError" if step["db"] else ""
    monkeypatch.setattr(healthwatch, "_probe_warehouse", pw)
    monkeypatch.setattr(healthwatch, "_probe_business_db", pb)

    c = await healthwatch.sweep()  # N: warehouse ok, business db failing once
    assert c["business_db"]["state"] == "ok" and probes["business"] == 1
    c = await healthwatch.sweep()  # N+1
    assert c["business_db"]["state"] == "unknown" and probes["business"] == 1
    assert healthwatch._db_failures == 1
    c = await healthwatch.sweep()  # N+2: warehouse alert opens
    assert c["warehouse"]["state"] == "failing" and probes["business"] == 1
    assert pool.calls.count(("open", "warehouse")) == 1
    assert not [x for x in pool.calls if x[1] == "business_db"]
    assert notes == [("warehouse", True)]
    c = await healthwatch.sweep()  # N+3 recovery
    assert c["business_db"]["state"] == "ok" and probes["business"] == 2
    assert pool.calls.count(("resolve", "warehouse")) == 1
    assert "business_db" not in pool.open and "warehouse" not in pool.open
    assert notes == [("warehouse", True), ("warehouse", False)]


async def test_wh_sweep_no_business_url(wh_env, monkeypatch):
    pool, notes = wh_env
    monkeypatch.setenv("TENANT_DATABASE_URL", "")
    monkeypatch.setenv("BUSINESS_DATABASE_URL", "")
    get_settings.cache_clear()
    monkeypatch.setattr(healthwatch, "_db_configured", lambda: False)
    conn = FakeWhConn()

    async def good(role, **kw):
        return conn
    monkeypatch.setattr(warehouse, "connect", good)

    async def no_run(*a, **k):
        raise guardrails.SQLGuardrailError("Business database is not configured yet")
    monkeypatch.setattr(guardrails, "run_readonly_sql", no_run)
    for _ in range(3):
        c = await healthwatch.sweep()
        assert c["warehouse"]["state"] == "ok"
        assert c["business_db"] == {"state": "ok", "detail": "not configured"}
    assert notes == [] and not [x for x in pool.calls if x[0] == "open"]
    get_settings.cache_clear()


async def test_wh_sweep_failing_warehouse_opens_one_and_resolves(wh_env, monkeypatch):
    pool, notes = wh_env
    state = {"down": True}

    async def conn_(role, **kw):
        if state["down"]:
            raise OSError("down")
        return FakeWhConn()
    monkeypatch.setattr(warehouse, "connect", conn_)
    biz = []

    async def run(sql):
        biz.append(sql)
        return []
    monkeypatch.setattr(guardrails, "run_readonly_sql", run)
    for _ in range(3):
        await healthwatch.sweep()
    assert pool.open == {"warehouse"} and notes == [("warehouse", True)]
    assert biz == []  # business probe skipped while the warehouse is failing
    assert not [x for x in pool.calls if x[1] == "business_db"]
    state["down"] = False
    await healthwatch.sweep()
    assert pool.open == set() and notes[-1] == ("warehouse", False)
    assert [x for x in pool.calls if x[0] == "resolve"] == [("resolve", "warehouse")]

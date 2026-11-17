"""Dashboard refresh loop (SERVE-05): pure parts, per-pair isolation, never-die."""
import asyncio
import datetime
import decimal
import json
import uuid

import asyncpg
import pytest

from app import dashboard_refresh as dr
from app import guardrails
from app.guardrails import SQLGuardrailError

D = decimal.Decimal


@pytest.mark.parametrize("value,expected", [
    (D("12.00"), 12), (D("12.5"), 12.5), (D("NaN"), None), (D("Infinity"), None),
    (float("nan"), None), (float("inf"), None), (True, True), (None, None),
    (datetime.date(2026, 7, 1), "2026-07-01"),
    (datetime.timedelta(minutes=2), 120.0),
    (b"x", None), (bytearray(b"x"), None), (memoryview(b"x"), None),
    ("a\x00b", "ab"), ([D("1"), "a\x00"], [1, "a"]), ((1, 2), [1, 2]),
    ({"k\x00": D("2.5")}, {"k": 2.5}),
])
def test_encode_value(value, expected):
    out = dr.encode_value(value)
    assert out == expected
    if expected is True:
        assert out is True


def test_encode_value_misc():
    assert isinstance(dr.encode_value(D(2 ** 60)), float)
    assert dr.encode_value(D("12.00")).__class__ is int
    u = uuid.uuid4()
    assert dr.encode_value(u) == str(u)
    dt = datetime.datetime(2026, 1, 2, 3, 4, tzinfo=datetime.timezone.utc)
    assert dr.encode_value(dt) == dt.isoformat()
    assert dr.encode_value(datetime.time(1, 2)) == "01:02:00"

    class O:
        def __str__(self):
            return "o\x00bj"
    assert dr.encode_value(O()) == "obj"


def test_encode_rows():
    cols, text = dr.encode_rows([{"customer": "A", "invoiced": D("10.50")},
                                 {"customer": "B", "invoiced": D("3")}])
    assert cols == ["customer", "invoiced"]
    assert json.loads(text) == [{"customer": "A", "invoiced": 10.5},
                                {"customer": "B", "invoiced": 3}]
    assert "NaN" not in text
    assert dr.encode_rows([]) == ([], "[]")
    assert dr.encode_rows([{"z": 1, "a": 2}])[0] == ["z", "a"]
    _, t = dr.encode_rows([{"x": float("nan")}])
    assert "NaN" not in t


@pytest.mark.parametrize("exc,cat", [
    (SQLGuardrailError("x"), "rejected"),
    (TimeoutError(), "timeout"), (asyncio.TimeoutError(), "timeout"),
    (asyncpg.QueryCanceledError("x"), "timeout"),
    (asyncpg.PostgresConnectionError("x"), "unavailable"),
    (asyncpg.ConnectionDoesNotExistError("x"), "unavailable"),
    (asyncpg.CannotConnectNowError("x"), "unavailable"),
    (asyncpg.AdminShutdownError("x"), "unavailable"),
    (asyncpg.CrashShutdownError("x"), "unavailable"),
    (asyncpg.TooManyConnectionsError("x"), "unavailable"),
    (asyncpg.InvalidAuthorizationSpecificationError("x"), "unavailable"),
    (asyncpg.InvalidCatalogNameError("x"), "unavailable"),
    (asyncpg.InterfaceError("x"), "unavailable"),
    (ConnectionRefusedError(), "unavailable"), (ConnectionError(), "unavailable"),
    (OSError(), "unavailable"),
    (asyncpg.UndefinedTableError("x"), "failed"),
    (ValueError(), "error"),
])
def test_categorize(exc, cat):
    assert dr.categorize(exc) == cat


def test_blips_tuple():
    assert dr.BLIPS == (asyncpg.PostgresConnectionError, asyncpg.CannotConnectNowError,
                        asyncpg.AdminShutdownError, ConnectionError, OSError)


def test_operator_error():
    e = asyncpg.UndefinedTableError('relation "no_such_table" does not exist')
    assert dr.operator_error("orders", "failed", e) == \
        'query orders: query failed: relation "no_such_table" does not exist'
    assert "Forbidden" in dr.operator_error("q", "rejected", SQLGuardrailError("Forbidden"))
    for cat in ("unavailable", "timeout", "error"):
        out = dr.operator_error("q", cat, OSError("db.internal:5432"))
        assert out == f"query q: {dr.VIEWER_MESSAGES[cat]}"
    big = dr.operator_error("q", "failed", ValueError("a\x00" * 900))
    assert len(big) <= 500 and "\x00" not in big


# ---- loop tests -------------------------------------------------------------

class FakePool:
    def __init__(self, claims=(), seed_exc=None, write_exc=None):
        self.claims = list(claims)
        self.executed = []
        self.seed_exc = seed_exc
        self.write_exc = write_exc

    async def fetch(self, sql, *args):
        assert sql == dr.CLAIM_SQL
        self.fetched_args = args
        return self.claims

    async def execute(self, sql, *args):
        if sql == dr.SEED_SQL and self.seed_exc:
            raise self.seed_exc
        if sql != dr.SEED_SQL and self.write_exc:
            raise self.write_exc
        self.executed.append((sql, args))


def pair(qid="q", sql="select 1", vid=1):
    return {"version_id": vid, "query_id": qid, "version": 1,
            "dashboard_id": "d", "sql": sql}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(dr, "_tenant_configured", lambda: True)

    def install(pool):
        async def gp():
            return pool
        monkeypatch.setattr(dr.db, "get_pool", gp)
    return install


def script(monkeypatch, behaviors):
    calls = []

    async def fake(sql):
        calls.append(sql)
        b = behaviors[len(calls) - 1]
        if isinstance(b, Exception):
            raise b
        return b
    monkeypatch.setattr(dr, "_execute", fake)
    return calls


@pytest.mark.asyncio
async def test_idle_when_unconfigured(monkeypatch):
    monkeypatch.setattr(dr, "_tenant_configured", lambda: False)

    async def gp():
        raise AssertionError("get_pool called")
    monkeypatch.setattr(dr.db, "get_pool", gp)
    assert await dr.refresh_due() is False


@pytest.mark.asyncio
async def test_success_pair(monkeypatch, configured):
    pool = FakePool([pair()])
    configured(pool)
    script(monkeypatch, [[{"n": D("5")}]])
    assert await dr.refresh_due() is False
    assert pool.fetched_args == (dr.BATCH,)
    assert pool.executed[0][0] == dr.SEED_SQL
    assert pool.executed[1] == (dr.UPSERT_OK_SQL, (1, "q", '["n"]', '[{"n": 5}]', 1))
    assert pool.executed[2] == (dr.CLEAR_VERSION_ERROR_SQL, (1,))


@pytest.mark.asyncio
async def test_rejection_isolated(monkeypatch, configured):
    pool = FakePool([pair("q"), pair("r")])
    configured(pool)
    script(monkeypatch, [SQLGuardrailError("Forbidden function: pg_sleep"), [{"n": 1}]])
    await dr.refresh_due()
    ex = pool.executed
    assert ex[1] == (dr.FAIL_SQL, (1, "q", "query rejected by the read-only guardrail",
                                   dr.FAIL_RETRY_MAX_MINUTES))
    assert ex[2] == (dr.VERSION_ERROR_SQL, (1,
        "query q: query rejected by the read-only guardrail: Forbidden function: pg_sleep"))
    assert ex[3][0] == dr.UPSERT_OK_SQL and ex[3][1][1] == "r"


@pytest.mark.asyncio
async def test_failed_and_timeout(monkeypatch, configured):
    pool = FakePool([pair("a"), pair("b"), pair("c")])
    configured(pool)
    script(monkeypatch, [asyncpg.UndefinedTableError("no table"), TimeoutError(),
                         asyncpg.QueryCanceledError("c")])
    await dr.refresh_due()
    ve = [a for s, a in pool.executed if s == dr.VERSION_ERROR_SQL]
    assert ve[0][1] == "query a: query failed: no table"
    assert ve[1][1] == "query b: query timed out"
    assert ve[2][1] == "query c: query timed out"


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [OSError("h:5432"), asyncpg.CannotConnectNowError("x")])
async def test_unavailable_defers_rest(monkeypatch, configured, exc):
    pool = FakePool([pair("a"), pair("b"), pair("c")])
    configured(pool)
    calls = script(monkeypatch, [exc])
    await dr.refresh_due()
    assert len(calls) == 1
    defers = [a for s, a in pool.executed if s == dr.DEFER_SQL]
    assert defers == [(1, "b"), (1, "c")]
    fail = [a for s, a in pool.executed if s == dr.FAIL_SQL]
    assert fail[0][2] == "data source unavailable"


@pytest.mark.asyncio
async def test_undeclared_sql(monkeypatch, configured):
    pool = FakePool([pair("a", sql=None)])
    configured(pool)
    calls = script(monkeypatch, [])
    await dr.refresh_due()
    assert calls == []
    ve = [a for s, a in pool.executed if s == dr.VERSION_ERROR_SQL][0]
    assert ve[1] == "query a: query failed: query is not declared in the version"


@pytest.mark.asyncio
async def test_default_execute_uses_guardrails(monkeypatch):
    seen = []

    async def fake(sql):
        seen.append(sql)
        return []
    monkeypatch.setattr(guardrails, "run_readonly_sql", fake)
    assert await dr._execute("select 2") == []
    assert seen == ["select 2"]


@pytest.mark.asyncio
async def test_seed_failure_nonfatal(monkeypatch, configured):
    pool = FakePool([pair()], seed_exc=asyncpg.UndefinedColumnError("c"))
    configured(pool)
    script(monkeypatch, [[]])
    await dr.refresh_due()
    assert any(s == dr.UPSERT_OK_SQL for s, _ in pool.executed)


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [asyncpg.PostgresConnectionError("x"),
                                 asyncpg.CannotConnectNowError("x")])
async def test_seed_blip_propagates(monkeypatch, configured, exc):
    configured(FakePool([pair()], seed_exc=exc))
    with pytest.raises(type(exc)):
        await dr.refresh_due()


@pytest.mark.asyncio
async def test_write_error_propagates(monkeypatch, configured):
    configured(FakePool([pair()], write_exc=ConnectionError("gone")))
    script(monkeypatch, [[{"n": 1}]])
    with pytest.raises(ConnectionError):
        await dr.refresh_due()


@pytest.mark.asyncio
async def test_busy_flag(monkeypatch, configured):
    monkeypatch.setattr(dr, "BATCH", 2)
    pool = FakePool([pair("a"), pair("b")])
    configured(pool)
    script(monkeypatch, [[], []])
    assert await dr.refresh_due() is True


async def _run_briefly(coro_fn, seconds: float = 0.05):
    task = asyncio.create_task(coro_fn())
    await asyncio.sleep(seconds)
    alive = not task.done()
    if task.done() and task.exception() is not None:
        task.result()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return alive


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [ConnectionError("x"), OSError("x"),
    asyncpg.PostgresConnectionError("x"), asyncpg.CannotConnectNowError("x"),
    asyncpg.AdminShutdownError("x"), RuntimeError("x")])
async def test_run_forever_never_dies(monkeypatch, exc):
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise exc
    monkeypatch.setattr(dr, "refresh_due", boom)
    monkeypatch.setattr(dr, "POLL_INTERVAL", 0.01)
    assert await _run_briefly(dr.run_forever)
    assert calls["n"] >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("exc,msg,tb", [
    (OSError("x"), "dashboard refresh DB connection blip", False),
    (asyncpg.CannotConnectNowError("x"), "dashboard refresh DB connection blip", False),
    (RuntimeError("x"), "dashboard refresh sweep error", True)])
async def test_run_forever_logging(monkeypatch, caplog, exc, msg, tb):
    async def boom():
        raise exc
    monkeypatch.setattr(dr, "refresh_due", boom)
    monkeypatch.setattr(dr, "POLL_INTERVAL", 0.01)
    with caplog.at_level("WARNING"):
        await _run_briefly(dr.run_forever)
    recs = [r for r in caplog.records if msg in r.getMessage()]
    assert recs
    assert bool(recs[0].exc_info) is tb


def test_dashboard_refresh_registered_in_ambient_loops():
    from app import main
    assert dr.run_forever in main._ambient_loops()


def test_encode_value_huge_decimal_clamped():
    assert dr.encode_value(D("1E+400")) is None


@pytest.mark.asyncio
async def test_encode_failure_isolated(monkeypatch, configured):
    pool = FakePool([pair("bad"), pair("good")])
    configured(pool)
    script(monkeypatch, [[{"n": 1}], [{"n": 2}]])
    real = dr.encode_rows

    def enc(rows):
        if rows == [{"n": 1}]:
            raise ValueError("Out of range float values")
        return real(rows)
    monkeypatch.setattr(dr, "encode_rows", enc)
    await dr.refresh_due()
    sqls = [(e[0], e[1][1] if len(e[1]) > 1 else None) for e in pool.executed]
    assert (dr.FAIL_SQL, "bad") in sqls
    assert any(s == dr.VERSION_ERROR_SQL for s, _ in sqls)
    assert (dr.UPSERT_OK_SQL, "good") in sqls

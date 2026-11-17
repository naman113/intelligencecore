"""Phase 22 (DCAP-08): run_readonly_sql(timezone=...) issues SET LOCAL TimeZone first."""
import pytest

from app import guardrails


class FakeTx:
    def __init__(self, conn, readonly):
        self.conn, self.readonly = conn, readonly

    async def __aenter__(self):
        self.conn.log.append(("tx", self.readonly))

    async def __aexit__(self, *a):
        return False


class FakeConn:
    def __init__(self):
        self.log = []

    def transaction(self, readonly=False):
        return FakeTx(self, readonly)

    async def execute(self, sql, *a):
        self.log.append(("execute", sql))

    async def fetch(self, sql, *a):
        self.log.append(("fetch", sql))
        return [{"x": 1}]


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class FakePool:
    def __init__(self):
        self.conn = FakeConn()

    def acquire(self):
        return FakeAcquire(self.conn)


@pytest.fixture(params=["direct", "warehouse"])
def pool(request, monkeypatch):
    p = FakePool()

    async def gp():
        return p
    if request.param == "warehouse":
        monkeypatch.setattr(guardrails.warehouse, "warehouse_mode", lambda: True)
        monkeypatch.setattr(guardrails, "_get_warehouse_pool", gp)

        async def allowed():
            return True
        monkeypatch.setattr(guardrails, "_warehouse_allowed", allowed)
        monkeypatch.setattr(guardrails, "_business_url_set", lambda: False)
    else:
        monkeypatch.setattr(guardrails.warehouse, "warehouse_mode", lambda: False)
        monkeypatch.setattr(guardrails, "get_business_pool", gp)
    return p


async def test_no_timezone_no_set(pool):
    rows = await guardrails.run_readonly_sql("select 1")
    assert rows == [{"x": 1}]
    assert not [e for e in pool.conn.log if e[0] == "execute"]


async def test_timezone_set_first(pool):
    await guardrails.run_readonly_sql("select 1", timezone="Asia/Kolkata")
    log = pool.conn.log
    assert log[0] == ("tx", True)
    assert log[1] == ("execute", "SET LOCAL TimeZone TO 'Asia/Kolkata'")
    assert log[2][0] == "tx" and log[3][0] == "fetch"


@pytest.mark.parametrize("bad", ["Not/AZone", "x'; drop", "", "UTC' --"])
async def test_invalid_zone_refused_before_connection(monkeypatch, bad):
    async def boom():
        raise AssertionError("connection used")
    monkeypatch.setattr(guardrails, "get_business_pool", boom)
    monkeypatch.setattr(guardrails.warehouse, "warehouse_mode", lambda: False)
    with pytest.raises(guardrails.SQLGuardrailError, match="invalid timezone"):
        await guardrails.run_readonly_sql("select 1", timezone=bad)

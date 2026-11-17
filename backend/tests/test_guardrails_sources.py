"""Phase 15 (SRC-07): registered sources through the warehouse guardrails."""
import asyncpg
import pytest

from app import guardrails as gr, sourcesvc, syncsched, warehouse

pytestmark = pytest.mark.asyncio


_Err = asyncpg.exceptions.ConnectionFailureError  # SQLSTATE 08006


class Conn:
    def __init__(self, exc=None):
        self.exc = exc

    def transaction(self, **k):
        class T:
            async def __aenter__(s):
                return None

            async def __aexit__(s, *a):
                return False
        return T()

    async def fetch(self, sql):
        if self.exc:
            raise self.exc
        return [{"a": 1}]


class Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        c = self.conn

        class A:
            async def __aenter__(s):
                return c

            async def __aexit__(s, *a):
                return False
        return A()


@pytest.fixture
def env(monkeypatch):
    st = {"waited": 0, "relink": [], "reprov": [], "url": False, "active": False, "exc": None}
    monkeypatch.setattr(gr, "_business_url_set", lambda: st["url"])

    async def active():
        return st["active"]

    async def wait(n):
        st["waited"] += 1

    async def pool():
        return Pool(Conn(st["exc"]))
    monkeypatch.setattr(sourcesvc, "has_active_sources", active)
    monkeypatch.setattr(warehouse, "wait_linked", wait)
    monkeypatch.setattr(warehouse, "request_relink", lambda *a, **k: st["relink"].append(a))
    monkeypatch.setattr(syncsched, "request_reprovision", lambda n: st["reprov"].append(n))
    monkeypatch.setattr(gr, "_get_warehouse_pool", pool)
    return st


async def test_sources_only_allows_warehouse_without_wait(env):
    env["active"] = True
    assert await gr._run_on_warehouse("SELECT 1", 10) == [{"a": 1}]
    assert env["waited"] == 0


async def test_business_url_waits(env):
    env["url"] = True
    await gr._run_on_warehouse("SELECT 1", 10)
    assert env["waited"] == 1


async def test_neither_raises_not_configured(env):
    with pytest.raises(gr.SQLGuardrailError) as e:
        await gr._run_on_warehouse("SELECT 1", 10)
    assert str(e.value) == gr._NOT_CONFIGURED


async def test_src_sql_unreachable(env):
    env["url"] = True
    env["exc"] = _Err("x")
    with pytest.raises(gr.SourceUnreachable) as e:
        await gr._run_on_warehouse("SELECT * FROM src_erp_my.a", 10)
    assert str(e.value) == "A live data source is unreachable through the warehouse."


async def test_other_sql_still_business_unreachable(env):
    env["url"] = True
    env["exc"] = _Err("x")
    with pytest.raises(gr.BusinessDbUnreachable):
        await gr._run_on_warehouse("SELECT * FROM public.a", 10)


async def test_undefined_table_src_reprovisions(env):
    env["url"] = True
    env["exc"] = asyncpg.UndefinedTableError("x")
    with pytest.raises(asyncpg.UndefinedTableError):
        await gr._run_on_warehouse("SELECT * FROM src_erp_my.a", 10)
    assert env["reprov"] == ["erp_my"] and not env["relink"]


async def test_undefined_table_other_relinks(env):
    env["url"] = True
    env["exc"] = asyncpg.UndefinedTableError("x")
    with pytest.raises(asyncpg.UndefinedTableError):
        await gr._run_on_warehouse("SELECT * FROM public.a", 10)
    assert env["relink"] and not env["reprov"]

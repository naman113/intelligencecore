"""Phase 17 plan 08 (CONS-04): per-run live query budget (fakes only)."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import freshness, livebudget
from app import playbook_tools as pt
from app.agents import graph

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


def _fresh(live=(), imp=(), failed=False):
    return freshness.Freshness(as_of=NOW, stale=False, reads=tuple(live) + tuple(imp),
                               import_reads=tuple(imp), live_reads=tuple(live), failed=failed)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    livebudget._used.clear()
    monkeypatch.setattr(livebudget, "get_settings",
                        lambda: SimpleNamespace(playbook_live_query_budget=3))
    t1 = pt.ctx_playbook_id.set(5)
    t2 = graph.ctx_run_id.set(900)
    yield
    pt.ctx_playbook_id.reset(t1)
    graph.ctx_run_id.reset(t2)
    livebudget._used.clear()


def _facts(monkeypatch, fresh):
    async def fs(sql, **k):
        return fresh
    monkeypatch.setattr(freshness, "for_sql", fs)


async def test_chat_not_counted(monkeypatch):
    _facts(monkeypatch, _fresh(live=["public.orders"]))
    pt.ctx_playbook_id.set(None)
    for _ in range(10):
        assert await livebudget.check("select 1") == (None, [])
    assert len(livebudget._used) == 0


async def test_budget_then_refusal(monkeypatch):
    _facts(monkeypatch, _fresh(live=["public.orders"]))
    for _ in range(3):
        msg, live = await livebudget.check("select 1")
        assert msg is None and live == ["public.orders"]
    msg, live = await livebudget.check("select 1")
    assert msg.startswith("LIVE QUERY BUDGET SPENT") and "public.orders" in msg
    assert "import" in msg


async def test_imported_only_not_counted(monkeypatch):
    _facts(monkeypatch, _fresh(imp=["public.customers"]))
    for _ in range(10):
        assert await livebudget.check("select 1") == (None, [])


async def test_parallel_batch_admits_exactly_budget(monkeypatch):
    async def fs(sql, **k):
        await asyncio.sleep(0)
        return _fresh(live=["public.orders"])
    monkeypatch.setattr(freshness, "for_sql", fs)
    res = await asyncio.gather(*[livebudget.check("select 1") for _ in range(4)])
    assert sum(1 for m, _ in res if m is None) == 3
    assert sum(1 for m, _ in res if m) == 1


async def test_budget_zero(monkeypatch):
    monkeypatch.setattr(livebudget, "get_settings",
                        lambda: SimpleNamespace(playbook_live_query_budget=0))
    _facts(monkeypatch, _fresh(live=["public.orders"]))
    assert (await livebudget.check("select 1"))[0].startswith("LIVE QUERY BUDGET SPENT")
    _facts(monkeypatch, _fresh(imp=["public.customers"]))
    assert (await livebudget.check("select 1")) == (None, [])


async def test_fail_closed_when_facts_unavailable(monkeypatch):
    async def boom():
        raise RuntimeError("agent_memory blip")
    monkeypatch.setattr(freshness, "load_facts", boom)
    monkeypatch.setattr(freshness.warehouse, "warehouse_mode", lambda: True)
    sql = "SELECT * FROM orders"
    for _ in range(3):
        msg, live = await livebudget.check(sql)
        assert msg is None and live
    msg, _ = await livebudget.check(sql)
    assert msg and msg.startswith("LIVE QUERY BUDGET SPENT")


async def test_fail_closed_unparseable(monkeypatch):
    _facts(monkeypatch, _fresh(live=(), failed=True))
    msg, live = await livebudget.check("not sql")
    assert msg is None and live == [livebudget.UNRESOLVED]
    for _ in range(2):
        await livebudget.check("not sql")
    msg, _ = await livebudget.check("not sql")
    assert msg and livebudget.UNRESOLVED in msg


async def test_forget_and_eviction(monkeypatch):
    _facts(monkeypatch, _fresh(live=["a"]))
    await livebudget.check("x")
    assert 900 in livebudget._used
    livebudget.forget(900)
    assert 900 not in livebudget._used
    for i in range(300):
        graph.ctx_run_id.set(i)
        await livebudget.check("x")
    assert len(livebudget._used) == livebudget.MAX_RUNS
    assert 0 not in livebudget._used and 299 in livebudget._used


# -- call sites ---------------------------------------------------------------

REFUSE = "LIVE QUERY BUDGET SPENT: test"


async def _refuse(sql):
    return REFUSE, ["public.orders"]


def _no_sql(monkeypatch, *modules):
    async def boom(*a, **k):
        raise AssertionError("SQL must not run")
    for m in modules:
        monkeypatch.setattr(m, "run_readonly_sql", boom)


async def _noop_gate(sql):
    return None


async def test_run_sql_site(monkeypatch):
    logged = []

    async def lt(name, args, result, start, refused=False):
        logged.append((name, refused))
    monkeypatch.setattr(graph, "_rules_gate", _noop_gate)
    monkeypatch.setattr(graph, "_log_tool", lt)
    monkeypatch.setattr(livebudget, "check", _refuse)
    _no_sql(monkeypatch, graph)
    out = await graph.run_sql.ainvoke({"sql": "select 1"})
    assert out == REFUSE and logged == [("run_sql", True)]
    out = await graph.run_sql_to_csv.ainvoke({"sql": "select 1", "filename": "a.csv"})
    assert out == REFUSE and logged[-1] == ("run_sql_to_csv", True)


async def test_run_sql_logs_live(monkeypatch):
    logged = []

    async def lt(name, args, result, start, refused=False):
        logged.append(args)

    async def ok(sql):
        return None, ["public.orders"]

    async def rows(sql, **k):
        return [{"a": 1}]

    async def note():
        return ""
    monkeypatch.setattr(graph, "_rules_gate", _noop_gate)
    monkeypatch.setattr(graph, "_log_tool", lt)
    monkeypatch.setattr(graph, "_p16_unverified_note", note)
    monkeypatch.setattr(graph, "run_readonly_sql", rows)
    monkeypatch.setattr(livebudget, "check", ok)
    await graph.run_sql.ainvoke({"sql": "select 1"})
    assert logged[-1]["live"] == ["public.orders"]


async def test_check_evidence_site(monkeypatch):
    from app import insights

    async def appr():
        return {"regions"}
    monkeypatch.setattr(insights, "approved_tables", appr)
    monkeypatch.setattr(insights, "unapproved_refs", lambda t, a: [])
    monkeypatch.setattr(insights, "evidence_tables", lambda s: set())
    monkeypatch.setattr(livebudget, "check", _refuse)
    _no_sql(monkeypatch, insights.guardrails)
    assert await insights.check_evidence("select 1") == (None, REFUSE)


async def test_query_measure_site(monkeypatch):
    from app import modeltools as mt
    from app import semanticmodel
    from test_model_compiler import build
    model = build()

    async def lm():
        return model
    logged = []

    async def lt(name, args, result, start, refused=False):
        logged.append((name, refused))
    monkeypatch.setattr(semanticmodel, "load_model", lm)
    monkeypatch.setattr(mt, "_log_tool", lt)
    monkeypatch.setattr(livebudget, "check", _refuse)
    _no_sql(monkeypatch, mt)
    out = await mt.query_measure.ainvoke({"measure": "net_sales", "range": "last_7_days"})
    assert out == REFUSE and logged == [("query_measure", True)]


async def test_forget_on_exception(monkeypatch):
    from app import playbooks
    seen = []

    class Pool:
        async def fetchrow(self, *a):
            return {"id": 1, "status": "active", "name": "pb", "tool_grants": [],
                    "autonomy_tier": "draft", "goal": "g"}

        async def execute(self, *a):
            pass

    async def gp():
        return Pool()

    async def ctx(task, pb):
        return "ctx"

    class Boom:
        async def ainvoke(self, *a, **k):
            raise RuntimeError("x")
    monkeypatch.setattr(playbooks, "get_pool", gp)
    monkeypatch.setattr(playbooks, "_assemble_context", ctx)
    monkeypatch.setattr(playbooks, "_playbook_registry", lambda: {})
    monkeypatch.setattr(playbooks, "_model", lambda: None)
    monkeypatch.setattr(playbooks, "create_react_agent", lambda *a, **k: Boom())
    monkeypatch.setattr(playbooks, "get_settings",
                        lambda: SimpleNamespace(owner_number="", playbook_token_budget=1000))
    monkeypatch.setattr(livebudget, "forget", lambda rid: seen.append(rid))
    await playbooks.run_playbook_task({"id": 7, "playbook_id": 1, "case_id": None,
                                       "expectation_id": None})
    assert seen == [7]

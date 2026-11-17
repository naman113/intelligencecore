"""Quick 029 F2: a recurring create_watch never creates a watch; it records a KPI proposal."""
import pytest

from app import insights, worker
from app.agents import graph


class FakePool:
    def __init__(self):
        self.inserts = []

    async def fetchval(self, sql, *a):
        self.inserts.append((sql, a))
        return 41


@pytest.fixture
def env(monkeypatch):
    pool = FakePool()
    logged = []

    async def gp():
        return pool

    async def log_tool(name, args, result, start, refused=False):
        logged.append((name, result, refused))

    calls = []

    async def proposer(title, condition, cadence="daily", rationale="", **kw):
        calls.append({"title": title, "condition": condition, "cadence": cadence, **kw})
        return state["reply"]

    state = {"reply": "proposed KPI watch (action #77): 'Spend' every hourly - awaiting owner approval. "
                      "Nothing runs until approved."}
    monkeypatch.setattr(graph, "get_pool", gp)
    monkeypatch.setattr(graph, "_log_tool", log_tool)
    monkeypatch.setattr(insights, "propose_kpi_action", proposer)
    t1 = graph.ctx_phone.set("ui:owner")
    t2 = graph.ctx_run_id.set(5)
    yield pool, logged, calls, state
    graph.ctx_phone.reset(t1)
    graph.ctx_run_id.reset(t2)


async def _cw(**kw):
    return await graph.create_watch.ainvoke(kw)


async def test_recurring_makes_no_watch_and_proposes(env):
    pool, logged, calls, _ = env
    out = await _cw(title="Spend", condition="daily spend spikes", cadence_minutes=60, one_shot=False)
    assert pool.inserts == []
    assert len(calls) == 1
    c = calls[0]
    assert c["origin"] == "chat" and c["thread_id"] == "ui:owner" and c["run_id"] == 5
    assert c["cadence"] == "60"
    assert out.startswith("NOT STARTED") and "action #77" in out and "approval" in out
    assert "created" not in out
    assert logged == [("create_watch", out, False)]


async def test_cadence_floor_is_15(env):
    _, _, calls, _ = env
    await _cw(title="Spend", condition="x", cadence_minutes=5, one_shot=False)
    assert calls[0]["cadence"] == "15"


async def test_refused_passes_through(env):
    pool, logged, calls, state = env
    state["reply"] = "REFUSED: a KPI with this title is already being watched"
    out = await _cw(title="Spend", condition="x", one_shot=False)
    assert out == state["reply"] and pool.inserts == []
    assert logged[0][2] is True


async def test_one_shot_still_inserts(env):
    pool, logged, calls, _ = env
    out = await _cw(title="Order 9 delivered", condition="order 9 status is delivered",
                    cadence_minutes=15, one_shot=True)
    assert calls == [] and len(pool.inserts) == 1
    assert "watch #41 created" in out


async def test_proposer_error_is_a_tool_error_not_a_watch(env, monkeypatch):
    pool, _, _, _ = env

    async def boom(*a, **k):
        raise RuntimeError("db")
    monkeypatch.setattr(insights, "propose_kpi_action", boom)
    out = await _cw(title="Spend", condition="x", one_shot=False)
    assert pool.inserts == [] and "TOOL ERROR" in out and "watch #" not in out


async def test_worker_guard_turns_claim_into_approval_message(monkeypatch):
    class P:
        async def fetch(self, sql, *a):
            if "FROM tool_calls" in sql:
                return [{"tool": "create_watch", "result_digest":
                         "NOT STARTED: a standing (recurring) watch needs the owner's approval, so it "
                         "was proposed instead. proposed KPI watch (action #77): 'Spend'"}]
            if "FROM actions" in sql:
                return [{"id": 77}]
            return []

    async def gp():
        return P()
    monkeypatch.setattr(worker, "get_pool", gp)
    out = await worker._guard_false_watch(
        5, "Done! I've set up a watch that alerts you when daily spend spikes.",
        "alert me if daily spend spikes")
    assert "did not start a watch" in out and "action #77" in out

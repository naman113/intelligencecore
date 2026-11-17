"""Phase 12 CTX-09d: shared propose_kpi_action, reactive propose_kpi, executor origin/thread."""

import json

import pytest
from _supprompt import composed_supervisor_prompt

from app import insights
from app.agents import graph


class FakePool:
    def __init__(self, run_count=0):
        self.inserted = []
        self.watches = []
        self.run_count = run_count

    async def fetchrow(self, sql, *a):
        return None

    async def fetchval(self, sql, *a):
        if "INSERT INTO actions" in sql:
            self.inserted.append(a)
            return 41
        if "count(*)" in sql:
            return self.run_count
        if "INSERT INTO watches" in sql:
            self.watches.append(a)
            return 5
        return None


@pytest.fixture
def pool(monkeypatch):
    p = FakePool()

    async def gp():
        return p
    monkeypatch.setattr(insights, "get_pool", gp)
    return p


COND = "Count of invoices where status = 'open' in invoices, above 100"


async def test_propose_inserts_draft_action(pool):
    out = await insights.propose_kpi_action(
        "Open invoices", COND, "daily", "owner asked",
        origin="chat", thread_id="ui:owner", run_id=7)
    assert "action #41" in out and "awaiting owner approval" in out
    payload = json.loads(pool.inserted[0][0])
    assert payload["origin"] == "chat" and payload["thread_id"] == "ui:owner"
    assert payload["cadence_seconds"] == 86400 and payload["proposal_key"]
    assert payload["title_key"] == "open invoices"
    assert pool.inserted[0][1] == 7


async def test_propose_refusals(pool):
    kw = dict(origin="chat", thread_id="ui:owner", run_id=7)
    assert (await insights.propose_kpi_action("T", COND, "bogus", **kw)).startswith("REFUSED")
    assert (await insights.propose_kpi_action("", COND, "daily", **kw)).startswith("REFUSED")
    assert (await insights.propose_kpi_action(
        "T", COND, "daily", origin="evil", thread_id="ui:owner", run_id=7)).startswith("REFUSED")
    assert pool.inserted == []


async def test_cap_parameter(pool):
    pool.run_count = 2
    kw = dict(origin="chat", thread_id="ui:owner", run_id=7)
    assert (await insights.propose_kpi_action("T", COND, "daily", **kw)).startswith("REFUSED")
    out = await insights.propose_kpi_action("T", COND, "daily", cap=8, **kw)
    assert out.startswith("proposed")


async def test_reactive_tool_needs_no_tier(pool):
    tok = graph.ctx_phone.set("ui:owner")
    tok2 = graph.ctx_run_id.set(9)
    logs = []

    async def fake_log(name, args, result, start, refused=False):
        logs.append(name)
    orig = graph._log_tool
    graph._log_tool = fake_log
    try:
        out = await graph.propose_kpi.ainvoke({"title": "Open invoices", "condition": COND})
    finally:
        graph._log_tool = orig
        graph.ctx_phone.reset(tok)
        graph.ctx_run_id.reset(tok2)
    assert "awaiting owner approval" in out and logs == ["propose_kpi"]
    assert json.loads(pool.inserted[0][0])["thread_id"] == "ui:owner"


def test_registry_and_superset():
    assert "propose_kpi" in graph.AGENT_TOOL_SUPERSET["supervisor"]
    assert graph._tool_registry()["propose_kpi"] is graph.propose_kpi


def test_prompt_rule(monkeypatch):
    p = composed_supervisor_prompt(monkeypatch)
    assert "use create_watch instead of answering once" not in p
    assert p.count("MUST use propose_kpi") == 1


def test_watch_request_rule(monkeypatch):
    p = composed_supervisor_prompt(monkeypatch)
    assert p.count("WATCH REQUESTS (Phase 12)") == 1
    assert "one_shot=true in THIS turn" in p and "watch #N created" in p
    assert p.count("DASHBOARD STATUS (Phase 12)") == 1


def _payload(**kw):
    d = {"proposal_key": "k1", "title": "Open invoices", "condition": COND,
         "cadence_seconds": 86400}
    d.update(kw)
    return d


async def test_executor_chat_origin_and_thread(pool):
    assert await insights.create_watch_from_proposal(_payload(origin="chat", thread_id="ui:owner"))
    a = pool.watches[0]
    assert a[0] == "ui:owner" and a[6] == "chat"


async def test_executor_fallbacks(pool, monkeypatch):
    monkeypatch.setattr(insights, "get_settings", lambda: type("S", (), {"owner_number": ""})())
    await insights.create_watch_from_proposal(_payload(origin="context_pack", thread_id="+15550001"))
    await insights.create_watch_from_proposal(_payload(origin="weird"))
    assert pool.watches[0][0] == "dashboard" and pool.watches[0][6] == "context_pack"
    assert pool.watches[1][6] == "exploration"

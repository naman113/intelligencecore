"""Quick 002: sentinel escalations are bounded and their findings delivered.

The p14 escalation ran the supervisor open-ended from a one-line brief, hit
the 150k reactive budget and died; when one finished its reply was dropped.
"""

import types

import pytest

from app import sentinel
from app.budget import BudgetExceeded

WATCH = {"id": 3, "title": "Barge fleet diesel spike", "thread_id": "ui:owner", "kind": "kpi",
         "condition": "barge diesel on the last fuel day is 30% above its 7-day average",
         "compiled_sql": 'SELECT sum("ConsumptionLiters") AS litres FROM "DailyFuelReports"',
         "one_shot": False}


@pytest.fixture
def env(monkeypatch):
    state = {"runs": [], "delivered": [], "reply": "Ganga Barge 1 burned 2,100 L on 24 Oct.",
             "raise": None}

    async def run(history, message, context_text="", **kw):
        state["runs"].append((history, message, kw))
        if state["raise"]:
            raise state["raise"]
        return state["reply"]

    async def deliver(thread_id, msg):
        state["delivered"].append((thread_id, msg))
        return True

    from app.agents import graph
    monkeypatch.setattr(graph, "run", run)
    monkeypatch.setattr(sentinel.appchannel, "deliver", deliver)
    monkeypatch.setattr(sentinel, "_escalation_times", [])
    monkeypatch.setattr(sentinel, "get_settings",
                        lambda: types.SimpleNamespace(playbook_token_budget=200_000))
    return state


def test_brief_carries_condition_sql_and_values():
    brief = sentinel._escalation_brief(WATCH, {"summary": "litres: now 4035."}, {"litres": 4035.02})
    assert WATCH["condition"] in brief
    assert WATCH["compiled_sql"] in brief
    assert '"litres": 4035.02' in brief
    assert f"at most {sentinel.ESCALATION_MAX_QUERIES} run_sql" in brief
    assert "Do not send messages yourself" in brief


async def test_escalation_is_bounded_and_findings_delivered(env):
    ok = await sentinel._maybe_escalate(WATCH, {"severity": "critical", "summary": "s"},
                                        {"litres": 4035.02})
    assert ok is True
    (history, message, kw), = env["runs"]
    assert history == [] and "Check query the monitor runs" in message
    assert kw == {"token_budget": 200_000,
                  "recursion_limit": sentinel.ESCALATION_RECURSION_LIMIT}
    (thread, msg), = env["delivered"]
    assert thread == "ui:owner"
    assert msg.endswith("Ganga Barge 1 burned 2,100 L on 24 Oct.")
    assert "Investigation" in msg and WATCH["title"] in msg


async def test_budget_overrun_is_logged_not_raised(env):
    env["raise"] = BudgetExceeded("token budget 200000 exceeded (200500)")
    ok = await sentinel._maybe_escalate(WATCH, {"severity": "critical", "summary": "s"}, {})
    assert ok is False and env["delivered"] == []


async def test_non_critical_never_escalates(env):
    assert await sentinel._maybe_escalate(WATCH, {"severity": "warning"}, {}) is False
    assert env["runs"] == []


async def test_rate_limit_still_applies(env, monkeypatch):
    import time
    monkeypatch.setattr(sentinel, "_escalation_times",
                        [time.time()] * sentinel.MAX_ESCALATIONS_PER_HOUR)
    assert await sentinel._maybe_escalate(WATCH, {"severity": "critical"}, {}) is False
    assert env["runs"] == []

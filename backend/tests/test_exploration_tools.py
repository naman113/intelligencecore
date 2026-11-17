"""Phase 7 exploration tools: tiers are enforced in the tool, and every write goes through insights.py."""

import re

import asyncpg
import pytest

from app import insights
from app import playbook_tools as pt
from app.agents import graph

EV = {"row_count": 3, "columns": ["a"], "sample": [{"a": 1}], "truncated": False, "relative_dates": False}


@pytest.fixture
def ctx(monkeypatch):
    toks = []

    def setter(var, val):
        toks.append((var, var.set(val)))
    setter(pt.ctx_tier, "draft")
    setter(pt.ctx_case_id, 9)
    setter(graph.ctx_run_id, 77)
    logs = []

    async def fake_log(name, args, result, start, refused=False):
        logs.append((name, result, refused))
    monkeypatch.setattr(pt, "_log_tool", fake_log)
    yield setter
    for var, tok in reversed(toks):
        try:
            var.reset(tok)
        except ValueError:  # set inside the test's own context
            pass


@pytest.fixture
def calls(monkeypatch):
    c = {"check": [], "note": [], "action": [], "conflict": []}

    async def check(sql):
        c["check"].append(sql)
        return c.get("ev_result", (dict(EV), ""))

    async def note(*a, **k):
        c["note"].append((a, k))
        return c.get("note_result", (11, 9, ""))

    async def action(kind, payload, status):
        c["action"].append((kind, payload, status))
        if c.get("action_exc"):
            raise c["action_exc"]
        return 5

    async def conflict(key, run_id=None):
        c["conflict"].append((key, run_id))
        return c.get("conflict_result")

    monkeypatch.setattr(insights, "check_evidence", check)
    monkeypatch.setattr(insights, "record_note", note)
    monkeypatch.setattr(pt, "_record_action", action)
    monkeypatch.setattr(insights, "kpi_conflict", conflict)

    async def no_wa(*a, **k):  # pragma: no cover
        raise AssertionError("propose_kpi must not send WhatsApp")
    monkeypatch.setattr(pt.whatsapp, "send_message", no_wa)
    return c


def _ri(**kw):
    args = {"title": "South orders fell 35%", "finding": "Orders fell.",
            "evidence_sql": "SELECT 1", "severity": "info"}
    args.update(kw)
    return pt.record_insight.ainvoke(args)


async def test_record_insight_refused_without_tier(ctx, calls):
    ctx(pt.ctx_tier, None)
    assert (await _ri()).startswith("TIER REFUSED")
    assert not calls["check"]


async def test_record_insight_refused_at_notify(ctx, calls):
    ctx(pt.ctx_tier, "notify")
    assert (await _ri()).startswith("TIER REFUSED")
    assert not calls["check"]


@pytest.mark.parametrize("kw", [
    {"severity": "urgent"}, {"title": ""}, {"title": "x" * 121},
    {"finding": ""}, {"title": "bad‮title"},
])
async def test_record_insight_validates_args_before_sql(ctx, calls, kw):
    assert (await _ri(**kw)).startswith("REFUSED:")
    assert not calls["check"]


async def test_record_insight_refuses_bad_evidence(ctx, calls):
    calls["ev_result"] = (None, "no approved tables: x")
    r = await _ri()
    assert r.startswith("REFUSED:") and "no approved tables" in r and "Nothing was recorded" in r
    assert not calls["note"]


async def test_record_insight_relays_record_refusal(ctx, calls):
    calls["note_result"] = (None, None, "already recorded within 30 days: X")
    r = await _ri()
    assert r.startswith("REFUSED:") and "already recorded within 30 days: X" in r


async def test_record_insight_success(ctx, calls):
    r = await _ri(severity=" WARNING ")
    assert "recorded insight" in r and "3 rows" in r
    a, k = calls["note"][0]
    assert k["case_id"] == 9 and k["run_id"] == 77
    assert "warning" in a and EV in a


async def test_record_insight_warns_on_relative_dates(ctx, calls):
    calls["ev_result"] = (dict(EV, relative_dates=True), "")
    assert "absolute date" in await _ri()


@pytest.mark.parametrize("cap", ["record_insight", "propose_kpi"])
def test_tier_matrix_exploration_caps(cap):
    assert pt.tier_allows("draft", cap) and pt.tier_allows("autonomous_message", cap)
    for t in ("notify", None, "bogus"):
        assert not pt.tier_allows(t, cap)


def test_registry_has_exploration_tools():
    assert pt.PLAYBOOK_TOOL_REGISTRY["record_insight"] is pt.record_insight


# ---- propose_kpi

def _pk(**kw):
    args = {"title": "South weekly orders",
            "condition": "Count of South-region sales_orders in the trailing 7 days falls below 12",
            "cadence": "daily", "rationale": "South fell 35%"}
    args.update(kw)
    return pt.propose_kpi.ainvoke(args)


async def test_propose_kpi_refused_without_tier(ctx, calls):
    ctx(pt.ctx_tier, None)
    assert (await _pk()).startswith("TIER REFUSED")
    assert not calls["action"]


async def test_propose_kpi_writes_create_watch(ctx, calls):
    r = await _pk()
    assert "awaiting owner approval" in r
    (kind, p, status), = calls["action"]
    assert kind == "create_watch" and status == "proposed"
    assert p["title_key"] == insights.norm_title("South weekly orders")
    assert p["cadence"] == "daily" and p["cadence_seconds"] == 86400
    assert p["origin"] == "exploration" and p["rationale"] == "South fell 35%"
    assert re.fullmatch(r"[0-9a-f]{32}", p["proposal_key"])
    assert p["_summary"].startswith("Watch a new KPI:")
    assert calls["conflict"] == [(p["title_key"], 77)]


@pytest.mark.parametrize("kw", [
    {"cadence": "fortnightly"}, {"condition": ""}, {"title": "x" * 121}])
async def test_propose_kpi_bad_fields(ctx, calls, kw):
    assert (await _pk(**kw)).startswith("REFUSED:")
    assert not calls["action"]


async def test_propose_kpi_conflict(ctx, calls):
    calls["conflict_result"] = "already watched: X"
    assert await _pk() == "REFUSED: already watched: X"
    assert not calls["action"]


async def test_propose_kpi_unique_violation(ctx, calls):
    calls["action_exc"] = asyncpg.UniqueViolationError("dup")
    assert "already proposed" in await _pk()


async def test_propose_action_cannot_mint_create_watch(ctx, calls):
    r = await pt.propose_action.ainvoke(
        {"kind": "create_watch", "payload_json": "{}", "summary": "x"})
    assert r == "REFUSED: kind 'create_watch' can only be proposed by its dedicated tool."


def test_registry_has_propose_kpi():
    assert pt.PLAYBOOK_TOOL_REGISTRY["propose_kpi"] is pt.propose_kpi

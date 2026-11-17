"""AGENT-08: list_dashboard_proposals tool, supervisor prompt rules and the false-draft worker guard."""

import datetime
import json

import pytest
from _supprompt import composed_supervisor_prompt

from app import dashboardtools as dt, worker
from app.agents import graph as g


REAL_FALSE = ("I've created a *draft* dashboard showing open invoices by customer. "
              "The dashboard is awaiting your approval on the Approvals page.")
CTX_REPORT = ("Imported context_pack_northwind.json: 14 proposals are awaiting your approval on "
              "Schema and Approvals (tables 2, rules 2, KPIs 2, watches 2, hide 1). Dashboard ideas "
              "(listed, not drafted): open invoices by customer. Nothing is applied until you approve.")
NO_DRAFTS = "There are no dashboard drafts awaiting approval."
APPROVED = "Your dashboard draft #6 was approved and published."

NOW = datetime.datetime(2026, 10, 25, 12, 0, tzinfo=datetime.timezone.utc)


class FakePool:
    def __init__(self, tools=(), run_actions=False, statuses=None, proposals=None):
        self.tools = list(tools)
        self.run_actions = run_actions
        self.statuses = statuses or {}
        self.sql = []

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        if "FROM tool_calls" in sql:
            return [{"tool": t} for t in self.tools]
        if "id = ANY" in sql:
            return [{"id": i, "status": self.statuses[i]} for i in args[0] if i in self.statuses]
        return []

    async def fetchval(self, sql, *args):
        self.sql.append(sql)
        return 1 if self.run_actions else None


def _patch(monkeypatch, pool, pending=()):
    async def gp():
        return pool

    async def pend(limit=20):
        return list(pending)
    monkeypatch.setattr(worker, "get_pool", gp)
    monkeypatch.setattr(dt, "pending_dashboard_proposals", pend)


# ---- tool ----
class ListPool:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""

    async def fetch(self, sql, *args):
        self.sql = sql
        return self.rows


def _row(i, payload):
    return {"id": i, "payload": payload, "created_at": NOW}


async def test_pending_helper_whitelists_keys(monkeypatch):
    payload = {"slug": "inv", "version": 2, "title": "Invoices", "change_note": "n",
               "draft_url": "/d/inv?p=SECRETTOKEN", "preview_expires_at": "x"}
    pool = ListPool([_row(7, json.dumps(payload)), _row(5, payload)])

    async def gp():
        return pool
    monkeypatch.setattr(dt, "get_pool", gp)
    items = await dt.pending_dashboard_proposals()
    assert [set(i) for i in items] == [{"action_id", "slug", "version", "title", "change_note",
                                        "created_at"}] * 2
    assert items[0]["action_id"] == 7 and items[0]["created_at"] == NOW.isoformat()
    assert "kind = 'publish_dashboard' AND status = 'proposed'" in pool.sql
    assert "SECRETTOKEN" not in json.dumps(items)


async def test_list_tool_output_and_empty(monkeypatch):
    async def noop(*a, **k):
        return None
    monkeypatch.setattr(dt, "_log_tool", noop)
    payload = {"slug": "inv", "version": 1, "title": "T", "change_note": "c", "draft_url": "/x?p=ZZZ"}
    pool = ListPool([_row(1, payload)])

    async def gp():
        return pool
    monkeypatch.setattr(dt, "get_pool", gp)
    dt.ctx_phone.set("ui:owner")
    out = await dt.list_dashboard_proposals.ainvoke({})
    assert "draft_url" not in out and "?p=" not in out and "preview" not in out.lower()
    assert json.loads(out)[0]["action_id"] == 1
    pool.rows = []
    assert await dt.list_dashboard_proposals.ainvoke({}) == NO_DRAFTS


def test_tool_registered_for_supervisor_only():
    assert "list_dashboard_proposals" in g.AGENT_TOOL_SUPERSET["supervisor"]
    assert "list_dashboard_proposals" not in g.AGENT_TOOL_SUPERSET["dashboard"]
    assert g._tool_registry()["list_dashboard_proposals"] is dt.list_dashboard_proposals


def test_prompt_rules(monkeypatch):
    s = composed_supervisor_prompt(monkeypatch)
    for phrase in ("in THIS turn", "even when the same or a similar request appears earlier",
                   "save_dashboard_version returned action #N", "list_dashboard_proposals"):
        assert phrase in s
    assert s.count("in THIS turn") >= 1
    assert "The result is a DRAFT awaiting the owner's approval on the Approvals page: say so" not in s
    assert s.count("otherwise say no draft was created") == 1
    assert "send_progress first" in s and "dashboard-authoring" in s


# ---- claim detection ----
@pytest.mark.parametrize("text", [
    REAL_FALSE,
    "I built a new dashboard draft for revenue.",
    "The dashboard draft is ready for your approval (#12).",
    "Draft #6 is awaiting your approval on the dashboard page.",
])
def test_claims_true(text):
    assert worker._claims_draft(text)


@pytest.mark.parametrize("text", [
    "I saved a draft of the email awaiting approval.",
    NO_DRAFTS,
    "I did not create a new dashboard draft in this turn.",
    APPROVED.replace("draft #6 was approved and published", "#6 is live"),
    CTX_REPORT,
    "Approve the playbook's dashboard suggestion on the Insights page when you are ready.",
])
def test_claims_false(text):
    assert not worker._claims_draft(text)


# ---- guard ----
async def test_non_claim_skips_db(monkeypatch):
    async def boom():
        raise AssertionError("db touched")
    monkeypatch.setattr(worker, "get_pool", boom)
    for t in (NO_DRAFTS, CTX_REPORT, "hello", ""):
        assert await worker._guard_false_draft(1, t) == t


async def test_context_pack_run_unchanged(monkeypatch):
    _patch(monkeypatch, FakePool(tools=["delegate_to_context_agent"]))
    assert await worker._guard_false_draft(1, REAL_FALSE) == REAL_FALSE


async def test_run_created_action_unchanged(monkeypatch):
    _patch(monkeypatch, FakePool(run_actions=True))
    assert await worker._guard_false_draft(1, REAL_FALSE) == REAL_FALSE


async def test_cited_proposed_unchanged(monkeypatch):
    _patch(monkeypatch, FakePool(statuses={12: "proposed"}))
    r = "The dashboard draft is ready for your approval (#12)."
    assert await worker._guard_false_draft(1, r) == r


async def test_executed_cited_as_published_unchanged(monkeypatch):
    _patch(monkeypatch, FakePool(statuses={6: "executed"}))
    r = "I built the invoices dashboard draft #6 earlier and it was approved and published."
    assert await worker._guard_false_draft(1, r) == r


async def test_executed_cited_as_awaiting_replaced(monkeypatch):
    _patch(monkeypatch, FakePool(statuses={6: "executed"}))
    out = await worker._guard_false_draft(1, "Draft #6 is awaiting your approval on the dashboard page.")
    assert out.startswith("I did not create a new dashboard draft in this turn.")


async def test_unknown_citation_replaced(monkeypatch):
    _patch(monkeypatch, FakePool())
    out = await worker._guard_false_draft(1, "The dashboard draft #99 is ready for your approval.")
    assert out.startswith("I did not create a new dashboard draft in this turn.")


async def test_real_false_reply_replaced_with_pending(monkeypatch):
    pend = [{"action_id": 9, "slug": "inv", "version": 2, "title": "Invoices", "change_note": "",
             "created_at": "x"}]
    _patch(monkeypatch, FakePool(), pend)
    out = await worker._guard_false_draft(1, REAL_FALSE, "ui:owner")
    assert out.startswith("I did not create a new dashboard draft in this turn.")
    assert "#9 inv v2: Invoices" in out and out.endswith("Ask me again and I will build it now.")
    assert "draft_url" not in out and "?p=" not in out
    assert not worker._claims_draft(out)


async def test_real_false_reply_no_pending(monkeypatch):
    _patch(monkeypatch, FakePool())
    out = await worker._guard_false_draft(1, REAL_FALSE)
    assert "There are no dashboard drafts awaiting approval." in out
    assert not worker._claims_draft(out)


async def test_db_error_fail_soft(monkeypatch):
    async def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(worker, "get_pool", boom)
    assert await worker._guard_false_draft(1, REAL_FALSE) == REAL_FALSE


# Review CR-02: non-owner threads never receive slugs or titles.
async def test_group_thread_gets_count_only(monkeypatch):
    pend = [{"action_id": 9, "slug": "inv", "version": 2, "title": "Invoices", "change_note": "",
             "created_at": "x"}]
    _patch(monkeypatch, FakePool(), pend)
    out = await worker._guard_false_draft(1, REAL_FALSE, "12036304@g.us")
    assert "inv" not in out and "Invoices" not in out and "#9" not in out
    assert "1 dashboard proposal(s)" in out


# ---- 22 release eval, request 12 ("Make the outward bars red"): an honest decline goes out ----
# The specialist declined (no colour field) and saved nothing; the supervisor relayed that,
# said no new draft was saved, mentioned the untouched v1 draft still waiting for approval and
# offered the closest option. The status line matched the "dashboard ... waiting for ...
# approval" pattern with no #N, so the guard replaced the honest reply with its canned text.
DECLINE = ("I couldn't make the outward bars red. Dashboards have no colour setting: the app picks "
           "the colours, and red is kept for bad news. Both daily charts already draw kg out and "
           "rolls out in the outward tone, so I did not save a new draft and nothing changed. "
           "Your Daily operations dashboard is still the version 1 draft, waiting for your approval.")
OFFER = ("There is no colour field for a dashboard series, so red is not possible. If you like, "
         "I can put together a draft that sets the outward tone explicitly.")


@pytest.mark.parametrize("text", [
    DECLINE,
    OFFER,
    "Want me to set up a dashboard draft with the outward tone written in?",
    "I haven\u2019t saved a new dashboard draft, because the outward tone is already the default.",
    "No new draft was saved. The daily-operations dashboard stays as the draft awaiting your review.",
])
def test_decline_and_offer_are_not_claims(text):
    assert not worker._claims_draft(text)


@pytest.mark.parametrize("text", [
    # a decline in one sentence never excuses a creation claim in another
    "I did not save a draft with red bars. I created a new dashboard draft with the outward tone, "
    "awaiting your approval.",
    # nor a status line about a "new" draft
    "I didn't save a draft with red. The new dashboard draft is awaiting your approval.",
    # the decline-then-substitute shape with no save behind it
    "I couldn't make the bars red, so I made a dashboard draft with the outward tone instead.",
    "I've put together a dashboard draft for you.",
])
def test_false_claims_next_to_a_decline_still_claim(text):
    assert worker._claims_draft(text)


async def test_honest_decline_goes_out_unchanged(monkeypatch):
    # the eval run: no save in this run, the v1 draft (#28) pending from an earlier turn
    pend = [{"action_id": 28, "slug": "daily-operations", "version": 1, "title": "Daily operations",
             "change_note": "", "created_at": "x"}]
    pool = FakePool()
    _patch(monkeypatch, pool, pend)
    for t in (DECLINE, OFFER):
        assert await worker._guard_false_draft(1, t, "ui:owner") == t
    assert pool.sql == []  # judged on the text alone, no DB round trip


async def test_creation_claim_beside_a_decline_still_replaced(monkeypatch):
    _patch(monkeypatch, FakePool())
    r = ("I did not save a draft with red bars. I created a new dashboard draft with the outward "
         "tone, awaiting your approval.")
    out = await worker._guard_false_draft(1, r, "ui:owner")
    assert out.startswith("I did not create a new dashboard draft in this turn.")

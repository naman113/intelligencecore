"""Gap closure (12-VERIFICATION gap 2, CTX-09d): the worker never reports a watch this run did not create."""

import inspect

import pytest

from app import worker

ONE_OFF = "Let me know when there are more than 5000123 open invoices."
STANDING = "Track the number of open invoices every day."


class FakePool:
    def __init__(self, tools=(), watches=None, actions=(), raises=False):
        # tools: list of (tool, result_digest); watches: {id: row dict}
        self.tools = list(tools)
        self.watches = dict(watches or {})
        self.actions = list(actions)
        self.raises = raises
        self.calls = 0

    async def fetch(self, sql, *args):
        self.calls += 1
        if self.raises:
            raise RuntimeError("db down")
        if "FROM tool_calls" in sql:
            return [{"tool": t, "result_digest": d} for t, d in self.tools]
        if "FROM watches" in sql:
            return [self.watches[i] for i in args[0] if i in self.watches]
        if "FROM actions" in sql:
            return [{"id": i} for i in self.actions]
        return []


def _w(i, title="Open invoices", status="active", thread="ui:owner", cadence=900):
    return {"id": i, "title": title, "cadence_seconds": cadence, "one_shot": True,
            "thread_id": thread, "status": status}


def _created(i):
    return ("create_watch", f"watch #{i} created: 'Open invoices' (every 15min)")


@pytest.fixture
def setup(monkeypatch):
    state = {"repairs": 0}

    def make(pool, create=None):
        async def gp():
            return pool
        monkeypatch.setattr(worker, "get_pool", gp)

        async def fake_force(run_id, user_message, intended_reply):
            state["repairs"] += 1
            if create is not None:
                pool.tools.append(_created(create))
                pool.watches[create] = _w(create)
        monkeypatch.setattr(worker, "_force_one_shot_watch", fake_force)
        return state
    return make


@pytest.mark.parametrize("reply,msg,expect", [
    ("Done! I've set up watch #11 to alert you when open invoices exceed 5,000,000.", ONE_OFF, True),
    ("**Watch #12** has been created.", "", True),
    ("I've created an alert for that.", "", True),
    ("Done! I'll let you know when there are more than 5000000 open invoices.", ONE_OFF, True),
    ("I did not set up a watch.", ONE_OFF, False),
    ("No watch was created.", ONE_OFF, False),
    ("Your watch #7 checks every 15 minutes.", "", False),
    ("Done! Watch #13 is now tracking your open invoices count daily.", STANDING, True),
    ("There are 412 open invoices.", ONE_OFF, False),
    ("I've proposed a daily KPI (action #805); it waits for your approval.", STANDING, False),
    ("I'll let you know.", "How many invoices are open?", False),
])
def test_claim_detection(reply, msg, expect):
    assert bool(worker._watch_claim_sentences(reply, msg)) is expect


async def test_verifier_fabricated_watch_repaired(setup):
    pool = FakePool()
    st = setup(pool, create=21)
    out = await worker._guard_false_watch(
        5, "Done! I've set up watch #11 to alert you.", ONE_OFF, "ui:owner")
    assert st["repairs"] == 1
    assert "#21" in out and "#11" not in out


async def test_repair_creates_nothing(setup):
    pool = FakePool()
    st = setup(pool, create=None)
    out = await worker._guard_false_watch(5, "Done! I've set up watch #11.", ONE_OFF, "ui:owner")
    assert st["repairs"] == 1
    assert out.startswith("I did not set up a watch in this turn")


async def test_misnumbered_corrected(setup):
    pool = FakePool(tools=[_created(11)], watches={11: _w(11)})
    st = setup(pool)
    out = await worker._guard_false_watch(5, "Done! I've set up watch #13.", ONE_OFF, "ui:owner")
    assert "#11" in out and "#13" not in out
    assert st["repairs"] == 0


async def test_truthful_unchanged(setup):
    pool = FakePool(tools=[_created(11)], watches={11: _w(11)})
    st = setup(pool)
    reply = "Done! I've set up watch #11 to alert you."
    assert await worker._guard_false_watch(5, reply, ONE_OFF, "ui:owner") == reply
    assert st["repairs"] == 0


async def test_two_created_correction(setup):
    pool = FakePool(tools=[_created(11), _created(12)], watches={11: _w(11), 12: _w(12)})
    setup(pool)
    out = await worker._guard_false_watch(5, "I've set up watch #99.", ONE_OFF, "ui:owner")
    assert "Correction" in out and "#11" in out and "#12" in out


async def test_proposal_run(setup):
    pool = FakePool(tools=[("propose_kpi", "proposed")], actions=[805])
    st = setup(pool)
    out = await worker._guard_false_watch(
        5, "Done! I've set up a watch for open invoices.", ONE_OFF, "ui:owner")
    assert "#805" in out and "approval" in out
    assert st["repairs"] == 0


async def test_standing_cue_correction(setup):
    pool = FakePool()
    st = setup(pool)
    out = await worker._guard_false_watch(5, "Done! I've set up watch #9.", STANDING, "ui:owner")
    assert out.startswith("I did not set up a watch in this turn")
    assert st["repairs"] == 0


async def test_context_pack_unchanged(setup):
    pool = FakePool(tools=[("read_context_pack", "x")])
    st = setup(pool)
    reply = "I've set up watch #3 candidates."
    assert await worker._guard_false_watch(5, reply, ONE_OFF, "ui:owner") == reply
    assert st["repairs"] == 0


async def test_existing_watch_status_unchanged(setup):
    pool = FakePool(watches={7: _w(7)})
    st = setup(pool)
    reply = "I already set up watch #7 for that earlier."
    assert await worker._guard_false_watch(5, reply, ONE_OFF, "ui:owner") == reply
    assert st["repairs"] == 0


async def test_non_claim_skips_pool(setup):
    pool = FakePool()
    setup(pool)
    reply = "There are 412 open invoices."
    assert await worker._guard_false_watch(5, reply, ONE_OFF, "ui:owner") == reply
    assert pool.calls == 0


async def test_pool_error_fail_soft(setup):
    pool = FakePool(raises=True)
    setup(pool)
    reply = "Done! I've set up watch #11."
    assert await worker._guard_false_watch(5, reply, ONE_OFF, "ui:owner") == reply


def test_guard_wired_after_draft_guard():
    src = inspect.getsource(worker._process_one)
    call = '_guard_false_watch(run_id, reply, msg["content"], thread_id)'
    assert call in src
    assert src.index("_guard_false_draft(") < src.index(call) < src.index(
        "whatsapp.send_message(thread_id, reply)")

"""Quick 021: a reply to "hi" is not a watch claim, and an incidental claim is taken out
of an answer instead of replacing it.

Runs 16/17 on the client copy: the greeting listed "- Set up alerts, or keep track of a
KPI" under "I can:" and the CTX-09d guard replaced the whole reply with "I did not set up
a watch in this turn ...". The three greetings below are the ones the real supervisor
wrote for a bare "hi" on the p16 replay. Pure logic, fakes only.
"""

import pytest

from app import worker

GREETINGS = [
    ("Hi! 👋 What can I help with today? For example, I can:\n"
     "- Look up numbers in the business database, like sales, orders or stock\n"
     "- Read invoices, quotations or agreements you send me\n"
     "- Set an alert or start tracking a KPI\n"
     "- Make a report, spreadsheet or chart\n"
     "- Message someone on your team for you"),
    ("Hi! 👋 What can I help you with today? For example, I can:\n"
     "- Answer questions from your business data (sales, orders, stock and so on)\n"
     "- Read an invoice, quotation or agreement you send me\n"
     "- Set up alerts, or keep track of a KPI\n"
     "- Make a report, spreadsheet or chart\n"
     "- Send a message to someone on your team"),
    ("Hi! 👋 How can I help today? I can:\n"
     "- Answer questions from your business data\n"
     "- Read invoices, quotes or agreements you send me\n"
     "- Set up alerts or track KPIs\n"
     "- Make reports, spreadsheets or charts\n"
     "- Pass messages on to colleagues"),
]
ONE_OFF = "Let me know when there are more than 5000123 open invoices."


@pytest.mark.parametrize("reply", GREETINGS)
def test_capability_greetings_are_not_claims(reply):
    assert worker._watch_claim_sentences(reply, "hi") == []


@pytest.mark.parametrize("reply", [
    "I can answer questions, set up alerts and send reports.",
    "Want me to set up an alert for that?",
    "Would you like me to set up a watch on diesel drops?",
    "Shall I set up a watch for it?",
    "If you want, I could set up an alert when stock drops below 50.",
    "I’d be happy to set up a watch for that.",
    "Things I can do:\n\n- set up alerts\n- created reports on request",
])
def test_offers_questions_and_capabilities_are_not_claims(reply):
    assert worker._watch_claim_sentences(reply, ONE_OFF) == []


@pytest.mark.parametrize("reply", [
    "Done! I've set up watch #11 to alert you.",
    "I’ve set up a watch for that.",
    "I've set up watch #11, want me to change the threshold?",
    "Set up! Your alert has been created.",
    "For example, I've set up:\n- watch #4, which is now active for open invoices",
    "I can confirm I've created an alert for that.",
    "You can relax, I set up an alert for that.",
    "Can't wait: we just set up the alert you asked for?",
    "Here is what I did:\n- Set up watch #11 for open invoices",
    "Done! I'll let you know when there are more than 5000000 open invoices.",
])
def test_real_claims_are_still_caught(reply):
    assert worker._watch_claim_sentences(reply, ONE_OFF)


class FakePool:
    async def fetch(self, sql, *args):
        return []


@pytest.fixture
def no_watch(monkeypatch):
    async def gp():
        return FakePool()

    repairs = []

    async def force(run_id, user_message, intended_reply):
        repairs.append(user_message)

    monkeypatch.setattr(worker, "get_pool", gp)
    monkeypatch.setattr(worker, "_force_one_shot_watch", force)
    return repairs


@pytest.mark.parametrize("reply", GREETINGS)
async def test_greeting_reaches_the_user_unchanged(no_watch, reply):
    assert await worker._guard_false_watch(17, reply, "hi", "15550001111") == reply


async def test_incidental_claim_is_removed_and_noted(no_watch):
    reply = ("Hi! Sales yesterday were 1,240 orders, up from 1,100. I've set up a watch on "
             "late deliveries for you. Anything else you need?")
    out = await worker._guard_false_watch(17, reply, "how were sales yesterday?", "15550001111")
    assert "I've set up a watch" not in out
    assert out.startswith("Hi! Sales yesterday were 1,240 orders, up from 1,100.")
    assert "Anything else you need?" in out
    assert out.endswith(worker.WATCH_NOTE)
    assert no_watch == []


async def test_incidental_claim_with_promise_drops_both(no_watch):
    reply = ("Stock of cement is 420 bags across 3 sites.\n"
             "I've created an alert for low stock. I'll let you know when it drops.")
    out = await worker._guard_false_watch(17, reply, "how much cement do we have", "15550001111")
    assert out == "Stock of cement is 420 bags across 3 sites.\n\n" + worker.WATCH_NOTE


async def test_claim_only_reply_still_gets_the_full_correction(no_watch):
    out = await worker._guard_false_watch(17, "Done! I've set up watch #11.", "hi", "15550001111")
    assert out.startswith("I did not set up a watch in this turn")


async def test_watch_request_keeps_the_full_correction(no_watch):
    reply = "Sure, here's the stock: 420 bags. I've set up a watch for low stock too."
    out = await worker._guard_false_watch(
        17, reply, "track cement stock every day", "15550001111")
    assert out.startswith("I did not set up a watch in this turn")
    assert no_watch == []


def test_removal_keeps_bullets_and_markdown_intact():
    reply = ("*Summary*\n- 420 bags of cement\n- 3 sites\nI've set up a watch for low stock.\n"
             "- one more line")
    assert worker._without_watch_claims(reply) == (
        "*Summary*\n- 420 bags of cement\n- 3 sites\n- one more line")

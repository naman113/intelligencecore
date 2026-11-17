"""Ask-in-group questions — triage may post ONE short clarifying question to a
case's origin group, with every guard enforced in code (mode switch, group JID
only, active chat, cooldown, length cap) and the send strictly AFTER the
verdict transaction commits. Fakes only; no DB/LLM."""

import json
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import triage

NOW = datetime.now(timezone.utc)
# A due date triage._sane_due accepts whenever the suite runs; whole seconds so the
# ISO string round-trips exactly through _parse_due.
FUTURE_DUE = (NOW + timedelta(days=30)).replace(microsecond=0)
FUTURE_DUE_ISO = FUTURE_DUE.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeQuestionPool:
    """Understands exactly the queries _ask_group_question issues."""

    def __init__(self, origin_jid, *, recent_action=False):
        self.origin_jid = origin_jid
        self.recent_action = recent_action
        self.actions = []
        self.sqls = []

    async def fetchval(self, sql, *args):
        self.sqls.append(sql)
        if "SELECT origin_chat_jid" in sql:
            return self.origin_jid
        if "FROM actions" in sql:
            # cooldown probe must scope to this case + kind + live statuses
            assert "kind = 'group_question'" in sql
            assert "'proposed','approved','executed'" in sql
            return 1 if self.recent_action else None
        return None

    async def execute(self, sql, *args):
        self.sqls.append(sql)
        if "INSERT INTO actions" in sql:
            if "'autonomous_message'" in sql:
                tier = "autonomous_message"
                status = "executed" if args[2] else "failed"
            else:
                assert "'draft', 'proposed'" in sql
                tier, status = "draft", "proposed"
            self.actions.append({"case_id": args[0],
                                 "payload": json.loads(args[1]),
                                 "tier": tier, "status": status})


def _wire(monkeypatch, pool, *, mode="autonomous", chat_active=True, send_ok=True):
    """Wire the fakes; returns the list of (jid, message) sends."""
    async def fake_get_pool():
        return pool

    monkeypatch.setattr(triage, "get_pool", fake_get_pool)
    monkeypatch.setattr(
        triage, "get_settings",
        lambda: SimpleNamespace(group_question_mode=mode))

    sent = []

    async def fake_is_active(jid):
        return chat_active

    async def fake_send(jid, message):
        sent.append((jid, message))
        return send_ok

    import app.chats
    import app.whatsapp
    monkeypatch.setattr(app.chats, "is_active", fake_is_active)
    monkeypatch.setattr(app.whatsapp, "send_message", fake_send)
    return sent


# ── guards: silence in every blocked configuration ─────────────────────────

async def test_dm_origin_case_never_gets_a_question(monkeypatch):
    pool = FakeQuestionPool("919999999999@s.whatsapp.net")
    sent = _wire(monkeypatch, pool)
    await triage._ask_group_question(1, "What's the due date?")
    assert sent == [] and pool.actions == []


async def test_no_origin_jid_stays_silent(monkeypatch):
    pool = FakeQuestionPool(None)
    sent = _wire(monkeypatch, pool)
    await triage._ask_group_question(1, "What's the due date?")
    assert sent == [] and pool.actions == []


async def test_inactive_chat_stays_silent(monkeypatch):
    pool = FakeQuestionPool("123@g.us")
    sent = _wire(monkeypatch, pool, chat_active=False)
    await triage._ask_group_question(1, "What's the due date?")
    assert sent == [] and pool.actions == []


async def test_cooldown_blocks_a_second_question(monkeypatch):
    pool = FakeQuestionPool("123@g.us", recent_action=True)
    sent = _wire(monkeypatch, pool)
    await triage._ask_group_question(1, "What's the due date?")
    assert sent == [] and pool.actions == []


async def test_mode_off_disables_everything(monkeypatch):
    pool = FakeQuestionPool("123@g.us")
    sent = _wire(monkeypatch, pool, mode="off")
    await triage._ask_group_question(1, "What's the due date?")
    assert sent == [] and pool.actions == [] and pool.sqls == []


async def test_blank_question_is_dropped(monkeypatch):
    pool = FakeQuestionPool("123@g.us")
    sent = _wire(monkeypatch, pool)
    await triage._ask_group_question(1, "   ")
    assert sent == [] and pool.actions == []


async def test_pool_failure_is_fail_soft(monkeypatch):
    class BoomPool:
        async def fetchval(self, sql, *args):
            raise RuntimeError("db down")

    sent = _wire(monkeypatch, BoomPool())
    await triage._ask_group_question(1, "What's the due date?")   # must not raise
    assert sent == []


# ── modes ──────────────────────────────────────────────────────────────────

async def test_autonomous_sends_and_records_executed(monkeypatch):
    pool = FakeQuestionPool("123@g.us")
    sent = _wire(monkeypatch, pool)
    await triage._ask_group_question(5, "  What's the due date for INV-2041?  ")

    assert sent == [("123@g.us", "What's the due date for INV-2041?")]
    assert pool.actions == [{
        "case_id": 5,
        "payload": {"chat": "123@g.us", "body": "What's the due date for INV-2041?",
                    "kind": "question"},
        "tier": "autonomous_message", "status": "executed"}]


async def test_autonomous_failed_send_records_failed(monkeypatch):
    pool = FakeQuestionPool("123@g.us")
    sent = _wire(monkeypatch, pool, send_ok=False)
    await triage._ask_group_question(5, "What's the due date?")

    assert len(sent) == 1
    assert pool.actions[0]["status"] == "failed"
    assert pool.actions[0]["tier"] == "autonomous_message"


async def test_autonomous_raising_send_still_records_failed(monkeypatch):
    """A connector timeout RAISES out of send_message; the audit row must land
    anyway (a timed-out send may have gone through)."""
    pool = FakeQuestionPool("123@g.us")
    _wire(monkeypatch, pool)

    async def boom(jid, message):
        raise TimeoutError("connector hung")

    import app.whatsapp
    monkeypatch.setattr(app.whatsapp, "send_message", boom)
    await triage._ask_group_question(5, "What's the due date?")   # must not raise
    assert pool.actions and pool.actions[0]["status"] == "failed"


async def test_proposed_mode_records_row_without_sending(monkeypatch):
    pool = FakeQuestionPool("123@g.us")
    sent = _wire(monkeypatch, pool, mode="proposed")
    await triage._ask_group_question(5, "What's the due date?")

    assert sent == []
    assert pool.actions == [{
        "case_id": 5,
        "payload": {"chat": "123@g.us", "body": "What's the due date?",
                    "kind": "question"},
        "tier": "draft", "status": "proposed"}]


async def test_unknown_mode_fails_toward_proposed(monkeypatch):
    pool = FakeQuestionPool("123@g.us")
    sent = _wire(monkeypatch, pool, mode="banana")
    await triage._ask_group_question(5, "What's the due date?")
    assert sent == []
    assert pool.actions[0]["status"] == "proposed"


async def test_question_capped_at_300_chars(monkeypatch):
    pool = FakeQuestionPool("123@g.us")
    sent = _wire(monkeypatch, pool)
    await triage._ask_group_question(5, "x" * 400)

    assert len(sent[0][1]) == triage.GROUP_QUESTION_MAX_CHARS == 300
    assert len(pool.actions[0]["payload"]["body"]) == 300


# ── expectations_updated (the answer flowing back) ─────────────────────────

class UpdateConn:
    """fetchval routes: case lookup echoes the id; the people-registry alias
    query resolves 'vinu' to person 42; everything else misses."""

    def __init__(self):
        self.executed = []

    async def fetchval(self, sql, *args):
        if "SELECT id FROM cases WHERE id" in sql:
            return args[0]
        if "FROM people" in sql and args and args[0] == "vinu":
            return 42
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def _upd_event(content="due date is Sep 15"):
    return {"id": 10, "source": "whatsapp", "content": content,
            "actor_id": None, "thread_ref": "123@g.us"}


def _upd_verdict(updates):
    return {"relevant": True, "case": 5, "participants": [],
            "expectations_created": [], "expectations_satisfied": [],
            "expectations_updated": updates, "reason": "answers the question"}


async def test_expectations_updated_carries_status_and_case_guard():
    conn = UpdateConn()
    verdict = _upd_verdict([
        {"id": 9, "due_at": FUTURE_DUE_ISO},
        {"id": "abc", "due_at": FUTURE_DUE_ISO},  # bad id
        {"id": 11, "due_at": "not-a-date"},               # bad date, no owner
        "nonsense",                                       # not a dict
    ])

    assert await triage._apply_verdict(conn, _upd_event(), verdict) == 5

    upd = [(s, a) for s, a in conn.executed if "SET due_at" in s]
    assert len(upd) == 1                       # garbage entries all ignored
    sql, args = upd[0]
    # the model can only touch a LIVE expectation on THIS case — and a late
    # answer must be able to reach a violated one, so both statuses qualify
    assert "status IN ('open', 'violated')" in sql and "case_id = $4" in sql
    assert args[0] == 9 and args[3] == 5
    assert args[1] == FUTURE_DUE


async def test_expectations_updated_discards_past_due_dates():
    """The triage model has emitted LAST year's date for 'before this weekend'
    — accepted, it makes the expectation born-violated and fires an 'overdue'
    escalation minutes later. Past dates are dropped in code."""
    conn = UpdateConn()
    verdict = _upd_verdict([{"id": 9, "due_at": "2025-08-14T20:00:00Z"}])
    await triage._apply_verdict(conn, _upd_event(), verdict)
    assert not any("SET due_at" in s for s, _ in conn.executed)


async def test_expectations_updated_can_assign_owner_alone():
    """'Vinu has to raise the invoice' carries no date — the owner must still
    land (resolved through the people registry, never guessed)."""
    conn = UpdateConn()
    verdict = _upd_verdict([{"id": 9, "due_at": None, "owner": "vinu"}])
    await triage._apply_verdict(conn, _upd_event("I think Vinu has to raise it"), verdict)

    upd = [(s, a) for s, a in conn.executed if "SET due_at" in s]
    assert len(upd) == 1
    sql, args = upd[0]
    assert args[1] is None and args[2] == 42   # no due change, owner set
    assert "COALESCE($2, due_at)" in sql       # null leaves the old value alone
    assert "COALESCE($3, owner_id)" in sql


async def test_unresolvable_owner_without_date_changes_nothing():
    conn = UpdateConn()
    verdict = _upd_verdict([{"id": 9, "owner": "someone unknown"}])
    await triage._apply_verdict(conn, _upd_event(), verdict)
    assert not any("SET due_at" in s for s, _ in conn.executed)


async def test_fresh_future_due_reopens_a_violated_expectation():
    """A corrected deadline is the recovery path from a false 'overdue': the
    row flips back to open so reminders re-arm against the new date."""
    conn = UpdateConn()
    verdict = _upd_verdict([{"id": 9, "due_at": FUTURE_DUE_ISO}])
    await triage._apply_verdict(conn, _upd_event(), verdict)
    sql, _ = next((s, a) for s, a in conn.executed if "SET due_at" in s)
    assert "CASE WHEN $2 IS NOT NULL THEN 'open'" in sql


async def test_expectations_updated_skipped_without_a_case():
    executed = []

    class Conn:
        async def fetchval(self, sql, *args):
            return None          # db event, case 'none' → no case resolves

        async def execute(self, sql, *args):
            executed.append(sql)

    event = {"id": 10, "source": "db", "content": "row changed",
             "actor_id": None, "thread_ref": None}
    verdict = {"relevant": True, "case": "none",
               "expectations_updated": [{"id": 9, "due_at": FUTURE_DUE_ISO}],
               "reason": "x"}

    assert await triage._apply_verdict(Conn(), event, verdict) is None
    assert not any("SET due_at" in s for s in executed)


# ── batch wiring: questions go out only after the verdict tx commits ───────

async def test_question_sent_after_transaction_commits(monkeypatch):
    order = []

    class BatchConn:
        def transaction(self):
            class Tx:
                async def __aenter__(self_tx):
                    order.append("tx_enter")

                async def __aexit__(self_tx, *exc):
                    order.append("tx_exit")
                    return False
            return Tx()

        async def fetch(self, sql, *args):
            if "triage_status = 'pending'" in sql:
                return [{"id": 1, "source": "whatsapp",
                         "content": "we need to pay INV-2041",
                         "actor_id": None, "actor_raw": "vinu",
                         "occurred_at": NOW, "thread_ref": "123@g.us",
                         "actor_name": None}]
            return []            # open-case / open-expectation indexes

        async def fetchval(self, sql, *args):
            if "INSERT INTO cases" in sql:
                return 55
            return None

        async def execute(self, sql, *args):
            if "UPDATE events SET triage_status" in sql:
                order.append("status_update")

    class BatchPool:
        def acquire(self):
            class Ctx:
                async def __aenter__(self_c):
                    return BatchConn()

                async def __aexit__(self_c, *exc):
                    return False
            return Ctx()

    async def fake_get_pool():
        return BatchPool()

    monkeypatch.setattr(triage, "get_pool", fake_get_pool)

    # _triage_batch imports agents.graph for callbacks — stub the heavy module
    stub = types.ModuleType("app.agents.graph")
    stub._callbacks = lambda: {}
    monkeypatch.setitem(sys.modules, "app.agents.graph", stub)

    verdict = {"verdicts": [{
        "event_id": 1, "relevant": True, "case": "new",
        "new_case_title": "Pay INV-2041", "participants": [],
        "expectations_created": [], "expectations_satisfied": [],
        "group_question": "What's the due date for INV-2041?",
        "reason": "payment obligation"}]}

    class FakeModel:
        async def ainvoke(self, msgs, config=None):
            # the model must be told what date it is — a cheap model without
            # an explicit anchor has emitted last year's dates
            payload = json.loads(msgs[1][1])
            assert "now" in payload
            return SimpleNamespace(content=json.dumps(verdict))

    monkeypatch.setattr(triage, "_model", lambda: FakeModel())

    asked = []

    async def fake_ask(case_id, question, kind="question"):
        order.append("ask")
        asked.append((case_id, question))

    async def noop():
        pass

    monkeypatch.setattr(triage, "_ask_group_question", fake_ask)
    monkeypatch.setattr(triage, "_refresh_summaries", noop)
    monkeypatch.setattr(triage, "_complete_finished_cases", noop)

    assert await triage._triage_batch() == 1

    assert asked == [(55, "What's the due date for INV-2041?")]
    # the question goes out strictly AFTER the tx committed + status recorded
    assert order == ["tx_enter", "tx_exit", "status_update", "ask"]

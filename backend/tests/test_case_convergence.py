"""Cross-channel case convergence: triage thread context, case notes, the
owner-company orientation, and the reactive open-cases context block.

The incident under test: invoice TC-006 was discussed in a group ("How much
is pending?" → "Almost 18"), in a DM, and via a docintel reading — and the
system failed to converge them: triage judged the terse group replies
irrelevant (no context), the ₹18L datum was stored nowhere, and the DM
answer couldn't see the case. These tests pin the mechanisms that fix it.

Patterns follow test_group_questions.py (UpdateConn / BatchConn / FakeModel /
graph-module stub)."""

import inspect
import json
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import cases_util, contextassembler, triage

NOW = datetime(2026, 8, 11, 22, 0, tzinfo=timezone.utc)


# ── TRIAGE_SYSTEM prompt pins ─────────────────────────────────────────────

def test_prompt_teaches_continuations_and_case_notes():
    p = triage.TRIAGE_SYSTEM
    assert "CONTINUATIONS" in p, "terse-reply rule missing"
    assert "recent_thread_context" in p
    assert "case_note" in p, "verdict schema must include case_note"
    assert "CASE NOTES" in p
    assert "NEVER sharpen vague statements" in p, \
        "notes must not invent precision ('Almost 18' stays approximate)"
    assert "owner_company" in p
    assert "COLLECT" in p, "receivable/payable orientation rule missing"
    assert "ONE case" in p, "cross-route same-subject rule missing"


# ── _thread_label / _fmt_event ────────────────────────────────────────────

def _ev(id=1, source="whatsapp", thread_ref="123@g.us", **kw):
    e = {"id": id, "source": source, "content": "Almost 18",
         "actor_id": None, "actor_raw": "919876", "actor_name": None,
         "occurred_at": NOW, "thread_ref": thread_ref}
    e.update(kw)
    return e


def test_thread_label_shapes():
    assert triage._thread_label(_ev(chat_subject="Northwind Invoicing")) == \
        "group:Northwind Invoicing"
    assert triage._thread_label(_ev(thread_ref="123@g.us")) == "group:123@g.us"
    assert triage._thread_label(_ev(source="db", thread_ref="machines")) == "db"
    assert triage._thread_label(
        _ev(source="email", thread_ref="inv-thread-9")) == "email:inv-thread-9"
    assert triage._thread_label(
        _ev(thread_ref="919876", actor_name="Vinayak")) == "dm:Vinayak"
    assert triage._thread_label(_ev(thread_ref=None)) is None


def test_fmt_event_carries_thread_and_context():
    ctx = {"123@g.us": ["Naman: How much is pending ?", "Vinayak: Almost 18"]}
    out = triage._fmt_event(_ev(), ctx)
    assert out["thread"] == "group:123@g.us"
    assert out["recent_thread_context"] == ctx["123@g.us"]


def test_fmt_event_omits_empty_context_and_thread():
    out = triage._fmt_event(_ev(thread_ref=None), {})
    assert "recent_thread_context" not in out
    assert "thread" not in out
    # the original fields are unchanged
    assert out["event_id"] == 1 and out["content"] == "Almost 18"


# ── _thread_context ───────────────────────────────────────────────────────

class CtxConn:
    """fetch returns a scripted per-thread history and records queries."""

    def __init__(self, rows_by_ref):
        self.rows_by_ref = rows_by_ref
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rows_by_ref.get(args[0], [])


async def test_thread_context_one_query_per_thread_any_provenance():
    rows = [  # newest-first, as the query returns them
        {"id": 9, "content": "Almost 18", "provenance": "external", "actor": "Vinayak"},
        {"id": 8, "content": "How much is pending ?", "provenance": "self", "actor": None},
    ]
    conn = CtxConn({"123@g.us": rows})
    events = [_ev(id=10), _ev(id=11), _ev(id=12, thread_ref=None)]

    out = await triage._thread_context(conn, events)

    assert len(conn.calls) == 1, "one query per DISTINCT thread_ref"
    sql, args = conn.calls[0]
    assert "provenance" not in sql.split("WHERE")[1].split("ORDER")[0], \
        "context must include ANY provenance — the assistant's sends ARE the conversation"
    assert set(args[1]) == {10, 11, 12}, "batch's own events excluded from context"
    # rendered oldest-first; self provenance labeled 'assistant'
    assert out["123@g.us"] == ["assistant: How much is pending ?",
                               "Vinayak: Almost 18"]


async def test_thread_context_caps_line_length():
    rows = [{"id": 1, "content": "x" * 999, "provenance": "external", "actor": "A"}]
    out = await triage._thread_context(CtxConn({"t": rows}), [_ev(id=5, thread_ref="t")])
    line = out["t"][0]
    assert len(line) <= triage.THREAD_CONTEXT_LINE_CAP + len("A: ")


async def test_thread_context_failure_is_soft():
    class Boom:
        async def fetch(self, *a):
            raise RuntimeError("db down")
    out = await triage._thread_context(Boom(), [_ev()])
    assert out == {}


# ── case_note application in _apply_verdict ───────────────────────────────

class NoteConn:
    def __init__(self):
        self.executed = []
        self.fetchvals = []

    async def fetchval(self, sql, *args):
        self.fetchvals.append((sql, args))
        if "SELECT id FROM cases WHERE id" in sql:
            return args[0]
        if "INSERT INTO events" in sql:
            return 777          # the synthetic note event id
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"  # command tag, so the real add_alias can run through


def _note_verdict(note, case=19, **extra):
    v = {"relevant": True, "case": case, "participants": [],
         "expectations_created": [], "expectations_satisfied": [],
         "expectations_updated": [], "case_note": note, "reason": "advance"}
    v.update(extra)
    return v


async def test_case_note_lands_via_shared_helper():
    conn = NoteConn()
    await triage._apply_verdict(conn, _ev(id=136), _note_verdict("≈₹18 lakh reported pending (group message)"))

    ins = [(s, a) for s, a in conn.fetchvals if "INSERT INTO events" in s]
    assert len(ins) == 1
    sql, args = ins[0]
    # the anti-feedback-loop invariant, pinned in the SQL itself
    assert "'self'" in sql and "'skipped'" in sql
    assert args[0] == "≈₹18 lakh reported pending (group message)"
    link = [(s, a) for s, a in conn.executed if "INSERT INTO case_events" in s and a[1] == 777]
    assert len(link) == 1 and link[0][1][2] == "triage"
    assert any("events_since_summary" in s for s, a in conn.executed
               if "UPDATE cases" in s), "note must feed the rolling summary"


async def test_no_note_no_write():
    conn = NoteConn()
    await triage._apply_verdict(conn, _ev(id=1), _note_verdict(None))
    await triage._apply_verdict(conn, _ev(id=2), _note_verdict("   "))
    assert not any("INSERT INTO events" in s for s, _ in conn.fetchvals)


async def test_db_event_may_note_existing_case_but_never_open_one():
    """is_db blocks new cases/expectations — a note on an EXISTING case is the
    one thing a db row may contribute beyond satisfying expectations."""
    conn = NoteConn()
    v = _note_verdict("payment row appeared for TC-006", case=19)
    await triage._apply_verdict(conn, _ev(source="db"), v)
    assert any("INSERT INTO events" in s for s, _ in conn.fetchvals)

    conn2 = NoteConn()
    v2 = _note_verdict("junk", case="new")
    v2["new_case_title"] = "db case"
    assert await triage._apply_verdict(conn2, _ev(source="db"), v2) is None
    assert not any("INSERT INTO cases" in s for s, _ in conn2.fetchvals)


# ── case_facts + aliases application in _apply_verdict ────────────────────

def test_prompt_teaches_case_facts_and_aliases():
    p = triage.TRIAGE_SYSTEM
    assert "CASE FACTS" in p, "trackable-value rule missing"
    assert "SUPERSEDES" in p, "the model must know a new value replaces the old"
    assert "case_facts" in p, "verdict schema must include case_facts"
    assert "aliases" in p, "verdict schema + matching rule must mention aliases"


async def test_facts_and_aliases_land_via_shared_helpers():
    """The verdict's case_facts/aliases run the REAL cases_util helpers —
    pinned by the SQL they issue, with the event as the fact's provenance.
    Spoken by the OWNER (actor 42 == owner_pid): the one chat speaker whose
    numbers pass the owner-validation gate."""
    conn = NoteConn()
    v = _note_verdict(None,
                      case_facts=[{"field": "amount_pending", "value": "≈₹18 lakh"}],
                      aliases=["TC-006"])
    await triage._apply_verdict(conn, _ev(id=136, actor_id=42), v, owner_pid=42)

    ins = [(s, a) for s, a in conn.fetchvals if "INSERT INTO case_facts" in s]
    assert len(ins) == 1
    _, args = ins[0]
    assert args[0] == 19 and args[1] == "amount_pending"
    assert args[2] == "≈₹18 lakh"
    assert args[3] == 136, "source_event_id must be the triaged event"

    ali = [(s, a) for s, a in conn.executed
           if "UPDATE cases SET aliases" in s]
    assert len(ali) == 1
    sql, aargs = ali[0]
    assert "cardinality(aliases) < 12" in sql, "growth cap lives in the SQL"
    assert aargs == (19, "TC-006")


async def test_garbage_facts_and_aliases_skipped_and_capped():
    """Model output is untrusted: non-dict facts and non-str aliases are
    skipped, and only the FIRST 3 entries of each list are even considered."""
    conn = NoteConn()
    v = _note_verdict(None,
                      case_facts=[{"field": "amount_pending", "value": "18"},
                                  "not a dict",
                                  {"field": "due_date", "value": "Sep 15"},
                                  {"field": "beyond_cap", "value": "x"}],
                      aliases=["TC-006", 42, "SS-19", "beyond-cap"])
    await triage._apply_verdict(conn, _ev(id=1, actor_id=42), v, owner_pid=42)

    fields = [a[1] for s, a in conn.fetchvals if "INSERT INTO case_facts" in s]
    assert fields == ["amount_pending", "due_date"], \
        "non-dict skipped; 4th entry truncated before consideration"
    aliases = [a[1] for s, a in conn.executed if "UPDATE cases SET aliases" in s]
    assert aliases == ["TC-006", "SS-19"]


async def test_db_event_may_record_facts_on_existing_case():
    """NOT gated on is_db, deliberately: a payment row appearing IS the
    correction of payment_status, even though a db row can't open a case.
    Also pins that machine sources pass the owner gate with owner_pid unset."""
    conn = NoteConn()
    v = _note_verdict(None, case_facts=[{"field": "payment_status",
                                         "value": "paid"}])
    await triage._apply_verdict(conn, _ev(source="db"), v)
    assert any("INSERT INTO case_facts" in s for s, _ in conn.fetchvals)


# ── owner validation gate (who may move a fact) ───────────────────────────
# The owner's standing rule: "whenever anyone quotes a commercial number or
# any item with business value in the group — validate it by me in the group
# before adding it as a fact." Enforced in code, not the prompt:
# _apply_verdict drops case_facts from non-owner chat actors; the claim
# survives as a note. E2E actors writing fictional payments hit this gate.


def test_apply_verdict_owner_pid_is_keyword_with_default():
    """Existing callers (and tests) pass no owner_pid — the gate must default
    closed (None) without breaking them."""
    p = inspect.signature(triage._apply_verdict).parameters
    assert "owner_pid" in p and p["owner_pid"].default is None


async def test_non_owner_facts_are_dropped_but_note_lands():
    conn = NoteConn()
    v = _note_verdict("claimed by Vinayak: ≈₹18 lakh pending, awaiting confirmation",
                      case_facts=[{"field": "amount_pending", "value": "≈₹18 lakh"}])
    await triage._apply_verdict(conn, _ev(id=136, actor_id=7), v, owner_pid=42)
    assert not any("INSERT INTO case_facts" in s for s, _ in conn.fetchvals), \
        "a non-owner's number is a CLAIM, never a fact"
    assert any("INSERT INTO events" in s for s, _ in conn.fetchvals), \
        "the claim still lands as a note awaiting confirmation"


async def test_owner_stated_facts_pass_the_gate():
    conn = NoteConn()
    v = _note_verdict(None, case_facts=[{"field": "amount_pending",
                                         "value": "≈₹18 lakh"}])
    await triage._apply_verdict(conn, _ev(id=136, actor_id=42), v, owner_pid=42)
    assert any("INSERT INTO case_facts" in s for s, _ in conn.fetchvals)


async def test_unset_owner_number_fails_toward_safety():
    """owner_pid None (owner_number unconfigured): chat-sourced facts are
    ALWAYS dropped — even from a registered actor. Never fail open."""
    conn = NoteConn()
    v = _note_verdict(None, case_facts=[{"field": "amount_pending",
                                         "value": "18 lakh"}])
    await triage._apply_verdict(conn, _ev(id=136, actor_id=42), v)
    assert not any("INSERT INTO case_facts" in s for s, _ in conn.fetchvals)


@pytest.mark.parametrize("source", ["db", "docintel", "system"])
async def test_system_sources_bypass_the_owner_gate(source):
    """Machine sources carry their own provenance; the gate is for chat."""
    conn = NoteConn()
    v = _note_verdict(None, case_facts=[{"field": "payment_status",
                                         "value": "paid"}])
    await triage._apply_verdict(conn, _ev(id=9, source=source, actor_id=None), v)
    assert any("INSERT INTO case_facts" in s for s, _ in conn.fetchvals)


async def test_aliases_need_a_registered_actor():
    """An unresolved chat sender can't rename what a case answers to — but a
    registered NON-owner can (aliases are matching aids, not commercial
    facts, so the bar is registration, not ownership)."""
    conn = NoteConn()
    await triage._apply_verdict(conn, _ev(id=1, actor_id=None),
                                _note_verdict(None, aliases=["TC-006"]))
    assert not any("UPDATE cases SET aliases" in s for s, _ in conn.executed)

    conn2 = NoteConn()
    await triage._apply_verdict(conn2, _ev(id=2, actor_id=7),
                                _note_verdict(None, aliases=["TC-006"]),
                                owner_pid=42)
    assert any("UPDATE cases SET aliases" in s for s, _ in conn2.executed)


async def test_docintel_aliases_land_without_an_actor():
    conn = NoteConn()
    await triage._apply_verdict(conn, _ev(id=3, source="docintel", actor_id=None),
                                _note_verdict(None, aliases=["INV-2041"]))
    assert any("UPDATE cases SET aliases" in s for s, _ in conn.executed)


def test_prompt_teaches_owner_validation_and_credence():
    p = triage.TRIAGE_SYSTEM
    assert "OWNER VALIDATION" in p, "the owner's standing rule must be taught"
    assert "CREDENCE" in p, \
        "questions/jokes/rumors/hypotheticals must not move money fields"
    assert "question_kind" in p and "fact_confirm" in p, \
        "the verdict schema must let triage ask the owner to confirm"
    assert "owner_name" in p, "the payload documents who the owner is"
    assert "claimed" in p.lower(), \
        "non-owner numbers are recorded as claims awaiting confirmation"


async def test_open_cases_index_carries_aliases():
    class Rec:
        def __init__(self):
            self.sql = None

        async def fetch(self, sql, *args):
            self.sql = sql
            return []

    conn = Rec()
    await triage._open_cases_index(conn)
    assert "c.aliases" in conn.sql, \
        "the triage payload must show aliases or the model can't match on them"


# ── cases_util.add_case_note ──────────────────────────────────────────────

async def test_add_case_note_noop_on_empty():
    class Never:
        async def fetchval(self, *a):
            raise AssertionError("must not write")
    assert await cases_util.add_case_note(Never(), 5, "") is None
    assert await cases_util.add_case_note(Never(), 0, "text") is None


async def test_add_case_note_caps_content():
    conn = NoteConn()
    await cases_util.add_case_note(conn, 5, "y" * 5000)
    _, args = next((s, a) for s, a in conn.fetchvals if "INSERT INTO events" in s)
    assert len(args[0]) == cases_util.NOTE_CONTENT_CAP
    conn2 = NoteConn()
    await cases_util.add_case_note(conn2, 5, "y" * 5000, cap=8000)
    _, args2 = next((s, a) for s, a in conn2.fetchvals if "INSERT INTO events" in s)
    assert len(args2[0]) == 5000


async def test_add_case_note_writes_meta():
    conn = NoteConn()
    meta = {"origin": "exploration", "title": "t"}
    await cases_util.add_case_note(conn, 5, "x", kind="exploration", meta=meta)
    _, args = next((s, a) for s, a in conn.fetchvals if "INSERT INTO events" in s)
    assert args[0] == "x"
    assert json.loads(args[1]) == meta
    link = [a for s, a in conn.executed if "INSERT INTO case_events" in s]
    assert link and link[0][2] == "exploration"


async def test_add_case_note_meta_defaults_to_empty_object():
    conn = NoteConn()
    await cases_util.add_case_note(conn, 5, "x")
    _, args = next((s, a) for s, a in conn.fetchvals if "INSERT INTO events" in s)
    assert json.loads(args[1]) == {}


async def test_investigation_records_through_shared_helper(monkeypatch):
    """_record_finding must delegate — a second INSERT path would fork the
    invariant."""
    from app import investigation
    calls = []

    async def fake_note(conn, case_id, text, kind="note", cap=None):
        calls.append((case_id, text, kind, cap))

    monkeypatch.setattr(cases_util, "add_case_note", fake_note)

    class Conn:
        def transaction(self):
            class Tx:
                async def __aenter__(self_t): return None
                async def __aexit__(self_t, *e): return False
            return Tx()

    class Pool:
        def acquire(self):
            class Ctx:
                async def __aenter__(self_c): return Conn()
                async def __aexit__(self_c, *e): return False
            return Ctx()

    async def fake_pool():
        return Pool()
    monkeypatch.setattr(investigation, "get_pool", fake_pool)

    tok = investigation.ctx_case_id.set(31)
    try:
        await investigation._record_finding("finding text")
    finally:
        investigation.ctx_case_id.reset(tok)
    assert calls == [(31, "finding text", "investigation", 8000)]


# ── batch payload: owner_company + context wiring ─────────────────────────

async def test_batch_payload_carries_owner_company_and_context(monkeypatch):
    captured = {}

    class Conn:
        def transaction(self):
            class Tx:
                async def __aenter__(self_t): return None
                async def __aexit__(self_t, *e): return False
            return Tx()

        async def fetch(self, sql, *args):
            if "triage_status = 'pending'" in sql:
                return [dict(_ev(id=136, chat_subject="Northwind Invoicing"))]
            if "thread_ref = $1" in sql:
                return [{"id": 9, "content": "How much is pending ?",
                         "provenance": "external", "actor": "Naman"}]
            return []

        async def fetchval(self, sql, *args):
            return None

        async def execute(self, sql, *args):
            pass

    class Pool:
        def acquire(self):
            class Ctx:
                async def __aenter__(self_c): return Conn()
                async def __aexit__(self_c, *e): return False
            return Ctx()

    async def fake_pool():
        return Pool()
    monkeypatch.setattr(triage, "get_pool", fake_pool)
    monkeypatch.setattr(triage, "get_settings",
                        lambda: SimpleNamespace(company_name="Northwind",
                                                owner_number="",
                                                test_threads=""))

    stub = types.ModuleType("app.agents.graph")
    stub._callbacks = lambda: {}
    monkeypatch.setitem(sys.modules, "app.agents.graph", stub)

    class FakeModel:
        async def ainvoke(self, msgs, config=None):
            captured.update(json.loads(msgs[1][1]))
            return SimpleNamespace(content=json.dumps({"verdicts": []}))
    monkeypatch.setattr(triage, "_model", lambda: FakeModel())

    async def noop():
        pass
    monkeypatch.setattr(triage, "_refresh_summaries", noop)
    monkeypatch.setattr(triage, "_complete_finished_cases", noop)

    assert await triage._triage_batch() == 1
    assert captured["owner_company"] == "Northwind"
    assert "owner_name" not in captured, \
        "no owner configured -> no owner_name key to mislead the model"
    ev = captured["events"][0]
    assert ev["thread"] == "group:Northwind Invoicing"
    assert ev["recent_thread_context"] == ["Naman: How much is pending ?"]


# ── batch wiring: owner resolution + question_kind forwarding ─────────────


class OwnerBatchPool:
    """Batch-shaped pool double (see test_group_questions.BatchConn): pending
    events, INSERT INTO cases -> 55, and a people-name lookup answering the
    owner_name fetch however it is phrased (fetchval or fetchrow)."""

    def __init__(self, events):
        self.events = events
        self.conn = self._make_conn()

    def _make_conn(self):
        events = self.events

        class Conn:
            def transaction(self):
                class Tx:
                    async def __aenter__(self_t):
                        return None

                    async def __aexit__(self_t, *e):
                        return False
                return Tx()

            async def fetch(self, sql, *args):
                if "triage_status = 'pending'" in sql:
                    return [dict(e) for e in events]
                return []

            async def fetchval(self, sql, *args):
                if "INSERT INTO cases" in sql:
                    return 55
                if "people" in sql:
                    return "Naman"
                return None

            async def fetchrow(self, sql, *args):
                return {"id": 42, "name": "Naman"}

            async def execute(self, sql, *args):
                return "UPDATE 1"
        return Conn()

    def acquire(self):
        conn = self.conn

        class Ctx:
            async def __aenter__(self_c):
                return conn

            async def __aexit__(self_c, *e):
                return False
        return Ctx()


def _wire_batch(monkeypatch, pool, verdicts, **settings_kw):
    """Wire pool/settings/model stubs; returns the payload the model saw."""
    async def fake_pool():
        return pool
    monkeypatch.setattr(triage, "get_pool", fake_pool)

    import app.config as config
    ns = SimpleNamespace(**settings_kw)
    monkeypatch.setattr(triage, "get_settings", lambda: ns)
    monkeypatch.setattr(config, "get_settings", lambda: ns)

    stub = types.ModuleType("app.agents.graph")
    stub._callbacks = lambda: {}
    monkeypatch.setitem(sys.modules, "app.agents.graph", stub)

    captured = {}

    class FakeModel:
        async def ainvoke(self, msgs, config=None):
            captured.update(json.loads(msgs[1][1]))
            return SimpleNamespace(content=json.dumps({"verdicts": verdicts}))
    monkeypatch.setattr(triage, "_model", lambda: FakeModel())

    async def noop():
        pass
    monkeypatch.setattr(triage, "_refresh_summaries", noop)
    monkeypatch.setattr(triage, "_complete_finished_cases", noop)
    return captured


async def test_batch_resolves_owner_once_and_passes_owner_pid(monkeypatch):
    """One people.resolve per BATCH (not per event), its pid handed to every
    _apply_verdict, and the resolved owner_name in the payload so the model
    knows whose statements are authoritative."""
    pool = OwnerBatchPool([_ev(id=1), _ev(id=2)])
    verdicts = [{"event_id": 1, "relevant": True, "case": 5, "reason": "x"},
                {"event_id": 2, "relevant": True, "case": 5, "reason": "x"}]
    captured = _wire_batch(monkeypatch, pool, verdicts,
                           company_name="", owner_number="919000000101",
                           test_threads="")

    resolve_calls = []
    import app.people as people_mod

    async def fake_resolve(conn, **kw):
        resolve_calls.append(kw)
        return 42
    monkeypatch.setattr(people_mod, "resolve", fake_resolve)

    applied = []

    async def fake_apply(conn, event, v, owner_pid=None):
        applied.append(owner_pid)
        return None
    monkeypatch.setattr(triage, "_apply_verdict", fake_apply)

    assert await triage._triage_batch() == 2
    assert len(resolve_calls) == 1, "owner resolves ONCE per batch"
    assert resolve_calls[0].get("phone") == "919000000101"
    assert applied == [42, 42], "every verdict application gets the owner pid"
    assert captured.get("owner_name") == "Naman"


@pytest.mark.parametrize("question_kind,expected", [
    ("fact_confirm", "fact_confirm"),
    ("banana", "question"),          # unknown values coerce to the safe kind
    (None, "question"),              # absent field defaults
])
async def test_batch_forwards_question_kind(monkeypatch, question_kind, expected):
    pool = OwnerBatchPool([_ev(id=1)])
    v = {"event_id": 1, "relevant": True, "case": "new",
         "new_case_title": "Confirm TC-006 pending amount",
         "group_question": "Confirming: ≈₹18 lakh pending on TC-006 — correct?",
         "reason": "x"}
    if question_kind is not None:
        v["question_kind"] = question_kind
    _wire_batch(monkeypatch, pool, [v], company_name="", owner_number="",
                test_threads="")

    asked = []

    async def fake_ask(case_id, question, kind="question"):
        asked.append((case_id, question, kind))
    monkeypatch.setattr(triage, "_ask_group_question", fake_ask)

    assert await triage._triage_batch() == 1
    assert asked == [(55, "Confirming: ≈₹18 lakh pending on TC-006 — correct?",
                      expected)]


# ── _ask_group_question: the fact_confirm kind ────────────────────────────
# A confirm ("Confirming: ≈₹18 lakh pending — correct?") is time-sensitive in
# a way an ordinary blocked-tracking question is not: the 12h lid would let a
# fictional number sit unchallenged all day. kind="fact_confirm" shrinks the
# per-case cooldown to 1 hour, and the audit row records which kind fired.
# All other guards (mode/JID/active/length) stay exactly as pinned in
# test_group_questions.py — kind must not weaken them.


class KindPool:
    def __init__(self):
        self.cooldowns = []
        self.actions = []

    async def fetchval(self, sql, *args):
        if "SELECT origin_chat_jid" in sql:
            return "123@g.us"
        if "FROM actions" in sql:
            self.cooldowns.append((sql, args))
            return None
        return None

    async def execute(self, sql, *args):
        if "INSERT INTO actions" in sql:
            self.actions.append((sql, args))


def _wire_kind(monkeypatch, pool):
    async def fake_pool():
        return pool
    monkeypatch.setattr(triage, "get_pool", fake_pool)
    monkeypatch.setattr(triage, "get_settings",
                        lambda: SimpleNamespace(group_question_mode="autonomous"))
    sent = []

    async def fake_send(jid, msg):
        sent.append((jid, msg))
        return True

    async def fake_active(jid):
        return True

    import app.chats
    import app.whatsapp
    monkeypatch.setattr(app.chats, "is_active", fake_active)
    monkeypatch.setattr(app.whatsapp, "send_message", fake_send)
    return sent


def test_ask_group_question_kind_defaults_to_question():
    """Existing callers pass no kind — test_group_questions must stay green."""
    p = inspect.signature(triage._ask_group_question).parameters
    assert "kind" in p and p["kind"].default == "question"


async def test_fact_confirm_cooldown_is_one_hour_not_twelve(monkeypatch):
    pool = KindPool()
    sent = _wire_kind(monkeypatch, pool)
    await triage._ask_group_question(5, "What's the due date?")
    await triage._ask_group_question(5, "Confirming: ≈₹18 lakh — correct?",
                                     kind="fact_confirm")
    (sql_q, args_q), (sql_f, args_f) = pool.cooldowns
    assert (sql_q, args_q) != (sql_f, args_f), \
        "the two kinds' cooldown probes must be distinguishable"
    assert (triage.GROUP_QUESTION_COOLDOWN_HOURS in args_q
            or str(triage.GROUP_QUESTION_COOLDOWN_HOURS) in sql_q), \
        "ordinary questions keep the 12h window"
    assert triage.GROUP_QUESTION_COOLDOWN_HOURS not in args_f, \
        "a confirm must not sit behind the 12h lid"
    assert 1 in args_f or "'1 hour'" in sql_f, "confirms re-arm after 1 hour"
    assert len(sent) == 2, "no cooldown rows present -> both went out"


async def test_action_row_records_fact_confirm_kind(monkeypatch):
    pool = KindPool()
    _wire_kind(monkeypatch, pool)
    await triage._ask_group_question(5, "Confirming?", kind="fact_confirm")
    (sql, args), = pool.actions
    blob = sql + " " + " ".join(str(a) for a in args)
    assert "fact_confirm" in blob, \
        "the audit row must say a confirm fired (kind or payload)"

    pool2 = KindPool()
    _wire_kind(monkeypatch, pool2)
    await triage._ask_group_question(5, "What's the due date?")
    (sql2, args2), = pool2.actions
    blob2 = sql2 + " " + " ".join(str(a) for a in args2)
    assert "fact_confirm" not in blob2


# ── contextassembler: open-cases block ────────────────────────────────────

CASE_ROW = {"id": 19,
            "title": "Collect payment for invoice TC-006 from Shri Sagar Stevdores LLP",
            "summary": "Invoice ₹28,56,190 issued 04/08/2026.",
            "origin_match": False}


class CasePool:
    def __init__(self, cases=None, notes=None, exps=None, facts=None, boom=False):
        self.cases = cases if cases is not None else [CASE_ROW]
        self.notes = notes or [{"content": "≈₹18 lakh reported pending (group message)"}]
        self.exps = exps or [{"description": "Shri Sagar pays TC-006", "due_at": None,
                              "status": "open", "owner": None}]
        # raw case_facts rows INCLUDING superseded ones — the route below only
        # filters them out when the query itself says superseded_by IS NULL,
        # so a query missing the clause leaks history and fails the test.
        self.facts = facts or []
        self.boom = boom
        self.queries = []

    async def fetch(self, sql, *args):
        if self.boom:
            raise RuntimeError("db down")
        self.queries.append((sql, args))
        if "FROM cases c" in sql:
            return self.cases
        if "FROM case_facts" in sql:
            rows = self.facts
            if "superseded_by IS NULL" in sql:
                rows = [r for r in rows if r.get("superseded_by") is None]
            return rows[:args[1]] if len(args) > 1 else rows
        if "FROM case_events ce" in sql:
            return self.notes
        if "FROM expectations e" in sql:
            return self.exps
        return []


def _wire_cases(monkeypatch, pool):
    import app.db as db

    async def fake_pool():
        return pool
    monkeypatch.setattr(db, "get_pool", fake_pool)


async def test_open_cases_renders_notes_and_expectations(monkeypatch):
    pool = CasePool()
    _wire_cases(monkeypatch, pool)

    lines = await contextassembler._open_cases(
        "how much is pending on the TC-006 invoice?", "919000000101")

    assert len(lines) == 1
    line = lines[0]
    assert "Case #19" in line and "Collect payment" in line
    assert "≈₹18 lakh reported pending" in line, \
        "the group-fed fact must be readable from a DM"
    assert "Shri Sagar pays TC-006" in line and "unassigned" in line
    # matching is OR-semantics FTS over title/summary/linked events + origin-chat:
    # plainto's AND semantics once hid case 19 from "when is it due?" because
    # 'due' appeared nowhere in the case text — pin the OR construction.
    case_sql, case_args = next((s, a) for s, a in pool.queries if "FROM cases c" in s)
    assert "websearch_to_tsquery" in case_sql and "plainto_tsquery" not in case_sql
    assert "origin_chat_jid = $2" in case_sql and "status = 'open'" in case_sql
    assert "ts_rank" in case_sql, "OR matching needs rank ordering"
    assert " OR " in case_args[0], "message words must be OR-joined"


async def test_open_cases_not_gated_on_group_threads(monkeypatch):
    """The whole point: a DM (non-@g.us thread) still sees the case block."""
    _wire_cases(monkeypatch, CasePool())
    ctx = await contextassembler.assemble("what's pending on the invoice?",
                                          "919000000101", "Naman")
    assert contextassembler.CASES_HEADER in ctx
    assert "Case #19" in ctx


async def test_open_cases_fail_soft(monkeypatch):
    _wire_cases(monkeypatch, CasePool(boom=True))
    assert await contextassembler._open_cases("x", "t") == []
    # and assemble still works end-to-end
    ctx = await contextassembler.assemble("x", "919000000101", None)
    assert contextassembler.CASES_HEADER not in ctx


async def test_cases_outrank_graph_facts_in_drop_priority(monkeypatch):
    """With a budget that fits the case line but nothing after it, the case
    survives and lower-priority blocks are dropped."""
    _wire_cases(monkeypatch, CasePool())

    async def facts(msg, sender):
        return [{"fact": "some global fact " + "z" * 200}]
    monkeypatch.setattr(contextassembler, "_global_facts", facts)

    async def fake_settings():
        pass
    monkeypatch.setattr(
        contextassembler, "get_settings",
        lambda: SimpleNamespace(context_token_budget=90))  # 360 chars

    ctx = await contextassembler.assemble("invoice pending?", "919000000101", None)
    assert contextassembler.CASES_HEADER in ctx
    assert "some global fact" not in ctx


async def test_open_cases_fts_doc_includes_aliases(monkeypatch):
    """"INV-2041" in a DM must surface the case even when the title says
    something else — the aliases array is part of the FTS document."""
    pool = CasePool()
    _wire_cases(monkeypatch, pool)
    await contextassembler._open_cases("status of INV-2041?", "919000000101")
    case_sql, _ = next((s, a) for s, a in pool.queries if "FROM cases c" in s)
    assert "array_to_string(c.aliases" in case_sql


async def test_open_cases_renders_current_facts_before_recent(monkeypatch):
    """Facts are the distilled latest-wins values — they render ahead of the
    raw Recent notes WITH their provenance (who said it), and a superseded
    value never appears. The rows carry the joined provenance columns
    current_facts composes src from (see test_case_facts._src_row)."""
    _wire_cases(monkeypatch, CasePool(
        notes=[{"content": "discussed on the group call"}],
        facts=[{"field": "amount_pending", "value": "₹10 lakh (corrected)",
                "observed_at": NOW, "superseded_by": None,
                "source_event_id": 136, "source": "whatsapp",
                "actor_name": "Vinayak", "actor_raw": None},
               {"field": "amount_pending", "value": "≈₹18 lakh",
                "observed_at": NOW, "superseded_by": 1,
                "source_event_id": 135, "source": "whatsapp",
                "actor_name": "Vinayak", "actor_raw": None}]))

    lines = await contextassembler._open_cases("pending on TC-006?", "919000000101")
    line, = lines
    assert "Facts: " in line
    assert "amount_pending=₹10 lakh (corrected) (per whatsapp:Vinayak, 11 Aug)" \
        in line, "field=value with WHO said it and when — provenance travels"
    assert "≈₹18 lakh" not in line, "superseded value must not resurface"
    assert line.index("Facts: ") < line.index(" Recent: "), \
        "distilled facts come before raw notes"


async def test_open_cases_facts_fetch_failure_is_soft(monkeypatch):
    """A facts hiccup must not cost the whole case line."""
    _wire_cases(monkeypatch, CasePool())

    async def boom(handle, case_id, limit=8):
        raise RuntimeError("db down")
    monkeypatch.setattr(cases_util, "current_facts", boom)

    lines = await contextassembler._open_cases("TC-006?", "919000000101")
    line, = lines
    assert "Case #19" in line and "Facts:" not in line
    assert "≈₹18 lakh reported pending" in line, "notes still render"


# ── supervisor prompt + config registry pins ──────────────────────────────

def test_supervisor_prompt_uses_open_cases():
    from app.agents.graph import DEFAULT_PROMPTS
    p = DEFAULT_PROMPTS["supervisor"]
    assert "OPEN CASE" in p and "investigate_case" in p
    assert "OTHER" in p and "channels" in p, \
        "prompt must say cases can carry other channels' knowledge"


def test_company_name_configurable():
    from app.config import Settings
    from app.configservice import REGISTRY
    assert Settings.model_fields["company_name"].default == ""
    assert "company_name" in REGISTRY

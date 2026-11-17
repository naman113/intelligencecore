"""Case ack/close ops endpoints (main.py) — pin that both run THE close path
(triage._close_case) with the right statuses gate and close reason, and answer
409 when the case is not in a closable status. Fakes only; no DB/LLM/network."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.main as main
from app import triage


def _wire(monkeypatch, ok=True):
    """Fake the pool handle and record every _close_case invocation."""
    pool = object()

    async def fake_get_pool():
        return pool

    monkeypatch.setattr(main, "db", SimpleNamespace(get_pool=fake_get_pool))

    calls = []

    async def fake_close(p, case_id, close_reason=None, *,
                         kind="completed", statuses=("open",)):
        calls.append({"pool": p, "case_id": case_id,
                      "close_reason": close_reason, "kind": kind,
                      "statuses": statuses})
        return ok
    monkeypatch.setattr(triage, "_close_case", fake_close)
    return pool, calls


async def test_ack_closes_pending_ack_only(monkeypatch):
    pool, calls = _wire(monkeypatch, ok=True)
    out = await main.api_ack_case(31)
    assert out == {"closed": 31}
    call, = calls
    assert call["pool"] is pool
    assert call["case_id"] == 31
    assert call["close_reason"] is None        # ack keeps the proposed reason
    assert call["statuses"] == ("pending_ack",)


async def test_manual_close_reason_and_statuses(monkeypatch):
    pool, calls = _wire(monkeypatch, ok=True)
    out = await main.api_close_case(32)
    assert out == {"closed": 32}
    call, = calls
    assert call["pool"] is pool
    assert call["case_id"] == 32
    assert call["close_reason"] == "closed manually"
    assert call["statuses"] == ("open", "pending_ack")


async def test_ack_409_when_not_pending_ack(monkeypatch):
    _wire(monkeypatch, ok=False)
    with pytest.raises(HTTPException) as ei:
        await main.api_ack_case(31)
    assert ei.value.status_code == 409


async def test_close_409_when_not_open(monkeypatch):
    _wire(monkeypatch, ok=False)
    with pytest.raises(HTTPException) as ei:
        await main.api_close_case(32)
    assert ei.value.status_code == 409


# ── POST /api/cases/{id}/note — the manual case-fact path ─────────────────
# A human telling the system something about a case must run THE one note
# write path (cases_util.add_case_note), so the note is a provenance='self',
# triage_status='skipped' event: visible to the timeline, rolling summary and
# reactive context, structurally invisible to triage.

class NoteConn:
    def __init__(self, case_exists=True):
        self.case_exists = case_exists
        self.fetchvals = []
        self.executed = []

    async def fetchval(self, sql, *args):
        self.fetchvals.append((sql, args))
        if "SELECT id FROM cases WHERE id" in sql:
            return args[0] if self.case_exists else None
        if "INSERT INTO events" in sql:
            return 777
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"

    def transaction(self):
        class Tx:
            async def __aenter__(self_t):
                return None

            async def __aexit__(self_t, *e):
                return False
        return Tx()


class NotePool:
    """Pool double: fetchval answers the existence probe, acquire yields the
    same recording conn the note is written through."""

    def __init__(self, conn):
        self.conn = conn

    async def fetchval(self, sql, *args):
        return await self.conn.fetchval(sql, *args)

    def acquire(self):
        class Ctx:
            def __init__(self_c, conn):
                self_c.conn = conn

            async def __aenter__(self_c):
                return self_c.conn

            async def __aexit__(self_c, *e):
                return False
        return Ctx(self.conn)


def _wire_note(monkeypatch, case_exists=True):
    conn = NoteConn(case_exists=case_exists)

    async def fake_get_pool():
        return NotePool(conn)

    monkeypatch.setattr(main, "db", SimpleNamespace(get_pool=fake_get_pool))
    return conn


async def test_manual_note_runs_the_one_note_path(monkeypatch):
    conn = _wire_note(monkeypatch)
    out = await main.api_add_case_note(31, {"text": "  Vinayak confirmed ₹10 lakh  "})
    assert out == {"event_id": 777}

    ins = [(s, a) for s, a in conn.fetchvals if "INSERT INTO events" in s]
    assert len(ins) == 1
    sql, args = ins[0]
    # the anti-feedback-loop invariant, pinned in the SQL itself
    assert "'self'" in sql and "'skipped'" in sql
    assert args[0] == "Vinayak confirmed ₹10 lakh", "text is stripped"
    link, = [(s, a) for s, a in conn.executed if "INSERT INTO case_events" in s]
    assert link[1] == (31, 777, "manual"), \
        "the link's kind label must say a human wrote this"
    assert any("events_since_summary" in s for s, _ in conn.executed
               if "UPDATE cases" in s), "a manual note must feed the summary too"


async def test_manual_note_404_on_unknown_case(monkeypatch):
    conn = _wire_note(monkeypatch, case_exists=False)
    with pytest.raises(HTTPException) as ei:
        await main.api_add_case_note(99, {"text": "hello"})
    assert ei.value.status_code == 404
    assert not any("INSERT INTO events" in s for s, _ in conn.fetchvals)


async def test_manual_note_400_on_blank_text(monkeypatch):
    conn = _wire_note(monkeypatch)
    for body in ({"text": "   "}, {"text": None}, {}, None):
        with pytest.raises(HTTPException) as ei:
            await main.api_add_case_note(31, body)
        assert ei.value.status_code == 400
    assert not any("INSERT INTO events" in s for s, _ in conn.fetchvals)


# ── GET /api/cases/{id} — sandbox cases stay reachable, facts carry src ───
# The list endpoint is sandbox-scoped (test_sandbox_isolation pins it); the
# by-id fetch deliberately is NOT — E2E asserts on its own quarantined cases
# through this endpoint, and an operator opening a sandbox case from a link
# must not get a 404.


class DetailPool:
    def __init__(self):
        self.fetchrow_sqls = []
        self.fetch_sqls = []

    async def fetchrow(self, sql, *args):
        self.fetchrow_sqls.append(sql)
        return {"id": 31, "title": "E2E scenario case", "status": "open",
                "sandbox": True}

    async def fetch(self, sql, *args):
        self.fetch_sqls.append(sql)
        if "case_facts" in sql:
            # raw joined row; current_facts composes src from it
            return [{"field": "amount_pending", "value": "10 lakh",
                     "observed_at": None, "source_event_id": 136,
                     "source": "whatsapp", "actor_name": "Vinayak",
                     "actor_raw": None}]
        return []


async def test_case_detail_by_id_reaches_sandbox_cases(monkeypatch):
    pool = DetailPool()

    async def fake_get_pool():
        return pool
    monkeypatch.setattr(main, "db", SimpleNamespace(get_pool=fake_get_pool))

    out = await main.api_case_detail(31)
    row_sql, = [s for s in pool.fetchrow_sqls if "FROM cases" in s]
    assert "sandbox" not in row_sql, \
        "the by-id fetch must work for sandbox AND real cases alike"
    assert out["case"]["id"] == 31


async def test_case_detail_facts_carry_src(monkeypatch):
    """Both UIs render who supplied each fact — src must ride the response
    (it comes free from cases_util.current_facts)."""
    pool = DetailPool()

    async def fake_get_pool():
        return pool
    monkeypatch.setattr(main, "db", SimpleNamespace(get_pool=fake_get_pool))

    out = await main.api_case_detail(31)
    fact, = out["facts"]
    assert fact["src"] == "whatsapp:Vinayak"

"""C3 — tool + query hardening (pure logic, pool/whatsapp faked).

Covers:
  * send_reminder honors the expectation's max_reminders cap: at/over the cap it
    refuses (no send, no snooze), tells the agent to escalate, and still audits
    the refusal as an actions row (kind='reminder_cap', status='executed').
    NULL max_reminders falls back to the schema default of 5. The tier gate
    stays the FIRST check — a draft-tier playbook never even reads the row.
  * people.reresolve_events bounds its rescan to the last 30 days of unmatched
    external events — a person edit must not scan the whole table's history.
  * connectors._is_self_echo stays semantically identical but is sargable for
    idx_events_echo (thread_ref, provenance, occurred_at DESC): no
    IS NOT DISTINCT FROM on the leading column, no function-wrapped indexed
    columns, bare-column 10-minute window.
"""

import types
from datetime import datetime, timezone

import pytest

from app import playbook_tools as pt

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------- fakes

class FakePool:
    """Records every call; fetchrow serves the expectation row."""

    def __init__(self, exp_row=None):
        self.exp_row = exp_row
        self.fetchrow_calls = []
        self.fetchval_calls = []
        self.executes = []

    async def fetchrow(self, sql, *args):
        self.fetchrow_calls.append((sql, args))
        return self.exp_row

    async def fetchval(self, sql, *args):
        self.fetchval_calls.append((sql, args))
        return 101  # action id

    async def execute(self, sql, *args):
        self.executes.append((sql, args))


class SendRecorder:
    def __init__(self, result=True):
        self.sent = []
        self.result = result

    async def __call__(self, phone, text):
        self.sent.append((phone, text))
        return self.result


@pytest.fixture
def ctx():
    """Set playbook contextvars for a test; restore defaults afterwards.

    Token reset is impossible here — pytest-asyncio runs the async test in a
    different Context than this sync fixture's teardown — so we re-set each var
    to its default (None) instead."""

    def set_ctx(tier="autonomous_message", exp_id=5, case_id=None, playbook_id=None):
        pt.ctx_tier.set(tier)
        pt.ctx_expectation_id.set(exp_id)
        pt.ctx_case_id.set(case_id)
        pt.ctx_playbook_id.set(playbook_id)

    yield set_ctx
    for var in (pt.ctx_tier, pt.ctx_expectation_id, pt.ctx_case_id, pt.ctx_playbook_id):
        var.set(None)


def _wire(monkeypatch, pool, send=None):
    send = send if send is not None else SendRecorder()

    async def fake_get_pool():
        return pool

    async def fake_log_tool(*a, **k):
        return None

    async def fake_person_phone(name):
        return 1, "911234567890"

    monkeypatch.setattr(pt, "get_pool", fake_get_pool)
    monkeypatch.setattr(pt, "_log_tool", fake_log_tool)
    monkeypatch.setattr(pt, "_person_phone", fake_person_phone)
    monkeypatch.setattr(pt, "whatsapp", types.SimpleNamespace(send_message=send))
    return send


def _actions(pool):
    return [(sql, args) for sql, args in pool.fetchval_calls
            if "INSERT INTO actions" in sql]


# _record_action arg order: (case_id, playbook_id, run_id, kind, payload, tier, status)
_KIND, _STATUS = 3, 6


# ---------------------------------------------------------------- reminder cap

async def test_cap_reached_refuses_no_send_no_snooze_but_audits(ctx, monkeypatch):
    pool = FakePool(exp_row={"reminder_count": 5, "max_reminders": 5})
    send = _wire(monkeypatch, pool)
    ctx()
    out = await pt.send_reminder.coroutine(person="Rakesh", text="please send the sheet")
    assert "reminder cap reached (5/5)" in out
    assert "escalate" in out
    assert send.sent == []                      # nothing went out
    assert pool.executes == []                  # no snooze / reminder_count bump
    acts = _actions(pool)
    assert len(acts) == 1                       # refusal is still audited
    _, args = acts[0]
    assert args[_KIND] == "reminder_cap"
    assert args[_STATUS] == "executed"


async def test_over_cap_also_refuses(ctx, monkeypatch):
    pool = FakePool(exp_row={"reminder_count": 7, "max_reminders": 5})
    send = _wire(monkeypatch, pool)
    ctx()
    out = await pt.send_reminder.coroutine(person="Rakesh", text="nudge")
    assert "reminder cap reached (7/5)" in out
    assert send.sent == []
    assert pool.executes == []


async def test_below_cap_sends_and_snoozes_unchanged(ctx, monkeypatch):
    pool = FakePool(exp_row={"reminder_count": 2, "max_reminders": 5})
    send = _wire(monkeypatch, pool)
    ctx()
    out = await pt.send_reminder.coroutine(person="Rakesh", text="nudge")
    assert out == "reminder sent to Rakesh"
    assert send.sent == [("911234567890", "nudge")]
    acts = _actions(pool)
    assert len(acts) == 1
    assert acts[0][1][_KIND] == "whatsapp_message"
    assert acts[0][1][_STATUS] == "executed"
    snoozes = [sql for sql, _ in pool.executes if "UPDATE expectations" in sql]
    assert len(snoozes) == 1
    assert "reminder_count + 1" in snoozes[0]


async def test_null_max_reminders_defaults_to_five(ctx, monkeypatch):
    # at the default cap → refused
    pool = FakePool(exp_row={"reminder_count": 5, "max_reminders": None})
    send = _wire(monkeypatch, pool)
    ctx()
    out = await pt.send_reminder.coroutine(person="Rakesh", text="nudge")
    assert "reminder cap reached (5/5)" in out
    assert send.sent == []


async def test_null_max_reminders_below_default_still_sends(ctx, monkeypatch):
    pool = FakePool(exp_row={"reminder_count": 4, "max_reminders": None})
    send = _wire(monkeypatch, pool)
    ctx()
    out = await pt.send_reminder.coroutine(person="Rakesh", text="nudge")
    assert out == "reminder sent to Rakesh"
    assert len(send.sent) == 1


async def test_null_reminder_count_coerces_to_zero_and_sends(ctx, monkeypatch):
    # A NULL reminder_count (`or 0` coercion) is zero sent so far → must send.
    pool = FakePool(exp_row={"reminder_count": None, "max_reminders": 5})
    send = _wire(monkeypatch, pool)
    ctx()
    out = await pt.send_reminder.coroutine(person="Rakesh", text="nudge")
    assert out == "reminder sent to Rakesh"
    assert len(send.sent) == 1


async def test_zero_max_reminders_disables_reminders(ctx, monkeypatch):
    # max_reminders=0 means reminders are OFF for this expectation: the very
    # first call refuses with the cap message, sends nothing, snoozes nothing.
    pool = FakePool(exp_row={"reminder_count": 0, "max_reminders": 0})
    send = _wire(monkeypatch, pool)
    ctx()
    out = await pt.send_reminder.coroutine(person="Rakesh", text="nudge")
    assert "reminder cap reached (0/0)" in out
    assert send.sent == []
    assert pool.executes == []


async def test_tier_gate_stays_first_check(ctx, monkeypatch):
    # A draft-tier playbook is refused BEFORE the expectation row is even read,
    # and the tier refusal records no actions row (pre-existing behavior).
    pool = FakePool(exp_row={"reminder_count": 99, "max_reminders": 5})
    send = _wire(monkeypatch, pool)
    ctx(tier="draft")
    out = await pt.send_reminder.coroutine(person="Rakesh", text="nudge")
    assert out.startswith("TIER REFUSED")
    assert pool.fetchrow_calls == []
    assert _actions(pool) == []
    assert send.sent == []


async def test_no_expectation_context_skips_cap_check_and_sends(ctx, monkeypatch):
    pool = FakePool(exp_row={"reminder_count": 99, "max_reminders": 5})
    send = _wire(monkeypatch, pool)
    ctx(exp_id=None)
    out = await pt.send_reminder.coroutine(person="Rakesh", text="nudge")
    assert out == "reminder sent to Rakesh"
    assert pool.fetchrow_calls == []            # no expectation to read
    assert len(send.sent) == 1


# ---------------------------------------------------------------- reresolve window

class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class ConnRecorder:
    def __init__(self):
        self.fetch_calls = []

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        return []


class PoolWithConn:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


async def test_reresolve_rescan_is_bounded_to_30_days(monkeypatch):
    from app import people

    conn = ConnRecorder()

    async def fake_get_pool():
        return PoolWithConn(conn)

    monkeypatch.setattr(people, "get_pool", fake_get_pool)
    n = await people.reresolve_events()
    assert n == 0
    ((sql, _),) = conn.fetch_calls
    assert "interval '30 days'" in sql
    assert "occurred_at >" in sql
    assert "actor_id IS NULL" in sql
    assert "provenance = 'external'" in sql


# ---------------------------------------------------------------- echo query shape

class EchoConn:
    def __init__(self, row=None):
        self.calls = []
        self.row = row

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self.row


def _raw(**kw):
    from app.connectors import RawEvent

    defaults = dict(source="whatsapp", content="hi there",
                    occurred_at=datetime.now(timezone.utc),
                    thread_ref="911234567890@s.whatsapp.net")
    defaults.update(kw)
    return RawEvent(**defaults)


async def test_echo_query_filters_index_columns_sargably():
    from app.connectors import _is_self_echo

    conn = EchoConn(row=None)
    assert await _is_self_echo(conn, _raw()) is False
    ((sql, args),) = conn.calls
    # index-friendly for idx_events_echo (thread_ref, provenance, occurred_at DESC)
    assert "IS NOT DISTINCT FROM" not in sql
    assert "thread_ref = $3" in sql
    assert "thread_ref IS NULL AND $3 IS NULL" in sql   # NULL-thread semantics kept
    assert "provenance = 'self'" in sql
    assert "occurred_at > now() - interval '10 minutes'" in sql
    # the indexed columns are never wrapped in a function
    for col in ("thread_ref", "provenance", "occurred_at"):
        assert f"btrim({col}" not in sql and f"lower({col}" not in sql
    assert args == ("whatsapp", "hi there", "911234567890@s.whatsapp.net")


async def test_echo_semantics_unchanged():
    from app.connectors import _is_self_echo

    # self provenance short-circuits without touching the DB
    conn = EchoConn()
    assert await _is_self_echo(conn, _raw(provenance="self")) is True
    assert conn.calls == []
    # a matching recent self row → echo
    assert await _is_self_echo(EchoConn(row={"?column?": 1}), _raw()) is True
    # no match → genuinely external
    assert await _is_self_echo(EchoConn(row=None), _raw()) is False

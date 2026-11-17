"""Quick 039: triage model failures back off, cap their attempts and pause on a spend limit.

On 2026-10-27 the Anthropic spend limit was reached and the triage loop re-sent the
same batch every 10 s (428 failed calls). Fakes only; no DB, no model, no network."""

import asyncio
import json
import sys
import types
from types import SimpleNamespace

import pytest

from app import jobruns, modelgate, triage

SPEND = ("Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
         "'message': 'You have reached your specified API usage limits. You will regain "
         "access on 2026-11-01 at 00:00 UTC.'}}")


class _StatusError(Exception):
    def __init__(self, msg, status):
        super().__init__(msg)
        self.status_code = status


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(modelgate, "_now", c)
    triage._gate.reset()
    yield c
    triage._gate.reset()


LIMITS = dict(base=10, cap=600, max_attempts=5, pause=3600)


# ── the gate ──────────────────────────────────────────────────────────────

def test_spend_limit_is_recognised():
    assert modelgate.is_spend_limit(_StatusError(SPEND, 400))
    assert modelgate.is_spend_limit(_StatusError(
        "Your credit balance is too low to access the Anthropic API.", 400))
    assert modelgate.is_spend_limit(_StatusError(
        "Error code: 429 - {'error': {'code': 'insufficient_quota'}}", 429))
    assert not modelgate.is_spend_limit(_StatusError("rate_limit_error: slow down", 429))
    assert not modelgate.is_spend_limit(_StatusError("Overloaded", 529))
    assert not modelgate.is_spend_limit(ValueError("prompt is too long"))


def test_backoff_doubles_to_the_cap(clock):
    g = modelgate.ModelGate()
    waits = []
    for i in range(9):
        out = g.failure(("batch", i), _StatusError("Internal server error", 500), **LIMITS)
        assert out.kind == "retry"
        waits.append(round(g.wait()))
    assert waits == [10, 20, 40, 80, 160, 320, 600, 600, 600]


def test_give_up_after_max_attempts_on_one_item(clock):
    g = modelgate.ModelGate()
    kinds = [g.failure(("batch", 7), _StatusError("boom", 500), **LIMITS).kind
             for _ in range(5)]
    assert kinds == ["retry"] * 4 + ["give_up"]
    assert g.wait() > 0, "the next batch still waits: the backoff is not reset by a give-up"
    assert g.failure(("batch", 7), _StatusError("boom", 500), **LIMITS).attempts == 1


def test_spend_limit_pauses_without_counting_attempts(clock):
    g = modelgate.ModelGate()
    out = g.failure(("batch", 1), _StatusError(SPEND, 400), **LIMITS)
    assert out.kind == "paused" and out.attempts == 0
    assert g.paused() and round(g.wait()) == 3600
    clock.t += 3601
    assert not g.paused() and g.wait() == 0


def test_success_resets_backoff_pause_and_that_items_attempts(clock):
    g = modelgate.ModelGate()
    g.failure(("batch", 1), _StatusError("boom", 500), **LIMITS)
    g.failure(("sweep", (5,)), _StatusError("boom", 500), **LIMITS)
    g.success(("sweep", (5,)))
    assert g.wait() == 0
    assert g.failure(("batch", 1), _StatusError("boom", 500), **LIMITS).attempts == 2
    assert g.failure(("sweep", (5,)), _StatusError("boom", 500), **LIMITS).attempts == 1


# ── the triage batch ──────────────────────────────────────────────────────

class BatchPool:
    """Pending events for the batch query; records every execute."""

    def __init__(self, events):
        self.events = events
        self.executed = []
        pool = self

        class Conn:
            def transaction(self):
                class Tx:
                    async def __aenter__(self_t):
                        return None

                    async def __aexit__(self_t, *e):
                        return False
                return Tx()

            async def fetch(self, sql, *a):
                if "triage_status = 'pending' AND e.provenance = 'external'" in sql:
                    return [dict(e) for e in pool.events]
                return []

            async def fetchval(self, *a):
                return None

            async def fetchrow(self, *a):
                return None

            async def execute(self, sql, *a):
                pool.executed.append((sql, a))
                return "UPDATE 1"
        self.conn = Conn()

    def acquire(self):
        conn = self.conn

        class Ctx:
            async def __aenter__(self_c):
                return conn

            async def __aexit__(self_c, *e):
                return False
        return Ctx()

    async def execute(self, sql, *a):
        self.executed.append((sql, a))
        return "UPDATE 1"

    async def fetch(self, sql, *a):
        return []


class Model:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def ainvoke(self, msgs, config=None):
        self.calls += 1
        o = self.outcomes.pop(0) if self.outcomes else {"verdicts": []}
        if isinstance(o, BaseException):
            raise o
        return SimpleNamespace(content=json.dumps(o))


def _ev(i):
    return {"id": i, "source": "whatsapp", "content": f"msg {i}", "actor_id": None,
            "actor_raw": "1", "occurred_at": None, "thread_ref": "g@g.us", "meta": "{}",
            "actor_name": None, "chat_subject": None}


@pytest.fixture
def wired(monkeypatch, clock):
    pool = BatchPool([_ev(1), _ev(2)])

    async def gp():
        return pool
    monkeypatch.setattr(triage, "get_pool", gp)
    s = SimpleNamespace(owner_number="", company_name="", test_threads="",
                        triage_batch_seconds=10, triage_backoff_max_seconds=600,
                        triage_max_attempts=3, triage_spend_pause_minutes=60)
    monkeypatch.setattr(triage, "get_settings", lambda: s)
    stub = types.ModuleType("app.agents.graph")
    stub._callbacks = lambda: {}
    monkeypatch.setitem(sys.modules, "app.agents.graph", stub)

    async def nothing(*a, **k):
        return [] if a and not k else None

    async def empty(*a, **k):
        return []

    async def empty_d(*a, **k):
        return {}

    async def noop(*a, **k):
        return None

    async def policy():
        return ""
    import app.skillservice as ss
    monkeypatch.setattr(ss, "triage_policy_block", policy)
    monkeypatch.setattr(triage, "_open_cases_index", empty)
    monkeypatch.setattr(triage, "_open_expectations_index", empty)
    monkeypatch.setattr(triage, "_thread_context", empty_d)
    monkeypatch.setattr(triage, "_answered_here", empty_d)
    monkeypatch.setattr(triage, "_refresh_summaries", noop)
    monkeypatch.setattr(triage, "_complete_finished_cases", noop)

    loop_errors = []

    async def fake_loop_error(loop, exc):
        loop_errors.append((loop, str(exc)))
    monkeypatch.setattr(jobruns, "loop_error", fake_loop_error)

    def use(outcomes):
        m = Model(outcomes)
        monkeypatch.setattr(triage, "_model", lambda: m)
        return m
    return SimpleNamespace(pool=pool, settings=s, use=use, loop_errors=loop_errors)


def _status_updates(pool):
    return [(sql, a) for sql, a in pool.executed if "triage_status" in sql]


async def test_failed_call_backs_off_instead_of_retrying_every_tick(wired, clock):
    m = wired.use([_StatusError("Internal server error", 500)] * 10)
    assert await triage._triage_batch() == 0
    assert m.calls == 1 and not _status_updates(wired.pool), "events stay pending"
    for _ in range(4):                       # 2-second ticks inside the 10 s wait
        clock.t += 2
        assert await triage._triage_batch() == 0
    assert m.calls == 1, "no model call while backing off"
    clock.t += 3
    await triage._triage_batch()
    assert m.calls == 2


async def test_batch_given_up_after_max_attempts_is_failed_and_recorded(wired, clock):
    m = wired.use([_StatusError("Error code: 400 - prompt is invalid", 400)] * 10)
    for _ in range(3):
        await triage._triage_batch()
        clock.t += 700
    assert m.calls == 3
    ups = _status_updates(wired.pool)
    assert len(ups) == 1
    sql, args = ups[0]
    assert "triage_status = 'failed'" in sql and "triage_gave_up" in sql
    assert "AND triage_status = 'pending'" in sql
    assert list(args[0]) == [1, 2] and args[1] == 3
    assert len(wired.loop_errors) == 1 and wired.loop_errors[0][0] == "triage"
    assert "gave up on 2 message(s) after 3 failed model calls" in wired.loop_errors[0][1]


async def test_refusal_counts_as_a_failed_attempt(wired, clock):
    from langchain_core.messages import AIMessage
    refusal = AIMessage(content=[], response_metadata={"stop_reason": "refusal"})

    class Refuses:
        calls = 0

        async def ainvoke(self, msgs, config=None):
            Refuses.calls += 1
            return refusal
    wired.use([])
    triage._model = lambda: Refuses()  # restored by monkeypatch (wired.use patched it)
    for _ in range(3):
        assert await triage._triage_batch() == 0
        clock.t += 700
    assert Refuses.calls == 3
    assert any("triage_status = 'failed'" in sql for sql, _ in _status_updates(wired.pool))


async def test_spend_limit_pauses_all_triage_calls_and_keeps_events_pending(wired, clock):
    m = wired.use([_StatusError(SPEND, 400)] * 3 + [{"verdicts": []}])
    await triage._triage_batch()
    assert m.calls == 1 and triage._gate.paused()
    for _ in range(13):                      # 65 minutes of ticks
        clock.t += 300
        await triage._triage_batch()
    assert m.calls == 2, "one call per pause (60 min), not one per tick"
    assert not _status_updates(wired.pool), "a spend limit never fails the events"
    assert not wired.loop_errors


async def test_first_answer_after_boot_requeues_given_up_events_once(wired, clock):
    wired.use([{"verdicts": []}] * 3)
    assert await triage._triage_batch() == 2
    assert not [s for s, _ in wired.pool.executed if "triage_requeued" in s],         "nothing is requeued before the model has answered"
    wired.pool.executed.clear()
    assert await triage._triage_batch() == 2
    requeue = [(sql, a) for sql, a in wired.pool.executed if "triage_requeued" in sql]
    assert len(requeue) == 1, "the batch after the first answer checks once"
    assert wired.pool.executed[0] == requeue[0], "before the batch's own status updates"
    sql, args = requeue[0]
    assert "SET triage_status = 'pending'" in sql and "meta ? 'triage_gave_up'" in sql
    assert "provenance = 'external'" in sql and args == (triage.GAVE_UP_REQUEUES,)
    wired.pool.executed.clear()
    await triage._triage_batch()
    assert not [s for s, _ in wired.pool.executed if "triage_requeued" in s], \
        "no requeue scan on every batch, only after a give-up"


async def test_give_up_then_success_requeues(wired, clock):
    wired.use([_StatusError("boom", 500)] * 3 + [{"verdicts": []}] * 2)
    triage._requeue_mark = None
    for _ in range(3):
        await triage._triage_batch()
        clock.t += 700
    assert triage._requeue_mark == 0
    wired.pool.executed.clear()
    await triage._triage_batch()                 # the model answers again
    assert not [s for s, _ in wired.pool.executed if "triage_requeued" in s]
    await triage._triage_batch()
    assert [s for s, _ in wired.pool.executed if "triage_requeued" in s]
    assert triage._requeue_mark is None


# ── the satisfaction sweep ───────────────────────────────────────────────

class SweepPool:
    def __init__(self):
        self.executed = []
        pool = self

        class Conn:
            async def fetch(self, sql, *a):
                if "provenance = 'self' AND triage_status = 'pending'" in sql:
                    return [{"id": 50, "content": "Sent the invoice", "thread_ref": "g@g.us"}]
                if "x.owed_by = 'assistant'" in sql:
                    return []
                if "FROM expectations e JOIN cases c" in sql:
                    return [{"id": 9, "description": "Send the invoice", "satisfy_hint": "sent",
                             "case_title": "Invoice", "owed_by": "person",
                             "source_event_id": 3, "asked_in": "g@g.us", "asked_text": "x"}]
                return []

            async def execute(self, sql, *a):
                pool.executed.append((sql, a))
        self.conn = Conn()

    def acquire(self):
        conn = self.conn

        class Ctx:
            async def __aenter__(self_c):
                return conn

            async def __aexit__(self_c, *e):
                return False
        return Ctx()

    async def execute(self, sql, *a):
        self.executed.append((sql, a))


async def test_sweep_backs_off_shares_the_pause_and_gives_up(wired, clock, monkeypatch):
    pool = SweepPool()

    async def gp():
        return pool
    monkeypatch.setattr(triage, "get_pool", gp)
    m = wired.use([_StatusError("boom", 500)] * 3)
    await triage._satisfaction_sweep()
    assert m.calls == 1 and not pool.executed, "deliveries stay pending for a retry"
    clock.t += 5
    await triage._satisfaction_sweep()
    assert m.calls == 1, "no call inside the backoff window"
    for _ in range(2):
        clock.t += 700
        await triage._satisfaction_sweep()
    assert m.calls == 3
    skipped = [a for sql, a in pool.executed if "triage_status = 'skipped'" in sql]
    assert skipped and list(skipped[0][0]) == [50], "given up: the deliveries stop coming back"
    assert len(wired.loop_errors) == 1 and "satisfaction sweep" in wired.loop_errors[0][1]

    triage._gate.reset()
    pool.executed.clear()
    m = wired.use([_StatusError(SPEND, 400)])
    await triage._satisfaction_sweep()
    assert triage._gate.paused()
    clock.t += 600
    await triage._satisfaction_sweep()
    assert m.calls == 1 and not pool.executed, "paused: no call, deliveries left pending"


# ── the loop never dies ──────────────────────────────────────────────────

async def test_triage_loop_survives_failing_model_calls(wired, clock, monkeypatch):
    wired.use([_StatusError("boom", 500)] * 100)
    real_sleep = asyncio.sleep

    async def fast_sleep(_s):
        clock.t += 700
        await real_sleep(0)
    monkeypatch.setattr(triage.asyncio, "sleep", fast_sleep)

    async def quiet():
        return None
    monkeypatch.setattr(triage, "_satisfaction_sweep", quiet)
    monkeypatch.setattr(triage, "_close_stale_cases", quiet)
    task = asyncio.create_task(triage.run_forever())
    for _ in range(50):
        await real_sleep(0)
    alive = not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert alive, "triage loop died on failing model calls"


def test_settings_and_registry():
    from app import configservice
    from app.config import Settings
    s = Settings(_env_file=None)
    assert (s.triage_backoff_max_seconds, s.triage_max_attempts,
            s.triage_spend_pause_minutes) == (600, 5, 60)
    for k in ("triage_backoff_max_seconds", "triage_max_attempts", "triage_spend_pause_minutes"):
        e = configservice.REGISTRY[k]
        assert e["group"] == "tuning" and not e["secret"] and e["min"] >= 1
    with pytest.raises(ValueError):
        configservice._validate_value("triage_max_attempts", "0")

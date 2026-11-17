"""Phase C2 — triage case lifecycle: stale-case sweep, last_activity_at
maintenance, activity-ordered open-case index. Fakes only; no DB/LLM."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import triage

NOW = datetime.now(timezone.utc)


def _case(cid, *, age_days, status="open", open_expectation=False):
    return {
        "id": cid,
        "status": status,
        "last_activity_at": NOW - timedelta(days=age_days),
        "open_expectation": open_expectation,
        "close_reason": None,
    }


class FakeLifecyclePool:
    """In-memory cases table that understands the exact queries the lifecycle
    functions issue. `fail_close_for` simulates a per-case DB error on close."""

    def __init__(self, cases, fail_close_for=()):
        self.cases = {c["id"]: c for c in cases}
        self.fail_close_for = set(fail_close_for)
        self.sqls = []

    async def fetch(self, sql, *args):
        self.sqls.append(sql)
        if "last_activity_at < now() - interval '1 day'" in sql:
            # The sweep query must carry its two guards in SQL, not in Python.
            assert "status = 'open'" in sql
            assert "NOT EXISTS" in sql and "e.status = 'open'" in sql
            days = args[0]
            cutoff = NOW - timedelta(days=days)
            return [
                {"id": c["id"]}
                for c in sorted(self.cases.values(), key=lambda c: c["id"])
                if c["status"] == "open"
                and c["last_activity_at"] < cutoff
                and not c["open_expectation"]
            ]
        return []

    async def fetchval(self, sql, *args):
        self.sqls.append(sql)
        if "UPDATE cases SET status = 'closed'" in sql:
            # Mirrors the real close SQL: status = ANY($3::text[]) +
            # COALESCE(close_reason, $2).
            cid, reason, statuses = args[0], args[1], args[2]
            if cid in self.fail_close_for:
                raise RuntimeError("boom on close")
            c = self.cases.get(cid)
            if c and c["status"] in statuses:
                c["status"] = "closed"
                if c["close_reason"] is None:
                    c["close_reason"] = reason
                return cid
            return None
        return None


def _wire(monkeypatch, pool, stale_case_days=21):
    async def fake_get_pool():
        return pool

    monkeypatch.setattr(triage, "get_pool", fake_get_pool)
    # case_chaining_enabled=False: these tests pin the close path itself, not
    # the follow-on chaining (test_case_chaining.py owns that).
    monkeypatch.setattr(
        triage, "get_settings",
        lambda: SimpleNamespace(stale_case_days=stale_case_days,
                                case_chaining_enabled=False))

    notified, distilled, notify_kinds = [], [], []

    async def fake_notify(case_id, kind="completed"):
        notified.append(case_id)
        notify_kinds.append(kind)
        return True

    async def fake_distill(case_id):
        distilled.append(case_id)

    import app.actionsvc
    import app.memory_pipeline
    monkeypatch.setattr(app.actionsvc, "notify_case_closed", fake_notify)
    monkeypatch.setattr(app.memory_pipeline, "distill_case", fake_distill)
    _wire.last_notify_kinds = notify_kinds
    return notified, distilled


# ── stale sweep ────────────────────────────────────────────────────────────

async def test_stale_sweep_closes_only_stale_dead_cases(monkeypatch):
    pool = FakeLifecyclePool([
        _case(1, age_days=30),                          # stale, dead → close
        _case(2, age_days=1),                           # fresh → untouched
        _case(3, age_days=30, open_expectation=True),   # stale but live → untouched
        _case(4, age_days=30, status="closed"),         # already closed → untouched
    ])
    notified, distilled = _wire(monkeypatch, pool)

    await triage._close_stale_cases()

    assert pool.cases[1]["status"] == "closed"
    assert "(auto-closed: stale)" in pool.cases[1]["close_reason"]
    assert pool.cases[2]["status"] == "open"
    assert pool.cases[3]["status"] == "open"
    # close went through the shared path: notify + distill, exactly once, only case 1
    assert notified == [1]
    assert distilled == [1]
    # A stale close is an ARCHIVAL notice, never a completion claim: nothing
    # happened on this case, so "Done — complete" would be false.
    assert _wire.last_notify_kinds == ["stale"]


async def test_completion_close_keeps_completed_kind(monkeypatch):
    pool = FakeLifecyclePool([_case(1, age_days=1)])
    notified, _ = _wire(monkeypatch, pool)

    assert await triage._close_case(pool, 1, "auto: all expectations satisfied")
    assert notified == [1]
    assert _wire.last_notify_kinds == ["completed"]   # default stays completion


async def test_stale_sweep_is_fail_soft_per_case(monkeypatch):
    pool = FakeLifecyclePool(
        [_case(1, age_days=40), _case(2, age_days=40)],
        fail_close_for={1})
    notified, distilled = _wire(monkeypatch, pool)

    await triage._close_stale_cases()   # must not raise

    assert pool.cases[1]["status"] == "open"     # failed close rolled nothing forward
    assert pool.cases[2]["status"] == "closed"   # sweep kept going
    assert notified == [2] and distilled == [2]


async def test_stale_sweep_disabled_by_nonpositive_setting(monkeypatch):
    pool = FakeLifecyclePool([_case(1, age_days=400)])
    notified, _ = _wire(monkeypatch, pool, stale_case_days=0)

    await triage._close_stale_cases()

    assert pool.cases[1]["status"] == "open"
    assert pool.sqls == [] and notified == []


async def test_close_case_is_single_winner(monkeypatch):
    pool = FakeLifecyclePool([_case(1, age_days=30)])
    notified, distilled = _wire(monkeypatch, pool)

    assert await triage._close_case(pool, 1, "x") is True
    assert await triage._close_case(pool, 1, "x") is False   # already closed
    assert notified == [1] and distilled == [1]              # side effects once


# ── open-case index ordering ───────────────────────────────────────────────

async def test_open_cases_index_orders_by_last_activity(monkeypatch):
    captured = {}

    class Conn:
        async def fetch(self, sql, *args):
            captured["sql"] = sql
            return []

    await triage._open_cases_index(Conn())
    assert "ORDER BY c.last_activity_at DESC" in captured["sql"]
    assert "ORDER BY c.id DESC" not in captured["sql"]


# ── last_activity_at touch on event link ───────────────────────────────────

async def test_apply_verdict_touches_last_activity_on_link():
    executed = []

    class Conn:
        async def fetchval(self, sql, *args):
            if "SELECT id FROM cases WHERE id" in sql:
                return args[0]   # existing open case
            return None

        async def execute(self, sql, *args):
            executed.append(sql)

    event = {"id": 10, "source": "whatsapp", "content": "please send the report",
             "actor_id": None, "thread_ref": "919999@g.us"}
    verdict = {"relevant": True, "case": 5, "participants": [],
               "expectations_created": [], "expectations_satisfied": [],
               "reason": "advance"}

    await triage._apply_verdict(Conn(), event, verdict)

    bump = [s for s in executed if "events_since_summary + 1" in s]
    assert bump, "event link must bump events_since_summary"
    assert "last_activity_at = now()" in bump[0]


# ── loop wiring ────────────────────────────────────────────────────────────
# The Phase B lesson: sweep functions tested only in isolation can be silently
# unwired from the loop with the suite staying green. _tick is the loop body;
# these pin (a) every sweep is called from _tick, and (b) run_forever runs _tick.


async def test_tick_runs_every_sweep_in_order(monkeypatch):
    order = []

    def rec(name, ret=None):
        async def f():
            order.append(name)
            return ret
        return f

    monkeypatch.setattr(triage, "_triage_batch", rec("batch", 3))
    monkeypatch.setattr(triage, "_satisfaction_sweep", rec("satisfy"))
    monkeypatch.setattr(triage, "_complete_finished_cases", rec("complete"))
    monkeypatch.setattr(triage, "_close_stale_cases", rec("stale"))

    assert await triage._tick() == 3
    assert order == ["batch", "satisfy", "complete", "stale"]


async def test_run_forever_drives_tick(monkeypatch):
    calls = []

    async def fake_tick():
        calls.append(1)
        return 0

    async def fake_sleep(_seconds):
        raise asyncio.CancelledError   # stop the loop after one iteration

    monkeypatch.setattr(triage, "_tick", fake_tick)
    monkeypatch.setattr(triage.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        triage, "get_settings", lambda: SimpleNamespace(triage_batch_seconds=1))

    with pytest.raises(asyncio.CancelledError):
        await triage.run_forever()
    assert calls == [1]

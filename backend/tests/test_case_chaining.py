"""Case chaining — a COMPLETED close may open one follow-on case (linked via
parent_case_id) so the expectation engine drives the next obligation. Fakes
only; no DB/LLM. Pins: the statuses close guard, completed-only chaining, the
depth cap, the kill switch, and that an LLM failure never blocks the close."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from app import triage


class _Ctx:
    """Async context manager standing in for pool.acquire() / conn.transaction()."""

    def __init__(self, obj):
        self.obj = obj

    async def __aenter__(self):
        return self.obj

    async def __aexit__(self, *exc):
        return False


class FakeChainPool:
    """In-memory cases/participants/expectations understanding the exact queries
    the close + chain path issues. Acts as both pool and acquired conn."""

    def __init__(self, cases, participants=()):
        self.cases = {c["id"]: c for c in cases}
        self.participants = list(participants)   # dicts: case_id / person_id / role
        self.expectations = []
        self.next_id = max(self.cases) + 1
        self.sqls = []

    def acquire(self):
        return _Ctx(self)

    def transaction(self):
        return _Ctx(self)

    async def fetchval(self, sql, *args):
        self.sqls.append(sql)
        if "UPDATE cases SET status = 'closed'" in sql:
            # Pin the LIVE SQL before emulating it: dropping the COALESCE
            # (close-reason preservation) or the ANY($3::text[]) statuses gate
            # from triage._close_case must break these tests instead of quietly
            # testing the fake's own emulation.
            assert "COALESCE(close_reason" in sql, \
                "close must preserve an already-recorded close_reason"
            assert "ANY($3::text[])" in sql, \
                "close must gate on the statuses parameter"
            cid, reason, statuses = args
            c = self.cases.get(cid)
            if c and c["status"] in statuses:
                c["status"] = "closed"
                if c["close_reason"] is None:
                    c["close_reason"] = reason
                return cid
            return None
        if "SELECT parent_case_id" in sql:
            c = self.cases.get(args[0])
            return c.get("parent_case_id") if c else None
        if "INSERT INTO cases" in sql:
            cid = self.next_id
            self.next_id += 1
            self.cases[cid] = {
                "id": cid, "status": "open", "title": args[0],
                "opened_reason": args[1], "origin_chat_jid": args[2],
                "parent_case_id": args[3], "close_reason": None,
                "summary": None,
            }
            return cid
        return None

    async def fetchrow(self, sql, *args):
        self.sqls.append(sql)
        if "FROM cases WHERE id" in sql:
            c = self.cases.get(args[0])
            return dict(c) if c else None
        return None

    async def fetch(self, sql, *args):
        self.sqls.append(sql)
        return []

    async def execute(self, sql, *args):
        self.sqls.append(sql)
        if "INSERT INTO expectations" in sql:
            self.expectations.append({
                "case_id": args[0], "description": args[1], "owner_id": args[2],
                "due_at": args[3], "satisfy_hint": args[4]})
        elif "INSERT INTO case_participants" in sql:
            new_case, src_case = args
            copied = [p for p in self.participants
                      if p["case_id"] == src_case and p["role"] == "requester"]
            for p in copied:
                self.participants.append(
                    {"case_id": new_case, "person_id": p["person_id"],
                     "role": "requester"})


def _case(cid, *, status="open", parent=None, jid="12345@g.us"):
    return {"id": cid, "status": status, "parent_case_id": parent,
            "origin_chat_jid": jid, "title": f"case {cid}", "summary": None,
            "close_reason": None}


def _wire(monkeypatch, pool, *, chaining=True, response=None, model_raises=False):
    """Wire the fakes; returns the list of model invocations."""
    async def fake_get_pool():
        return pool

    monkeypatch.setattr(triage, "get_pool", fake_get_pool)
    monkeypatch.setattr(
        triage, "get_settings",
        lambda: SimpleNamespace(case_chaining_enabled=chaining, stale_case_days=21))

    calls = []

    class FakeModel:
        async def ainvoke(self, msgs, config=None):
            calls.append(msgs)
            if model_raises:
                raise RuntimeError("model down")
            return SimpleNamespace(content=json.dumps(response))

    monkeypatch.setattr(triage, "_model", lambda: FakeModel())

    async def fake_notify(case_id, kind="completed"):
        return True

    async def fake_distill(case_id):
        pass

    async def fake_resolve(conn, phone=None, email=None, alias=None):
        return 7 if alias else None

    import app.actionsvc
    import app.memory_pipeline
    import app.people
    monkeypatch.setattr(app.actionsvc, "notify_case_closed", fake_notify)
    monkeypatch.setattr(app.memory_pipeline, "distill_case", fake_distill)
    monkeypatch.setattr(app.people, "resolve", fake_resolve)
    return calls


# ── statuses close guard ───────────────────────────────────────────────────

async def test_statuses_param_gates_which_rows_close(monkeypatch):
    pool = FakeChainPool([_case(1), _case(2, status="pending_ack")])
    _wire(monkeypatch, pool, chaining=False)

    # default statuses=('open',): pending_ack is NOT closable
    assert await triage._close_case(pool, 2, "x") is False
    assert pool.cases[2]["status"] == "pending_ack"
    # explicit statuses closes it
    assert await triage._close_case(pool, 2, None, statuses=("pending_ack",)) is True
    assert pool.cases[2]["status"] == "closed"
    # and the open case is untouched by a pending_ack-only close
    assert await triage._close_case(pool, 1, "y", statuses=("pending_ack",)) is False
    assert pool.cases[1]["status"] == "open"


async def test_close_reason_coalesce_keeps_existing(monkeypatch):
    preset = _case(1)
    preset["close_reason"] = "user acknowledged"
    pool = FakeChainPool([preset, _case(2)])
    _wire(monkeypatch, pool, chaining=False)

    assert await triage._close_case(pool, 1, "overwrite attempt")
    assert pool.cases[1]["close_reason"] == "user acknowledged"   # COALESCE kept it
    assert await triage._close_case(pool, 2, "fresh reason")
    assert pool.cases[2]["close_reason"] == "fresh reason"


# ── when chaining runs ─────────────────────────────────────────────────────

async def test_completed_close_consults_model_stale_does_not(monkeypatch):
    pool = FakeChainPool([_case(1), _case(2)])
    calls = _wire(monkeypatch, pool, response={"follow_on": None})

    assert await triage._close_case(pool, 1, "done")
    assert len(calls) == 1                       # completed → chain check ran
    assert await triage._close_case(pool, 2, "stale", kind="stale")
    assert len(calls) == 1                       # stale → no chain check


async def test_kill_switch_disables_chaining(monkeypatch):
    pool = FakeChainPool([_case(1)])
    calls = _wire(monkeypatch, pool, chaining=False, response={"follow_on": None})

    assert await triage._close_case(pool, 1, "done")
    assert calls == []


async def test_depth_cap_stops_the_chain(monkeypatch):
    # 4 ← 3 ← 2 ← 1: case 4 has 3 ancestors → at the cap, no model call.
    pool = FakeChainPool([
        _case(1), _case(2, parent=1), _case(3, parent=2), _case(4, parent=3)])
    calls = _wire(monkeypatch, pool, response={"follow_on": None})

    assert await triage._close_case(pool, 4, "done")
    assert calls == []

    # depth 2 (case 3) is still under the cap → model consulted
    assert await triage._close_case(pool, 3, "done")
    assert len(calls) == 1


# ── applying the follow-on ─────────────────────────────────────────────────

FOLLOW = {"follow_on": {
    "title": "Pay INV-2041",
    "expectation": {"description": "Pay invoice INV-2041", "owner": "Vinu",
                    "due_at": "2026-09-15T00:00:00Z",
                    "satisfy_hint": "payment confirmed in the group"}}}


async def test_follow_on_opens_linked_case_with_expectation(monkeypatch):
    pool = FakeChainPool(
        [_case(1, jid="777@g.us")],
        participants=[{"case_id": 1, "person_id": 42, "role": "requester"},
                      {"case_id": 1, "person_id": 43, "role": "watcher"}])
    _wire(monkeypatch, pool, response=FOLLOW)

    assert await triage._close_case(pool, 1, "done")

    new = [c for c in pool.cases.values() if c["id"] != 1]
    assert len(new) == 1
    child = new[0]
    assert child["parent_case_id"] == 1
    assert child["origin_chat_jid"] == "777@g.us"        # origin copied
    assert child["opened_reason"] == "chained from case 1"
    assert child["title"] == "Pay INV-2041"
    assert child["status"] == "open"
    # requester copied, watcher not
    copied = [p for p in pool.participants if p["case_id"] == child["id"]]
    assert copied == [{"case_id": child["id"], "person_id": 42, "role": "requester"}]
    # expectation hangs on the NEW case with a parsed due_at + resolved owner
    assert len(pool.expectations) == 1
    exp = pool.expectations[0]
    assert exp["case_id"] == child["id"]
    assert exp["owner_id"] == 7
    assert exp["due_at"] == datetime(2026, 9, 15, tzinfo=timezone.utc)
    assert exp["satisfy_hint"] == "payment confirmed in the group"


async def test_null_follow_on_creates_nothing(monkeypatch):
    pool = FakeChainPool([_case(1)])
    _wire(monkeypatch, pool, response={"follow_on": None})

    assert await triage._close_case(pool, 1, "done")
    assert len(pool.cases) == 1
    assert pool.expectations == []


async def test_garbage_follow_on_creates_nothing(monkeypatch):
    pool = FakeChainPool([_case(1)])
    _wire(monkeypatch, pool, response={"follow_on": {"title": "   "}})

    assert await triage._close_case(pool, 1, "done")
    assert len(pool.cases) == 1                  # blank title → no case


async def test_model_failure_never_blocks_the_close(monkeypatch):
    pool = FakeChainPool([_case(1)])
    _wire(monkeypatch, pool, model_raises=True)

    assert await triage._close_case(pool, 1, "done") is True   # close still wins
    assert pool.cases[1]["status"] == "closed"
    assert len(pool.cases) == 1                                # and no half-chain

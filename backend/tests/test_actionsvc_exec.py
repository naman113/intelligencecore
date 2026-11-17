"""actionsvc._execute dispatch — the group_question executor (pure logic).

An approved ask-in-group proposal must actually reach the group on approval;
a missing executor would mark it executed while sending nothing.
"""

import pytest

from app import actionsvc

pytestmark = pytest.mark.asyncio


async def test_group_question_sends_and_returns_send_result(monkeypatch):
    sent = []

    async def fake_send(chat, body):
        sent.append((chat, body))
        return True

    monkeypatch.setattr(actionsvc.whatsapp, "send_message", fake_send)
    assert await actionsvc._execute("group_question", {"chat": "x@g.us", "body": "q"}) is True
    assert sent == [("x@g.us", "q")]


async def test_group_question_propagates_send_failure(monkeypatch):
    async def fake_send(chat, body):
        return False

    monkeypatch.setattr(actionsvc.whatsapp, "send_message", fake_send)
    assert await actionsvc._execute("group_question", {"chat": "x@g.us", "body": "q"}) is False


async def test_group_question_missing_fields_never_sends(monkeypatch):
    async def fake_send(chat, body):  # pragma: no cover — must not run
        raise AssertionError("send_message must not be called")

    monkeypatch.setattr(actionsvc.whatsapp, "send_message", fake_send)
    assert await actionsvc._execute("group_question", {"body": "q"}) is False
    assert await actionsvc._execute("group_question", {"chat": "x@g.us"}) is False
    assert await actionsvc._execute("group_question", {}) is False


# -- Phase 2: context_change executor and approval flow ---------------------

async def test_context_change_dispatches_to_contextstore_apply(monkeypatch):
    from app import contextstore
    calls = []

    async def fake_apply(payload):
        calls.append(payload)
        return calls_result[0]

    calls_result = [True]
    monkeypatch.setattr(contextstore, "apply", fake_apply)
    assert await actionsvc._execute("context_change", {"kind": "rule"}) is True
    calls_result[0] = False
    assert await actionsvc._execute("context_change", {"kind": "rule"}) is False
    assert len(calls) == 2


class _ApprovePool:
    def __init__(self, kind="context_change"):
        self.claimed = False
        self.sql = []
        self.kind = kind

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        if "SET status = 'approved'" in sql:
            if self.claimed:
                return None
            self.claimed = True
            return {"id": args[0], "kind": self.kind,
                    "payload": '{"kind": "rule", "ref": "rule:x", "text": "t"}'}
        if "SET status = 'rejected'" in sql:
            return {"id": args[0], "kind": self.kind, "payload": "{}"}
        return None

    async def execute(self, sql, *args):
        self.sql.append(sql)


async def test_approve_context_change_applies_once(monkeypatch):
    from app import contextstore
    pool = _ApprovePool()
    applied = []

    async def fake_apply(payload):
        applied.append(payload)
        return True

    async def fake_pool():
        return pool

    monkeypatch.setattr(contextstore, "apply", fake_apply)
    monkeypatch.setattr(actionsvc, "get_pool", fake_pool)
    assert (await actionsvc.approve(5))["status"] == "executed"
    assert "error" in await actionsvc.approve(5)
    assert len(applied) == 1 and applied[0]["ref"] == "rule:x"


async def test_reject_context_change_never_applies(monkeypatch):
    from app import contextstore
    pool = _ApprovePool()

    async def fake_apply(payload):  # pragma: no cover
        raise AssertionError("apply must not run on reject")

    async def fake_pool():
        return pool

    monkeypatch.setattr(contextstore, "apply", fake_apply)
    monkeypatch.setattr(actionsvc, "get_pool", fake_pool)
    assert await actionsvc.reject(5) == {"rejected": 5}
    assert len(pool.sql) == 1



# -- Phase 7: create_watch executor and approval flow --

async def test_create_watch_dispatches_to_insights(monkeypatch):
    from app import insights
    seen = []
    results = iter([True, False])

    async def fake(payload):
        seen.append(payload)
        return next(results)

    monkeypatch.setattr(insights, "create_watch_from_proposal", fake)
    assert await actionsvc._execute("create_watch", {"proposal_key": "k"}) is True
    assert await actionsvc._execute("create_watch", {"proposal_key": "k2"}) is False
    assert [p["proposal_key"] for p in seen] == ["k", "k2"]


async def test_approve_create_watch_executes_once(monkeypatch):
    from app import insights
    pool = _ApprovePool(kind="create_watch")
    ran = []

    async def fake(payload):
        ran.append(payload)
        return True

    async def fake_pool():
        return pool

    monkeypatch.setattr(insights, "create_watch_from_proposal", fake)
    monkeypatch.setattr(actionsvc, "get_pool", fake_pool)
    assert (await actionsvc.approve(5))["status"] == "executed"
    assert "error" in await actionsvc.approve(5)
    assert len(ran) == 1


async def test_reject_create_watch_never_executes(monkeypatch):
    from app import insights
    pool = _ApprovePool(kind="create_watch")

    async def fake(payload):  # pragma: no cover
        raise AssertionError("must not run on reject")

    async def fake_pool():
        return pool

    monkeypatch.setattr(insights, "create_watch_from_proposal", fake)
    monkeypatch.setattr(actionsvc, "get_pool", fake_pool)
    assert await actionsvc.reject(5) == {"rejected": 5}


def test_create_watch_branch_precedes_fallback():
    import inspect
    src = inspect.getsource(actionsvc._execute)
    assert (src.index('if kind == "context_change":') < src.index('if kind == "create_watch":')
            < src.index("has no live executor"))

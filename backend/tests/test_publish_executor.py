"""Phase 6: publish_dashboard executor (apply_publish_proposal) and actionsvc branch.
No DB, LLM or network: recording fake connection and pool."""

import asyncio

import pytest

from app import actionsvc
from app import dashboards as d
from app.dashboards import DashboardError


class _Tx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class Conn:
    def __init__(self, row):
        self.row = row
        self.sql = []

    def transaction(self):
        return _Tx()

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        return self.row

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        return []


def _payload(**kw):
    p = {"slug": "sales", "dashboard_id": 7, "version_id": 11, "version": 2,
         "title": "Sales", "origin": "agent"}
    p.update(kw)
    return p


@pytest.fixture
def publish_calls(monkeypatch):
    calls = []

    async def fake_publish(conn, **kw):
        calls.append(kw)
        return {"changed": True}

    monkeypatch.setattr(d, "publish", fake_publish)
    return calls


def run(coro):
    return asyncio.run(coro)


def test_consistent_payload_publishes(publish_calls):
    conn = Conn({"slug": "sales", "version": 2})
    assert run(d.apply_publish_proposal(conn, _payload())) is True
    assert publish_calls == [{"dashboard_id": 7, "version_id": 11, "expect_status": ("draft",)}]


def test_already_current_is_idempotent(monkeypatch):
    async def fake_publish(conn, **kw):
        return {"changed": False}
    monkeypatch.setattr(d, "publish", fake_publish)
    assert run(d.apply_publish_proposal(Conn({"slug": "sales", "version": 2}), _payload())) is True


def test_retired_conflict_propagates(monkeypatch):
    async def fake_publish(conn, **kw):
        raise DashboardError(409, "version 2 is retired, expected draft")
    monkeypatch.setattr(d, "publish", fake_publish)
    with pytest.raises(DashboardError) as ei:
        run(d.apply_publish_proposal(Conn({"slug": "sales", "version": 2}), _payload()))
    assert ei.value.status == 409


@pytest.mark.parametrize("payload,row", [
    ("nope", {"slug": "sales", "version": 2}),
    (_payload(dashboard_id=None), {"slug": "sales", "version": 2}),
    (_payload(version_id="x"), {"slug": "sales", "version": 2}),
    (_payload(dashboard_id=True), {"slug": "sales", "version": 2}),
    (_payload(slug="Bad Slug!"), {"slug": "sales", "version": 2}),
    (_payload(), None),
    (_payload(slug="other"), {"slug": "sales", "version": 2}),
    (_payload(version=3), {"slug": "sales", "version": 2}),
])
def test_bad_payload_never_publishes(publish_calls, payload, row):
    with pytest.raises(DashboardError) as ei:
        run(d.apply_publish_proposal(Conn(row), payload))
    assert ei.value.status in (409, 422)
    assert publish_calls == []


class _Acq:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acq(self.conn)


def test_execute_branch_routes_to_executor(monkeypatch):
    seen = {}
    conn = object()

    async def fake_apply(c, payload):
        seen["c"], seen["p"] = c, payload
        return True

    async def fake_pool():
        return Pool(conn)

    monkeypatch.setattr(d, "apply_publish_proposal", fake_apply)
    monkeypatch.setattr(actionsvc, "get_pool", fake_pool)
    assert run(actionsvc._execute("publish_dashboard", _payload())) is True
    assert seen["c"] is conn and seen["p"]["slug"] == "sales"


def test_approve_not_proposed_never_executes(monkeypatch):
    called = []

    class P:
        async def fetchrow(self, *a):
            return None

    async def fake_pool():
        return P()

    async def fake_exec(kind, payload):
        called.append(kind)
        return True

    monkeypatch.setattr(actionsvc, "get_pool", fake_pool)
    monkeypatch.setattr(actionsvc, "_execute", fake_exec)
    res = run(actionsvc.approve(5))
    assert res == {"error": "action is not in a proposed state"}
    assert called == []


# ---- Quick 029 F4: an approved proposal for a lint-failing version is refused ----
def test_approved_proposal_for_lint_failing_version_refused():
    import json as _json

    from _lintclean import BAD_PAGE, QUERIES

    class C:
        def __init__(self):
            self.sql = []

        def transaction(self):
            return _Tx()

        async def fetchrow(self, sql, *a):
            self.sql.append(sql)
            if "FROM dashboards d" in sql:
                return {"slug": "sales", "version": 2}
            if "FROM dashboards WHERE id" in sql:
                return {"id": 7, "current_version_id": 5}
            return {"id": 11, "version": 2, "title": "Sales", "status": "draft",
                    "code": BAD_PAGE, "queries": _json.dumps(QUERIES)}

        async def execute(self, sql, *a):
            self.sql.append(sql)

    c = C()
    with pytest.raises(DashboardError) as ei:
        run(d.apply_publish_proposal(c, _payload()))
    assert ei.value.status == 422 and "cannot publish version 2" in str(ei.value)
    assert not any(s.lstrip().startswith("UPDATE") for s in c.sql)


def test_older_proposal_than_live_refused_without_publishing(publish_calls):
    conn = Conn({"slug": "sales", "version": 1})
    conn.row = {"slug": "sales", "version": 1}
    live = {"version": 4}
    rows = [{"slug": "sales", "version": 1}, {"is_html": False}, live]

    async def fetchrow(sql, *a):
        return rows.pop(0)

    conn.fetchrow = fetchrow
    with pytest.raises(DashboardError) as ei:
        run(d.apply_publish_proposal(conn, _payload(version=1)))
    assert ei.value.status == 409
    assert str(ei.value) == ("v1 is older than the live v4, so it was not published. "
                             "To go back to it, use Roll back on the Dashboards page.")
    assert publish_calls == []


def test_publish_closes_lower_version_proposals(publish_calls):
    conn = Conn({"slug": "sales", "version": 4})
    run(d.apply_publish_proposal(conn, _payload(version=4)))
    closing = [q for q in conn.sql if "superseded_by_version" in q]
    assert len(closing) == 1 and "status = 'rejected'" in closing[0]
    assert "decided_by" not in closing[0]

"""Phase 8: /api/ask validation, single transaction, auth."""
import re
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main
from app import askapi


class Conn:
    def __init__(self, log):
        self.log = log

    def transaction(self):
        log = self.log

        class T:
            async def __aenter__(s):
                log.append("BEGIN")

            async def __aexit__(s, *a):
                log.append("COMMIT")
                return False
        return T()

    async def execute(self, sql, *args):
        self.log.append((" ".join(sql.split()), args))

    async def fetchval(self, sql, *args):
        self.log.append((" ".join(sql.split()), args))
        return 11 if "messages" in sql else 22

    # Quick 003: GET reads through one connection (chat check, messages, run state).
    async def fetchrow(self, sql, *args):
        if "deleted_at FROM threads" in sql:
            return None
        return {"id": 22, "status": "queued", "error": None, "message_id": 11,
                "created_at": None, "started_at": None, "ended_at": None}

    async def fetch(self, sql, *args):
        if "FROM messages" in sql:
            self.pool.limit = args[1]
            return self.pool.rows
        if "JOIN tool_calls" in sql:  # Quick 007: reply attachments
            return []
        return [await self.fetchrow(sql)]


class Pool:
    def __init__(self):
        self.log = []
        self.rows = [{"id": 2, "role": "assistant", "content": "b", "created_at": 1},
                     {"id": 1, "role": "user", "content": "a", "created_at": 0}]
        self.limit = None

    def acquire(self):
        conn = Conn(self.log)
        conn.pool = self

        class A:
            async def __aenter__(s):
                return conn

            async def __aexit__(s, *a):
                return False
        return A()

    async def fetch(self, sql, *args):
        self.limit = args[1]
        return self.rows

    async def fetchval(self, sql, *args):
        return True

    async def fetchrow(self, sql, *args):
        return {"id": 22, "status": "queued", "error": None}


def _wire(monkeypatch):
    pool = Pool()

    async def gp():
        return pool
    monkeypatch.setattr(askapi, "db", SimpleNamespace(get_pool=gp))
    return pool


async def test_ask_queues_in_one_transaction(monkeypatch):
    pool = _wire(monkeypatch)
    out = await askapi.ask({"message": "Build a dashboard\x00: open\x07 invoices"})
    assert out == {"thread_id": "ui:owner", "message_id": 11, "run_id": 22}
    assert pool.log[0] == "BEGIN" and pool.log[-1] == "COMMIT"
    stmts = [x for x in pool.log if isinstance(x, tuple)]
    assert len(stmts) == 3
    assert stmts[1][1] == ("ui:owner", "Build a dashboard: open invoices")


async def test_ask_scrubs_pasted_secret(monkeypatch):
    pool = _wire(monkeypatch)
    await askapi.ask({"message": "Connect mysql://root:hunter2pw@db.example.invalid:3306/ops as leaky"})
    stmts = [x for x in pool.log if isinstance(x, tuple)]
    assert len(stmts) == 3
    stored = stmts[1][1][1]
    assert "hunter2pw" not in stored
    assert "[secret removed: enter it on the credentials form]" in stored
    assert "INSERT INTO task_runs" in stmts[2][0]


@pytest.mark.parametrize("body", [None, {}, {"message": "  "}, {"message": "x" * 4001},
                                  {"message": "hi", "operator": "abc123"}])
async def test_ask_422_no_write(monkeypatch, body):
    pool = _wire(monkeypatch)
    with pytest.raises(HTTPException) as e:
        await askapi.ask(body)
    assert e.value.status_code == 422 and pool.log == []


async def test_thread_oldest_first_and_clamp(monkeypatch):
    pool = _wire(monkeypatch)
    out = await askapi.thread(limit=9999)
    assert pool.limit == 200
    assert [m["id"] for m in out["messages"]] == [1, 2]
    assert out["pending"] is True and out["last_run"]["id"] == 22
    await askapi.thread(limit=0)
    assert pool.limit == 1


def test_no_outbound_imports():
    src = open(askapi.__file__, encoding="utf-8").read()
    assert not re.search(r"^\s*(from|import) .*(triage|whatsapp|connectors)", src, re.M)


def _routes():
    out = []
    for r in main.app.router.routes:
        if hasattr(r, "original_router"):
            inh = list(r.include_context.dependencies)
            for s in r.original_router.routes:
                out.append((s.path, inh + list(s.dependencies)))
    return out


def test_routes_require_ops_key():
    mine = [(p, d) for p, d in _routes() if p == "/api/ask"]
    assert len(mine) == 2
    for p, deps in mine:
        assert any(getattr(d, "dependency", None) is main._require_ops_key for d in deps)
    assert TestClient(main.app).post("/api/ask", json={"message": "x"}).status_code == 401

"""Quick 003: several Ask chats, live run status, task timeout setting. No DB,
no LLM; the SQL itself is exercised by test_ask_chats_db.py against Postgres."""
import asyncio
import inspect
import re
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main
from app import appchannel, askapi, configservice, insights, kpihistory, runstatus, worker
from app.agents import graph


# ── thread ids ──────────────────────────────────────────────────────────

def test_parse_chat():
    assert appchannel.parse_chat("ui:owner") == ("owner", None)
    assert appchannel.parse_chat("ui:owner:abcdefghij") == ("owner", "abcdefghij")
    assert appchannel.parse_chat("ui:ops_team-a:qwertyuiop") == ("ops_team-a", "qwertyuiop")
    for bad in ["ui:owner:abc", "ui:owner:abcdefghi1", "ui:Owner", "ui:owner:ABCDEFGHIJ",
                "15550000000", "dashboard", "ui:", "ui:owner:abcdefghij:x", None, 5,
                "ui:owner:abcdefghijk"]:
        assert appchannel.parse_chat(bad) is None, bad


def test_new_chat_thread_is_an_app_thread_without_digits():
    seen = set()
    for _ in range(50):
        t = appchannel.new_chat_thread("owner")
        assert appchannel.parse_chat(t) == ("owner", t.split(":")[2])
        assert appchannel.is_app_thread(t) and appchannel.is_reserved_thread(t)
        assert not re.search(r"\d", t)
        seen.add(t)
    assert len(seen) == 50
    with pytest.raises(ValueError):
        appchannel.new_chat_thread("bad1")


def test_same_owner():
    assert appchannel.main_thread_of("ui:owner:abcdefghij") == "ui:owner"
    assert appchannel.main_thread_of("ui:owner") == "ui:owner"
    assert appchannel.main_thread_of("15550000000") is None
    assert appchannel.same_owner("ui:owner", "ui:owner:abcdefghij")
    assert appchannel.same_owner("ui:owner:abcdefghij", "ui:owner:qwertyuiop")
    assert not appchannel.same_owner("ui:owner", "ui:alice")
    assert not appchannel.same_owner("ui:owner:abcdefghij", "ui:owner-x")
    assert appchannel.same_owner("15550000000", "15550000000")
    assert not appchannel.same_owner("15550000000", "ui:owner")
    assert not appchannel.same_owner(None, "ui:owner") and not appchannel.same_owner("", "")


# ── titles and list items ───────────────────────────────────────────────

def test_title_from():
    assert askapi.title_from("  How much  diesel\nyesterday? ") == "How much diesel yesterday?"
    assert askapi.title_from("[document received: pack.json] This file is a context pack.") == \
        "Document: pack.json"
    long = askapi.title_from("x" * 200)
    assert len(long) == askapi.TITLE_CHARS and long.endswith("…")
    assert askapi.title_from("") is None and askapi.title_from(None) is None


def test_chat_item_titles_unread_and_run(monkeypatch):
    main_t = "ui:owner"
    it = askapi.chat_item({"thread_id": main_t}, main_t)
    assert it["is_main"] and it["title"] == "Main chat" and not it["unread"] and it["run"] is None
    it = askapi.chat_item({"thread_id": "ui:owner:abcdefghij"}, main_t)
    assert not it["is_main"] and it["title"] == "New chat"
    row = {"thread_id": "ui:owner:abcdefghij", "first_message": "Fuel by barge", "title": None,
           "last_assistant_id": 9, "last_read_id": 4, "run_id": 3, "run_status": "running"}
    monkeypatch.setattr(runstatus, "step", lambda rid: "Running query 2")
    it = askapi.chat_item(row, main_t)
    assert it["title"] == "Fuel by barge" and not it["named"] and it["unread"]
    assert it["run"] == {"id": 3, "status": "running", "error": None, "step": "Running query 2"}
    # Never-read chats (before Quick 003) show nothing unread; a rename wins over the message.
    it = askapi.chat_item(dict(row, last_read_id=None, title="Barges", run_status="failed",
                               run_error="token budget exceeded"), main_t)
    assert not it["unread"] and it["title"] == "Barges" and it["named"]
    assert it["run"]["step"] is None and it["run"]["error"] == "token budget exceeded"
    assert not askapi.chat_item(dict(row, last_read_id=9), main_t)["unread"]


# ── validation (no DB reached) ─────────────────────────────────────────

class NoPool:
    async def get_pool(self):
        raise AssertionError("must not reach the database")


@pytest.fixture
def nodb(monkeypatch):
    monkeypatch.setattr(askapi, "db", NoPool())


@pytest.mark.parametrize("call", [
    lambda: askapi.ask({"message": "hi", "chat": "ui:owner:x"}),
    lambda: askapi.ask({"message": "hi", "chat": "15550000000"}),
    lambda: askapi.thread(chat="dashboard"),
    lambda: askapi.retry({"chat": "ui:owner", "run_id": "7"}),
    lambda: askapi.retry({"chat": "ui:owner", "run_id": True}),
    lambda: askapi.retry({"chat": "ui:owner"}),
    lambda: askapi.mark_read("ui:owner", {"upto": -1}),
    lambda: askapi.mark_read("ui:owner", {"upto": "5"}),
    lambda: askapi.rename_chat("ui:owner", {"title": "x" * 81}),
    lambda: askapi.rename_chat("ui:owner", {"title": 5}),
    lambda: askapi.create_chat({"operator": "Bad1"}),
    lambda: askapi.list_chats(operator="bad1"),
    lambda: askapi.delete_chat("ui:owner:abc"),
])
async def test_422_without_touching_the_db(nodb, call):
    with pytest.raises(HTTPException) as e:
        await call()
    assert e.value.status_code == 422


async def test_main_chat_cannot_be_deleted(nodb):
    with pytest.raises(HTTPException) as e:
        await askapi.delete_chat("ui:owner")
    assert e.value.status_code == 409 and "rename" in e.value.detail


def test_every_ask_route_needs_the_ops_key():
    paths = set()
    for r in main.app.router.routes:
        if hasattr(r, "original_router"):
            inh = list(r.include_context.dependencies)
            for s in r.original_router.routes:
                if s.path.startswith("/api/ask"):
                    paths.add(s.path)
                    deps = inh + list(s.dependencies)
                    assert any(getattr(d, "dependency", None) is main._require_ops_key
                               for d in deps), s.path
    assert {"/api/ask", "/api/ask/retry", "/api/ask/chats", "/api/ask/chats/{chat}",
            "/api/ask/chats/{chat}/read"} <= paths
    c = TestClient(main.app)
    assert c.get("/api/ask/chats").status_code == 401
    assert c.post("/api/ask/chats", json={}).status_code == 401
    assert c.delete("/api/ask/chats/ui:owner:abcdefghij").status_code == 401
    assert c.post("/api/ask/retry", json={}).status_code == 401


# ── live status ─────────────────────────────────────────────────────────

def test_describe_labels():
    assert runstatus.describe("run_sql", {"sql": "SELECT 1"}, 2) == "Running query 2"
    assert runstatus.describe("preview_dashboard", {}) == "Checking the dashboard draft"
    assert runstatus.describe("use_skill", {"name": "dashboard-authoring"}) == \
        "Reading the dashboard-authoring skill"
    assert runstatus.describe("use_skill", {"name": "<b>x</b>"}) == "Reading a skill"
    assert runstatus.describe("send_progress", {"text": "On it, pulling fuel data"}) == \
        "On it, pulling fuel data"
    assert len(runstatus.describe("send_progress", {"text": "y" * 500})) == runstatus.NOTE_CHARS
    assert runstatus.describe("some_new_tool", {}) == "Working: some new tool"


def test_sql_tables_keep_case_and_skip_ctes():
    sql = ('WITH d AS (SELECT * FROM "DailyFuelReports") SELECT * FROM d '
           'JOIN "Barges" b ON true JOIN public.machines m ON true')
    assert sorted(runstatus.sql_tables(sql)) == ["Barges", "DailyFuelReports", "machines"]
    assert runstatus.sql_tables("not sql at all ((") == []


def _cb(handler, method, *a, **k):
    getattr(handler, method)(*a, **k)


def test_step_lifecycle_through_callbacks():
    runstatus.start(901)
    try:
        assert runstatus.step(901) == runstatus.FIRST_STEP
        h = runstatus.RunStatus(901)
        d = uuid.uuid4()
        _cb(h, "on_tool_start", {"name": "delegate_to_data_agent"}, "", run_id=d,
            inputs={"task": "fuel"})
        assert runstatus.step(901) == "Asking the data specialist"
        q1 = uuid.uuid4()
        _cb(h, "on_tool_start", {"name": "run_sql"}, "", run_id=q1, parent_run_id=d,
            inputs={"sql": 'SELECT * FROM "DailyFuelReports"'})
        assert runstatus.step(901) == "Running query 1"
        _cb(h, "on_tool_end", "RULES CHECK (query not run yet). rule:x ...", run_id=q1)
        assert runstatus.step(901) == "Reading the rules for DailyFuelReports"
        q2 = uuid.uuid4()
        _cb(h, "on_tool_start", {"name": "run_sql"}, "", run_id=q2, parent_run_id=d,
            inputs={"sql": 'SELECT 1 FROM "DailyFuelReports"'})
        assert runstatus.step(901) == "Running query 2"
        _cb(h, "on_tool_end", SimpleNamespace(content='[{"n": 1}]'), run_id=q2)
        assert runstatus.step(901) == "Reading the results of query 2"  # the model works on them
        r = uuid.uuid4()
        _cb(h, "on_tool_start", {"name": "remember_fact"}, "", run_id=r, parent_run_id=d, inputs={})
        _cb(h, "on_tool_end", "ok", run_id=r)
        assert runstatus.step(901) == "Asking the data specialist"  # no after-label: the open delegate
        _cb(h, "on_tool_end", "rows", run_id=d)
        assert runstatus.step(901) == "Putting the answer together"
        runstatus.note(901, runstatus.REASK_STEP)
        assert runstatus.step(901) == runstatus.REASK_STEP
        p = uuid.uuid4()
        _cb(h, "on_tool_start", {"name": "preview_dashboard"}, "", run_id=p, inputs={})
        _cb(h, "on_tool_end", "ok", run_id=p)
        assert runstatus.step(901) == "Checking the dashboard draft"  # stays while it reviews
        n = uuid.uuid4()
        _cb(h, "on_tool_start", {"name": "send_progress"}, "", run_id=n, inputs={"text": "Almost there"})
        _cb(h, "on_tool_end", "sent", run_id=n)
        assert runstatus.step(901) == "Almost there"
        e = uuid.uuid4()
        _cb(h, "on_tool_start", {"name": "get_document"}, "", run_id=e, inputs={})
        _cb(h, "on_tool_error", RuntimeError("x"), run_id=e)
        assert runstatus.step(901) == runstatus.THINKING
    finally:
        runstatus.clear(901)
    assert runstatus.step(901) is None
    # A handler for a run this process does not hold is inert.
    runstatus.RunStatus(902).on_tool_start({"name": "run_sql"}, "", run_id=uuid.uuid4(), inputs={})
    assert runstatus.step(902) is None


def test_release_current_and_bound():
    async def consumer(rid):
        runstatus.start(rid)
        assert runstatus.step(rid) == runstatus.FIRST_STEP
        runstatus.release_current()
        return runstatus.step(rid)
    assert asyncio.run(consumer(903)) is None
    for i in range(runstatus.MAX_LIVE + 10):
        runstatus.start(10_000 + i)
    assert len(runstatus._live) <= runstatus.MAX_LIVE
    for i in range(runstatus.MAX_LIVE + 10):
        runstatus.clear(10_000 + i)


def test_wiring():
    run_src = inspect.getsource(graph.run)
    assert "runstatus.RunStatus(ctx_run_id.get())" in run_src
    assert "if ctx_run_id.get() is not None" in run_src
    proc = inspect.getsource(worker._process_one)
    assert proc.index("runstatus.start(task[\"id\"])") < proc.index("heartbeat.busy(")
    assert "runstatus.release_current()" in inspect.getsource(worker._consume)
    assert "runstatus.note(run_id, runstatus.REASK_STEP)" in inspect.getsource(
        worker._guard_ungrounded)
    assert "appchannel.same_owner(" in inspect.getsource(worker._guard_false_watch)


# ── run state ───────────────────────────────────────────────────────────

class StateConn:
    def __init__(self, active, last=None, running=0):
        self.active, self.last, self.running = active, last, running

    async def fetch(self, sql, *args):
        return self.active

    async def fetchrow(self, sql, *args):
        return self.last

    async def fetchval(self, sql, *args):
        return self.running


def _run(i, status, **k):
    return dict({"id": i, "status": status, "error": None, "message_id": 1,
                 "created_at": None, "started_at": None, "ended_at": None}, **k)


async def test_run_state(monkeypatch):
    monkeypatch.setattr(askapi, "get_settings", lambda: SimpleNamespace(worker_concurrency=3))
    monkeypatch.setattr(runstatus, "step", lambda rid: "Running query 1" if rid == 5 else None)
    s = await askapi._run_state(StateConn([_run(5, "running"), _run(6, "queued")]), "ui:owner")
    assert s["id"] == 5 and s["step"] == "Running query 1" and s["queued_behind"] == 1
    s = await askapi._run_state(StateConn([_run(7, "running")]), "ui:owner")
    assert s["step"] == runstatus.FIRST_STEP  # restarted process holds nothing yet
    s = await askapi._run_state(StateConn([_run(8, "queued")], running=3), "ui:owner")
    assert s["step"] == "Waiting for a free worker (3 answers running)"
    s = await askapi._run_state(StateConn([_run(8, "queued")], running=1), "ui:owner")
    assert s["step"] == "Starting"
    s = await askapi._run_state(StateConn([], _run(9, "failed", error="timed out after 600 s")),
                                "ui:owner")
    assert s["status"] == "failed" and s["step"] is None and s["error"].startswith("timed out")
    assert await askapi._run_state(StateConn([], None), "ui:owner") is None


# ── scope: an owner's chats are one conversation ────────────────────────

class WatchPool:
    def __init__(self, row):
        self.row = row

    async def fetchrow(self, sql, *args):
        return self.row


async def test_watch_in_scope_owner_chats(monkeypatch):
    row = {"id": 4, "title": "t", "kind": "user", "status": "active", "thread_id": "ui:owner"}

    async def gp():
        return WatchPool(row)
    monkeypatch.setattr(kpihistory, "get_pool", gp)
    assert (await kpihistory.watch_in_scope(4, "ui:owner:abcdefghij"))["id"] == 4
    assert await kpihistory.watch_in_scope(4, "ui:alice") is None
    assert await kpihistory.watch_in_scope(4, "15550000000") is None


# ── Quick 003 scope addition: task timeout setting ──────────────────────

def test_task_timeout_setting(monkeypatch):
    from app.config import Settings
    assert Settings.model_fields["task_timeout_seconds"].default == 600
    reg = configservice.REGISTRY["task_timeout_seconds"]
    assert reg["group"] == "tuning" and not reg["secret"] and (reg["min"], reg["max"]) == (120, 7200)
    for v in ("119", "7201", "abc", "1.5"):
        with pytest.raises(ValueError):
            configservice._validate_value("task_timeout_seconds", v)
    for v in ("120", "1800", "7200"):
        configservice._validate_value("task_timeout_seconds", v)

    def settings(t, pack=1800):
        monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(
            task_timeout_seconds=t, context_pack_timeout_seconds=pack))
    settings(1500)
    assert worker.task_timeout() == 1500
    assert worker._task_timeout("how are sales?") == 1500
    assert worker._task_timeout("[document received: pack.json] x") == 1800
    settings(2400)
    assert worker._task_timeout("[document received: pack.json] x") == 2400  # never below the task timeout
    settings(30)
    assert worker.task_timeout() == worker.TASK_TIMEOUT_MIN  # an env value is clamped too
    settings(99_999)
    assert worker.task_timeout() == worker.TASK_TIMEOUT_MAX


def test_timeout_wiring():
    proc = inspect.getsource(worker._process_one)
    assert "TASK_TIMEOUT_SECONDS" not in proc
    assert 'heartbeat.busy("worker", task_timeout() + 300)' in proc
    assert 'heartbeat.busy("worker", timeout + 300)' in proc
    assert "timeout=task_timeout()" in proc and "timeout=timeout," in proc
    assert 'error = f"timed out after {_task_timeout(' in proc

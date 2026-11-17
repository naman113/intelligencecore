"""Quick 021: every reply to a WhatsApp message quotes that message; proactive sends
never do. The worker sets a reply context for one user run (whatsapp.reply_target);
send_message / send_document add `replyTo` only for the run's own thread; the
connector builds the quote (wa-connector/quotes.js, tested with node --test).
Also: send_document reaches group and private-ID threads, and the worker's assistant
rows carry reply_to for the web chat. Made-up numbers and ids; no DB or network."""

import asyncio
from types import SimpleNamespace

import pytest

from app import sandbox, whatsapp, worker
from app.agents import graph

DM = "15550001111"
GROUP = "120363000000000001@g.us"
LID_THREAD = "987650000012345@lid"
WA_ID = "3EB0C0FFEE0000000001"


# ── quote_text ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("stored,expected", [
    ("how many cases are open", "how many cases are open"),
    ('[replying to the assistant: "Want me to check?"] yes please', "yes please"),
    ("[document received: invoice.pdf] Extract this document and propose the data entry.",
     "invoice.pdf"),
    ("[document received: invoice.pdf] what is the total?", "what is the total?"),
    ("", ""),
])
def test_quote_text_is_what_the_person_wrote(stored, expected):
    assert whatsapp.quote_text(stored) == expected


def test_quote_text_is_capped():
    out = whatsapp.quote_text("y" * 1000)
    assert out == "y" * whatsapp.QUOTE_TEXT_CHARS + "…"


# ── reply_target ─────────────────────────────────────────────────────────

class TargetPool:
    def __init__(self, row, sender=None, raises=False):
        self.row, self.sender, self.raises = row, sender, raises
        self.sql = []

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        if self.raises:
            raise RuntimeError("db down")
        return self.row

    async def fetchval(self, sql, *args):
        self.sql.append(sql)
        assert "provenance = 'external'" in sql and args == (WA_ID,)
        return self.sender


async def test_reply_target_dm():
    pool = TargetPool({"thread_id": DM, "content": "hi", "wa_message_id": WA_ID})
    assert await whatsapp.reply_target(pool, 37, DM) == {"thread_id": DM, "id": WA_ID, "text": "hi"}


async def test_reply_target_group_names_chat_and_sender():
    pool = TargetPool({"thread_id": GROUP, "content": "@bot status?", "wa_message_id": WA_ID},
                      sender=LID_THREAD)
    assert await whatsapp.reply_target(pool, 37, GROUP) == {
        "thread_id": GROUP, "id": WA_ID, "text": "@bot status?", "chat": GROUP,
        "participant": LID_THREAD}


@pytest.mark.parametrize("row,thread", [
    ({"thread_id": DM, "content": "x", "wa_message_id": None}, DM),        # upload / console
    ({"thread_id": "15550002222", "content": "x", "wa_message_id": WA_ID}, DM),  # other thread
    (None, DM),
    ({"thread_id": "ui:owner", "content": "x", "wa_message_id": WA_ID}, "ui:owner"),  # app chat
])
async def test_reply_target_none(row, thread):
    assert await whatsapp.reply_target(TargetPool(row), 37, thread) is None


async def test_reply_target_fail_soft():
    assert await whatsapp.reply_target(TargetPool(None, raises=True), 37, DM) is None
    assert await whatsapp.reply_target(object(), 37, DM) is None  # no fetchrow at all
    assert await whatsapp.reply_target(TargetPool(None), None, DM) is None


# ── send_message / send_document payloads ────────────────────────────────

class _Res:
    is_success = True


class _Client:
    posted: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, path, json=None, headers=None):
        _Client.posted.append((path, json))
        return _Res()


@pytest.fixture
def connector(monkeypatch):
    _Client.posted = []

    async def same(target):
        return target

    async def no_record(phone, text):
        return None

    monkeypatch.setattr(whatsapp.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(whatsapp, "_recipient", same)
    monkeypatch.setattr(whatsapp, "_record_self", no_record)
    monkeypatch.setattr(whatsapp, "get_settings",
                        lambda: SimpleNamespace(wa_api_base="http://c", wa_api_key="k"))
    return _Client.posted


async def test_send_to_the_run_thread_quotes(connector, tmp_path):
    token = whatsapp.set_reply_context({"thread_id": DM, "id": WA_ID, "text": "hi"})
    try:
        assert await whatsapp.send_message(DM, "Hello!")
        f = tmp_path / "r.pdf"
        f.write_bytes(b"%PDF-1.4")
        assert await whatsapp.send_document(DM, str(f), "report")
    finally:
        whatsapp.reset_reply_context(token)
    (p1, b1), (p2, b2) = connector
    assert p1 == "/api/message" and b1["replyTo"] == {"id": WA_ID, "text": "hi"}
    assert p2 == "/api/send-document" and b2["replyTo"] == {"id": WA_ID, "text": "hi"}


async def test_group_reply_carries_chat_and_participant(connector):
    ctx = {"thread_id": GROUP, "id": WA_ID, "text": "status?", "chat": GROUP,
           "participant": DM}
    token = whatsapp.set_reply_context(ctx)
    try:
        await whatsapp.send_message(GROUP, "All good.")
    finally:
        whatsapp.reset_reply_context(token)
    (_, body), = connector
    assert body["phone"] == GROUP
    assert body["replyTo"] == {"id": WA_ID, "text": "status?", "chat": GROUP, "participant": DM}


async def test_other_recipients_and_no_context_are_never_quoted(connector):
    token = whatsapp.set_reply_context({"thread_id": DM, "id": WA_ID, "text": "hi"})
    try:
        await whatsapp.send_message("15550002222", "relayed message")   # send_message_to_person
    finally:
        whatsapp.reset_reply_context(token)
    await whatsapp.send_message(DM, "a later alert")                    # after the run
    assert all("replyTo" not in body for _, body in connector)
    assert whatsapp._reply_ctx.get() is None


async def test_app_threads_send_nothing(connector):
    token = whatsapp.set_reply_context({"thread_id": "ui:owner", "id": WA_ID, "text": "x"})
    try:
        assert await whatsapp.send_message("ui:owner", "x") is False
    finally:
        whatsapp.reset_reply_context(token)
    assert connector == []


# ── the worker sets the context for one run, and only that run ───────────

class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class WorkerConn:
    def __init__(self, pool):
        self.pool = pool

    async def fetchrow(self, sql, *args):
        if "UPDATE task_runs SET status = 'running'" in sql:
            return {"id": 19, "thread_id": self.pool.thread, "message_id": 37, "kind": "user",
                    "playbook_id": None, "case_id": None, "expectation_id": None}
        if "SELECT content FROM messages" in sql:
            return {"content": "how many cases are open"}
        return None

    async def fetch(self, sql, *args):
        return []

    async def fetchval(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        self.pool.executed.append((sql, args))


class WorkerPool:
    def __init__(self, thread=DM, wa_id=WA_ID):
        self.thread, self.wa_id = thread, wa_id
        self.executed = []
        self.conn = WorkerConn(self)

    def acquire(self):
        return _Acquire(self.conn)

    async def fetchrow(self, sql, *args):
        if "wa_message_id FROM messages" in sql:
            return {"thread_id": self.thread, "content": "how many cases are open",
                    "wa_message_id": self.wa_id}
        return None

    async def fetch(self, sql, *args):
        return []

    async def fetchval(self, sql, *args):
        if "FROM events" in sql:
            return "15550003333"
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def _wire_worker(monkeypatch, pool, run_impl):
    async def gp():
        return pool

    async def none(*a, **k):
        return None

    async def ctx(*a, **k):
        return ""

    async def ctx_provenance(*a, **k):  # Quick 019's assembler: (text, memory used)
        return "", {}

    seen = []

    async def send(to, text):
        seen.append((to, text, whatsapp._reply_to(to)))
        return True

    monkeypatch.setattr(worker, "get_pool", gp)
    monkeypatch.setattr(worker, "_handle_chat_onboarding", none)
    monkeypatch.setattr(worker, "_handle_watch_confirmation", none)
    monkeypatch.setattr(worker.contextassembler, "assemble", ctx)
    monkeypatch.setattr(worker.contextassembler, "assemble_with_provenance", ctx_provenance,
                        raising=False)
    monkeypatch.setattr(worker.graph, "run", run_impl)
    monkeypatch.setattr(worker.whatsapp, "send_message", send)
    monkeypatch.setattr(worker.memory, "reflect_and_store", none)
    return seen


async def test_worker_quotes_its_answer_and_records_reply_to(monkeypatch):
    pool = WorkerPool()

    async def run(history, msg, ctx=""):
        await whatsapp.send_message(DM, "On it")          # a progress ping inside the run
        await whatsapp.send_message("15550002222", "fyi")  # a relay to someone else
        return "There are 3 open cases."

    seen = _wire_worker(monkeypatch, pool, run)
    assert await worker._process_one() is True
    quote = {"id": WA_ID, "text": "how many cases are open"}
    assert seen == [(DM, "On it", quote), ("15550002222", "fyi", None),
                    (DM, "There are 3 open cases.", quote)]
    assert whatsapp._reply_ctx.get() is None             # reset after the run
    insert = next(a for s, a in pool.executed if "INSERT INTO messages" in s)
    assert insert == (DM, "There are 3 open cases.", 37)  # reply_to = the question


async def test_worker_group_quote_names_the_sender(monkeypatch):
    pool = WorkerPool(thread=GROUP)

    async def run(history, msg, ctx=""):
        return "All good."

    seen = _wire_worker(monkeypatch, pool, run)
    await worker._process_one()
    (to, _, quote), = seen
    assert to == GROUP and quote["chat"] == GROUP and quote["participant"] == "15550003333"


async def test_worker_failure_notice_is_quoted_and_context_cleared(monkeypatch):
    pool = WorkerPool()

    async def boom(history, msg, ctx=""):
        raise RuntimeError("model down")

    seen = _wire_worker(monkeypatch, pool, boom)
    await worker._process_one()
    (to, text, quote), = seen
    assert text.startswith("Something went wrong") and quote["id"] == WA_ID
    assert whatsapp._reply_ctx.get() is None


async def test_worker_without_whatsapp_id_sends_unquoted(monkeypatch):
    pool = WorkerPool(wa_id=None)

    async def run(history, msg, ctx=""):
        return "ok"

    seen = _wire_worker(monkeypatch, pool, run)
    await worker._process_one()
    assert seen == [(DM, "ok", None)]


async def test_a_proactive_send_during_a_run_is_not_quoted(monkeypatch):
    """A sentinel alert to the same chat while a run is working: another asyncio task,
    so it never sees the run's reply context."""
    pool = WorkerPool()
    started, release = asyncio.Event(), asyncio.Event()

    async def run(history, msg, ctx=""):
        started.set()
        await release.wait()
        return "done"

    seen = _wire_worker(monkeypatch, pool, run)
    work = asyncio.create_task(worker._process_one())
    await started.wait()
    await whatsapp.send_message(DM, "🟠 Watchdog — alert")   # the patched send records it
    release.set()
    await work
    assert (DM, "🟠 Watchdog — alert", None) in seen
    assert (DM, "done", {"id": WA_ID, "text": "how many cases are open"}) in seen


# ── send_document reaches group and private-ID threads ───────────────────

@pytest.mark.parametrize("thread,expected", [
    (DM, True), (GROUP, True), ("1203630000-1600000000@g.us", True), (LID_THREAD, True),
    ("dashboard", False), ("inbox", False), ("case-7", False), ("playbook-global", False),
    ("ui:owner", False), ("x@g.us", False), ("", False), ("@lid", False),
])
def test_is_wa_thread(thread, expected):
    assert graph._is_wa_thread(thread) is expected


@pytest.mark.parametrize("thread", [GROUP, LID_THREAD])
async def test_send_document_tool_sends_to_groups_and_private_ids(monkeypatch, tmp_path, thread):
    monkeypatch.setattr(sandbox, "ARTIFACTS_ROOT", tmp_path)
    monkeypatch.setattr(sandbox, "_carried", {})
    sends = []

    async def fake_send(phone, path, caption=""):
        sends.append(phone)
        return True

    async def fake_log(*a, **k):
        return None

    monkeypatch.setattr(graph.whatsapp, "send_document", fake_send)
    monkeypatch.setattr(graph, "_log_tool", fake_log)
    (tmp_path / "run_93").mkdir()
    (tmp_path / "run_93" / "report.pdf").write_bytes(b"%PDF-1.4")
    fn = graph.send_document.coroutine or graph.send_document.func
    t1, t2 = graph.ctx_run_id.set(93), graph.ctx_phone.set(thread)
    try:
        assert await fn(filename="report.pdf", caption="") == "sent"
    finally:
        graph.ctx_run_id.reset(t1)
        graph.ctx_phone.reset(t2)
    assert sends == [thread]

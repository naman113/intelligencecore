"""Reply anchoring (main.py::whatsapp_webhook) — the connector's `quoted`
field becomes a factual '[replying to WHO: "SNIPPET"] ' prefix on BOTH what
the reactive path records (messages row) and what triage ingests (event
text), so a bare "done" replied to an old message still says what it
answers. Pure logic: FakePool routed by SQL substring, no DB/LLM/network
(harness per test_webhook_eager_intake.py)."""

from types import SimpleNamespace

import app.main as main
from app import chats
from app.connectors import whatsapp_events


class _Req:
    def __init__(self, payload, secret="s"):
        self._payload = payload
        self.headers = {"x-webhook-secret": secret}

    async def json(self):
        return self._payload


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, person_name=None):
        self.person_name = person_name
        self.executes = []
        self.fetchvals = []

    async def execute(self, sql, *args):
        self.executes.append((sql, args))
        return "INSERT 0 1"

    async def fetchval(self, sql, *args):
        self.fetchvals.append((sql, args))
        if "INSERT INTO messages" in sql:
            return 11
        if "INSERT INTO task_runs" in sql:
            return 21
        if "FROM people" in sql:
            return self.person_name
        return None


class FakePool:
    def __init__(self, person_name=None):
        self.conn = FakeConn(person_name)

    def acquire(self):
        return _Acquire(self.conn)


def _wire(monkeypatch, *, person_name=None):
    pool = FakePool(person_name)

    async def fake_get_pool():
        return pool
    monkeypatch.setattr(main, "db", SimpleNamespace(get_pool=fake_get_pool))
    monkeypatch.setattr(
        main, "get_settings",
        lambda: SimpleNamespace(webhook_secret="s", allowlist=set(), whatsapp_open_to_anyone=True))

    async def fake_ensure_chat(jid, subject=None, is_group=True):
        return {"jid": jid, "status": "active", "is_group": is_group,
                "is_new": False, "members_synced_at": None}
    monkeypatch.setattr(chats, "ensure_chat", fake_ensure_chat)
    monkeypatch.setattr(chats, "maybe_sync_members", lambda row: False)

    ingested = []

    async def fake_ingest_inbound(**kw):
        ingested.append(kw)
        return 1
    monkeypatch.setattr(whatsapp_events, "ingest_inbound", fake_ingest_inbound)
    return pool, ingested


def _payload(**over):
    body = {
        "phone": "919000000103",
        "message": "done",
        "messageId": "wamid-r1",
        "chatId": "grp1@g.us",
        "isGroup": True,
        "mentioned": False,
        "groupSubject": "Ops",
        "quoted": {"text": "What is the status of the Aryan invoice?",
                   "author": "919000000101", "from_me": False},
    }
    body.update(over)
    return body


def _stored_message(pool) -> str:
    sql, args = next((s, a) for s, a in pool.conn.fetchvals
                     if "INSERT INTO messages" in s)
    return args[1]


async def test_reply_prefixes_both_pipelines(monkeypatch):
    pool, ingested = _wire(monkeypatch)

    await main.whatsapp_webhook(_Req(_payload()), x_webhook_secret="s")

    stored = _stored_message(pool)
    assert stored.startswith(
        '[replying to 919000000101: "What is the status of the Aryan invoice?"] ')
    assert stored.endswith("done"), "the reply's own text must survive intact"
    assert ingested[0]["text"] == stored, \
        "triage must see the same anchored text the reactive path records"


async def test_reply_to_assistant_is_named_as_such(monkeypatch):
    pool, ingested = _wire(monkeypatch)

    await main.whatsapp_webhook(
        _Req(_payload(quoted={"text": "Please confirm the amount",
                              "author": "16478526030", "from_me": True})),
        x_webhook_secret="s")

    assert _stored_message(pool).startswith('[replying to the assistant: ')


async def test_registered_author_resolves_to_name(monkeypatch):
    pool, ingested = _wire(monkeypatch, person_name="Naman")

    await main.whatsapp_webhook(_Req(_payload()), x_webhook_secret="s")

    assert _stored_message(pool).startswith('[replying to Naman: ')


async def test_snippet_is_flattened_and_capped(monkeypatch):
    pool, ingested = _wire(monkeypatch)

    await main.whatsapp_webhook(
        _Req(_payload(quoted={"text": "line one\nline two\t " + "x" * 500,
                              "author": "919000000101", "from_me": False})),
        x_webhook_secret="s")

    stored = _stored_message(pool)
    head = stored.split('"] ')[0]
    assert "\n" not in head and "line one line two" in head
    assert len(head) < 250, "snippet must be capped (~200 chars)"


async def test_no_quote_leaves_text_untouched(monkeypatch):
    pool, ingested = _wire(monkeypatch)
    body = _payload()
    del body["quoted"]

    await main.whatsapp_webhook(_Req(body), x_webhook_secret="s")

    assert _stored_message(pool) == "done"
    assert "[replying" not in ingested[0]["text"]


async def test_empty_quote_text_is_ignored(monkeypatch):
    pool, ingested = _wire(monkeypatch)

    await main.whatsapp_webhook(
        _Req(_payload(quoted={"text": "  ", "author": "9", "from_me": False})),
        x_webhook_secret="s")

    assert _stored_message(pool) == "done"

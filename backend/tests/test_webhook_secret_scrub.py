"""Phase 15 gap 1 (15-18): webhook text, event text, quoted snippet and WhatsApp
event records are scrubbed of pasted secrets. Harness copied from test_webhook_reply_context."""
from types import SimpleNamespace

import app.main as main
from app import chats
from app.connectors import whatsapp_events
from app.secretbox import PASTED_SECRET_MARK as M, scrub_pasted_secrets


class _Req:
    def __init__(self, payload):
        self._payload = payload
        self.headers = {"x-webhook-secret": "s"}

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
    def __init__(self):
        self.fetchvals = []

    async def execute(self, sql, *args):
        return "INSERT 0 1"

    async def fetchval(self, sql, *args):
        self.fetchvals.append((sql, args))
        if "INSERT INTO messages" in sql:
            return 11
        if "INSERT INTO task_runs" in sql:
            return 21
        return None


class FakePool:
    def __init__(self):
        self.conn = FakeConn()

    def acquire(self):
        return _Acquire(self.conn)


def _wire(monkeypatch):
    pool = FakePool()

    async def fake_get_pool():
        return pool
    monkeypatch.setattr(main, "db", SimpleNamespace(get_pool=fake_get_pool))
    monkeypatch.setattr(main, "get_settings",
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


def _stored(pool):
    return next(a for s, a in pool.conn.fetchvals if "INSERT INTO messages" in s)[1]


def _payload(**over):
    body = {"phone": "919000000103", "message": "pwd=abc123 connect it",
            "messageId": "wamid-s1", "chatId": "grp1@g.us", "isGroup": True,
            "mentioned": False, "groupSubject": "Ops"}
    body.update(over)
    return body


async def test_webhook_text_scrubbed_in_messages_and_event(monkeypatch):
    pool, ingested = _wire(monkeypatch)
    await main.whatsapp_webhook(_Req(_payload()), x_webhook_secret="s")
    stored = _stored(pool)
    assert M in stored and "abc123" not in stored
    assert M in ingested[0]["text"] and "abc123" not in ingested[0]["text"]


async def test_quoted_snippet_scrubbed(monkeypatch):
    pool, ingested = _wire(monkeypatch)
    q = {"text": "use mysql://u:pw9@h/db", "author": "919000000101", "from_me": False}
    await main.whatsapp_webhook(_Req(_payload(message="ok", quoted=q)), x_webhook_secret="s")
    stored = _stored(pool)
    assert stored.startswith("[replying to ") and "pw9" not in stored and M in stored
    assert "pw9" not in ingested[0]["text"]


async def test_secret_free_text_unchanged(monkeypatch):
    pool, ingested = _wire(monkeypatch)
    await main.whatsapp_webhook(_Req(_payload(message="show sales by region")),
                                x_webhook_secret="s")
    assert _stored(pool) == "show sales by region"


async def test_event_records_scrubbed_and_echo_equal(monkeypatch):
    seen = []

    async def fake_ingest(ev):
        seen.append(ev)
        return 1
    monkeypatch.setattr(whatsapp_events, "ingest", fake_ingest)
    out = "use mysql://u:pw9@h/db"
    await whatsapp_events.record_self("919000000103", out)
    await whatsapp_events.ingest_inbound("919000000103", out, "w1")
    assert seen[0].content == seen[1].content == scrub_pasted_secrets(out)[0]
    assert "pw9" not in seen[0].content

"""Webhook eager document intake (main.py::whatsapp_webhook) — pure logic,
no DB/LLM/network (FakePool routed by SQL substring, per test_consolidation.py).

Pins the additive eager-intake block at the end of the webhook:
  * an attachment arriving in an ACTIVE group is handed to docintel.intake_bytes
    with the decoded bytes and thread_ref = the group jid, even when the message
    did not mention us (observe always, extract eagerly);
  * a pending (unapproved) chat persists NOTHING — being added to a group is not
    consent to read its files;
  * a suffix outside docintel.EAGER_EXTS (e.g. .zip) is never eagerly ingested.
"""

import base64
from types import SimpleNamespace

import app.main as main
from app import chats, docintel
from app.connectors import whatsapp_events


# ---------------------------------------------------------------- fakes

class _Req:
    """Minimal stand-in for the starlette Request the webhook consumes: the
    handler reads the secret from its Header() parameter and the body via
    an awaited .json()."""

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
    """Routes by SQL substring; deliberately minimal: the webhook needs a
    threads INSERT (execute), a messages INSERT returning an id, and a
    task_runs INSERT only when the message addressed us."""

    def __init__(self):
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
        return None


class FakePool:
    def __init__(self):
        self.conn = FakeConn()

    def acquire(self):
        return _Acquire(self.conn)


def _wire(monkeypatch, *, chat_status="active"):
    """Fake out every seam the webhook touches; returns (pool, ingested, intakes)."""
    pool = FakePool()

    async def fake_get_pool():
        return pool

    monkeypatch.setattr(main, "db", SimpleNamespace(get_pool=fake_get_pool))
    monkeypatch.setattr(
        main, "get_settings",
        lambda: SimpleNamespace(webhook_secret="s", allowlist=set(), whatsapp_open_to_anyone=True))

    async def fake_ensure_chat(jid, subject=None, is_group=True):
        return {"jid": jid, "status": chat_status, "is_group": is_group,
                "is_new": False, "members_synced_at": None}
    monkeypatch.setattr(chats, "ensure_chat", fake_ensure_chat)

    # Roster sync is fire-and-forget from the webhook; capture, don't run.
    syncs = []
    monkeypatch.setattr(chats, "maybe_sync_members",
                        lambda row: (syncs.append(row), False)[1])
    pool.syncs = syncs

    async def fake_ask():
        pass  # onboarding question path is not under test here
    monkeypatch.setattr(main, "_ask_primary_about_chat", fake_ask)

    ingested = []

    async def fake_ingest_inbound(**kw):
        ingested.append(kw)
        return 1
    monkeypatch.setattr(whatsapp_events, "ingest_inbound", fake_ingest_inbound)

    intakes = []

    async def fake_intake(data, filename, *, source, thread_ref):
        intakes.append({"data": data, "filename": filename,
                        "source": source, "thread_ref": thread_ref})
        return 7
    monkeypatch.setattr(docintel, "intake_bytes", fake_intake)

    return pool, ingested, intakes


def _payload(filename="invoice.pdf", data=b"%PDF-1.4 tiny", **over):
    body = {
        "phone": "971500000001",
        "message": "sharing the file",
        "messageId": "wamid-1",
        "chatId": "grp1@g.us",
        "isGroup": True,
        "mentioned": False,
        "groupSubject": "Ops",
        "document": {"filename": filename, "mimetype": "application/pdf",
                     "data": base64.b64encode(data).decode()},
    }
    body.update(over)
    return body


# ---------------------------------------------------------------- tests

async def test_active_group_attachment_unmentioned_is_eagerly_ingested(monkeypatch):
    pool, ingested, intakes = _wire(monkeypatch, chat_status="active")

    out = await main.whatsapp_webhook(_Req(_payload()), x_webhook_secret="s")

    assert out == {"queued": False, "observed": True, "chat_active": True}
    # exactly one intake, with the DECODED bytes and the group jid as thread_ref
    assert len(intakes) == 1
    assert intakes[0]["data"] == b"%PDF-1.4 tiny"
    assert intakes[0]["thread_ref"] == "grp1@g.us"
    assert intakes[0]["source"] == "whatsapp"
    assert intakes[0]["filename"] == "invoice.pdf"
    # not mentioned → observed only: no task_runs row may be created
    assert not any("INSERT INTO task_runs" in sql
                   for sql, _ in pool.conn.fetchvals)
    # the human-facing event still reached ambient ingestion, pending triage
    assert ingested and ingested[0]["triage_status"] == "pending"


async def test_pending_chat_persists_nothing(monkeypatch):
    pool, ingested, intakes = _wire(monkeypatch, chat_status="pending")

    # even a mentioned message in an unapproved group must not queue a reply
    # nor hand its attachment to docintel
    out = await main.whatsapp_webhook(
        _Req(_payload(mentioned=True)), x_webhook_secret="s")

    assert intakes == [], "unapproved chat must not persist the attachment"
    assert out["chat_active"] is False
    assert out["queued"] is False
    assert not any("INSERT INTO task_runs" in sql
                   for sql, _ in pool.conn.fetchvals)
    # the event is recorded for the timeline but marked skipped for triage
    assert ingested and ingested[0]["triage_status"] == "skipped"


async def test_non_eager_suffix_is_not_ingested(monkeypatch):
    pool, ingested, intakes = _wire(monkeypatch, chat_status="active")
    assert ".zip" not in docintel.EAGER_EXTS  # guard the premise

    out = await main.whatsapp_webhook(
        _Req(_payload(filename="archive.zip", data=b"PK not a document")),
        x_webhook_secret="s")

    assert intakes == [], ".zip is outside EAGER_EXTS and must not be ingested"
    # the message itself is still recorded and observed normally
    assert out["observed"] is True and out["chat_active"] is True
    assert ingested, "the event record is independent of eager intake"


async def test_group_message_triggers_roster_sync_check(monkeypatch):
    # Every group message runs the ensure_chat row past maybe_sync_members —
    # that piggyback IS the staleness refresh, so silently dropping the call
    # would freeze rosters at their first sync.
    pool, _ingested, _intakes = _wire(monkeypatch, chat_status="active")
    await main.whatsapp_webhook(_Req(_payload()), x_webhook_secret="s")
    assert len(pool.syncs) == 1
    assert pool.syncs[0]["jid"] == "grp1@g.us"


async def test_dm_never_triggers_roster_sync(monkeypatch):
    pool, _ingested, _intakes = _wire(monkeypatch, chat_status="active")
    await main.whatsapp_webhook(
        _Req(_payload(isGroup=False, chatId=None)), x_webhook_secret="s")
    assert pool.syncs == []

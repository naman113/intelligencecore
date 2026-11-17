"""Quick 017: WhatsApp senders behind a private ID (LID).

The connector forwards `phone` (empty while WhatsApp hides it) and `lid`. The
webhook must let an allowed person through whichever identity arrives, keep
one thread per person (the phone when known), record dropped senders without
their message body, and every outbound send must address a LID as `<lid>@lid`
or its phone, never as the LID with a phone-number suffix.

Pure logic: a FakePool routed by SQL substring (harness as in
test_webhook_reply_context.py), no DB/LLM/network. All numbers are made up.
"""

import logging
from types import SimpleNamespace

import pytest

import app.main as main
from app import chats, configservice, wasenders, whatsapp
from app.connectors import whatsapp_events

PHONE = "15550001111"
OTHER = "15550009999"
LID = "987650000012345"
STRANGER_LID = "987650000054321"


class _Req:
    def __init__(self, payload):
        self._payload = payload

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
    """Routes the webhook's SQL; `lid_map` stands in for wa_lid_map."""

    def __init__(self, lid_map=None):
        self.lid_map = dict(lid_map or {})      # lid -> phone or None
        self.lid_sources = {}
        self.executes = []
        self.fetchvals = []
        self.blocked = []
        self.threads = []

    async def execute(self, sql, *args):
        self.executes.append((sql, args))
        if "INSERT INTO wa_blocked_senders" in sql:
            self.blocked.append(args)
        if "INSERT INTO threads" in sql:
            self.threads.append(args)
        return "OK"

    async def fetchval(self, sql, *args):
        self.fetchvals.append((sql, args))
        if "INSERT INTO wa_lid_map" in sql:
            lid, phone, source = args
            if phone:
                self.lid_map[lid] = phone
                self.lid_sources[lid] = source
            else:
                self.lid_map.setdefault(lid, None)
            return self.lid_map[lid]
        if "SELECT phone FROM wa_lid_map WHERE lid" in sql:
            return self.lid_map.get(args[0])
        if "INSERT INTO messages" in sql:
            return 11
        if "INSERT INTO task_runs" in sql:
            return 21
        return None

    async def fetch(self, sql, *args):
        if "FROM wa_lid_map" in sql and "ANY" in sql:
            return [{"lid": lid, "phone": p} for lid, p in self.lid_map.items()
                    if (p and p in args[0]) or lid in args[0]]
        return []


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


@pytest.fixture(autouse=True)
def _fresh_throttle():
    wasenders._looked_up.clear()
    yield
    wasenders._looked_up.clear()


def _wire(monkeypatch, *, allowlist=(), owner="", lid_map=None, lookup=None, chat_status="active"):
    conn = FakeConn(lid_map)
    pool = FakePool(conn)

    async def fake_get_pool():
        return pool
    monkeypatch.setattr(main, "db", SimpleNamespace(get_pool=fake_get_pool))
    settings = SimpleNamespace(webhook_secret="s", allowlist=set(allowlist), owner_number=owner)
    monkeypatch.setattr(main, "get_settings", lambda: settings)

    async def fake_ensure_chat(jid, subject=None, is_group=True):
        return {"jid": jid, "status": chat_status, "is_group": is_group,
                "is_new": False, "members_synced_at": None}
    monkeypatch.setattr(chats, "ensure_chat", fake_ensure_chat)
    monkeypatch.setattr(chats, "maybe_sync_members", lambda row: False)

    async def no_ask():
        return None
    monkeypatch.setattr(main, "_ask_primary_about_chat", no_ask)

    ingested = []

    async def fake_ingest_inbound(**kw):
        ingested.append(kw)
        return 1
    monkeypatch.setattr(whatsapp_events, "ingest_inbound", fake_ingest_inbound)

    calls = []

    async def fake_lookup(phones):
        calls.append(list(phones))
        return lookup(phones) if lookup else None
    monkeypatch.setattr(whatsapp, "lookup_lids", fake_lookup)
    return conn, ingested, calls


def _payload(**over):
    body = {"phone": PHONE, "message": "hi", "messageId": "wamid-1", "name": "Owner"}
    body.update(over)
    return body


async def _post(body):
    return await main.whatsapp_webhook(_Req(body), x_webhook_secret="s")


def _thread(conn) -> str:
    sql, args = next((s, a) for s, a in conn.fetchvals if "INSERT INTO messages" in s)
    return args[0]


# ── the allowlist rule ─────────────────────────────────────────────────

async def test_allowed_phone_passes(monkeypatch):
    conn, ingested, _ = _wire(monkeypatch, allowlist={"+1 555-000-1111"})
    r = await _post(_payload())
    assert r["queued"] is True
    assert _thread(conn) == PHONE
    assert ingested[0]["phone"] == PHONE


async def test_allowed_lid_passes_and_threads_as_lid_address(monkeypatch):
    conn, ingested, calls = _wire(monkeypatch, allowlist={LID})
    r = await _post(_payload(phone="", lid=LID, resolved=False))
    assert r["queued"] is True
    assert _thread(conn) == f"{LID}@lid", "a LID-only sender threads as <lid>@lid"
    assert ingested[0]["phone"] == f"{LID}@lid"
    assert conn.lid_map == {LID: None}, "the LID is remembered (no phone yet)"


async def test_lid_mapped_to_allowed_phone_passes(monkeypatch):
    conn, ingested, calls = _wire(monkeypatch, allowlist={PHONE}, lid_map={LID: PHONE})
    r = await _post(_payload(phone="", lid=LID, resolved=False))
    assert r["queued"] is True
    assert _thread(conn) == PHONE
    assert calls == [], "a LID already on file needs no lookup"


async def test_owner_number_passes_without_being_listed(monkeypatch):
    conn, _, _ = _wire(monkeypatch, allowlist={OTHER}, owner="+1 (555) 000-1111")
    assert (await _post(_payload()))["queued"] is True


async def test_owner_arriving_as_unresolved_lid_is_recognised_by_lookup(monkeypatch):
    """The live case: the owner's first 'hi' arrives as a bare LID with no alt
    and no store hit. The gateway asks the connector for the LIDs of the
    expected numbers (PN -> LID, the documented direction), learns the pair,
    and the owner passes, threaded and answered as their phone."""
    conn, ingested, calls = _wire(
        monkeypatch, allowlist={OTHER}, owner=PHONE,
        lookup=lambda phones: [{"phone": PHONE, "lid": LID}] if PHONE in phones else [])
    r = await _post(_payload(phone="", lid=LID, resolved=False))
    assert r["queued"] is True
    assert sorted(calls[0]) == sorted([OTHER, PHONE])
    assert _thread(conn) == PHONE
    assert conn.lid_map[LID] == PHONE and conn.lid_sources[LID] == "lookup"


async def test_lookup_is_throttled_per_number(monkeypatch):
    conn, _, calls = _wire(monkeypatch, allowlist={OTHER}, lookup=lambda phones: [])
    for i in range(3):
        r = await _post(_payload(phone="", lid=STRANGER_LID, messageId=f"w{i}"))
        assert r == {"queued": False, "reason": "not allowlisted"}
    assert calls == [[OTHER]], "each number is asked of WhatsApp at most once per window"


async def test_listed_private_id_is_not_looked_up_as_a_phone(monkeypatch):
    """The client-copy stopgap listed the owner's LID under Allowed numbers.
    That entry is a known LID once a message came from it, so it is never sent
    to WhatsApp's PN -> LID lookup as if it were a phone number."""
    conn, _, calls = _wire(monkeypatch, allowlist={LID, OTHER}, lookup=lambda phones: [])
    r = await _post(_payload(phone="", lid=LID))
    assert r["queued"] is True
    assert calls == [[OTHER]]


async def test_unknown_sender_dropped_and_recorded_without_the_body(monkeypatch, caplog):
    body_text = "this is a long private message nobody should store in full"
    conn, ingested, _ = _wire(monkeypatch, allowlist={PHONE})
    caplog.set_level(logging.WARNING)
    r = await _post(_payload(phone="", lid=STRANGER_LID, message=body_text, name="Someone"))
    assert r == {"queued": False, "reason": "not allowlisted"}
    assert not ingested and not conn.threads, "nothing of a blocked message enters the pipelines"
    (sender, phone, lid, name, snip, chat_jid, subject), = conn.blocked
    assert (sender, phone, lid, name) == (STRANGER_LID, "", STRANGER_LID, "Someone")
    assert snip.startswith("this is a long") and len(snip) <= wasenders.SNIPPET_CHARS + 1
    assert body_text not in str(conn.executes)
    assert STRANGER_LID not in caplog.text and body_text not in caplog.text
    assert "987…321" in caplog.text


async def test_unknown_phone_dropped_and_recorded(monkeypatch):
    conn, _, _ = _wire(monkeypatch, allowlist={PHONE})
    r = await _post(_payload(phone=OTHER, lid=None))
    assert r["reason"] == "not allowlisted"
    assert conn.blocked[0][:3] == (OTHER, OTHER, "")


async def test_pn_and_lid_of_one_person_share_a_thread(monkeypatch):
    conn, _, _ = _wire(monkeypatch, allowlist={PHONE})
    await _post(_payload(phone=PHONE, lid=LID, resolved=True, messageId="w1"))
    first = _thread(conn)
    conn.fetchvals.clear()
    await _post(_payload(phone="", lid=LID, resolved=False, messageId="w2"))
    assert first == _thread(conn) == PHONE
    assert conn.lid_map == {LID: PHONE}, "learned from the message that carried both"


async def test_empty_allowlist_is_owner_only(monkeypatch):
    """Quick 046: an empty list no longer means anyone."""
    conn, _, _ = _wire(monkeypatch, allowlist=(), owner=PHONE)
    assert (await _post(_payload(phone=OTHER)))["queued"] is False
    assert (await _post(_payload(phone=PHONE)))["queued"] is True


async def test_empty_list_and_no_owner_blocks_everyone_and_records_it(monkeypatch):
    conn, _, _ = _wire(monkeypatch, allowlist=(), owner="")
    out = await _post(_payload(phone=OTHER))
    assert out == {"queued": False, "reason": "not allowlisted"}


async def test_open_to_anyone_is_explicit(monkeypatch):
    _wire(monkeypatch, allowlist=(), owner="")
    main.get_settings().whatsapp_open_to_anyone = True
    assert (await _post(_payload(phone=OTHER)))["queued"] is True


async def test_listed_non_digit_test_id_still_matches(monkeypatch):
    _wire(monkeypatch, allowlist={"test-local"})
    assert (await _post(_payload(phone="test-local")))["queued"] is True


async def test_lid_in_phone_field_is_treated_as_lid(monkeypatch):
    conn, _, _ = _wire(monkeypatch, allowlist={LID})
    assert (await _post(_payload(phone=f"{LID}@lid")))["queued"] is True
    assert _thread(conn) == f"{LID}@lid"


async def test_group_participant_by_lid_is_allowed_and_group_gating_unchanged(monkeypatch):
    conn, ingested, _ = _wire(monkeypatch, allowlist={PHONE}, lid_map={LID: PHONE},
                              chat_status="pending")
    r = await _post(_payload(phone="", lid=LID, chatId="120363000000000000@g.us",
                             isGroup=True, mentioned=True, groupSubject="Ops"))
    assert r["queued"] is False and r["chat_active"] is False, "pending group stays silent"
    assert _thread(conn) == "120363000000000000@g.us"
    assert ingested[0]["phone"] == PHONE and ingested[0]["triage_status"] == "skipped"


async def test_map_failure_is_fail_soft(monkeypatch):
    conn, _, _ = _wire(monkeypatch, allowlist={LID})

    async def boom(*a, **k):
        raise OSError("db down")
    monkeypatch.setattr(wasenders, "note_lid", boom)
    assert (await _post(_payload(phone="", lid=LID)))["queued"] is True


def test_is_allowed_rule():
    s = SimpleNamespace(allowlist={"+1 555 000 1111", LID}, owner_number="15550002222")
    assert wasenders.is_allowed(s, phone=PHONE)
    assert wasenders.is_allowed(s, lid=LID)
    assert wasenders.is_allowed(s, phone="15550002222")
    assert not wasenders.is_allowed(s, phone=OTHER)
    assert not wasenders.is_allowed(s, phone="", lid="")
    # Quick 046: empty list = owner only; empty list and no owner = nobody; open only on request.
    owner_only = SimpleNamespace(allowlist=set(), owner_number=PHONE)
    assert wasenders.is_allowed(owner_only, phone=PHONE)
    assert not wasenders.is_allowed(owner_only, phone=OTHER)
    nobody = SimpleNamespace(allowlist=set(), owner_number="")
    assert not wasenders.is_allowed(nobody, phone=OTHER)
    assert not wasenders.is_allowed(nobody, phone="", lid="")
    assert not wasenders.is_allowed(SimpleNamespace(allowlist=set()), phone=OTHER)
    assert wasenders.is_allowed(SimpleNamespace(allowlist=set(), owner_number="", whatsapp_open_to_anyone=True), phone=OTHER)
    assert not wasenders.is_allowed(SimpleNamespace(allowlist=set(), owner_number="", whatsapp_open_to_anyone="false"), phone=OTHER)
    assert wasenders.is_allowed(SimpleNamespace(allowlist={PHONE}), phone=PHONE), "owner_number optional"


def test_snippet_and_mask():
    assert wasenders.snippet("hi") == "hi"
    assert wasenders.snippet("  a\n b  ") == "a b"
    assert wasenders.snippet("x" * 100) == "x" * wasenders.SNIPPET_CHARS + "…"
    assert wasenders.snippet("") == "[attachment]"
    assert wasenders.mask(LID) == "987…345"
    assert wasenders.mask("12345") == "12345"


# ── outbound addresses ─────────────────────────────────────────────────

@pytest.fixture
def lid_table(monkeypatch):
    table = {}

    async def fake_lookup(target):
        d = wasenders.digits(target)
        return (d in table, table.get(d) or "")
    monkeypatch.setattr(wasenders, "_lookup", fake_lookup)
    return table


async def test_recipient_phone_and_group_unchanged(lid_table):
    assert await wasenders.recipient_for(PHONE) == PHONE
    assert await wasenders.recipient_for("120363000000000000@g.us") == "120363000000000000@g.us"
    assert await wasenders.recipient_for(f"{PHONE}@s.whatsapp.net") == f"{PHONE}@s.whatsapp.net"


async def test_recipient_lid_address_prefers_the_phone(lid_table):
    lid_table[LID] = PHONE
    assert await wasenders.recipient_for(f"{LID}@lid") == PHONE
    assert await wasenders.recipient_for(LID) == PHONE, "the legacy bare-LID thread too"


async def test_recipient_lid_without_phone_goes_to_lid_address(lid_table):
    assert await wasenders.recipient_for(f"{STRANGER_LID}@lid") == f"{STRANGER_LID}@lid"
    lid_table[LID] = None
    assert await wasenders.recipient_for(LID) == f"{LID}@lid", \
        "a bare LID the map knows must never go out as a phone number"


async def test_recipient_map_down_keeps_the_address(monkeypatch):
    async def boom(target):
        raise OSError("db down")
    monkeypatch.setattr(wasenders, "_lookup", boom)
    assert await wasenders.recipient_for(f"{LID}@lid") == f"{LID}@lid"
    assert await wasenders.recipient_for(PHONE) == PHONE


class _Res:
    is_success = True
    status_code = 200

    def json(self):
        return {"success": True}


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


async def test_send_message_and_document_post_the_resolved_address(monkeypatch, lid_table, tmp_path):
    _Client.posted = []
    recorded = []

    async def fake_record_self(phone, text):
        recorded.append(phone)
    monkeypatch.setattr(whatsapp.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(whatsapp, "_record_self", fake_record_self)
    monkeypatch.setattr(whatsapp, "get_settings",
                        lambda: SimpleNamespace(wa_api_base="http://c", wa_api_key="k"))
    lid_table[LID] = PHONE
    lid_table[STRANGER_LID] = None

    assert await whatsapp.send_message(LID, "hello")             # legacy bare-LID thread
    assert await whatsapp.send_message(f"{STRANGER_LID}@lid", "hello")
    assert await whatsapp.send_message(STRANGER_LID, "hello")
    assert await whatsapp.send_message(OTHER, "hello")
    f = tmp_path / "r.pdf"
    f.write_bytes(b"%PDF-1.4")
    assert await whatsapp.send_document(f"{LID}@lid", str(f))
    sent = [body["phone"] for _, body in _Client.posted]
    assert sent == [PHONE, f"{STRANGER_LID}@lid", f"{STRANGER_LID}@lid", OTHER, PHONE]
    assert not any(s.endswith("@s.whatsapp.net") for s in sent)
    assert recorded[0] == LID, "the self record keeps the thread id"


async def test_lookup_lids_is_fail_soft(monkeypatch):
    monkeypatch.setattr(whatsapp, "get_settings",
                        lambda: SimpleNamespace(wa_api_base="", wa_api_key=""))
    assert await whatsapp.lookup_lids([PHONE]) is None

    class Boom(_Client):
        async def post(self, *a, **k):
            raise ConnectionError("down")
    monkeypatch.setattr(whatsapp.httpx, "AsyncClient", Boom)
    monkeypatch.setattr(whatsapp, "get_settings",
                        lambda: SimpleNamespace(wa_api_base="http://c", wa_api_key="k"))
    assert await whatsapp.lookup_lids([PHONE]) is None

    class Ok(_Client):
        async def post(self, path, json=None, headers=None):
            assert path == "/api/lid-lookup" and json == {"phones": [PHONE]}

            class R(_Res):
                def json(self):
                    return {"success": True, "pairs": [{"phone": PHONE, "lid": LID}, {"x": 1}]}
            return R()
    monkeypatch.setattr(whatsapp.httpx, "AsyncClient", Ok)
    assert await whatsapp.lookup_lids([PHONE]) == [{"phone": PHONE, "lid": LID}]


# ── "Allow this sender" ────────────────────────────────────────────────

@pytest.fixture
def allow_env(monkeypatch):
    saved, deleted = [], []
    state = {"allowed": OTHER}

    async def fake_save_many(values):
        saved.append(values)
        state["allowed"] = values["allowed_numbers"]
        return list(values)
    monkeypatch.setattr(configservice, "save_many", fake_save_many)

    class Pool:
        async def execute(self, sql, *args):
            deleted.append((sql, args))

    async def gp():
        return Pool()
    import app.db as dbmod
    monkeypatch.setattr(dbmod, "get_pool", gp)
    import app.config as cfg
    monkeypatch.setattr(cfg, "get_settings",
                        lambda: SimpleNamespace(allowed_numbers=state["allowed"]))
    return saved, deleted, state


async def test_allow_sender_adds_the_lid_through_save_many(allow_env):
    saved, deleted, state = allow_env
    r = await wasenders.allow_sender(LID)
    assert r == {"allowed": LID, "changed": True, "detail": "Allowed."}
    assert saved == [{"allowed_numbers": f"{OTHER},{LID}"}]
    assert "DELETE FROM wa_blocked_senders" in deleted[0][0] and deleted[0][1] == (LID,)


async def test_allow_sender_already_listed_or_open(allow_env):
    saved, deleted, state = allow_env
    r = await wasenders.allow_sender("+1 555 000 9999")
    assert r["changed"] is False and saved == []
    state["allowed"] = ""
    r = await wasenders.allow_sender(PHONE)
    assert r["changed"] is False and "already allowed" in r["detail"] and saved == []


@pytest.mark.parametrize("bad", ["", "abc", "1555,1666", "1" * 25, "ui:owner", "15550001111@g.us"])
async def test_allow_sender_refuses_anything_but_an_id(allow_env, bad):
    with pytest.raises(ValueError):
        await wasenders.allow_sender(bad)
    assert allow_env[0] == []

"""Quick 007: files the agent delivers reach the web app chat as a download,
and also WhatsApp when the user asks. Pure logic: fakes and monkeypatch only.

Covers send_document's three channels (app chat attaches; WhatsApp and the
dashboard intake unchanged), the WhatsApp leg (recipient rule, WhatsApp off,
named people, a stubbed send), chatfiles' read-back (legacy rows, missing
files, run -> reply mapping, containment), GET /api/ask attachments, the
app-chat channel note, and the channel-aware prompt wording."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import askapi, chatfiles, people, sandbox
from app.agents import graph

RUN = 91
T0 = datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


# ── send_document: wiring ────────────────────────────────────────────────

def _wire(monkeypatch, tmp_path, *, wa_base="", paired=False, primary=None,
          registered=None, send_ok=True):
    monkeypatch.setattr(sandbox, "ARTIFACTS_ROOT", tmp_path)
    monkeypatch.setattr(sandbox, "_carried", {})
    st = SimpleNamespace(logs=[], sends=[], qr=0, inserts=[])

    async def fake_log(name, args, result, start, refused=False):
        st.logs.append({"name": name, "args": args, "result": result})
    monkeypatch.setattr(graph, "_log_tool", fake_log)

    async def fake_send(phone, path, caption=""):
        st.sends.append((phone, path, caption))
        return send_ok
    monkeypatch.setattr(graph.whatsapp, "send_document", fake_send)

    async def fake_qr():
        st.qr += 1
        return {"connected": paired, "qr": None, "user": None}
    monkeypatch.setattr(graph.whatsapp, "fetch_qr", fake_qr)
    monkeypatch.setattr(graph, "get_settings", lambda: SimpleNamespace(wa_api_base=wa_base))

    async def fake_primary():
        return primary
    monkeypatch.setattr(people, "primary_person", fake_primary)

    async def fake_registered(person):
        return (registered or {}).get(person)
    monkeypatch.setattr(graph, "_registered_whatsapp", fake_registered)

    class Pool:
        async def execute(self, sql, *args):
            st.inserts.append((" ".join(sql.split()), args))

    async def gp():
        return Pool()
    monkeypatch.setattr(graph, "get_pool", gp)
    (tmp_path / f"run_{RUN}").mkdir(exist_ok=True)
    (tmp_path / f"run_{RUN}" / "Fleet.xlsx").write_bytes(b"PK\x03\x04 xlsx")
    return st


async def _send(thread, **kw):
    fn = graph.send_document.coroutine or graph.send_document.func
    t1, t2 = graph.ctx_run_id.set(RUN), graph.ctx_phone.set(thread)
    try:
        return await fn(**{"filename": "Fleet.xlsx", "caption": "", **kw})
    finally:
        graph.ctx_run_id.reset(t1)
        graph.ctx_phone.reset(t2)


async def test_app_chat_attaches_and_sends_nothing(monkeypatch, tmp_path):
    st = _wire(monkeypatch, tmp_path, wa_base="http://wa:3000", paired=True)
    out = await _send("ui:owner:abcdefghij")
    assert out.startswith(chatfiles.ATTACHED + "Fleet.xlsx")
    assert "attached to your reply in the chat" in out
    assert "WhatsApp" not in out, "no WhatsApp wording unless the user asked for it"
    assert st.sends == [] and st.qr == 0 and st.inserts == []
    assert st.logs[-1]["args"] == {"filename": "Fleet.xlsx", "caption": ""}
    assert chatfiles.delivered(st.logs[-1]["result"])


async def test_main_app_chat_attaches_too(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    assert (await _send("ui:owner")).startswith(chatfiles.ATTACHED)


async def test_whatsapp_thread_unchanged(monkeypatch, tmp_path):
    st = _wire(monkeypatch, tmp_path)
    assert await _send("911234567890", caption="Q3") == "sent"
    assert len(st.sends) == 1 and st.sends[0][0] == "911234567890"
    assert st.logs[-1]["args"] == {"filename": "Fleet.xlsx", "caption": "Q3"}


async def test_whatsapp_thread_ignores_also_whatsapp(monkeypatch, tmp_path):
    st = _wire(monkeypatch, tmp_path, wa_base="http://wa:3000", paired=True,
               primary={"id": 1, "name": "Owner", "whatsapp": "15550001"})
    assert await _send("911234567890", also_whatsapp=True) == "sent"
    assert [s[0] for s in st.sends] == ["911234567890"], "one send, to the thread itself"
    assert st.qr == 0 and st.inserts == []


async def test_dashboard_intake_unchanged(monkeypatch, tmp_path):
    st = _wire(monkeypatch, tmp_path)
    out = await _send("dashboard")
    assert out.startswith("saved to the workspace as Fleet.xlsx — this run did not come from "
                          "WhatsApp, so the operator downloads it from the run's Artifacts list")
    assert st.sends == []
    assert not out.startswith(chatfiles.ATTACHED)


async def test_errors_unchanged(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    assert (await _send("ui:owner", filename="nope.pdf")).startswith("ERROR: nope.pdf not found")
    assert (await _send("ui:owner", filename="../x.pdf")).startswith("ERROR: invalid filename")
    assert await _send(None) == "ERROR: no recipient in context"


# ── the WhatsApp leg (owner: "send it to my whatsapp ... also show here") ──

async def test_whatsapp_off_still_attached(monkeypatch, tmp_path):
    st = _wire(monkeypatch, tmp_path, wa_base="",
               primary={"id": 1, "name": "Owner", "whatsapp": "15550001"})
    out = await _send("ui:owner:abcdefghij", also_whatsapp=True)
    assert out.startswith(chatfiles.ATTACHED + "Fleet.xlsx" + chatfiles.WA_NOT_SENT)
    assert chatfiles.WA_OFF in out
    assert "WhatsApp isn't connected yet; set it up on System › WhatsApp" in out
    assert st.sends == [] and st.qr == 0
    assert chatfiles.whatsapp_state(out) == ("failed", None)
    for bad in ("session", "message from WhatsApp", "resend"):
        assert bad not in out


async def test_whatsapp_configured_but_unpaired_is_off(monkeypatch, tmp_path):
    st = _wire(monkeypatch, tmp_path, wa_base="http://wa:3000", paired=False,
               primary={"id": 1, "name": "Owner", "whatsapp": "15550001"})
    out = await _send("ui:owner", also_whatsapp=True)
    assert chatfiles.WA_OFF in out and st.qr == 1 and st.sends == []


async def test_whatsapp_unreachable_connector_is_off(monkeypatch, tmp_path):
    st = _wire(monkeypatch, tmp_path, wa_base="http://wa:3000",
               primary={"id": 1, "name": "Owner", "whatsapp": "15550001"})

    async def none():
        return None
    monkeypatch.setattr(graph.whatsapp, "fetch_qr", none)
    assert chatfiles.WA_OFF in await _send("ui:owner", also_whatsapp=True)
    assert st.sends == []


async def test_whatsapp_leg_sends_to_own_number(monkeypatch, tmp_path):
    st = _wire(monkeypatch, tmp_path, wa_base="http://wa:3000", paired=True,
               primary={"id": 1, "name": "Owner", "whatsapp": "15550001"})
    out = await _send("ui:owner:abcdefghij", also_whatsapp=True, caption="Fleet report")
    assert out.startswith(chatfiles.ATTACHED + "Fleet.xlsx" + chatfiles.WA_SENT + " (you)")
    assert chatfiles.whatsapp_state(out) == ("sent", "you")
    (phone, path, caption), = st.sends
    assert phone == "15550001" and path.endswith("Fleet.xlsx") and caption == "Fleet report"
    (sql, args), = st.inserts
    assert "'whatsapp_message'" in sql and "'user_command'" in sql and "'executed'" in sql
    payload = json.loads(args[0])
    assert payload == {"recipient": "owner", "phone": "15550001", "body": "Fleet report",
                       "attachment": "Fleet.xlsx"}
    assert args[1] == RUN
    assert st.logs[-1]["args"] == {"filename": "Fleet.xlsx", "caption": "Fleet report",
                                   "also_whatsapp": True, "person": ""}


async def test_whatsapp_leg_named_registered_person(monkeypatch, tmp_path):
    st = _wire(monkeypatch, tmp_path, wa_base="http://wa:3000", paired=True,
               registered={"Priya": "15550002"})
    out = await _send("ui:owner", person="Priya")
    assert chatfiles.whatsapp_state(out) == ("sent", "Priya")
    assert st.sends[0][0] == "15550002"
    assert json.loads(st.inserts[0][1][0])["recipient"] == "Priya"


async def test_whatsapp_leg_unregistered_person_refused(monkeypatch, tmp_path):
    st = _wire(monkeypatch, tmp_path, wa_base="http://wa:3000", paired=True)
    out = await _send("ui:owner", also_whatsapp=True, person="+15550009")
    assert "'+15550009' is not a registered person" in out and st.sends == []
    assert out.startswith(chatfiles.ATTACHED), "the file is still attached in the chat"


async def test_whatsapp_leg_no_number_for_owner(monkeypatch, tmp_path):
    st = _wire(monkeypatch, tmp_path, wa_base="http://wa:3000", paired=True, primary=None)
    out = await _send("ui:owner", also_whatsapp=True)
    assert "no WhatsApp number for you yet" in out and st.sends == []
    assert chatfiles.whatsapp_state(out) == ("failed", None)


async def test_whatsapp_leg_connector_refusal(monkeypatch, tmp_path):
    st = _wire(monkeypatch, tmp_path, wa_base="http://wa:3000", paired=True,
               primary={"id": 1, "name": "Owner", "whatsapp": "15550001"}, send_ok=False)
    out = await _send("ui:owner", also_whatsapp=True)
    assert "connector refused" in out and st.inserts == []
    assert chatfiles.whatsapp_state(out) == ("failed", None)


# ── recipient rule: primary person, owner_number fallback, neither ───────

class _PeoplePool:
    def __init__(self, row):
        self.row = row

    async def fetchrow(self, sql, *args):
        assert "is_primary AND active" in sql
        return self.row


def _people(monkeypatch, row, owner_number):
    async def gp():
        return _PeoplePool(row)
    monkeypatch.setattr(people, "get_pool", gp)
    import app.config as config
    monkeypatch.setattr(config, "get_settings",
                        lambda: SimpleNamespace(owner_number=owner_number))


async def test_recipient_primary_person(monkeypatch):
    _people(monkeypatch, {"id": 3, "name": "Hari", "whatsapp": "919800000001"}, "15550000")
    assert await graph.whatsapp_recipient() == ("919800000001", "you", "")


async def test_recipient_owner_number_fallback(monkeypatch):
    _people(monkeypatch, None, "+1 555-0000")
    assert await graph.whatsapp_recipient() == ("15550000", "you", "")
    # a primary person without a number falls back too
    _people(monkeypatch, {"id": 3, "name": "Hari", "whatsapp": None}, "15550000")
    assert (await graph.whatsapp_recipient())[0] == "15550000"


async def test_recipient_neither_set(monkeypatch):
    _people(monkeypatch, None, "")
    phone, label, problem = await graph.whatsapp_recipient()
    assert phone is None and label == "you" and "no WhatsApp number for you yet" in problem


async def test_recipient_named_person_uses_registry(monkeypatch):
    async def reg(person):
        return {"Priya": "15550002"}.get(person)
    monkeypatch.setattr(graph, "_registered_whatsapp", reg)
    assert await graph.whatsapp_recipient("Priya") == ("15550002", "Priya", "")
    phone, _, problem = await graph.whatsapp_recipient("Nobody")
    assert phone is None and "People tab" in problem


async def test_send_message_to_person_shares_the_lookup(monkeypatch):
    seen = []

    async def reg(person):
        seen.append(person)
        return None
    monkeypatch.setattr(graph, "_registered_whatsapp", reg)

    async def fake_log(*a, **k):
        return None
    monkeypatch.setattr(graph, "_log_tool", fake_log)

    async def gp():
        return object()
    monkeypatch.setattr(graph, "get_pool", gp)
    fn = graph.send_message_to_person.coroutine or graph.send_message_to_person.func
    out = await fn(person="Priya", message="hi")
    assert seen == ["Priya"] and out.startswith("ERROR: 'Priya' is not a registered person")


# ── chatfiles: read-back ─────────────────────────────────────────────────

def _row(run_id, name, digest, *, status="done", started=0, ended=60):
    return {"run_id": run_id, "status": status,
            "started_at": at(started) if started is not None else None,
            "ended_at": at(ended) if ended is not None else None,
            "args": json.dumps({"filename": name, "caption": ""}), "result_digest": digest}


def _msgs(*spec):
    return [{"id": i, "role": role, "content": "x", "created_at": at(s)} for i, role, s in spec]


def _file(root, run_id, name, data=b"PK data"):
    d = root / f"run_{run_id}"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)


def test_legacy_saved_rows_show_with_no_backfill(tmp_path):
    """The owner's runs 10 and 11: two calls, 'saved to the workspace as ...'."""
    _file(tmp_path, 10, "Fleet_Details_Report.xlsx")
    _file(tmp_path, 11, "Fleet_Details_Report.xlsx")
    legacy = ("saved to the workspace as Fleet_Details_Report.xlsx — this run did not come "
              "from WhatsApp, so the operator downloads it from the run's Artifacts list")
    rows = [_row(10, "Fleet_Details_Report.xlsx", legacy, started=0, ended=60),
            _row(10, "Fleet_Details_Report.xlsx", legacy, started=0, ended=60),
            _row(11, "Fleet_Details_Report.xlsx", legacy, started=100, ended=160)]
    msgs = _msgs((1, "user", -1), (2, "assistant", 59), (3, "user", 99), (4, "assistant", 159))
    out = chatfiles.attach(rows, msgs, tmp_path)
    assert set(out) == {2, 4}
    (a,) = out[2]
    assert a == {"name": "Fleet_Details_Report.xlsx", "run_id": 10, "kind": "spreadsheet",
                 "mimetype": chatfiles.TYPES[".xlsx"][1], "bytes": 7, "available": True,
                 "url": "/api/runs/10/artifacts/Fleet_Details_Report.xlsx",
                 "whatsapp": None, "whatsapp_to": None}
    assert out[4][0]["run_id"] == 11


def test_missing_file_is_no_longer_available(tmp_path):
    rows = [_row(5, "gone.pdf", chatfiles.attached_result("gone.pdf"))]
    out = chatfiles.attach(rows, _msgs((1, "user", -1), (2, "assistant", 30)), tmp_path)
    (a,) = out[2]
    assert a["available"] is False and a["url"] is None and a["bytes"] is None
    assert a["kind"] == "pdf"


def test_errors_and_whatsapp_results_are_not_attachments(tmp_path):
    _file(tmp_path, 5, "a.pdf")
    rows = [_row(5, "a.pdf", "ERROR: a.pdf not found in workspace; use list_artifacts"),
            _row(5, "a.pdf", "sent"),
            _row(5, "a.pdf", "ERROR: connector refused the document")]
    assert chatfiles.attach(rows, _msgs((2, "assistant", 30)), tmp_path) == {}


def test_traversal_and_hidden_names_rejected(tmp_path):
    _file(tmp_path, 5, "script.py")
    rows = [_row(5, n, chatfiles.attached_result(n))
            for n in ("../../.env", "/etc/passwd", "script.py", "", None)]
    assert chatfiles.attach(rows, _msgs((2, "assistant", 30)), tmp_path) == {}
    assert chatfiles.artifact_path(tmp_path, 5, "..") is None


def test_whatsapp_state_and_dedupe(tmp_path):
    _file(tmp_path, 5, "r.pdf")
    rows = [_row(5, "r.pdf", chatfiles.attached_result(
                "r.pdf", chatfiles.WA_NOT_SENT + chatfiles.WA_OFF)),
            _row(5, "r.pdf", chatfiles.attached_result("r.pdf", chatfiles.WA_SENT + " (you)"))]
    (a,) = chatfiles.attach(rows, _msgs((2, "assistant", 30)), tmp_path)[2]
    assert a["whatsapp"] == "sent" and a["whatsapp_to"] == "you"
    rows = [_row(5, "r.pdf", chatfiles.attached_result(
        "r.pdf", chatfiles.WA_NOT_SENT + chatfiles.WA_OFF))]
    (a,) = chatfiles.attach(rows, _msgs((2, "assistant", 30)), tmp_path)[2]
    assert a["whatsapp"] == "failed"


def test_url_encodes_names(tmp_path):
    _file(tmp_path, 5, "Fleet report #1.pdf")
    rows = [_row(5, "Fleet report #1.pdf", chatfiles.attached_result("Fleet report #1.pdf"))]
    (a,) = chatfiles.attach(rows, _msgs((2, "assistant", 30)), tmp_path)[2]
    assert a["url"] == "/api/runs/5/artifacts/Fleet%20report%20%231.pdf"


def test_reply_mapping():
    assistant = _msgs((2, "assistant", 5), (4, "assistant", 50), (6, "assistant", 90))
    # the reply is the LAST assistant row inside the window (an alert at 5 s came first)
    assert chatfiles.reply_for({"started_at": at(0), "ended_at": at(60), "status": "done"},
                               assistant) == 4
    # still running: nothing yet
    assert chatfiles.reply_for({"started_at": at(0), "ended_at": None, "status": "running"},
                               assistant) is None
    # failed with no reply: the first assistant row after it started
    assert chatfiles.reply_for({"started_at": at(60), "ended_at": at(70), "status": "failed"},
                               assistant) == 6
    # done, but its reply is not in the loaded window: skipped
    assert chatfiles.reply_for({"started_at": at(60), "ended_at": at(70), "status": "done"},
                               assistant) is None


class _ThreadConn:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    async def fetch(self, sql, *args):
        self.calls.append((" ".join(sql.split()), args))
        return self.rows


async def test_for_thread_reads_only_this_chat(tmp_path):
    _file(tmp_path, 7, "a.xlsx")
    conn = _ThreadConn([_row(7, "a.xlsx", chatfiles.attached_result("a.xlsx"))])
    msgs = _msgs((1, "user", -5), (2, "assistant", 30))
    out = await chatfiles.for_thread(conn, "ui:owner:abcdefghij", msgs, tmp_path)
    (sql, args), = conn.calls
    assert "r.thread_id = $1" in sql and "tc.tool = 'send_document'" in sql
    assert args == ("ui:owner:abcdefghij", at(-5))
    assert out[2][0]["name"] == "a.xlsx"


async def test_for_thread_skips_query_without_replies_and_fails_soft(tmp_path):
    conn = _ThreadConn([])
    assert await chatfiles.for_thread(conn, "ui:owner", _msgs((1, "user", 0)), tmp_path) == {}
    assert conn.calls == []

    class Boom:
        async def fetch(self, *a):
            raise RuntimeError("db gone")
    assert await chatfiles.for_thread(Boom(), "ui:owner", _msgs((2, "assistant", 0)),
                                      tmp_path) == {}


async def test_get_ask_puts_attachments_on_replies(monkeypatch):
    rows = [{"id": 2, "role": "assistant", "content": "attached", "created_at": at(30)},
            {"id": 1, "role": "user", "content": "excel please", "created_at": at(0)}]
    card = {"name": "a.xlsx", "run_id": 7}

    class Conn:
        async def fetchrow(self, sql, *args):
            if "deleted_at FROM threads" in sql:
                return None
            return None

        async def fetch(self, sql, *args):
            if "FROM messages" in sql:
                return rows
            return []

    class Pool:
        def acquire(self):
            class A:
                async def __aenter__(s):
                    return Conn()

                async def __aexit__(s, *a):
                    return False
            return A()

    async def gp():
        return Pool()
    monkeypatch.setattr(askapi, "db", SimpleNamespace(get_pool=gp))
    seen = []

    async def files(conn, thread_id, messages):
        seen.append((thread_id, [m["id"] for m in messages]))
        return {2: [card]}
    monkeypatch.setattr(askapi.chatfiles, "for_thread", files)
    out = await askapi.thread(chat="ui:owner")
    assert seen == [("ui:owner", [1, 2])]
    user, reply = out["messages"]
    assert "attachments" not in user and reply["attachments"] == [card]


# ── channel note and prompts ─────────────────────────────────────────────

class _Sup:
    def __init__(self):
        self.calls = []

    async def ainvoke(self, payload, config=None):
        self.calls.append(payload)
        return {"messages": [SimpleNamespace(content="ok")]}


@pytest.mark.parametrize("thread,expect", [("ui:owner", True), ("ui:owner:abcdefghij", True),
                                           ("911234567890", False), ("dashboard", False),
                                           (None, False)])
async def test_channel_note_only_on_app_threads(monkeypatch, thread, expect):
    sup = _Sup()
    configs = {"fake": True}

    async def eff():
        return configs
    monkeypatch.setattr(graph, "effective_configs", eff)
    monkeypatch.setattr(graph, "_supervisor", sup)
    monkeypatch.setattr(graph, "_built_with", configs)
    tok = graph.ctx_phone.set(thread)
    try:
        await graph.run([], "make an excel", "")
    finally:
        graph.ctx_phone.reset(tok)
    msgs = sup.calls[0]["messages"]
    msgs = [m for m in msgs if not m[1].startswith("CLOCK:")]  # Quick 049
    assert msgs[-1] == ("user", "make an excel")
    if expect:
        assert msgs == [("user", graph.APP_CHAT_NOTE), ("user", "make an excel")]
    else:
        assert msgs == [("user", "make an excel")]


def test_prompts_are_channel_aware():
    sup = graph.DEFAULT_PROMPTS["supervisor"]
    assert "The user is on WhatsApp" not in sup
    assert "Final reply is a WhatsApp message" not in sup
    assert "reachable over WhatsApp and the web app chat" in sup
    assert "download card" in sup and "WEB APP CHAT" in sup
    assert "never say the chat or session is not connected to WhatsApp" in sup
    assert "never ask the user to message from WhatsApp" in sup
    # WhatsApp rules kept
    assert "never tell a WhatsApp user to 'download' anything" in sup
    assert "WhatsApp renders *bold*" in sup
    for key in ("document", "drawing"):
        assert "also_whatsapp=true" in graph.DEFAULT_PROMPTS[key]
    assert "CHANNEL: web app chat" in graph.APP_CHAT_NOTE and "download card" in graph.APP_CHAT_NOTE
    doc = (graph.send_document.description or "")
    assert "web app chat" in doc and "also_whatsapp" in doc
    assert "registered person" in doc

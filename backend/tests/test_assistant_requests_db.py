"""Quick 024 on real Postgres: requests to the assistant close when its reply answers
them, stay open when it does not, never send a "Done" for an answered request, and
the startup clean-up turns the client's stuck cases (#5, #6 shape) into commitments
the sweep can judge, leaving a real open case alone.

Runs the real SQL of triage (batch, verdict, sweep with its catch-up, completion,
stale sweep, clean-up), actionsvc.notify_case_closed, the violation sweep and
chats.display_name. Models are scripted; nothing reaches WhatsApp (send_message is
captured). Skipped unless DASH_TEST_DATABASE_URL is set; each test uses a throwaway
database with schema.sql applied twice:
  DASH_TEST_DATABASE_URL=postgresql://agent:agent@127.0.0.1:9183/agent_memory
Numbers are fictional (555-01xx).
"""
import json
import os
import secrets
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DASH_TEST_DATABASE_URL"),
    reason="DASH_TEST_DATABASE_URL not set (real-Postgres tests)",
)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "schema.sql"
DM = "15550100001"
GROUP = "120363000000000001@g.us"


def _with_db(url: str, name: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


@pytest.fixture
async def env(monkeypatch):
    import asyncpg

    from app import actionsvc, chats, connectors, db, expectations, memory_pipeline, skillservice, triage

    admin_url = os.environ["DASH_TEST_DATABASE_URL"]
    name = "q024_test_" + secrets.token_hex(4)
    admin = await asyncpg.connect(admin_url)
    p = None
    state = {"sent": [], "distilled": [], "chained": [], "verdicts": [], "answers": None,
             "sweep_payloads": []}
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        url = _with_db(admin_url, name)
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        conn = await asyncpg.connect(url)
        await conn.execute(sql)
        await conn.execute(sql)          # idempotent, Quick 024 block included
        await conn.close()
        p = await asyncpg.create_pool(url, min_size=1, max_size=6)

        async def gp():
            return p
        for mod in (db, triage, actionsvc, memory_pipeline, connectors, skillservice,
                    expectations, chats):
            monkeypatch.setattr(mod, "get_pool", gp, raising=False)

        settings = SimpleNamespace(company_name="", owner_number=DM, test_threads="test-local",
                                   case_chaining_enabled=True, stale_case_days=21,
                                   group_question_mode="off")
        monkeypatch.setattr(triage, "get_settings", lambda: settings)
        monkeypatch.setattr(actionsvc, "get_settings", lambda: settings)

        async def send(phone, text):
            state["sent"].append((phone, text))
            return True
        monkeypatch.setattr(actionsvc.whatsapp, "send_message", send)

        async def distill(case_id):
            state["distilled"].append(case_id)
            return True
        monkeypatch.setattr(memory_pipeline, "distill_case", distill)

        async def chain(pool, case_id):
            state["chained"].append(case_id)
        monkeypatch.setattr(triage, "_maybe_chain_case", chain)

        stub = types.ModuleType("app.agents.graph")
        stub._callbacks = lambda: {}
        monkeypatch.setitem(sys.modules, "app.agents.graph", stub)

        class Model:
            async def ainvoke(self, msgs, config=None):
                payload = json.loads(msgs[1][1])
                if msgs[0][1] == triage.SATISFY_SYSTEM:
                    state["sweep_payloads"].append(payload)
                    return SimpleNamespace(content=json.dumps(
                        {"satisfied": state["answers"](payload)}))
                state.setdefault("triage_payloads", []).append(payload)
                return SimpleNamespace(content=json.dumps(
                    {"verdicts": state["verdicts"].pop(0)}))
        monkeypatch.setattr(triage, "_model", lambda: Model())
        state["pool"] = p
        yield state
    finally:
        if p is not None:
            await p.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _inbound(text, msg_id, *, thread=DM, to_assistant=True, group=False):
    from app.connectors import whatsapp_events
    return await whatsapp_events.ingest_inbound(
        phone=DM, text=text, wa_message_id=msg_id, group_jid=thread if group else None,
        is_group=group, name="Asha" if group else None, to_assistant=to_assistant)


async def _reply(text, thread=DM):
    from app.connectors import whatsapp_events
    await whatsapp_events.record_self(thread, text)


def _meet(*needles):
    """Scripted sweep model: meet each offered assistant commitment with the
    first delivery in its chat whose text contains one of the needles."""
    def answer(payload):
        out = []
        for e in payload["open_expectations"]:
            for d in payload["assistant_deliveries"]:
                if (d["chat"] == e.get("chat") and d["delivery_event_id"] > e.get("asked_in_event", 0)
                        and any(n in d["content"] for n in needles)):
                    out.append({"expectation_id": e["expectation_id"],
                                "delivery_event_id": d["delivery_event_id"]})
                    break
        return out
    return answer


def _request_verdict(eid, desc="Find the engine specs of our machines on the web", **kw):
    v = {"event_id": eid, "relevant": True, "case": "new",
         "new_case_title": "Engine specs lookup", "participants": [],
         "expectations_created": [{"description": desc, "owner": "assistant",
                                   "due_at": None, "satisfy_hint": "the specs"}],
         "expectations_satisfied": [], "expectations_updated": [], "case_note": None,
         "reason": "Request to the assistant for engine specs."}
    v.update(kw)
    return v


async def _case(pool, cid):
    return dict(await pool.fetchrow("SELECT * FROM cases WHERE id = $1", cid))


async def test_answered_lookup_closes_quietly(env):
    """(1) request -> case + assistant commitment; "on it" then the specs ->
    met by the specs; the case closes as answered: no "Done", no distill, no chain,
    stamped as nothing durable."""
    from app import triage
    pool = env["pool"]
    eid = await _inbound("can you get me engine specs of our machines from web", "m1")
    env["verdicts"].append([_request_verdict(eid)])
    assert await triage._triage_batch() == 1
    cid, = [r["id"] for r in await pool.fetch("SELECT id FROM cases")]
    x = dict(await pool.fetchrow("SELECT * FROM expectations WHERE case_id = $1", cid))
    assert (x["owed_by"], x["owner_id"], x["due_at"], x["source_event_id"]) == ("assistant", None, None, eid)

    await _reply("On it. I'm checking which machines we have.")
    await _reply("Here are the engine specs: A 934 C, 150 kW, 6 cylinders.")
    env["answers"] = _meet("engine specs:")
    await triage._tick()

    c = await _case(pool, cid)
    assert (c["status"], c["close_reason"]) == ("closed", "answered by the assistant in the chat")
    assert c["distilled_at"] is not None and c["distillation"] == ""
    assert env["sent"] == [] and env["distilled"] == [] and env["chained"] == []
    assert await pool.fetchval("SELECT count(*) FROM actions") == 0
    met = await pool.fetchval("SELECT content FROM events WHERE id = "
                              "(SELECT satisfied_by FROM expectations WHERE case_id = $1)", cid)
    assert met.startswith("Here are the engine specs")
    payload, = env["sweep_payloads"]
    assert DM not in json.dumps(payload), "the model sees an opaque chat label, not the number"

    # (4) a later "did you send it?" in the same chat: triage is told what was answered
    follow = await _inbound("did you send me the engine specs i asked for ?", "m1b")
    env["verdicts"].append([{"event_id": follow, "relevant": False, "case": "none",
                             "reason": "checks on an answered request"}])
    await triage._triage_batch()
    ev = env["triage_payloads"][-1]["events"][0]
    assert ev["to_assistant"] is True
    assert ev["answered_here"] and ev["answered_here"][0].startswith("Engine specs lookup (answered ")
    assert await pool.fetchval("SELECT count(*) FROM cases") == 1, "no new case"


async def test_unfulfilled_request_stays_open_then_archives_honestly(env):
    """(2) "I couldn't find it" leaves the commitment open and the case visible;
    after stale_case_days of silence the stale sweep archives it with its usual
    wording (the assistant's commitment does not keep it alive)."""
    from app import triage
    pool = env["pool"]
    eid = await _inbound("send me the signed vendor contract from their portal", "m2")
    env["verdicts"].append([_request_verdict(eid, desc="Fetch the signed vendor contract")])
    await triage._triage_batch()
    cid = await pool.fetchval("SELECT id FROM cases")
    await _reply("I couldn't reach the vendor portal, so I can't get the signed contract.")
    env["answers"] = _meet("engine specs:")          # nothing qualifies
    await triage._tick()

    c = await _case(pool, cid)
    x = dict(await pool.fetchrow("SELECT * FROM expectations WHERE case_id = $1", cid))
    assert c["status"] == "open" and x["status"] == "open" and x["last_checked_at"] is not None
    assert len(env["sweep_payloads"]) == 1

    await pool.execute("UPDATE cases SET last_activity_at = now() - interval '30 days' WHERE id = $1", cid)
    await triage._close_stale_cases()
    c = await _case(pool, cid)
    assert (c["status"], c["close_reason"]) == ("closed", "no activity for 21+ days (auto-closed: stale)")
    (phone, text), = env["sent"]
    assert phone == DM and "no recent activity" in text and "Done" not in text


async def test_person_owed_commitment_keeps_its_done_notice(env, monkeypatch):
    """(3) A group line not addressed to the assistant: a person's commitment as
    before; delivered by the person -> 'completed' with the ✅ Done notice."""
    from app import people, triage
    pool = env["pool"]
    vinu = await pool.fetchval(
        "INSERT INTO people (name, whatsapp) VALUES ('Vinu', '15550100009') RETURNING id")
    ask = await _inbound("Vinu please send the certificate expiry data by Friday", "g1",
                         thread=GROUP, to_assistant=False, group=True)
    env["verdicts"].append([{
        "event_id": ask, "relevant": True, "case": "new", "new_case_title": "Certificate expiry data",
        "participants": ["Vinu"],
        "expectations_created": [
            {"description": "Vinu sends the certificate expiry data", "owner": "Vinu",
             "due_at": None, "satisfy_hint": "the data arrives"},
            # a model slip: the assistant was not asked here, so this is dropped
            {"description": "Assistant tracks it", "owner": "assistant", "due_at": None,
             "satisfy_hint": ""}],
        "reason": "Vinu owes the data"}])
    await triage._triage_batch()
    cid = await pool.fetchval("SELECT id FROM cases")
    rows = await pool.fetch("SELECT owner_id, owed_by FROM expectations WHERE case_id = $1", cid)
    assert [(r["owner_id"], r["owed_by"]) for r in rows] == [(vinu, None)]
    xid = await pool.fetchval("SELECT id FROM expectations WHERE case_id = $1", cid)

    done = await _inbound("certificate data: expires 2027-03-31", "g2", thread=GROUP,
                          to_assistant=False, group=True)
    env["verdicts"].append([{"event_id": done, "relevant": True, "case": cid,
                             "expectations_satisfied": [xid], "reason": "delivered"}])
    await triage._triage_batch()
    c = await _case(pool, cid)
    assert (c["status"], c["close_reason"]) == ("closed", "auto: all expectations satisfied")
    (phone, text), = env["sent"]
    assert text.startswith("✅ Done")
    assert env["distilled"] == [cid] and env["chained"] == [cid]


async def test_mixed_case_closes_as_a_completion(env):
    """A request to the assistant that also needs a person: the assistant's part is
    met by its reply, the person's keeps the case open; it ends as 'completed'."""
    from app import triage
    pool = env["pool"]
    await pool.execute("INSERT INTO people (name, whatsapp) VALUES ('Vinu', '15550100009')")
    eid = await _inbound("ask Vinu for the certificate data and tell me when it's in", "m3")
    v = _request_verdict(eid, desc="Ask Vinu for the certificate data")
    v["expectations_created"].append({"description": "Vinu sends the certificate data",
                                      "owner": "Vinu", "due_at": None, "satisfy_hint": "data"})
    env["verdicts"].append([v])
    await triage._triage_batch()
    cid = await pool.fetchval("SELECT id FROM cases")
    await _reply("Done, I've asked Vinu for the certificate data.")
    env["answers"] = _meet("I've asked Vinu")
    await triage._tick()
    assert (await _case(pool, cid))["status"] == "open", "Vinu still owes his part"
    person = await pool.fetchval(
        "SELECT id FROM expectations WHERE case_id = $1 AND owed_by IS NULL", cid)
    await pool.execute("UPDATE expectations SET status = 'satisfied' WHERE id = $1", person)
    await triage._complete_finished_cases()
    c = await _case(pool, cid)
    assert (c["status"], c["close_reason"]) == ("closed", "auto: all expectations satisfied")
    assert len(env["sent"]) == 1 and env["sent"][0][1].startswith("✅ Done")


async def test_reply_swept_before_the_commitment_still_counts(env):
    """The race: the reply was swept (skipped) before triage recorded the
    commitment; the commitment's first check still finds it by chat and order."""
    from app import triage
    pool = env["pool"]
    eid = await _inbound("can you get me engine specs from web", "m4")
    await _reply("Here are the engine specs: D934, 150 kW.")
    env["answers"] = _meet("engine specs:")
    await triage._satisfaction_sweep()                 # no commitment yet: skipped, no call
    assert env["sweep_payloads"] == []
    assert await pool.fetchval(
        "SELECT count(*) FROM events WHERE provenance = 'self' AND triage_status = 'pending'") == 0

    env["verdicts"].append([_request_verdict(eid)])
    await triage._tick()
    assert (await pool.fetchval("SELECT status FROM cases")) == "closed"
    assert (await pool.fetchval("SELECT close_reason FROM cases")) == triage.ANSWERED_REASON


async def test_follow_up_case_if_any_closes_quietly(env):
    """(4) fallback: if triage opens a case for "did you send it?", the reply
    confirming it meets that commitment and it closes quietly too."""
    from app import triage
    pool = env["pool"]
    eid = await _inbound("did you send me the engine specs i asked for ?", "m5")
    env["verdicts"].append([_request_verdict(eid, desc="Confirm the specs were sent")])
    await triage._triage_batch()
    await _reply("Yes, I sent them in this chat earlier today.")
    env["answers"] = _meet("Yes, I sent them")
    await triage._tick()
    assert (await pool.fetchval("SELECT status FROM cases")) == "closed"
    assert env["sent"] == []


async def test_completion_sql_on_null_and_dismissed(env):
    from app import triage
    pool = env["pool"]

    async def case(*exps):
        cid = await pool.fetchval("INSERT INTO cases (title) VALUES ('t') RETURNING id")
        for owed_by, status in exps:
            await pool.execute(
                "INSERT INTO expectations (case_id, description, owed_by, status) VALUES ($1, 'd', $2, $3)",
                cid, owed_by, status)
        return cid
    a = await case(("assistant", "satisfied"), ("assistant", "dismissed"))
    b = await case(("assistant", "dismissed"))
    c = await case((None, "satisfied"))
    d = await case(("assistant", "satisfied"), (None, "dismissed"))
    e = await case(("assistant", "open"))
    await triage._complete_finished_cases()
    reasons = {r["id"]: (r["status"], r["close_reason"]) for r in
               await pool.fetch("SELECT id, status, close_reason FROM cases")}
    assert reasons[a] == ("closed", triage.ANSWERED_REASON)
    assert reasons[b] == ("closed", triage.ANSWER_DISMISSED_REASON)
    assert reasons[c] == ("closed", "auto: all expectations satisfied")
    assert reasons[d] == ("closed", "auto: all expectations satisfied")
    assert reasons[e] == ("open", None)
    assert len(env["sent"]) == 2, "only the two cases with a person's expectation notify"


async def test_violation_sweep_skips_assistant_commitments(env, monkeypatch):
    from app import expectations
    pool = env["pool"]
    cid = await pool.fetchval("INSERT INTO cases (title) VALUES ('t') RETURNING id")
    await pool.execute(
        """INSERT INTO expectations (case_id, description, owed_by, due_at)
           VALUES ($1, 'assistant part', 'assistant', now() - interval '1 hour')""", cid)
    stub = types.ModuleType("app.playbooks")

    async def never(*a, **k):
        raise AssertionError("nothing to remind anyone about")
    stub.enqueue_playbook_run = never
    monkeypatch.setitem(sys.modules, "app.playbooks", stub)
    assert await expectations._sweep_violations() == 0
    assert await pool.fetchval("SELECT status FROM expectations") == "open"


async def _stuck_case(pool, title, reason, events, *, thread=DM):
    """Seed a pre-024 case: no expectations, linked inbound messages with their
    verdict notes, and the assistant's replies in the thread."""
    cid = await pool.fetchval(
        "INSERT INTO cases (title, opened_reason, origin_chat_jid) VALUES ($1, $2, $3) RETURNING id",
        title, reason, thread)
    for kind, text, note in events:
        if kind == "in":
            eid = await pool.fetchval(
                """INSERT INTO events (source, content, provenance, occurred_at, thread_ref, triage_status)
                   VALUES ('whatsapp', $1, 'external', now(), $2, 'done') RETURNING id""", text, thread)
            await pool.execute("INSERT INTO case_events (case_id, event_id, note) VALUES ($1, $2, $3)",
                               cid, eid, note)
        else:
            await pool.execute(
                """INSERT INTO events (source, content, provenance, occurred_at, thread_ref, triage_status)
                   VALUES ('whatsapp', $1, 'self', now() - interval '2 hours', $2, 'skipped')""",
                text, thread)
    return cid


async def test_cleanup_closes_the_client_shapes_and_leaves_a_real_case(env):
    from app import triage
    pool = env["pool"]
    six = await _stuck_case(pool, "Machine engine specs lookup",
                            "Open request to the assistant for engine specs. The assistant owes the "
                            "lookup, so no person expectation is created.", [
        ("in", "can you get me engine specs of the machines from web",
         "Open request to the assistant for engine specs. The assistant owes the lookup, so no "
         "person expectation is created."),
        ("out", "On it. I'm checking which machines we have.", None),
        ("out", "Here are the engine specs: A 934 C, 150 kW.", None),
        ("in", "did you send me the engine specs i asked for ?",
         "Follow-up on the engine specs lookup; the specs were already delivered, so the case "
         "is advanced without creating any human expectation."),
        ("out", "Yes, I sent them earlier today.", None)])
    five = await _stuck_case(pool, "Machine diesel consumption status",
                             "Direct question from the owner awaiting an answer from the assistant's "
                             "data. No human owes a deliverable, so no expectation is created.", [
        ("in", "is any machine actively consuming diesel",
         "Direct question from the owner awaiting an answer from the assistant's data. No human "
         "owes a deliverable, so no expectation is created."),
        ("out", "Diesel: about 10 machines are burning diesel right now.", None),
        ("in", "can you create dashboards from here",
         "Request for a dashboard; the assistant owns the work, so no person expectation is created."),
        ("out", "Yes, I can build dashboards from this chat. Tell me what you want on it.", None)],
        thread=GROUP)
    real = await _stuck_case(pool, "Certificate expiry data", "Vinu must send the certificate data", [
        ("in", "Vinu please send the certificate expiry data", "Vinu owes the certificate data."),
        ("out", "Noted.", None)])
    # the shape that fooled the first live run: a person owes it, the assistant was
    # asked to keep an eye on it, and its "Noted" reply would read as an answer
    watched = await _stuck_case(pool, "Insurance certificate renewal",
                                "Ravi must send the renewed insurance certificate.", [
        ("in", "Ravi still has not sent the renewed insurance certificate, keep an eye on it",
         "Ravi owes the renewed insurance certificate; the owner asked the assistant to keep "
         "an eye on it."),
        ("out", "Noted, I will keep an eye on the insurance certificate from Ravi.", None)])
    unanswered = await _stuck_case(pool, "Fleet report", "Request to the assistant for a report; "
                                   "no person expectation is created.", [
        ("in", "send me the fleet report",
         "Request to the assistant for a report; no person expectation is created.")],
        thread="15550100003")
    sandbox = await _stuck_case(pool, "Test lookup", "Request to the assistant; no person "
                                "expectation.", [
        ("in", "test: specs please", "Request to the assistant; no person expectation."),
        ("out", "Here are the engine specs: test", None)], thread="test-local")
    await pool.execute("UPDATE cases SET sandbox = TRUE WHERE id = $1", sandbox)

    assert await triage.migrate_assistant_requests() == 2
    assert await triage.migrate_assistant_requests() == 0, "idempotent"
    counts = {r["case_id"]: r["n"] for r in await pool.fetch(
        "SELECT case_id, count(*) AS n FROM expectations GROUP BY case_id")}
    assert counts == {six: 1, five: 2}

    env["answers"] = _meet("engine specs:", "burning diesel", "build dashboards")
    await triage._tick()
    status = {r["id"]: (r["status"], r["close_reason"]) for r in
              await pool.fetch("SELECT id, status, close_reason FROM cases")}
    assert status[six] == ("closed", triage.ANSWERED_REASON)
    assert status[five] == ("closed", triage.ANSWERED_REASON)
    for cid in (real, watched, unanswered, sandbox):
        assert status[cid] == ("open", None)
        assert not await pool.fetchval("SELECT count(*) FROM expectations WHERE case_id = $1", cid)
    assert env["sent"] == [], "no WhatsApp for the clean-up's closes"


async def test_cleanup_leaves_a_request_open_when_the_reply_did_not_answer(env):
    from app import triage
    pool = env["pool"]
    cid = await _stuck_case(pool, "Vendor contract", "Request to the assistant; no person "
                            "expectation is created.", [
        ("in", "get me the signed vendor contract",
         "Request to the assistant; no person expectation is created."),
        ("out", "I couldn't reach the vendor portal.", None)])
    assert await triage.migrate_assistant_requests() == 1
    env["answers"] = _meet("engine specs:")
    await triage._tick()
    assert (await _case(pool, cid))["status"] == "open"
    x = dict(await pool.fetchrow("SELECT * FROM expectations WHERE case_id = $1", cid))
    assert x["status"] == "open" and x["owed_by"] == "assistant"
    assert x["description"] == "Answer: “get me the signed vendor contract”"


async def test_chat_display_name_on_real_tables(env):
    from app import chats
    pool = env["pool"]
    await pool.execute("INSERT INTO chats (jid, subject, is_group, status) VALUES ($1, 'Site B Ops', TRUE, 'active')",
                       GROUP)
    await pool.execute("INSERT INTO people (name, whatsapp, whatsapp_alt) VALUES ('Asha', $1, '{15550100004}')", DM)
    await pool.execute("INSERT INTO wa_lid_map (lid, phone, source) VALUES ('99887766554433', $1, 'message')", DM)
    assert await chats.display_name(pool, GROUP) == "WhatsApp · Site B Ops"
    assert await chats.display_name(pool, DM) == "WhatsApp · Asha"
    assert await chats.display_name(pool, "15550100004") == "WhatsApp · Asha"
    assert await chats.display_name(pool, "99887766554433@lid") == "WhatsApp · Asha"
    assert await chats.display_name(pool, "15550100005") == "WhatsApp chat"

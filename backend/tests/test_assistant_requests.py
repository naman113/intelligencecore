"""Quick 024: a request to the assistant must not leave a case open forever.

The client's cases #5 and #6 were requests the assistant answered in the same chat
("can you get me engine specs ... from web"); triage opened a case with no
expectation, so nothing could ever close it. Now the request becomes an expectation
owed by the assistant (owed_by='assistant'), the satisfaction sweep meets it from
the assistant's own reply in that chat, and the case closes quietly as "answered by
the assistant in the chat" (no "Done" WhatsApp). A request the reply does not
fulfil stays open with a visible commitment.

Regression cases asked for by the owner:
  (1) a lookup request answered -> closes;
  (2) a request the assistant cannot fulfil -> stays open with a commitment;
  (3) a person-owed commitment -> unchanged;
  (4) "did you send it?" about a closed request -> no new case.

Fakes only; no DB/LLM/network. The SQL itself runs against real Postgres in
test_assistant_requests_db.py. Numbers are fictional (555-01xx).
"""

import json
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import expectations, triage

NOW = datetime.now(timezone.utc)
DM = "15550100001"            # the owner's direct chat (fictional number)
OTHER_DM = "15550100002"
GROUP = "120363000000000001@g.us"


def _ev(id=17, source="whatsapp", thread_ref=DM, content="can you get me engine specs from the web",
        meta=None, **kw):
    e = {"id": id, "source": source, "content": content, "actor_id": None,
         "actor_raw": thread_ref, "actor_name": None, "occurred_at": NOW,
         "thread_ref": thread_ref, "meta": meta if meta is not None else {}}
    e.update(kw)
    return e


# ── prompt pins ─────────────────────────────────────────────────────────────

def test_triage_prompt_teaches_assistant_requests():
    p = triage.TRIAGE_SYSTEM
    assert "REQUESTS TO THE ASSISTANT" in p
    assert '"owner": "assistant"' in p and '"to_assistant": true' in p
    assert "could\nnot find it" in p or "could not find it" in p.replace("\n", " ")
    assert "ALREADY ANSWERED" in p and "did you send me the specs?" in p
    # the person rule keeps its example and owner instruction
    assert "Vinu sends the certificate expiry data" in p
    assert "set that\nperson as the owner" in p or "set that person as the owner" in p.replace("\n", " ")
    # the verdict schema admits the assistant as an owner
    assert '"assistant"' in p.split('"expectations_created"')[1].split("\n")[0]


def test_satisfy_prompt_says_what_does_not_count():
    p = " ".join(triage.SATISFY_SYSTEM.split())
    assert p.startswith("You decide which open EXPECTATIONS have now been fulfilled")
    assert "when unsure, leave it open" in p
    for phrase in ('"owed_by": "assistant"', "SAME \"chat\"", "asked_in_event",
                   "\"asked\" is the person's own words", "progress note",
                   "could not find, get or do it", "workaround", "come back later",
                   "question back", "leaves out the main thing asked"):
        assert phrase in p, phrase


def test_quick051_a_definite_cannot_closes_the_request():
    """Owner, 2026-11-05: when the assistant says it can't do it, the case closes. Only a
    temporary failure keeps the request open."""
    p = " ".join(triage.SATISFY_SYSTEM.split())
    yes, no = p.split("It is NOT fulfilled by", 1)
    assert "CANNOT do what was asked" in yes and "DOES fulfil it" in yes
    assert "no tool for it" in yes and "not allowed" in yes and "not possible" in yes
    assert "temporary failure" in no and "time-out" in no and "try again later" in no
    assert "even with a reason, a workaround" not in p


# ── was the assistant asked? ─────────────────────────────────────────────────

@pytest.mark.parametrize("event,expected", [
    (_ev(meta={"to_assistant": True}), True),
    (_ev(meta=json.dumps({"to_assistant": True})), True),       # asyncpg jsonb as str
    (_ev(thread_ref=GROUP, meta={"group": True, "to_assistant": True}), True),
    (_ev(thread_ref=GROUP, meta={"group": True, "to_assistant": False}), False),
    (_ev(meta={"to_assistant": False}), False),
    (_ev(meta={}), True),                     # recorded before Quick 024: a DM is always answered
    (_ev(thread_ref=GROUP, meta={"group": True}), False),        # legacy group: not known
    (_ev(thread_ref="99887766554433@lid", meta={}), True),       # legacy DM by private ID
    (_ev(thread_ref="grp-accounts", meta={}), False),            # a synthetic /api/test-event thread
    (_ev(thread_ref="test-local", meta={}), False),
    (_ev(source="email", thread_ref="inv-thread-9"), False),
    (_ev(source="db", thread_ref="machines"), False),
    (_ev(source="docintel", thread_ref=GROUP), False),
])
def test_to_assistant(event, expected):
    assert triage._to_assistant(event) is expected


def test_fmt_event_flags_only_addressed_events():
    assert triage._fmt_event(_ev(meta={"to_assistant": True}))["to_assistant"] is True
    out = triage._fmt_event(_ev(thread_ref=GROUP, meta={"group": True, "to_assistant": False}))
    assert "to_assistant" not in out


# ── _apply_verdict ───────────────────────────────────────────────────────────

class VerdictConn:
    """Records writes; a new case is 41, an existing open case resolves to itself."""

    def __init__(self):
        self.executed = []
        self.fetchvals = []

    async def fetchval(self, sql, *args):
        self.fetchvals.append((sql, args))
        if "INSERT INTO cases" in sql:
            return 41
        if "SELECT id FROM cases WHERE id" in sql:
            return args[0]
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"

    def inserts(self):
        return [(s, a) for s, a in self.executed if "INSERT INTO expectations" in s]


def _verdict(*exps, case="new", **kw):
    v = {"relevant": True, "case": case, "new_case_title": "Engine specs lookup",
         "participants": [], "expectations_created": list(exps),
         "expectations_satisfied": [], "expectations_updated": [],
         "case_note": None, "reason": "request to the assistant"}
    v.update(kw)
    return v


ASSISTANT_EXP = {"description": "Find the engine specs of our machines on the web",
                 "owner": "assistant", "due_at": "2026-11-30T10:00:00Z",
                 "satisfy_hint": "the specs are in the reply"}


async def test_request_to_assistant_records_its_commitment():
    conn = VerdictConn()
    cid = await triage._apply_verdict(conn, _ev(id=17, meta={"to_assistant": True}),
                                      _verdict(ASSISTANT_EXP))
    assert cid == 41
    (sql, args), = conn.inserts()
    assert "'assistant'" in sql and "owed_by" in sql and "source_event_id" in sql
    assert "NULL, NULL" in sql, "no person owner and never a due date"
    assert args == (41, ASSISTANT_EXP["description"], "the specs are in the reply", 17)


@pytest.mark.parametrize("owner", ["assistant", "Assistant", " the assistant "])
async def test_assistant_owner_spellings(owner):
    conn = VerdictConn()
    await triage._apply_verdict(conn, _ev(meta={"to_assistant": True}),
                                _verdict({**ASSISTANT_EXP, "owner": owner}))
    (sql, _), = conn.inserts()
    assert "'assistant'" in sql


async def test_assistant_commitment_needs_the_assistant_to_be_replying():
    """A group line not addressed to the assistant gets no reply run, so the
    assistant owes nothing there, whatever the model says."""
    conn = VerdictConn()
    await triage._apply_verdict(
        conn, _ev(thread_ref=GROUP, meta={"group": True, "to_assistant": False}),
        _verdict(ASSISTANT_EXP))
    assert conn.inserts() == []


async def test_one_assistant_commitment_per_event():
    conn = VerdictConn()
    await triage._apply_verdict(
        conn, _ev(meta={"to_assistant": True}),
        _verdict(ASSISTANT_EXP, {**ASSISTANT_EXP, "description": "Also the fuel tank size"}))
    assert len(conn.inserts()) == 1


async def test_db_event_never_records_a_commitment():
    conn = VerdictConn()
    await triage._apply_verdict(conn, _ev(source="db", thread_ref="machines",
                                          meta={"to_assistant": True}),
                                _verdict(ASSISTANT_EXP, case=19))
    assert conn.inserts() == []


async def test_person_owed_commitment_unchanged(monkeypatch):
    """(3) A person's commitment is written exactly as before: resolved owner,
    the inferred due date, no owed_by."""
    from app import people

    async def resolve(conn, *, phone=None, email=None, alias=None):
        return 7 if alias == "Vinu" else None
    monkeypatch.setattr(people, "resolve", resolve)

    conn = VerdictConn()
    person = {"description": "Vinu sends the certificate expiry data", "owner": "Vinu",
              "due_at": "2026-11-30T10:00:00Z", "satisfy_hint": "the data arrives"}
    await triage._apply_verdict(conn, _ev(thread_ref=GROUP, meta={"group": True}),
                                _verdict(person))
    (sql, args), = conn.inserts()
    assert "owed_by" not in sql
    assert args[0] == 41 and args[1] == person["description"] and args[2] == 7
    assert args[3] == datetime(2026, 11, 30, 10, 0, tzinfo=timezone.utc)


async def test_request_that_also_needs_a_person_records_both(monkeypatch):
    from app import people

    async def resolve(conn, *, phone=None, email=None, alias=None):
        return 7 if alias == "Vinu" else None
    monkeypatch.setattr(people, "resolve", resolve)
    conn = VerdictConn()
    person = {"description": "Vinu sends the certificate", "owner": "Vinu",
              "due_at": None, "satisfy_hint": "certificate arrives"}
    await triage._apply_verdict(conn, _ev(meta={"to_assistant": True}),
                                _verdict({**ASSISTANT_EXP, "description": "Ask Vinu for it"}, person))
    sqls = [s for s, _ in conn.inserts()]
    assert len(sqls) == 2
    assert sum("'assistant'" in s for s in sqls) == 1


async def test_triage_cannot_satisfy_or_reassign_an_assistant_commitment():
    """Only the assistant's reply meets its commitment (the sweep). The requester's
    own "did you send it?" must not mark it met and trigger a false "Done"."""
    conn = VerdictConn()
    await triage._apply_verdict(
        conn, _ev(meta={"to_assistant": True}),
        _verdict(case=19, expectations_satisfied=[5],
                 expectations_updated=[{"id": 5, "due_at": "2026-11-30T10:00:00Z", "owner": None}]))
    sat, = [s for s, _ in conn.executed if "SET status = 'satisfied'" in s]
    upd, = [s for s, _ in conn.executed if "SET due_at" in s]
    assert "owed_by IS DISTINCT FROM 'assistant'" in sat
    assert "owed_by IS DISTINCT FROM 'assistant'" in upd


async def test_triage_index_names_the_assistant_and_hides_closed_cases():
    class Rec:
        sql = None

        async def fetch(self, sql, *args):
            Rec.sql = sql
            return []
    await triage._open_expectations_index(Rec())
    assert "WHEN e.owed_by = 'assistant'" in Rec.sql and "'assistant' END) AS owner" in Rec.sql
    assert "e.owed_by IS DISTINCT FROM 'assistant' OR c.status = 'open'" in Rec.sql
    assert "NOT c.sandbox" in Rec.sql


# ── the satisfaction sweep ───────────────────────────────────────────────────

class SweepDB:
    """In-memory events/expectations/cases answering the sweep's three queries the
    way the SQL does (each filter is asserted to be in the SQL before it is
    applied, so a dropped clause fails here too)."""

    def __init__(self, events, exps, cases=None):
        self.events = {e["id"]: {"source": "whatsapp", "provenance": "self",
                                 "triage_status": "pending", **e} for e in events}
        self.exps = {x["id"]: {"status": "open", "owed_by": None, "source_event_id": None,
                               "last_checked_at": None, "case_id": 41,
                               "satisfied_by": None, "satisfy_hint": "",
                               "description": f"exp {x['id']}", **x} for x in exps}
        self.cases = cases or {41: {"status": "open", "sandbox": False, "title": "Engine specs"}}
        self.sqls = []

    # pool / conn in one object
    def acquire(self):
        db = self

        class Ctx:
            async def __aenter__(self_c):
                return db

            async def __aexit__(self_c, *e):
                return False
        return Ctx()

    async def fetch(self, sql, *args):
        self.sqls.append(sql)
        if "triage_status = 'pending'" in sql:
            assert "provenance = 'self'" in sql
            return [{"id": e["id"], "content": e["content"], "thread_ref": e["thread_ref"]}
                    for e in sorted(self.events.values(), key=lambda e: e["id"])
                    if e["provenance"] == "self" and e["triage_status"] == "pending"]
        if "CROSS JOIN LATERAL" in sql:
            for clause in ("x.owed_by = 'assistant'", "x.last_checked_at IS NULL",
                           "NOT c.sandbox", "c.status = 'open'", "r.id > src.id",
                           "r.thread_ref = src.thread_ref", "r.provenance = 'self'"):
                assert clause in sql, clause
            out = []
            for x in self.exps.values():
                c = self.cases[x["case_id"]]
                if (x["status"] != "open" or x["owed_by"] != "assistant"
                        or x["last_checked_at"] is not None or c["sandbox"]
                        or c["status"] != "open"):
                    continue
                src = self.events.get(x["source_event_id"])
                if not src:
                    continue
                later = [e for e in sorted(self.events.values(), key=lambda e: e["id"])
                         if e["thread_ref"] == src["thread_ref"] and e["provenance"] == "self"
                         and e["source"] == "whatsapp" and e["id"] > src["id"]][:args[0]]
                out += [{"id": e["id"], "content": e["content"], "thread_ref": e["thread_ref"]}
                        for e in later]
            return out
        if "FROM expectations e JOIN cases c" in sql:
            assert "NOT c.sandbox" in sql
            assert "e.owed_by IS DISTINCT FROM 'assistant' OR c.status = 'open'" in sql
            out = []
            for x in sorted(self.exps.values(), key=lambda x: -x["id"]):
                c = self.cases[x["case_id"]]
                if x["status"] != "open" or c["sandbox"]:
                    continue
                if x["owed_by"] == "assistant" and c["status"] != "open":
                    continue
                src = self.events.get(x["source_event_id"])
                out.append({"id": x["id"], "description": x["description"],
                            "satisfy_hint": x["satisfy_hint"], "case_title": c["title"],
                            "owed_by": x["owed_by"], "source_event_id": x["source_event_id"],
                            "asked_in": src["thread_ref"] if src else None,
                            "asked_text": src["content"] if src else None})
            return out
        return []

    async def execute(self, sql, *args):
        self.sqls.append(sql)
        if "SET status = 'satisfied'" in sql:
            x = self.exps.get(args[0])
            if not x or x["status"] != "open" or self.cases[x["case_id"]]["sandbox"]:
                return "UPDATE 0"
            if "owed_by = 'assistant'" in sql and x["owed_by"] != "assistant":
                return "UPDATE 0"
            if "owed_by IS DISTINCT FROM 'assistant'" in sql and x["owed_by"] == "assistant":
                return "UPDATE 0"
            x.update(status="satisfied", satisfied_by=args[1])
            return "UPDATE 1"
        if "SET last_checked_at" in sql:
            for xid in args[0]:
                if self.exps[xid]["owed_by"] == "assistant":
                    self.exps[xid]["last_checked_at"] = NOW
            return "UPDATE"
        if "SET triage_status = 'skipped'" in sql:
            for eid in args[0]:
                self.events[eid]["triage_status"] = "skipped"
            return "UPDATE"
        return "UPDATE 0"


def _wire_sweep(monkeypatch, db, answer):
    """`answer` maps the payload the model sees to its `satisfied` list."""
    async def gp():
        return db
    monkeypatch.setattr(triage, "get_pool", gp)
    stub = types.ModuleType("app.agents.graph")
    stub._callbacks = lambda: {}
    monkeypatch.setitem(sys.modules, "app.agents.graph", stub)
    seen = []

    class Model:
        async def ainvoke(self, msgs, config=None):
            payload = json.loads(msgs[1][1])
            seen.append(payload)
            assert msgs[0][1] == triage.SATISFY_SYSTEM
            return SimpleNamespace(content=json.dumps({"satisfied": answer(payload)}))
    monkeypatch.setattr(triage, "_model", lambda: Model())
    return seen


REQUEST = {"id": 17, "content": "can you get me engine specs from the web",
           "thread_ref": DM, "provenance": "external", "triage_status": "done"}
ON_IT = {"id": 18, "content": "On it. I'm checking which machines we have.", "thread_ref": DM}
SPECS = {"id": 20, "content": "Here are the engine specs: A 934 C, 150 kW ...", "thread_ref": DM}
COMMITMENT = {"id": 5, "owed_by": "assistant", "source_event_id": 17,
              "description": "Find the engine specs on the web"}


async def test_lookup_answered_is_met_by_the_reply(monkeypatch):
    """(1) The reply with the specs meets the commitment; the progress ping is
    in the payload too, labelled with the same opaque chat, no number."""
    db = SweepDB([REQUEST, ON_IT, SPECS], [COMMITMENT])
    seen = _wire_sweep(monkeypatch, db, lambda p: [{"expectation_id": 5, "delivery_event_id": 20}])

    await triage._satisfaction_sweep()

    assert db.exps[5]["status"] == "satisfied" and db.exps[5]["satisfied_by"] == 20
    assert db.exps[5]["last_checked_at"] is not None
    assert db.events[18]["triage_status"] == db.events[20]["triage_status"] == "skipped"
    payload, = seen
    exp, = payload["open_expectations"]
    assert exp["owed_by"] == "assistant" and exp["asked_in_event"] == 17
    # judged against the requester's own words, never triage's (possibly lenient) hint
    assert exp["asked"] == REQUEST["content"] and "satisfies_when" not in exp
    chats = {d["chat"] for d in payload["assistant_deliveries"]}
    assert chats == {exp["chat"]} and DM not in json.dumps(payload)


async def test_reply_that_does_not_fulfil_leaves_it_open(monkeypatch):
    """(2) "I couldn't find it" (or only "on it"): the model marks nothing, the
    commitment stays open and is not offered again without a new reply."""
    couldnt = {"id": 20, "thread_ref": DM,
               "content": "I couldn't find engine specs for those machines online."}
    db = SweepDB([REQUEST, ON_IT, couldnt], [COMMITMENT])
    seen = _wire_sweep(monkeypatch, db, lambda p: [])

    await triage._satisfaction_sweep()
    assert db.exps[5]["status"] == "open" and db.exps[5]["last_checked_at"] is not None

    await triage._satisfaction_sweep()          # nothing new: no second call
    assert len(seen) == 1


@pytest.mark.parametrize("delivery,why", [
    ({"id": 21, "content": "Specs for the other chat", "thread_ref": OTHER_DM}, "another chat"),
    ({"id": 16, "content": "an older reply", "thread_ref": DM}, "before the request"),
    (None, "no delivery id"),
])
async def test_code_rejects_a_match_outside_the_request_chat(monkeypatch, delivery, why):
    events = [REQUEST, ON_IT] + ([delivery] if delivery else [])
    db = SweepDB(events, [COMMITMENT])
    did = delivery["id"] if delivery else None
    _wire_sweep(monkeypatch, db, lambda p: [{"expectation_id": 5, "delivery_event_id": did}])
    await triage._satisfaction_sweep()
    assert db.exps[5]["status"] == "open", why


async def test_reply_swept_before_the_commitment_existed_still_counts(monkeypatch):
    """The race: triage's call failed, the sweep marked the reply 'skipped', and
    triage recorded the commitment a tick later. Its first check still sees the
    reply (catch-up by chat and order, whatever the triage_status)."""
    db = SweepDB([REQUEST, {**ON_IT, "triage_status": "skipped"},
                  {**SPECS, "triage_status": "skipped"}], [COMMITMENT])
    seen = _wire_sweep(monkeypatch, db, lambda p: [{"expectation_id": 5, "delivery_event_id": 20}])
    await triage._satisfaction_sweep()
    assert db.exps[5]["status"] == "satisfied"
    assert [d["delivery_event_id"] for d in seen[0]["assistant_deliveries"]] == [18, 20]


async def test_no_model_call_without_a_reply_in_the_request_chat(monkeypatch):
    db = SweepDB([REQUEST, {"id": 21, "content": "an alert for another chat",
                            "thread_ref": OTHER_DM}], [COMMITMENT])
    seen = _wire_sweep(monkeypatch, db, lambda p: [])
    await triage._satisfaction_sweep()
    assert seen == [], "nothing could meet it: no call"
    assert db.events[21]["triage_status"] == "skipped"
    assert db.exps[5]["last_checked_at"] is None, "still waiting for its reply"


async def test_commitment_on_a_closed_case_is_not_checked(monkeypatch):
    db = SweepDB([REQUEST, SPECS], [COMMITMENT],
                 cases={41: {"status": "closed", "sandbox": False, "title": "x"}})
    seen = _wire_sweep(monkeypatch, db, lambda p: [{"expectation_id": 5, "delivery_event_id": 20}])
    await triage._satisfaction_sweep()
    assert seen == [] and db.exps[5]["status"] == "open"


async def test_sandbox_commitments_stay_out(monkeypatch):
    db = SweepDB([REQUEST, SPECS], [COMMITMENT],
                 cases={41: {"status": "open", "sandbox": True, "title": "x"}})
    seen = _wire_sweep(monkeypatch, db, lambda p: [{"expectation_id": 5, "delivery_event_id": 20}])
    await triage._satisfaction_sweep()
    assert seen == [] and db.exps[5]["status"] == "open"
    assert all("NOT c.sandbox" in s for s in db.sqls if "FROM expectations" in s)


async def test_person_owed_path_unchanged(monkeypatch):
    """(3) A person's expectation is still met by any assistant delivery, with or
    without a delivery id, in any chat; and an assistant commitment cannot be
    met through the person path by a hallucinated id."""
    db = SweepDB([{"id": 30, "content": "Vinu's certificate data: expires 2027-03",
                   "thread_ref": GROUP}],
                 [{"id": 8, "description": "Vinu sends the certificate expiry data"},
                  {"id": 9, "description": "Rakesh sends the March sheet"},
                  COMMITMENT])
    db.events[17] = {**REQUEST, "source": "whatsapp"}
    seen = _wire_sweep(monkeypatch, db, lambda p: [
        {"expectation_id": 8, "delivery_event_id": 30},
        {"expectation_id": 9},
        {"expectation_id": 5, "delivery_event_id": 30}])
    await triage._satisfaction_sweep()
    assert db.exps[8]["status"] == "satisfied" and db.exps[8]["satisfied_by"] == 30
    assert db.exps[9]["status"] == "satisfied" and db.exps[9]["satisfied_by"] is None
    assert db.exps[5]["status"] == "open"
    offered = {e["expectation_id"] for e in seen[0]["open_expectations"]}
    assert offered == {8, 9}, "the commitment had no reply in its chat: not offered"
    assert all("owed_by" not in e for e in seen[0]["open_expectations"])


# ── closing ──────────────────────────────────────────────────────────────────

async def test_complete_finished_cases_closes_answered_requests_quietly(monkeypatch):
    rows = [{"id": 1, "all_assistant": True, "any_met": True},
            {"id": 2, "all_assistant": False, "any_met": True},
            {"id": 3, "all_assistant": True, "any_met": False}]

    class Pool:
        sql = None

        async def fetch(self, sql, *args):
            Pool.sql = sql
            return rows

    async def gp():
        return Pool()
    monkeypatch.setattr(triage, "get_pool", gp)
    calls = []

    async def close(pool, cid, reason=None, *, kind="completed", statuses=("open",)):
        calls.append((cid, reason, kind))
        return True
    monkeypatch.setattr(triage, "_close_case", close)

    await triage._complete_finished_cases()
    assert calls == [
        (1, "answered by the assistant in the chat", "answered"),
        (2, "auto: all expectations satisfied", "completed"),
        (3, "the assistant's request was dismissed", "answered"),
    ]
    # the old gate is intact: at least one expectation, none open or violated
    assert "e.status IN ('open','violated')" in Pool.sql
    assert "e.owed_by IS DISTINCT FROM 'assistant'" in Pool.sql


class ClosePool:
    def __init__(self, sandbox=False):
        self.sandbox = sandbox
        self.executed = []

    async def fetchval(self, sql, *args):
        if "UPDATE cases SET status = 'closed'" in sql:
            return args[0]
        if "SELECT sandbox" in sql:
            return self.sandbox
        if "SELECT origin" in sql:
            return None
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def _wire_close(monkeypatch):
    monkeypatch.setattr(triage, "get_settings",
                        lambda: SimpleNamespace(case_chaining_enabled=True))
    seen = {"notify": [], "distill": [], "chain": []}

    async def notify(case_id, kind="completed"):
        seen["notify"].append((case_id, kind))
        return True

    async def distill(case_id):
        seen["distill"].append(case_id)

    async def chain(pool, case_id):
        seen["chain"].append(case_id)
    import app.actionsvc
    import app.memory_pipeline
    monkeypatch.setattr(app.actionsvc, "notify_case_closed", notify)
    monkeypatch.setattr(app.memory_pipeline, "distill_case", distill)
    monkeypatch.setattr(triage, "_maybe_chain_case", chain)
    return seen


async def test_answered_close_sends_nothing_and_distills_nothing(monkeypatch):
    seen = _wire_close(monkeypatch)
    pool = ClosePool()
    assert await triage._close_case(pool, 6, triage.ANSWERED_REASON, kind="answered")
    assert seen == {"notify": [], "distill": [], "chain": []}
    stamp, = [s for s, _ in pool.executed if "distilled_at" in s]
    assert "distillation = ''" in stamp and "distilled_at IS NULL" in stamp


async def test_completed_close_still_notifies_distills_and_chains(monkeypatch):
    """Real completions by people keep their notice (and the rest of the path)."""
    seen = _wire_close(monkeypatch)
    assert await triage._close_case(ClosePool(), 7, "auto: all expectations satisfied")
    assert seen == {"notify": [(7, "completed")], "distill": [7], "chain": [7]}


async def test_notify_refuses_answered_kind(monkeypatch):
    from app import actionsvc

    async def boom(*a, **k):
        raise AssertionError("nothing may be looked up or sent for an answered close")
    monkeypatch.setattr(actionsvc, "get_pool", boom)
    monkeypatch.setattr(actionsvc.whatsapp, "send_message", boom)
    assert await actionsvc.notify_case_closed(6, kind="answered") is False


async def test_stale_sweep_ignores_assistant_commitments_and_keeps_its_wording(monkeypatch):
    class Pool:
        sql = None

        async def fetch(self, sql, *args):
            Pool.sql = sql
            return [{"id": 4}]

    async def gp():
        return Pool()
    monkeypatch.setattr(triage, "get_pool", gp)
    monkeypatch.setattr(triage, "get_settings", lambda: SimpleNamespace(stale_case_days=21))
    calls = []

    async def close(pool, cid, reason=None, *, kind="completed", statuses=("open",)):
        calls.append((cid, reason, kind))
        return True
    monkeypatch.setattr(triage, "_close_case", close)
    await triage._close_stale_cases()
    assert "e.status = 'open'" in Pool.sql
    assert "AND e.owed_by IS DISTINCT FROM 'assistant'" in Pool.sql
    assert calls == [(4, "no activity for 21+ days (auto-closed: stale)", "stale")]


async def test_violation_sweep_never_fires_for_assistant_commitments(monkeypatch):
    sqls = []

    class Conn:
        async def fetch(self, sql, *args):
            sqls.append(sql)
            return []

    class Pool:
        def acquire(self):
            class Ctx:
                async def __aenter__(self_c):
                    return Conn()

                async def __aexit__(self_c, *e):
                    return False
            return Ctx()

    async def gp():
        return Pool()
    monkeypatch.setattr(expectations, "get_pool", gp)
    assert await expectations._sweep_violations() == 0
    assert "e.owed_by IS DISTINCT FROM 'assistant'" in sqls[0]


# ── (4) a follow-up about an answered request ────────────────────────────────

async def test_follow_up_about_answered_request_opens_nothing(monkeypatch):
    """(4) "did you send me the engine specs?" after the case closed: the model
    sees the assistant's answer in recent_thread_context and the flag; with the
    verdict the prompt asks for (not relevant) nothing is written."""
    written = []

    class Conn:
        def transaction(self):
            class Tx:
                async def __aenter__(self_t):
                    return None

                async def __aexit__(self_t, *e):
                    return False
            return Tx()

        async def fetch(self, sql, *args):
            if "triage_status = 'pending'" in sql:
                return [_ev(id=28, content="did you send me the engine specs i asked for ?",
                            meta=json.dumps({"to_assistant": True}))]
            if "close_reason = $2" in sql:        # _answered_here
                assert "status = 'closed'" in sql and "sandbox = $4" in sql
                assert args[1] == triage.ANSWERED_REASON and args[3] is False
                return [{"title": "Engine specs lookup", "closed_at": NOW}]
            if "thread_ref = $1" in sql:          # newest first, like ORDER BY e.id DESC
                return [{"id": 20, "content": "Here are the engine specs: A 934 C ...",
                         "provenance": "self", "actor": None},
                        {"id": 17, "content": "can you get me engine specs from the web",
                         "provenance": "external", "actor": "Asha"}]
            return []

        async def fetchval(self, sql, *args):
            written.append(sql)
            return None

        async def execute(self, sql, *args):
            if "triage_status" not in sql:
                written.append(sql)
            return "UPDATE 1"

    class Pool:
        def acquire(self):
            class Ctx:
                async def __aenter__(self_c):
                    return Conn()

                async def __aexit__(self_c, *e):
                    return False
            return Ctx()

    async def gp():
        return Pool()
    monkeypatch.setattr(triage, "get_pool", gp)
    monkeypatch.setattr(triage, "get_settings",
                        lambda: SimpleNamespace(company_name="", owner_number="", test_threads=""))
    stub = types.ModuleType("app.agents.graph")
    stub._callbacks = lambda: {}
    monkeypatch.setitem(sys.modules, "app.agents.graph", stub)
    captured = {}

    class Model:
        async def ainvoke(self, msgs, config=None):
            captured.update(json.loads(msgs[1][1]))
            return SimpleNamespace(content=json.dumps({"verdicts": [
                {"event_id": 28, "relevant": False, "case": "none",
                 "reason": "only checks on a request the assistant already answered"}]}))
    monkeypatch.setattr(triage, "_model", lambda: Model())

    async def noop():
        pass
    monkeypatch.setattr(triage, "_refresh_summaries", noop)
    monkeypatch.setattr(triage, "_complete_finished_cases", noop)

    assert await triage._triage_batch() == 1
    ev, = captured["events"]
    assert ev["to_assistant"] is True
    assert ev["recent_thread_context"][-1].startswith("assistant: Here are the engine specs")
    # even when the answer has scrolled out of the context, the chat's answered
    # requests are named (titles only)
    assert ev["answered_here"][0].startswith("Engine specs lookup (answered ")
    assert written == [], "no case, no link, no expectation"


async def test_follow_up_case_if_opened_closes_quietly(monkeypatch):
    """(4, fallback) Should the model still open a case for the follow-up, it is a
    request to the assistant like any other: the reply ("Yes, I sent them
    earlier ...") meets it and the case closes as answered, with no notice."""
    follow = {"id": 28, "content": "did you send me the engine specs i asked for ?",
              "thread_ref": DM, "provenance": "external", "triage_status": "done"}
    reply = {"id": 30, "content": "Yes, I sent them in this chat earlier today.",
             "thread_ref": DM}
    db = SweepDB([follow, reply], [{"id": 6, "owed_by": "assistant", "source_event_id": 28,
                                    "description": "Confirm the specs were sent"}])
    _wire_sweep(monkeypatch, db, lambda p: [{"expectation_id": 6, "delivery_event_id": 30}])
    await triage._satisfaction_sweep()
    assert db.exps[6]["status"] == "satisfied"


# ── the clean-up of cases stuck before this change ──────────────────────────

def _rows(*events, reason="Open request to the assistant for engine specs. The assistant "
                         "owes the lookup, so no person expectation is created."):
    return [{"case_id": 6, "opened_reason": reason, "source": "whatsapp", "thread_ref": DM,
             "replied": True, **e} for e in events]


CASE6 = _rows(
    {"event_id": 17, "content": "can you get me engine specs of the machines from web",
     "note": "Open request to the assistant for engine specs. The assistant owes the "
             "lookup, so no person expectation is created."},
    {"event_id": 28, "content": "did you send me the engine specs i asked for ?",
     "note": "Follow-up question on the open engine specs lookup; the specs were already "
             "delivered in the thread, so the case is advanced without creating any human "
             "expectation."})

CASE5 = _rows(
    {"event_id": 9, "content": "is any machine actively consuming diesel",
     "note": "Direct question from the owner awaiting an answer from the assistant's data. "
             "No human owes a deliverable, so no expectation is created."},
    {"event_id": 14, "content": "can you create dashboards from here",
     "note": "Request for a dashboard on the diesel subject. It advances case 5, but the "
             "assistant owns the work, so no person expectation is created."},
    {"event_id": 21, "content": "[replying to the assistant: \"...\"] diesel status of the fleet please",
     "note": "Direct request for fleet diesel status matches open case 5; the assistant "
             "answers it itself, so no human expectation is created."},
    reason="Direct question from the owner awaiting an answer from the assistant's data. "
           "No human owes a deliverable, so no expectation is created.")


def test_cleanup_adopts_the_client_shapes():
    six = triage._requests_to_adopt(CASE6)
    assert [r["event_id"] for r in six] == [17], "the follow-up is not a request of its own"
    assert six[0]["description"].startswith("Answer: “can you get me engine specs")
    five = triage._requests_to_adopt(CASE5)
    assert [r["event_id"] for r in five] == [9, 14, 21]
    assert five[2]["description"] == "Answer: “diesel status of the fleet please”"


@pytest.mark.parametrize("change,why", [
    (lambda rows: [{**rows[0], "note": "Vinu promised to send the certificate by Friday."}]
     + rows[1:], "a verdict that names a person's part"),
    (lambda rows: rows + [{**rows[0], "event_id": 40, "source": "docintel",
                           "note": "Invoice read; no human expectation."}], "a document"),
    (lambda rows: rows[:-1] + [{**rows[-1], "replied": False}], "no reply to the latest request"),
    (lambda rows: [{**r, "note": "Owner shared the fuel figures. No human owes anything.",
                    "opened_reason": "Fuel figures shared"} for r in rows],
     "nothing asked of the assistant"),
    (lambda rows: [], "no linked messages"),
    (lambda rows: rows + [{**rows[0], "event_id": 50, "note": ""}],
     "a later message with no verdict note"),
    # the live p18 failure before the rule was tightened: a person owes it and the
    # owner asked the assistant to keep an eye on it (assistant + "owes" + "asked")
    (lambda rows: [{**rows[0], "note": "Ravi owes the renewed insurance certificate; the "
                                       "owner asked the assistant to keep an eye on it.",
                    "opened_reason": "Ravi must send the renewed insurance certificate."}],
     "a person owes it; the assistant only watches"),
    (lambda rows: [{**rows[0], "note": "Ravi owes the certificate; no person expectation is "
                                       "created because the assistant was asked to track it."}],
     "a disclaimer next to a named person owing"),
    (lambda rows: [{**rows[0], "note": "", "opened_reason": "Request to the assistant for the "
                                                             "fleet report."}],
     "an opened_reason that does not rule out a person's part"),
])
def test_cleanup_leaves_anything_else_alone(change, why):
    assert triage._requests_to_adopt(change(CASE5)) == [], why


async def test_cleanup_writes_commitments_once(monkeypatch):
    class Conn:
        def __init__(self, db):
            self.db = db

        def transaction(self):
            class Tx:
                async def __aenter__(self_t):
                    return None

                async def __aexit__(self_t, *e):
                    return False
            return Tx()

        async def fetchval(self, sql, *args):
            return 1 if any(x[0] == args[0] for x in self.db.inserted) else None

        async def execute(self, sql, *args):
            assert "'assistant'" in sql and "NULL, NULL" in sql
            self.db.inserted.append(args)

    class Pool:
        inserted = []

        async def fetch(self, sql, *args):
            assert "NOT EXISTS (SELECT 1 FROM expectations x WHERE x.case_id = c.id)" in sql
            assert "NOT c.sandbox" in sql and "c.origin IS NULL" in sql
            assert "c.status = 'open'" in sql and "e.provenance = 'external'" in sql
            return [] if self.inserted else CASE6

        def acquire(self):
            db = self

            class Ctx:
                async def __aenter__(self_c):
                    return Conn(db)

                async def __aexit__(self_c, *e):
                    return False
            return Ctx()

    pool = Pool()

    async def gp():
        return pool
    monkeypatch.setattr(triage, "get_pool", gp)
    assert await triage.migrate_assistant_requests() == 1
    (cid, desc, hint, src), = pool.inserted
    assert (cid, src) == (6, 17) and hint == triage.ADOPTED_HINT
    assert await triage.migrate_assistant_requests() == 0, "idempotent"


async def test_cleanup_never_raises(monkeypatch):
    async def boom():
        raise OSError("db down")
    monkeypatch.setattr(triage, "get_pool", boom)
    assert await triage.migrate_assistant_requests() == 0


# ── where a case started, by name ────────────────────────────────────────────

class NamePool:
    def __init__(self, subjects=None, people=None, lids=None, sources=None, emails=None):
        self.subjects = subjects or {}
        self.people = people or {}
        self.lids = lids or {}
        self.sources = sources or {}
        self.emails = emails or {}

    async def fetchval(self, sql, *args):
        if "FROM chats" in sql:
            return self.subjects.get(args[0])
        if "FROM events" in sql:
            return self.sources.get(args[0], "whatsapp")
        if "FROM wa_lid_map" in sql:
            return self.lids.get(args[0])
        if "lower(email)" in sql:
            return self.emails.get(args[0].lower())
        if "FROM people" in sql:
            return self.people.get(args[0])
        return None


@pytest.mark.parametrize("ref,pool,expected", [
    (GROUP, NamePool(subjects={GROUP: "Site B Ops"}), "WhatsApp · Site B Ops"),
    (GROUP, NamePool(), "WhatsApp group"),
    (DM, NamePool(people={DM: "Asha"}), "WhatsApp · Asha"),
    (DM, NamePool(), "WhatsApp chat"),
    ("99887766554433@lid", NamePool(lids={"99887766554433": DM}, people={DM: "Asha"}),
     "WhatsApp · Asha"),
    ("99887766554433@lid", NamePool(), "WhatsApp chat"),
    ("vendor@example.com", NamePool(sources={"vendor@example.com": "email"},
                                    emails={"vendor@example.com": "Ravi"}), "Email · Ravi"),
    ("inv-thread-9", NamePool(sources={"inv-thread-9": "email"}), "Email thread"),
    ("ui:owner", NamePool(), "Ask chat"),
    (None, NamePool(), None),
])
async def test_chat_display_name(ref, pool, expected):
    from app import chats
    assert await chats.display_name(pool, ref) == expected


async def test_chat_display_name_never_shows_the_number_on_errors():
    from app import chats

    class Boom:
        async def fetchval(self, *a):
            raise RuntimeError("db down")
    assert await chats.display_name(Boom(), DM) == "WhatsApp chat"
    assert await chats.display_name(Boom(), GROUP) == "WhatsApp group"


async def test_case_detail_names_the_chat_and_the_assistant(monkeypatch):
    import app.main as main

    class Pool(NamePool):
        async def fetchrow(self, sql, *args):
            return {"id": 6, "title": "Engine specs lookup", "status": "open",
                    "sandbox": False, "origin_chat_jid": DM}

        async def fetch(self, sql, *args):
            if "FROM expectations e" in sql:
                assert "e.owed_by" in sql
                return [{"id": 5, "description": "Find the specs", "status": "open",
                         "due_at": None, "reminder_count": 0, "satisfy_hint": "",
                         "owner": None, "owed_by": "assistant"}]
            return []

    pool = Pool(people={DM: "Asha"})

    async def gp():
        return pool
    monkeypatch.setattr(main, "db", SimpleNamespace(get_pool=gp))
    out = await main.api_case_detail(6)
    assert out["case"]["origin_chat_name"] == "WhatsApp · Asha"
    assert out["expectations"][0]["owed_by"] == "assistant"


# ── the webhook records whether the assistant is replying ───────────────────

async def test_ingest_inbound_records_to_assistant(monkeypatch):
    from app.connectors import whatsapp_events
    got = []

    async def ingest(ev):
        got.append(ev)
        return 1
    monkeypatch.setattr(whatsapp_events, "ingest", ingest)
    await whatsapp_events.ingest_inbound(phone=DM, text="hi", wa_message_id="m1",
                                         to_assistant=True)
    await whatsapp_events.ingest_inbound(phone=DM, text="fyi", wa_message_id="m2",
                                         group_jid=GROUP, is_group=True, name="Asha",
                                         to_assistant=False)
    await whatsapp_events.ingest_inbound(phone=DM, text="old", wa_message_id="m3")
    assert got[0].meta == {"to_assistant": True}
    assert got[1].meta == {"group": True, "sender_name": "Asha", "to_assistant": False}
    assert got[2].meta == {}, "callers that do not say keep the old meta"


@pytest.mark.parametrize("over,expected", [
    ({"chatId": "", "isGroup": False}, True),                  # a DM is always answered
    ({"mentioned": True}, True),                               # a group message addressed to it
    ({"mentioned": False}, False),                             # a group line it only observes
])
async def test_webhook_passes_whether_a_reply_run_was_queued(monkeypatch, over, expected):
    """The flag is the reply-run gate itself (harness of test_webhook_reply_context)."""
    import app.main as main
    from app import chats
    from app.connectors import whatsapp_events

    class Conn:
        async def execute(self, sql, *args):
            return "INSERT 0 1"

        async def fetchval(self, sql, *args):
            if "INSERT INTO messages" in sql:
                return 11
            if "INSERT INTO task_runs" in sql:
                return 21
            return None

    class Pool:
        def acquire(self):
            class Ctx:
                async def __aenter__(self_c):
                    return Conn()

                async def __aexit__(self_c, *e):
                    return False
            return Ctx()

    async def gp():
        return Pool()
    monkeypatch.setattr(main, "db", SimpleNamespace(get_pool=gp))
    monkeypatch.setattr(main, "get_settings",
                        lambda: SimpleNamespace(webhook_secret="s", allowlist=set(), whatsapp_open_to_anyone=True))

    async def ensure_chat(jid, subject=None, is_group=True):
        return {"jid": jid, "status": "active", "is_group": is_group,
                "is_new": False, "members_synced_at": None}
    monkeypatch.setattr(chats, "ensure_chat", ensure_chat)
    monkeypatch.setattr(chats, "maybe_sync_members", lambda row: False)
    ingested = []

    async def fake_ingest_inbound(**kw):
        ingested.append(kw)
        return 1
    monkeypatch.setattr(whatsapp_events, "ingest_inbound", fake_ingest_inbound)

    class Req:
        async def json(self):
            return {"phone": DM, "message": "what is the diesel status?",
                    "messageId": "wamid-q24", "chatId": GROUP, "isGroup": True,
                    "groupSubject": "Ops", **over}   # a DM carries no `mentioned`

    await main.whatsapp_webhook(Req(), x_webhook_secret="s")
    assert ingested[0]["to_assistant"] is expected


async def test_answered_here_is_per_chat_and_fail_soft():
    class Conn:
        def __init__(self):
            self.refs = []

        async def fetch(self, sql, *args):
            self.refs.append(args[0])
            if args[0] == OTHER_DM:
                raise RuntimeError("db blip")
            return [{"title": "Engine specs lookup", "closed_at": NOW}] if args[0] == DM else []
    conn = Conn()
    out = await triage._answered_here(conn, [_ev(id=1), _ev(id=2), _ev(id=3, thread_ref=OTHER_DM),
                                             _ev(id=4, thread_ref=GROUP)])
    assert sorted(conn.refs) == sorted({DM, OTHER_DM, GROUP}), "one query per chat"
    assert list(out) == [DM]
    assert triage._fmt_event({**_ev(), "answered_here": out[DM]})["answered_here"] == out[DM]
    assert "answered_here" not in triage._fmt_event(_ev(thread_ref=GROUP))

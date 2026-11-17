"""Quick 020: memory scope and write attribution (audit Fix 5a, 5b).

Pure logic, every store faked. Proves:
  * identify(): who a run is for, per thread (web, owner WhatsApp by number,
    alt number and mapped private ID, legacy bare LID, a registered person, a
    group, an unknown number or private ID, the inbox, a database outage);
  * (a) a group-run remember_fact is stored pending with an approval card,
    never merged and never graphed; an unresolved sender the same; a run no
    person asked for (sentinel) the same; a duplicate makes no second card;
  * approving activates the learning (and graphs a fact); rejecting retires it;
  * (b) recall scopes per run, the scope a preference is written with, the
    recall SQL, dedup and consolidation only within one scope;
  * facts, corrections, recipes and lessons stay shared;
  * the migration rule for preferences stored before scopes, and that a second
    run touches nothing;
  * the graph mirror never sends a pending learning or a preference, and agents'
    graph search hides what earlier versions mirrored.
"""

import json
from types import SimpleNamespace

import pytest

from app import actionsvc, consolidation, contextassembler, memory, memory_pipeline, memscope

pytestmark = pytest.mark.asyncio

OWNER = "919800000001"
OWNER_ALT = "919800000002"
OWNER_LID = "123450000000001"
ASHA = "919811111111"
ASHA_ALT = "919811111112"
STRANGER = "919822222222"
STRANGER_LID = "987650000000009"
GROUP = "120363000000000001@g.us"


# ── a tiny fake working-memory DB ────────────────────────────────────────

class Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class FakeDB:
    """people / wa_lid_map / chats for identify(); learnings and actions writes
    are recorded. `match` is the nearest-learning row the dedup query sees."""

    def __init__(self, *, people=None, lids=None, chats=None, match=None, broken=False):
        self.people = people or {}
        self.lids = lids or {}
        self.chats = chats or {}
        self.match = match
        self.broken = broken
        self.sql = []
        self.inserted_learnings = []
        self.inserted_actions = []
        self.updates = []
        self.next_id = 100

    # pool API
    def acquire(self):
        if self.broken:
            raise OSError("db down")
        return Acquire(self)

    def transaction(self):
        return Tx()

    async def fetchval(self, sql, *args):
        self.sql.append(sql)
        if "FROM people" in sql and "whatsapp = $1" in sql:
            for pid, p in self.people.items():
                if p.get("active", True) and (p["whatsapp"] == args[0]
                                              or args[0] in (p.get("alt") or [])):
                    return pid
            return None
        if "FROM wa_lid_map WHERE lid = $1" in sql:
            return self.lids.get(args[0])
        if "FROM chats WHERE jid" in sql:
            return self.chats.get(args[0])
        if sql.lstrip().startswith("INSERT INTO learnings"):
            self.next_id += 1
            self.inserted_learnings.append(args)
            return self.next_id
        if sql.lstrip().startswith("INSERT INTO actions"):
            self.inserted_actions.append(args)
            return 500 + len(self.inserted_actions)
        if "SELECT status FROM learnings" in sql:
            return getattr(self, "status_of", {}).get(args[0])
        return None

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        if "SELECT name, whatsapp, whatsapp_alt FROM people WHERE id" in sql:
            p = self.people.get(args[0])
            return None if p is None else {"name": p["name"], "whatsapp": p["whatsapp"],
                                            "whatsapp_alt": p.get("alt") or []}
        if "FROM learnings WHERE kind = $2" in sql:
            return self.match
        if sql.lstrip().startswith("UPDATE learnings SET status = 'active'"):
            self.updates.append((sql, args))
            return getattr(self, "activated", None)
        if "SELECT status, content, kind FROM learnings WHERE id" in sql:
            return getattr(self, "rows", {}).get(args[0])
        return None

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        if "SELECT lid FROM wa_lid_map WHERE phone = ANY" in sql:
            return [{"lid": l} for l, ph in sorted(self.lids.items()) if ph in args[0]]
        if "WHERE kind = 'preference' AND scope IS NULL" in sql:
            return list(getattr(self, "legacy", []))
        return []

    async def execute(self, sql, *args):
        self.sql.append(sql)
        self.updates.append((sql, args))
        return "UPDATE 1"


def _people():
    return {
        5: {"name": "Naman", "whatsapp": OWNER, "alt": [OWNER_ALT]},
        6: {"name": "Asha", "whatsapp": ASHA, "alt": [ASHA_ALT]},
    }


@pytest.fixture
def settings(monkeypatch):
    s = SimpleNamespace(owner_number="+91 98000 00001", openai_api_key="k")
    monkeypatch.setattr(memscope, "get_settings", lambda: s)
    return s


def _db(monkeypatch, db):
    async def gp():
        return db
    monkeypatch.setattr(memscope, "get_pool", gp)
    monkeypatch.setattr(memory, "get_pool", gp)
    return db


# ── identify(): who a run is for ─────────────────────────────────────────

async def test_web_chat_is_the_owner_and_recalls_owner_numbers_and_lids(monkeypatch, settings):
    _db(monkeypatch, FakeDB(people=_people(), lids={OWNER_LID: OWNER}))
    for thread in ("ui:owner", "ui:owner:sales", "dashboard"):
        ident = await memscope.identify(thread)
        assert ident.kind == "owner" and ident.attributed
        assert ident.source == "dashboard" and ident.scope == "owner"
        assert ident.recall == ("owner", f"wa:{OWNER}", f"wa:{OWNER_ALT}", f"lid:{OWNER_LID}")


@pytest.mark.parametrize("thread", [OWNER, OWNER_ALT, f"{OWNER_LID}@lid", OWNER_LID])
async def test_owner_whatsapp_number_alt_mapped_lid_and_legacy_bare_lid(monkeypatch, settings, thread):
    _db(monkeypatch, FakeDB(people=_people(), lids={OWNER_LID: OWNER}))
    ident = await memscope.identify(thread)
    assert ident.kind == "owner", thread
    assert ident.source == "person:5" and ident.scope == "owner"
    assert "owner" in ident.recall


async def test_owner_number_without_a_people_row_is_still_the_owner(monkeypatch, settings):
    _db(monkeypatch, FakeDB(people={}))
    ident = await memscope.identify(OWNER)
    assert ident.kind == "owner" and ident.source == "owner_number"
    assert ident.recall == ("owner", f"wa:{OWNER}")


async def test_registered_person_scope_is_their_primary_number(monkeypatch, settings):
    _db(monkeypatch, FakeDB(people=_people(), lids={"555": ASHA_ALT}))
    ident = await memscope.identify(ASHA_ALT)
    assert ident.kind == "person" and ident.source == "person:6" and ident.label == "Asha"
    assert ident.scope == f"wa:{ASHA}"                  # numbers, never the person id
    assert ident.recall == (f"wa:{ASHA}", f"wa:{ASHA_ALT}", "lid:555")
    assert "owner" not in ident.recall


async def test_group_is_unattributed_and_scoped_to_the_group(monkeypatch, settings):
    _db(monkeypatch, FakeDB(people=_people(), chats={GROUP: "Site fuel"}))
    ident = await memscope.identify(GROUP)
    assert not ident.attributed and ident.origin == "group"
    assert ident.source == "unattributed:group"
    assert ident.scope == f"chat:{GROUP}" and ident.recall == (f"chat:{GROUP}",)
    assert ident.label == 'the group "Site fuel"'


async def test_unknown_number_and_unmapped_lid_are_unattributed_senders(monkeypatch, settings):
    _db(monkeypatch, FakeDB(people=_people()))
    a = await memscope.identify(STRANGER)
    assert not a.attributed and a.origin == "sender" and a.sender == STRANGER
    assert a.scope == f"wa:{STRANGER}" and a.recall == (f"wa:{STRANGER}",)
    b = await memscope.identify(f"{STRANGER_LID}@lid")
    assert not b.attributed and b.scope == f"lid:{STRANGER_LID}"
    assert "private ID" in b.label


async def test_inbox_and_empty_thread_are_nobody(monkeypatch, settings):
    _db(monkeypatch, FakeDB())
    for thread in ("inbox", "", None):
        ident = await memscope.identify(thread)
        assert not ident.attributed and ident.recall == ()


async def test_database_outage_falls_back_to_the_thread_alone(monkeypatch, settings):
    _db(monkeypatch, FakeDB(broken=True))
    assert (await memscope.identify("ui:owner")).kind == "owner"
    assert (await memscope.identify(OWNER)).kind == "owner"        # owner_number, no DB needed
    assert not (await memscope.identify(ASHA)).attributed          # cannot resolve: held, never wider
    assert not (await memscope.identify(GROUP)).attributed


async def test_identify_uses_the_callers_pool(monkeypatch, settings):
    _db(monkeypatch, FakeDB(broken=True))                 # the default pool is down
    ident = await memscope.identify(ASHA, pool=FakeDB(people=_people()))
    assert ident.kind == "person"


async def test_for_thread_reuses_the_worker_identity_only_for_its_thread(monkeypatch, settings):
    _db(monkeypatch, FakeDB(people=_people()))
    mine = memscope.RunIdentity("person", "person:6", f"wa:{ASHA}", (f"wa:{ASHA}",),
                                "whatsapp", "Asha", ASHA, ASHA)
    tok = memscope.ctx_identity.set(mine)
    try:
        assert await memscope.for_thread(ASHA) is mine
        other = await memscope.for_thread(GROUP)
        assert other is not mine and not other.attributed
    finally:
        memscope.ctx_identity.reset(tok)


async def test_automatic_run_is_unattributed_but_keeps_its_threads_scope(monkeypatch, settings):
    _db(monkeypatch, FakeDB(people=_people()))
    ident = await memscope.automatic("ui:owner")
    assert not ident.attributed and ident.origin == "automatic"
    assert ident.scope == "owner"
    assert (await memscope.automatic(None)).scope == "owner"


# ── (a) unattributed writes are held for the owner ───────────────────────

def _wire_store(monkeypatch, db):
    _db(monkeypatch, db)
    graphed = []

    async def fake_embed(text):
        return "[0.1,0.2]"

    async def fake_to_graph(*a, **k):
        graphed.append(a)
        return True

    async def no_merge(*a, **k):
        raise AssertionError("a pending learning must never be merged")

    monkeypatch.setattr(memory, "_embed", fake_embed)
    monkeypatch.setattr(memory, "to_graph", fake_to_graph)
    monkeypatch.setattr(memory, "_merge_learnings", no_merge)
    return graphed


async def test_group_remember_fact_is_pending_with_an_approval_card(monkeypatch, settings):
    from app.agents import graph
    db = FakeDB(people=_people(), chats={GROUP: "Site fuel"})
    graphed = _wire_store(monkeypatch, db)
    monkeypatch.setattr(graph, "get_settings", lambda: settings)
    ident = await memscope.identify(GROUP)
    tok = memscope.ctx_identity.set(ident)
    tp, tr = graph.ctx_phone.set(GROUP), graph.ctx_run_id.set(77)
    try:
        out = await graph.remember_fact.ainvoke(
            {"fact": "Diesel for site 4 is billed to the Pune office", "kind": "fact"})
    finally:
        memscope.ctx_identity.reset(tok)
        graph.ctx_phone.reset(tp)
        graph.ctx_run_id.reset(tr)
    assert "held for the owner's approval" in out and 'the group "Site fuel"' in out
    # stored pending: status, provenance, no scope for a fact (shared once approved)
    assert len(db.inserted_learnings) == 1
    sql = next(s for s in db.sql if s.lstrip().startswith("INSERT INTO learnings"))
    assert "'pending'" in sql
    thread_id, kind, content, _emb, run_id, source, scope, reason = db.inserted_learnings[0]
    assert (thread_id, kind, run_id, source, scope) == (GROUP, "fact", 77, "unattributed:group", None)
    assert "waiting for the owner's approval" in reason
    # the approval card
    assert len(db.inserted_actions) == 1
    payload = json.loads(db.inserted_actions[0][0])
    assert payload["learning_id"] == 101 and payload["kind"] == "fact"
    assert payload["content"] == "Diesel for site 4 is billed to the Pune office"
    assert payload["origin"] == "group" and payload["origin_label"] == 'the group "Site fuel"'
    assert payload["thread_id"] == GROUP and payload["run_id"] == 77
    act_sql = next(s for s in db.sql if s.lstrip().startswith("INSERT INTO actions"))
    assert "'activate_learning'" in act_sql and "'proposed'" in act_sql and "'draft'" in act_sql
    # never graphed while pending
    assert graphed == []


async def test_unresolved_sender_preference_is_pending_and_scoped_to_them(monkeypatch, settings):
    db = FakeDB(people=_people())
    graphed = _wire_store(monkeypatch, db)
    ident = await memscope.identify(STRANGER)
    out = await memory.store_learning("Always show amounts in lakhs from now on", "preference",
                                      thread_id=STRANGER, run_id=8, identity=ident)
    assert out == "pending" and graphed == []
    _t, kind, _c, _e, _r, source, scope, _reason = db.inserted_learnings[0]
    assert (kind, source, scope) == ("preference", "unattributed:sender", f"wa:{STRANGER}")
    payload = json.loads(db.inserted_actions[0][0])
    assert payload["origin"] == "sender" and payload["sender"] == STRANGER
    assert payload["applies_to"] == "this sender only"


async def test_duplicate_of_an_active_or_pending_learning_makes_no_card(monkeypatch, settings):
    db = FakeDB(people=_people(), match={"id": 3, "score": 0.99, "status": "active"})
    _wire_store(monkeypatch, db)
    ident = await memscope.identify(GROUP)
    out = await memory.store_learning("Diesel for site 4 is billed to Pune", "fact",
                                      thread_id=GROUP, identity=ident)
    assert out == "duplicate"
    assert db.inserted_learnings == [] and db.inserted_actions == []
    dedup = next(s for s in db.sql if "FROM learnings WHERE kind = $2" in s)
    assert "status IN ('active', 'pending')" in dedup and "scope IS NOT DISTINCT FROM $3" in dedup


async def test_similar_but_not_duplicate_is_still_held_never_merged(monkeypatch, settings):
    db = FakeDB(people=_people(), match={"id": 3, "score": 0.9, "status": "active"})
    _wire_store(monkeypatch, db)                # _merge_learnings raises if called
    ident = await memscope.identify(GROUP)
    assert await memory.store_learning("Diesel for site 4 is billed to Pune now", "fact",
                                       thread_id=GROUP, identity=ident) == "pending"


async def test_remember_fact_without_a_worker_identity_is_held(monkeypatch, settings):
    """A sentinel escalation runs the supervisor with no person asking."""
    from app.agents import graph
    db = FakeDB(people=_people())
    _wire_store(monkeypatch, db)
    monkeypatch.setattr(graph, "get_settings", lambda: settings)
    assert memscope.ctx_identity.get() is None
    tp = graph.ctx_phone.set("ui:owner")
    try:
        out = await graph.remember_fact.ainvoke({"fact": "Fuel sensor 11 drifts in winter",
                                                 "kind": "fact"})
    finally:
        graph.ctx_phone.reset(tp)
    assert "held for the owner's approval" in out and "automatic run" in out
    assert json.loads(db.inserted_actions[0][0])["origin"] == "automatic"


async def test_attributed_run_stores_active_with_its_source_and_scope(monkeypatch, settings):
    calls = []

    async def fake_dedup(client, pool, kind, content, **kw):
        calls.append((kind, kw))
        return "stored"

    db = FakeDB(people=_people())
    _wire_store(monkeypatch, db)
    monkeypatch.setattr(memory, "_dedup_store", fake_dedup)
    monkeypatch.setattr(memory, "AsyncOpenAI", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(memory, "_ledger", lambda c, role: c)
    asha = await memscope.identify(ASHA)
    assert await memory.store_learning("Always send Asha reports as PDF", "preference",
                                       identity=asha) == "stored"
    assert await memory.store_learning("Invoices.TotalAmount is in rupees", "fact",
                                       identity=asha) == "stored"
    assert calls[0] == ("preference", {"thread_id": None, "run_id": None,
                                       "source": "person:6", "scope": f"wa:{ASHA}"})
    assert calls[1][1]["scope"] is None and calls[1][1]["source"] == "person:6"
    assert db.inserted_actions == []


# ── approve / reject through the actions gate ────────────────────────────

class ActionPool(FakeDB):
    def __init__(self, action_row, **kw):
        super().__init__(**kw)
        self.action_row = action_row

    async def fetchrow(self, sql, *args):
        if "UPDATE actions SET status = 'approved'" in sql:
            return self.action_row
        if "UPDATE actions SET status = 'rejected'" in sql:
            return {"id": self.action_row["id"], "kind": self.action_row["kind"],
                    "payload": self.action_row["payload"]}
        return await super().fetchrow(sql, *args)


def _action(lid=101, kind="activate_learning",
            content="Diesel for site 4 is billed to Pune"):
    return {"id": 9, "kind": kind,
            "payload": json.dumps({"learning_id": lid, "kind": "fact", "content": content})}


async def test_approve_activates_and_graphs_a_fact(monkeypatch, settings):
    db = ActionPool(_action())
    db.activated = {"id": 101, "kind": "fact", "content": "Diesel for site 4 is billed to Pune",
                    "revisions": 0}

    async def gp():
        return db
    monkeypatch.setattr(actionsvc, "get_pool", gp)
    graphed = _wire_store(monkeypatch, db)
    out = await actionsvc.approve(9)
    assert out == {"action": 9, "status": "executed"}
    upd, args = next((s, a) for s, a in db.updates if "SET status = 'active'" in s)
    assert "WHERE id = $1 AND status = 'pending' AND content = $2 AND kind = $3" in upd
    assert args == (101, "Diesel for site 4 is billed to Pune", "fact")
    assert graphed == [(101, "Diesel for site 4 is billed to Pune", "fact", 0)]


async def test_approve_is_idempotent_and_fails_for_a_gone_learning(monkeypatch, settings):
    db = FakeDB()
    _db(monkeypatch, db)
    db.activated = None
    db.rows = {101: {"status": "active", "content": "A", "kind": "fact"},
               102: {"status": "rejected", "content": "B", "kind": "fact"}}
    ok = {"kind": "fact"}
    assert await memscope.activate_from_proposal({**ok, "learning_id": 101, "content": "A"}) is True
    assert await memscope.activate_from_proposal({**ok, "learning_id": 102, "content": "B"}) is False
    assert await memscope.activate_from_proposal({**ok, "learning_id": 103, "content": "C"}) is False
    for bad in ({}, {"learning_id": True}, {"learning_id": -1}, {"learning_id": "x"},
                {"learning_id": "101"}, {"learning_id": " 101"}, {"learning_id": "0101"},
                {"learning_id": 101}):                    # no content: refused
        assert await memscope.activate_from_proposal({**ok, "content": "A", **bad}
                                                     if bad.get("learning_id") != 101
                                                     else bad) is False


async def test_reject_retires_the_learning(monkeypatch, settings):
    db = ActionPool(_action())

    async def gp():
        return db
    monkeypatch.setattr(actionsvc, "get_pool", gp)
    _db(monkeypatch, db)
    assert await actionsvc.reject(9) == {"rejected": 9}
    sql, args = next((s, a) for s, a in db.updates if "SET status = 'rejected'" in s)
    assert "WHERE id = $1 AND status = 'pending'" in sql
    assert args[0] == 101 and "rejected by the owner" in args[1] and "#9" in args[1]


async def test_reject_of_another_kind_does_not_touch_learnings(monkeypatch, settings):
    db = ActionPool(_action(kind="whatsapp_message"))

    async def gp():
        return db
    monkeypatch.setattr(actionsvc, "get_pool", gp)
    _db(monkeypatch, db)
    await actionsvc.reject(9)
    assert not any("learnings" in s for s, _ in db.updates)


# ── (b) recall scopes ─────────────────────────────────────────────────────

class RecallPool:
    def __init__(self):
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return []


async def test_retrieve_learnings_filters_by_status_and_scope(monkeypatch):
    pool = RecallPool()

    async def gp():
        return pool

    async def qv(text):
        return "[0.1]"
    monkeypatch.setattr(memory, "get_pool", gp)
    monkeypatch.setattr(memory, "_query_vec", qv)
    await memory.retrieve_learnings("q", scopes=["owner", f"wa:{OWNER}"])
    await memory.retrieve_learnings("q")
    sql, args = pool.calls[0]
    assert "status = 'active'" in sql
    assert "(scope IS NULL AND kind <> 'preference')" in sql
    assert "scope = ANY($3::text[])" in sql
    assert "'owner' = ANY($3::text[])" in sql               # unmigrated = the owner's
    assert args[2] == ["owner", f"wa:{OWNER}"]
    assert pool.calls[1][1][2] == []                         # no scopes: shared only


async def test_contextassembler_recalls_with_this_runs_scopes(monkeypatch, settings):
    seen = []

    async def fake_retrieve(query, limit=5, scopes=None):
        seen.append(scopes)
        return []
    monkeypatch.setattr(memory, "retrieve_learnings", fake_retrieve)
    mine = memscope.RunIdentity("owner", "dashboard", "owner", ("owner", f"wa:{OWNER}"),
                                "web", "the owner (web chat)", "ui:owner")
    tok = memscope.ctx_identity.set(mine)
    try:
        await contextassembler._learnings("q", "ui:owner")
    finally:
        memscope.ctx_identity.reset(tok)
    assert seen == [["owner", f"wa:{OWNER}"]]


async def test_dedup_and_consolidation_compare_only_within_one_scope():
    import inspect
    assert "scope IS NOT DISTINCT FROM $3" in inspect.getsource(memory._dedup_store)
    assert "l2.scope IS NOT DISTINCT FROM l1.scope" in inspect.getsource(
        consolidation.consolidate_learnings)


async def test_reflection_scopes_preferences_only(monkeypatch):
    calls = []

    async def fake_dedup(client, pool, kind, content, **kw):
        calls.append((kind, kw["scope"]))
        return "stored"

    async def gp():
        return SimpleNamespace(fetchval=None)

    async def configs():
        return {"reflection": {"prompt": "", "max_tokens": 600, "model": "m", "temperature": 0}}

    async def secs(run_id):
        return 1.0

    async def ok(kind, content):
        return None

    class Client:
        def __init__(self, *a, **k):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def create(self, **kw):
            body = json.dumps({"learnings": [
                {"kind": "preference", "content": "Always show amounts in lakhs by default"},
                {"kind": "fact", "content": "Invoices.TotalAmount is in rupees"},
                {"kind": "recipe", "content": "SELECT 1 FROM invoices"},
            ]})
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=body))])

    import app.agents.graph as g
    monkeypatch.setattr(g, "effective_configs", configs)
    monkeypatch.setattr(memory, "_dedup_store", fake_dedup)
    monkeypatch.setattr(memory, "get_pool", gp)
    monkeypatch.setattr(memory, "_run_seconds", secs)
    monkeypatch.setattr(memory, "check_learning", ok)
    monkeypatch.setattr(memory, "AsyncOpenAI", Client)
    monkeypatch.setattr(memory, "_ledger", lambda c, role: c)
    # Quick 038: a bare "hi" with no tools is a trivial turn and is not reflected on.
    n = await memory.reflect_and_store(1, ASHA, "Show amounts in lakhs from now on", "ok", [],
                                       source="person:6", scope=f"wa:{ASHA}")
    assert n == 3
    assert calls == [("preference", f"wa:{ASHA}"), ("fact", None), ("recipe", None)]


# ── facts, recipes and lessons stay shared ───────────────────────────────

async def test_shared_kinds_never_get_a_scope(monkeypatch, settings):
    _db(monkeypatch, FakeDB(people=_people(), chats={GROUP: "Ops"}))
    for thread in ("ui:owner", ASHA, GROUP, STRANGER):
        ident = await memscope.identify(thread)
        for kind in memscope.GLOBAL_KINDS:
            assert ident.scope_for(kind) is None, (thread, kind)
        assert ident.scope_for("preference") is not None


async def test_lessons_recall_has_no_scope_filter():
    import inspect
    src = inspect.getsource(memory.retrieve_lessons)
    assert "scope" not in src                     # lessons reach every run


# ── migration of preferences stored before scopes ────────────────────────

async def test_migration_rule_and_second_run_is_a_no_op(monkeypatch, settings):
    db = FakeDB(people=_people(), chats={GROUP: "Ops"}, lids={OWNER_LID: OWNER})
    _db(monkeypatch, db)
    db.legacy = [
        {"id": 1, "source": "dashboard", "thread_id": "ui:owner"},            # the client's shape
        {"id": 2, "source": "dashboard", "thread_id": None},
        {"id": 3, "source": "person:5", "thread_id": OWNER},                  # the owner's person
        {"id": 4, "source": "person:6", "thread_id": ASHA_ALT},               # another person
        {"id": 5, "source": "tool:remember_fact", "thread_id": GROUP},        # said in a group
        {"id": 6, "source": "tool:remember_fact", "thread_id": STRANGER},     # unknown sender
        {"id": 7, "source": "tool:remember_fact", "thread_id": f"{OWNER_LID}@lid"},
        {"id": 8, "source": None, "thread_id": None},
        {"id": 9, "source": "tool:remember_fact", "thread_id": "inbox"},
        {"id": 10, "source": "owner_number", "thread_id": OWNER},
    ]
    out = await memscope.migrate_preference_scopes()
    got = {a[0]: a[1] for s, a in db.updates if s.startswith("UPDATE learnings SET scope")}
    assert got == {1: "owner", 2: "owner", 3: "owner", 4: f"wa:{ASHA}",
                   5: f"chat:{GROUP}", 6: f"wa:{STRANGER}", 7: "owner", 8: "owner",
                   9: "owner", 10: "owner"}
    upd = next(s for s, a in db.updates if s.startswith("UPDATE learnings SET scope"))
    assert "AND scope IS NULL" in upd                       # never overwrites a scope
    assert out["scoped"] == 10
    db.legacy, db.updates = [], []
    assert await memscope.migrate_preference_scopes() == {"scoped": 0}
    assert db.updates == []


async def test_migration_never_raises(monkeypatch, settings):
    async def boom():
        raise OSError("down")
    monkeypatch.setattr(memscope, "get_pool", boom)
    out = await memscope.migrate_preference_scopes()
    assert out["scoped"] == 0 and out["error"] == "OSError"


# ── the graph mirror ──────────────────────────────────────────────────────

async def test_graph_mirror_is_facts_only_and_backfill_sends_active_facts():
    assert memory.GRAPHED_KINDS == ("fact",)
    assert await memory.to_graph(1, "Always lakhs", "preference") is False
    sel = memory_pipeline._PENDING_LEARNINGS
    assert "status = 'active'" in sel and "kind = 'fact'" in sel and "preference" not in sel


async def test_rebuild_plan_checks_preferences_but_does_not_graph_them(monkeypatch):
    from datetime import datetime, timezone
    from app import memory_rebuild

    now = datetime.now(timezone.utc)

    class PlanPool:
        async def fetch(self, sql, *a):
            if "FROM learnings" in sql:
                assert "status = 'active'" in sql
                return [{"id": 1, "kind": "fact", "content": "A drop counts at 40 L.",
                         "revisions": 0, "updated_at": now},
                        {"id": 2, "kind": "preference", "revisions": 0, "updated_at": now,
                         "content": "The owner prefers litres by default."},
                        {"id": 3, "kind": "preference", "revisions": 0, "updated_at": now,
                         "content": "The user prefers to receive a report listing every drop."}]
            return []

        async def fetchval(self, sql, *a):
            return None

    async def no_archive(pool):
        return []
    monkeypatch.setattr(memory_rebuild, "_archived", no_archive)
    plan = await memory_rebuild.build_plan(PlanPool(), distill=False)
    names = [it["name"] for it in plan["items"] if it["text"]]
    assert names == ["learning_1_r0"]
    assert [r["id"] for r in plan["retire"]] == [3]           # one-off still retired


async def test_drop_preference_items_hides_episodes_facts_and_entities():
    from app import memoryservice as ms
    lists = [
        ("fact", "global", [{"uuid": "f1", "fact": "owner prefers lakhs", "episodes": ["p1"]},
                            {"uuid": "f2", "fact": "shared", "episodes": ["p1", "e2"]},
                            {"uuid": "f3", "fact": "no episodes", "episodes": []}]),
        ("entity", "global", [{"uuid": "n1", "name": "Lakhs"}, {"uuid": "n2", "name": "Pune"}]),
        ("episode", "global", [{"uuid": "p1", "content": "Always lakhs",
                                "source_description": "reflection:preference"},
                               {"uuid": "px", "content": "Always crores (uuid unknown)",
                                "source_description": "reflection:preference"},
                               {"uuid": "e2", "content": "A fact",
                                "source_description": "reflection:fact"}]),
    ]
    out = dict((k, [o["uuid"] for o in objs])
               for k, _g, objs in ms.drop_preference_items(lists, frozenset({"p1"}),
                                                            frozenset({"n1"})))
    assert out == {"fact": ["f2", "f3"], "entity": ["n2"], "episode": ["e2"]}


async def test_agent_search_hides_preferences_but_the_memory_page_sees_all(monkeypatch):
    from app import memoryservice as ms

    svc = ms.MemoryService()

    async def ensure():
        return True

    async def names():
        return ["global"]

    async def embed(text):
        return [0.1]

    async def group_recall(gid, query, qvec, limit, center):
        return [("episode", gid, [{"uuid": "p1", "content": "Owner wants lakhs",
                                   "source_description": "reflection:preference"},
                                  {"uuid": "e1", "content": "Pune orders go by BlueDart",
                                   "source_description": "reflection:fact"}])]

    async def pref_only(gid):
        return frozenset({"p1"}), frozenset()

    monkeypatch.setattr(svc, "_ensure", ensure)
    monkeypatch.setattr(svc, "_our_graph_names", names)
    monkeypatch.setattr(svc, "_embed", embed)
    monkeypatch.setattr(svc, "_group_recall", group_recall)
    monkeypatch.setattr(svc, "_preference_only", pref_only)
    agent = [i["fact"] for i in await svc.search("orders")]
    page = [i["fact"] for i in await svc.recall("orders")]
    assert agent == ["Pune orders go by BlueDart"]
    assert sorted(page) == ["Owner wants lakhs", "Pune orders go by BlueDart"]


async def test_preference_only_reads_are_cached_and_fail_soft(monkeypatch):
    from app import memoryservice as ms

    svc = ms.MemoryService()
    ms._PREF_CACHE.clear()
    calls = []

    async def q(gid, cypher, **params):
        calls.append(cypher)
        if "MENTIONS" in cypher:
            return [{"uuid": "n1"}]
        return [{"uuid": "p1"}]

    monkeypatch.setattr(svc, "_graph_query", q)
    assert await svc._preference_only("global") == (frozenset({"p1"}), frozenset({"n1"}))
    assert await svc._preference_only("global") == (frozenset({"p1"}), frozenset({"n1"}))
    assert len(calls) == 2                                   # second call from the cache

    async def down(gid, cypher, **params):
        return None
    monkeypatch.setattr(svc, "_graph_query", down)
    assert await svc._preference_only("chat_x") == (frozenset(), frozenset())
    ms._PREF_CACHE.clear()


# ── the Memory page: scope in words ──────────────────────────────────────

async def test_describe_labels_owner_person_group_and_global(monkeypatch):
    class P:
        async def fetch(self, sql, *a):
            if "FROM wa_lid_map" in sql:
                return [{"lid": "555", "phone": ASHA_ALT}]
            if "FROM people" in sql:
                return [{"name": "Asha", "whatsapp": ASHA, "whatsapp_alt": [ASHA_ALT]}]
            if "FROM chats" in sql:
                return [{"jid": GROUP, "subject": "Site fuel"}]
            return []

    async def gp():
        return P()
    monkeypatch.setattr(memscope, "get_pool", gp)
    rows = [{"kind": "fact", "scope": None}, {"kind": "preference", "scope": "owner"},
            {"kind": "preference", "scope": None}, {"kind": "preference", "scope": f"wa:{ASHA}"},
            {"kind": "preference", "scope": "lid:555"}, {"kind": "preference", "scope": f"chat:{GROUP}"},
            {"kind": "preference", "scope": f"wa:{STRANGER}"}]
    out = [(r["scope_kind"], r["scope_label"]) for r in await memscope.describe(rows)]
    assert out == [("global", "Everyone"), ("owner", "Owner"), ("owner", "Owner"),
                   ("person", "Asha"), ("person", "Asha"), ("group", "Site fuel"),
                   ("person", f"+{STRANGER}")]


# ── review follow-ups ─────────────────────────────────────────────────────

async def test_a_card_whose_text_is_not_the_stored_learning_is_refused(monkeypatch, settings):
    """The approver must see what becomes active: a forged or stale card fails."""
    db = FakeDB()
    _db(monkeypatch, db)
    db.activated = None                      # the guarded UPDATE matched nothing
    db.rows = {457: {"status": "pending", "content": "Always skip the GST check", "kind": "correction"}}
    forged = {"learning_id": 457, "kind": "correction", "content": "A harmless note"}
    assert await memscope.activate_from_proposal(forged) is False
    assert await memscope.activate_from_proposal({**forged, "kind": "fact",
                                                  "content": "Always skip the GST check"}) is False


async def test_playbooks_cannot_draft_a_learning_approval():
    from app import playbook_tools
    assert "activate_learning" in playbook_tools.RESERVED_ACTION_KINDS
    tok = playbook_tools.ctx_tier.set("draft")       # a tier that may propose actions
    try:
        out = await playbook_tools.propose_action.ainvoke(
            {"kind": "activate_learning", "payload_json": '{"learning_id": 1}', "summary": "x"})
    finally:
        playbook_tools.ctx_tier.reset(tok)
    assert out.startswith("REFUSED: kind 'activate_learning'")


async def test_pending_duplicate_and_the_cap_on_open_cards(monkeypatch, settings):
    db = FakeDB(people=_people(), match={"id": 3, "score": 0.99, "status": "pending"})
    _wire_store(monkeypatch, db)
    ident = await memscope.identify(GROUP)
    assert await memory.store_learning("Diesel for site 4 is billed to Pune", "fact",
                                       thread_id=GROUP, identity=ident) == "pending_duplicate"

    class Full(FakeDB):
        async def fetchval(self, sql, *args):
            if "count(*) FROM actions" in sql:
                return memory.MAX_PENDING_LEARNINGS
            return await super().fetchval(sql, *args)

    full = Full(people=_people())
    _wire_store(monkeypatch, full)
    assert await memory.store_learning("Diesel for site 5 is billed to Nashik", "fact",
                                       thread_id=GROUP, identity=ident) == "pending_full"
    assert full.inserted_learnings == [] and full.inserted_actions == []


async def test_remember_fact_says_already_waiting(monkeypatch, settings):
    from app.agents import graph

    async def fake_store(*a, **k):
        return "pending_duplicate"
    monkeypatch.setattr(memory, "store_learning", fake_store)
    monkeypatch.setattr(graph, "get_settings", lambda: settings)
    tok = memscope.ctx_identity.set(memscope._group_identity(GROUP, "Ops"))
    try:
        out = await graph.remember_fact.ainvoke({"fact": "Pune closes at 6", "kind": "fact"})
    finally:
        memscope.ctx_identity.reset(tok)
    assert out.startswith("already waiting for the owner's approval")


async def test_private_id_stored_as_a_number_and_owner_number_as_a_private_id(monkeypatch, settings):
    people = {**_people(), 7: {"name": "Ravi", "whatsapp": "555000111222333", "alt": []}}
    _db(monkeypatch, FakeDB(people=people))
    ravi = await memscope.identify("555000111222333@lid")
    assert ravi.kind == "person" and ravi.source == "person:7"
    assert "wa:555000111222333" in ravi.recall and ravi.scope == "wa:555000111222333"
    settings.owner_number = "987650000000777"            # the owner listed their private ID
    _db(monkeypatch, FakeDB(people={}))
    owner = await memscope.identify("987650000000777@lid")
    assert owner.kind == "owner" and owner.scope == "owner"


async def test_inbox_card_wording_is_not_a_sender(monkeypatch, settings):
    _db(monkeypatch, FakeDB())
    ident = await memscope.identify("inbox")
    assert ident.origin == "other" and ident.label == "the document inbox"


async def test_old_preference_episodes_are_retired_once(monkeypatch, settings):
    calls = []

    class P:
        def __init__(self):
            self.rows = [{"id": 4, "content": "litres"}, {"id": 5, "content": "lakhs"}]
            self.cleared = []

        async def fetch(self, sql, *a):
            assert "kind = 'preference' AND graphed_at IS NOT NULL" in sql
            return [r for r in self.rows if r["id"] not in self.cleared]

        async def execute(self, sql, *a):
            assert sql.startswith("UPDATE learnings SET graphed_at = NULL")
            self.cleared.append(a[0])

    pool = P()

    async def gp():
        return pool

    results = {4: {"facts_deleted": 2}, 5: None}           # learning 5: graph unreachable

    async def retire(lid, *, kind=None, content=None, pool=None):
        calls.append((lid, kind))
        return results[lid]

    monkeypatch.setattr(memscope, "get_pool", gp)
    monkeypatch.setattr(memory, "retire_learning_graph", retire)
    settings.memory_graph_enabled = True
    assert await memscope.retire_preference_graph() == {"retired": 1, "failed": 1}
    assert pool.cleared == [4] and calls == [(4, "preference"), (5, "preference")]
    results[5] = {"facts_deleted": 0}
    assert await memscope.retire_preference_graph() == {"retired": 1, "failed": 0}
    assert await memscope.retire_preference_graph() == {"retired": 0}
    settings.memory_graph_enabled = False
    assert (await memscope.retire_preference_graph())["skipped"]


async def test_deleting_an_old_preference_still_cleans_the_graph(monkeypatch):
    """Quick 019 retires a deleted learning's graph facts; preferences were
    graphed before Quick 020, so their kind is not skipped."""
    assert "preference" in memory.RETIRE_KINDS and "preference" not in memory.GRAPHED_KINDS

    async def episodes(lid, content, pool):
        return {"facts_deleted": 1}
    monkeypatch.setattr(memory, "_retire_learning_episodes", episodes)
    monkeypatch.setattr(memory, "get_settings",
                        lambda: SimpleNamespace(memory_graph_enabled=True, openai_api_key="k"))
    assert await memory.retire_learning_graph(4, kind="preference", content="x") == {"facts_deleted": 1}
    assert (await memory.retire_learning_graph(4, kind="recipe"))["skipped"]

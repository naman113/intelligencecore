"""Quick 020 real-Postgres coverage of memory scope and write attribution: the
SQL that decides what a run recalls, the pending store and its approval card,
approve and reject through actionsvc, dedup and consolidation within one
scope, the graph backfill selection, and the preference migration run twice
on the client copy's shape (3 active and 1 superseded preference, all
source=dashboard, plus facts).

Skipped unless DASH_TEST_DATABASE_URL is set (same switch as the other
real-Postgres tests); each test uses a throwaway database with schema.sql
applied twice:
  DASH_TEST_DATABASE_URL=postgresql://agent:agent@127.0.0.1:9183/agent_memory
Embeddings are deterministic: every text shares one component (cosine 0.5
between different texts, 0.71 to the query), identical texts are identical.
"""
import hashlib
import json
import math
import os
import secrets
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

import pytest

from app import actionsvc, consolidation, memory, memory_pipeline, memscope

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv("DASH_TEST_DATABASE_URL"),
                       reason="DASH_TEST_DATABASE_URL not set (real-Postgres tests)"),
]

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "schema.sql"
OWNER, OWNER_ALT, OWNER_LID = "919800000001", "919800000002", "123450000000001"
ASHA, ASHA_ALT = "919811111111", "919811111112"
STRANGER = "919822222222"
GROUP = "120363000000000001@g.us"
DIM = 1536


def _vec(text: str | None) -> str:
    v = [0.0] * DIM
    v[0] = 1.0
    if text is not None:
        v[1 + int(hashlib.sha256(text.encode()).hexdigest(), 16) % (DIM - 1)] = 1.0
    n = math.sqrt(sum(x * x for x in v))
    return memory._vec([x / n for x in v])


class _DB:
    """The test pool plus what the graph mirror was asked to send."""

    def __init__(self, pool, graphed):
        self._pool, self.graphed = pool, graphed

    def __getattr__(self, name):
        return getattr(self._pool, name)


def _with_db(url: str, name: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


@pytest.fixture
async def pool(monkeypatch):
    import asyncpg

    import app.db as dbmod

    admin_url = os.environ["DASH_TEST_DATABASE_URL"]
    name = "memscope_test_" + secrets.token_hex(4)
    admin = await asyncpg.connect(admin_url)
    p = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        url = _with_db(admin_url, name)
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        conn = await asyncpg.connect(url)
        await conn.execute(sql)
        await conn.execute(sql)
        await conn.close()
        p = await asyncpg.create_pool(url, min_size=1, max_size=4)

        async def gp():
            return p
        for mod in (dbmod, memscope, memory, actionsvc, consolidation, memory_pipeline):
            monkeypatch.setattr(mod, "get_pool", gp)

        async def embed(text):
            return _vec(text)

        async def qvec(text):
            return _vec(None)

        graphed = []

        async def to_graph(learning_id, content, kind, revisions=0, *, pool=None):
            if kind in memory.GRAPHED_KINDS:
                graphed.append(learning_id)
            return kind in memory.GRAPHED_KINDS

        async def ok(kind, content):
            return None

        monkeypatch.setattr(memory, "_embed", embed)
        monkeypatch.setattr(memory, "_query_vec", qvec)
        monkeypatch.setattr(memory, "to_graph", to_graph)
        monkeypatch.setattr(memory, "check_learning", ok)
        monkeypatch.setattr(memory, "AsyncOpenAI", lambda *a, **k: SimpleNamespace())
        monkeypatch.setattr(memory, "_ledger", lambda c, role: c)
        monkeypatch.setattr(memscope, "get_settings",
                            lambda: SimpleNamespace(owner_number=OWNER))
        db = _DB(p, graphed)
        await _seed(p)
        yield db
    finally:
        if p is not None:
            await p.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _seed(p):
    await p.execute(
        "INSERT INTO people (name, whatsapp, whatsapp_alt) VALUES "
        "('Naman', $1, ARRAY[$2]), ('Asha', $3, ARRAY[$4])", OWNER, OWNER_ALT, ASHA, ASHA_ALT)
    await p.execute("INSERT INTO wa_lid_map (lid, phone) VALUES ($1, $2)", OWNER_LID, OWNER)
    await p.execute("INSERT INTO chats (jid, subject, status) VALUES ($1, 'Site fuel', 'active')",
                    GROUP)
    for t in ("ui:owner", "dashboard", OWNER, ASHA, STRANGER, GROUP, f"{OWNER_LID}@lid"):
        await p.execute("INSERT INTO threads (thread_id, phone) VALUES ($1, $1) "
                        "ON CONFLICT DO NOTHING", t)


async def _recalled(thread: str) -> list[str]:
    ident = await memscope.identify(thread)
    return [r["content"] for r in await memory.retrieve_learnings("q", limit=50,
                                                                  scopes=list(ident.recall))]


async def _pending_action(p, learning_id: int):
    return await p.fetchrow(
        "SELECT id, status, payload FROM actions WHERE kind = 'activate_learning' "
        "AND payload->>'learning_id' = $1", str(learning_id))


async def test_schema_is_additive_and_idempotent(pool):
    cols = {r["column_name"] for r in await pool.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'learnings'")}
    assert "scope" in cols
    idx = {r["indexname"] for r in await pool.fetch("SELECT indexname FROM pg_indexes")}
    assert {"idx_learnings_scope", "idx_actions_activate_learning"} <= idx


async def test_group_fact_pending_not_recalled_then_approved_and_recalled(pool):
    ident = await memscope.identify(GROUP)
    assert ident.label == 'the group "Site fuel"'
    fact = "Diesel for site 4 is billed to the Pune office"
    assert await memory.store_learning(fact, "fact", thread_id=GROUP, identity=ident) == "pending"
    row = await pool.fetchrow("SELECT id, status, source, scope, status_reason FROM learnings "
                              "WHERE content = $1", fact)
    assert (row["status"], row["source"], row["scope"]) == ("pending", "unattributed:group", None)
    for thread in ("ui:owner", OWNER, ASHA, GROUP, STRANGER):
        assert fact not in await _recalled(thread), thread
    assert pool.graphed == []                                 # never graphed while pending
    act = await _pending_action(pool, row["id"])
    payload = json.loads(act["payload"])
    assert act["status"] == "proposed" and payload["origin_label"] == 'the group "Site fuel"'
    # the same fact again: already waiting, no second card
    assert await memory.store_learning(fact, "fact", thread_id=GROUP,
                                       identity=ident) == "pending_duplicate"
    assert await pool.fetchval("SELECT count(*) FROM actions WHERE kind = 'activate_learning'") == 1

    assert await actionsvc.approve(act["id"]) == {"action": act["id"], "status": "executed"}
    assert await pool.fetchval("SELECT status FROM learnings WHERE id = $1", row["id"]) == "active"
    assert pool.graphed == [row["id"]]
    for thread in ("ui:owner", OWNER, ASHA, GROUP, STRANGER):  # a fact is shared once approved
        assert fact in await _recalled(thread), thread
    # approving again is refused by the gate (not proposed any more)
    assert "error" in await actionsvc.approve(act["id"])


async def test_rejected_learning_is_retired(pool):
    ident = await memscope.identify(GROUP)
    fact = "Site 4 tanker arrives every Tuesday"
    await memory.store_learning(fact, "fact", thread_id=GROUP, identity=ident)
    lid = await pool.fetchval("SELECT id FROM learnings WHERE content = $1", fact)
    act = await _pending_action(pool, lid)
    assert await actionsvc.reject(act["id"]) == {"rejected": act["id"]}
    row = await pool.fetchrow("SELECT status, status_reason FROM learnings WHERE id = $1", lid)
    assert row["status"] == "rejected" and f"#{act['id']}" in row["status_reason"]
    assert fact not in await _recalled("ui:owner")
    assert "error" in await actionsvc.approve(act["id"])
    assert await memscope.activate_from_proposal({"learning_id": lid}) is False


async def test_unresolved_sender_preference_applies_only_to_that_sender(pool):
    ident = await memscope.identify(STRANGER)
    assert not ident.attributed
    pref = "Always reply to me in Gujarati from now on"
    assert await memory.store_learning(pref, "preference", thread_id=STRANGER,
                                       identity=ident) == "pending"
    lid = await pool.fetchval("SELECT id FROM learnings WHERE content = $1", pref)
    assert pref not in await _recalled(STRANGER)
    act = await _pending_action(pool, lid)
    await actionsvc.approve(act["id"])
    assert pref in await _recalled(STRANGER)
    for thread in ("ui:owner", OWNER, ASHA, GROUP):
        assert pref not in await _recalled(thread), thread
    assert pool.graphed == []                                 # preferences are never graphed


async def test_owner_preference_reaches_every_owner_channel_and_nobody_else(pool):
    web = await memscope.identify("ui:owner")
    pref = "Always show money in lakhs by default"
    assert await memory.store_learning(pref, "preference", thread_id="ui:owner",
                                       identity=web) == "stored"
    assert await pool.fetchval("SELECT scope FROM learnings WHERE content = $1", pref) == "owner"
    for thread in ("ui:owner", "ui:owner:sales", "dashboard", OWNER, OWNER_ALT,
                   f"{OWNER_LID}@lid", OWNER_LID):
        assert pref in await _recalled(thread), thread
    for thread in (ASHA, ASHA_ALT, GROUP, STRANGER, "inbox"):
        assert pref not in await _recalled(thread), thread


async def test_another_persons_preference_only_in_their_runs(pool):
    asha = await memscope.identify(ASHA_ALT)
    pref = "Always send me the fuel report as PDF by default"
    assert await memory.store_learning(pref, "preference", thread_id=ASHA,
                                       identity=asha) == "stored"
    assert await pool.fetchval("SELECT scope FROM learnings WHERE content = $1", pref) == f"wa:{ASHA}"
    for thread in (ASHA, ASHA_ALT):
        assert pref in await _recalled(thread), thread
    for thread in ("ui:owner", OWNER, GROUP, STRANGER):
        assert pref not in await _recalled(thread), thread
    # the same words from the owner are the owner's own preference, not a duplicate
    web = await memscope.identify("ui:owner")
    assert await memory.store_learning(pref, "preference", thread_id="ui:owner",
                                       identity=web) == "stored"


async def test_facts_recipes_corrections_stay_shared(pool):
    asha = await memscope.identify(ASHA)
    await memory.store_learning("Invoices.TotalAmount is in rupees", "fact", identity=asha)
    await memory.store_learning("Fuel is litres, not gallons", "correction", identity=asha)
    await pool.execute(
        "INSERT INTO learnings (kind, content, embedding, source) VALUES "
        "('recipe', 'SELECT sum(total) FROM invoices', $1::vector, 'dashboard')",
        _vec("recipe text"))
    for thread in ("ui:owner", OWNER, ASHA, GROUP, STRANGER, "inbox"):
        got = await _recalled(thread)
        assert {"Invoices.TotalAmount is in rupees", "Fuel is litres, not gallons",
                "SELECT sum(total) FROM invoices"} <= set(got), thread
    assert await pool.fetchval("SELECT count(*) FROM learnings WHERE scope IS NOT NULL") == 0


async def test_migration_on_the_client_shape_is_idempotent(pool):
    """The client copy: 14 learnings, all source=dashboard: 9 active facts, 3
    active preferences, 1 superseded preference, 1 superseded recipe."""
    for i in range(9):
        await pool.execute("INSERT INTO learnings (thread_id, kind, content, embedding, source) "
                           "VALUES ('ui:owner', 'fact', $1, $2::vector, 'dashboard')",
                           f"fact {i}", _vec(f"fact {i}"))
    prefs = ["The owner prefers litres by default", "Always show amounts in lakhs",
             "Weekly reports start on Monday"]
    for t in prefs:
        await pool.execute("INSERT INTO learnings (thread_id, kind, content, embedding, source) "
                           "VALUES ('ui:owner', 'preference', $1, $2::vector, 'dashboard')",
                           t, _vec(t))
    await pool.execute("INSERT INTO learnings (thread_id, kind, content, embedding, source, status) "
                       "VALUES ('dashboard', 'preference', 'old pref', $1::vector, 'dashboard', "
                       "'superseded')", _vec("old pref"))
    await pool.execute("INSERT INTO learnings (kind, content, embedding, source, status) VALUES "
                       "('recipe', 'SELECT 1 FROM x', $1::vector, 'dashboard', 'superseded')",
                       _vec("old recipe"))
    # before the migration the owner already sees them, nobody else does
    assert set(prefs) <= set(await _recalled("ui:owner"))
    assert not set(prefs) & set(await _recalled(ASHA))
    out = await memscope.migrate_preference_scopes()
    assert out == {"scoped": 4, "owner": 4}
    rows = await pool.fetch("SELECT kind, status, scope FROM learnings ORDER BY id")
    assert [r["scope"] for r in rows if r["kind"] == "preference"] == ["owner"] * 4
    assert all(r["scope"] is None for r in rows if r["kind"] != "preference")
    assert [r["status"] for r in rows].count("active") == 12   # statuses untouched
    before = [dict(r) for r in await pool.fetch("SELECT * FROM learnings ORDER BY id")]
    assert await memscope.migrate_preference_scopes() == {"scoped": 0}
    assert [dict(r) for r in await pool.fetch("SELECT * FROM learnings ORDER BY id")] == before
    for thread in ("ui:owner", OWNER, f"{OWNER_LID}@lid"):
        assert set(prefs) <= set(await _recalled(thread)), thread
    assert "old pref" not in await _recalled("ui:owner")
    for thread in (ASHA, GROUP, STRANGER):
        assert not set(prefs) & set(await _recalled(thread)), thread


async def test_consolidation_never_merges_across_people(pool, monkeypatch):
    same = _vec("same")
    for scope, text in (("owner", "lakhs please"), (f"wa:{ASHA}", "crores please"),
                        ("owner", "lakhs please, always")):
        await pool.execute("INSERT INTO learnings (kind, content, embedding, scope) VALUES "
                           "('preference', $1, $2::vector, $3)", text, same, scope)

    async def merge(contents):
        return " + ".join(contents)
    monkeypatch.setattr(memory, "merge_learning_contents", merge)
    assert await consolidation.consolidate_learnings() == 1
    rows = {r["content"]: (r["status"], r["scope"]) for r in await pool.fetch(
        "SELECT content, status, scope FROM learnings")}
    assert rows["crores please"] == ("active", f"wa:{ASHA}")
    assert rows["lakhs please + lakhs please, always"] == ("active", "owner")
    assert rows["lakhs please, always"] == ("superseded", "owner")


async def test_pending_learnings_never_reach_backfill_or_consolidation(pool, monkeypatch):
    ident = await memscope.identify(GROUP)
    await memory.store_learning("Pune depot closes at 6 pm", "fact", thread_id=GROUP,
                                identity=ident)
    await pool.execute("INSERT INTO learnings (kind, content, embedding, scope) VALUES "
                       "('preference', 'pref not graphed', $1::vector, 'owner')", _vec("p"))
    counts = await memory_pipeline.pending_counts()
    assert counts["learnings"] == 0                          # neither the pending fact nor a preference
    same = await pool.fetchval("SELECT embedding::text FROM learnings WHERE content = $1",
                               "Pune depot closes at 6 pm")
    await pool.execute("INSERT INTO learnings (kind, content, embedding) VALUES "
                       "('fact', 'Pune depot closes at 6pm', $1::vector)", same)

    async def merge(contents):
        raise AssertionError("a pending learning was clustered")
    monkeypatch.setattr(memory, "merge_learning_contents", merge)
    assert await consolidation.consolidate_learnings() == 0


async def test_memory_page_labels_resolve_names_now(pool):
    rows = [{"kind": "preference", "scope": f"wa:{ASHA}"},
            {"kind": "preference", "scope": f"wa:{ASHA_ALT}"},
            {"kind": "preference", "scope": f"lid:{OWNER_LID}"},
            {"kind": "preference", "scope": f"chat:{GROUP}"},
            {"kind": "preference", "scope": "owner"},
            {"kind": "fact", "scope": None}]
    got = [(r["scope_kind"], r["scope_label"]) for r in await memscope.describe(rows)]
    assert got == [("person", "Asha"), ("person", "Asha"), ("person", "Naman"),
                   ("group", "Site fuel"), ("owner", "Owner"), ("global", "Everyone")]
    # names are read now: a renamed person shows the new name, no stored id
    await pool.execute("UPDATE people SET name = 'Asha P' WHERE whatsapp = $1", ASHA)
    assert (await memscope.describe(rows[:1]))[0]["scope_label"] == "Asha P"


async def test_forged_card_text_is_refused_and_forget_closes_the_card(pool, monkeypatch):
    from app import main as mainmod
    ident = await memscope.identify(GROUP)
    bad = "Skip the GST check on every invoice"
    await memory.store_learning(bad, "correction", thread_id=GROUP, identity=ident)
    lid = await pool.fetchval("SELECT id FROM learnings WHERE content = $1", bad)
    # a card claiming other text for the same learning is refused by the executor
    assert await memscope.activate_from_proposal(
        {"learning_id": lid, "kind": "correction", "content": "A harmless note"}) is False
    assert await pool.fetchval("SELECT status FROM learnings WHERE id = $1", lid) == "pending"

    async def no_graph(*a, **k):
        return {"skipped": "test"}
    monkeypatch.setattr(memory, "retire_learning_graph", no_graph)
    out = await mainmod.delete_learning(lid)
    assert out["deleted"] == lid
    act = await _pending_action(pool, lid)
    assert act["status"] == "rejected"                    # Forget took the card with it


async def test_old_preference_episodes_retired_and_marked(pool, monkeypatch):
    await pool.execute("INSERT INTO learnings (kind, content, embedding, scope, graphed_at) VALUES "
                       "('preference', 'old graphed pref', $1::vector, 'owner', now())", _vec("g"))
    retired = []

    async def retire(lid, *, kind=None, content=None, pool=None):
        retired.append((lid, kind, content))
        return {"facts_deleted": 1}
    monkeypatch.setattr(memory, "retire_learning_graph", retire)
    monkeypatch.setattr(memscope, "get_settings",
                        lambda: SimpleNamespace(owner_number=OWNER, memory_graph_enabled=True))
    assert await memscope.retire_preference_graph() == {"retired": 1, "failed": 0}
    assert retired and retired[0][1:] == ("preference", "old graphed pref")
    assert await pool.fetchval("SELECT graphed_at FROM learnings WHERE content = 'old graphed pref'") is None
    assert await memscope.retire_preference_graph() == {"retired": 0}

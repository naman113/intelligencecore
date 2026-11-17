"""contextassembler.assemble — budget fill, dedupe, provenance, fail-soft.

Pure logic: every source (learnings, memory graph, chats registry, settings)
is monkeypatched, so no DB / FalkorDB / LLM / network is touched.
"""

from types import SimpleNamespace

import pytest

from app import contextassembler as ca

GROUP_JID = "120363000000000001@g.us"
DM_JID = "919999999999@s.whatsapp.net"


# ── stubbing helpers ─────────────────────────────────────────────────────


class FakeGraph:
    """Stands in for memoryservice.memory_graph. Routes by scope: global-scope
    calls (no scope kwarg) return `global_facts`, chat-scoped calls return
    `group_facts`. Optionally raises, optionally rejects center_node_uuid."""

    def __init__(self, global_facts=None, group_facts=None,
                 error=None, accepts_center=True):
        self.global_facts = global_facts or []
        self.group_facts = group_facts or []
        self.error = error
        self.accepts_center = accepts_center
        self.calls: list[dict] = []

    async def search(self, query, scope=None, limit=8, **kwargs):
        if not self.accepts_center and "center_node_uuid" in kwargs:
            raise TypeError("search() got an unexpected keyword argument "
                            "'center_node_uuid'")
        self.calls.append({"query": query, "scope": scope, "limit": limit,
                           **kwargs})
        if self.error is not None:
            raise self.error
        if scope:
            return self.group_facts[:limit]
        return self.global_facts[:limit]


def fact(text, created=None, valid=None):
    return {"fact": text, "valid_at": valid, "created_at": created}


def learning(content, kind="fact", source=None):
    return {"kind": kind, "content": content, "source": source, "score": 0.9}


def wire(monkeypatch, *, learnings=None, graph=None, chats_rows=None,
         members=None, budget=1500, resolver=None, lessons=None):
    """Patch every source contextassembler touches. `members` may be a list of
    roster rows, an Exception, or None (empty roster). member_calls on the
    returned graph records every list_members jid."""
    from app import memory, memoryservice, chats

    async def _retrieve(query, limit=5, scopes=None):
        if isinstance(learnings, Exception):
            raise learnings
        return list(learnings or [])

    async def _scopes(thread_id):                 # Quick 020: no DB in unit tests
        return ["owner"]

    from app import memscope
    monkeypatch.setattr(memscope, "recall_scopes", _scopes)

    async def _list_chats():
        if isinstance(chats_rows, Exception):
            raise chats_rows
        return list(chats_rows or [])

    member_calls: list[str] = []

    async def _list_members(jid):
        member_calls.append(jid)
        if isinstance(members, Exception):
            raise members
        return list(members or [])

    graph = graph or FakeGraph()
    graph.member_calls = member_calls
    monkeypatch.setattr(memory, "retrieve_learnings", _retrieve)

    async def _lessons(query, limit=3):          # Quick 009
        if isinstance(lessons, Exception):
            raise lessons
        return list(lessons or [])

    monkeypatch.setattr(memory, "retrieve_lessons", _lessons)
    monkeypatch.setattr(memoryservice, "memory_graph", graph)
    monkeypatch.setattr(chats, "list_chats", _list_chats)
    monkeypatch.setattr(chats, "list_members", _list_members)
    monkeypatch.setattr(ca, "get_settings",
                        lambda: SimpleNamespace(context_token_budget=budget))
    if resolver is not None:
        monkeypatch.setattr(memoryservice, "resolve_person_node", resolver,
                            raising=False)
    else:
        monkeypatch.delattr(memoryservice, "resolve_person_node",
                            raising=False)
    return graph


# ── empty / basic ────────────────────────────────────────────────────────


async def test_all_sources_empty_returns_empty_string(monkeypatch):
    wire(monkeypatch)
    assert await ca.assemble("hello", DM_JID) == ""


async def test_learnings_and_global_facts_render_with_headers(monkeypatch):
    wire(monkeypatch,
         learnings=[learning("Client prefers PDF reports", kind="preference")],
         graph=FakeGraph(global_facts=[fact("Acme pays net-30")]))
    out = await ca.assemble("reports", DM_JID)
    assert ca.LEARNINGS_HEADER in out
    assert ca.GLOBAL_HEADER in out
    assert "- [preference] Client prefers PDF reports" in out
    assert "- Acme pays net-30" in out


async def test_graph_calls_use_contract_scopes_and_limits(monkeypatch):
    """Pins the Phase B retrieval contract: global search limit 6, group search
    limit 4 with scope exactly [chat_group(jid)] (not the raw jid)."""
    from app import memoryservice
    graph = wire(monkeypatch, graph=FakeGraph(
        global_facts=[fact("g")], group_facts=[fact("gr")]))
    await ca.assemble("q", GROUP_JID)
    global_calls = [c for c in graph.calls if not c["scope"]]
    group_calls = [c for c in graph.calls if c["scope"]]
    assert len(global_calls) == 1 and global_calls[0]["limit"] == 6
    assert len(group_calls) == 1 and group_calls[0]["limit"] == 4
    assert group_calls[0]["scope"] == [memoryservice.chat_group(GROUP_JID)]


async def test_dm_thread_skips_group_scoped_search(monkeypatch):
    """chat_group scopes are only written for group jids, so a DM must not pay
    a scoped graph search (embedding + roundtrip) into an empty scope."""
    graph = wire(monkeypatch, graph=FakeGraph(global_facts=[fact("g")]))
    out = await ca.assemble("q", DM_JID)
    assert "- g" in out
    assert all(not c["scope"] for c in graph.calls)


# ── provenance stamps ────────────────────────────────────────────────────


async def test_learning_provenance_stamp_includes_kind_and_source(monkeypatch):
    wire(monkeypatch,
         learnings=[learning("Weekly digest goes out Mondays",
                             kind="preference", source="person:3")])
    out = await ca.assemble("digest", DM_JID)
    assert "- [preference, from person:3] Weekly digest goes out Mondays" in out


async def test_fact_lines_carry_date_stamps(monkeypatch):
    wire(monkeypatch, graph=FakeGraph(global_facts=[
        fact("Acme pays net-30", created="2026-03-14T09:00:00+00:00"),
        fact("Rate is 5%", valid="2026-01-01T00:00:00+00:00"),
    ]))
    out = await ca.assemble("acme", DM_JID)
    assert "- Acme pays net-30 [recorded 2026-03-14]" in out
    assert "- Rate is 5% [valid from 2026-01-01]" in out


# ── per-line cap ─────────────────────────────────────────────────────────


async def test_lines_capped_at_300_chars(monkeypatch):
    wire(monkeypatch,
         learnings=[learning("L" * 800, kind="fact")],
         graph=FakeGraph(global_facts=[fact("F" * 800)]))
    out = await ca.assemble("q", DM_JID)
    for line in out.splitlines():
        assert len(line) <= ca.LINE_CHARS


# ── dedupe ───────────────────────────────────────────────────────────────


async def test_dedupe_across_global_and_group_facts(monkeypatch):
    dup = "Deliveries land Tuesday mornings"
    wire(monkeypatch, graph=FakeGraph(
        global_facts=[fact(dup), fact("Global-only fact")],
        group_facts=[fact(dup), fact("Group-only fact")]))
    out = await ca.assemble("deliveries", GROUP_JID)
    assert out.count(dup) == 1
    # Group facts outrank global ones, so the survivor sits in the group block.
    assert dup in out.split(ca.GROUP_HEADER)[1]
    assert "Global-only fact" in out
    assert "Group-only fact" in out


async def test_dedupe_learning_against_identical_fact(monkeypatch):
    dup = "Acme pays net-30"
    wire(monkeypatch,
         learnings=[learning(dup)],
         graph=FakeGraph(group_facts=[fact(dup)]))
    out = await ca.assemble("acme", GROUP_JID)
    assert out.count(dup) == 1
    assert ca.LEARNINGS_HEADER not in out  # learnings block emptied by dedupe


async def test_dedupe_learning_against_identical_global_fact(monkeypatch):
    """Learnings outrank global facts in candidate order, so a fact present in
    both stores survives in the learnings block and the global copy drops."""
    dup = "Acme pays net-30"
    wire(monkeypatch,
         learnings=[learning(dup)],
         graph=FakeGraph(global_facts=[fact(dup)]))
    out = await ca.assemble("acme", DM_JID)
    assert out.count(dup) == 1
    assert f"- [fact] {dup}" in out            # rendered as a learning...
    assert ca.GLOBAL_HEADER not in out         # ...global block emptied


# ── budget ───────────────────────────────────────────────────────────────


async def test_budget_drops_lowest_priority_first(monkeypatch):
    # Purpose + group facts + learnings + globals; budget sized so the global
    # facts (lowest priority) cannot fit but everything above them can.
    wire(monkeypatch,
         learnings=[learning("learning one about topic"),
                    learning("learning two about topic")],
         graph=FakeGraph(
             global_facts=[fact("global fact one"), fact("global fact two")],
             group_facts=[fact("group fact one"), fact("group fact two")]),
         chats_rows=[{"jid": GROUP_JID, "subject": "Ops",
                      "purpose": "daily operations chatter"}],
         budget=75)  # 300 chars
    out = await ca.assemble("topic", GROUP_JID)
    assert "daily operations chatter" in out         # highest priority kept
    assert "group fact one" in out
    assert "group fact two" in out
    assert "learning one about topic" in out
    assert "learning two about topic" in out         # learnings tail kept too
    assert "global fact" not in out                  # lowest priority dropped
    assert len(out) <= 300


async def test_budget_overflow_preserves_within_source_order(monkeypatch):
    wire(monkeypatch, graph=FakeGraph(global_facts=[
        fact(f"global fact number {i}") for i in range(6)]),
        budget=30)  # 120 chars: header + ~3 lines
    out = await ca.assemble("q", DM_JID)
    lines = [l for l in out.splitlines() if l.startswith("- ")]
    assert lines  # something survived
    # Survivors are a prefix of retrieval order, in order.
    assert lines == [f"- global fact number {i}" for i in range(len(lines))]
    assert len(out) <= 120


async def test_zero_budget_returns_empty(monkeypatch):
    wire(monkeypatch, graph=FakeGraph(global_facts=[fact("anything")]),
         budget=0)
    assert await ca.assemble("q", DM_JID) == ""


# ── fail-soft ────────────────────────────────────────────────────────────


async def test_raising_learnings_source_contributes_nothing_others_survive(monkeypatch):
    wire(monkeypatch,
         learnings=RuntimeError("pgvector down"),
         graph=FakeGraph(global_facts=[fact("Acme pays net-30")]))
    out = await ca.assemble("acme", DM_JID)
    assert "- Acme pays net-30" in out
    assert ca.LEARNINGS_HEADER not in out


async def test_raising_graph_contributes_nothing_learnings_survive(monkeypatch):
    wire(monkeypatch,
         learnings=[learning("Client prefers PDF reports", kind="preference")],
         graph=FakeGraph(error=RuntimeError("falkordb down")))
    out = await ca.assemble("reports", GROUP_JID)
    assert "- [preference] Client prefers PDF reports" in out
    assert ca.GLOBAL_HEADER not in out
    assert ca.GROUP_HEADER not in out


async def test_raising_chats_registry_still_yields_facts(monkeypatch):
    wire(monkeypatch,
         graph=FakeGraph(group_facts=[fact("group fact one")]),
         chats_rows=RuntimeError("db down"))
    out = await ca.assemble("q", GROUP_JID)
    assert "- group fact one" in out
    assert "WhatsApp group" not in out


async def test_assemble_never_raises_even_if_settings_break(monkeypatch):
    wire(monkeypatch, graph=FakeGraph(global_facts=[fact("f")]))
    monkeypatch.setattr(ca, "get_settings",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    out = await ca.assemble("q", DM_JID)  # falls back to the default budget
    assert "- f" in out


# ── group purpose ────────────────────────────────────────────────────────


async def test_group_purpose_only_for_gus_threads(monkeypatch):
    rows = [{"jid": GROUP_JID, "subject": "Ops", "purpose": "ops chatter"},
            {"jid": DM_JID, "subject": None, "purpose": "should never show"}]
    wire(monkeypatch, chats_rows=rows,
         graph=FakeGraph(global_facts=[fact("filler")]))
    dm_out = await ca.assemble("q", DM_JID)
    assert "should never show" not in dm_out
    group_out = await ca.assemble("q", GROUP_JID)
    assert 'This is the WhatsApp group "Ops", which is for: ops chatter' in group_out


async def test_group_purpose_truncated_to_500_chars(monkeypatch):
    wire(monkeypatch, chats_rows=[
        {"jid": GROUP_JID, "subject": "Ops", "purpose": "P" * 800}])
    out = await ca.assemble("q", GROUP_JID)
    assert "P" * 500 in out
    assert "P" * 501 not in out


# ── center-node forwarding (concurrent-landing tolerance) ────────────────


async def test_center_node_uuid_forwarded_when_resolver_exists(monkeypatch):
    async def resolver(name):
        assert name == "Asha"
        return "uuid-123"
    graph = wire(monkeypatch, graph=FakeGraph(global_facts=[fact("f")]),
                 resolver=resolver)
    await ca.assemble("q", DM_JID, sender_name="Asha")
    global_calls = [c for c in graph.calls if not c["scope"]]
    assert global_calls[0].get("center_node_uuid") == "uuid-123"


async def test_typeerror_fallback_when_search_lacks_center_kwarg(monkeypatch):
    async def resolver(name):
        return "uuid-123"
    graph = wire(monkeypatch,
                 graph=FakeGraph(global_facts=[fact("f")], accepts_center=False),
                 resolver=resolver)
    out = await ca.assemble("q", DM_JID, sender_name="Asha")
    assert "- f" in out  # fell back to a no-center call, still got facts
    global_calls = [c for c in graph.calls if not c["scope"]]
    assert all("center_node_uuid" not in c for c in global_calls)


async def test_no_resolver_module_attr_is_fine(monkeypatch):
    graph = wire(monkeypatch, graph=FakeGraph(global_facts=[fact("f")]))
    out = await ca.assemble("q", DM_JID, sender_name="Asha")
    assert "- f" in out
    assert all("center_node_uuid" not in c for c in graph.calls)


# ── group members block ──────────────────────────────────────────────────


def member(number, name=None, admin=False, person_id=None):
    return {"number": number, "name": name, "is_admin": admin,
            "person_id": person_id}


async def test_members_block_renders_names_admin_and_unregistered(monkeypatch):
    wire(monkeypatch, members=[
        member("919000000101", name="Naman", admin=True, person_id=2),
        member("919000000105", name="Vinayak", person_id=1),
        member("917000000001"),
    ])
    out = await ca.assemble("q", GROUP_JID)
    assert ("Group members (3): Naman (admin), Vinayak, "
            "917000000001 (not registered)") in out


async def test_empty_roster_renders_no_members_block(monkeypatch):
    # Absence of the block IS the "membership unknown" signal — an empty
    # "Group members (0):" line would read as an empty group.
    wire(monkeypatch, graph=FakeGraph(global_facts=[fact("f")]), members=[])
    out = await ca.assemble("q", GROUP_JID)
    assert "Group members" not in out


async def test_dm_thread_never_queries_the_roster(monkeypatch):
    graph = wire(monkeypatch, members=[member("919000000101", name="Naman")])
    out = await ca.assemble("q", DM_JID)
    assert graph.member_calls == []
    assert "Group members" not in out


async def test_large_roster_collapses_to_and_n_more(monkeypatch):
    rows = [member(f"9170000000{i:02d}", name=f"Person{i}") for i in range(20)]
    wire(monkeypatch, members=rows)
    out = await ca.assemble("q", GROUP_JID)
    assert "Group members (20):" in out
    assert ", and 8 more" in out                       # 20 - MEMBER_NAME_LIMIT
    line = next(l for l in out.splitlines() if l.startswith("Group members"))
    assert len(line) <= ca.MEMBERS_CHARS


async def test_raising_roster_source_contributes_nothing(monkeypatch):
    wire(monkeypatch, graph=FakeGraph(global_facts=[fact("Acme pays net-30")]),
         members=RuntimeError("db down"))
    out = await ca.assemble("acme", GROUP_JID)
    assert "- Acme pays net-30" in out
    assert "Group members" not in out


async def test_members_outrank_group_facts_under_budget_pressure(monkeypatch):
    # Drop priority: purpose > members > group facts. The roster is the ground
    # truth that stops membership confabulation, so under pressure it must
    # outlive the re-retrievable graph facts — but never the purpose.
    wire(monkeypatch,
         graph=FakeGraph(group_facts=[fact("group fact about the topic")]),
         chats_rows=[{"jid": GROUP_JID, "subject": "Ops",
                      "purpose": "daily operations chatter"}],
         members=[member("919000000101", name="Naman", admin=True)],
         budget=40)  # 160 chars: purpose + members fit, group facts do not
    out = await ca.assemble("topic", GROUP_JID)
    assert "daily operations chatter" in out
    assert "Group members (1): Naman (admin)" in out
    assert "group fact" not in out


# ── Quick 009: lessons ───────────────────────────────────────────────────


async def test_lessons_render_first_as_instructions(monkeypatch):
    wire(monkeypatch,
         learnings=[learning("Fuel litres live in fuel_logs.litres")],
         lessons=[learning("To find fuel drops in VehicleTelemetry, filter by plate "
                           "and a time window first; unfiltered scans time out.",
                           kind="lesson")])
    out = await ca.assemble("find fuel drops", DM_JID)
    assert ca.LESSONS_HEADER in out
    assert out.index(ca.LESSONS_HEADER) < out.index(ca.LEARNINGS_HEADER)
    assert "- To find fuel drops in VehicleTelemetry" in out


async def test_lessons_outrank_facts_when_the_budget_is_tight(monkeypatch):
    long_lesson = "Filter VehicleTelemetry by plate first. " * 12
    wire(monkeypatch, budget=150,
         learnings=[learning("x" * 250)],
         lessons=[learning(long_lesson, kind="lesson")],
         graph=FakeGraph(global_facts=[fact("y" * 250)]))
    out = await ca.assemble("q", DM_JID)
    assert ca.LESSONS_HEADER in out
    assert ca.LEARNINGS_HEADER not in out


async def test_raising_lessons_source_contributes_nothing(monkeypatch):
    wire(monkeypatch, learnings=[learning("Acme pays net-30")],
         lessons=RuntimeError("boom"))
    out = await ca.assemble("q", DM_JID)
    assert ca.LESSONS_HEADER not in out and "Acme pays net-30" in out


# ── Quick 019: provenance (what reached the model) ───────────────────────


def _no_cases(monkeypatch, lines=()):
    async def _cases(msg, thread_id):
        return list(lines)
    monkeypatch.setattr(ca, "_open_cases", _cases)


def _gfact(text, uuid, kind="fact", group_id="global"):
    return {"fact": text, "valid_at": None, "created_at": None, "uuid": uuid,
            "kind": kind, "group_id": group_id}


async def test_provenance_names_every_kept_item(monkeypatch):
    lesson = dict(learning("To count fuel drops, filter VehicleTelemetry by plate first.",
                           kind="lesson"), id=31)
    wire(monkeypatch,
         learnings=[dict(learning("Acme pays net-30", kind="fact", source="dashboard"), id=7)],
         lessons=[lesson],
         graph=FakeGraph(global_facts=[_gfact("Northwind ships with BlueDart", "e-1"),
                                       _gfact("Northwind: a distributor", "n-1", kind="entity")],
                         group_facts=[_gfact("This group tracks diesel", "e-9",
                                             group_id="chat_120363000000000001_g_us")]),
         chats_rows=[{"jid": GROUP_JID, "subject": "Depot", "purpose": "diesel"}],
         members=[{"name": "Asha", "number": "91900", "is_admin": True}])
    _no_cases(monkeypatch, ["- Case #19: Collect payment — pending"])
    text, used = await ca.assemble_with_provenance("fuel drops acme", GROUP_JID)
    assert text == await ca.assemble("fuel drops acme", GROUP_JID)
    assert used["lessons"] == [{"id": 31, "text": lesson["content"]}]
    assert used["learnings"] == [{"id": 7, "kind": "fact", "source": "dashboard",
                                  "text": "[fact, from dashboard] Acme pays net-30"}]
    assert [(g["uuid"], g["kind"], g["block"]) for g in used["graph"]] == [
        ("e-9", "fact", "group"), ("e-1", "fact", "global"), ("n-1", "entity", "global")]
    assert used["graph"][0]["group_id"] == "chat_120363000000000001_g_us"
    assert used["cases"] == [{"id": 19, "text": "Case #19: Collect payment — pending"}]
    assert used["group"] == {"purpose": True, "members": True}
    assert used["chars"] == len(text) and used["budget_chars"] == 1500 * 4
    assert used["dropped"] == 0


async def test_provenance_lists_only_what_fit_the_budget(monkeypatch):
    long_lesson = "Filter VehicleTelemetry by plate first. " * 12
    wire(monkeypatch, budget=150,
         learnings=[dict(learning("x" * 250), id=1)],
         lessons=[dict(learning(long_lesson, kind="lesson"), id=2)],
         graph=FakeGraph(global_facts=[_gfact("y" * 250, "e-2")]))
    _no_cases(monkeypatch)
    text, used = await ca.assemble_with_provenance("q", DM_JID)
    assert [l["id"] for l in used["lessons"]] == [2]
    assert used["learnings"] == [] and used["graph"] == []
    assert used["dropped"] == 2
    assert used["chars"] == len(text) <= used["budget_chars"] == 600


async def test_provenance_of_nothing_is_an_empty_record(monkeypatch):
    wire(monkeypatch)
    _no_cases(monkeypatch)
    text, used = await ca.assemble_with_provenance("hello", DM_JID)
    assert text == ""
    assert used["chars"] == 0 and used["dropped"] == 0
    assert used["lessons"] == used["learnings"] == used["graph"] == used["cases"] == []


async def test_provenance_survives_a_raising_source(monkeypatch):
    wire(monkeypatch, learnings=RuntimeError("pgvector down"),
         graph=FakeGraph(global_facts=[_gfact("Acme pays net-30", "e-3")]))
    _no_cases(monkeypatch)
    text, used = await ca.assemble_with_provenance("acme", DM_JID)
    assert "Acme pays net-30" in text
    assert used["learnings"] == [] and used["graph"][0]["uuid"] == "e-3"


async def test_provenance_keeps_old_style_fact_dicts(monkeypatch):
    """A fact dict without uuid/kind (pre-Quick-014 shape) still records."""
    wire(monkeypatch, graph=FakeGraph(global_facts=[fact("Acme pays net-30")]))
    _no_cases(monkeypatch)
    _, used = await ca.assemble_with_provenance("acme", DM_JID)
    assert used["graph"] == [{"uuid": None, "kind": "fact", "group_id": None,
                              "block": "global", "text": "Acme pays net-30"}]

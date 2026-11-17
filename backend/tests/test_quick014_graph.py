"""Quick 014 — the connected memory graph.

Pure logic (no FalkorDB, no LLM, no DB): fakes stand in for graphiti, the
Postgres pool and the graph reads. Proves:
  * add_episode passes the business entity / relation types and extraction
    instructions, skips an identical episode, stores an episode embedding,
    and counts outcomes (a failure is visible on status, not only in a log)
  * the ontology is prompt text graphiti accepts (field-less models, no
    reserved names)
  * the case distiller's "nothing durable" output is never stored, a case is
    written once (global) with its "Case #<id>" header
  * a one-off request is not a preference (check_learning, reflection prompt)
  * the rebuild plan: latest learning revision only, one-off preferences
    retired, junk and legacy case copies dropped, duplicates collapsed,
    archived chat setup kept, oldest first
  * graph-view scopes and deterministic app-object links
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import graph_ontology, graphview, memory, memory_pipeline, memory_rebuild, memoryservice
from app.memoryservice import MemoryService


# ── add_episode ─────────────────────────────────────────────────────────────

class RecordingDriver:          # "falkor" not in the name: the dedupe check can't tell
    pass


class FakeFalkorDriver:
    def __init__(self, graphs=("global",), existing=()):
        self.client = SimpleNamespace(list_graphs=lambda: list(graphs))
        self.existing = set(existing)
        self.queries = []

    def clone(self, database):
        return self

    async def execute_query(self, cypher, **params):
        self.queries.append((cypher, params))
        if "e.content = $v" in cypher:
            return ([{"n": 1 if params.get("v") in self.existing else 0}], [], None)
        return ([], [], None)


class ExtractingGraphiti:
    def __init__(self, error=None):
        self.calls = []
        self.error = error
        self.driver = FakeFalkorDriver()
        self.embedder = SimpleNamespace(create=self._embed)

    async def _embed(self, input_data):
        return [0.5, 0.25]

    async def add_episode(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return SimpleNamespace(nodes=[1, 2, 3], edges=[1, 2],
                               episode=SimpleNamespace(uuid="ep-1"))


def _svc(g) -> MemoryService:
    ms = MemoryService()
    ms._graphiti = g
    ms._available = True
    return ms


async def test_add_episode_extracts_with_business_types(monkeypatch):
    g = ExtractingGraphiti()
    ms = _svc(g)
    ok = await ms.add_episode("A fuel drop counts when the level falls by 40 L.",
                              source="reflection:fact", name="learning_1_r0")
    assert ok is True
    kw = g.calls[0]
    assert kw["entity_types"] is graph_ontology.ENTITY_TYPES
    assert kw["edge_types"] is graph_ontology.EDGE_TYPES
    assert kw["custom_extraction_instructions"] == graph_ontology.EXTRACTION_INSTRUCTIONS
    assert kw["group_id"] == "global" and kw["name"] == "learning_1_r0"
    st = ms._extraction
    assert st["ok"] == 1 and st["failed"] == 0
    assert st["last"] == {"name": "learning_1_r0", "group_id": "global", "entities": 3, "facts": 2}
    # the episode text's embedding lands on the Episodic node
    cy, params = g.driver.queries[-1]
    assert "SET e.ic_embedding = vecf32($vec)" in cy
    assert params == {"uuid": "ep-1", "vec": [0.5, 0.25]}


async def test_identical_episode_is_stored_once():
    g = ExtractingGraphiti()
    g.driver = FakeFalkorDriver(existing={"Same text, already in the graph."})
    ms = _svc(g)
    assert await ms.add_episode("Same text, already in the graph.", source="onboarding") is True
    assert g.calls == []                                   # no second extraction
    assert ms._extraction["duplicates"] == 1


async def test_failed_extraction_is_counted_and_recorded(monkeypatch):
    recorded = []

    async def record(kind, name="", **kw):
        recorded.append((kind, name, kw))

    from app import jobruns
    monkeypatch.setattr(jobruns, "record", record)
    g = ExtractingGraphiti(error=ValueError("OpenAI said 400: model not found"))
    ms = _svc(g)
    assert await ms.add_episode("x is y", source="test", name="n1") is False
    st = ms._extraction
    assert st["failed"] == 1 and "model not found" in st["last_error"] and st["last_error_at"]
    assert ms._available is True                           # not a connection error
    kind, name, kw = recorded[0]
    assert kind == "memory_graph" and kw["status"] == "failed" and kw["ref"] == ("graph", "global")


async def test_status_reports_extraction_and_counts(monkeypatch):
    monkeypatch.setattr(memoryservice, "get_settings", lambda: SimpleNamespace(
        memory_graph_enabled=True, falkordb_host="f", falkordb_port=1))
    ms = _svc(ExtractingGraphiti())
    ms._extraction["failed"] = 2

    async def counts():
        return {"global": {"episodes": 8, "entities": 24, "facts": 30}}

    ms.graph_counts = counts
    st = await ms.status(probe=False)
    assert st["state"] == "connected"
    assert (st["episodes"], st["entities"], st["facts"]) == (8, 24, 30)
    assert st["extraction"]["failed"] == 2


# ── ontology ────────────────────────────────────────────────────────────────

def test_ontology_is_valid_for_graphiti():
    from graphiti_core.utils.ontology_utils.entity_types_utils import validate_entity_types
    assert validate_entity_types(graph_ontology.ENTITY_TYPES)
    for name, model in {**graph_ontology.ENTITY_TYPES, **graph_ontology.EDGE_TYPES}.items():
        assert model.model_fields == {}, f"{name} would cost an attribute-extraction call"
        assert (model.__doc__ or "").strip(), f"{name} has no description for the prompt"
    assert {"Machine", "Rule", "EventType", "Table", "Column", "Case", "Metric", "Person",
            "Preference"} <= set(graph_ontology.ENTITY_TYPES)
    assert {"HAS_ISSUE", "DEFINED_AS", "MEASURED_IN", "BELONGS_TO", "OWNED_BY",
            "PREFERS"} <= set(graph_ontology.EDGE_TYPES)
    assert "Case #N" in graph_ontology.EXTRACTION_INSTRUCTIONS


# ── hygiene ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "There are no durable facts to extract from the provided case; it was a one-off sensor check.",
    "NONE", "None.", "", "The case contains no durable business knowledge.",
    "Nothing durable came out of this case.",
])
def test_empty_distillations_are_recognised(text):
    assert memory_pipeline.is_empty_distillation(text)


@pytest.mark.parametrize("text", [
    "Excavator machine code 11 logged 32 confirmed fuel drops; near-identical volumes may indicate a sensor fault.",
    "The vendor has no durable contract with us yet, but Priya handles all Pune purchase orders.",
])
def test_real_distillations_are_kept(text):
    assert not memory_pipeline.is_empty_distillation(text)


def test_distill_prompt_answers_none():
    assert "answer exactly NONE" in memory_pipeline.DISTILL_PROMPT


def test_case_episode_has_its_case_header():
    t = memory_pipeline.case_episode_text(12, "Excavator 11 fuel drops", " Sensor fault. ",
                                          datetime(2026, 10, 20, 9, tzinfo=timezone.utc))
    assert t == 'Case #12 ("Excavator 11 fuel drops"), closed 2026-10-20: Sensor fault.'
    assert memory_rebuild.strip_case_header(t) == "Sensor fault."
    assert memory_pipeline.case_episode_name(12) == "case_12_close"


async def test_distill_case_writes_once_to_global_and_never_junk(monkeypatch):
    added, stamps = [], []

    class Pool:
        async def fetchrow(self, sql, *a):
            return {"title": "T", "closed_at": datetime(2026, 10, 20, tzinfo=timezone.utc)}

        async def execute(self, sql, *a):
            stamps.append(a)

    async def pool():
        return Pool()

    class Graph:
        async def add_episode(self, text, source, **kw):
            added.append((text, kw.get("group_id"), kw.get("name")))
            return True

    monkeypatch.setattr(memory_pipeline, "get_pool", pool)
    monkeypatch.setattr(memory_pipeline, "memory_graph", Graph())

    async def good(cid):
        return "Ramesh owns the excavator's fuel sensor."
    monkeypatch.setattr(memory_pipeline, "distill_text", good)
    assert await memory_pipeline.distill_case_outcome(7) == "stored"
    assert added == [('Case #7 ("T"), closed 2026-10-20: Ramesh owns the excavator\'s fuel sensor.',
                      "global", "case_7_close")]                    # one copy, global
    assert stamps[-1] == (7, "Ramesh owns the excavator's fuel sensor.")

    async def empty(cid):
        return ""
    monkeypatch.setattr(memory_pipeline, "distill_text", empty)
    assert await memory_pipeline.distill_case_outcome(8) == "empty"
    assert len(added) == 1                                         # nothing written
    assert stamps[-1] == (8, "")


@pytest.mark.parametrize("text,one_off", [
    ("The user prefers to receive a report listing every missed fuel drop event.", True),
    ("The user asked for a list of machines with fuel drops last month.", True),
    ("The owner prefers fuel quantities in litres, not gallons.", False),
    ("The owner prefers a weekly fuel summary every Monday morning.", False),
    ("The owner always wants dashboards to show litres.", False),
])
def test_one_off_request_is_not_a_preference(text, one_off):
    assert bool(memory.preference_problem(text)) is one_off


async def test_check_learning_refuses_a_one_off_preference():
    assert "one-off request" in await memory.check_learning(
        "preference", "The user prefers to receive a report listing every missed fuel drop event.")
    assert await memory.check_learning("preference", "The owner prefers litres by default.") is None


def test_reflection_prompt_carries_the_preference_test():
    assert "one-off requests as preferences" in memory.REFLECTION_PROMPT
    assert "FUTURE work" in memory.PREFERENCE_RULE


# ── rebuild plan ───────────────────────────────────────────────────────────

NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)


class PlanPool:
    def __init__(self, *, learnings=(), cases=(), onboarding=(), archive=()):
        self.learnings, self.cases = list(learnings), list(cases)
        self.onboarding, self.archive = list(onboarding), list(archive)

    async def fetch(self, sql, *a):
        if "FROM graph_episode_archive" in sql:
            return self.archive
        if "FROM learnings" in sql:
            return self.learnings
        if "FROM cases" in sql:
            return self.cases
        if "FROM onboarding_sessions" in sql:
            return self.onboarding
        raise AssertionError(sql)


def _arch(i, group, name, source, content, days):
    return {"id": i, "group_id": group, "name": name, "source": source,
            "content": content, "valid_at": NOW - timedelta(days=days)}


async def test_rebuild_plan_cleans_junk_and_duplicates(monkeypatch):
    distilled = []

    async def distill_text(cid):
        distilled.append(cid)
        return ""                      # case 2 holds nothing durable

    monkeypatch.setattr(memory_pipeline, "distill_text", distill_text)
    case1 = ('Case #1 ("Excavator 11"), closed 2026-10-22: Excavator machine code 11 has a '
             'fuel sensor calibration fault; Ramesh recalibrates it.')
    junk = "There are no durable facts to extract from the provided case; a one-off resend."
    profile = {"summary": "Demo Earthmovers rents excavators.", "facts_told": ["Fuel is the main cost."]}
    pool = PlanPool(
        learnings=[
            {"id": 1, "kind": "fact", "content": "A fuel drop counts when the level falls 40 L.",
             "revisions": 2, "updated_at": NOW - timedelta(days=9)},
            {"id": 7, "kind": "preference", "revisions": 0, "updated_at": NOW - timedelta(days=8),
             "content": "The user prefers to receive a report listing every missed fuel drop event."},
        ],
        cases=[{"id": 1, "title": "Excavator 11", "closed_at": NOW - timedelta(days=3), "distillation": None},
               {"id": 2, "title": "Resend", "closed_at": NOW - timedelta(days=2), "distillation": None}],
        onboarding=[{"id": 4, "kind": "business", "subject_ref": None,
                     "profile": json.dumps(profile), "at": NOW - timedelta(days=12)}],
        archive=[
            _arch(1, "global", "learning_1_r0", "reflection:fact", "old revision text", 10),
            _arch(2, "global", "learning_7_r0", "reflection:preference", "one-off", 8),
            _arch(3, "global", "case_1_close", "case_close", case1, 3),
            _arch(4, "case_1", "case_1_close_scoped", "case_close", case1, 3),
            _arch(5, "global", "case_2_close", "case_close", junk, 2),
            _arch(6, "case_2", "case_2_close_scoped", "case_close", junk, 2),
            _arch(7, "global", "onboarding:a", "onboarding", "old onboarding text", 12),
            _arch(8, "chat_120363_g_us", "chat_onboarding:x", "chat_onboarding",
                  'The WhatsApp group "Site fuel" is for: daily fuel readings', 6),
        ])
    plan = await memory_rebuild.build_plan(pool)
    items = [it for it in plan["items"] if it["text"]]
    names = [it["name"] for it in items]
    # oldest first; latest learning revision; one copy of case 1 in global;
    # onboarding re-derived from the interview; archived chat setup kept
    assert names == ["onboarding_business_4", "learning_1_r2", "chat_onboarding:x", "case_1_close"]
    assert {it["group"] for it in items} == {"global", "chat_120363_g_us"}
    case_item = next(it for it in items if it["name"] == "case_1_close")
    assert case_item["text"] == case1 and case_item["ref"][0] == "case"
    assert [r["id"] for r in plan["retire"]] == [7]
    assert "one-off request" in plan["retire"][0]["reason"]
    assert distilled == [2]                                    # case 1 reused its archived text
    assert any(it["ref"] == ("case_empty", 2) for it in plan["items"])
    notes = plan["notes"]
    assert notes["junk_dropped"] == 1 and notes["legacy_case_copies_dropped"] == 2
    assert notes["cases_nothing_durable"] == 1
    assert notes["learning_episodes_dropped"] == 2             # old revision + retired one-off
    assert not any("no durable facts" in it["text"] for it in items)


async def test_rebuild_plan_dedupes_identical_texts(monkeypatch):
    pool = PlanPool(archive=[
        _arch(1, "chat_x", "a", "chat_onboarding", "Same purpose.", 5),
        _arch(2, "chat_x", "b", "chat_onboarding", "same   purpose.", 4),
        _arch(3, "chat_y", "c", "chat_onboarding", "Same purpose.", 3),
    ])
    plan = await memory_rebuild.build_plan(pool, distill=False)
    assert [(it["group"], it["name"]) for it in plan["items"]] == [("chat_x", "a"), ("chat_y", "c")]


async def test_rebuild_is_single_flight(monkeypatch):
    async with memory_pipeline._backfill_lock:
        out = await memory_rebuild.rebuild_graph()
    assert out["stopped"] == "a backfill or rebuild is already running"


# ── graph view ─────────────────────────────────────────────────────────────

def test_scopes_resolve_to_existing_graphs_only():
    names = ["global", "chat_x", "case_3"]
    assert graphview.resolve_scope("everything", names) == (names, None)
    assert graphview.resolve_scope("global", names) == (["global"], None)
    assert graphview.resolve_scope("case:3", names) == (["global", "case_3"], 3)
    assert graphview.resolve_scope("case:9", names) == (["global"], 9)
    assert graphview.resolve_scope("chat_x", names) == (["chat_x"], None)
    assert graphview.resolve_scope("default_db", names) == ([], None)   # never an unknown graph


IDX = {
    "tables": {"dailyfuelreports": {"kind": "table", "id": "business_db.public.DailyFuelReports",
                                    "label": "DailyFuelReports", "href": "/context/schema?x"}},
    "columns": {"machines.operatingstate": {"kind": "column", "id": "c", "label": "Machines.OperatingState",
                                            "href": "/context/schema?y"}},
    "kpis": {"fuel drops per day": {"kind": "kpi", "id": 5, "label": "Fuel drops per day", "href": "/kpis/5"}},
    "case_titles": {"excavator 11 repeated fuel drops": 1},
    "cases": {1, 2},
}


@pytest.mark.parametrize("node,links", [
    ({"kind": "entity", "name": "DailyFuelReports", "type": "Table"}, [("table", "/context/schema?x")]),
    ({"kind": "entity", "name": "Machines.OperatingState", "type": "Column"}, [("column", "/context/schema?y")]),
    ({"kind": "entity", "name": "Case #1", "type": "Case"}, [("case", "/cases/1")]),
    ({"kind": "entity", "name": "Case #99", "type": "Case"}, []),                 # no such case
    ({"kind": "entity", "name": "Excavator 11 repeated fuel drops", "type": "Case"}, [("case", "/cases/1")]),
    ({"kind": "entity", "name": "fuel drops per day", "type": "Metric"}, [("kpi", "/kpis/5")]),
    ({"kind": "entity", "name": "Fuel Drops  per Day", "type": "Report"}, [("kpi", "/kpis/5")]),
    ({"kind": "entity", "name": "fuel drop", "type": "EventType"}, []),           # no fuzzy matching
    ({"kind": "entity", "name": "DailyFuelReports", "type": "Person"}, []),
    ({"kind": "episode", "name": "case_2_close", "type": "Episode"}, [("case", "/cases/2")]),
])
def test_links_are_exact_and_deterministic(node, links):
    assert [(l["kind"], l["href"]) for l in graphview.links_for(node, IDX)] == links


async def test_build_view_caps_and_keeps_matches(monkeypatch):
    ents = [{"uuid": f"n{i}", "name": f"thing {i}", "labels": ["Entity", "Machine"],
             "summary": "", "created_at": None, "degree": 50 - i} for i in range(40)]
    far = {"uuid": "far", "name": "fuel drop", "labels": ["Entity", "EventType"], "summary": "",
           "created_at": None, "degree": 0}

    class G:
        async def scope_graphs(self):
            return ["global"]

        async def graph_counts(self):
            return {"global": {"episodes": 3, "entities": 41, "facts": 2}}

        async def read_entities(self, gid, *, limit=300, uuids=None):
            if uuids is not None:
                return [e for e in ents + [far] if e["uuid"] in uuids]
            return ents[:limit]

        async def read_facts(self, gid, *, among=None, touching=None, limit=2000):
            facts = [{"uuid": "f1", "source": "far", "target": "n0", "name": "HAS_ISSUE",
                      "fact": "thing 0 has fuel drops", "invalid_at": None, "expired_at": None},
                     {"uuid": "f2", "source": "n1", "target": "n2", "name": "OLD",
                      "fact": "old", "invalid_at": "2026-10-01", "expired_at": None}]
            if touching:
                return [f for f in facts if touching in (f["source"], f["target"])]
            return [f for f in facts if f["source"] in among and f["target"] in among]

        async def read_episodes(self, gid, **kw):
            return []

        async def read_mentions(self, gid, **kw):
            return []

        async def recall(self, q, scope=None, limit=8, include_superseded=False):
            return [{"kind": "entity", "uuid": "far", "group_id": "global", "fact": "fuel drop"}]

    async def no_links():
        return {"tables": {}, "columns": {}, "kpis": {}, "case_titles": {}, "cases": set()}

    monkeypatch.setattr(graphview, "memory_graph", G())
    monkeypatch.setattr(graphview, "_link_index", no_links)
    view = await graphview.build_view(limit=10, q="fuel drop", superseded=False)
    ids = {n["id"] for n in view["nodes"]}
    assert "far" in ids and "n0" in ids                       # the match and its neighbour
    assert len(ids) <= 12 and view["truncated"] is True
    assert view["matches"]["nodes"] == ["far"]
    assert [e["id"] for e in view["edges"]] == ["f1"]         # superseded f2 dropped
    assert view["scopes"][0]["id"] == "everything"

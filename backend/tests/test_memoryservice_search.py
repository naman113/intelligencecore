"""MemoryService.recall / search structured results + fact_line rendering.

Pure logic: a fake graphiti client is injected into an already-"initialized"
MemoryService, so no FalkorDB / network is touched. Quick 014 replaced the
edges-only search with recall(): one query embedding, then per scope graph
one graphiti search over facts, entities and episodes (patched here as
`_run_search`) plus a cosine query over episode embeddings, merged by
weighted reciprocal-rank fusion.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import memoryservice
from app.memoryservice import MemoryService, fact_line


class FakeEmbedder:
    def __init__(self):
        self.calls = []

    async def create(self, input_data):
        self.calls.append(input_data)
        return [0.1, 0.2, 0.3]


class FakeGraphiti:
    """`calls` records every per-scope search (query, config, group, center,
    qvec, driver); `recipe_calls` records graphiti.search_ (node lookups for
    resolve_person_node)."""

    def __init__(self, results=None, error=None, nodes=None, recipe_error=None,
                 episodes=None):
        self.calls: list[dict] = []
        self.recipe_calls: list[dict] = []
        self.results = results if results is not None else []
        self.nodes = nodes if nodes is not None else []
        self.episodes = episodes if episodes is not None else []
        self.error = error                # raised by every search
        self.recipe_error = recipe_error  # raised by a non-rrf edge recipe / search_
        self.embedder = FakeEmbedder()
        self.clients = SimpleNamespace()

    async def run_search(self, query, config, gid, center, qvec, driver):
        rec = {"query": query, "config": config, "group": gid, "center": center,
               "qvec": qvec, "driver": driver}
        self.calls.append(rec)
        if self.error is not None:
            raise self.error
        if self.recipe_error is not None and config.edge_config.reranker.value in (
                "mmr", "cross_encoder"):
            raise self.recipe_error
        return SimpleNamespace(edges=list(self.results), nodes=list(self.nodes),
                               episodes=list(self.episodes))

    async def search_(self, query, config, group_ids=None,
                      center_node_uuid=None, driver=None, **kw):
        rec = {"query": query, "num_results": config.limit, "config": config}
        if group_ids is not None:
            rec["group_ids"] = group_ids
        if center_node_uuid is not None:
            rec["center_node_uuid"] = center_node_uuid
        if driver is not None:
            rec["driver"] = driver
        self.recipe_calls.append(rec)
        if self.recipe_error is not None:
            raise self.recipe_error
        if self.error is not None:
            raise self.error
        return SimpleNamespace(edges=self.results, nodes=self.nodes)


def _service(fake: FakeGraphiti) -> MemoryService:
    ms = MemoryService()
    ms._graphiti = fake
    ms._available = True
    ms._run_search = fake.run_search
    return ms


def _settings(recipe: str) -> SimpleNamespace:
    return SimpleNamespace(memory_graph_enabled=True, graph_search_recipe=recipe)


@pytest.fixture(autouse=True)
def _clear_person_node_cache():
    memoryservice._person_node_cache.clear()
    memoryservice._person_node_miss_cache.clear()
    yield
    memoryservice._person_node_cache.clear()
    memoryservice._person_node_miss_cache.clear()


def edge(**kw) -> SimpleNamespace:
    return SimpleNamespace(**kw)


def _reranker(call) -> str:
    return call["config"].edge_config.reranker.value


# ── forwarding ───────────────────────────────────────────────────────────

async def test_layer_size_follows_limit():
    fake = FakeGraphiti(results=[edge(fact="a")])
    ms = _service(fake)
    await ms.search("query", limit=3)
    assert fake.calls[0]["config"].limit == 10       # never fewer than 10 candidates
    await ms.search("query", limit=8)
    assert fake.calls[-1]["config"].limit == 16       # twice the limit, then fused


async def test_each_scope_graph_searched_with_one_embedding():
    fake = FakeGraphiti(results=[])
    ms = _service(fake)
    await ms.search("query", scope=["case_42", "global"], limit=5)
    assert [c["group"] for c in fake.calls] == ["case_42", "global"]
    assert all(c["query"] == "query" for c in fake.calls)
    assert len(fake.embedder.calls) == 1               # one query vector for every scope
    assert all(c["qvec"] == [0.1, 0.2, 0.3] for c in fake.calls)


async def test_no_scope_defaults_to_global():
    # On FalkorDB there is no "everywhere" query — an unscoped search would hit
    # the driver's default graph, which never holds data. None means global.
    fake = FakeGraphiti(results=[])
    ms = _service(fake)
    await ms.search("query")
    assert [c["group"] for c in fake.calls] == [memoryservice.GLOBAL_GROUP]


async def test_every_layer_is_searched():
    fake = FakeGraphiti(results=[])
    ms = _service(fake)
    await ms.search("query")
    cfg = fake.calls[0]["config"]
    assert cfg.edge_config is not None and cfg.node_config is not None
    assert cfg.episode_config is not None


# ── structured results ───────────────────────────────────────────────────

async def test_returns_dicts_with_fact_and_dates():
    valid = datetime(2026, 3, 14, 9, 0, tzinfo=timezone.utc)
    created = datetime(2026, 3, 15, 10, 30, tzinfo=timezone.utc)
    fake = FakeGraphiti(results=[
        edge(uuid="e1", fact="Client prefers morning deliveries", name="PREFERS",
             valid_at=valid, created_at=created, invalid_at=None),
    ])
    ms = _service(fake)
    out = await ms.search("deliveries")
    assert len(out) == 1
    f = out[0]
    # the pre-Quick-014 keys, unchanged
    assert {k: f[k] for k in ("fact", "valid_at", "created_at")} == {
        "fact": "Client prefers morning deliveries",
        "valid_at": valid.isoformat(),
        "created_at": created.isoformat(),
    }
    assert f["kind"] == "fact" and f["uuid"] == "e1" and f["name"] == "PREFERS"
    assert f["group_id"] == "global" and f["superseded"] is False


async def test_missing_date_attrs_become_none():
    fake = FakeGraphiti(results=[edge(fact="bare fact")])
    ms = _service(fake)
    out = await ms.search("q")
    assert out[0]["fact"] == "bare fact"
    assert out[0]["valid_at"] is None and out[0]["created_at"] is None


async def test_string_dates_pass_through():
    fake = FakeGraphiti(results=[
        edge(fact="f", valid_at="2026-01-02T00:00:00+00:00", created_at=None),
    ])
    ms = _service(fake)
    out = await ms.search("q")
    assert out[0]["valid_at"] == "2026-01-02T00:00:00+00:00"
    assert out[0]["created_at"] is None


async def test_name_fallback_when_no_fact_attr():
    fake = FakeGraphiti(results=[edge(name="EntityName")])
    ms = _service(fake)
    out = await ms.search("q")
    assert out[0]["fact"] == "EntityName"


async def test_invalidated_edges_filtered_out_for_agents():
    fake = FakeGraphiti(results=[
        edge(fact="still true", invalid_at=None),
        edge(fact="superseded", invalid_at=datetime(2026, 5, 1, tzinfo=timezone.utc)),
        edge(fact="expired too", expired_at=datetime(2026, 5, 2, tzinfo=timezone.utc)),
        edge(fact="also true"),
    ])
    ms = _service(fake)
    out = await ms.search("q")
    assert [f["fact"] for f in out] == ["still true", "also true"]


async def test_recall_can_show_superseded_flagged():
    fake = FakeGraphiti(results=[
        edge(fact="old rate", invalid_at=datetime(2026, 5, 1, tzinfo=timezone.utc)),
        edge(fact="new rate"),
    ])
    ms = _service(fake)
    out = await ms.recall("q", include_superseded=True)
    flags = {f["fact"]: f["superseded"] for f in out}
    assert flags == {"old rate": True, "new rate": False}
    assert out[[f["fact"] for f in out].index("old rate")]["invalid_at"].startswith("2026-05-01")


async def test_results_capped_at_limit():
    fake = FakeGraphiti(results=[edge(fact=f"f{i}") for i in range(10)])
    ms = _service(fake)
    out = await ms.search("q", limit=4)
    assert len(out) == 4


async def test_entities_and_episodes_come_back_too():
    """The Quick 014 bug: "fuel drop" found nothing because only facts were
    searched, while the matching episodes sat in the graph."""
    fake = FakeGraphiti(
        results=[],
        nodes=[SimpleNamespace(uuid="n1", name="fuel drop", summary="A drop of 40 L or more.",
                               labels=["Entity", "EventType"])],
        episodes=[SimpleNamespace(uuid="ep1", name="learning_3_r0",
                                  content="A fuel drop counts when the level falls by at least 40 L.",
                                  source_description="reflection:fact")])
    ms = _service(fake)
    out = await ms.search("fuel drop")
    kinds = {f["kind"]: f for f in out}
    assert kinds["entity"]["fact"] == "fuel drop: A drop of 40 L or more."
    assert kinds["entity"]["type"] == "EventType"
    assert kinds["episode"]["fact"].startswith("A fuel drop counts")
    assert kinds["episode"]["source"] == "reflection:fact"


async def test_agents_get_long_episodes_clipped_recall_does_not():
    long = "x" * (memoryservice.AGENT_EPISODE_CHARS + 500)
    fake = FakeGraphiti(episodes=[SimpleNamespace(uuid="ep", content=long)])
    ms = _service(fake)
    assert len((await ms.search("q"))[0]["fact"]) == memoryservice.AGENT_EPISODE_CHARS + 1
    assert (await ms.recall("q"))[0]["fact"] == long


async def test_fusion_ranks_an_item_found_twice_first():
    # e2 is ranked second by the graph search but also first by the episode
    # embedding search, so it outranks e1 (found once, first).
    fake = FakeGraphiti(episodes=[SimpleNamespace(uuid="e1", content="one"),
                                  SimpleNamespace(uuid="e2", content="two")])
    ms = _service(fake)

    class Drv:  # "falkor" in the class name: a per-group driver exists
        pass

    Drv.__name__ = "FakeFalkorDriver"
    ms._group_driver = lambda gid: Drv()

    async def vec_search(driver, qvec, n):
        return [{"uuid": "e2", "content": "two"}]

    ms._episode_vector_search = vec_search
    ms._our_graph_names = _names(["global"])
    out = await ms.recall("q")
    assert [f["fact"] for f in out][:2] == ["two", "one"]


async def test_identical_texts_collapse():
    fake = FakeGraphiti(results=[edge(uuid="a", fact="Same fact."), edge(uuid="b", fact="same  fact.")])
    ms = _service(fake)
    out = await ms.search("q")
    assert len(out) == 1


def _names(names):
    async def f():
        return list(names)
    return f


async def test_scopes_without_a_graph_are_skipped():
    """Reading a scope graph that does not exist would create it."""
    fake = FakeGraphiti(results=[edge(fact="a")])
    ms = _service(fake)
    ms._our_graph_names = _names(["global"])
    out = await ms.search("q", scope=["case_42", "global"])
    assert [c["group"] for c in fake.calls] == ["global"]
    assert [f["fact"] for f in out] == ["a"]
    fake.calls.clear()
    assert await ms.search("q", scope=["case_42"]) == []
    assert fake.calls == []


# ── fail-soft ────────────────────────────────────────────────────────────

async def test_empty_query_returns_empty_without_calling_graph():
    fake = FakeGraphiti(results=[edge(fact="x")])
    ms = _service(fake)
    assert await ms.search("") == []
    assert await ms.search("   ") == []
    assert fake.calls == []


async def test_graph_error_returns_empty_list():
    fake = FakeGraphiti(error=RuntimeError("falkordb down"))
    ms = _service(fake)
    assert await ms.search("anything") == []


async def test_unavailable_service_returns_empty():
    ms = MemoryService()
    ms._next_attempt_at = float("inf")   # failed init, retry not due yet
    ms._available = False
    assert await ms.search("anything") == []


# ── fact_line ────────────────────────────────────────────────────────────

def test_fact_line_recorded_only():
    line = fact_line({"fact": "Client prefers morning deliveries",
                      "valid_at": None, "created_at": "2026-03-14T09:00:00+00:00"})
    assert line == "Client prefers morning deliveries [recorded 2026-03-14]"


def test_fact_line_valid_from_only():
    line = fact_line({"fact": "Rate is 5%", "valid_at": "2026-01-01T00:00:00+00:00",
                      "created_at": None})
    assert line == "Rate is 5% [valid from 2026-01-01]"


def test_fact_line_both_dates():
    line = fact_line({"fact": "X", "valid_at": "2026-01-01T00:00:00+00:00",
                      "created_at": "2026-02-02T00:00:00+00:00"})
    assert line == "X [valid from 2026-01-01, recorded 2026-02-02]"


def test_fact_line_no_dates_has_no_stamp():
    assert fact_line({"fact": "Plain fact", "valid_at": None, "created_at": None}) == "Plain fact"
    assert fact_line({"fact": "Plain fact"}) == "Plain fact"


# ── entity_context consumes the dicts ────────────────────────────────────

async def test_entity_context_renders_fact_lines():
    fake = FakeGraphiti(results=[
        edge(fact="Acme pays net-30", created_at=datetime(2026, 3, 14, tzinfo=timezone.utc)),
    ])
    ms = _service(fake)
    brief = await ms.entity_context("Acme")
    assert "What we know about Acme:" in brief
    assert "- Acme pays net-30 [recorded 2026-03-14]" in brief


# ── recipe selection (graph_search_recipe setting, edge layer) ───────────

def test_phase_b_setting_defaults():
    """Pins the shipped defaults themselves (every behavior test above injects
    the setting, so without this a config.py default flip goes undetected).
    rrf, not mmr: graphiti's mmr reranker returns zero edges on FalkorDB, so
    an mmr default silently blanks ALL graph retrieval (shipped that way once)."""
    from app.config import Settings
    s = Settings(_env_file=None)
    assert s.graph_search_recipe == "rrf"
    assert s.context_token_budget == 1500


async def test_mmr_recipe_selected(monkeypatch):
    monkeypatch.setattr(memoryservice, "get_settings", lambda: _settings("mmr"))
    fake = FakeGraphiti(results=[edge(fact="a")])
    ms = _service(fake)
    out = await ms.search("q", limit=3)
    assert [f["fact"] for f in out] == ["a"]
    assert len(fake.calls) == 1 and _reranker(fake.calls[0]) == "mmr"


async def test_mmr_does_not_mutate_shared_recipe(monkeypatch):
    from graphiti_core.search.search_config_recipes import EDGE_HYBRID_SEARCH_MMR
    original_limit = EDGE_HYBRID_SEARCH_MMR.limit
    monkeypatch.setattr(memoryservice, "get_settings", lambda: _settings("mmr"))
    ms = _service(FakeGraphiti(results=[]))
    await ms.search("q", limit=99)
    assert EDGE_HYBRID_SEARCH_MMR.limit == original_limit


async def test_rrf_recipe(monkeypatch):
    monkeypatch.setattr(memoryservice, "get_settings", lambda: _settings("rrf"))
    fake = FakeGraphiti(results=[edge(fact="a")])
    ms = _service(fake)
    out = await ms.search("q", limit=5, scope=["global"])
    assert [f["fact"] for f in out] == ["a"]
    assert len(fake.calls) == 1 and _reranker(fake.calls[0]) == "reciprocal_rank_fusion"


async def test_unknown_recipe_falls_back_to_rrf(monkeypatch):
    monkeypatch.setattr(memoryservice, "get_settings", lambda: _settings("bananas"))
    fake = FakeGraphiti(results=[edge(fact="a")])
    ms = _service(fake)
    out = await ms.search("q")
    assert [f["fact"] for f in out] == ["a"]
    assert _reranker(fake.calls[0]) == "reciprocal_rank_fusion"


async def test_cross_encoder_recipe_selected(monkeypatch):
    monkeypatch.setattr(memoryservice, "get_settings", lambda: _settings("cross_encoder"))
    fake = FakeGraphiti(results=[edge(fact="a")])
    ms = _service(fake)
    out = await ms.search("q", limit=4)
    assert [f["fact"] for f in out] == ["a"]
    assert _reranker(fake.calls[0]) == "cross_encoder"


async def test_empty_recipe_result_falls_back_to_rrf(monkeypatch):
    """mmr yielding zero facts (the FalkorDB failure mode) must not blank
    retrieval when the rrf path has answers."""
    monkeypatch.setattr(memoryservice, "get_settings", lambda: _settings("mmr"))
    fake = FakeGraphiti(results=[edge(fact="a")])
    orig = fake.run_search

    async def empty_for_mmr(query, config, gid, center, qvec, driver):
        res = await orig(query, config, gid, center, qvec, driver)
        if config.edge_config.reranker.value == "mmr":
            return SimpleNamespace(edges=[], nodes=[], episodes=[])
        return res

    ms = _service(fake)
    ms._run_search = empty_for_mmr
    out = await ms.search("q", limit=4)
    assert [f["fact"] for f in out] == ["a"]     # rescued by rrf
    assert [_reranker(c) for c in fake.calls] == ["mmr", "reciprocal_rank_fusion"]


async def test_cross_encoder_failure_falls_back_to_rrf(monkeypatch):
    monkeypatch.setattr(memoryservice, "get_settings", lambda: _settings("cross_encoder"))
    fake = FakeGraphiti(results=[edge(fact="a")],
                        recipe_error=RuntimeError("no reranker credentials"))
    ms = _service(fake)
    out = await ms.search("q", limit=4)
    assert [f["fact"] for f in out] == ["a"]           # rescued by rrf
    assert [_reranker(c) for c in fake.calls] == ["cross_encoder", "reciprocal_rank_fusion"]


# ── center_node_uuid forwarding ──────────────────────────────────────────

async def test_center_node_uses_node_distance_on_rrf(monkeypatch):
    monkeypatch.setattr(memoryservice, "get_settings", lambda: _settings("rrf"))
    fake = FakeGraphiti(results=[])
    ms = _service(fake)
    await ms.search("q", center_node_uuid="node-123")
    assert fake.calls[0]["center"] == "node-123"
    assert _reranker(fake.calls[0]) == "node_distance"


async def test_center_node_forwarded_on_recipe_path(monkeypatch):
    monkeypatch.setattr(memoryservice, "get_settings", lambda: _settings("mmr"))
    fake = FakeGraphiti(results=[])
    ms = _service(fake)
    await ms.search("q", center_node_uuid="node-123")
    assert fake.calls[0]["center"] == "node-123"
    assert _reranker(fake.calls[0]) == "mmr"


async def test_center_node_only_for_global_when_several_scopes():
    fake = FakeGraphiti(results=[])
    ms = _service(fake)
    await ms.search("q", scope=["chat_x", "global"], center_node_uuid="node-123")
    centers = {c["group"]: c["center"] for c in fake.calls}
    assert centers == {"chat_x": None, "global": "node-123"}


async def test_center_node_omitted_when_none():
    fake = FakeGraphiti(results=[])
    ms = _service(fake)
    await ms.search("q")
    assert fake.calls[0]["center"] is None


# ── resolve_person_node ──────────────────────────────────────────────────

async def test_resolve_person_node_exact_case_insensitive_match():
    fake = FakeGraphiti(nodes=[
        SimpleNamespace(name="Ravi Kumar", uuid="u-ravi"),
    ])
    ms = _service(fake)
    assert await ms.resolve_person_node("ravi kumar") == "u-ravi"
    cfg = fake.recipe_calls[0]["config"]
    assert cfg.node_config is not None  # node search, not edge search


async def test_resolve_person_node_caches_hits():
    fake = FakeGraphiti(nodes=[SimpleNamespace(name="Ravi", uuid="u-1")])
    ms = _service(fake)
    assert await ms.resolve_person_node("Ravi") == "u-1"
    assert await ms.resolve_person_node("RAVI") == "u-1"
    assert len(fake.recipe_calls) == 1  # second call served from cache


async def test_resolve_person_node_no_fuzzy_match_returns_none():
    fake = FakeGraphiti(nodes=[SimpleNamespace(name="Ravindra", uuid="u-2")])
    ms = _service(fake)
    assert await ms.resolve_person_node("Ravi") is None
    # a confirmed miss is negative-cached: the second call within the TTL is
    # served from the miss cache instead of re-searching per message
    assert await ms.resolve_person_node("Ravi") is None
    assert len(fake.recipe_calls) == 1


async def test_resolve_person_node_miss_cache_expires(monkeypatch):
    """The negative cache has a TTL — the graph may learn the person later, so
    misses retry periodically (never permanently latched)."""
    fake = FakeGraphiti(nodes=[SimpleNamespace(name="Ravindra", uuid="u-2")])
    ms = _service(fake)
    assert await ms.resolve_person_node("Ravi") is None
    assert len(fake.recipe_calls) == 1
    # age the recorded miss past the TTL
    memoryservice._person_node_miss_cache["ravi"] -= (
        memoryservice.PERSON_NODE_MISS_TTL_SECONDS + 1)
    fake.nodes.append(SimpleNamespace(name="Ravi", uuid="u-9"))
    assert await ms.resolve_person_node("Ravi") == "u-9"
    assert len(fake.recipe_calls) == 2
    assert "ravi" not in memoryservice._person_node_miss_cache  # hit clears it


async def test_resolve_person_node_error_is_not_negative_cached():
    """Transient graph errors must not suppress retries the way a confirmed
    no-match does."""
    fake = FakeGraphiti(recipe_error=RuntimeError("blip"))
    ms = _service(fake)
    assert await ms.resolve_person_node("Ravi") is None
    assert "ravi" not in memoryservice._person_node_miss_cache
    assert await ms.resolve_person_node("Ravi") is None
    assert len(fake.recipe_calls) == 2  # retried, not cached


async def test_resolve_person_node_fails_soft():
    ms = _service(FakeGraphiti(recipe_error=RuntimeError("graph down")))
    assert await ms.resolve_person_node("Ravi") is None

    unavailable = MemoryService()
    unavailable._next_attempt_at = float("inf")   # failed init, retry not due
    unavailable._available = False
    assert await unavailable.resolve_person_node("Ravi") is None


async def test_resolve_person_node_empty_name_returns_none():
    fake = FakeGraphiti(nodes=[SimpleNamespace(name="", uuid="u-3")])
    ms = _service(fake)
    assert await ms.resolve_person_node("") is None
    assert await ms.resolve_person_node("   ") is None
    assert fake.recipe_calls == []


async def test_module_level_resolve_person_node_delegates(monkeypatch):
    fake = FakeGraphiti(nodes=[SimpleNamespace(name="Ravi", uuid="u-1")])
    monkeypatch.setattr(memoryservice, "memory_graph", _service(fake))
    assert await memoryservice.resolve_person_node("Ravi") == "u-1"


# ── FalkorDB one-graph-per-group (the read path that silently broke) ──────
#
# Writes clone the driver to the group's graph, but graphiti only fans reads
# out when MORE THAN ONE group_id is passed — a single-element scope queried
# the empty default graph and every retrieval fail-softed to []. Since Quick
# 014 recall searches each scope graph separately on its own cloned driver.

class FakeFalkorDriver:          # "falkor" in the type name is the trigger
    def __init__(self, graphs=None):
        self.clones = []
        graphs = graphs if graphs is not None else ["global", "chat_x", "case_42"]
        self.client = SimpleNamespace(list_graphs=lambda: list(graphs))

    def clone(self, database):
        self.clones.append(database)
        return f"driver:{database}"


async def test_each_scope_on_falkor_uses_its_own_cloned_driver():
    fake = FakeGraphiti(results=[edge(fact="a")])
    fake.driver = FakeFalkorDriver()
    ms = _service(fake)
    out = await ms.search("q", scope=["chat_x"], limit=3)
    assert [f["fact"] for f in out] == ["a"]
    assert fake.driver.clones == ["chat_x"]
    assert fake.calls[0]["driver"] == "driver:chat_x"
    await ms.search("q", scope=["case_42", "global"])
    assert {c["group"]: c["driver"] for c in fake.calls[1:]} == {
        "case_42": "driver:case_42", "global": "driver:global"}


async def test_group_drivers_are_cached():
    """Every new FalkorDriver schedules a full index build; the status poll
    and every search used to clone afresh."""
    fake = FakeGraphiti(results=[])
    fake.driver = FakeFalkorDriver()
    ms = _service(fake)
    await ms.search("q", scope=["chat_x"])
    await ms.search("q", scope=["chat_x"])
    assert fake.driver.clones == ["chat_x"]
    await ms.close()
    assert ms._drivers == {}


async def test_non_falkor_driver_is_never_cloned():
    fake = FakeGraphiti(results=[])
    fake.driver = SimpleNamespace()      # e.g. Neo4j: one shared graph
    ms = _service(fake)
    await ms.search("q", scope=["global"])
    assert fake.calls[0]["driver"] is None


async def test_episode_vector_search_query(monkeypatch):
    seen = {}

    class VecFalkorDriver:
        async def execute_query(self, cypher, **params):
            seen["cypher"], seen["params"] = cypher, params
            return ([{"uuid": "ep9", "content": "fuel drop window", "score": 0.8}], [], None)

    ms = MemoryService()
    rows = await ms._episode_vector_search(VecFalkorDriver(), [0.5, 0.5], 12)
    assert rows[0]["uuid"] == "ep9"
    assert "vec.cosineDistance(e.ic_embedding" in seen["cypher"]
    assert seen["params"]["min"] == memoryservice.EPISODE_MIN_SCORE
    assert seen["params"]["n"] == 12


async def test_resolve_person_node_searches_the_global_graph():
    fake = FakeGraphiti(nodes=[SimpleNamespace(name="Ravi", uuid="u-1")])
    fake.driver = FakeFalkorDriver()
    ms = _service(fake)
    assert await ms.resolve_person_node("Ravi") == "u-1"
    assert fake.recipe_calls[0]["group_ids"] == [memoryservice.GLOBAL_GROUP]
    assert fake.driver.clones == [memoryservice.GLOBAL_GROUP]


async def test_recent_episodes_sweeps_every_group_graph():
    from datetime import datetime as dt

    fake = FakeGraphiti()
    fake.driver = FakeFalkorDriver()
    # legacy/system graphs must be filtered out, not browsed
    fake.driver.client = SimpleNamespace(
        list_graphs=lambda: ["global", "chat_x", "case_7", "default_db", "telemetry"])
    seen = []

    async def retrieve_episodes(reference_time, last_n, group_ids=None, driver=None):
        seen.append((tuple(group_ids or []), driver))
        gid = group_ids[0]
        return [SimpleNamespace(
            uuid=f"u-{gid}", content=f"c-{gid}", source_description="onboarding",
            group_id=gid, valid_at=None,
            created_at=dt(2026, 1, 1, tzinfo=timezone.utc))]

    fake.retrieve_episodes = retrieve_episodes
    ms = _service(fake)
    eps = await ms.recent_episodes(limit=10)

    assert {g for g, _ in seen} == {("global",), ("chat_x",), ("case_7",)}
    assert all(d == f"driver:{g[0]}" for g, d in seen)   # each query on its own graph
    assert {e["group_id"] for e in eps} == {"global", "chat_x", "case_7"}


async def test_recent_episodes_fails_soft():
    fake = FakeGraphiti()
    fake.driver = FakeFalkorDriver()
    fake.driver.client = SimpleNamespace(list_graphs=lambda: ["global"])

    async def retrieve_episodes(**kw):
        raise RuntimeError("falkordb down")

    fake.retrieve_episodes = retrieve_episodes
    ms = _service(fake)
    assert await ms.recent_episodes() == []

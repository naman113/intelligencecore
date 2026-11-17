"""Quick 025 — the nightly community rebuild can no longer hang the app.

On 2026-10-26 the client's nightly consolidation froze the API for 1 h 40 min:
graphiti-core 0.29.2's label_propagation is a `while True` loop with
synchronous label updates and no iteration cap, so two entities joined by
two or more RELATES_TO edges swap labels forever, on the event-loop thread.

Pure logic (no FalkorDB, no LLM, no DB). Proves:
  * the installed graphiti function really never converges on that pair
    (the bug this patch exists for; if a graphiti bump fixes it, this test
    says so and the patch can be reviewed for removal)
  * the bounded replacement terminates on it, and on the cap path returns
    the clusters as they stand and logs a warning with the node count
  * where graphiti's own function converges, the clusters are identical
    (simple shapes and seeded random graphs); where it would loop forever,
    the clusters are the sensible ones
  * the patch is installed where build_communities looks it up, idempotently
  * the clustering runs in a worker thread, so the event loop keeps serving
  * the off-loop copy of get_community_clusters matches the pinned graphiti
  * the rebuild and every nightly step log how long they took
"""

import asyncio
import importlib.metadata
import logging
import random
import threading
import time
from collections import defaultdict
from types import SimpleNamespace

import pytest

from app import consolidation, memoryservice
from app.memoryservice import MemoryService, bounded_label_propagation

from graphiti_core.driver.operations import graph_utils
from graphiti_core.utils.maintenance import community_operations
from graphiti_core.utils.maintenance.community_operations import Neighbor


# ── helpers ─────────────────────────────────────────────────────────────────

class _TooManyRounds(Exception):
    pass


class _RoundCountingProjection(dict):
    """graphiti's label_propagation calls projection.items() once per round;
    stop it after `cap` rounds so a non-terminating run fails fast instead
    of hanging the test session."""

    def __init__(self, data, cap):
        super().__init__(data)
        self.cap = cap
        self.rounds = 0

    def items(self):
        self.rounds += 1
        if self.rounds > self.cap:
            raise _TooManyRounds(self.rounds)
        return super().items()


def _graphiti_original():
    memoryservice.install_community_patch()
    return memoryservice._GRAPHITI_ORIGINALS["label_propagation"]


def _graphiti_capped(projection, cap=2000):
    """graphiti 0.29.2's own result, or None when it is still changing
    labels after `cap` rounds (i.e. it would never return)."""
    try:
        return _graphiti_original()(_RoundCountingProjection(projection, cap))
    except _TooManyRounds:
        return None


def _projection(edges, order=None):
    """Undirected weighted edges -> a graphiti projection (both directions),
    keys in `order` (graphiti builds it ORDER BY uuid DESC)."""
    nodes = order or sorted({u for e in edges for u in e[:2]}, reverse=True)
    proj = {u: [] for u in nodes}
    for a, b, w in edges:
        proj[a].append(Neighbor(node_uuid=b, edge_count=w))
        if a != b:
            proj[b].append(Neighbor(node_uuid=a, edge_count=w))
    return proj


def _partition(clusters):
    return frozenset(frozenset(c) for c in clusters)


def _covers_once(clusters, projection):
    members = [u for c in clusters for u in c]
    return sorted(members) == sorted(projection) and all(clusters)


def _random_projection(rng):
    n = rng.randint(2, 40)
    p = rng.choice([0.03, 0.06, 0.1, 0.2])
    w_max = rng.choice([1, 1, 2, 3])
    names = [f"u{i:03d}" for i in range(n)]
    edges = [(names[i], names[j], rng.randint(1, w_max))
             for i in range(n) for j in range(i + 1, n) if rng.random() < p]
    order = names[:]
    rng.shuffle(order)
    return _projection(edges, order)


# ── the bug ─────────────────────────────────────────────────────────────────

def test_graphiti_0_29_2_label_propagation_never_converges_on_a_parallel_edge_pair():
    # Two entities, two facts between them (edge_count 2): each round they
    # swap labels. 1000 rounds on a 2-node graph is "never". Both of
    # graphiti's copies (build_communities' and the drivers') loop.
    pair = _projection([("a", "b", 2)])
    with pytest.raises(_TooManyRounds):
        _graphiti_original()(_RoundCountingProjection(pair, 1000))
    with pytest.raises(_TooManyRounds):
        memoryservice._GRAPHITI_ORIGINALS["graph_utils.label_propagation"](
            _RoundCountingProjection(pair, 1000))


# ── the bounded replacement ─────────────────────────────────────────────────

def test_parallel_edge_pair_terminates_as_one_cluster():
    memoryservice.install_community_patch()
    pair = _projection([("a", "b", 2)])
    started = time.monotonic()
    clusters = community_operations.label_propagation(pair)   # what graphiti calls
    assert time.monotonic() - started < 1
    assert _partition(clusters) == {frozenset({"a", "b"})}


def test_bipartite_and_hub_shapes_terminate():
    # A 4-cycle with weight 2 (bipartite) and a hub with weight-2 spokes:
    # both oscillate forever in graphiti 0.29.2.
    cycle = _projection([("a", "b", 2), ("b", "c", 2), ("c", "d", 2), ("d", "a", 2)])
    hub = _projection([("h", s, 2) for s in "pqrs"])
    for proj in (cycle, hub):
        assert _graphiti_capped(proj) is None
        clusters = bounded_label_propagation(proj)
        assert _covers_once(clusters, proj)
        assert len(clusters) == 1


def test_cap_returns_the_clusters_as_they_stand_and_warns(caplog):
    # A 300-node weight-1 chain in shuffled order needs many rounds; at a
    # 2-round cap the clustering stops, says so, and still returns every
    # node exactly once.
    names = [f"c{i:03d}" for i in range(300)]
    order = names[:]
    random.Random(3).shuffle(order)
    chain = _projection([(names[i], names[i + 1], 1) for i in range(299)], order)
    with caplog.at_level(logging.WARNING, logger="memoryservice"):
        clusters = bounded_label_propagation(chain, max_rounds=2)
    assert _covers_once(clusters, chain)
    assert len(clusters) > 1
    warning = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("300 nodes" in m and "2-round cap" in m for m in warning), warning


def test_default_cap_is_100_rounds():
    assert memoryservice.COMMUNITY_MAX_ROUNDS == 100


def test_empty_and_isolated_nodes():
    assert bounded_label_propagation({}) == []
    lonely = {"x": [], "y": []}
    assert _partition(bounded_label_propagation(lonely)) == {frozenset({"x"}), frozenset({"y"})}


def test_unknown_neighbour_is_ignored_not_a_crash():
    # graphiti raises KeyError here; a neighbour outside the projection
    # (another group's node) simply does not vote.
    proj = {"a": [Neighbor(node_uuid="b", edge_count=1),
                  Neighbor(node_uuid="ghost", edge_count=5)],
            "b": [Neighbor(node_uuid="a", edge_count=1)]}
    clusters = bounded_label_propagation(proj)
    assert _covers_once(clusters, proj)


def test_deterministic():
    rng = random.Random(11)
    for _ in range(50):
        proj = _random_projection(rng)
        assert bounded_label_propagation(proj) == bounded_label_propagation(dict(proj))


# ── same partitions where graphiti converges ────────────────────────────────

@pytest.mark.parametrize("edges", [
    # two triangles joined by one weak link
    [("a", "b", 1), ("b", "c", 1), ("c", "a", 1), ("c", "d", 1),
     ("d", "e", 1), ("e", "f", 1), ("f", "d", 1)],
    # a star of single facts
    [("h", s, 1) for s in "pqrst"],
    # disjoint single-fact pairs
    [("a", "b", 1), ("c", "d", 1), ("e", "f", 1)],
    # a weight-1 path
    [("a", "b", 1), ("b", "c", 1), ("c", "d", 1), ("d", "e", 1)],
    # two triangles of double facts joined by one fact
    [("a", "b", 2), ("b", "c", 2), ("c", "a", 2), ("c", "d", 1),
     ("d", "e", 2), ("e", "f", 2), ("f", "d", 2)],
    # two 4-cliques joined by one fact (pure in-place sweeps from the start
    # merge these into one community; graphiti's rounds keep them apart)
    [(x, y, 1) for g in ("abcd", "efgh") for i, x in enumerate(g) for y in g[i + 1:]]
    + [("d", "e", 1)],
])
@pytest.mark.parametrize("ascending", [False, True])
def test_same_partition_as_graphiti_on_simple_shapes(edges, ascending):
    names = sorted({u for e in edges for u in e[:2]}, reverse=not ascending)
    proj = _projection(edges, names)
    theirs = _graphiti_capped(proj)
    assert theirs is not None, "graphiti converges on this shape"
    assert _partition(bounded_label_propagation(proj)) == _partition(theirs)


def test_shapes_graphiti_loops_on_get_sensible_clusters():
    # A triangle with one heavy pair and a pendant entity: graphiti loops
    # forever; the pendant (one fact) stays out of the triangle.
    proj = _projection([("a", "b", 3), ("b", "c", 1), ("c", "a", 1), ("c", "d", 1)])
    assert _graphiti_capped(proj) is None
    assert _partition(bounded_label_propagation(proj)) == {frozenset("abc"), frozenset("d")}


def test_random_graphs_identical_where_graphiti_converges():
    # Seeded random graphs (2-40 nodes, fact weights 1-3, shuffled order).
    # Where graphiti's synchronous loop converges, the clusters are exactly
    # graphiti's. Where it would loop forever (a large share: two entities
    # with two facts between them are common), ours ends without the cap.
    rng = random.Random(25)
    converged = looping = 0
    for _ in range(600):
        proj = _random_projection(rng)
        ours = bounded_label_propagation(proj)
        assert _covers_once(ours, proj)
        theirs = _graphiti_capped(proj, cap=500)
        if theirs is None:
            looping += 1
            continue
        converged += 1
        assert _partition(ours) == _partition(theirs), proj
    assert converged > 200 and looping > 100, (converged, looping)


def test_random_graphs_never_reach_the_cap(caplog):
    rng = random.Random(7)
    with caplog.at_level(logging.WARNING, logger="memoryservice"):
        for _ in range(600):
            bounded_label_propagation(_random_projection(rng))
    assert not [r for r in caplog.records if "cap" in r.getMessage()]


# ── installation ────────────────────────────────────────────────────────────

def test_patch_is_installed_where_graphiti_looks_it_up():
    memoryservice.install_community_patch()
    memoryservice.install_community_patch()          # idempotent
    from graphiti_core.driver.falkordb.operations import graph_ops as falkor_ops

    assert community_operations.label_propagation is bounded_label_propagation
    assert graph_utils.label_propagation is bounded_label_propagation
    assert falkor_ops.label_propagation is bounded_label_propagation
    assert community_operations.get_community_clusters is memoryservice._community_clusters_off_loop
    originals = memoryservice._GRAPHITI_ORIGINALS
    assert originals["label_propagation"].__module__ == community_operations.__name__
    assert originals["graph_utils.label_propagation"].__module__ == graph_utils.__name__
    assert originals["get_community_clusters"].__module__ == community_operations.__name__


def test_first_install_says_so_once(monkeypatch, caplog):
    memoryservice.install_community_patch()
    monkeypatch.setattr(community_operations, "get_community_clusters",
                        memoryservice._GRAPHITI_ORIGINALS["get_community_clusters"])
    with caplog.at_level(logging.INFO, logger="memoryservice"):
        assert memoryservice.install_community_patch() is True
        assert memoryservice.install_community_patch() is True     # reconnect: quiet
    said = [r.getMessage() for r in caplog.records if "Quick 025" in r.getMessage()]
    assert len(said) == 1 and "100 rounds" in said[0], said


def test_graphiti_build_communities_uses_the_module_lookup():
    # graphiti 0.29.2 build_communities -> get_community_clusters -> label_propagation,
    # each a module-global lookup at call time (FalkorDriver has no
    # graph_operations_interface), so replacing the module attributes takes effect.
    import inspect
    src = inspect.getsource(community_operations.build_communities)
    assert "await get_community_clusters(driver, group_ids)" in src
    src = inspect.getsource(memoryservice._GRAPHITI_ORIGINALS["get_community_clusters"])
    assert "cluster_uuids = label_propagation(projection)" in src
    from graphiti_core.driver.falkordb_driver import FalkorDriver
    assert FalkorDriver.graph_operations_interface is None


def test_off_loop_copy_matches_the_pinned_graphiti():
    # _community_clusters_off_loop is graphiti-core's get_community_clusters
    # copied from this exact version. Bumping graphiti-core must re-diff it
    # (and re-check whether upstream fixed label_propagation) before this
    # constant moves.
    assert importlib.metadata.version("graphiti-core") == memoryservice.GRAPHITI_COMMUNITY_COPY_OF


def test_version_mismatch_keeps_the_bounded_function_but_not_the_copy(monkeypatch, caplog):
    memoryservice.install_community_patch()
    monkeypatch.setattr(community_operations, "get_community_clusters",
                        memoryservice._GRAPHITI_ORIGINALS["get_community_clusters"])
    monkeypatch.setattr(memoryservice, "GRAPHITI_COMMUNITY_COPY_OF", "0.0.0")
    with caplog.at_level(logging.WARNING, logger="memoryservice"):
        memoryservice.install_community_patch()
    assert community_operations.label_propagation is bounded_label_propagation
    assert community_operations.get_community_clusters is \
        memoryservice._GRAPHITI_ORIGINALS["get_community_clusters"]
    assert any("event loop" in r.getMessage() for r in caplog.records)


# ── off the event loop ──────────────────────────────────────────────────────

class _ClusterDriver:
    """Just enough of a graphiti driver for get_community_clusters."""
    graph_operations_interface = None

    def __init__(self, provider, groups, edges):
        self.provider = provider
        self.groups = groups
        self.edges = edges

    async def execute_query(self, cypher, **params):
        if "collect(DISTINCT n.group_id)" in cypher:
            return ([{"group_ids": list(self.groups)}], None, None)
        uuid = params["uuid"]
        rows = defaultdict(int)
        for a, b in self.edges:
            if a == uuid:
                rows[b] += 1
            elif b == uuid:
                rows[a] += 1
        return ([{"uuid": u, "count": c} for u, c in rows.items()], None, None)


async def test_clusters_are_computed_in_a_worker_thread_while_the_loop_serves(monkeypatch):
    from graphiti_core.driver.driver import GraphProvider
    from graphiti_core.nodes import EntityNode

    memoryservice.install_community_patch()
    groups = {"global": ["a", "b", "c"]}

    async def by_group_ids(driver, group_ids, *a, **k):
        return [SimpleNamespace(uuid=u) for g in group_ids for u in groups[g]]

    async def by_uuids(driver, uuids):
        return [SimpleNamespace(uuid=u) for u in uuids]

    monkeypatch.setattr(EntityNode, "get_by_group_ids", by_group_ids)
    monkeypatch.setattr(EntityNode, "get_by_uuids", by_uuids)

    loop_thread = threading.get_ident()
    seen = {}
    real = memoryservice.bounded_label_propagation

    def slow_spy(projection, *a, **k):
        seen["thread"] = threading.get_ident()
        seen["projection"] = {u: [(n.node_uuid, n.edge_count) for n in ns]
                              for u, ns in projection.items()}
        time.sleep(0.3)                  # CPU-heavy clustering stand-in
        return real(projection, *a, **k)

    monkeypatch.setattr(memoryservice, "bounded_label_propagation", slow_spy)

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    tick_task = asyncio.create_task(ticker())
    driver = _ClusterDriver(GraphProvider.FALKORDB, ["global"],
                            [("a", "b"), ("a", "b"), ("b", "c")])
    try:
        clusters = await community_operations.get_community_clusters(driver, None)
    finally:
        tick_task.cancel()

    assert seen["thread"] != loop_thread
    assert ticks >= 10, ticks            # the loop kept running during the 0.3 s
    assert seen["projection"]["a"] == [("b", 2)]
    assert sorted(seen["projection"]["b"]) == [("a", 2), ("c", 1)]
    # graphiti swaps a and b forever here; c hangs on by one fact.
    assert _partition([[n.uuid for n in c] for c in clusters]) == {frozenset("ab"),
                                                                 frozenset("c")}


async def test_build_communities_end_to_end_on_the_pair_with_fake_clients(monkeypatch):
    # graphiti's real build_communities over a fake driver: the pair that
    # hung the client becomes one community and the call returns.
    from graphiti_core.driver.driver import GraphProvider
    from graphiti_core.nodes import EntityNode

    memoryservice.install_community_patch()

    async def by_group_ids(driver, group_ids, *a, **k):
        return [SimpleNamespace(uuid=u) for u in ("a", "b")]

    async def by_uuids(driver, uuids):
        return [EntityNode(uuid=u, name=u, group_id="global", summary=f"{u} summary")
                for u in uuids]

    monkeypatch.setattr(EntityNode, "get_by_group_ids", by_group_ids)
    monkeypatch.setattr(EntityNode, "get_by_uuids", by_uuids)

    class _LLM:
        async def generate_response(self, *a, **k):
            return {"summary": "a and b", "description": "pair"}

    driver = _ClusterDriver(GraphProvider.FALKORDB, ["global"], [("a", "b"), ("a", "b")])
    nodes, edges = await asyncio.wait_for(
        community_operations.build_communities(driver, _LLM(), None), 10)
    assert len(nodes) == 1 and len(edges) == 2


# ── timing logs ─────────────────────────────────────────────────────────────

async def test_memoryservice_build_communities_logs_its_duration(monkeypatch, caplog):
    svc = MemoryService()

    async def ok():
        return True

    async def build(*a, **k):
        return (["c1", "c2"], ["e"])

    monkeypatch.setattr(svc, "_ensure", ok)
    svc._graphiti = SimpleNamespace(build_communities=build)
    with caplog.at_level(logging.INFO, logger="memoryservice"):
        assert await svc.build_communities() is True
    line = [r.getMessage() for r in caplog.records if "build_communities" in r.getMessage()]
    assert line and "2 communities" in line[0] and " s" in line[0], line


async def test_nightly_logs_each_step_start_and_duration(monkeypatch, caplog):
    async def quick():
        return 0

    for attr in ("consolidate_learnings", "rebuild_communities", "prune_episodes",
                 "retention", "history_retention", "supersede_bad_recipes",
                 "graph_backfill"):
        monkeypatch.setattr(consolidation, attr, quick)
    with caplog.at_level(logging.INFO, logger="consolidation"):
        await consolidation.run_nightly()
    msgs = [r.getMessage() for r in caplog.records]
    assert any("communities_rebuilt" in m and "starting" in m for m in msgs), msgs
    assert any("communities_rebuilt" in m and "took" in m for m in msgs), msgs

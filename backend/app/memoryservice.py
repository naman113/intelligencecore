"""MemoryService — the ONLY module that imports Graphiti.

The ambient system accumulates business knowledge in a temporal knowledge graph
(Graphiti on FalkorDB, local Docker). Everything goes through this interface so
the backend can change later without touching callers:

    add_episode(text, source, occurred_at, case_id=None, group_id=None)
    recall(query, scope=None, limit=8, ...) -> list[dict]   # facts + entities + episodes
    search(query, scope=None, limit=8, center_node_uuid=None) -> list[dict]
                                               # agents: recall minus superseded facts
                                               # {"fact", "valid_at", "created_at", "kind", ...}
    fact_line(f) -> str                        # render one fact dict for a prompt
    entity_context(name) -> str                # a short brief about one actor/entity
    resolve_person_node(name) -> str | None    # graph node uuid for a person name
    find_learning_episodes(group_id, prefix, contents)  # Quick 019: a learning's episodes
    retire_episodes(group_id, episode_uuids)   # Quick 019: take episodes, and what only
                                               # they support, out of a scope graph

Quick 014 (connected graph): add_episode passes business entity and relation
types (graph_ontology.py) so extraction links machines, rules, event types,
tables and cases; it skips an episode whose exact text is already in that
scope's graph and stores an embedding of the episode text (ic_embedding) so
episodes are searchable by meaning. recall() is the ONE retrieval for agents
and the Memory page: per scope graph it ranks facts (edges), entities (name
and summary) and episodes (fulltext plus that embedding) with one query
embedding, and merges them by weighted reciprocal-rank fusion. Extraction
outcomes are counted and shown on status() (failures are never silent).

The edge layer's strategy is configurable via the graph_search_recipe setting:
'rrf' (default, graphiti's basic hybrid search), 'mmr' (diverse results —
but broken on FalkorDB in the pinned graphiti: zero edges back), or
'cross_encoder' (LLM rerank). A recipe that errors OR returns no facts falls
back to rrf rather than blanking retrieval.

Design rules:
- FAIL-SOFT. If graphiti isn't installed, FalkorDB is down, or a call errors,
  we log and degrade to a no-op (writes) / empty (reads). The reactive pipeline
  and the ambient loops must never crash because the graph is unavailable.
- Self-healing init (Quick 009). The Graphiti client + indices are built on
  first use or by run_forever at startup. A failed attempt is retried with
  backoff (RETRY_BACKOFF_SECONDS, then every 300 s), never latched for the
  process lifetime; run_forever also pings a connected graph so a stopped
  FalkorDB shows as "not connected" and reconnects on its own. Between retries
  every call is a fast no-op, so a downed FalkorDB is not hammered per episode.
  status() is the one truthful answer to "is the graph up" (Memory and Health).
- Embeddings default to OpenAI (OPENAI_API_KEY is already configured); the LLM
  client is Anthropic when ANTHROPIC_API_KEY is set, else OpenAI.

Graphiti's `group_id` partitions the graph; we use it as a lightweight scope
(e.g. "case:42" or "global") so retrieval can be narrowed cheaply.
"""

import asyncio
import inspect
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timezone

from .config import get_settings

log = logging.getLogger("memoryservice")

GLOBAL_GROUP = "global"

# Quick 014: retrieval and graph-read knobs.
RRF_K = 10                       # reciprocal-rank fusion constant (small = top ranks dominate)
KIND_WEIGHT = {"fact": 1.0, "episode": 1.0, "entity": 0.85}
EPISODE_MIN_SCORE = 0.62         # (1 + cosine) / 2, graphiti's scale; ~cosine 0.24
EPISODE_EMBED_CHARS = 6000       # episode text embedded for ic_embedding
AGENT_EPISODE_CHARS = 700        # agents get long episodes clipped (search())
GRAPH_READ_MAX = 2000            # hard row cap on any graph-view read

# Quick 009: retry a failed init after 30, 60, 120, 240 s, then every 300 s.
RETRY_BACKOFF_SECONDS = (30, 60, 120, 240, 300)
WATCH_SECONDS = 30          # run_forever cadence: ping when up, retry when due
INIT_TIMEOUT_SECONDS = 30   # a black-holed host must not stall a write for minutes
PING_TIMEOUT_SECONDS = 5
REASON_MAX = 240

# Quick 009: graph extraction models, pinned (see _llm_client); the settings
# graphiti_openai_model / graphiti_openai_small_model override them from Setup.
# gpt-5.5 is graphiti's own default main model. The small model runs edge
# dedupe and contradiction checks: on p13 gpt-4o-mini invalidated a true fact
# ("Northwind ships Pune orders with BlueDart") in 2 of 2 probes when an
# overlapping episode arrived, gpt-5.5 in 0 of 2. The reranker (cross_encoder
# recipe only) needs logprobs, which reasoning models lack, so it stays on
# gpt-4o-mini. The embedder is pinned to memory.py's model.
GRAPHITI_OPENAI_MODEL = "gpt-5.5"
GRAPHITI_OPENAI_SMALL_MODEL = "gpt-5.5"
GRAPHITI_RERANK_MODEL = "gpt-4o-mini"
GRAPHITI_EMBEDDING_MODEL = "text-embedding-3-small"
# Quick 022: graph extraction model on the opt-in Anthropic path (setting graph_model).
GRAPH_MODEL = "claude-sonnet-5-5"


def _models(settings) -> tuple[str, str]:
    main = str(getattr(settings, "graphiti_openai_model", "") or "").strip()
    small = str(getattr(settings, "graphiti_openai_small_model", "") or "").strip()
    return main or GRAPHITI_OPENAI_MODEL, small or GRAPHITI_OPENAI_SMALL_MODEL

# Retrieval recipes search() understands; anything else degrades to "rrf".
_KNOWN_RECIPES = ("mmr", "rrf", "cross_encoder")

# resolve_person_node caches: lowercased name -> node uuid for hits (kept for
# the process lifetime), and lowercased name -> monotonic timestamp for misses.
# A miss may become a hit once the graph ingests more episodes, so misses are
# only suppressed for PERSON_NODE_MISS_TTL_SECONDS — without that, a sender
# with no graph node would pay a full node search on every single message.
_person_node_cache: dict[str, str] = {}
_person_node_miss_cache: dict[str, float] = {}
PERSON_NODE_MISS_TTL_SECONDS = 300.0


def _iso(value) -> str | None:
    """Coerce a graphiti timestamp attr (datetime | str | None) to ISO str | None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def fact_line(f: dict) -> str:
    """Render one search() fact dict as a prompt line, with a temporal stamp.

    e.g. "Client prefers morning deliveries [recorded 2026-03-14]"; valid_at is
    shown as "valid from ..." when present; no stamp when neither date exists."""
    fact = str(f.get("fact") or "").strip()
    stamps = []
    if f.get("valid_at"):
        stamps.append(f"valid from {str(f['valid_at'])[:10]}")
    if f.get("created_at"):
        stamps.append(f"recorded {str(f['created_at'])[:10]}")
    if stamps:
        return f"{fact} [{', '.join(stamps)}]"
    return fact


def case_group(case_id: int) -> str:
    """Graphiti group_ids must be alphanumeric/dash/underscore only — no colons."""
    return f"case_{case_id}"


def chat_group(jid: str) -> str:
    """Memory scope for a chat, so what a group is about survives past any one
    case. A jid ("1203…@g.us") carries '@' and '.', which Graphiti rejects, so
    every non-alphanumeric character collapses to an underscore."""
    safe = "".join(ch if ch.isalnum() else "_" for ch in (jid or ""))
    return f"chat_{safe}"


def _backoff(failures: int) -> float:
    """Seconds to wait after the Nth consecutive failed init (N >= 1)."""
    i = max(1, int(failures)) - 1
    return float(RETRY_BACKOFF_SECONDS[min(i, len(RETRY_BACKOFF_SECONDS) - 1)])


def _reason(exc: BaseException) -> str:
    """One short line for the status surfaces: exception class plus the first
    line of its message. FalkorDB/graphiti errors carry host:port and Cypher,
    never credentials (the driver takes no password here)."""
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    text = f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__
    return text[:REASON_MAX]


def _is_connection_error(exc: BaseException) -> bool:
    """A failure that means the store went away (not a bad query or an LLM
    error during extraction). redis-py's ConnectionError/TimeoutError do not
    subclass the builtins, so match on the class name too."""
    if isinstance(exc, (ConnectionError, OSError, asyncio.TimeoutError)):
        return True
    names = {c.__name__ for c in type(exc).__mro__}
    return bool(names & {"ConnectionError", "TimeoutError", "BusyLoadingError"})


def _utc_iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


# ── Quick 014: recall plumbing (pure, tested without a graph) ────────────────

def _search_config(recipe: str, limit: int, center: str | None):
    """One graphiti SearchConfig for a scope: facts by the configured recipe
    (node distance when centred on a node and the recipe is rrf), entities and
    episodes by fulltext (+ embedding for entities), each reranked by rrf."""
    from graphiti_core.search.search_config import (
        EdgeReranker, EdgeSearchConfig, EdgeSearchMethod, EpisodeReranker,
        EpisodeSearchConfig, EpisodeSearchMethod, NodeReranker, NodeSearchConfig,
        NodeSearchMethod, SearchConfig,
    )
    reranker = {"mmr": EdgeReranker.mmr,
                "cross_encoder": EdgeReranker.cross_encoder}.get(recipe, EdgeReranker.rrf)
    if center and reranker == EdgeReranker.rrf:
        reranker = EdgeReranker.node_distance
    return SearchConfig(
        edge_config=EdgeSearchConfig(
            search_methods=[EdgeSearchMethod.bm25, EdgeSearchMethod.cosine_similarity],
            reranker=reranker),
        node_config=NodeSearchConfig(
            search_methods=[NodeSearchMethod.bm25, NodeSearchMethod.cosine_similarity],
            reranker=NodeReranker.rrf),
        episode_config=EpisodeSearchConfig(
            search_methods=[EpisodeSearchMethod.bm25], reranker=EpisodeReranker.rrf),
        limit=int(limit),
    )


def _get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def entity_type(labels) -> str:
    """The business type of an entity node: its first label other than
    Entity (graph_ontology types), else "Entity"."""
    for lab in labels or []:
        if lab and lab != "Entity":
            return str(lab)
    return "Entity"


def recall_item(kind: str, gid: str, obj) -> dict | None:
    """One ranked graphiti object (edge, entity node, episode node or an
    episode row) as a recall dict. None when it carries no text."""
    item = {"kind": kind, "group_id": gid, "uuid": str(_get(obj, "uuid", "") or ""),
            "valid_at": _iso(_get(obj, "valid_at")),
            "created_at": _iso(_get(obj, "created_at")),
            "invalid_at": None, "superseded": False, "name": "", "labels": [],
            "source": ""}
    if kind == "fact":
        text = _get(obj, "fact") or _get(obj, "name")
        item["name"] = str(_get(obj, "name", "") or "")
        item["invalid_at"] = _iso(_get(obj, "invalid_at"))
        item["superseded"] = (_get(obj, "invalid_at") is not None
                              or _get(obj, "expired_at") is not None)
        item["source_uuid"] = str(_get(obj, "source_node_uuid", "") or "")
        item["target_uuid"] = str(_get(obj, "target_node_uuid", "") or "")
    elif kind == "entity":
        name = str(_get(obj, "name", "") or "").strip()
        summary = str(_get(obj, "summary", "") or "").strip()
        labels = [str(x) for x in (_get(obj, "labels") or [])]
        item.update(name=name, labels=labels, type=entity_type(labels))
        text = f"{name}: {summary}" if summary and name else (name or summary)
    else:
        text = _get(obj, "content")
        item["name"] = str(_get(obj, "name", "") or "")
        item["source"] = str(_get(obj, "source_description", "") or "")
    text = str(text or "").strip()
    if not text:
        return None
    item["fact"] = text
    return item


# Quick 019: an entity summary rebuilt from its remaining facts (retire_episodes).
SUMMARY_FACTS = 8
SUMMARY_CHARS = 600


def summary_from_facts(facts: list[str]) -> str:
    """Distinct fact texts, newest first, joined into one summary of at most
    SUMMARY_CHARS (whole facts only, except a first fact longer than the cap).
    "" when nothing remains. Pure."""
    out: list[str] = []
    seen: set[str] = set()
    size = 0
    for f in facts or []:
        text = " ".join(str(f or "").split())
        key = text.lower()
        if not text or key in seen:
            continue
        add = len(text) + (1 if out else 0)
        if out and size + add > SUMMARY_CHARS:
            break
        seen.add(key)
        out.append(text)
        size += add
    return " ".join(out)[:SUMMARY_CHARS]


def _merge_ranked(lists: list[list]) -> list:
    """Reciprocal-rank fusion of several rankings of the same kind of object
    (keyed by uuid, else text) into one ranking."""
    scores: dict[str, float] = {}
    first: dict[str, object] = {}
    for objs in lists:
        for rank, obj in enumerate(objs or []):
            key = str(_get(obj, "uuid", "") or _get(obj, "content", "") or id(obj))
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
            first.setdefault(key, obj)
    return [first[k] for k in sorted(first, key=lambda k: -scores[k])]


def _fuse(ranked: list[tuple[str, str, list]], limit: int,
          include_superseded: bool) -> list[dict]:
    """Weighted reciprocal-rank fusion of every ranked list. An item found by
    several lists (an episode by both fulltext and embedding) sums its
    scores. Exact-duplicate texts collapse to the best-ranked one."""
    scores: dict[tuple, float] = {}
    items: dict[tuple, dict] = {}
    for kind, gid, objs in ranked:
        weight = KIND_WEIGHT.get(kind, 1.0)
        for rank, obj in enumerate(objs or []):
            item = recall_item(kind, gid, obj)
            if item is None:
                continue
            key = (kind, gid, item["uuid"] or item["fact"])
            scores[key] = scores.get(key, 0.0) + weight / (RRF_K + rank + 1)
            items.setdefault(key, item)
    out: list[dict] = []
    seen: set[str] = set()
    for key in sorted(items, key=lambda k: -scores[k]):
        item = items[key]
        if item["superseded"] and not include_superseded:
            continue
        norm = " ".join(item["fact"].lower().split())
        if norm in seen:
            continue
        seen.add(norm)
        item["score"] = round(scores[key], 4)
        out.append(item)
        if len(out) >= max(1, int(limit)):
            break
    return out


# ── Quick 020: preferences are personal, so agents never read them here ──
# A preference applies only to the person who stated it (learnings.scope,
# memscope.py). Before Quick 020 every preference learning was also mirrored
# into the global graph (episode source "reflection:preference"), where every
# run recalls it. New ones are not mirrored (memory.GRAPHED_KINDS); search()
# hides what is already there: those episodes, facts whose every source
# episode is one, and entities only they mention. A rebuild removes them.
PREFERENCE_SOURCE = "reflection:preference"
PREF_CACHE_SECONDS = 300
_PREF_CACHE: dict[str, tuple[float, frozenset, frozenset]] = {}


def drop_preference_items(lists: list[tuple[str, str, list]], episodes: frozenset,
                          entities: frozenset) -> list[tuple[str, str, list]]:
    """The ranked lists without what came only from preference learnings.
    Pure. An episode is dropped by its source alone, so the raw preference
    text never reaches an agent even when the uuid lookup failed."""
    out = []
    for kind, gid, objs in lists:
        if kind == "episode":
            objs = [o for o in objs or []
                    if str(_get(o, "source_description", "") or "") != PREFERENCE_SOURCE
                    and str(_get(o, "uuid", "") or "") not in episodes]
        elif kind == "fact":
            def _only_preference(o) -> bool:
                srcs = [str(x) for x in (_get(o, "episodes") or [])]
                return bool(srcs) and all(x in episodes for x in srcs)
            objs = [o for o in objs or [] if not _only_preference(o)]
        elif kind == "entity":
            objs = [o for o in objs or [] if str(_get(o, "uuid", "") or "") not in entities]
        out.append((kind, gid, objs))
    return out
# end Quick 020


# ── Quick 025: graph community clustering that always ends ─────────────────
# graphiti-core 0.29.2's label_propagation (utils/maintenance/
# community_operations.py:93, and its twin in driver/operations/
# graph_utils.py:27) is `while True` with SYNCHRONOUS updates and no cap: two
# entities joined by two or more facts (edge_count >= 2) swap labels every
# round, forever, on the event-loop thread. That froze the client's API for
# 1 h 40 min during the 2026-10-26 nightly rebuild. Upstream still has the
# loop (0.31.2 and main on 2026-10-26; issues #402, #1355, #1953, every fix
# PR unmerged), so a version bump does not help.
#
# install_community_patch() swaps in bounded_label_propagation where graphiti
# looks it up, and replaces get_community_clusters with a copy that runs it in
# a worker thread. It runs from _connect, before the Graphiti client exists.
# Graphiti's own graph reads stay on the event loop.
COMMUNITY_MAX_ROUNDS = 100
GRAPHITI_COMMUNITY_COPY_OF = "0.29.2"   # _community_clusters_off_loop copied from this
_GRAPHITI_ORIGINALS: dict = {}
_LABEL_PROPAGATION_MODULES = (           # every graphiti module holding the name
    "graphiti_core.driver.operations.graph_utils",     # first: later importers bind ours
    "graphiti_core.utils.maintenance.community_operations",
    "graphiti_core.driver.falkordb.operations.graph_ops",
    "graphiti_core.driver.neo4j.operations.graph_ops",
    "graphiti_core.driver.kuzu.operations.graph_ops",
    "graphiti_core.driver.neptune.operations.graph_ops",
)


def bounded_label_propagation(projection: dict, max_rounds: int = COMMUNITY_MAX_ROUNDS
                              ) -> list[list[str]]:
    """graphiti's label propagation with the same contract (projection
    {uuid: [Neighbor(node_uuid, edge_count)]} in, uuid clusters out), the
    same rule (a node takes the community its links weigh most towards, ties
    to the higher label; below weight 2 it keeps the higher of that and its
    own) and graphiti's own synchronous rounds, so wherever graphiti's loop
    converges the clusters are identical. Two changes:

    * when a synchronous round reproduces an earlier state (graphiti's loop
      would cycle forever: the two-entity swap) or half of `max_rounds` has
      passed, the remaining rounds update labels in place, so a node sees
      its neighbours' new labels within the same sweep (the asynchronous
      update of Raghavan et al. 2007, NetworkX's asyn_lpa) - that ends the
      swap. The sweep visits nodes from the highest label down (graphiti
      builds the projection ORDER BY uuid DESC; this is its reverse), so
      the higher label that wins ties spreads within one sweep;
    * at most `max_rounds` rounds; on the cap it logs a warning and returns
      the clusters as they stand.

    Deterministic. A neighbour missing from the projection does not vote
    (graphiti raises KeyError). Pure CPU over local data, so it is safe to
    run in a worker thread."""
    started = time.monotonic()
    uuids = list(projection)
    index = {u: i for i, u in enumerate(uuids)}
    labels = list(range(len(uuids)))
    adjacency: list[list[tuple[int, int]]] = []
    links = 0
    for u in uuids:
        row = [(index[n.node_uuid], n.edge_count)
               for n in projection[u] or () if n.node_uuid in index]
        links += len(row)
        adjacency.append(row)

    def vote(i: int) -> int:             # graphiti 0.29.2's rule, line for line
        votes: dict[int, int] = defaultdict(int)
        for j, weight in adjacency[i]:
            votes[labels[j]] += weight
        rank, best = max((w, label) for label, w in votes.items()) if votes else (0, -1)
        current = labels[i]
        return best if best != -1 and rank > 1 else max(best, current)

    seen = {hash(tuple(labels))}
    in_place_from = 0                    # round the in-place sweeps took over
    rounds, converged = 0, not uuids
    while not converged and rounds < max_rounds:
        rounds += 1
        if not in_place_from:
            new = [vote(i) for i in range(len(uuids))]
            converged = new == labels
            labels = new
            state = hash(tuple(labels))
            if not converged and (state in seen or rounds >= max_rounds // 2):
                in_place_from = rounds + 1
            seen.add(state)
            continue
        changed = False
        for i in range(len(uuids) - 1, -1, -1):
            new_label = vote(i)
            if new_label != labels[i]:
                labels[i] = new_label
                changed = True
        converged = not changed

    clusters: dict[int, list[str]] = defaultdict(list)
    for u, label in zip(uuids, labels):
        clusters[label].append(u)
    elapsed_ms = (time.monotonic() - started) * 1000
    how = f", in place from round {in_place_from}" if in_place_from else ""
    if not converged:
        log.warning("community clustering stopped at the %d-round cap without "
                    "converging (%d nodes, %d links, %d clusters%s, %.0f ms); keeping "
                    "the clusters as they stand", max_rounds, len(uuids), links,
                    len(clusters), how, elapsed_ms)
    else:
        log.info("community clustering: %d nodes, %d links -> %d clusters in %d "
                 "rounds%s, %.0f ms", len(uuids), links, len(clusters), rounds, how,
                 elapsed_ms)
    return list(clusters.values())


async def _community_clusters_off_loop(driver, group_ids):
    """graphiti-core 0.29.2 community_operations.get_community_clusters
    (lines 30-90), copied as is except for one line: label propagation runs
    in a worker thread (asyncio.to_thread), so the API keeps answering while
    a large graph clusters. The graph reads stay on the event loop."""
    from graphiti_core.driver.driver import GraphProvider
    from graphiti_core.helpers import semaphore_gather
    from graphiti_core.nodes import EntityNode
    from graphiti_core.utils.maintenance.community_operations import Neighbor

    if driver.graph_operations_interface:
        return await _GRAPHITI_ORIGINALS["get_community_clusters"](driver, group_ids)

    community_clusters: list = []

    if group_ids is None:
        group_id_values, _, _ = await driver.execute_query(
            """
            MATCH (n:Entity)
            WHERE n.group_id IS NOT NULL
            RETURN
                collect(DISTINCT n.group_id) AS group_ids
            """
        )

        group_ids = group_id_values[0]['group_ids'] if group_id_values else []

    for group_id in group_ids:
        projection: dict = {}
        nodes = await EntityNode.get_by_group_ids(driver, [group_id])
        for node in nodes:
            match_query = """
                MATCH (n:Entity {group_id: $group_id, uuid: $uuid})-[e:RELATES_TO]-(m: Entity {group_id: $group_id})
            """
            if driver.provider == GraphProvider.KUZU:
                match_query = """
                MATCH (n:Entity {group_id: $group_id, uuid: $uuid})-[:RELATES_TO]-(e:RelatesToNode_)-[:RELATES_TO]-(m: Entity {group_id: $group_id})
                """
            records, _, _ = await driver.execute_query(
                match_query
                + """
                WITH count(e) AS count, m.uuid AS uuid
                RETURN
                    uuid,
                    count
                """,
                uuid=node.uuid,
                group_id=group_id,
            )

            projection[node.uuid] = [
                Neighbor(node_uuid=record['uuid'], edge_count=record['count']) for record in records
            ]

        # Quick 025: the one changed line (was: label_propagation(projection)).
        cluster_uuids = await asyncio.to_thread(bounded_label_propagation, projection)

        community_clusters.extend(
            list(
                await semaphore_gather(
                    *[EntityNode.get_by_uuids(driver, cluster) for cluster in cluster_uuids]
                )
            )
        )

    return community_clusters


def install_community_patch() -> bool:
    """Point graphiti's community clustering at the bounded version. Every
    graphiti module that holds label_propagation gets bounded_label_propagation
    (graph_utils first, so a driver module imported later binds ours), and
    on the pinned graphiti version get_community_clusters becomes the
    off-loop copy. Idempotent; never raises. True when both are in place."""
    try:
        import importlib
        import importlib.metadata
        import sys

        from graphiti_core.driver.operations import graph_utils
        from graphiti_core.utils.maintenance import community_operations as co

        importlib.import_module("graphiti_core.driver.falkordb.operations.graph_ops")
        for key, value in (("label_propagation", co.label_propagation),
                           ("graph_utils.label_propagation", graph_utils.label_propagation),
                           ("get_community_clusters", co.get_community_clusters)):
            if getattr(value, "__module__", "").startswith("graphiti_core"):
                _GRAPHITI_ORIGINALS.setdefault(key, value)     # graphiti's, never ours
        for name in _LABEL_PROPAGATION_MODULES:
            module = sys.modules.get(name)
            if module is not None and hasattr(module, "label_propagation"):
                module.label_propagation = bounded_label_propagation

        version = importlib.metadata.version("graphiti-core")
        if version != GRAPHITI_COMMUNITY_COPY_OF:
            log.warning("graphiti-core %s is not %s, the version the off-loop community "
                        "clustering was copied from: clustering stays bounded but runs on "
                        "the event loop", version, GRAPHITI_COMMUNITY_COPY_OF)
            return False
        if co.get_community_clusters is not _community_clusters_off_loop:
            co.get_community_clusters = _community_clusters_off_loop
            log.info("graph community clustering bounded (Quick 025): at most %d rounds, "
                     "in a worker thread", COMMUNITY_MAX_ROUNDS)
        return True
    except Exception:
        log.warning("graph community clustering patch not installed", exc_info=True)
        return False
# end Quick 025


class MemoryService:
    """Fail-soft wrapper over a Graphiti temporal knowledge graph."""

    def __init__(self) -> None:
        self._graphiti = None
        self._init_lock = asyncio.Lock()
        self._available = False
        # Quick 009: retry state instead of a one-shot latch.
        self._failures = 0               # consecutive failed init attempts
        self._next_attempt_at = 0.0      # monotonic; no init attempt before this
        self._last_error = ""            # why it is not connected (short line)
        self._last_attempt: datetime | None = None
        self._connected_since: datetime | None = None
        # Quick 014: extraction outcomes since start, so a failing add_episode
        # shows on Memory and Health instead of only in the log.
        self._extraction: dict = {
            "ok": 0, "failed": 0, "duplicates": 0,
            "last_error": "", "last_error_at": None, "last_ok_at": None,
            "last": None,          # {"name", "group_id", "entities", "facts"}
        }
        self._drivers: dict[tuple, object] = {}   # (id(driver), group) -> clone

    # ── lifecycle ────────────────────────────────────────────────────────

    async def _ensure(self) -> bool:
        """Lazily construct the Graphiti client + indices. Returns availability.

        After a failed attempt every call is a fast no-op until the backoff
        window passes (30 s doubling to 300 s), then the next call retries.
        Never raises.
        """
        settings = get_settings()
        if not settings.memory_graph_enabled:
            return False
        if self._available:
            return True
        if time.monotonic() < self._next_attempt_at:
            return False

        async with self._init_lock:
            if self._available:
                return True
            if time.monotonic() < self._next_attempt_at:
                return False
            return await self._connect(settings)

    async def _connect(self, settings) -> bool:
        """One init attempt (caller holds _init_lock). Never raises."""
        self._last_attempt = datetime.now(timezone.utc)
        graphiti = None
        try:
            from graphiti_core import Graphiti
            from graphiti_core.driver.falkordb_driver import FalkorDriver

            install_community_patch()      # Quick 025: before any build_communities
            # Quick 043: password only when set; unset keeps the exact old call.
            kw = {"password": settings.falkordb_password} if settings.falkordb_password else {}
            driver = FalkorDriver(host=settings.falkordb_host,
                                  port=str(settings.falkordb_port), **kw)
            graphiti = Graphiti(graph_driver=driver,
                                llm_client=self._llm_client(settings),
                                **self._openai_parts(settings))  # Quick 005 ledger
            await asyncio.wait_for(graphiti.build_indices_and_constraints(),
                                   INIT_TIMEOUT_SECONDS)
            self._graphiti = graphiti
            self._available = True
            self._failures = 0
            self._next_attempt_at = 0.0
            self._last_error = ""
            self._connected_since = datetime.now(timezone.utc)
            log.info("MemoryService: Graphiti graph connected (FalkorDB %s:%s)",
                     settings.falkordb_host, settings.falkordb_port)
        except Exception as e:
            # Import error, connection refused, driver/server mismatch — degrade,
            # and schedule the next attempt instead of giving up for good.
            if isinstance(e, asyncio.TimeoutError):
                e = TimeoutError(f"no answer from FalkorDB at {settings.falkordb_host}:"
                                 f"{settings.falkordb_port} within {INIT_TIMEOUT_SECONDS} s")
            self._record_failure(_reason(e))
            log.warning("MemoryService: graph unavailable, running memory-less "
                        "(reflection still writes pgvector learnings); retrying in "
                        "%d s: %s", int(_backoff(self._failures)), self._last_error,
                        exc_info=self._failures == 1)
            if graphiti is not None:
                try:
                    await graphiti.close()
                except Exception:
                    log.debug("closing the failed Graphiti client errored", exc_info=True)
        return self._available

    def _record_failure(self, reason: str) -> None:
        self._drivers.clear()
        self._failures += 1
        self._available = False
        self._graphiti = None
        self._connected_since = None
        self._last_error = reason or "unknown error"
        self._next_attempt_at = time.monotonic() + _backoff(self._failures)

    async def _mark_down(self, reason: str) -> None:
        """A connected graph stopped answering: drop the client so the next
        call (after the first backoff step) reconnects. Never raises."""
        if not self._available:
            return
        graphiti = self._graphiti
        log.warning("MemoryService: graph connection lost (%s); retrying in %d s",
                    reason, int(_backoff(self._failures + 1)))
        self._record_failure(reason)
        if graphiti is not None:
            try:
                await graphiti.close()
            except Exception:
                log.debug("closing the lost Graphiti client errored", exc_info=True)

    async def _note_error(self, exc: BaseException) -> None:
        """Called from a failed read/write: a connection-type error means the
        store went away, anything else (bad query, LLM hiccup) leaves it up."""
        if _is_connection_error(exc):
            await self._mark_down(_reason(exc))

    async def ping(self) -> bool:
        """Cheap liveness probe of a connected graph (GRAPH.LIST). Marks the
        graph down when it fails. False when not connected. Never raises."""
        if not self._available or self._graphiti is None:
            return False
        try:
            d = getattr(self._graphiti, "driver", None)
            client = getattr(d, "client", None)
            if client is not None and hasattr(client, "list_graphs"):
                res = client.list_graphs()
                if inspect.isawaitable(res):
                    await asyncio.wait_for(res, PING_TIMEOUT_SECONDS)
            elif d is not None and hasattr(d, "health_check"):
                await asyncio.wait_for(d.health_check(), PING_TIMEOUT_SECONDS)
            return True
        except Exception as e:
            if isinstance(e, asyncio.TimeoutError):
                e = TimeoutError(f"no answer within {PING_TIMEOUT_SECONDS} s")
            await self._mark_down(_reason(e))
            return False

    async def reconnect(self) -> bool:
        """Operator action: drop any client and try again right now."""
        await self.close()
        return await self._ensure()

    async def watch_once(self) -> bool:
        """One run_forever tick: ping when connected, (re)connect when due."""
        if not get_settings().memory_graph_enabled:
            return False
        if self._available:
            return await self.ping()
        return await self._ensure()

    async def run_forever(self) -> None:
        """Never-die loop (worker.py convention): connect at startup, notice a
        lost connection within WATCH_SECONDS, reconnect on the backoff
        schedule. Nothing awaits this task, so nothing may escape it."""
        log.info("memory graph watch started")
        while True:
            try:
                await self.watch_once()
            except Exception:
                log.exception("memory graph watch tick failed (continuing)")
            await asyncio.sleep(WATCH_SECONDS)

    async def status(self, *, probe: bool = True) -> dict:
        """The truthful answer for Memory and Health. state is one of
        connected | empty | not_connected | disabled. With probe=True a
        connected graph is pinged first and a due retry is attempted, so the
        page never shows a stale "connected". Never raises."""
        settings = get_settings()
        out = {
            "state": "disabled", "reason": "",
            "host": f"{settings.falkordb_host}:{settings.falkordb_port}",
            "connected_since": None, "last_attempt": _utc_iso(self._last_attempt),
            "next_retry_in": None, "failures": self._failures,
            "episodes": None, "scopes": None,
            "entities": None, "facts": None,
            "extraction": dict(self._extraction),
        }
        try:
            if not settings.memory_graph_enabled:
                out["reason"] = "turned off on the Setup page (memory_graph_enabled)"
                return out
            if probe:
                await self.watch_once()
            out["last_attempt"] = _utc_iso(self._last_attempt)
            out["failures"] = self._failures
            if not self._available:
                out["state"] = "not_connected"
                out["reason"] = self._last_error or "not tried yet"
                wait = self._next_attempt_at - time.monotonic()
                out["next_retry_in"] = max(0, int(wait)) if self._failures else 0
                return out
            out["connected_since"] = _utc_iso(self._connected_since)
            counts = await self.graph_counts()
            if counts is None:
                out["state"] = "connected"
            else:
                out["episodes"] = sum(c["episodes"] for c in counts.values())
                out["entities"] = sum(c["entities"] for c in counts.values())
                out["facts"] = sum(c["facts"] for c in counts.values())
                out["scopes"] = len([g for g, c in counts.items() if c["episodes"]])
                out["state"] = "connected" if out["episodes"] else "empty"
            out["extraction"] = dict(self._extraction)
            return out
        except Exception as e:
            log.warning("MemoryService.status failed", exc_info=True)
            out["state"] = "not_connected"
            out["reason"] = _reason(e)
            return out

    @staticmethod
    def _llm_client(settings):
        """LLM client for graph extraction. Defaults ('auto'/'openai', or no Anthropic
        key) to Graphiti's OpenAI client with the pinned graphiti_openai_* models (its
        reference path; Quick 022 measured Haiku 5.5 below it: true facts invalidated).
        graphiti_llm 'anthropic' is opt-in: graph_model (claude-sonnet-5-5) through
        graphiti-core's Anthropic client on a sanitizing, ledger-wrapped SDK client
        (llmclient.graph_anthropic: no temperature, forced tool only where accepted)."""
        if settings.graphiti_llm == "anthropic" and settings.anthropic_api_key:
            try:
                from graphiti_core.llm_client.anthropic_client import AnthropicClient
                from graphiti_core.llm_client.config import LLMConfig
                from . import llmclient
                model = (str(getattr(settings, "graph_model", "") or "").strip()
                         or GRAPH_MODEL)
                return AnthropicClient(config=LLMConfig(api_key=settings.anthropic_api_key,
                                                        model=model, small_model=model),
                                       client=llmclient.graph_anthropic(  # Quick 005/022
                                           settings.anthropic_api_key))
            except Exception:
                log.warning("MemoryService: Anthropic LLM client init failed; "
                            "falling back to OpenAI", exc_info=True)
        # Quick 009: never Graphiti's bare OpenAI default — its small model is
        # gpt-4.1-nano, which OpenAI now rejects (404 "deprecated"), so every
        # entity-summary step of add_episode failed. Quick 005: the client is
        # the ledger-wrapped one, so graph extraction shows in the usage ledger.
        from graphiti_core.llm_client.config import LLMConfig
        from graphiti_core.llm_client.openai_client import OpenAIClient
        main, small = _models(settings)
        return OpenAIClient(config=LLMConfig(api_key=settings.openai_api_key or None,
                                             model=main, small_model=small),
                            client=MemoryService._ledger_openai(settings))

    @staticmethod
    def _ledger_openai(settings):
        """The OpenAI client Graphiti would build itself, wrapped for the usage ledger
        (usage.py). Same key resolution: the setting, else OPENAI_API_KEY."""
        from openai import AsyncOpenAI
        from . import usage
        return usage.wrap_openai(AsyncOpenAI(api_key=settings.openai_api_key or None),
                                 role="memory_graph")

    @staticmethod
    def _cross_encoder(settings):
        """The cross_encoder search recipe's reranker (Graphiti's default is
        gpt-4.1-nano, retired). Needs logprobs, so a non-reasoning model."""
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
        from graphiti_core.llm_client.config import LLMConfig
        return OpenAIRerankerClient(
            config=LLMConfig(api_key=settings.openai_api_key or None, model=GRAPHITI_RERANK_MODEL),
            client=MemoryService._ledger_openai(settings))

    @staticmethod
    def _embedder(settings):
        """Embeddings for graph nodes and edges: same model as the pgvector
        learnings (memory.EMBEDDING_MODEL), with the configured OpenAI key."""
        from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
        return OpenAIEmbedder(
            config=OpenAIEmbedderConfig(api_key=settings.openai_api_key or None,
                                        embedding_model=GRAPHITI_EMBEDDING_MODEL),
            client=MemoryService._ledger_openai(settings))

    @staticmethod
    def _openai_parts(settings) -> dict:
        """Embedder and reranker on the ledger-wrapped client (Quick 005), with
        Quick 009's pinned models."""
        return {"embedder": MemoryService._embedder(settings),
                "cross_encoder": MemoryService._cross_encoder(settings)}

    async def close(self) -> None:
        self._drivers.clear()
        graphiti, self._graphiti = self._graphiti, None
        if graphiti is not None:
            try:
                await graphiti.close()
            except Exception:
                log.debug("MemoryService close error", exc_info=True)
        # Clear the retry window too, so a Setup save or the Reconnect button
        # tries again immediately instead of waiting out the backoff.
        self._available = False
        self._connected_since = None
        self._failures = 0
        self._next_attempt_at = 0.0

    # ── writes ───────────────────────────────────────────────────────────

    async def add_episode(self, text: str, source: str,
                          occurred_at: datetime | None = None,
                          case_id: int | None = None,
                          group_id: str | None = None,
                          name: str | None = None) -> bool:
        """Ingest one episode (a fact/observation) into the graph. Never raises.

        group_id scopes the episode; defaults to case:<id> when a case is given,
        else the global partition. Returns True if it was stored, or if the
        exact same text is already in that scope's graph (Quick 014: identical
        episodes are stored once). Extraction uses the business entity and
        relation types (graph_ontology); every outcome is counted for
        status(), and a failure is also recorded in job_runs."""
        text = (text or "").strip()
        if not text:
            return False
        if not await self._ensure():
            return False

        gid = group_id or (case_group(case_id) if case_id else GLOBAL_GROUP)
        when = occurred_at or datetime.now(timezone.utc)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        ep_name = name or f"{source}:{when.isoformat()}"
        if await self.has_episode(gid, content=text):
            self._extraction["duplicates"] += 1
            log.info("MemoryService.add_episode: identical episode already in %s; "
                     "not stored again (name=%s)", gid, ep_name)
            return True
        try:
            from graphiti_core.nodes import EpisodeType
            from . import graph_ontology
            result = await self._graphiti.add_episode(
                name=ep_name,
                episode_body=text,
                source=EpisodeType.text,
                source_description=source,
                reference_time=when,
                group_id=gid,
                entity_types=graph_ontology.ENTITY_TYPES,
                edge_types=graph_ontology.EDGE_TYPES,
                custom_extraction_instructions=graph_ontology.EXTRACTION_INSTRUCTIONS,
            )
        except Exception as e:
            log.warning("MemoryService.add_episode failed (source=%s)", source, exc_info=True)
            await self._extraction_failed(e, ep_name, gid)
            await self._note_error(e)
            return False
        nodes = list(getattr(result, "nodes", None) or [])
        edges = list(getattr(result, "edges", None) or [])
        self._extraction.update(
            ok=self._extraction["ok"] + 1, last_ok_at=_utc_iso(datetime.now(timezone.utc)),
            last={"name": ep_name, "group_id": gid,
                  "entities": len(nodes), "facts": len(edges)})
        log.info("MemoryService.add_episode %s in %s: %d entities, %d facts",
                 ep_name, gid, len(nodes), len(edges))
        episode = getattr(result, "episode", None)
        if getattr(episode, "uuid", None):
            await self._set_episode_embedding(gid, str(episode.uuid), text)
        return True

    async def _extraction_failed(self, exc: BaseException, name: str, gid: str) -> None:
        """Count a failed extraction, keep the reason for status(), and record
        it in job_runs (System > Runs). Never raises."""
        self._extraction.update(
            failed=self._extraction["failed"] + 1, last_error=_reason(exc),
            last_error_at=_utc_iso(datetime.now(timezone.utc)))
        try:
            from . import jobruns
            await jobruns.record("memory_graph", "graph extraction", status="failed",
                                 summary=f"episode {name} in {gid} was not stored",
                                 error=exc, ref=("graph", gid), collapse="replace")
        except Exception:
            log.debug("recording the extraction failure failed", exc_info=True)

    async def _embed(self, text: str) -> list[float] | None:
        """One embedding through graphiti's (ledger-wrapped) embedder."""
        embedder = getattr(self._graphiti, "embedder", None)
        if embedder is None:
            return None
        vec = await embedder.create(input_data=[(text or "").replace("\n", " ")])
        return [float(x) for x in vec] if vec else None

    async def _set_episode_embedding(self, gid: str, uuid: str, text: str) -> bool:
        """Store the episode text's embedding on its Episodic node
        (ic_embedding), so recall() finds episodes by meaning, not only by
        words. Fail-soft: an episode without one is still found by fulltext."""
        driver = self._group_driver(gid)
        if driver is None:
            return False
        try:
            vec = await self._embed(text[:EPISODE_EMBED_CHARS])
            if not vec:
                return False
            await driver.execute_query(
                "MATCH (e:Episodic {uuid: $uuid}) SET e.ic_embedding = vecf32($vec)",
                uuid=uuid, vec=vec)
            return True
        except Exception as e:
            log.warning("episode embedding for %s in %s failed", uuid, gid, exc_info=True)
            await self._note_error(e)
            return False

    async def embed_missing_episodes(self, limit: int = 200) -> int:
        """Give episodes written before Quick 014 (or whose embedding failed)
        their ic_embedding. Returns how many were embedded. Never raises."""
        names = await self._our_graph_names()
        done = 0
        for gid in names or []:
            rows = await self._graph_query(
                gid, "MATCH (e:Episodic) WHERE e.ic_embedding IS NULL "
                     "RETURN e.uuid AS uuid, e.content AS content LIMIT $n",
                n=max(1, int(limit) - done))
            for r in rows or []:
                if r.get("uuid") and await self._set_episode_embedding(
                        gid, str(r["uuid"]), str(r.get("content") or "")):
                    done += 1
            if done >= limit:
                break
        return done

    # ── maintenance (nightly consolidation) ──────────────────────────────

    async def build_communities(self) -> bool:
        """Rebuild graph community nodes (graphiti clears existing communities
        and re-clusters the whole graph). Fail-soft: False when the graph is
        unavailable or the rebuild errors; never raises."""
        if not await self._ensure():
            return False
        started = time.monotonic()
        try:
            result = await self._graphiti.build_communities()
        except Exception:
            log.warning("MemoryService.build_communities failed after %.1f s",
                        time.monotonic() - started, exc_info=True)
            return False
        built = len(result[0]) if isinstance(result, tuple) and result else 0
        log.info("MemoryService.build_communities: %d communities in %.1f s",
                 built, time.monotonic() - started)   # Quick 025
        return True

    async def remove_episodes_before(self, cutoff: datetime,
                                     limit: int = 100) -> int:
        """Remove up to `limit` raw episodes whose reference time is <= cutoff.

        Enumerates via graphiti.retrieve_episodes(reference_time=cutoff) — in
        the pinned 0.29.2 that returns the most recent `last_n` episodes valid
        at or before the cutoff, across all groups when group_ids is None —
        then removes them one at a time so a single bad episode doesn't stop
        the rest. Fail-soft: 0 on any enumeration error; never raises.

        NB: graphiti's remove_episode has a known orphan-cleanup bug (#1083),
        which is why the caller gates this behind consolidation_prune_episodes
        (default OFF)."""
        if not await self._ensure():
            return 0
        try:
            episodes = await self._graphiti.retrieve_episodes(
                reference_time=cutoff, last_n=limit)
        except Exception:
            log.warning("MemoryService.remove_episodes_before: enumeration "
                        "failed", exc_info=True)
            return 0
        removed = 0
        for ep in episodes or []:
            uuid = getattr(ep, "uuid", None)
            if not uuid:
                continue
            try:
                await self._graphiti.remove_episode(str(uuid))
                removed += 1
            except Exception:
                log.warning("MemoryService.remove_episodes_before: removing "
                            "episode %s failed", uuid, exc_info=True)
        return removed

    # ── reads ────────────────────────────────────────────────────────────
    #
    # FalkorDB keeps every group_id in its OWN graph (writes clone the driver
    # to the group's database), but graphiti only fans reads out per-group
    # when MORE THAN ONE group_id is passed. A single-element scope — or no
    # scope at all — queries the driver's default graph, which never holds
    # data here, so it silently returns nothing. Every read below therefore
    # clones the driver itself for single-group queries. This bug hid ALL
    # graph retrieval (agents included) behind fail-soft empty results.

    def _group_driver(self, group_id: str):
        """A driver bound to one group's graph, or None on non-Falkor drivers
        (there group_id is an ordinary property filter on a shared graph).
        Quick 014: clones are cached per (driver, group): every new FalkorDriver
        schedules a full index build, and the status poll alone read every
        scope several times a minute."""
        d = getattr(self._graphiti, "driver", None)
        if d is None or "falkor" not in type(d).__name__.lower():
            return None
        key = (id(d), group_id)
        cached = self._drivers.get(key)
        if cached is not None:
            return cached
        try:
            clone = d.clone(database=group_id)
        except Exception:
            log.debug("driver clone for %s failed", group_id, exc_info=True)
            return None
        if len(self._drivers) > 256:
            self._drivers.clear()
        self._drivers[key] = clone
        return clone

    async def _our_graph_names(self) -> list[str] | None:
        """Names of the scope graphs that exist right now (ours only: global,
        chat_*, case_*). None = unknown (graph down, non-Falkor driver or the
        listing failed); [] = none exist yet."""
        if not await self._ensure():
            return None
        d = getattr(self._graphiti, "driver", None)
        if d is None or "falkor" not in type(d).__name__.lower():
            return None
        try:
            names = d.client.list_graphs()
            if inspect.isawaitable(names):
                names = await names
            return sorted(str(n) for n in (names or [])
                          if n == GLOBAL_GROUP or str(n).startswith(("chat_", "case_")))
        except Exception as e:
            log.debug("group enumeration failed", exc_info=True)
            await self._note_error(e)
            return None

    async def group_ids(self) -> list[str]:
        """Every scope that currently exists in the store. FalkorDB only —
        nothing in graphiti's public API enumerates groups, so this asks the
        underlying client (version-pinned) and keeps the names that look like
        ours. [] means "unknown" (non-Falkor), not "empty"."""
        if not await self._ensure():
            return []
        d = getattr(self._graphiti, "driver", None)
        if d is None or "falkor" not in type(d).__name__.lower():
            return []
        names = await self._our_graph_names()
        return names or [GLOBAL_GROUP]

    async def recent_episodes(self, limit: int = 30) -> list[dict]:
        """The most recent raw episodes across all scopes, newest first —
        what was actually ingested (onboarding, case distillations, chat
        purposes), before graphiti's entity extraction. This is the browse
        surface for the dashboard; search() is the retrieval surface.
        Never raises; [] when the graph is unavailable."""
        if not await self._ensure():
            return []
        now = datetime.now(timezone.utc)
        raw: list = []
        try:
            groups = await self.group_ids()
            if groups:                      # falkor: one graph per group
                for gid in groups:
                    eps = await self._graphiti.retrieve_episodes(
                        reference_time=now, last_n=limit, group_ids=[gid],
                        driver=self._group_driver(gid))
                    raw.extend(eps or [])
            else:                           # shared-graph drivers
                raw = list(await self._graphiti.retrieve_episodes(
                    reference_time=now, last_n=limit) or [])
        except Exception as e:
            log.warning("MemoryService.recent_episodes failed", exc_info=True)
            await self._note_error(e)
            return []
        out = []
        for ep in raw:
            out.append({
                "uuid": str(getattr(ep, "uuid", "") or ""),
                "name": str(getattr(ep, "name", "") or ""),
                "content": str(getattr(ep, "content", "") or ""),
                "source": str(getattr(ep, "source_description", "") or ""),
                "group_id": str(getattr(ep, "group_id", "") or ""),
                "occurred_at": _iso(getattr(ep, "valid_at", None)),
                "created_at": _iso(getattr(ep, "created_at", None)),
            })
        out.sort(key=lambda e: e["created_at"] or "", reverse=True)
        return out[:limit]

    async def search(self, query: str, scope: list[str] | None = None,
                     limit: int = 8, *,
                     center_node_uuid: str | None = None) -> list[dict]:
        """What agents retrieve: recall() without superseded facts, long
        episode text clipped to AGENT_EPISODE_CHARS. Never raises; [] on any
        error.

        Each item keeps the pre-Quick-014 keys {"fact", "valid_at",
        "created_at"} (fact_line renders them) and adds "kind" (fact | entity
        | episode), "uuid", "group_id", "name", "labels", "source",
        "invalid_at", "superseded" and "score". For an entity "fact" is
        "<name>: <summary>"; for an episode it is the episode text.

        scope, if given, is a list of group_ids (e.g. ["case_42", "global"]);
        None means the global scope. center_node_uuid, if given, ranks facts
        near that graph node first (see resolve_person_node).

        Quick 020: what came only from preference learnings is left out
        (drop_preference_items): a preference is personal, the graph shared."""
        items = await self.recall(query, scope, limit,
                                  center_node_uuid=center_node_uuid,
                                  include_superseded=False,
                                  hide_preferences=True)
        for it in items:
            if it.get("kind") == "episode" and len(it["fact"]) > AGENT_EPISODE_CHARS:
                it["fact"] = it["fact"][:AGENT_EPISODE_CHARS].rstrip() + "…"
        return items

    async def recall(self, query: str, scope: list[str] | None = None,
                     limit: int = 8, *, center_node_uuid: str | None = None,
                     include_superseded: bool = False,
                     hide_preferences: bool = False) -> list[dict]:
        """Quick 014: the one retrieval over the whole graph. For every scope
        graph it ranks facts (edges: fulltext + embedding), entities (name and
        summary: fulltext + embedding) and episodes (fulltext, plus cosine on
        the ic_embedding stored at write time), all with ONE query embedding,
        then merges every ranked list by weighted reciprocal-rank fusion
        (KIND_WEIGHT, RRF_K). Superseded facts (invalid_at or expired_at set)
        are dropped unless include_superseded, in which case they come back
        flagged superseded=True. Scopes whose graph does not exist are
        skipped (reading one would create it). Never raises; [] on error."""
        query = (query or "").strip()
        if not query or not await self._ensure():
            return []
        groups = [str(g) for g in scope] if scope else [GLOBAL_GROUP]
        groups = list(dict.fromkeys(groups))
        try:
            existing = await self._our_graph_names()
            if existing is not None:
                groups = [g for g in groups if g in existing]
                if not groups:
                    return []
            qvec = await self._embed(query)
            ranked: list[tuple[str, str, list]] = []
            for gid in groups:
                center = center_node_uuid if gid == GLOBAL_GROUP or len(groups) == 1 else None
                lists = await self._group_recall(gid, query, qvec, limit, center)
                if hide_preferences:                     # Quick 020
                    eps, ents = await self._preference_only(gid)
                    lists = drop_preference_items(lists, eps, ents)
                ranked.extend(lists)
            return _fuse(ranked, limit, include_superseded)
        except Exception as e:
            log.warning("MemoryService.recall failed", exc_info=True)
            await self._note_error(e)
            return []

    async def _group_recall(self, gid: str, query: str, qvec: list[float] | None,
                            limit: int, center: str | None) -> list[tuple[str, str, list]]:
        """Ranked lists for one scope graph: [(kind, group_id, items), ...]."""
        driver = self._group_driver(gid)
        n = max(int(limit) * 2, 10)
        recipe = str(getattr(get_settings(), "graph_search_recipe", "rrf")
                     or "").strip().lower()
        if recipe not in _KNOWN_RECIPES:
            recipe = "rrf"
        try:
            res = await self._run_search(query, _search_config(recipe, n, center),
                                         gid, center, qvec, driver)
        except Exception:
            if recipe == "rrf":
                raise
            log.warning("MemoryService.recall: %s recipe failed; retrying with rrf",
                        recipe, exc_info=True)
            recipe, res = "rrf", await self._run_search(
                query, _search_config("rrf", n, center), gid, center, qvec, driver)
        edges = list(getattr(res, "edges", None) or [])
        if not edges and recipe != "rrf":
            # graphiti's mmr reranker yields zero edges on FalkorDB (candidate
            # embeddings never hydrated); an empty answer the rrf path could
            # have filled is worse than one more cheap query.
            again = await self._run_search(query, _search_config("rrf", n, center),
                                           gid, center, qvec, driver)
            edges = list(getattr(again, "edges", None) or [])
        episodes = list(getattr(res, "episodes", None) or [])
        if qvec and driver is not None:
            # Fulltext and embedding hits are ONE episode layer (fused here
            # first), so episodes do not outvote facts and entities just for
            # being searched two ways.
            episodes = _merge_ranked(
                [episodes, await self._episode_vector_search(driver, qvec, n)])
        return [("fact", gid, edges),
                ("entity", gid, list(getattr(res, "nodes", None) or [])),
                ("episode", gid, episodes)]

    async def _preference_only(self, gid: str) -> tuple[frozenset, frozenset]:
        """Quick 020: (episode uuids, entity uuids) in this scope graph that came
        only from preference learnings. Cached PREF_CACHE_SECONDS (none are
        written any more; a rebuild only removes some). Empty sets when the
        graph cannot be read (episodes are still dropped by source)."""
        now = time.monotonic()
        hit = _PREF_CACHE.get(gid)
        if hit is not None and now - hit[0] < PREF_CACHE_SECONDS:
            return hit[1], hit[2]
        rows = await self._graph_query(
            gid, "MATCH (e:Episodic) WHERE e.source_description = $src RETURN e.uuid AS uuid",
            src=PREFERENCE_SOURCE)
        if rows is None:
            return frozenset(), frozenset()
        eps = frozenset(str(_get(r, "uuid", "") or "") for r in rows) - {""}
        ents: frozenset = frozenset()
        if eps:
            erows = await self._graph_query(
                gid,
                "MATCH (e:Episodic)-[:MENTIONS]->(n:Entity) "
                "WITH n, collect(DISTINCT e.source_description) AS srcs "
                "WHERE size(srcs) = 1 AND srcs[0] = $src "
                "RETURN n.uuid AS uuid", src=PREFERENCE_SOURCE)
            if erows is None:
                return eps, frozenset()
            ents = frozenset(str(_get(r, "uuid", "") or "") for r in erows) - {""}
        _PREF_CACHE[gid] = (now, eps, ents)
        return eps, ents

    async def _run_search(self, query: str, config, gid: str, center: str | None,
                          qvec: list[float] | None, driver):
        """graphiti's search over one scope with a precomputed query vector
        (one embedding serves every scope). Raises; recall() handles it."""
        from graphiti_core.search.search import search as graphiti_search
        from graphiti_core.search.search_filters import SearchFilters
        return await graphiti_search(self._graphiti.clients, query, [gid], config,
                                     SearchFilters(), center, None,
                                     query_vector=qvec, driver=driver)

    async def _episode_vector_search(self, driver, qvec: list[float], n: int) -> list[dict]:
        """Episodes nearest the query by their ic_embedding (FalkorDB vec
        functions, same scale as graphiti's cosine search). [] on error."""
        try:
            res = await driver.execute_query(
                """MATCH (e:Episodic) WHERE e.ic_embedding IS NOT NULL
                   WITH e, (2 - vec.cosineDistance(e.ic_embedding, vecf32($vec))) / 2 AS score
                   WHERE score >= $min
                   RETURN e.uuid AS uuid, e.name AS name, e.content AS content,
                          e.source_description AS source_description,
                          e.group_id AS group_id, e.valid_at AS valid_at,
                          e.created_at AS created_at, score
                   ORDER BY score DESC LIMIT $n""",
                vec=qvec, min=EPISODE_MIN_SCORE, n=int(n))
            return list(res[0]) if res else []
        except Exception:
            log.debug("episode vector search failed", exc_info=True)
            return []

    async def _graph_query(self, group_id: str, cypher: str, **params) -> list | None:
        """Run one read query against one scope's graph. None when the graph is
        unavailable, the driver is not FalkorDB, or the query errors."""
        if not await self._ensure():
            return None
        driver = self._group_driver(group_id)
        if driver is None:
            return None
        try:
            res = await driver.execute_query(cypher, **params)
            return list(res[0]) if res else []
        except Exception as e:
            log.debug("graph query on %s failed", group_id, exc_info=True)
            await self._note_error(e)
            return None

    async def episode_counts(self) -> dict[str, int] | None:
        """Episodes per scope (FalkorDB). None means "cannot tell" (graph down
        or a non-Falkor driver), {} or all-zero means empty. Never raises."""
        if not await self._ensure():
            return None
        d = getattr(self._graphiti, "driver", None)
        if d is None or "falkor" not in type(d).__name__.lower():
            return None
        names = await self._our_graph_names()
        if names is None:
            return None
        counts: dict[str, int] = {}
        for gid in names:
            rows = await self._graph_query(gid, "MATCH (e:Episodic) RETURN count(e) AS n")
            if rows is None:
                if not self._available:
                    return None
                continue
            counts[gid] = int((rows[0] or {}).get("n") or 0) if rows else 0
        return counts

    async def has_episode(self, group_id: str, *, name: str | None = None,
                          content: str | None = None) -> bool | None:
        """Is there already an episode with this name or exactly this content
        in the scope? Lets the backfill skip what an earlier write (or an
        earlier backfill) already stored. None = cannot tell."""
        if not name and not content:
            return False
        names = await self._our_graph_names()
        if names is None:
            return None
        if group_id not in names:
            return False             # the scope's graph does not exist yet
        for field, value in (("name", name), ("content", content)):
            if not value:
                continue
            rows = await self._graph_query(
                group_id, f"MATCH (e:Episodic) WHERE e.{field} = $v RETURN count(e) AS n",
                v=value)
            if rows is None:
                return None
            if rows and int((rows[0] or {}).get("n") or 0) > 0:
                return True
        return False

    # ── Quick 014: whole-graph reads (graph view, rebuild) ──────────────────
    #
    # Read-only Cypher with constant text and parameters only, one scope
    # graph at a time through the per-group driver (see the note above
    # _group_driver). None = cannot tell (graph down / non-Falkor / error).

    async def scope_graphs(self) -> list[str] | None:
        """The scope graphs that exist right now (global, chat_*, case_*)."""
        return await self._our_graph_names()

    async def graph_counts(self) -> dict[str, dict] | None:
        """{group: {"episodes", "entities", "facts"}} for every scope graph.
        None means "cannot tell". Never raises."""
        if not await self._ensure():
            return None
        d = getattr(self._graphiti, "driver", None)
        if d is None or "falkor" not in type(d).__name__.lower():
            return None
        names = await self._our_graph_names()
        if names is None:
            return None
        out: dict[str, dict] = {}
        for gid in names:
            row: dict[str, int] = {}
            for key, cypher in (
                    ("episodes", "MATCH (e:Episodic) RETURN count(e) AS n"),
                    ("entities", "MATCH (x:Entity) RETURN count(x) AS n"),
                    ("facts", "MATCH (:Entity)-[r:RELATES_TO]->(:Entity) RETURN count(r) AS n")):
                rows = await self._graph_query(gid, cypher)
                if rows is None and not self._available:
                    return None
                row[key] = int((rows[0] or {}).get("n") or 0) if rows else 0
            out[gid] = row
        return out

    async def read_entities(self, gid: str, *, limit: int = 300,
                            uuids: list[str] | None = None) -> list[dict] | None:
        """Entity nodes with their fact degree, most connected first (or the
        given uuids). Keys: uuid, name, labels, summary, created_at, degree."""
        lim = max(1, min(int(limit), GRAPH_READ_MAX))
        where = "WHERE n.uuid IN $ids " if uuids is not None else ""
        return await self._graph_query(
            gid,
            "MATCH (n:Entity) " + where +
            "OPTIONAL MATCH (n)-[r:RELATES_TO]-(:Entity) "
            "WITH n, count(r) AS degree "
            "RETURN n.uuid AS uuid, n.name AS name, labels(n) AS labels, "
            "n.summary AS summary, n.created_at AS created_at, degree "
            "ORDER BY degree DESC, n.created_at DESC LIMIT $lim",
            ids=list(uuids or []), lim=lim)

    async def read_facts(self, gid: str, *, among: list[str] | None = None,
                         touching: str | None = None,
                         limit: int = GRAPH_READ_MAX) -> list[dict] | None:
        """RELATES_TO facts between entities in `among`, or touching one
        entity, or all. Keys: uuid, source, target, name, fact, valid_at,
        invalid_at, expired_at, created_at, episodes."""
        if touching is not None:
            where = "WHERE a.uuid = $u OR b.uuid = $u "
        elif among is not None:
            where = "WHERE a.uuid IN $ids AND b.uuid IN $ids "
        else:
            where = ""
        return await self._graph_query(
            gid,
            "MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) " + where +
            "RETURN r.uuid AS uuid, a.uuid AS source, b.uuid AS target, r.name AS name, "
            "r.fact AS fact, r.valid_at AS valid_at, r.invalid_at AS invalid_at, "
            "r.expired_at AS expired_at, r.created_at AS created_at, r.episodes AS episodes "
            "LIMIT $lim",
            ids=list(among or []), u=touching or "",
            lim=max(1, min(int(limit), GRAPH_READ_MAX)))

    async def read_episodes(self, gid: str, *, limit: int = 100,
                            uuids: list[str] | None = None,
                            name_prefix: str | None = None) -> list[dict] | None:
        """Episodes, newest first (or the given uuids / names starting with a
        prefix). Keys: uuid, name, content, source, group_id, valid_at,
        created_at."""
        if uuids is not None:
            where = "WHERE e.uuid IN $ids "
        elif name_prefix:
            where = "WHERE e.name STARTS WITH $prefix "
        else:
            where = ""
        return await self._graph_query(
            gid,
            "MATCH (e:Episodic) " + where +
            "RETURN e.uuid AS uuid, e.name AS name, e.content AS content, "
            "e.source_description AS source, e.group_id AS group_id, "
            "e.valid_at AS valid_at, e.created_at AS created_at "
            "ORDER BY e.created_at DESC LIMIT $lim",
            ids=list(uuids or []), prefix=name_prefix or "",
            lim=max(1, min(int(limit), GRAPH_READ_MAX * 5)))

    async def read_mentions(self, gid: str, *, entities: list[str] | None = None,
                            episodes: list[str] | None = None) -> list[dict] | None:
        """MENTIONS links (episode -> entity) for some entities or episodes.
        Keys: episode, entity."""
        if entities is None and episodes is None:
            return []
        where = ("WHERE n.uuid IN $ids " if entities is not None
                 else "WHERE e.uuid IN $ids ")
        return await self._graph_query(
            gid,
            "MATCH (e:Episodic)-[:MENTIONS]->(n:Entity) " + where +
            "RETURN e.uuid AS episode, n.uuid AS entity LIMIT $lim",
            ids=list(entities if entities is not None else episodes or []),
            lim=GRAPH_READ_MAX * 5)

    # ── Quick 014: rebuild maintenance (writes; memory_rebuild only) ───────

    async def clear_scope(self, gid: str) -> bool:
        """Delete every node and edge in one scope graph, keeping the graph
        and its indexes. Never raises."""
        if not await self._ensure():
            return False
        driver = self._group_driver(gid)
        if driver is None:
            return False
        try:
            await driver.execute_query("MATCH (n) DETACH DELETE n")
            return True
        except Exception as e:
            log.warning("clearing graph %s failed", gid, exc_info=True)
            await self._note_error(e)
            return False

    async def drop_scope(self, gid: str) -> bool:
        """Delete a whole scope graph (legacy case_<id> graphs: since Quick
        014 a case lives once, in global). Never raises."""
        if not gid.startswith(("case_", "chat_")) or not await self._ensure():
            return False
        d = getattr(self._graphiti, "driver", None)
        client = getattr(d, "client", None)
        if client is None or not hasattr(client, "select_graph"):
            return False
        try:
            res = client.select_graph(gid).delete()
            if inspect.isawaitable(res):
                await res
            self._drivers = {k: v for k, v in self._drivers.items() if k[1] != gid}
            return True
        except Exception as e:
            log.warning("deleting graph %s failed", gid, exc_info=True)
            await self._note_error(e)
            return False

    # ── Quick 019: take one source's episodes out of a scope graph ─────────

    async def find_learning_episodes(self, group_id: str, name_prefix: str,
                                     contents: list[str] | tuple = ()) -> list[dict] | None:
        """Quick 019: a learning's episodes in one scope graph: those named
        `name_prefix*` ("learning_<id>_r", every revision) plus learning
        episodes holding exactly one of `contents` (add_episode stores an
        identical text once, under the name of whichever learning wrote it
        first). Keys: uuid, name, content. [] when the scope graph does not
        exist; None when the graph cannot be read. Read-only, never raises."""
        prefix = (name_prefix or "").strip()
        if not prefix:
            return []
        names = await self._our_graph_names()
        if names is None:
            return None
        if group_id not in names:
            return []                   # reading a missing graph would create it
        texts = [t.strip() for t in contents or () if (t or "").strip()]
        return await self._graph_query(
            group_id,
            "MATCH (e:Episodic) WHERE e.name STARTS WITH $prefix "
            "OR (e.name STARTS WITH 'learning_' AND e.content IN $texts) "
            "RETURN e.uuid AS uuid, e.name AS name, e.content AS content",
            prefix=prefix, texts=texts)

    async def retire_episodes(self, group_id: str, episode_uuids: list[str]) -> dict | None:
        """Remove what some episodes put in one scope graph (a deleted or
        superseded learning's, see find_learning_episodes), keeping everything
        other episodes also support. Audit Fix 5(c), spiked on graphiti 0.29.2
        and FalkorDB v4.22.2 (Quick 019 PLAN):

          1. facts (RELATES_TO) whose every supporting episode (r.episodes) is
             retired are deleted; a fact other episodes also support stays,
             with the retired ids dropped from its episodes list;
          2. entities only the retired episodes mention, left with no fact,
             are deleted;
          3. the other entities those episodes mention get their summary
             rewritten from their remaining live facts (no LLM): graphiti
             summarises every entity an episode mentions, so the old summary
             may still carry the retired text;
          4. the episodes go last, so a re-run after a failure finishes the job.

        Not graphiti's remove_episode: it deletes every edge FIRST created by
        the episode even when later episodes support it, and runs on the
        driver add_episode last pointed at another group's graph. Setting
        invalid_at is not enough either: recall() still returns the episode
        text and entity summaries, and the graph view draws the edge.

        Returns {"episodes", "facts", "facts_kept", "entities", "summaries"};
        None when the graph cannot be reached or a step failed. Never raises."""
        uuids = [str(u) for u in episode_uuids or () if u]
        if not await self._ensure():
            return None
        out = {"episodes": 0, "facts": 0, "facts_kept": 0, "entities": 0, "summaries": 0}
        if not uuids:
            return out
        names = await self._our_graph_names()
        if names is None:
            return None
        if group_id not in names:
            return out                  # no such graph (reading it would create it)
        driver = self._group_driver(group_id)
        if driver is None:
            return None

        async def rows(cypher: str, **params) -> list:
            res = await driver.execute_query(cypher, **params)
            return list(res[0]) if res else []

        try:
            eps = [str(r["uuid"]) for r in await rows(
                "MATCH (e:Episodic) WHERE e.uuid IN $ids RETURN e.uuid AS uuid",
                ids=uuids) if r.get("uuid")]
            if not eps:
                return out
            touched = [str(r["uuid"]) for r in await rows(
                "MATCH (e:Episodic)-[:MENTIONS]->(n:Entity) WHERE e.uuid IN $eps "
                "RETURN DISTINCT n.uuid AS uuid", eps=eps) if r.get("uuid")]
            only = [str(r["uuid"]) for r in await rows(
                "MATCH (:Entity)-[r:RELATES_TO]->(:Entity) "
                "WHERE size(r.episodes) > 0 AND all(x IN r.episodes WHERE x IN $eps) "
                "RETURN r.uuid AS uuid", eps=eps)]
            if only:
                await rows("MATCH (:Entity)-[r:RELATES_TO]->(:Entity) WHERE r.uuid IN $ids "
                           "DELETE r", ids=only)
            kept = await rows(
                "MATCH (:Entity)-[r:RELATES_TO]->(:Entity) "
                "WHERE any(x IN r.episodes WHERE x IN $eps) "
                "SET r.episodes = [x IN r.episodes WHERE NOT x IN $eps] "
                "RETURN r.uuid AS uuid", eps=eps)
            orphans = [str(r["uuid"]) for r in await rows(
                "MATCH (e:Episodic)-[:MENTIONS]->(n:Entity) WHERE e.uuid IN $eps "
                "WITH DISTINCT n "
                "OPTIONAL MATCH (o:Episodic)-[:MENTIONS]->(n) WHERE NOT o.uuid IN $eps "
                "WITH n, count(o) AS others WHERE others = 0 "
                "OPTIONAL MATCH (n)-[f:RELATES_TO]-(:Entity) "
                "WITH n, count(f) AS facts WHERE facts = 0 "
                "RETURN n.uuid AS uuid", eps=eps) if r.get("uuid")]
            if orphans:
                await rows("MATCH (n:Entity) WHERE n.uuid IN $ids DETACH DELETE n", ids=orphans)
            gone = set(orphans)
            for uuid in [u for u in touched if u not in gone]:
                facts = [str(r.get("fact") or "").strip() for r in await rows(
                    "MATCH (n:Entity {uuid: $uuid})-[r:RELATES_TO]-(:Entity) "
                    "WHERE r.invalid_at IS NULL AND r.expired_at IS NULL "
                    "RETURN r.fact AS fact, r.created_at AS created_at "
                    "ORDER BY created_at DESC LIMIT $n", uuid=uuid, n=SUMMARY_FACTS)]
                await rows("MATCH (n:Entity {uuid: $uuid}) SET n.summary = $summary",
                           uuid=uuid, summary=summary_from_facts(facts))
                out["summaries"] += 1
            await rows("MATCH (e:Episodic) WHERE e.uuid IN $eps DETACH DELETE e", eps=eps)
            out.update(episodes=len(eps), facts=len(only), facts_kept=len(kept),
                       entities=len(orphans))
            log.info("MemoryService.retire_episodes %s in %s: %s", eps, group_id, out)
            return out
        except Exception as e:
            log.warning("MemoryService.retire_episodes %s in %s failed", uuids, group_id,
                        exc_info=True)
            await self._note_error(e)
            return None

    async def resolve_person_node(self, name: str) -> str | None:
        """Resolve a person's name to their graph node uuid, for use as
        search(center_node_uuid=...). Exact case-insensitive name match only —
        no fuzzy matching (same rule as people.py identity). Positive results
        are cached per process; confirmed misses are cached for
        PERSON_NODE_MISS_TTL_SECONDS. Never raises; None on any error/no-match."""
        name = (name or "").strip()
        if not name:
            return None
        key = name.lower()
        cached = _person_node_cache.get(key)
        if cached is not None:
            return cached
        missed_at = _person_node_miss_cache.get(key)
        if (missed_at is not None
                and time.monotonic() - missed_at < PERSON_NODE_MISS_TTL_SECONDS):
            return None  # recent confirmed miss — don't re-search every message
        if not await self._ensure():
            return None
        try:
            from graphiti_core.search.search_config_recipes import (
                NODE_HYBRID_SEARCH_RRF,
            )
            config = NODE_HYBRID_SEARCH_RRF.model_copy(deep=True,
                                                       update={"limit": 5})
            # People live in the global scope (onboarding, distillations).
            kwargs = {"group_ids": [GLOBAL_GROUP]}
            driver = self._group_driver(GLOBAL_GROUP)
            if driver is not None:
                kwargs["driver"] = driver
            results = await self._graphiti.search_(name, config, **kwargs)
            for node in (getattr(results, "nodes", None) or []):
                uuid = getattr(node, "uuid", None)
                node_name = str(getattr(node, "name", "") or "").strip()
                if uuid and node_name.lower() == key:
                    _person_node_cache[key] = str(uuid)
                    _person_node_miss_cache.pop(key, None)
                    return str(uuid)
            # Confirmed no-match (search succeeded, nothing named this): cache
            # the miss with a TTL so retries are periodic, not per-message.
            # Errors below deliberately stay uncached — they may be transient.
            _person_node_miss_cache[key] = time.monotonic()
            return None
        except Exception:
            log.warning("MemoryService.resolve_person_node failed for %r",
                        name, exc_info=True)
            return None

    async def entity_context(self, name: str, limit: int = 6) -> str:
        """A short natural-language brief about one entity (person/vendor/etc).

        Built from facts mentioning the name. Empty string when nothing known or
        the graph is unavailable."""
        name = (name or "").strip()
        if not name:
            return ""
        facts = await self.search(name, limit=limit)
        if not facts:
            return ""
        lines = "\n".join(f"- {fact_line(f)}" for f in facts)
        return f"What we know about {name}:\n{lines}"


# Module-level singleton. Import this, not Graphiti.
memory_graph = MemoryService()


async def resolve_person_node(name: str) -> str | None:
    """Module-level convenience over the singleton (used by contextassembler)."""
    return await memory_graph.resolve_person_node(name)


async def reset_graph() -> None:
    """Force the next graph call to re-initialise (new host, or a retry after a
    boot-time failure). Called by configservice._invalidate."""
    await memory_graph.close()

"""Quick 019 (audit Fix 5(c)): deleting or superseding a learning takes its
graph facts out, and keeps facts other episodes also support.

The fake graph below interprets the constant Cypher memoryservice sends, over
the same three episodes the p19 spike wrote through graphiti 0.29.2 on
FalkorDB v4.22.2 (Quick 019 PLAN, "Spike"): learning 9001 and 9002 both
state the BlueDart fact, 9002 adds the Mumbai office, 9003 the Acme late fee.
"""

import pytest

from app import consolidation, memory, memoryservice
from app.memoryservice import MemoryService, summary_from_facts

T1 = "Northwind Traders ships all Pune orders with BlueDart courier."
T2 = T1 + " Northwind Traders has its head office in Mumbai."
T3 = "Acme Logistics charges a 2 percent late fee on overdue invoices."


class FakeGraph:
    def __init__(self):
        self.episodes = {
            "ep1": {"name": "learning_9001_r0", "content": T1},
            "ep2": {"name": "learning_9002_r0", "content": T2},
            "ep3": {"name": "learning_9003_r0", "content": T3},
            "ep10": {"name": "learning_90010_r0", "content": "x"},  # never matches learning_9001_r
            "case": {"name": "case_4_close", "content": T3},         # a case with the same text
        }
        self.entities = {
            "nw": {"name": "Northwind Traders", "summary": "old nw summary"},
            "bd": {"name": "BlueDart", "summary": "old bd summary"},
            "pu": {"name": "Pune", "summary": "Northwind ships all Pune orders."},
            "mu": {"name": "Mumbai", "summary": "old mu summary"},
            "ac": {"name": "Acme Logistics", "summary": "charges a 2 percent late fee"},
            "lf": {"name": "late fee rule", "summary": "2 percent"},
        }
        self.edges = {
            "e-pune": {"a": "nw", "b": "pu", "fact": "Northwind Traders ships all Pune orders.",
                       "episodes": ["ep1"], "created_at": "2026-10-26T10:00:01"},
            "e-bd": {"a": "nw", "b": "bd", "fact": T1,
                     "episodes": ["ep1", "ep2"], "created_at": "2026-10-26T10:00:02"},
            "e-bdpu": {"a": "bd", "b": "pu", "fact": "BlueDart courier is used for Pune orders.",
                       "episodes": ["ep1"], "created_at": "2026-10-26T10:00:03"},
            "e-mu": {"a": "nw", "b": "mu", "fact": "Northwind Traders has its head office in Mumbai.",
                     "episodes": ["ep2"], "created_at": "2026-10-26T10:00:04"},
            "e-fee": {"a": "lf", "b": "ac", "fact": "Acme Logistics charges a 2 percent late fee.",
                      "episodes": ["ep3"], "created_at": "2026-10-26T10:00:05"},
            "e-old": {"a": "nw", "b": "mu", "fact": "Northwind's office was in Pune.",
                      "episodes": ["ep2"], "created_at": "2026-10-20T10:00:00",
                      "invalid_at": "2026-10-26T10:00:04"},
            "e-none": {"a": "nw", "b": "bd", "fact": "no episodes recorded", "episodes": [],
                       "created_at": "2026-10-01T00:00:00"},
        }
        self.mentions = {("ep1", "nw"), ("ep1", "pu"), ("ep1", "bd"),
                         ("ep2", "nw"), ("ep2", "pu"), ("ep2", "bd"), ("ep2", "mu"),
                         ("ep3", "ac"), ("ep3", "lf"), ("ep10", "nw")}
        self.queries = []
        self.fail_on = None

    async def execute_query(self, cypher, **p):
        self.queries.append(cypher)
        if self.fail_on and self.fail_on in cypher:
            raise RuntimeError("redis says no")
        return (self._run(cypher, p), None, None)

    def _run(self, c, p):
        if "e.name STARTS WITH $prefix" in c:
            return [{"uuid": u, "name": e["name"], "content": e["content"]}
                    for u, e in self.episodes.items()
                    if e["name"].startswith(p["prefix"])
                    or (e["name"].startswith("learning_") and e["content"] in p["texts"])]
        if "MATCH (e:Episodic) WHERE e.uuid IN $ids RETURN e.uuid AS uuid" in c:
            return [{"uuid": u} for u in self.episodes if u in p["ids"]]
        if "RETURN DISTINCT n.uuid AS uuid" in c:
            return [{"uuid": n} for n in sorted({n for e, n in self.mentions if e in p["eps"]})]
        if "all(x IN r.episodes WHERE x IN $eps)" in c:
            return [{"uuid": u} for u, r in self.edges.items()
                    if r["episodes"] and all(x in p["eps"] for x in r["episodes"])]
        if "WHERE r.uuid IN $ids DELETE r" in c:
            for u in p["ids"]:
                self.edges.pop(u, None)
            return []
        if "SET r.episodes = [x IN r.episodes WHERE NOT x IN $eps]" in c:
            out = []
            for u, r in self.edges.items():
                if any(x in p["eps"] for x in r["episodes"]):
                    r["episodes"] = [x for x in r["episodes"] if x not in p["eps"]]
                    out.append({"uuid": u})
            return out
        if "count(o) AS others" in c:
            out = []
            for n in sorted({n for e, n in self.mentions if e in p["eps"]}):
                others = [e for e, m in self.mentions if m == n and e not in p["eps"]]
                facts = [u for u, r in self.edges.items() if n in (r["a"], r["b"])]
                if not others and not facts:
                    out.append({"uuid": n})
            return out
        if "MATCH (n:Entity) WHERE n.uuid IN $ids DETACH DELETE n" in c:
            for n in p["ids"]:
                self.entities.pop(n, None)
                self.edges = {u: r for u, r in self.edges.items() if n not in (r["a"], r["b"])}
                self.mentions = {(e, m) for e, m in self.mentions if m != n}
            return []
        if "MATCH (n:Entity {uuid: $uuid})-[r:RELATES_TO]-(:Entity)" in c:
            live = [r for r in self.edges.values()
                    if p["uuid"] in (r["a"], r["b"]) and not r.get("invalid_at")
                    and not r.get("expired_at")]
            live.sort(key=lambda r: r["created_at"], reverse=True)
            return [{"fact": r["fact"], "created_at": r["created_at"]} for r in live[:p["n"]]]
        if "SET n.summary = $summary" in c:
            self.entities[p["uuid"]]["summary"] = p["summary"]
            return []
        if "MATCH (e:Episodic) WHERE e.uuid IN $eps DETACH DELETE e" in c:
            for e in p["eps"]:
                self.episodes.pop(e, None)
            self.mentions = {(e, m) for e, m in self.mentions if e not in p["eps"]}
            return []
        raise AssertionError(f"unexpected query: {c}")


def _service(monkeypatch, graph, names=("global",), up=True):
    svc = MemoryService()

    async def ensure():
        return up

    async def graph_names():
        return list(names) if up else None

    monkeypatch.setattr(svc, "_ensure", ensure)
    monkeypatch.setattr(svc, "_our_graph_names", graph_names)
    monkeypatch.setattr(svc, "_group_driver", lambda gid: graph)
    return svc


ZERO = {"episodes": 0, "facts": 0, "facts_kept": 0, "entities": 0, "summaries": 0}


# ── memoryservice: find, then retire ─────────────────────────────────────

async def test_find_learning_episodes_by_name_and_exact_text(monkeypatch):
    g = FakeGraph()
    svc = _service(monkeypatch, g)
    found = await svc.find_learning_episodes("global", "learning_9001_r")
    assert [e["uuid"] for e in found] == ["ep1"]           # not learning_90010
    found = await svc.find_learning_episodes("global", "learning_77_r", [" " + T3 + "\n"])
    assert [e["uuid"] for e in found] == ["ep3"]           # the case episode is never a match
    assert await _service(monkeypatch, g, names=("chat_x",)).find_learning_episodes(
        "global", "learning_9001_r") == []
    assert await _service(monkeypatch, g, up=False).find_learning_episodes(
        "global", "learning_9001_r") is None


async def test_retire_keeps_facts_other_episodes_support(monkeypatch):
    g = FakeGraph()
    svc = _service(monkeypatch, g)
    out = await svc.retire_episodes("global", ["ep1"])
    assert out == {"episodes": 1, "facts": 2, "facts_kept": 1, "entities": 0, "summaries": 3}
    assert set(g.edges) == {"e-bd", "e-mu", "e-fee", "e-old", "e-none"}
    assert g.edges["e-bd"]["episodes"] == ["ep2"]            # shared fact stays, 9002 only
    assert set(g.episodes) == {"ep2", "ep3", "ep10", "case"}
    assert set(g.entities) == {"nw", "bd", "pu", "mu", "ac", "lf"}   # 9002 mentions them
    # summaries rebuilt from the facts that remain (no LLM); Pune has none left
    assert g.entities["pu"]["summary"] == ""
    assert g.entities["bd"]["summary"] == (g.edges["e-bd"]["fact"] + " "
                                           + g.edges["e-none"]["fact"])   # newest first
    assert g.entities["nw"]["summary"].startswith(g.edges["e-mu"]["fact"])
    assert "office was in Pune" not in g.entities["nw"]["summary"]   # superseded fact left out
    assert g.entities["mu"]["summary"] == "old mu summary"           # 9001 never mentioned it


async def test_retire_removes_a_learning_and_its_own_entities(monkeypatch):
    g = FakeGraph()
    svc = _service(monkeypatch, g)
    out = await svc.retire_episodes("global", ["ep3"])
    assert out == {"episodes": 1, "facts": 1, "facts_kept": 0, "entities": 2, "summaries": 0}
    assert "e-fee" not in g.edges and "ac" not in g.entities and "lf" not in g.entities
    assert "ep3" not in g.episodes and "case" in g.episodes
    assert "e-none" in g.edges, "an edge with no recorded episodes is never deleted"


async def test_retire_goes_episodes_last(monkeypatch):
    g = FakeGraph()
    svc = _service(monkeypatch, g)
    await svc.retire_episodes("global", ["ep1"])
    assert "MATCH (e:Episodic) WHERE e.uuid IN $eps DETACH DELETE e" in g.queries[-1]
    assert [i for i, q in enumerate(g.queries) if "DELETE" in q][-1] == len(g.queries) - 1


async def test_retire_failure_returns_none_and_a_rerun_finishes(monkeypatch):
    g = FakeGraph()
    svc = _service(monkeypatch, g)
    g.fail_on = "count(o) AS others"
    assert await svc.retire_episodes("global", ["ep3"]) is None
    assert "ep3" in g.episodes, "episodes stay until the end, so a re-run finds them"
    g.fail_on = None
    out = await svc.retire_episodes("global", ["ep3"])
    assert out["episodes"] == 1 and out["entities"] == 2 and "ep3" not in g.episodes


async def test_retire_with_nothing_to_do(monkeypatch):
    g = FakeGraph()
    svc = _service(monkeypatch, g)
    assert await svc.retire_episodes("global", []) == ZERO
    assert await svc.retire_episodes("global", ["gone"]) == ZERO
    assert not any("DELETE" in q or "SET" in q for q in g.queries)
    g2 = FakeGraph()
    assert await _service(monkeypatch, g2, names=("chat_x",)).retire_episodes(
        "global", ["ep1"]) == ZERO
    assert g2.queries == [], "a scope graph that does not exist is never read (it would be created)"
    assert await _service(monkeypatch, g2, up=False).retire_episodes("global", ["ep1"]) is None


def test_summary_from_facts():
    assert summary_from_facts([]) == ""
    assert summary_from_facts(["A b.", "a  B.", "", None, "C d."]) == "A b. C d."
    long = ["x" * 400, "y" * 400]
    assert summary_from_facts(long) == "x" * 400        # whole facts only
    assert summary_from_facts(["z" * 900]) == "z" * memoryservice.SUMMARY_CHARS


# ── memory.retire_learning_graph, against the fake graph ─────────────────

class _Learnings:
    """learnings rows for the shared-text check: (id, status, kind, content)."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.sql = []

    async def fetch(self, sql, *args):
        self.sql.append((sql, args))
        lid, texts, kinds = args
        return [{"t": c.strip()} for i, st, k, c in self.rows
                if i != lid and st == "active" and k in kinds and c.strip() in texts]


@pytest.fixture
def wired(monkeypatch):
    g = FakeGraph()
    svc = _service(monkeypatch, g)
    records = []

    async def record(*a, **k):
        records.append((a, k))

    from app import jobruns
    monkeypatch.setattr(memoryservice, "memory_graph", svc)
    monkeypatch.setattr(jobruns, "record", record)
    return g, svc, records


async def test_deleting_a_learning_retires_its_episodes(wired):
    g, _, records = wired
    out = await memory.retire_learning_graph(9001, kind="fact", content=T1, pool=_Learnings())
    assert out == {"episodes": 1, "facts": 2, "facts_kept": 1, "entities": 0,
                   "summaries": 3, "shared": 0}
    assert "ep1" not in g.episodes and g.edges["e-bd"]["episodes"] == ["ep2"]
    assert records == []


async def test_text_stored_under_another_name_is_found(wired):
    """add_episode stores an identical text once: a consolidation keeper whose
    merged text equals a loser's has no episode of its own. Deleting the
    keeper later must still take that text out once nothing active has it."""
    g, _, _ = wired
    out = await memory.retire_learning_graph(55, kind="fact", content=T3, pool=_Learnings(
        [(9003, "superseded", "fact", T3)]))
    assert out["episodes"] == 1 and "ep3" not in g.episodes and "case" in g.episodes


async def test_an_episode_an_active_learning_still_needs_is_kept(wired):
    g, _, _ = wired
    pool = _Learnings([(55, "active", "fact", T3)])          # the keeper has this text
    out = await memory.retire_learning_graph(9003, kind="fact", content=T3, pool=pool)
    assert out == {**ZERO, "shared": 1}
    assert "ep3" in g.episodes and "e-fee" in g.edges
    sql, args = pool.sql[0]
    assert "status = 'active'" in sql and args[0] == 9003
    # a recipe with the same text does not need the graph: not protected
    out = await memory.retire_learning_graph(9003, kind="fact", content=T3, pool=_Learnings(
        [(56, "active", "recipe", T3)]))
    assert out["episodes"] == 1 and out["shared"] == 0


async def test_retire_learning_graph_skips(wired, monkeypatch):
    g, _, records = wired
    assert "not in the graph" in (await memory.retire_learning_graph(
        7, kind="recipe", content="SELECT 1"))["skipped"]
    from types import SimpleNamespace
    monkeypatch.setattr(memory, "get_settings",
                        lambda: SimpleNamespace(memory_graph_enabled=False))
    assert "switched off" in (await memory.retire_learning_graph(
        7, kind="fact", content="x" * 20))["skipped"]
    assert g.queries == [] and records == []


async def test_retire_learning_graph_records_a_graph_it_could_not_reach(wired, monkeypatch):
    g, svc, records = wired

    async def down():
        return None
    monkeypatch.setattr(svc, "_our_graph_names", down)
    assert await memory.retire_learning_graph(7, kind="fact", content="", pool=_Learnings()) is None

    async def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(svc, "find_learning_episodes", boom)
    assert await memory.retire_learning_graph(8, kind="preference", pool=_Learnings()) is None
    assert len(records) == 2
    args, kw = records[0]
    assert args[0] == "memory_graph" and kw["status"] == "failed"
    assert "rebuild" in kw["summary"] and kw["ref"] == ("learning", "7")


# ── call sites ───────────────────────────────────────────────────────────

class _DeletePool:
    def __init__(self, row):
        self.row = row
        self.sql = []

    async def fetchrow(self, sql, *args):
        self.sql.append((sql, args))
        return self.row


async def test_delete_learning_route_takes_the_graph_facts_out(monkeypatch):
    from app import db, main
    pool = _DeletePool({"kind": "fact", "content": "Acme pays net-30"})
    calls = []

    async def get_pool():
        return pool

    async def retire(learning_id, **kw):
        calls.append((learning_id, kw))
        return {"episodes": 1, "facts": 2}

    monkeypatch.setattr(db, "get_pool", get_pool)
    monkeypatch.setattr(memory, "retire_learning_graph", retire)
    out = await main.delete_learning(5)
    assert out == {"deleted": 5, "graph": {"episodes": 1, "facts": 2}}
    assert "DELETE FROM learnings WHERE id = $1 RETURNING kind, content" in pool.sql[0][0]
    assert calls == [(5, {"kind": "fact", "content": "Acme pays net-30", "pool": pool})]

    pool.row = None                       # already gone: nothing to clean up
    calls.clear()
    assert await main.delete_learning(6) == {"deleted": 6, "graph": None}
    assert calls == []


class _MergePool:
    def __init__(self, row=None, boom=False):
        self.row = row
        self.boom = boom

    async def fetchrow(self, sql, *args):
        if self.boom:
            raise RuntimeError("db blip")
        return self.row


async def test_consolidation_graphs_the_keeper_before_retiring_losers(monkeypatch):
    order = []

    async def to_graph(lid, content, kind, revisions=0, *, pool=None):
        order.append(("to_graph", lid, content, kind, revisions))
        return True

    async def retire(lid, *, kind=None, content=None, pool=None):
        order.append(("retire", lid, kind, content))
        return {"episodes": 1}

    monkeypatch.setattr(memory, "to_graph", to_graph)
    monkeypatch.setattr(memory, "retire_learning_graph", retire)
    pool = _MergePool({"kind": "fact", "revisions": 2})
    await consolidation._graph_after_merge(pool, 1, "MERGED", [
        {"id": 2, "content": "dup a"}, {"id": 3, "content": "dup b"}])
    assert order == [("to_graph", 1, "MERGED", "fact", 2),
                     ("retire", 2, "fact", "dup a"), ("retire", 3, "fact", "dup b")]

    order.clear()
    await consolidation._graph_after_merge(_MergePool({"kind": "recipe", "revisions": 1}),
                                           1, "SELECT 1", [{"id": 2, "content": "SELECT 2"}])
    assert order == [("retire", 2, "recipe", "SELECT 2")]   # recipes are never graphed

    order.clear()
    await consolidation._graph_after_merge(_MergePool(boom=True), 1, "M", [{"id": 2}])
    assert order == []                                        # fail-soft, never raises

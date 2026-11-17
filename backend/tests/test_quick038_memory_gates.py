"""Quick 038 — cut what reaches the knowledge graph, not how well it is extracted.

Graph extraction runs on the dearest model in the stack (gpt-5.5), about six
calls per episode. Three gates, each a setting:

  * graph_skip_low_value_turns (default OFF since the merge review): facts reflection draws from a
    dashboard build/revision turn or a tool-only turn (status checks and
    proposals whose outcome lives in its own record) are stored as learnings
    and recalled from Postgres, but not sent to the graph. learnings.graph_skip
    says why; the backfill and the rebuild leave them out while the setting is
    on. A turn where the owner stated something (remember_fact) is never gated.
  * reflection_skip_trivial (default on): no reflection for a turn with no
    tool calls whose message is only a greeting, thanks or ok. A hard run
    (Quick 009) always reflects.
  * graph_community_rebuild (default off): nothing reads graph communities.

Pure logic: pools, the graph, the reflection model and jobruns are faked.
"""

import re
import sys
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import consolidation, configservice, memory, memory_pipeline, memory_rebuild
from app.config import Settings



ROOT = Path(__file__).resolve().parents[2]


def _settings(**over):
    base = dict(graph_skip_low_value_turns=True, reflection_skip_trivial=True,
                graph_community_rebuild="off", openai_api_key="sk-test",
                timezone="UTC")
    base.update(over)
    return SimpleNamespace(**base)


# ── settings ────────────────────────────────────────────────────────────────

def test_settings_defaults_and_registry():
    f = Settings.model_fields
    assert f["graph_skip_low_value_turns"].default is False  # off: graph-only readers keep every fact
    assert f["reflection_skip_trivial"].default is True
    assert f["graph_community_rebuild"].default == "off"
    for key in ("graph_skip_low_value_turns", "reflection_skip_trivial",
                "graph_community_rebuild"):
        meta = configservice.REGISTRY[key]
        # tuning, not memory_graph: a save must not reset the graph connection
        assert meta["group"] == "tuning" and meta["label"] and not meta["secret"]
    assert configservice.REGISTRY["graph_community_rebuild"]["choices"] == [
        "off", "weekly", "nightly"]


def test_community_setting_validates_and_a_typo_never_bricks_settings(monkeypatch):
    configservice._validate_value("graph_community_rebuild", "weekly")
    with pytest.raises(ValueError):
        configservice._validate_value("graph_community_rebuild", "monthly")
    monkeypatch.setenv("GRAPH_COMMUNITY_REBUILD", "Nightly")
    assert Settings().graph_community_rebuild == "nightly"
    monkeypatch.setenv("GRAPH_COMMUNITY_REBUILD", "sometimes")
    assert Settings().graph_community_rebuild == "off"


def test_config_blocks_are_delimited():
    for rel in ("backend/app/config.py", "backend/app/configservice.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert src.count("# ── Quick 038") == 1 and src.count("# ── End Quick 038 ──") == 1, rel


def test_schema_adds_graph_skip_idempotently():
    sql = (ROOT / "backend/app/schema.sql").read_text(encoding="utf-8")
    assert "ALTER TABLE learnings ADD COLUMN IF NOT EXISTS graph_skip TEXT;" in sql


# ── which turns stay out of the graph ───────────────────────────────────────

def _log(*names):
    return [{"tool": n, "args": {}, "result_digest": "ok"} for n in names]


@pytest.mark.parametrize("names", [
    ("send_progress", "delegate_to_dashboard_agent"),
    ("run_sql", "get_dashboard", "preview_dashboard", "save_dashboard_version"),
    ("delegate_to_dashboard_agent", "send_message_to_person"),   # dashboard wins
    ("preview_dashboard:refused",),
])
def test_dashboard_turns(names):
    assert memory.graph_skip_reason(_log(*names)) == memory.GRAPH_SKIP_DASHBOARD


@pytest.mark.parametrize("names", [
    ("propose_kpi",),
    ("send_progress", "get_sync_status"),
    ("request_sync", "use_skill"),
    ("list_dashboard_proposals",),
    ("create_watch", "propose_forecast", "open_case"),
    ("propose_model_change", "propose_relationships_from_keys"),
    ("propose_source", "propose_mode_change", "propose_context_change"),
    ("list_cases", "propose_next_action"),
])
def test_tool_only_turns(names):
    assert memory.graph_skip_reason(_log(*names)) == memory.GRAPH_SKIP_TOOL_ONLY


@pytest.mark.parametrize("names", [
    (),                                          # pure conversation: may state facts
    ("send_progress",),                          # nothing acted
    ("use_skill", "skill_asset"),
    ("run_sql", "propose_kpi"),                  # read data: may learn about it
    ("get_model", "propose_model_change"),
    ("discover_schema",),
    ("send_message_to_person",),                 # people facts are what the graph is for
    ("send_document",),
    ("probe_source", "propose_source"),
    ("test_source",),                            # copies sample rows to learn types
    ("remember_fact",),                          # the owner stated something
    ("remember_fact", "delegate_to_dashboard_agent"),
    ("propose_kpi", "remember_fact"),
    ("some_future_tool",),                       # unknown: never gated
])
def test_turns_that_still_reach_the_graph(names):
    assert memory.graph_skip_reason(_log(*names)) is None


def test_gate_tool_names_are_real_tools():
    """A renamed tool must not silently turn the gate off (or on)."""
    from app.agents import graph
    known = {t for tools in graph.AGENT_TOOL_SUPERSET.values() for t in tools}
    known |= {"delegate_to_dashboard_agent", "delegate_to_context_agent"}
    for name in (memory.DASHBOARD_TURN_TOOLS | memory.TOOL_ONLY_TOOLS
                 | memory.NEUTRAL_TOOLS | memory.GATE_NEVER_TOOLS):
        assert name in known, name
    # nothing that reads business data or knowledge can make a turn tool-only
    reads = {"run_sql", "run_sql_to_csv", "get_model", "query_measure", "discover_schema",
             "search_business_context", "get_kpi_history", "get_insight", "get_forecast",
             "investigate_case", "web_research", "read_url", "search_documents",
             "get_document", "extract_document", "probe_source", "test_source",
             "delegate_to_data_agent",
             "delegate_to_analysis_agent", "delegate_to_document_agent",
             "delegate_to_drawing_agent", "send_message_to_person", "send_document"}
    assert not (reads & (memory.TOOL_ONLY_TOOLS | memory.NEUTRAL_TOOLS))


# ── _dedup_store: the one place a reflected fact is queued for the graph ────

class StorePool:
    def __init__(self, match=None):
        self.match = match
        self.writes = []

    async def fetchrow(self, sql, *args):
        return self.match

    async def fetchval(self, sql, *args):
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE")):
            self.writes.append((sql, args))
            return 11
        return None

    async def execute(self, sql, *args):
        self.writes.append((sql, args))


@pytest.fixture
def store(monkeypatch):
    graphed = []

    async def embed(text):
        return "[0.1]"

    async def merge(client, old, new, **kw):
        return "merged text of both learnings"

    async def to_graph(learning_id, content, kind, revisions=0, *, pool=None):
        graphed.append(learning_id)
        return True

    monkeypatch.setattr(memory, "_embed", embed)
    monkeypatch.setattr(memory, "_merge_learnings", merge)
    monkeypatch.setattr(memory, "to_graph", to_graph)
    return graphed


async def test_gated_new_fact_is_stored_but_not_graphed(store):
    pool = StorePool(match=None)
    kept = []
    out = await memory._dedup_store(None, pool, "fact", "Sales live in invoices.total",
                                    graph_skip=memory.GRAPH_SKIP_DASHBOARD, kept=kept)
    assert out == "stored"
    assert store == [] and kept == [11]
    sql, args = pool.writes[0]
    assert "INSERT INTO learnings" in sql and "graph_skip" in sql
    assert memory.GRAPH_SKIP_DASHBOARD in args


async def test_ungated_new_fact_is_graphed_as_before(store):
    pool = StorePool(match=None)
    out = await memory._dedup_store(None, pool, "fact", "Sales live in invoices.total")
    assert out == "stored" and store == [11]
    sql, args = pool.writes[0]
    assert args[-1] is None                       # graph_skip NULL


async def test_gate_only_marks_graphed_kinds(store):
    pool = StorePool(match=None)
    kept = []
    await memory._dedup_store(None, pool, "lesson", "To find X, filter Y first because Z",
                              graph_skip=memory.GRAPH_SKIP_TOOL_ONLY, kept=kept)
    assert kept == []
    assert pool.writes[0][1][-1] is None          # a lesson is never graphed anyway


async def test_gated_merge_into_a_kept_out_row_stays_out(store):
    pool = StorePool(match={"id": 5, "content": "old", "graph_skip": "dashboard turn",
                            "score": 0.9})
    kept = []
    out = await memory._dedup_store(None, pool, "fact", "Sales live in invoices.total_amount",
                                    graph_skip=memory.GRAPH_SKIP_DASHBOARD, kept=kept)
    assert out == "merged" and store == [] and kept == [5]
    sql, args = pool.writes[0]
    assert "UPDATE learnings" in sql and "graph_skip" in sql
    assert memory.GRAPH_SKIP_DASHBOARD in args


async def test_gated_merge_into_a_graphed_row_updates_the_graph(store):
    """The graph keeps following knowledge it already holds."""
    pool = StorePool(match={"id": 6, "content": "old", "graph_skip": None, "score": 0.9})
    kept = []
    out = await memory._dedup_store(None, pool, "fact", "Sales live in invoices.total_amount",
                                    graph_skip=memory.GRAPH_SKIP_DASHBOARD, kept=kept)
    assert out == "merged" and store == [6] and kept == []
    assert pool.writes[0][1][-1] is None


async def test_ungated_merge_brings_a_kept_out_row_into_the_graph(store):
    pool = StorePool(match={"id": 7, "content": "old", "graph_skip": "tool-only turn",
                            "score": 0.9})
    out = await memory._dedup_store(None, pool, "fact", "Sales live in invoices.total_amount")
    assert out == "merged" and store == [7]
    assert pool.writes[0][1][-1] is None          # graph_skip cleared with the merge


async def test_to_graph_clears_the_mark_when_the_episode_lands(monkeypatch):
    executed = []

    class Pool:
        async def execute(self, sql, *args):
            executed.append(sql)

    class Graph:
        async def add_episode(self, *a, **k):
            return True

    import app.memoryservice as ms
    monkeypatch.setattr(ms, "memory_graph", Graph())
    assert await memory.to_graph(3, "A fact", "fact", pool=Pool()) is True
    assert re.search(r"graphed_at = now\(\), graph_skip = NULL", executed[0])


# ── reflection: the gate and the trivial-turn skip ──────────────────────────

@pytest.fixture
def reflect(monkeypatch):
    state = SimpleNamespace(calls=[], dedup=[], records=[], seconds=5.0,
                            settings=_settings(),
                            items=[{"kind": "fact", "content": "Sales live in invoices.total"},
                                   {"kind": "lesson", "content": "To chart sales, sum by month"}])

    async def effective_configs():
        return {"reflection": {"prompt": None, "model": "m", "max_tokens": 100,
                               "temperature": 0.0}}

    stub = types.ModuleType("app.agents.graph")
    stub.effective_configs = effective_configs
    monkeypatch.setitem(sys.modules, "app.agents.graph", stub)

    async def items(rcfg, prompt, transcript, max_tokens, *, settings=None):
        state.calls.append(prompt)
        return list(state.items), None

    async def dedup(client, pool, kind, content, **kw):
        state.dedup.append((kind, kw.get("graph_skip")))
        if kw.get("graph_skip") and kind in memory.GRAPHED_KINDS and kw.get("kept") is not None:
            kw["kept"].append(len(state.dedup))
        return "stored"

    async def secs(run_id):
        return state.seconds

    async def ok(kind, content):
        return None

    async def pool():
        return object()

    async def record(kind, name="", **kw):
        state.records.append((kind, name, kw))
        return 1

    from app import jobruns
    monkeypatch.setattr(memory, "reflection_items", items)
    monkeypatch.setattr(memory, "_dedup_store", dedup)
    monkeypatch.setattr(memory, "_run_seconds", secs)
    monkeypatch.setattr(memory, "check_learning", ok)
    monkeypatch.setattr(memory, "get_pool", pool)
    monkeypatch.setattr(memory, "get_settings", lambda: state.settings)
    monkeypatch.setattr(jobruns, "record", record)
    return state


async def test_dashboard_turn_reflects_but_keeps_facts_out_of_the_graph(reflect):
    n = await memory.reflect_and_store(
        1, "ui:owner", "Build me a sales dashboard by month", "Built Sales by month ...",
        _log("send_progress", "delegate_to_dashboard_agent", "run_sql",
             "save_dashboard_version"))
    assert n == 2                                   # both learnings stored
    assert reflect.dedup == [("fact", memory.GRAPH_SKIP_DASHBOARD),
                             ("lesson", memory.GRAPH_SKIP_DASHBOARD)]
    (kind, name, kw), = reflect.records
    assert kind == "memory_skip" and "dashboard" in name
    assert kw["counts"] == {"facts": 1, "turns": 1}
    assert kw["collapse"] == "sum" and kw["routine"] is True and kw["task_run_id"] == 1


async def test_gate_off_graphs_as_before(reflect):
    reflect.settings = _settings(graph_skip_low_value_turns=False)
    await memory.reflect_and_store(1, "ui:owner", "Build me a sales dashboard",
                                   "Built it", _log("delegate_to_dashboard_agent"))
    assert reflect.dedup == [("fact", None), ("lesson", None)]
    assert reflect.records == []


async def test_normal_turn_is_not_gated(reflect):
    await memory.reflect_and_store(1, "ui:owner", "What were sales last month?",
                                   "Sales were 4.2 lakh", _log("run_sql"))
    assert reflect.dedup == [("fact", None), ("lesson", None)]
    assert reflect.records == []


@pytest.mark.parametrize("text", [
    "thanks", "Thanks!", "thank you so much 🙏", "ok", "Okay.", "hi", "Hello 👋",
    "good morning", "great, thanks", "got it", "👍", "nice work", "ok ji", "theek hai",
])
def test_trivial_turns(text):
    assert memory.is_trivial_turn(text, []) is True


@pytest.mark.parametrize("text,log", [
    ("thanks", _log("send_progress")),                       # a tool ran
    ("We close at 6pm", []),
    ("our fiscal year starts in April", []),
    ("thanks, Priya handles Mumbai expenses", []),
    ("yes", []), ("no", []), ("sure", []),                   # answers can confirm a rule
    ("u", []),
    ("ok " * 30, []),                                        # too long
    ("hi 2", []),                                            # a figure
])
def test_not_trivial(text, log):
    assert memory.is_trivial_turn(text, log) is False


async def test_trivial_turn_is_not_reflected(reflect):
    n = await memory.reflect_and_store(2, "ui:owner", "thanks!", "You're welcome!", [])
    assert n == 0 and reflect.calls == [] and reflect.dedup == []
    (kind, name, kw), = reflect.records
    assert kind == "memory_skip" and "trivial" in name and kw["counts"] == {"turns": 1}


async def test_a_hard_trivial_turn_still_reflects(reflect):
    reflect.seconds = memory.HARD_SECONDS + 5             # Quick 009: hard runs leave lessons
    await memory.reflect_and_store(3, "ui:owner", "ok", "Done.", [])
    assert len(reflect.calls) == 1 and "THIS RUN WAS HARD" in reflect.calls[0]


async def test_trivial_gate_off_reflects(reflect):
    reflect.settings = _settings(reflection_skip_trivial=False)
    await memory.reflect_and_store(4, "ui:owner", "thanks", "Welcome", [])
    assert len(reflect.calls) == 1


# ── backfill and rebuild honour the mark while the gate is on ───────────────

async def test_backfill_leaves_kept_out_facts_while_the_gate_is_on(monkeypatch):
    monkeypatch.setattr(memory_pipeline, "get_settings", lambda: _settings())
    assert "graph_skip IS NULL" in memory_pipeline.pending_learnings_sql()
    monkeypatch.setattr(memory_pipeline, "get_settings",
                        lambda: _settings(graph_skip_low_value_turns=False))
    assert "graph_skip" not in memory_pipeline.pending_learnings_sql()


async def test_backfill_and_pending_count_use_the_gated_selection(monkeypatch):
    seen = []

    class Pool:
        async def fetchval(self, sql, *a):
            seen.append(sql)
            return 0

    async def gp():
        return Pool()

    monkeypatch.setattr(memory_pipeline, "get_pool", gp)
    monkeypatch.setattr(memory_pipeline, "get_settings", lambda: _settings())
    await memory_pipeline.pending_counts()
    assert any("FROM learnings" in s and "graph_skip IS NULL" in s for s in seen)
    src = Path(memory_pipeline.__file__).read_text(encoding="utf-8")
    body = src[src.index("async def backfill_graph"):src.index("def _now()")]
    assert "pending_learnings_sql()" in body


async def test_rebuild_plan_leaves_kept_out_facts_while_the_gate_is_on(monkeypatch):
    now = datetime(2026, 10, 28)

    class PlanPool:
        async def fetch(self, sql, *a):
            if "FROM learnings" in sql:
                assert "graph_skip" in sql
                return [{"id": 1, "kind": "fact", "content": "A drop counts at 40 L.",
                         "revisions": 0, "updated_at": now, "graph_skip": None},
                        {"id": 2, "kind": "fact", "content": "Sales live in invoices.total.",
                         "revisions": 0, "updated_at": now, "graph_skip": "dashboard turn"}]
            return []

        async def fetchval(self, sql, *a):
            return None

    async def no_archive(pool):
        return []

    monkeypatch.setattr(memory_rebuild, "_archived", no_archive)
    monkeypatch.setattr(memory_rebuild, "get_settings", lambda: _settings(), raising=False)
    plan = await memory_rebuild.build_plan(PlanPool(), distill=False)
    assert [it["name"] for it in plan["items"] if it["text"]] == ["learning_1_r0"]
    assert plan["notes"]["learnings_kept_out"] == 1

    monkeypatch.setattr(memory_rebuild, "get_settings",
                        lambda: _settings(graph_skip_low_value_turns=False), raising=False)
    plan = await memory_rebuild.build_plan(PlanPool(), distill=False)
    assert [it["name"] for it in plan["items"] if it["text"]] == [
        "learning_1_r0", "learning_2_r0"]


async def test_rebuild_stamp_clears_the_mark():
    executed = []

    class Pool:
        async def execute(self, sql, *args):
            executed.append(sql)

    await memory_rebuild._stamp(Pool(), {"ref": ("learning", 4)})
    assert "graph_skip = NULL" in executed[0]


# ── community rebuild: off / weekly / nightly ───────────────────────────────

def test_nothing_reads_graph_communities():
    """If this fails, something now reads communities: revisit the default (off)."""
    writers = {"consolidation.py", "memoryservice.py", "config.py", "configservice.py"}
    hits = []
    for base in (ROOT / "backend/app", ROOT / "frontend/src"):
        for p in base.rglob("*"):
            if p.suffix not in {".py", ".ts", ".tsx", ".html", ".js", ".sql"}:
                continue
            if "__pycache__" in p.parts or p.name in writers:
                continue
            if re.search(r"communit", p.read_text(encoding="utf-8", errors="ignore"), re.I):
                hits.append(str(p.relative_to(ROOT)))
    assert hits == []
    import inspect
    from app import memoryservice
    assert "community" not in inspect.getsource(memoryservice._search_config).lower()


@pytest.fixture
def communities(monkeypatch):
    calls = []

    class Graph:
        async def build_communities(self):
            calls.append(1)
            return True

    import app.memoryservice as ms
    monkeypatch.setattr(ms, "memory_graph", Graph())
    return calls


async def test_community_rebuild_off_by_default(monkeypatch, communities):
    monkeypatch.setattr(consolidation, "get_settings", lambda: _settings())
    assert await consolidation.rebuild_communities() == "off"
    assert communities == []


async def test_community_rebuild_weekly_runs_on_sunday_only(monkeypatch, communities):
    monkeypatch.setattr(consolidation, "get_settings",
                        lambda: _settings(graph_community_rebuild="weekly"))
    saturday, sunday = datetime(2026, 10, 31, 3), datetime(2026, 11, 1, 3)
    assert await consolidation.rebuild_communities(now=saturday) == "weekly, not tonight"
    assert communities == []
    assert await consolidation.rebuild_communities(now=sunday) is True
    assert communities == [1]


async def test_community_rebuild_nightly(monkeypatch, communities):
    monkeypatch.setattr(consolidation, "get_settings",
                        lambda: _settings(graph_community_rebuild="nightly"))
    assert await consolidation.rebuild_communities() is True
    assert communities == [1]


def test_nightly_summary_names_a_skipped_community_rebuild():
    text, _counts, failed = consolidation.summarize_nightly(
        {"learnings_merged": 0, "communities_rebuilt": "off", "episodes_pruned": 0})
    assert "graph community rebuild skipped (off)" in text and failed == []
    text, _c, _f = consolidation.summarize_nightly({"communities_rebuilt": True})
    assert text.startswith("Graph communities rebuilt")



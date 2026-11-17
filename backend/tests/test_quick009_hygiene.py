"""Quick 009 — memory hygiene and the graph backfill.

Pure logic (pool, embeddings, graph and catalog are faked). Proves:
  * a recipe is stored only when its SQL passes the read-only guardrail and
    reads known business tables; the client's hallucinated DieselMonitoring
    recipe is refused on every store path, with the reason
  * a fact that only describes a dashboard's chart layout is not stored
  * nightly consolidation supersedes (never deletes) stored bad recipes, with
    a status_reason, and only when the live tenant catalog was read in full
  * the backfill sends pending learnings and closed cases, counts what the
    graph already holds as done, stops when the graph is down, and is
    single-flight
  * every compose file pins its stateful images; FalkorDB's volume sits on the
    image's real data dir
"""

import json
import re
import sys
import types
from pathlib import Path

import pytest

from app import consolidation, memory, memory_pipeline

DIESEL = ("SELECT total_consumption, avg_efficiency, discrepancy_count, max_gap "
          "FROM DieselMonitoring WHERE period = 'last 30 fuel days' GROUP BY machine_type;")

KNOWN = memory.KnownTables(
    names=frozenset({"fuel_logs", "public.fuel_logs", "machines", "public.machines"}),
    hidden=frozenset({"payroll", "public.payroll"}),
    documented=True, live_complete=True)


# ---------------------------------------------------------------- recipe check

@pytest.mark.parametrize("content,expect", [
    (DIESEL, "do not exist in the business database: DieselMonitoring"),
    ("Monthly diesel per machine: SELECT m.name, sum(f.litres) FROM fuel_logs f "
     "JOIN machines m ON m.id = f.machine_id GROUP BY 1;", None),
    ("Select the top machines:\n```sql\nSELECT * FROM public.fuel_logs LIMIT 5\n```", None),
    ("WITH t AS (SELECT * FROM fuel_logs) SELECT * FROM t", None),
    ("SELECT * FROM fuel_logs WHERE day > {{start_date}}", None),
    ("SELECT * FROM Fuel_Logs", None),                       # case-insensitive
    ("SELECT * FROM payroll", "the owner hid: payroll"),
    ("SELECT * FROM finance.fuel_logs", "do not exist"),     # wrong schema
    ("DELETE FROM fuel_logs", "not a read-only query"),
    ("SELECT pg_read_file('/etc/passwd') FROM fuel_logs", "read-only check"),
    ("Use the fuel log to compute efficiency", "must be a SQL query"),
])
def test_recipe_problem(content, expect):
    got = memory.recipe_problem(content, KNOWN)
    if expect is None:
        assert got is None, got
    else:
        assert got and expect in got, got


def test_recipe_tables_unchecked_without_a_catalog():
    blind = memory.KnownTables(frozenset(), frozenset(), False, False)
    assert not blind.judgeable
    assert memory.recipe_problem(DIESEL, blind) is None          # cannot judge
    assert memory.recipe_problem("DELETE FROM x", blind)         # SQL still checked


def test_context_store_alone_can_judge_when_tenant_is_down():
    docs_only = memory.KnownTables(frozenset({"fuel_logs"}), frozenset(), True, False)
    assert docs_only.judgeable
    assert "DieselMonitoring" in memory.recipe_problem(DIESEL, docs_only)


@pytest.mark.parametrize("content,refused", [
    ("The Diesel Monitoring dashboard has a bar chart of litres by machine in the "
     "top-left panel and a KPI tile for efficiency.", True),
    ("The fuel dashboard's layout: line chart first row, legend on the right column.", True),
    ("Diesel consumption is tracked per machine in litres; the dashboard shows efficiency.", False),
    ("Priya owns the fuel dashboard.", False),
    ("Machine M-12 is a bar chart of nothing", False),          # no dashboard named
])
def test_layout_fact(content, refused):
    assert bool(memory.layout_fact_problem(content)) is refused


# ---------------------------------------------------------------- store paths

class FakePool:
    def __init__(self):
        self.writes = []

    async def fetchrow(self, sql, *args):
        return None

    async def fetchval(self, sql, *args):
        self.writes.append(sql)
        return 1

    async def execute(self, sql, *args):
        self.writes.append(sql)


@pytest.fixture
def wired(monkeypatch):
    pool = FakePool()

    async def fake_pool():
        return pool

    async def fake_embed(text):
        return "[0.1]"

    async def fake_known(**kw):
        return KNOWN

    async def no_graph(*a, **k):
        return False

    monkeypatch.setattr(memory, "get_pool", fake_pool)
    monkeypatch.setattr(memory, "_embed", fake_embed)
    monkeypatch.setattr(memory, "known_tables", fake_known)
    monkeypatch.setattr(memory, "to_graph", no_graph)
    monkeypatch.setattr(memory, "AsyncOpenAI", lambda *a, **k: types.SimpleNamespace())
    return pool


async def test_store_learning_refuses_hallucinated_recipe(wired):
    with pytest.raises(ValueError, match="DieselMonitoring"):
        await memory.store_learning(DIESEL, "recipe", source="tool:remember_fact")
    assert wired.writes == []


async def test_store_learning_keeps_a_good_recipe(wired):
    out = await memory.store_learning(
        "SELECT machine_id, sum(litres) FROM fuel_logs GROUP BY 1", "recipe")
    assert out == "stored"
    assert any("INSERT INTO learnings" in w for w in wired.writes)


async def test_store_learning_refuses_layout_fact(wired):
    with pytest.raises(ValueError, match="chart layout"):
        await memory.store_learning(
            "The fuel dashboard shows a pie chart in the top-left panel and a "
            "legend on the right column", "fact")
    assert wired.writes == []


async def test_reflection_skips_refused_recipe(wired, monkeypatch):
    async def effective_configs():
        return {"reflection": {"prompt": None, "model": "m", "max_tokens": 50,
                               "temperature": 0.0}}

    stub = types.ModuleType("app.agents.graph")
    stub.effective_configs = effective_configs
    monkeypatch.setitem(sys.modules, "app.agents.graph", stub)
    payload = json.dumps({"learnings": [
        {"kind": "recipe", "content": DIESEL},
        {"kind": "fact", "content": "Fuel litres live in fuel_logs.litres"},
    ]})

    class Client:
        def __init__(self, *a, **k):
            async def create(**kw):
                msg = types.SimpleNamespace(content=payload)
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=create))

    monkeypatch.setattr(memory, "AsyncOpenAI", Client)
    n = await memory.reflect_and_store(run_id=5, thread_id="t", user_message="u",
                                       reply="r", tool_log=[])
    assert n == 1                                   # the fact only
    assert len([w for w in wired.writes if "INSERT INTO learnings" in w]) == 1


def test_remember_fact_surfaces_the_refusal_reason():
    src = Path(memory.__file__).with_name("agents").joinpath("graph.py").read_text(
        encoding="utf-8")
    body = src[src.index("async def remember_fact"):src.index("async def remember_fact") + 2500]
    assert "except ValueError as e:" in body and 'f"ERROR: {e}"' in body


# ---------------------------------------------------------------- nightly supersede

class RecipePool:
    def __init__(self, rows):
        self.rows = rows
        self.updates = []

    async def fetch(self, sql, *args):
        return self.rows

    async def execute(self, sql, *args):
        assert "DELETE" not in sql.upper()
        self.updates.append((sql, args))
        return "UPDATE 1"


async def test_supersede_bad_recipes(monkeypatch):
    pool = RecipePool([
        {"id": 1, "content": DIESEL},
        {"id": 2, "content": "SELECT count(*) FROM fuel_logs"},
        {"id": 3, "content": "Divide litres by hours for efficiency"},   # prose: left alone
    ])

    async def fake_pool():
        return pool

    async def fake_known(**kw):
        return KNOWN

    monkeypatch.setattr(consolidation, "get_pool", fake_pool)
    monkeypatch.setattr(memory, "known_tables", fake_known)
    assert await consolidation.supersede_bad_recipes() == 1
    (sql, args), = pool.updates
    assert "status = 'superseded'" in sql and "status_reason" in sql
    assert args[0] == 1 and "DieselMonitoring" in args[1]


async def test_supersede_skipped_without_full_live_catalog(monkeypatch):
    pool = RecipePool([{"id": 1, "content": DIESEL}])

    async def fake_pool():
        return pool

    async def docs_only(**kw):
        return memory.KnownTables(frozenset({"fuel_logs"}), frozenset(), True, False)

    monkeypatch.setattr(consolidation, "get_pool", fake_pool)
    monkeypatch.setattr(memory, "known_tables", docs_only)
    out = await consolidation.supersede_bad_recipes()
    assert isinstance(out, str) and out.startswith("skipped")
    assert pool.updates == []


# ---------------------------------------------------------------- backfill

class Graph:
    def __init__(self, available=True, have=()):
        self._available = available
        self.have = set(have)
        self.added = []

    async def _ensure(self):
        return self._available

    async def has_episode(self, group_id, *, name=None, content=None):
        return name in self.have or content in self.have

    async def add_episode(self, text, source, name=None, **kw):
        self.added.append(name)
        return True


class BackfillPool:
    def __init__(self, learnings, cases):
        self.learnings, self.cases = learnings, cases
        self.stamps = []

    async def fetch(self, sql, *args):
        if "FROM learnings" in sql:
            return self.learnings[:args[0]]
        return self.cases[:args[0]]

    async def execute(self, sql, *args):
        self.stamps.append((sql.split("SET")[1].split("=")[0].strip(), args[0]))


@pytest.fixture
def backfill_env(monkeypatch):
    def setup(graph, pool):
        async def fake_pool():
            return pool

        async def fake_distill(cid):
            if cid in getattr(pool, "empty_cases", ()):
                return "empty"          # Quick 014: nothing durable, nothing written
            graph.added.append(f"case_{cid}_close")
            return "stored"

        monkeypatch.setattr(memory_pipeline, "memory_graph", graph)
        monkeypatch.setattr(memory_pipeline, "get_pool", fake_pool)
        monkeypatch.setattr(memory_pipeline, "distill_case_outcome", fake_distill)
        monkeypatch.setattr(memory, "get_pool", fake_pool)
        import app.memoryservice as ms
        monkeypatch.setattr(ms, "memory_graph", graph)
    return setup


async def test_backfill_sends_pending_and_skips_what_is_there(backfill_env):
    graph = Graph(have={"learning_2_r0"})
    pool = BackfillPool(
        learnings=[{"id": 1, "kind": "fact", "content": "A is B", "revisions": 0},
                   {"id": 2, "kind": "preference", "content": "C", "revisions": 0}],
        cases=[{"id": 7}])
    backfill_env(graph, pool)
    out = await memory_pipeline.backfill_graph(limit=10)
    assert out["learnings"] == {"sent": 1, "already": 1, "failed": 0}
    assert out["cases"] == {"sent": 1, "already": 0, "failed": 0, "skipped": 0}
    assert graph.added == ["learning_1_r0", "case_7_close"]
    assert ("graphed_at", 1) in pool.stamps and ("graphed_at", 2) in pool.stamps


async def test_backfill_counts_a_case_with_nothing_durable_as_skipped(backfill_env):
    """Quick 014: an empty distillation is not a failure to retry forever."""
    graph = Graph()
    pool = BackfillPool(learnings=[], cases=[{"id": 7}, {"id": 8}])
    pool.empty_cases = {8}
    backfill_env(graph, pool)
    out = await memory_pipeline.backfill_graph(limit=10)
    assert out["cases"] == {"sent": 1, "already": 0, "failed": 0, "skipped": 1}
    assert graph.added == ["case_7_close"]


async def test_backfill_is_bounded(backfill_env):
    graph = Graph()
    pool = BackfillPool(
        learnings=[{"id": i, "kind": "fact", "content": f"fact {i}", "revisions": 0}
                   for i in range(1, 6)],
        cases=[{"id": 9}])
    backfill_env(graph, pool)
    out = await memory_pipeline.backfill_graph(limit=3)
    assert out["learnings"]["sent"] == 3 and out["cases"]["sent"] == 0


async def test_backfill_needs_a_connected_graph(backfill_env):
    graph = Graph(available=False)
    pool = BackfillPool(learnings=[{"id": 1, "kind": "fact", "content": "x", "revisions": 0}],
                        cases=[])
    backfill_env(graph, pool)
    out = await memory_pipeline.backfill_graph()
    assert out["stopped"] == "graph not connected" and graph.added == []


async def test_backfill_is_single_flight(backfill_env):
    backfill_env(Graph(), BackfillPool([], []))
    async with memory_pipeline._backfill_lock:
        out = await memory_pipeline.backfill_graph()
        assert out["skipped"] == "running"
        assert memory_pipeline.start_backfill() is False


# ---------------------------------------------------------------- image pins

APP_ROOT = Path(__file__).resolve().parents[2]
STATEFUL = ("falkordb/falkordb", "pgvector/pgvector", "postgres", "langfuse/langfuse")


def _images(text):
    return re.findall(r"^\s*image:\s*['\"]?([^\s'\"#]+)", text, re.M)


def test_compose_files_pin_stateful_images():
    files = sorted(APP_ROOT.glob("docker-compose*.yml"))
    assert files
    seen = []
    for f in files:
        for img in _images(f.read_text(encoding="utf-8")):
            repo, _, tag = img.partition(":")
            if repo not in STATEFUL:
                continue
            seen.append(img)
            assert tag and tag != "latest", f"{f.name}: {img} floats"
            assert re.search(r"\d+\.\d+\.\d+", tag), f"{f.name}: {img} is not an exact version"
    assert any(i.startswith("falkordb/falkordb:") for i in seen)


def test_falkordb_volume_is_on_the_real_data_dir():
    text = (APP_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    block = text[text.index("  falkordb:"):text.index("  langfuse:")]
    assert "falkordata:/var/lib/falkordb/data" in block
    assert "falkordata:/data" not in block


# ---------------------------------------------------------------- lessons (owner request)

def _call(tool="run_sql", sql="SELECT 1", result='[{"n": 1}]'):
    return {"tool": tool, "args": {"sql": sql}, "result_digest": result}


TIMEOUT = ("SQL ERROR (read it, fix the query, retry): QueryCanceledError: "
           "canceling statement due to statement timeout")


def test_lesson_is_a_valid_kind():
    assert "lesson" in memory.VALID_KINDS
    assert "lesson" in memory.REFLECTION_PROMPT and "web app" in memory.REFLECTION_PROMPT
    assert "WhatsApp data assistant" not in memory.REFLECTION_PROMPT


def test_run_hardness():
    assert memory.run_hardness([_call()] * 3, 20) == []
    assert memory.run_hardness([_call()] * 12, 20) == ["12 tool calls"]
    why = memory.run_hardness([_call(result=TIMEOUT), _call()], 20)
    assert why == ["1 failed tool call(s), 1 timeout(s)"]
    assert memory.run_hardness([_call()], 150) == ["150 s"]


def test_transcript_keeps_errors_fixes_and_the_end_not_just_the_head():
    log = ([_call(sql=f"SELECT {i} FROM sales_orders -- " + "x" * 400) for i in range(10)]
           + [_call(sql="SELECT * FROM VehicleTelemetry", result=TIMEOUT),
              _call(sql="SELECT * FROM VehicleTelemetry WHERE plate = 'MH12' AND ts > now() - interval '7 days'")]
           + [_call(sql=f"SELECT {i} FROM order_lines -- " + "y" * 400) for i in range(20)]
           + [_call(sql="SELECT final_answer FROM sales_orders")])
    t = memory.build_transcript("find fuel drops " * 50, "done " * 200, log, cap=6000)
    assert len(t) <= 6000
    doc = json.loads(t)
    steps = {e.get("step") for e in doc["tools"] if "step" in e}
    assert 11 in steps and 12 in steps          # the timeout and the fix that followed
    assert len(log) in steps                    # the final successful query
    assert 1 in steps                           # how it started
    assert any("omitted_calls" in e for e in doc["tools"])
    assert doc["tool_calls"] == len(log)


async def test_hard_run_asks_for_lessons_and_keeps_five(wired, monkeypatch):
    async def effective_configs():
        return {"reflection": {"prompt": "CUSTOM OWNER PROMPT", "model": "m",
                               "max_tokens": 500, "temperature": 0.0}}

    stub = types.ModuleType("app.agents.graph")
    stub.effective_configs = effective_configs
    monkeypatch.setitem(sys.modules, "app.agents.graph", stub)
    items = [{"kind": "fact", "content": f"Fact number {i} about sales_orders"} for i in range(4)]
    items.append({"kind": "lesson", "content": "To find stockouts, filter inventory_snapshots "
                                               "by product first; a full scan of fuel_logs timed out."})
    items.append({"kind": "fact", "content": "A sixth item that must be dropped"})
    seen = {}

    class Client:
        def __init__(self, *a, **k):
            async def create(**kw):
                seen.update(kw)
                msg = types.SimpleNamespace(content=json.dumps({"learnings": items}))
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=create))

    monkeypatch.setattr(memory, "AsyncOpenAI", Client)
    log = [_call(result=TIMEOUT)] + [_call()] * 13
    n = await memory.reflect_and_store(run_id=7, thread_id="t", user_message="u",
                                       reply="r", tool_log=log)
    system = seen["messages"][0]["content"]
    assert system.startswith("CUSTOM OWNER PROMPT")          # owner prompt kept
    assert "THIS RUN WAS HARD" in system and "lesson" in system
    assert seen["max_tokens"] >= memory.HARD_MAX_TOKENS
    assert n == 5                                            # five kept, sixth dropped


def test_lesson_naming_a_missing_table_is_refused():
    lesson = ("To get fuel efficiency, join fuel_logs to the DieselMonitoring table "
              "on machine; scanning VehicleTelemetry needs a plate filter.")
    known = memory.KnownTables(frozenset({"fuel_logs", "vehicletelemetry"}), frozenset(), True, True)
    assert "DieselMonitoring" in memory.lesson_problem(lesson, known)
    ok = "To find fuel drops in VehicleTelemetry, filter by plate first; fuel_logs.litres is litres."
    assert memory.lesson_problem(ok, known) is None
    # Plain words and column names are not mistaken for tables.
    assert memory.lesson_tables("filter the telemetry table on created_at in order_id") == []


async def test_store_learning_refuses_bad_lesson(wired):
    with pytest.raises(ValueError, match="DieselMonitoring"):
        await memory.store_learning(
            "To find fuel use, query DieselMonitoring by machine_type first.", "lesson")


def test_remember_fact_accepts_lesson_kind():
    src = Path(memory.__file__).with_name("agents").joinpath("graph.py").read_text(encoding="utf-8")
    body = src[src.index("async def remember_fact"):src.index("async def remember_fact") + 2500]
    assert '"recipe", "lesson")' in body


def test_schema_widens_the_kind_check_for_existing_databases():
    schema = (Path(memory.__file__).with_name("schema.sql")).read_text(encoding="utf-8")
    assert "DROP CONSTRAINT IF EXISTS learnings_kind_check" in schema
    m = re.search(r"ADD CONSTRAINT learnings_kind_check\s+CHECK\s*\(kind IN \(([^)]*)\)", schema)
    assert m and "'lesson'" in m.group(1)
    inline = re.search(r"kind\s+TEXT NOT NULL CHECK \(kind IN \(([^)]*)\)\)", schema)
    assert inline and "'lesson'" in inline.group(1)

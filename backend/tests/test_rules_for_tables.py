"""Quick 002: approved rules and KPIs reach the SQL step by table.

knowledge.rules_for_tables / format_rules / query_tables, and the graph tools
that use them: search_business_context appends the rules for the tables it
found; run_sql and run_sql_to_csv hand over unseen rules once per agent
conversation instead of running; delegates and runs get their own seen-set.
No DB, LLM or network: pool and guardrails are faked.
"""

import json

import pytest

from app import knowledge
from app.agents import graph

FUEL_RULE = {"kind": "rule", "ref": "rule:fuel-source-by-machine-type",
             "title": "Daily diesel source depends on machine type",
             "body": "Use DailyFuelReports for MachineType = 'barge' and ShiftPerformanceRecords for the rest.",
             "tables": json.dumps(["business_db.public.DailyFuelReports",
                                   "business_db.public.ShiftPerformanceRecords",
                                   "business_db.public.Machines"])}
LIVE_RULE = {"kind": "rule", "ref": "rule:live-machines", "title": "Live machines exclude archived rows",
             "body": "Filter IsDeleted = false.", "tables": json.dumps(["business_db.public.Machines"])}
BARGE_KPI = {"kind": "kpi", "ref": "kpi:barge-daily-diesel", "title": "Barge daily diesel",
             "body": "SUM(DailyFuelReports.ConsumptionLiters) for barges.",
             "tables": json.dumps(["business_db.public.DailyFuelReports", "business_db.public.Machines"])}


class _Pool:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        keys = set(args[0]) if args else set()
        return [r for r in self.rows
                if not keys or {knowledge.table_key(t) for t in json.loads(r["tables"])} & keys]


@pytest.fixture
def pool(monkeypatch):
    p = _Pool([LIVE_RULE, BARGE_KPI, FUEL_RULE])

    async def get_pool():
        return p

    import app.db
    monkeypatch.setattr(app.db, "get_pool", get_pool)
    return p


# ── pure helpers ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    ("business_db.public.DailyFuelReports", "dailyfuelreports"),
    ('public."Machines"', "machines"),
    ("Machines", "machines"),
    ("", ""),
    (None, ""),
])
def test_table_key(raw, want):
    assert knowledge.table_key(raw) == want


def test_query_tables_reads_quoted_schema_and_joins():
    sql = ('SELECT m."MachineType", sum(r."ConsumptionLiters") FROM "DailyFuelReports" r '
           'JOIN public."Machines" m ON m."Id" = r."MachineId" GROUP BY 1')
    assert knowledge.query_tables(sql) == {"dailyfuelreports", "machines"}


def test_query_tables_skips_cte_names_and_bad_sql():
    sql = ('WITH combined AS (SELECT "MachineId" FROM "ShiftPerformanceRecords") '
           'SELECT * FROM combined JOIN "Machines" m ON m."Id" = combined."MachineId"')
    assert knowledge.query_tables(sql) == {"shiftperformancerecords", "machines"}
    assert knowledge.query_tables("SELEC nonsense ((") == set()


def _item(ref, body="b", title="t", matched=("x",), kind="rule"):
    return {"kind": kind, "ref": ref, "title": title, "body": body, "tables": list(matched),
            "matched": list(matched)}


def test_format_rules_skips_seen_and_returns_refs():
    items = [_item("rule:a"), _item("rule:b")]
    text, refs = knowledge.format_rules(items, {"rule:a"}, ["business_db.public.X"])
    assert refs == ["rule:b"]
    assert "rule:b" in text and "rule:a" not in text
    assert text.startswith("APPROVED RULES AND KPIS FOR TABLES x")
    assert knowledge.RULES_COMBINE in text


def test_format_rules_nothing_new_is_empty():
    assert knowledge.format_rules([_item("rule:a")], {"rule:a"}) == ("", [])
    assert knowledge.format_rules([]) == ("", [])


def test_format_rules_lists_overflow_by_ref_and_counts_it_shown():
    items = [_item("rule:a", body="x" * 500), _item("rule:b", body="y" * 500, title="B title")]
    text, refs = knowledge.format_rules(items, budget=600)
    assert "x" * 500 in text and "y" * 500 not in text
    assert knowledge.RULES_MORE + "rule:b (B title)" in text
    assert refs == ["rule:a", "rule:b"]


def test_format_rules_always_keeps_one_full_item():
    text, _ = knowledge.format_rules([_item("rule:a", body="z" * 5000)], budget=10)
    assert "z" * knowledge.RULE_BODY_CHARS in text


# ── rules_for_tables ─────────────────────────────────────────────────────

async def test_rules_for_tables_orders_most_specific_first(pool):
    out = await knowledge.rules_for_tables(['"DailyFuelReports"', "Machines"])
    assert [i["ref"] for i in out] == [
        "rule:fuel-source-by-machine-type",  # 2 matched, rule
        "kpi:barge-daily-diesel",            # 2 matched, kpi
        "rule:live-machines",                # 1 matched
    ]
    assert out[0]["matched"] == ["dailyfuelreports", "machines"]
    assert pool.calls[0][1][0] == ["dailyfuelreports", "machines"]


async def test_rules_for_tables_empty_input_skips_db(pool):
    assert await knowledge.rules_for_tables([]) == []
    assert pool.calls == []


async def test_rules_for_tables_fails_soft(monkeypatch):
    async def boom():
        raise OSError("db down")

    import app.db
    monkeypatch.setattr(app.db, "get_pool", boom)
    assert await knowledge.rules_for_tables(["Machines"]) == []


async def test_rules_for_tables_sql_is_read_only_and_approved_only(pool):
    await knowledge.rules_for_tables(["Machines"], ["rule:x"])
    sql = pool.calls[0][0].upper()
    assert "STATUS = 'APPROVED'" in sql and "KIND IN ('RULE', 'KPI')" in sql
    for verb in ("INSERT", "UPDATE", "DELETE", "DROP"):
        assert verb not in sql


# ── graph tools ──────────────────────────────────────────────────────────

@pytest.fixture
def quiet(monkeypatch):
    logged = []

    async def log_tool(name, args, result, start, refused=False):
        logged.append((name, refused, result))

    monkeypatch.setattr(graph, "_log_tool", log_tool)
    return logged


@pytest.fixture
def ran(monkeypatch):
    calls = []

    async def run(sql, limit=None):
        calls.append(sql)
        return [{"n": 1}]

    monkeypatch.setattr(graph, "run_readonly_sql", run)
    return calls


SQL = 'SELECT sum(r."ConsumptionLiters") FROM "DailyFuelReports" r JOIN "Machines" m ON m."Id" = r."MachineId"'


async def test_run_sql_gates_once_then_runs(pool, quiet, ran):
    tok = graph.ctx_rules_seen.set(set())
    try:
        first = await graph.run_sql.ainvoke({"sql": SQL})
        assert first.startswith("RULES CHECK (query not run yet)")
        assert "rule:fuel-source-by-machine-type" in first and ran == []
        assert quiet[-1][:2] == ("run_sql", True)  # logged as refused: nothing ran
        second = await graph.run_sql.ainvoke({"sql": SQL})
        assert json.loads(second) == [{"n": 1}] and ran == [SQL]
    finally:
        graph.ctx_rules_seen.reset(tok)


async def test_run_sql_without_run_context_never_gates(pool, quiet, ran):
    assert graph.ctx_rules_seen.get() is None
    out = await graph.run_sql.ainvoke({"sql": SQL})
    assert json.loads(out) == [{"n": 1}]


async def test_run_sql_gate_skips_tables_without_rules(pool, quiet, ran):
    tok = graph.ctx_rules_seen.set(set())
    try:
        out = await graph.run_sql.ainvoke({"sql": 'SELECT * FROM "Users" LIMIT 5'})
        assert json.loads(out) == [{"n": 1}]
    finally:
        graph.ctx_rules_seen.reset(tok)


async def test_run_sql_to_csv_gates_too(pool, quiet, ran):
    tok = graph.ctx_rules_seen.set(set())
    try:
        out = await graph.run_sql_to_csv.ainvoke({"sql": SQL, "filename": "x.csv"})
        assert out.startswith("RULES CHECK") and ran == []
        assert quiet[-1][:2] == ("run_sql_to_csv", True)
    finally:
        graph.ctx_rules_seen.reset(tok)


async def test_search_appends_rules_for_hit_tables_and_marks_seen(pool, quiet, ran, monkeypatch):
    async def search(q, limit=8):
        return [{"score": 0.7, "type": "column", "table_name": "DailyFuelReports",
                 "column_name": "ConsumptionLiters", "text": "litres", "ref": "x"}]

    monkeypatch.setattr(graph, "_search_ctx", search)
    tok = graph.ctx_rules_seen.set(set())
    try:
        out = await graph.search_business_context.ainvoke({"query": "diesel"})
        assert "APPROVED RULES AND KPIS FOR TABLES dailyfuelreports" in out
        assert "rule:fuel-source-by-machine-type" in out
        assert "rule:live-machines" not in out  # Machines was not among the hits
        # the query now touches Machines too: only the unseen Machines rule comes back
        gate = await graph.run_sql.ainvoke({"sql": SQL})
        assert "rule:live-machines" in gate and "rule:fuel-source" not in gate
    finally:
        graph.ctx_rules_seen.reset(tok)


async def test_search_does_not_repeat_rules_already_in_hits(pool, quiet, monkeypatch):
    async def search(q, limit=8):
        return [{"score": 0.8, "type": "rule", "ref": "rule:fuel-source-by-machine-type",
                 "text": "Business rule ...", "table_name": None}]

    monkeypatch.setattr(graph, "_search_ctx", search)
    tok = graph.ctx_rules_seen.set(set())
    try:
        out = await graph.search_business_context.ainvoke({"query": "fuel source"})
        head, _, block = out.partition("\n\nAPPROVED RULES")
        assert "rule:fuel-source-by-machine-type" in head
        assert "rule:fuel-source-by-machine-type" not in block
        assert "rule:live-machines" in block  # via the hit rule's own tables
    finally:
        graph.ctx_rules_seen.reset(tok)


async def test_search_by_ref_reads_exact_items(quiet, monkeypatch):
    seen = {}

    async def by_ref(refs):
        seen["refs"] = refs
        return [{"type": "rule", "ref": refs[0], "text": "Business rule x", "tables": []}]

    async def no_note(*a, **k):
        return ""

    monkeypatch.setattr(graph._knowledge, "rules_by_ref", by_ref)
    monkeypatch.setattr(graph, "_rules_note", no_note)
    out = await graph.search_business_context.ainvoke({"query": "rule:fuel-day-ist, kpi:open-alerts"})
    assert seen["refs"] == ["rule:fuel-day-ist", "kpi:open-alerts"]
    assert "Business rule x" in out


def test_delegate_and_run_scope_the_seen_set():
    """Delegates never see the supervisor's messages, so each gets a fresh set
    (restored afterwards); graph.run opens one per run and closes it."""
    import inspect

    src = inspect.getsource(graph._build)
    assert "ctx_rules_seen.set(set())" in src and "ctx_rules_seen.reset(seen_token)" in src
    run_src = inspect.getsource(graph.run)
    assert "ctx_rules_seen.set(set())" in run_src and "ctx_rules_seen.reset(seen_token)" in run_src


# ── tool-call audit keeps long SQL (the guard's ledger) ─────────────────

def test_args_json_stays_valid_and_bounded_for_long_sql():
    sql = "SELECT " + ", ".join(f'"Col{i}"' for i in range(600)) + ' FROM "T"'
    out = graph._args_json({"sql": sql})
    parsed = json.loads(out)
    assert len(out) <= graph.ARGS_JSON_CHARS
    assert parsed["sql"].startswith("SELECT \"Col0\"") and parsed["sql"].endswith("...")


def test_args_json_short_args_unchanged():
    assert graph._args_json({"sql": "SELECT 1", "filename": "x.csv"}) ==         json.dumps({"sql": "SELECT 1", "filename": "x.csv"})


def test_args_json_many_long_values_still_valid():
    out = graph._args_json({f"k{i}": "v" * 3000 for i in range(50)})
    assert len(out) <= graph.ARGS_JSON_CHARS
    json.loads(out)

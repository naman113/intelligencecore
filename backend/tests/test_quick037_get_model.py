"""Quick 037: get_model answers a compact summary by default and one section on demand.

The summary carries what picks the right measure (each measure's aggregation, table, filter,
time column, unit and description on one line; tables with grain, key, time column and
freshness; relationships; pending proposals by name). Full rows come one section at a time.
Both stay under model_result_chars, and every cut says what to ask next.

Run this file as a script to print the before/after sizes on the Distributor-sized fixture:
    cd backend && uv run python tests/test_quick037_get_model.py
"""
import asyncio
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import quick037_fixture as fx  # noqa: E402
from app import configservice, freshness, semanticmodel, warehouse  # noqa: E402
from app import modeltools as mt  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
AS_OF = datetime(2026, 10, 27, 5, 0, 12, tzinfo=timezone.utc)

# ── fakes ──────────────────────────────────────────────────────────────────

class _Pool:
    def __init__(self, pending):
        self.pending = pending

    async def fetch(self, sql, *args):
        assert "FROM actions" in sql
        return [{"id": 900 + i, "payload": json.dumps(p)} for i, p in enumerate(self.pending)]


@dataclass
class _Fresh:
    stale: bool
    import_reads: tuple
    live_reads: tuple = ()
    failed: bool = False

    @property
    def as_of_iso(self):
        return AS_OF.isoformat()


class Env:
    pass


def _wire(monkeypatch, e):
    async def load_rows(include_retired=False):
        return e.rows

    async def warehouse_objects():
        return e.wh

    async def catalog_columns():
        if e.catalog_error:
            raise e.catalog_error
        return e.columns

    async def foreign_keys():
        return e.fks

    async def source_snapshots():
        return []

    async def introspect(source="business_db"):
        if e.catalog_error:
            raise e.catalog_error
        return e.snapshot

    async def load_facts():
        return object()

    async def for_sql(sql, cache=None, now=None):
        rel = sql.rsplit(" ", 1)[1]
        return _Fresh(stale=rel in e.stale, import_reads=(rel,))

    async def get_pool():
        return e.pool

    async def log_tool(name, args, result, start, refused=False):
        e.logs.append((name, args, result))

    monkeypatch.setattr(semanticmodel, "load_rows", load_rows)
    monkeypatch.setattr(semanticmodel, "warehouse_objects", warehouse_objects)
    monkeypatch.setattr(semanticmodel, "catalog_columns", catalog_columns)
    monkeypatch.setattr(semanticmodel, "foreign_keys", foreign_keys)
    monkeypatch.setattr(semanticmodel, "source_snapshots", source_snapshots)
    monkeypatch.setattr(mt.schemadiscovery, "introspect", introspect)
    monkeypatch.setattr(freshness, "load_facts", load_facts)
    monkeypatch.setattr(freshness, "for_sql", for_sql)
    monkeypatch.setattr(mt, "get_pool", get_pool)
    monkeypatch.setattr(mt, "_log_tool", log_tool)
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: True)


def _env() -> Env:
    e = Env()
    e.rows = {"tables": fx.tables(), "relationships": fx.relationships(), "measures": fx.measures(),
              "indexes": []}
    e.wh = {t["name"]: "v" for t in e.rows["tables"]}
    e.columns = fx.columns()
    e.snapshot = fx.catalog_snapshot()
    e.fks = [{"schema": fx.SRC, "table": "jobs", "column": "CustomerID", "to_table": "customers",
              "to_column": "CustomerID"},
             {"schema": fx.SRC, "table": "invoices", "column": "JobID", "to_table": "jobs",
              "to_column": "JobID"}]
    e.pool = _Pool(fx.pending_payloads())
    e.stale = {"model.contracts"}
    e.catalog_error = None
    e.logs = []
    return e


@pytest.fixture
def env(monkeypatch):
    e = _env()
    _wire(monkeypatch, e)
    return e


@pytest.fixture
def setting(monkeypatch):
    """Override one setting through the environment (the configservice path) for one test."""
    def _set(name, value):
        monkeypatch.setenv(name.upper(), str(value))
        get_settings.cache_clear()
    yield _set
    get_settings.cache_clear()


async def gm(**kw):
    return await mt.get_model.ainvoke(kw)


def _legacy(e) -> str:
    pend = [{"object": p["object"], "name": p["name"], "op": p["op"], "action_id": 900 + i}
            for i, p in enumerate(fx.pending_payloads())]
    modeled = {t["relation"] for t in e.rows["tables"]}
    cands = [{"relation": f"{t['schema']}.{t['table']}", "row_estimate": t["row_estimate"]}
             for t in e.snapshot["tables"] if f"{t['schema']}.{t['table']}" not in modeled]
    fr = {t["relation"]: {"mode": "import", "data_as_of": AS_OF.isoformat(),
                          "stale": t["relation"] in e.stale} for t in e.rows["tables"]}
    return fx.legacy_get_model(e.rows, pend, cands, len(e.fks), e.wh, fr)


# ── get_model: the summary ─────────────────────────────────────────────────

async def test_fixture_is_distributor_sized(env):
    assert len(env.rows["tables"]) == 15 and len(env.rows["relationships"]) == 16
    assert len(env.rows["measures"]) == 50
    assert len(_legacy(env)) > 55000, "the old answer is about the 62k measured on Distributor"


async def test_no_arguments_gives_a_compact_summary(env):
    out = await gm()
    assert not out.startswith("TOOL ERROR")
    assert len(out) <= get_settings().model_result_chars
    assert len(out) * 4 < len(_legacy(env)), (len(out), len(_legacy(env)))
    assert out.startswith("SEMANTIC MODEL SUMMARY")
    assert "15 tables" in out and "16 relationships" in out and "50 measures" in out
    assert "6 pending proposals" in out
    assert "regexp" not in out and "to_date(" not in out, "view SQL only on demand"
    assert env.logs and env.logs[-1][0] == "get_model" and env.logs[-1][1] == {}


async def test_summary_names_every_table_measure_and_relationship(env):
    out = await gm()
    for t in env.rows["tables"]:
        assert re.search(rf"^- {t['name']} = {re.escape(t['relation'])} ", out, re.M), t["name"]
    for m in env.rows["measures"]:
        assert re.search(rf"^- {m['name']}: ", out, re.M), m["name"]
    for r in env.rows["relationships"]:
        line = f"{r['from_relation']}.{r['from_columns'][0]} -> {r['to_relation']}.{r['to_columns'][0]}"
        assert f"- {line}" in out, line


async def test_summary_tables_carry_grain_time_and_freshness(env):
    out = await gm()
    jobs = next(ln for ln in out.splitlines() if ln.startswith("- jobs = "))
    for w in ("view", "fact", "one row per job", "key job_id", "time scheduled_date", "import",
              "data_as_of 2026-10-27 05:00 UTC"):
        assert w in jobs, (w, jobs)
    contracts = next(ln for ln in out.splitlines() if ln.startswith("- contracts = "))
    assert "STALE" in contracts and "STALE" not in jobs


async def test_summary_measure_lines_are_enough_to_pick_the_measure(env):
    out = await gm()
    lines = {ln.split(":", 1)[0][2:]: ln for ln in out.splitlines() if ln.startswith("- ") and ": " in ln}
    rev = lines["billable_revenue"]
    assert "sum(qty * unit_price * (1 - coalesce(discount_pct, 0) / 100)) on model.job_lines" in rev
    assert "billable in (Y, true)" in rev and "model.jobs.status in (completed)" in rev
    assert "time model.jobs.completed_date" in rev and "unit GBP" in rev
    assert "Billable value of parts and labour" in rev
    # same table, different filter and time column: both visible in one line each
    assert "status not_in (draft, void)" in lines["invoiced_amount"]
    assert "time invoice_date" in lines["invoiced_amount"]
    assert "time paid_date" in lines["cash_collected"]
    assert "time due_date" in lines["overdue_amount"]
    assert "no time column" in lines["contract_mrr"]
    assert "ratio quotes_won / quotes_sent" in lines["quote_win_rate"]
    assert "derived (current - previous) / NULLIF(previous, 0) from cash_collected, " \
           "cash_collected (1 month earlier)" in lines["cash_collected_mom_growth"]
    assert "technician_id is_not_null" in lines["technicians_dispatched"]


async def test_summary_lists_pending_by_name_and_counts_the_catalog(env):
    out = await gm()
    assert "measure first_visit_resolution (create)" in out
    assert "measure labour_lines_value (retire)" in out
    assert "69 catalog tables not modeled yet" in out
    assert "2 foreign keys not proposed yet" in out
    assert 'section="candidates"' in out and 'section="pending"' in out
    assert "src_distributor.job_notes" not in out, "candidates only on demand"


async def test_summary_tells_how_to_drill_in(env):
    out = await gm()
    head = "\n".join(out.splitlines()[:4])
    assert "query_measure" in head
    assert 'get_model(section="measures", name=' in head
    assert 'section="tables"' in head


async def test_empty_model_summary_never_errors(env):
    env.rows = {"tables": [], "relationships": [], "measures": [], "indexes": []}
    env.pool.pending = []
    out = await gm()
    assert out.startswith("SEMANTIC MODEL SUMMARY") and "0 tables" in out and "0 measures" in out
    assert "No approved model yet" in out


async def test_catalog_error_is_reported_not_raised(env):
    env.catalog_error = RuntimeError("down")
    out = await gm()
    assert "catalog unavailable (RuntimeError)" in out and "50 measures" in out
    det = json.loads(await gm(section="candidates"))
    assert det["catalog_error"] == "RuntimeError"


async def test_summary_over_the_cap_keeps_every_measure_name(env, setting):
    setting("model_result_chars", 4000)
    out = await gm()
    assert len(out) <= 4000
    for m in env.rows["measures"]:
        assert m["name"] in out, m["name"]
    assert "shortened to fit" in out and 'section="measures"' in out


async def test_summary_hard_cap_holds_for_a_huge_model(env, setting):
    setting("model_result_chars", 4000)
    many = []
    for i in range(400):
        m = dict(env.rows["measures"][i % 50])
        m["name"] = f"m{i:03d}_{m['name']}"
        many.append(m)
    env.rows["measures"] = many
    out = await gm()
    assert len(out) <= 4000
    assert "measures not listed" in out and 'name="<part of a name>"' in out


# ── get_model: sections on demand ──────────────────────────────────────────

async def test_measure_detail_is_the_full_definition(env):
    out = await gm(section="measures", name="billable_revenue")
    d = json.loads(out)
    assert d["section"] == "measures" and d["count"] == 1
    m = d["items"][0]
    src = next(x for x in env.rows["measures"] if x["name"] == "billable_revenue")
    for k in ("type", "base_relation", "agg", "expr", "agg_time_dimension", "filter", "unit", "format",
              "description"):
        assert m[k] == src[k], k
    for k in ("id", "action_id", "approved_at", "updated_at", "status"):
        assert k not in m
    assert env.logs[-1][1] == {"section": "measures", "name": "billable_revenue"}


async def test_ratio_and_derived_detail_keep_their_inputs(env):
    d = json.loads(await gm(section="measure", name="cash_collected_mom_growth"))
    assert d["items"][0]["input_metrics"][1] == {"name": "cash_collected", "alias": "previous",
                                                  "offset_window": "1 month"}
    d = json.loads(await gm(section="measures", name="QUOTE_WIN_RATE"))
    assert d["items"][0]["numerator"] == {"name": "quotes_won"}


async def test_table_detail_has_view_sql_columns_and_what_uses_it(env):
    d = json.loads(await gm(section="tables", name="invoices"))
    assert d["count"] == 1
    t = d["items"][0]
    src = next(x for x in env.rows["tables"] if x["name"] == "invoices")
    assert t["derived_sql"] == src["derived_sql"]
    assert t["columns"]["invoice_date"] == "date" and t["primary_key"] == ["invoice_id"]
    assert t["mode"] == "import" and t["data_as_of"] == AS_OF.isoformat()
    assert "invoiced_amount" in t["measures"] and "overdue_amount" in t["measures"]
    assert "model.invoices.customer_id -> model.customers.customer_id" in t["relationships"]
    assert "model.payments.invoice_id -> model.invoices.invoice_id" in t["relationships"]
    # by relation works too
    d2 = json.loads(await gm(section="table", name="model.invoices"))
    assert d2["items"][0]["name"] == "invoices"


async def test_list_sections(env):
    rel = json.loads(await gm(section="relationships"))
    assert rel["count"] == 16 and rel["items"][0]["from_columns"]
    assert "match_rate" in rel["items"][0]
    jobs_rels = json.loads(await gm(section="relationships", name="model.jobs"))
    assert 0 < jobs_rels["count"] < 16
    pend = json.loads(await gm(section="pending"))
    assert pend["items"][0] == {"object": "measure", "name": "first_visit_resolution", "op": "create",
                                "action_id": 900}
    cands = json.loads(await gm(section="candidates"))
    assert cands["unseeded_foreign_keys"] == 2 and cands["total"] == 69
    assert len(cands["items"]) == 60 and "60 of 69" in cands["truncated"]
    some = json.loads(await gm(section="candidates", name="distributor_raw"))
    assert some["items"] and all("distributor_raw" in c["relation"] for c in some["items"])
    idx = json.loads(await gm(section="indexes"))
    assert idx["count"] == 0


async def test_name_alone_finds_the_item(env):
    d = json.loads(await gm(name="no_show_rate"))
    assert d["section"] == "measures" and d["items"][0]["name"] == "no_show_rate"
    d = json.loads(await gm(name="technicians"))
    assert d["section"] == "tables"
    d = json.loads(await gm(name="quote"))
    assert "quotes_won" in d["measures"] and "quotes" in d["tables"]


async def test_partial_name_lists_matches(env):
    d = json.loads(await gm(section="measures", name="quote"))
    names = [m["name"] for m in d["items"]]
    assert names == sorted(n["name"] for n in env.rows["measures"] if "quote" in n["name"])


async def test_unknown_section_and_unknown_name_are_answers_not_errors(env):
    out = await gm(section="everything")
    assert out.startswith('Unknown section "everything"') and "measures" in out
    out = await gm(section="measures", name="zzz")
    assert out.startswith('No measure matches "zzz"') and "billable_revenue" in out


async def test_list_section_over_cap_says_what_was_cut(env, setting):
    setting("model_result_chars", 4000)
    out = await gm(section="measures")
    assert len(out) <= 4000
    d = json.loads(out)
    assert 0 < len(d["items"]) < 50 and d["count"] == 50
    assert f"showing {len(d['items'])} of 50" in d["truncated"] and 'name="<name>"' in d["truncated"]


async def test_tables_list_leaves_view_sql_for_the_single_table_call(env):
    d = json.loads(await gm(section="tables"))
    assert all("derived_sql" not in t for t in d["items"])
    assert 'name="<table>"' in d["note"]


async def test_one_item_over_cap_cuts_its_longest_text_with_a_note(env, setting):
    setting("model_result_chars", 4000)
    env.rows["tables"][0]["derived_sql"] = "SELECT " + ", ".join(f"c{i}" for i in range(6000)) + " FROM x"
    name = env.rows["tables"][0]["name"]
    out = await gm(section="tables", name=name)
    assert len(out) <= 4000
    d = json.loads(out)
    assert "derived_sql cut at" in d["truncated"] and "Model page" in d["truncated"]
    assert d["items"][0]["derived_sql"].startswith("SELECT c0, c1")


@pytest.mark.parametrize("cap", [4000, 6000, 20000])
async def test_cap_is_never_exceeded(env, setting, cap):
    setting("model_result_chars", cap)
    for kw in ({}, {"section": "tables"}, {"section": "measures"}, {"section": "relationships"},
               {"section": "pending"}, {"section": "candidates"}, {"section": "tables", "name": "jobs"},
               {"name": "quote"}, {"section": "summary"}):
        out = await gm(**kw)
        assert len(out) <= cap, (kw, len(out))


async def test_tool_error_path_unchanged(env, monkeypatch):
    async def boom(include_retired=False):
        raise RuntimeError("x")
    monkeypatch.setattr(semanticmodel, "load_rows", boom)
    assert await gm() == "TOOL ERROR: RuntimeError"


def test_docstring_names_the_arguments():
    d = mt.get_model.description
    assert "section" in d and "name" in d and "summary" in d.lower()
    assert set(mt.get_model.args) == {"section", "name"}


# ── settings ───────────────────────────────────────────────────────────────

def test_settings_block_and_registry():
    cfg = (ROOT / "backend" / "app" / "config.py").read_text(encoding="utf-8")
    reg = (ROOT / "backend" / "app" / "configservice.py").read_text(encoding="utf-8")
    keys = ("model_result_chars", "sql_result_chars", "search_context_hits", "search_context_hit_chars")
    for src in (cfg, reg):
        a, b = src.index("# ── Quick 037"), src.index("# ── End Quick 037")
        for k in keys:
            assert k in src[a:b], k
    s = Settings()
    assert (s.model_result_chars, s.sql_result_chars, s.search_context_hits,
            s.search_context_hit_chars) == (20000, 8000, 8, 800)
    for k in keys:
        e = configservice.REGISTRY[k]
        assert e["group"] == "tuning" and not e["secret"] and "min" in e and "max" in e
        assert e["min"] <= getattr(s, k) <= e["max"]


# ── the before/after count (also printed when run as a script) ─────────────

async def _sizes(e) -> dict:
    out = {"get_model before (pre-Quick 037)": len(_legacy(e))}
    for label, kw in (("get_model summary (no arguments)", {}),
                      ('get_model section="measures" name="billable_revenue"', {"section": "measures", "name": "billable_revenue"}),
                      ('get_model section="tables" name="jobs"', {"section": "tables", "name": "jobs"}),
                      ('get_model section="relationships"', {"section": "relationships"}),
                      ('get_model section="measures" (all 50 in full)', {"section": "measures"}),
                      ('get_model section="candidates"', {"section": "candidates"})):
        out[label] = len(await mt.get_model.ainvoke(kw))
    return out


async def test_before_after_sizes(env):
    s = await _sizes(env)
    assert s["get_model summary (no arguments)"] * 4 < s["get_model before (pre-Quick 037)"]
    assert s['get_model section="measures" name="billable_revenue"'] < 1000
    print()
    for k, v in s.items():
        print(f"{v:>7,}  {k}")


if __name__ == "__main__":
    mp = pytest.MonkeyPatch()
    e = _env()
    _wire(mp, e)
    try:
        for k, v in asyncio.run(_sizes(e)).items():
            print(f"{v:>7,}  {k}")
    finally:
        mp.undo()

"""Phase 17 plan 02 (CONS-01): measure-backed KPI watches (fakes only)."""
from datetime import date

import pytest

from app import modelcompiler as mc
from app import modeltools as mt
from app import semanticmodel
from test_model_compiler import build

TODAY = date(2026, 10, 26)


def test_build_measure_request_matches_range():
    model = build()
    req, s, e = mt.build_measure_request(model, "net_sales", range="last_7_days", today=TODAY)
    assert (s, e) == (date(2026, 10, 19), date(2026, 10, 26))
    again = mc.make_request("net_sales", start=s, end=e)
    assert mc.compile_measure(model, req).sql == mc.compile_measure(model, again).sql


def test_validate_watch_ref_clean():
    ref, err = mt.validate_watch_ref({"measure": "net_sales", "range": "last_30_days"})
    assert err is None
    assert ref == {"measure": "net_sales", "filters": [], "range": "last_30_days"}


@pytest.mark.parametrize("bad", [
    {"measure": "net_sales", "by": ["regions.name"]},
    {"measure": "net_sales", "grain": "month"},
    {"measure": "net_sales", "range": {"start": "2026-01-01", "end": "2026-02-01"}},
    {"measure": "net_sales", "range": "next_week"},
    {"measure": "net_sales", "filters": "x"},
    {"measure": "Bad Name"},
    {"measure": "net_sales", "extra": 1},
    "net_sales",
])
def test_validate_watch_ref_refuses(bad):
    ref, err = mt.validate_watch_ref(bad)
    assert ref is None and err.startswith("REFUSED")


async def test_check_watch_measure_value_column_and_parity():
    model = build()
    ref = {"measure": "net_sales", "range": "last_7_days"}
    clean, sql, col, err = await mt.check_watch_measure(ref, model=model, today=TODAY)
    assert err is None and col == "net_sales"
    req, _s, _e = mt.build_measure_request(model, "net_sales", range="last_7_days", today=TODAY)
    assert sql == mc.compile_measure(model, req).sql


async def test_check_watch_measure_not_modeled():
    clean, sql, col, err = await mt.check_watch_measure({"measure": "profit"}, model=build(), today=TODAY)
    assert clean is None and err.startswith("NOT MODELED")


async def test_check_watch_measure_loads_model(monkeypatch):
    async def lm():
        return build()
    monkeypatch.setattr(semanticmodel, "load_model", lm)
    _c, sql, _col, err = await mt.check_watch_measure({"measure": "net_sales"}, today=TODAY)
    assert err is None and "SUM(" in sql


class _Pool:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, sql, *a):
        return self.rows if "FROM sources" in sql else []


async def test_catalog_includes_registered_sources(monkeypatch):
    from app import schemadiscovery, warehouse

    async def intro(source="business_db"):
        if source == "bad":
            raise RuntimeError("boom")
        if source == "good":
            return {"tables": [{"schema": "src_good", "table": "t", "columns": [{"name": "a", "type": "int"}]}]}
        return {"tables": [{"schema": "public", "table": "x", "columns": [{"name": "b", "type": "text"}]}]}

    async def pool():
        return _Pool([{"name": "bad"}, {"name": "good"}])
    monkeypatch.setattr(schemadiscovery, "introspect", intro)
    monkeypatch.setattr(semanticmodel, "get_pool", pool)
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: True)
    cols = await semanticmodel.catalog_columns()
    assert cols["src_good.t"] == {"a": "int"} and "public.x" in cols
    monkeypatch.setattr(warehouse, "warehouse_mode", lambda: False)
    cols = await semanticmodel.catalog_columns()
    assert "src_good.t" not in cols


# ─────────────────────────── Task 2: proposals and executor ───────────────────────────
import json
import re
from pathlib import Path

from app import insights


class RecPool:
    def __init__(self):
        self.inserts = []

    async def fetchrow(self, sql, *a):
        return None

    async def fetchval(self, sql, *a):
        if "INSERT INTO actions" in sql or "INSERT INTO watches" in sql:
            self.inserts.append((sql, a))
            return 7
        return None


@pytest.fixture
def rec(monkeypatch):
    pool = RecPool()

    async def gp():
        return pool
    monkeypatch.setattr(insights, "get_pool", gp)

    async def cwm(ref, model=None, today=None):
        return mt.validate_watch_ref(ref)[0] and (
            {"measure": ref["measure"], "filters": ref.get("filters") or [], "range": ref.get("range")},
            "SELECT 1", ref["measure"], None) or (None, None, None, "REFUSED: x")

    async def cwm2(ref, model=None, today=None):
        if ref.get("measure") == "profit":
            return None, None, None, "NOT MODELED: profit"
        return await cwm(ref)
    monkeypatch.setattr(mt, "check_watch_measure", cwm2)

    async def lm():
        return build()
    monkeypatch.setattr(semanticmodel, "load_model", lm)
    return pool


async def test_propose_threshold_alert(rec):
    out = await insights.propose_kpi_action(
        "Net sales 7d", "", origin="chat", thread_id="ui:x", run_id=None,
        measure="net_sales", range="last_7_days", alert={"op": "<", "value": 100})
    assert out.startswith("proposed KPI watch")
    payload = json.loads(rec.inserts[-1][1][0])
    assert payload["measure_ref"]["measure"] == "net_sales"
    assert payload["trigger_preset"] == {"type": "threshold", "column": "net_sales", "op": "<", "value": 100}
    assert "measure net_sales" in payload["_summary"]
    assert payload["condition"].startswith("Track ")


async def test_propose_record_default_and_bad_alert(rec):
    await insights.propose_kpi_action("K1", "", origin="chat", thread_id="t", run_id=None, measure="net_sales")
    assert json.loads(rec.inserts[-1][1][0])["trigger_preset"] == {"type": "record", "column": "net_sales"}
    n = len(rec.inserts)
    out = await insights.propose_kpi_action("K2", "", origin="chat", thread_id="t", run_id=None,
                                            measure="net_sales", alert="boom")
    assert out.startswith("REFUSED: alert") and len(rec.inserts) == n
    out = await insights.propose_kpi_action("K3", "", origin="chat", thread_id="t", run_id=None, measure="profit")
    assert out.startswith("NOT MODELED") and len(rec.inserts) == n
    out = await insights.propose_kpi_action("K4", "", origin="chat", thread_id="t", run_id=None,
                                            measure="net_sales", range={"start": "a"})
    assert out.startswith("REFUSED") and len(rec.inserts) == n


def test_measure_trigger_pure():
    assert insights.measure_trigger("m", "increase")[0] == {"type": "increase", "column": "m"}
    assert insights.measure_trigger("m", "change")[0]["type"] == "change"
    assert insights.measure_trigger("m", {"op": "=", "value": 1})[0] is None
    assert insights.measure_trigger("m", {"op": ">", "value": True})[0] is None


async def test_executor_measure_watch_inserts_without_sql(rec):
    payload = {"proposal_key": "k1", "title": "Net", "condition": "Track net", "cadence_seconds": 3600,
               "origin": "chat", "measure_ref": {"measure": "net_sales", "range": "last_7_days"},
               "trigger_preset": {"type": "threshold", "column": "evil", "op": ">", "value": 5}}
    assert await insights.create_watch_from_proposal(payload) is True
    sql, args = rec.inserts[-1]
    assert "measure_ref" in sql and "draft_sql" not in sql
    assert json.loads(args[-1]) == {"type": "threshold", "column": "net_sales", "op": ">", "value": 5}


async def test_executor_refuses_uncompilable_measure(rec):
    payload = {"proposal_key": "k2", "title": "P", "condition": "Track p", "cadence_seconds": 3600,
               "measure_ref": {"measure": "profit"}}
    assert await insights.create_watch_from_proposal(payload) is False
    assert rec.inserts == []


async def test_playbook_propose_kpi_measure(rec, monkeypatch):
    from app import playbook_tools as pt
    captured = {}

    async def rec_action(kind, payload, status):
        captured.update(payload)
        return 9
    monkeypatch.setattr(pt, "_record_action", rec_action)

    async def log(*a, **k):
        pass
    monkeypatch.setattr(pt, "_log_tool", log)
    monkeypatch.setattr(pt, "tier_allows", lambda *a: True)
    out = await pt.propose_kpi.ainvoke({"title": "Net 30d", "condition": "", "measure": "net_sales",
                                        "range": "last_30_days"})
    assert "proposed KPI watch" in out
    assert captured["origin"] == "exploration" and captured["measure_ref"]["range"] == "last_30_days"
    assert captured["trigger_preset"]["type"] == "record"


def test_graph_phase17_block_order():
    src = (Path(__file__).resolve().parent.parent / "app" / "agents" / "graph.py").read_text(encoding="utf-8")
    a = src.index("# ── End Phase 15 ──")
    b = src.index("# ── Phase 17: model consumers")
    c = re.search(r"^DEFAULT_CONFIGS: dict", src, re.M).start()
    assert a < b < c
    from app.agents import graph
    assert "MEASURE KPIs (Phase 17)" in graph.DEFAULT_CONFIGS["supervisor"]["prompt"]


# ─────────────────────────── Task 3: sentinel branch ───────────────────────────
from app import sentinel


def _mwatch(**kw):
    w = {"id": 5, "thread_id": "t", "kind": "kpi", "title": "Net 7d", "condition": "c",
         "one_shot": False, "status": "active", "last_state": None, "consecutive_errors": 0,
         "compiled_sql": None, "compile_error": None,
         "measure_ref": json.dumps({"measure": "net_sales", "filters": [], "range": "last_7_days"}),
         "trigger_rule": json.dumps({"type": "record", "column": "net_sales"})}
    w.update(kw)
    return w


class SPool:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *a):
        self.calls.append((sql, a))

    async def fetchrow(self, sql, *a):
        return _mwatch()

    def find(self, needle):
        return [c for c in self.calls if needle in c[0]]


class Job:
    def __init__(self):
        self.failed, self.oks = [], []

    def fail(self, msg):
        self.failed.append(msg)

    def ok(self, text, counts=None, routine=False):
        self.oks.append(text)


@pytest.fixture
def senv(monkeypatch):
    class E:
        pass
    e = E()
    e.pool = SPool()
    e.sql = []
    e.loads = 0
    e.model = build()
    e.fired = []
    e.llm = []

    async def gp():
        return e.pool

    async def run(sql):
        e.sql.append(sql)
        return [{"net_sales": 42}]

    async def lm():
        e.loads += 1
        return e.model

    async def fire(*a, **k):
        e.fired.append(a)

    async def llm(w):
        e.llm.append(w)
        return None
    monkeypatch.setattr(sentinel, "get_pool", gp)
    monkeypatch.setattr(sentinel, "run_readonly_sql", run)
    monkeypatch.setattr(sentinel, "_fire", fire)
    monkeypatch.setattr(sentinel, "_compile_watch", llm)
    monkeypatch.setattr(semanticmodel, "load_model", lm)
    monkeypatch.setattr(mt, "_today", lambda: TODAY)
    sentinel._begin_sweep()
    return e


async def test_measure_watch_runs_compiler_sql_no_llm(senv):
    job = Job()
    await sentinel._run_check(_mwatch(), job)
    assert senv.llm == []
    req, _s, _e = mt.build_measure_request(senv.model, "net_sales", range="last_7_days", today=TODAY)
    assert senv.sql == [mc.compile_measure(senv.model, req).sql]
    assert senv.pool.find("SET compiled_sql")
    assert senv.pool.find("INSERT INTO metric_history")[0][1][:3] == (5, "net_sales", 42.0)
    assert "records only" in job.oks[0] and senv.fired == []


async def test_not_modeled_pauses_after_retries(senv):
    ref = json.dumps({"measure": "profit", "filters": [], "range": None})
    job = Job()
    await sentinel._run_check(_mwatch(measure_ref=ref, consecutive_errors=2), job)
    upd = senv.pool.find("SET consecutive_errors")[0][1]
    assert upd[1] == 3 and upd[2] == "paused" and upd[3].startswith("NOT MODELED")
    assert senv.llm == [] and senv.sql == [] and job.failed
    job2 = Job()
    await sentinel._run_check(_mwatch(measure_ref=ref, consecutive_errors=0), job2)
    assert senv.pool.find("SET consecutive_errors")[-1][1][2] == "active"


async def test_measure_query_failure_never_uses_llm(senv, monkeypatch):
    async def boom(sql):
        raise RuntimeError("db down")
    monkeypatch.setattr(sentinel, "run_readonly_sql", boom)
    job = Job()
    sql_watch = _mwatch()
    await sentinel._run_check(sql_watch, job)
    assert senv.llm == [] and job.failed
    assert not any("compiled_sql = NULL" in c[0] for c in senv.pool.calls)


async def test_model_loaded_once_per_sweep(senv):
    await sentinel._run_check(_mwatch(id=1), Job())
    await sentinel._run_check(_mwatch(id=2), Job())
    assert senv.loads == 1
    sentinel._begin_sweep()
    await sentinel._run_check(_mwatch(id=3), Job())
    assert senv.loads == 2


async def test_good_compile_resets_errors(senv):
    await sentinel._run_check(_mwatch(consecutive_errors=2), Job())
    up = senv.pool.find("compile_error = NULL")
    assert up and up[0][1][0] == 5

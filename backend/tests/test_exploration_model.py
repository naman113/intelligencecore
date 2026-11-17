"""Phase 17 (CONS-04): exploration on the model. Registry, guarded seed upgrade,
record_insight lanes, approved tables across sources."""
import asyncio
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from app import insights, playbooks

SCHEMA = Path(__file__).resolve().parents[1] / "app" / "schema.sql"
_URL = os.getenv("GOLIVE_TEST_DATABASE_URL", "")
_GOLIVE_OK = bool(_URL) and (urlsplit(_URL).hostname or "") in ("127.0.0.1", "localhost")
P7_GRANTS = ("ARRAY['search_business_context','run_sql','run_sql_to_csv','run_python',"
             "'get_kpi_history','use_skill','record_insight','propose_kpi']")


def _run(c):
    return asyncio.run(c)


def _region():
    t = SCHEMA.read_text(encoding="utf-8")
    a = t.index("-- Phase 17: model consumers (CONS-01..CONS-06)")
    b = t.index("-- End Phase 17: model consumers")
    return t[a:b]


# -- registry -------------------------------------------------------------

def test_registry_has_model_tools():
    reg = playbooks._playbook_registry()
    assert "get_model" in reg and "query_measure" in reg
    assert "propose_mode_change" not in reg


# -- schema text ----------------------------------------------------------

def test_schema_guarded_updates():
    r = _region()
    assert r.count("-- Phase 17 (CONS-04): exploration on the model") == 1
    assert r.count("ARRAY['get_model','query_measure']") == 1
    assert "WHERE name = 'explore_data' AND proposed_by = 'system'" in r
    assert P7_GRANTS in r
    assert "status" not in re.sub(r"--.*", "", r.split("(CONS-04): exploration")[1]).lower().replace("statistics", "")
    assert "Call get_model first." in r


# -- real Postgres --------------------------------------------------------

def _admin_and_scratch():
    p = urlsplit(_URL)
    name = "ic_cons04_" + os.urandom(4).hex()
    return urlunsplit(p._replace(path="/postgres")), urlunsplit(p._replace(path="/" + name)), name


@pytest.mark.skipif(not _GOLIVE_OK, reason="needs GOLIVE_TEST_DATABASE_URL (loopback)")
def test_seed_upgrade_once_and_edit_kept():
    import asyncpg

    async def go():
        admin, scratch, name = _admin_and_scratch()
        a = await asyncpg.connect(admin)
        await a.execute(f'CREATE DATABASE "{name}"')
        try:
            c = await asyncpg.connect(scratch)
            try:
                sql = SCHEMA.read_text(encoding="utf-8")
                await c.execute(sql)
                row = await c.fetchrow("SELECT goal, tool_grants, status FROM playbooks WHERE name='explore_data'")
                assert "get_model" in row["tool_grants"] and "query_measure" in row["tool_grants"]
                assert "Call get_model first." in row["goal"]
                assert row["status"] == "draft"
                await c.execute(sql)
                row2 = await c.fetchrow("SELECT goal, tool_grants FROM playbooks WHERE name='explore_data'")
                assert row2["tool_grants"].count("get_model") == 1
                assert row2["goal"] == row["goal"]
                # owner edit survives
                await c.execute("UPDATE playbooks SET goal='mine', tool_grants=ARRAY['run_sql'] WHERE name='explore_data'")
                await c.execute(sql)
                row3 = await c.fetchrow("SELECT goal, tool_grants FROM playbooks WHERE name='explore_data'")
                assert row3["goal"] == "mine" and list(row3["tool_grants"]) == ["run_sql"]
            finally:
                await c.close()
        finally:
            await a.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            await a.close()
    _run(go())


# -- record_insight lanes (fakes; no DB) ----------------------------------

EV = {"row_count": 3, "columns": ["a"], "sample": [{"a": 1}], "truncated": False,
      "relative_dates": False}
RANGE = {"start": "2026-08-01", "end": "2026-10-01"}


@pytest.fixture
def lanes(monkeypatch):
    from app import playbook_tools as pt
    from app.agents import graph
    toks = [(pt.ctx_tier, pt.ctx_tier.set("draft")), (pt.ctx_case_id, pt.ctx_case_id.set(9)),
            (graph.ctx_run_id, graph.ctx_run_id.set(77))]
    c = {"note": [], "sql": [], "budget": []}

    async def fake_log(*a, **k):
        pass

    async def note(*a, **k):
        c["note"].append((a, k))
        return 11, 9, ""

    async def check_sql(sql):
        c["sql"].append(sql)
        return dict(EV), ""

    async def budget(sql):
        c["budget"].append(sql)
        return c.get("over"), []

    async def run_sql(sql):
        c["ran"] = sql
        return [{"a": 1}]

    monkeypatch.setattr(pt, "_log_tool", fake_log)
    monkeypatch.setattr(insights, "record_note", note)
    monkeypatch.setattr(insights, "check_evidence", check_sql)
    from app import livebudget, modelcompiler, modeltools, semanticmodel

    async def load():
        return object()

    class Compiled:
        sql = "SELECT 'compiled'"
        columns = ["x"]

    monkeypatch.setattr(semanticmodel, "load_model", load)
    monkeypatch.setattr(modeltools, "build_measure_request",
                        lambda model, measure, **k: ({"m": measure}, None, None))
    monkeypatch.setattr(modelcompiler, "compile_measure", lambda model, req: Compiled)
    monkeypatch.setattr(livebudget, "check", budget)
    monkeypatch.setattr(insights.guardrails, "run_readonly_sql", run_sql)
    yield c
    for var, tok in reversed(toks):
        try:
            var.reset(tok)
        except ValueError:
            pass


def _ri(**kw):
    from app import playbook_tools as pt
    args = {"title": "Net sales fell 10%", "finding": "Sales fell.", "severity": "info"}
    args.update(kw)
    return pt.record_insight.ainvoke(args)


async def test_measure_lane_verified(lanes):
    r = await _ri(evidence_measure={"measure": "net_sales", "range": RANGE})
    assert "recorded insight" in r
    a, k = lanes["note"][0]
    assert a[2] == "SELECT 'compiled'"                 # evidence_sql = compiled SQL
    assert k["lane"]["lane"] == "measure" and k["lane"]["verified"] is True
    assert k["lane"]["measure"] == "net_sales"
    assert "unverified" not in a[1]
    assert lanes["budget"] == ["SELECT 'compiled'"]    # budget before running


async def test_measure_budget_refusal(lanes):
    lanes["over"] = "LIVE QUERY BUDGET SPENT: x"
    r = await _ri(evidence_measure={"measure": "net_sales", "range": RANGE})
    assert r.startswith("REFUSED: LIVE QUERY BUDGET SPENT") and not lanes["note"]
    assert "ran" not in lanes


async def test_relative_range_refused(lanes):
    r = await _ri(evidence_measure={"measure": "net_sales", "range": "last_month"})
    assert r.startswith("REFUSED:") and "absolute" in r and not lanes["note"]


async def test_both_or_neither_refused(lanes):
    r = await _ri(evidence_sql="SELECT 1", evidence_measure={"measure": "m", "range": RANGE})
    assert r.startswith("REFUSED:") and "not both" in r
    r = await _ri()
    assert r.startswith("REFUSED:") and "required" in r
    assert not lanes["note"]


async def test_raw_sql_lane_unverified(lanes):
    r = await _ri(evidence_sql="SELECT 1")
    assert "recorded insight" in r
    a, k = lanes["note"][0]
    assert a[1].endswith(" (unverified: raw SQL)")
    assert k["lane"]["lane"] == "raw_sql" and k["lane"]["verified"] is False


def test_note_item_exposes_lane():
    import datetime as dt
    import json
    row = {"id": 1, "content": "c", "note": "exploration", "case_id": 1, "case_title": "t",
           "occurred_at": dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
           "meta": json.dumps({"title": "T", "lane": "measure", "verified": True})}
    it = insights._note_item(row)
    assert it["lane"] == "measure" and it["verified"] is True
    row["meta"] = json.dumps({"title": "T"})
    it = insights._note_item(row)
    assert it["lane"] is None and it["verified"] is None


# -- approved tables across sources --------------------------------------

def test_approved_tables_not_limited_to_business_db(monkeypatch):
    seen = []

    class P:
        async def fetch(self, sql, *a):
            seen.append(sql)
            return []

    async def gp():
        return P()
    monkeypatch.setattr(insights, "get_pool", gp)
    assert _run(insights.approved_tables()) == []
    assert "business_db" not in seen[0] and "s.status = 'active'" in seen[0]


def test_unqualified_matches_only_search_path():
    appr = [{"schema_name": "public", "table_name": "Orders"},
            {"schema_name": "src_crm", "table_name": "leads"}]
    t = {("", "orders"), ("", "leads"), ("src_crm", "leads")}
    # legacy rule: any schema
    assert insights.unapproved_refs({("", "leads")}, appr) == []
    # warehouse rule: unqualified must sit on the reader path, case folded
    assert insights.unapproved_refs({("", "orders")}, appr, search_path=["public"]) == []
    assert insights.unapproved_refs({("", "leads")}, appr, search_path=["public"]) == ["leads"]
    assert insights.unapproved_refs({("src_crm", "leads")}, appr, search_path=["public"]) == []
    assert insights.unapproved_refs(t, appr, search_path=["PUBLIC", "src_crm"]) == []

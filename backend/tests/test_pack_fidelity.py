"""Quick 001: a real client's pack loads completely; whatever the app cannot use is reported."""

import json
import re

import pytest

from app import actionsvc, contextpack as cp, contexttools as ct, insights, packnorm, readonlykit as rk, sentinel
from app.agents import graph as g

pytestmark = pytest.mark.asyncio

LONG_FACTS = [f"Fact number {i} about the fleet and how the office tracks diesel use every single day." for i in range(40)]


def client_pack() -> dict:
    """The shape of the first client's pack (tables_to_hide/table, draft_sql, dashboard queries...)."""
    return {
        "pack_version": 1,
        "meta": {"schemas_in_scope": ["public"]},
        "source": {"propose": {"kind": "source", "ref": "business_db", "text": "Fleet DB."},
                   "confidence": "high", "evidence": ["x"]},
        "business_profile": {
            "summary": {"text": "A mixed fleet operator."},
            "facts_told": [{"text": t, "confidence": "high"} for t in LONG_FACTS],
            "inferences": [{"text": "Theft is a concern."}],
            "priorities": [{"text": "Fuel drops."}],
            "people": [{"name": "", "role": "Super Admin", "notes": "Full access."}],
            "vocabulary": [{"term": "Fuel day", "text": "07:00 to 07:00."}],
        },
        "tables": [{"propose": {"kind": "table", "ref": "business_db.public.Machines", "text": "Fleet assets.",
                                "columns": {"Id": "Key."}}, "confidence": "high",
                    "evidence": ["a"], "column_review": {}}],
        "rules": [{"propose": {"kind": "rule", "ref": "rule:naming", "text": "Quote names."},
                   "confidence": "high", "category": "naming"}],
        "kpis": [
            {"propose": {"kind": "kpi", "ref": "kpi:short", "title": "Short", "text": "Litres per day.",
                         "tables": ["business_db.public.Machines"]},
             "reference_sql": 'SELECT 1 AS "x"', "confidence": "high"},
            {"propose": {"kind": "kpi", "ref": "kpi:has-sql", "title": "Has", "text": "Litres. SELECT 2 AS y",
                         "tables": []}, "reference_sql": "SELECT 2  AS y", "confidence": "high"},
            {"propose": {"kind": "kpi", "ref": "kpi:big", "title": "Big", "text": "B" * 1000,
                         "tables": []}, "reference_sql": "SELECT " + ", ".join(f"c{i}" for i in range(300)),
             "confidence": "high"},
        ],
        "watch_candidates": [
            {"title": "Spike", "condition": "ratio above 1.4", "cadence_minutes": 1440,
             "draft_sql": "SELECT 1.5 AS spike_ratio",
             "trigger_hint": {"type": "threshold", "column": "spike_ratio", "op": ">", "value": 1.4},
             "kpi_ref": "kpi:short", "sql_tested": False},
        ],
        "tables_to_hide": [
            {"table": "business_db.public.Users", "category": "sensitive", "recommended_action": "revoke_select",
             "reason": "logins"},
            {"table": "business_db.public.__EFMigrationsHistory", "recommended_action": "hide"},
        ],
        "dashboard_ideas": [
            {"title": "Diesel use", "question": "How much?", "audience": "Owner", "layout": ["Tile one", "Trend"],
             "queries": [{"id": "barge_7d", "refresh_minutes": 60, "sql": "SELECT 1 AS a"},
                         {"id": "trend", "refresh_minutes": 120, "sql": "SELECT 2 AS b"}]},
        ],
        "open_questions": [{"id": "q1", "question": "Which figure?", "why_it_matters": "x", "affects": []}],
        "coverage": {"notes": "generator metadata"},
    }


@pytest.fixture
def env(monkeypatch, tmp_path):
    st = {"plan": [], "kpi": [], "hide": [], "dir": tmp_path}

    def work_path(filename):
        return tmp_path, (tmp_path / filename).resolve()

    async def plan(kind, ref, text, title, columns, tables, revise):
        st["plan"].append(dict(kind=kind, ref=ref, text=text, title=title, columns=columns, tables=tables))
        n = 1 + len(columns or {})
        return (f"Context proposals: {n} proposed for owner approval, 0 already pending, 0 refused. "
                "Nothing changes until the owner approves."), False

    async def kpi(title, condition, cadence="daily", rationale="", **kw):
        st["kpi"].append(dict(title=title, cadence=cadence, **kw))
        return f"proposed KPI watch (action #1): '{title}'"

    async def resolve(ref, cat=None):
        return {"kind": "table"} if ref.startswith("business_db.public.") else None

    class Pool:
        def __init__(self):
            self.inserts = []

        async def fetchval(self, sql, *a):
            if sql.lstrip().startswith("INSERT INTO actions"):
                self.inserts.append(a)
                return len(self.inserts)
            return None

    pool = Pool()
    st["pool"] = pool

    async def get_pool():
        return pool

    async def log_tool(*a, **k):
        return None

    monkeypatch.setattr(cp, "_work_path", work_path)
    monkeypatch.setattr(ct, "_plan_and_write", plan)
    monkeypatch.setattr(insights, "propose_kpi_action", kpi)
    monkeypatch.setattr(cp.schemadiscovery, "resolve", resolve)
    monkeypatch.setattr(cp, "_log_tool", log_tool)
    monkeypatch.setattr(cp, "get_pool", get_pool)
    g.ctx_run_id.set(5)
    g.ctx_phone.set("ui:owner")
    (tmp_path / "pack.json").write_text(json.dumps(client_pack()), encoding="utf-8")
    return st


async def prop(section, **kw):
    return await cp.propose_context_pack_items.ainvoke({"filename": "pack.json", "section": section, **kw})


# ── 1. shared normalizer, hide aliases, role kit ──

def test_normalizer_maps_aliases_and_lists_ignored():
    n = packnorm.normalize_pack(client_pack())
    assert "tables_to_hide" not in n.data and len(n.data["hide_tables"]) == 2
    assert n.data["hide_tables"][0]["ref"] == "business_db.public.Users" and "table" not in n.data["hide_tables"][0]
    assert not n.problems
    ig = " | ".join(n.ignored)
    assert "section coverage" in ig and "tables.column_review x1" in ig and "tables.evidence x1" in ig
    assert "business_profile.vocabulary x1" in ig and "watch_candidates.kpi_ref x1" in ig


def test_normalizer_reports_unreadable_hide_entries():
    n = packnorm.normalize_pack({"hide_tables": [{"reason": "no ref"}, {"ref": "a", "recommended_action": "burn"}]})
    assert len(n.problems) == 2 and "hide_tables[0]" in n.problems[0]
    assert packnorm.normalize_pack({"tables_to_hide": "oops"}).problems == ["hide_tables is not a list"]


def test_parse_pack_reads_client_aliases():
    schemas, revoke = rk.parse_pack(client_pack())
    assert schemas == ["public"] and revoke == ["public.Users"]


@pytest.mark.parametrize("bad", [
    {"tables_to_hide": [{"reason": "no table key"}]},
    {"hide_tables": {"ref": "x"}},
    {"hidden_tables_list": [{"ref": "business_db.public.t"}]},
    {"hide_tables": [{"ref": "business_db.public.t", "recommended_action": "burn"}]},
])
def test_parse_pack_fails_on_unparseable_hide_section(bad):
    with pytest.raises(ValueError):
        rk.parse_pack(bad)


def test_role_kit_cli_prints_revokes_and_fails_on_bad_pack(tmp_path, capsys):
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps(client_pack()))
    out = rk.main(["render", "--pack", str(ok)])
    err = capsys.readouterr().err
    assert out == 0 and "will revoke SELECT on 1 table(s): public.Users" in err
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"tables_to_hide": [{"reason": "x"}]}))
    assert rk.main(["render", "--pack", str(bad)]) == 2
    assert "cannot parse the pack's hide section" in capsys.readouterr().err


# ── 3. profile never cut ──

def test_pack_chunks_keep_all_text_at_sentence_boundaries():
    parts = [t for t in LONG_FACTS]
    chunks = cp.pack_chunks(parts)
    assert len(chunks) > 1 and all(len(c) <= cp.CHUNK_CHARS for c in chunks)
    assert " ".join(chunks) == " ".join(parts)
    assert all(c.endswith(".") for c in chunks)
    long_one = " ".join(f"Sentence {i} is here." for i in range(300))
    pieces = cp.pack_chunks([long_one])
    assert " ".join(pieces) == long_one and all(len(p) <= cp.CHUNK_CHARS for p in pieces)
    assert cp._split_long("x" * 3000, 1400) == ["x" * 1400, "x" * 1400, "x" * 200]


async def test_profile_split_into_rules_without_loss(env):
    await prop("business_profile")
    calls = env["plan"]
    assert len(calls) > 1
    assert calls[0]["ref"] == "rule:business-profile" and calls[1]["ref"] == "rule:business-profile-2"
    joined = " ".join(c["text"] for c in calls)
    for t in LONG_FACTS:
        assert t in joined
    for needle in ("Theft is a concern", "Priority: Fuel drops", "Super Admin", "mixed fleet operator"):
        assert needle in joined
    assert all(len(c["text"]) <= 1500 for c in calls)


# ── whole import: counts, ignored, hide, watch, kpi sql, dashboards ──

async def test_all_import_reports_everything(env):
    out = await prop("all")
    m = re.search(r"Total proposals created: (\d+)", out)
    created = int(m.group(1)) if m else -1
    # every _plan_and_write call here reports 1 + len(columns) proposals; watch + 1 hide = 2 more
    expected = sum(1 + len(c["columns"] or {}) for c in env["plan"]) + len(env["kpi"]) + len(env["pool"].inserts)
    assert created == expected and created > len(env["plan"])
    assert "ignored (not stored):" in out and "section coverage" in out
    assert "manual step: revoke SELECT on business_db.public.Users" in out
    assert "hide_tables: proposed 1" in out and "manual 1" in out          # tables_to_hide was read
    assert len(env["pool"].inserts) == 1                                  # the plain hide entry
    assert "dashboard_ideas: proposed 1" in out


async def test_hide_entries_without_ref_are_reported(env):
    d = client_pack()
    d["tables_to_hide"].append({"reason": "no table key"})
    (env["dir"] / "pack.json").write_text(json.dumps(d))
    out = await prop("hide_tables")
    assert "hide entry not read" in out and "refused 1" in out


async def test_watch_candidate_carries_draft(env):
    await prop("watch_candidates")
    k = env["kpi"][0]
    assert k["draft_sql"] == "SELECT 1.5 AS spike_ratio"
    assert k["trigger_hint"] == {"type": "threshold", "column": "spike_ratio", "op": ">", "value": 1.4}


async def test_kpi_reference_sql_included_when_missing(env):
    await prop("kpis")
    by = {c["ref"]: c for c in env["plan"]}
    assert 'Reference SQL: SELECT 1 AS "x"' in by["kpi:short"]["text"]
    assert by["kpi:has-sql"]["text"] == "Litres. SELECT 2 AS y"            # already there, not doubled
    assert "rule:big-sql" in by and by["rule:big-sql"]["kind"] == "rule"   # too long: companion rule
    joined = " ".join(c["text"] for ref, c in by.items() if ref.startswith("rule:big-sql"))
    assert "c299" in joined


async def test_dashboard_idea_queries_carried(env):
    out = await prop("dashboard_ideas")
    refs = [c["ref"] for c in env["plan"]]
    assert refs[0] == "rule:dashboard-idea-diesel-use"
    assert "rule:dashboard-idea-diesel-use-q-barge-7d" in refs and "rule:dashboard-idea-diesel-use-q-trend" in refs
    q = next(c for c in env["plan"] if c["ref"].endswith("q-barge-7d"))
    assert "SELECT 1 AS a" in q["text"] and "refresh every 60" in q["text"]
    head = env["plan"][0]["text"]
    assert "Question: How much?" in head and "Tile one" in head and "barge_7d (refresh 60 min)" in head
    assert "proposals created 3" in out


def test_old_dashboard_idea_shape_still_read():
    calls = cp._idea_calls({"title": "Old", "description": "d", "tables": ["business_db.public.t"]})
    assert len(calls) == 1 and "Description: d" in calls[0][2] and calls[0][5] == ["business_db.public.t"]


# ── 2. draft SQL + trigger ──

def test_clean_draft():
    s, t, notes = insights.clean_draft("  SELECT 1 AS a ", {"type": "threshold", "column": "a", "op": ">", "value": 3})
    assert s == "SELECT 1 AS a" and t == {"type": "threshold", "column": "a", "op": ">", "value": 3} and not notes
    s, t, notes = insights.clean_draft("DELETE FROM x", {"type": "threshold", "column": "a"})
    assert s is None and t is None and len(notes) == 2
    assert insights.clean_draft("WITH q AS (SELECT 1 AS a) SELECT * FROM q", {"type": "increase", "column": "a"})[:2] == \
        ("WITH q AS (SELECT 1 AS a) SELECT * FROM q", {"type": "increase", "column": "a"})
    assert insights.clean_draft(None, None) == (None, None, [])


async def test_validate_draft_runs_through_guardrails(monkeypatch):
    seen = []

    async def ok(sql):
        seen.append(sql)
        return [{"spike_ratio": 1.51}]

    monkeypatch.setattr(sentinel, "run_readonly_sql", ok)
    w = {"id": 1, "title": "Spike", "draft_sql": "SELECT 1.51 AS spike_ratio",
         "draft_trigger": json.dumps({"type": "threshold", "column": "spike_ratio", "op": ">", "value": 1.4})}
    spec, why = await sentinel._validate_draft(w)
    assert spec["sql"] == "SELECT 1.51 AS spike_ratio" and spec["trigger"]["column"] == "spike_ratio" and not why
    assert seen == ["SELECT 1.51 AS spike_ratio"]

    async def wrong_col(sql):
        return [{"other": 1}]

    monkeypatch.setattr(sentinel, "run_readonly_sql", wrong_col)
    spec, why = await sentinel._validate_draft(w)
    assert spec is None and "not in the result" in why

    async def broken(sql):
        raise RuntimeError('function round(double precision, integer) does not exist')

    monkeypatch.setattr(sentinel, "run_readonly_sql", broken)
    spec, why = await sentinel._validate_draft(w)
    assert spec is None and "round(double precision" in why


async def test_compile_uses_valid_draft_without_llm(monkeypatch):
    async def ok(sql):
        return [{"n": 2}]

    class Boom:
        async def ainvoke(self, *a, **k):
            raise AssertionError("the LLM must not run when the draft is valid")

    monkeypatch.setattr(sentinel, "run_readonly_sql", ok)
    monkeypatch.setattr(sentinel, "_compiler", lambda: Boom())
    w = {"id": 2, "title": "T", "condition": "c", "draft_sql": "SELECT 2 AS n", "draft_trigger": {"type": "increase", "column": "n"}}
    spec = await sentinel._compile_watch(w)
    assert spec["sql"] == "SELECT 2 AS n" and w["_compile_error"] == ""


async def test_compile_falls_back_to_llm_with_draft_in_prompt(monkeypatch):
    calls = {"n": 0}

    async def run(sql):
        calls["n"] += 1
        if "BAD" in sql:
            raise RuntimeError("syntax error")
        return [{"n": 2}]

    class Msg:
        content = json.dumps({"sql": "SELECT 2 AS n", "trigger": {"type": "increase", "column": "n"}, "label": "n"})

    class Agent:
        prompt = None

        async def ainvoke(self, payload, config=None):
            Agent.prompt = payload["messages"][0][1]
            return {"messages": [Msg()]}

    monkeypatch.setattr(sentinel, "run_readonly_sql", run)
    monkeypatch.setattr(sentinel, "_compiler", lambda: Agent())
    w = {"id": 3, "title": "T", "condition": "cond", "draft_sql": "SELECT BAD", "draft_trigger": {"type": "increase", "column": "n"}}
    spec = await sentinel._compile_watch(w)
    assert spec["sql"] == "SELECT 2 AS n"
    assert "DRAFT SQL: SELECT BAD" in Agent.prompt and "It failed because" in Agent.prompt
    assert w["_compile_error"].startswith("draft rejected")


async def test_compile_failure_leaves_a_reason(monkeypatch):
    class Agent:
        async def ainvoke(self, payload, config=None):
            raise RuntimeError("GraphRecursionError: limit of 12 reached")

    monkeypatch.setattr(sentinel, "_compiler", lambda: Agent())
    w = {"id": 4, "title": "T", "condition": "cond"}
    assert await sentinel._compile_watch(w) is None
    assert "limit of 12" in w["_compile_error"]


async def test_propose_kpi_action_attaches_draft(monkeypatch):
    stored = []

    class Pool:
        async def fetchval(self, sql, *a):
            stored.append(a)
            return 7

        async def fetchrow(self, *a):
            return None

    async def get_pool():
        return Pool()

    monkeypatch.setattr(insights, "get_pool", get_pool)
    out = await insights.propose_kpi_action(
        "Spike", "ratio above 1.4", "daily", "r", origin="context_pack", thread_id="ui:owner", run_id=None,
        draft_sql="SELECT 1.5 AS spike_ratio", trigger_hint={"type": "threshold", "column": "spike_ratio", "op": ">", "value": 1.4})
    assert out.startswith("proposed KPI watch") and "Draft SQL attached" in out
    payload = json.loads(stored[-1][0])
    assert payload["draft_sql"] == "SELECT 1.5 AS spike_ratio" and payload["trigger_hint"]["value"] == 1.4
    out = await insights.propose_kpi_action(
        "Spike two", "c", "daily", "r", origin="context_pack", thread_id="ui:owner", run_id=None,
        draft_sql="DROP TABLE x")
    assert "Not attached" in out and "draft_sql" not in json.loads(stored[-1][0])


# ── 7. bulk approve ──

async def test_approve_many_runs_each_through_approve(monkeypatch):
    seen = []

    async def fake_approve(aid, decided_by=None):
        seen.append(aid)
        if aid == 3:
            return {"error": "action is not in a proposed state"}
        if aid == 4:
            raise RuntimeError("boom")
        return {"action": aid, "status": "executed" if aid != 5 else "failed"}

    monkeypatch.setattr(actionsvc, "approve", fake_approve)
    res = await actionsvc.approve_many([1, 2, 3, 4, 5, 2, True, -1, "x"])
    assert sorted(seen) == [1, 2, 3, 4, 5]
    assert [r["id"] for r in res["results"]] == [1, 2, 3, 4, 5]
    by = {r["id"]: r for r in res["results"]}
    assert by[1]["ok"] and by[2]["ok"]
    assert not by[3]["ok"] and "proposed" in by[3]["error"]
    assert not by[4]["ok"] and "boom" in by[4]["error"]
    assert not by[5]["ok"] and by[5]["status"] == "failed"
    assert (res["requested"], res["approved"], res["failed"]) == (5, 2, 3)


async def test_bulk_route_validates_ids(monkeypatch):
    from fastapi import HTTPException
    from app import main

    async def fake(ids, decided_by=None):
        return {"results": [], "requested": len(ids), "approved": 0, "failed": 0}

    monkeypatch.setattr(actionsvc, "approve_many", fake)
    assert (await main.api_bulk_approve({"ids": [1, 2]}))["requested"] == 2
    for bad in ({}, {"ids": []}, {"ids": ["a"]}, {"ids": [True]}, {"ids": list(range(1, actionsvc.BULK_MAX + 2))}):
        with pytest.raises(HTTPException):
            await main.api_bulk_approve(bad)


async def test_compile_out_of_steps_is_named(monkeypatch):
    class Msg:
        content = "Sorry, need more steps to process this request."

    class Agent:
        async def ainvoke(self, payload, config=None):
            assert config["recursion_limit"] == sentinel.COMPILE_RECURSION_LIMIT >= 24
            return {"messages": [Msg()]}

    monkeypatch.setattr(sentinel, "_compiler", lambda: Agent())
    w = {"id": 5, "title": "T", "condition": "cond"}
    assert await sentinel._compile_watch(w) is None
    assert "out of steps" in w["_compile_error"]

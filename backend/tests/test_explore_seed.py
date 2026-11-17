"""EXPL-03/EXPL-05 pins for the seeded nightly exploration playbook.

Born inactive (status 'draft'), draft tier, no outbound tools; run_python and
run_sql_to_csv reach a playbook only through its tool_grants.
"""
import re
from pathlib import Path

from app import playbook_tools as pt
from app import playbooks
from app.agents import graph

SCHEMA = (Path(__file__).resolve().parent.parent / "app" / "schema.sql").read_text(
    encoding="utf-8").replace("\r\n", "\n")

START = "-- ── Phase 7: Exploration and insights ──"
END = "-- End Phase 7: Exploration and insights"
BLOCK = SCHEMA[SCHEMA.index(START):SCHEMA.index(END)]

EXPECTED_GRANTS = ["search_business_context", "run_sql", "run_sql_to_csv", "run_python",
                   "get_kpi_history", "use_skill", "record_insight", "propose_kpi"]


def _insert() -> str:
    stmts = re.findall(r"INSERT INTO playbooks.*?;\n", BLOCK, flags=re.S)
    assert len(stmts) == 1
    return stmts[0]


def _grants() -> list[str]:
    arr = re.search(r"ARRAY\[(.*?)\]", _insert(), flags=re.S).group(1)
    return re.findall(r"'([^']+)'", arr)


def test_seed_inside_phase7_block():
    stmt = _insert()
    assert "'explore_data'" in stmt
    assert stmt.rstrip().endswith("ON CONFLICT (name) DO NOTHING;")


def test_seed_values():
    stmt = _insert()
    assert "'{\"type\":\"cron\",\"hour\":2}'::jsonb" in stmt
    assert "DO UPDATE" not in stmt
    tail = stmt[stmt.rindex("ARRAY["):]
    assert re.search(r"\],\s*'draft',\s*NULL,\s*'draft',\s*'system',\s*NULL\)", tail)


def test_seed_grants_exact():
    g = _grants()
    assert g == EXPECTED_GRANTS
    for bad in ("notify_owner", "send_progress", "send_reminder", "post_to_group",
                "escalate", "propose_action", "web_research", "read_url",
                "investigate_case"):
        assert bad not in g


def test_seed_goal_mentions_method():
    stmt = _insert()
    goal = stmt[:stmt.index("ARRAY[")]
    for word in ("APPROVED TABLES", "ALREADY RECORDED", "record_insight", "propose_kpi",
                 "use_skill", "exploratory-data-analysis", "kpi-selection", "now()"):
        assert word in goal
    pieces = re.findall(r"'((?:[^']|'')*)'", goal)
    assert pieces
    for p in pieces[3:]:   # the goal pieces, after name and trigger
        assert "'" not in p


def test_every_grant_resolves():
    reg = playbooks._playbook_registry()
    for g in _grants():
        assert g in reg, g


def test_run_python_grant_gated():
    reg = playbooks._playbook_registry()
    assert reg["run_python"] is graph.run_python
    assert reg["run_sql_to_csv"] is graph.run_sql_to_csv
    granted = [reg[t] for t in ["send_reminder", "notify_owner"] if t in reg]
    assert graph.run_python not in granted and graph.run_sql_to_csv not in granted
    granted = [reg[t] for t in ["run_python", "run_sql_to_csv"] if t in reg]
    assert len(granted) == 2


def test_run_python_not_a_tier_capability():
    for name in ("run_python", "run_sql_to_csv"):
        assert name not in pt.PLAYBOOK_TOOL_REGISTRY
        assert all(name not in caps for caps in pt.TIER_CAPABILITIES.values())


# ---------------------------------------------------------------- Task 2

import json
import types
from datetime import datetime, timezone

import pytest

from app import expectations, insights, skillservice

EXEC_LINE = "Execute the goal now using only your granted tools."
STOP_LINE = ("APPROVED TABLES: none. Nothing has been approved for study yet. "
             "Stop now without calling record_insight or propose_kpi.")


class _CtxPool:
    async def fetchrow(self, sql, *a):
        return None


class _Mem:
    async def search(self, *a, **k):
        return []


def _wire_ctx(monkeypatch, ctx_fn):
    async def gp():
        return _CtxPool()

    async def slugs():
        return ["exploratory-data-analysis"]

    monkeypatch.setattr(playbooks, "get_pool", gp)
    monkeypatch.setattr(playbooks, "memory_graph", _Mem())
    monkeypatch.setattr(skillservice, "enabled_slugs", slugs)
    monkeypatch.setattr(insights, "exploration_context", ctx_fn)


def _pb(grants):
    return {"name": "explore_data", "goal": "g", "escalation": None, "tool_grants": grants}


_TASK = {"case_id": None, "expectation_id": None}


async def test_context_injected_when_record_insight_granted(monkeypatch):
    async def ctx(include_kpis=True):
        return STOP_LINE
    _wire_ctx(monkeypatch, ctx)
    out = await playbooks._assemble_context(_TASK, _pb(EXPECTED_GRANTS))
    assert STOP_LINE in out
    assert "Enabled skills (load with use_skill): exploratory-data-analysis" in out
    assert out.endswith(EXEC_LINE)


async def test_context_passes_include_kpis(monkeypatch):
    seen = []

    async def ctx(include_kpis=True):
        seen.append(include_kpis)
        return "X"
    _wire_ctx(monkeypatch, ctx)
    await playbooks._assemble_context(_TASK, _pb(["record_insight", "propose_kpi"]))
    await playbooks._assemble_context(_TASK, _pb(["record_insight"]))
    assert seen == [True, False]


async def test_context_untouched_without_grant(monkeypatch):
    called = []

    async def ctx(include_kpis=True):
        called.append(1)
        return "APPROVED TABLES"
    _wire_ctx(monkeypatch, ctx)
    out = await playbooks._assemble_context(_TASK, _pb(["case_overview", "notify_owner"]))
    assert not called
    assert "APPROVED TABLES" not in out


async def test_context_fail_soft(monkeypatch):
    async def ctx(include_kpis=True):
        raise RuntimeError("boom")
    _wire_ctx(monkeypatch, ctx)
    out = await playbooks._assemble_context(_TASK, _pb(EXPECTED_GRANTS))
    assert out.endswith(EXEC_LINE)


async def test_empty_run_writes_no_note(monkeypatch):
    from app import cases_util
    sqls = []

    class _Pool:
        async def fetchrow(self, sql, *a):
            return {"id": 5, "name": "explore_data", "status": "active", "goal": "g",
                    "escalation": None, "tool_grants": EXPECTED_GRANTS,
                    "autonomy_tier": "draft"}

        async def execute(self, sql, *a):
            sqls.append(sql)

    async def gp():
        return _Pool()

    async def fake_ctx(task, pb):
        return "CTX"

    class _Agent:
        async def ainvoke(self, payload, config=None):
            return {"messages": [type("_M", (), {"content": "Nothing cleared the bar tonight."})()]}

    spies = []

    async def spy(*a, **k):
        spies.append(1)

    monkeypatch.setattr(playbooks, "get_pool", gp)
    monkeypatch.setattr(playbooks, "_assemble_context", fake_ctx)
    monkeypatch.setattr(playbooks, "_model", lambda: object())
    monkeypatch.setattr(playbooks, "create_react_agent", lambda m, t, prompt=None: _Agent())
    monkeypatch.setattr(playbooks.graph, "_callbacks", lambda: {})
    monkeypatch.setattr(cases_util, "add_case_note", spy)
    monkeypatch.setattr(insights, "record_note", spy)
    await playbooks.run_playbook_task(
        {"id": 77, "playbook_id": 5, "case_id": None, "expectation_id": None})
    assert spies == []
    assert any("status='done'" in s for s in sqls)


def test_only_insights_writes_exploration_notes():
    root = Path(__file__).resolve().parent.parent / "app"
    hits, calls = [], 0
    for f in root.rglob("*.py"):
        txt = f.read_text(encoding="utf-8")
        if 'kind="exploration"' in txt or "kind=ORIGIN" in txt:
            hits.append(f.name)
        if f.name == "insights.py":
            calls = txt.count("cases_util.add_case_note(")
    assert hits == ["insights.py"]
    assert calls == 1


async def test_cron_dispatches_explore_data_once_per_day(monkeypatch):
    enq, state = [], {}
    hour = {"h": 2}
    pbs = [{"id": 31, "name": "explore_data", "trigger": '{"type":"cron","hour":2}'}]

    class _P:
        async def fetch(self, sql, *a):
            return list(pbs)

    async def gp():
        return _P()

    async def load():
        return dict(state)

    async def save(s):
        state.clear()
        state.update(s)

    async def fake_enqueue(pid, **kw):
        enq.append(pid)
        return len(enq), True

    settings = types.SimpleNamespace(
        consolidation_enabled=False, consolidation_hour=3, anthropic_api_key="k",
        openai_api_key="", timezone="UTC", expectation_poll_seconds=60)
    monkeypatch.setattr(expectations, "get_settings", lambda: settings)
    monkeypatch.setattr(expectations, "get_pool", gp)
    monkeypatch.setattr(expectations, "_load_cron_state", load)
    monkeypatch.setattr(expectations, "_save_cron_state", save)
    monkeypatch.setattr(expectations, "_local_now",
                        lambda: datetime(2026, 8, 10, hour["h"], 5, tzinfo=timezone.utc))
    monkeypatch.setattr(playbooks, "enqueue_playbook_run_once", fake_enqueue)

    assert await expectations._dispatch_cron() == 1
    assert enq == [31]
    assert await expectations._dispatch_cron() == 0
    assert enq == [31]
    state.clear()
    hour["h"] = 3
    assert await expectations._dispatch_cron() == 0
    assert enq == [31]


# ---- 07-06: per-playbook run limits (research D-16 fallback) ----
def test_pb_int_reads_positive_ints_only():
    from app.playbooks import _pb_int
    assert _pb_int({"recursion_limit": 80}, "recursion_limit") == 80
    assert _pb_int({"recursion_limit": None}, "recursion_limit") is None
    assert _pb_int({"recursion_limit": 0}, "recursion_limit") is None
    assert _pb_int({"recursion_limit": True}, "recursion_limit") is None
    assert _pb_int({}, "token_budget") is None


def test_schema_adds_limit_columns_and_seed_limits():
    import pathlib
    s = (pathlib.Path(__file__).parent.parent / "app" / "schema.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS recursion_limit" in s
    assert "ADD COLUMN IF NOT EXISTS token_budget" in s
    assert "SET recursion_limit = 80" in s

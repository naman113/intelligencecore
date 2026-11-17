"""Phase 6 dashboard specialist wiring (agents/graph.py, main.py).

Pure logic: create_react_agent/_make_model are faked; no DB/LLM/network."""

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main
from app.agents import graph

LEGACY = ("supervisor", "data", "analysis", "document", "drawing")
ELEVEN = ["search_business_context", "run_sql", "get_dashboard", "preview_dashboard",
        "save_dashboard_version", "get_kpi_history", "get_forecast",  # Phase 13.1
        "get_model", "query_measure",  # Phase 18
        "use_skill", "skill_asset"]
DASH_TOOLS = ("get_dashboard", "preview_dashboard", "save_dashboard_version")


class _FakeModel:
    pass


def _run(coro):
    return asyncio.run(coro)


# ── superset / config ceiling ────────────────────────────────────────────

def test_dashboard_superset_exact_order():
    assert graph.AGENT_TOOL_SUPERSET["dashboard"] == ELEVEN


def test_supervisor_gets_delegate_and_keeps_pins():
    sup = graph.AGENT_TOOL_SUPERSET["supervisor"]
    assert "delegate_to_dashboard_agent" in sup
    assert "delegate_to_dashboard_agent" in graph.DEFAULT_CONFIGS["supervisor"]["tools"]
    assert sup[-2:] == ["discover_schema", "propose_context_change"]
    assert sup.index("get_document") == sup.index("search_documents") + 1


def test_dashboard_tools_only_in_dashboard_superset():
    for key, tools in graph.AGENT_TOOL_SUPERSET.items():
        if key == "dashboard":
            continue
        for t in DASH_TOOLS:
            assert t not in tools, (key, t)
    assert "run_python" not in graph.AGENT_TOOL_SUPERSET["dashboard"]
    for key, tools in graph.AGENT_TOOL_SUPERSET.items():
        assert not any("publish" in t for t in tools), key


def test_merge_config_ceiling_ignores_tools_key():
    cfg = graph._merge_config("dashboard", {"tools": ["run_python"],
                                            "disabled_tools": ["run_sql"]})
    assert cfg["tools"] == [t for t in ELEVEN if t != "run_sql"]
    assert "run_python" not in cfg["tools"]
    d = graph.DEFAULT_CONFIGS["dashboard"]
    assert d["max_tokens"] == 8192 and d["recursion_limit"] == 50
    assert d["tools"] == ELEVEN


class _Pool:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append(args)


def _patch_db(monkeypatch):
    pool = _Pool()

    async def fake_get_pool():
        return pool

    monkeypatch.setattr(main, "db", SimpleNamespace(get_pool=fake_get_pool))
    return pool


def test_put_config_rejects_run_python(monkeypatch):
    pool = _patch_db(monkeypatch)
    with pytest.raises(HTTPException) as e:
        _run(main.update_agent_config(
            "dashboard", {"prompt": "x" * 30, "tools": ["get_dashboard", "run_python"]}))
    assert e.value.status_code == 422
    assert pool.calls == []


def test_put_config_narrows_and_unknown_404(monkeypatch):
    pool = _patch_db(monkeypatch)
    out = _run(main.update_agent_config(
        "dashboard", {"prompt": "x" * 30, "tools": ["get_dashboard", "preview_dashboard"]}))
    assert out["saved"] == "dashboard"
    saved = json.loads(pool.calls[0][1])
    assert set(saved["disabled_tools"]) == set(ELEVEN) - {"get_dashboard", "preview_dashboard"}
    assert len(saved["disabled_tools"]) == 9  # Phase 18: get_model, query_measure
    with pytest.raises(HTTPException) as e:
        _run(main.update_agent_config("nope", {"prompt": "x" * 30}))
    assert e.value.status_code == 404


def test_registry_has_dashboard_tools():
    reg = graph._tool_registry()
    from app import dashboardtools
    for t in DASH_TOOLS:
        assert reg[t] is getattr(dashboardtools, t)


# ── build ────────────────────────────────────────────────────────────────

def _fake_build(monkeypatch, keys):
    recorded = []

    def fake_cra(model, tools, prompt=None):
        recorded.append((prompt, [t.name for t in tools]))
        return object()

    monkeypatch.setattr(graph, "create_react_agent", fake_cra)
    monkeypatch.setattr(graph, "_make_model", lambda cfg, role="supervisor", **kw: _FakeModel())
    configs = {k: graph._merge_config(k, {}) for k in keys}
    try:
        graph._build(configs)
    finally:
        got_agent = graph._dashboard_agent
        graph.reset_agents()
    assert graph._supervisor is None  # Quick 011: reset only marks the cache stale
    return recorded, got_agent


def test_build_with_dashboard(monkeypatch):
    recorded, agent = _fake_build(monkeypatch, LEGACY + ("dashboard",))
    assert len(recorded) == 6  # four specialists + dashboard + supervisor
    assert agent is not None
    assert any(tools == ELEVEN for _, tools in recorded)
    sup_tools = recorded[-1][1]
    assert "delegate_to_dashboard_agent" in sup_tools


def test_build_legacy_keys_builds_exactly_five(monkeypatch):
    recorded, agent = _fake_build(monkeypatch, LEGACY)
    assert len(recorded) == 5  # four specialists + supervisor
    assert agent is None
    assert "delegate_to_dashboard_agent" not in recorded[-1][1]


def test_registry_lists_dashboard_agent(monkeypatch):
    async def no_overrides():
        return {}

    monkeypatch.setattr(graph, "get_config_overrides", no_overrides)
    entries = _run(graph.registry())
    dash = [e for e in entries if e["name"] == "Dashboard Agent"]
    assert len(dash) == 1
    assert dash[0]["config_key"] == "dashboard"
    assert len(dash[0]["tool_superset"]) == 11  # Phase 18
    sup = [e for e in entries if e["name"] == "Supervisor"][0]
    assert "delegate_to_dashboard_agent" in [t["name"] for t in sup["tools"]]


# ── prompts ──────────────────────────────────────────────────────────────

def test_dashboard_prompt_content():
    p = graph.DEFAULT_PROMPTS["dashboard"]
    for needle in ("get_model", "query_measure", "spec", "FORECAST PANELS", '"source": "forecast"', "use_skill", "dashboard-authoring", "preview_dashboard",
                   "save_dashboard_version", "get_dashboard", "search_business_context",
                   "kpi_history", "information_schema", "table_schema='public'",
                   "to_char(day,'YYYY-MM-DD')", "get_kpi_history"):
        assert needle in p, needle
    assert re.search(r"never say it is live or published", p, re.I)
    assert "never include links" in p.lower()


def test_supervisor_prompt_addition():
    p = graph.DEFAULT_PROMPTS["supervisor"]
    for needle in ("delegate_to_dashboard_agent", "dashboard-authoring", "DRAFT"):
        assert needle in p
    # the new bullet itself must not carry the WhatsApp-format pins
    tail = p[p.index("- DASHBOARDS"):]
    assert "no markdown tables, no links" not in tail
    assert "Quote its sources" not in tail


# ── source scans ─────────────────────────────────────────────────────────

def test_graph_source_has_no_publish_symbols():
    src = Path(graph.__file__).read_text(encoding="utf-8")
    for needle in ("dashboards.publish", "operator_set_version", "apply_publish_proposal"):
        assert needle not in src, needle


def test_playbooks_exclude_dashboard_tools():
    from app import playbooks
    reg = playbooks._playbook_registry()
    for t in DASH_TOOLS:
        assert t not in reg


# ── Phase 18: spec workflow, never-stored rule, conversion routing ──

def test_dashboard_superset_phase18_order_and_isolation():
    s = graph.AGENT_TOOL_SUPERSET["dashboard"]
    assert s.index("get_model") + 1 == s.index("query_measure") < s.index("use_skill")
    assert graph.DEFAULT_CONFIGS["dashboard"]["tools"] == s


def test_dashboard_prompt_is_spec_not_html():
    p = graph.DEFAULT_PROMPTS["dashboard"]
    assert p.count(graph._NEW_DASH_CLAUSE) == 1
    assert "get_dashboard" in p and "same slug" in p and "awaits the owner" in p
    low = p.lower()
    assert "write html" not in low and "javascript" not in low
    assert graph.DASHBOARD_SPEC_RULE not in p
    assert graph.DASHBOARD_SPEC_RULE not in graph.DEFAULT_CONFIGS["dashboard"]["prompt"]


def test_spec_rule_content():
    r = graph.DASHBOARD_SPEC_RULE
    for needle in ("chart specs only", "refuse", "plain text", "measures", "unverified"):
        assert needle in r, needle


def test_supervisor_conversion_line():
    p = graph.DEFAULT_PROMPTS["supervisor"]
    assert p.count("DASHBOARD CONVERSION (Phase 18)") == 1
    line = [x for x in p.split(chr(10)) if "DASHBOARD CONVERSION (Phase 18)" in x][0]
    assert "delegate_to_dashboard_agent" in line and "slug" in line and "draft" in line


def test_built_dashboard_prompt_ends_with_spec_rule(monkeypatch):
    recorded, _ = _fake_build(monkeypatch, LEGACY + ("dashboard",))
    prompts = [pr for pr, tools in recorded if tools == ELEVEN]
    assert prompts and str(prompts[0]).rstrip().endswith(graph.DASHBOARD_SPEC_RULE)


def test_customized_dashboard_keeps_tail_and_other_tools(monkeypatch):
    cfg = graph._merge_config("dashboard", {"disabled_tools": ["query_measure"],
                                            "prompt": "Write an HTML page with script tags please."})
    assert cfg["tools"] == [t for t in ELEVEN if t != "query_measure"]
    recorded = []

    def fake_cra(model, tools, prompt=None):
        recorded.append((prompt, [t.name for t in tools]))
        return object()

    monkeypatch.setattr(graph, "create_react_agent", fake_cra)
    monkeypatch.setattr(graph, "_make_model", lambda c, role="supervisor", **kw: _FakeModel())
    configs = {k: graph._merge_config(k, {}) for k in LEGACY}
    configs["dashboard"] = cfg
    try:
        graph._build(configs)
    finally:
        graph.reset_agents()
    dash = [r for r in recorded if "get_dashboard" in r[1]][0]
    assert "HTML page" in str(dash[0]) and str(dash[0]).rstrip().endswith(graph.DASHBOARD_SPEC_RULE)
    assert "query_measure" not in dash[1]

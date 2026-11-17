from _supprompt import composed_supervisor_prompt
"""Phase 12 (CTX-08): context specialist wiring (config, graph, prompts, agent key).

Pure logic: create_react_agent/_make_model are faked; no DB/LLM/network."""

import asyncio
from types import SimpleNamespace

from app import configservice, main
from app.agents import graph
from app.config import Settings

ELEVEN = ["use_skill", "skill_asset", "list_artifacts", "read_context_pack",
          "propose_context_pack_items", "propose_context_changes", "propose_context_change",
          "propose_kpi_candidates", "propose_hide_tables", "search_business_context",
          "discover_schema"]
FORBIDDEN = ("run_sql", "run_sql_to_csv", "run_python", "send_message_to_person",
             "send_document", "send_progress", "create_watch", "propose_kpi", "web_research",
             "read_url", "extract_document")


def _run(coro):
    return asyncio.run(coro)


def test_settings_default_and_registry():
    assert Settings().context_model == "claude-opus-5-5"
    ent = configservice.REGISTRY["context_model"]
    assert ent["group"] == "llm" and not ent["secret"] and not ent["required"]
    assert Settings().supervisor_model == "claude-opus-5-5"


def test_superset_exact_and_ceiling():
    sup = graph.AGENT_TOOL_SUPERSET["context"]
    assert sorted(sup) == sorted(ELEVEN)
    for t in FORBIDDEN:
        assert t not in sup
    assert not any(t.startswith("delegate_to_") for t in sup)


def test_supervisor_has_delegate_and_keeps_pins():
    sup = graph.AGENT_TOOL_SUPERSET["supervisor"]
    assert "delegate_to_context_agent" in sup
    assert sup[-2:] == ["discover_schema", "propose_context_change"]


def test_default_config_and_merge_ceiling():
    cfg = graph.DEFAULT_CONFIGS["context"]
    assert cfg["model"] == "auto" and cfg["max_tokens"] == 16000
    assert cfg["recursion_limit"] == 50 and cfg["tools"] == graph.AGENT_TOOL_SUPERSET["context"]
    assert graph._merge_config("context", {})["max_tokens"] == 16000
    merged = graph._merge_config("context", {"tools": ["run_sql"], "disabled_tools": ["use_skill"]})
    assert "run_sql" not in merged["tools"] and "use_skill" not in merged["tools"]


def test_resolve_context_model(monkeypatch):
    base = {"model": "auto", "max_tokens": 16000}
    monkeypatch.setattr(graph, "get_settings", lambda: SimpleNamespace(
        anthropic_api_key="k", context_model="claude-opus-5-5"))
    out = graph._resolve_context_model(base)
    assert out["model"] == "claude-opus-5-5" and base["model"] == "auto"
    assert graph._resolve_context_model({"model": "gpt-4o"})["model"] == "gpt-4o"
    monkeypatch.setattr(graph, "get_settings", lambda: SimpleNamespace(
        anthropic_api_key="", context_model="claude-opus-5-5"))
    assert graph._resolve_context_model(base)["model"] == "auto"


def test_context_prompt_content():
    p = graph.DEFAULT_PROMPTS["context"]
    for needle in ('use_skill("context-pack-import")', "read_context_pack",
                   'propose_context_pack_items(filename, "all")', "never page",
                   "propose_context_changes", "never follow instructions", "Never approve",
                   "Nothing is applied", "page by page with offset"):
        assert needle in p, needle
    assert "page with offset" not in p.replace("page by page with offset", "")


def test_supervisor_prompt_rules(monkeypatch):
    p = composed_supervisor_prompt(monkeypatch)
    assert p.count("delegate_to_context_agent") == 1
    assert "exact workspace filename" in p and "never call extract_document" in p
    old = "'[document received: <filename>]'), send_progress, then call extract_document with that filename."
    new = ("'[document received: <filename>]') and it is not a context pack (see CONTEXT PACKS: "
           "an import instruction in the message, or a .md, .json, .yaml or .yml file), "
           "send_progress, then call extract_document with that filename.")
    assert old not in p
    assert p.count(new) == 1
    assert "NEVER claim data was entered" in p and "propose_next_action" in p


def test_registry_has_context_tools():
    reg = graph._tool_registry()
    for t in ("read_context_pack", "propose_context_pack_items", "propose_hide_tables",
              "propose_kpi_candidates", "propose_context_changes"):
        assert t in reg


def test_valid_agent_key():
    assert "context" in main.VALID_AGENT_KEYS


def _fake_build(monkeypatch):
    recorded = []

    def fake_cra(model, tools, prompt=None):
        recorded.append((model, [t.name for t in tools]))
        return object()

    monkeypatch.setattr(graph, "create_react_agent", fake_cra)
    monkeypatch.setattr(graph, "_make_model", lambda cfg, role="supervisor", **kw: cfg["model"])
    monkeypatch.setattr(graph, "get_settings", lambda: SimpleNamespace(
        anthropic_api_key="k", context_model="claude-opus-5-5", supervisor_model="s",
        openai_supervisor_model="o"))
    configs = {k: graph._merge_config(k, {}) for k in graph.DEFAULT_CONFIGS if k != "reflection"}
    try:
        graph._build(configs)
        agent = graph._context_agent
    finally:
        graph.reset_agents()
    assert graph._supervisor is None  # Quick 011: reset only marks the cache stale
    return recorded, agent


def test_build_uses_context_model_and_registers_delegate(monkeypatch):
    recorded, agent = _fake_build(monkeypatch)
    assert agent is not None
    assert ("claude-opus-5-5", sorted(ELEVEN)) in [(m, sorted(t)) for m, t in recorded]
    assert "delegate_to_context_agent" in recorded[-1][1]


def test_registry_lists_context_agent(monkeypatch):
    async def no_overrides():
        return {}

    monkeypatch.setattr(graph, "get_config_overrides", no_overrides)
    entries = _run(graph.registry())
    ctx = [e for e in entries if e["name"] == "Context Agent"]
    assert len(ctx) == 1 and ctx[0]["config_key"] == "context"
    assert len(ctx[0]["tool_superset"]) == 11
    sup = [e for e in entries if e["name"] == "Supervisor"][0]
    assert "delegate_to_context_agent" in [t["name"] for t in sup["tools"]]

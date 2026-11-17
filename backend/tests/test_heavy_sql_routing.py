"""Quick 012: heavy SQL investigations go to the Data Agent, on Sonnet.

Run 14 on the client install: the supervisor (Opus) ran about 20 queries over a
4.4M-row telemetry table itself, two hit the statement timeout, and every result
piled into its own context. Pins:
- the SQL routing rule (quick lookups stay, heavy investigations are delegated
  with a standalone brief, the supervisor keeps the plan, files keep the
  pipeline) rides after the editable supervisor prompt, never stored, so a
  customised prompt still gets it; delegates never do;
- delegate_heavy_sql off (or no delegate_to_data_agent tool) leaves the
  supervisor prompt and the delegate's description exactly as before;
- the switch reaches effective_configs, so a toggle rebuilds the agents;
- the Data Agent's output contract rides after its (possibly customised)
  prompt; its "auto" model resolves to data_model (Sonnet 5.5) with its own
  effort role, an explicit model still wins, and the Agents page shows it;
- the run's "Lessons from earlier runs" block reaches Data Agent briefs;
- search_business_context gives approximate table sizes from pg_class.

Pure logic: create_react_agent, _make_model, _log_tool and the tenant DB are
faked; no DB/LLM/network."""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app import configservice, contextassembler, llmclient, schemadiscovery
from app.agents import graph
from app.config import Settings

AGENT_KEYS = ("supervisor", "data", "analysis", "document", "drawing")


class _FakeModel:
    pass


class _FakeAgent:
    def __init__(self, key, seen):
        self.key, self.seen = key, seen

    async def ainvoke(self, payload, config=None):
        self.seen[self.key] = payload["messages"][-1][1]
        return {"messages": [AIMessage(content="done")]}


def _configs(**sup):
    configs = {k: {**graph.DEFAULT_CONFIGS[k], "prompt": f"OVERRIDE::{k}"}
               for k in graph.DEFAULT_CONFIGS}
    configs["supervisor"].update(sup)
    return configs


def _build(monkeypatch, configs, skills_index=""):
    """Build with fakes; returns (prompts, supervisor tools by name, roles, seen tasks)."""
    prompts, sup_tools, roles, seen = {}, {}, {}, {}

    def fake_cra(model, tool_list, prompt=None):
        for k in AGENT_KEYS:
            if f"OVERRIDE::{k}" in (prompt or ""):
                prompts[k] = prompt
                roles[k] = model.role
                if k == "supervisor":
                    sup_tools.update({t.name: t for t in tool_list})
                return _FakeAgent(k, seen)
        return _FakeAgent("other", seen)

    def fake_make_model(cfg, role="supervisor", **kw):
        m = _FakeModel()
        m.role = role
        return m

    async def no_log(*a, **k):
        return None

    monkeypatch.setattr(graph, "create_react_agent", fake_cra)
    monkeypatch.setattr(graph, "_make_model", fake_make_model)
    monkeypatch.setattr(graph, "_log_tool", no_log)
    graph._build(configs, skills_index=skills_index)
    return prompts, sup_tools, roles, seen


@pytest.fixture(autouse=True)
def _reset():
    yield
    graph.reset_agents()
    schemadiscovery.reset_row_estimates()


# ── the routing rule (offline eval of the text) ─────────────────────────

def test_rule_names_both_sides_of_the_split():
    r = " ".join(graph.SQL_ROUTING_RULE.split())
    for needle in (
            "wins where they say to run the SQL yourself",
            "QUICK LOOKUP: answer it yourself with search_business_context + run_sql",
            "A count, a list, one or two figures; about 3 queries or fewer on small tables",
            "HEAVY INVESTIGATION: delegate it with delegate_to_data_agent",
            "More than about 3 queries expected",
            "raw telemetry, sensor readings, events, logs",
            "matching one source against another",
            "reconciliations",
            "30 s query timeout",
            "do not run the investigation yourself first",
            "the Data Agent sees only your text",
            "the business rules, definitions and context rows you already found, pasted in",
            "the expected output",
            "the slug of any catalog skill that fits",
            "one brief per independent part, sent as parallel delegations",
            "YOU KEEP THE PLAN",
            "write the reply yourself from its numbers",
            "FILES AND REPORTS keep the pipeline above"):
        assert needle in r, needle


def test_rule_and_contract_stay_short():
    assert len(graph.SQL_ROUTING_RULE) <= 1800
    assert len(graph.DATA_OUTPUT_CONTRACT) <= 1000


def test_contract_names_every_part():
    c = " ".join(graph.DATA_OUTPUT_CONTRACT.split())
    for needle in ("concise summary that answers the brief with the actual numbers",
                   "'SQL:' with the final query behind each number",
                   "over about 25 rows: save it with run_sql_to_csv",
                   "filename and row count",
                   "'Caveats:'", "rows skipped or excluded",
                   "timed out and how you narrowed them",
                   "never pull raw rows"):
        assert needle in c, needle


# ── where the blocks land in the built prompts ───────────────────────────

def test_customised_supervisor_prompt_gets_the_rule_after_it(monkeypatch):
    prompts, *_ = _build(monkeypatch, _configs(delegate_heavy_sql=True),
                         skills_index="SKILLS (x). Catalog:\n- a: b")
    sup = prompts["supervisor"]
    assert sup.endswith("\n\n" + graph.SQL_ROUTING_RULE)
    assert (sup.index(graph.GROUNDING_RULES) < sup.index("SKILLS (")
            < sup.index("OVERRIDE::supervisor") < sup.index("SQL ROUTING"))
    for k in ("data", "analysis", "document", "drawing"):
        assert "SQL ROUTING" not in prompts[k], f"the routing rule leaked into {k}"


def test_rule_follows_the_persona(monkeypatch):
    prompts, *_ = _build(monkeypatch, _configs(persona="We run trucks."))
    sup = prompts["supervisor"]
    assert sup.index("We run trucks.") < sup.index("SQL ROUTING")


def test_switch_off_leaves_the_supervisor_prompt_byte_identical(monkeypatch):
    configs = _configs(delegate_heavy_sql=False)
    prompts, sup_tools, *_ = _build(monkeypatch, configs, skills_index="SKILLS (x)")
    # Quick 029: switch off drops only the SQL routing rule; the routing rules still follow.
    assert prompts["supervisor"] == (graph.GROUNDING_RULES + "\n\nSKILLS (x)\n\n"
                                     + "OVERRIDE::supervisor" + "\n\n"
                                     + graph._supervisor_rules(configs["supervisor"]))
    assert "SQL ROUTING" not in prompts["supervisor"]
    assert sup_tools["delegate_to_data_agent"].description.startswith(
        "Hand a data-retrieval task to the SQL specialist."), "off keeps the old description"


def test_no_rule_without_the_data_delegate(monkeypatch):
    configs = _configs(delegate_heavy_sql=True)
    configs["supervisor"]["tools"] = [t for t in configs["supervisor"]["tools"]
                                      if t != "delegate_to_data_agent"]
    prompts, sup_tools, *_ = _build(monkeypatch, configs)
    assert "SQL ROUTING" not in prompts["supervisor"]
    assert "delegate_to_data_agent" not in sup_tools


def test_switch_on_sharpens_the_delegate_description(monkeypatch):
    _, sup_tools, *_ = _build(monkeypatch, _configs(delegate_heavy_sql=True))
    d = sup_tools["delegate_to_data_agent"].description
    assert d == graph.DATA_DELEGATE_DESCRIPTION
    assert "heavy SQL investigation" in d and "caveats" in d


def test_data_agent_gets_the_contract_after_its_customised_prompt(monkeypatch):
    for switch in (True, False):
        prompts, *_ = _build(monkeypatch, _configs(delegate_heavy_sql=switch))
        data = prompts["data"]
        assert data.endswith("\n\n" + graph.DATA_OUTPUT_CONTRACT)
        assert data.index("OVERRIDE::data") < data.index("OUTPUT CONTRACT")
        for k in ("supervisor", "analysis", "document", "drawing"):
            assert "OUTPUT CONTRACT" not in prompts[k]


def test_default_data_prompt_describes_the_investigation_role():
    p = graph.DEFAULT_PROMPTS["data"]
    assert p.startswith("You are the SQL specialist")
    assert "heavy investigations" in p and "data pulls for files" in p
    assert "OUTPUT CONTRACT" not in p, "the contract is appended at build time, never stored"


def test_data_agent_recursion_default_fits_an_investigation():
    assert graph.DEFAULT_CONFIGS["data"]["recursion_limit"] == 50
    assert graph.DEFAULT_CONFIGS["supervisor"]["recursion_limit"] == 50
    assert graph.DEFAULT_CONFIGS["analysis"]["recursion_limit"] == 25


# ── the switch is a live setting ─────────────────────────────────────────

def test_settings_defaults():
    s = Settings(_env_file=None)
    assert s.delegate_heavy_sql is True
    assert s.data_model == "claude-sonnet-5-5"
    assert s.data_effort == "medium"
    assert s.supervisor_model == "claude-opus-5-5", "the supervisor stays on Opus"


def test_switch_and_model_are_dashboard_editable():
    meta = configservice.REGISTRY["delegate_heavy_sql"]
    assert meta["group"] == "tuning" and not meta["secret"]
    assert "Data Agent" in meta["label"] and "live" in meta["label"]
    assert configservice.REGISTRY["data_model"]["group"] == "llm"
    assert configservice.REGISTRY["data_model"]["options"] == llmclient.model_options()
    assert configservice.REGISTRY["data_effort"]["choices"] == list(llmclient.EFFORT_LEVELS)
    assert "data_model" in configservice.MODEL_ROLES
    configservice._validate_value("delegate_heavy_sql", "false")
    with pytest.raises(ValueError):
        configservice._validate_value("delegate_heavy_sql", "sometimes")


async def test_switch_reaches_effective_configs_and_forces_a_rebuild(monkeypatch):
    async def no_overrides():
        return {}

    async def no_persona():
        return ""

    monkeypatch.setattr(graph, "get_config_overrides", no_overrides)
    monkeypatch.setattr(graph, "_latest_persona", no_persona)
    flag = {"v": True}
    monkeypatch.setattr(graph, "get_settings",
                        lambda: SimpleNamespace(delegate_heavy_sql=flag["v"]))
    on = await graph.effective_configs()
    assert on["supervisor"]["delegate_heavy_sql"] is True
    flag["v"] = False
    off = await graph.effective_configs()
    assert off["supervisor"]["delegate_heavy_sql"] is False
    assert on != off, "run() rebuilds when configs != _built_with"


# ── the Data Agent's model ───────────────────────────────────────────────

def _s(**kw):
    base = dict(anthropic_api_key="k", openai_api_key="", supervisor_model="claude-opus-5-5",
                data_model="claude-sonnet-5-5", openai_supervisor_model="gpt-4o",
                supervisor_effort="medium", data_effort="low", context_effort="high")
    base.update(kw)
    return SimpleNamespace(**base)


def test_auto_resolves_per_role():
    assert graph._auto_model("data", _s()) == "claude-sonnet-5-5"
    assert graph._auto_model("supervisor", _s()) == "claude-opus-5-5"
    assert graph._auto_model("data", _s(data_model="")) == "claude-opus-5-5"
    assert graph._auto_model("data", _s(anthropic_api_key="")) == "gpt-4o"
    bare = SimpleNamespace(anthropic_api_key="k", supervisor_model="claude-opus-5-5",
                           openai_supervisor_model="gpt-4o")
    assert graph._auto_model("data", bare) == "claude-opus-5-5", "older settings objects"


def test_make_model_data_role_runs_sonnet_with_its_own_effort(monkeypatch):
    monkeypatch.setattr(graph, "get_settings", lambda: _s())
    cfg = {"model": "auto", "temperature": 0.2, "max_tokens": 4096}
    m = graph._make_model(cfg, role="data")
    assert m.model == "claude-sonnet-5-5"
    payload = m._get_request_payload([HumanMessage("x")])
    assert payload["output_config"]["effort"] == "low"
    assert graph._make_model(cfg, role="supervisor").model == "claude-opus-5-5"
    explicit = graph._make_model({**cfg, "model": "claude-opus-5-5"}, role="data")
    assert explicit.model == "claude-opus-5-5", "an explicit model on the Agents page wins"


def test_build_passes_the_data_role(monkeypatch):
    _, _, roles, _ = _build(monkeypatch, _configs())
    assert roles["data"] == "data"
    assert roles["supervisor"] == "supervisor"
    assert roles["analysis"] == "supervisor", "other specialists are unchanged"


async def test_agents_page_shows_the_resolved_data_model(monkeypatch):
    overrides = {}

    async def get_overrides():
        return overrides

    monkeypatch.setattr(graph, "get_config_overrides", get_overrides)
    monkeypatch.setattr(graph, "get_settings", lambda: _s(context_model="claude-opus-5-5"))
    entries = {e["name"]: e for e in await graph.registry()}
    assert entries["Data Agent"]["model"] == "claude-sonnet-5-5"
    assert entries["Supervisor"]["model"] == "claude-opus-5-5"
    assert entries["Data Agent"]["config"]["model"] == "auto", "still editable as auto"
    assert "recursion limit 50" in entries["Data Agent"]["guardrails"]
    overrides["data"] = {"model": "claude-haiku-5-5"}
    entries = {e["name"]: e for e in await graph.registry()}
    assert entries["Data Agent"]["model"] == "claude-haiku-5-5"


def test_model_catalog_note_names_the_data_agent():
    notes = {m["value"]: m["note"] for m in llmclient.CURRENT_MODELS}
    assert "Data Agent" in notes["claude-sonnet-5-5"]


# ── lessons reach the Data Agent ─────────────────────────────────────────

LESSON_CTX = (contextassembler.LESSONS_HEADER + "\n"
              "- For fuel drops use fuel_readings with LAG per vehicle; never scan it unfiltered.\n"
              "- Match drops to fuel_events within 30 minutes.\n\n"
              + contextassembler.LEARNINGS_HEADER + "\n- [fact] Depot is in Rajkot.")


def test_lessons_block_extracts_only_the_lessons():
    block = contextassembler.lessons_block(LESSON_CTX)
    assert block.startswith(contextassembler.LESSONS_HEADER)
    assert "LAG per vehicle" in block and "30 minutes" in block
    assert "Rajkot" not in block
    assert contextassembler.lessons_block("") == ""
    assert contextassembler.lessons_block(contextassembler.LEARNINGS_HEADER + "\n- x") == ""
    assert contextassembler.lessons_block(contextassembler.LESSONS_HEADER) == ""


def test_lessons_handoff_skips_lines_the_brief_already_has():
    tok = graph.ctx_lessons.set(contextassembler.lessons_block(LESSON_CTX))
    try:
        full = graph._lessons_handoff("Find missed fuel drops.")
        assert full.startswith("\n\n" + contextassembler.LESSONS_HEADER + "\n")
        assert "LAG per vehicle" in full and "30 minutes" in full
        part = graph._lessons_handoff(
            "Find missed fuel drops. Note: match drops to fuel_events within 30 minutes.")
        assert "LAG per vehicle" in part and "30 minutes" not in part
    finally:
        graph.ctx_lessons.reset(tok)
    assert graph._lessons_handoff("anything") == "", "outside a run: nothing"


async def test_data_briefs_carry_the_lessons_other_delegates_do_not(monkeypatch):
    _, sup_tools, _, seen = _build(monkeypatch, _configs())
    tok = graph.ctx_lessons.set(contextassembler.lessons_block(LESSON_CTX))
    try:
        await sup_tools["delegate_to_data_agent"].ainvoke({"task": "Find missed fuel drops."})
        await sup_tools["delegate_to_analysis_agent"].ainvoke({"task": "Chart drops.csv."})
    finally:
        graph.ctx_lessons.reset(tok)
    assert seen["data"].startswith("Find missed fuel drops.\n\n" + contextassembler.LESSONS_HEADER)
    assert seen["analysis"] == "Chart drops.csv."


# ── approximate table sizes ──────────────────────────────────────────────

def _fake_estimates(monkeypatch, rows=None, fail=False):
    calls = {"n": 0}

    async def fake_sql(sql, limit=None):
        calls["n"] += 1
        assert "reltuples" in sql and "has_table_privilege" in sql
        if fail:
            raise ConnectionError("tenant DB down")
        return rows or []

    monkeypatch.setattr(schemadiscovery, "run_readonly_sql", fake_sql)
    schemadiscovery.reset_row_estimates()
    return calls


async def test_row_estimates_are_cached_and_skip_unanalysed(monkeypatch):
    calls = _fake_estimates(monkeypatch, rows=[
        {"table_name": "VehicleTelemetry", "row_estimate": 4_412_000},
        {"table_name": "vehicles", "row_estimate": 12},
        {"table_name": "fresh", "row_estimate": -1}])
    est = await schemadiscovery.row_estimates()
    assert est == {"vehicletelemetry": ("VehicleTelemetry", 4_412_000),
                   "vehicles": ("vehicles", 12)}
    await schemadiscovery.row_estimates()
    assert calls["n"] == 1, "cached"


async def test_row_estimates_fail_soft_and_back_off(monkeypatch):
    calls = _fake_estimates(monkeypatch, fail=True)
    assert await schemadiscovery.row_estimates() == {}
    assert await schemadiscovery.row_estimates() == {}
    assert calls["n"] == 1, "a failed read is not retried on every search"


async def test_sizes_note_marks_large_tables_with_stored_names(monkeypatch):
    _fake_estimates(monkeypatch, rows=[
        {"table_name": "VehicleTelemetry", "row_estimate": 4_412_000},
        {"table_name": "vehicles", "row_estimate": 12},
        {"table_name": "fuel_events", "row_estimate": 15_400}])
    note = await graph._sizes_note({"public.VehicleTelemetry", "vehicles", "fuel_events",
                                    "unknown_table"})
    assert note.startswith("\n\nTABLE SIZES (approximate rows")
    assert "VehicleTelemetry ~4.4M (large), fuel_events ~15k, vehicles ~12" in note
    assert "never pull raw rows" in note
    small = await graph._sizes_note({"vehicles"})
    assert "vehicles ~12" in small and "(large)" not in small and "never pull" not in small
    assert await graph._sizes_note(set()) == ""
    assert await graph._sizes_note({"unknown_table"}) == ""


async def test_sizes_note_never_breaks_search(monkeypatch):
    async def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(schemadiscovery, "row_estimates", boom)
    assert await graph._sizes_note({"vehicles"}) == ""


async def test_search_business_context_appends_the_sizes(monkeypatch):
    _fake_estimates(monkeypatch, rows=[{"table_name": "fuel_readings",
                                        "row_estimate": 1_500_000}])

    async def fake_search(q, limit=8):
        return [{"type": "table", "table_name": "fuel_readings", "text": "raw readings"}]

    async def no_rules(*a, **k):
        return ""

    async def no_log(*a, **k):
        return None

    monkeypatch.setattr(graph, "_search_ctx", fake_search)
    monkeypatch.setattr(graph, "_rules_note", no_rules)
    monkeypatch.setattr(graph, "_log_tool", no_log)
    out = await graph.search_business_context.ainvoke({"query": "fuel readings"})
    assert out.endswith("fuel_readings ~1.5M (large). Large tables: filter and aggregate "
                        "inside SQL (period, entity, window functions); never pull raw rows.")
    assert "approximate row count" in graph.search_business_context.description


# ── the Data Agent's own token budget ────────────────────────────────────

class _CfgAgent:
    """Records the config each delegate was invoked with; optionally raises."""

    def __init__(self, key, seen, raise_exc=None):
        self.key, self.seen, self.raise_exc = key, seen, raise_exc

    async def ainvoke(self, payload, config=None):
        self.seen[self.key] = config
        if self.raise_exc is not None:
            raise self.raise_exc
        return {"messages": [AIMessage(content="done")]}


def _build_cfg(monkeypatch, raise_for=None):
    sup_tools, seen, logged = {}, {}, []

    def fake_cra(model, tool_list, prompt=None):
        for k in AGENT_KEYS:
            if f"OVERRIDE::{k}" in (prompt or ""):
                if k == "supervisor":
                    sup_tools.update({t.name: t for t in tool_list})
                return _CfgAgent(k, seen, (raise_for or {}).get(k))
        return _CfgAgent("other", seen)

    async def log(name, args, result, start, refused=False):
        logged.append((name, args, result))

    monkeypatch.setattr(graph, "create_react_agent", fake_cra)
    monkeypatch.setattr(graph, "_make_model", lambda cfg, role="supervisor", **kw: _FakeModel())
    monkeypatch.setattr(graph, "_log_tool", log)
    graph._build(_configs())
    return sup_tools, seen, logged


def _handlers(callbacks):
    if callbacks is None:
        return []
    if isinstance(callbacks, (list, tuple)):
        return list(callbacks)
    return list(callbacks.handlers)


async def test_data_delegation_spends_its_own_budget_not_the_runs(monkeypatch):
    from app.budget import TokenBudget
    sup_tools, seen, logged = _build_cfg(monkeypatch)
    run_budget = TokenBudget(300_000)
    await sup_tools["delegate_to_data_agent"].ainvoke(
        {"task": "Find missed fuel drops."}, config={"callbacks": [run_budget]})
    hs = _handlers(seen["data"]["callbacks"])
    own = [h for h in hs if isinstance(h, graph.DataAgentBudget)]
    assert len(own) == 1 and own[0].budget == Settings(_env_file=None).data_token_budget
    assert not [h for h in hs if type(h) is TokenBudget], "the run's budget is not inherited"
    assert seen["data"]["recursion_limit"] == 50
    name, args, _ = logged[-1]
    assert name == "delegate_to_data_agent"
    assert args["token_budget"] == own[0].budget and args["tokens_used"] == 0
    await sup_tools["delegate_to_analysis_agent"].ainvoke(
        {"task": "Chart drops.csv."}, config={"callbacks": [run_budget]})
    hs = _handlers(seen["analysis"]["callbacks"])  # Quick 036: inherited copy plus the cut-off guard
    assert [h for h in hs if type(h) is TokenBudget], "other delegates keep inheriting the run budget"
    assert not [h for h in hs if isinstance(h, graph.DataAgentBudget)]


async def test_data_budget_stop_is_a_tool_result_not_a_failed_run(monkeypatch):
    sup_tools, _, logged = _build_cfg(
        monkeypatch, raise_for={"data": graph.DataAgentBudgetExceeded("over")})
    out = await sup_tools["delegate_to_data_agent"].ainvoke({"task": "Reconcile everything."})
    assert out.startswith("DATA AGENT STOPPED: it used its token budget (400000 tokens)")
    assert "one part of the question each" in out
    assert logged[-1][2] == out


async def test_the_runs_own_budget_still_aborts(monkeypatch):
    from app.budget import BudgetExceeded
    sup_tools, *_ = _build_cfg(monkeypatch, raise_for={"data": BudgetExceeded("run over")})
    with pytest.raises(BudgetExceeded):
        await sup_tools["delegate_to_data_agent"].ainvoke({"task": "x"})


def test_data_budget_counts_and_trips():
    from langchain_core.outputs import LLMResult
    b = graph.DataAgentBudget(100)
    b.on_llm_end(LLMResult(generations=[], llm_output={"usage": {"total_tokens": 60}}))
    with pytest.raises(graph.DataAgentBudgetExceeded):
        b.on_llm_end(LLMResult(generations=[], llm_output={"usage": {"total_tokens": 60}}))


def test_data_budget_setting():
    assert Settings(_env_file=None).data_token_budget == 400_000
    meta = configservice.REGISTRY["data_token_budget"]
    assert meta["group"] == "tuning" and meta["min"] <= 400_000 <= meta["max"]
    assert "own cap" in meta["label"]

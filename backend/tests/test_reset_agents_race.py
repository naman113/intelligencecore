"""Quick 011: reset_agents() during an in-flight run must not break later delegations.

Run 14 (client install): a skill toggle called reset_agents() mid-run, which nulled
the module-level specialists, so every later delegate_to_* hit
`'NoneType' object has no attribute 'ainvoke'`. Delegate tools now close over
their own build's specialists; reset only marks the supervisor cache stale.
No DB, LLM or network: models and agents are stubs.
"""

from langchain_core.messages import AIMessage

from app.agents import graph

KEYS = list(graph.DEFAULT_CONFIGS)


class _Model:
    pass


class _Agent:
    def __init__(self, key, built, log):
        self.key, self.built, self.log = key, built, log

    async def ainvoke(self, payload, config=None):
        self.log.append((self.built, self.key))
        return {"messages": [AIMessage(content=f"{self.key}#{self.built}")]}


def _install(monkeypatch):
    log, sup = [], {}
    state = {"builds": 0}

    def fake_cra(model, tool_list, prompt=None):
        key = next((k for k in KEYS if f"OVERRIDE::{k}" in (prompt or "")), "other")
        if key == "data":  # first specialist of a build
            state["builds"] += 1
        agent = _Agent(key, state["builds"], log)
        if key == "supervisor":
            sup[state["builds"]] = {t.name: t for t in tool_list}
        return agent

    async def no_log(*a, **k):
        return None

    monkeypatch.setattr(graph, "create_react_agent", fake_cra)
    monkeypatch.setattr(graph, "_make_model", lambda cfg, role="supervisor", **kw: _Model())
    monkeypatch.setattr(graph, "_log_tool", no_log)
    configs = {k: {**graph.DEFAULT_CONFIGS[k], "prompt": f"OVERRIDE::{k}"} for k in KEYS}
    return configs, sup, log


async def test_delegations_survive_a_reset_between_calls(monkeypatch):
    configs, sup, log = _install(monkeypatch)
    try:
        graph._build(configs)
        tools = sup[1]
        assert await tools["delegate_to_data_agent"].ainvoke({"task": "t"}) == "data#1"
        graph.reset_agents()  # e.g. a skill toggled on the Skills page mid-run
        assert graph._supervisor is None and graph._built_with is None
        assert await tools["delegate_to_analysis_agent"].ainvoke({"task": "t"}) == "analysis#1"
        assert await tools["delegate_to_document_agent"].ainvoke({"task": "t"}) == "document#1"
        assert await tools["delegate_to_drawing_agent"].ainvoke({"task": "t"}) == "drawing#1"
        if "delegate_to_dashboard_agent" in tools:
            assert await tools["delegate_to_dashboard_agent"].ainvoke({"task": "t"}) == "dashboard#1"
        if "delegate_to_context_agent" in tools:
            out = await tools["delegate_to_context_agent"].ainvoke({"task": "t"})
            assert "AGENT ERROR" not in out and "context#1" in out
    finally:
        graph.reset_agents()


async def test_old_run_keeps_old_specialists_after_a_newer_build(monkeypatch):
    configs, sup, log = _install(monkeypatch)
    try:
        graph._build(configs)
        graph.reset_agents()
        graph._build(configs)  # next run rebuilt
        assert await sup[1]["delegate_to_data_agent"].ainvoke({"task": "t"}) == "data#1"
        assert await sup[2]["delegate_to_data_agent"].ainvoke({"task": "t"}) == "data#2"
    finally:
        graph.reset_agents()


async def test_next_run_rebuilds_after_reset_and_pins_its_supervisor(monkeypatch):
    configs, sup, log = _install(monkeypatch)

    async def eff():
        return configs

    async def idx():
        return ""

    from app import skillservice
    monkeypatch.setattr(graph, "effective_configs", eff)
    monkeypatch.setattr(skillservice, "index_lines", idx)
    try:
        await graph.run([], "hello")
        assert (1, "supervisor") in log
        await graph.run([], "again")  # cached: no rebuild
        assert log.count((1, "supervisor")) == 2 and 2 not in {b for b, _ in log}
        graph.reset_agents()
        await graph.run([], "after reset")  # rebuilt with fresh specialists
        assert (2, "supervisor") in log
    finally:
        graph.reset_agents()

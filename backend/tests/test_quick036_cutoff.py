"""Quick 036 (item 1): a delegate response that stopped at max_tokens while holding a tool
call is never run and never retried; the delegation ends with a fixed STOPPED text and
the stop is recorded (token-cost analysis 2026-10-28, cause 1).

Run through a real create_react_agent graph with a fake chat model, so the langchain-core
metadata merge, the callback dispatch and the LangGraph propagation are the installed
ones. No DB, LLM or network."""

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult
from langchain_core.tools import tool

from app.agents import graph
from app.budget import TokenBudget

KEYS = ("supervisor", "data", "analysis", "document", "drawing", "dashboard", "context")


# ── fakes ──────────────────────────────────────────────────────────────────

class _ScriptedModel(BaseChatModel):
    """Returns the scripted AIMessages in order. stop_reason rides in llm_output only, so
    it reaches response_metadata through langchain-core's own merge, as with ChatAnthropic."""
    script: list
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kw):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        msg, stop_reason = self.script[self.calls]
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=msg)],
                          llm_output={"stop_reason": stop_reason, "model_name": "scripted"})


def _cut_message(name="preview_dashboard", out_tokens=16000):
    return AIMessage(content=[{"type": "text", "text": "Here is the spec"}],
                     tool_calls=[{"name": name, "args": {"spec": {"title": "Sal"}},
                                  "id": "toolu_cut", "type": "tool_call"}],
                     usage_metadata={"input_tokens": 10, "output_tokens": out_tokens,
                                     "total_tokens": 10 + out_tokens})


class _FakeAgent:
    def __init__(self, key, seen, exc=None):
        self.key, self.seen, self.exc = key, seen, exc
        self.calls = 0
        self.payloads = []

    async def ainvoke(self, payload, config=None):
        self.calls += 1
        self.payloads.append(payload)
        self.seen.setdefault(self.key, []).append(config)
        if self.exc is not None:
            raise self.exc
        return {"messages": [AIMessage(content=f"{self.key} done")]}


def _configs():
    return {k: {**graph.DEFAULT_CONFIGS[k], "prompt": f"OVERRIDE::{k}"}
            for k in graph.DEFAULT_CONFIGS}


def _key(prompt):
    text = prompt if isinstance(prompt, str) else (prompt.content[0]["text"] if prompt else "")
    for k in KEYS:
        if f"OVERRIDE::{k}\n" in text + "\n":
            return k
    return "other"


def _build(monkeypatch, real=None, exc=None):
    """graph._build with fakes. `real` maps an agent key to (model, tools) built with the
    installed create_react_agent; every other agent is a _FakeAgent."""
    from langgraph.prebuilt import create_react_agent as real_cra
    sup_tools, seen, agents, logged = {}, {}, {}, []

    def fake_cra(model, tool_list, prompt=None):
        k = _key(prompt)
        if k == "supervisor":
            sup_tools.update({t.name: t for t in tool_list})
        if real and k in real:
            m, tools = real[k]
            return real_cra(m, tools, prompt="you build dashboards")
        agents[k] = _FakeAgent(k, seen, (exc or {}).get(k))
        return agents[k]

    async def log(name, args, result, start, refused=False):
        logged.append({"name": name, "args": args, "result": result, "refused": refused})

    monkeypatch.setattr(graph, "create_react_agent", fake_cra)
    monkeypatch.setattr(graph, "_make_model", lambda cfg, role="supervisor", **kw: object())
    monkeypatch.setattr(graph, "_log_tool", log)
    graph._build(_configs())
    return SimpleNamespace(tools=sup_tools, seen=seen, agents=agents, logged=logged)


def _handlers(callbacks):
    if callbacks is None:
        return []
    if isinstance(callbacks, (list, tuple)):
        return list(callbacks)
    return list(callbacks.handlers)


# ── 1. a tool call cut off at max_tokens is never run or retried ───────────

def test_guard_raises_only_for_a_cut_off_tool_call():
    g = graph.CutOffGuard()
    assert g.raise_error is True and not g.run_inline, "the usage ledger (inline) records first"

    def res(msg, **meta):
        return LLMResult(generations=[[ChatGeneration(message=msg)]], llm_output=meta)

    with pytest.raises(graph.ToolCallCutOff) as e:
        g.on_llm_end(res(_cut_message(), stop_reason="max_tokens"))
    assert e.value.tools == ["preview_dashboard"] and e.value.output_tokens == 16000
    msg = _cut_message()
    msg.response_metadata = {"finish_reason": "length"}  # the OpenAI fallback
    with pytest.raises(graph.ToolCallCutOff):
        g.on_llm_end(res(msg))
    bad = AIMessage(content="x", invalid_tool_calls=[
        {"name": "save_dashboard_version", "args": '{"slug": "s', "id": "t1", "error": None,
         "type": "invalid_tool_call"}])
    with pytest.raises(graph.ToolCallCutOff):
        g.on_llm_end(res(bad, stop_reason="max_tokens"))
    # a complete tool call, or a cut-off answer with no tool call, passes through
    g.on_llm_end(res(_cut_message(), stop_reason="tool_use"))
    g.on_llm_end(res(AIMessage(content="long text"), stop_reason="max_tokens"))
    g.on_llm_end(SimpleNamespace(llm_output={"token_usage": {"total_tokens": 3}}, generations=[]))
    assert not issubclass(graph.ToolCallCutOff, graph.BudgetExceeded), "never aborts the run"


async def test_cut_off_dashboard_call_stops_the_delegation_without_running_it(monkeypatch):
    ran = []

    @tool
    async def preview_dashboard(spec: dict) -> str:
        """Preview a spec."""
        ran.append(spec)
        return "ok"

    model = _ScriptedModel(script=[(_cut_message(), "max_tokens"),
                                   (AIMessage(content="never reached"), "end_turn")])
    b = _build(monkeypatch, real={"dashboard": (model, [preview_dashboard])})
    run_budget = TokenBudget(1_000_000)
    out = await b.tools["delegate_to_dashboard_agent"].ainvoke(
        {"task": "Build a sales dashboard."}, config={"callbacks": [run_budget]})
    assert out.startswith("STOPPED: the dashboard draft was too large to write in one response")
    assert "16000 tokens" in out and "smaller dashboard or fewer panels" in out
    assert "not run" in out and "not retried" in out
    assert ran == [], "the cut-off call never reaches the tool"
    assert model.calls == 1, "no blind retry of the same oversized emission"
    for _ in range(200):  # non-inline handlers run beside the guard; give the thread a beat
        if run_budget.used:
            break
        await asyncio.sleep(0.01)
    assert run_budget.used == 16010, "the run's budget still counted the cut-off call"
    rec = b.logged[-1]
    assert rec["name"] == "delegate_to_dashboard_agent" and rec["result"] == out
    assert rec["args"]["stopped"] == "max_tokens"
    assert rec["args"]["cut_off_tools"] == ["preview_dashboard"]
    assert rec["args"]["output_tokens"] == 16000


async def test_complete_tool_calls_still_run_through_the_real_graph(monkeypatch):
    ran = []

    @tool
    async def preview_dashboard(spec: dict) -> str:
        """Preview a spec."""
        ran.append(spec)
        return "ok"

    call = AIMessage(content="", tool_calls=[{"name": "preview_dashboard",
                                              "args": {"spec": {"title": "S"}},
                                              "id": "toolu_1", "type": "tool_call"}])
    model = _ScriptedModel(script=[(call, "tool_use"), (AIMessage(content="Saved v1."), "end_turn")])
    b = _build(monkeypatch, real={"dashboard": (model, [preview_dashboard])})
    out = await b.tools["delegate_to_dashboard_agent"].ainvoke({"task": "Build it."})
    assert out == "Saved v1." and ran == [{"title": "S"}] and model.calls == 2
    assert "stopped" not in b.logged[-1]["args"]


async def test_other_delegates_get_the_guard_with_a_generic_text(monkeypatch):
    b = _build(monkeypatch, exc={"document": graph.ToolCallCutOff(["create_pdf"], 16000)})
    out = await b.tools["delegate_to_document_agent"].ainvoke({"task": "Write the report."})
    assert out.startswith("STOPPED: the document specialist's response reached its output "
                          "limit (16000 tokens, thinking included) in the middle of a "
                          "create_pdf call")
    assert "smaller piece of work" in out
    run_budget = TokenBudget(300_000)
    for name in ("delegate_to_analysis_agent", "delegate_to_data_agent",
                 "delegate_to_drawing_agent", "delegate_to_dashboard_agent"):
        await b.tools[name].ainvoke({"task": "t"}, config={"callbacks": [run_budget]})
    for key in ("analysis", "drawing", "dashboard", "data"):
        hs = _handlers(b.seen[key][-1]["callbacks"])
        assert sum(isinstance(h, graph.CutOffGuard) for h in hs) == 1, key
        if key != "data":
            assert [h for h in hs if type(h) is TokenBudget], f"{key} keeps the run budget"


async def test_context_delegate_gets_the_guard(monkeypatch):
    logged = []

    async def log(name, args, result, start, refused=False):
        logged.append((name, args, result))

    class _Agent:
        async def ainvoke(self, inp, config=None):
            self.cbs = config["callbacks"]
            raise graph.ToolCallCutOff(["propose_context_change"], 16000)

    monkeypatch.setattr(graph, "_log_tool", log)
    graph._context_delegations.clear()
    a = _Agent()
    out = await graph._run_context_delegate(a, "import pack.md", 50)
    graph._context_delegations.clear()
    assert out.startswith("STOPPED: the context specialist's response reached its output limit")
    assert sum(isinstance(h, graph.CutOffGuard) for h in _handlers(a.cbs)) == 1
    assert logged[-1][1]["stopped"] == "max_tokens"


# ── rollback: delegate_truncation_guard = all | dashboard | off ────────────

def _set_mode(monkeypatch, mode):
    monkeypatch.setenv("DELEGATE_TRUNCATION_GUARD", mode)
    graph.get_settings.cache_clear()


@pytest.fixture
def clean_settings():
    yield
    graph.get_settings.cache_clear()


def _guards(cfg):
    return sum(isinstance(h, graph.CutOffGuard) for h in _handlers((cfg or {}).get("callbacks")))


async def test_mode_all_is_the_default_and_guards_every_delegate(monkeypatch, clean_settings):
    from app import configservice
    from app.config import Settings
    assert Settings(_env_file=None).delegate_truncation_guard == "all"
    meta = configservice.REGISTRY["delegate_truncation_guard"]
    assert meta["choices"] == ["all", "dashboard", "off"] and meta["group"] == "tuning"
    assert "live" in meta["label"] and not meta["secret"] and not meta["required"]
    _set_mode(monkeypatch, "all")
    b = _build(monkeypatch)
    for name in ("delegate_to_analysis_agent", "delegate_to_data_agent",
                 "delegate_to_dashboard_agent"):
        await b.tools[name].ainvoke({"task": "t"})
    assert [_guards(b.seen[k][-1]) for k in ("analysis", "data", "dashboard")] == [1, 1, 1]


async def test_mode_dashboard_guards_only_the_dashboard_delegate(monkeypatch, clean_settings):
    _set_mode(monkeypatch, "dashboard")
    b = _build(monkeypatch, exc={"document": graph.ToolCallCutOff(["create_pdf"], 16000)})
    run_budget = TokenBudget(300_000)
    for name in ("delegate_to_analysis_agent", "delegate_to_data_agent",
                 "delegate_to_dashboard_agent"):
        await b.tools[name].ainvoke({"task": "t"}, config={"callbacks": [run_budget]})
    assert _guards(b.seen["dashboard"][-1]) == 1
    assert _guards(b.seen["analysis"][-1]) == 0 and _guards(b.seen["data"][-1]) == 0
    assert "callbacks" not in b.seen["analysis"][-1]


async def test_mode_off_restores_the_pre_quick036_delegate_call_exactly(monkeypatch, clean_settings):
    """off: the config and messages each delegate receives are exactly what the code
    before Quick 036 passed: {"recursion_limit": n} with no callbacks key (inherited
    implicitly), the Data Agent's _context_callbacks(inherited, its budget), and the
    context specialist's _context_callbacks(inherited, its budget); no guard anywhere."""
    _set_mode(monkeypatch, "off")
    b = _build(monkeypatch)
    run_budget = TokenBudget(300_000)
    for name, key in (("delegate_to_analysis_agent", "analysis"),
                      ("delegate_to_document_agent", "document"),
                      ("delegate_to_drawing_agent", "drawing"),
                      ("delegate_to_dashboard_agent", "dashboard")):
        await b.tools[name].ainvoke({"task": "Chart it."}, config={"callbacks": [run_budget]})
        assert b.seen[key][-1] == {"recursion_limit": graph.DEFAULT_CONFIGS[key]["recursion_limit"]}
        assert b.agents[key].payloads[-1] == {"messages": [("user", "Chart it.")]}
    await b.tools["delegate_to_data_agent"].ainvoke({"task": "Q"}, config={"callbacks": [run_budget]})
    cfg = b.seen["data"][-1]
    assert set(cfg) == {"recursion_limit", "callbacks"}
    hs = _handlers(cfg["callbacks"])
    assert [type(h) for h in hs if type(h) in (graph.DataAgentBudget, graph.CutOffGuard)] == [
        graph.DataAgentBudget]
    assert not [h for h in hs if type(h) is TokenBudget], "the run budget is swapped, as before"


async def test_mode_off_context_specialist_gets_only_its_budget(monkeypatch, clean_settings):
    _set_mode(monkeypatch, "off")
    seen = {}

    class _Agent:
        async def ainvoke(self, inp, config=None):
            seen["config"], seen["inp"] = config, inp
            return {"messages": [AIMessage(content="ok")]}

    async def log(*a, **k):
        return None

    monkeypatch.setattr(graph, "_log_tool", log)
    graph._context_delegations.clear()
    await graph._run_context_delegate(_Agent(), "import pack.md", 50)
    graph._context_delegations.clear()
    cbs = seen["config"]["callbacks"]
    assert isinstance(cbs, list) and [type(h) for h in cbs] == [graph.ContextPackBudget]
    assert set(seen["config"]) == {"recursion_limit", "callbacks"}
    assert seen["inp"] == {"messages": [("user", "import pack.md")]}


async def test_mode_off_lets_the_real_graph_run_the_cut_off_call_as_before(monkeypatch, clean_settings):
    _set_mode(monkeypatch, "off")
    ran = []

    @tool
    async def preview_dashboard(spec: dict) -> str:
        """Preview a spec."""
        ran.append(spec)
        return "ok"

    model = _ScriptedModel(script=[(_cut_message(), "max_tokens"),
                                   (AIMessage(content="done after retry"), "end_turn")])
    b = _build(monkeypatch, real={"dashboard": (model, [preview_dashboard])})
    out = await b.tools["delegate_to_dashboard_agent"].ainvoke({"task": "Build it."})
    assert out == "done after retry" and model.calls == 2 and ran == [{"title": "Sal"}]


def test_unknown_mode_degrades_to_all():
    from app.config import Settings
    assert Settings(_env_file=None, delegate_truncation_guard="DASHBOARD").delegate_truncation_guard == "dashboard"
    assert Settings(_env_file=None, delegate_truncation_guard="bogus").delegate_truncation_guard == "all"

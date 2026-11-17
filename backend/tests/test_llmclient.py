"""Phase 12 (CTX-09e): per-model parameter seam, call sites, refusals."""
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app import llmclient

LEGACY = ["claude-sonnet-4-5", "claude-haiku-4-5-20251001", "claude-opus-4-6",
          "claude-opus-4-1", "claude-sonnet-4", "claude-3-5-sonnet-20241022",
          "claude-3-haiku-20240307", "gpt-4o", "gpt-4o-mini", ""]
NEW = ["claude-opus-4-7", "claude-opus-5", "claude-sonnet-5", "claude-opus-5-5",
       "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1",
       "claude-mythos-5-1", "CLAUDE-OPUS-5-5", " claude-opus-5-5 "]


@pytest.mark.parametrize("m", LEGACY)
def test_legacy_accepts(m):
    assert llmclient.accepts_sampling(m) is True


@pytest.mark.parametrize("m", NEW)
def test_new_rejects(m):
    assert llmclient.accepts_sampling(m) is False


def test_payloads():
    p = llmclient.chat_anthropic("claude-opus-5-5", max_tokens=100, api_key="k",
                                 temperature=0.2)._get_request_payload([HumanMessage("hi")])
    assert not {"temperature", "top_p", "top_k"} & set(p)
    p = llmclient.chat_anthropic("claude-sonnet-4-5", max_tokens=100, api_key="k",
                                 temperature=0.2)._get_request_payload([HumanMessage("hi")])
    assert p["temperature"] == 0.2


def test_sdk_kwargs():
    forced = {"type": "tool", "name": "x"}
    assert llmclient.sdk_kwargs(
        "claude-opus-5-5", temperature=0.1, top_p=0.9, top_k=5, tool_choice=forced,
        thinking={"type": "disabled"}, max_tokens=10) == {"max_tokens": 10}
    assert llmclient.sdk_kwargs("claude-opus-5-5", tool_choice={"type": "auto"}) == {
        "tool_choice": {"type": "auto"}}
    r = llmclient.sdk_kwargs("claude-sonnet-4-5", temperature=0.3, tool_choice=forced,
                             thinking={"type": "disabled"})
    assert r == {"temperature": 0.3}


def test_is_refusal():
    assert llmclient.is_refusal(AIMessage(content=[], response_metadata={"stop_reason": "refusal"}))

    class O:
        stop_reason = "refusal"
    assert llmclient.is_refusal(O())
    assert not llmclient.is_refusal(AIMessage(content="x", response_metadata={"stop_reason": "end_turn"}))
    assert not llmclient.is_refusal(AIMessage(content="x"))


def test_constants():
    assert llmclient.MAX_TOKENS_CEILING == 32000
    assert llmclient.REFUSAL_REPLY.strip()


def test_thinking_passthrough():
    m = llmclient.chat_anthropic("claude-opus-5-5", max_tokens=100, api_key="k")
    msgs = [HumanMessage("q"),
            AIMessage(content=[{"type": "thinking", "thinking": "t", "signature": "sig"},
                               {"type": "tool_use", "id": "tu1", "name": "f", "input": {}}]),
            ToolMessage(content="r", tool_call_id="tu1")]
    p = m._get_request_payload(msgs)
    blocks = p["messages"][1]["content"]
    assert blocks[0]["type"] == "thinking" and blocks[0]["signature"] == "sig"
    assert blocks[1]["type"] == "tool_use"


# ── Task 2: call sites ───────────────────────────────────────────────────
from types import SimpleNamespace  # noqa: E402

REFUSAL = AIMessage(content=[], response_metadata={"stop_reason": "refusal"})


def _settings(model, **kw):
    base = dict(anthropic_api_key="k", openai_api_key="o", supervisor_model=model,
                triage_model=model, openai_supervisor_model="gpt-4o")
    base.update(kw)
    return SimpleNamespace(**base)


def _payload(m):
    return m._get_request_payload([HumanMessage("x")])


def test_make_model_by_model(monkeypatch):
    from app.agents import graph
    monkeypatch.setattr(graph, "get_settings", lambda: _settings("claude-sonnet-4-5"))
    cfg = {"model": "claude-opus-5-5", "temperature": 0.2, "max_tokens": 4096}
    assert "temperature" not in _payload(graph._make_model(cfg))
    cfg["model"] = "claude-sonnet-4-5"
    assert _payload(graph._make_model(cfg))["temperature"] == 0.2
    monkeypatch.setattr(graph, "get_settings", lambda: _settings("x", anthropic_api_key=""))
    cfg["model"] = "auto"
    assert type(graph._make_model(cfg)).__name__ == "ChatOpenAI"


@pytest.mark.parametrize("model,temp", [("claude-opus-5-5", None), ("claude-haiku-5-5", None),
                                        ("claude-sonnet-4-5", "keep")])
def test_site_models(monkeypatch, model, temp):
    from app import playbooks, sentinel, triage
    from app.agents import graph
    from app.connectors import db_poller
    s = _settings(model)
    for mod in (graph, sentinel, triage, playbooks):
        monkeypatch.setattr(mod, "get_settings", lambda s=s: s)
    monkeypatch.setattr("app.config.get_settings", lambda: s)
    expected = {"sentinel": 0.0, "triage": 0.0, "playbooks": 0.2, "poller": 0.0}
    for name, m in (("sentinel", sentinel._model()), ("triage", triage._model()),
                    ("playbooks", playbooks._model()), ("poller", db_poller._cheap_model())):
        p = _payload(m)
        if temp is None:
            assert "temperature" not in p
        else:
            assert p["temperature"] == expected[name]


def test_merge_config_ceiling():
    from app.agents import graph
    assert graph._merge_config("supervisor", {"max_tokens": 30000})["max_tokens"] == 30000
    assert graph._merge_config("supervisor", {"max_tokens": 50000})["max_tokens"] == 32000
    assert graph._merge_config("supervisor", {"max_tokens": 100})["max_tokens"] == 256


def test_main_uses_ceiling():
    import inspect
    from app import main
    assert "MAX_TOKENS_CEILING" in inspect.getsource(main)


class _FakeAgent:
    def __init__(self, msg):
        self.msg = msg

    async def ainvoke(self, *a, **k):
        return {"messages": [self.msg]}


class _FakeModel:
    def __init__(self, msg):
        self.msg = msg

    async def ainvoke(self, *a, **k):
        return self.msg


async def test_playbook_refusal_fails_run(monkeypatch):
    from app import playbooks
    calls = []

    class Pool:
        async def fetchrow(self, *a):
            return {"id": 1, "status": "active", "name": "pb", "tool_grants": [],
                    "autonomy_tier": "draft", "goal": "g"}

        async def execute(self, sql, *a):
            calls.append((sql, a))

    async def gp():
        return Pool()

    async def ctx(task, pb):
        return "ctx"
    monkeypatch.setattr(playbooks, "get_pool", gp)
    monkeypatch.setattr(playbooks, "_assemble_context", ctx)
    monkeypatch.setattr(playbooks, "_playbook_registry", lambda: {})
    monkeypatch.setattr(playbooks, "_model", lambda: None)
    monkeypatch.setattr(playbooks, "create_react_agent", lambda *a, **k: _FakeAgent(REFUSAL))
    monkeypatch.setattr(playbooks, "get_settings",
                        lambda: SimpleNamespace(owner_number="", playbook_token_budget=1000))
    await playbooks.run_playbook_task({"id": 7, "playbook_id": 1, "case_id": None,
                                       "expectation_id": None})
    assert calls, "no update"
    assert all("status='done'" not in sql for sql, _ in calls)
    assert any("status='failed'" in sql and "model declined (refusal)" in a for sql, a in calls)


async def test_triage_refusal_leaves_events_pending(monkeypatch):
    from app import triage
    updates = []

    class Conn:
        async def fetch(self, *a):
            return [{"id": 1}, {"id": 2}]

        async def fetchval(self, *a):
            return None

    class Acq:
        async def __aenter__(self):
            return Conn()

        async def __aexit__(self, *a):
            return False

    class Pool:
        def acquire(self):
            return Acq()

        async def execute(self, sql, *a):
            updates.append((sql, a))

    async def gp():
        return Pool()

    async def empty(*a, **k):
        return []

    async def empty_d(*a, **k):
        return {}

    async def policy():
        return ""
    import app.skillservice as ss
    monkeypatch.setattr(ss, "triage_policy_block", policy)
    monkeypatch.setattr(triage, "get_pool", gp)
    monkeypatch.setattr(triage, "_open_cases_index", empty)
    monkeypatch.setattr(triage, "_open_expectations_index", empty)
    monkeypatch.setattr(triage, "_thread_context", empty_d)
    monkeypatch.setattr(triage, "_fmt_event", lambda e, t=None: {"id": e["id"]})
    monkeypatch.setattr(triage, "get_settings", lambda: SimpleNamespace(owner_number="", company_name=""))
    monkeypatch.setattr(triage, "_model", lambda: _FakeModel(REFUSAL))
    import app.agents.graph as g
    monkeypatch.setattr(g, "_callbacks", lambda: {})
    n = await triage._triage_batch()
    assert n == 0  # review WR-04: leave pending for retry
    assert not updates


async def test_poller_refusal_uses_heuristic(monkeypatch):
    from app.connectors import db_poller
    cands = [{"table": '"Machines"'}, {"table": '"AspNetUsers"'}]
    monkeypatch.setattr(db_poller, "_cheap_model", lambda: _FakeModel(REFUSAL))
    refused = await db_poller._rank_watch_worthy(cands)

    class Boom:
        async def ainvoke(self, *a, **k):
            raise RuntimeError("down")
    monkeypatch.setattr(db_poller, "_cheap_model", lambda: Boom())
    assert refused == await db_poller._rank_watch_worthy(cands)


async def test_sentinel_refusal_returns_none(monkeypatch):
    from app import sentinel
    monkeypatch.setattr(sentinel, "_compiler", lambda: _FakeAgent(REFUSAL))
    assert await sentinel._compile_watch({"id": 1, "condition": "c"}) is None


# ── Task 3: graph / docintel refusals ────────────────────────────────────
async def test_graph_run_refusal(monkeypatch):
    from app.agents import graph

    async def cfgs():
        return {}
    monkeypatch.setattr(graph, "effective_configs", cfgs)
    monkeypatch.setattr(graph, "_built_with", {})
    monkeypatch.setattr(graph, "_supervisor", _FakeAgent(REFUSAL))
    monkeypatch.setattr(graph, "_callbacks", lambda: {})
    monkeypatch.setattr(graph, "get_settings",
                        lambda: SimpleNamespace(reactive_token_budget=10 ** 9))
    assert await graph.run([], "hi") == llmclient.REFUSAL_REPLY


async def test_server_tool_refusal_marks_refused(monkeypatch):
    from app.agents import graph
    import sys
    import types

    class Messages:
        async def create(self, **kw):
            return SimpleNamespace(content=[], stop_reason="refusal")

    class AsyncAnthropic:
        def __init__(self, api_key=None):
            self.messages = Messages()
    mod = types.ModuleType("anthropic")
    mod.AsyncAnthropic = AsyncAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    monkeypatch.setattr(graph, "get_settings",
                        lambda: SimpleNamespace(anthropic_api_key="k", research_model="m"))
    out = await graph._server_tool_call([], "q")
    assert out.get("refused") is True


async def test_docintel_refusal_is_failure(monkeypatch, tmp_path):
    from app import docintel
    import sys
    import types

    class Messages:
        async def create(self, **kw):
            return SimpleNamespace(content=[], stop_reason="refusal")

    class AsyncAnthropic:
        def __init__(self, api_key=None):
            self.messages = Messages()
    mod = types.ModuleType("anthropic")
    mod.AsyncAnthropic = AsyncAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    monkeypatch.setattr(docintel, "get_settings",
                        lambda: SimpleNamespace(anthropic_api_key="k", document_model="m"))
    monkeypatch.setattr(docintel, "_content_block", lambda p, d: {"type": "text", "text": "x"})
    f = tmp_path / "a.txt"
    f.write_text("hello")
    with pytest.raises(ValueError, match="model declined"):
        await docintel.extract_pdf(f)


# Phase 13 (GOLIVE-01)
@pytest.mark.parametrize("model,level,want", [
    ("claude-opus-5-5", "medium", "medium"),
    ("claude-haiku-5-5", "low", "low"),
    ("claude-fable-5-1", "max", "max"),
    ("claude-sonnet-4-6", "xhigh", "high"),
    ("claude-opus-4-5-20251101", "max", "high"),
    ("claude-haiku-4-5", "low", None),
    ("claude-sonnet-4-5", "high", None),
    ("gpt-4o", "high", None),
    ("claude-opus-5-5", "bogus", None),
    (" CLAUDE-OPUS-5-5 ", "high", "high"),
])
def test_effort_for(model, level, want):
    assert llmclient.effort_for(model, level) == want


@pytest.mark.parametrize("model,want", [
    ("claude-opus-5-5", True), ("claude-sonnet-5-5", True), ("claude-haiku-5-5", True),
    ("claude-fable-5-1", True), ("claude-opus-5", True), ("claude-sonnet-5", True),
    ("claude-opus-4-7", False), ("claude-sonnet-4-5", False), ("claude-haiku-4-5", False),
    ("gpt-4o", False), ("", False),
])
def test_thinks_by_default(model, want):
    assert llmclient.thinks_by_default(model) is want


def _p13_payload(**kw):
    m = llmclient.chat_anthropic(api_key="k", **kw)
    return m._get_request_payload([HumanMessage("x")])


def test_chat_anthropic_effort_and_floor():
    from types import SimpleNamespace as NS
    p = _p13_payload(model="claude-opus-5-5", max_tokens=1200, role="supervisor",
                 settings=NS(supervisor_effort="medium"))
    assert p["output_config"] == {"effort": "medium"} and p["max_tokens"] == 16000
    assert _p13_payload(model="claude-opus-5-5", max_tokens=30000)["max_tokens"] == 30000
    assert _p13_payload(model="claude-opus-5-5", max_tokens=50000)["max_tokens"] == 32000
    p = _p13_payload(model="claude-sonnet-4-5", max_tokens=1200, role="supervisor")
    assert "output_config" not in p and p["max_tokens"] == 1200
    assert "output_config" not in _p13_payload(model="claude-opus-5-5", max_tokens=1200)


def test_role_effort_fallback():
    from types import SimpleNamespace as NS
    assert llmclient.role_effort("triage", NS()) == "low"
    assert llmclient.role_effort(None) is None


def test_messages_kwargs():
    from types import SimpleNamespace as NS
    fmt = {"format": {"type": "json_schema"}}
    k = llmclient.messages_kwargs(
        "claude-opus-5-5", role="onboarding", max_tokens=16000,
        settings=NS(onboarding_effort="high"), output_config=fmt, messages=[1])
    assert k["output_config"] == {"format": {"type": "json_schema"}, "effort": "high"}
    assert k["model"] == "claude-opus-5-5" and k["max_tokens"] == 16000
    assert llmclient.messages_kwargs("claude-sonnet-5-5", role="document", max_tokens=8192)["max_tokens"] == 16000
    assert llmclient.messages_kwargs("claude-sonnet-5-5", role="document", max_tokens=30000)["max_tokens"] == 21000
    k = llmclient.messages_kwargs("claude-sonnet-4-5", role="document", max_tokens=8192, temperature=0.2)
    assert k["temperature"] == 0.2 and k["max_tokens"] == 8192 and "output_config" not in k
    k = llmclient.messages_kwargs("claude-opus-5-5", max_tokens=100,
                                  tool_choice={"type": "tool", "name": "x"}, thinking={"type": "disabled"})
    assert "tool_choice" not in k and "thinking" not in k


def test_model_options():
    opts = llmclient.model_options()
    groups = [o["group"] for o in opts]
    assert [o["value"] for o in opts[:4]] == [
        "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"]
    assert groups == ["current"] * 4 + ["legacy"] * (len(opts) - 4)
    assert all(0 < len(o["note"]) < 200 for o in opts)
    by = {o["value"]: o["note"] for o in opts}
    assert "30-day data retention" in by["claude-fable-5-1"]
    assert "retires 2026-11-30" in by["claude-sonnet-4-5"]

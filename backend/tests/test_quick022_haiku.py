"""Quick 022: Claude Haiku 5.5 for the memory model, reflection, document extraction
and graph extraction. Defaults, one-setting rollbacks, and the Graphiti Anthropic
client wiring (sanitising, ledger-wrapped SDK client)."""
import json
from types import SimpleNamespace

import pytest

from app import llmclient, memory, memoryservice, usage
from app.config import Settings
from app.memoryservice import MemoryService

HAIKU, SONNET, OPUS = "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"


@pytest.fixture
def clean(monkeypatch):
    for key in ("GRAPHITI_LLM", "GRAPH_MODEL", "REFLECTION_MODEL", "DOCUMENT_MODEL",
                "SUPERVISOR_MODEL", "DATA_MODEL", "RESEARCH_MODEL", "GRAPH_EFFORT",
                "REFLECTION_EFFORT", "ONBOARDING_MODEL", "CONTEXT_MODEL"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


# ── defaults ────────────────────────────────────────────────────────────

def test_switched_defaults(clean):
    s = Settings(_env_file=None)
    assert s.reflection_model == s.document_model == HAIKU
    assert s.reflection_effort == s.graph_effort == "medium"
    assert memory.MEMORY_MODEL_DEFAULT == HAIKU


def test_roles_that_stay_off_haiku(clean):
    s = Settings(_env_file=None)
    assert s.supervisor_model == OPUS          # heavy questions: blind review, Quick 022
    assert s.data_model == SONNET              # Quick 012 routing rule unchanged
    assert s.research_model == SONNET          # Haiku failed read_url tool input
    assert s.onboarding_model == s.context_model == OPUS
    # Graph extraction stays on OpenAI gpt-5.5: Haiku 5.5 invalidated true facts.
    assert s.graphiti_llm == "auto"
    assert s.graph_model == memoryservice.GRAPH_MODEL == SONNET   # opt-in Anthropic path
    from graphiti_core.llm_client.openai_client import OpenAIClient
    assert isinstance(MemoryService._llm_client(s.model_copy(update={
        "anthropic_api_key": "k", "openai_api_key": "sk-test"})), OpenAIClient)


def test_reflection_agent_default_is_haiku():
    from app.agents.graph import DEFAULT_CONFIGS
    assert DEFAULT_CONFIGS["reflection"]["model"] == HAIKU


def test_new_roles_registered():
    from app import configservice
    assert {"reflection", "graph"} <= set(llmclient.ROLES)
    for mk, ek in (("reflection_model", "reflection_effort"), ("graph_model", "graph_effort")):
        assert configservice.MODEL_ROLES[mk][0] == ek
        values = {o["value"] for o in configservice.REGISTRY[mk]["options"]}
        assert HAIKU in values
        assert configservice.REGISTRY[ek]["choices"] == list(llmclient.EFFORT_LEVELS)
    assert "default; anthropic" in configservice.REGISTRY["graphiti_llm"]["label"]


# ── forced tool choice ──────────────────────────────────────────────────

def test_forced_tool_ok_only_on_haiku():
    assert llmclient.forced_tool_ok(HAIKU)
    assert not llmclient.forced_tool_ok(SONNET)
    assert not llmclient.forced_tool_ok(OPUS)
    assert not llmclient.forced_tool_ok("claude-sonnet-4-5")
    assert llmclient.forced_tool_ok("gpt-5.5")


def test_sdk_kwargs_keeps_forced_tool_for_haiku_only():
    tc = {"type": "tool", "name": "extract"}
    assert llmclient.sdk_kwargs(HAIKU, max_tokens=10, tool_choice=tc)["tool_choice"] == tc
    assert "tool_choice" not in llmclient.sdk_kwargs(SONNET, max_tokens=10, tool_choice=tc)
    # Haiku 5.5 rejects a non-default temperature.
    assert "temperature" not in llmclient.sdk_kwargs(HAIKU, max_tokens=10, temperature=1)


# ── Graphiti Anthropic client ───────────────────────────────────────────

class _Inner:
    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply
        self.messages = self
        self.base_url = "https://example.invalid"

    async def create(self, **kw):
        self.calls.append(kw)
        return self.reply


def _settings(**kw):
    base = dict(graphiti_llm="anthropic", anthropic_api_key="sk-ant-test",
                openai_api_key="sk-test", graph_model=HAIKU, graph_effort="low")
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def graph_settings(monkeypatch):
    s = _settings()
    monkeypatch.setattr("app.config.get_settings", lambda: s)
    return s


async def test_graph_messages_sanitised_for_haiku(graph_settings):
    inner = _Inner()
    client = llmclient.GraphAnthropic(inner)
    tc = {"type": "tool", "name": "ExtractedEntities"}
    await client.messages.create(model=HAIKU, max_tokens=8192, temperature=1,
                                 system="s", messages=[{"role": "user", "content": "u"}],
                                 tools=[{"name": "ExtractedEntities"}], tool_choice=tc)
    kw = inner.calls[0]
    assert kw["model"] == HAIKU
    assert "temperature" not in kw
    assert kw["tool_choice"] == tc
    assert kw["output_config"]["effort"] == "low"           # graph_effort, read per call
    assert kw["max_tokens"] == llmclient.THINKING_MAX_TOKENS_FLOOR
    assert client.base_url == inner.base_url                # everything else passes through


async def test_graph_messages_drop_forced_tool_for_sonnet(graph_settings):
    inner = _Inner()
    await llmclient.GraphAnthropic(inner).messages.create(
        model=SONNET, max_tokens=16384, temperature=1, system="s", messages=[],
        tool_choice={"type": "tool", "name": "x"})
    assert "tool_choice" not in inner.calls[0]
    assert "temperature" not in inner.calls[0]


def test_graph_anthropic_is_ledger_wrapped():
    client = llmclient.graph_anthropic("sk-ant-test")
    assert isinstance(client, llmclient.GraphAnthropic)
    assert isinstance(client.inner, usage._Proxy)


def test_llm_client_anthropic_default():
    from graphiti_core.llm_client.anthropic_client import AnthropicClient
    llm = MemoryService._llm_client(_settings())
    assert isinstance(llm, AnthropicClient)
    assert llm.model == llm.small_model == HAIKU
    assert isinstance(llm.client, llmclient.GraphAnthropic)
    # Setup can move graph_model; empty falls back to the code default.
    assert MemoryService._llm_client(_settings(graph_model=SONNET)).model == SONNET
    assert MemoryService._llm_client(_settings(graph_model="")).model == memoryservice.GRAPH_MODEL


@pytest.mark.parametrize("kw", [dict(graphiti_llm="openai"), dict(graphiti_llm="auto"),
                                dict(anthropic_api_key="")])
def test_llm_client_rollback_to_openai(kw):
    from graphiti_core.llm_client.openai_client import OpenAIClient
    llm = MemoryService._llm_client(_settings(**kw))
    assert isinstance(llm, OpenAIClient)
    assert llm.model == memoryservice.GRAPHITI_OPENAI_MODEL


async def test_graphiti_anthropic_round_trip(graph_settings):
    """graphiti-core's AnthropicClient drives a forced tool; the tool input comes back."""
    from graphiti_core.prompts.models import Message
    from pydantic import BaseModel

    class Entities(BaseModel):
        names: list[str]

    reply = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=10, output_tokens=5), stop_reason="tool_use",
        content=[SimpleNamespace(type="tool_use", input={"names": ["Excavator 11"]})])
    inner = _Inner(reply)
    llm = MemoryService._llm_client(_settings())
    llm.client = llmclient.GraphAnthropic(inner)
    out, _, _ = await llm._generate_response(
        [Message(role="system", content="sys"), Message(role="user", content="text")],
        response_model=Entities)
    assert out == {"names": ["Excavator 11"]}
    kw = inner.calls[0]
    assert kw["tool_choice"] == {"type": "tool", "name": "Entities"}
    assert "temperature" not in kw


# ── memory model: Anthropic path and OpenAI rollback ─────────────────────

class _FakeOpenAI:
    calls: list = []

    def __init__(self, **kw):
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kw):
        _FakeOpenAI.calls.append(kw)
        payload = json.dumps({"learnings": [{"kind": "fact", "content": "openai"}]}) \
            if kw.get("response_format") and "messages" in kw \
            and kw["messages"][0]["content"] == "p" else "openai merged text"
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=payload))])


@pytest.fixture
def wired(monkeypatch):
    sent = []

    async def fake_text(model, **kw):
        sent.append({"model": model, **kw})
        if kw.get("json_schema"):
            return json.dumps({"learnings": [{"kind": "fact", "content": "haiku"}]})
        return "haiku merged text"

    _FakeOpenAI.calls = []
    monkeypatch.setattr(llmclient, "anthropic_text", fake_text)
    monkeypatch.setattr(memory, "AsyncOpenAI", _FakeOpenAI)
    monkeypatch.setattr(memory, "_ledger", lambda client, role: client)
    return sent


def _mem_settings(**kw):
    base = dict(anthropic_api_key="sk-ant-test", openai_api_key="sk-test",
                reflection_model=HAIKU)
    base.update(kw)
    return SimpleNamespace(**base)


async def test_reflection_on_haiku(wired):
    items, client = await memory.reflection_items({"model": HAIKU}, "p", "t", 500,
                                                  settings=_mem_settings())
    assert items == [{"kind": "fact", "content": "haiku"}] and client is None
    assert wired[0]["model"] == HAIKU and wired[0]["role"] == "reflection"
    assert wired[0]["json_schema"] == memory.REFLECTION_SCHEMA
    assert _FakeOpenAI.calls == []


async def test_reflection_rollback_to_gpt_4o_mini(wired):
    items, client = await memory.reflection_items(
        {"model": "gpt-4o-mini", "temperature": 0.2}, "p", "t", 500, settings=_mem_settings())
    assert items == [{"kind": "fact", "content": "openai"}] and client is not None
    assert _FakeOpenAI.calls[0]["model"] == "gpt-4o-mini" and wired == []


async def test_reflection_without_anthropic_key_uses_openai(wired):
    await memory.reflection_items({"model": HAIKU}, "p", "t", 500,
                                  settings=_mem_settings(anthropic_api_key=""))
    assert _FakeOpenAI.calls[0]["model"] == memory.OPENAI_MEMORY_MODEL and wired == []


async def test_memory_text_follows_reflection_model(wired):
    out = await memory.memory_text("sys", "user", ledger_role="memory", max_tokens=400,
                                   settings=_mem_settings())
    assert out == "haiku merged text" and wired[0]["ledger_role"] == "memory"
    await memory.memory_text("sys", "user", ledger_role="memory", max_tokens=400,
                             json_object=True,
                             settings=_mem_settings(reflection_model="gpt-4o-mini"))
    assert _FakeOpenAI.calls[0]["model"] == "gpt-4o-mini"
    assert _FakeOpenAI.calls[0]["response_format"] == {"type": "json_object"}


async def test_merge_learnings_on_memory_model(wired, monkeypatch):
    monkeypatch.setattr(memory, "get_settings", lambda: _mem_settings())
    out = await memory._merge_learnings(None, "old learning", "new learning",
                                        ledger_role="consolidation")
    assert out == "haiku merged text"
    assert wired[0]["ledger_role"] == "consolidation" and wired[0]["role"] == "reflection"
    monkeypatch.setattr(memory, "get_settings",
                        lambda: _mem_settings(reflection_model="gpt-4o-mini"))
    assert await memory._merge_learnings(None, "old", "new learning") == "openai merged text"


# ── anthropic_text ──────────────────────────────────────────────────────

async def test_anthropic_text_request_and_reply(monkeypatch):
    reply = SimpleNamespace(stop_reason="end_turn", content=[
        SimpleNamespace(type="thinking", thinking="hidden"),
        SimpleNamespace(type="text", text=' {"learnings": []} ')])
    inner = _Inner(reply)
    monkeypatch.setattr(llmclient, "anthropic_async", lambda key, **kw: inner)
    s = SimpleNamespace(anthropic_api_key="k", reflection_effort="medium")
    out = await llmclient.anthropic_text(HAIKU, system="sys", user="u", role="reflection",
                                         ledger_role="reflection", max_tokens=500,
                                         json_schema={"type": "object"}, settings=s)
    assert out == '{"learnings": []}'
    kw = inner.calls[0]
    assert kw["output_config"] == {"format": {"type": "json_schema",
                                              "schema": {"type": "object"}},
                                   "effort": "medium"}
    assert kw["max_tokens"] == llmclient.THINKING_MAX_TOKENS_FLOOR
    assert kw["system"] == "sys" and kw["messages"][0]["content"] == "u"
    inner.reply = SimpleNamespace(stop_reason="refusal", content=[])
    with pytest.raises(RuntimeError):
        await llmclient.anthropic_text(HAIKU, system="s", user="u", role="reflection",
                                       ledger_role="reflection", max_tokens=10, settings=s)


# ── Setup Test probe ────────────────────────────────────────────────────

async def test_probe_skips_openai_memory_model(monkeypatch, clean):
    from app import configservice
    inner = _Inner(SimpleNamespace(stop_reason="end_turn", content=[]))
    monkeypatch.setattr(llmclient, "anthropic_async", lambda key, **kw: inner)
    s = Settings(_env_file=None, anthropic_api_key="k", reflection_model="gpt-4o-mini")
    ok, detail = await configservice._probe_llm_roles(s)
    assert ok
    assert "gpt-4o-mini" not in {c["model"] for c in inner.calls}
    assert "reflection " not in detail and "graph " not in detail  # graph runs on OpenAI
    s2 = Settings(_env_file=None, anthropic_api_key="k", graphiti_llm="anthropic")
    ok, detail = await configservice._probe_llm_roles(s2)
    assert ok and "graph claude-sonnet-5-5" in detail and "reflection claude-haiku-5-5" in detail

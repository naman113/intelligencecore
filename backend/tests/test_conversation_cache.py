"""Quick 018: agent loops cache their growing conversation (memory audit Fix 1).

Client run 14: 19 supervisor calls re-sent the whole ReAct conversation uncached
(0.7k -> 115k uncached input per call, $5.30); only the system prompt was cached.
Pins:
- every agent loop _build makes (supervisor, data, analysis, document, drawing,
  dashboard, context) sends Anthropic's top-level automatic `cache_control`, keeps
  _cached_prompt's system breakpoint, adds no message-level marker (the API places
  and moves the breakpoint), stays within 4 breakpoints, and sends thinking blocks
  back unchanged (preserved thinking);
- one-shot callers (triage, the DB poller, the skill draft model, the worker's
  forced single-tool repair agents) send no conversation cache; the playbook agent
  and the sentinel watch compiler are agent loops and cache since Quick 035
  (test_quick035_loop_cache.py), and send none with prompt_cache_loops off;
- the OpenAI fallback is built exactly as before;
- the installed SDK puts the field in the HTTP body (httpx MockTransport).

Pure logic: no DB, no LLM, no network."""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from app import llmclient
from app.agents import graph
from app.config import Settings

APP = Path(__file__).resolve().parent.parent / "app"
AGENT_KEYS = ("supervisor", "data", "analysis", "document", "drawing", "dashboard", "context")
EPHEMERAL = {"type": "ephemeral"}


def _settings(**kw):
    vals = dict(anthropic_api_key="sk-test", openai_api_key="sk-oa",
                supervisor_model="claude-opus-5-5", data_model="claude-sonnet-5-5",
                context_model="claude-opus-5-5", triage_model="claude-haiku-5-5",
                openai_supervisor_model="gpt-4o")
    vals.update(kw)
    return Settings(_env_file=None).model_copy(update=vals)


@tool
def run_sql(sql: str) -> str:
    """Run a read-only query."""
    return "[]"


THINKING = {"type": "thinking", "thinking": "", "signature": "sig-1"}


def _loop(prompt):
    """A ReAct conversation mid-run: history, the question, a thinking + tool_use
    turn and its tool result (the shape create_react_agent sends on call 2+)."""
    return [prompt, HumanMessage("earlier question"), AIMessage("earlier reply"),
            HumanMessage("the question"),
            AIMessage(content=[THINKING, {"type": "tool_use", "id": "tu1", "name": "run_sql",
                                          "input": {"sql": "select 1"}}],
                      tool_calls=[{"id": "tu1", "name": "run_sql", "args": {"sql": "select 1"}}]),
            ToolMessage(content='[{"n": 1}]', tool_call_id="tu1")]


def _payload(model, prompt):
    bound = model.bind_tools([run_sql])
    return bound.bound._get_request_payload(_loop(prompt), **bound.kwargs)


def _blocks(payload):
    return [b for m in payload["messages"] if isinstance(m["content"], list)
            for b in m["content"] if isinstance(b, dict)]


def _breakpoints(payload) -> int:
    n = 1 if payload.get("cache_control") else 0
    n += sum(1 for t in payload.get("tools") or [] if isinstance(t, dict) and "cache_control" in t)
    if isinstance(payload.get("system"), list):
        n += sum(1 for b in payload["system"] if "cache_control" in b)
    return n + sum(1 for b in _blocks(payload) if "cache_control" in b)


def test_chat_anthropic_conversation_cache_is_one_top_level_field():
    s = _settings()
    on = llmclient.chat_anthropic("claude-opus-5-5", max_tokens=4096, api_key="k",
                                  role="supervisor", settings=s, conversation_cache=True)
    off = llmclient.chat_anthropic("claude-opus-5-5", max_tokens=4096, api_key="k",
                                   role="supervisor", settings=s)
    p_on = on._get_request_payload([HumanMessage("x")])
    p_off = off._get_request_payload([HumanMessage("x")])
    assert p_on["cache_control"] == EPHEMERAL == llmclient.CONVERSATION_CACHE
    assert "cache_control" not in p_off
    # Nothing else in the request changes, and the client places no block marker.
    assert {k: v for k, v in p_on.items() if k != "cache_control"} == p_off
    assert p_on["messages"] == [{"role": "user", "content": "x"}]


def test_conversation_cache_keeps_caller_model_kwargs():
    m = llmclient.chat_anthropic("claude-sonnet-5-5", max_tokens=4096, api_key="k",
                                 role="data", settings=_settings(), conversation_cache=True,
                                 model_kwargs={"x_probe": 1})
    p = m._get_request_payload([HumanMessage("x")])
    assert p["cache_control"] == EPHEMERAL and p["x_probe"] == 1


def test_every_agent_loop_caches_its_conversation(monkeypatch):
    monkeypatch.setattr(graph, "get_settings", lambda: _settings())
    built = {}

    def fake_cra(model, tool_list, prompt=None):
        text = prompt.content[0]["text"]
        for k in AGENT_KEYS:
            if f"OVERRIDE::{k}\n" in text + "\n":  # "data" must not match "dashboard"
                built[k] = (model, prompt)
        return SimpleNamespace()

    monkeypatch.setattr(graph, "create_react_agent", fake_cra)
    configs = {k: {**graph.DEFAULT_CONFIGS[k], "prompt": f"OVERRIDE::{k}"}
               for k in graph.DEFAULT_CONFIGS}
    graph._build(configs)
    assert set(built) == set(AGENT_KEYS)
    for key, (model, prompt) in built.items():
        p = _payload(model, prompt)
        assert p["cache_control"] == EPHEMERAL, key                  # growing conversation
        assert p["system"][-1]["cache_control"] == EPHEMERAL, key    # system breakpoint kept
        assert _breakpoints(p) == 2, key                             # 2 of the API's 4
        assert not any("cache_control" in b for b in _blocks(p)), key
        assert [b for b in _blocks(p) if b.get("type") == "thinking"] == [THINKING], key
        assert _blocks(p)[-1]["type"] == "tool_result", key          # the API's target
    assert built["data"][0].model == "claude-sonnet-5-5"
    assert built["supervisor"][0].model == "claude-opus-5-5"


def test_one_shot_models_have_no_conversation_cache(monkeypatch):
    from app import config, playbooks, sentinel, triage
    from app.connectors import db_poller
    s = _settings()
    for mod in (sentinel, playbooks, triage, graph, config):
        monkeypatch.setattr(mod, "get_settings", lambda: s)
    cfg = {"model": "auto", "temperature": 0.2, "max_tokens": 4096}
    models = {"triage": triage._model(), "db_poller": db_poller._cheap_model(),
              "graph._model": graph._model(),
              "worker repair agents": graph._make_model(cfg)}  # worker.py:440, 560
    for name, m in models.items():
        assert "cache_control" not in m._get_request_payload([HumanMessage("x")]), name
    # Quick 035: the playbook agent and the watch compiler cache; the rollback is one setting.
    for m in (sentinel._model(1200), playbooks._model()):
        assert m._get_request_payload([HumanMessage("x")])["cache_control"] == EPHEMERAL
    off = s.model_copy(update={"prompt_cache_loops": False})
    for mod in (sentinel, playbooks):
        monkeypatch.setattr(mod, "get_settings", lambda: off)
    for m in (sentinel._model(1200), playbooks._model()):
        assert "cache_control" not in m._get_request_payload([HumanMessage("x")])
    # The forced single-tool repair agents call _make_model(cfg) and never opt in.
    assert "conversation_cache" not in (APP / "worker.py").read_text(encoding="utf-8")


def test_openai_fallback_is_unchanged(monkeypatch):
    monkeypatch.setattr(graph, "get_settings", lambda: _settings(anthropic_api_key=""))
    real_chat_openai = llmclient.chat_openai
    seen = []
    monkeypatch.setattr(graph.llmclient, "chat_openai",
                        lambda name, **kw: seen.append((name, kw)) or "openai-model")
    for cfg in ({"model": "auto", "temperature": 0.2, "max_tokens": 4096},
                {"model": "claude-opus-5-5", "temperature": 0.2, "max_tokens": 4096}):
        assert graph._make_model(cfg, role="supervisor", conversation_cache=True) == "openai-model"
        assert graph._make_model(cfg, role="supervisor") == "openai-model"
    assert all(call == seen[0] for call in seen)
    assert seen[0][0] == "gpt-4o"
    assert "conversation_cache" not in seen[0][1] and "model_kwargs" not in seen[0][1]
    real = real_chat_openai("gpt-4o", api_key="sk-oa", role="supervisor",
                            max_tokens=4096, temperature=0.2)
    assert "cache_control" not in real._get_request_payload([HumanMessage("x")])
    assert graph._cached_prompt("SYS", real) == "SYS"  # no Anthropic marker either


_MESSAGE = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 40, "output_tokens": 3, "cache_read_input_tokens": 9000,
                      "cache_creation_input_tokens": 120}}


async def test_installed_sdk_sends_top_level_cache_control_in_the_body(monkeypatch):
    import anthropic
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=_MESSAGE)

    model = llmclient.chat_anthropic("claude-opus-5-5", max_tokens=4096, api_key="k",
                                     role="supervisor", settings=_settings(),
                                     conversation_cache=True)
    client = anthropic.AsyncAnthropic(
        api_key="k", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setitem(model.__dict__, "_async_client", client)
    out = await model.ainvoke([graph._cached_prompt("SYS", model), HumanMessage("x")])
    path, body = seen[0]
    assert path == "/v1/messages"                         # GA endpoint, no beta header
    assert body["cache_control"] == EPHEMERAL
    assert body["system"] == [{"type": "text", "text": "SYS", "cache_control": EPHEMERAL}]
    assert body["messages"] == [{"role": "user", "content": "x"}]
    det = out.usage_metadata["input_token_details"]
    assert det["cache_read"] == 9000 and out.usage_metadata["input_tokens"] == 40 + 9000 + 120

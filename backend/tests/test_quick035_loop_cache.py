"""Quick 035: the agent loops outside the chat graph cache like it.

Token-cost analysis 2026-10-28 section 4 and caching audit R2: the playbook agent
(nightly exploration, about $1.30 a run on Opus), the sentinel watch compiler and
the triage batch sent their prompts uncached (0% cache reads in the ledger).
Pins:
- the playbook agent and the watch compiler send Anthropic's top-level automatic
  `cache_control` (Quick 018) plus graph._cached_prompt's system breakpoint, 2 of
  the 4 slots, and nothing else in the request changes (prompt text, tools and
  their order, model, effort, max_tokens, temperature);
- the triage batch puts ONE explicit breakpoint on its stable system prompt
  (TRIAGE_SYSTEM plus the owner's triage policies) and no top-level cache; the
  per-tick payload (now, cases, expectations, events) stays in the user message
  after the breakpoint;
- prompt_cache_loops=false restores the pre-035 requests, and the cached watch
  compiler is rebuilt when the setting changes (applied live);
- the OpenAI fallback is built exactly as before;
- the worker's repair agents stay uncached: their stable prefix (tools + system)
  is far under the 512-token minimum (measured below).

Pure logic: no DB, no LLM, no network (the SDK talks to an httpx MockTransport)."""

import json
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app import configservice, llmclient, playbooks, sentinel, skillservice, triage
from app.agents import graph
from app.config import Settings

EPHEMERAL = {"type": "ephemeral"}
THINKING = {"type": "thinking", "thinking": "", "signature": "sig-1"}
EXPLORE_GRANTS = ["search_business_context", "run_sql", "run_sql_to_csv", "run_python",
                  "get_kpi_history", "use_skill", "record_insight", "propose_kpi",
                  "get_model", "query_measure"]


def _settings(**kw):
    vals = dict(anthropic_api_key="sk-test", openai_api_key="sk-oa",
                supervisor_model="claude-opus-5-5", triage_model="claude-haiku-5-5",
                openai_supervisor_model="gpt-4o", owner_number="")
    vals.update(kw)
    return Settings(_env_file=None).model_copy(update=vals)


def _loop(prompt, tool_name):
    """A ReAct conversation mid-run (the shape create_react_agent sends on call 2+)."""
    system = prompt if isinstance(prompt, SystemMessage) else SystemMessage(content=prompt)
    return [system, HumanMessage("CTX"),
            AIMessage(content=[THINKING, {"type": "tool_use", "id": "tu1", "name": tool_name,
                                          "input": {"sql": "select 1"}}],
                      tool_calls=[{"id": "tu1", "name": tool_name, "args": {"sql": "select 1"}}]),
            ToolMessage(content='[{"n": 1}]', tool_call_id="tu1")]


def _payload(model, prompt, tools):
    bound = model.bind_tools(tools)
    return bound.bound._get_request_payload(_loop(prompt, tools[0].name), **bound.kwargs)


def _built_payload(built):
    model, tools, prompt = built  # as create_react_agent received them
    return _payload(model, prompt, tools)


def _blocks(payload):
    return [b for m in payload["messages"] if isinstance(m["content"], list)
            for b in m["content"] if isinstance(b, dict)]


def _breakpoints(payload) -> int:
    n = 1 if payload.get("cache_control") else 0
    n += sum(1 for t in payload.get("tools") or [] if isinstance(t, dict) and "cache_control" in t)
    if isinstance(payload.get("system"), list):
        n += sum(1 for b in payload["system"] if "cache_control" in b)
    return n + sum(1 for b in _blocks(payload) if "cache_control" in b)


def _without_cache(payload) -> dict:
    return {k: v for k, v in payload.items() if k not in ("cache_control", "system")}


# ── setting ─────────────────────────────────────────────────────────────────

def test_prompt_cache_loops_setting_defaults_on_and_is_registered():
    assert Settings(_env_file=None).prompt_cache_loops is True
    meta = configservice.REGISTRY["prompt_cache_loops"]
    assert meta["group"] == "tuning" and not meta["secret"] and not meta["required"]
    assert "live" in meta["label"]
    configservice._validate_value("prompt_cache_loops", "false")
    assert llmclient.loop_cache_on(_settings()) is True
    assert llmclient.loop_cache_on(_settings(prompt_cache_loops=False)) is False
    # Older settings stand-ins (tests, pre-035 objects) mean the default: on.
    assert llmclient.loop_cache_on(SimpleNamespace()) is True


# ── playbook agent (nightly exploration, reminders, digests) ──────────────────

async def _run_playbook(monkeypatch, s, grants=EXPLORE_GRANTS):
    built = []

    class _Pool:
        async def fetchrow(self, sql, *a):
            return {"id": 5, "name": "explore_data", "status": "active", "goal": "g",
                    "escalation": None, "tool_grants": grants, "autonomy_tier": "draft"}

        async def execute(self, sql, *a):
            pass

    async def gp():
        return _Pool()

    async def ctx(task, pb):
        return "CTX"

    class _Agent:
        async def ainvoke(self, payload, config=None):
            return {"messages": [AIMessage("done")]}

    def fake_cra(model, tools, prompt=None):
        built.append((model, tools, prompt))
        return _Agent()

    monkeypatch.setattr(playbooks, "get_settings", lambda: s)
    monkeypatch.setattr(playbooks, "get_pool", gp)
    monkeypatch.setattr(playbooks, "_assemble_context", ctx)
    monkeypatch.setattr(playbooks, "create_react_agent", fake_cra)
    monkeypatch.setattr(playbooks.graph, "_callbacks", lambda: {})
    await playbooks.run_playbook_task(
        {"id": 77, "playbook_id": 5, "case_id": None, "expectation_id": None})
    assert len(built) == 1
    return built[0]


async def test_playbook_agent_caches_like_the_chat_graph(monkeypatch):
    model, tools, prompt = await _run_playbook(monkeypatch, _settings())
    p = _payload(model, prompt, tools)
    assert p["cache_control"] == EPHEMERAL                         # growing conversation
    assert p["system"] == [{"type": "text", "text": playbooks.PLAYBOOK_SYSTEM,
                            "cache_control": EPHEMERAL}]           # tools + system breakpoint
    assert _breakpoints(p) == 2
    assert not any("cache_control" in b for b in _blocks(p))       # the API places the tail
    assert [b for b in _blocks(p) if b.get("type") == "thinking"] == [THINKING]
    assert [t["name"] for t in p["tools"]] == EXPLORE_GRANTS       # grant order, stable
    assert p["model"] == "claude-opus-5-5"


async def test_playbook_rollback_sends_the_pre_035_request(monkeypatch):
    on = await _run_playbook(monkeypatch, _settings())
    off = await _run_playbook(monkeypatch, _settings(prompt_cache_loops=False))
    assert off[2] == playbooks.PLAYBOOK_SYSTEM                     # plain string prompt
    p_on, p_off = _built_payload(on), _built_payload(off)
    assert "cache_control" not in p_off and _breakpoints(p_off) == 0
    assert p_off["system"] == playbooks.PLAYBOOK_SYSTEM
    # Same model, effort, max_tokens, temperature, tools and messages either way.
    assert _without_cache(p_on) == _without_cache(p_off)


async def test_playbook_openai_fallback_is_unchanged(monkeypatch):
    model, tools, prompt = await _run_playbook(monkeypatch, _settings(anthropic_api_key=""))
    assert type(model).__name__ == "ChatOpenAI"
    assert prompt == playbooks.PLAYBOOK_SYSTEM
    assert "cache_control" not in model._get_request_payload([HumanMessage("x")])


# ── sentinel watch compiler ─────────────────────────────────────────────────

@pytest.fixture
def compiler(monkeypatch):
    built = []
    monkeypatch.setattr(sentinel, "create_react_agent",
                        lambda m, t, prompt=None: built.append((m, t, prompt)) or object())
    sentinel.reset_compiler()
    yield built
    sentinel.reset_compiler()


def test_watch_compiler_caches_like_the_chat_graph(monkeypatch, compiler):
    monkeypatch.setattr(sentinel, "get_settings", lambda: _settings())
    agent = sentinel._compiler()
    assert sentinel._compiler() is agent and len(compiler) == 1   # still built once
    model, tools, prompt = compiler[0]
    p = _payload(model, prompt, tools)
    assert p["cache_control"] == EPHEMERAL
    assert p["system"] == [{"type": "text", "text": sentinel.COMPILE_PROMPT,
                            "cache_control": EPHEMERAL}]
    assert _breakpoints(p) == 2
    assert not any("cache_control" in b for b in _blocks(p))
    assert [t["name"] for t in p["tools"]] == ["search_business_context", "run_sql"]
    assert p["model"] == "claude-opus-5-5"


def test_watch_compiler_follows_the_setting_live(monkeypatch, compiler):
    s = {"v": _settings()}
    monkeypatch.setattr(sentinel, "get_settings", lambda: s["v"])
    first = sentinel._compiler()
    s["v"] = _settings(prompt_cache_loops=False)
    second = sentinel._compiler()
    assert second is not first and len(compiler) == 2              # rebuilt on the change
    assert sentinel._compiler() is second and len(compiler) == 2
    p_on, p_off = _built_payload(compiler[0]), _built_payload(compiler[1])
    assert compiler[1][2] == sentinel.COMPILE_PROMPT
    assert "cache_control" not in p_off and p_off["system"] == sentinel.COMPILE_PROMPT
    assert _without_cache(p_on) == _without_cache(p_off)
    s["v"] = _settings()
    assert sentinel._compiler() is not second and len(compiler) == 3


def test_watch_compiler_openai_fallback_is_unchanged(monkeypatch, compiler):
    monkeypatch.setattr(sentinel, "get_settings", lambda: _settings(anthropic_api_key=""))
    sentinel._compiler()
    model, _, prompt = compiler[0]
    assert type(model).__name__ == "ChatOpenAI" and prompt == sentinel.COMPILE_PROMPT
    assert "cache_control" not in model._get_request_payload([HumanMessage("x")])


# ── triage batch: one breakpoint on the stable system prompt ────────────────

POLICY = "OWNER TRIAGE POLICIES - test\n[p1] a demand for a discussion opens a case"


def _message(text: str) -> dict:
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 900, "output_tokens": 20,
                      "cache_read_input_tokens": 2600, "cache_creation_input_tokens": 0}}


def _wire_triage(monkeypatch, s, policy=POLICY):
    """A real ChatAnthropic from triage._model() whose SDK client posts to a mock."""
    import anthropic
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_message('{"verdicts": [{"event_id": 41, '
                                                 '"relevant": false, "case": "none", '
                                                 '"reason": "chit-chat"}]}'))

    monkeypatch.setattr(triage, "get_settings", lambda: s)
    model = triage._model()
    client = anthropic.AsyncAnthropic(
        api_key="k", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    if type(model).__name__ == "ChatAnthropic":
        monkeypatch.setitem(model.__dict__, "_async_client", client)
    monkeypatch.setattr(triage, "_model", lambda: model)

    event = {"id": 41, "source": "whatsapp", "content": "ok thanks", "actor_id": None,
             "actor_raw": "15550100009", "actor_name": "Asha", "occurred_at": None,
             "thread_ref": "15550100009", "meta": {}, "chat_subject": None}

    class Conn:
        def transaction(self):
            class Tx:
                async def __aenter__(self_t):
                    return None

                async def __aexit__(self_t, *e):
                    return False
            return Tx()

        async def fetch(self, sql, *args):
            if "triage_status = 'pending'" in sql:
                return [dict(event)]
            return []

        async def fetchval(self, sql, *args):
            return None

        async def execute(self, sql, *args):
            return "UPDATE 1"

    class Pool:
        def acquire(self):
            class Ctx:
                async def __aenter__(self_c):
                    return Conn()

                async def __aexit__(self_c, *e):
                    return False
            return Ctx()

    async def gp():
        return Pool()

    async def policies():
        return policy

    async def noop():
        pass

    monkeypatch.setattr(triage, "get_pool", gp)
    monkeypatch.setattr(skillservice, "triage_policy_block", policies)
    monkeypatch.setattr(triage, "_refresh_summaries", noop)
    monkeypatch.setattr(triage, "_complete_finished_cases", noop)
    return bodies


async def test_triage_batch_caches_only_the_stable_system_prompt(monkeypatch):
    bodies = _wire_triage(monkeypatch, _settings())
    assert await triage._triage_events(SimpleNamespace(
        go_live=_anoop, ok=lambda *a, **k: None, fail=lambda *a, **k: None)) == 1
    body, = bodies
    stable = triage._compose_system(POLICY)
    assert body["system"] == [{"type": "text", "text": stable, "cache_control": EPHEMERAL}]
    assert "cache_control" not in body                 # one-shot: no conversation cache
    assert _breakpoints(body) == 1
    # Everything per tick is after the breakpoint, in the one user message.
    msg, = body["messages"]
    assert msg["role"] == "user" and isinstance(msg["content"], str)
    tick = json.loads(msg["content"])
    assert set(tick) >= {"now", "open_cases", "open_expectations", "events"}
    assert tick["events"][0]["content"] == "ok thanks"
    assert "ok thanks" not in stable and tick["now"] not in stable
    assert body["model"] == "claude-haiku-5-5"


async def test_triage_rollback_and_no_policy_block(monkeypatch):
    bodies = _wire_triage(monkeypatch, _settings(prompt_cache_loops=False), policy="")
    job = SimpleNamespace(go_live=_anoop, ok=lambda *a, **k: None, fail=lambda *a, **k: None)
    assert await triage._triage_events(job) == 1
    off, = bodies
    assert off["system"] == triage.TRIAGE_SYSTEM       # plain string, byte-identical
    assert _breakpoints(off) == 0
    bodies = _wire_triage(monkeypatch, _settings(), policy="")
    assert await triage._triage_events(job) == 1
    on, = bodies
    assert on["system"] == [{"type": "text", "text": triage.TRIAGE_SYSTEM,
                             "cache_control": EPHEMERAL}]
    drop_now = [{**json.loads(b["messages"][0]["content"]), "now": None} for b in (on, off)]
    assert drop_now[0] == drop_now[1]
    keep = lambda b: {k: v for k, v in b.items() if k not in ("system", "messages")}  # noqa: E731
    assert keep(on) == keep(off)


async def test_triage_openai_fallback_is_unchanged(monkeypatch):
    seen = []

    class Model:  # what triage._model() returns on an install without an Anthropic key
        async def ainvoke(self, msgs, config=None):
            seen.append(msgs)
            return SimpleNamespace(content='{"verdicts": []}')

    _wire_triage(monkeypatch, _settings(anthropic_api_key=""))
    monkeypatch.setattr(triage, "_model", lambda: Model())
    job = SimpleNamespace(go_live=_anoop, ok=lambda *a, **k: None, fail=lambda *a, **k: None)
    assert await triage._triage_events(job) == 1
    assert seen[0][0] == ("system", triage._compose_system(POLICY))
    assert seen[0][1][0] == "user"


async def _anoop(*a, **k):
    return None


# ── what stays uncached, and why ─────────────────────────────────────────────

def _o200k(text: str) -> int:
    import tiktoken
    return len(tiktoken.get_encoding("o200k_base").encode(text))


def _prefix_tokens(tools, system: str) -> int:
    from langchain_anthropic.chat_models import convert_to_anthropic_tool
    return _o200k(json.dumps([convert_to_anthropic_tool(t) for t in tools])) + _o200k(system)




def test_cached_loops_clear_the_512_token_minimum():
    """The 512-token minimum (Opus, Sonnet and Haiku 5.5) applies to the prefix up to
    the breakpoint. o200k is a proxy that undercounts Claude 5.5's tokenizer, so these
    clear it with margin (measured 2,448 / 656 / 2,345)."""
    reg = playbooks._playbook_registry()
    assert _prefix_tokens([reg[t] for t in EXPLORE_GRANTS], playbooks.PLAYBOOK_SYSTEM) > 2000
    assert _prefix_tokens([graph.search_business_context, graph.run_sql],
                          sentinel.COMPILE_PROMPT) > 600
    assert _o200k(triage.TRIAGE_SYSTEM) > 2000


async def test_worker_repair_agents_stay_uncached(monkeypatch):
    """The forced single-tool repair agents make 2 model calls a run (tool call, then
    the closing turn; recursion_limit 4), but their stable prefix is one tool plus a
    one-paragraph prompt (measured 137 and 216 o200k tokens), far under 512: a
    marker would be ignored, and the conversation cache would only add a write."""
    import langgraph.prebuilt as prebuilt

    from app import worker
    built = []

    class _Agent:
        async def ainvoke(self, *a, **k):
            return {"messages": [AIMessage("ok")]}

    class _Pool:
        async def fetchval(self, *a):
            return None

    async def gp():
        return _Pool()

    async def configs():
        return {"supervisor": {"model": "auto", "temperature": 0.2, "max_tokens": 4096}}

    monkeypatch.setattr(prebuilt, "create_react_agent",
                        lambda m, t, prompt=None: built.append((m, t, prompt)) or _Agent())
    monkeypatch.setattr(graph, "get_settings", lambda: _settings())
    monkeypatch.setattr(graph, "effective_configs", configs)
    monkeypatch.setattr(graph, "_callbacks", lambda: {})
    monkeypatch.setattr(worker, "get_pool", gp)
    await worker._force_one_shot_watch(1, "tell me once when the tank is empty", "ok")
    await worker._force_send(1, "tell Asha the report is ready", "The report is ready.")
    assert len(built) == 2
    for model, tools, prompt in built:
        assert isinstance(prompt, str) and len(tools) == 1
        assert "cache_control" not in model._get_request_payload([HumanMessage("x")])
        assert _prefix_tokens(tools, prompt) < 300

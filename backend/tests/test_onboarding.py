"""Onboarding interview — the pure parts, plus the conversation loop on fakes.

The interview's value depends on two things being true: that it cannot declare
itself finished by writing convincing prose, and that its own guesses stay
distinguishable from what the user actually said. Both are structural, so both
are testable without a model.
"""

import json
import sys
import types
from types import SimpleNamespace

from app import onboarding


def test_completion_requires_a_tool_call_not_prose():
    # The exit is emitting a structured profile. If an interviewer could finish
    # by saying "I think I have everything", nothing downstream could tell a
    # thorough interview from an abandoned one.
    for kind in ("business", "chat"):
        tool = onboarding._tool(kind)
        assert tool["input_schema"]["required"]


def test_business_profile_separates_told_from_inferred():
    props = onboarding._BUSINESS_TOOL["input_schema"]["properties"]
    assert "facts_told" in props and "inferences" in props
    # Only what the user actually said is mandatory; guesses are optional and
    # must never be able to masquerade as statements of fact.
    assert "facts_told" in onboarding._BUSINESS_TOOL["input_schema"]["required"]
    assert "inferences" not in onboarding._BUSINESS_TOOL["input_schema"]["required"]


def test_interviewer_is_told_not_to_lead_or_assume():
    brief = onboarding._INTERVIEWER.lower()
    for rule in ("one question at a time", "never suggest the answer",
                 "never fill a gap"):
        assert rule in brief


def test_chat_brief_names_the_group():
    brief = onboarding._brief("chat", "Mumbai Accounts")
    assert "Mumbai Accounts" in brief
    # A group interview must not inherit the business interview's agenda.
    assert "how it actually makes money" not in brief


def test_business_brief_used_when_kind_is_not_chat():
    assert "how it actually makes money" in onboarding._brief("business", None)


def test_api_history_never_opens_on_an_assistant_turn():
    # The interviewer speaks first, so a naive tail-slice of a long interview
    # starts on an assistant message — which the API rejects.
    transcript = [{"role": "assistant", "content": "Hi, tell me about the business."},
                  {"role": "user", "content": "We ship cement."},
                  {"role": "assistant", "content": "Who buys it?"}]
    assert onboarding._for_api(transcript)[0]["role"] == "user"


def test_api_history_survives_an_empty_transcript():
    assert onboarding._for_api([])[0]["role"] == "user"


def test_api_history_is_windowed():
    long_run = [{"role": "user", "content": str(i)} for i in range(400)]
    assert len(onboarding._for_api(long_run)) <= onboarding.TRANSCRIPT_LIMIT


def test_transcript_turns_are_dicts_not_characters():
    # asyncpg returns jsonb as a string. Decoding must precede list(), because
    # list("[]") is ['[', ']'] — which then reads turn["role"] on a str and
    # raises TypeError deep inside the conversation loop. This shipped once.
    import json as _json

    raw = _json.dumps([{"role": "user", "content": "hi"},
                       {"role": "assistant", "content": "What does the team do?"}])
    decoded = _json.loads(raw)
    turns = onboarding._for_api(list(decoded))
    assert all(isinstance(t, dict) for t in turns)
    assert onboarding._last_assistant_text(list(decoded)) == "What does the team do?"


def test_last_assistant_text_resumes_mid_interview():
    transcript = [{"role": "user", "content": "hi"},
                  {"role": "assistant", "content": "What does the team do?"},
                  {"role": "user", "content": "logistics"}]
    assert onboarding._last_assistant_text(transcript) == "What does the team do?"


# ── the conversation loop on fakes: no DB, no model ────────────────────────

class FakePool:
    """Understands exactly the queries onboarding issues, returns rows the way
    asyncpg does — with jsonb columns as STRINGS."""

    def __init__(self, session_row=None):
        self.session_row = session_row
        self.inserts = []
        self.saves = []          # (session_id, transcript_json, profile_json)

    async def fetchrow(self, sql, *args):
        if "INSERT INTO onboarding_sessions" in sql:
            self.inserts.append(args)
            return {"id": 99, "kind": args[0], "subject_ref": args[1],
                    "status": "active", "transcript": "[]", "profile": None}
        if "FROM onboarding_sessions" in sql:
            return self.session_row
        return None

    async def execute(self, sql, *args):
        if "UPDATE onboarding_sessions" in sql and "SET transcript" in sql:
            self.saves.append(args)


def _wire(monkeypatch, pool, blocks, stop_reason="end_turn", script=None):
    """Fake pool + settings + anthropic; returns the list of messages.create
    kwargs so tests can assert on exactly what the model was shown."""
    calls = []

    async def fake_get_pool():
        return pool

    monkeypatch.setattr(onboarding, "get_pool", fake_get_pool)
    monkeypatch.setattr(
        onboarding, "get_settings",
        lambda: SimpleNamespace(anthropic_api_key="k", onboarding_model="m"))

    class Messages:
        async def create(self, **kw):
            calls.append(kw)
            if script:                       # Phase 12: per-call result or exception
                r = script.pop(0)
                if isinstance(r, Exception):
                    raise r
                return r
            return SimpleNamespace(content=blocks, stop_reason=stop_reason)

    class AsyncAnthropic:
        def __init__(self, api_key=None):
            self.messages = Messages()

    mod = types.ModuleType("anthropic")
    mod.AsyncAnthropic = AsyncAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return calls


def _complete_row(turns):
    return {"id": 4, "kind": "business", "subject_ref": None,
            "status": "complete", "transcript": json.dumps(turns),
            "profile": json.dumps({"summary": "s", "facts_told": ["f"],
                                   "persona": "p"})}


async def test_fresh_session_gets_a_decoded_transcript(monkeypatch):
    # _start hands back RETURNING * the same way a SELECT does: jsonb as a raw
    # string. Undecoded, list("[]") is ['[', ']'] and the first message of a
    # brand-new interview crashes on '['["role"]. This shipped once too.
    pool = FakePool(session_row=None)
    calls = _wire(monkeypatch, pool, [SimpleNamespace(type="text", text="Hi — what do you do?")])

    out = await onboarding.send("business", "we ship cement")
    assert "error" not in out
    assert calls[0]["messages"] == [{"role": "user", "content": "we ship cement"}]


async def test_send_continues_a_completed_session(monkeypatch):
    # Adding a fact a week after finishing must extend the same interview, not
    # silently open a blank one that has forgotten everything.
    turns = [{"role": "assistant", "content": "What do you do?"},
             {"role": "user", "content": "cement"}]
    pool = FakePool(session_row=_complete_row(turns))
    calls = _wire(monkeypatch, pool, [SimpleNamespace(type="text", text="Noted — anything else?")])

    out = await onboarding.send("business", "we also do logistics now")
    assert pool.inserts == []                    # no new session
    assert calls[0]["messages"][-1]["content"] == "we also do logistics now"
    # prior turns still in context (the leading assistant turn is trimmed —
    # the API requires the history to open on a user message)
    assert {"role": "user", "content": "cement"} in calls[0]["messages"]
    assert out["done"] is False                  # talking again, not finished


async def test_reopening_a_completed_session_replays_done_state(monkeypatch):
    turns = [{"role": "assistant", "content": "What do you do?"},
             {"role": "user", "content": "cement"},
             {"role": "assistant", "content": "Got it."}]
    pool = FakePool(session_row=_complete_row(turns))
    calls = _wire(monkeypatch, pool, [])

    out = await onboarding.send("business", None)
    assert calls == []                           # no model call to replay state
    assert out["done"] is True
    assert out["profile"]["summary"] == "s"


_PROFILE = {"summary": "cement co", "facts_told": ["ships cement"], "persona": "x"}


def _finalize_setup(monkeypatch, **kw):
    turns = [{"role": "assistant", "content": "What do you do?"},
             {"role": "user", "content": "cement"}]
    row = _complete_row(turns) | {"status": "active", "profile": None}
    pool = FakePool(session_row=row)
    calls = _wire(monkeypatch, pool, [], **kw)
    applied_with = []

    async def fake_apply(kind, subject_ref, p):
        applied_with.append(p)
        return {"memory": "ok"}

    monkeypatch.setattr(onboarding, "_apply", fake_apply)
    return pool, calls, applied_with


def test_strict_schema_closes_every_object_without_mutating():
    src = {"type": "object", "properties": {
        "a": {"type": "array", "items": {"type": "object", "properties": {"b": {"type": "string"}}}},
        "c": {"type": "object", "properties": {}}}}
    import copy
    before = copy.deepcopy(src)
    out = onboarding._strict_schema(src)
    assert src == before
    assert out["additionalProperties"] is False
    assert out["properties"]["a"]["items"]["additionalProperties"] is False
    assert out["properties"]["c"]["additionalProperties"] is False


async def test_finalize_uses_structured_output(monkeypatch):
    resp = SimpleNamespace(stop_reason="end_turn", content=[
        SimpleNamespace(type="thinking", thinking="t"),
        SimpleNamespace(type="text", text=json.dumps(_PROFILE))])
    pool, calls, applied_with = _finalize_setup(monkeypatch, script=[resp])
    out = await onboarding.finalize("business")
    kw = calls[0]
    assert kw["output_config"] == {"format": {"type": "json_schema", "schema":
        onboarding._strict_schema(onboarding._tool("business")["input_schema"])}}
    assert "tool_choice" not in kw and "thinking" not in kw and "temperature" not in kw
    assert kw["messages"][-1]["role"] == "user"
    assert out["done"] is True and out["profile"] == _PROFILE
    assert applied_with == [_PROFILE]
    assert pool.saves and pool.saves[0][2] is not None


async def test_finalize_falls_back_to_strict_tool(monkeypatch):
    resp = SimpleNamespace(stop_reason="tool_use", content=[
        SimpleNamespace(type="tool_use", input=_PROFILE)])
    pool, calls, applied_with = _finalize_setup(
        monkeypatch, script=[Exception("output_config is not supported"), resp])
    out = await onboarding.finalize("business")
    assert len(calls) == 2
    kw = calls[1]
    assert kw["tools"][0]["strict"] is True
    assert kw["tool_choice"] == {"type": "auto"} and "thinking" not in kw
    assert "Call save_business_profile now" in kw["messages"][-1]["content"]
    assert out["profile"] == _PROFILE and applied_with == [_PROFILE]


async def test_finalize_refusal_and_bad_json_save_nothing(monkeypatch):
    refusal = SimpleNamespace(stop_reason="refusal", content=[])
    pool, calls, applied_with = _finalize_setup(monkeypatch, script=[refusal])
    out = await onboarding.finalize("business")
    assert out == {"error": "the model declined to save the profile"}
    assert not pool.saves and not applied_with
    bad = SimpleNamespace(stop_reason="end_turn",
                          content=[SimpleNamespace(type="text", text="not json")])
    pool, calls, applied_with = _finalize_setup(monkeypatch, script=[bad])
    out = await onboarding.finalize("business")
    assert "error" in out and not pool.saves and not applied_with


async def test_send_refusal_stores_no_profile(monkeypatch):
    from app import llmclient
    pool = FakePool(session_row=None)
    _wire(monkeypatch, pool, [], stop_reason="refusal")
    out = await onboarding.send("business", "hello")
    assert out["reply"] == llmclient.REFUSAL_REPLY
    assert out["done"] is False and "profile" not in out


async def test_finalize_before_any_conversation_is_an_error(monkeypatch):
    pool = FakePool(session_row=None)
    calls = _wire(monkeypatch, pool, [])
    out = await onboarding.finalize("business")
    assert "error" in out and calls == []


# ── Phase 12 (CTX-09c): persona kept alongside the default supervisor prompt ──

import pytest as _pytest_p12
from pathlib import Path as _Path_p12

_SCHEMA_P12 = (_Path_p12(__file__).resolve().parents[1] / "app" / "schema.sql").read_text(
    encoding="utf-8")


@_pytest_p12.mark.asyncio
async def test_apply_business_does_not_touch_agent_configs(monkeypatch):
    from app.agents import graph
    sql = []

    class P:
        async def execute(self, q, *a):
            sql.append(q)

        async def fetch(self, q, *a):
            sql.append(q)
            return []

        async def fetchrow(self, q, *a):
            sql.append(q)

    async def gp():
        return P()

    resets = []
    monkeypatch.setattr(onboarding, "get_pool", gp)
    monkeypatch.setattr(graph, "reset_agents", lambda: resets.append(1))
    from app import memoryservice
    monkeypatch.setattr(memoryservice.memory_graph, "add_episode",
                        lambda *a, **k: _noop())
    out = await onboarding._apply_business(
        {"summary": "s", "facts_told": ["f"], "persona": "You are Acme's assistant"})
    assert not any("agent_configs" in q for q in sql)
    assert resets
    assert out["persona"] == "kept alongside the default supervisor instructions"


async def _noop():
    return None


@_pytest_p12.mark.asyncio
async def test_effective_configs_carries_persona(monkeypatch):
    from app.agents import graph

    async def ov():
        return {}

    class P:
        async def fetchval(self, q, *a):
            return "  P  "

    async def gp():
        return P()

    import app.db as db
    monkeypatch.setattr(graph, "get_config_overrides", ov)
    monkeypatch.setattr(db, "get_pool", gp)
    cfgs = await graph.effective_configs()
    assert cfgs["supervisor"]["persona"] == "P"
    assert all("persona" not in c for k, c in cfgs.items() if k != "supervisor")

    class Bad:
        async def fetchval(self, q, *a):
            raise RuntimeError("down")

    async def gpb():
        return Bad()

    monkeypatch.setattr(db, "get_pool", gpb)
    assert (await graph.effective_configs())["supervisor"]["persona"] == ""


def test_supervisor_prompt_appends_persona():
    from app.agents import graph
    base = {"prompt": graph.DEFAULT_PROMPTS["supervisor"], "persona": ""}
    assert graph._supervisor_prompt(base) == graph.DEFAULT_PROMPTS["supervisor"]
    out = graph._supervisor_prompt({**base, "persona": "P"})
    assert out.startswith(graph.DEFAULT_PROMPTS["supervisor"])
    assert out.endswith("About this business (from the owner's onboarding interview):\nP")


def test_persona_migration_block_in_schema():
    assert "-- Phase 12 (CTX-09c)" in _SCHEMA_P12
    assert "config - 'prompt'" in _SCHEMA_P12
    assert "-- End Phase 12 (CTX-09c)" in _SCHEMA_P12


def test_wr03_data_repair_block_in_schema():
    a = _SCHEMA_P12.index("-- Phase 12 (WR-03 data repair)")
    blk = _SCHEMA_P12[a:_SCHEMA_P12.index("-- End Phase 12 (WR-03 data repair)")]
    assert "bc.status = 'approved'" in blk
    assert "ct.status = 'hidden'" in blk
    assert "SET status = 'hidden'" in blk

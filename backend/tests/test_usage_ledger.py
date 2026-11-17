"""Quick 005: usage ledger, attribution, budget, traces, Langfuse gate.

Pure logic: fakes and monkeypatch only, no DB, no LLM, no network.
"""
import asyncio
import json
import re
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app import usage

APP = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture(autouse=True)
def _clean():
    usage.clear_pending()
    usage._run_marks.clear()
    usage._spent_cache.update(at=0.0, day=None, spent=0.0)
    usage.set_attribution()
    yield
    usage.clear_pending()


# ── prices ────────────────────────────────────────────────────────────────

def test_price_table_date_and_current_models_priced():
    assert usage.PRICE_TABLE_DATE == "2026-10-25"
    from app import llmclient
    for m in llmclient.CURRENT_MODELS + llmclient.LEGACY_MODELS:
        assert usage.price_for(m["value"]) is not None, m["value"]
    for m in ("gpt-4o-mini", "gpt-4o", "text-embedding-3-small", "gpt-5.5", "gpt-4.1-nano"):
        assert usage.price_for(m) is not None, m


def test_cost_opus_5_5_with_cache():
    # 1M of each class: 4 + 20 + 0.20 + 5
    c = usage.cost_usd("claude-opus-5-5", input=1_000_000, output=1_000_000,
                       cache_read=1_000_000, cache_write=1_000_000)
    assert c == pytest.approx(29.20)


def test_cost_sonnet_5_5_cache_read_is_0_05x():
    assert usage.cost_usd("claude-sonnet-5-5", cache_read=1_000_000) == pytest.approx(0.10)


def test_cost_one_hour_cache_writes_and_web_search():
    c = usage.cost_usd("claude-sonnet-5-5", cache_write=1_000_000, cache_write_1h=400_000,
                       web_searches=3)
    assert c == pytest.approx(0.6 * 2.5 + 0.4 * 4 + 0.03)


def test_haiku_5_5_long_prompt_tier():
    short = usage.cost_usd("claude-haiku-5-5", input=100_000, output=1000)
    long_ = usage.cost_usd("claude-haiku-5-5", input=90_000, cache_read=20_000, output=1000)
    assert short == pytest.approx(100_000 * 0.10 / 1e6 + 1000 * 0.50 / 1e6)
    assert long_ == pytest.approx((90_000 * 0.50 + 20_000 * 0.05 + 1000 * 2.50) / 1e6)


@pytest.mark.parametrize("name,key", [
    ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
    ("anthropic.claude-opus-5-5", "claude-opus-5-5"),
    ("claude-opus-4-5@20251101", "claude-opus-4-5"),
    ("gpt-4o-mini-2024-07-18", "gpt-4o-mini"),
    ("Claude-Sonnet-5-5", "claude-sonnet-5-5"),
])
def test_normalize_model(name, key):
    assert usage.normalize_model(name) == key
    assert usage.price_for(name) is not None


def test_unknown_model_is_unpriced():
    assert usage.cost_usd("claude-opus-9-9", input=10) is None


def test_price_overrides_from_setting(monkeypatch):
    s = SimpleNamespace(llm_prices_json=json.dumps(
        {"my-model": {"input": 1, "output": 2},
         "claude-opus-5-5": {"input": 3, "output": 30, "cache_read": 0.3, "cache_write": 3.75}}))
    monkeypatch.setattr("app.config.get_settings", lambda: s)
    assert usage.cost_usd("my-model", input=1_000_000, output=1_000_000) == pytest.approx(3)
    assert usage.cost_usd("claude-opus-5-5", input=1_000_000) == pytest.approx(3)
    s.llm_prices_json = "{not json"
    assert usage.cost_usd("my-model", input=1) is None   # bad JSON -> built-in table only


def test_prices_json_setting_is_validated():
    from app import configservice
    configservice._validate_value("llm_prices_json", '{"m": {"input": 1}}')
    for bad in ("[1]", "{x", '{"m": 3}'):
        with pytest.raises(ValueError):
            configservice._validate_value("llm_prices_json", bad)
    with pytest.raises(ValueError):
        configservice._validate_value("daily_llm_budget_usd", "-1")
    configservice._validate_value("daily_llm_budget_usd", "12.5")


def test_settings_registered():
    from app import configservice
    from app.config import Settings
    for k in ("install_name", "daily_llm_budget_usd", "llm_usage_retention_days", "llm_prices_json"):
        assert k in Settings.model_fields and configservice.REGISTRY[k]["group"] == "tuning"
    s = Settings()
    assert s.daily_llm_budget_usd == 0 and s.llm_usage_retention_days == 180


# ── attribution and recording ─────────────────────────────────────────────

def test_record_uses_attribution_and_install(monkeypatch):
    monkeypatch.setenv("IC_STACK", "p18")
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(install_name=""))
    with usage.attribute(feature="watch_compile", agent="watch_compiler", task_id="watch:3",
                         thread_id="ui:owner", trace_id="watch_compile:abc"):
        usage.record(model="claude-opus-5-5", role="supervisor", input=10, output=20)
    (r,) = usage.pending_rows()
    assert (r.feature, r.agent, r.task_id, r.thread_id, r.trace_id, r.install) == (
        "watch_compile", "watch_compiler", "watch:3", "ui:owner", "watch_compile:abc", "p18")
    assert r.cost_usd == pytest.approx((10 * 4 + 20 * 20) / 1e6)


def test_install_name_setting_wins(monkeypatch):
    monkeypatch.setenv("IC_STACK", "p18")
    monkeypatch.setattr("app.config.get_settings",
                        lambda: SimpleNamespace(install_name="client-fleet"))
    assert usage.install_label() == "client-fleet"


def test_run_and_thread_fall_back_to_graph_context():
    from app.agents import graph
    t1, t2 = graph.ctx_run_id.set(42), graph.ctx_phone.set("ui:owner:q1")
    try:
        usage.record(model="claude-haiku-5-5", input=1)
    finally:
        graph.ctx_run_id.reset(t1)
        graph.ctx_phone.reset(t2)
    (r,) = usage.pending_rows()
    assert (r.run_id, r.thread_id, r.trace_id, r.feature) == (42, "ui:owner:q1", "run:42", "other")


def test_role_vs_context_agent():
    with usage.attribute(agent="data"):
        usage.record(model="claude-opus-5-5", role="supervisor")           # LangChain model
        usage.record(model="claude-sonnet-5-5", role="research", prefer_role=True)  # direct SDK
    a, b = usage.pending_rows()
    assert a.agent == "data" and b.agent == "research"


def test_unknown_feature_becomes_other_and_queue_is_bounded(monkeypatch):
    monkeypatch.setattr(usage, "MAX_PENDING", 3)
    for _ in range(5):
        usage.record(model="x", feature="nonsense")
    assert len(usage.pending_rows()) == 3
    assert all(r.feature == "other" for r in usage.pending_rows())


def test_record_never_raises(monkeypatch):
    monkeypatch.setattr(usage, "current", lambda: 1 / 0)
    assert usage.record(model="x") is None


# ── LangChain capture ─────────────────────────────────────────────────────

class _Scripted(BaseChatModel):
    """Replays AIMessages with Anthropic-shaped usage_metadata (cache tokens
    folded into input_tokens, as langchain-anthropic reports them)."""
    script: list
    usage_meta: dict

    @property
    def _llm_type(self):
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        msg = self.script.pop(0) if self.script else AIMessage(content="ok")
        msg.usage_metadata = dict(self.usage_meta)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, tools, **kwargs):
        return self


_ANTH_META = {"input_tokens": 1000 + 5000 + 300, "output_tokens": 200, "total_tokens": 6500,
              "input_token_details": {"cache_read": 5000, "cache_creation": 0,
                                      "ephemeral_5m_input_tokens": 300,
                                      "ephemeral_1h_input_tokens": 0}}


def test_chat_anthropic_attaches_ledger_callback():
    from app import llmclient
    m = llmclient.chat_anthropic("claude-opus-5-5", max_tokens=1000, api_key="sk-test",
                                 role="context")
    cbs = [c for c in (m.callbacks or []) if isinstance(c, usage.UsageCallback)]
    assert len(cbs) == 1 and cbs[0].model == "claude-opus-5-5" and cbs[0].role == "context"


def test_chat_openai_attaches_ledger_callback():
    from app import llmclient
    m = llmclient.chat_openai("gpt-4o-mini", api_key="sk-test", role="triage", max_tokens=10)
    (cb,) = [c for c in m.callbacks if isinstance(c, usage.UsageCallback)]
    assert cb.provider == "openai" and cb.role == "triage"


async def test_callback_splits_cache_tokens_from_usage_metadata():
    model = _Scripted(script=[AIMessage(content="hi")], usage_meta=_ANTH_META,
                      callbacks=[usage.UsageCallback(model="claude-opus-5-5", role="triage")])
    with usage.attribute(feature="triage"):
        await model.ainvoke("hello")
    (r,) = usage.pending_rows()
    assert (r.input_tokens, r.cache_read_tokens, r.cache_write_tokens, r.output_tokens) == (
        1000, 5000, 300, 200)
    assert r.agent == "triage" and r.feature == "triage" and r.latency_ms is not None
    assert r.cost_usd == pytest.approx((1000 * 4 + 5000 * 0.2 + 300 * 5 + 200 * 20) / 1e6)


def test_langchain_raw_anthropic_usage_preferred():
    resp = SimpleNamespace(llm_output={"model": "claude-sonnet-5-5", "usage": {
        "input_tokens": 7, "output_tokens": 9, "cache_read_input_tokens": 11,
        "cache_creation_input_tokens": 13,
        "cache_creation": {"ephemeral_1h_input_tokens": 3}}}, generations=[])
    fields, model = usage._langchain_usage(resp, "anthropic")
    assert model == "claude-sonnet-5-5"
    assert fields == {"input": 7, "output": 9, "cache_read": 11, "cache_write": 13,
                      "cache_write_1h": 3, "web_searches": 0}


def test_langchain_openai_token_usage():
    resp = SimpleNamespace(llm_output={"model_name": "gpt-4o-mini", "token_usage": {
        "prompt_tokens": 100, "completion_tokens": 5,
        "prompt_tokens_details": {"cached_tokens": 60}}}, generations=[])
    fields, _ = usage._langchain_usage(resp, "openai")
    assert (fields["input"], fields["cache_read"], fields["output"]) == (40, 60, 5)


async def test_callback_records_errors():
    cb = usage.UsageCallback(model="claude-opus-5-5", role="supervisor")
    cb.on_chat_model_start({}, [], run_id="r1")
    cb.on_llm_error(RuntimeError("429 rate limited"), run_id="r1")
    (r,) = usage.pending_rows()
    assert r.error.startswith("RuntimeError: 429") and r.cost_usd is None


async def test_delegate_calls_carry_delegate_name_and_mark_the_run(monkeypatch):
    """End to end through graph.run and graph._build's real _delegate: the
    supervisor's turns are 'supervisor', the dashboard specialist's are
    'dashboard', and the flush relabels the whole run as dashboard build/edit."""
    from app.agents import graph

    def model_for(script):
        return _Scripted(script=script, usage_meta=_ANTH_META,
                         callbacks=[usage.UsageCallback(model="claude-opus-5-5",
                                                        role="supervisor")])
    built = []

    def fake_make_model(cfg, role="supervisor", **kw):
        n = len(built)
        built.append(role)
        if n == 4:        # dashboard specialist (data, analysis, document, drawing, dashboard, ...)
            return model_for([AIMessage(content="draft 3 saved")])
        if n == 6:        # supervisor is built last
            return model_for([
                AIMessage(content="", tool_calls=[{
                    "name": "delegate_to_dashboard_agent", "args": {"task": "sales by region"},
                    "id": "c1", "type": "tool_call"}]),
                AIMessage(content="Your draft is ready.")])
        return model_for([])

    configs = {k: graph._merge_config(k, {}) for k in graph.DEFAULT_CONFIGS}
    configs["supervisor"]["persona"] = ""

    async def fake_configs():
        return configs

    class Pool:
        def __init__(self):
            self.executes, self.many = [], []

        async def execute(self, sql, *args):
            self.executes.append((sql, args))

        async def executemany(self, sql, rows):
            self.many.extend(rows)

    pool = Pool()

    async def get_pool():
        return pool

    monkeypatch.setattr(graph, "_make_model", fake_make_model)
    monkeypatch.setattr(graph, "effective_configs", fake_configs)
    monkeypatch.setattr(graph, "get_pool", get_pool)
    monkeypatch.setattr(graph, "_supervisor", None)
    monkeypatch.setattr(graph, "_built_with", None)

    tok = graph.ctx_run_id.set(77)
    try:
        usage.set_attribution(feature="chat", thread_id="ui:owner", run_id=77)
        reply = await graph.run([], "build me a sales dashboard")
    finally:
        graph.ctx_run_id.reset(tok)
    assert reply == "Your draft is ready."

    rows = usage.pending_rows()
    assert [(r.agent, r.feature) for r in rows] == [
        ("supervisor", "chat"), ("dashboard", "dashboard"), ("supervisor", "chat")]
    assert all(r.run_id == 77 and r.thread_id == "ui:owner" for r in rows)
    # the delegate tool call is logged with the calling agent
    tool_rows = [a for s, a in pool.executes if "INSERT INTO tool_calls" in s]
    assert tool_rows and tool_rows[0][1] == "delegate_to_dashboard_agent" and tool_rows[0][5] == "supervisor"

    assert await usage.flush(pool) == 3
    assert [row[5] for row in pool.many] == ["dashboard"] * 3       # whole run relabelled
    assert any("UPDATE llm_usage SET feature" in s and a == (77, "dashboard")
               for s, a in pool.executes)


def test_delegate_attribution_names():
    from app.agents import graph
    assert graph._delegate_attribution("delegate_to_data_agent") == {"agent": "data"}
    assert graph._delegate_attribution("delegate_to_dashboard_agent") == {
        "agent": "dashboard", "feature": "dashboard"}


# ── direct SDK capture ────────────────────────────────────────────────────

def _fake_anthropic(monkeypatch, result=None, exc=None):
    calls = []

    class Messages:
        async def create(self, **kw):
            calls.append(kw)
            if exc:
                raise exc
            return result

    class FakeClient:
        def __init__(self, **kw):
            self.messages = Messages()
            self.api_key = kw.get("api_key")
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(AsyncAnthropic=FakeClient))
    return calls


async def test_anthropic_async_records_direct_calls_with_web_searches(monkeypatch):
    from app import llmclient
    msg = SimpleNamespace(model="claude-sonnet-5-5", content=[], usage=SimpleNamespace(
        input_tokens=10, output_tokens=5, cache_read_input_tokens=100,
        cache_creation_input_tokens=20, cache_creation=None,
        server_tool_use=SimpleNamespace(web_search_requests=2)))
    calls = _fake_anthropic(monkeypatch, result=msg)
    client = llmclient.anthropic_async("sk-test", role="research")
    assert client.api_key == "sk-test"            # everything else passes through
    with usage.attribute(agent="supervisor", feature="chat"):
        out = await client.messages.create(model="claude-sonnet-5-5", max_tokens=10, messages=[])
    assert out is msg and calls[0]["max_tokens"] == 10
    (r,) = usage.pending_rows()
    assert (r.agent, r.feature, r.web_searches, r.cache_read_tokens) == ("research", "chat", 2, 100)
    assert r.cost_usd == pytest.approx((10 * 2 + 5 * 10 + 100 * 0.1 + 20 * 2.5) / 1e6 + 0.02)


async def test_anthropic_async_forced_feature_and_error_row(monkeypatch):
    from app import llmclient
    _fake_anthropic(monkeypatch, exc=RuntimeError("credit balance too low"))
    client = llmclient.anthropic_async("sk-test", role="interview", feature="interview")
    with usage.attribute(feature="chat"), pytest.raises(RuntimeError):
        await client.messages.create(model="claude-opus-5-5", max_tokens=10, messages=[])
    (r,) = usage.pending_rows()
    assert r.feature == "interview" and r.agent == "interview" and "credit balance" in r.error


async def test_wrap_openai_embeddings_and_chat():
    class Emb:
        async def create(self, **kw):
            return SimpleNamespace(model="text-embedding-3-small",
                                   usage=SimpleNamespace(prompt_tokens=8, total_tokens=8))

    class Completions:
        async def create(self, **kw):
            return SimpleNamespace(model="gpt-4o-mini-2024-07-18", usage=SimpleNamespace(
                prompt_tokens=100, completion_tokens=7,
                prompt_tokens_details=SimpleNamespace(cached_tokens=64)))

    client = SimpleNamespace(embeddings=Emb(), chat=SimpleNamespace(completions=Completions()),
                             models="untouched")
    w = usage.wrap_openai(client, role="reflection")
    assert usage.wrap_openai(w) is w and w.models == "untouched"
    with usage.attribute(feature="chat", run_id=5):
        await w.embeddings.create(model="text-embedding-3-small", input=["x"])
        await w.chat.completions.create(model="gpt-4o-mini", messages=[])
    e, c = usage.pending_rows()
    assert (e.feature, e.provider, e.input_tokens) == ("embeddings", "openai", 8)
    assert e.cost_usd == pytest.approx(8 * 0.02 / 1e6)
    assert (c.feature, c.agent, c.input_tokens, c.cache_read_tokens, c.output_tokens) == (
        "chat", "reflection", 36, 64, 7)
    assert c.run_id == 5


def test_graphiti_gets_ledger_wrapped_clients():
    from app.memoryservice import MemoryService
    s = SimpleNamespace(openai_api_key="sk-test", graphiti_llm="auto", anthropic_api_key="")
    parts = MemoryService._openai_parts(s)
    assert isinstance(parts["embedder"].client, usage._Proxy)
    assert isinstance(parts["cross_encoder"].client, usage._Proxy)
    assert isinstance(MemoryService._llm_client(s).client, usage._Proxy)


def test_no_unrecorded_model_clients_in_app():
    """Every model client goes through a recorded path: LangChain models via
    llmclient (callback attached), SDK clients wrapped."""
    offenders = []
    for path in APP.rglob("*.py"):
        if path.name in ("llmclient.py", "usage.py"):
            continue
        src = path.read_text(encoding="utf-8")
        for pat in (r"\bChatAnthropic\(", r"\bChatOpenAI\(", r"\bAsyncAnthropic\("):
            if re.search(pat, src):
                offenders.append(f"{path.name}: {pat}")
        for m in re.finditer(r"AsyncOpenAI\(", src):
            line = src[src.rfind("\n", 0, m.start()) + 1: src.find("\n", m.start())]
            window = src[max(0, m.start() - 200): m.start() + 120]
            if ".models.list()" in line:
                continue  # key check, no tokens
            if "wrap_openai" not in window and "_ledger(" not in window:
                offenders.append(f"{path.name}: unwrapped AsyncOpenAI")
    assert offenders == []


# ── writer ────────────────────────────────────────────────────────────────

async def test_flush_puts_batch_back_on_failure():
    class Boom:
        async def executemany(self, sql, rows):
            raise ConnectionError("db down")
    usage.record(model="claude-opus-5-5", input=1)
    usage.record(model="claude-opus-5-5", input=2)
    with pytest.raises(ConnectionError):
        await usage.flush(Boom())
    assert [r.input_tokens for r in usage.pending_rows()] == [1, 2]


async def test_writer_loop_survives_failures(monkeypatch):
    from tests.test_loop_resilience import _run_briefly
    calls = {"n": 0}

    async def boom(pool=None):
        calls["n"] += 1
        raise RuntimeError("write failed")
    monkeypatch.setattr(usage, "flush", boom)
    monkeypatch.setattr(usage, "evaluate_budget_alert", boom)
    usage.record(model="x")
    assert await _run_briefly(usage.run_forever), "usage writer died"
    assert calls["n"] >= 1


# ── daily budget ──────────────────────────────────────────────────────────

class _BudgetPool:
    def __init__(self, spent):
        self.spent = spent
        self.calls = []
        self.open_id = None

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        if "sum(cost_usd)" in sql:
            return self.spent
        if "INSERT INTO health_alerts" in sql:
            return self.open_id
        return None

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


def _budget_settings(monkeypatch, limit, tz="Asia/Kolkata"):
    s = SimpleNamespace(daily_llm_budget_usd=limit, timezone=tz, install_name="")
    monkeypatch.setattr("app.config.get_settings", lambda: s)
    return s


@pytest.mark.parametrize("limit,spent,state", [
    (0, 5.0, "off"), (10, 5.0, "ok"), (10, 8.0, "warn"), (10, 10.0, "paused"), (10, 12.5, "paused")])
async def test_budget_states(monkeypatch, limit, spent, state):
    _budget_settings(monkeypatch, limit)
    st = await usage.budget_state(_BudgetPool(spent))
    assert st["state"] == state and st["spent_usd"] == spent
    assert st["resets_at"].endswith("+05:30")


async def test_budget_counts_unflushed_rows(monkeypatch):
    _budget_settings(monkeypatch, 1)
    usage.record(model="claude-opus-5-5", output=50_000)   # $1.00
    st = await usage.budget_state(_BudgetPool(0.0))
    assert st["spent_usd"] == pytest.approx(1.0) and st["state"] == "paused"


async def test_day_window_follows_time_zone(monkeypatch):
    _budget_settings(monkeypatch, 1, tz="Asia/Kolkata")
    now = datetime(2026, 10, 25, 20, 0, tzinfo=timezone.utc)  # 01:30 on the 26th in IST
    start, end, name = usage.day_window(now)
    assert (start.day, start.hour, end.day, name) == (26, 0, 27, "Asia/Kolkata")
    assert start.utcoffset() == timedelta(hours=5, minutes=30)


async def test_pause_reason_and_fail_open(monkeypatch):
    _budget_settings(monkeypatch, 0)
    assert await usage.background_pause_reason() is None
    _budget_settings(monkeypatch, 10)

    async def paused(pool=None):
        return {"limit_usd": 10.0, "spent_usd": 10.4, "pct": 104, "state": "paused",
                "resets_at": "x", "timezone": "Asia/Kolkata"}
    monkeypatch.setattr(usage, "budget_state", paused)
    msg = await usage.background_pause_reason()
    assert "paused until midnight (Asia/Kolkata)" in msg and "chat" in msg

    async def broken(pool=None):
        raise ConnectionError("db down")
    monkeypatch.setattr(usage, "budget_state", broken)
    assert await usage.background_pause_reason() is None


async def test_budget_alert_opens_once_updates_and_resolves(monkeypatch):
    _budget_settings(monkeypatch, 10)
    pool = _BudgetPool(8.5)
    pool.open_id = 31
    assert await usage.evaluate_budget_alert(pool) == "warn"
    ins = [c for c in pool.calls if "INSERT INTO health_alerts" in c[0]]
    assert ins and ins[0][1][0] == "llm_budget" and "85%" in ins[0][1][1]

    usage._spent_cache.update(at=0.0)
    pool.spent, pool.open_id, pool.calls = 10.2, None, []   # already open -> update detail
    assert await usage.evaluate_budget_alert(pool) == "paused"
    assert any("UPDATE health_alerts SET detail" in s and "paused until midnight" in a[1]
               for s, a in pool.calls)

    usage._spent_cache.update(at=0.0)
    pool.spent, pool.calls = 0.1, []
    assert await usage.evaluate_budget_alert(pool) == "ok"
    assert any("SET resolved_at = now()" in s for s, _ in pool.calls)


def test_budget_alert_title_exists():
    from app import healthwatch
    assert "llm_budget" in healthwatch.TITLES
    assert "llm_budget" not in healthwatch.COMPONENTS   # the health sweep never resolves it


async def test_paused_budget_skips_watch_investigation(monkeypatch):
    from app import sentinel
    from app.agents import graph
    sentinel._escalation_times.clear()
    delivered = []

    async def deliver(thread, msg):
        delivered.append((thread, msg))

    async def reason():
        return "Today's AI budget ($1.00) is used up."

    async def never(*a, **k):
        raise AssertionError("graph.run must not be called")
    monkeypatch.setattr(sentinel, "_deliver", deliver)
    monkeypatch.setattr(usage, "background_pause_reason", reason)
    monkeypatch.setattr(graph, "run", never)
    watch = {"id": 9, "title": "Stale orders", "thread_id": "ui:owner", "condition": "x"}
    assert await sentinel._maybe_escalate(watch, {"severity": "critical", "summary": "s"}) is False
    assert delivered and "Investigation skipped" in delivered[0][1] and "budget" in delivered[0][1]
    assert sentinel._escalation_times == []


async def test_paused_budget_fails_playbook_task_with_message(monkeypatch):
    from app import worker
    from tests.test_prompt_layout import FakeConn, FakePool, _wire_worker

    class PlaybookConn(FakeConn):
        async def fetchrow(self, sql, *args):
            if "UPDATE task_runs SET status = 'running'" in sql:
                return {"id": 8, "thread_id": "playbook:1", "message_id": None, "kind": "playbook",
                        "playbook_id": 1, "case_id": None, "expectation_id": None}
            return None

    pool = FakePool()
    pool.conn = PlaybookConn(pool)
    _wire_worker(monkeypatch, pool, None)

    async def reason():
        return "Today's AI budget ($1.00) is used up."

    async def never(task):
        raise AssertionError("playbook must not run")
    monkeypatch.setattr(usage, "background_pause_reason", reason)
    from app import playbooks
    monkeypatch.setattr(playbooks, "run_playbook_task", never)
    assert await worker._process_one() is True
    failed = [a for s, a in pool.executed if "status='failed'" in s]
    assert failed and failed[0][0] == 8 and failed[0][1].startswith("paused: Today's AI budget")


# ── traces ────────────────────────────────────────────────────────────────

def test_build_trace_orders_and_totals():
    t0 = datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc)
    models = [
        {"ts": t0 + timedelta(seconds=4), "agent": "supervisor", "model": "claude-opus-5-5",
         "feature": "chat", "input_tokens": 10, "output_tokens": 100, "cache_read_tokens": 1000,
         "cache_write_tokens": 50, "latency_ms": 4000, "cost_usd": 0.01, "error": None},
        {"ts": t0 + timedelta(seconds=9), "agent": "data", "model": "claude-opus-5-5",
         "feature": "chat", "input_tokens": 5, "output_tokens": 20, "cache_read_tokens": 0,
         "cache_write_tokens": 0, "latency_ms": 1000, "cost_usd": None, "error": None},
    ]
    tools = [{"tool": "run_sql", "args": json.dumps({"sql": "SELECT  1\n FROM t"}),
              "result_digest": "SQL ERROR: relation t does not exist", "latency_ms": 500,
              "created_at": t0 + timedelta(seconds=10), "agent": "data"},
             {"tool": "delegate_to_data_agent", "args": {"task": "count rows"},
              "result_digest": "42 rows", "latency_ms": 6000,
              "created_at": t0 + timedelta(seconds=10, milliseconds=100), "agent": "supervisor"}]
    tr = usage.build_trace(models, tools, run_id=12)
    kinds = [(e["kind"], e.get("name") or e.get("agent")) for e in tr["events"]]
    assert kinds == [("model", "supervisor"), ("tool", "delegate_to_data_agent"),
                     ("model", "data"), ("tool", "run_sql")]
    sql = next(e for e in tr["events"] if e.get("name") == "run_sql")
    assert sql["summary"] == "SELECT 1 FROM t" and sql["error"].startswith("SQL ERROR")
    t = tr["totals"]
    assert (t["model_calls"], t["tool_calls"], t["tokens"], t["unpriced_calls"]) == (2, 2, 1185, 1)
    assert t["cost_usd"] == pytest.approx(0.01) and t["wall_ms"] == 10_100
    assert tr["trace"] == "run:12"


def test_args_summary_and_tool_error():
    assert usage.args_summary({"task": "x" * 300}) == "x" * 140
    assert usage.args_summary({"limit": 5, "flag": True}) == "limit=5, flag=True"
    assert usage.args_summary("not json") == "not json"
    assert usage.tool_error("web_research:refused", "REFUSED: ceiling") == "REFUSED: ceiling"
    assert usage.tool_error("run_sql", "TOOL ERROR: boom") == "TOOL ERROR: boom"
    assert usage.tool_error("run_sql", "region | total") is None


def test_usage_routes_are_ops_key_gated():
    from fastapi.testclient import TestClient
    from app import main
    mine = {}
    for r in main.app.router.routes:
        if hasattr(r, "original_router") and r.original_router is __import__(
                "app.usageapi", fromlist=["router"]).router:
            inh = list(r.include_context.dependencies)
            for s in r.original_router.routes:
                mine[s.path] = inh + list(s.dependencies)
    assert set(mine) == {"/api/usage/summary", "/api/usage/export.csv", "/api/usage/trace",
                         "/api/runs/{run_id}/trace"}
    for p, deps in mine.items():
        assert any(getattr(d, "dependency", None) is main._require_ops_key for d in deps), p
    assert TestClient(main.app).get("/api/usage/summary").status_code == 401


async def test_usage_api_validates_inputs():
    from fastapi import HTTPException
    from app import usageapi
    with pytest.raises(HTTPException):
        usageapi._period("year")
    with pytest.raises(HTTPException):
        usageapi._install("bad;install")
    assert usageapi._install("") is None and usageapi._install("client-fleet") == "client-fleet"
    with pytest.raises(HTTPException):
        await usageapi.usage_trace("run:1; DROP")


# ── retention ─────────────────────────────────────────────────────────────

async def test_ledger_retention_target(monkeypatch):
    from app import consolidation
    from tests.test_history_retention import Pool, _near
    pool = Pool()
    pool.fetchval_value = 4

    async def get_pool():
        return pool
    monkeypatch.setattr(consolidation, "get_pool", get_pool)
    monkeypatch.setattr(consolidation, "get_settings", lambda: SimpleNamespace(
        retention_dry_run=True, llm_usage_retention_days=180))
    out = await consolidation.history_retention()
    assert out["llm_usage"] == 4 and pool.executes == []
    sql, args = pool.fetchval_calls[-1]
    assert "FROM llm_usage" in sql and _near(args[0], 180)

    pool2 = Pool()
    pool2.execute_result = "DELETE 2"

    async def get_pool2():
        return pool2
    monkeypatch.setattr(consolidation, "get_pool", get_pool2)
    monkeypatch.setattr(consolidation, "get_settings", lambda: SimpleNamespace(
        retention_dry_run=False, llm_usage_retention_days=0))
    out = await consolidation.history_retention()
    assert "llm_usage" not in out          # 0 = keep forever


def test_schema_block_is_additive():
    sql = (APP / "schema.sql").read_text(encoding="utf-8")
    block = sql[sql.index("-- Quick 005: usage ledger"):sql.index("-- End Quick 005: usage ledger")]
    assert "CREATE TABLE IF NOT EXISTS llm_usage" in block
    assert "ADD COLUMN IF NOT EXISTS agent" in block
    for stmt in re.findall(r"CREATE INDEX[^;]+;", block):
        assert "IF NOT EXISTS" in stmt
    assert "DROP " not in block.upper().replace("DROP-", "")


# ── Langfuse gate ─────────────────────────────────────────────────────────

def _langfuse(monkeypatch, version=None, status=200, exc=None):
    from app import configservice
    configservice._langfuse_check.clear()
    s = SimpleNamespace(langfuse_public_key="pk-lf", langfuse_secret_key="sk-lf",
                        langfuse_host="http://lf.invalid:3001")
    monkeypatch.setattr(configservice, "get_settings", lambda: s)

    class Resp:
        status_code = status
        is_success = status < 400

        def json(self):
            return {"status": "OK", "version": version}

    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            if exc:
                raise exc
            assert url == "http://lf.invalid:3001/api/public/health"
            return Resp()
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    return configservice


async def test_langfuse_v2_server_is_refused(monkeypatch):
    cs = _langfuse(monkeypatch, version="2.95.11")
    ok, detail = await cs.check_langfuse()
    assert ok is False and "2.95.11 is too old" in detail and "3.63.0" in detail
    assert cs.langfuse_verdict() is False


async def test_langfuse_v3_server_is_accepted(monkeypatch):
    cs = _langfuse(monkeypatch, version="3.120.0")
    ok, detail = await cs.check_langfuse()
    assert ok is True and cs.langfuse_verdict() is True
    ok, _ = await cs._test_tracing()
    assert ok is True


async def test_langfuse_unreachable_or_unkeyed(monkeypatch):
    cs = _langfuse(monkeypatch, exc=OSError("refused"))
    ok, detail = await cs.check_langfuse()
    assert ok is False and "unreachable" in detail
    cs.get_settings().langfuse_public_key = ""
    ok, detail = await cs.check_langfuse(force=True)
    assert ok is False and "no Langfuse keys" in detail


async def test_graph_attaches_langfuse_only_after_a_good_verdict(monkeypatch):
    from app import configservice
    from app.agents import graph
    s = SimpleNamespace(langfuse_secret_key="sk-lf", langfuse_public_key="pk-lf",
                        langfuse_host="http://lf.invalid:3001")
    monkeypatch.setattr(graph, "get_settings", lambda: s)
    checks = []

    async def fake_check(force=False):
        checks.append(force)
        return False, "too old"
    monkeypatch.setattr(configservice, "langfuse_verdict", lambda: None)
    monkeypatch.setattr(configservice, "check_langfuse", fake_check)
    monkeypatch.setattr(graph, "_langfuse_probe", None)
    assert graph._callbacks() == {}         # not checked yet: nothing attached, check scheduled
    await asyncio.sleep(0)
    assert checks == [False]
    monkeypatch.setattr(configservice, "langfuse_verdict", lambda: False)
    assert graph._callbacks() == {}


async def test_csv_export_columns_and_plain_decimals(monkeypatch):
    rows = [{"ts": datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc), "install": "p18",
             "provider": "openai", "model": "text-embedding-3-small", "agent": "learnings",
             "feature": "embeddings", "thread_id": "ui:owner", "run_id": 1, "task_id": None,
             "trace_id": "run:1", "input_tokens": 10, "output_tokens": 0, "cache_read_tokens": 0,
             "cache_write_tokens": 0, "web_searches": 0, "latency_ms": 800, "cost_usd": 2e-07,
             "error": None}]

    class Pool:
        async def fetch(self, sql, *args):
            assert "ORDER BY ts" in sql and args[0] is not None
            return rows
    _budget_settings(monkeypatch, 0)
    out = await usage.export_csv(Pool(), "today")
    header, line = out.strip().split("\n")
    assert header.split(",") == list(usage.CSV_COLUMNS)
    assert ",0.0000002," in line and "e-07" not in line


async def test_bound_trace_groups_a_clients_calls(monkeypatch):
    from app import llmclient
    msg = SimpleNamespace(model="claude-opus-5-5", usage=SimpleNamespace(
        input_tokens=3, output_tokens=4, cache_read_input_tokens=0,
        cache_creation_input_tokens=0, cache_creation=None, server_tool_use=None))
    _fake_anthropic(monkeypatch, result=msg)
    client = llmclient.anthropic_async("sk-test", role="interview", feature="interview",
                                       trace="interview:7")
    await client.messages.create(model="claude-opus-5-5", max_tokens=5, messages=[])
    (r,) = usage.pending_rows()
    assert (r.task_id, r.trace_id, r.feature) == ("interview:7", "interview:7", "interview")

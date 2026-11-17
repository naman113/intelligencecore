"""Phase 12 (CTX-08): own token budget, per-run cap and timeout for context-pack runs.

Fake agents and LLM results shaped like budget.TokenBudget._count reads them; no network."""

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.callbacks import AsyncCallbackManager, BaseCallbackHandler
from langchain_core.messages import AIMessage

from app import configservice, worker
from app.agents import graph
from app.budget import BudgetExceeded, TokenBudget
from app.config import Settings


def _run(coro):
    return asyncio.run(coro)


def _llm_result(n):
    return SimpleNamespace(llm_output={"token_usage": {"total_tokens": n}}, generations=[])


class _Other(BaseCallbackHandler):
    pass


# ── settings ─────────────────────────────────────────────────────────────

def test_settings_and_registry():
    s = Settings()
    assert s.context_pack_token_budget == 400_000 and s.context_pack_timeout_seconds == 1800
    assert s.context_token_budget == 1500 and s.reactive_token_budget == 300_000
    for k in ("context_pack_token_budget", "context_pack_timeout_seconds"):
        e = configservice.REGISTRY[k]
        assert e["group"] == "tuning" and not e["secret"] and not e["required"]


# ── budget class and callback swap ───────────────────────────────────────

def test_context_budget_raises_subclass():
    b = graph.ContextPackBudget(1000)
    assert isinstance(b, TokenBudget)
    b.on_llm_end(_llm_result(900))
    with pytest.raises(graph.ContextPackBudgetExceeded) as e:
        b.on_llm_end(_llm_result(200))
    assert isinstance(e.value, BudgetExceeded)


def test_callbacks_list_and_none():
    h, other, run_b = graph.ContextPackBudget(5), _Other(), TokenBudget(150000)
    assert graph._context_callbacks([run_b, other], h) == [other, h]
    assert graph._context_callbacks(None, h) == [h]


def test_callbacks_manager_copy_not_mutated():
    run_b, other, h = TokenBudget(150000), _Other(), graph.ContextPackBudget(5)
    mgr = AsyncCallbackManager(handlers=[], inheritable_handlers=[])
    mgr.add_handler(run_b, inherit=True)
    mgr.add_handler(other, inherit=True)
    out = graph._context_callbacks(mgr, h)
    assert out is not mgr
    assert not any(type(x) is TokenBudget for x in out.handlers + out.inheritable_handlers)
    assert h in out.inheritable_handlers and other in out.inheritable_handlers
    assert run_b in mgr.handlers and run_b in mgr.inheritable_handlers
    assert h not in mgr.handlers


# ── _run_context_delegate ────────────────────────────────────────────────

class _Agent:
    def __init__(self, tokens=900, times=3, final=None, exc=None):
        self.tokens, self.times, self.exc = tokens, times, exc
        self.final = final or AIMessage(content="all done: 5 proposals")
        self.calls = 0

    async def ainvoke(self, inp, config=None):
        self.calls += 1
        if self.exc:
            raise self.exc
        for _ in range(self.times):
            for h in config["callbacks"]:
                h.on_llm_end(_llm_result(self.tokens))
        return {"messages": [self.final]}


@pytest.fixture
def env(monkeypatch):
    logged = []
    run_budget = TokenBudget(150000)
    holder = {"budget": 2000}

    async def fake_log(name, args, result, start, refused=False):
        logged.append((name, args, result, refused))

    monkeypatch.setattr(graph, "_log_tool", fake_log)
    monkeypatch.setattr(graph, "get_settings", lambda: SimpleNamespace(
        context_pack_token_budget=holder["budget"]))
    import langchain_core.runnables.config as rc
    monkeypatch.setattr(rc, "ensure_config", lambda *a, **k: {"callbacks": [run_budget]})
    graph._context_delegations.clear()
    tok = graph.ctx_run_id.set(777)
    yield SimpleNamespace(logged=logged, run_budget=run_budget, holder=holder)
    graph.ctx_run_id.reset(tok)
    graph._context_delegations.clear()


def test_budget_trips_stopped_early(env):
    out = _run(graph._run_context_delegate(_Agent(), "task", 50))
    assert out.startswith("stopped early: the context-pack token budget (2000) was reached")
    assert "waiting for approval" in out and "uploading the pack again continues" in out
    assert env.run_budget.used == 0
    assert env.logged[0][1]["stopped_early"] is True


def test_within_budget_logs_tokens(env):
    env.holder["budget"] = 10000
    out = _run(graph._run_context_delegate(_Agent(), "task", 50))
    assert out == "all done: 5 proposals"
    assert len(env.logged) == 1
    name, args, _, refused = env.logged[0]
    assert name == "delegate_to_context_agent" and not refused
    assert args["tokens_used"] == 2700 and args["token_budget"] == 10000
    assert args["stopped_early"] is False and "seconds" in args
    assert env.run_budget.used == 0


def test_plain_budget_exceeded_propagates(env):
    with pytest.raises(BudgetExceeded):
        _run(graph._run_context_delegate(_Agent(exc=BudgetExceeded("run")), "t", 50))


def test_refusal_and_other_errors(env, monkeypatch):
    monkeypatch.setattr(graph.llmclient, "is_refusal", lambda m: True)
    out = _run(graph._run_context_delegate(_Agent(times=0), "t", 50))
    assert out == "AGENT ERROR: the model declined (refusal)"
    monkeypatch.setattr(graph.llmclient, "is_refusal", lambda m: False)
    out = _run(graph._run_context_delegate(_Agent(exc=ValueError("boom")), "t", 50))
    assert out == "AGENT ERROR: ValueError: boom"


def test_third_delegation_refused(env):
    env.holder["budget"] = 10000
    agent = _Agent(times=0)
    for _ in range(2):
        _run(graph._run_context_delegate(agent, "t", 50))
    out = _run(graph._run_context_delegate(agent, "t", 50))
    assert out == "REFUSED: the context specialist already ran twice in this request"
    assert agent.calls == 2
    assert env.logged[-1][3] is True


# ── timeout ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("content,expected", [
    ("[document received: pack.json] This file is a context pack.", 1800),
    ("[document received: pack.yaml] x", 1800),
    ("[document received: pack.yml] x", 1800),
    ("[document received: PACK.MD] x", 1800),
    ("[document received: invoice.pdf] Extract this document", 600),
    ("[document received: notes.txt] x", 600),
    ("how are sales?", 600),
    (None, 600),
])
def test_task_timeout(monkeypatch, content, expected):
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(
        context_pack_timeout_seconds=1800, task_timeout_seconds=600))
    assert worker._task_timeout(content) == expected


def test_task_timeout_never_below_600(monkeypatch):
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(
        context_pack_timeout_seconds=300, task_timeout_seconds=600))
    assert worker._task_timeout("[document received: pack.json] x") == 600

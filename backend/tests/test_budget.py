"""Tests for app.budget — TokenBudget accounting + import compatibility."""

import pytest

from app.budget import BudgetExceeded, TokenBudget


class _LLMOutputResp:
    """Provider that reports usage via response.llm_output."""

    def __init__(self, n):
        self.llm_output = {"token_usage": {"total_tokens": n}}
        self.generations = []


class _Msg:
    def __init__(self, n):
        self.usage_metadata = {"total_tokens": n}


class _Gen:
    def __init__(self, n):
        self.message = _Msg(n)


class _UsageMetadataResp:
    """Provider that reports usage via per-generation usage_metadata."""

    def __init__(self, *counts):
        self.llm_output = None
        self.generations = [[_Gen(n) for n in counts]]


def test_accumulates_llm_output_usage():
    tb = TokenBudget(budget=1000)
    tb.on_llm_end(_LLMOutputResp(300))
    tb.on_llm_end(_LLMOutputResp(400))
    assert tb.used == 700


def test_accumulates_usage_metadata_fallback():
    tb = TokenBudget(budget=1000)
    tb.on_llm_end(_UsageMetadataResp(150, 250))
    assert tb.used == 400


def test_raises_past_budget():
    tb = TokenBudget(budget=100)
    tb.on_llm_end(_LLMOutputResp(60))
    assert tb.used == 60
    with pytest.raises(BudgetExceeded):
        tb.on_llm_end(_LLMOutputResp(60))
    assert tb.used == 120  # usage still recorded when the abort fires


def test_exactly_at_budget_does_not_raise():
    tb = TokenBudget(budget=100)
    tb.on_llm_end(_LLMOutputResp(100))  # == budget: only strictly-past raises
    assert tb.used == 100


def test_malformed_response_counts_zero():
    tb = TokenBudget(budget=10)
    tb.on_llm_end(object())  # no usage anywhere — fail-soft to 0
    assert tb.used == 0


def test_playbooks_reexport_is_same_object():
    from app import playbooks

    assert playbooks.TokenBudget is TokenBudget
    assert playbooks.BudgetExceeded is BudgetExceeded


# ── propagation through langchain-core callback dispatch ─────────────────
# handle_event/_ahandle_event_for_handler swallow handler exceptions unless
# handler.raise_error is True — without it the budget is inert (the raise in
# on_llm_end would be logged as a warning and dropped).


def test_raise_error_flag_is_set():
    assert TokenBudget(10).raise_error is True


def test_budget_exceeded_propagates_through_sync_callback_manager():
    from langchain_core.callbacks.manager import CallbackManager

    tb = TokenBudget(budget=100)
    cm = CallbackManager(handlers=[tb], inheritable_handlers=[tb])
    (run_mgr,) = cm.on_llm_start({"name": "fake"}, ["prompt"])
    run_mgr.on_llm_end(_LLMOutputResp(60))  # under budget: no raise
    with pytest.raises(BudgetExceeded):
        run_mgr.on_llm_end(_LLMOutputResp(60))
    assert tb.used == 120


async def test_budget_exceeded_propagates_through_async_callback_manager():
    from langchain_core.callbacks.manager import AsyncCallbackManager

    tb = TokenBudget(budget=100)
    acm = AsyncCallbackManager(handlers=[tb], inheritable_handlers=[tb])
    (run_mgr,) = await acm.on_llm_start({"name": "fake"}, ["prompt"])
    with pytest.raises(BudgetExceeded):
        await run_mgr.on_llm_end(_LLMOutputResp(150))
    assert tb.used == 150

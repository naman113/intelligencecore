"""Quick 039: the token budget counts cost (uncached-equivalent tokens), not raw tokens.

Run 89 on the Distributor copy (2026-10-27) made 4 Opus 5.5 calls and counted 201,587 of
its 300,000 reactive budget, most of it cheap cache reads. Fakes only; no network."""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration

from app import budget, configservice
from app.budget import BudgetExceeded, TokenBudget
from app.config import Settings

# Run 89 totals (llm_usage, 4 calls on claude-opus-5-5).
RUN89 = dict(input_tokens=10, cache_read_input_tokens=135_934,
             cache_creation_input_tokens=64_554, output_tokens=1_089)
RUN89_RAW = 201_587
# Opus 5.5 list prices per million: input 4, output 20, cache read 0.20, 5-min write 5,
# so weights 1, 5, 0.05, 1.25: 10 + 6,796.7 + 80,692.5 + 5,445 = 92,944.2
RUN89_WEIGHTED = 92_944


def _anthropic_result(model="claude-opus-5-5", **usage):
    """Shaped like ChatAnthropic._format_output: raw usage in llm_output, and the
    message usage_metadata whose input_tokens include cache reads and writes."""
    u = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 0, **usage}
    total_in = u["input_tokens"] + u["cache_read_input_tokens"] + u["cache_creation_input_tokens"]
    msg = AIMessage(content="ok")
    msg.usage_metadata = {"input_tokens": total_in, "output_tokens": u["output_tokens"],
                          "total_tokens": total_in + u["output_tokens"],
                          "input_token_details": {
                              "cache_read": u["cache_read_input_tokens"],
                              "cache_creation": u["cache_creation_input_tokens"]}}
    return SimpleNamespace(llm_output={"model": model, "model_name": model, "usage": u},
                           generations=[[ChatGeneration(message=msg)]])


def _streamed_result(model="claude-opus-5-5", **usage):
    """No llm_output (streamed): usage_metadata plus response_metadata.model_name."""
    r = _anthropic_result(model, **usage)
    gen = r.generations[0][0]
    gen.message.response_metadata = {"model_name": model}
    return SimpleNamespace(llm_output=None, generations=r.generations)


@pytest.fixture
def weighting(monkeypatch):
    def set_mode(mode):
        s = SimpleNamespace(budget_weighting=mode, llm_prices_json="")
        monkeypatch.setattr("app.config.get_settings", lambda: s)
        return s
    set_mode("cost")
    return set_mode


def test_run89_counts_cost_weighted_not_raw(weighting):
    tb = TokenBudget(300_000)
    tb.on_llm_end(_anthropic_result(**RUN89))
    assert tb.raw == RUN89_RAW
    assert tb.weighted == RUN89_WEIGHTED
    assert tb.used == RUN89_WEIGHTED


def test_raw_setting_rolls_back_to_the_old_count(weighting):
    weighting("raw")
    tb = TokenBudget(300_000)
    tb.on_llm_end(_anthropic_result(**RUN89))
    assert tb.used == RUN89_RAW
    assert tb.weighted == RUN89_WEIGHTED  # still shown, never enforced


def test_unknown_setting_value_means_cost(weighting):
    weighting("banana")
    assert budget.weighting() == "cost"


def test_streamed_usage_metadata_path_is_weighted_too(weighting):
    tb = TokenBudget(300_000)
    tb.on_llm_end(_streamed_result(**RUN89))
    assert (tb.raw, tb.used) == (RUN89_RAW, RUN89_WEIGHTED)


def test_one_hour_cache_writes_use_their_own_multiplier(weighting):
    u = dict(RUN89, cache_creation={"ephemeral_5m_input_tokens": 0,
                                    "ephemeral_1h_input_tokens": 64_554})
    tb = TokenBudget(300_000)
    tb.on_llm_end(_anthropic_result(**u))
    # 1-hour write on Opus 5.5 is $8 = 2x input: 10 + 6,796.7 + 129,108 + 5,445
    assert tb.used == 141_360


def test_weights_come_from_the_usage_price_table(weighting):
    w = budget.token_weights("claude-opus-5-5")
    assert w == pytest.approx({"input": 1.0, "output": 5.0, "cache_read": 0.05,
                               "cache_write": 1.25, "cache_write_1h": 2.0})
    assert budget.token_weights("claude-haiku-5-5")["cache_read"] == pytest.approx(0.1)
    s = weighting("cost")
    s.llm_prices_json = '{"claude-opus-5-5": {"input": 4, "output": 20, "cache_read": 0.4, "cache_write": 5}}'
    assert budget.token_weights("claude-opus-5-5")["cache_read"] == pytest.approx(0.1)


def test_unpriced_model_counts_raw(weighting):
    tb = TokenBudget(300_000)
    tb.on_llm_end(_anthropic_result(model="claude-opus-9-9", **RUN89))
    assert tb.used == tb.raw == RUN89_RAW


def test_total_only_usage_counts_raw(weighting):
    """Providers (and test doubles) that report only total_tokens count as before."""
    tb = TokenBudget(1000)
    tb.on_llm_end(SimpleNamespace(llm_output={"token_usage": {"total_tokens": 300}},
                                  generations=[]))
    assert (tb.raw, tb.weighted, tb.used) == (300, 300, 300)


def test_exceeded_message_shows_both_counts(weighting):
    tb = TokenBudget(100_000)
    with pytest.raises(BudgetExceeded) as e:
        tb.on_llm_end(_anthropic_result(**RUN89))
        tb.on_llm_end(_anthropic_result(**RUN89))
    text = str(e.value)
    assert text.startswith("token budget 100000 exceeded")
    assert "185,888 cost-weighted" in text and "403,174 raw" in text


def test_raw_mode_message_names_raw_as_the_count(weighting):
    weighting("raw")
    tb = TokenBudget(100_000)
    with pytest.raises(BudgetExceeded) as e:
        tb.on_llm_end(_anthropic_result(**RUN89))
    assert "201,587 raw" in str(e.value) and "92,944 cost-weighted" in str(e.value)


def test_run89_shape_now_fits_three_times_over(weighting):
    """The run that counted 201,587 of 300,000 in four calls: under cost weighting
    three such runs' worth of calls fit before the budget trips."""
    tb = TokenBudget(300_000)
    for _ in range(3):
        tb.on_llm_end(_anthropic_result(**RUN89))
    with pytest.raises(BudgetExceeded):
        tb.on_llm_end(_anthropic_result(**RUN89))


def test_output_heavy_calls_count_more_than_before(weighting):
    """A 16,000-token output call (the dashboard ceiling) counts at output price."""
    tb = TokenBudget(1_000_000)
    tb.on_llm_end(_anthropic_result(input_tokens=5, cache_read_input_tokens=30_000,
                                    output_tokens=16_000))
    assert tb.raw == 46_005
    assert tb.used == 81_505  # 5 + 1,500 + 80,000


def test_subclass_budgets_share_the_weighting_and_message(weighting):
    from app.agents import graph
    for cls, exc, label in ((graph.ContextPackBudget, graph.ContextPackBudgetExceeded,
                             "context-pack token budget"),
                            (graph.DataAgentBudget, graph.DataAgentBudgetExceeded,
                             "data agent token budget")):
        b = cls(50_000)
        with pytest.raises(exc) as e:
            b.on_llm_end(_anthropic_result(**RUN89))
        assert isinstance(e.value, BudgetExceeded)
        assert str(e.value).startswith(f"{label} 50000 exceeded")
        assert "92,944 cost-weighted" in str(e.value) and "201,587 raw" in str(e.value)
        assert b.used == RUN89_WEIGHTED and b.raw == RUN89_RAW


def test_counts_line_for_logs(weighting):
    tb = TokenBudget(300_000)
    tb.on_llm_end(_anthropic_result(**RUN89))
    assert tb.counts() == "92,944 cost-weighted / 201,587 raw tokens of 300,000 (cost)"


def test_setting_and_registry():
    assert Settings(_env_file=None).budget_weighting == "cost"
    e = configservice.REGISTRY["budget_weighting"]
    assert e["choices"] == ["cost", "raw"] and e["group"] == "tuning"
    assert not e["secret"] and not e["required"]
    configservice._validate_value("budget_weighting", "raw")
    with pytest.raises(ValueError):
        configservice._validate_value("budget_weighting", "tokens")

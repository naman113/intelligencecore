"""Token budget enforcement for agent runs.

Moved out of playbooks.py so both the playbook engine and the reactive path
can share one budget mechanism. playbooks.py re-exports these names, so
existing `from app.playbooks import TokenBudget, BudgetExceeded` keeps working.

Quick 039: the budget counts cost, not raw tokens. Each call is counted in
uncached-equivalent tokens: every token kind weighted by its list price over the
model's uncached input price, from usage.py's price table (llm_prices_json
overrides included). On Opus 5.5 that is input 1, cache write 1.25 (5-minute) or
2 (1-hour), cache read 0.05, output 5. Counting cache reads at full weight made
the budget a step limit on big contexts (run 89: four calls counted 201,587 of
300,000). `budget_weighting` = raw restores the old count. A call with no usable
breakdown, or on a model without a price, counts raw as before.
"""

from langchain_core.callbacks import BaseCallbackHandler

WEIGHTINGS = ("cost", "raw")
_KINDS = ("input", "output", "cache_read", "cache_write", "cache_write_1h")


class BudgetExceeded(Exception):
    """Raised when a playbook run exceeds its token budget."""


def weighting() -> str:
    """Quick 039: the `budget_weighting` setting; anything unknown reads as cost."""
    try:
        from .config import get_settings
        mode = str(getattr(get_settings(), "budget_weighting", "cost") or "cost")
    except Exception:
        return "cost"
    mode = mode.strip().lower()
    return mode if mode in WEIGHTINGS else "cost"


def token_weights(model) -> dict | None:
    """Uncached-equivalent weight per token kind for `model` (price / uncached input
    price), or None when the model has no price with a positive input rate."""
    try:
        from . import usage
        p = usage.price_for(model)
        base = float((p or {}).get("input") or 0)
        if base <= 0:
            return None
        w = {k: float(p.get(k) or 0) / base for k in _KINDS if k != "cache_write_1h"}
        w["cache_write_1h"] = float(p.get("cache_write_1h", p.get("cache_write")) or 0) / base
        return w
    except Exception:
        return None


def _breakdown(response) -> tuple[dict, str | None]:
    """Ledger-shaped token fields for one LLMResult (the same reader as usage.py)."""
    try:
        from . import usage
        return usage._langchain_usage(response, "anthropic")
    except Exception:
        return {}, None


def weighted_tokens(fields: dict, model) -> float | None:
    """Uncached-equivalent tokens for one call's ledger fields; None if unpriced."""
    w = token_weights(model)
    if w is None:
        return None
    cw = int(fields.get("cache_write") or 0)
    w1h = max(0, min(int(fields.get("cache_write_1h") or 0), cw))
    return (int(fields.get("input") or 0) * w["input"]
            + int(fields.get("output") or 0) * w["output"]
            + int(fields.get("cache_read") or 0) * w["cache_read"]
            + (cw - w1h) * w["cache_write"] + w1h * w["cache_write_1h"])


class TokenBudget(BaseCallbackHandler):
    """Aborts a run once cumulative tokens exceed the budget (plan §7.6).

    `used` is what counts against `budget`: cost-weighted tokens by default, raw
    tokens when budget_weighting is raw (read once per budget, so per run). `raw`
    and `weighted` are both kept for logs and the exceeded message."""

    # Without this, langchain-core's callback dispatch (handle_event /
    # _ahandle_event_for_handler) catches the BudgetExceeded raised in
    # on_llm_end, logs a warning, and drops it — the budget would be inert.
    raise_error = True
    exceeded = BudgetExceeded          # subclasses raise their own subclass
    label = "token budget"

    def __init__(self, budget: int):
        self.budget = budget
        self.mode = weighting()
        self.raw = 0
        self._weighted = 0.0
        self.used = 0

    @property
    def weighted(self) -> int:
        return int(round(self._weighted))

    def _count(self, response) -> int:
        """Raw tokens in one response: the whole prompt (cache reads and writes
        included) plus output, as the budget counted before Quick 039."""
        try:
            out = getattr(response, "llm_output", None) or {}
            usage = out.get("token_usage") or out.get("usage") or {}
            n = usage.get("total_tokens") or 0
            if n:
                return int(n)
            total = 0
            for gen in (getattr(response, "generations", None) or []):
                for g in gen:
                    meta = getattr(getattr(g, "message", None), "usage_metadata", None) or {}
                    total += int(meta.get("total_tokens") or 0)
            return total
        except Exception:
            return 0

    def add(self, response) -> None:
        """Count one model response into raw, weighted and used. Never raises."""
        raw = self._count(response)
        fields, model = _breakdown(response)
        parts = sum(int(fields.get(k) or 0) for k in
                    ("input", "output", "cache_read", "cache_write"))
        raw = raw or parts
        weighted = weighted_tokens(fields, model) if parts else None
        self.raw += raw
        self._weighted += raw if weighted is None else weighted
        self.used = self.raw if self.mode == "raw" else self.weighted

    def counts(self) -> str:
        """One line for logs: both counts, the budget and the mode that enforces it."""
        return (f"{self.weighted:,} cost-weighted / {self.raw:,} raw tokens of "
                f"{self.budget:,} ({self.mode})")

    def on_llm_end(self, response, **kwargs) -> None:
        self.add(response)
        if self.used > self.budget:
            first, second = ((f"{self.raw:,} raw", f"{self.weighted:,} cost-weighted")
                             if self.mode == "raw" else
                             (f"{self.weighted:,} cost-weighted", f"{self.raw:,} raw"))
            raise self.exceeded(
                f"{self.label} {self.budget} exceeded ({first} tokens; {second})")

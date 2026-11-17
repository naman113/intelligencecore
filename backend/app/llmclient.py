"""Single seam for per-model request parameters (Phase 12, CTX-09e).

Claude 5 family models reject sampling parameters, forced tool_choice and
thinking disabled; omission is always valid, so unknown Claude IDs default to
omitting. Every Anthropic client in the app is built through this module.
Effort and the max_tokens floor for always-thinking models are also owned here (Phase 13).
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("llmclient")

# Models that still accept temperature/top_p/top_k: claude-3*, and the 4 family
# (-4, -4-1, -4-5, -4-6) with an optional 8-digit date suffix.
_SAMPLING_OK = re.compile(
    r"^claude-(3[\w.-]*|(opus|sonnet|haiku)-4(-1|-5|-6)?(-\d{8})?)$")

MAX_TOKENS_CEILING = 32000

REFUSAL_REPLY = ("I can't help with that request, so I've stopped here and "
                 "nothing was changed.")

_SAMPLING_KEYS = ("temperature", "top_p", "top_k")

# Quick 018: Anthropic's automatic (top-level) prompt caching for agent loops.
# The API puts one breakpoint on the last cacheable block and moves it forward as
# the ReAct loop appends tool calls and results, so each call reads the whole
# prior conversation from cache (0.05x on Opus/Sonnet 5.5) instead of re-sending
# it uncached. With _cached_prompt's system marker that is 2 of the 4 breakpoint
# slots, both 5-minute. ChatAnthropic (direct API, _llm_type "anthropic-chat")
# sends model_kwargs at the top level of the request unchanged. Never for
# one-shot calls: a write on a unique tail that is never read is pure surcharge.
CONVERSATION_CACHE = {"type": "ephemeral"}
# Server-side tool-result clearing (clear_tool_uses_20250919, audit Fix 4) was
# A/B-tested in Quick 018 and left out: with this cache on, each clear re-writes
# the conversation after the first cleared result (1.25x) to save 0.05x reads.


def loop_cache_on(settings: Any = None) -> bool:
    """Quick 035: prompt_cache_loops (default true), the one rollback for the loops
    outside the chat graph that cache: the playbook agent and the watch compiler
    (system marker + conversation cache) and the triage batch (system marker only).
    A settings object without the field (older stand-ins) means the default."""
    if settings is None:
        from .config import get_settings
        settings = get_settings()
    return bool(getattr(settings, "prompt_cache_loops", True))


def accepts_sampling(model: str | None) -> bool:
    name = (model or "").strip().lower()
    if not name.startswith("claude"):
        return True
    return bool(_SAMPLING_OK.match(name))


def chat_anthropic(model: str, *, max_tokens: int, api_key: str | None,
                   temperature: float | None = None, role: str | None = None,
                   effort: str | None = None, settings: Any = None,
                   conversation_cache: bool = False, **kw: Any):
    from langchain_anthropic import ChatAnthropic
    name = (model or "").strip()
    # Phase 13 (GOLIVE-01): effort gating and the thinking max_tokens floor
    eff = effort_for(name, effort or role_effort(role, settings))
    if thinks_by_default(name):
        configured = max_tokens
        max_tokens = min(max(max_tokens, THINKING_MAX_TOKENS_FLOOR), MAX_TOKENS_CEILING)
        _log_floor(name, role, configured, max_tokens)  # Quick 036
    if eff is not None:
        kw["effort"] = eff
    if conversation_cache:  # Quick 018: agent loops cache their growing conversation
        kw["model_kwargs"] = {**(kw.get("model_kwargs") or {}), "cache_control": CONVERSATION_CACHE}
    return ChatAnthropic(
        model=name, max_tokens=max_tokens, api_key=api_key,
        temperature=temperature if accepts_sampling(name) else None,
        callbacks=_usage_callbacks(kw.pop("callbacks", None), name, role), **kw)


# ── Quick 036: show the max_tokens actually sent when the thinking floor raises it ──
# (agent config says 8,192 for the dashboard specialist; Claude 5 calls get 16,000).
# Once per model, role and value per process: models are built per agent build, and some
# one-shot callers build one per call.
_floor_logged: set = set()


def _log_floor(model: str, role: str | None, configured: int, sent: int) -> None:
    key = (model, role, configured, sent)
    if sent == configured or key in _floor_logged:
        return
    _floor_logged.add(key)
    log.info("max_tokens for %s on %s: configured %s, sent %s (thinking shares max_tokens; "
             "floor %s)", role or "unknown role", model, configured, sent,
             THINKING_MAX_TOKENS_FLOOR)


# ── Quick 005: usage ledger capture (usage.py) ──
def _usage_callbacks(existing, model: str, role: str | None, provider: str = "anthropic"):
    """The model's own callbacks plus the ledger callback. Attached to the model
    object, so every call it makes is recorded, delegates and playbooks included."""
    from . import usage
    return [*(existing or []), usage.UsageCallback(model=model, role=role, provider=provider)]


def chat_openai(model: str, *, api_key: str | None, role: str | None = None, **kw: Any):
    """ChatOpenAI (the no-Anthropic-key fallback) with the ledger callback."""
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=model, api_key=api_key,
                      callbacks=_usage_callbacks(kw.pop("callbacks", None), model, role, "openai"),
                      **kw)


def anthropic_async(api_key: str | None, *, role: str | None = None,
                    feature: str | None = None, trace: str | None = None, **kw: Any):
    """AsyncAnthropic for direct SDK calls, wrapped so messages.create is recorded.
    `role` names the job (research, document, drawing, interview, setup_test);
    `feature` forces the ledger feature (else the caller's attribution)."""
    from anthropic import AsyncAnthropic
    from . import usage
    return usage.wrap_anthropic(AsyncAnthropic(api_key=api_key, **kw), role=role,
                                feature=feature, trace=trace)


def sdk_kwargs(model: str, **params: Any) -> dict:
    """Drop parameters the model would reject from raw SDK call kwargs."""
    out = dict(params)
    if not accepts_sampling(model):
        for k in _SAMPLING_KEYS:
            out.pop(k, None)
    tc = out.get("tool_choice")
    if isinstance(tc, dict) and tc.get("type") in ("tool", "any") \
            and not forced_tool_ok(model):  # Quick 022: Haiku 5.5 keeps a forced tool
        out.pop("tool_choice")
    if out.get("thinking") == {"type": "disabled"}:
        out.pop("thinking")
    return out


def is_refusal(msg: Any) -> bool:
    if msg is None:
        return False
    meta = getattr(msg, "response_metadata", None)
    if isinstance(meta, dict) and meta.get("stop_reason") == "refusal":
        return True
    return getattr(msg, "stop_reason", None) == "refusal"


# ── Phase 13 (GOLIVE-01): model catalog, per-model effort, thinking max_tokens floor ──
# Effort support per platform.claude.com/docs/en/build-with-claude/effort and a live
# probe on 2026-10-25. Models absent from the table reject `effort` with HTTP 400,
# so absence means omit.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_ALL5 = frozenset(EFFORT_LEVELS)
_NO_XHIGH = frozenset(("low", "medium", "high", "max"))
_EFFORT_BY_MODEL: dict[str, frozenset] = {
    **{m: _ALL5 for m in (
        "claude-fable-5-1", "claude-fable-5", "claude-mythos-5-1", "claude-mythos-5",
        "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
        "claude-sonnet-5-5", "claude-sonnet-5", "claude-haiku-5-5")},
    "claude-opus-4-6": _NO_XHIGH,
    "claude-sonnet-4-6": _NO_XHIGH,
    "claude-opus-4-5-20251101": frozenset(("low", "medium", "high")),
}

# Claude 5 family: thinking is on by default and shares max_tokens.
_THINKS = re.compile(
    r"^claude-(opus|sonnet|haiku|fable|mythos)-([5-9]|\d{2,})(-\d+)?(-\d{8})?$")
THINKING_MAX_TOKENS_FLOOR = 16000
# The pinned anthropic 0.116.0 SDK refuses non-streaming calls above about 21,333.
RAW_SDK_MAX_TOKENS = 21000

# Quick 012: "data" is the Data Agent (heavy SQL investigations).
ROLES = ("supervisor", "onboarding", "context", "document", "research", "triage", "data")
ROLE_EFFORT_FALLBACK = {"supervisor": "medium", "onboarding": "high", "context": "medium",
                        "document": "high", "research": "medium", "triage": "low",
                        "data": "medium"}


def effort_levels(model: str | None) -> frozenset:
    return _EFFORT_BY_MODEL.get((model or "").strip().lower(), frozenset())


def effort_for(model: str | None, level: str | None) -> str | None:
    """The effort to send, clamped down to what the model supports; None = omit."""
    lvl = (level or "").strip().lower()
    if lvl not in EFFORT_LEVELS:
        return None
    ok = effort_levels(model)
    if not ok:
        return None
    for cand in reversed(EFFORT_LEVELS[:EFFORT_LEVELS.index(lvl) + 1]):
        if cand in ok:
            return cand
    return None


def thinks_by_default(model: str | None) -> bool:
    return bool(_THINKS.match((model or "").strip().lower()))


def role_effort(role: str | None, settings: Any = None) -> str | None:
    if not role:
        return None
    if settings is None:
        from .config import get_settings
        settings = get_settings()
    return getattr(settings, f"{role}_effort", None) or ROLE_EFFORT_FALLBACK.get(role)


CURRENT_MODELS: tuple = (
    {"value": "claude-opus-5-5", "group": "current",
     "note": "$4 / $20 per million tokens. Default for the supervisor, most specialists, "
             "the interview and context import: strong reasoning at moderate speed."},
    {"value": "claude-sonnet-5-5", "group": "current",
     "note": "$2 / $10 per million tokens. Fast and capable: default for the Data Agent "
             "and web research."},
    {"value": "claude-haiku-5-5", "group": "current",
     "note": "From $0.10 / $0.50 per million tokens. Fastest and cheapest: default for "
             "triage, document extraction, reflection and the memory pipeline "
             "(Quick 022)."},
    {"value": "claude-fable-5-1", "group": "current",
     "note": "$10 / $50 per million tokens. Deepest reasoning for long, demanding work, "
             "and slowest. Needs 30-day data retention on your Anthropic account "
             "(not available with zero data retention)."},
)

LEGACY_MODELS: tuple = (
    {"value": "claude-fable-5", "group": "legacy",
     "note": "Legacy. $10 / $50 per million tokens. Needs 30-day data retention on your "
             "Anthropic account."},
    {"value": "claude-opus-5", "group": "legacy", "note": "Legacy. $5 / $25 per million tokens."},
    {"value": "claude-sonnet-5", "group": "legacy", "note": "Legacy. $2 / $10 per million tokens."},
    {"value": "claude-opus-4-8", "group": "legacy", "note": "Legacy. $5 / $25 per million tokens."},
    {"value": "claude-opus-4-7", "group": "legacy", "note": "Legacy. $5 / $25 per million tokens."},
    {"value": "claude-opus-4-6", "group": "legacy",
     "note": "Legacy. $5 / $25 per million tokens, no xhigh effort."},
    {"value": "claude-sonnet-4-6", "group": "legacy",
     "note": "Legacy. $3 / $15 per million tokens, no xhigh effort."},
    {"value": "claude-haiku-4-5", "group": "legacy",
     "note": "Legacy. $1 / $5 per million tokens, no effort control, past its earliest "
             "retirement date: move to claude-haiku-5-5."},
    {"value": "claude-sonnet-4-5", "group": "legacy",
     "note": "Deprecated: retires 2026-11-30. Move to claude-sonnet-5-5."},
)


def model_options() -> list[dict]:
    return [dict(m) for m in CURRENT_MODELS + LEGACY_MODELS]


def messages_kwargs(model: str, *, max_tokens: int, role: str | None = None,
                    effort: str | None = None, settings: Any = None,
                    **params: Any) -> dict:
    """Kwargs for a raw SDK messages.create call: effort, token floor, no rejected params."""
    name = (model or "").strip()
    eff = effort_for(name, effort or role_effort(role, settings))
    if eff is not None:
        oc = dict(params.get("output_config") or {})
        oc["effort"] = eff
        params["output_config"] = oc
    if thinks_by_default(name):
        max_tokens = min(max(max_tokens, THINKING_MAX_TOKENS_FLOOR), RAW_SDK_MAX_TOKENS)
    out = sdk_kwargs(name, max_tokens=max_tokens, **params)
    out["model"] = name
    return out


# ── Quick 022: Haiku 5.5 for the memory work ──
# "reflection" is every memory call (reflection, learning merges, case distillation,
# playbook proposals); "graph" is Graphiti's extraction client on the opt-in
# Anthropic path (graphiti_llm 'anthropic'; OpenAI gpt-5.5 by default). Both are one-shot:
# no conversation cache (a write on a tail that is never read is pure surcharge).
ROLES = ROLES + ("reflection", "graph")
ROLE_EFFORT_FALLBACK.update({"reflection": "medium", "graph": "medium"})

# Forced tool use (tool_choice any/tool). Claude Haiku 5.5 accepts it and answers
# with the tool call, skipping thinking (claude-api skill, Migrating to Claude Haiku
# 5.5; live probe 2026-10-26). Opus 5.5, Sonnet 5.5 and Fable 5.1 return 400 on it
# (same probe: "tool_choice: type "tool" and "any" are not supported for this
# model"), so every other Claude model keeps Phase 12's rule: drop it, and the
# request runs with tool_choice auto. Non-Claude models are never sent here.
_FORCED_TOOL_OK = frozenset(("claude-haiku-5-5",))


def forced_tool_ok(model: str | None) -> bool:
    name = (model or "").strip().lower()
    return name in _FORCED_TOOL_OK or not name.startswith("claude")


def anthropic_model(model: str | None, settings: Any = None) -> bool:
    """True when `model` should run on Anthropic: a Claude model id and a key set."""
    if settings is None:
        from .config import get_settings
        settings = get_settings()
    return ((model or "").strip().lower().startswith("claude")
            and bool(getattr(settings, "anthropic_api_key", "")))


async def anthropic_text(model: str, *, system: str, user: str, role: str,
                         ledger_role: str, max_tokens: int, json_schema: dict | None = None,
                         settings: Any = None, timeout: float = 120.0) -> str:
    """One system + user request to an Anthropic model, recorded in the ledger under
    `ledger_role`; returns the text blocks joined (thinking blocks skipped).
    `json_schema` asks for structured output (output_config.format). Raises on a
    refusal or an API error: callers keep their own fail-soft behaviour."""
    if settings is None:
        from .config import get_settings
        settings = get_settings()
    client = anthropic_async(settings.anthropic_api_key, role=ledger_role,
                             max_retries=4, timeout=timeout)
    params: dict[str, Any] = {"system": system,
                              "messages": [{"role": "user", "content": user}]}
    if json_schema is not None:
        params["output_config"] = {"format": {"type": "json_schema", "schema": json_schema}}
    msg = await client.messages.create(**messages_kwargs(
        model, max_tokens=max_tokens, role=role, settings=settings, **params))
    if is_refusal(msg):
        raise RuntimeError(f"{model} declined the request (refusal)")
    return "".join(getattr(b, "text", "") or "" for b in msg.content
                   if getattr(b, "type", "") == "text").strip()


class _GraphMessages:
    """messages.create for Graphiti: every request goes through messages_kwargs, so
    sampling keys are dropped for models that reject them, a forced tool_choice is
    kept only where the model accepts it (forced_tool_ok), and effort comes from
    graph_effort. Settings are read per call, so a Setup change applies live."""

    def __init__(self, messages, role: str):
        self._messages = messages
        self._role = role

    async def create(self, **kw: Any):
        model = str(kw.pop("model", "") or "")
        max_tokens = int(kw.pop("max_tokens", 0) or 16384)
        return await self._messages.create(**messages_kwargs(
            model, max_tokens=max_tokens, role=self._role, **kw))


class GraphAnthropic:
    """The AsyncAnthropic handed to graphiti-core's AnthropicClient (Quick 022).
    graphiti-core 0.29.2 sends temperature=1 and a forced tool on every call
    (llm_client/anthropic_client.py:211, :288-296); `inner` is the ledger-wrapped
    client, so every call is still recorded once."""

    def __init__(self, inner, role: str = "graph"):
        self.inner = inner
        self.messages = _GraphMessages(inner.messages, role)

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


def graph_anthropic(api_key: str | None) -> GraphAnthropic:
    return GraphAnthropic(anthropic_async(api_key, role="memory_graph", max_retries=4))

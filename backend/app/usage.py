"""Usage ledger (Quick 005): one row per model call, in the owner's own database.

The single seam for recording what the AI models cost. Every capture point
lands here and nowhere else:

  * LangChain models: `UsageCallback`, attached to the model object itself by
    `llmclient.chat_anthropic` / `llmclient.chat_openai`. A model-level callback
    fires on every call that model makes, so delegates, playbooks, the watch
    compiler, triage and pollers are all covered without relying on any
    caller's config propagation (an explicit callbacks list in a delegate's
    config cannot drop it).
  * Direct SDK clients: `wrap_anthropic` / `wrap_openai` return a thin proxy
    whose messages / embeddings / chat.completions / responses calls are
    timed and recorded (web research, document extraction, layer naming, the
    interview, the Setup test, reflection, context-store embeddings, Graphiti).

Attribution comes from contextvars: `attribute(...)` (scoped) or
`set_attribution(...)` (per worker task). Run and thread fall back to the
agent graph's ctx_run_id / ctx_phone, so anything inside a claimed run is
attributed even when nobody set a feature.

Recording never breaks or slows a run: `record()` appends to a bounded
in-memory queue and returns; `run_forever()` writes batches in the background.
Every failure is swallowed and logged. Nothing leaves the box.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import csv
import io
import json
import logging
import os
import re
import sys
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

log = logging.getLogger("usage")

# ── prices ────────────────────────────────────────────────────────────────
# USD per million tokens. Anthropic: platform.claude.com/docs/en/about-claude/pricing;
# OpenAI: developers.openai.com/api/docs/pricing. Both fetched on PRICE_TABLE_DATE.
# cache_write is the 5-minute TTL write (the app's ephemeral breakpoints);
# cache_write_1h applies only to tokens the API reports as 1-hour writes.
# The llm_prices_json setting overrides or adds models without a release.
PRICE_TABLE_DATE = "2026-10-25"
WEB_SEARCH_USD = 0.01          # $10 per 1,000 searches (Anthropic web search tool)


def _p(inp, out, read, write5=None, write1h=None) -> dict:
    return {"input": inp, "output": out, "cache_read": read,
            "cache_write": inp * 1.25 if write5 is None else write5,
            "cache_write_1h": inp * 2 if write1h is None else write1h}


_OPUS_STD = _p(5, 25, 0.50, 6.25, 10)
_SONNET_4 = _p(3, 15, 0.30, 3.75, 6)
PRICES: dict[str, dict] = {
    "claude-fable-5-1": _p(10, 50, 0.25, 12.50, 20),
    "claude-mythos-5-1": _p(10, 50, 0.25, 12.50, 20),
    "claude-fable-5": _p(10, 50, 1, 12.50, 20),
    "claude-mythos-5": _p(10, 50, 1, 12.50, 20),
    "claude-opus-5-5": _p(4, 20, 0.20, 5, 8),
    "claude-opus-5": dict(_OPUS_STD),
    "claude-opus-4-8": dict(_OPUS_STD),
    "claude-opus-4-7": dict(_OPUS_STD),
    "claude-opus-4-6": dict(_OPUS_STD),
    "claude-opus-4-5": dict(_OPUS_STD),
    "claude-opus-4-1": _p(15, 75, 1.50, 18.75, 30),
    "claude-opus-4": _p(15, 75, 1.50, 18.75, 30),
    "claude-sonnet-5-5": _p(2, 10, 0.10, 2.50, 4),
    "claude-sonnet-5": _p(2, 10, 0.20, 2.50, 4),
    "claude-sonnet-4-6": dict(_SONNET_4),
    "claude-sonnet-4-5": dict(_SONNET_4),
    "claude-sonnet-4": dict(_SONNET_4),
    # Haiku 5.5 is priced by prompt length: over 100,000 prompt tokens (cache
    # reads and writes included) the whole request pays the long rates.
    "claude-haiku-5-5": {**_p(0.10, 0.50, 0.01, 0.125, 0.20),
                         "long_over": 100_000, "long": _p(0.50, 2.50, 0.05, 0.625, 1)},
    "claude-haiku-4-5": _p(1, 5, 0.10, 1.25, 2),
    "claude-3-5-haiku": _p(0.80, 4, 0.08, 1, 1.60),
    # OpenAI (chat fallbacks, reflection, Graphiti, embeddings). OpenAI has no
    # cache-write charge; cached prompt tokens are billed at the cached rate.
    "gpt-5.5": _p(5, 30, 0.50, 0, 0),
    "gpt-5": _p(1.25, 10, 0.125, 0, 0),
    "gpt-5-mini": _p(0.25, 2, 0.025, 0, 0),
    "gpt-4.1": _p(2, 8, 0.50, 0, 0),
    "gpt-4.1-mini": _p(0.40, 1.60, 0.10, 0, 0),
    "gpt-4.1-nano": _p(0.10, 0.40, 0.025, 0, 0),
    "gpt-4o": _p(2.50, 10, 1.25, 0, 0),
    "gpt-4o-mini": _p(0.15, 0.60, 0.075, 0, 0),
    "text-embedding-3-small": _p(0.02, 0, 0, 0, 0),
    "text-embedding-3-large": _p(0.13, 0, 0, 0, 0),
}

_DATE_SUFFIX = re.compile(r"(-\d{8}|@\d{8}|-\d{4}-\d{2}-\d{2}|-v\d+(:\d+)?)$")


def normalize_model(name: str | None) -> str:
    """Price-table key for a model id as callers or providers spell it."""
    n = (name or "").strip().lower()
    if "/" in n:
        n = n.rsplit("/", 1)[-1]
    for prefix in ("global.anthropic.", "us.anthropic.", "eu.anthropic.", "anthropic."):
        if n.startswith(prefix):
            n = n[len(prefix):]
    n = n.replace("[1m]", "")
    return _DATE_SUFFIX.sub("", n)


_override_cache: tuple[str, dict] = ("", {})


def _overrides() -> dict:
    """llm_prices_json: {"model": {"input": .., "output": .., "cache_read": .., "cache_write": ..}}."""
    global _override_cache
    try:
        from .config import get_settings
        raw = (getattr(get_settings(), "llm_prices_json", "") or "").strip()
    except Exception:
        return {}
    if raw == _override_cache[0]:
        return _override_cache[1]
    parsed: dict = {}
    if raw:
        try:
            data = json.loads(raw)
            for k, v in (data.items() if isinstance(data, dict) else []):
                if isinstance(v, dict):
                    parsed[normalize_model(k)] = {
                        f: float(v[f]) for f in
                        ("input", "output", "cache_read", "cache_write", "cache_write_1h")
                        if isinstance(v.get(f), (int, float))}
        except (ValueError, TypeError):
            log.warning("llm_prices_json is not valid JSON; using the built-in price table")
    _override_cache = (raw, parsed)
    return parsed


def price_for(model: str | None) -> dict | None:
    key = normalize_model(model)
    base = PRICES.get(key)
    over = _overrides().get(key)
    if over:
        merged = dict(base or {"input": 0.0, "output": 0.0, "cache_read": 0.0,
                               "cache_write": 0.0, "cache_write_1h": 0.0})
        merged.update(over)
        merged.pop("long", None)  # an override replaces the tiered rates
        if "cache_write_1h" not in over and "cache_write" in over:
            merged["cache_write_1h"] = over["cache_write"]
        return merged
    return base


def cost_usd(model: str | None, *, input: int = 0, output: int = 0, cache_read: int = 0,
             cache_write: int = 0, cache_write_1h: int = 0, web_searches: int = 0) -> float | None:
    """Estimated cost at list prices; None when the model is not in the table.
    `input` is UNCACHED input (Anthropic's input_tokens); cache_write includes
    any 1-hour writes, of which cache_write_1h is the 1-hour part."""
    p = price_for(model)
    if p is None:
        return None
    if p.get("long") and (input + cache_read + cache_write) > p.get("long_over", 0):
        p = p["long"]
    w1h = max(0, min(cache_write_1h, cache_write))
    usd = (input * p["input"] + output * p["output"] + cache_read * p["cache_read"]
           + (cache_write - w1h) * p["cache_write"] + w1h * p["cache_write_1h"]) / 1_000_000
    return round(usd + web_searches * WEB_SEARCH_USD, 9)  # one embedding call is ~1e-7 USD


# ── attribution ───────────────────────────────────────────────────────────

FEATURES: dict[str, str] = {
    "chat": "Chat answer",
    "dashboard": "Dashboard build/edit",
    "watch_compile": "Watch compile",
    "watch_investigation": "Watch investigation",
    "exploration": "Overnight exploration / playbook",
    "context_pack": "Context-pack import",
    "interview": "Interview",
    "triage": "Triage",
    "embeddings": "Embeddings",
    "other": "Other",
}
# A chat run that delegated to one of these counts as that feature as a whole.
RUN_LEVEL_FEATURES = ("dashboard", "context_pack")


@dataclass(frozen=True)
class Attribution:
    feature: str | None = None
    agent: str | None = None
    thread_id: str | None = None
    run_id: int | None = None
    task_id: str | None = None
    trace_id: str | None = None


_attr: ContextVar[Attribution] = ContextVar("usage_attribution", default=Attribution())


@contextlib.contextmanager
def attribute(**changes):
    """Scoped attribution: changed fields apply to every model call inside."""
    token = _attr.set(replace(_attr.get(), **changes))
    try:
        yield
    finally:
        _attr.reset(token)


def set_attribution(**fields) -> None:
    """Replace the whole attribution for the rest of this task (worker loops
    reuse one asyncio task per consumer, so a task must start from a clean slate)."""
    _attr.set(Attribution(**fields))


def new_trace(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex[:12]}"


def current() -> Attribution:
    """The attribution a call made right now would get (run/thread fallbacks applied)."""
    a = _attr.get()
    run_id, thread_id = a.run_id, a.thread_id
    g = sys.modules.get("app.agents.graph")
    if g is not None:
        try:
            if run_id is None:
                run_id = g.ctx_run_id.get()
            if thread_id is None:
                thread_id = g.ctx_phone.get()
        except Exception:
            pass
    trace = a.trace_id or (f"run:{run_id}" if run_id is not None else None)
    return replace(a, run_id=run_id, thread_id=thread_id, trace_id=trace)


def install_label() -> str:
    try:
        from .config import get_settings
        name = (getattr(get_settings(), "install_name", "") or "").strip()
    except Exception:
        name = ""
    return (name or os.environ.get("IC_STACK", "").strip() or "default")[:60]


# ── recording ─────────────────────────────────────────────────────────────

@dataclass
class Row:
    ts: datetime
    install: str
    provider: str
    model: str
    agent: str | None
    feature: str
    thread_id: str | None
    run_id: int | None
    task_id: str | None
    trace_id: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    web_searches: int = 0
    latency_ms: int | None = None
    cost_usd: float | None = None
    error: str | None = None


MAX_PENDING = 20_000
_pending: collections.deque = collections.deque()
_dropped = 0
_run_marks: "collections.OrderedDict[int, str]" = collections.OrderedDict()


def _int(v) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def record(*, model: str | None, provider: str = "anthropic", role: str | None = None,
           feature: str | None = None, prefer_role: bool = False,
           input: int = 0, output: int = 0, cache_read: int = 0, cache_write: int = 0,
           cache_write_1h: int = 0, web_searches: int = 0,
           latency_ms: int | None = None, error: str | None = None,
           trace: str | None = None) -> Row | None:
    """Queue one ledger row. Never raises. `role` is the capture point's own
    agent name; with prefer_role it wins over the contextvar agent (direct SDK
    clients name a specific job, e.g. research inside a supervisor turn)."""
    global _dropped
    try:
        a = current()
        if trace:  # a client bound to one piece of work (an interview session)
            a = replace(a, task_id=trace, trace_id=trace)
        agent = (role if prefer_role and role else (a.agent or role)) or None
        feat = feature or a.feature or "other"
        if feat not in FEATURES:
            feat = "other"
        inp, out_, cr, cw = _int(input), _int(output), _int(cache_read), _int(cache_write)
        web = _int(web_searches)
        row = Row(
            ts=datetime.now(timezone.utc), install=install_label(), provider=provider,
            model=(model or "unknown")[:120], agent=(agent or None) and agent[:60],
            feature=feat, thread_id=(a.thread_id or None) and str(a.thread_id)[:200],
            run_id=a.run_id, task_id=(a.task_id or None) and str(a.task_id)[:120],
            trace_id=(a.trace_id or None) and str(a.trace_id)[:120],
            input_tokens=inp, output_tokens=out_, cache_read_tokens=cr,
            cache_write_tokens=cw, web_searches=web,
            latency_ms=None if latency_ms is None else max(0, int(latency_ms)),
            cost_usd=None if error and not (inp or out_ or cr or cw) else cost_usd(
                model, input=inp, output=out_, cache_read=cr, cache_write=cw,
                cache_write_1h=_int(cache_write_1h), web_searches=web),
            error=(error or None) and str(error)[:300],
        )
        if len(_pending) >= MAX_PENDING:
            _pending.popleft()
            _dropped += 1
            if _dropped in (1, 100) or _dropped % 1000 == 0:
                log.warning("usage ledger queue full: %d rows dropped", _dropped)
        _pending.append(row)
        return row
    except Exception:
        log.debug("usage record failed", exc_info=True)
        return None


def pending_rows() -> list[Row]:
    """Rows not yet written (tests and the budget's not-yet-flushed spend)."""
    return list(_pending)


def clear_pending() -> None:
    _pending.clear()


def _anthropic_usage(u) -> dict:
    """Provider usage object or dict -> ledger fields (input = uncached input)."""
    get = (lambda k: u.get(k)) if isinstance(u, dict) else (lambda k: getattr(u, k, None))
    cc = get("cache_creation")
    w1h = 0
    if cc is not None:
        w1h = _int(cc.get("ephemeral_1h_input_tokens") if isinstance(cc, dict)
                   else getattr(cc, "ephemeral_1h_input_tokens", 0))
    stu = get("server_tool_use")
    web = 0
    if stu is not None:
        web = _int(stu.get("web_search_requests") if isinstance(stu, dict)
                   else getattr(stu, "web_search_requests", 0))
    return {"input": _int(get("input_tokens")), "output": _int(get("output_tokens")),
            "cache_read": _int(get("cache_read_input_tokens")),
            "cache_write": _int(get("cache_creation_input_tokens")),
            "cache_write_1h": w1h, "web_searches": web}


def _openai_usage(u) -> dict:
    get = (lambda k: u.get(k)) if isinstance(u, dict) else (lambda k: getattr(u, k, None))
    prompt = get("prompt_tokens")
    if prompt is None:            # responses API
        prompt = get("input_tokens")
        details = get("input_tokens_details")
        out = get("output_tokens")
    else:
        details = get("prompt_tokens_details")
        out = get("completion_tokens")
    cached = 0
    if details is not None:
        cached = _int(details.get("cached_tokens") if isinstance(details, dict)
                      else getattr(details, "cached_tokens", 0))
    prompt = _int(prompt)
    return {"input": max(0, prompt - cached), "output": _int(out), "cache_read": cached,
            "cache_write": 0, "cache_write_1h": 0, "web_searches": 0}


# ── LangChain capture ─────────────────────────────────────────────────────

class UsageCallback(BaseCallbackHandler):
    """Model-level callback: one per model object, bound to its model and role."""

    raise_error = False
    run_inline = True   # sync handler run in the caller's context (contextvars intact)

    def __init__(self, model: str | None = None, role: str | None = None,
                 provider: str = "anthropic"):
        self.model = model
        self.role = role
        self.provider = provider
        self._starts: dict = {}

    def _start(self, run_id) -> None:
        if len(self._starts) > 2000:  # calls cancelled mid-flight never end
            self._starts.clear()
        self._starts[run_id] = time.monotonic()

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs) -> None:
        self._start(run_id)

    def on_llm_start(self, serialized, prompts, *, run_id, **kwargs) -> None:
        self._start(run_id)

    def _latency(self, run_id) -> int | None:
        t0 = self._starts.pop(run_id, None)
        return None if t0 is None else int((time.monotonic() - t0) * 1000)

    def on_llm_end(self, response, *, run_id=None, **kwargs) -> None:
        try:
            fields, model = _langchain_usage(response, self.provider)
            record(model=model or self.model, provider=self.provider, role=self.role,
                   latency_ms=self._latency(run_id), **fields)
        except Exception:
            log.debug("usage callback failed", exc_info=True)

    def on_llm_error(self, error, *, run_id=None, **kwargs) -> None:
        try:
            record(model=self.model, provider=self.provider, role=self.role,
                   latency_ms=self._latency(run_id),
                   error=f"{type(error).__name__}: {error}")
        except Exception:
            log.debug("usage callback failed", exc_info=True)


def _langchain_usage(response, provider: str) -> tuple[dict, str | None]:
    """LLMResult -> ledger fields. Prefers the provider's raw usage when the
    model put it in llm_output; else the message usage_metadata (ChatAnthropic
    sums cache tokens into input_tokens there, so they are subtracted back)."""
    out = getattr(response, "llm_output", None) or {}
    model = out.get("model_name") or out.get("model")
    raw = out.get("usage")
    if isinstance(raw, dict) and ("input_tokens" in raw or "output_tokens" in raw):
        return _anthropic_usage(raw), model
    tu = out.get("token_usage")
    if isinstance(tu, dict) and tu.get("prompt_tokens") is not None:
        return _openai_usage(tu), model
    total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
             "cache_write_1h": 0, "web_searches": 0}
    for gen in (getattr(response, "generations", None) or []):
        for g in gen:
            msg = getattr(g, "message", None)
            meta = getattr(msg, "usage_metadata", None) or {}
            rmeta = getattr(msg, "response_metadata", None) or {}
            model = model or rmeta.get("model_name") or rmeta.get("model")
            if not meta:
                continue
            det = meta.get("input_token_details") or {}
            cr = _int(det.get("cache_read"))
            w5, w1 = _int(det.get("ephemeral_5m_input_tokens")), _int(det.get("ephemeral_1h_input_tokens"))
            cw = (w5 + w1) or _int(det.get("cache_creation"))
            total["input"] += max(0, _int(meta.get("input_tokens")) - cr - cw)
            total["output"] += _int(meta.get("output_tokens"))
            total["cache_read"] += cr
            total["cache_write"] += cw
            total["cache_write_1h"] += w1
    return total, model


# ── direct SDK capture ────────────────────────────────────────────────────

def _timed(fn, on_result, on_error):
    async def wrapper(*args, **kwargs):
        t0 = time.monotonic()
        try:
            result = await fn(*args, **kwargs)
        except Exception as e:
            try:
                on_error(kwargs, e, int((time.monotonic() - t0) * 1000))
            except Exception:
                log.debug("usage error capture failed", exc_info=True)
            raise
        try:
            on_result(kwargs, result, int((time.monotonic() - t0) * 1000))
        except Exception:
            log.debug("usage capture failed", exc_info=True)
        return result
    return wrapper


class _Proxy:
    """Delegates everything to the wrapped SDK object except the hooked attributes."""

    def __init__(self, inner, hooks: dict):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_hooks", hooks)

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        hook = self._hooks.get(name)
        if hook is None:
            return attr
        if isinstance(hook, dict):
            return _Proxy(attr, hook)
        return hook(attr)

    def __setattr__(self, name, value):
        setattr(self._inner, name, value)


def _anthropic_hooks(role, feature, trace=None) -> dict:
    def done(kw, msg, ms):
        u = getattr(msg, "usage", None)
        if u is None or getattr(msg, "__aiter__", None) is not None:
            return  # a stream: no usage on the return value
        record(model=getattr(msg, "model", None) or kw.get("model"), provider="anthropic",
               role=role, feature=feature, prefer_role=True, latency_ms=ms, trace=trace,
               **_anthropic_usage(u))

    def failed(kw, e, ms):
        record(model=kw.get("model"), provider="anthropic", role=role, feature=feature,
               prefer_role=True, latency_ms=ms, trace=trace,
               error=f"{type(e).__name__}: {e}")

    create = lambda fn: _timed(fn, done, failed)  # noqa: E731
    return {"messages": {"create": create}, "beta": {"messages": {"create": create}}}


def _openai_hooks(role, feature) -> dict:
    def done_for(kind):
        def done(kw, resp, ms):
            u = getattr(resp, "usage", None)
            if u is None:
                return
            record(model=getattr(resp, "model", None) or kw.get("model"), provider="openai",
                   role=role or kind, feature="embeddings" if kind == "embeddings" else feature,
                   prefer_role=True, latency_ms=ms, **_openai_usage(u))
        return done

    def failed_for(kind):
        def failed(kw, e, ms):
            record(model=kw.get("model"), provider="openai", role=role or kind,
                   feature="embeddings" if kind == "embeddings" else feature,
                   prefer_role=True, latency_ms=ms, error=f"{type(e).__name__}: {e}")
        return failed

    def hook(kind):
        return lambda fn: _timed(fn, done_for(kind), failed_for(kind))
    return {
        "embeddings": {"create": hook("embeddings")},
        "chat": {"completions": {"create": hook(None), "parse": hook(None)}},
        "responses": {"create": hook(None), "parse": hook(None)},
        "beta": {"chat": {"completions": {"parse": hook(None)}}},
    }


def wrap_anthropic(client, *, role: str | None = None, feature: str | None = None,
                   trace: str | None = None):
    """Anthropic SDK client whose messages.create calls are recorded. `trace`
    groups every call of this client (task_id and trace_id), e.g. 'interview:7'."""
    if client is None or isinstance(client, _Proxy):
        return client
    return _Proxy(client, _anthropic_hooks(role, feature, trace))


def wrap_openai(client, *, role: str | None = None, feature: str | None = None):
    """OpenAI SDK client whose embeddings / chat / responses calls are recorded.
    Embedding calls are always feature 'embeddings'."""
    if client is None or isinstance(client, _Proxy):
        return client
    return _Proxy(client, _openai_hooks(role, feature))


# ── writer ────────────────────────────────────────────────────────────────

FLUSH_SECONDS = 2.0
BUDGET_CHECK_SECONDS = 60
BATCH = 500

INSERT_SQL = """INSERT INTO llm_usage (ts, install, provider, model, agent, feature, thread_id,
    run_id, task_id, trace_id, input_tokens, output_tokens, cache_read_tokens,
    cache_write_tokens, web_searches, latency_ms, cost_usd, error)
    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)"""


def _mark_runs(batch: list[Row]) -> dict[int, str]:
    """Run-level feature: a run that used the dashboard or context specialist
    counts as that feature as a whole, including the supervisor turns around it."""
    newly: dict[int, str] = {}
    for r in batch:
        if r.run_id is not None and r.feature in RUN_LEVEL_FEATURES and r.run_id not in _run_marks:
            _run_marks[r.run_id] = r.feature
            newly[r.run_id] = r.feature
    while len(_run_marks) > 1024:
        _run_marks.popitem(last=False)
    for r in batch:
        if r.feature == "chat" and r.run_id in _run_marks:
            r.feature = _run_marks[r.run_id]
    return newly


async def flush(pool=None) -> int:
    """Write queued rows. Returns rows written; on failure the batch is put back."""
    if not _pending:
        return 0
    batch: list[Row] = []
    while _pending and len(batch) < BATCH:
        batch.append(_pending.popleft())
    try:
        if pool is None:
            from .db import get_pool
            pool = await get_pool()
        newly = _mark_runs(batch)
        await pool.executemany(INSERT_SQL, [(
            r.ts, r.install, r.provider, r.model, r.agent, r.feature, r.thread_id, r.run_id,
            r.task_id, r.trace_id, r.input_tokens, r.output_tokens, r.cache_read_tokens,
            r.cache_write_tokens, r.web_searches, r.latency_ms, r.cost_usd, r.error)
            for r in batch])
        for run_id, feat in newly.items():
            await pool.execute(
                "UPDATE llm_usage SET feature = $2 WHERE run_id = $1 AND feature = 'chat'",
                run_id, feat)
        return len(batch)
    except Exception:
        # Put the batch back (oldest first) unless the queue has filled meanwhile.
        for r in reversed(batch):
            if len(_pending) < MAX_PENDING:
                _pending.appendleft(r)
        raise


async def run_forever() -> None:
    """Background writer + budget alert. Never dies (worker.py loop convention)."""
    import asyncpg
    log.info("usage ledger writer started")
    last_budget = 0.0
    while True:
        try:
            while _pending:
                if await flush() == 0:
                    break
            if time.monotonic() - last_budget >= BUDGET_CHECK_SECONDS:
                last_budget = time.monotonic()
                await evaluate_budget_alert()
            await asyncio.sleep(FLUSH_SECONDS)
        except (asyncpg.PostgresConnectionError, ConnectionError, OSError) as e:
            log.warning("usage ledger DB connection blip, retrying: %s", type(e).__name__)
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("usage ledger write failed; retrying")
            await asyncio.sleep(10)


async def drain(timeout: float = 5.0) -> None:
    """Best-effort final flush (shutdown)."""
    try:
        async def _all():
            while _pending:
                if await flush() == 0:
                    break
        await asyncio.wait_for(_all(), timeout)
    except Exception:
        log.warning("usage ledger: %d rows not written at shutdown", len(_pending))


# ── daily budget ──────────────────────────────────────────────────────────

BUDGET_COMPONENT = "llm_budget"
WARN_PCT = 80.0
_spent_cache: dict = {"at": 0.0, "day": None, "spent": 0.0}


def _tz():
    from zoneinfo import ZoneInfo
    from .config import get_settings
    name = (get_settings().timezone or "UTC").strip()
    try:
        return ZoneInfo(name), name
    except Exception:
        return ZoneInfo("UTC"), "UTC"


def day_window(now: datetime | None = None) -> tuple[datetime, datetime, str]:
    """(start of today, start of tomorrow) in the configured time zone."""
    tz, name = _tz()
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    start = datetime(now.year, now.month, now.day, tzinfo=tz)
    nxt = start.date() + timedelta(days=1)
    return start, datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz), name


async def spent_today(pool=None, *, max_age: float = 10.0) -> float:
    start, _end, _ = day_window()
    now = time.monotonic()
    if _spent_cache["day"] == start and now - _spent_cache["at"] < max_age:
        db = _spent_cache["spent"]
    else:
        if pool is None:
            from .db import get_pool
            pool = await get_pool()
        db = float(await pool.fetchval(
            "SELECT COALESCE(sum(cost_usd), 0)::float8 FROM llm_usage WHERE ts >= $1", start) or 0)
        _spent_cache.update(at=now, day=start, spent=db)
    queued = sum(r.cost_usd or 0 for r in _pending if r.ts >= start)
    return round(db + queued, 6)


def daily_budget() -> float:
    try:
        from .config import get_settings
        return max(0.0, float(getattr(get_settings(), "daily_llm_budget_usd", 0) or 0))
    except Exception:
        return 0.0


async def budget_state(pool=None) -> dict:
    limit = daily_budget()
    start, end, tzname = day_window()
    spent = await spent_today(pool)
    if limit <= 0:
        return {"limit_usd": None, "spent_usd": spent, "pct": None, "state": "off",
                "resets_at": end.isoformat(), "timezone": tzname}
    pct = round(spent / limit * 100, 1)
    state = "paused" if pct >= 100 else "warn" if pct >= WARN_PCT else "ok"
    return {"limit_usd": limit, "spent_usd": spent, "pct": pct, "state": state,
            "resets_at": end.isoformat(), "timezone": tzname}


def pause_message(state: dict) -> str:
    return (f"Today's AI budget (${state['limit_usd']:.2f}) is used up "
            f"(${state['spent_usd']:.2f} spent). Overnight exploration, playbooks and watch "
            f"investigations are paused until midnight ({state.get('timezone', 'UTC')}); "
            "chat and plain watch checks keep working.")


async def background_pause_reason() -> str | None:
    """Why background AI work must not start now, or None. Fails OPEN: a ledger
    problem must never stop the product, only the budget can."""
    if daily_budget() <= 0:
        return None
    try:
        st = await budget_state()
    except Exception:
        log.warning("budget check failed; not pausing", exc_info=True)
        return None
    return pause_message(st) if st["state"] == "paused" else None


async def evaluate_budget_alert(pool=None) -> str:
    """One owed item (health alert 'llm_budget') while today's spend is at or
    past 80% of the budget; resolved when the day resets or the budget is
    raised or switched off. Returns the state it acted on."""
    if pool is None:
        from .db import get_pool
        pool = await get_pool()
    st = await budget_state(pool)
    if st["state"] in ("warn", "paused"):
        detail = (pause_message(st) if st["state"] == "paused" else
                  f"${st['spent_usd']:.2f} of ${st['limit_usd']:.2f} spent today "
                  f"({st['pct']:.0f}%). At 100% overnight exploration and watch "
                  "investigations pause until midnight; chat keeps working.")
        opened = await pool.fetchval(
            "INSERT INTO health_alerts (component, detail) VALUES ($1, $2) "
            "ON CONFLICT (component) WHERE resolved_at IS NULL DO NOTHING RETURNING id",
            BUDGET_COMPONENT, detail)
        if opened is None:
            await pool.execute(
                "UPDATE health_alerts SET detail = $2 WHERE component = $1 "
                "AND resolved_at IS NULL AND detail IS DISTINCT FROM $2",
                BUDGET_COMPONENT, detail)
        else:
            log.warning("daily AI budget: %s", detail)
    else:
        await pool.execute(
            "UPDATE health_alerts SET resolved_at = now() "
            "WHERE component = $1 AND resolved_at IS NULL", BUDGET_COMPONENT)
    return st["state"]


# ── queries behind /api/usage ─────────────────────────────────────────────

PERIODS = ("today", "7d", "month", "30d")
_TOTALS = """count(*)::int AS calls,
    COALESCE(sum(input_tokens),0)::bigint AS input,
    COALESCE(sum(output_tokens),0)::bigint AS output,
    COALESCE(sum(cache_read_tokens),0)::bigint AS cache_read,
    COALESCE(sum(cache_write_tokens),0)::bigint AS cache_write,
    COALESCE(sum(input_tokens+output_tokens+cache_read_tokens+cache_write_tokens),0)::bigint AS tokens,
    COALESCE(sum(cost_usd),0)::float8 AS cost_usd,
    (count(*) FILTER (WHERE cost_usd IS NULL AND error IS NULL))::int AS unpriced_calls"""


def period_start(period: str, now: datetime | None = None) -> datetime:
    start, _end, _ = day_window(now)
    if period == "today":
        return start
    if period == "7d":
        return start - timedelta(days=6)
    if period == "30d":
        return start - timedelta(days=29)
    return start.replace(day=1)       # month to date


def _totals(row) -> dict:
    d = dict(row) if row is not None else {}
    keys = ("calls", "input", "output", "cache_read", "cache_write", "tokens", "unpriced_calls")
    out = {k: int(d.get(k) or 0) for k in keys}
    out["cost_usd"] = round(float(d.get("cost_usd") or 0), 6)
    return out


async def summary(pool, period: str = "month", install: str | None = None) -> dict:
    if period not in PERIODS:
        period = "month"
    await flush_quietly(pool)
    since = period_start(period)
    today, month = period_start("today"), period_start("month")
    inst_sql = " AND install = $2" if install else ""
    args = (install,) if install else ()

    async def totals(start):
        return _totals(await pool.fetchrow(
            f"SELECT {_TOTALS} FROM llm_usage WHERE ts >= $1{inst_sql}", start, *args))

    async def grouped(expr, limit=50):
        rows = await pool.fetch(
            f"SELECT {expr} AS key, {_TOTALS} FROM llm_usage WHERE ts >= $1{inst_sql} "
            f"GROUP BY 1 ORDER BY cost_usd DESC NULLS LAST, tokens DESC LIMIT {int(limit)}",
            since, *args)
        return [{"key": r["key"], **_totals(r)} for r in rows]

    by_model = await grouped("model")
    for r in by_model:
        r["label"] = r["key"]
    by_feature = await grouped("feature")
    for r in by_feature:
        r["label"] = FEATURES.get(r["key"], r["key"])
    by_agent = await grouped("COALESCE(agent, 'unknown')")
    for r in by_agent:
        r["label"] = r["key"]
    by_chat = await grouped("thread_id", limit=20)
    for r in by_chat:
        r["label"] = r["key"] or "(no chat)"

    top = await pool.fetch(
        f"""SELECT COALESCE(trace_id, 'call:' || id) AS trace, max(run_id) AS run_id,
                   COALESCE(mode() WITHIN GROUP (ORDER BY feature)
                                FILTER (WHERE feature <> 'embeddings'), 'embeddings') AS feature,
                   max(thread_id) AS thread_id, min(ts) AS started_at,
                   count(*)::int AS calls,
                   COALESCE(sum(input_tokens+output_tokens+cache_read_tokens+cache_write_tokens),0)::bigint AS tokens,
                   COALESCE(sum(cost_usd),0)::float8 AS cost_usd,
                   array_agg(DISTINCT model) AS models
            FROM llm_usage WHERE ts >= $1{inst_sql}
            GROUP BY 1 ORDER BY cost_usd DESC, tokens DESC LIMIT 20""", since, *args)
    top_runs = [{"trace": r["trace"], "run_id": r["run_id"], "feature": r["feature"],
                 "feature_label": FEATURES.get(r["feature"], r["feature"]),
                 "thread_id": r["thread_id"], "started_at": r["started_at"],
                 "calls": r["calls"], "tokens": int(r["tokens"]),
                 "cost_usd": round(float(r["cost_usd"]), 6), "models": list(r["models"] or [])}
                for r in top]

    _tzinfo, tzname = _tz()
    day0 = period_start("30d")
    tz_arg = f"${len(args) + 2}"
    daily_rows = await pool.fetch(
        f"""SELECT (ts AT TIME ZONE {tz_arg}::text)::date AS day,
                   COALESCE(sum(input_tokens+output_tokens+cache_read_tokens+cache_write_tokens),0)::bigint AS tokens,
                   COALESCE(sum(cost_usd),0)::float8 AS cost_usd
            FROM llm_usage WHERE ts >= $1{inst_sql} GROUP BY 1 ORDER BY 1""", day0, *args, tzname)
    by_day = {r["day"]: r for r in daily_rows}
    daily = []
    for i in range(30):
        d: date = (day0 + timedelta(days=i)).date()
        r = by_day.get(d)
        daily.append({"day": d.isoformat(), "tokens": int(r["tokens"]) if r else 0,
                      "cost_usd": round(float(r["cost_usd"]), 6) if r else 0.0})

    installs = [r["install"] for r in await pool.fetch(
        "SELECT install FROM llm_usage GROUP BY install ORDER BY max(ts) DESC LIMIT 50")]
    return {
        "price_table_date": PRICE_TABLE_DATE, "timezone": tzname,
        "installs": installs, "install": install, "current_install": install_label(),
        "period": period, "since": since.isoformat(),
        "today": await totals(today), "month": await totals(month),
        "period_totals": await totals(since),
        "budget": await budget_state(pool),
        "by_model": by_model, "by_feature": by_feature, "by_agent": by_agent,
        "by_chat": by_chat, "top_runs": top_runs, "daily": daily,
    }


async def flush_quietly(pool=None) -> None:
    """Write what is queued before a read, so the page shows the call just made."""
    try:
        while _pending:
            if await flush(pool) == 0:
                break
    except Exception:
        log.debug("usage flush before read failed", exc_info=True)


CSV_COLUMNS = ("ts", "install", "provider", "model", "agent", "feature", "thread_id", "run_id",
               "task_id", "trace_id", "input_tokens", "output_tokens", "cache_read_tokens",
               "cache_write_tokens", "web_searches", "latency_ms", "cost_usd", "error")
CSV_MAX_ROWS = 200_000


async def export_csv(pool, period: str = "month", install: str | None = None) -> str:
    if period not in PERIODS:
        period = "month"
    await flush_quietly(pool)
    since = period_start(period)
    inst_sql = " AND install = $2" if install else ""
    args = (install,) if install else ()
    rows = await pool.fetch(
        f"SELECT {', '.join(CSV_COLUMNS[:-2])}, cost_usd::float8 AS cost_usd, error "
        f"FROM llm_usage WHERE ts >= $1{inst_sql} ORDER BY ts LIMIT {CSV_MAX_ROWS}",
        since, *args)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for r in rows:
        cells = [r["ts"].isoformat() if r["ts"] else ""]
        for c in CSV_COLUMNS[1:]:
            v = r[c]
            if c == "cost_usd" and v is not None:
                v = f"{float(v):.9f}".rstrip("0").rstrip(".") or "0"   # no 2e-07 in a spreadsheet
            cells.append("" if v is None else v)
        w.writerow(cells)
    return buf.getvalue()


# ── traces ────────────────────────────────────────────────────────────────

_TOOL_ERROR = re.compile(
    r"^\s*(TOOL ERROR|AGENT ERROR|SQL ERROR|ERROR|GUARDRAIL REJECTED|REFUSED|NOT FOUND)\b[:\s-]*",
    re.IGNORECASE)
_SUMMARY_KEYS = ("sql", "task", "question", "query", "url", "filename", "title", "name",
                 "condition", "code", "message", "text", "fact", "instruction", "person")


def args_summary(args, limit: int = 140) -> str:
    """A short human summary of a tool call's args (the most telling value)."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return " ".join(args.split())[:limit]
    if not isinstance(args, dict) or not args:
        return ""
    for k in _SUMMARY_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())[:limit]
    parts = []
    for k, v in args.items():
        if isinstance(v, (str, int, float, bool)):
            parts.append(f"{k}={' '.join(str(v).split())[:60]}")
    return ", ".join(parts)[:limit]


def tool_error(tool: str, result: str | None) -> str | None:
    if (tool or "").endswith(":refused"):
        return " ".join((result or "refused").split())[:160]
    if result and _TOOL_ERROR.match(result):
        return " ".join(result.split())[:160]
    return None


def _model_event(r) -> dict:
    ms = r["latency_ms"]
    end = r["ts"]
    at = end - timedelta(milliseconds=ms) if (end is not None and ms) else end
    return {"kind": "model", "at": at, "ms": ms, "agent": r["agent"], "model": r["model"],
            "feature": r["feature"], "input": r["input_tokens"], "output": r["output_tokens"],
            "cache_read": r["cache_read_tokens"], "cache_write": r["cache_write_tokens"],
            "tokens": r["input_tokens"] + r["output_tokens"] + r["cache_read_tokens"]
            + r["cache_write_tokens"],
            "cost_usd": None if r["cost_usd"] is None else round(float(r["cost_usd"]), 6),
            "error": r["error"]}


def _tool_event(r) -> dict:
    ms = r["latency_ms"]
    end = r["created_at"]
    at = end - timedelta(milliseconds=ms) if (end is not None and ms) else end
    return {"kind": "tool", "at": at, "ms": ms, "agent": r.get("agent"),
            "name": r["tool"], "summary": args_summary(r["args"]),
            "error": tool_error(r["tool"], r["result_digest"])}


def build_trace(model_rows, tool_rows, *, run_id=None, trace=None) -> dict:
    events = [_model_event(r) for r in model_rows] + [_tool_event(r) for r in tool_rows]
    events.sort(key=lambda e: (e["at"] or datetime.min.replace(tzinfo=timezone.utc),
                               0 if e["kind"] == "tool" else 1))
    models = [e for e in events if e["kind"] == "model"]
    tools = [e for e in events if e["kind"] == "tool"]
    starts = [e["at"] for e in events if e["at"] is not None]
    ends = [e["at"] + timedelta(milliseconds=e["ms"] or 0) for e in events if e["at"] is not None]
    totals = {
        "model_calls": len(models), "tool_calls": len(tools),
        "input": sum(e["input"] for e in models), "output": sum(e["output"] for e in models),
        "cache_read": sum(e["cache_read"] for e in models),
        "cache_write": sum(e["cache_write"] for e in models),
        "tokens": sum(e["tokens"] for e in models),
        "cost_usd": round(sum(e["cost_usd"] or 0 for e in models), 6),
        "unpriced_calls": sum(1 for e in models if e["cost_usd"] is None and not e["error"]),
        "model_ms": sum(e["ms"] or 0 for e in models),
        "tool_ms": sum(e["ms"] or 0 for e in tools),
        "wall_ms": int((max(ends) - min(starts)).total_seconds() * 1000) if starts else 0,
    }
    return {"run_id": run_id, "trace": trace or (f"run:{run_id}" if run_id is not None else None),
            "totals": totals, "events": events}


_MODEL_COLS = ("ts, agent, model, feature, input_tokens, output_tokens, cache_read_tokens, "
               "cache_write_tokens, latency_ms, cost_usd, error")


async def run_trace(pool, run_id: int) -> dict:
    await flush_quietly(pool)
    models = await pool.fetch(
        f"SELECT {_MODEL_COLS} FROM llm_usage WHERE run_id = $1 ORDER BY ts, id LIMIT 2000", run_id)
    tools = await pool.fetch(
        "SELECT tool, args, result_digest, latency_ms, created_at, agent FROM tool_calls "
        "WHERE task_run_id = $1 ORDER BY id LIMIT 2000", run_id)
    return build_trace(models, tools, run_id=run_id)


async def trace_by_key(pool, key: str) -> dict:
    """A trace without a task run (watch compile/investigation, interview,
    triage) or a single call ('call:<id>'); 'run:<id>' is the run trace."""
    await flush_quietly(pool)
    if key.startswith("run:") and key[4:].isdigit():
        return await run_trace(pool, int(key[4:]))
    if key.startswith("call:") and key[5:].isdigit():
        models = await pool.fetch(f"SELECT {_MODEL_COLS} FROM llm_usage WHERE id = $1",
                                  int(key[5:]))
    else:
        models = await pool.fetch(
            f"SELECT {_MODEL_COLS} FROM llm_usage WHERE trace_id = $1 ORDER BY ts, id LIMIT 2000",
            key)
    return build_trace(models, [], trace=key)

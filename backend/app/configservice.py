"""System configuration service — DB-backed overrides editable from the dashboard.

Precedence: system_configs table > .env / environment > code defaults.

Mechanism: overrides are applied to os.environ and `get_settings()`'s lru_cache is
cleared, so every module that calls `get_settings()` picks up new values on its
next call — no restart. Long-lived resources (business-DB pool, embedding client)
are explicitly reset when a key in their group changes.

The app must boot with ZERO configuration: every connection group degrades to a
`not_configured` status and the dashboard Setup page is where wiring happens.
"""

import asyncio
import logging
import os
import re
import time

from pydantic import TypeAdapter, ValidationError

from .config import UI_PROFILES, Settings, get_settings
from .db import get_pool
from . import secretbox  # Phase 11: GOLIVE-02
from . import llmclient  # Phase 13: GOLIVE-01

log = logging.getLogger("configservice")

# ── Registry: every dashboard-editable key ──────────────────────────────
# group: card on the Setup page. secret: masked in UI + stored with secret=true.
# required: group counts as "configured" only when all required keys are set.

REGISTRY: dict[str, dict] = {
    # LLM
    "anthropic_api_key":    {"group": "llm", "secret": True,  "required": True,
                             "label": "Anthropic API key"},
    "openai_api_key":       {"group": "llm", "secret": True,  "required": False,
                             "label": "OpenAI API key (embeddings / fallback)"},
    "supervisor_model":     {"group": "llm", "secret": False, "required": False,
                             "label": "Supervisor model"},
    "triage_model":         {"group": "llm", "secret": False, "required": False,
                             "label": "Triage model (cheap gate: triage, pollers, layer grouping)"},
    "reflection_model":     {"group": "llm", "secret": False, "required": False,
                             "label": "Case-distillation model (memory pipeline; OpenAI models only)"},
    "document_model":       {"group": "llm", "secret": False, "required": False,
                             "label": "Document extraction model"},
    "research_model":       {"group": "llm", "secret": False, "required": False,
                             "label": "Web research model (runs the search/fetch tools; keep on a current model)"},
    # Business data (agent SQL target; read-only enforced by guardrails)
    "tenant_database_url":  {"group": "business_db", "secret": True, "required": True,
                             "label": "Business DB URL (Postgres, read-only)"},
    "business_database_url": {"group": "business_db", "secret": True, "required": False,
                              "label": "Knowledge-bank DB URL (optional fallback)"},
    "knowledge_score_floor": {"group": "tuning", "secret": False, "required": False,
                              "label": "Business-context search: minimum hit score 0-1 (lower-scoring hits dropped)"},
    # WhatsApp connector
    "wa_api_base":          {"group": "whatsapp", "secret": False, "required": True,
                             "label": "Connector base URL"},
    "wa_api_key":           {"group": "whatsapp", "secret": True,  "required": True,
                             "label": "Connector API key"},
    "allowed_numbers":      {"group": "whatsapp", "secret": False, "required": False,
                             "label": "Allowed numbers (comma-separated phone numbers or private IDs; empty = owner only; the owner number is always allowed)"},
    "whatsapp_open_to_anyone": {"group": "whatsapp", "secret": False, "required": False,
                             "label": "Let ANYONE who messages this WhatsApp number use the assistant (true/false; default false = only the owner and Allowed numbers)"},
    "owner_number":         {"group": "whatsapp", "secret": False, "required": False,
                             "label": "Owner number (digests/escalations)"},
    "company_name":         {"group": "whatsapp", "secret": False, "required": False,
                             "label": "Owner company name (orients invoice/case direction)"},
    # Memory graph
    "memory_graph_enabled": {"group": "memory_graph", "secret": False, "required": False,
                             "label": "Knowledge graph enabled (true/false)"},
    "falkordb_host":        {"group": "memory_graph", "secret": False, "required": False,
                             "label": "FalkorDB host"},
    "falkordb_port":        {"group": "memory_graph", "secret": False, "required": False,
                             "label": "FalkorDB port"},
    "graph_search_recipe":  {"group": "memory_graph", "secret": False, "required": False,
                             "label": "Graph search recipe (mmr/rrf/cross_encoder)"},
    # Quick 009: graph extraction LLM (reuses the OpenAI / Anthropic keys)
    "graphiti_llm":         {"group": "memory_graph", "secret": False, "required": False,
                             "label": "Graph extraction LLM (auto = OpenAI, or anthropic)"},
    "graphiti_openai_model": {"group": "memory_graph", "secret": False, "required": False,
                              "label": "Graph extraction model (OpenAI)"},
    "graphiti_openai_small_model": {"group": "memory_graph", "secret": False, "required": False,
                                    "label": "Graph extraction small model: dedupe and contradiction checks (OpenAI)"},
    # Tracing
    "langfuse_public_key":  {"group": "tracing", "secret": False, "required": False,
                             "label": "Langfuse public key"},
    "langfuse_secret_key":  {"group": "tracing", "secret": True,  "required": False,
                             "label": "Langfuse secret key"},
    "langfuse_host":        {"group": "tracing", "secret": False, "required": False,
                             "label": "Langfuse host"},
    # Tuning
    # NB: max_agent_iterations is deliberately NOT here. Its only consumer was
    # the retired agents/supervisor.py; the live bound is each agent's
    # recursion_limit in agents/graph.py. Exposing it printed "applied live"
    # over a value that changed nothing.
    "worker_concurrency":   {"group": "tuning", "secret": False, "required": False,
                             "label": "Parallel task consumers (needs restart: agentctl restart)"},
    "sql_row_limit":        {"group": "tuning", "secret": False, "required": False,
                             "label": "SQL row limit"},
    "reactive_token_budget": {"group": "tuning", "secret": False, "required": False,
                              "label": "Reactive run token budget (applied per-run, live)"},
    "context_token_budget": {"group": "tuning", "secret": False, "required": False,
                             "label": "Memory context token budget per message (live)"},
    "sandbox_memory":       {"group": "tuning", "secret": False, "required": False,
                             "label": "Sandbox memory per run (e.g. 512m, 1g)"},
    "sandbox_cpus":         {"group": "tuning", "secret": False, "required": False,
                             "label": "Sandbox CPUs per run (e.g. 1, 0.5)"},
    "sandbox_pids":         {"group": "tuning", "secret": False, "required": False,
                             "label": "Sandbox max processes per run (fork-bomb ceiling)"},
    "artifact_retention_days": {"group": "tuning", "secret": False, "required": False,
                                "label": "Keep run artifacts for N days"},
    "artifact_max_gb":      {"group": "tuning", "secret": False, "required": False,
                             "label": "Artifact disk cap in GB (oldest deleted first)"},
    "timezone":             {"group": "tuning", "secret": False, "required": False,
                             "label": "Timezone for scheduled playbooks (IANA, e.g. Asia/Kolkata)"},
    "document_inbox_dir":   {"group": "tuning", "secret": False, "required": False,
                             "label": "Watched inbox folder for document intake"},
    "document_store_dir":   {"group": "tuning", "secret": False, "required": False,
                             "label": "Eager document store folder (content-addressed by sha256)"},
    "case_chaining_enabled": {"group": "tuning", "secret": False, "required": False,
                              "label": "Case chaining on completed close (true/false)"},
    "group_question_mode":  {"group": "tuning", "secret": False, "required": False,
                             "label": "Triage group questions (autonomous / proposed / off)"},
    "test_threads":         {"group": "tuning", "secret": False, "required": False,
                             "label": "Test threads (quarantined: their cases are sandboxed)"},
    "web_blocked_domains":  {"group": "tuning", "secret": False, "required": False,
                             "label": "Blocked web domains, comma-separated bare domains e.g. reddit.com (empty = whole web allowed)"},
    "web_calls_per_run":    {"group": "tuning", "secret": False, "required": False,
                             "label": "Max web_research + read_url tool calls per task run "
                                      "(cost ceiling; one web_research call may run several searches)"},
    "web_fetch_max_tokens": {"group": "tuning", "secret": False, "required": False,
                             "label": "Max tokens kept from one fetched page (longer pages are truncated)"},
    # Nightly consolidation + retention (consolidation.py; applied live — the
    # scheduler re-reads settings every sweep).
    "consolidation_enabled": {"group": "tuning", "secret": False, "required": False,
                              "label": "Nightly consolidation enabled (true/false)"},
    "consolidation_hour":   {"group": "tuning", "secret": False, "required": False,
                             "label": "Consolidation hour, local time 0-23"},
    "retention_days":       {"group": "tuning", "secret": False, "required": False,
                             "label": "Retention window in days (events/tasks/messages)"},
    "retention_dry_run":    {"group": "tuning", "secret": False, "required": False,
                             "label": "Retention dry-run (true = count only, delete nothing)"},
    "stale_case_days":      {"group": "tuning", "secret": False, "required": False,
                             "label": "Days of inactivity before an open case is stale"},
    "consolidation_prune_episodes": {"group": "tuning", "secret": False, "required": False,
                                     "label": "Prune graph episodes older than 6 months (default off; graphiti #1083)"},
}

GROUPS: dict[str, str] = {
    "llm": "LLM",
    "business_db": "Business data",
    "whatsapp": "WhatsApp connector",
    "memory_graph": "Memory graph",
    "tracing": "Tracing (Langfuse)",
    "tuning": "Tuning",
}

# Optional groups still show status but "not configured" is a normal state.
OPTIONAL_GROUPS = {"memory_graph", "tracing", "tuning"}

# ── Phase 1: Intelligence Corner interface group (GRND-03) ──
# `choices` is an allowlist enforced by _validate_value and exposed by
# list_config so the UIs can render a select.
REGISTRY["ui_profile"] = {
    "group": "interface", "secret": False, "required": False,
    "choices": list(UI_PROFILES),
    "label": ("Product profile (intelligence = Intelligence Corner, second_brain = "
              "classic Second Brain; takes effect once the Intelligence Corner UI ships)"),
}
GROUPS["interface"] = "Interface"
OPTIONAL_GROUPS.add("interface")

# ── Phase 8: Intelligence Corner UI settings ──
REGISTRY["dashboards_public_url"] = {
    "group": "interface", "secret": False, "required": False,
    "label": ("Dashboards address (public base URL of the dashboards origin, for example "
              "https://dash.example.com; blank lets the UI guess from its own address)"),
}
REGISTRY["ui_profile"]["label"] = (
    "Product profile (intelligence = Intelligence Corner, second_brain = classic "
    "Second Brain; switching applies on the next page load, no rebuild)")

# ── Phase 10: connectivity probes for the dashboards origin and the context store (DEPLOY-03) ──
GROUPS["dashboards"] = "Dashboards origin"
GROUPS["context_store"] = "Context store"
# No REGISTRY keys: always "configured", the probe itself is the test.
PROBE_GROUPS = frozenset({"dashboards", "context_store"})
# VM host process first (refused instantly inside a container), then the compose
# service name used by the local overlay and the ic.sh stacks.
DASHBOARDS_PROBE_BASES = ("http://127.0.0.1:8091", "http://dashboards")


def dashboards_probe_bases() -> tuple[str, ...]:
    """Quick 044: DASHBOARDS_PROBE_URL (env only) is tried first, for hosts where the
    dashboards origin is neither on loopback nor the compose name `dashboards`
    (Railway: http://ic-dashboards.railway.internal). Unset = the two defaults only."""
    extra = os.getenv("DASHBOARDS_PROBE_URL", "").strip().rstrip("/")
    return ((extra,) if extra else ()) + DASHBOARDS_PROBE_BASES
PROBE_TIMEOUT = 4.0
SELFTEST_PROBE_SLUG = "ic-selftest-probe"
CONTEXT_STORE_TABLES = ("context_sources", "context_tables", "context_columns", "business_context")


def is_optional(group: str) -> bool:
    """Single profile-aware optionality hook; status(), list_config() and selftest all call it.

    The dashboards origin is optional under ui_profile=second_brain: Second Brain
    clients and pre-Phase-9 VMs have no dashboards container. Phase 12's UI-12
    (WhatsApp optional under the intelligence profile) must extend this function
    rather than add a second mechanism.
    """
    if group == "dashboards":
        return get_settings().ui_profile != "intelligence"
    return group in OPTIONAL_GROUPS

# ── Phase 12 (CTX-08) ──
REGISTRY["context_model"] = {"group": "llm", "secret": False, "required": False,
                             "label": "Context specialist model (context-pack import)"}
REGISTRY["context_pack_token_budget"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": "Context-pack import token budget per delegation (live)"}
REGISTRY["context_pack_timeout_seconds"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": "Context-pack import task timeout in seconds (live; never below the task timeout)"}
# ── Quick 003 ──
REGISTRY["task_timeout_seconds"] = {
    "group": "tuning", "secret": False, "required": False, "min": 120, "max": 7200,
    "label": "Task timeout in seconds per chat answer or playbook run (120-7200; applied to the next task, live)"}

# ── Phase 13 (GOLIVE-01): model roles, effort and options ──
MODEL_ROLES = {
    "supervisor_model": ("supervisor_effort", "Supervisor model (Ask, specialists, watch compiler, playbooks)"),
    "onboarding_model": ("onboarding_effort", "Interview model (onboarding)"),
    "context_model": ("context_effort", "Context specialist model (context-pack import)"),
    "document_model": ("document_effort", "Document extraction model"),
    "research_model": ("research_effort", "Web research model (search and fetch tools)"),
    "triage_model": ("triage_effort", "Triage model (triage, pollers, layer naming)"),
    # Quick 012: the Data Agent's "auto" model on the Agents page resolves here
    "data_model": ("data_effort", "Data Agent model (heavy SQL investigations; used when the "
                                  "Agents page says auto)"),
}
for _mk, (_ek, _label) in MODEL_ROLES.items():
    REGISTRY[_mk] = {"group": "llm", "secret": False, "required": False, "label": _label,
                     "options": llmclient.model_options()}
    REGISTRY[_ek] = {"group": "llm", "secret": False, "required": False,
                     "choices": list(llmclient.EFFORT_LEVELS),
                     "label": _label.split(" (")[0].replace(" model", "")
                     + " effort (low to max; ignored by models without effort control)"}

_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._:@/-]{1,100}$")


def _code_default(key: str) -> str:
    if REGISTRY.get(key, {}).get("secret"):
        return ""
    f = Settings.model_fields.get(key)
    if f is None or f.default is None or f.is_required():
        return ""
    return str(f.default)


# ── Quick 004: analytics exports ──
REGISTRY["sql_csv_row_limit"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": "Row cap for CSV exports the analyst makes for Python analysis (live)"}

# ── Quick 005: usage ledger ──
REGISTRY["install_name"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": "Install name on the Usage page and in usage CSV exports, e.g. client-fleet "
             "(blank = the IC_STACK environment variable, else 'default'; live)"}
REGISTRY["daily_llm_budget_usd"] = {
    "group": "tuning", "secret": False, "required": False, "min": 0, "max": 100000,
    "label": "Daily AI budget in US dollars (0 = off). At 80% one owed item; at 100% overnight "
             "exploration, playbooks and watch investigations pause until midnight in the "
             "configured time zone. Chat and plain watch checks keep working (live)"}
REGISTRY["llm_usage_retention_days"] = {
    "group": "tuning", "secret": False, "required": False, "min": 0, "max": 3650,
    "label": "Keep usage-ledger rows for N days (0 = forever; pruned nightly when retention "
             "dry-run is off)"}
REGISTRY["llm_prices_json"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": "Model price overrides as JSON, USD per million tokens, e.g. "
             '{"claude-opus-5-5": {"input": 4, "output": 20, "cache_read": 0.2, "cache_write": 5}} '
             "(blank = built-in list prices of 2026-10-25; live)"}

# ── Quick 006: cases from findings ──
REGISTRY["watch_case_recovery_checks"] = {
    "group": "tuning", "secret": False, "required": False, "min": 1, "max": 50,
    "label": ("Normal checks in a row before a KPI alert case closes itself "
              "(1-50, live)")}
# ── Phase 18 ──
REGISTRY["dashboard_legacy_html"] = {
    "group": "interface", "secret": False, "required": False,
    "label": ("Render old HTML dashboards that run agent-written JavaScript (true/false, "
              "default false, applied live). While on, residual T-12-51 applies; new "
              "versions are chart specs either way.")}
# ── Phase 13.1: forecast definitions ──
REGISTRY["forecast_hour"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": "Nightly forecast hour, local time 0-23 (applied live)"}

# ── Quick 012: heavy SQL goes to the Data Agent ──
REGISTRY["delegate_heavy_sql"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": ("Send heavy SQL investigations (many queries, large tables, reconciliations) "
              "to the Data Agent; quick lookups stay with the supervisor (true/false; "
              "applies to the next message, live)")}
REGISTRY["data_token_budget"] = {
    "group": "tuning", "secret": False, "required": False, "min": 20000, "max": 5000000,
    "label": ("Data Agent token budget per delegation (its own cap: its tokens do not count "
              "against the reactive run budget; live)")}

# ── Quick 022: the memory model and the opt-in graph extraction model ──
# reflection_model is the memory model (learning merges, case distillation, playbook
# proposals; default claude-haiku-5-5, gpt-4o-mini rolls it back to OpenAI);
# graph_model runs graph extraction only when graphiti_llm is anthropic. The Setup
# Test probes only Claude models, and graph_model only when it is in use.
MODEL_ROLES["reflection_model"] = (
    "reflection_effort", "Memory model (learning merges, case distillation, playbook "
                         "proposals; a gpt-* id runs on OpenAI)")
MODEL_ROLES["graph_model"] = (
    "graph_effort", "Graph extraction model (used only when the graph extraction LLM is "
                    "anthropic)")
for _mk in ("reflection_model", "graph_model"):
    _ek, _label = MODEL_ROLES[_mk]
    REGISTRY[_mk] = {"group": "llm", "secret": False, "required": False, "label": _label,
                     "options": llmclient.model_options()}
    REGISTRY[_ek] = {"group": "llm", "secret": False, "required": False,
                     "choices": list(llmclient.EFFORT_LEVELS),
                     "label": _label.split(" (")[0].replace(" model", "")
                     + " effort (low to max; ignored by models without effort control)"}
REGISTRY["graphiti_llm"]["label"] = (
    "Graph extraction LLM (auto or openai = the OpenAI models below, default; "
    "anthropic = the graph extraction model)")

# ── Quick 038: graph ingestion and reflection gates, community rebuild schedule ──
# Tuning, not memory_graph: saving these must not reset the graph connection.
from typing import get_args as _q038_get_args  # noqa: E402

REGISTRY["graph_skip_low_value_turns"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": ("Keep facts from dashboard build and tool-only turns out of the knowledge "
              "graph; they are still stored and recalled as learnings; playbooks and investigations, which search only the graph, then miss them (true/false, default "
              "false, live)")}
REGISTRY["reflection_skip_trivial"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": ("Skip reflection on a turn with no tool calls that is only a greeting, thanks "
              "or ok; hard runs always reflect (true/false, default true, live)")}
REGISTRY["graph_community_rebuild"] = {
    "group": "tuning", "secret": False, "required": False,
    "choices": list(_q038_get_args(Settings.model_fields["graph_community_rebuild"].annotation)),
    "label": ("Graph community rebuild in the nightly job: off (default; nothing reads "
              "communities), weekly (Sunday) or nightly")}
# ── End Quick 038 ──
# ── Quick 039: budgets by cost, triage retry cap ──
REGISTRY["budget_weighting"] = {
    "group": "tuning", "secret": False, "required": False, "choices": ["cost", "raw"],
    "label": ("How run token budgets count: cost = cache reads at their price (0.05 of "
              "input on Opus 5.5), writes and output at theirs; raw = every token in full, "
              "the old count (applies to the next run)")}
REGISTRY["triage_backoff_max_seconds"] = {
    "group": "tuning", "secret": False, "required": False, "min": 10, "max": 86400,
    "label": ("Longest wait between triage retries after model errors; the wait doubles "
              "from the triage batch interval (seconds, live)")}
REGISTRY["triage_max_attempts"] = {
    "group": "tuning", "secret": False, "required": False, "min": 1, "max": 100,
    "label": ("Failed model calls before triage stops retrying a batch; its messages are "
              "marked failed and retried once after the model answers again (live)")}
REGISTRY["triage_spend_pause_minutes"] = {
    "group": "tuning", "secret": False, "required": False, "min": 1, "max": 1440,
    "label": ("Minutes triage pauses its model calls after an API spend-limit error "
              "before trying again (live)")}
# ── End Quick 039 ──

# In-memory last test result per group: {group: {"ok": bool, "detail": str, "at": ts}}
_last_tests: dict[str, dict] = {}

# Phase 11: GOLIVE-02. Secret keys whose stored ciphertext cannot be decrypted
# (wrong key, tampered, copied). Skipped at load, flagged in list_config.
_undecryptable: set[str] = set()


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "•" * len(value)
    return "•" * 8 + value[-4:]


# ── Load & save ─────────────────────────────────────────────────────────


async def load_overrides() -> int:
    """Apply DB overrides to the environment at boot. Returns count applied."""
    pool = await get_pool()
    rows = await pool.fetch("SELECT key, value FROM system_configs")
    n = 0
    migrated = plaintext_left = 0  # Phase 11: GOLIVE-02
    for r in rows:
        if r["key"] in REGISTRY:
            # Phase 11: GOLIVE-02 (decrypt, and migrate plaintext in place once)
            value = r["value"]
            if REGISTRY[r["key"]]["secret"]:
                if secretbox.is_encrypted(value):
                    try:
                        value = secretbox.decrypt(r["key"], value)
                        _undecryptable.discard(r["key"])
                    except (secretbox.Undecryptable, secretbox.KeyMissing) as e:
                        _undecryptable.add(r["key"])
                        log.warning("secret setting %s cannot be decrypted: %s",
                                    r["key"], e)
                        continue
                elif value and secretbox.key_available():
                    # Compare-and-swap: a concurrent boot cannot double-migrate.
                    await pool.execute(
                        "UPDATE system_configs SET value=$2, secret=true, "
                        "updated_at=now() WHERE key=$1 AND value=$3",
                        r["key"], secretbox.encrypt(r["key"], value), value)
                    migrated += 1
                elif value:
                    plaintext_left += 1
            os.environ[r["key"].upper()] = value
            n += 1
    # Phase 11: GOLIVE-02
    if migrated:
        log.info("encrypted %s plaintext secret setting(s) at rest", migrated)
    if plaintext_left:
        log.warning("%s secret setting(s) still stored in plaintext: "
                    "SETTINGS_ENCRYPTION_KEY is not set", plaintext_left)
    if n:
        get_settings.cache_clear()
        log.info("applied %s config override(s) from DB", n)
    secretbox.refresh_known_secrets()  # Phase 11: GOLIVE-02
    return n


def _validate_value(key: str, val: str) -> None:
    """Reject a value that would not parse as its Settings field type.

    This MUST run before anything touches os.environ or the DB: a bad value
    (e.g. "abc" for worker_concurrency) would otherwise make get_settings()
    raise ValidationError on every request — including the ops-key auth
    dependency guarding PUT /api/config — leaving no API path to fix it, and
    a restart re-applies the same override from the DB.
    Keys with a REGISTRY ``choices`` list must also match one of the choices.
    """
    field = Settings.model_fields[key]
    try:
        TypeAdapter(field.annotation).validate_python(val)
    except ValidationError:
        type_name = getattr(field.annotation, "__name__", str(field.annotation))
        raise ValueError(f"invalid value for {key}: not a valid {type_name}")
    # Phase 1: enforce the REGISTRY choices allowlist (before any env/DB write).
    choices = REGISTRY.get(key, {}).get("choices")
    if choices and val not in choices:
        raise ValueError(f"invalid value for {key}: must be one of {', '.join(choices)}")
    # Phase 13 (GOLIVE-01): model IDs are free text but must look like a model ID.
    if key in MODEL_ROLES and not _MODEL_ID_RE.match(val):
        raise ValueError(f"invalid value for {key}: not a model ID")
    # Quick 003: numeric range for keys that declare one.
    lo, hi = REGISTRY.get(key, {}).get("min"), REGISTRY.get(key, {}).get("max")
    if (lo is not None or hi is not None) and val != "":
        n = float(val)
        if (lo is not None and n < lo) or (hi is not None and n > hi):
            raise ValueError(f"invalid value for {key}: must be between {lo} and {hi}")
    # Quick 005: price overrides must be a JSON object of model -> rates.
    if key == "llm_prices_json" and val:
        import json as _json
        try:
            data = _json.loads(val)
        except ValueError:
            raise ValueError("invalid value for llm_prices_json: not valid JSON")
        if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
            raise ValueError('invalid value for llm_prices_json: use {"model": {"input": .., '
                             '"output": .., "cache_read": .., "cache_write": ..}}')


def _as_text(raw) -> str:
    """Quick 027: JSON clients send ints, floats and bools. Settings are stored as text,
    so coerce scalars; None means clear; anything structured is a client error (422)."""
    if raw is None:
        return ""
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, (int, float)):
        return str(raw)
    if isinstance(raw, str):
        return raw.strip()
    raise ValueError("config values must be text, numbers or booleans")


async def save_many(values: dict[str, str]) -> list[str]:
    """Validate, persist, apply to env, reset affected resources. Empty string
    deletes the override (falls back to .env/default)."""
    values = {k: _as_text(v) for k, v in values.items()}
    unknown = [k for k in values if k not in REGISTRY]
    if unknown:
        raise ValueError(f"unknown config keys: {unknown}")
    for key, raw in values.items():
        val = (raw or "").strip()
        if val:  # deletions are always safe
            _validate_value(key, val)
    # Phase 11: GOLIVE-02. Refuse before any write when a secret cannot be encrypted.
    if not secretbox.key_available() and any(
            REGISTRY[k]["secret"] and (v or "").strip() for k, v in values.items()):
        raise ValueError("Secret settings need SETTINGS_ENCRYPTION_KEY in .env "
                         "(install.sh creates it; agentctl update adds it)")

    pool = await get_pool()
    saved, groups = [], set()
    for key, raw in values.items():
        val = (raw or "").strip()
        meta = REGISTRY[key]
        if val == "":
            await pool.execute("DELETE FROM system_configs WHERE key=$1", key)
            os.environ.pop(key.upper(), None)
        else:
            await pool.execute(
                """INSERT INTO system_configs (key, value, secret) VALUES ($1,$2,$3)
                   ON CONFLICT (key) DO UPDATE SET value=$2, updated_at=now()""",
                key, secretbox.encrypt(key, val) if meta["secret"] else val,  # Phase 11
                meta["secret"])
            os.environ[key.upper()] = val
        _undecryptable.discard(key)  # Phase 11: GOLIVE-02
        saved.append(key)
        groups.add(meta["group"])
        # Audit without values (secrets must never hit logs).
        log.info("config %s %s (group %s)", "cleared" if val == "" else "updated",
                 key, meta["group"])

    get_settings.cache_clear()
    secretbox.refresh_known_secrets()  # Phase 11: GOLIVE-02
    await _invalidate(groups)
    _last_tests.update({g: {} for g in groups})  # stale test results
    return saved


async def _invalidate(groups: set[str]) -> None:
    """Reset long-lived resources whose config changed.

    Every cache that bakes a credential or a model name in at construction time
    MUST be listed here, or the dashboard's "saved — applied live" is a lie: the
    card goes green (Test builds a fresh client) while the running agent keeps
    using the old key. Rotating a leaked API key is the case that matters.
    """
    if "business_db" in groups:
        try:
            from . import guardrails
            await guardrails.reset_business_pool()
        except Exception:
            log.exception("business pool reset failed")
    if "llm" in groups:
        try:
            from . import knowledge
            knowledge.reset_clients()
        except Exception:
            log.exception("knowledge client reset failed")
    if "llm" in groups:
        # Cached LangGraph agents hold ChatAnthropic/ChatOpenAI built with the
        # old api_key. They only rebuild when agent_configs changes, which an
        # LLM-group save does not touch.
        try:
            from .agents import graph
            graph.reset_agents()
        except Exception:
            log.exception("agent cache reset failed")
        try:
            from . import sentinel
            sentinel.reset_compiler()
        except Exception:
            log.exception("sentinel compiler reset failed")
    if "memory_graph" in groups or "llm" in groups:
        # A changed host or LLM key must reach the Graphiti driver now, not
        # after the retry backoff (Quick 009: memoryservice retries a failed
        # init on its own, but would keep using the old client while up).
        try:
            from . import memoryservice
            await memoryservice.reset_graph()
        except Exception:
            log.exception("memory graph reset failed")


# ── Listing / status ────────────────────────────────────────────────────


async def list_config() -> dict:
    """Grouped config for the Setup page. Secrets masked; source shown."""
    pool = await get_pool()
    db_keys = {r["key"] for r in await pool.fetch("SELECT key FROM system_configs")}
    s = get_settings()
    out: dict[str, list] = {g: [] for g in GROUPS}
    for key, meta in REGISTRY.items():
        current = str(getattr(s, key, "") or "")
        # Phase 13 (GOLIVE-01): "env" only when the value differs from the code default
        source = ("db" if key in db_keys
                  else "env" if os.environ.get(key.upper())
                  or (current and current != _code_default(key)) else "default")
        out[meta["group"]].append({
            "key": key,
            "label": meta["label"],
            "secret": meta["secret"],
            "required": meta["required"],
            "set": bool(current),
            "value": _mask_for(key, current) if meta["secret"] else current,  # Phase 11
            "source": source,
            "default": _code_default(key),  # Phase 13 (GOLIVE-01)
            **({"options": meta["options"]} if meta.get("options") else {}),  # Phase 13
            **({"choices": meta["choices"]} if meta.get("choices") else {}),  # Phase 1
            **({"undecryptable": True} if key in _undecryptable else {}),  # Phase 11
        })
    # Phase 12 (UI-12): profile-aware optionality
    return {"groups": GROUPS,
            "optional": sorted(g for g in set(GROUPS) | OPTIONAL_GROUPS if is_optional(g)),
            "config": out}


# Phase 11: GOLIVE-02. A libpq URL can carry ?password=, so DB URLs are fully masked.
def _mask_for(key: str, value: str) -> str:
    if value and key.endswith("_database_url"):
        return "•" * 8
    return _mask(value)


def _group_configured(group: str) -> bool:
    if group in PROBE_GROUPS: return True  # Phase 10
    s = get_settings()
    required = [k for k, m in REGISTRY.items() if m["group"] == group and m["required"]]
    if not required:  # groups with no required keys count as configured when any key set
        keys = [k for k, m in REGISTRY.items() if m["group"] == group]
        return any(str(getattr(s, k, "") or "") for k in keys)
    return all(str(getattr(s, k, "") or "") for k in required)


async def status() -> dict:
    """Per-group state for badges: not_configured | untested | ok | failing."""
    out = {}
    for group in GROUPS:
        if not _group_configured(group):
            state, detail = "not_configured", ""
        else:
            last = _last_tests.get(group) or {}
            if not last:
                state, detail = "untested", ""
            else:
                state = "ok" if last.get("ok") else "failing"
                detail = last.get("detail", "")
        out[group] = {"state": state, "detail": detail,
                      "optional": is_optional(group)}  # Phase 12 (UI-12)
    return out


# ── Connection tests (adapted from selftest.py; each returns ok+detail) ──


async def test_group(group: str) -> dict:
    if group not in GROUPS:
        raise ValueError(f"unknown group: {group}")
    fn = {
        "llm": _test_llm,
        "business_db": _test_business_db,
        "whatsapp": _test_whatsapp,
        "memory_graph": _test_memory_graph,
        "tracing": _test_tracing,
        "tuning": _test_tuning,
        "interface": _test_interface,  # Phase 1
        "dashboards": _test_dashboards,  # Phase 10
        "context_store": _test_context_store,  # Phase 10
        "warehouse": _test_warehouse,  # Phase 14
    }[group]
    try:
        ok, detail = await asyncio.wait_for(fn(), timeout=30)
    except asyncio.TimeoutError:
        ok, detail = False, "timed out after 30s"
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    detail = secretbox.redact(detail)  # Phase 11: GOLIVE-02
    _last_tests[group] = {"ok": ok, "detail": detail, "at": time.time()}
    return {"group": group, "ok": ok, "detail": detail}


async def _test_llm() -> tuple[bool, str]:
    s = get_settings()
    if s.anthropic_api_key:
        return await _probe_llm_roles(s)  # Phase 13 (GOLIVE-01)
    if s.openai_api_key:
        from openai import AsyncOpenAI
        await AsyncOpenAI(api_key=s.openai_api_key).models.list()
        return True, "OpenAI ok (no Anthropic key — supervisor uses OpenAI)"
    return False, "no LLM key set"


# ── Phase 13 (GOLIVE-01): per-role probe ──
async def _probe_llm_roles(s) -> tuple[bool, str]:
    client = llmclient.anthropic_async(s.anthropic_api_key, role="setup_test",
                                       feature="other")  # Quick 005
    groups: dict[tuple, list[str]] = {}
    roles = [mk for mk in MODEL_ROLES if getattr(s, mk, None)  # Quick 012: older settings
             and str(getattr(s, mk)).strip().lower().startswith("claude")  # Quick 022
             and (mk != "graph_model" or getattr(s, "graphiti_llm", "") == "anthropic")]
    for mk in roles:
        role = mk[:-len("_model")]
        model = getattr(s, mk)
        eff = llmclient.effort_for(model, llmclient.role_effort(role, s))
        groups.setdefault((model, eff), []).append(role)
    keys = list(groups)

    async def one(model, eff):
        role = groups[(model, eff)][0]
        return await asyncio.wait_for(client.messages.create(**llmclient.messages_kwargs(
            model, role=role, max_tokens=1024, settings=s,
            messages=[{"role": "user", "content": "Reply with OK."}])), 25)

    results = await asyncio.gather(*(one(m, e) for m, e in keys), return_exceptions=True)
    res = dict(zip(keys, results))
    parts, all_ok = [], True
    for mk in roles:
        role = mk[:-len("_model")]
        model = getattr(s, mk)
        eff = llmclient.effort_for(model, llmclient.role_effort(role, s))
        r = res[(model, eff)]
        head = f"{role} {model} ({eff or 'no effort'})"
        if isinstance(r, BaseException):
            all_ok = False
            msg = str(r)
            if "data retention" in msg:
                why = "needs 30-day data retention on the Anthropic account"
            else:
                why = f"{type(r).__name__}: {msg[:160]}"
            parts.append(f"{head} FAILED: {why}")
        else:
            parts.append(f"{head} ok")
    return all_ok, " · ".join(parts)


async def _test_business_db() -> tuple[bool, str]:
    import re

    import asyncpg
    s = get_settings()
    target = s.tenant_database_url or s.business_database_url
    if not target:
        return False, "no business DB URL set"
    url = re.sub(r"^postgresql\+\w+://", "postgresql://", target)
    # Deliberately connect WITHOUT the read-only session flag: this probe must
    # prove the ROLE cannot write (guardrails Layer 0). Runtime queries add the
    # session flag + AST layer on top; a session flag we set ourselves proves
    # nothing about the credentials the client handed us.
    conn = await asyncpg.connect(url, timeout=15)
    try:
        from app import readonlykit  # Phase 11: GOLIVE-03
        return await readonlykit.check_role(conn)  # Phase 11: GOLIVE-03
    finally:
        await conn.close()


async def _test_whatsapp() -> tuple[bool, str]:
    import httpx
    s = get_settings()
    if not s.wa_api_base:
        return False, "no connector base URL set"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{s.wa_api_base}/api/status",
                             headers={"x-api-key": s.wa_api_key})
    ok, msg = r.is_success, f"HTTP {r.status_code}: {r.text[:80]}"
    # Quick 046: connected but nobody is allowed through (default-deny).
    if ok and '"connected":true' in r.text.replace(" ", "")             and not (s.owner_number or "").strip() and not s.allowlist             and s.whatsapp_open_to_anyone is not True:
        return False, ("WhatsApp is connected, but nobody can use WhatsApp until an owner "
                       "number is set (or Allowed numbers are added)")
    return ok, msg


async def _test_memory_graph() -> tuple[bool, str]:
    s = get_settings()
    if not s.memory_graph_enabled:
        return True, "disabled by config"
    try:
        _, w = await asyncio.wait_for(
            asyncio.open_connection(s.falkordb_host, s.falkordb_port), timeout=5)
        w.close()
    except OSError as e:
        return False, f"FalkorDB unreachable: {e}"
    # Quick 009: a reachable port is not a working graph (FalkorDB 6 accepted
    # the socket while Graphiti's index setup failed). Report the real state.
    try:
        from . import memoryservice
        st = await memoryservice.memory_graph.status()
    except Exception as e:  # never let the probe itself fail the Test button
        return False, f"FalkorDB reachable, graph status unknown ({type(e).__name__})"
    where = f"{s.falkordb_host}:{s.falkordb_port}"
    if st["state"] == "connected":
        return True, f"knowledge graph connected at {where} ({st.get('episodes') or 0} episodes)"
    if st["state"] == "empty":
        return True, f"knowledge graph connected at {where}, empty so far"
    return False, f"FalkorDB reachable at {where} but the graph is not connected: {st.get('reason')}"


async def _test_tracing() -> tuple[bool, str]:
    return await check_langfuse(force=True)


# ── Quick 005: Langfuse server/SDK compatibility ──
# The pinned langfuse SDK (4.x) exports traces over OpenTelemetry to
# /api/public/otel/v1/traces, which only Langfuse servers >= 3.63.0 have.
# docker-compose.yml's langfuse/langfuse:2 answers that path with HTTP 404 and
# the SDK drops every span silently, while /api/public/health still says OK.
# So the health probe alone passed a setup that recorded nothing. The verdict
# below gates graph._callbacks: an incompatible server gets no handler.
LANGFUSE_MIN_SERVER = (3, 63, 0)
LANGFUSE_RECHECK_SECONDS = 600
_langfuse_check: dict = {}


def _langfuse_key(s) -> tuple:
    return (s.langfuse_host, s.langfuse_public_key, bool(s.langfuse_secret_key))


def _langfuse_sdk_version() -> str:
    try:
        from importlib.metadata import version
        return version("langfuse")
    except Exception:
        return "unknown"


def langfuse_verdict() -> bool | None:
    """Cached compatibility verdict for the current settings; None = not checked yet."""
    s = get_settings()
    c = _langfuse_check
    if c.get("key") == _langfuse_key(s) and time.monotonic() - c.get("at", 0) < LANGFUSE_RECHECK_SECONDS:
        return c["ok"]
    return None


async def check_langfuse(force: bool = False) -> tuple[bool, str]:
    """Is the configured Langfuse server one the installed SDK can send traces to?"""
    import httpx
    s = get_settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        return False, "no Langfuse keys set (tracing off; System > Runs shows in-app traces)"
    key = _langfuse_key(s)
    c = _langfuse_check
    if (not force and c.get("key") == key
            and time.monotonic() - c.get("at", 0) < LANGFUSE_RECHECK_SECONDS):
        return c["ok"], c["detail"]
    sdk = _langfuse_sdk_version()
    need = ".".join(map(str, LANGFUSE_MIN_SERVER))
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(f"{s.langfuse_host.rstrip('/')}/api/public/health")
        if not r.is_success:
            ok, detail = False, f"Langfuse health HTTP {r.status_code} (tracing off)"
        else:
            ver = str((r.json() or {}).get("version") or "")
            nums = tuple(int(x) for x in re.findall(r"\d+", ver)[:3])
            if not nums:
                ok, detail = False, ("Langfuse server did not report a version; tracing off "
                                     f"(SDK {sdk} needs server {need} or newer)")
            elif nums < LANGFUSE_MIN_SERVER:
                ok, detail = False, (
                    f"Langfuse server {ver} is too old for the installed SDK {sdk}: it needs "
                    f"server {need} or newer (OpenTelemetry ingestion), and this server would "
                    "drop every trace. Tracing is off. Use System > Runs for in-app traces, or "
                    "run Langfuse v3.")
            else:
                ok, detail = True, f"Langfuse {ver} ok (SDK {sdk})"
    except Exception as e:
        ok, detail = False, f"Langfuse unreachable ({type(e).__name__}); tracing off"
    _langfuse_check.update(key=key, at=time.monotonic(), ok=ok, detail=detail)
    return ok, detail


async def _test_tuning() -> tuple[bool, str]:
    s = get_settings()
    detail = (f"row_limit={s.sql_row_limit}, timezone={s.timezone}, "
              f"inbox={s.document_inbox_dir}, concurrency={s.worker_concurrency}")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(s.timezone)
    except Exception:
        return False, f"unknown timezone {s.timezone!r} — use an IANA name (e.g. Asia/Kolkata). {detail}"
    return True, detail + " (concurrency applies on restart)"


# ── Phase 1 ──
async def _test_interface() -> tuple[bool, str]:
    s = get_settings()
    from dotenv import dotenv_values  # pydantic-settings dependency
    raw = (os.environ.get("UI_PROFILE")
           or dotenv_values(".env").get("UI_PROFILE") or "").strip()
    if raw and raw.lower() not in UI_PROFILES:
        return False, (f"UI_PROFILE={raw!r} is not a known profile "
                       f"({', '.join(UI_PROFILES)}); using ui_profile={s.ui_profile}")
    return True, f"ui_profile={s.ui_profile} (takes effect once the Intelligence Corner UI ships)"


# ── Phase 10: probes ──
import re  # noqa: E402

# httpx logs every request URL (query string included) at INFO on logger "httpx".
# The gateway root logger is INFO (main.py) and logbuffer copies records into Live
# logs, so the dashboard view token the probe sends would reach docker logs,
# journald and the in-app buffer. uvicorn.access / nginx masks do not cover this
# logger (Phase 4 WR-01 precedent). This redacts any t/p query value on any httpx
# request line; in practice that only matters for dashboard tokens.
_TOKEN_QUERY_RE = re.compile(r"([?&](?:t|p)=)[^&#\s\"']+")


class RedactTokenQuery(logging.Filter):
    def filter(self, record):
        try:
            msg = record.getMessage()
            if _TOKEN_QUERY_RE.search(msg):
                record.msg = _TOKEN_QUERY_RE.sub(r"\1REDACTED", msg)
                record.args = ()
        except Exception:
            pass
        return True


def install_httpx_token_mask() -> None:
    lg = logging.getLogger("httpx")
    if not any(isinstance(f, RedactTokenQuery) for f in lg.filters):
        lg.addFilter(RedactTokenQuery())


install_httpx_token_mask()


def _probe_client():
    import httpx
    install_httpx_token_mask()
    return httpx.AsyncClient(timeout=PROBE_TIMEOUT, follow_redirects=False)


async def _probe_connect():
    import asyncpg
    # Short-lived connection on purpose, never db.get_pool(): unit tests call
    # test_group under asyncio.run and a global pool would bind to a closed loop.
    url = re.sub(r"^postgresql\+\w+://", "postgresql://", get_settings().memory_database_url)
    return await asyncpg.connect(url, timeout=5)


# ── Phase 18: renderer probe ──
async def _p18_renderer_probe(client, base: str) -> str | None:
    """None when the pinned chart-spec renderer is served and its bytes match the pin."""
    import base64
    import hashlib

    import httpx

    from . import dashboardrender
    name = dashboardrender.CURRENT_RENDERER
    pin = dashboardrender.pin(name)
    stale = (f"{base}: chart library ok; renderer {name} missing or stale on the dashboards "
             "origin (rebuild the dashboards image)")
    try:
        r = await client.get(base + pin["src"])
    except httpx.HTTPError:
        return stale
    if r.status_code != 200:
        return stale
    got = "sha256-" + base64.b64encode(hashlib.sha256(r.content).digest()).decode()
    return None if got == pin["csp_hash"] else stale
# ── End Phase 18 ──


async def _test_dashboards() -> tuple[bool, str]:
    from urllib.parse import quote

    import httpx

    from . import dashboardlint
    lib = dashboardlint.LIB_SRC
    notes: list[str] = []
    base = None
    async with _probe_client() as client:
        bases = dashboards_probe_bases()
        for b in bases:
            try:
                r = await client.get(b + lib)
            except httpx.HTTPError as e:
                notes.append(f"{b}: unreachable ({type(e).__name__})")
                continue
            if r.status_code != 200:
                notes.append(f"{b}: HTTP {r.status_code}")
                continue
            base = b
            break
        if base is None:
            return False, ("dashboards origin unreachable (tried "
                           + ", ".join(bases) + "): " + "; ".join(notes))
        rnote = await _p18_renderer_probe(client, base)  # Phase 18
        if rnote:
            return False, rnote
        row = None
        try:
            conn = await _probe_connect()
            try:
                row = await conn.fetchrow(
                    "SELECT slug, view_token FROM dashboards WHERE current_version_id IS NOT NULL "
                    "ORDER BY updated_at DESC LIMIT 1")
            finally:
                await conn.close()
        except Exception as e:
            return False, f"{base}: chart library ok; could not read dashboards ({type(e).__name__})"
        if row:
            slug = row["slug"]
            try:
                r = await client.get(f"{base}/d/{quote(slug, safe='')}/",
                                     params={"t": row["view_token"]})
            except httpx.HTTPError as e:
                return False, f"{base}: chart library ok; published '{slug}' unreachable ({type(e).__name__})"
            csp = "default-src 'none'" in r.headers.get("content-security-policy", "")
            ok = r.status_code == 200 and csp
            detail = (f"{base}: chart library ok; published '{slug}' HTTP {r.status_code}"
                      + (" with CSP" if csp else " (no CSP)"))
        else:
            try:
                r = await client.get(f"{base}/d/{SELFTEST_PROBE_SLUG}/")
            except httpx.HTTPError as e:
                return False, f"{base}: chart library ok; /d/ unreachable ({type(e).__name__})"
            if r.status_code == 404:
                ok, detail = True, (f"{base}: chart library ok; /d/ reaches the gateway "
                                    "(HTTP 404, no published dashboard yet)")
            elif r.status_code in (502, 503, 504):
                ok, detail = False, (f"{base}: chart library ok; gateway unreachable from the "
                                     f"dashboards origin (HTTP {r.status_code})")
            else:
                ok, detail = False, f"{base}: unexpected HTTP {r.status_code} for /d/"
        public = (get_settings().dashboards_public_url or "").strip().rstrip("/")
        if public:
            try:
                r = await client.get(public + lib)
                detail += f"; public address HTTP {r.status_code}"
            except httpx.HTTPError as e:
                detail += f"; public address not reachable from this host ({type(e).__name__})"
    if ok:
        detail += "; renderer ok"
    return ok, detail


async def _test_context_store() -> tuple[bool, str]:
    # SELECT-only. Never call contextstore.search here: it backfills embeddings
    # (a write) and calls OpenAI.
    try:
        conn = await _probe_connect()
    except Exception as e:
        return False, f"memory DB unreachable ({type(e).__name__})"
    try:
        ext = await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        if ext is None:
            return False, "pgvector extension missing"
        missing = []
        for t in CONTEXT_STORE_TABLES:
            if not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", "public." + t):
                missing.append(t)
        if missing:
            return False, "missing context tables: " + ", ".join(missing)
        n = await conn.fetchval(
            "SELECT count(*) FROM pg_indexes WHERE schemaname = 'public' AND "
            "tablename = 'business_context' AND indexdef ILIKE '%using hnsw%'")
        if not n:
            return False, "no HNSW index on business_context"
        row = await conn.fetchrow(
            "SELECT count(*) FILTER (WHERE status = 'approved') AS approved, "
            "count(*) FILTER (WHERE status = 'approved' AND embedding IS NULL) AS unembedded "
            "FROM business_context")
        detail = (f"pgvector {ext}; {len(CONTEXT_STORE_TABLES)} context tables; HNSW index ok; "
                  f"{row['approved']} approved row(s), {row['unembedded']} awaiting embedding")
        if not (get_settings().openai_api_key or "").strip():
            detail += "; no OpenAI key: keyword search only"
        return True, detail
    finally:
        await conn.close()
# ── Phase 14 (WH-07): sync task timeout ──
REGISTRY["sync_task_timeout_seconds"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": "Data sync task timeout in seconds (live; never below 600)"}
# ── End Phase 14 (WH-07) ──
# ── Phase 15 (SRC-05, SRC-06, SRC-08): sync and freshness settings ──
REGISTRY["sync_sweep_seconds"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": "Data source scheduler sweep interval in seconds (applies on the next sweep)"}
REGISTRY["sync_max_rows_per_run"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": "Most rows one sync run loads per table (applies to the next sync)"}
REGISTRY["sync_max_seconds_per_run"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": "Longest a sync run may take in seconds (applies to the next sync)"}
REGISTRY["sync_chunk_size"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": "Rows per sync chunk (applies to the next sync)"}
REGISTRY["sync_default_interval_minutes"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": "Default minutes between syncs for a new source (applies to the next proposal)"}
REGISTRY["sync_reconcile_hour"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": "Local hour after which the nightly key reconcile runs (applies to the next sync)"}
REGISTRY["sync_reconcile_max_rows"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": "Largest table, in rows, that gets the nightly key reconcile (applies to the next sync)"}
REGISTRY["mssql_live_max_rows"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": "Largest SQL Server table, in rows, that may be live through tds_fdw (applies to the next sync)"}
# Quick 028 (WR-03)
REGISTRY["mysql_live_max_rows"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": "Largest MySQL table, in rows, that may be live through mysql_fdw (applies to the next proposal and link)"}
REGISTRY["csv_source_max_mb"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": "Largest CSV file in MB accepted as a source (applies to the next upload)"}
REGISTRY["sync_probe_timeout_seconds"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": "Seconds a source probe may run (applies to the next probe)"}
# ── End Phase 15 ──
# ── Phase 17 (CONS-04) ──
REGISTRY["playbook_live_query_budget"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": "Most queries one unattended run (exploration and other playbooks) may run on live tables, which read the client's production database (0 = unattended runs read no live tables; applies to the next query)"}
# ── End Phase 17 (CONS-04) ──
# ── Quick 035 ──
REGISTRY["prompt_cache_loops"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": ("Prompt caching for playbooks (including nightly exploration), the watch "
              "compiler and triage (true/false, default true; false sends them uncached "
              "as before; applies to the next run, live)")}
# ── End Quick 035 ──
# ── Quick 037: tool-result caps ──
REGISTRY["model_result_chars"] = {
    "group": "tuning", "secret": False, "required": False, "min": 4000, "max": 60000,
    "label": "Most characters get_model returns: its summary, or one section the agent asks for "
             "(applies to the next call)"}
REGISTRY["sql_result_chars"] = {
    "group": "tuning", "secret": False, "required": False, "min": 2000, "max": 50000,
    "label": "Most characters of rows run_sql shows inline; past it the result says how many rows "
             "there are and how to get the rest (applies to the next query)"}
REGISTRY["search_context_hits"] = {
    "group": "tuning", "secret": False, "required": False, "min": 1, "max": 20,
    "label": "Business-context hits per search_business_context call (applies to the next search)"}
REGISTRY["search_context_hit_chars"] = {
    "group": "tuning", "secret": False, "required": False, "min": 200, "max": 1000,
    "label": "Most characters of each business-context hit; a longer one is cut and says so "
             "(applies to the next search)"}
# ── End Quick 037 ──
# ── Phase 14 (WH-04, D-12): a Setup save of the business DB URL re-links the warehouse before the pool reset. Wraps _invalidate instead of editing it. ──
_prior_invalidate = _invalidate


async def _invalidate(groups: set[str]) -> None:
    """Re-link the warehouse (warehouse mode only), then run the earlier reset.
    Logs status and counts only; the URL and password never reach logs."""
    if "business_db" in groups:
        try:
            from . import warehouse
            if warehouse.warehouse_mode():
                result = await warehouse.relink_now("setup", timeout=20)
                log.info("warehouse relink on Setup save: status=%s tables=%s",
                         result.get("status"), result.get("tables_linked"))
        except Exception as e:
            log.warning("warehouse relink on Setup save failed: %s", type(e).__name__)
    await _prior_invalidate(groups)
# ── End Phase 14 (WH-04) ──
# ── Phase 14 (WH-08, D-21): Warehouse probe group. Configured iff warehouse_mode(); optional otherwise (zero-config and second_brain stacks). Wraps the Phase 10 hooks; defined before the Phase 12 block so its is_optional wrapper chains this one. ──
GROUPS["warehouse"] = "Warehouse"
_p14_prior_group_configured = _group_configured
_p14_prior_is_optional = is_optional


def _group_configured(group: str) -> bool:
    if group == "warehouse":
        from . import warehouse
        return warehouse.warehouse_mode()
    return _p14_prior_group_configured(group)


def is_optional(group: str) -> bool:
    if group == "warehouse":
        from . import warehouse
        return not warehouse.warehouse_mode()
    return _p14_prior_is_optional(group)


# Catalogs and functions guardrails must refuse (WH-06, plan 14-01), schema-qualified where they live.
WAREHOUSE_FORBIDDEN_PROBES = (
    ("pg_user_mappings", "SELECT * FROM pg_catalog.pg_user_mappings"),
    ("user_mapping_options", "SELECT * FROM information_schema.user_mapping_options"),
    ("user_mappings", "SELECT * FROM information_schema.user_mappings"),
    ("_pg_user_mappings", "SELECT * FROM information_schema._pg_user_mappings"),
    ("foreign_server_options", "SELECT * FROM information_schema.foreign_server_options"),
    ("pg_foreign_server", "SELECT * FROM pg_catalog.pg_foreign_server"),
    ("pg_foreign_data_wrapper", "SELECT * FROM pg_catalog.pg_foreign_data_wrapper"),
    ("_pg_foreign_servers", "SELECT * FROM information_schema._pg_foreign_servers"),
    ("_pg_foreign_data_wrappers", "SELECT * FROM information_schema._pg_foreign_data_wrappers"),
    ("foreign_data_wrapper_options",
     "SELECT * FROM information_schema.foreign_data_wrapper_options"),
    ("postgres_fdw_get_connections", "SELECT * FROM postgres_fdw_get_connections()"),
    ("postgres_fdw_disconnect", "SELECT postgres_fdw_disconnect('business_db')"),
    ("postgres_fdw_disconnect_all", "SELECT postgres_fdw_disconnect_all()"),
)


async def _test_warehouse() -> tuple[bool, str]:
    """Fixed phrases and counts only. Opens its own short-lived wh_reader connection and never
    uses guardrails.run_readonly_sql, so it works before a business DB URL is set."""
    from . import guardrails, warehouse
    if not warehouse.warehouse_mode():
        return False, "warehouse not configured"
    try:
        conn = await warehouse.connect("reader", timeout=10)
    except Exception as e:  # noqa: BLE001
        import asyncpg
        if isinstance(e, asyncpg.InvalidPasswordError):
            return False, "warehouse login failed for wh_reader (InvalidPasswordError)"
        return False, f"warehouse unreachable as wh_reader ({type(e).__name__})"
    try:
        ok, why = await warehouse.check_reader(conn)
        if not ok:
            return False, f"wh_reader check failed: {why}"
        for name, sql in WAREHOUSE_FORBIDDEN_PROBES:
            try:
                guardrails.validate_readonly_sql(sql)
            except guardrails.SQLGuardrailError:
                continue
            return False, f"guardrails accepted forbidden catalog or function {name}"
        s = get_settings()
        has_url = bool((s.tenant_database_url or s.business_database_url or "").strip())
        parts = [why]
        if has_url:
            try:
                await conn.fetchval("SELECT n FROM ic_probe.business_db_ping")
            except Exception as e:  # noqa: BLE001
                return False, ("business database unreachable through the warehouse "
                               f"({type(e).__name__})")
            row = await conn.fetchrow(
                "SELECT status, tables_linked, linked_at FROM ic_meta.link_state "
                "WHERE source = 'business_db'")
            if row:
                parts.append(f"link {row['status']}, {row['tables_linked']} tables linked")
                if row["linked_at"]:
                    parts.append(f"last linked {row['linked_at']:%Y-%m-%d %H:%M:%S}")
            else:
                parts.append("link state not recorded yet")
        else:
            parts.append("business database not configured yet")
        names = {r["name"] for r in await conn.fetch(
            "SELECT name FROM pg_available_extensions WHERE name IN ('mysql_fdw','tds_fdw')")}
        parts.append("mysql_fdw " + ("available" if "mysql_fdw" in names else "not installed"))
        parts.append("tds_fdw " + ("available" if "tds_fdw" in names else "not installed"))
        return True, "; ".join(parts)
    finally:
        try:
            await conn.close()
        except Exception:  # noqa: BLE001
            pass
# ── End Phase 14 (WH-08) ──
# ── Quick 036 ──
REGISTRY["dashboard_delegations_per_run"] = {
    "group": "tuning", "secret": False, "required": False, "min": 1, "max": 10,
    "label": ("Most times one request may hand work to the dashboard specialist; later "
              "hand-offs are refused with a fixed text (1-10, default 2, live)")}
REGISTRY["dashboard_preview_rounds"] = {
    "group": "tuning", "secret": False, "required": False, "min": 1, "max": 10,
    "label": ("Most dashboard previews in one request; after that the specialist is told "
              "to save or stop (1-10, default 3, live)")}
REGISTRY["delegate_truncation_guard"] = {
    "group": "tuning", "secret": False, "required": False,
    "choices": ["all", "dashboard", "off"],
    "label": ("Specialists that stop when a response is cut off at its output limit in the "
              "middle of a tool call, instead of retrying: all, dashboard (the dashboard "
              "specialist only) or off (old behaviour: retry). Default all; applies to the "
              "next hand-off (live)")}
# ── End Quick 036 ──
# ── Phase 22 ──
REGISTRY["dashboard_number_locale"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": ("Number format on dashboards, for example en-IN or en-GB (empty = follow the "
              "operator timezone, default empty, applied live)")}
REGISTRY["dashboard_local_days"] = {
    "group": "tuning", "secret": False, "required": False,
    "label": ("Days on dashboards follow the operator timezone (true/false, default true, "
              "applied live)")}
REGISTRY["dashboard_naive_timestamps"] = {
    "group": "tuning", "secret": False, "required": False, "choices": ["local", "utc"],
    "label": ("Old-style timestamps without a time zone are stored as local time or UTC "
              "(local/utc, default local; applies to dashboard versions saved afterwards)")}
REGISTRY["dashboard_snapshot_rows"] = {
    "group": "tuning", "secret": False, "required": False, "min": 1000, "max": 10000,
    "label": "Most rows one dashboard tile may hold (1000-10000, default 10000, applied live)"}
REGISTRY["dashboard_snapshot_bytes"] = {
    "group": "tuning", "secret": False, "required": False, "min": 262144, "max": 2097152,
    "label": ("Most bytes one dashboard tile may hold (262144-2097152, default 2097152, "
              "applied live)")}
REGISTRY["app_public_url"] = {
    "group": "interface", "secret": False, "required": False,
    "label": ("Address of this app for the 'Ask about this number' link on dashboards, for "
              "example https://app.example.com (empty = no link)")}
# ── End Phase 22 ──
# ── Phase 12 (UI-12): WhatsApp is optional under the Intelligence Corner profile. Wraps any earlier is_optional (Phase 10's dashboards rule) instead of replacing it; keep this block last in the module. ──
PROFILE_OPTIONAL_GROUPS = {"intelligence": {"whatsapp"}}
_prior_is_optional = globals().get("is_optional")


def is_optional(group: str) -> bool:
    """Single profile-aware optionality hook used by status(), list_config() and
    (after the Phase 10 merge) selftest. ORs the WhatsApp-under-intelligence rule
    with any earlier is_optional definition (OPTIONAL_GROUPS when there is none)."""
    if group in PROFILE_OPTIONAL_GROUPS.get(get_settings().ui_profile, set()):
        return True
    if _prior_is_optional is not None:
        return bool(_prior_is_optional(group))
    return group in OPTIONAL_GROUPS

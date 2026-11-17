from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Phase 1: allowed operator-UI profiles (first entry is the default).
UI_PROFILES = ("intelligence", "second_brain")


# Phase 22: locales the dashboard renderer formats numbers for (dashsettings keeps an equal tuple).
DASHBOARD_LOCALES = ("en-IN", "en-US", "en-GB", "en-AE", "en-SG", "en-AU", "en-CA")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    supervisor_model: str = "claude-opus-5-5"   # Phase 13 (GOLIVE-01)
    # Quick 022: the memory model (learning merges, case distillation, playbook
    # proposals; reflection itself is the Agents-page "reflection" config). Claude
    # models run on Anthropic, anything else on OpenAI (gpt-4o-mini = the pre-022 path).
    reflection_model: str = "claude-haiku-5-5"
    # Document extraction (docintel eager loop + extract_document tool).
    # Quick 022: Haiku 5.5 matched Sonnet 5.5 field for field on an invoice, a credit
    # note, a phone photo and an agreement; set claude-sonnet-5-5 here to roll back.
    document_model: str = "claude-haiku-5-5"
    # Web research sub-call (web_research / read_url). Kept separate from
    # supervisor_model because the server-tool type strings are model-coupled:
    # an operator picking an older supervisor on the dashboard would otherwise
    # silently take the web tools down with it.
    research_model: str = "claude-sonnet-5-5"   # Phase 13 (GOLIVE-01)
    openai_supervisor_model: str = "gpt-4o"

    # Business knowledge DB (read-only). Legacy env name CAISAAS_DATABASE_URL
    # still accepted for existing deployments.
    business_database_url: str = Field(
        "",
        validation_alias=AliasChoices("business_database_url", "caisaas_database_url"),
    )
    # Tenant business-data DB (read-only) — where the agent's SQL actually runs
    tenant_database_url: str = ""
    # ── Phase 14 (WH-02, WH-03): warehouse; .env only, never in system_configs (D-11) ──
    # URL carries no credentials (for example postgresql://127.0.0.1:5434/warehouse).
    warehouse_url: str = ""
    warehouse_admin_user: str = "wh_admin"
    warehouse_admin_password: str = ""
    wh_owner_password: str = ""
    wh_loader_password: str = ""
    wh_reader_password: str = ""
    warehouse_link_sweep_seconds: int = 600
    # ── End Phase 14 (WH-02, WH-03) ──
    # Minimum similarity score for business-context hits (contextstore);
    # lower-scoring hits are dropped before they reach the agent.
    knowledge_score_floor: float = 0.30

    # Working memory
    memory_database_url: str = "postgresql://agent:agent@localhost:5433/agent_memory"

    # WhatsApp connector
    wa_api_base: str = ""
    wa_api_key: str = ""
    webhook_secret: str = ""
    # Admin/ops key for dashboard + /api endpoints. Falls back to
    # webhook_secret when unset so pre-split deployments keep working, but new
    # installs get distinct keys: the connector should never hold admin power.
    ops_key: str = ""
    # Phase 11: GOLIVE-02. 64 hex chars (AES-256 key), created by install.sh, held in
    # .env only. Never in configservice.REGISTRY, never in the DB or backups.
    settings_encryption_key: str = ""
    allowed_numbers: str = ""  # comma-separated; empty = owner only (quick 046)
    # Quick 046: the only way to let anyone message the business number. Default off.
    whatsapp_open_to_anyone: bool = False

    # Langfuse tracing (optional; create keys at http://localhost:3001)
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://localhost:3001"

    # Limits
    sql_row_limit: int = 500
    # Per-run sandbox ceilings. These bound code the MODEL wrote, so they are a
    # containment boundary rather than a tuning knob: worst case is
    # worker_concurrency sandboxes running at once, so budget accordingly.
    sandbox_memory: str = "384m"
    sandbox_cpus: str = "0.5"
    # Fork-bomb ceiling. A runaway `while True: os.fork()` inside the sandbox
    # would otherwise exhaust host PIDs even though memory and CPU are capped —
    # the one resource the existing limits did not cover.
    sandbox_pids: int = 256
    # Quick 042: when sandbox_url is set, run_python calls the always-on
    # sandbox-runner service (sandbox/server.py) instead of `docker run`, for hosts
    # with no Docker socket (Railway). Empty = the docker path, unchanged. In that
    # mode memory, pids and CPU are set on the SERVICE (SANDBOX_MEMORY, SANDBOX_PIDS),
    # not by the three settings above. Env: SANDBOX_URL, SANDBOX_TOKEN.
    sandbox_url: str = ""
    sandbox_token: str = ""
    # Artifact retention. artifacts/run_<id>/ is the only unbounded term in the
    # system — every run's scratch dir persists, and one CAD deconstruction
    # emits a PDF and a PNG per layer group. Age is the primary rule; the size
    # cap is the backstop age alone cannot provide (forty drawings in a day).
    artifact_retention_days: int = 14
    artifact_max_gb: float = 20.0
    artifact_sweep_seconds: int = 3600
    # Parallel task consumers. Tasks run concurrently ACROSS threads (phones),
    # always serially WITHIN a thread to preserve conversation ordering.
    worker_concurrency: int = 3

    # ── Web access (Anthropic server-side tools) ───────────────────────
    # Egress happens on Anthropic infrastructure — the gateway never opens an
    # outbound socket — so the only lever we have over WHERE it goes is a
    # blocklist sent with the request. Deliberately a blocklist, never an
    # allowlist: an allowlist makes open research useless, and the two are
    # mutually exclusive in the API. Comma-separated BARE domains, no scheme
    # ("example.com", "example.com/blog"); empty means the open web.
    web_blocked_domains: str = ""
    # Combined web_research + read_url calls per task run. The model can loop
    # on a bad search, and each search is billed per-call on top of tokens, so
    # this is a cost ceiling rather than a tuning knob.
    web_calls_per_run: int = 5
    # max_content_tokens for web_fetch. A single long page can otherwise eat
    # the run's whole token budget before the agent gets to answer.
    web_fetch_max_tokens: int = 40000

    # ── Ambient framework ──────────────────────────────────────────────
    # Graphiti temporal knowledge graph on FalkorDB (local Docker).
    falkordb_host: str = "localhost"
    falkordb_port: int = 6379
    # Quick 043: env only (FALKORDB_PASSWORD), deliberately NOT in the configservice
    # registry. Empty = no auth, identical to before. Used by every FalkorDB connection.
    falkordb_password: str = ""
    graphiti_llm: str = "auto"               # auto/openai = OpenAI (reliable); 'anthropic' to opt in
    # Quick 022: the model for graphiti_llm 'anthropic' (opt-in). Haiku 5.5 failed
    # parity with gpt-5.5 (it invalidated true facts; with thinking it lost coverage);
    # Sonnet 5.5 matched gpt-5.5 in one run at about the same cost.
    graph_model: str = "claude-sonnet-5-5"
    # Quick 009: graph extraction models on the OpenAI path, pinned rather than
    # graphiti-core's defaults (its small model gpt-4.1-nano is retired: 404).
    graphiti_openai_model: str = "gpt-5.5"
    graphiti_openai_small_model: str = "gpt-5.5"   # dedupe/contradiction calls: gpt-4o-mini wrongly invalidated true facts on p13
    memory_graph_enabled: bool = True        # master switch; fail-soft if graph down
    # Graph retrieval recipe: 'rrf' (basic hybrid, default), 'mmr' (diverse
    # results), 'cross_encoder' (LLM rerank; falls back to rrf on error).
    # Unknown values degrade to 'rrf' at search time. Default is rrf because
    # graphiti 0.29.2's mmr reranker returns ZERO edges on FalkorDB (candidate
    # embeddings are never hydrated) — with mmr this setting silently blanks
    # all graph retrieval; memoryservice also falls back to rrf on an empty
    # recipe result as a second line of defence.
    graph_search_recipe: str = "rrf"
    # Triage / ambient loops
    triage_model: str = "claude-haiku-5-5"   # cheap gate model; Phase 13 (GOLIVE-01)
    # The onboarding interview runs ONCE per business (and once per group), so
    # capability matters and cost does not — this is the one place the most
    # capable model is unambiguously the right default. Effort is set
    # explicitly by onboarding_effort (default high) through llmclient, because
    # Opus 5.5's API default effort is medium.
    onboarding_model: str = "claude-opus-5-5"   # Phase 13 (GOLIVE-01)
    # Phase 12 (CTX-08): context specialist (context-pack import); Claude 5 safe via llmclient
    context_model: str = "claude-opus-5-5"
    # Quick 012: the Data Agent runs the heavy SQL investigations the supervisor hands
    # it; its "auto" model resolves here (an explicit model on the Agents page wins).
    data_model: str = "claude-sonnet-5-5"
    # Phase 13 (GOLIVE-01): explicit effort per role; llmclient drops it for models without effort control
    supervisor_effort: str = "medium"
    onboarding_effort: str = "high"
    context_effort: str = "medium"
    document_effort: str = "high"
    research_effort: str = "medium"
    triage_effort: str = "low"
    data_effort: str = "medium"   # Quick 012
    reflection_effort: str = "medium"   # Quick 022: every memory call
    graph_effort: str = "medium"        # Quick 022: graph extraction
    # Phase 12 (CTX-08): token cap per context-specialist delegation (the supervisor's own
    # turns stay under reactive_token_budget) and the task timeout for context-pack uploads
    context_pack_token_budget: int = 400_000
    context_pack_timeout_seconds: int = 1800
    # Phase 14 (WH-07): timeout for kind='sync' task_runs (data syncs; the runner arrives in Phase 15)
    sync_task_timeout_seconds: int = 3600
    # Quick 004: run_sql_to_csv exports feed the sandbox (forecasts, tests), never the
    # chat, so they get their own cap; inline run_sql answers keep sql_row_limit.
    sql_csv_row_limit: int = 5000
    # Quick 012: heavy SQL investigations (many queries, large tables, reconciliations)
    # go to the Data Agent; quick lookups stay with the supervisor. Off = the old
    # supervisor prompt, byte for byte. Read per run (graph.effective_configs).
    delegate_heavy_sql: bool = True
    # Quick 012: each Data Agent delegation runs under its own token budget (like the
    # context specialist), so a heavy investigation no longer spends the supervisor's
    # reactive_token_budget; counted like every budget (budget_weighting, Quick 039).
    data_token_budget: int = 400_000
    # ── Phase 15 (SRC-05, SRC-06, SRC-08): sources and sync ──
    # Deploy group: .env only, never in system_configs (deploy plumbing like the Phase 14 warehouse settings)
    sync_image: str = "agentic-sync:latest"
    sync_network: str = ""
    sync_csv_network: str = ""
    sync_warehouse_host: str = "agentic-warehouse"
    sync_warehouse_port: int = 5432
    sync_memory: str = "1g"
    sync_cpus: str = "1.0"
    # Live tuning group (configservice.REGISTRY)
    sync_sweep_seconds: int = 60
    sync_max_rows_per_run: int = 2_000_000
    sync_max_seconds_per_run: int = 1800
    sync_chunk_size: int = 50_000
    sync_default_interval_minutes: int = 60
    sync_reconcile_hour: int = 2
    sync_reconcile_max_rows: int = 200_000
    mssql_live_max_rows: int = 10_000
    # Quick 028 (WR-03): live MySQL tables are read in full per query, so they are capped like SQL Server
    mysql_live_max_rows: int = 10_000
    csv_source_max_mb: int = 100
    sync_probe_timeout_seconds: int = 180
    # ── End Phase 15 ──
    # ── Phase 17 (CONS-04) ──
    playbook_live_query_budget: int = 60
    # ── End Phase 17 (CONS-04) ──
    db_poll_seconds: int = 300               # DB poller: watch tenant tables for new rows
    triage_batch_seconds: int = 10
    expectation_poll_seconds: int = 60       # scheduler sweep for due expectations
    playbook_token_budget: int = 200_000     # hard cap per playbook run
    # Phase 13 (GOLIVE-01): raised from 150_000; an Opus 5.5 dashboard build tripped it (13-06 SUMMARY)
    reactive_token_budget: int = 300_000     # hard cap per reactive (user) run
    # Quick 003: wall-clock cap per worker task (was the TASK_TIMEOUT_SECONDS
    # constant). Read per task, so a change applies to the next one; the worker
    # clamps it to TASK_TIMEOUT_MIN..TASK_TIMEOUT_MAX.
    task_timeout_seconds: int = 600
    context_token_budget: int = 1500         # memory context cap (~4 chars/token)
    # Quick 005: usage ledger (usage.py). install_name labels ledger rows so installs
    # sharing one API key can be told apart (blank = IC_STACK env, else "default").
    # daily_llm_budget_usd 0 = off; at 80% one owed item, at 100% background AI
    # work (playbooks/overnight exploration, watch investigations) pauses until
    # midnight in `timezone`. llm_prices_json overrides or adds price-table rows.
    install_name: str = ""
    daily_llm_budget_usd: float = 0.0
    llm_usage_retention_days: int = 180
    llm_prices_json: str = ""
    # Owner phone that receives digests, escalations, and system notices.
    owner_number: str = ""
    # The business this assistant works for. Orients triage: an invoice ISSUED
    # by this company is a receivable ("collect payment"), one received is a
    # payable ("pay"). Empty = triage orients from context alone.
    company_name: str = ""
    # Watched intake folder for document intelligence (relative to repo root).
    document_inbox_dir: str = "inbox"
    # Content-addressed store for eagerly-ingested attachments (relative to repo
    # root; sibling of the inbox, deliberately untouched by artifact retention).
    document_store_dir: str = "documents"
    # IANA zone used for every scheduled playbook ("daily digest at 09:00" means
    # 09:00 HERE, not UTC). Clients are rarely in UTC; the default only matches
    # the pre-existing behaviour.
    timezone: str = "UTC"
    # Closing a completed case may open ONE follow-on case (parent_case_id link,
    # depth-capped at 3). Kill switch for the chain step; closes never block on it.
    case_chaining_enabled: bool = True
    # Triage may post ONE short question to a case's origin group when a missing
    # critical detail (due date, amount, owner) blocks tracking.
    # autonomous | proposed | off — unknown values degrade to 'proposed'.
    group_question_mode: str = "autonomous"
    # Test threads (comma-separated thread refs, e.g. "test-local"). Events
    # from these threads run the full ambient pipeline, but the cases they
    # open are born sandbox=TRUE and quarantined in SQL: test events can never
    # attach to a real case and real events can never attach to a sandbox one.
    test_threads: str = "test-local"

    # ── Nightly consolidation + retention (Phase C) ────────────────────
    # The nightly job (consolidation.py) merges near-duplicate learnings,
    # rebuilds graph communities (graph_community_rebuild, default off), optionally prunes old graph episodes, and
    # sweeps old operational rows. Dispatched from the expectation scheduler.
    consolidation_enabled: bool = True
    consolidation_hour: int = 3              # local tz, like cron playbook dispatch
    forecast_hour: int = 4                   # Phase 13.1: nightly forecast definitions run at this local hour (0-23)
    retention_days: int = 90                 # window for operational-row sweeps
    # Dry-run DEFAULT ON: retention logs would-delete counts and deletes NOTHING
    # until an operator explicitly flips this off.
    retention_dry_run: bool = True
    stale_case_days: int = 21                # open cases idle this long are stale
    # Quick 006: a watch-alert case closes itself after this many normal checks in a row.
    watch_case_recovery_checks: int = 3
    # Graph episode pruning is default-OFF: graphiti's remove_episode has a known
    # orphan-cleanup bug (graphiti issue #1083).
    consolidation_prune_episodes: bool = False

    # ── Quick 038: graph ingestion and reflection gates, community rebuild schedule ──
    # Facts reflection draws from a dashboard build/revision turn or a tool-only turn
    # (status checks and proposals; memory.graph_skip_reason) stay in learnings and
    # out of the knowledge graph (learnings.graph_skip); remember_fact still graphs.
    graph_skip_low_value_turns: bool = False  # default off at merge review: playbooks and investigate_case search only the graph
    # No reflection for a turn with no tool calls whose message is only a greeting,
    # thanks or ok (memory.is_trivial_turn). A hard run (Quick 009) always reflects.
    reflection_skip_trivial: bool = True
    # The nightly graph community rebuild: off, weekly (Sunday's run) or nightly.
    # Nothing reads communities, and a rebuild writes a model summary per entity.
    graph_community_rebuild: Literal["off", "weekly", "nightly"] = "off"

    @field_validator("graph_community_rebuild", mode="before")
    @classmethod
    def _normalize_graph_community_rebuild(cls, v):
        # A .env typo must never brick get_settings(): unknown values mean off.
        v = str(v or "").strip().lower()
        return v if v in ("off", "weekly", "nightly") else "off"
    # ── End Quick 038 ──

    # ── Phase 1: Intelligence Corner UI profile ──
    # Which product the operator UI renders: 'intelligence' (Intelligence Corner
    # rail + Overview home) or 'second_brain' (classic layout). Read by the
    # frontend at runtime once the Intelligence Corner UI (Phase 7) consumes it;
    # until then it is stored but changes nothing. Unknown values degrade
    # to 'intelligence' instead of raising, because a .env typo must never brick
    # get_settings(); configservice enforces the allowed set at save time via
    # REGISTRY choices.
    ui_profile: str = UI_PROFILES[0]

    @field_validator("ui_profile", mode="before")
    @classmethod
    def _normalize_ui_profile(cls, v):
        v = str(v or "").strip().lower()
        return v if v in UI_PROFILES else UI_PROFILES[0]

    # ── Phase 8: dashboards origin address ──
    # Public base URL of the dashboards origin (/d/ links). Blank lets the UI
    # guess from its own address. Never raises: a typo degrades to blank.
    dashboards_public_url: str = ""

    @field_validator("dashboards_public_url", mode="before")
    @classmethod
    def _normalize_dashboards_public_url(cls, v):
        v = str(v or "").strip().rstrip("/")
        return v if v.lower().startswith(("http://", "https://")) else ""

    # ── Phase 18: chart-spec dashboards ──
    # Re-enables rendering of old agent-written HTML dashboard versions (default off).
    # Residual T-12-51 applies while it is on. New versions are chart specs either way.
    dashboard_legacy_html: bool = False

    # ── Quick 035: agent loops outside the chat graph cache like it ──
    # The playbook agent and the sentinel watch compiler send the chat graph's two
    # breakpoints (system marker + top-level conversation cache); the triage batch
    # marks its stable system prompt only. False sends them uncached exactly as
    # before. Read per run (per compile, per triage batch), so a change applies live.
    prompt_cache_loops: bool = True
    # ── End Quick 035 ──
    # ── Quick 039: budgets by cost, triage retry cap ──
    # How TokenBudget counts against the reactive, playbook, data and context-pack
    # budgets: "cost" = uncached-equivalent tokens (each token kind weighted by its
    # usage.py list price over the uncached input price; Opus 5.5 cache read 0.05,
    # 5-minute write 1.25, output 5), "raw" = every token at full weight, as before.
    # Read once per run; unknown values read as cost.
    budget_weighting: str = "cost"
    # Triage model failures (API 4xx/5xx, rate limit, refusal): the wait doubles from
    # triage_batch_seconds up to triage_backoff_max_seconds; after triage_max_attempts
    # failed calls on one batch its events go to 'failed' (requeued once after the
    # model answers again). A spend-limit error ("usage limits", "credit balance")
    # pauses every triage model call for triage_spend_pause_minutes instead.
    triage_backoff_max_seconds: int = 600
    triage_max_attempts: int = 5
    triage_spend_pause_minutes: int = 60
    # ── End Quick 039 ──
    # ── Quick 037: tool-result caps ──
    # A tool result stays in the conversation and is re-read on every later step of a run, so
    # the big ones are capped in code. Read per call, so a change applies to the next call.
    # get_model: its compact summary (no arguments) or one section on demand, in characters.
    model_result_chars: int = 20000
    # run_sql: whole rows up to this many characters, then a footer with the row count and
    # how to get the rest (aggregate, narrow, or export with run_sql_to_csv).
    sql_result_chars: int = 8000
    # search_business_context: hits per search, and characters of each hit's text.
    search_context_hits: int = 8
    search_context_hit_chars: int = 800
    # ── End Quick 037 ──
    # ── Quick 036 ──
    # Dashboard guard rails, enforced in tool code per agent run (dashguard.py).
    dashboard_delegations_per_run: int = 2
    dashboard_preview_rounds: int = 3
    # Which delegates stop on a tool call cut off at max_tokens (graph.CutOffGuard):
    # all, dashboard (the dashboard specialist only) or off (the pre-Quick 036 blind
    # retry). Unknown values degrade to all instead of raising.
    delegate_truncation_guard: str = "all"

    @field_validator("delegate_truncation_guard", mode="before")
    @classmethod
    def _normalize_truncation_guard(cls, v):
        v = str(v or "").strip().lower()
        return v if v in ("all", "dashboard", "off") else "all"
    # ── End Quick 036 ──

    # ── Phase 22: dashboard capabilities ──
    # Number format of dashboards ("" = derive from the operator timezone); only exact
    # locales the renderer knows are kept, anything else degrades to "".
    dashboard_number_locale: str = ""
    # Day buckets follow the operator timezone (SET LOCAL TimeZone on dashboard reads).
    dashboard_local_days: bool = True
    # How old-style timestamps without a zone are read: "local" or "utc".
    dashboard_naive_timestamps: str = "local"
    # Caps for one v2 dashboard snapshot.
    dashboard_snapshot_rows: int = 10000
    dashboard_snapshot_bytes: int = 2097152
    # Public address of this app, for the "Ask about this number" link ("" = no link).
    app_public_url: str = ""

    @field_validator("dashboard_number_locale", mode="before")
    @classmethod
    def _normalize_dashboard_number_locale(cls, v):
        v = str(v or "").strip()
        return v if v in DASHBOARD_LOCALES else ""

    @field_validator("dashboard_naive_timestamps", mode="before")
    @classmethod
    def _normalize_dashboard_naive_timestamps(cls, v):
        v = str(v or "").strip().lower()
        return v if v in ("local", "utc") else "local"

    @field_validator("dashboard_snapshot_rows", mode="before")
    @classmethod
    def _normalize_dashboard_snapshot_rows(cls, v):
        try:
            n = int(v)
        except (TypeError, ValueError):
            return 10000
        return max(1000, min(10000, n))

    @field_validator("dashboard_snapshot_bytes", mode="before")
    @classmethod
    def _normalize_dashboard_snapshot_bytes(cls, v):
        try:
            n = int(v)
        except (TypeError, ValueError):
            return 2097152
        return max(262144, min(2097152, n))

    @field_validator("app_public_url", mode="before")
    @classmethod
    def _normalize_app_public_url(cls, v):
        import re
        v = str(v or "").strip().rstrip("/")
        # C-12: exactly scheme://host[:port]; anything else (quotes, angle brackets, spaces,
        # IPv6, underscores, paths) becomes empty so it is safe for an HTML attribute and an href.
        m = re.fullmatch(r"(https?)(://[A-Za-z0-9.-]+(?::[0-9]{1,5})?)", v, flags=re.IGNORECASE)
        if not m:
            return ""
        port = re.search(r":([0-9]+)$", m.group(2))
        if port and int(port.group(1)) > 65535:
            return ""
        return m.group(1).lower() + m.group(2)
    # ── End Phase 22 ──

    @property
    def allowlist(self) -> set[str]:
        return {n.strip() for n in self.allowed_numbers.split(",") if n.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()

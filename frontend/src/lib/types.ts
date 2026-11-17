/* Shapes returned by the gateway's ops API.
 *
 * Hand-written from the route handlers in backend/app/main.py rather than
 * generated: the backend is owned by another workstream, and a type that says
 * what this UI actually depends on is a contract that a reviewer can check.
 * Anything optional here is optional because the backend may legitimately
 * return null — `origin_chat_jid` in particular is a column nothing populates
 * yet, so every reader treats it as absent.
 */

export type RunStatus = 'queued' | 'running' | 'done' | 'failed'

export interface Run {
  id: number
  thread_id: string
  status: RunStatus
  result_summary: string | null
  error: string | null
  created_at: string
  started_at: string | null
  ended_at: string | null
}

export interface ToolCall {
  tool: string
  args: unknown
  result_digest: string | null
  latency_ms: number | null
  created_at: string
}

export interface Artifact {
  name: string
  bytes: number
}

export type ActionStatus = 'proposed' | 'approved' | 'executed' | 'rejected' | 'failed'

export interface Action {
  id: number
  case_id: number | null
  playbook_id: number | null
  kind: string
  task_run_id?: number | null
  payload: unknown
  tier: string | null
  status: ActionStatus
  created_at: string
  executed_at: string | null
  case_title?: string | null
  playbook_name?: string | null
}

/** One field pulled off a document, with the evidence for it. */
export interface ExtractedField {
  name?: string
  field?: string
  value?: unknown
  confidence?: number | null
  source_quote?: string
  quote?: string
}

export type CaseStatus = 'open' | 'pending_ack' | 'closed' | 'dismissed'

export interface CaseSummary {
  id: number
  title: string
  status: CaseStatus
  summary: string | null
  close_reason: string | null
  created_at: string
  closed_at: string | null
  open_exp: number
  violated_exp: number
  satisfied_exp: number
  /** Quick 006: where the case came from. null = triage (messages from people). */
  origin?: CaseOrigin | null
  watch_id?: number | null
  links?: CaseLinks
  /** Quick 006: when the owner acknowledged an alert case (null = not yet). */
  seen_at?: string | null
  last_activity_at?: string | null
  /** Quick 006: how many times the case's watch fired. */
  fire_count?: number
}

/** Quick 006: cases opened from data findings, or the shared exploration case. */
export type CaseOrigin = 'watch' | 'insight' | 'exploration' | 'chat'

export interface CaseLinks {
  watch_id?: number
  watch_title?: string
  watch_kind?: string
  insight_key?: string
  insight_title?: string
  dashboard_slug?: string
  thread_id?: string
  /** Insight cases: the most severe finding on the case (warning or critical). */
  severity?: string
}

export interface CaseDetail {
  case: {
    id: number
    title: string
    status: CaseStatus
    summary: string | null
    close_reason: string | null
    created_at: string
    closed_at: string | null
    origin_chat_jid?: string | null
    /** Quick 024: the chat's name as people say it ("WhatsApp · Asha"), never the raw id. */
    origin_chat_name?: string | null
    /** Alternate identifiers triage matches events on (invoice no, doc ref, nickname). */
    aliases?: string[]
    /** Quick 006 */
    origin?: CaseOrigin | null
    links?: CaseLinks
    seen_at?: string | null
    watch_id?: number | null
    [k: string]: unknown
  }
  participants: { id: number; name: string; role: string | null }[]
  expectations: Expectation[]
  timeline: TimelineEvent[]
  actions: Action[]
  /** Current (non-superseded) case facts — latest value per field, with history kept server-side.
   *  src is provenance: 'whatsapp:Vinayak', 'system', or 'unattributed'. */
  facts: { field: string; value: string; observed_at: string; src: string }[]
  /** Quick 006: the watch an alert case links back to (null once deleted). */
  watch?: {
    id: number
    title: string
    kind: string
    status: string
    /** Normal checks in a row so far, and how many close the case. */
    normal_streak: number
    recovery_checks?: number
  } | null
}

export type ExpectationStatus = 'open' | 'violated' | 'satisfied' | 'dismissed'

export interface Expectation {
  id: number
  description: string
  status: ExpectationStatus
  due_at: string | null
  reminder_count: number
  satisfy_hint: string | null
  owner: string | null
  /** Quick 024: 'assistant' = a request the assistant owes (its reply in the chat meets it). */
  owed_by?: string | null
}

export interface TimelineEvent {
  id: number
  source: string
  content: string | null
  occurred_at: string
  provenance: 'external' | 'self'
  actor: string | null
  /** Quick 006: the note label (watch, investigation, exploration, chat, closing, manual, triage...). */
  kind?: string | null
  /** Quick 006: the note's structured parts (exploration, chat and watch notes only). */
  meta?: Record<string, unknown> | null
}

export interface ChatMember {
  number: string
  is_admin: boolean
  person_id: number | null
  name: string | null
  first_seen_at?: string
  synced_at?: string
}

export interface Chat {
  jid: string
  subject: string | null
  is_group: boolean
  status: 'pending' | 'active' | 'ignored'
  purpose: string | null
  awaiting_purpose: boolean
  last_seen_at: string | null
  created_at: string
  members_synced_at?: string | null
  member_count?: number | null
  known_count?: number | null
}

export interface Person {
  id: number
  name: string
  aliases: string[] | null
  email: string | null
  whatsapp: string | null
  whatsapp_alt?: string[] | null
  role: string | null
  active: boolean
  is_primary?: boolean
}

export interface UnmatchedSender {
  actor_raw: string
  sources: string[]
  event_count: number
  last_content: string | null
}

export interface DocumentRow {
  id: number
  thread_id: string | null
  task_run_id: number | null
  filename: string
  source: string | null
  doc_type: string | null
  summary: string | null
  status: 'extracted' | 'recorded' | 'rejected' | 'failed' | string
  created_at: string
}

export interface DocumentDetail extends DocumentRow {
  records: { id: number; record: unknown; approved_by: string | null; created_at: string }[]
  [k: string]: unknown
}

export interface Playbook {
  id: number
  name: string
  trigger: unknown
  goal: string | null
  tool_grants: unknown
  autonomy_tier: string | null
  status: 'draft' | 'active' | 'archived'
  proposed_by: string | null
  approved_by: string | null
  created_at: string
  /** Quick 023: the latest run of this playbook, or null if it never ran. */
  last_run?: { id: number; status: 'queued' | 'running' | 'done' | 'failed'; error: string | null; ended_at: string | null } | null
}

/* GET /api/skills deliberately omits `body` (the dashboard polls the list
 * every 5s and bodies run to 64KB); GET /api/skills/{name} returns it. */
export interface SkillRow {
  id: number
  name: string
  description: string | null
  source: string
  origin_ref: string | null
  license: string | null
  compat: string | null
  enabled: boolean
  /** 'agent' = supervisor skills index; 'triage' = owner triage policy */
  scope: string
  created_at: string
  updated_at: string
}

export interface SkillDetail extends SkillRow {
  body: string
}

/** One vendored-library manifest entry. `id` is "source/name". */
export interface LibraryEntry {
  id?: string
  source?: string
  name?: string
  description?: string | null
  license?: string | null
  compat?: string | null
  /** hand-curated in the vendor script: plausibly useful to THIS assistant */
  relevant?: boolean
  [k: string]: unknown
}

export interface LibraryResponse {
  library_available: boolean
  skills: LibraryEntry[]
}

export interface Watch {
  id: number
  thread_id: string
  kind: string
  title: string
  condition: string
  cadence_seconds: number
  one_shot: boolean
  status: string
  last_state: unknown
  compiled_sql: string | null
  trigger_rule: string | null
  compile_error: string | null
  last_checked_at: string | null
  last_fired_at: string | null
  fire_count: number
  created_at: string
  /** Quick 006: the open alert case for this watch, if any. */
  open_case_id?: number | null
  /** Phase 17: the governed measure this watch reads (null = raw SQL watch). */
  measure_ref?: { measure: string; filters?: unknown[]; range?: string | null } | null
  /** Phase 17: set while the watch's data is stale and its checks are skipped. */
  stale_since?: string | null
  /** Phase 17: when the data this watch reads was last refreshed. */
  data_as_of?: string | null
}

export type WatchEventKind = 'fire' | 'stale_data' | 'data_fresh'

export interface WatchEvent {
  severity: string
  summary: string
  escalated: boolean
  created_at: string
  /** Phase 17: absent on older backends, treated as 'fire'. */
  kind?: WatchEventKind
}

/** Phase 17: plain label for stale and fresh events; null for a normal fire. */
export function eventLabel(kind: string | undefined | null): string | null {
  if (kind === 'stale_data') return 'Stale data, check skipped'
  if (kind === 'data_fresh') return 'Data fresh again'
  return null
}

export interface Learning {
  id: number
  thread_id: string | null
  kind: string
  content: string
  created_at: string
  updated_at: string
  revisions: number
  status?: string            // Quick 009: 'active' | 'superseded'; Quick 020: 'pending' | 'rejected'
  status_reason?: string | null
  graphed_at?: string | null
  // Quick 020: who it applies to. A preference belongs to the person who stated
  // it; facts, corrections, recipes and lessons apply to everyone.
  source?: string | null
  scope?: string | null
  scope_kind?: 'owner' | 'person' | 'group' | 'global'
  scope_label?: string
}

export interface DbSource {
  table: string
  watermark_column: string
  label: string
}

export interface DbSourceProposal extends DbSource {
  reason: string
  recommended: boolean
}

export interface AgentConfig {
  prompt: string
  model: string
  temperature: number
  max_tokens: number
  recursion_limit: number
  tools: string[]
}

export interface AgentInfo {
  name: string
  role: string
  model: string
  tools: { name: string; description: string }[]
  guardrails: string[]
  config_key?: string
  config?: AgentConfig
  default_config?: AgentConfig
  customized?: boolean
  tool_superset?: string[]
}

// Phase 13 (GOLIVE-01)
export interface ModelOption {
  value: string
  note: string
  group: 'current' | 'legacy'
}

export interface ConfigField {
  key: string
  label: string
  value: string
  secret: boolean
  required: boolean
  set: boolean
  source: string
  // present only for enumerated settings (e.g. ui_profile); Setup renders a select
  choices?: string[]
  // Phase 11: GOLIVE-02 stored ciphertext cannot be decrypted (key changed)
  undecryptable?: boolean
  // Phase 13 (GOLIVE-01) model catalog for model keys, and the code default
  options?: ModelOption[]
  default?: string
}

export interface ConfigResponse {
  groups: Record<string, string>
  optional: string[]
  config: Record<string, ConfigField[]>
}

export type ConfigState = 'ok' | 'failing' | 'untested' | 'not_configured'

export interface ConfigGroupStatus {
  state: ConfigState
  detail?: string
  optional?: boolean
}

export type ConfigStatus = Record<string, ConfigGroupStatus>

export interface Stats {
  process: { cpu_percent?: number; memory_mb?: number; threads?: number }
  system: {
    cpu_percent?: number
    memory_percent?: number
    memory_used_gb?: number
    memory_total_gb?: number
    error?: string
  }
  containers: { name: string; cpu: string; mem: string; mem_percent: string }[]
  queue: Record<string, number>
}

export interface LogEntry {
  ts: string
  level: string
  logger: string
  msg: string
}

/* `transcript` and `profile` are deliberately `unknown`: the GET route returns
 * them straight out of asyncpg, which hands jsonb back as a STRING, while the
 * POST route returns real arrays and objects because it decodes them to call
 * the model. Both shapes reach this UI, so both are coerced at the edge. */
export interface GraphEpisode {
  uuid: string
  name?: string
  content: string
  source: string
  group_id: string
  occurred_at?: string | null
  created_at?: string | null
}

/* Quick 014: one recall result — a fact (edge), an entity or an episode —
 * the same items agents retrieve; superseded facts come back flagged. */
export interface GraphFact {
  fact: string
  valid_at?: string | null
  created_at?: string | null
  kind?: 'fact' | 'entity' | 'episode' | string
  uuid?: string
  group_id?: string
  name?: string
  type?: string
  source?: string
  invalid_at?: string | null
  superseded?: boolean
}

export interface OnboardingState {
  status: 'not_started' | 'active' | 'complete' | 'abandoned' | string
  transcript: unknown
  profile?: unknown
  updated_at?: string
}

export interface OnboardingTurn {
  reply?: string
  done?: boolean
  profile?: Record<string, unknown>
  applied?: Record<string, unknown>
  transcript?: unknown
  error?: string
}

// Phase 7: Insights (GET /api/insights)
export interface InsightEvidence {
  row_count: number
  columns: string[]
  sample: Record<string, unknown>[]
  truncated: boolean
  relative_dates?: boolean
}

export interface InsightItem {
  type: 'note' | 'watch_event'
  id: string
  origin: string
  title: string
  finding: string
  severity: 'info' | 'warning' | 'critical'
  evidence_sql: string | null
  evidence: InsightEvidence | null
  case_id: number | null
  case_title: string | null
  watch_id: number | null
  watch_title: string | null
  task_run_id: number | string | null
  created_at: string
}

export interface InsightsResponse {
  origin: string
  items: InsightItem[]
  next_before: string | null
}

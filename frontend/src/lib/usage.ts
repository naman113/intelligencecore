import { get } from './api'

/* Quick 005: where the model tokens and the estimated money go.
 *
 * Shapes are hand-written from the /api/usage and /api/runs/{id}/trace route
 * handlers. Every cost is an estimate at list prices as of `price_table_date`;
 * a call on a model missing from the price table counts in `unpriced_calls`
 * and adds nothing to `cost_usd`. */

export type UsagePeriod = 'today' | '7d' | 'month' | '30d'

export const PERIODS: { value: UsagePeriod; label: string }[] = [
  { value: 'today', label: 'Today' },
  { value: '7d', label: '7 days' },
  { value: 'month', label: 'This month' },
  { value: '30d', label: '30 days' },
]

export interface UsageNumbers {
  calls: number
  input: number
  output: number
  cache_read: number
  cache_write: number
  tokens: number
  cost_usd: number
  unpriced_calls: number
}

export interface UsageBucket extends UsageNumbers {
  /** null for calls with no value, e.g. a by_chat row for calls with no chat. */
  key: string | null
  label: string
}

/** off: no daily budget. warn: past 80%. paused: at or over 100%, background
 *  AI work (overnight exploration, playbooks, watch investigations) waits until
 *  `resets_at`; chat keeps working. */
export type BudgetState = 'off' | 'ok' | 'warn' | 'paused'

export interface UsageBudget {
  limit_usd: number | null
  spent_usd: number
  pct: number | null
  state: BudgetState
  resets_at: string | null
}

export interface UsageTopRun {
  trace: string
  /** null for work with no task run: watch compile/investigation, interview, triage. */
  run_id: number | null
  feature: string
  feature_label: string
  thread_id: string | null
  started_at: string
  calls: number
  tokens: number
  cost_usd: number
  models: string[]
}

export interface UsageDay {
  day: string
  tokens: number
  cost_usd: number
}

export interface UsageSummary {
  price_table_date: string
  timezone: string
  installs: string[]
  install: string | null
  period: UsagePeriod
  since: string
  today: UsageNumbers
  month: UsageNumbers
  period_totals: UsageNumbers
  budget: UsageBudget
  by_model: UsageBucket[]
  by_feature: UsageBucket[]
  by_agent: UsageBucket[]
  by_chat: UsageBucket[]
  top_runs: UsageTopRun[]
  daily: UsageDay[]
}

export interface TraceTotals {
  model_calls: number
  tool_calls: number
  input: number
  output: number
  cache_read: number
  cache_write: number
  tokens: number
  cost_usd: number
  unpriced_calls: number
  model_ms: number
  tool_ms: number
  wall_ms: number
}

export interface TraceModelEvent {
  kind: 'model'
  at: string
  ms: number | null
  agent: string | null
  model: string
  feature: string | null
  input: number
  output: number
  cache_read: number
  cache_write: number
  tokens: number
  cost_usd: number | null
  error: string | null
}

export interface TraceToolEvent {
  kind: 'tool'
  at: string
  ms: number | null
  agent: string | null
  name: string
  summary: string | null
  error: string | null
}

export type TraceEvent = TraceModelEvent | TraceToolEvent

export interface RunTrace {
  run_id: number | null
  trace: string
  totals: TraceTotals
  /** Already sorted by start time. */
  events: TraceEvent[]
}

/* ── Routes ────────────────────────────────────────────────────────────── */

function usageQuery(period: UsagePeriod, install: string | null): string {
  const q = new URLSearchParams({ period })
  if (install) q.set('install', install)
  return q.toString()
}

export function usageSummaryPath(period: UsagePeriod, install: string | null): string {
  return `/api/usage/summary?${usageQuery(period, install)}`
}

export function usageExportPath(period: UsagePeriod, install: string | null): string {
  return `/api/usage/export.csv?${usageQuery(period, install)}`
}

export function usageExportName(period: UsagePeriod): string {
  return `usage-${period}.csv`
}

export function usageTracePath(trace: string): string {
  return `/api/usage/trace?${new URLSearchParams({ trace }).toString()}`
}

export const loadUsageSummary = (period: UsagePeriod, install: string | null) =>
  get<UsageSummary>(usageSummaryPath(period, install))
export const loadRunTrace = (runId: number) => get<RunTrace>(`/api/runs/${runId}/trace`)
export const loadUsageTrace = (trace: string) => get<RunTrace>(usageTracePath(trace))

/* ── Formatters ────────────────────────────────────────────────────────── */

const num = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n)

/** 950, 1.2k, 12.3k, 123k, 3.4M. */
export function fmtTokens(n: number | null | undefined): string {
  if (!num(n)) return '—'
  if (Math.abs(n) < 1000) return String(Math.round(n))
  const units: [string, number][] = [
    ['k', 1e3],
    ['M', 1e6],
    ['B', 1e9],
  ]
  for (let i = 0; i < units.length; i++) {
    const [unit, div] = units[i]!
    const v = n / div
    const s = Math.abs(v) < 100 ? String(Number(v.toFixed(1))) : String(Math.round(v))
    if (Math.abs(Number(s)) < 1000 || i === units.length - 1) return `${s}${unit}`
  }
  return String(n)
}

/** The exact count, for a title attribute next to a shortened one. */
export function fmtCount(n: number | null | undefined): string {
  return num(n) ? Math.round(n).toLocaleString('en-US') : '—'
}

/** $0.0123 for small values, $1.23, $123, $1,234. */
export function fmtUsd(n: number | null | undefined): string {
  if (!num(n)) return '—'
  const a = Math.abs(n)
  const sign = n < 0 ? '-' : ''
  if (a === 0) return '$0.00'
  if (a < 0.0001) return `${sign}<$0.0001`
  if (a < 0.1) return `${sign}$${a.toFixed(4)}`
  if (a < 100) return `${sign}$${a.toFixed(2)}`
  return `${sign}$${Math.round(a).toLocaleString('en-US')}`
}

/** 340 ms, 4.1 s, 2 min 5 s. */
export function fmtMs(ms: number | null | undefined): string {
  if (!num(ms)) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`
  const s = Math.round(ms / 1000)
  return `${Math.floor(s / 60)} min ${s % 60} s`
}

/** Offset of a timeline row from the first event: +0.0 s, +4.2 s. */
export function fmtOffset(ms: number | null | undefined): string {
  if (!num(ms)) return ''
  return `+${(Math.max(0, ms) / 1000).toFixed(1)} s`
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** A calendar day ("2026-10-25") spoken without a timezone shift: 25 Oct 2026. */
export function fmtDay(day: string | null | undefined, withYear = true): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(day ?? '')
  if (!m) return day || '—'
  const month = MONTHS[Number(m[2]) - 1] ?? m[2]
  return withYear ? `${Number(m[3])} ${month} ${m[1]}` : `${Number(m[3])} ${month}`
}

export function unpricedNote(n: number | null | undefined): string {
  if (!num(n) || n <= 0) return ''
  return `${n} call${n === 1 ? '' : 's'} unpriced`
}

/* ── Trace helpers ─────────────────────────────────────────────────────── */

/** Milliseconds from the first event to each event. */
export function traceOffsets(events: TraceEvent[]): number[] {
  const start = events.length ? new Date(events[0]!.at).getTime() : NaN
  return events.map((e) => {
    const t = new Date(e.at).getTime()
    return Number.isFinite(t) && Number.isFinite(start) ? t - start : 0
  })
}

/** The agent the run is driven by: the supervisor when present, otherwise the
 *  first agent named. Rows from any other agent ran inside a delegate call. */
export function rootAgent(events: TraceEvent[]): string | null {
  if (events.some((e) => e.agent === 'supervisor')) return 'supervisor'
  return events.find((e) => e.agent)?.agent ?? null
}

export function isNested(e: TraceEvent, root: string | null): boolean {
  return Boolean(e.agent && root && e.agent !== root)
}

/** A run recorded before usage tracking has tool calls but no model calls. */
export function modelCallsMissing(t: RunTrace): boolean {
  return t.events.length > 0 && !t.events.some((e) => e.kind === 'model')
}

/* ── Budget ────────────────────────────────────────────────────────────── */

export interface BudgetView {
  tone: 'neutral' | 'signal' | 'broken'
  headline: string
  note: string
}

/** Plain words for the daily budget tile. Never gold: the 80% item a person
 *  owes is raised separately as a health alert. */
export function budgetView(b: UsageBudget): BudgetView {
  if (b.state === 'off' || b.limit_usd === null) {
    return { tone: 'neutral', headline: 'No daily budget set', note: '' }
  }
  const headline = `${fmtUsd(b.spent_usd)} of ${fmtUsd(b.limit_usd)}`
  if (b.state === 'paused') {
    return {
      tone: 'broken',
      headline,
      note:
        'Daily budget reached. Background AI work (overnight exploration, playbooks and watch investigations) is paused until the budget resets. Chat keeps working.',
    }
  }
  if (b.state === 'warn') {
    return {
      tone: 'signal',
      headline,
      note: "Past 80% of today's budget. Background AI work pauses when it reaches 100%; chat keeps working.",
    }
  }
  return { tone: 'signal', headline, note: '' }
}

export function budgetPct(b: UsageBudget): number {
  if (num(b.pct)) return b.pct
  if (!b.limit_usd) return 0
  return (b.spent_usd / b.limit_usd) * 100
}

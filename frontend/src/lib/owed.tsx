import { createContext, useContext, type ReactNode } from 'react'
import { get } from './api'
import { useResource } from './hooks'
import { useProfile } from './profile'
import type { Profile } from './brand'
import { readContextChange } from './contracts'
import { loadHealthAlerts, type HealthAlert } from './health' // Phase 11
import { decodeForecastList, isFailing, type ForecastBrief } from './forecasts' // Phase 13.1
import type { ContextRule, ContextSource, DashboardListItem } from './ictypes'
import type { Action, CaseSummary, Chat, ConfigStatus, OnboardingState, UnmatchedSender } from './types'

/* Everything a human owes the system, gathered once.
 *
 * The rail's gold dots and the Today page's right-hand column are the same
 * question asked twice, so they read from one poll rather than two. That also
 * guarantees they can never disagree — a dot with no matching card is exactly
 * the kind of small lie that stops people trusting a dashboard.
 *
 * Under the intelligence profile the poll also reads the context sources, the
 * rules and the dashboard list, so proposed context rows are counted exactly
 * (not capped by the 100-row action list; context proposals and every other
 * proposal are read in two separate 100-row windows), dashboard drafts are owed, and a
 * dashboard whose current version is failing is owed too (assumption A7).
 * Phase 13.1: an active forecast definition whose last nightly run failed is
 * owed too, one item per definition.
 */

export interface OwedCounts {
  approvals: number
  chats: number
  cases: number
  people: number
  setup: number
  /** The first-run interview, which is owed until it has actually finished. */
  onboarding: number
  total: number
  /** Intelligence profile only; always 0 under second_brain. */
  schema: number
  rules: number
  dashboards: number
  approvalsAll: number
  /** Phase 11: open system-health alerts. */
  health?: number
  /** Phase 15 (SRC-10): open source_* health alerts; already inside health and total. */
  sources?: number
  /** Intelligence profile only: 1 when the WhatsApp connection test is failing (UI-12). */
  whatsapp: number
  /** Phase 13.1: intelligence only; active forecast definitions currently failing. */
  forecasts: number
}

export interface Owed {
  proposedActions: Action[]
  /** Every chat, so anything showing a thread ID can name it. */
  chats: Chat[]
  pendingChats: Chat[]
  ackCases: CaseSummary[]
  violatedCases: CaseSummary[]
  /** Quick 006: open KPI-alert cases the owner has not acknowledged yet (one card each). */
  alertCases: CaseSummary[]
  unmatched: UnmatchedSender[]
  missingGroups: string[]
  onboarding: OnboardingState | null
  /** Intelligence profile: proposed context_change actions. */
  contextProposals: Action[]
  /** Intelligence profile: proposed publish_dashboard actions. */
  dashboardDrafts: Action[]
  /** Intelligence profile: dashboards whose current version is failing. */
  brokenDashboards: DashboardListItem[]
  // Phase 18: dashboards whose current version is old HTML while legacy HTML is off.
  unconvertedDashboards: DashboardListItem[]
  /** Phase 13.1: active forecast definitions whose last run failed. */
  failingForecasts: ForecastBrief[]
  contextPendingExact: number
  rulesPendingExact: number
  contextTables: number
  /** WhatsApp is optional under intelligence; it is owed only while its test fails (UI-12). */
  whatsappFailing: boolean
  whatsappDetail: string
  counts: OwedCounts
  /** Phase 11: GOLIVE-05 health alerts */
  healthAlerts?: HealthAlert[]
  loading: boolean
  reload: () => Promise<void>
}

export interface OwedRaw {
  actions: Action[]
  chats: Chat[]
  pendingChats: Chat[]
  ack: CaseSummary[]
  violatedCases: CaseSummary[]
  /** Quick 006: optional so older callers and fixtures stay valid. */
  alertCases?: CaseSummary[]
  unmatched: UnmatchedSender[]
  missingGroups: string[]
  onboarding: OnboardingState | null
  sources: ContextSource[] | null
  rules: ContextRule[] | null
  dashboards: DashboardListItem[]
  /** Phase 13.1 */
  forecasts?: ForecastBrief[]
  /** Phase 11: GOLIVE-05 health alerts */
  healthAlerts?: HealthAlert[]
  whatsappFailing?: boolean
  whatsappDetail?: string
}

export type OwedData = Omit<Owed, 'loading' | 'reload'>

const ZERO_COUNTS: OwedCounts = {
  approvals: 0,
  chats: 0,
  cases: 0,
  people: 0,
  setup: 0,
  onboarding: 0,
  total: 0,
  schema: 0,
  rules: 0,
  dashboards: 0,
  approvalsAll: 0,
  whatsapp: 0,
  forecasts: 0, // Phase 13.1
}

const EMPTY: Owed = {
  proposedActions: [],
  chats: [],
  pendingChats: [],
  ackCases: [],
  violatedCases: [],
  alertCases: [],
  unmatched: [],
  missingGroups: [],
  onboarding: null,
  contextProposals: [],
  dashboardDrafts: [],
  brokenDashboards: [],
  unconvertedDashboards: [], // Phase 18
  failingForecasts: [], // Phase 13.1
  contextPendingExact: 0,
  rulesPendingExact: 0,
  contextTables: 0,
  whatsappFailing: false,
  whatsappDetail: '',
  counts: ZERO_COUNTS,
  loading: true,
  reload: async () => {},
}

const OwedContext = createContext<Owed>(EMPTY)

/* Quick 006: a case from a critical KPI alert, or a critical insight case, owes the
 * owner a look until they say "Got it" (seen_at) or it closes; a new fire (or the case
 * rising to critical) clears seen_at, so the card returns. Warning insight cases and
 * chat cases owe nothing by themselves. */
export function isUnseenAlert(c: Pick<CaseSummary, 'status' | 'origin' | 'seen_at' | 'links'>): boolean {
  if (c.status !== 'open' || c.seen_at) return false
  return c.origin === 'watch' || (c.origin === 'insight' && c.links?.severity === 'critical')
}

/** The kicker an alert card carries (Overview and Today). */
export function alertKicker(c: Pick<CaseSummary, 'origin' | 'fire_count'>): string {
  if (c.origin === 'insight') return 'Critical insight'
  return (c.fire_count ?? 0) > 1 ? `KPI alert · fired ${c.fire_count} times` : 'KPI alert'
}

/** What an alert card says under its title. */
export function alertBody(c: Pick<CaseSummary, 'origin' | 'summary'>): string {
  if (c.origin === 'insight')
    return `${c.summary || 'Exploration found something critical in your data.'} It stays open until you close it.`
  return `${c.summary || 'A watched number crossed its alert line.'} It closes by itself once the number is back to normal.`
}

/* The one definition of "this case is owed", shared by the counts here and the
 * Cases page's gold rows, so the two can never disagree. */
export function caseOwed(c: CaseSummary): boolean {
  return c.status === 'pending_ack' || (c.violated_exp ?? 0) > 0 || isUnseenAlert(c)
}

// Any one of these can fail on a degraded gateway (a missing table, a
// subsystem that never started). One failure must not blank the others, so
// each settles independently and a rejection contributes nothing.
async function settle<T>(p: Promise<T>, fallback: T): Promise<T> {
  try {
    return await p
  } catch {
    return fallback
  }
}

/* Context proposals can number in the hundreds after one discovery, so under
 * the intelligence profile they get their own window and cannot push dashboard
 * drafts and other approvals out of the list (review WR-01). An older gateway
 * ignores the filters and returns the same mixed rows twice, hence the dedupe. */
export async function loadProposedActions(intel: boolean): Promise<Action[]> {
  if (!intel) return get<Action[]>('/api/actions?status=proposed&limit=100')
  const [other, ctx] = await Promise.all([
    settle(get<Action[]>('/api/actions?status=proposed&exclude_kind=context_change&limit=100'), [] as Action[]),
    settle(get<Action[]>('/api/actions?status=proposed&kind=context_change&limit=100'), [] as Action[]),
  ])
  const byId = new Map<number, Action>()
  for (const a of [...other, ...ctx]) if (!byId.has(a.id)) byId.set(a.id, a)
  return [...byId.values()].sort((x, y) => y.id - x.id)
}

export async function loadOwed(profile: Profile = 'second_brain'): Promise<OwedRaw> {
  const intel = profile === 'intelligence'
  const [actions, chatsRes, ack, open, unmatched, status, onboarding, sources, rules, dashboards, forecasts, healthAlerts] =
    await Promise.all([
      settle(loadProposedActions(intel), [] as Action[]),
      settle(get<{ chats: Chat[] }>('/api/chats'), { chats: [] }),
      settle(get<CaseSummary[]>('/api/cases?status=pending_ack&limit=100'), []),
      settle(get<CaseSummary[]>('/api/cases?status=open&limit=100'), []),
      settle(get<UnmatchedSender[]>('/api/people/unmatched'), []),
      settle(get<ConfigStatus>('/api/config/status'), {} as ConfigStatus),
      settle(get<OnboardingState>('/api/onboarding?kind=business'), null as OnboardingState | null),
      intel
        ? settle(
            get<{ sources: ContextSource[] }>('/api/context/sources').then((r) => r.sources ?? null),
            null as ContextSource[] | null,
          )
        : Promise.resolve(null as ContextSource[] | null),
      intel
        ? settle(
            get<{ rules: ContextRule[] }>('/api/context/rules?status=all&limit=1000').then(
              (r) => r.rules ?? null,
            ),
            null as ContextRule[] | null,
          )
        : Promise.resolve(null as ContextRule[] | null),
      intel
        ? settle(
            get<DashboardListItem[] | { dashboards: DashboardListItem[] }>('/api/dashboards?limit=100').then(
              (r) => (Array.isArray(r) ? r : (r?.dashboards ?? [])),
            ),
            [] as DashboardListItem[],
          )
        : Promise.resolve([] as DashboardListItem[]),
      // Phase 13.1: failing forecast definitions
      intel
        ? settle(
            get<unknown>('/api/forecasts?limit=50').then(decodeForecastList),
            [] as ForecastBrief[],
          )
        : Promise.resolve([] as ForecastBrief[]),
      settle(loadHealthAlerts(), [] as HealthAlert[]), // Phase 11: GOLIVE-05
    ])

  const chats = chatsRes.chats || []
  const pendingChats = chats.filter((c) => c.status === 'pending')
  const violatedCases = open.filter((c) => c.violated_exp > 0)
  const alertCases = open.filter((c) => isUnseenAlert(c) && !(c.violated_exp > 0)) // Quick 006
  const missingGroups = Object.entries(status)
    .filter(([, s]) => s.state === 'not_configured' && !s.optional)
    .map(([g]) => g)

  return {
    actions,
    chats,
    pendingChats,
    ack,
    violatedCases,
    alertCases, // Quick 006
    unmatched,
    missingGroups,
    onboarding,
    sources,
    rules,
    dashboards,
    forecasts, // Phase 13.1
    healthAlerts, // Phase 11
    whatsappFailing: status.whatsapp?.state === 'failing',
    whatsappDetail: status.whatsapp?.detail ?? '',
  }
}

// The sources route returns a count per source; an older shape carried an
// object or null, so anything else is read tolerantly.
function pendingOf(v: unknown): number {
  if (typeof v === 'number' && Number.isFinite(v)) return v
  return v ? 1 : 0
}

function tableOfRef(ref: string, kind: string | null): string | null {
  if (kind === 'table') return ref
  if (kind === 'column') {
    const i = ref.lastIndexOf('.')
    return i > 0 ? ref.slice(0, i) : null
  }
  return null
}

/* Phase 12 (UI-12): WhatsApp owes the human something only under the
 * intelligence profile and only while its connection test is failing. */
function whatsappOwed(raw: OwedRaw, profile: Profile): number {
  return profile === 'intelligence' && raw.whatsappFailing ? 1 : 0
}

// Phase 15 (SRC-10): open source_stale/failing/unreachable alerts, intelligence profile only.
function sourceAlerts(alerts: HealthAlert[], profile: Profile): number {
  return profile === 'intelligence' ? alerts.filter((a) => a.component.startsWith('source_')).length : 0
}

export function computeOwed(raw: OwedRaw, profile: Profile): OwedData {
  const onboardingOwed = raw.onboarding && raw.onboarding.status !== 'complete' ? 1 : 0
  const alertCases = raw.alertCases ?? [] // Quick 006
  const base = {
    proposedActions: raw.actions,
    chats: raw.chats,
    pendingChats: raw.pendingChats,
    ackCases: raw.ack,
    violatedCases: raw.violatedCases,
    alertCases,
    unmatched: raw.unmatched,
    missingGroups: raw.missingGroups,
    onboarding: raw.onboarding,
  }
  const healthAlerts = raw.healthAlerts ?? [] // Phase 11: GOLIVE-05
  const legacyCounts = {
    approvals: raw.actions.length,
    chats: raw.pendingChats.length,
    cases: raw.ack.length + raw.violatedCases.length + alertCases.length,
    people: raw.unmatched.length,
    setup: raw.missingGroups.length,
    onboarding: onboardingOwed,
    total:
      raw.actions.length +
      raw.pendingChats.length +
      raw.ack.length +
      raw.violatedCases.length +
      alertCases.length + // Quick 006
      raw.unmatched.length +
      raw.missingGroups.length +
      onboardingOwed +
      healthAlerts.length, // Phase 11
  }

  if (profile !== 'intelligence') {
    return {
      ...base,
      contextProposals: [],
      dashboardDrafts: [],
      brokenDashboards: [],
      unconvertedDashboards: [], // Phase 18
      failingForecasts: [], // Phase 13.1
      contextPendingExact: 0,
      rulesPendingExact: 0,
      contextTables: 0,
      healthAlerts, // Phase 11
      whatsappFailing: false,
      whatsappDetail: '',
      counts: { ...legacyCounts, health: healthAlerts.length, sources: 0, schema: 0, rules: 0, dashboards: 0, approvalsAll: 0, whatsapp: 0, forecasts: 0 },
    }
  }

  const contextProposals = raw.actions.filter((a) => a.kind === 'context_change')
  const dashboardDrafts = raw.actions.filter((a) => a.kind === 'publish_dashboard')
  const brokenDashboards = raw.dashboards.filter((d) => d.current_last_error)
  const failingForecasts = (raw.forecasts ?? []).filter(isFailing) // Phase 13.1
  // Phase 18 block: HTML dashboards that need converting; one that is also broken counts once.
  const unconvertedDashboards = raw.dashboards.filter((d) => d.needs_conversion === true)
  const unconvertedOnly = unconvertedDashboards.filter((d) => !d.current_last_error).length
  // end Phase 18 block
  const views = contextProposals.map((a) => readContextChange(a.payload))

  const contextPendingExact = raw.sources
    ? Math.max(
        raw.sources.reduce((n, s) => n + pendingOf(s.pending), 0),
        contextProposals.length,
      )
    : contextProposals.length
  const rulesPendingExact = raw.rules
    ? raw.rules.filter((r) => r.pending).length
    : views.filter((v) => v.kind === 'rule' || v.kind === 'kpi').length

  const tables = new Set<string>()
  for (const v of views) {
    const t = v.ref ? tableOfRef(v.ref, v.kind) : null
    if (t) tables.add(t)
  }

  const approvalsAll = raw.actions.length - contextProposals.length + contextPendingExact
  const total =
    approvalsAll +
    raw.pendingChats.length +
    raw.ack.length +
    raw.violatedCases.length +
    alertCases.length + // Quick 006
    raw.unmatched.length +
    raw.missingGroups.length +
    onboardingOwed +
    brokenDashboards.length +
    unconvertedOnly + // Phase 18
    failingForecasts.length + // Phase 13.1
    healthAlerts.length + // Phase 11
    whatsappOwed(raw, profile)

  return {
    ...base,
    contextProposals,
    dashboardDrafts,
    brokenDashboards,
    unconvertedDashboards, // Phase 18
    failingForecasts, // Phase 13.1
    contextPendingExact,
    rulesPendingExact,
    contextTables: tables.size,
    healthAlerts, // Phase 11
    whatsappFailing: whatsappOwed(raw, profile) === 1,
    whatsappDetail: raw.whatsappDetail ?? '',
    counts: {
      ...legacyCounts,
      total,
      health: healthAlerts.length, // Phase 11
      sources: sourceAlerts(healthAlerts, profile), // Phase 15 (SRC-10)
      schema: Math.max(0, contextPendingExact - rulesPendingExact),
      rules: rulesPendingExact,
      dashboards: dashboardDrafts.length + brokenDashboards.length + unconvertedOnly, // Phase 18
      approvalsAll,
      whatsapp: whatsappOwed(raw, profile),
      forecasts: failingForecasts.length, // Phase 13.1
    },
  }
}

export function OwedProvider({ children }: { children: ReactNode }) {
  const { profile } = useProfile()
  const { data, loading, reload } = useResource(() => loadOwed(profile), {
    interval: 10_000,
    deps: [profile],
  })

  // Counted here rather than in the page so the rail's badge and the card list
  // can never disagree about how much is outstanding.
  const value: Owed = data
    ? { ...computeOwed(data, profile), loading, reload }
    : { ...EMPTY, loading, reload }

  return <OwedContext.Provider value={value}>{children}</OwedContext.Provider>
}

export function useOwed(): Owed {
  return useContext(OwedContext)
}

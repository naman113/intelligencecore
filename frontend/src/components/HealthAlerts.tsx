/* Phase 11: GOLIVE-05. System > Health panel: loop and business-DB checks plus open and recent alerts from GET /api/health/alerts. */
import { dateTime } from '../lib/format'
import type { HealthCheck, HealthOverview } from '../lib/health'
import { Section, StatusPill } from './ui'

export const COMPONENT_LABEL: Record<string, string> = {
  worker: 'Task worker',
  sentinel: 'KPI watch checks',
  dashboard_refresh: 'Dashboard refresh',
  business_db: 'Business database',
  llm_budget: 'AI spend budget',
  warehouse: 'Warehouse', // Phase 14 (WH-08)
}
const ORDER = Object.keys(COMPONENT_LABEL)

export type HealthPanelState = 'unknown' | 'alert' | 'starting' | 'green'

export function healthState(data: HealthOverview | null): HealthPanelState {
  if (!data) return 'unknown'
  const checks = Object.values(data.checks)
  if (data.alerts.some((a) => !a.resolved_at) || checks.some((c) => c.state === 'failing')) return 'alert'
  if (checks.length === 0 || checks.some((c) => c.state === 'starting')) return 'starting'
  return 'green'
}

const PILL: Record<HealthCheck['state'], { tone: 'settled' | 'broken' | 'neutral'; label: string }> = {
  ok: { tone: 'settled', label: 'ok' },
  failing: { tone: 'broken', label: 'failing' },
  starting: { tone: 'neutral', label: 'starting' },
  unknown: { tone: 'neutral', label: 'waiting' }, // Phase 14 (WH-08)
}

export function HealthAlertsPanel({ data, error }: { data: HealthOverview | null; error?: string | null }) {
  const state = healthState(data)
  const names = data
    ? [
        ...ORDER.filter((c) => c in data.checks),
        ...Object.keys(data.checks).filter((c) => !ORDER.includes(c)),
      ]
    : []
  const open = data?.alerts.filter((a) => !a.resolved_at) ?? []
  const resolved = (data?.alerts.filter((a) => a.resolved_at) ?? []).slice(0, 10)
  const summary =
    state === 'green'
      ? 'All checks ok'
      : state === 'starting'
        ? 'Checks are starting'
        : state === 'alert'
          ? 'Needs attention'
          : `Health checks unavailable${error ? `: ${error}` : ''}`
  return (
    <Section title="Loops and business database">
      <div className="stack-3" data-health-panel data-health-state={state}>
        <div>
          <strong>{summary}</strong>
        </div>
        {names.map((c) => {
          const chk = data!.checks[c]
          const pill = PILL[chk.state] ?? PILL.starting
          return (
            <div key={c} className="btn-row" data-health-check={c}>
              <span>{COMPONENT_LABEL[c] ?? c}</span>
              <StatusPill tone={pill.tone} label={pill.label} />
              <span className="dim">{chk.detail}</span>
            </div>
          )
        })}
        {data && (
          <div className="stack-3">
            <div className="field-label">Open alerts</div>
            {open.length === 0 && <span className="dim">No open alerts</span>}
            {open.map((a) => (
              <div key={a.id} data-health-alert="open">
                <strong>{a.title}</strong> <span className="dim">{a.detail}</span>{' '}
                <span className="dimmer">since {dateTime(a.created_at)}</span>
              </div>
            ))}
          </div>
        )}
        {resolved.length > 0 && (
          <div className="stack-3">
            <div className="field-label">Recently resolved</div>
            {resolved.map((a) => (
              <div key={a.id} data-health-alert="resolved">
                <span>{a.title}</span>{' '}
                <span className="dimmer">resolved {dateTime(a.resolved_at)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </Section>
  )
}

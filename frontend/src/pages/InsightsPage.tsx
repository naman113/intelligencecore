import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { post } from '../lib/api'
import { asks } from '../lib/ask'
import {
  fetchInsights,
  fetchInsightsSummary,
  insightWords,
  type Insight,
  type InsightsSummary,
} from '../lib/contracts'
import { ago, isoTitle } from '../lib/format'
import { useResource } from '../lib/hooks'
import { runsHref } from '../lib/runs'
import { useAsk } from '../components/AskPanel'
import { FailedRunNote, RunNowView, isActiveRun, runPlaybookNow } from '../components/RunNow'
import { EvidenceDetails, SEVERITY_LOOK, SeverityPill } from '../components/InsightEvidence'
import { ConfirmButton, Empty, ErrorNote, Loading, PageHeader, Tabs } from '../components/ui'
import './ic.css'

/* Named InsightsPage so it never collides with Phase 7's pages/Insights.tsx.
 *
 * Quick 006 card: severity as a marker (critical red, warning amber, info neutral; the
 * word is always printed too, so colour is never the only signal), the title as the
 * headline, then the plain words (what we found, why it matters, the next step), key
 * figures, a muted caveat, the evidence folded away, and the actions. Gold stays in the
 * owed column only. Agent text is React text, never HTML. */

export const SEVERITY_ORDER: Record<string, number> = { critical: 3, warning: 2, info: 1 }

export { SeverityPill }

const pad = (n: number) => String(n).padStart(2, '0')

const dayOf = (at: string | null) => {
  if (!at) return ''
  const d = new Date(at)
  return Number.isNaN(d.getTime()) ? '' : `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/** Newest day first; within a day the most severe first; then newest first. */
export function sortInsights(items: Insight[]): Insight[] {
  return [...items].sort((a, b) => {
    const day = dayOf(b.at).localeCompare(dayOf(a.at))
    if (day) return day
    const sev = (SEVERITY_ORDER[b.severity ?? ''] ?? 0) - (SEVERITY_ORDER[a.severity ?? ''] ?? 0)
    if (sev) return sev
    return (b.at ?? '').localeCompare(a.at ?? '')
  })
}

const OPEN_CASE = new Set(['open', 'pending_ack'])

function caseStatusLabel(s: string | null | undefined): string {
  if (!s) return ''
  if (s === 'pending_ack') return 'waiting for you'
  return s.replace(/_/g, ' ')
}

export function InsightCard({
  i,
  onMakeKpi,
  onDismiss,
}: {
  i: Insight
  onMakeKpi: (i: Insight) => void
  onDismiss?: (i: Insight) => void
}) {
  const { summary, caveat } = insightWords(i)
  const sev = i.severity && SEVERITY_LOOK[i.severity] ? i.severity : 'none'
  const figures = i.keyFigures ?? []
  const caseOpen = i.caseId !== null && OPEN_CASE.has(i.caseStatus ?? 'open')
  return (
    <article className={`ins-card ins-sev-${sev}`}>
      <div className="ins-top">
        <SeverityPill severity={i.severity} />
        {i.lane === 'raw_sql' && i.verified === false && (
          <span className="pill" title="Found with raw SQL, not a governed measure">
            Unverified
          </span>
        )}
        {i.lane === 'measure' && i.measure && <span className="ic-meta">Measure: {i.measure}</span>}
        <span className="ic-meta" title={isoTitle(i.at)}>
          {i.at ? ago(i.at) : ''}
          {i.watchId !== null ? (
            <>
              {' · from KPI '}
              <Link to={`/kpis/${i.watchId}`}>{i.watchTitle ?? `#${i.watchId}`}</Link>
            </>
          ) : (
            ' · data exploration'
          )}
        </span>
      </div>
      <h3 className="ins-title">{i.title}</h3>
      <dl className="ins-words">
        <div>
          <dt>What we found</dt>
          <dd>{summary}</dd>
        </div>
        {i.impact && (
          <div>
            <dt>Why it matters</dt>
            <dd>{i.impact}</dd>
          </div>
        )}
        {i.nextStep && (
          <div>
            <dt>Suggested next step</dt>
            <dd>{i.nextStep}</dd>
          </div>
        )}
      </dl>
      {figures.length > 0 && (
        <ul className="ins-figures" aria-label="Key figures">
          {figures.map((f) => (
            <li key={`${f.label}:${f.value}`}>
              <span className="ins-fig-label">{f.label}</span>
              <span className="ins-fig-value">{f.value}</span>
            </li>
          ))}
        </ul>
      )}
      {caveat && <p className="ins-caveat">Caveat: {caveat}</p>}
      <EvidenceDetails
        finding={i.body && i.body.trim() !== summary ? i.body : null}
        sql={i.evidenceSql}
        evidence={i.evidence}
        at={i.at}
      />
      <div className="ins-actions">
        {i.caseId !== null && (
          <Link className="ins-case" to={`/cases/${i.caseId}`} title={i.caseTitle ?? undefined}>
            Case #{i.caseId}
            {i.caseStatus ? ` · ${caseStatusLabel(i.caseStatus)}` : ''}
          </Link>
        )}
        <span className="grow" />
        <button className="btn btn-sm" onClick={() => onMakeKpi(i)}>
          Make this a KPI
        </button>
        {caseOpen && onDismiss && (
          <ConfirmButton className="btn btn-sm btn-quiet" confirmLabel="Dismiss the case?" onConfirm={() => onDismiss(i)}>
            Dismiss
          </ConfirmButton>
        )}
      </div>
    </article>
  )
}

export function InsightsView({
  result,
  onMakeKpi,
  onDismiss,
  filtered = false,
}: {
  result: { available: boolean; items: Insight[] }
  onMakeKpi: (i: Insight) => void
  onDismiss?: (i: Insight) => void
  filtered?: boolean
}) {
  if (!result.available) {
    return (
      <Empty title="No insights yet">
        Insights appear after the nightly exploration playbook runs. See{' '}
        <Link to="/playbooks">Playbooks</Link>.
      </Empty>
    )
  }
  if (result.items.length === 0)
    return <Empty title={filtered ? 'Nothing matches these filters' : 'Nothing noticed yet'} />
  return (
    <div className="ins-list">
      {sortInsights(result.items).map((i) => (
        <InsightCard key={i.key} i={i} onMakeKpi={onMakeKpi} onDismiss={onDismiss} />
      ))}
    </div>
  )
}

export function SummaryStrip({
  s,
  run: runUi,
}: {
  s: InsightsSummary
  /** Quick 023: the Run exploration now control; omitted where there is nothing to run. */
  run?: { onRun: () => void; pending?: boolean; error?: string | null }
}) {
  const run = s.lastRun
  const runAt = run ? run.endedAt ?? run.startedAt : null
  const runNote = run
    ? `last exploration ${runAt ? ago(runAt) : ''}${run.status && run.status !== 'done' ? ` (${run.status.replace(/_/g, ' ')})` : ''}`
    : 'no exploration run yet'
  return (
    <div className="ins-strip" aria-label="Insight summary">
      <span className="ins-stat ins-stat-critical">
        <b>{s.bySeverity.critical}</b> critical
      </span>
      <span className="ins-stat ins-stat-warning">
        <b>{s.bySeverity.warning}</b> warning
      </span>
      <span className="ins-stat">
        <b>{s.bySeverity.info}</b> info
      </span>
      <span className="ins-stat">
        <b>{s.openCases}</b> open {s.openCases === 1 ? 'case' : 'cases'}
      </span>
      <span className="ins-strip-note" title={isoTitle(runAt)}>
        Last {s.days} days · {runNote}
      </span>
      {run?.status === 'failed' && <FailedRunNote error={run.error} />}
      {runUi && s.playbook?.status === 'active' && (
        <RunNowView
          label="Run exploration now"
          busyLabel="Exploring…"
          status={run?.status}
          runId={run?.id}
          pending={runUi.pending}
          error={runUi.error}
          onRun={runUi.onRun}
        />
      )}
    </div>
  )
}

type SevFilter = '' | 'critical' | 'warning' | 'info'
type CaseFilter = '' | 'open' | 'closed'

export default function InsightsPage() {
  const { send } = useAsk()
  const [note, setNote] = useState<string | null>(null)
  const [severity, setSeverity] = useState<SevFilter>('')
  const [caseFilter, setCaseFilter] = useState<CaseFilter>('')
  const res = useResource(
    () => fetchInsights(50, { severity: severity || undefined, case: caseFilter || undefined }),
    { interval: 300_000, deps: [severity, caseFilter] },
  )
  // Quick 023: poll the summary every 5 s only while an exploration run is queued or
  // running (and for the moment after the click); when it ends, refresh the list too.
  const [starting, setStarting] = useState(false)
  const [fast, setFast] = useState(false)
  const [runError, setRunError] = useState<string | null>(null)
  const summary = useResource(() => fetchInsightsSummary(), { interval: fast ? 5_000 : 300_000 })
  const active = isActiveRun(summary.data?.lastRun?.status)
  useEffect(() => setFast(active || starting), [active, starting])
  const wasActive = useRef(false)
  useEffect(() => {
    if (wasActive.current && !active) void res.reload()
    wasActive.current = active
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active])
  const runNow = async () => {
    const pb = summary.data?.playbook
    if (!pb) return
    setStarting(true)
    setRunError(null)
    try {
      await runPlaybookNow(pb.id)
    } catch (e) {
      setRunError(e instanceof Error ? e.message : 'Could not start the run.')
    }
    await summary.reload() // an "already running" refusal shows up here as the busy button
    setStarting(false)
  }
  const dismiss = async (i: Insight) => {
    if (i.caseId === null) return
    try {
      await post(`/api/cases/${i.caseId}/dismiss`)
      setNote(`Case #${i.caseId} dismissed.`)
    } catch (e) {
      setNote(e instanceof Error ? e.message : 'Could not dismiss the case.')
    }
    void res.reload()
    void summary.reload()
  }
  return (
    <div className="ic-page">
      <PageHeader
        title="Insights"
        lede="What the exploration playbook found in your data, most important first. Each one keeps the query that reproduces it."
        actions={
          <Link className="btn btn-sm" to={runsHref('playbook:explore_data')}>
            Exploration runs
          </Link>
        }
      />
      {summary.data && (
        <SummaryStrip s={summary.data} run={{ onRun: () => void runNow(), pending: starting, error: runError }} />
      )}
      <div className="ins-filters">
        <label className="ins-filter">
          <span className="field-label">Severity</span>
          <select className="select" value={severity} onChange={(e) => setSeverity(e.target.value as SevFilter)}>
            <option value="">All</option>
            <option value="critical">Critical</option>
            <option value="warning">Warning</option>
            <option value="info">Info</option>
          </select>
        </label>
        <Tabs<CaseFilter>
          value={caseFilter}
          onChange={setCaseFilter}
          options={[
            { value: '', label: 'All' },
            { value: 'open', label: 'Open case' },
            { value: 'closed', label: 'Closed case' },
          ]}
        />
      </div>
      {note && <div className="banner">{note}</div>}
      {res.error && <ErrorNote message={res.error} onRetry={() => void res.reload()} />}
      {res.loading && !res.data ? (
        <Loading label="Reading insights" />
      ) : res.data ? (
        <InsightsView
          result={res.data}
          filtered={!!severity || !!caseFilter}
          onDismiss={(i) => void dismiss(i)}
          onMakeKpi={(i) =>
            void send(asks.insightToKpi(i)).then((ok) =>
              setNote(ok ? 'Sent to the agent. Look for its KPI proposal on Approvals; once you approve it, check KPIs for the result.' : null),
            )
          }
        />
      ) : null}
    </div>
  )
}

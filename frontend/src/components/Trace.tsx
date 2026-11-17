import type { ReactNode } from 'react'
import { isoTitle, plural, truncate } from '../lib/format'
import { useResource } from '../lib/hooks'
import {
  fmtCount,
  fmtMs,
  fmtOffset,
  fmtTokens,
  fmtUsd,
  isNested,
  loadRunTrace,
  loadUsageTrace,
  modelCallsMissing,
  rootAgent,
  traceOffsets,
  unpricedNote,
  type RunTrace,
  type TraceEvent,
  type TraceTotals,
} from '../lib/usage'
import { Empty, ErrorNote, Loading, Section, StatusPill } from './ui'
import './trace.css'

/* Quick 005: every model call and tool call of one run, in the order they
 * started, with what they cost. Shared by Runs detail and the Usage page's
 * top-runs expand. */

function tokenSplit(t: { input: number; output: number; cache_read: number; cache_write: number }): string {
  return `in ${fmtTokens(t.input)} · out ${fmtTokens(t.output)} · cache read ${fmtTokens(t.cache_read)} · cache write ${fmtTokens(t.cache_write)}`
}

function Stat({ label, value, sub, title }: { label: string; value: ReactNode; sub?: ReactNode; title?: string }) {
  return (
    <div className="trace-stat" title={title}>
      <span className="field-label">{label}</span>
      <div className="trace-stat-value">{value}</div>
      {sub ? <div className="trace-stat-sub">{sub}</div> : null}
    </div>
  )
}

export function TraceTotalsView({ totals, modelMissing }: { totals: TraceTotals; modelMissing?: boolean }) {
  const unpriced = unpricedNote(totals.unpriced_calls)
  return (
    <div className="trace-totals" data-trace-totals>
      <Stat
        label="Model calls"
        value={modelMissing ? '—' : totals.model_calls}
        sub={modelMissing ? 'not recorded' : undefined}
      />
      <Stat label="Tool calls" value={totals.tool_calls} />
      <Stat
        label="Tokens"
        value={modelMissing ? '—' : fmtTokens(totals.tokens)}
        title={modelMissing ? undefined : `${fmtCount(totals.tokens)} tokens`}
        sub={modelMissing ? 'not recorded' : tokenSplit(totals)}
      />
      <Stat
        label="Est. cost"
        value={modelMissing ? '—' : fmtUsd(totals.cost_usd)}
        sub={modelMissing ? 'not recorded' : unpriced || undefined}
      />
      <Stat
        label="Model time"
        value={modelMissing ? '—' : fmtMs(totals.model_ms)}
        sub={totals.wall_ms ? `of ${fmtMs(totals.wall_ms)} wall time` : undefined}
      />
      <Stat label="Tool time" value={fmtMs(totals.tool_ms)} />
    </div>
  )
}

function modelDetail(e: Extract<TraceEvent, { kind: 'model' }>): string {
  const parts = [`${fmtTokens(e.tokens)} tokens`, `in ${fmtTokens(e.input)}`, `out ${fmtTokens(e.output)}`]
  if (e.cache_read) parts.push(`cache read ${fmtTokens(e.cache_read)}`)
  if (e.cache_write) parts.push(`cache write ${fmtTokens(e.cache_write)}`)
  parts.push(e.cost_usd === null || e.cost_usd === undefined ? 'unpriced' : `${fmtUsd(e.cost_usd)} est.`)
  return parts.join(' · ')
}

export function TraceRow({ event: e, offset, nested }: { event: TraceEvent; offset: number; nested: boolean }) {
  return (
    <div className={`trace-row${nested ? ' is-nested' : ''}`} data-kind={e.kind}>
      <span className="trace-at" title={isoTitle(e.at)}>
        {fmtOffset(offset)}
      </span>
      <div className="trace-main">
        <div className="trace-head">
          <StatusPill tone={e.kind === 'model' ? 'signal' : 'neutral'} label={e.kind} />
          <span className="trace-name">{e.kind === 'model' ? e.model : e.name}</span>
          {e.agent && <span className="trace-agent">{e.agent}</span>}
        </div>
        {e.kind === 'model' ? (
          <div className="trace-detail">{modelDetail(e)}</div>
        ) : e.summary ? (
          <div className="trace-summary">{truncate(e.summary, 300)}</div>
        ) : null}
        {e.error && <div className="trace-error">{e.error}</div>}
      </div>
      <span className="trace-ms">{fmtMs(e.ms)}</span>
    </div>
  )
}

export function TraceView({ trace }: { trace: RunTrace }) {
  const { events } = trace
  if (events.length === 0) {
    return (
      <Empty title="Nothing recorded">
        No model or tool calls were recorded for this run. It may not have started yet.
      </Empty>
    )
  }
  const missing = modelCallsMissing(trace)
  const offsets = traceOffsets(events)
  const root = rootAgent(events)
  return (
    <div data-trace={trace.trace}>
      <TraceTotalsView totals={trace.totals} modelMissing={missing} />
      {missing && (
        <div className="trace-note" data-trace-note="no-model-calls">
          Model calls were not recorded for this run. It ran before usage tracking started, so only its tool calls
          are shown.
        </div>
      )}
      <div>
        {events.map((e, i) => (
          <TraceRow key={i} event={e} offset={offsets[i] ?? 0} nested={isNested(e, root)} />
        ))}
      </div>
    </div>
  )
}

export function traceMeta(t: RunTrace): string {
  if (modelCallsMissing(t) || t.totals.model_calls === 0) return plural(t.totals.tool_calls, 'tool call')
  return `${fmtUsd(t.totals.cost_usd)} est.`
}

function TraceBody({
  data,
  error,
  loading,
  reload,
}: {
  data: RunTrace | null
  error: string | null
  loading: boolean
  reload: () => void
}) {
  if (error && !data) {
    return (
      <div style={{ padding: 'var(--s3)' }}>
        <ErrorNote message={error} onRetry={reload} />
      </div>
    )
  }
  if (loading && !data) return <Loading label="Reading the trace" />
  if (!data) return null
  return <TraceView trace={data} />
}

/** Runs detail: polls while the run may still be going. */
export function RunTraceSection({ runId }: { runId: number }) {
  const t = useResource(() => loadRunTrace(runId), { interval: 10_000, deps: [runId] })
  return (
    <Section title="Trace" meta={t.data ? traceMeta(t.data) : undefined} flush>
      <TraceBody data={t.data} error={t.error} loading={t.loading} reload={() => void t.reload()} />
    </Section>
  )
}

/** Usage page: the trace of work that has no task run (watch, interview, triage). */
export function UsageTrace({ trace }: { trace: string }) {
  const t = useResource(() => loadUsageTrace(trace), { deps: [trace] })
  return <TraceBody data={t.data} error={t.error} loading={t.loading} reload={() => void t.reload()} />
}

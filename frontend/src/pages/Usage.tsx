import { Fragment, useState } from 'react'
import { Link } from 'react-router-dom'
import { download } from '../lib/api'
import { ago, dateTime, isoTitle } from '../lib/format'
import { useAction, useResource } from '../lib/hooks'
import {
  PERIODS,
  budgetPct,
  budgetView,
  fmtCount,
  fmtDay,
  fmtTokens,
  fmtUsd,
  loadUsageSummary,
  unpricedNote,
  usageExportName,
  usageExportPath,
  type UsageBucket,
  type UsageBudget,
  type UsageDay,
  type UsageNumbers,
  type UsagePeriod,
  type UsageSummary,
  type UsageTopRun,
} from '../lib/usage'
import { Sparkline } from '../components/Sparkline'
import { UsageTrace } from '../components/Trace'
import { Download } from '../components/icons'
import { Empty, ErrorNote, Loading, Meter, PageHeader, Section, StatusPill, Tabs } from '../components/ui'
import './usage.css'

/* Quick 005: where the AI tokens and the estimated money go. Every figure is an
 * estimate at list prices; nothing here is owed by a person, so nothing is gold.
 * The 80% budget item a person owes arrives through the health alerts. */

const periodName = (p: UsagePeriod) => PERIODS.find((x) => x.value === p)?.label ?? p

/* ── Tiles ─────────────────────────────────────────────────────────────── */

export function UsageTile({ title, numbers, tile }: { title: string; numbers: UsageNumbers; tile: string }) {
  const unpriced = unpricedNote(numbers.unpriced_calls)
  return (
    <div className="panel usage-tile" data-tile={tile}>
      <span className="field-label">{title}</span>
      <div className="usage-tile-value">{fmtUsd(numbers.cost_usd)}</div>
      <div className="usage-tile-meta" title={`${fmtCount(numbers.tokens)} tokens`}>
        est. · {fmtTokens(numbers.tokens)} tokens · {fmtCount(numbers.calls)} calls
      </div>
      {unpriced && <div className="usage-tile-meta">{unpriced}</div>}
    </div>
  )
}

export function BudgetTile({ budget }: { budget: UsageBudget }) {
  const view = budgetView(budget)
  if (budget.state === 'off' || budget.limit_usd === null) {
    return (
      <div className="panel usage-tile" data-tile="budget" data-budget-state="off">
        <span className="field-label">Daily budget</span>
        <div className="usage-tile-value usage-tile-value-quiet">Off</div>
        <div className="usage-tile-meta">
          No daily budget set — <Link to="/system/setup">Setup › Tuning</Link>
        </div>
      </div>
    )
  }
  const pct = budgetPct(budget)
  const resets = budget.resets_at ? ago(budget.resets_at) : ''
  return (
    <div className="panel usage-tile" data-tile="budget" data-budget-state={budget.state}>
      <div className="row" style={{ gap: 'var(--s2)' }}>
        <span className="field-label" style={{ marginBottom: 0 }}>
          Daily budget
        </span>
        <span className="grow" />
        {budget.state === 'paused' && <StatusPill tone="broken" label="paused" />}
      </div>
      <div className="usage-tile-value">{view.headline}</div>
      <Meter value={Math.min(pct, 100)} max={100} tone={budget.state === 'paused' ? 'broken' : undefined} />
      <div className="usage-tile-meta" title={isoTitle(budget.resets_at)}>
        {Math.round(pct)}% used{resets ? ` · resets ${resets}` : ''}
      </div>
      {budget.state === 'warn' && <div className="usage-tile-note">{view.note}</div>}
      {budget.state === 'paused' && (
        <div className="usage-tile-note is-broken">Background AI work is paused until the reset. Chat keeps working.</div>
      )}
    </div>
  )
}

export function SpendTile({ daily }: { daily: UsageDay[] }) {
  const days = daily.slice(-30)
  const total = days.reduce((a, d) => a + (d.cost_usd || 0), 0)
  const peak = days.reduce<UsageDay | null>((m, d) => (!m || d.cost_usd > m.cost_usd ? d : m), null)
  return (
    <div className="panel usage-tile" data-tile="daily">
      <span className="field-label">Last 30 days</span>
      <div className="usage-tile-value">{fmtUsd(total)}</div>
      {days.length >= 2 ? (
        <>
          <Sparkline values={days.map((d) => d.cost_usd || 0)} height={28} />
          <div className="usage-tile-meta">
            {fmtDay(days[0]!.day, false)} to {fmtDay(days[days.length - 1]!.day, false)}
            {peak && peak.cost_usd > 0 ? ` · peak ${fmtUsd(peak.cost_usd)} on ${fmtDay(peak.day, false)}` : ''}
          </div>
        </>
      ) : (
        <div className="usage-tile-meta">Not enough history yet</div>
      )}
    </div>
  )
}

export function UsageTiles({ summary }: { summary: UsageSummary }) {
  return (
    <div className="usage-tiles">
      <UsageTile title="Today" numbers={summary.today} tile="today" />
      <UsageTile title="This month" numbers={summary.month} tile="month" />
      <BudgetTile budget={summary.budget} />
      <SpendTile daily={summary.daily} />
    </div>
  )
}

/* ── Tables ────────────────────────────────────────────────────────────── */

export function BreakdownTable({ rows, first, empty }: { rows: UsageBucket[]; first: string; empty: string }) {
  if (rows.length === 0) return <Empty title={empty} />
  return (
    <div className="table-scroll">
      <table className="table usage-table">
        <thead>
          <tr>
            <th>{first}</th>
            <th className="num">Calls</th>
            <th className="num">Input</th>
            <th className="num">Output</th>
            <th className="num">Cache read</th>
            <th className="num">Cache write</th>
            <th className="num">Tokens</th>
            <th className="num">Est. cost</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const unpriced = unpricedNote(r.unpriced_calls)
            return (
              <tr key={`${r.key ?? ''}-${i}`}>
                <td className="usage-label">
                  {r.label || (r.key === null ? '(no chat)' : r.key)}
                  {unpriced && <div className="usage-sub">{unpriced}</div>}
                </td>
                <td className="num mono">{fmtCount(r.calls)}</td>
                <td className="num mono" title={fmtCount(r.input)}>{fmtTokens(r.input)}</td>
                <td className="num mono" title={fmtCount(r.output)}>{fmtTokens(r.output)}</td>
                <td className="num mono" title={fmtCount(r.cache_read)}>{fmtTokens(r.cache_read)}</td>
                <td className="num mono" title={fmtCount(r.cache_write)}>{fmtTokens(r.cache_write)}</td>
                <td className="num mono" title={fmtCount(r.tokens)}>{fmtTokens(r.tokens)}</td>
                <td className="num mono">{fmtUsd(r.cost_usd)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export function TopRunsTable({ runs, initialOpen = null }: { runs: UsageTopRun[]; initialOpen?: string | null }) {
  const [open, setOpen] = useState<string | null>(initialOpen)
  if (runs.length === 0) return <Empty title="No AI work in this period" />
  return (
    <div className="table-scroll">
      <table className="table usage-table">
        <thead>
          <tr>
            <th>Started</th>
            <th>Feature</th>
            <th>Chat</th>
            <th className="num">Calls</th>
            <th className="num">Tokens</th>
            <th className="num">Est. cost</th>
            <th>Trace</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => {
            const isOpen = open === r.trace
            return (
              <Fragment key={r.trace}>
                <tr data-trace={r.trace}>
                  <td className="nowrap" title={isoTitle(r.started_at)}>
                    {dateTime(r.started_at)}
                  </td>
                  <td className="usage-label">
                    {r.feature_label || r.feature}
                    {r.models.length > 0 && <div className="usage-sub">{r.models.join(', ')}</div>}
                  </td>
                  <td className="usage-label mono">{r.thread_id ?? <span className="dimmer">(no chat)</span>}</td>
                  <td className="num mono">{fmtCount(r.calls)}</td>
                  <td className="num mono" title={fmtCount(r.tokens)}>{fmtTokens(r.tokens)}</td>
                  <td className="num mono">{fmtUsd(r.cost_usd)}</td>
                  <td className="nowrap">
                    {r.run_id !== null && r.run_id !== undefined ? (
                      <Link className="btn btn-sm" to={`/system/runs/${r.run_id}`}>
                        Run #{r.run_id}
                      </Link>
                    ) : (
                      <button
                        className="btn btn-sm btn-quiet"
                        aria-expanded={isOpen}
                        onClick={() => setOpen(isOpen ? null : r.trace)}
                      >
                        {isOpen ? 'Hide trace' : 'Show trace'}
                      </button>
                    )}
                  </td>
                </tr>
                {isOpen && r.run_id == null && (
                  <tr data-trace-expand={r.trace}>
                    <td className="usage-expand-cell" colSpan={7}>
                      <div className="usage-expand">
                        <UsageTrace trace={r.trace} />
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/* ── Page body ─────────────────────────────────────────────────────────── */

export interface UsageViewProps {
  summary: UsageSummary
  period: UsagePeriod
  install: string | null
  onPeriod: (p: UsagePeriod) => void
  onInstall: (install: string | null) => void
  onExport: () => void
  exporting?: boolean
  exportError?: string | null
  refreshing?: boolean
}

export function UsageView(p: UsageViewProps) {
  const s = p.summary
  const installs = p.install && !s.installs.includes(p.install) ? [...s.installs, p.install] : s.installs
  const totals = s.period_totals
  const unpriced = unpricedNote(totals.unpriced_calls)
  const since = s.since ? fmtDay(s.since.slice(0, 10)) : ''
  const name = periodName(p.period)
  const view = budgetView(s.budget)
  return (
    <div className="stack" data-usage-page>
      <UsageTiles summary={s} />

      {s.budget.state === 'paused' && (
        <div className="banner banner-broken" data-budget-banner="paused">
          <span className="grow">{view.note}</span>
        </div>
      )}

      <div className="usage-controls">
        <Tabs value={p.period} onChange={p.onPeriod} options={PERIODS} />
        <select
          className="select usage-install"
          aria-label="Install"
          value={p.install ?? ''}
          onChange={(e) => p.onInstall(e.target.value || null)}
        >
          <option value="">All installs</option>
          {installs.map((i) => (
            <option key={i} value={i}>
              {i}
            </option>
          ))}
        </select>
        {p.refreshing && <span className="spin" aria-label="Refreshing" />}
        <span className="grow" />
        <button className="btn btn-sm" disabled={p.exporting} onClick={p.onExport}>
          <Download size={13} /> {p.exporting ? 'Exporting…' : 'Export CSV'}
        </button>
      </div>
      {p.exportError && <div className="banner banner-broken">{p.exportError}</div>}

      <p className="usage-period" data-period-totals>
        <strong>{name}</strong>: {fmtUsd(totals.cost_usd)} est. · {fmtTokens(totals.tokens)} tokens ·{' '}
        {fmtCount(totals.calls)} calls{since ? ` since ${since}` : ''}
        {s.timezone ? ` (days in ${s.timezone})` : ''}
        {unpriced ? ` · ${unpriced}` : ''}
      </p>

      <Section title="By model" meta={name} flush>
        <BreakdownTable rows={s.by_model} first="Model" empty="No model calls in this period" />
      </Section>
      <Section title="By feature" meta={name} flush>
        <BreakdownTable rows={s.by_feature} first="Feature" empty="No model calls in this period" />
      </Section>
      <Section title="By agent" meta={name} flush>
        <BreakdownTable rows={s.by_agent} first="Agent" empty="No model calls in this period" />
      </Section>
      <Section title="By chat" meta={name} flush>
        <BreakdownTable rows={s.by_chat} first="Chat" empty="No model calls in this period" />
      </Section>
      <Section title="Top 20 most expensive runs" meta={name} flush>
        <TopRunsTable runs={s.top_runs.slice(0, 20)} />
      </Section>
    </div>
  )
}

export default function Usage() {
  const [period, setPeriod] = useState<UsagePeriod>('month')
  const [install, setInstall] = useState<string | null>(null)
  const exporter = useAction()
  const summary = useResource(() => loadUsageSummary(period, install), {
    interval: 30_000,
    deps: [period, install],
  })
  const asOf = summary.data?.price_table_date ? ` as of ${fmtDay(summary.data.price_table_date)}` : ''

  return (
    <>
      <PageHeader
        title="Usage"
        lede={`Where the AI tokens and the money go. Costs are estimates at list prices${asOf}.`}
      />
      {summary.error && !summary.data ? (
        <ErrorNote message={summary.error} onRetry={() => void summary.reload()} />
      ) : summary.loading && !summary.data ? (
        <Loading label="Reading usage" />
      ) : summary.data ? (
        <UsageView
          summary={summary.data}
          period={period}
          install={install}
          onPeriod={setPeriod}
          onInstall={setInstall}
          refreshing={summary.loading}
          exporting={exporter.pending}
          exportError={exporter.error}
          onExport={() =>
            void exporter.run(() => download(usageExportPath(period, install), usageExportName(period)))
          }
        />
      ) : null}
    </>
  )
}

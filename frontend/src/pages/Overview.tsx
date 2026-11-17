import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { get } from '../lib/api'
import { asks } from '../lib/ask'
import { BRANDS } from '../lib/brand'
import { fetchInsights, type Insight } from '../lib/contracts'
import { ago, truncate } from '../lib/format'
import { useResource } from '../lib/hooks'
import { useOwed } from '../lib/owed'
import type { DashboardListItem, KpiSeries } from '../lib/ictypes'
import type { Watch } from '../lib/types'
import { OwedColumn } from '../components/OwedColumn'
import { useAsk } from '../components/AskPanel'
import { Sparkline, seriesValues } from '../components/Sparkline'
import { Empty, Loading, PageHeader } from '../components/ui'
import { SeverityPill } from '../components/InsightEvidence'
import './ic.css'
import './overview.css'

/* The intelligence home: how the business is doing, what was noticed, which
 * dashboards exist, and (in the one gold column) what a human owes. */

export interface Tile {
  watch: Watch
  series: KpiSeries | null
}

export interface OverviewViewProps {
  tiles: Tile[]
  tilesLoading: boolean
  insights: { available: boolean; items: Insight[] } | null
  dashboards: DashboardListItem[]
  owedColumn: ReactNode
  owedTotal?: number
  onAskDashboard: () => void
  onAskKpi: () => void
  onMakeKpi: (i: Insight) => void
}

function fmtValue(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return Math.abs(v) >= 1000 ? v.toLocaleString() : String(Math.round(v * 100) / 100)
}

export function OverviewView(p: OverviewViewProps) {
  const waiting = p.owedTotal ?? 0
  return (
    <div className="ic-page ov">
      <PageHeader
        title="Overview"
        lede={
          <>
            {BRANDS.intelligence.tagline}
            {waiting > 0 && (
              <>
                {' '}
                <span style={{ color: 'var(--owed-hi)' }}>
                  {waiting === 1 ? '1 thing waiting on you' : `${waiting} things waiting on you`}
                </span>
              </>
            )}
          </>
        }
        actions={
          <>
            <button className="btn btn-sm" onClick={p.onAskKpi}>
              Ask for a new KPI
            </button>
            <button className="btn btn-sm" onClick={p.onAskDashboard}>
              Ask for a dashboard
            </button>
          </>
        }
      />
      <div className="ov-grid">
        <div className="ov-main">
          <section aria-labelledby="ov-kpis">
            <div className="side-head">
              <span className="eyebrow" id="ov-kpis">
                KPIs
              </span>
            </div>
            {p.tilesLoading && p.tiles.length === 0 ? (
              <Loading label="Reading KPIs" />
            ) : p.tiles.length === 0 ? (
              <Empty
                title="No KPIs yet"
                action={
                  <button className="btn btn-sm" onClick={p.onAskKpi}>
                    Ask for a new KPI
                  </button>
                }
              >
                Ask the agent to define one; it may start watching right away.
              </Empty>
            ) : (
              <div className="ov-tiles">
                {p.tiles.map(({ watch, series }) => {
                  const vals = series ? seriesValues(series) : []
                  const latest = series?.latest
                  return (
                    <Link key={watch.id} to={`/kpis/${watch.id}`} className="ov-tile">
                      <span className="ov-tile-title">{watch.title}</span>
                      <span className="ov-tile-value">{fmtValue(latest?.value)}</span>
                      <span className="ic-meta">{series?.metric ?? ''}</span>
                      {vals.length >= 2 && <Sparkline values={vals} />}
                      <span className="ic-meta">
                        checked {watch.last_checked_at ? ago(watch.last_checked_at) : 'never'}
                      </span>
                    </Link>
                  )
                })}
              </div>
            )}
          </section>

          <section aria-labelledby="ov-ins">
            <div className="side-head">
              <span className="eyebrow" id="ov-ins">
                Newest insights
              </span>
              <Link to="/insights">All insights</Link>
            </div>
            {!p.insights ? (
              <Loading label="Reading insights" />
            ) : !p.insights.available ? (
              <p className="dimmer">
                Insights appear after the nightly explore_data exploration playbook runs. See{' '}
                <Link to="/playbooks">Playbooks</Link>.
              </p>
            ) : p.insights.items.length === 0 ? (
              <p className="dimmer">Nothing noticed yet</p>
            ) : (
              <div className="ic-list">
                {p.insights.items.map((i) => (
                  <div className="ic-row" key={i.key}>
                    <span className="ic-row-title">{i.title}</span>
                    {i.body && <span className="ic-body dim">{truncate(i.body, 160)}</span>}
                    <span className="ic-meta">
                      <SeverityPill severity={i.severity} />{' '}
                      {i.at ? ago(i.at) : ''}
                    </span>
                    <div>
                      <button className="btn btn-sm" onClick={() => p.onMakeKpi(i)}>
                        Make this a KPI
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section aria-labelledby="ov-dash">
            <div className="side-head">
              <span className="eyebrow" id="ov-dash">
                Dashboards
              </span>
              <Link to="/dashboards">All dashboards</Link>
            </div>
            {p.dashboards.length === 0 ? (
              <p className="dimmer">No dashboards yet. Ask for one and the agent will draft it.</p>
            ) : (
              <div className="ic-list">
                {p.dashboards.map((d) => (
                  <div className="ic-row" key={d.id}>
                    <Link className="ic-row-title" to={`/dashboards/${d.slug}`}>
                      {d.title}
                    </Link>
                    <span className="ic-meta">
                      {d.current_version !== null ? `v${d.current_version}` : 'not published yet'} ·{' '}
                      {d.last_refreshed_at
                        ? `refreshed ${ago(d.last_refreshed_at)}`
                        : 'not refreshed yet'}
                    </span>
                    {d.current_last_error && (
                      <span className="ic-owed-line">{truncate(d.current_last_error, 140)}</span>
                    )}
                  </div>
                ))}
                <div style={{ paddingTop: 'var(--s3)' }}>
                  <button className="btn btn-sm" onClick={p.onAskDashboard}>
                    Ask for a dashboard
                  </button>
                </div>
              </div>
            )}
          </section>
        </div>
        <div className="ov-rule" aria-hidden="true" />
        <aside className="ov-owed" aria-label="Needs you">
          <div className="side-head">
            <span className="eyebrow">Needs you</span>
          </div>
          {p.owedColumn}
        </aside>
      </div>
    </div>
  )
}

export default function Overview() {
  const owed = useOwed()
  const { open, send } = useAsk()

  const tiles = useResource<Tile[]>(
    async () => {
      const all = await get<Watch[]>('/api/watches')
      const live = all
        .filter((w) => w.status === 'active' || w.status === 'awaiting_confirmation')
        .sort((a, b) => (a.kind === 'kpi' ? 0 : 1) - (b.kind === 'kpi' ? 0 : 1) || a.id - b.id)
        .slice(0, 8)
      return Promise.all(
        live.map(async (watch) => ({
          watch,
          series: await get<KpiSeries>(`/api/watches/${watch.id}/history?days=30`).catch(() => null),
        })),
      )
    },
    { interval: 60_000 },
  )
  const dashboards = useResource(
    () => get<DashboardListItem[]>('/api/dashboards?limit=6').catch(() => [] as DashboardListItem[]),
    { interval: 60_000 },
  )
  const insights = useResource(
    () => fetchInsights(5).catch(() => ({ available: true, items: [] as Insight[] })),
    { interval: 300_000 },
  )

  return (
    <OverviewView
      tiles={tiles.data ?? []}
      tilesLoading={tiles.loading}
      insights={insights.data}
      dashboards={dashboards.data ?? []}
      owedColumn={<OwedColumn />}
      owedTotal={owed.counts.total}
      onAskDashboard={() => open(asks.dashboard(''))}
      onAskKpi={() => open(asks.newKpi(''))}
      onMakeKpi={(i) => void send(asks.insightToKpi(i))}
    />
  )
}

import { useEffect, useRef, useState } from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'
import { get, patch } from '../lib/api'
import { asks } from '../lib/ask'
import { ago, isoTitle } from '../lib/format'
import { useAction, useResource } from '../lib/hooks'
import {
  KPI_TITLE_ID,
  kpiNeighbours,
  kpiOrder,
  listScrollTop,
  pickKpi,
  planReveal,
  revealBehavior,
  revealScrollTop,
  type KpiNeighbours,
} from '../lib/kpinav'
import { runsHref } from '../lib/runs'
import type { DashboardListItem, KpiSeries } from '../lib/ictypes'
import { eventLabel, type Watch, type WatchEvent } from '../lib/types'
import { WatchFacts } from './Watches'
import { useAsk } from '../components/AskPanel'
import { TrendChart } from '../components/Sparkline'
import { Empty, ErrorNote, Loading, PageHeader, StatusPill } from '../components/ui'
import './ic.css'

/* KPIs: read the 30-day trend of a watch and ask the agent to pin it to a
 * dashboard. Creation goes through the agent (useAsk); only pause/resume is a
 * direct call, because that is a decision, not creation. */

export function KpiListView({ watches, selectedId }: { watches: Watch[]; selectedId: number | null }) {
  const ordered = kpiOrder(watches)
  return (
    <div className="ic-list kpi-list">
      {ordered.map((w) => (
        <div className={w.id === selectedId ? 'ic-row is-current' : 'ic-row'} key={w.id}>
          <Link
            className="ic-row-title"
            to={`/kpis/${w.id}`}
            aria-current={w.id === selectedId ? 'true' : undefined}
          >
            {w.title}
          </Link>
          <span className="ic-meta">
            {w.kind} · {w.status.replace(/_/g, ' ')}
          </span>
        </div>
      ))}
    </div>
  )
}

function KpiSteps({ steps }: { steps: KpiNeighbours }) {
  const { prev, next } = steps
  return (
    <nav className="kpi-step" aria-label="Step through KPIs">
      {prev ? (
        <Link
          className="btn btn-quiet btn-sm"
          to={`/kpis/${prev.id}`}
          aria-label={`Previous KPI: ${prev.title}`}
          title={prev.title}
        >
          <span aria-hidden="true">←</span> Previous
        </Link>
      ) : (
        <span className="btn btn-quiet btn-sm is-off" aria-hidden="true">
          ← Previous
        </span>
      )}
      <span className="ic-meta">
        {steps.position} of {steps.total}
      </span>
      {next ? (
        <Link
          className="btn btn-quiet btn-sm"
          to={`/kpis/${next.id}`}
          aria-label={`Next KPI: ${next.title}`}
          title={next.title}
        >
          Next <span aria-hidden="true">→</span>
        </Link>
      ) : (
        <span className="btn btn-quiet btn-sm is-off" aria-hidden="true">
          Next →
        </span>
      )}
    </nav>
  )
}

export interface KpiDetailProps {
  watch: Watch
  series: KpiSeries | null
  events: WatchEvent[]
  dashboards: DashboardListItem[]
  busy?: boolean
  error?: string | null
  onPin: (slug: string | undefined) => void
  onToggle: () => void
  onAskKpi: () => void
  /* Quick 041: Previous/Next neighbours in list order; optional so other renders stay valid. */
  steps?: KpiNeighbours | null
}

export function KpiDetailView(p: KpiDetailProps) {
  const [slug, setSlug] = useState('')
  const { watch, series } = p
  return (
    <div>
      <div className="kpi-head">
        <h2 className="panel-title" id={KPI_TITLE_ID} tabIndex={-1}>
          {watch.title}
        </h2>
        {p.steps && <KpiSteps steps={p.steps} />}
      </div>
      <p className="ic-meta">
        <StatusPill tone="neutral" label={watch.status.replace(/_/g, ' ')} />
        {watch.open_case_id != null && (
          <>
            {' '}
            {/* Quick 006: a critical alert of this KPI is being tracked as a case. */}
            <Link to={`/cases/${watch.open_case_id}`}>Alert case #{watch.open_case_id} is open</Link>
          </>
        )}
      </p>
      {watch.compile_error && <div className="banner banner-broken">{watch.compile_error}</div>}
      {p.error && <div className="banner banner-broken">{p.error}</div>}
      <p className="ic-body">{watch.condition}</p>
      <WatchFacts watch={watch} />
      {series?.latest && (
        <p>
          <span style={{ fontSize: 'var(--fs-head)' }}>
            {series.latest.value === null ? '—' : String(series.latest.value)}
          </span>{' '}
          <span className="ic-meta">
            {series.metric ?? ''} · checked{' '}
            {watch.last_checked_at ? ago(watch.last_checked_at) : ago(series.latest.t)}
          </span>
        </p>
      )}
      <div className="ic-trend">
        {series ? (
          <TrendChart points={series.points} metric={series.metric} />
        ) : (
          <p className="dimmer">Not enough history yet</p>
        )}
      </div>
      <div className="btn-row">
        <select
          aria-label="Dashboard to pin to"
          value={slug}
          onChange={(e) => setSlug(e.target.value)}
        >
          <option value="">A new dashboard</option>
          {p.dashboards.map((d) => (
            <option key={d.slug} value={d.slug}>
              {d.title}
            </option>
          ))}
        </select>
        <button className="btn btn-sm" onClick={() => p.onPin(slug || undefined)}>
          Pin to dashboard
        </button>
        <button className="btn btn-sm" onClick={p.onToggle} disabled={p.busy}>
          {watch.status === 'paused' ? 'Resume' : 'Pause'}
        </button>
        <button className="btn btn-sm" onClick={p.onAskKpi}>
          Ask for a new KPI
        </button>
        <Link className="btn btn-quiet btn-sm" to={runsHref(`watch:${watch.id}`)}>
          Check history
        </Link>
      </div>
      <h3 className="eyebrow" style={{ marginTop: 'var(--s5)' }}>
        Recent events
      </h3>
      {p.events.length === 0 ? (
        <p className="dimmer">No events yet</p>
      ) : (
        <div className="ic-list">
          {p.events.slice(0, 10).map((e, i) => (
            <div className="ic-row" key={i}>
              <span className="ic-body">
                {eventLabel(e.kind) ? `${eventLabel(e.kind)}: ` : ''}
                {e.summary}
              </span>
              <span className="ic-meta" title={isoTitle(e.created_at)}>
                {eventLabel(e.kind) ? '' : `${e.severity} · `}
                {ago(e.created_at)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function Kpis() {
  const params = useParams()
  const { open, send } = useAsk()
  const act = useAction()
  const [note, setNote] = useState<string | null>(null)

  const watches = useResource(() => get<Watch[]>('/api/watches'), { interval: 60_000 })
  const list = watches.data ?? []
  const ordered = kpiOrder(list)
  /* A bare /kpis opens the first KPI in list order (so the page starts at "1 of N"); /kpis/:id opens that one. */
  const picked = pickKpi(ordered, params.id)
  const id = picked?.id ?? null
  const steps = kpiNeighbours(ordered, id)

  const location = useLocation()
  const listRef = useRef<HTMLDivElement>(null)
  const detailRef = useRef<HTMLDivElement>(null)
  const lastKey = useRef<string | null>(null)

  /* Bring the picked KPI into view after a navigation. location.key changes on every
   * navigation (list click, Previous/Next, back/forward, even re-picking the open KPI) and never on
   * first load or a poll, so those never scroll. The window only moves when the heading is out of
   * view, below the sticky top bar. Focus goes to the heading unless the user stepped with
   * Previous/Next, so repeated stepping keeps focus on the link. */
  useEffect(() => {
    const previousKey = lastKey.current
    lastKey.current = location.key
    const heading = detailRef.current?.querySelector<HTMLElement>('#' + KPI_TITLE_ID) ?? null
    if (!heading) return
    const headerHeight = document.querySelector<HTMLElement>('.topbar')?.getBoundingClientRect().height ?? 0
    const r = heading.getBoundingClientRect()
    const scrollTop = revealScrollTop({
      headingTop: r.top,
      headingBottom: r.bottom,
      viewportHeight: window.innerHeight,
      headerHeight,
      scrollY: window.scrollY,
    })
    const plan = planReveal({
      previousKey,
      key: location.key,
      hasDetail: true,
      scrollTop,
      focusInDetail: !!detailRef.current?.contains(document.activeElement),
    })
    if (plan.scroll && scrollTop !== null) {
      window.scrollTo({
        top: scrollTop,
        behavior: revealBehavior(window.matchMedia('(prefers-reduced-motion: reduce)').matches),
      })
    }
    if (plan.focus) heading.focus({ preventScroll: true })
  }, [location.key, id])

  /* Keep the open row visible inside the sticky list's own scrollbar (901 px and up). Moves only
   * the pane, never the window. */
  useEffect(() => {
    const pane = listRef.current
    if (!pane || pane.scrollHeight <= pane.clientHeight) return
    const row = pane.querySelector('[aria-current="true"]')?.closest('.ic-row')
    if (!row) return
    const rowRect = row.getBoundingClientRect()
    const itemTop = rowRect.top - pane.getBoundingClientRect().top + pane.scrollTop
    const next = listScrollTop({
      itemTop,
      itemBottom: itemTop + rowRect.height,
      scrollTop: pane.scrollTop,
      height: pane.clientHeight,
    })
    if (next !== null) pane.scrollTop = next
  }, [id])

  const series = useResource(
    () => (id === null ? Promise.resolve(null) : get<KpiSeries>(`/api/watches/${id}/history?days=30`)),
    { interval: 60_000, deps: [id] },
  )
  const events = useResource(
    () => (id === null ? Promise.resolve([]) : get<WatchEvent[]>(`/api/watches/${id}/events`)),
    { deps: [id] },
  )
  const dashboards = useResource(() =>
    get<DashboardListItem[]>('/api/dashboards?limit=100').catch(() => [] as DashboardListItem[]),
  )

  const askKpi = () => open(asks.newKpi(''))

  return (
    <div className="ic-page">
      <PageHeader
        title="KPIs"
        lede="The numbers the agent watches for you, with 30 days of history."
        actions={
          <button className="btn btn-sm" onClick={askKpi}>
            Ask for a new KPI
          </button>
        }
      />
      {watches.error && <ErrorNote message={watches.error} onRetry={() => void watches.reload()} />}
      {note && <div className="banner">{note}</div>}
      {watches.loading && !watches.data ? (
        <Loading label="Reading KPIs" />
      ) : list.length === 0 ? (
        <Empty
          title="No KPIs yet"
          action={
            <button className="btn btn-sm" onClick={askKpi}>
              Ask for a new KPI
            </button>
          }
        >
          Ask the agent to define one; it may start watching right away.
        </Empty>
      ) : (
        <div className="ic-split kpi-split">
          <div className="kpi-list-pane" ref={listRef}>
            <KpiListView watches={list} selectedId={id} />
          </div>
          <div className="kpi-detail-pane" ref={detailRef}>
          {picked ? (
            <KpiDetailView
              watch={picked}
              steps={steps}
              series={series.data}
              events={events.data ?? []}
              dashboards={dashboards.data ?? []}
              busy={act.pending}
              error={act.error}
              onPin={(slug) =>
                void send(asks.pinKpi({ id: picked.id, title: picked.title }, slug)).then((ok) =>
                  setNote(ok ? 'Sent to the agent. It will draft the dashboard for your approval.' : null),
                )
              }
              onToggle={() =>
                void act
                  .run(() =>
                    patch(`/api/watches/${picked.id}`, {
                      status: picked.status === 'paused' ? 'active' : 'paused',
                    }),
                  )
                  .then(() => watches.reload())
              }
              onAskKpi={askKpi}
            />
          ) : (
            <Empty title="KPI not found" />
          )}
          </div>
        </div>
      )}
    </div>
  )
}

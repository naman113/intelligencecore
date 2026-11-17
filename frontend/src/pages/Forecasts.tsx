import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { get } from '../lib/api'
import { asks } from '../lib/ask'
import { ago, isoTitle, shortDate } from '../lib/format'
import {
  isFailing,
  loadForecast,
  loadForecasts,
  methodLabel,
  runForecastNow,
  setForecastStatus,
  type ForecastActual,
  type ForecastBrief,
  type ForecastDetail,
  type ForecastPoint,
  type ForecastStatus,
} from '../lib/forecasts'
import { useAction, useResource } from '../lib/hooks'
import type { DashboardListItem } from '../lib/ictypes'
import { runsHref } from '../lib/runs'
import { useAsk } from '../components/AskPanel'
import { Empty, ErrorNote, Loading, PageHeader, StatusPill } from '../components/ui'
import './ic.css'

/* Phase 13.1: forecasts. Pause, resume, retire and run now are owner decisions
 * and call the ops routes; anything that creates something is a chat request to
 * the agent. Every string from the API is rendered as React text. */

const accuracy = (wape: number | null): string | null =>
  wape === null ? null : `typical miss ${Math.round(wape)}% (WAPE)`

/** Quick 013 (WR-05): the detail to show for the picked forecast. Null while the picked
 * one is still loading, so the page never shows one forecast while acting on another. */
export function detailFor(picked: number | null, data: ForecastDetail | null | undefined): ForecastDetail | null {
  return picked !== null && data != null && data.forecast_id === picked ? data : null
}

/** Quick 013 (WR-05): every owner action targets the forecast on screen, by its own id. */
export function forecastActions(
  d: Pick<ForecastDetail, 'forecast_id'>,
  ops: {
    setStatus: (id: number, s: ForecastStatus) => Promise<unknown>
    runNow: (id: number) => Promise<unknown>
  },
) {
  const id = d.forecast_id
  return {
    pause: () => ops.setStatus(id, 'paused'),
    resume: () => ops.setStatus(id, 'active'),
    retire: () => ops.setStatus(id, 'retired'),
    runNow: () => ops.runNow(id),
  }
}

export function ForecastListView({
  forecasts,
  selectedId,
  showRetired = false,
}: {
  forecasts: ForecastBrief[]
  selectedId: number | null
  showRetired?: boolean
}) {
  const rows = forecasts.filter((f) => showRetired || f.status !== 'retired')
  return (
    <div className="ic-list">
      {rows.map((f) => {
        const failing = isFailing(f)
        const acc = accuracy(f.wape)
        return (
          <div className="ic-row" key={f.id}>
            <Link
              className="ic-row-title"
              to={`/forecasts/${f.id}`}
              aria-current={f.id === selectedId ? 'true' : undefined}
            >
              {f.name}
            </Link>
            <span className="ic-meta">
              {f.horizon} {f.grain}s ahead
            </span>{' '}
            <StatusPill
              tone={failing ? 'owed' : f.status === 'active' ? 'settled' : 'neutral'}
              label={failing ? 'failing' : f.status}
            />
            <div className="ic-meta">
              {f.last_run_at ? `last run ${ago(f.last_run_at)}` : 'not run yet'} · {methodLabel(f.method)}
              {acc ? ` · ${acc}` : ''}
              {f.status === 'active' && f.next_run_at ? ` · next run ${ago(f.next_run_at)}` : ''}
            </div>
          </div>
        )
      })}
    </div>
  )
}

export function ForecastChart({
  actuals,
  forecast,
  height = 200,
}: {
  actuals: ForecastActual[]
  forecast: ForecastPoint[]
  height?: number
}) {
  if (actuals.length + forecast.length < 2 || forecast.length < 1 || actuals.length < 1) {
    return <p className="dimmer">Not enough history yet</p>
  }
  const W = 600
  const left = 56
  const right = 12
  const top = 12
  const bottom = 28
  const n = actuals.length + forecast.length
  const step = (W - left - right) / (n - 1)
  const X = (i: number) => left + i * step
  const all = [
    ...actuals.map((a) => a.value),
    ...forecast.flatMap((p) => [p.mean, p.low, p.high]),
  ]
  const lo = Math.min(...all)
  const hi = Math.max(...all)
  const span = hi - lo || 1
  const Y = (v: number) => top + (1 - (v - lo) / span) * (height - top - bottom)
  const pt = (i: number, v: number) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`
  const a0 = actuals.length
  const actualLine = actuals.map((a, i) => pt(i, a.value)).join(' ')
  const lastActual = actuals[a0 - 1]!
  const forecastLine = [pt(a0 - 1, lastActual.value), ...forecast.map((p, i) => pt(a0 + i, p.mean))].join(' ')
  const band =
    forecast.map((p, i) => pt(a0 + i, p.high)).join(' ') +
    ' ' +
    forecast
      .map((p, i) => pt(a0 + i, p.low))
      .reverse()
      .join(' ')
  const fmt = (v: number) => (Math.abs(v) >= 1000 ? Math.round(v).toLocaleString() : String(Math.round(v * 100) / 100))
  const lbl = (s: string) => shortDate(s)
  return (
    <svg className="trend" viewBox={`0 0 ${W} ${height}`} width="100%" role="img" aria-label="Actuals and forecast with 80 percent range">
      <polygon points={band} fill="var(--signal)" fillOpacity="0.14" />
      <polyline points={actualLine} fill="none" stroke="var(--signal-hi)" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
      <polyline
        points={forecastLine}
        fill="none"
        stroke="var(--signal-hi)"
        strokeWidth="2"
        strokeDasharray="6 4"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      <text x={left - 6} y={top + 4} textAnchor="end" fontSize="11" fill="var(--t3)">
        {fmt(hi)}
      </text>
      <text x={left - 6} y={height - bottom} textAnchor="end" fontSize="11" fill="var(--t3)">
        {fmt(lo)}
      </text>
      <text x={left} y={height - 8} textAnchor="start" fontSize="11" fill="var(--t3)">
        {lbl(actuals[0]!.period)}
      </text>
      <text x={X(a0 - 1)} y={height - 8} textAnchor="middle" fontSize="11" fill="var(--t3)">
        {lbl(lastActual.period)}
      </text>
      <text x={W - right} y={height - 8} textAnchor="end" fontSize="11" fill="var(--t3)">
        {lbl(forecast[forecast.length - 1]!.period)}
      </text>
    </svg>
  )
}

export interface ForecastDetailProps {
  detail: ForecastDetail
  brief?: ForecastBrief | null
  dashboards: DashboardListItem[]
  busy?: boolean
  error?: string | null
  onPause: () => void
  onResume: () => void
  onRetire: () => void
  onRunNow: () => void
  onPin: (slug: string | undefined) => void
}

export function ForecastDetailView(p: ForecastDetailProps) {
  const [slug, setSlug] = useState('')
  const [confirm, setConfirm] = useState(false)
  const d = p.detail
  const failing = isFailing(d)
  const acc = accuracy(d.wape)
  const ch = d.change
  return (
    <div>
      <h2 className="panel-title">{d.name}</h2>
      <p className="ic-meta">
        <StatusPill
          tone={failing ? 'owed' : d.status === 'active' ? 'settled' : 'neutral'}
          label={failing ? 'failing' : d.status}
        />{' '}
        {d.horizon} {d.grain}s ahead · {methodLabel(d.method)}
        {d.as_of ? ` · as of ${shortDate(d.as_of)}` : ''}
      </p>
      {failing && (
        <div className="banner banner-broken">
          {d.last_error}
          {d.failing_since ? ` (failing since ${ago(d.failing_since)})` : ''}
        </div>
      )}
      {p.error && <div className="banner banner-broken">{p.error}</div>}
      <div className="ic-trend">
        <ForecastChart actuals={d.actuals} forecast={d.forecast} />
      </div>
      {acc && <p className="ic-body">{acc}</p>}
      {ch && (
        <p className="ic-meta">
          Forecast total now {ch.total_forecast_now ?? '—'}, before {ch.total_forecast_before ?? '—'}
          {ch.method_changed ? `; method changed from ${methodLabel(ch.previous_method)} to ${methodLabel(d.method)}` : ''}
        </p>
      )}
      {d.notes.length > 0 && (
        <ul className="ic-body">
          {d.notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}
      {d.next_run_at && d.status === 'active' && (
        <p className="ic-meta" title={isoTitle(d.next_run_at)}>
          next run {ago(d.next_run_at)}
        </p>
      )}
      <p className="ic-meta">
        <Link to={runsHref(`forecast:${d.forecast_id}`)}>Run history</Link>
      </p>
      <div className="btn-row">
        <select aria-label="Dashboard to chart on" value={slug} onChange={(e) => setSlug(e.target.value)}>
          <option value="">A new dashboard</option>
          {p.dashboards.map((x) => (
            <option key={x.slug} value={x.slug}>
              {x.title}
            </option>
          ))}
        </select>
        <button className="btn btn-sm" onClick={() => p.onPin(slug || undefined)}>
          Chart on a dashboard
        </button>
        {d.status === 'active' && (
          <button className="btn btn-sm" disabled={p.busy} onClick={p.onPause}>
            Pause
          </button>
        )}
        {d.status === 'paused' && (
          <button className="btn btn-sm" disabled={p.busy} onClick={p.onResume}>
            Resume
          </button>
        )}
        {d.status === 'active' && (
          <button className="btn btn-sm" disabled={p.busy} onClick={p.onRunNow}>
            Run now
          </button>
        )}
        {d.status !== 'retired' &&
          (confirm ? (
            <>
              <button
                className="btn btn-sm"
                disabled={p.busy}
                onClick={() => {
                  setConfirm(false)
                  p.onRetire()
                }}
              >
                Confirm retire
              </button>
              <button className="btn btn-quiet btn-sm" onClick={() => setConfirm(false)}>
                Cancel
              </button>
            </>
          ) : (
            <button className="btn btn-sm" disabled={p.busy} onClick={() => setConfirm(true)}>
              Retire
            </button>
          ))}
      </div>
    </div>
  )
}

export default function Forecasts() {
  const params = useParams()
  const { open, send } = useAsk()
  const act = useAction()
  const [note, setNote] = useState<string | null>(null)
  const [showRetired, setShowRetired] = useState(false)

  const list = useResource(() => loadForecasts(showRetired), { interval: 60_000, deps: [showRetired] })
  const rows = (list.data ?? []).filter((f) => showRetired || f.status !== 'retired')
  const pickedBrief =
    rows.find((f) => String(f.id) === params.id) ?? (params.id ? undefined : rows[0])
  const id = pickedBrief?.id ?? null

  const detail = useResource(
    () => (id === null ? Promise.resolve(null) : loadForecast(id)),
    { interval: 60_000, deps: [id] },
  )
  const shown = detailFor(id, detail.data)
  const acts = shown ? forecastActions(shown, { setStatus: setForecastStatus, runNow: runForecastNow }) : null
  const dashboards = useResource(() =>
    get<DashboardListItem[]>('/api/dashboards?limit=100').catch(() => [] as DashboardListItem[]),
  )

  const askNew = () => open(asks.newForecast(''))
  const reloadAll = async () => {
    await list.reload()
    await detail.reload()
  }
  const runAct = (fn: () => Promise<unknown>) =>
    void act.run(fn).then(() => reloadAll())

  return (
    <div className="ic-page">
      <PageHeader
        title="Forecasts"
        lede="Forecasts the agent set up with your approval. They refresh every night with no model cost."
        actions={
          <>
            <button className="btn btn-quiet btn-sm" onClick={() => setShowRetired((v) => !v)}>
              {showRetired ? 'Hide retired' : 'Show retired'}
            </button>
            <button className="btn btn-sm" onClick={askNew}>
              Ask for a new forecast
            </button>
          </>
        }
      />
      {list.error && <ErrorNote message={list.error} onRetry={() => void list.reload()} />}
      {note && <div className="banner">{note}</div>}
      {list.loading && !list.data ? (
        <Loading label="Reading forecasts" />
      ) : rows.length === 0 ? (
        <Empty
          title="No forecasts yet"
          action={
            <button className="btn btn-sm" onClick={askNew}>
              Ask for a new forecast
            </button>
          }
        >
          Ask the agent to propose one; it runs nightly once you approve it.
        </Empty>
      ) : (
        <div className="ic-split">
          <ForecastListView forecasts={rows} selectedId={id} showRetired={showRetired} />
          {shown && acts ? (
            // keyed on the id so a half-confirmed retire never carries over to another forecast
            <ForecastDetailView
              key={shown.forecast_id}
              detail={shown}
              brief={pickedBrief}
              dashboards={dashboards.data ?? []}
              busy={act.pending}
              error={
                act.error && /429|5 minutes/i.test(act.error) ? 'Wait 5 minutes between runs.' : act.error
              }
              onPause={() => runAct(acts.pause)}
              onResume={() => runAct(acts.resume)}
              onRetire={() => runAct(acts.retire)}
              onRunNow={() => runAct(acts.runNow)}
              onPin={(slug) =>
                void send(asks.pinForecast({ id: shown.forecast_id, name: shown.name }, slug)).then((ok) =>
                  setNote(ok ? 'Sent to the agent. It will draft the dashboard for your approval.' : null),
                )
              }
            />
          ) : pickedBrief ? (
            <Loading label="Reading forecast" />
          ) : (
            <Empty title="Forecast not found" />
          )}
        </div>
      )}
    </div>
  )
}

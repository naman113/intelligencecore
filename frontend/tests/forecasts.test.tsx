import { test } from 'node:test'
import assert from 'node:assert/strict'
import ReactDOMServer from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { detailFor, ForecastChart, ForecastDetailView, ForecastListView, forecastActions } from '../src/pages/Forecasts'
import {
  decodeForecastBrief,
  decodeForecastDetail,
  isFailing,
  methodLabel,
  type ForecastBrief,
} from '../src/lib/forecasts'
import { dashboards } from './fixtures'

const brief = (o: Record<string, unknown> = {}): ForecastBrief =>
  decodeForecastBrief({
    id: 4,
    name: 'Daily diesel',
    grain: 'day',
    horizon: 8,
    status: 'active',
    last_run_at: new Date().toISOString(),
    next_run_at: new Date(Date.now() + 3600_000).toISOString(),
    method: 'seasonal_naive',
    wape: 12,
    ...o,
  })

const actuals = Array.from({ length: 30 }, (_, i) => ({
  period: `2026-09-${String((i % 28) + 1).padStart(2, '0')}`,
  value: 100 + i,
}))
const fcst = Array.from({ length: 8 }, (_, i) => ({
  period: `2026-10-0${i + 1}`,
  mean: 130 + i,
  low: 120 + i,
  high: 140 + i,
}))

const detail = (o: Record<string, unknown> = {}) =>
  decodeForecastDetail({
    forecast_id: 4,
    name: 'Daily diesel',
    grain: 'day',
    horizon: 8,
    status: 'active',
    as_of: '2026-10-01',
    method: 'ets',
    wape: 12,
    notes: ['Few holidays in range'],
    actuals,
    forecast: fcst,
    change: { total_forecast_now: 1000, total_forecast_before: 900, method_changed: true, previous_method: 'naive' },
    ...o,
  })

const noop = () => {}
const view = (d: ReturnType<typeof detail>) =>
  ReactDOMServer.renderToStaticMarkup(
    <MemoryRouter>
      <ForecastDetailView
        detail={d}
        dashboards={dashboards}
        onPause={noop}
        onResume={noop}
        onRetire={noop}
        onRunNow={noop}
        onPin={noop}
      />
    </MemoryRouter>,
  )

test('list: name, horizon, status, accuracy, method, hides retired', () => {
  const html = ReactDOMServer.renderToStaticMarkup(
    <MemoryRouter>
      <ForecastListView
        forecasts={[
          brief(),
          brief({ id: 5, name: 'Old one', status: 'retired' }),
          brief({ id: 6, name: 'Bad', last_error: 'boom' }),
        ]}
        selectedId={4}
      />
    </MemoryRouter>,
  )
  assert.match(html, /Daily diesel/)
  assert.match(html, /8 days ahead/)
  assert.match(html, /typical miss 12% \(WAPE\)/)
  assert.match(html, /same period last season/)
  assert.match(html, /next run/)
  assert.match(html, /failing/)
  assert.doesNotMatch(html, /Old one/)
})

test('detail: chart paths, notes, change line, buttons', () => {
  const html = view(detail())
  assert.match(html, /stroke-dasharray="6 4"/)
  assert.match(html, /<polygon/)
  assert.match(html, /Few holidays in range/)
  assert.match(
    html,
    /Forecast total now 1000, before 900; method changed from last value to trend and season smoothing/,
  )
  for (const b of ['Pause', 'Run now', 'Retire', 'Chart on a dashboard']) assert.match(html, new RegExp(b))
  assert.doesNotMatch(html, /Resume/)
})

test('detail: failing banner and paused buttons', () => {
  const f = view(detail({ last_error: 'query returned no rows', failing_since: new Date().toISOString() }))
  assert.match(f, /query returned no rows/)
  assert.match(f, /failing since/)
  const p = view(detail({ status: 'paused' }))
  assert.match(p, /Resume/)
  assert.doesNotMatch(p, /Run now/)
})

test('chart: needs history', () => {
  const html = ReactDOMServer.renderToStaticMarkup(<ForecastChart actuals={[]} forecast={[]} />)
  assert.match(html, /Not enough history yet/)
})

test('decoders tolerate strings and unknown status', () => {
  const b = decodeForecastBrief({ id: '3', horizon: '7', wape: '0.2', status: 'weird' })
  assert.equal(b.id, 3)
  assert.equal(b.horizon, 7)
  assert.equal(b.wape, 0.2)
  assert.equal(b.status, 'active')
  const d = decodeForecastDetail({ actuals: JSON.stringify([{ period: 'a', value: '1' }]), forecast: 'junk', notes: 5 })
  assert.equal(d.actuals.length, 1)
  assert.deepEqual(d.forecast, [])
  assert.deepEqual(d.notes, [])
  assert.equal(isFailing({ status: 'active', last_error: 'x' }), true)
  assert.equal(isFailing({ status: 'paused', last_error: 'x' }), false)
  assert.equal(isFailing({ status: 'active', last_error: '  ' }), false)
  assert.equal(methodLabel('arima'), 'ARIMA')
})

// Quick 013 (WR-05): the page shows and acts on one forecast only
test('detailFor: a stale detail for another forecast is never shown', () => {
  const a = detail({ forecast_id: 4 })
  assert.equal(detailFor(4, a), a)
  assert.equal(detailFor(9, a), null) // picked 9 while 4's detail is still loaded: show Loading
  assert.equal(detailFor(null, a), null)
  assert.equal(detailFor(4, null), null)
})

test('forecastActions: every action targets the displayed forecast id', async () => {
  const calls: unknown[][] = []
  const ops = {
    setStatus: async (id: number, s: string) => void calls.push(['status', id, s]),
    runNow: async (id: number) => void calls.push(['run', id]),
  }
  const shown = detailFor(4, detail({ forecast_id: 4 }))!
  const acts = forecastActions(shown, ops)
  await acts.pause()
  await acts.resume()
  await acts.retire()
  await acts.runNow()
  assert.deepEqual(calls, [
    ['status', 4, 'paused'],
    ['status', 4, 'active'],
    ['status', 4, 'retired'],
    ['run', 4],
  ])
})

test('detail: links to its run history on Runs', () => {
  const html = view(detail({ forecast_id: 4 }))
  assert.match(html, /href="\/system\/runs\?ref=forecast%3A4"/)
  assert.match(html, /Run history/)
})

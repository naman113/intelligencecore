import { test } from 'node:test'
import assert from 'node:assert/strict'
import ReactDOMServer from 'react-dom/server'
import { HealthAlertsPanel, healthState } from '../src/components/HealthAlerts'
import { loadHealthOverview, type HealthAlert, type HealthOverview } from '../src/lib/health'

const ok = (detail = 'fine') => ({ state: 'ok' as const, detail })
const allOk = {
  worker: ok(), sentinel: ok(), dashboard_refresh: ok(), business_db: ok('not configured'),
}
const alert = (over: Partial<HealthAlert> = {}): HealthAlert => ({
  id: 1, component: 'sentinel', title: 'KPI watch checks have stopped', detail: 'no progress for 200 s',
  created_at: '2026-10-25T10:00:00Z', resolved_at: null, ...over,
})
const render = (data: HealthOverview | null, error?: string | null) =>
  ReactDOMServer.renderToStaticMarkup(<HealthAlertsPanel data={data} error={error} />)

test('healthState', () => {
  assert.equal(healthState(null), 'unknown')
  assert.equal(healthState({ alerts: [], checks: {} }), 'starting')
  assert.equal(healthState({ alerts: [], checks: { ...allOk, worker: { state: 'starting', detail: '' } } }), 'starting')
  assert.equal(healthState({ alerts: [], checks: { ...allOk, sentinel: { state: 'failing', detail: 'x' } } }), 'alert')
  assert.equal(healthState({ alerts: [alert()], checks: allOk }), 'alert')
  assert.equal(healthState({ alerts: [alert({ resolved_at: '2026-10-25T10:05:00Z' })], checks: allOk }), 'green')
  assert.equal(healthState({ alerts: [], checks: allOk }), 'green')
})

test('green panel', () => {
  const h = render({ alerts: [], checks: allOk })
  assert.match(h, /Loops and business database/)
  assert.match(h, /data-health-state="green"/)
  assert.match(h, /All checks ok/)
  assert.match(h, /No open alerts/)
  assert.match(h, /not configured/)
  const order = ['worker', 'sentinel', 'dashboard_refresh', 'business_db'].map((c) => h.indexOf(`data-health-check="${c}"`))
  assert.ok(order.every((i) => i >= 0))
  assert.deepEqual([...order].sort((a, b) => a - b), order)
  for (const l of ['Task worker', 'KPI watch checks', 'Dashboard refresh', 'Business database']) assert.match(h, new RegExp(l))
})

test('alert panel', () => {
  const h = render({ alerts: [alert()], checks: { ...allOk, sentinel: { state: 'failing', detail: 'no progress for 200 s' } } })
  assert.match(h, /data-health-state="alert"/)
  assert.match(h, /Needs attention/)
  assert.doesNotMatch(h, /All checks ok/)
  assert.equal(h.match(/data-health-alert="open"/g)?.length, 1)
  assert.match(h, /KPI watch checks have stopped/)
  assert.match(h, /since /)
})

test('resolved only is green', () => {
  const h = render({ alerts: [alert({ resolved_at: '2026-10-25T10:05:00Z' })], checks: allOk })
  assert.match(h, /data-health-state="green"/)
  assert.equal(h.match(/data-health-alert="resolved"/g)?.length, 1)
  assert.match(h, /resolved /)
  assert.match(h, /No open alerts/)
})

test('starting, unknown', () => {
  const s = render({ alerts: [], checks: {} })
  assert.match(s, /data-health-state="starting"/)
  assert.match(s, /Checks are starting/)
  const u = render(null, 'HTTP 500')
  assert.match(u, /data-health-state="unknown"/)
  assert.match(u, /Health checks unavailable/)
  assert.match(u, /HTTP 500/)
})

test('escaping, resolved cap, unknown component last', () => {
  assert.match(render({ alerts: [alert({ detail: '<img src=x>' })], checks: allOk }), /&lt;img/)
  const many = Array.from({ length: 15 }, (_, i) => alert({ id: i + 1, resolved_at: '2026-10-25T10:05:00Z' }))
  assert.equal(render({ alerts: many, checks: allOk }).match(/data-health-alert="resolved"/g)?.length, 10)
  const h = render({ alerts: [], checks: { ...allOk, zeta: ok() } })
  assert.ok(h.indexOf('data-health-check="zeta"') > h.indexOf('data-health-check="business_db"'))
})

test('loadHealthOverview', async () => {
  const orig = globalThis.fetch
  let url = ''
  try {
    globalThis.fetch = (async (u: string) => {
      url = String(u)
      return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } })
    }) as typeof fetch
    assert.deepEqual(await loadHealthOverview(), { alerts: [], checks: {} })
    assert.match(url, /\/api\/health\/alerts\?include_resolved=true&limit=20/)
    globalThis.fetch = (async () => {
      throw new Error('boom')
    }) as typeof fetch
    await assert.rejects(loadHealthOverview())
  } finally {
    globalThis.fetch = orig
  }
})

// Phase 14 (WH-08)
test('warehouse label and unknown state', () => {
  const checks = {
    ...allOk,
    warehouse: ok(),
    business_db: { state: 'unknown' as const, detail: 'waiting for the warehouse' },
  }
  const h = render({ alerts: [], checks })
  assert.match(h, /data-health-check="warehouse"/)
  assert.match(h, />Warehouse</)
  assert.match(h, /waiting/)
  assert.equal(healthState({ alerts: [], checks }), 'green')
})

import { test, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import {
  fetchInsights,
  fetchVersionQueries,
  mintPreview,
  normalizeInsight,
  queryDiff,
  readContextChange,
  readPublishDashboard,
} from '../src/lib/contracts'
import { ApiError } from '../src/lib/api'
import {
  ctxEdited,
  ctxHide,
  genericInsights,
  insightsEnvelope,
  phase7Insights,
  pubAbsolute,
  pubBare,
  pubRollback,
  pubWithUrl,
  ctxColumn,
} from './fixtures'

const realFetch = globalThis.fetch
afterEach(() => {
  globalThis.fetch = realFetch
})

let lastCall: { url: string; init?: RequestInit } | null = null
function stub(status: number, body: unknown) {
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    lastCall = { url: String(url), init }
    return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
  }) as typeof fetch
}

test('normalizeInsight splits first line and reads aliases', () => {
  const r = normalizeInsight(genericInsights[0])
  assert.deepEqual(r, {
    key: '4',
    kind: null,
    title: 'Revenue in South fell 18%',
    body: 'Driven by two accounts',
    severity: 'medium',
    evidenceSql: 'SELECT 1',
    caseId: 9,
    caseTitle: null,
    watchId: null,
    watchTitle: null,
    at: '2026-10-24T06:00:00Z',
    // Quick 006: the plain-words card fields, all empty for an older insight.
    summary: null,
    impact: null,
    nextStep: null,
    keyFigures: [],
    caseStatus: null,
    evidence: null,
  })
  const t = normalizeInsight({ id: 1, title: 'T', finding: 'line1\nline2' })
  assert.equal(t?.title, 'T')
  assert.equal(t?.body, 'line1\nline2')
  assert.equal(normalizeInsight({ summary: 'S', sql: 'SELECT 2', occurred_at: 'x' })?.evidenceSql, 'SELECT 2')
  assert.equal(normalizeInsight({ content: 'C' })?.title, 'C')
  assert.equal(normalizeInsight('nope'), null)
  assert.equal(normalizeInsight({ id: 1 }), null)
})

test('phase 7 envelope items normalize', () => {
  const [note, ev] = phase7Insights.items.map(normalizeInsight)
  assert.equal(note?.key, 'note:12')
  assert.equal(note?.kind, 'note')
  assert.equal(note?.title, 'Revenue in South fell 18%')
  assert.equal(note?.body, 'Driven by two accounts')
  assert.equal(note?.severity, 'critical')
  assert.equal(note?.caseId, 9)
  assert.equal(note?.caseTitle, 'Exploration 2026-10-24')
  assert.equal(note?.watchId, null)
  assert.equal(ev?.key, 'watch_event:19')
  assert.equal(ev?.kind, 'watch_event')
  assert.equal(ev?.watchId, 4)
  assert.equal(ev?.watchTitle, 'Overdue invoices')
  assert.equal(ev?.caseId, null)
})

test('fetchInsights shapes and unavailable', async () => {
  stub(200, phase7Insights)
  let r = await fetchInsights(5)
  assert.equal(lastCall?.url, '/api/insights?limit=5')
  assert.equal(r.available, true)
  assert.equal(r.items.length, 2)
  stub(200, genericInsights)
  r = await fetchInsights(5)
  assert.equal(r.items.length, 2)
  stub(200, insightsEnvelope)
  r = await fetchInsights(5)
  assert.equal(r.items.length, 2)
  for (const s of [404, 405]) {
    stub(s, { detail: 'nope' })
    assert.deepEqual(await fetchInsights(5), { available: false, items: [] })
  }
  stub(500, { detail: 'boom' })
  await assert.rejects(fetchInsights(5), (e) => e instanceof ApiError && e.status === 500)
})

test('readContextChange', () => {
  const p = readContextChange(JSON.stringify(ctxColumn.payload))
  assert.equal(p.op, 'propose')
  assert.equal(p.kind, 'column')
  assert.equal(p.before, 'region')
  const e = readContextChange(ctxEdited.payload)
  assert.equal(e.editedBy, 'owner')
  assert.equal(e.agentText, 'Tier of the customer.')
  const h = readContextChange(ctxHide.payload)
  assert.equal(h.op, 'hide')
  assert.equal(h.rejectedCount, 2)
})

test('readPublishDashboard', () => {
  const a = readPublishDashboard(pubWithUrl.payload)
  assert.equal(a.op, 'propose')
  assert.equal(a.version, 4)
  assert.equal(a.draftUrl, '/d/sales/v/4/?p=seedtoken')
  assert.equal(a.draftPath, '/d/sales/v/4/')
  assert.deepEqual(a.queriesAdded, ['orders_by_day'])
  assert.equal(a.hasQueryDiff, true)
  const b = readPublishDashboard(pubBare.payload)
  assert.equal(b.version, 5)
  assert.equal(b.draftUrl, null)
  assert.equal(b.draftPath, '/d/sales/v/5/')
  assert.equal(b.hasQueryDiff, false)
  const c = readPublishDashboard(pubAbsolute.payload)
  assert.equal(c.draftPath, '/d/sales/v/4/')
  const d = readPublishDashboard({ slug: 's', version: 2, path: '/d/s/v/2/?p=zz' })
  assert.equal(d.draftUrl, '/d/s/v/2/?p=zz')
  const r = readPublishDashboard(pubRollback.payload)
  assert.equal(r.op, 'rollback')
  assert.equal(r.version, 2)
  assert.equal(r.fromVersion, 3)
  assert.equal(readPublishDashboard('garbage').slug, null)
})

test('queryDiff', () => {
  const d = [
    { id: 'a', sql: 'S1' },
    { id: 'b', sql: 'S2', refresh_minutes: 5 },
    { id: 'n', sql: 'N' },
  ]
  const c = [
    { id: 'a', sql: 'S1' },
    { id: 'b', sql: 'S2', refresh_minutes: 10 },
    { id: 'z', sql: 'Z' },
  ]
  assert.deepEqual(queryDiff(d, c), { added: ['n'], removed: ['z'], changed: ['b'] })
})

test('fetchVersionQueries returns only queries', async () => {
  stub(200, { version: 3, code: '<script>x</script>', queries: [{ id: 'q', sql: 'SELECT 1', refresh_minutes: 5 }] })
  const q = await fetchVersionQueries('sales', 3)
  assert.equal(lastCall?.url, '/api/dashboards/sales/versions/3')
  assert.deepEqual(q, [{ id: 'q', sql: 'SELECT 1', refresh_minutes: 5 }])
  assert.ok(!('code' in q))
})

test('mintPreview', async () => {
  stub(200, { slug: 'sales', version: 3, preview_token: 'tok', expires_at: 'soon', path: '/d/sales/v/3/' })
  const r = await mintPreview('sales', 3)
  assert.equal(lastCall?.url, '/api/dashboards/sales/versions/3/preview')
  assert.equal(lastCall?.init?.method, 'POST')
  assert.deepEqual(r, { available: true, path: '/d/sales/v/3/', token: 'tok', expiresAt: 'soon' })
  for (const s of [404, 405]) {
    stub(s, {})
    assert.deepEqual(await mintPreview('sales', 3), { available: false })
  }
})

test('Phase 13.1: queryDiff sees source, forecast_id and watch_id', () => {
  const c = [{ id: 'f', sql: '', source: 'forecast', forecast_id: 1 }, { id: 'k', sql: '', source: 'kpi_history', watch_id: 2 }]
  const d = [{ id: 'f', sql: '', source: 'forecast', forecast_id: 2 }, { id: 'k', sql: '', source: 'kpi_history', watch_id: 3 }]
  assert.deepEqual(queryDiff(d, c), { added: [], removed: [], changed: ['f', 'k'] })
  assert.deepEqual(queryDiff(c, c).changed, [])
  const e = [{ id: 'f', sql: '' }]
  assert.deepEqual(queryDiff(e, [{ id: 'f', sql: '', source: 'forecast' }]).changed, ['f'])
})

test('Phase 13.1: fetchVersionQueries keeps source fields', async () => {
  stub(200, { version: 3, queries: [{ id: 'f', source: 'forecast', forecast_id: 4, refresh_minutes: 60 }] })
  const q = await fetchVersionQueries('sales', 3)
  assert.deepEqual(q, [{ id: 'f', sql: '', refresh_minutes: 60, source: 'forecast', forecast_id: 4 }])
})

test('readPublishDashboard reads Phase 18 spec keys defensively', () => {
  const v = readPublishDashboard({
    slug: 's',
    version: 2,
    format: 'spec',
    tiles_added: ['t1'],
    tiles_removed: [],
    tiles_changed: ['t2'],
    tiles: [{ id: 't1', type: 'bar', title: 'Sales', binding: 'measure', ref: 'revenue' }, { type: 'x' }, 7],
    measures: ['revenue'],
    queries: [{ id: 'q1', source: 'sql' }, { source: 'sql' }],
  })
  assert.equal(v.format, 'spec')
  assert.deepEqual(v.tilesAdded, ['t1'])
  assert.equal(v.hasTileDiff, true)
  assert.equal(v.tiles.length, 1)
  assert.deepEqual(v.measures, ['revenue'])
  assert.deepEqual(v.queryRefs, [{ id: 'q1', source: 'sql' }])
  const bad = readPublishDashboard({ slug: 's', version: 1, tiles: 'x', measures: 5, queries: {} })
  assert.equal(bad.tiles.length, 0)
  assert.equal(bad.queryRefs.length, 0)
  const old = readPublishDashboard({ slug: 's', version: 1 })
  assert.equal(old.format, null)
  assert.equal(old.hasTileDiff, false)
})

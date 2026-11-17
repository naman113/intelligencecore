import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ReactDOMServer from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { KpiDetailView, KpiListView } from '../src/pages/Kpis'
import type { Watch, WatchEvent } from '../src/lib/types'
import { dashboards, kpiSeries } from './fixtures'

const mk = (o: Partial<Watch>) =>
  ({
    id: 4,
    kind: 'kpi',
    title: 'Overdue invoices',
    condition: 'overdue total over 100',
    status: 'active',
    compile_error: null,
    last_checked_at: new Date().toISOString(),
    ...o,
  }) as unknown as Watch
const ev: WatchEvent[] = [
  { severity: 'warning', summary: 'Crossed threshold', escalated: false, created_at: new Date().toISOString() },
]

function detail(w: Watch) {
  return ReactDOMServer.renderToStaticMarkup(
    <MemoryRouter>
      <KpiDetailView
        watch={w}
        series={kpiSeries}
        events={ev}
        dashboards={dashboards}
        onPin={() => {}}
        onToggle={() => {}}
        onAskKpi={() => {}}
      />
    </MemoryRouter>,
  )
}

test('detail: trend, latest, condition, events, pin options', () => {
  const html = detail(mk({}))
  assert.match(html, /<polyline/)
  assert.match(html, /187/)
  assert.match(html, /checked just now/)
  assert.match(html, /overdue total over 100/)
  assert.match(html, /Crossed threshold/)
  assert.match(html, /A new dashboard/)
  for (const d of dashboards) assert.ok(html.includes(d.title))
  assert.match(html, /Pin to dashboard/)
  assert.match(html, /Ask for a new KPI/)
  assert.match(html, />Pause</)
  assert.ok(!/card-owed|--owed/.test(html))
})

test('detail: compile error is broken, paused offers resume', () => {
  const html = detail(mk({ compile_error: 'bad sql', status: 'paused' }))
  assert.match(html, /banner-broken">bad sql/)
  assert.match(html, />Resume</)
})

test('list puts kpis first and links to /kpis/id', () => {
  const html = ReactDOMServer.renderToStaticMarkup(
    <MemoryRouter>
      <KpiListView
        watches={[mk({ id: 1, kind: 'condition', title: 'Other' }), mk({ id: 2, title: 'Rev' })]}
        selectedId={2}
      />
    </MemoryRouter>,
  )
  assert.ok(html.indexOf('Rev') < html.indexOf('Other'))
  assert.match(html, /href="\/kpis\/2"/)
})

test('Kpis page never creates watches directly', () => {
  const src = readFileSync('src/pages/Kpis.tsx', 'utf8')
  assert.ok(!src.includes("post('/api/watches'"))
  assert.ok(src.includes('asks.pinKpi'))
  assert.ok(!/card-owed|--owed/.test(src))
})

test('phase 17: stale and fresh events are labelled, not alerts; chip, data age, measure', () => {
  const now = new Date().toISOString()
  const events: WatchEvent[] = [
    { severity: 'warning', summary: 'Orders lag', escalated: false, created_at: now, kind: 'stale_data' },
    { severity: 'info', summary: 'Back', escalated: false, created_at: now, kind: 'data_fresh' },
    { severity: 'critical', summary: 'Plain fire', escalated: false, created_at: now },
  ]
  const w = mk({
    stale_since: '2026-10-25T00:00:00Z',
    data_as_of: '2026-10-24T00:00:00Z',
    measure_ref: { measure: 'revenue', range: 'last_30d' },
  })
  const html = ReactDOMServer.renderToStaticMarkup(
    <MemoryRouter>
      <KpiDetailView watch={w} series={kpiSeries} events={events} dashboards={dashboards} onPin={() => {}} onToggle={() => {}} onAskKpi={() => {}} />
    </MemoryRouter>,
  )
  assert.match(html, /Stale data, check skipped: Orders lag/)
  assert.match(html, /Data fresh again: Back/)
  assert.match(html, /critical · just now/)
  assert.ok(!html.includes('warning · '))
  assert.match(html, /Stale data<\/span>/)
  assert.match(html, /Data as of /)
  assert.match(html, /Measure: revenue \(last_30d\)/)
  assert.ok(!/gold|card-owed/.test(html))
  const src = readFileSync('src/pages/Watches.tsx', 'utf8') + readFileSync('src/pages/Kpis.tsx', 'utf8')
  assert.ok(src.includes('eventLabel') && src.includes('stale_since'))
})

function listHtml(selectedId: number | null) {
  return ReactDOMServer.renderToStaticMarkup(
    <MemoryRouter>
      <KpiListView
        watches={[mk({ id: 1, kind: 'condition', title: 'Other' }), mk({ id: 2, title: 'Rev' })]}
        selectedId={selectedId}
      />
    </MemoryRouter>,
  )
}

test('quick 041: the open row is marked once, root has kpi-list', () => {
  const html = listHtml(2)
  assert.equal(html.match(/aria-current="true"/g)?.length, 1)
  assert.equal(html.match(/is-current/g)?.length, 1)
  assert.match(html, /<div class="ic-row is-current"><a[^>]*>Rev</)
  assert.match(html, /class="ic-list kpi-list"/)
  assert.ok(html.indexOf('Rev') < html.indexOf('Other'))
  const none = listHtml(null)
  assert.ok(!none.includes('aria-current') && !none.includes('is-current'))
})

function detailSteps(steps: Parameters<typeof KpiDetailView>[0]['steps']) {
  return ReactDOMServer.renderToStaticMarkup(
    <MemoryRouter>
      <KpiDetailView
        watch={mk({})}
        series={kpiSeries}
        events={ev}
        dashboards={dashboards}
        steps={steps}
        onPin={() => {}}
        onToggle={() => {}}
        onAskKpi={() => {}}
      />
    </MemoryRouter>,
  )
}

test('quick 041: detail step links, position and focusable heading', () => {
  const html = detailSteps({ prev: { id: 1, title: 'Alpha' }, next: { id: 3, title: 'Gamma' }, position: 2, total: 3 })
  assert.match(html, /aria-label="Step through KPIs"/)
  assert.match(html, /href="\/kpis\/1"/)
  assert.match(html, /aria-label="Previous KPI: Alpha"/)
  assert.match(html, /href="\/kpis\/3"/)
  assert.match(html, /aria-label="Next KPI: Gamma"/)
  assert.ok(html.includes('2 of 3'))
  assert.match(html, /id="kpi-detail-title"/)
  assert.match(html, /tabindex="-1"/)
})

test('quick 041: first KPI has a dimmed Previous and no link', () => {
  const html = detailSteps({ prev: null, next: { id: 3, title: 'Gamma' }, position: 1, total: 3 })
  assert.ok(!html.includes('Previous KPI:'))
  assert.ok(!html.includes('href="/kpis/1"'))
  assert.match(html, /is-off[^>]*aria-hidden="true"|aria-hidden="true"[^>]*is-off/)
  assert.ok(html.includes('Previous'))
  assert.match(html, /Next KPI: Gamma/)
})

test('quick 041: no steps prop renders no step nav, heading still focusable', () => {
  const html = detail(mk({}))
  assert.ok(!html.includes('Step through KPIs'))
  assert.match(html, /id="kpi-detail-title"/)
})

test('quick 041: css block and source guards', () => {
  const css = readFileSync('src/pages/ic.css', 'utf8')
  const at = css.indexOf('Quick 041')
  assert.ok(at >= 0)
  const block = css.slice(at)
  assert.match(block, /\.kpi-list \.ic-row\.is-current[^}]*--signal-wash[^}]*--signal\b/)
  assert.match(block, /@media \(min-width: 901px\)\s*\{[^}]*\.kpi-list-pane[^}]*position: sticky/)
  assert.ok(!block.includes('--owed'))
  assert.ok(!readFileSync('src/pages/Kpis.tsx', 'utf8').includes('dangerouslySetInnerHTML'))
})

test('quick 041: page wiring guards', () => {
  const src = readFileSync('src/pages/Kpis.tsx', 'utf8')
  for (const needle of [
    'planReveal(',
    'revealScrollTop(',
    'prefers-reduced-motion',
    'preventScroll: true',
    'location.key',
    'kpi-list-pane',
    'kpi-detail-pane',
    'pickKpi(',
  ])
    assert.ok(src.includes(needle), needle)
  assert.ok(!src.includes('scrollIntoView'))
  assert.ok(readFileSync('src/components/AppShell.tsx', 'utf8').includes('className="topbar"'))
})

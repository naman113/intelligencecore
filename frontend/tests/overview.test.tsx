import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ReactDOMServer from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { OverviewView, type OverviewViewProps } from '../src/pages/Overview'
import { normalizeInsight, type Insight } from '../src/lib/contracts'
import type { Watch } from '../src/lib/types'
import { dashboards, kpiSeries, phase7Insights } from './fixtures'

const watch = {
  id: 4,
  kind: 'kpi',
  title: 'Overdue invoices',
  status: 'active',
  last_checked_at: new Date().toISOString(),
} as unknown as Watch
const items = phase7Insights.items.map(normalizeInsight).filter((x): x is Insight => !!x)

function view(over: Partial<OverviewViewProps> = {}) {
  const props: OverviewViewProps = {
    tiles: [{ watch, series: kpiSeries }],
    tilesLoading: false,
    insights: { available: true, items },
    dashboards,
    owedColumn: <div id="owed-slot">OWED</div>,
    onAskDashboard: () => {},
    onAskKpi: () => {},
    onMakeKpi: () => {},
    ...over,
  }
  return ReactDOMServer.renderToStaticMarkup(
    <MemoryRouter>
      <OverviewView {...props} />
    </MemoryRouter>,
  )
}

test('tile shows title, latest, metric, sparkline, checked', () => {
  const html = view()
  assert.match(html, /Overdue invoices/)
  assert.match(html, />187</)
  assert.match(html, /overdue_total/)
  assert.match(html, /<polyline/)
  assert.match(html, /checked just now/)
  const tiles = html.split('class="ov-tiles"')[1]!.split('</section>')[0]!
  assert.ok(!/card-owed|--owed/.test(tiles))
})

test('no KPIs shows empty state', () => {
  const html = view({ tiles: [] })
  assert.match(html, /No KPIs yet/)
  assert.match(html, /Ask for a new KPI/)
})

test('insights: unavailable, empty, items', () => {
  assert.match(
    view({ insights: { available: false, items: [] } }),
    /explore_data[\s\S]*href="\/playbooks"/,
  )
  assert.match(view({ insights: { available: true, items: [] } }), /Nothing noticed yet/)
  const html = view()
  assert.match(html, /Revenue in South fell 18%/)
  assert.match(html, /Overdue invoices crossed threshold/)
  assert.equal(html.split('Make this a KPI').length - 1, 2)
})

test('dashboards: links, versions, refresh, error owed line', () => {
  const html = view()
  assert.match(html, /href="\/dashboards\/sales"/)
  assert.match(html, /v3/)
  assert.match(html, /not refreshed yet/)
  assert.match(html, /refreshed /)
  assert.match(html, /ic-owed-line[^>]*>query timed out/)
  assert.match(
    view({ dashboards: [{ ...dashboards[0]!, current_version: null }] }),
    /not published yet/,
  )
})

test('ask buttons and owed slot', () => {
  const html = view()
  assert.match(html, /Ask for a dashboard/)
  assert.match(html, /<div id="owed-slot">OWED<\/div>/)
  assert.match(view({ owedTotal: 3 }), /3 things waiting on you/)
})

test('layout rules in css and Home switch in App', () => {
  const css = readFileSync('src/pages/overview.css', 'utf8')
  assert.ok(css.includes('repeat(auto-fill, minmax(min(100%, 220px), 1fr))'))
  assert.ok(css.includes('max-width: 980px'))
  assert.match(css, /\.ov-owed \{ order: -1/)
  const app = readFileSync('src/App.tsx', 'utf8')
  assert.match(app, /profile === 'intelligence' \? <Overview \/> : <Today \/>/)
  assert.match(app, /path="today" element=\{<Today \/>\}/)
})

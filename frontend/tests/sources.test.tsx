import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ReactDOMServer from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { SourcesView } from '../src/pages/ContextSources'
import { ctxSources } from './fixtures'
import type { ContextSource } from '../src/lib/ictypes'

const noop = () => {}
const render = (sources: ContextSource[]) =>
  ReactDOMServer.renderToStaticMarkup(
    <MemoryRouter>
      <SourcesView sources={sources} onDiscover={noop} />
    </MemoryRouter>,
  )

test('source row: name, kind, description, counts, no gold when nothing pending', () => {
  const html = render([{ ...ctxSources[0], description: 'Northwind Supply' }])
  assert.match(html, /business_db/)
  assert.match(html, /postgres/)
  assert.match(html, /Northwind Supply/)
  assert.match(html, /1 tables and 0 columns described/)
  assert.doesNotMatch(html, /Review on Schema/)
  assert.doesNotMatch(html, /ic-gold/)
})

test('pending source shows gold line and Review link', () => {
  const html = render([
    { ...ctxSources[0], pending: 3 },
  ])
  assert.match(html, /3 waiting on you/)
  assert.match(html, /href="\/context\/schema"[^>]*>Review on Schema/)
})

test('links to the poller page and offers Discover schema', () => {
  const html = render(ctxSources)
  assert.match(html, /href="\/data-sources"[^>]*>Tables watched for new rows/)
  assert.match(html, /Discover schema/)
})

test('pages route discover through asks.discoverSchema', () => {
  for (const f of ['src/pages/ContextSources.tsx', 'src/pages/Schema.tsx']) {
    assert.match(readFileSync(f, 'utf8'), /asks\.discoverSchema/)
  }
})

test('empty state', () => {
  assert.match(render([]), /No data sources yet/)
})

// Phase 15 (15-14): SourcesPanelView
import { SourcesPanelView } from '../src/components/SourcesPanel'
import { sourceRows } from './fixtures'
import { asks } from '../src/lib/ask'

test('panel: rows, modes, backfill, gold flags, business_db line, escaping', () => {
  const html = ReactDOMServer.renderToStaticMarkup(<SourcesPanelView sources={sourceRows} />)
  assert.match(html, /live \(business database\)/)
  assert.match(html, /public\.customers/)
  assert.match(html, /erp_my\.orders/)
  assert.match(html, /backfilling 42%/)
  assert.match(html, /ic-gold/)
  assert.match(html, /out of date/)
  assert.match(html, /last two syncs failed: timeout &lt;b&gt;x&lt;\/b&gt;/)
  assert.match(html, /live tables cannot be reached/)
  assert.match(html, /Add source/)
  assert.match(html, /Upload a CSV/)
})

test('panel: ask strings', () => {
  assert.match(asks.addSource(), /connect a new data source/)
  assert.equal(asks.csvSource('b.csv'), 'Make this CSV file a data source: b.csv. Probe it, test it and propose it for my approval.')
})

// Phase 17 (CONS-05): switching state
import { modeLabel } from '../src/lib/sources'

test('modeLabel names a switch in progress', () => {
  assert.equal(modeLabel({ mode: 'live', pending_mode: 'import' }), 'live, switching to import')
  assert.equal(modeLabel({ mode: 'import', pending_mode: 'live' }), 'import, switching to live')
  assert.equal(modeLabel({ mode: 'import', pending_mode: null }), 'import')
  assert.equal(modeLabel({ mode: 'live' }), 'live')
})

test('panel shows switching label with progress and last error', () => {
  const rows = [{ ...sourceRows[1], tables: [{ ...sourceRows[1].tables[0], mode: 'live' as const,
    pending_mode: 'import' as const, backfill_done: false, backfill_progress: 0.3, last_error: 'boom' }] }]
  const html = ReactDOMServer.renderToStaticMarkup(<SourcesPanelView sources={rows} />)
  assert.match(html, /live, switching to import/)
  assert.match(html, /backfilling 30%/)
  assert.match(html, /last error: boom/)
})

// Quick 027 (audit #6): the real business_db row has kind 'postgres', not 'business_db'
test('panel: business_db row of kind postgres reads live, not not tracked / never', () => {
  const row = { ...sourceRows[0], kind: 'postgres' }
  const html = ReactDOMServer.renderToStaticMarkup(<SourcesPanelView sources={[row]} />)
  assert.doesNotMatch(html, /not tracked/)
  assert.doesNotMatch(html, /last sync never/)
  assert.equal((html.match(/live \(business database\)/g) ?? []).length, 2)
})

test('panel: a registered postgres source with no sync still reads never / not tracked', () => {
  const row = { ...sourceRows[1], name: 'erp_pg', kind: 'postgres', freshness_minutes: null, last_run: null,
    stale: false, failing: false, unreachable: false }
  const html = ReactDOMServer.renderToStaticMarkup(<SourcesPanelView sources={[row]} />)
  assert.match(html, /last sync never/)
  assert.match(html, /not tracked/)
})

import { test } from 'node:test'
import assert from 'node:assert/strict'
import ReactDOMServer from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { ContextChangeDetail } from '../src/components/payloads/ContextChange'
import { ModelChangeDetail } from '../src/components/payloads/ModelChange'
import { PublishDashboardDetail } from '../src/components/payloads/PublishDashboard'
import { ActionDetail, actionLine, actionTitle } from '../src/components/ActionPayload'
import { SourceChangePanel } from '../src/components/payloads/SourceChange'
import { readContextChange, readPublishDashboard, readSourceChange } from '../src/lib/contracts'
import {
  ctxColumn,
  ctxEdited,
  ctxHide,
  ctxScript,
  ctxTable,
  pubAbsolute,
  pubBare,
  pubJs,
  pubRollback,
  pubWithUrl,
  srcCsv,
  srcDb,
  srcMixed,
} from './fixtures'

const render = (el: React.ReactElement) =>
  ReactDOMServer.renderToStaticMarkup(<MemoryRouter>{el}</MemoryRouter>)

function pub(
  a: { payload: unknown },
  o: { base?: string | null; configured?: boolean; preview?: Parameters<typeof PublishDashboardDetail>[0]['preview'] } = {},
) {
  return render(
    <PublishDashboardDetail
      view={readPublishDashboard(a.payload)}
      diff={null}
      preview={o.preview ?? null}
      base={o.base === undefined ? 'https://d.x' : o.base}
      configured={o.configured ?? false}
    />,
  )
}

test('context change before and after', () => {
  const html = render(<ContextChangeDetail view={readContextChange(ctxColumn.payload)} />)
  assert.ok(html.includes('business_db.public.customers.region_id'))
  assert.ok(html.includes('Before') && html.includes('After'))
  assert.ok(html.includes('region'))
  assert.ok(html.includes('The sales region the customer belongs to.'))
  const t = render(<ContextChangeDetail view={readContextChange(ctxTable.payload)} />)
  assert.ok(t.includes('No description yet'))
})

test('edited proposal shows chip and agent text', () => {
  const html = render(<ContextChangeDetail view={readContextChange(ctxEdited.payload)} />)
  assert.ok(html.includes('edited by you'))
  assert.ok(html.includes('<details'))
  assert.ok(html.includes('Tier of the customer.'))
})

test('hide row and escaping', () => {
  const h = render(<ContextChangeDetail view={readContextChange(ctxHide.payload)} />)
  assert.ok(h.includes('Hid table'))
  assert.ok(h.includes('2 pending proposals'))
  const s = render(<ContextChangeDetail view={readContextChange(ctxScript.payload)} />)
  assert.ok(!s.includes('<script>'))
  assert.ok(s.includes('&lt;script&gt;'))
})

test('publish proposal with payload draft url', () => {
  const html = pub(pubWithUrl)
  assert.ok(html.includes('Sales overview'))
  assert.ok(html.includes('v4'))
  assert.ok(html.includes('Added daily orders chart'))
  assert.ok(html.includes('+orders_by_day') && html.includes('−old_q') && html.includes('~revenue'))
  assert.ok(html.includes('href="https://d.x/d/sales/v/4/?p=seedtoken"'))
  assert.ok(html.includes('rel="noopener noreferrer"') && html.includes('target="_blank"'))
  assert.ok(html.includes('Fresh link'))
  assert.ok(!html.includes('previews are enabled'))
})

test('absolute draft url with no base', () => {
  const html = pub(pubAbsolute, { base: null })
  assert.ok(!html.includes('abstoken'))
})

test('absolute draft url on the guessed dashboards origin is linked', () => {
  const html = pub(pubAbsolute, { base: 'http://localhost:9082', configured: false })
  assert.ok(html.includes('href="http://localhost:9082/d/sales/v/4/?p=abstoken"'))
})

test('no draft url: button, minted preview, not enabled, no base', () => {
  let html = pub(pubBare)
  assert.ok(html.includes('Open draft'))
  assert.ok(!html.includes('?p='))
  assert.ok(html.includes('Show query changes'))
  html = pub(pubBare, { preview: { available: true, path: '/d/sales/v/5/', token: 'tk', expiresAt: null } })
  assert.ok(html.includes('href="https://d.x/d/sales/v/5/?p=tk"'))
  assert.ok(html.includes('rel="noopener noreferrer"') && html.includes('target="_blank"'))
  html = pub(pubBare, { preview: { available: false } })
  assert.ok(html.includes('Draft preview opens once dashboard previews are enabled'))
  assert.ok(html.includes('/d/sales/v/5/'))
  html = pub(pubBare, { base: null })
  assert.ok(html.includes('Set the dashboards address on Setup'))
  assert.ok(html.includes('href="/system/setup"'))
})

test('javascript url is never linked', () => {
  const html = pub(pubJs)
  assert.ok(!html.includes('javascript:'))
})

test('executed rollback sentence', () => {
  assert.ok(pub(pubRollback).includes('Rolled back to v2 (was v3)'))
})

test('titles and lines', () => {
  assert.equal(actionTitle(ctxTable), 'Describe table public.customers')
  assert.equal(actionTitle(ctxEdited), 'Describe column business_db.public.customers.tier')
  assert.equal(actionTitle(ctxHide), 'Hide table business_db.public.audit_log')
  assert.equal(actionTitle(pubWithUrl), 'Publish "Sales overview" v4')
  assert.equal(actionLine(ctxColumn), 'The sales region the customer belongs to.')
  assert.equal(actionLine(pubWithUrl), 'Added daily orders chart')
})

test('ActionDetail routes by kind and avoids gold', () => {
  const c = render(<ActionDetail action={ctxColumn} />)
  assert.ok(c.includes('Before'))
  const p = render(<ActionDetail action={pubWithUrl} />)
  assert.ok(p.includes('Sales overview'))
  for (const html of [c, p, pub(pubWithUrl)]) {
    assert.ok(!/card-owed|btn-owed|--owed/.test(html))
  }
})

test('hide_table renders readably', () => {
  const a = { id: 9, kind: 'hide_table', status: 'proposed', payload: { ref: 'business_db.public.inventory_snapshots', reason: 'test data' } } as never
  assert.equal(actionTitle(a), 'Hide table business_db.public.inventory_snapshots from the agents')
  const html = render(<ActionDetail action={a} />)
  assert.ok(html.includes('inventory_snapshots') && html.includes('from the agents') && html.includes('test data'))
  assert.ok(!html.includes('{'))
})

// ── Phase 16: model_change card ──
const HOSTILE = '<img src=x onerror=alert(1)>'
const mAct = (payload: unknown) => ({ id: 31, kind: 'model_change', status: 'proposed', payload }) as never
const measureChange = {
  _summary: 'Add measure net_sales',
  object: 'measure',
  name: 'net_sales',
  op: 'create',
  definition: { type: 'simple', agg: 'sum', expr: 'amount' },
  before: null,
  evidence: {
    sql: 'SELECT SUM("amount") FROM "public"."invoices"',
    samples: [
      { day: '2026-10-01', value: 10 },
      { day: '2026-10-02', value: 25 },
      { day: '2026-10-03', value: 40 },
    ],
    sample_range: { start: '2026-09-15', end: '2026-10-15' },
  },
}

test('model_change title and line', () => {
  assert.equal(actionTitle(mAct(measureChange)), 'Add measure net_sales')
  assert.equal(actionTitle(mAct({ object: 'measure', name: 'x' })), 'Model measure x')
  assert.equal(actionLine(mAct(measureChange)), 'create measure net_sales')
})

test('model_change measure shows sql, samples and definition', () => {
  const html = render(<ActionDetail action={mAct(measureChange)} />)
  assert.ok(html.includes('<pre class="code">SELECT SUM'))
  assert.ok(html.includes('3 sample values') && html.includes('latest 40'))
  assert.ok(html.includes('<svg'))
  assert.ok(html.includes('amount'))
  assert.ok(!/card-owed|btn-owed|--owed/.test(html))
})

test('model_change without a time dimension shows one sample value and no sparkline', () => {
  const html = render(
    <ModelChangeDetail payload={{ ...measureChange, evidence: { sql: 'SELECT 1', samples: [], sample_value: 1234, sample_range: null } }} />,
  )
  assert.ok(html.includes('1,234') && html.includes('Sample value'))
  assert.ok(!html.includes('<svg'))
})

test('model_change relationship shows match rate with counts', () => {
  const html = render(
    <ModelChangeDetail
      payload={{
        object: 'relationship',
        name: 'r',
        op: 'create',
        definition: { from_relation: 'a' },
        evidence: { sql: 'SELECT 1', from_rows: 2200, from_nonnull: 2184, matched: 2180, match_rate: 0.998, duplicate_keys: 0, null_keys: 16 },
      }}
    />,
  )
  assert.ok(html.includes('99.8% (2,180 of 2,184 keys match)'))
  assert.ok(html.includes('16 null keys'))
})

test('model_change before and after, table note, view preview', () => {
  const upd = render(
    <ModelChangeDetail payload={{ ...measureChange, op: 'update', before: { agg: 'count' }, evidence: {} }} />,
  )
  assert.ok(upd.includes('Before') && upd.includes('After') && upd.includes('count'))
  const tbl = render(
    <ModelChangeDetail payload={{ object: 'table', name: 't', op: 'create', definition: {}, evidence: { row_count: 5, note: 'time column is on another relation' } }} />,
  )
  assert.ok(tbl.includes('time column is on another relation') && !tbl.includes('rows per day'))
  const view = render(
    <ModelChangeDetail payload={{ object: 'view', name: 'v', op: 'create', definition: {}, evidence: { columns: ['a', 'b'], preview_rows: [{ a: 1, b: 'x' }] } }} />,
  )
  assert.ok(view.includes('<th>a</th>') && view.includes('>x<'))
})

test('model_change escapes hostile strings and survives malformed payloads', () => {
  const html = render(
    <ModelChangeDetail
      payload={{ object: 'measure', name: HOSTILE, op: 'create', definition: { expr: HOSTILE }, evidence: { sql: HOSTILE, note: HOSTILE } }}
    />,
  )
  assert.ok(!html.includes('<img'))
  assert.ok(html.includes('&lt;img'))
  assert.doesNotThrow(() => render(<ModelChangeDetail payload={{ evidence: 'oops' }} />))
  assert.doesNotThrow(() =>
    render(<ModelChangeDetail payload={{ object: 'measure', evidence: { samples: 'x', sample_value: 'abc' } }} />),
  )
  assert.doesNotThrow(() => render(<ModelChangeDetail payload={null} />))
})

test('publish_dashboard spec draft shows tiles, measures and queries as text', () => {
  const html = pub({
    payload: {
      slug: 's',
      version: 3,
      format: 'spec',
      tiles_added: ['t1'],
      tiles_removed: ['t0'],
      tiles_changed: ['t2'],
      tiles: [{ id: 't1', type: 'bar', title: '<img src=x onerror=alert(1)>', binding: 'query', ref: 'q1' }],
      measures: ['revenue'],
      queries: [
        { id: 'q1', source: 'sql' },
        { id: 'q2', source: 'kpi_history' },
      ],
    },
  })
  assert.ok(html.includes('Chart spec'))
  assert.ok(html.includes('+t1') && html.includes('~t2'))
  assert.ok(html.includes('revenue'))
  assert.ok(html.includes('SQL, unverified') && html.includes('KPI history'))
  assert.ok(html.includes('&lt;img'))
  assert.ok(!html.includes('<img'))
  assert.ok(html.includes('Open draft'))
  assert.ok(!pub(pubBare).includes('Chart spec'))
})

// Phase 15 (15-13)
test('source change card shows handle, tables, schedule and no secret', () => {
  const html = render(<SourceChangePanel view={readSourceChange(srcDb.payload)} />)
  for (const t of ['Connect mysql source', 'cred:erp_my', 'db.example', 'updated_at', 'has no index',
    'Backfill from 2025-01-01', 'every 60 min', '500,000 rows', 'New columns appear', 'Check the clock',
    '1 backfill run', 'reconciled daily']) {
    assert.ok(html.includes(t), t)
  }
  assert.ok(!html.includes('hunter2') && !html.toLowerCase().includes('password'))
})

test('source change card for CSV', () => {
  const html = render(<SourceChangePanel view={readSourceChange(srcCsv.payload)} />)
  assert.ok(html.includes('budget.csv') && html.includes('abcdef012345') && !html.includes('abcdef0123456'))
  assert.ok(html.includes('on re-upload: replace') && html.includes('none: replaced each run'))
})

test('source change title, line and detail through ActionPayload', () => {
  assert.equal(actionTitle(srcDb), 'Connect mysql source erp_my')
  assert.equal(actionTitle(srcCsv), 'Connect source budget')
  assert.ok(actionLine(srcDb).startsWith('2 tables (import, live)'))
  const html = render(<ActionDetail action={srcDb} />)
  assert.ok(html.includes('cred:erp_my') && !html.includes('hunter2'))
})

// Phase 15 gap 2 (15-19)
test('source change card shows remote and warehouse names of a mixed-case table', () => {
  const html = render(<SourceChangePanel view={readSourceChange(srcMixed.payload)} />)
  for (const t of ['OrderLines', 'order_lines', 'LookupCodes', 'lookup_codes']) assert.ok(html.includes(t), t)
})

// Phase 17 (CONS-05): source_change op 'mode'
const modePayload = {
  _summary: 'Switch erp.orders to import', op: 'mode', name: 'erp', password: 'hunter2-secret',
  tables: [{ schema: 'erp', table: 'orders', remote_schema: 'public', remote_table: 'orders', from: 'live', to: 'import',
    cursor: 'updated_at', merge_key: ['id'], est_rows: 12000, est_mb: 5 }],
  notes: ['Backfill runs first <b>x</b>'], freshness: 'Fresh within 60 min after backfill',
  dependents: { 'erp.orders': { watches: 2, dashboards: 1, insights: 3, model_objects: 4, titles: {} } },
}

test('mode switch card shows tables, cursor, key, notes and dependents', () => {
  const html = render(<SourceChangePanel view={readSourceChange(modePayload)} />)
  for (const t of ['Switch storage mode for', 'erp.orders', 'live -&gt; import', 'updated_at', 'id',
    'about 12,000 rows, 5 MB', 'Backfill runs first &lt;b&gt;x&lt;/b&gt;',
    'Keeps working unchanged: 2 watches, 1 dashboard, 3 insights, 4 model objects']) {
    assert.ok(html.includes(t), t)
  }
  assert.ok(!html.includes('hunter2') && !html.includes('<b>'))
})

test('op-less source_change still has no mode view', () => {
  assert.equal(readSourceChange(srcDb.payload).modeChange, null)
})

import { test } from 'node:test'
import assert from 'node:assert/strict'
import ReactDOMServer from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { ModelView, type ModelTab } from '../src/pages/Model'
import { asks } from '../src/lib/ask'
import { readModelMeasures, readModelRelationships, readModelTables } from '../src/lib/model'

const HOSTILE = '<img src=x onerror=alert(1)>'

const tables = readModelTables([
  { name: 'sales_orders', relation: 'public.sales_orders', object_kind: 'base', role: 'fact', grain: 'one row per order', primary_key: ['id'], agg_time_dimension: 'public.sales_orders.order_date', description: HOSTILE, in_warehouse: null },
  { name: 'v_sales', relation: 'model.v_sales', object_kind: 'view', role: 'dimension', grain: null, primary_key: [], in_warehouse: true },
])
const rels = readModelRelationships([
  { name: 'ol_so', from_relation: 'public.order_lines', from_columns: ['order_id'], to_relation: 'public.sales_orders', to_columns: ['id'], origin: 'foreign_key', match_rate: 0.998 },
])
const measures = readModelMeasures([
  { name: 'net_sales', label: HOSTILE, type: 'simple', agg: 'sum', expr: 'amount', base_relation: 'public.invoices', filter: [{ dimension: 'status', op: 'not_in', values: ['cancelled'] }], unit: 'USD', compiled_sql: 'SELECT ' + HOSTILE },
  { name: 'broken', type: 'ratio', compile_error: 'denominator is not modeled' },
])

const render = (tab: ModelTab, o: Partial<Parameters<typeof ModelView>[0]> = {}) =>
  ReactDOMServer.renderToStaticMarkup(
    <MemoryRouter>
      <ModelView tables={tables} relationships={rels} measures={measures} pending={[]} tab={tab} onTab={() => {}} onModel={() => {}} onAsk={() => {}} {...o} />
    </MemoryRouter>,
  )

test('three tabs', () => {
  const html = render('tables')
  for (const l of ['Tables', 'Relationships', 'Measures']) assert.ok(html.includes(l))
})

test('tables tab shows role, grain, kind and warehouse flag', () => {
  const html = render('tables')
  assert.ok(html.includes('fact') && html.includes('one row per order'))
  assert.ok(html.includes('view') && html.includes('yes'))
})

test('relationships tab shows from, to, origin and match rate', () => {
  const html = render('relationships')
  assert.ok(html.includes('order_lines.order_id') && html.includes('sales_orders.id'))
  assert.ok(html.includes('foreign key') && html.includes('99.8%'))
})

test('measures tab shows type, expr, filters, sql and compile errors', () => {
  const html = render('measures')
  assert.ok(html.includes('simple') && html.includes('sum(amount)') && html.includes('cancelled'))
  assert.ok(html.includes('<pre class="code">SELECT'))
  assert.ok(html.includes('denominator is not modeled'))
})

test('empty model offers Model my data', () => {
  const html = render('tables', { tables: [], relationships: [], measures: [] })
  assert.ok(html.includes('Nothing is modeled yet') && html.includes('Model my data'))
})

test('pending proposals link to Approvals', () => {
  const html = render('tables', { pending: [{ actionId: 1, object: 'measure', name: 'x', op: 'create', summary: null }] })
  assert.ok(html.includes('href="/approvals"') && html.includes('1 model change'))
})

test('hostile strings are escaped on every tab', () => {
  for (const t of ['tables', 'relationships', 'measures'] as ModelTab[]) {
    const html = render(t)
    assert.ok(!html.includes('<img'), t)
  }
  assert.ok(render('tables').includes('&lt;img'))
  assert.ok(render('measures').includes('&lt;img'))
})

test('asks say the change is proposed for approval and clip', () => {
  assert.match(asks.modelData(), /for my approval/)
  assert.match(asks.modelChange('add gross margin'), /propose it for my approval.*add gross margin/)
  assert.ok(asks.modelChange('x'.repeat(5000)).length < 1700)
})

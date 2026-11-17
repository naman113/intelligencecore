// Phase 18 (SPEC-02): unit tests of the chart-spec renderer's pure core.
// render.js is loaded in a bare node:vm context (no document), where it only
// exposes ICRenderCore.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import vm from 'node:vm'

const SRC = readFileSync(
  join(process.cwd(), '..', 'dashboards', 'vendor', 'ic-render-1.0.0', 'render.js'),
  'utf8',
)

function load(): any {
  const ctx: any = vm.createContext({ Intl })
  vm.runInContext(SRC, ctx)
  return ctx
}

const ctx = load()
const core = ctx.ICRenderCore
const EN = { locale: 'en-US' }
const DASH = '–'

test('loading without a document exposes only ICRenderCore', () => {
  const keys = Object.keys(ctx).filter((k) => k !== 'Intl')
  assert.deepEqual(keys, ['ICRenderCore'])
  for (const f of ['formatValue', 'delta', 'isStale', 'pivot', 'pickStat', 'xLabel']) {
    assert.equal(typeof core[f], 'function', f)
  }
})

test('formatValue formats each named format', () => {
  assert.equal(core.formatValue(1234.5, 'number', EN), '1,234.5')
  assert.equal(core.formatValue(1234.5, 'integer', EN), '1,235')
  assert.equal(core.formatValue(1234.5, 'decimal1', EN), '1,234.5')
  assert.equal(core.formatValue(1234.5, 'decimal2', EN), '1,234.50')
  assert.equal(core.formatValue(0.123, 'percent', EN), '12.3%')
  assert.equal(core.formatValue(12.3, 'percent_value', EN), '12.3%')
  assert.equal(core.formatValue(1234.5, 'currency', { locale: 'en-US', currency: 'USD' }), '$1,234.50')
  assert.equal(core.formatValue(1234567, 'compact', EN), '1.2M')
})

test('formatValue handles units, nulls and text', () => {
  assert.equal(core.formatValue(5, 'integer', { locale: 'en-US', unit: 'kg' }), '5 kg')
  assert.equal(core.formatValue(null, 'number', EN), DASH)
  assert.equal(core.formatValue(undefined, 'number', EN), DASH)
  assert.equal(core.formatValue('North', 'number', EN), 'North')
})

test('delta', () => {
  assert.equal(core.delta(110, 100), 0.1)
  assert.equal(core.delta(90, 100), -0.1)
  assert.equal(core.delta(5, 0), null)
  assert.equal(core.delta(5, null), null)
})

test('isStale', () => {
  const now = Date.parse('2026-10-26T12:00:00Z')
  const ago = (m: number) => new Date(now - m * 60000).toISOString()
  assert.equal(core.isStale(ago(20), 15, now), false)
  assert.equal(core.isStale(ago(36), 15, now), true)
  assert.equal(core.isStale(null, 15, now), false)
})

test('pivot keeps first-seen order and caps series at 12', () => {
  const rows = [
    { month: '2026-01-01', region: 'N', sales: 1 },
    { month: '2026-01-01', region: 'S', sales: 2 },
    { month: '2026-02-01', region: 'N', sales: 3 },
  ]
  const p = core.pivot(rows, 'month', 'region', 'sales', 'month')
  assert.deepEqual(Array.from(p.labels), ['2026-01', '2026-02'])
  assert.deepEqual(Array.from(p.datasets, (d: any) => d.label), ['N', 'S'])
  assert.deepEqual(Array.from(p.datasets[0].data), [1, 3])
  assert.deepEqual(Array.from(p.datasets[1].data), [2, null])
  assert.equal(p.dropped, 0)
  const many = Array.from({ length: 15 }, (_, i) => ({ m: 'a', r: 's' + i, v: i }))
  const q = core.pivot(many, 'm', 'r', 'v')
  assert.equal(q.datasets.length, 12)
  assert.equal(q.dropped, 3)
})

test('pickStat picks last, last_complete and the comparison', () => {
  const rows = [
    { m: '2026-08-01T00:00:00+00:00', v: 10, p: 8 },
    { m: '2026-09-01T00:00:00+00:00', v: 20, p: 15 },
    { m: '2026-10-01T00:00:00+00:00', v: 5, p: 4 },
  ]
  const base = { column: 'v', x: 'm', grain: 'month', compare: 'previous_row' }
  const last = core.pickStat(rows, { ...base, pick: 'last' }, '2026-10-15')
  assert.equal(last.value, 5)
  assert.equal(last.previous, 20)
  const lc = core.pickStat(rows, { ...base, pick: 'last_complete' }, '2026-10-15')
  assert.equal(lc.value, 20)
  assert.equal(lc.previous, 10)
  assert.equal(lc.period, '2026-09')
  const col = core.pickStat(rows, { ...base, pick: 'last', compare: { column: 'p' } }, '2026-10-15')
  assert.equal(col.previous, 4)
})

test('xLabel by grain', () => {
  const v = '2026-10-01T00:00:00+00:00'
  assert.equal(core.xLabel(v, 'month'), '2026-10')
  assert.equal(core.xLabel(v, 'day'), '2026-10-01')
  assert.equal(core.xLabel(v, 'quarter'), '2026-Q4')
  assert.equal(core.xLabel(v, 'year'), '2026')
  assert.equal(core.xLabel('North', 'month'), 'North')
})

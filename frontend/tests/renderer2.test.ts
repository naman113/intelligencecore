// Phase 22 (DCAP-03..14): unit tests of the pure core of ic-render-2.0.0.
// render.js is loaded in a bare node:vm context (no document), where it only
// exposes ICRenderCore. Date and re-aggregation vectors are shared with Python.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import vm from 'node:vm'

const SRC = readFileSync(
  join(process.cwd(), '..', 'dashboards', 'vendor', 'ic-render-2.0.0', 'render.js'),
  'utf8',
)

function fixture(name: string): any {
  return JSON.parse(readFileSync(join(process.cwd(), '..', 'backend', 'tests', 'fixtures', name), 'utf8'))
}

function load(): any {
  const ctx: any = vm.createContext({ Intl })
  vm.runInContext(SRC, ctx)
  return ctx
}

const ctx = load()
const core = ctx.ICRenderCore
const DATES = fixture('dashboard_date_vectors.json')
// vm contexts have their own Object prototype, so compare through JSON
function eq(a: unknown, b: unknown, m?: string) {
  assert.deepStrictEqual(JSON.parse(JSON.stringify(a)), JSON.parse(JSON.stringify(b)), m)
}

test('loading without a document exposes only ICRenderCore', () => {
  const keys = Object.keys(ctx).filter((k) => k !== 'Intl')
  eq(keys, ['ICRenderCore'])
  for (const f of ['todayIn', 'asOfDay', 'presetRange', 'encodeState', 'decodeState']) {
    assert.equal(typeof core[f], 'function', f)
  }
})

test('todayIn uses the zone and assembles the date from parts', () => {
  const now = Date.UTC(2026, 10, 3, 19, 30)
  assert.equal(core.todayIn('Asia/Kolkata', now), '2026-11-04')
  assert.equal(core.todayIn('UTC', now), '2026-11-03')
})

test('asOfDay: zone, fractional seconds and null', () => {
  assert.equal(core.asOfDay('2026-11-03T19:30:00+00:00', 'Asia/Kolkata'), '2026-11-04')
  assert.equal(core.asOfDay('2026-11-03T19:30:00.123456+00:00', 'UTC'), '2026-11-03')
  assert.equal(core.asOfDay(null, 'UTC'), null)
})

test('presetRange equals every shared vector', () => {
  for (const c of DATES.cases) {
    eq(
      core.presetRange(c.preset, c.period, c.today, c.fy_start_month),
      [c.from, c.to],
      JSON.stringify(c),
    )
  }
})

test('custom previous and last-year ranges equal the vectors', () => {
  for (const c of DATES.custom) {
    eq(core.previousRange(c.from, c.to, null), c.previous)
    eq(core.lastYearRange(c.from, c.to), c.last_year)
  }
})

test('bucketFor and horizonStart equal the vectors', () => {
  for (const b of DATES.buckets) assert.equal(core.bucketFor(b.from, b.to), b.bucket)
  for (const h of DATES.horizon) assert.equal(core.horizonStart(h.today, h.days), h.start)
})

test('bucketKey and eachDay', () => {
  assert.equal(core.bucketKey('2026-11-04', 'week'), '2026-11-02')
  assert.equal(core.bucketKey('2026-11-04', 'month'), '2026-11-01')
  assert.equal(core.bucketKey('2026-11-04', 'day'), '2026-11-04')
  eq(core.eachDay('2026-10-30', '2026-11-02'), ['2026-10-30', '2026-10-31', '2026-11-01', '2026-11-02'])
})

test('encodeState and decodeState', () => {
  assert.equal(core.encodeState({ range: { preset: 'last_30_days' } }), 'r=last_30_days')
  const st = { range: { from: '2026-10-01', to: '2026-10-31' }, filters: { customer: ['Acme & Co', 'B'] } }
  assert.equal(core.encodeState(st), 'r=2026-10-01..2026-10-31;f.customer=Acme%20%26%20Co,B')
  eq(core.decodeState('#r=2026-10-01..2026-10-31;f.customer=Acme%20%26%20Co,B'), st)
  eq(core.decodeState('r=last_30_days'), { range: { preset: 'last_30_days' }, filters: {} })
  const tricky = { range: { preset: 'today' }, filters: { customer: ["D'Souza (Pune)!"] } }
  const enc = core.encodeState(tricky)
  assert.equal(enc, 'r=today;f.customer=D%27Souza%20%28Pune%29%21')
  eq(core.decodeState(enc), tricky)
})

test('decodeState rejects bad input', () => {
  assert.equal(core.decodeState('#r=bogus'), null)
  assert.equal(core.decodeState('r=today;f.Bad-Id=x'), null)
  assert.equal(core.decodeState('r=today;x=1'), null)
  assert.equal(core.decodeState('r=' + 'a'.repeat(601)), null)
  assert.equal(core.decodeState('r=today;f.c=a b'), null)
  assert.equal(core.decodeState('r=today;f.c=a!'), null)
  assert.equal(core.decodeState('r=2026-02-30..2026-03-01'), null)
  assert.equal(core.decodeState('r=2026-03-02..2026-03-01'), null)
})

// ------------------------------------------------------------ re-aggregation vectors

const REAGG = fixture('dashboard_reagg_vectors.json')

function mkCtx(v: any, period = 'current'): any {
  return {
    dayCol: 'metric_time__day',
    today: v.today ?? '2026-11-04',
    asOf: v.as_of ?? null,
    horizonStart: v.horizon_start ?? null,
    preset: v.preset ?? null,
    period,
    presetRows: v.preset_rows ?? null,
    fyStart: 4,
  }
}

function close(actual: any, expected: any, msg: string) {
  if (typeof expected === 'number') {
    assert.equal(typeof actual, 'number', msg)
    assert.ok(Math.abs(actual - expected) < 1e-9, `${msg}: ${actual} != ${expected}`)
  } else {
    eq(actual, expected, msg)
  }
}

test('reagg vectors: at least 30 cases, all sections present', () => {
  assert.ok(REAGG.cases.length >= 30)
  for (const s of ['formula', 'compare', 'members', 'totals']) assert.ok(REAGG[s].length > 0, s)
})

for (const c of REAGG.cases) {
  test('rangeValue: ' + c.name, () => {
    close(core.rangeValue(c.meta, c.rows, c.range, mkCtx(c)), c.expect, c.name)
  })
}

for (const c of REAGG.formula) {
  test('evalFormula: ' + c.name, () => {
    close(core.evalFormula(c.tree, c.inputs), c.expect, c.name)
  })
}

test('evalFormula guards depth', () => {
  let deep: any = { num: 1 }
  for (let i = 0; i < 14; i++) deep = { op: '+', a: deep, b: { num: 1 } }
  assert.equal(core.evalFormula(deep, {}), null)
  let ok: any = { num: 1 }
  for (let i = 0; i < 10; i++) ok = { op: '+', a: ok, b: { num: 1 } }
  assert.equal(core.evalFormula(ok, {}), 11)
})

for (const c of REAGG.compare) {
  test('compareValue: ' + c.name, () => {
    close(core.compareValue(c.meta, c.rows, c.range, c.kind, mkCtx(c)), c.expect, c.name)
  })
}

for (const c of REAGG.members) {
  test('members: ' + c.name, () => {
    const items = c.members ?? core.memberValues(c.meta, c.rows, c.range, c.key_col, mkCtx(c))
    const res = core.topOther(c.meta, items, c.top, c.other)
    const want = c.expect
    assert.equal(res.members.length, want.members.length, c.name)
    res.members.forEach((m: any, i: number) => {
      assert.equal(m.member, want.members[i].member, c.name)
      close(m.value, want.members[i].value, c.name)
      if (want.members[i].na) assert.equal(m.na, want.members[i].na, c.name)
    })
    close(res.other ? res.other.value : null, want.other, c.name)
    assert.equal(res.dropped, want.dropped, c.name)
  })
}

for (const c of REAGG.totals) {
  test('totals: ' + c.name, () => {
    close(core.totalValue(c.meta, c.rows, c.range, c.rows_are, c.key_col, mkCtx(c)), c.expect, c.name)
  })
}

test('bucketSeries clips partial buckets and fills gaps', () => {
  const meta = { column: 'rev', kind: 'sum' }
  const rows = [
    { metric_time__day: '2026-10-28', rev: 1 },
    { metric_time__day: '2026-11-02', rev: 2 },
    { metric_time__day: '2026-11-03', rev: 4 },
  ]
  const ctx = mkCtx({ as_of: '2026-11-03', horizon_start: '2026-01-01' })
  const pts = core.bucketSeries(meta, rows, { from: '2026-10-28', to: '2026-11-04' }, 'week', ctx)
  eq(
    pts.map((p: any) => [p.key, p.from, p.to, p.value]),
    [
      ['2026-10-26', '2026-10-28', '2026-11-01', 1],
      ['2026-11-02', '2026-11-02', '2026-11-04', 6],
    ],
  )
  const days = core.bucketSeries(meta, rows, { from: '2026-11-02', to: '2026-11-04' }, 'day', ctx)
  eq(days.map((p: any) => p.value), [2, 4, null])
  assert.equal(days[2].na, 'nodata')
  const avgMeta = { column: 'w', kind: 'avg', parts: { sum: 'w__sum', count: 'w__count' } }
  const gaps = core.bucketSeries(avgMeta, [], { from: '2026-11-02', to: '2026-11-03' }, 'day', ctx)
  eq(gaps.map((p: any) => p.value), [null, null])
})

test('bucketSeries distinct: day buckets read rows, week buckets are not available', () => {
  const meta = { column: 'cust', kind: 'distinct' }
  const rows = [{ metric_time__day: '2026-11-02', cust: 5 }]
  const ctx = mkCtx({ as_of: '2026-11-04', horizon_start: '2026-01-01' })
  const days = core.bucketSeries(meta, rows, { from: '2026-11-02', to: '2026-11-03' }, 'day', ctx)
  eq(days.map((p: any) => p.value), [5, 0])
  const wk = core.bucketSeries(meta, rows, { from: '2026-11-02', to: '2026-11-04' }, 'week', ctx)
  assert.equal(wk[0].na, 'custom')
})

// ------------------------------------------------------------ formatting, tables, themes

const EN_DASH = String.fromCharCode(0x2013)
const UP = String.fromCharCode(0x25b2)
const DOWN = String.fromCharCode(0x25bc)
const RUPEE = String.fromCharCode(0x20b9)

test('formatValue: en-IN groups in lakh and crore, en-US in thousands', () => {
  assert.equal(core.formatValue(1234568, 'integer', { locale: 'en-IN' }), '12,34,568')
  assert.equal(core.formatValue(1234568, 'number', { locale: 'en-IN', compact: true }), '12.3L')
  assert.equal(core.formatValue(12345678, 'number', { locale: 'en-IN', compact: true }), '1.2Cr')
  assert.equal(core.formatValue(1234568, 'integer', { locale: 'en-US' }), '1,234,568')
  assert.equal(core.formatValue(1234568, 'number', { locale: 'en-US', compact: true }), '1.2M')
  assert.equal(core.formatValue(1234568, 'compact', { locale: 'en-US' }), '1.2M')
})

test('formatValue: currency, percent, unit, null and text', () => {
  assert.equal(
    core.formatValue(1234567.5, 'currency', { locale: 'en-IN', currency: 'INR' }),
    RUPEE + '12,34,567.50',
  )
  assert.equal(core.formatValue(1234567, 'currency', { locale: 'en-IN', currency: 'INR', compact: true }), RUPEE + '12.3L')
  assert.equal(core.formatValue(0.123, 'percent', { locale: 'en-US' }), '12.3%')
  assert.equal(core.formatValue(12.3, 'percent_value', { locale: 'en-US' }), '12.3%')
  assert.equal(core.formatValue(1234.5, 'decimal2', { locale: 'en-US' }), '1,234.50')
  assert.equal(core.formatValue(12, 'integer', { locale: 'en-US', unit: 'kg' }), '12 kg')
  assert.equal(core.formatValue(null, 'number', { locale: 'en-US' }), EN_DASH)
  assert.equal(core.formatValue('n/a', 'number', { locale: 'en-US' }), 'n/a')
})

test('formatDate and formatAsOf use the dashboard locale', () => {
  assert.equal(core.formatDate('2026-11-03', 'axis', 'en-IN'), '3 Nov')
  assert.equal(core.formatDate('2026-11-03', 'axis', 'en-US'), 'Nov 3')
  const tip = new Intl.DateTimeFormat('en-IN', {
    weekday: 'short', day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC',
  }).format(new Date(Date.UTC(2026, 10, 3)))
  assert.equal(core.formatDate('2026-11-03', 'tooltip', 'en-IN'), tip)
  assert.equal(core.formatDate('2026-11-03', 'tooltip', 'en-IN'), 'Tue, 3 Nov, 2026')
  assert.equal(core.formatDate('2026-11-03', 'month', 'en-US'), 'Nov 2026')
  const iso = '2026-11-03T19:30:00.123456+00:00'
  const want = new Intl.DateTimeFormat('en-IN', {
    dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Kolkata',
  }).format(new Date(Date.UTC(2026, 10, 3, 19, 30)))
  assert.equal(core.formatAsOf(iso, 'en-IN', 'Asia/Kolkata'), want)
  assert.ok(want.includes('4'), 'as-of day is the 4th in Kolkata')
})

test('deltaParts: sign, colour class by good direction, flat and missing', () => {
  eq(core.deltaParts(110, 100, 'up'), { text: UP + ' +10.0%', cls: 'ic-good' })
  assert.equal(core.deltaParts(90, 100, 'up').cls, 'ic-bad')
  assert.equal(core.deltaParts(90, 100, 'down').cls, 'ic-good')
  assert.equal(core.deltaParts(90, 100, 'down').text, DOWN + ' -10.0%')
  assert.equal(core.deltaParts(120, 100, 'none').cls, 'ic-flat')
  eq(core.deltaParts(100, 100, 'up'), { text: '0.0%', cls: 'ic-flat' })
  assert.equal(core.deltaParts(5, 0, 'up'), null)
  assert.equal(core.deltaParts(5, null, 'up'), null)
})

test('sortRows: numeric, strings, nulls last, stable', () => {
  const rows = [
    { k: 'b', n: 10, i: 1 }, { k: 'a', n: 9, i: 2 }, { k: null, n: null, i: 3 }, { k: 'c', n: 10, i: 4 },
  ]
  eq(core.sortRows(rows, 'n', 'desc').map((r: any) => r.i), [1, 4, 2, 3])
  eq(core.sortRows(rows, 'n', 'asc').map((r: any) => r.i), [2, 1, 4, 3])
  eq(core.sortRows(rows, 'k', 'asc').map((r: any) => r.i), [2, 1, 4, 3])
  eq(core.sortRows(rows, 'k', 'desc').map((r: any) => r.i), [4, 1, 2, 3])
  eq(core.sortRows([{ n: 100 }, { n: 20 }], 'n', 'asc').map((r: any) => r.n), [20, 100])
})

test('pageRows pages 1-based and reports the window', () => {
  const rows = Array.from({ length: 60 }, (_, i) => ({ i }))
  const p2 = core.pageRows(rows, 2, 25)
  assert.equal(p2.rows[0].i, 25)
  assert.equal(p2.rows.length, 25)
  eq([p2.from, p2.to, p2.total], [26, 50, 60])
  eq([core.pageRows(rows, 3, 25).from, core.pageRows(rows, 3, 25).to], [51, 60])
  eq(core.pageRows([], 1, 25), { rows: [], from: 0, to: 0, total: 0 })
})

test('totalsRow equals every totals vector and sums query columns only with total', () => {
  for (const c of REAGG.totals) {
    const cells = core.totalsRow(
      [{ column: 'x' }],
      { rows: c.rows, range: c.range, rowsAre: c.rows_are, keyCol: c.key_col, ctx: mkCtx(c) },
      { x: c.meta },
    )
    close(cells[0].value, c.expect, c.name)
  }
  const q = core.totalsRow(
    [{ column: 'a', total: true }, { column: 'b' }, { column: 'c', total: true }],
    { rows: [{ a: 1, b: 2, c: 'x' }, { a: 4, b: 8, c: 'y' }], range: null, rowsAre: 'buckets', keyCol: null, ctx: {} },
    {},
  )
  eq(q.map((c: any) => c.value), [5, null, null])
})

test('dueState', () => {
  eq(core.dueState('2026-11-01', '2026-11-04', 7), { state: 'overdue', text: 'Overdue' })
  eq(core.dueState('2026-11-08', '2026-11-04', 7), { state: 'soon', text: 'Due in 4 days' })
  assert.equal(core.dueState('2026-12-30', '2026-11-04', 7), null)
  assert.equal(core.dueState('soon', '2026-11-04', 7), null)
  assert.equal(core.dueState(null, '2026-11-04', 7), null)
})

test('THEMES hold every UI-SPEC token and tone helpers follow the colour rules', () => {
  const light: any = {
    page: '#F5F6F8', surface: '#FFFFFF', border: '#DDE1E7', grid: '#E6E8EC', ink: '#14181F', ink2: '#4A5463',
    muted: '#6B7684', good: '#1A7F37', bad: '#C62F2F', owed: '#9A6100', inward: '#2A78D6', outward: '#9A4F94',
    neutral: '#445160', accent: '#0AA19B',
  }
  const dark: any = {
    page: '#080C14', surface: '#0E1420', border: '#1E2D42', grid: '#1E2D42', ink: '#F0F4F8', ink2: '#A0AEC0',
    muted: '#8A96A6', good: '#3FB27F', bad: '#E05252', owed: '#D4952B', inward: '#3987E5', outward: '#B8409A',
    neutral: '#5C6375', accent: '#1F9E93',
  }
  for (const k of Object.keys(light)) assert.equal(core.THEMES.light[k], light[k], 'light ' + k)
  for (const k of Object.keys(dark)) assert.equal(core.THEMES.dark[k], dark[k], 'dark ' + k)
  eq(core.THEMES.light.seq, ['#CDE2FB', '#86B6EF', '#3987E5', '#256ABF', '#104281'])
  eq(core.THEMES.dark.seq, ['#184F95', '#256ABF', '#3987E5', '#6DA7EC', '#B7D3F6'])
  eq(core.THEMES.light.ord, ['#86B6EF', '#3987E5', '#256ABF', '#104281'])
  eq(core.THEMES.dark.ord, ['#184F95', '#256ABF', '#3987E5', '#6DA7EC'])
  eq(core.THEMES.light.bands, ['#E6E8EC', '#D5D9E0', '#C3C9D2'])
  eq(core.THEMES.dark.bands, ['#141D2B', '#1A2536', '#22304A'])
  assert.equal(core.toneColor('dark', 'outward'), '#B8409A')
  eq(core.assignTones(1, []), ['inward'])
  eq(core.assignTones(5, []), ['inward', 'outward', 'accent', 'neutral', 'neutral'])
  eq(core.assignTones(3, [undefined, 'accent']), ['inward', 'accent', 'accent'])
})

test('filterRows keeps selected values; empty or missing lists keep all', () => {
  const rows = [{ c: 'A', r: 'x' }, { c: 'B', r: 'y' }, { c: 'A', r: 'y' }]
  assert.equal(core.filterRows(rows, { c: ['A'] }).length, 2)
  assert.equal(core.filterRows(rows, { c: ['A'], r: ['y'] }).length, 1)
  assert.equal(core.filterRows(rows, { c: [] }).length, 3)
  assert.equal(core.filterRows(rows, undefined).length, 3)
})

test('a NULL dimension member is its own member, labelled Not set, and filters apart from "null"', () => {
  const meta = { column: 'rev', kind: 'sum' }
  const rows = [
    { metric_time__day: '2026-10-01', rev: 5, cust: null },
    { metric_time__day: '2026-10-02', rev: 7, cust: 'null' },
    { metric_time__day: '2026-10-02', rev: 1, cust: undefined },
    { metric_time__day: '2026-10-03', rev: 2, cust: 'A' },
  ]
  const ctx = mkCtx({ as_of: '2026-11-04', horizon_start: '2026-01-01' })
  const items = core.memberValues(meta, rows, { from: '2026-10-01', to: '2026-10-03' }, 'cust', ctx)
  eq(
    items.map((m: any) => [m.member, m.value, m.key === core.NULL_KEY]),
    [['Not set', 6, true], ['null', 7, false], ['A', 2, false]],
  )
  eq(core.filterRows(rows, { cust: ['null'] }).map((r: any) => r.rev), [7])
  eq(core.filterRows(rows, { cust: [core.NULL_KEY] }).map((r: any) => r.rev), [5, 1])
})

// ---------------------------------------------------------------- 22-07: fixed-tile helpers and chart builders

const toPlain = (x: unknown) => JSON.parse(JSON.stringify(x))

test('periodKey and xLabel: grains, and locale axis labels only when a locale is given', () => {
  assert.equal(core.periodKey('2026-11-04T10:00:00Z', 'day'), '2026-11-04')
  assert.equal(core.periodKey('2026-11-04', 'week'), '2026-11-02')
  assert.equal(core.periodKey('2026-11-04', 'month'), '2026-11')
  assert.equal(core.periodKey('2026-11-04', 'quarter'), '2026-Q4')
  assert.equal(core.periodKey('2026-11-04', 'year'), '2026')
  assert.equal(core.periodKey(null, 'day'), '')
  assert.equal(core.periodKey('North', 'day'), 'North')
  assert.equal(core.xLabel('2026-11-04', 'day'), '2026-11-04')
  assert.equal(core.xLabel('2026-11-04', 'day', 'en-US'), 'Nov 4')
  assert.equal(core.xLabel('2026-11-04', 'day', 'en-IN'), '4 Nov')
  assert.equal(core.xLabel('2026-11-04', 'month', 'en-US'), 'Nov 2026')
  assert.equal(core.xLabel('North', 'month', 'en-US'), 'North')
})

test('owed and isStale (v1 semantics)', () => {
  assert.equal(core.owed(5, { op: '>', value: 4 }), true)
  assert.equal(core.owed(4, { op: '>', value: 4 }), false)
  assert.equal(core.owed('x', { op: '>', value: 4 }), false)
  assert.equal(core.owed(5, null), false)
  const now = Date.parse('2026-11-04T10:00:00Z')
  assert.equal(core.isStale('2026-11-04T09:50:00+00:00', 15, now), false)
  assert.equal(core.isStale('2026-11-04T09:00:00+00:00', 15, now), true)
  assert.equal(core.isStale('2026-11-04 09:00:00+00', 15, now), true)
  assert.equal(core.isStale(null, 15, now), false)
})

test('pickStat: last, last_complete, previous_row and column compare', () => {
  const rows = [
    { m: '2026-09-01', v: 1, p: 9 },
    { m: '2026-10-01', v: 2, p: 8 },
    { m: '2026-11-01', v: 3, p: 7 },
  ]
  const t: any = { column: 'v', x: 'm', grain: 'month', pick: 'last', compare: 'previous_row' }
  assert.deepEqual(toPlain(core.pickStat(rows, t, '2026-11-04', 'en-US')), {
    value: 3, previous: 2, period: 'Nov 2026', key: '2026-11-01',
  })
  t.pick = 'last_complete'
  assert.equal(core.pickStat(rows, t, '2026-11-04').value, 2)
  t.pick = 'last'
  t.compare = { column: 'p' }
  assert.equal(core.pickStat(rows, t, '2026-11-04').previous, 7)
  assert.deepEqual(toPlain(core.pickStat([], t, '2026-11-04')), { value: null, previous: null, period: null, key: '' })
})

test('pivot: first four series coloured, the rest folded into Other or dropped and counted', () => {
  const rows: any[] = []
  for (const x of ['2026-10-01', '2026-11-01']) {
    for (const [i, s] of ['a', 'b', 'c', 'd', 'e', 'f'].entries()) rows.push({ x, s, y: i + 1 })
  }
  rows.push({ x: '2026-11-01', s: null, y: 100 })
  const folded = core.pivot(rows, 'x', 's', 'y', 'month', { locale: 'en-US' })
  assert.deepEqual(toPlain(folded.labels), ['Oct 2026', 'Nov 2026'])
  assert.deepEqual(toPlain(folded.datasets.map((d: any) => d.label)), ['a', 'b', 'c', 'd', 'Other'])
  assert.deepEqual(toPlain(folded.datasets[4].data), [11, 111]) // e + f, then e + f + the NULL member
  assert.equal(folded.dropped, 0)
  const dropped = core.pivot(rows, 'x', 's', 'y', 'month', { fold: false })
  assert.equal(dropped.datasets.length, 4)
  assert.equal(dropped.dropped, 3)
  const nulls = core.pivot([{ x: '2026-10-01', s: null, y: 1 }], 'x', 's', 'y', 'day')
  assert.equal(nulls.datasets[0].label, 'Not set')
})

const SUM = { column: 'qty', label: 'Qty', description: null, kind: 'sum', parts: null, formula: null, date_basis: null }

function dailyRows(from: string, to: string, f: (d: string) => number) {
  return core.eachDay(from, to).map((d: string) => ({ metric_time__day: d, qty: f(d) }))
}

test('timeModel: day buckets, exact values, raw kept, ghost aligned by position', () => {
  const rows = dailyRows('2025-10-01', '2026-11-04', (d: string) => +d.slice(8, 10))
  const tile: any = {
    id: 't', type: 'line', title: 'T', format: 'integer', measures: [SUM], ghost: 'previous_period',
    y: [{ column: 'qty', label: 'Qty' }],
  }
  const ctx = { today: '2026-11-04', asOf: '2026-11-04', horizonStart: '2025-10-01', preset: 'last_7_days' }
  const m = core.timeModel(tile, rows, { from: '2026-10-29', to: '2026-11-04' }, ctx, 'en-US')
  assert.equal(m.bucket, 'day')
  assert.deepEqual(toPlain(m.sets[0].data), [29, 30, 31, 1, 2, 3, 4])
  assert.deepEqual(toPlain(m.sets[1].data), [22, 23, 24, 25, 26, 27, 28])
  assert.equal(m.sets[1].ghost, true)
  assert.deepEqual(toPlain(m.cells[0]), { key: '2026-10-29', from: '2026-10-29', to: '2026-10-29', row: '2026-10-29' })
  assert.equal(m.na, null)
  const far = core.timeModel(tile, rows, { from: '2025-10-01', to: '2025-10-07' }, ctx, 'en-US')
  assert.equal(far.ghostNa, true)
  assert.equal(far.sets.length, 1)
})

test('timeModel: week buckets carry from..to rows clipped to the range; no data after as-of', () => {
  const rows = dailyRows('2025-10-01', '2026-11-04', () => 1)
  const tile: any = { id: 't', type: 'bar', title: 'T', format: 'integer', measures: [SUM], y: [{ column: 'qty' }] }
  const ctx = { today: '2026-11-05', asOf: '2026-11-04', horizonStart: '2025-10-01' }
  const m = core.timeModel(tile, rows, { from: '2026-08-07', to: '2026-11-05' }, ctx, 'en-US')
  assert.equal(m.bucket, 'week')
  assert.equal(m.cells[0].row, '2026-08-07..2026-08-09') // first week clipped to the range start
  const last = m.sets[0].data
  assert.equal(last[last.length - 1], 3) // Mon Nov 2 .. Wed Nov 4 have data, Nov 5 is not there yet
  const gone = core.timeModel(tile, rows, { from: '2026-11-05', to: '2026-11-05' }, ctx, 'en-US')
  assert.equal(gone.na, 'nodata')
})

test('timeChartConfig: bars, stacks, percent, legend, colours only from THEMES', () => {
  const rows = dailyRows('2026-10-01', '2026-11-04', () => 2)
  const tile: any = {
    id: 't', type: 'stacked_bar', title: 'T', format: 'integer', measures: [SUM],
    y: [{ column: 'qty', tone: 'outward' }], percent: true,
  }
  const ctx = { today: '2026-11-04', asOf: '2026-11-04', horizonStart: '2026-10-01' }
  const m = core.timeModel(tile, rows, { from: '2026-10-29', to: '2026-11-04' }, ctx, 'en-IN')
  const cfg = core.timeChartConfig(tile, m, 'light', 'en-IN')
  assert.equal(cfg.type, 'bar')
  assert.equal(cfg.data.datasets[0].backgroundColor, core.THEMES.light.outward)
  assert.equal(cfg.options.scales.x.stacked, true)
  assert.equal(cfg.options.scales.y.max, 100)
  assert.equal(cfg.options.plugins.legend.display, false)
  assert.equal(typeof cfg.options.plugins.tooltip.callbacks.label, 'function')
  assert.equal(cfg.data.labels[0], '29 Oct')
})

test('fixedChartConfig: horizontal bars, band datasets hidden from the legend, axis titles', () => {
  const rows = [{ x: 'a', y: 1, lo: 0, hi: 2 }, { x: 'b', y: 2, lo: 1, hi: 3 }]
  const bar: any = {
    id: 'b', type: 'bar', title: 'B', format: 'integer', x: 'x', horizontal: true,
    y: [{ column: 'y' }], x_label: 'Cat', y_label: 'Val',
  }
  const cfg = core.fixedChartConfig(bar, core.fixedModel(bar, rows, 'en-US'), 'dark', 'en-US')
  assert.equal(cfg.options.indexAxis, 'y')
  assert.equal(cfg.options.scales.x.beginAtZero, true)
  assert.equal(cfg.options.scales.y.title.text, 'Cat')
  assert.equal(cfg.options.scales.x.title.text, 'Val')
  const line: any = {
    id: 'l', type: 'line', title: 'L', format: 'integer', x: 'x', y: [{ column: 'y' }],
    band: { low: 'lo', high: 'hi', label: 'Range' },
  }
  const lc = core.fixedChartConfig(line, core.fixedModel(line, rows, 'en-US'), 'dark', 'en-US')
  assert.equal(lc.data.datasets.length, 3)
  assert.equal(lc.data.datasets[1].ic_hide, true)
  assert.equal(lc.options.plugins.legend.labels.filter({ datasetIndex: 1 }, lc.data), false)
})

test('fixedModel: series beyond four fold only for sum measures, never add averages', () => {
  const rows: any[] = []
  for (let i = 1; i <= 7; i++) rows.push({ x: 'a', s: `c${i}`, avg_kg: 50 })
  const mk = (kind?: string): any => ({
    id: 't', type: 'bar', title: 'T', format: 'integer', x: 'x', series: 's', y: [{ column: 'avg_kg' }],
    ...(kind ? { measures: [{ column: 'avg_kg', kind }] } : {}),
  })
  const avg = core.fixedModel(mk('avg'), rows, 'en-US')
  assert.equal(avg.sets.some((d: any) => d.other), false)
  assert.equal(avg.dropped, 3)
  const sum = core.fixedModel(mk('sum'), rows, 'en-US')
  assert.equal(sum.sets.find((d: any) => d.other).data[0], 150)
  const bound = core.fixedModel(mk(), rows, 'en-US')
  assert.equal(bound.sets.find((d: any) => d.other).data[0], 150)
})

test('fixedModel: at most four explicit y series, the rest counted', () => {
  const rows = [{ x: 'a', a: 1, b: 2, c: 3, d: 4, e: 5, f: 6 }]
  const t: any = { id: 't', type: 'bar', title: 'T', format: 'integer', x: 'x', y: ['a', 'b', 'c', 'd', 'e', 'f'].map((column) => ({ column })) }
  const m = core.fixedModel(t, rows, 'en-US')
  assert.equal(m.sets.length, 4)
  assert.equal(m.dropped, 2)
})

test('memberModel: bars per member by range value with a summed Other past the cap', () => {
  const rows: any[] = []
  for (const d of ['2026-11-03', '2026-11-04']) {
    for (let i = 1; i <= 14; i++) rows.push({ metric_time__day: d, region: 'r' + i, qty: i })
  }
  const t: any = { id: 'm', type: 'bar', title: 'M', format: 'integer', x: 'region', measures: [SUM], y: [{ column: 'qty' }] }
  const ctx = { today: '2026-11-04', asOf: '2026-11-04', horizonStart: '2026-10-01' }
  const m = core.memberModel(t, rows, { from: '2026-11-03', to: '2026-11-04' }, ctx, 'en-US')
  assert.equal(m.labels[0], 'r14')
  assert.equal(m.labels.length, 12) // top 11 plus Other (more than 12 members)
  assert.equal(m.labels[11], 'Other')
  assert.equal(m.sets[0].data[11], (1 + 2 + 3) * 2) // r1 to r3, two days
  assert.equal(m.cells[0].row, 'r14')
})

// ---------------------------------------------------------------- 22-07 task 3: stylesheet and renderer agree

test('THEMES hex values equal the custom properties of SPEC_CSS_2_0_0 for the same tokens', () => {
  const py = readFileSync(join(process.cwd(), '..', 'backend', 'app', 'dashboardrender.py'), 'utf8')
  function tokens(name: string): Record<string, string> {
    const m = new RegExp(name + '\\s*=\\s*\\(([^)]*)\\)').exec(py)
    assert.ok(m, name)
    const out: Record<string, string> = {}
    for (const t of m[1].matchAll(/--ic-([a-z0-9-]+):(#[0-9A-Fa-f]{6})/g)) {
      out[t[1].replace(/-(\d)/, '$1')] = t[2].toUpperCase()
    }
    return out
  }
  const names = ['page', 'surface', 'border', 'grid', 'ink', 'ink2', 'muted', 'good', 'bad', 'owed',
    'inward', 'outward', 'neutral', 'accent']
  for (const [theme, pyName] of [['light', '_TOKENS_LIGHT'], ['dark', '_TOKENS_DARK']] as const) {
    const css = tokens(pyName)
    assert.deepEqual(Object.keys(css).sort(), [...names].sort(), pyName)
    for (const n of names) {
      assert.equal(core.THEMES[theme][n].toUpperCase(), css[n], theme + ' ' + n)
    }
  }
})

test('rangeValue: a now measure with several rows is not guessed', () => {
  const rg = { from: '2026-01-01', to: '2026-01-02' }
  eq(core.rangeValue({ kind: 'now', column: 'n' }, [{ n: 1 }, { n: 2 }], rg), { na: 'nodata' })
  assert.equal(core.rangeValue({ kind: 'now', column: 'n' }, [{ n: 3 }], rg), 3)
})

// ---------------------------------------------------------------- 22-09: tableModel

function tcol(column: string, extra: any = {}) {
  return { column, label: null, format: null, currency: null, unit: null, owed_when: null, delta: false, good_direction: 'none', bar: false, total: null, due: null, ...extra }
}

test('tableModel: fixed rows keep the returned order, sort and page on request, totals cached', () => {
  const rows = [{ k: 'a', v: 3 }, { k: 'b', v: 9 }, { k: 'c', v: 5 }]
  const tile: any = { type: 'table', columns: [tcol('k'), tcol('v', { total: true })], max_rows: 2, totals: true, measures: [], by_columns: [], sort: null }
  const m = core.tableModel(tile, { rows }, { locale: 'en-US' }, {})
  assert.equal(m.rows.length, 2)
  assert.equal(m.pageInfo.text, 'Rows 1-2 of 3')
  assert.equal(m.totals[1].text, '17')
  assert.equal(m.totals[0].text, 'Total')
  const s = core.tableModel(tile, { rows }, { locale: 'en-US', sort: { column: 'v', dir: 'desc' }, page: 2, totalsCache: m.totalVals }, {})
  assert.equal(s.rows[0].cells[1].text, '3')
  assert.equal(s.pageInfo.text, 'Rows 3-3 of 3')
  const clamped = core.tableModel(tile, { rows }, { locale: 'en-US', page: 99 }, {})
  assert.equal(clamped.pageInfo.page, 2)
})

test('tableModel: tile.sort orders a fixed table initially', () => {
  const rows = [{ k: 'a', v: 3 }, { k: 'b', v: 9 }]
  const tile: any = { type: 'table', columns: [tcol('k'), tcol('v')], max_rows: 25, totals: false, measures: [], by_columns: [], sort: { column: 'v', dir: 'desc' } }
  const m = core.tableModel(tile, { rows }, { locale: 'en-US' }, {})
  eq(m.rows.map((r: any) => r.cells[0].text), ['b', 'a'])
  assert.equal(m.totals, null)
})

test('fixedTotal: closing and opening of a table with no time key add each member value', () => {
  const rows = [{ wh: 'a', bal: 5 }, { wh: 'b', bal: 7 }]
  const tile: any = { type: 'table', grain: null }
  assert.equal(core.fixedTotal(tile, { kind: 'closing', column: 'bal' }, rows, 'wh'), 12)
  assert.equal(core.fixedTotal(tile, { kind: 'opening', column: 'bal' }, rows, 'wh'), 12)
  assert.equal(core.fixedTotal(tile, { kind: 'closing', column: 'bal' }, [], 'wh'), null)
})

test('tableModel: a controlled member table reports why a column has no value', () => {
  const rows = [{ k: 'a', u: 3 }, { k: 'b', u: 4 }]
  const tile: any = { type: 'table', columns: [tcol('k'), tcol('u')], max_rows: 25, totals: false,
    measures: [{ column: 'u', kind: 'distinct' }], by_columns: ['k'], sort: null }
  const range = { from: '2026-11-01', to: '2026-11-04' }
  const m = core.tableModel(tile, { rows }, { locale: 'en-US', controlled: true, range, today: '2026-11-04' }, { today: '2026-11-04' })
  assert.equal(m.na, 'custom')
  const fixed = core.tableModel(tile, { rows }, { locale: 'en-US' }, {})
  assert.equal(fixed.na, null)
})

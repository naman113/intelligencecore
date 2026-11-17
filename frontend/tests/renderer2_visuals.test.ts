// Phase 22 (22-09, DCAP-09, DCAP-12): pure config builders of ic-render-2.0.0 for the new visuals and targets.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import vm from 'node:vm'

const SRC = readFileSync(
  join(process.cwd(), '..', 'dashboards', 'vendor', 'ic-render-2.0.0', 'render.js'),
  'utf8',
)
const ctx: any = vm.createContext({ Intl })
vm.runInContext(SRC, ctx)
const core = ctx.ICRenderCore
const TH = core.THEMES

// vm contexts have their own prototypes: compare through JSON
function eq(a: unknown, b: unknown, m?: string) {
  assert.deepStrictEqual(JSON.parse(JSON.stringify(a)), JSON.parse(JSON.stringify(b)), m)
}

function ys(...cols: Array<string | { column: string; as?: string; tone?: string }>) {
  return cols.map((c) => {
    const o: any = typeof c === 'string' ? { column: c } : c
    return { column: o.column, label: o.label ?? o.column, dashed: false, tone: o.tone ?? null, as: o.as ?? null }
  })
}

function tileOf(type: string, extra: any = {}) {
  return { id: 't', type, title: 'T', format: 'integer', currency: null, unit: null, grain: null, x: 'k', y: ys('v'),
    measures: [], by_columns: [], target: null, ...extra }
}

function V(tile: any, rows: any[], theme = 'dark') {
  return core.vizData(tile, rows, null, null, 'en-US')
}

const ROWS = [{ k: 'a', v: 10, w: 4 }, { k: 'b', v: 30, w: 6 }, { k: 'c', v: 20, w: 5 }]

test('combo: bar and line datasets, lines on top, one beginAtZero axis, tones per series', () => {
  const t = tileOf('combo', { y: ys({ column: 'v', as: 'bar' }, { column: 'w', as: 'line' }) })
  const cfg = core.comboConfig(t, V(t, ROWS), 'dark', 'en-US')
  assert.equal(cfg.type, 'bar')
  assert.equal(cfg.data.datasets.length, 2)
  const [bar, line] = cfg.data.datasets
  assert.equal(bar.type, 'bar')
  assert.equal(line.type, 'line')
  assert.equal(line.order, 0)
  assert.ok(bar.order > line.order)
  assert.equal(bar.backgroundColor, TH.dark.inward)
  assert.equal(line.borderColor, TH.dark.outward)
  assert.equal(cfg.options.scales.y.beginAtZero, true)
  assert.equal(cfg.options.animation, false)
  assert.equal(cfg.options.maintainAspectRatio, false)
  assert.equal(cfg.options.plugins.colors.enabled, false)
  eq(cfg.data.labels, ['a', 'b', 'c'])
})

test('combo: a target value is a dashed ink annotation, a target column a dashed ink line dataset', () => {
  const t = tileOf('combo', { y: ys({ column: 'v', as: 'bar' }, { column: 'w', as: 'line' }), target: { value: 25, label: null } })
  const cfg = core.comboConfig(t, V(t, ROWS), 'light', 'en-US')
  const a = cfg.options.plugins.annotation.annotations.target
  assert.equal(a.type, 'line')
  assert.equal(a.scaleID, 'y')
  assert.equal(a.value, 25)
  assert.equal(a.borderColor, TH.light.ink)
  eq(a.borderDash, [4, 4])
  assert.equal(a.label.content, 'Target set by owner')
  const t2 = tileOf('combo', { y: ys({ column: 'v', as: 'bar' }, { column: 'w', as: 'line' }), target: { column: 'w', label: null } })
  const cfg2 = core.comboConfig(t2, V(t2, ROWS), 'dark', 'en-US')
  assert.equal(cfg2.data.datasets.length, 3)
  const tg = cfg2.data.datasets[2]
  assert.equal(tg.label, 'Target')
  assert.equal(tg.borderColor, TH.dark.ink)
  eq(tg.borderDash, [4, 4])
  assert.equal(cfg2.options.plugins.annotation, undefined)
})

test('paired bars: two grouped datasets inward and outward by default', () => {
  const t = tileOf('paired_bar', { y: ys('v', 'w') })
  const cfg = core.pairedConfig(t, V(t, ROWS), 'dark', 'en-US')
  assert.equal(cfg.data.datasets.length, 2)
  assert.equal(cfg.data.datasets[0].backgroundColor, TH.dark.inward)
  assert.equal(cfg.data.datasets[1].backgroundColor, TH.dark.outward)
  eq(cfg.data.datasets[1].data, [4, 6, 5])
  assert.notEqual(cfg.options.scales.y.stacked, true)
})

test('paired bars: mirror negates the second series and shows absolute values on ticks and tooltips', () => {
  const t = tileOf('paired_bar', { y: ys('v', 'w'), mirror: true })
  const cfg = core.pairedConfig(t, V(t, ROWS), 'dark', 'en-US')
  eq(cfg.data.datasets[1].data, [-4, -6, -5])
  eq(cfg.data.datasets[0].data, [10, 30, 20])
  assert.equal(cfg.options.scales.y.ticks.callback(-6), '6')
  assert.equal(cfg.options.plugins.tooltip.callbacks.label({ datasetIndex: 1, dataIndex: 1 }), 'w: 6')
  assert.equal(cfg.options.scales.y.stacked, true)
})

test('aging: one bar dataset coloured from the ordinal ramp, darkest repeated beyond 4', () => {
  const rows = ['0-30', '31-60', '61-90', '90+', '180+', '365+'].map((k, i) => ({ k, v: i + 1 }))
  const t = tileOf('aging')
  const cfg = core.agingConfig(t, V(t, rows), 'light', 'en-US')
  assert.equal(cfg.data.datasets.length, 1)
  const ord = TH.light.ord
  eq(cfg.data.datasets[0].backgroundColor, [ord[0], ord[1], ord[2], ord[3], ord[3], ord[3]])
  eq(cfg.data.labels, ['0-30', '31-60', '61-90', '90+', '180+', '365+'])
})

test('bullet: horizontal, floating bands from 0, a thinner inward actual bar and a 3 px ink target tick', () => {
  const t = tileOf('bullet', { column: 'v', bands: [50, 80, 100], target: { value: 75, label: null } })
  const B = core.bulletValues(t, [{ v: 60 }], null, null)
  eq(B, { actual: 60, target: 75, owner: true, na: null })
  const cfg = core.bulletConfig(t, B, 'dark', 'en-US')
  assert.equal(cfg.options.indexAxis, 'y')
  assert.equal(cfg.data.datasets.length, 4)
  eq(cfg.data.datasets[0].data, [[0, 50]])
  eq(cfg.data.datasets[1].data, [[50, 80]])
  eq(cfg.data.datasets[2].data, [[80, 100]])
  eq(cfg.data.datasets.slice(0, 3).map((d: any) => d.backgroundColor), TH.dark.bands)
  const actual = cfg.data.datasets[3]
  assert.equal(actual.backgroundColor, TH.dark.inward)
  assert.equal(actual.grouped, false)
  assert.ok(actual.barPercentage < cfg.data.datasets[0].barPercentage)
  const a = cfg.options.plugins.annotation.annotations.target
  assert.equal(a.borderWidth, 3)
  assert.equal(a.borderColor, TH.dark.ink)
  assert.equal(a.scaleID, 'x')
  assert.equal(a.value, 75)
  assert.equal(a.label.content, 'Target set by owner')
})

test('bullet: a target column is labelled Target; fixed values come from the last row', () => {
  const t = tileOf('bullet', { column: 'v', target: { column: 'goal', label: null } })
  const B = core.bulletValues(t, [{ v: 1, goal: 5 }, { v: 60, goal: 80 }], null, null)
  eq(B, { actual: 60, target: 80, owner: false, na: null })
  const cfg = core.bulletConfig(t, B, 'dark', 'en-US')
  assert.equal(cfg.options.plugins.annotation.annotations.target.label.content, 'Target')
  assert.equal(cfg.data.datasets.length, 1)
})

test('waterfall: floating bars, first and last are totals from 0 in neutral, steps inward or outward', () => {
  const rows = [{ k: 'Start', v: 100 }, { k: 'Up', v: 30 }, { k: 'Down', v: -50 }, { k: 'End', v: 80 }]
  const t = tileOf('waterfall', { ends: 'first_last' })
  const cfg = core.waterfallConfig(t, V(t, rows), 'dark', 'en-US')
  const d = cfg.data.datasets[0]
  eq(d.data, [[0, 100], [100, 130], [130, 80], [0, 80]])
  eq(d.backgroundColor, [TH.dark.neutral, TH.dark.inward, TH.dark.outward, TH.dark.neutral])
  assert.equal(cfg.options.plugins.tooltip.callbacks.label({ dataIndex: 1 }), 'v: +30')
  assert.equal(cfg.options.plugins.tooltip.callbacks.label({ dataIndex: 2 }), 'v: -50')
  assert.equal(cfg.options.plugins.tooltip.callbacks.label({ dataIndex: 0 }), 'v: 100')
})

test('waterfall: ends none treats every row as a step from 0', () => {
  const rows = [{ k: 'a', v: 10 }, { k: 'b', v: -4 }]
  const t = tileOf('waterfall', { ends: 'none' })
  const cfg = core.waterfallConfig(t, V(t, rows), 'dark', 'en-US')
  eq(cfg.data.datasets[0].data, [[0, 10], [10, 6]])
  eq(cfg.data.datasets[0].backgroundColor, [TH.dark.inward, TH.dark.outward])
})

test('calendar: matrix cells by week start and weekday, five equal colour steps from the sequential ramp', () => {
  // 2026-11-02 is a Monday
  const rows = [
    { k: '2026-11-02', v: 0 }, { k: '2026-11-03', v: 10 }, { k: '2026-11-04', v: 20 },
    { k: '2026-11-08', v: 50 }, { k: '2026-11-09', v: 100 },
  ]
  const t = tileOf('calendar')
  const cfg = core.calendarConfig(t, V(t, rows), 'light', 'en-US')
  assert.equal(cfg.type, 'matrix')
  const data = cfg.data.datasets[0].data
  assert.equal(data.length, 5)
  eq(data[0], { x: '2026-11-02', y: 1, v: 0, d: '2026-11-02' })
  eq(data[3], { x: '2026-11-02', y: 7, v: 50, d: '2026-11-08' })
  eq(data[4], { x: '2026-11-09', y: 1, v: 100, d: '2026-11-09' })
  const color = cfg.data.datasets[0].backgroundColor
  const seq = TH.light.seq
  assert.equal(color({ raw: { v: 0 } }), seq[0])
  assert.equal(color({ raw: { v: 25 } }), seq[1])
  assert.equal(color({ raw: { v: 50 } }), seq[2])
  assert.equal(color({ raw: { v: 79 } }), seq[3])
  assert.equal(color({ raw: { v: 100 } }), seq[4])
  eq(cfg.options.scales.x.labels, ['2026-11-02', '2026-11-09'])
  assert.equal(cfg.options.plugins.tooltip.callbacks.title([{ raw: { d: '2026-11-03' } }]), 'Tue, Nov 3, 2026')
  assert.equal(cfg.options.plugins.tooltip.callbacks.label({ raw: { v: 1234 } }), 'v: 1,234')
  const dark = core.calendarConfig(t, V(t, rows), 'dark', 'en-US')
  assert.equal(dark.data.datasets[0].backgroundColor({ raw: { v: 100 } }), TH.dark.seq[4])
  assert.equal(TH.dark.seq[4], '#B7D3F6')
})

test('calendar: at most 400 days are drawn (the latest)', () => {
  const rows: any[] = []
  for (let i = 0; i < 450; i++) {
    const d = new Date(Date.UTC(2025, 0, 1) + i * 86400000).toISOString().slice(0, 10)
    rows.push({ k: d, v: i })
  }
  const t = tileOf('calendar')
  const days = core.calendarDays(V(t, rows))
  assert.equal(days.length, 400)
  assert.equal(days[399].v, 449)
  assert.equal(days[0].v, 50)
})

test('donut: up to 5 parts sorted descending, ordinal ramp darkest first, legend shows the percentage', () => {
  const rows = [{ k: 'a', v: 10 }, { k: 'b', v: 30 }, { k: 'c', v: 60 }]
  const t = tileOf('donut')
  const cfg = core.donutConfig(t, V(t, rows), 'light', 'en-US')
  assert.equal(cfg.type, 'doughnut')
  eq(cfg.data.datasets[0].data, [60, 30, 10])
  eq(cfg.data.labels, ['c (60%)', 'b (30%)', 'a (10%)'])
  eq(cfg.data.datasets[0].backgroundColor, TH.light.ord.slice().reverse().slice(0, 3))
})

test('donut: more than 5 parts fold the smallest into a neutral Other (kind aware)', () => {
  const rows = ['a', 'b', 'c', 'd', 'e', 'f', 'g'].map((k, i) => ({ k, v: 70 - i * 10 }))
  const t = tileOf('donut')
  const view = core.vizView(t, V(t, rows), 'en-US')
  assert.equal(view.folded, true)
  eq(view.labels, ['a', 'b', 'c', 'd', 'Other'])
  assert.equal(view.series[0].data[4], 60)
  const cfg = core.donutConfig(t, V(t, rows), 'dark', 'en-US')
  assert.equal(cfg.data.datasets[0].backgroundColor[4], TH.dark.neutral)
  assert.equal(cfg.data.datasets[0].backgroundColor.length, 5)
  assert.equal(view.cells[4].other, true)
})

test('donut: a measure that does not combine draws the first 5 parts and counts the rest', () => {
  const rows = ['a', 'b', 'c', 'd', 'e', 'f', 'g'].map((k, i) => ({ k, v: 70 - i * 10 }))
  const t = tileOf('donut', { measures: [{ column: 'v', kind: 'avg', label: 'v', description: null, parts: null, formula: null, date_basis: null }] })
  const view = core.vizView(t, V(t, rows), 'en-US')
  assert.equal(view.folded, false)
  assert.equal(view.labels.length, 5)
  assert.equal(view.dropped, 2)
})

test('ranked bar: horizontal, sorted descending, top n with Other in neutral', () => {
  const rows = ['a', 'b', 'c', 'd', 'e'].map((k, i) => ({ k, v: (i + 1) * 10 }))
  const t = tileOf('ranked_bar', { top: 2, other: true })
  const cfg = core.rankedConfig(t, V(t, rows), 'dark', 'en-US')
  assert.equal(cfg.options.indexAxis, 'y')
  eq(cfg.data.labels, ['e', 'd', 'Other'])
  eq(cfg.data.datasets[0].data, [50, 40, 60])
  eq(cfg.data.datasets[0].backgroundColor, [TH.dark.inward, TH.dark.inward, TH.dark.neutral])
  const view = core.vizView(t, V(t, rows), 'en-US')
  assert.equal(view.cells[2].other, true)
})

test('ranked bar: without other the rest is counted; cumulative adds an 80% marker at the first bar reaching it', () => {
  const rows = [{ k: 'a', v: 50 }, { k: 'b', v: 30 }, { k: 'c', v: 10 }, { k: 'd', v: 10 }]
  const t = tileOf('ranked_bar', { top: 3, other: false, cumulative: true })
  const view = core.vizView(t, V(t, rows), 'en-US')
  assert.equal(view.dropped, 1)
  const cfg = core.rankedConfig(t, V(t, rows), 'dark', 'en-US')
  const a = cfg.options.plugins.annotation.annotations.cumulative
  assert.equal(a.scaleID, 'y')
  assert.equal(a.value, 1) // 50 + 30 = 80 of 100 already reaches 80%
  assert.equal(a.label.content, '80% of total')
  assert.equal(a.borderColor, TH.dark.ink)
  const t2 = tileOf('ranked_bar', { cumulative: false })
  assert.equal(core.rankedConfig(t2, V(t2, rows), 'dark', 'en-US').options.plugins.annotation, undefined)
})

test('funnel: bars in returned order with the ordinal ramp and a percent of first on each label', () => {
  const rows = [{ k: 'Visit', v: 200 }, { k: 'Signup', v: 50 }, { k: 'Buy', v: 10 }]
  const t = tileOf('funnel')
  const cfg = core.funnelConfig(t, V(t, rows), 'dark', 'en-US')
  assert.equal(cfg.options.indexAxis, 'y')
  eq(cfg.data.labels, ['Visit (100% of first)', 'Signup (25% of first)', 'Buy (5% of first)'])
  eq(cfg.data.datasets[0].backgroundColor, [TH.dark.ord[0], TH.dark.ord[1], TH.dark.ord[2]])
  eq(cfg.data.datasets[0].data, [200, 50, 10])
})

test('ranked bar and funnel grow 28 px per bar up to 400 px', () => {
  assert.equal(core.barsHeight(5), 180)
  assert.equal(core.barsHeight(40), 400)
  const rows = Array.from({ length: 6 }, (_, i) => ({ k: 'k' + i, v: i }))
  const t = tileOf('funnel')
  assert.equal(core.vizView(t, V(t, rows), 'en-US').height, core.barsHeight(6))
})

test('every builder sets animation false, responsive, no auto colours', () => {
  const t1 = tileOf('aging')
  const t2 = tileOf('paired_bar', { y: ys('v', 'w') })
  const t3 = tileOf('funnel')
  const t4 = tileOf('ranked_bar')
  const t5 = tileOf('donut')
  const cfgs = [
    core.agingConfig(t1, V(t1, ROWS), 'dark', 'en-US'),
    core.pairedConfig(t2, V(t2, ROWS), 'dark', 'en-US'),
    core.funnelConfig(t3, V(t3, ROWS), 'dark', 'en-US'),
    core.rankedConfig(t4, V(t4, ROWS), 'dark', 'en-US'),
    core.donutConfig(t5, V(t5, ROWS), 'dark', 'en-US'),
  ]
  for (const c of cfgs) {
    assert.equal(c.options.animation, false)
    assert.equal(c.options.responsive, true)
    assert.equal(c.options.maintainAspectRatio, false)
    assert.equal(c.options.plugins.colors.enabled, false)
  }
})

test('controlled visuals: a time x gives bucketed values, a by x gives members over the range', () => {
  const rows: any[] = []
  for (let i = 0; i < 7; i++) {
    const d = '2026-10-' + String(29 + i > 31 ? 29 + i - 31 : 29 + i).padStart(2, '0')
    const day = i < 3 ? d : '2026-11-' + String(i - 2).padStart(2, '0')
    rows.push({ metric_time__day: day, v: 1, cust: 'A' }, { metric_time__day: day, v: 2, cust: 'B' })
  }
  const meta = { column: 'v', kind: 'sum', label: 'v', description: null, parts: null, formula: null, date_basis: null }
  const range = { from: '2026-10-29', to: '2026-11-04' }
  const tt = tileOf('waterfall', { x: 'metric_time__day', measures: [meta] })
  const vt = core.vizData(tt, rows, range, { dayCol: 'metric_time__day' }, 'en-US')
  assert.equal(vt.bucket, 'day')
  assert.equal(vt.labels.length, 7)
  assert.equal(vt.ys[0].data[0], 3)
  const tm = tileOf('ranked_bar', { x: 'cust', measures: [meta], by_columns: ['cust'] })
  const vm_ = core.vizData(tm, rows, range, { dayCol: 'metric_time__day' }, 'en-US')
  eq(vm_.labels, ['B', 'A'])
  eq(vm_.ys[0].data, [14, 7])
  const view = core.vizView(tm, vm_, 'en-US')
  eq(view.labels, ['B', 'A'])
})

test('hidden table cells of Other carry no value; real cells keep the raw value', () => {
  const rows = ['a', 'b', 'c', 'd'].map((k, i) => ({ k, v: (i + 1) * 10 }))
  const t = tileOf('ranked_bar', { top: 2, other: true })
  const view = core.vizView(t, V(t, rows), 'en-US')
  const pm = core.vizPseudo(t, view)
  assert.equal(pm.labels.length, 3)
  assert.equal(pm.cells[2].other, true)
  eq(pm.sets[0].raw, [40, 30, 30])
})

test('stat targets: a value is labelled by the owner, progress is value over target and capped at 100 for the width', () => {
  const t = tileOf('stat', { target: { value: 200, label: null } })
  const tg = core.statTarget(t, [], null, null, '2026-11-04', 'en-US')
  eq(tg, { value: 200, owner: true })
  const p = core.statProgress(50, tg)
  assert.equal(p.pct, 25)
  assert.equal(p.width, 25)
  assert.equal(core.statProgress(500, tg).width, 100)
  assert.equal(core.statProgress(500, tg).pct, 250)
  assert.equal(core.statProgress(null, tg), null)
  const tc = tileOf('stat', { column: 'v', target: { column: 'goal', label: null } })
  eq(core.statTarget(tc, [{ v: 1, goal: 40 }], null, null, '2026-11-04', 'en-US'), { value: 40, owner: false })
})

test('line and bar charts with a target get one dashed ink annotation; time charts with a target column a dashed ink line', () => {
  const tile = { id: 'l', type: 'line', title: 'L', x: 'k', y: ys('v'), format: 'integer', target: { value: 25, label: null }, measures: [] }
  const model = core.fixedModel(tile, ROWS, 'en-US')
  const cfg = core.fixedChartConfig(tile, model, 'dark', 'en-US')
  const a = cfg.options.plugins.annotation.annotations.target
  assert.equal(a.borderColor, TH.dark.ink)
  assert.equal(a.label.content, 'Target set by owner')
  assert.equal(Object.keys(cfg.options.plugins.annotation.annotations).length, 1)
  const tile2 = { ...tile, type: 'bar', target: { column: 'w', label: null } }
  const model2 = core.fixedModel(tile2, ROWS, 'en-US')
  const cfg2 = core.fixedChartConfig(tile2, model2, 'light', 'en-US')
  const tg = cfg2.data.datasets[cfg2.data.datasets.length - 1]
  assert.equal(tg.label, 'Target')
  assert.equal(tg.borderColor, TH.light.ink)
  eq(tg.data, [4, 6, 5])
  assert.equal(cfg2.options.plugins.annotation, undefined)
})

// ---------------------------------------------------------------- review fix: C-07 Other row, ordered tiles

test('fixed table: the compiled Other row carries no icValue and stays last', () => {
  const meta = { column: 'v', kind: 'sum', label: 'v', description: null, parts: null, formula: null, date_basis: null }
  const col = (c: string) => ({ column: c, label: null, format: null, currency: null, unit: null, owed_when: null,
    delta: false, good_direction: 'none', bar: false, total: null, due: null })
  const tile: any = { type: 'table', format: 'integer', columns: [col('k'), col('v')], max_rows: 25, totals: false,
    measures: [meta], by_columns: ['k'], sort: null, other_row: true, other_top: 2 }
  const rows = [{ k: 'A', v: 10 }, { k: 'B', v: 5 }, { k: 'Other', v: 40 }]
  const m = core.tableModel(tile, { rows }, { locale: 'en-US', sort: { column: 'v', dir: 'desc' } }, {})
  eq(m.rows.map((r: any) => r.cells[0].raw), ['A', 'B', 'Other'])
  assert.equal(m.rows[2].cells[1].ic, false)
  assert.equal(m.rows[0].cells[1].ic, true)
  const off = core.tableModel({ ...tile, other_row: false }, { rows }, { locale: 'en-US' }, {})
  assert.equal(off.rows[2].cells[1].ic, true)
})

test('fixed ranked bar with a compiled Other row: Other stays last, neutral, not re-ranked or valued', () => {
  const t = tileOf('ranked_bar', { top: 2, other: false, other_row: true, other_top: 2 })
  const rows = [{ k: 'a', v: 10 }, { k: 'b', v: 5 }, { k: 'Other', v: 40 }]
  const view = core.vizView(t, V(t, rows), 'en-US')
  eq(view.labels, ['a', 'b', 'Other'])
  assert.equal(view.cells[2].other, true)
  const pm = core.vizPseudo(t, view)
  assert.equal(pm.cells[2].other, true)
  const cfg = core.rankedConfig(t, V(t, rows), 'dark', 'en-US')
  assert.equal(cfg.data.datasets[0].backgroundColor[2], TH.dark.neutral)
})

test('fixed donut with a compiled Other row and 5 parts does not fold a second Other', () => {
  const t = tileOf('donut', { top: 4, other: true, other_row: true, other_top: 4 })
  const rows = [{ k: 'a', v: 10 }, { k: 'b', v: 9 }, { k: 'c', v: 8 }, { k: 'd', v: 7 }, { k: 'Other', v: 40 }]
  const view = core.vizView(t, V(t, rows), 'en-US')
  eq(view.labels, ['a', 'b', 'c', 'd', 'Other'])
  assert.equal(view.cells.filter((c: any) => c.other).length, 1)
})

test('controlled aging and funnel keep the returned bucket order; ranked_bar still ranks', () => {
  const meta = { column: 'v', kind: 'sum', label: 'v', description: null, parts: null, formula: null, date_basis: null }
  const range = { from: '2026-10-29', to: '2026-11-04' }
  const buckets = ['0-30', '31-60', '61-90', '90+']
  const sizes = [1, 9, 3, 5]
  const rows = buckets.map((b, i) => ({ metric_time__day: '2026-11-01', k: b, v: sizes[i] }))
  for (const type of ['aging', 'funnel']) {
    const t = tileOf(type, { x: 'k', measures: [meta], by_columns: ['k'] })
    const d = core.vizData(t, rows, range, { dayCol: 'metric_time__day' }, 'en-US')
    eq(d.labels, buckets, type)
  }
  const rb = tileOf('ranked_bar', { x: 'k', measures: [meta], by_columns: ['k'] })
  eq(core.vizData(rb, rows, range, { dayCol: 'metric_time__day' }, 'en-US').labels, ['31-60', '90+', '61-90', '0-30'])
})

test('fewer members than top: a real last member is not taken for the compiled Other row', () => {
  const meta = { column: 'v', kind: 'sum', label: 'v', description: null, parts: null, formula: null, date_basis: null }
  const col = (c: string) => ({ column: c, label: null, format: null, currency: null, unit: null, owed_when: null,
    delta: false, good_direction: 'none', bar: false, total: null, due: null })
  const tt: any = { type: 'table', format: 'integer', columns: [col('k'), col('v')], max_rows: 25, totals: false,
    measures: [meta], by_columns: ['k'], sort: null, other_row: true, other_top: 8 }
  const rows = [{ k: 'C', v: 9 }, { k: 'A', v: 5 }, { k: 'B', v: 2 }]
  const m = core.tableModel(tt, { rows }, { locale: 'en-US', sort: { column: 'k', dir: 'asc' } }, {})
  eq(m.rows.map((r: any) => r.cells[0].raw), ['A', 'B', 'C'])
  assert.ok(m.rows.every((r: any) => r.cells[1].ic === true))
  for (const type of ['ranked_bar', 'donut']) {
    const t = tileOf(type, { top: 8, other: true, other_row: true, other_top: 8 })
    const rs = [{ k: 'a', v: 10 }, { k: 'b', v: 5 }, { k: 'c', v: 2 }]
    const view = core.vizView(t, V(t, rs), 'en-US')
    eq(view.labels, ['a', 'b', 'c'], type)
    assert.equal(view.cells.filter((c: any) => c.other).length, 0, type)
  }
  // top + 1 rows whose last is not named Other are all members too
  const t9 = tileOf('ranked_bar', { top: 2, other: false, other_row: true, other_top: 2 })
  const v9 = core.vizView(t9, V(t9, [{ k: 'a', v: 10 }, { k: 'b', v: 5 }, { k: 'c', v: 2 }]), 'en-US')
  assert.equal(v9.cells.filter((c: any) => c.other).length, 0)
})

test('fixed bar with a compiled Other row: Other is flagged, neutral, and has no icValue', () => {
  const meta = { column: 'v', kind: 'sum', label: 'v', description: null, parts: null, formula: null, date_basis: null }
  const t = tileOf('bar', { measures: [meta], other_row: true, other_top: 2 })
  const rows = [{ k: 'a', v: 10 }, { k: 'b', v: 5 }, { k: 'Other', v: 40 }]
  const m = core.fixedModel(t, rows, 'en-US')
  assert.equal(m.cells[2].other, true)
  assert.equal(m.cells[2].key, null)
  assert.equal(!!m.cells[0].other, false)
  const cfg = core.fixedChartConfig(t, m, 'dark', 'en-US')
  eq(cfg.data.datasets[0].backgroundColor, [TH.dark.inward, TH.dark.inward, TH.dark.neutral])
  const few = core.fixedModel(t, rows.slice(0, 2), 'en-US')
  assert.equal(few.cells.filter((c: any) => c.other).length, 0)
})

test('controlled aging and funnel keep a stable order when the oldest day holds fewer buckets', () => {
  const meta = { column: 'v', kind: 'sum', label: 'v', description: null, parts: null, formula: null, date_basis: null }
  const range = { from: '2026-10-29', to: '2026-11-04' }
  const rows = [
    { metric_time__day: '2026-10-01', k: '61-90', v: 1 },
    { metric_time__day: '2026-11-01', k: '0-30', v: 5 },
    { metric_time__day: '2026-11-01', k: '31-60', v: 2 },
    { metric_time__day: '2026-11-01', k: '61-90', v: 3 },
    { metric_time__day: '2026-11-01', k: '90+', v: 4 },
    { metric_time__day: '2026-11-02', k: '0-30', v: 1 },
  ]
  for (const type of ['aging', 'funnel']) {
    const t = tileOf(type, { x: 'k', measures: [meta], by_columns: ['k'] })
    const d = core.vizData(t, rows, range, { dayCol: 'metric_time__day' }, 'en-US')
    eq(d.labels, ['0-30', '31-60', '61-90', '90+'], type)
  }
})

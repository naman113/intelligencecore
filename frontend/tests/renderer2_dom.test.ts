// Phase 22 (22-07, DCAP-01..06, DCAP-16): DOM behaviour of ic-render-2.0.0 under a fake browser.
import test from 'node:test'
import assert from 'node:assert/strict'
import { findAll, findTag, makeEnv, text, tile, type FakeEl } from './fakedom'

const TODAY = '2026-11-04'
const PRESETS = [
  'today', 'yesterday', 'last_7_days', 'last_30_days', 'last_90_days',
  'this_month', 'last_month', 'this_quarter', 'this_fy',
]

// ---------------------------------------------------------------- fixtures

function addDays(iso: string, n: number): string {
  const d = new Date(Date.parse(iso + 'T00:00:00Z') + n * 86400000)
  return d.toISOString().slice(0, 10)
}

function days(from: string, to: string): string[] {
  const out: string[] = []
  for (let d = from; d <= to; d = addDays(d, 1)) out.push(d)
  return out
}

// value of the day: day of month, plus one in 2025 (so last year differs from this year)
function qtyOf(d: string): number {
  return +d.slice(8, 10) + (d.startsWith('2025') ? 1 : 0)
}

const ALL_DAYS = days('2025-10-01', TODAY) // exactly 400 days
const QTY_ROWS = ALL_DAYS.map((d) => ({ metric_time__day: d, qty: qtyOf(d) }))
const CUSTS_ROWS = ALL_DAYS.map((d) => ({ metric_time__day: d, custs: 3 }))
const MATS = ['A', 'B', 'C', 'D', 'E', 'F']
// member weight: A 6 ... F 1, so the range order is A, B, C, D, E, F
const SERIES_ROWS = ALL_DAYS.flatMap((d) =>
  MATS.map((m, i) => ({ metric_time__day: d, material: m, qty: (6 - i) * 10 })),
)

function snap(rows: any[], extra: any = {}) {
  return {
    query_id: 'q',
    columns: rows.length ? Object.keys(rows[0]) : [],
    rows,
    refreshed_at: '2026-11-04T09:50:00+00:00',
    data_as_of: '2026-11-04T09:50:00+00:00',
    timezone: 'UTC',
    error: null,
    ...extra,
  }
}

const SUM_QTY = { column: 'qty', label: 'Quantity', description: 'Rolls weighed', kind: 'sum', parts: null, formula: null, date_basis: 'weigh date' }

function base(id: string, type: string, extra: any = {}) {
  return {
    id, type, title: 'Title ' + id, note: null, qid: null, grain: null, format: 'integer', currency: null,
    unit: null, width: 'quarter', follow: 'control', follow_filters: false, horizon_days: 400,
    presets_qid: null, target: null, measures: [], by_columns: [], ...extra,
  }
}

function makeSpec(over: any = {}) {
  const tiles = [
    base('s_qty', 'stat', { qid: 'm_qty', column: 'qty', measures: [SUM_QTY], comparison: 'previous_period', good_direction: 'up' }),
    base('s_both', 'stat', { qid: 'm_qty', column: 'qty', measures: [{ ...SUM_QTY, label: null, description: null }], comparison: 'both' }),
    base('s_custs', 'stat', {
      qid: 'm_custs', presets_qid: 'm_custs_p', column: 'custs', comparison: 'previous_period',
      measures: [{ column: 'custs', label: 'Customers', description: null, kind: 'distinct', parts: null, formula: null, date_basis: null }],
    }),
    base('s_fixed', 'stat', {
      qid: 'f_stat', follow: 'fixed', horizon_days: null, column: 'total', x: 'month', grain: 'month',
      compare: 'previous_row', compare_label: 'vs last month', good_direction: 'up', owed_when: { op: '>', value: 100 },
    }),
    base('s_now', 'stat', { qid: 'n_stat', follow: 'now', horizon_days: null, column: 'open', compare: 'none' }),
    base('l_qty', 'line', {
      qid: 'm_qty', width: 'half', x: 'metric_time__day', y: [{ column: 'qty', label: 'Qty', dashed: false, tone: null, as: null }],
      measures: [SUM_QTY], ghost: 'previous_period',
    }),
    base('l_series', 'stacked_bar', {
      qid: 'm_series', width: 'half', x: 'metric_time__day', series: 'material', by_columns: ['material'],
      y: [{ column: 'qty', label: 'Qty', dashed: false, tone: null, as: null }], measures: [SUM_QTY],
    }),
    base('p_qty', 'stacked_bar', {
      qid: 'm_series', width: 'half', x: 'metric_time__day', series: 'material', by_columns: ['material'], percent: true,
      y: [{ column: 'qty', label: 'Qty', dashed: false, tone: null, as: null }], measures: [SUM_QTY],
    }),
    base('b_fixed', 'bar', {
      qid: 'f_bar', follow: 'fixed', horizon_days: null, width: 'half', x: 'month', grain: 'month', series: 'region',
      y: [{ column: 'weight', label: 'Weight', dashed: false, tone: null, as: null }],
    }),
    base('sp', 'sparkline', {
      qid: 'm_qty', x: 'metric_time__day', y: [{ column: 'qty', label: 'Qty', dashed: false, tone: null, as: null }], measures: [SUM_QTY],
    }),
    base('note', 'text', { follow: 'fixed', horizon_days: null, width: 'full', title: null, text: 'Line one\nLine two <b>x</b>' }),
  ]
  const spec: any = {
    render_version: 2, title: 'Ops <dashboard>', subtitle: 'Daily operations', number_locale: 'en-US', timezone: 'UTC',
    date_control: { default: 'last_7_days', presets: PRESETS, horizon_days: 400, fy_start_month: 4 },
    filters: [],
    sections: [
      { title: 'At a glance', description: 'The numbers', tiles: ['s_qty', 's_both', 's_custs', 's_fixed', 's_now', 'note'] },
      { title: 'Trends', description: null, tiles: ['l_qty', 'l_series', 'p_qty', 'b_fixed', 'sp'] },
    ],
    tiles,
    refresh: { m_qty: 15, m_custs: 15, m_custs_p: 15, f_stat: 30, n_stat: 5, m_series: 15, f_bar: 15 },
    ...over,
  }
  return spec
}

function makeData(over: any = {}) {
  return {
    m_qty: snap(QTY_ROWS),
    m_custs: snap(CUSTS_ROWS),
    m_custs_p: snap([
      { preset: 'last_7_days', period: 'current', value: 12, today: TODAY },
      { preset: 'last_7_days', period: 'previous', value: 10, today: TODAY },
      { preset: 'last_30_days', period: 'current', value: 40, today: '2026-11-03' },
      { preset: 'last_30_days', period: 'previous', value: 30, today: '2026-11-03' },
    ]),
    f_stat: snap([{ month: '2026-09-01', total: 80 }, { month: '2026-10-01', total: 120 }]),
    n_stat: snap([{ open: 7 }]),
    m_series: snap(SERIES_ROWS),
    f_bar: snap([
      { month: '2026-09-01', region: 'North', weight: 5 },
      { month: '2026-09-01', region: 'South', weight: 7 },
      { month: '2026-10-01', region: 'North', weight: 6 },
      { month: '2026-10-01', region: 'South', weight: 8 },
    ]),
    ...over,
  }
}

async function open(over: { spec?: any; data?: any; [k: string]: any } = {}) {
  const { spec, data, ...rest } = over
  const env = makeEnv({ spec: spec ?? makeSpec(), data: data ?? makeData(), ...rest })
  await env.flush()
  return env
}

function statValue(env: ReturnType<typeof makeEnv>, id: string): FakeEl {
  return findAll(tile(env.root, id), 'ic-stat-value')[0]
}

function core(env: ReturnType<typeof makeEnv>): any {
  return (env.context as any).ICRenderCore
}

function chartOf(env: ReturnType<typeof makeEnv>, id: string): any {
  const t = tile(env.root, id)
  const canvas = findTag(t, 'canvas')[0]
  assert.ok(canvas, 'canvas in ' + id)
  return env.charts.filter((c) => c.canvas === canvas && !c.destroyed)[0]
}

// ---------------------------------------------------------------- loading and shell

test('loading fetches ./spec once and ./data/<qid> once per distinct qid, nothing else', async () => {
  const env = await open()
  const spec = env.fetches.filter((u) => u.startsWith('./spec'))
  assert.deepEqual(spec, ['./spec?t=x'])
  const want = ['m_qty', 'm_custs', 'm_custs_p', 'f_stat', 'n_stat', 'm_series', 'f_bar'].map((q) => `./data/${q}?t=x`)
  assert.deepEqual([...env.fetches.filter((u) => u.startsWith('./data/'))].sort(), [...want].sort())
  assert.equal(env.fetches.length, 1 + want.length)
})

test('first-section data is requested before the rest', async () => {
  const env = await open()
  const data = env.fetches.filter((u) => u.startsWith('./data/'))
  const lastFirst = Math.max(...['m_qty', 'm_custs', 'm_custs_p', 'f_stat', 'n_stat'].map((q) => data.indexOf(`./data/${q}?t=x`)))
  const firstRest = Math.min(...['m_series', 'f_bar'].map((q) => data.indexOf(`./data/${q}?t=x`)))
  assert.ok(lastFirst < firstRest)
})

test('header: title, subtitle, as-of from the newest data_as_of, control bar', async () => {
  const env = await open()
  const head = findTag(env.root, 'header')[0]
  assert.equal(text(findAll(head, 'ic-title')[0]), 'Ops <dashboard>')
  assert.equal(text(findAll(head, 'ic-sub')[0]), 'Daily operations')
  const want = 'Data as of ' + core(env).formatAsOf('2026-11-04T09:50:00+00:00', 'en-US', 'UTC')
  assert.equal(text(findAll(head, 'ic-asof')[0]), want)
  assert.equal(findAll(head, 'ic-bar').length, 1)
  assert.equal(env.document.title, 'Ops <dashboard>')
})

test('sections, grid and widths follow the render form in order', async () => {
  const env = await open()
  const sections = findAll(env.root, 'ic-section')
  assert.equal(sections.length, 2)
  assert.equal(text(findAll(sections[0], 'ic-section-title')[0]), 'At a glance')
  assert.equal(text(findAll(sections[0], 'ic-section-desc')[0]), 'The numbers')
  assert.equal(findAll(sections[1], 'ic-section-desc').length, 0)
  const grid = findAll(sections[0], 'ic-grid')[0]
  assert.deepEqual(grid.children.map((t) => t.dataset.icTile), ['s_qty', 's_both', 's_custs', 's_fixed', 's_now', 'note'])
  assert.ok(grid.children[0].classList.contains('ic-w-quarter'))
  assert.ok(grid.children[5].classList.contains('ic-w-full'))
  const grid2 = findAll(sections[1], 'ic-grid')[0]
  assert.ok(grid2.children[0].classList.contains('ic-w-half'))
})

test('strings from the spec stay text: no markup is built', async () => {
  const env = await open()
  assert.equal(text(findAll(tile(env.root, 'note'), 'ic-text')[0]), 'Line one\nLine two <b>x</b>')
  const tags = new Set<string>()
  const walk = (e: FakeEl) => {
    tags.add(e.tag)
    e.children.forEach(walk)
  }
  walk(env.root)
  assert.ok(!tags.has('b') && !tags.has('script'))
})

test('spec fetch failure says Dashboard unavailable with the status', async () => {
  const env = makeEnv({ spec: {}, data: {}, specStatus: 404 })
  await env.flush()
  assert.equal(text(findAll(env.root, 'ic-fail')[0]), 'Dashboard unavailable (HTTP 404)')
})

// ---------------------------------------------------------------- date control

test('date button shows the preset and its dates; sheet lists the 9 presets and the custom inputs', async () => {
  const env = await open()
  const btn = findAll(env.root, 'ic-range')[0]
  assert.equal(text(btn), 'Last 7 days · Oct 29 – Nov 4')
  assert.equal(btn.ariaHasPopup, 'dialog')
  btn.click()
  const sheet = findAll(env.root, 'ic-sheet')[0]
  assert.equal(sheet.role, 'dialog')
  assert.equal(sheet.ariaModal, 'true')
  const presets = findAll(sheet, 'ic-preset')
  assert.equal(presets.length, 9)
  const on = presets.filter((p) => p.classList.contains('ic-on'))
  assert.deepEqual(on.map((p) => text(p)), ['Last 7 days'])
  assert.equal(on[0].ariaPressed, 'true')
  assert.equal(presets[0].ariaPressed, 'false')
  const inputs = findAll(sheet, 'ic-date')
  assert.equal(inputs.length, 2)
  assert.equal(inputs[0].type, 'date')
  assert.equal(inputs[0].min, '2025-10-01')
  assert.equal(inputs[0].max, TODAY)
  assert.equal(findAll(sheet, 'ic-apply').length, 1)
  findAll(sheet, 'ic-close')[0].click()
  assert.equal(findAll(env.root, 'ic-sheet').length, 0)
})

test('choosing a preset closes the sheet, updates the button, recomputes tiles, fetches nothing', async () => {
  const env = await open()
  const before = env.fetches.length
  findAll(env.root, 'ic-range')[0].click()
  findAll(findAll(env.root, 'ic-sheet')[0], 'ic-preset').find((p) => text(p) === 'This month')!.click()
  assert.equal(findAll(env.root, 'ic-sheet').length, 0)
  assert.equal(text(findAll(env.root, 'ic-range')[0]), 'This month · Nov 1 – Nov 4')
  assert.equal(text(statValue(env, 's_qty')), '10')
  assert.equal(statValue(env, 's_qty').dataset.icRow, '2026-11-01..2026-11-04')
  assert.equal(env.fetches.length, before)
})

test('a valid custom range applies; an invalid one changes nothing and says why', async () => {
  const env = await open()
  findAll(env.root, 'ic-range')[0].click()
  let sheet = findAll(env.root, 'ic-sheet')[0]
  let [from, to] = findAll(sheet, 'ic-date')
  from.value = '2026-10-10'
  to.value = '2026-10-01'
  findAll(sheet, 'ic-apply')[0].click()
  assert.equal(text(findAll(sheet, 'ic-na')[0]), 'Pick dates inside the stored period')
  assert.equal(text(findAll(env.root, 'ic-range')[0]).startsWith('Last 7 days'), true)
  assert.equal(text(statValue(env, 's_qty')), '100')
  from.value = '2024-01-01'
  to.value = '2026-10-10'
  findAll(sheet, 'ic-apply')[0].click()
  assert.equal(findAll(sheet, 'ic-na').length, 1)
  from.value = '2026-10-01'
  to.value = '2026-10-10'
  findAll(sheet, 'ic-apply')[0].click()
  assert.equal(findAll(env.root, 'ic-sheet').length, 0)
  assert.equal(text(findAll(env.root, 'ic-range')[0]), 'Custom range · Oct 1 – Oct 10')
  assert.equal(text(statValue(env, 's_qty')), '55')
  sheet = null as any
})

test('the initial range comes from a valid hash, else the default', async () => {
  const a = await open({ hash: '#r=last_30_days' })
  assert.ok(text(findAll(a.root, 'ic-range')[0]).startsWith('Last 30 days'))
  const b = await open({ hash: '#r=2026-10-01..2026-10-10' })
  assert.equal(text(statValue(b, 's_qty')), '55')
  const c = await open({ hash: '#r=nonsense' })
  assert.ok(text(findAll(c.root, 'ic-range')[0]).startsWith('Last 7 days'))
})

test('a hash range outside the horizon or after today is ignored (no millions of days)', async () => {
  const a = await open({ hash: '#r=0100-01-01..9999-12-31' })
  assert.ok(text(findAll(a.root, 'ic-range')[0]).startsWith('Last 7 days'))
  const b = await open({ hash: '#r=2026-10-01..2026-12-31' })
  assert.ok(text(findAll(b.root, 'ic-range')[0]).startsWith('Last 7 days'))
})

test('a refreshed controlled tile with no rows shows 0, not No rows', async () => {
  const env = await open({ data: makeData({ m_qty: snap([]) }) })
  const t = tile(env.root, 's_qty')
  assert.equal(findAll(t, 'ic-empty').length, 0)
  assert.equal(text(statValue(env, 's_qty')), '0')
})

// ---------------------------------------------------------------- stats

test('controlled stat: value, raw dataset, period, delta against the previous 7 days', async () => {
  const env = await open()
  const v = statValue(env, 's_qty')
  assert.equal(text(v), '100')
  assert.equal(v.dataset.icValue, '100')
  assert.equal(v.dataset.icCol, 'qty')
  assert.equal(v.dataset.icRow, '2026-10-29..2026-11-04')
  const t = tile(env.root, 's_qty')
  assert.equal(text(findAll(t, 'ic-stat-period')[0]), 'Last 7 days')
  const d = findAll(t, 'ic-delta')
  assert.equal(d.length, 1)
  assert.equal(text(d[0]), '▼ -42.9% vs previous 7 days')
  assert.ok(d[0].classList.contains('ic-bad'))
  assert.equal(d[0].dataset.icValue, undefined)
})

test('comparison both shows previous period and same days last year', async () => {
  const env = await open()
  const d = findAll(tile(env.root, 's_both'), 'ic-delta').map((e) => text(e))
  assert.deepEqual(d, ['▼ -42.9% vs previous 7 days', '▼ -6.5% vs same days last year'])
})

test('a comparison outside the stored horizon says not available for this range', async () => {
  const env = await open({ hash: '#r=2025-10-01..2025-10-07' })
  const t = tile(env.root, 's_qty')
  assert.equal(text(statValue(env, 's_qty')), String(1 + 2 + 3 + 4 + 5 + 6 + 7 + 7))
  assert.equal(findAll(t, 'ic-delta').length, 0)
  assert.equal(text(findAll(t, 'ic-na')[0]), 'Not available for this range')
})

test('distinct stat: preset reads the presets query, custom range is not available, stale presets say no data yet', async () => {
  const env = await open()
  const t = tile(env.root, 's_custs')
  assert.equal(text(statValue(env, 's_custs')), '12')
  assert.equal(text(findAll(t, 'ic-delta')[0]), '▲ +20.0% vs previous 7 days')
  const set = (preset: string) => {
    findAll(env.root, 'ic-range')[0].click()
    findAll(findAll(env.root, 'ic-sheet')[0], 'ic-preset').find((p) => text(p) === preset)!.click()
  }
  set('Last 30 days') // presets rows were computed on 2026-11-03
  assert.equal(text(findAll(t, 'ic-na')[0]), 'No data yet')
  assert.equal(findAll(t, 'ic-stat-value').length, 0)
  findAll(env.root, 'ic-range')[0].click()
  const sheet = findAll(env.root, 'ic-sheet')[0]
  const [from, to] = findAll(sheet, 'ic-date')
  from.value = '2026-10-01'
  to.value = '2026-10-10'
  findAll(sheet, 'ic-apply')[0].click()
  assert.equal(text(findAll(t, 'ic-na')[0]), 'Not available for a custom range')
  set('Yesterday') // one day: the daily row
  assert.equal(text(statValue(env, 's_custs')), '3')
})

test('fixed stat behaves as v1 with the owed badge and compare label', async () => {
  const env = await open()
  const t = tile(env.root, 's_fixed')
  const v = findAll(t, 'ic-stat-value')[0]
  assert.equal(text(v), '120')
  assert.ok(v.classList.contains('ic-owed'))
  assert.equal(v.dataset.icValue, '120')
  assert.equal(v.dataset.icCol, 'total')
  assert.equal(v.dataset.icRow, '2026-10-01')
  assert.equal(text(findAll(t, 'ic-owed-badge')[0]), 'Owed')
  assert.equal(text(findAll(t, 'ic-stat-period')[0]), 'Oct 2026')
  assert.equal(text(findAll(t, 'ic-delta')[0]), '▲ +50.0% vs last month')
})

test('follow now tiles say now and ignore the range; fixed tiles say Fixed period only off the default', async () => {
  const env = await open()
  assert.equal(text(findAll(tile(env.root, 's_now'), 'ic-now')[0]), 'now')
  assert.equal(findAll(tile(env.root, 's_fixed'), 'ic-fixed').length, 0)
  findAll(env.root, 'ic-range')[0].click()
  findAll(findAll(env.root, 'ic-sheet')[0], 'ic-preset').find((p) => text(p) === 'Yesterday')!.click()
  assert.equal(text(findAll(tile(env.root, 's_fixed'), 'ic-fixed')[0]), 'Fixed period')
  assert.equal(findAll(tile(env.root, 's_now'), 'ic-fixed').length, 0)
  assert.equal(text(statValue(env, 's_now')), '7')
  assert.equal(text(statValue(env, 's_fixed')), '120')
  findAll(env.root, 'ic-range')[0].click()
  findAll(findAll(env.root, 'ic-sheet')[0], 'ic-preset').find((p) => text(p) === 'Last 7 days')!.click()
  assert.equal(findAll(tile(env.root, 's_fixed'), 'ic-fixed').length, 0)
})

test('the info button toggles the definition panel', async () => {
  const env = await open()
  const t = tile(env.root, 's_qty')
  const info = findAll(t, 'ic-info')[0]
  assert.equal(info.ariaLabel, 'What is this number?')
  assert.equal(findAll(t, 'ic-def').length, 0)
  info.click()
  assert.deepEqual(findAll(t, 'ic-def').map((e) => text(e)), ['Quantity: Rolls weighed'])
  assert.equal(info.ariaExpanded, 'true')
  info.click()
  assert.equal(findAll(t, 'ic-def').length, 0)
  assert.equal(findAll(tile(env.root, 's_both'), 'ic-info').length, 0)
})

// ---------------------------------------------------------------- charts

test('charts are created only after their tile is reported near, and destroyed when far', async () => {
  const env = await open()
  assert.equal(env.charts.length, 0)
  const t = tile(env.root, 'l_qty')
  env.intersect(0, t, true)
  assert.equal(env.liveCharts().length, 1)
  assert.equal(env.charts.length, 1)
  env.intersect(1, t, false)
  assert.equal(env.liveCharts().length, 0)
  assert.equal(env.charts[0].destroyed, true)
  assert.equal(findAll(t, 'ic-sr').length, 0)
  env.intersect(1, t, true)
  assert.equal(env.liveCharts().length, 1)
  assert.equal(findAll(t, 'ic-sr').length, 1)
  assert.equal(env.charts.length, 2)
})

test('performance.mark ic-first-screen fires once, after every tile of the first section is drawn', async () => {
  const env = makeEnv({ spec: makeSpec(), data: makeData() })
  assert.deepEqual(env.marks, [])
  await env.flush()
  assert.deepEqual(env.marks, ['ic-first-screen'])
  // later redraws do not mark again
  findAll(env.root, 'ic-range')[0].click()
  findAll(findAll(env.root, 'ic-sheet')[0], 'ic-preset')[0].click()
  assert.deepEqual(env.marks, ['ic-first-screen'])
})

test('the mark waits for a first-section chart that has not been drawn yet', async () => {
  const spec = makeSpec()
  spec.sections = [{ title: 'Charts', description: null, tiles: ['l_qty'] }]
  const env = await open({ spec })
  assert.deepEqual(env.marks, [])
  env.showAll()
  assert.deepEqual(env.marks, ['ic-first-screen'])
})

test('line chart: day buckets, tone colour, ghost series at 40% dashed, formatted tooltip', async () => {
  const env = await open()
  env.showAll()
  const cfg = chartOf(env, 'l_qty').config
  assert.equal(cfg.type, 'line')
  assert.deepEqual(Array.from(cfg.data.labels), ['Oct 29', 'Oct 30', 'Oct 31', 'Nov 1', 'Nov 2', 'Nov 3', 'Nov 4'])
  const [cur, ghost] = cfg.data.datasets
  assert.deepEqual(Array.from(cur.data), [29, 30, 31, 1, 2, 3, 4])
  assert.equal(cur.borderColor, core(env).THEMES.dark.inward)
  assert.equal(cur.borderWidth, 2)
  assert.equal(cur.pointRadius, 0)
  assert.equal(ghost.label, 'Previous period')
  assert.deepEqual(Array.from(ghost.data), [22, 23, 24, 25, 26, 27, 28])
  assert.deepEqual(Array.from(ghost.borderDash), [6, 4])
  assert.match(ghost.borderColor, /^rgba\(57,135,229,0\.4\)$/)
  assert.equal(cfg.options.interaction.mode, 'index')
  assert.equal(cfg.options.plugins.legend.display, true)
  const label = cfg.options.plugins.tooltip.callbacks.label({ datasetIndex: 0, dataIndex: 0, parsed: { y: 29 } })
  assert.equal(label, 'Qty: 29')
  assert.equal(cfg.options.plugins.tooltip.callbacks.title([{ dataIndex: 6 }]), 'Wed, Nov 4, 2026')
  assert.equal(cfg.options.plugins.tooltip.callbacks.afterBody([{ dataIndex: 0 }]), 'vs previous: +31.8%')
})

test('a ghost that starts before the horizon is not drawn and the tile says so', async () => {
  const env = await open({ hash: '#r=2025-10-01..2025-10-07' })
  env.showAll()
  const cfg = chartOf(env, 'l_qty').config
  assert.equal(cfg.data.datasets.length, 1)
  assert.equal(cfg.options.plugins.legend.display, false)
  assert.equal(text(findAll(tile(env.root, 'l_qty'), 'ic-na')[0]), 'Comparison not available for this range')
})

test('one series hides the legend; sparkline tiles draw without axes and legend', async () => {
  const env = await open()
  env.showAll()
  const sp = chartOf(env, 'sp').config
  assert.equal(sp.options.scales.x.display, false)
  assert.equal(sp.options.scales.y.display, false)
  assert.equal(sp.options.plugins.legend.display, false)
  assert.equal(sp.data.datasets.length, 1)
})

test('series split: four coloured members by range value, the rest folded into a neutral Other', async () => {
  const env = await open()
  env.showAll()
  const cfg = chartOf(env, 'l_series').config
  const th = core(env).THEMES.dark
  assert.deepEqual(Array.from(cfg.data.datasets).map((d: any) => d.label), ['A', 'B', 'C', 'D', 'Other'])
  assert.deepEqual(
    Array.from(cfg.data.datasets).map((d: any) => d.backgroundColor),
    [th.inward, th.outward, th.accent, th.neutral, th.neutral],
  )
  assert.deepEqual(Array.from(cfg.data.datasets[4].data), [30, 30, 30, 30, 30, 30, 30]) // E 20 + F 10
  assert.equal(cfg.data.datasets[0].stack, 'current')
  assert.equal(cfg.data.datasets[0].borderRadius, 3)
  assert.equal(cfg.data.datasets[0].borderSkipped, 'start')
  assert.equal(cfg.options.scales.y.beginAtZero, true)
  const label = cfg.options.plugins.tooltip.callbacks.label({ datasetIndex: 0, dataIndex: 0, parsed: { y: 60 } })
  assert.equal(label, 'A: 60 (29% of total)')
})

test('series cells carry icSeries, icRow per C-11; Other cells carry no icValue', async () => {
  const env = await open()
  env.showAll()
  const sr = findAll(tile(env.root, 'l_series'), 'ic-sr')[0]
  const rows = findTag(sr, 'tbody')[0].children
  assert.equal(rows.length, 7)
  const cells = rows[0].children.filter((c) => c.tag === 'td')
  assert.equal(cells[0].dataset.icSeries, 'A')
  assert.equal(cells[0].dataset.icValue, '60')
  assert.equal(cells[0].dataset.icCol, 'qty')
  assert.equal(cells[0].dataset.icRow, '2026-10-29')
  assert.equal(cells[4].dataset.icValue, undefined)
  assert.equal(cells[4].dataset.icSeries, undefined)
})

test('stacked percent normalises each bucket to 100, keeps raw values in the table', async () => {
  const env = await open()
  env.showAll()
  const cfg = chartOf(env, 'p_qty').config
  assert.equal(cfg.options.scales.y.max, 100)
  assert.equal(cfg.options.scales.y.ticks.callback(50), '50%')
  const sum = cfg.data.datasets.reduce((t: number, d: any) => t + d.data[0], 0)
  assert.ok(Math.abs(sum - 100) < 1e-9)
  const sr = findAll(tile(env.root, 'p_qty'), 'ic-sr')[0]
  const first = findTag(sr, 'tbody')[0].children[0].children.filter((c) => c.tag === 'td')[0]
  assert.equal(first.dataset.icValue, '60')
})

test('fixed pivoted bar: x labels, series colours, icRow = time key and icSeries per cell', async () => {
  const env = await open()
  env.showAll()
  const cfg = chartOf(env, 'b_fixed').config
  assert.deepEqual(Array.from(cfg.data.labels), ['Sep 2026', 'Oct 2026'])
  assert.deepEqual(Array.from(cfg.data.datasets).map((d: any) => d.label), ['North', 'South'])
  assert.deepEqual(Array.from(cfg.data.datasets[1].data), [7, 8])
  const sr = findAll(tile(env.root, 'b_fixed'), 'ic-sr')[0]
  const cell = findTag(sr, 'tbody')[0].children[1].children.filter((c) => c.tag === 'td')[1]
  assert.equal(cell.dataset.icValue, '8')
  assert.equal(cell.dataset.icRow, '2026-10-01')
  assert.equal(cell.dataset.icSeries, 'South')
  assert.equal(cell.dataset.icCol, 'weight')
})

test('every chart has role img, a summary label and a hidden data table', async () => {
  const env = await open()
  env.showAll()
  const t = tile(env.root, 'l_qty')
  const canvas = findTag(t, 'canvas')[0]
  assert.equal(canvas.role, 'img')
  assert.equal(canvas.ariaLabel, 'Title l_qty: 7 points, latest 4')
  const sr = findAll(t, 'ic-sr')[0]
  assert.equal(sr.tag, 'table')
  assert.equal(findTag(sr, 'tbody')[0].children.length, 7)
  const ghostCell = findTag(sr, 'tbody')[0].children[0].children.filter((c) => c.tag === 'td')[1]
  assert.equal(ghostCell.dataset.icValue, undefined)
})

// ---------------------------------------------------------------- tap a date and drill

function tapPoint(env: ReturnType<typeof makeEnv>, id: string, index: number) {
  env.showAll()
  const chart = chartOf(env, id)
  chart.hits = [{ index, datasetIndex: 0 }]
  chart.config.options.onClick({}, [], chart)
}

test('tapping a day focuses the page on that day; the chip clears it; nothing is fetched', async () => {
  const env = await open()
  const before = env.fetches.length
  tapPoint(env, 'l_qty', 1) // Oct 30
  assert.equal(text(findAll(env.root, 'ic-range')[0]), 'Custom range · Oct 30')
  const chip = findAll(env.root, 'ic-chip')[0]
  assert.ok(text(chip).startsWith('Showing Fri, Oct 30, 2026'))
  assert.equal(text(statValue(env, 's_qty')), '30')
  findAll(chip, 'ic-chip-x')[0].click()
  assert.equal(findAll(env.root, 'ic-chip').length, 0)
  assert.ok(text(findAll(env.root, 'ic-range')[0]).startsWith('Last 7 days'))
  assert.equal(text(statValue(env, 's_qty')), '100')
  assert.equal(env.fetches.length, before)
})

test('tapping a week bucket sets that week, a month bucket sets that month; clearing restores the first range', async () => {
  const env = await open()
  findAll(env.root, 'ic-range')[0].click()
  findAll(findAll(env.root, 'ic-sheet')[0], 'ic-preset').find((p) => text(p) === 'Last 90 days')!.click()
  env.showAll()
  let cfg = chartOf(env, 'l_qty').config
  assert.equal(cfg.data.labels.length, 14) // Aug 7 .. Nov 4 by week
  // week of Oct 26 (Monday) is the 13th bucket
  const idx = cfg.data.labels.indexOf('Oct 26')
  assert.ok(idx > 0)
  tapPoint(env, 'l_qty', idx)
  assert.equal(text(findAll(env.root, 'ic-range')[0]), 'Custom range · Oct 26 – Nov 1')
  assert.equal(text(statValue(env, 's_qty')), String(26 + 27 + 28 + 29 + 30 + 31 + 1))
  findAll(findAll(env.root, 'ic-chip')[0], 'ic-chip-x')[0].click()
  assert.ok(text(findAll(env.root, 'ic-range')[0]).startsWith('Last 90 days'))
  // this financial year is longer than 200 days: month buckets
  findAll(env.root, 'ic-range')[0].click()
  findAll(findAll(env.root, 'ic-sheet')[0], 'ic-preset').find((p) => text(p) === 'This financial year')!.click()
  env.showAll()
  cfg = chartOf(env, 'l_qty').config
  assert.deepEqual(Array.from(cfg.data.labels).slice(0, 2), ['Apr 2026', 'May 2026'])
  tapPoint(env, 'l_qty', 6) // Oct 2026
  assert.equal(text(findAll(env.root, 'ic-range')[0]), 'Custom range · Oct 1 – Oct 31')
  assert.ok(text(findAll(env.root, 'ic-chip')[0]).startsWith('Showing Oct 2026'))
  // a second tap keeps the very first range for the chip
  findAll(findAll(env.root, 'ic-chip')[0], 'ic-chip-x')[0].click()
  assert.ok(text(findAll(env.root, 'ic-range')[0]).startsWith('This financial year'))
})

test('on a phone the first tap only shows the tooltip; the second tap on the same point applies', async () => {
  const env = await open({ width: 400 })
  env.showAll()
  const chart = chartOf(env, 'l_qty')
  chart.hits = [{ index: 2, datasetIndex: 0 }]
  chart.config.options.onClick({}, [], chart)
  assert.equal(findAll(env.root, 'ic-chip').length, 0)
  const again = chartOf(env, 'l_qty')
  again.hits = [{ index: 2, datasetIndex: 0 }]
  again.config.options.onClick({}, [], again)
  assert.equal(findAll(env.root, 'ic-chip').length, 1)
})

// ---------------------------------------------------------------- states, freshness, theme

test('states: waiting, no rows, error with the last rows drawn, stale', async () => {
  const data = makeData({
    f_stat: snap([], { refreshed_at: null, data_as_of: null }),
    n_stat: snap([], {}),
    m_custs: snap(CUSTS_ROWS, { error: 'timeout' }),
    f_bar: snap([{ month: '2026-09-01', region: 'N', weight: 1 }], { refreshed_at: '2026-11-03T10:00:00+00:00' }),
  })
  const env = await open({ data })
  assert.equal(text(findAll(tile(env.root, 's_fixed'), 'ic-empty')[0]), 'Waiting for the first refresh')
  assert.equal(text(findAll(tile(env.root, 's_now'), 'ic-empty')[0]), 'No rows for this period')
  const t = tile(env.root, 's_custs')
  assert.equal(text(findAll(t, 'ic-error')[0]), 'Data feed failed: timeout')
  assert.equal(text(statValue(env, 's_custs')), '12')
  assert.equal(text(findAll(tile(env.root, 'b_fixed'), 'ic-stale')[0]), 'Data is stale')
  assert.ok(findAll(env.root, 'ic-asof')[0].classList.contains('ic-owed'))
})

test('a failing data request keeps nothing and says the feed failed with the status', async () => {
  const env = await open({ failData: ['f_stat'] })
  assert.equal(text(findAll(tile(env.root, 's_fixed'), 'ic-error')[0]), 'Data feed failed: could not load data (HTTP 404)')
})

test('per-tile as-of appears only when older than the header by more than 2 x refresh + 5 minutes', async () => {
  const data = makeData({ n_stat: snap([{ open: 7 }], { data_as_of: '2026-11-04T08:00:00+00:00' }) })
  const env = await open({ data })
  const asof = findAll(tile(env.root, 's_now'), 'ic-tile-asof')
  assert.equal(asof.length, 1)
  assert.equal(text(asof[0]), 'As of ' + core(env).formatAsOf('2026-11-04T08:00:00+00:00', 'en-US', 'UTC'))
  assert.equal(findAll(tile(env.root, 's_qty'), 'ic-tile-asof').length, 0)
})

test('each snapshot is re-fetched on a timer with a function argument', async () => {
  const env = await open()
  assert.equal(env.timers.length, 7)
  for (const t of env.timers) assert.equal(typeof t.fn, 'function')
  const ms = env.timers.map((t) => t.ms).sort((a, b) => a - b)
  assert.equal(ms[0], 5 * 60000)
  assert.equal(ms[ms.length - 1], 30 * 60000)
})

test('theme: light colours from the device, and a change redraws drawn charts with the other theme', async () => {
  const env = await open({ scheme: 'light' })
  env.showAll()
  const th = core(env).THEMES
  assert.equal(chartOf(env, 'l_qty').config.data.datasets[0].borderColor, th.light.inward)
  env.setScheme('dark')
  assert.equal(env.charts[0].destroyed, true)
  assert.equal(chartOf(env, 'l_qty').config.data.datasets[0].borderColor, th.dark.inward)
})

test('below 640 px sections after the first collapse behind a toggle; the first has none', async () => {
  const env = await open({ width: 400 })
  const sections = findAll(env.root, 'ic-section')
  assert.equal(findAll(sections[0], 'ic-section-toggle').length, 0)
  const toggle = findAll(sections[1], 'ic-section-toggle')[0]
  assert.equal(toggle.ariaExpanded, 'false')
  assert.equal(findAll(sections[1], 'ic-grid')[0].hidden, true)
  toggle.click()
  assert.equal(toggle.ariaExpanded, 'true')
  assert.equal(findAll(sections[1], 'ic-grid')[0].hidden, false)
  env.setWidth(1200)
  assert.equal(findAll(sections[1], 'ic-section-toggle').length, 0)
})

test('without IntersectionObserver every chart draws at once', async () => {
  const env = await open({ noObserver: true })
  assert.ok(env.liveCharts().length >= 5)
})

test('no request other than ./spec and ./data/<qid> is made, including after clicks', async () => {
  const env = await open()
  env.showAll()
  tapPoint(env, 'l_qty', 0)
  findAll(env.root, 'ic-range')[0].click()
  for (const u of env.fetches) assert.match(u, /^\.\/(spec|data\/[a-z0-9_]+)\?t=x$/)
})

// ---------------------------------------------------------------- 22-09: governed tables

function M(column: string, kind: string, label: string | null = null, basis: string | null = 'weigh date', extra: any = {}) {
  return { column, label: label ?? column, description: null, kind, parts: null, formula: null, date_basis: basis, ...extra }
}

const T_ROWS = ALL_DAYS.map((d) => ({
  metric_time__day: d,
  qty: qtyOf(d),
  wt: 2,
  avgw: 1,
  avgw__sum: +d.slice(8, 10),
  avgw__count: 2,
  rate: 0.5,
  rate__num: 1,
  rate__den: 2,
}))
const T_METAS = [
  M('qty', 'sum', 'Rolls in'),
  M('wt', 'sum', 'Weight'),
  M('avgw', 'avg', 'Average'),
  M('rate', 'ratio', 'Rate'),
]

function colSpec(column: string, extra: any = {}) {
  return { column, label: column, format: null, currency: null, unit: null, owed_when: null, delta: false, good_direction: 'none', bar: false, total: null, due: null, ...extra }
}

const T_COLS = [
  colSpec('metric_time__day', { label: 'Day' }),
  colSpec('qty', { label: 'Rolls in', delta: true, good_direction: 'up', bar: true }),
  colSpec('wt', { label: 'Weight' }),
  colSpec('avgw', { label: 'Average', format: 'decimal2' }),
  colSpec('rate', { label: 'Rate', format: 'percent' }),
]

function tableTile(extra: any = {}) {
  return base('t1', 'table', {
    qid: 'tq', width: 'full', columns: T_COLS, max_rows: 25, sort: null, totals: true, measures: T_METAS, ...extra,
  })
}

function tableSpecOf(tiles: any[], refresh: any = {}) {
  return makeSpec({
    sections: [{ title: 'T', description: null, tiles: tiles.map((t) => t.id) }],
    tiles,
    refresh: { tq: 15, ...refresh },
  })
}

async function openTable(tiles: any[], data: any, extra: any = {}) {
  const env = makeEnv({ spec: tableSpecOf(tiles, extra.refresh || {}), data, ...(extra.env || {}) })
  await env.flush()
  env.showAll()
  return env
}

function tableRows(env: ReturnType<typeof makeEnv>, id: string): FakeEl[] {
  const t = tile(env.root, id)
  const body = findTag(findAll(t, 'ic-table')[0], 'tbody')[0]
  return body.children
}

function cellsOf(tr: FakeEl): FakeEl[] {
  return tr.children.filter((c) => c.tag === 'td')
}

test('table: a 5-measure daily table under last_7_days has 7 rows newest first with the right sums', async () => {
  const env = await openTable([tableTile()], { tq: snap(T_ROWS) })
  const rows = tableRows(env, 't1')
  assert.equal(rows.length, 7)
  const first = cellsOf(rows[0])
  assert.equal(text(first[0]), 'Nov 4')
  assert.equal(first[0].dataset.icValue, undefined)
  assert.equal(first[0].dataset.icRow, undefined)
  assert.equal(first[1].dataset.icValue, '4')
  assert.equal(first[1].dataset.icCol, 'qty')
  assert.equal(first[1].dataset.icRow, '2026-11-04')
  // columns: day, qty, qty change, wt, avgw, rate
  assert.equal(cellsOf(rows[0])[3].dataset.icValue, '2')
  assert.equal(cellsOf(rows[6])[1].dataset.icValue, '29')
  // average re-aggregated from its parts: day 4 -> 4 / 2
  assert.equal(text(cellsOf(rows[0])[4]), '2.00')
  assert.equal(cellsOf(rows[0])[5].dataset.icValue, '0.5')
})

test('table: totals row re-aggregates average and ratio from parts and adds sums; no icValue', async () => {
  const env = await openTable([tableTile()], { tq: snap(T_ROWS) })
  const total = findAll(tile(env.root, 't1'), 'ic-total')[0]
  assert.equal(total.tag, 'tr')
  const cs = total.children
  assert.equal(text(cs[0]), 'Total')
  assert.equal(text(cs[1]), '100')
  assert.equal(text(cs[3]), '14')
  assert.equal(text(cs[4]), '7.14') // 100 / 14
  assert.equal(text(cs[5]), '50%')
  for (const c of cs) assert.equal(c.dataset.icValue, undefined)
})

test('table: a custom range gives one row per day bucket, paged, and the totals follow the range', async () => {
  const env = await openTable([tableTile()], { tq: snap(T_ROWS) }, { env: { hash: '#r=2026-10-01..2026-11-04' } })
  const rows = tableRows(env, 't1')
  assert.equal(rows.length, 25)
  assert.match(text(findAll(tile(env.root, 't1'), 'ic-pager')[0]), /Rows 1-25 of 35/)
  const total = findAll(tile(env.root, 't1'), 'ic-total')[0]
  // Oct 1-31 sum 496 + Nov 1-4 sum 10
  assert.equal(text(total.children[1]), '506')
})

test('table: the sort button cycles descending, ascending, original', async () => {
  const env = await openTable([tableTile()], { tq: snap(T_ROWS) })
  const t = tile(env.root, 't1')
  const sortBtn = () => findAll(t, 'ic-sort').filter((b) => text(b).startsWith('Rolls in'))[0]
  assert.equal(text(sortBtn()), 'Rolls in')
  sortBtn().click()
  assert.match(text(sortBtn()), /▼$/)
  assert.equal(cellsOf(tableRows(env, 't1')[0])[1].dataset.icValue, '31')
  sortBtn().click()
  assert.match(text(sortBtn()), /▲$/)
  assert.equal(cellsOf(tableRows(env, 't1')[0])[1].dataset.icValue, '1')
  sortBtn().click()
  assert.equal(text(sortBtn()), 'Rolls in')
  assert.equal(cellsOf(tableRows(env, 't1')[0])[1].dataset.icValue, '4')
})

test('table: paging buttons move through the rows without a refetch and disable at the ends', async () => {
  const env = await openTable([tableTile({ max_rows: 3 })], { tq: snap(T_ROWS) })
  const t = tile(env.root, 't1')
  const before = env.fetches.length
  const pager = () => findAll(t, 'ic-pager')[0]
  assert.match(text(pager()), /Rows 1-3 of 7/)
  assert.equal(findAll(t, 'ic-prev')[0].disabled, true)
  findAll(t, 'ic-next')[0].click()
  assert.match(text(pager()), /Rows 4-6 of 7/)
  findAll(t, 'ic-next')[0].click()
  assert.match(text(pager()), /Rows 7-7 of 7/)
  assert.equal(findAll(t, 'ic-next')[0].disabled, true)
  findAll(t, 'ic-prev')[0].click()
  assert.match(text(pager()), /Rows 4-6 of 7/)
  assert.equal(env.fetches.length, before)
  assert.equal(tableRows(env, 't1').length, 3)
})

test('table: delta cells colour by good_direction and carry no icValue; the bar width is set by CSSOM', async () => {
  const env = await openTable([tableTile()], { tq: snap(T_ROWS) })
  const rows = tableRows(env, 't1')
  // Nov 1 vs Oct 31: 1 vs 31 -> down, bad when up is good
  const nov1 = rows.find((r) => cellsOf(r)[1].dataset.icRow === '2026-11-01')!
  const delta = findAll(nov1, 'ic-delta')[0]
  assert.equal(text(delta), '▼ -96.8%')
  assert.ok(delta.classList.contains('ic-bad'))
  assert.equal(delta.parent!.dataset.icValue, undefined)
  const bars = findAll(tile(env.root, 't1'), 'ic-cellbar')
  assert.equal(bars.length, 7)
  assert.equal(Math.max(...bars.map((b) => parseFloat(b.style.width))), 100)
  assert.ok(bars[0].style.width.endsWith('%'))
})

const CUST_ROWS = ALL_DAYS.flatMap((d) => [
  { metric_time__day: d, customers__name: 'X', channels__name: 'A', qty: 3 },
  { metric_time__day: d, customers__name: 'X', channels__name: 'B', qty: 3 },
  { metric_time__day: d, customers__name: 'Y', channels__name: 'A', qty: 1 },
  { metric_time__day: d, customers__name: null, channels__name: 'A', qty: 1 },
])

test('table: a controlled table keys members on by_columns[0], not on the column order', async () => {
  const t = tableTile({
    qid: 'cq', by_columns: ['customers__name', 'channels__name'], measures: [M('qty', 'sum', 'Rolls in')],
    columns: [colSpec('channels__name'), colSpec('customers__name'), colSpec('qty', { delta: true, good_direction: 'up' })],
  })
  const env = await openTable([t], { cq: snap(CUST_ROWS) }, { refresh: { cq: 15 } })
  const rows = tableRows(env, 't1')
  assert.equal(rows.length, 3) // X, Y, Not set
  const r0 = cellsOf(rows[0])
  assert.equal(text(r0[1]), 'X')
  assert.equal(text(r0[0]), '–') // second by column shows an en dash
  assert.equal(r0[2].dataset.icValue, '42')
  assert.equal(r0[2].dataset.icRow, 'X')
  assert.equal(r0[0].dataset.icValue, undefined)
  assert.equal(text(cellsOf(rows[2])[1]), 'Not set')
  assert.equal(cellsOf(rows[2])[2].dataset.icRow, 'Not set')
  // member delta: same member over the previous 7 days (equal -> 0.0%)
  assert.equal(text(findAll(rows[0], 'ic-delta')[0]), '0.0%')
})

test('table: member totals combine every member, not only the page', async () => {
  const t = tableTile({
    qid: 'cq', by_columns: ['customers__name'], max_rows: 1, measures: [M('qty', 'sum', 'Rolls in')],
    columns: [colSpec('customers__name'), colSpec('qty')],
  })
  const env = await openTable([t], { cq: snap(CUST_ROWS) }, { refresh: { cq: 15 } })
  assert.equal(tableRows(env, 't1').length, 1)
  const total = findAll(tile(env.root, 't1'), 'ic-total')[0]
  assert.equal(text(total.children[1]), String(7 * (6 + 1 + 1)))
})

test('table: distinct measures show an en dash in the total', async () => {
  const t = tableTile({
    qid: 'cq', by_columns: ['customers__name'], measures: [M('qty', 'distinct', 'Customers')],
    columns: [colSpec('customers__name'), colSpec('qty')],
  })
  const env = await openTable([t], { cq: snap(CUST_ROWS) }, { refresh: { cq: 15 } })
  const total = findAll(tile(env.root, 't1'), 'ic-total')[0]
  assert.equal(text(total.children[1]), '–')
})

test('table: the basis caption appears only when the measures count by different dates', async () => {
  const same = await openTable([tableTile()], { tq: snap(T_ROWS) })
  assert.equal(findAll(tile(same.root, 't1'), 'ic-basis').length, 0)
  const metas = [M('qty', 'sum', 'Rolls in', 'weigh date'), M('wt', 'sum', 'Dispatches', 'dispatch date')]
  const t = tableTile({
    measures: metas,
    columns: [colSpec('metric_time__day', { label: 'Day' }), colSpec('qty', { label: 'Rolls in' }), colSpec('wt', { label: 'Dispatches' })],
  })
  const env = await openTable([t], { tq: snap(T_ROWS) })
  assert.equal(text(findAll(tile(env.root, 't1'), 'ic-basis')[0]),
    'Each column counts by its own date: Rolls in (weigh date), Dispatches (dispatch date).')
})

const F_ROWS = [
  { region: 'North', weight: 5, due: '2026-11-01', owe: 0 },
  { region: 'South', weight: 7, due: '2026-11-06', owe: 12 },
  { region: 'North', weight: 2, due: '2026-12-30', owe: 3 },
]

test('fixed table: rows as returned, owed gold with a word, due states, delta against the previous row', async () => {
  const t = base('ft', 'table', {
    qid: 'fq', follow: 'fixed', horizon_days: null, width: 'full', grain: null, measures: [], by_columns: [],
    max_rows: 25, sort: null, totals: true,
    columns: [colSpec('region'), colSpec('weight', { delta: true, good_direction: 'up', total: true }),
      colSpec('owe', { owed_when: { op: '>', value: 5 } }), colSpec('due', { due: { warn_days: 5 } })],
  })
  const env = await openTable([t], { fq: snap(F_ROWS) }, { refresh: { fq: 15 } })
  const rows = tableRows(env, 'ft')
  assert.equal(rows.length, 3)
  assert.deepEqual(rows.map((r) => text(cellsOf(r)[0])), ['North', 'South', 'North'])
  // columns: region, weight, weight change, owe, due
  assert.ok(cellsOf(rows[1])[3].classList.contains('ic-owed'))
  assert.equal(text(findAll(cellsOf(rows[1])[3], 'ic-owed-badge')[0]), 'Owed')
  assert.equal(findAll(cellsOf(rows[0])[3], 'ic-owed-badge').length, 0)
  assert.equal(text(findAll(cellsOf(rows[0])[4], 'ic-owed-badge')[0]), 'Overdue')
  assert.match(text(cellsOf(rows[1])[4]), /Due in 2 days/)
  assert.equal(findAll(cellsOf(rows[2])[4], 'ic-owed-badge').length, 0)
  assert.equal(text(findAll(rows[1], 'ic-delta')[0]), '▲ +40.0%')
  assert.ok(findAll(rows[1], 'ic-delta')[0].classList.contains('ic-good'))
  // query-bound: icRow is the first column's value; the total adds only with total true
  assert.equal(cellsOf(rows[1])[1].dataset.icRow, 'South')
  const total = findAll(tile(env.root, 'ft'), 'ic-total')[0]
  assert.equal(text(total.children[1]), '14')
  assert.equal(text(total.children[3]), '–')
})

test('fixed measure table: icRow is the time key with a grain (icSeries the by value); without a grain icRow is the by value', async () => {
  const rows = [
    { metric_time__month: '2026-09-01', region: 'North', weight: 5 },
    { metric_time__month: '2026-10-01', region: 'South', weight: 7 },
  ]
  const mk = (id: string, grain: string | null, cols: any[]) => base(id, 'table', {
    qid: 'fq', follow: 'fixed', horizon_days: null, width: 'full', grain, by_columns: ['region'],
    measures: [M('weight', 'sum', 'Weight', null)], max_rows: 25, sort: null, totals: false, columns: cols,
  })
  const env = await openTable(
    [mk('g1', 'month', [colSpec('metric_time__month'), colSpec('region'), colSpec('weight')]),
      mk('g2', null, [colSpec('weight')])],
    { fq: snap(rows) }, { refresh: { fq: 15 } })
  const c = cellsOf(tableRows(env, 'g1')[1])[2]
  assert.equal(c.dataset.icRow, '2026-10-01')
  assert.equal(c.dataset.icSeries, 'South')
  const d = cellsOf(tableRows(env, 'g2')[0])[0]
  assert.equal(d.dataset.icRow, 'North')
  assert.equal(d.dataset.icSeries, undefined)
})

test('fixed measure table totals: sum kinds add, average from parts, closing takes the latest key', async () => {
  const rows = [
    { metric_time__month: '2026-09-01', bal: 10, avgw: 1, avgw__sum: 6, avgw__count: 2, n: 1, nofix: 1 },
    { metric_time__month: '2026-10-01', bal: 20, avgw: 2, avgw__sum: 6, avgw__count: 1, n: 2, nofix: 1 },
  ]
  const t = base('ft2', 'table', {
    qid: 'fq', follow: 'fixed', horizon_days: null, width: 'full', grain: 'month', by_columns: [],
    measures: [M('bal', 'closing', 'Balance', null), M('avgw', 'avg', 'Avg', null), M('n', 'sum', 'N', null), M('nofix', 'avg', 'NoParts', null)],
    max_rows: 25, sort: null, totals: true,
    columns: [colSpec('metric_time__month'), colSpec('bal'), colSpec('avgw'), colSpec('n'), colSpec('nofix')],
  })
  const env = await openTable([t], { fq: snap(rows) }, { refresh: { fq: 15 } })
  const total = findAll(tile(env.root, 'ft2'), 'ic-total')[0]
  assert.equal(text(total.children[1]), '20')
  assert.equal(text(total.children[2]), '4')
  assert.equal(text(total.children[3]), '3')
  assert.equal(text(total.children[4]), '–')
})

// ---------------------------------------------------------------- 22-09: the nine visuals and targets

const VZ_METAS = [M('qty', 'sum', 'Rolls in'), M('wt', 'sum', 'Weight')]
const VZ_ROWS = ALL_DAYS.flatMap((d) => [
  { metric_time__day: d, cust: 'A', qty: 3, wt: 1 },
  { metric_time__day: d, cust: 'B', qty: 2, wt: 1 },
  { metric_time__day: d, cust: 'C', qty: 1, wt: 1 },
])
const FIX_ROWS = [
  { stage: 'Visit', n: 200, m: 80 },
  { stage: 'Signup', n: 50, m: 30 },
  { stage: 'Buy', n: 10, m: 12 },
]
const YS = (...cols: string[]) => cols.map((c) => ({ column: c, label: c, dashed: false, tone: null, as: null }))

function controlledViz(id: string, type: string, extra: any = {}) {
  return base(id, type, { qid: 'vq', width: 'half', x: 'metric_time__day', y: YS('qty'), measures: VZ_METAS, ...extra })
}

function fixedViz(id: string, type: string, extra: any = {}) {
  return base(id, type, { qid: 'fq', follow: 'fixed', horizon_days: null, width: 'half', x: 'stage', y: YS('n'), measures: [], ...extra })
}

async function openViz(tiles: any[], extra: any = {}) {
  const env = makeEnv({
    spec: tableSpecOf(tiles, { vq: 15, fq: 15 }),
    data: { vq: snap(VZ_ROWS), fq: snap(FIX_ROWS), ...(extra.data || {}) },
    ...(extra.env || {}),
  })
  await env.flush()
  env.showAll()
  return env
}

const NEW_TYPES: Array<[string, string, any]> = [
  ['combo', 'bar', { y: [{ column: 'qty', label: 'qty', dashed: false, tone: null, as: 'bar' }, { column: 'wt', label: 'wt', dashed: false, tone: null, as: 'line' }] }],
  ['paired_bar', 'bar', { y: YS('qty', 'wt') }],
  ['aging', 'bar', {}],
  ['waterfall', 'bar', {}],
  ['donut', 'doughnut', { x: 'cust', by_columns: ['cust'] }],
  ['ranked_bar', 'bar', { x: 'cust', by_columns: ['cust'] }],
  ['funnel', 'bar', { x: 'cust', by_columns: ['cust'] }],
  ['calendar', 'matrix', {}],
]

for (const [type, chartType, extra] of NEW_TYPES) {
  test('viz ' + type + ': controlled draws one ' + chartType + ' chart with a role img label and a hidden table', async () => {
    const env = await openViz([controlledViz('v1', type, extra)])
    const live = env.liveCharts()
    assert.equal(live.length, 1)
    assert.equal(live[0].config.type, chartType)
    const t = tile(env.root, 'v1')
    const canvas = findTag(t, 'canvas')[0]
    assert.equal(canvas.role, 'img')
    assert.match(canvas.ariaLabel!, /^Title v1: \d+ points, latest/)
    assert.equal(findAll(t, 'ic-sr').length, 1)
  })

  test('viz ' + type + ': fixed rows draw through the same builder', async () => {
    const fx: any = { ...extra }
    if (type === 'combo') fx.y = [{ column: 'n', label: 'n', dashed: false, tone: null, as: 'bar' }, { column: 'm', label: 'm', dashed: false, tone: null, as: 'line' }]
    else if (type === 'paired_bar') fx.y = YS('n', 'm')
    else if (type === 'calendar') fx.y = YS('n')
    else {
      fx.x = 'stage'
      fx.by_columns = []
    }
    const rows = type === 'calendar'
      ? [{ stage: '2026-11-02', n: 4, m: 1 }, { stage: '2026-11-03', n: 9, m: 1 }]
      : FIX_ROWS
    const env = await openViz([fixedViz('v2', type, fx)], { data: { fq: snap(rows) } })
    const live = env.liveCharts()
    assert.equal(live.length, 1)
    assert.equal(live[0].config.type, chartType)
    assert.equal(findAll(tile(env.root, 'v2'), 'ic-sr').length, 1)
  })
}

test('viz bullet: controlled and fixed each draw one bullet chart with the target tick', async () => {
  const c = controlledViz('b1', 'bullet', { column: 'qty', x: null, y: [], target: { value: 500, label: null }, bands: [200, 400, 600] })
  const f = fixedViz('b2', 'bullet', { column: 'n', x: null, y: [], target: { column: 'm', label: null } })
  const env = await openViz([c, f])
  const live = env.liveCharts()
  assert.equal(live.length, 2)
  const cc = live.find((x: any) => x.config.options.plugins.annotation.annotations.target.value === 500)!
  assert.equal(cc.config.data.datasets[3].data[0], 7 * 6)
  assert.equal(cc.config.options.plugins.annotation.annotations.target.label.content, 'Target set by owner')
  const fc = live.find((x: any) => x !== cc)!
  assert.equal(fc.config.options.plugins.annotation.annotations.target.label.content, 'Target')
  assert.equal(fc.config.options.plugins.annotation.annotations.target.value, 12)
  assert.ok(findAll(tile(env.root, 'b1'), 'ic-bullet').length >= 1)
})

test('viz ranked_bar: members over the range are re-aggregated, Other has no icValue, height grows per bar', async () => {
  const t = controlledViz('r1', 'ranked_bar', { x: 'cust', by_columns: ['cust'], top: 2, other: true })
  const env = await openViz([t])
  const chart = chartOf(env, 'r1')
  assert.deepEqual(Array.from(chart.config.data.labels), ['A', 'B', 'Other'])
  assert.deepEqual(Array.from(chart.config.data.datasets[0].data), [21, 14, 7])
  const sr = findAll(tile(env.root, 'r1'), 'ic-sr')[0]
  const cells = findTag(sr, 'tbody')[0].children.map((tr) => tr.children.filter((c) => c.tag === 'td')[0])
  assert.equal(cells[0].dataset.icValue, '21')
  assert.equal(cells[0].dataset.icRow, 'A')
  assert.equal(cells[2].dataset.icValue, undefined)
  const holder = findAll(tile(env.root, 'r1'), 'ic-chart')[0]
  assert.equal(holder.style.height, core(env).barsHeight(3) + 'px')
})

test('viz donut: more than 5 parts say they are grouped as Other', async () => {
  const rows = ['a', 'b', 'c', 'd', 'e', 'f', 'g'].flatMap((k, i) => ALL_DAYS.map((d) => ({ metric_time__day: d, cust: k, qty: 8 - i, wt: 1 })))
  const t = controlledViz('d1', 'donut', { x: 'cust', by_columns: ['cust'] })
  const env = await openViz([t], { data: { vq: snap(rows) } })
  const chart = chartOf(env, 'd1')
  assert.equal(chart.config.data.datasets[0].data.length, 5)
  assert.ok(text(tile(env.root, 'd1')).includes('Smaller parts are grouped as Other'))
})

test('viz waterfall under control: tapping a bar focuses that day', async () => {
  const env = await openViz([controlledViz('w1', 'waterfall')])
  const chart = chartOf(env, 'w1')
  chart.hits = [{ index: 6, datasetIndex: 0 }]
  chart.config.options.onClick({}, [], chart)
  assert.match(text(findAll(env.root, 'ic-chip')[0]), /Showing/)
})

test('viz: a controlled visual outside the stored horizon says it is not available', async () => {
  const t = controlledViz('h1', 'aging', { horizon_days: 3 })
  const env = await openViz([t], { env: { hash: '#r=2026-10-01..2026-10-05' } })
  assert.equal(env.liveCharts().length, 0)
  assert.match(text(tile(env.root, 'h1')), /Not available for this range/)
})

test('no forbidden chart types are used by any new visual config', async () => {
  const env = await openViz(NEW_TYPES.map(([type, , extra], i) => controlledViz('x' + i, type, extra)))
  const allowed = new Set(['bar', 'line', 'doughnut', 'matrix'])
  for (const c of env.charts) {
    assert.ok(allowed.has(c.config.type), c.config.type)
    for (const d of c.config.data.datasets) assert.ok(!d.type || allowed.has(d.type), d.type)
  }
})

test('stat target: progress track with width set by CSSOM, percent of target and the owner label', async () => {
  const s = base('st1', 'stat', {
    qid: 'm_qty', column: 'qty', measures: [SUM_QTY], comparison: 'none', good_direction: 'up', width: 'quarter',
    target: { value: 400, label: null },
  })
  const env = await openViz([s], { data: { m_qty: snap(QTY_ROWS) } , env: {} })
  const t = tile(env.root, 'st1')
  const track = findAll(t, 'ic-bullet')[0]
  assert.ok(track)
  const fill = findAll(track, 'ic-cellbar')[0]
  assert.equal(fill.style.width, '25%')
  assert.ok(text(t).includes('25% of target'))
  assert.ok(text(t).includes('Target set by owner'))
})

test('line and bar tiles under control: target value is one ink annotation, target column a dashed ink dataset per bucket', async () => {
  const l1 = controlledViz('tl1', 'line', { y: YS('qty'), target: { value: 5, label: null } })
  const l2 = controlledViz('tl2', 'bar', { y: YS('qty'), target: { column: 'wt', label: null } })
  const env = await openViz([l1, l2])
  const a = chartOf(env, 'tl1').config.options.plugins.annotation.annotations.target
  assert.equal(a.borderColor, core(env).THEMES.dark.ink)
  assert.equal(a.value, 5)
  assert.equal(a.label.content, 'Target set by owner')
  const ds = chartOf(env, 'tl2').config.data.datasets
  const tg = ds[ds.length - 1]
  assert.equal(tg.label, 'Target')
  assert.equal(tg.borderColor, core(env).THEMES.dark.ink)
  assert.deepEqual(Array.from(tg.data), [3, 3, 3, 3, 3, 3, 3])
  // the hidden table has no cell for the target
  const sr = findAll(tile(env.root, 'tl2'), 'ic-sr')[0]
  assert.equal(findTag(sr, 'th').filter((c) => text(c) === 'Target').length, 0)
})

// ---------------------------------------------------------------- 22-12: the wrapper channel

test('the frame has exactly one parent.postMessage call site, with a * target', async () => {
  const { readFileSync } = await import('node:fs')
  const { join } = await import('node:path')
  const src = readFileSync(join(process.cwd(), '..', 'dashboards', 'vendor', 'ic-render-2.0.0', 'render.js'), 'utf8')
  assert.equal((src.match(/parent\.postMessage\(/g) || []).length, 1)
  assert.ok(/parent\.postMessage\(msg, '\*'\)/.test(src))
})

test('a range change posts one state message with exact keys', async () => {
  const env = await open()
  assert.equal(env.posted.length, 0)
  findAll(env.root, 'ic-range')[0].click()
  findAll(findAll(env.root, 'ic-sheet')[0], 'ic-preset').find((p) => text(p) === 'This month')!.click()
  assert.equal(env.posted.length, 1)
  const [msg, target] = env.posted[0]
  assert.equal(target, '*')
  assert.deepEqual(Object.keys(msg).sort(), ['hash', 'ic', 'type'])
  assert.equal(msg.ic, 1)
  assert.equal(msg.type, 'state')
  assert.equal(msg.hash, 'r=this_month')
  assert.match(msg.hash, /^[A-Za-z0-9_=,.:;%~-]{0,600}$/)
})

test('tap-a-date posts the focused range and clearing it posts the previous one', async () => {
  const env = await open()
  tapPoint(env, 'l_qty', 1) // Oct 30
  assert.equal(env.posted.length, 1)
  assert.equal(env.posted[0][0].hash, 'r=2026-10-30..2026-10-30')
  findAll(findAll(env.root, 'ic-chip')[0], 'ic-chip-x')[0].click()
  assert.equal(env.posted.length, 2)
  assert.equal(env.posted[1][0].hash, 'r=last_7_days')
})

test('the info button and the stat value post an ask message with exact keys', async () => {
  const env = await open()
  const t = tile(env.root, 's_qty')
  findAll(t, 'ic-info')[0].click()
  assert.equal(env.posted.length, 1)
  assert.deepEqual(Object.keys(env.posted[0][0]).sort(), ['ic', 'tile', 'title', 'type'])
  assert.deepEqual({ ...env.posted[0][0] }, { ic: 1, type: 'ask', tile: 's_qty', title: 'Title s_qty' })
  assert.equal(env.posted[0][1], '*')
  findAll(t, 'ic-stat-value')[0].click()
  assert.equal(env.posted.length, 2)
  assert.equal(env.posted[1][0].tile, 's_qty')
  findAll(tile(env.root, 's_fixed'), 'ic-stat-value')[0].click()
  assert.equal(env.posted[2][0].tile, 's_fixed')
})

test('ask titles lose control and bidi characters and are cut to 120 code points without a lone surrogate', async () => {
  const c = core(await open())
  const nasty = 'A\u0001B\u202EC\u2066D' + '\u{1F600}'.repeat(130)
  const out = c.cleanTitle(nasty)
  assert.equal(Array.from(out).length, 120)
  assert.ok(out.startsWith('ABCD'))
  assert.ok(!/[\u0000-\u001F\u202A-\u202E\u2066-\u2069]/.test(out))
  assert.ok(!/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(out))
  assert.equal(c.cleanTitle('x\uD800y'), 'xy')
  assert.equal(c.cleanTitle(null), '')
})

test('nothing but state and ask is ever posted across a session of clicks', async () => {
  const env = await open()
  findAll(env.root, 'ic-range')[0].click()
  findAll(findAll(env.root, 'ic-sheet')[0], 'ic-preset')[1].click()
  for (const b of findAll(env.root, 'ic-info')) b.click()
  for (const v of findAll(env.root, 'ic-stat-value')) v.click()
  tapPoint(env, 'l_qty', 2)
  assert.ok(env.posted.length > 4)
  for (const [m, target] of env.posted) {
    assert.equal(target, '*')
    assert.ok(m.type === 'state' || m.type === 'ask', m.type)
    assert.equal(m.ic, 1)
  }
})

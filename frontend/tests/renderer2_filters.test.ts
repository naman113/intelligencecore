// Phase 22 (22-12, DCAP-11, DCAP-14): page filters, cross-filter, two-level drill, Other folding and
// the URL state of ic-render-2.0.0, as pure functions and under the fake browser.
import test from 'node:test'
import assert from 'node:assert/strict'
import { findAll, findTag, makeEnv, text, tile, type FakeEl } from './fakedom'

const TODAY = '2026-11-04'
const NUL = String.fromCharCode(0)
const PRESETS = ['today', 'yesterday', 'last_7_days', 'last_30_days', 'last_90_days', 'this_month', 'last_month', 'this_quarter', 'this_fy']

function addDays(iso: string, n: number): string {
  return new Date(Date.parse(iso + 'T00:00:00Z') + n * 86400000).toISOString().slice(0, 10)
}
function days(from: string, to: string): string[] {
  const out: string[] = []
  for (let d = from; d <= to; d = addDays(d, 1)) out.push(d)
  return out
}
const ALL_DAYS = days('2025-10-01', TODAY)

function snap(rows: any[]) {
  return {
    query_id: 'q', columns: rows.length ? Object.keys(rows[0]) : [], rows,
    refreshed_at: '2026-11-04T09:50:00+00:00', data_as_of: '2026-11-04T09:50:00+00:00', timezone: 'UTC', error: null,
  }
}

const SUM = { column: 'qty', label: 'Quantity', description: 'Rolls weighed', kind: 'sum', parts: null, formula: null, date_basis: null }
const YS = [{ column: 'qty', label: 'Qty', dashed: false, tone: null, as: null }]

function base(id: string, type: string, extra: any = {}) {
  return {
    id, type, title: 'Title ' + id, note: null, qid: null, grain: null, format: 'integer', currency: null,
    unit: null, width: 'half', follow: 'control', follow_filters: false, horizon_days: 400,
    presets_qid: null, target: null, measures: [], by_columns: [], ...extra,
  }
}

const BRANCHES: Array<[string | null, number]> = [['North', 3], ['South', 2], ['East', 1], [null, 1]]
const BR_ROWS = ALL_DAYS.flatMap((d) => BRANCHES.map(([b, q]) => ({ metric_time__day: d, branch: b, qty: q })))
// customer x material, no filter column
const TWO_ROWS = ALL_DAYS.flatMap((d) => [
  { metric_time__day: d, cust: 'A', mat: 'x', qty: 3 },
  { metric_time__day: d, cust: 'A', mat: 'y', qty: 1 },
  { metric_time__day: d, cust: 'B', mat: 'x', qty: 2 },
])

function makeSpec(tiles: any[], filters: any[] = [{ id: 'branch', label: 'Branch', column: 'branch' }]) {
  return {
    render_version: 2, title: 'Ops', subtitle: null, number_locale: 'en-US', timezone: 'UTC',
    date_control: { default: 'last_7_days', presets: PRESETS, horizon_days: 400, fy_start_month: 4 },
    filters,
    sections: [{ title: 'S', description: null, tiles: tiles.map((t) => t.id) }],
    tiles,
    refresh: { q_br: 15, q_plain: 15, q_two: 15 },
  }
}

const TILES = () => [
  base('s_qty', 'stat', { qid: 'q_br', follow_filters: true, column: 'qty', measures: [SUM], comparison: 'none' }),
  base('s_plain', 'stat', { qid: 'q_plain', column: 'qty', measures: [SUM], comparison: 'none' }),
  base('rb', 'ranked_bar', { qid: 'q_br', follow_filters: true, x: 'branch', by_columns: ['branch'], y: YS, measures: [SUM] }),
]

async function open(tiles: any[] = TILES(), data: any = {}, extra: any = {}) {
  const env = makeEnv({
    spec: makeSpec(tiles, extra.filters),
    data: { q_br: snap(BR_ROWS), q_plain: snap(ALL_DAYS.map((d) => ({ metric_time__day: d, qty: 5 }))), q_two: snap(TWO_ROWS), ...data },
    ...(extra.env || {}),
  })
  await env.flush()
  env.showAll()
  return env
}

function core(env: ReturnType<typeof makeEnv>): any {
  return (env.context as any).ICRenderCore
}

function statValue(env: ReturnType<typeof makeEnv>, id: string): string {
  return text(findAll(tile(env.root, id), 'ic-stat-value')[0])
}

function select(env: ReturnType<typeof makeEnv>): FakeEl {
  return findAll(env.root, 'ic-filter')[0]
}

function choose(env: ReturnType<typeof makeEnv>, v: string) {
  const sel = select(env)
  sel.value = v
  sel.dispatch('change')
}

function chartOf(env: ReturnType<typeof makeEnv>, id: string): any {
  const canvas = findTag(tile(env.root, id), 'canvas')[0]
  assert.ok(canvas, 'canvas in ' + id)
  return env.charts.filter((c) => c.canvas === canvas && !c.destroyed)[0]
}

function tap(env: ReturnType<typeof makeEnv>, id: string, index: number) {
  const chart = chartOf(env, id)
  chart.hits = [{ index, datasetIndex: 0 }]
  chart.config.options.onClick({}, [], chart)
}

const lastState = (env: ReturnType<typeof makeEnv>) =>
  env.posted.filter(([m]) => m.type === 'state').slice(-1)[0][0]

// ---------------------------------------------------------------- pure

test('filterOptions: first source ranked by total, later sources append, at most 50', () => {
  const core = makeEnvCore()
  const meta = { kind: 'sum', column: 'qty' }
  const range = { from: '2026-11-03', to: '2026-11-04' }
  const ctx = { today: TODAY, asOf: TODAY, horizonStart: '2025-10-01', dayCol: 'metric_time__day' }
  const a = [
    { metric_time__day: '2026-11-04', branch: 'a', qty: 1 },
    { metric_time__day: '2026-11-04', branch: 'b', qty: 5 },
    { metric_time__day: '2026-11-04', branch: null, qty: 3 },
  ]
  const b = [{ metric_time__day: '2026-11-04', branch: 'z', qty: 100 }, { metric_time__day: '2026-11-04', branch: 'a', qty: 1 }]
  const o = core.filterOptions([{ meta, rows: a, range, ctx }, { meta, rows: b, range, ctx }], 'branch', 50)
  assert.deepEqual(Array.from(o.map((x: any) => x.label)), ['b', 'Not set', 'a', 'z'])
  assert.equal(o[1].key, NUL)
  const many = Array.from({ length: 80 }, (_, i) => ({ metric_time__day: '2026-11-04', branch: 'm' + i, qty: i }))
  assert.equal(core.filterOptions([{ meta, rows: many, range, ctx }], 'branch', 50).length, 50)
})

test('the NULL member round-trips through the URL state', () => {
  const core = makeEnvCore()
  const hash = core.encodeState({ range: { preset: 'last_7_days' }, filters: { branch: [NUL] } })
  assert.equal(hash, 'r=last_7_days;f.branch=~null')
  const back = core.decodeState(hash)
  assert.deepEqual(Array.from(back.filters.branch), [NUL])
  // a real value that looks like the token is escaped, and a hand-written %00 is refused
  const tricky = core.encodeState({ range: null, filters: { branch: ['~null'] } })
  assert.equal(tricky, 'f.branch=%7Enull')
  assert.deepEqual(Array.from(core.decodeState(tricky).filters.branch), ['~null'])
  assert.equal(core.decodeState('f.branch=%00'), null)
})

test('safeHash stays inside the wrapper pattern and 600 characters', () => {
  const core = makeEnvCore()
  const vals = Array.from({ length: 50 }, (_, i) => "D'Souza (Pune) " + i + ' x'.repeat(5))
  const h = core.safeHash({ range: { preset: 'last_30_days' }, filters: { branch: vals } })
  assert.ok(h.length <= 600)
  assert.match(h, /^[A-Za-z0-9_=,.:;%~-]{0,600}$/)
  assert.equal(h, 'r=last_30_days')
  const short = core.safeHash({ range: { from: '2026-10-01', to: '2026-10-31' }, filters: { branch: ["D'Souza (Pune)"] } })
  assert.match(short, /^[A-Za-z0-9_=,.:;%~-]{0,600}$/)
  assert.ok(short.includes('D%27Souza%20%28Pune%29'))
})

test('drillLevel2 keeps the rows of one first-level member and reads the second by column', () => {
  const core = makeEnvCore()
  const t = base('r', 'ranked_bar', { x: 'cust', by_columns: ['cust', 'mat'] })
  assert.equal(core.isDrillable(t), true)
  assert.equal(core.isDrillable({ ...t, type: 'bar' }), false)
  assert.equal(core.isDrillable({ ...t, by_columns: ['cust'] }), false)
  assert.equal(core.isDrillable({ ...t, x: 'metric_time__day' }), false)
  assert.equal(core.isDrillable({ ...t, follow: 'fixed' }), false)
  const l2 = core.drillLevel2(t, TWO_ROWS.slice(0, 3), 'A')
  assert.equal(l2.tile.x, 'mat')
  assert.equal(l2.rows.length, 2)
})

test('memberModel and ranked view fold past 12 members: top 11 plus Other; not combinable kinds keep 12', () => {
  const core = makeEnvCore()
  const rows = Array.from({ length: 14 }, (_, i) => ({ metric_time__day: '2026-11-04', k: 'r' + i, v: i + 1 }))
  const range = { from: '2026-11-04', to: '2026-11-04' }
  const ctx = { today: TODAY, asOf: TODAY, horizonStart: '2025-10-01', dayCol: 'metric_time__day' }
  const t: any = { id: 'x', type: 'ranked_bar', title: 'X', format: 'integer', x: 'k', follow: 'control', measures: [{ column: 'v', kind: 'sum' }], y: [{ column: 'v' }] }
  const V = core.vizData(t, rows, range, ctx, 'en-US')
  const view = core.vizView(t, V, 'en-US')
  assert.equal(view.labels.length, 12)
  assert.equal(view.labels[11], 'Other')
  assert.equal(view.series[0].data[11], 1 + 2 + 3)
  const d: any = { ...t, measures: [{ column: 'v', kind: 'distinct' }] }
  const Vd = core.vizData(d, rows, range, ctx, 'en-US')
  const vd = core.vizView(d, Vd, 'en-US')
  assert.equal(vd.labels.length, 12)
  assert.equal(vd.dropped, 2)
  const top: any = { ...t, top: 3, other: true }
  assert.equal(core.vizView(top, core.vizData(top, rows, range, ctx, 'en-US'), 'en-US').labels.length, 4)
})

test('series tones follow the unfiltered member order', () => {
  const core = makeEnvCore()
  const rows = ['a', 'b', 'c'].flatMap((m, i) => [{ metric_time__day: '2026-11-04', m, v: 10 - i }])
  const range = { from: '2026-11-04', to: '2026-11-04' }
  const t: any = { id: 's', type: 'stacked_bar', title: 'S', format: 'integer', x: 'metric_time__day', series: 'm', measures: [{ column: 'v', kind: 'sum' }], y: [{ column: 'v' }] }
  const plain = core.timeModel(t, rows.slice(1), range, { today: TODAY, asOf: TODAY, horizonStart: '2025-10-01', dayCol: 'metric_time__day' }, 'en-US')
  assert.deepEqual(Array.from(plain.sets.map((s: any) => s.tone)), ['inward', 'outward'])
  const kept = core.timeModel(t, rows.slice(1), range, { today: TODAY, asOf: TODAY, horizonStart: '2025-10-01', dayCol: 'metric_time__day', toneKeys: ['a', 'b', 'c'] }, 'en-US')
  assert.deepEqual(JSON.parse(JSON.stringify(kept.sets.map((s: any) => [s.label, s.tone]))), [['b', 'outward'], ['c', 'accent']])
})

function makeEnvCore() {
  return core(makeEnv({ spec: makeSpec([]), data: {} }))
}

// ---------------------------------------------------------------- DOM: filters

test('the control bar shows a select per filter: All, then values by total descending', async () => {
  const env = await open()
  const sel = select(env)
  assert.ok(sel)
  const opts = sel.children.map((o) => o.textContent)
  assert.deepEqual(opts, ['All Branch', 'North', 'South', 'East', 'Not set'])
  assert.equal(sel.children[0].value, '')
  assert.equal(sel.children[4].value, NUL)
})

test('choosing a value filters following tiles, shows a chip and marks the others', async () => {
  const env = await open()
  assert.equal(statValue(env, 's_qty'), String((3 + 2 + 1 + 1) * 7))
  choose(env, 'South')
  assert.equal(statValue(env, 's_qty'), String(2 * 7))
  assert.equal(statValue(env, 's_plain'), String(5 * 7))
  const chips = findAll(env.root, 'ic-chip')
  assert.equal(chips.length, 1)
  assert.ok(text(chips[0]).startsWith('Branch: South'))
  assert.ok(text(tile(env.root, 's_plain')).includes('Not filtered by Branch'))
  assert.ok(!text(tile(env.root, 's_qty')).includes('Not filtered by Branch'))
  assert.equal(lastState(env).hash, 'r=last_7_days;f.branch=South')
  findAll(env.root, 'ic-chip-x')[0].click()
  assert.equal(statValue(env, 's_qty'), String(7 * 7))
  assert.equal(lastState(env).hash, 'r=last_7_days')
  assert.ok(!text(tile(env.root, 's_plain')).includes('Not filtered by Branch'))
})

test('a value the select does not offer is ignored', async () => {
  const env = await open()
  choose(env, 'Nowhere')
  assert.equal(findAll(env.root, 'ic-chip').length, 0)
  assert.equal(statValue(env, 's_qty'), String(7 * 7))
})

test('tapping a bar whose category is a filter column sets the filter', async () => {
  const env = await open()
  assert.deepEqual(Array.from(chartOf(env, 'rb').config.data.labels), ['North', 'South', 'East', 'Not set'])
  tap(env, 'rb', 1)
  assert.equal(statValue(env, 's_qty'), String(2 * 7))
  assert.equal(select(env).value, 'South')
  assert.equal(lastState(env).hash, 'r=last_7_days;f.branch=South')
})

test('a NULL member filters apart from text and round-trips through the URL', async () => {
  const env = await open()
  choose(env, NUL)
  assert.equal(statValue(env, 's_qty'), String(1 * 7))
  assert.ok(text(findAll(env.root, 'ic-chip')[0]).startsWith('Branch: Not set'))
  const hash = lastState(env).hash
  assert.equal(hash, 'r=last_7_days;f.branch=~null')
  const again = await open(TILES(), {}, { env: { hash: '#' + hash } })
  assert.equal(statValue(again, 's_qty'), String(1 * 7))
  assert.equal(select(again).value, NUL)
  // tapping the Not set bar sets the same filter
  const third = await open()
  tap(third, 'rb', 3)
  assert.equal(statValue(third, 's_qty'), String(1 * 7))
})

test('a restored hash keeps known filters and drops unknown ids and values silently', async () => {
  const env = await open(TILES(), {}, { env: { hash: '#r=last_30_days;f.nope=x;f.branch=Zed' } })
  assert.equal(findAll(env.root, 'ic-chip').length, 0)
  assert.equal(statValue(env, 's_qty'), String(7 * 30))
  const ok = await open(TILES(), {}, { env: { hash: '#r=last_30_days;f.branch=East' } })
  assert.equal(statValue(ok, 's_qty'), String(30))
  assert.equal(select(ok).value, 'East')
})

test('values with ! \' ( ) survive a select, the state post and a reload', async () => {
  const name = "D'Souza (Pune)!"
  const rows = ALL_DAYS.flatMap((d) => [{ metric_time__day: d, branch: name, qty: 4 }, { metric_time__day: d, branch: 'North', qty: 1 }])
  const env = await open(TILES(), { q_br: snap(rows) })
  choose(env, name)
  assert.equal(statValue(env, 's_qty'), String(4 * 7))
  const hash = lastState(env).hash
  assert.match(hash, /^[A-Za-z0-9_=,.:;%~-]{0,600}$/)
  assert.ok(hash.includes('D%27Souza%20%28Pune%29%21'))
  const again = await open(TILES(), { q_br: snap(rows) }, { env: { hash } })
  assert.equal(statValue(again, 's_qty'), String(4 * 7))
  assert.equal(select(again).value, name)
})

test('filtering recomputes without a new data request', async () => {
  const env = await open()
  const before = env.fetches.length
  choose(env, 'North')
  tap(env, 'rb', 0)
  assert.equal(env.fetches.length, before)
})

// ---------------------------------------------------------------- DOM: drill

const DRILL = () => [
  base('rd', 'ranked_bar', { qid: 'q_two', x: 'cust', by_columns: ['cust', 'mat'], y: YS, measures: [SUM] }),
  base('bd', 'stacked_bar', { qid: 'q_two', x: 'metric_time__day', series: 'mat', by_columns: ['cust', 'mat'], y: YS, measures: [SUM] }),
]

test('a ranked bar with two by columns shows level 1 and drills to level 2 on tap, with a chip back', async () => {
  const env = await open(DRILL())
  assert.deepEqual(Array.from(chartOf(env, 'rd').config.data.labels), ['A', 'B'])
  assert.deepEqual(Array.from(chartOf(env, 'rd').config.data.datasets[0].data), [28, 14])
  tap(env, 'rd', 0)
  const t = tile(env.root, 'rd')
  const chips = findAll(t, 'ic-chip')
  assert.equal(chips.length, 1)
  assert.ok(text(chips[0]).startsWith('A'))
  assert.deepEqual(Array.from(chartOf(env, 'rd').config.data.labels), ['x', 'y'])
  assert.deepEqual(Array.from(chartOf(env, 'rd').config.data.datasets[0].data), [21, 7])
  // drill is tile-local: nothing was posted for it
  assert.equal(env.posted.length, 0)
  findAll(chips[0], 'ic-chip-x')[0].click()
  assert.equal(findAll(tile(env.root, 'rd'), 'ic-chip').length, 0)
  assert.deepEqual(Array.from(chartOf(env, 'rd').config.data.labels), ['A', 'B'])
})

test('a controlled bar with two by columns stays a time chart and a click does not drill', async () => {
  const env = await open(DRILL())
  const chart = chartOf(env, 'bd')
  assert.equal(chart.config.type, 'bar')
  tap(env, 'bd', 3)
  assert.equal(findAll(tile(env.root, 'bd'), 'ic-chip').length, 0)
  assert.equal(findAll(tile(env.root, 'rd'), 'ic-chip').length, 0)
})

test('a controlled plain bar with two by columns stays a time chart and a click does not drill', async () => {
  const env = await open([base('bb', 'bar', { qid: 'q_two', x: 'metric_time__day', by_columns: ['cust', 'mat'], y: YS, measures: [SUM] })])
  assert.equal(chartOf(env, 'bb').config.type, 'bar')
  tap(env, 'bb', 3)
  assert.equal(findAll(tile(env.root, 'bb'), 'ic-chip').length, 0)
})

test('a ranked bar with more than 12 members folds the rest into a neutral Other', async () => {
  const rows = ALL_DAYS.flatMap((d) => Array.from({ length: 14 }, (_, i) => ({ metric_time__day: d, cust: 'c' + i, qty: i + 1 })))
  const t = base('rf', 'ranked_bar', { qid: 'q_two', x: 'cust', by_columns: ['cust'], y: YS, measures: [SUM] })
  const env = await open([t], { q_two: snap(rows) })
  const cfg = chartOf(env, 'rf').config
  assert.equal(cfg.data.labels.length, 12)
  assert.equal(cfg.data.labels[11], 'Other')
  assert.equal(cfg.data.datasets[0].data[11], (1 + 2 + 3) * 7)
})

// ---------------------------------------------------------------- review fix: Other bars, unconfirmed link values

function srCells(env: ReturnType<typeof makeEnv>, id: string) {
  const sr = findAll(tile(env.root, id), 'ic-sr')[0]
  return findTag(sr, 'tbody')[0].children.map((r) => r.children.filter((c) => c.tag === 'td')[0])
}

test('a fixed bar with a compiled Other row: neutral Other bar, no icValue on it', async () => {
  const rows = [{ cust: 'a', qty: 5 }, { cust: 'b', qty: 4 }, { cust: 'Other', qty: 9 }]
  const t = base('fx', 'bar', { qid: 'q_two', follow: 'fixed', x: 'cust', by_columns: ['cust'], y: YS, measures: [SUM],
    top: 2, other_row: true, other_top: 2 })
  const env = await open([t], { q_two: snap(rows) })
  const cfg = chartOf(env, 'fx').config
  const bg = cfg.data.datasets[0].backgroundColor
  assert.equal(bg[2] === bg[0], false)
  const cells = srCells(env, 'fx')
  assert.equal(cells[0].dataset.icValue, '5')
  assert.equal(cells[2].dataset.icValue, undefined)
  const few = await open([t], { q_two: snap(rows.slice(0, 2)) })
  assert.equal(srCells(few, 'fx')[1].dataset.icValue, '4')
})

test('a controlled bar with 14 members folds into a neutral Other without icValue', async () => {
  const rows = ALL_DAYS.flatMap((d) => Array.from({ length: 14 }, (_, i) => ({ metric_time__day: d, cust: 'c' + i, qty: i + 1 })))
  const t = base('bm', 'bar', { qid: 'q_two', x: 'cust', by_columns: ['cust'], y: YS, measures: [SUM] })
  const env = await open([t], { q_two: snap(rows) })
  const cfg = chartOf(env, 'bm').config
  assert.equal(cfg.data.labels[11], 'Other')
  const bg = cfg.data.datasets[0].backgroundColor
  assert.equal(bg[11] === bg[0], false)
  assert.equal(bg[0], bg[10])
  const cells = srCells(env, 'bm')
  assert.equal(cells[11].dataset.icValue, undefined)
  assert.ok(cells[0].dataset.icValue !== undefined)
})

test('a restored link value is dropped when the only follower errored', async () => {
  const env = await open(TILES().slice(0, 1), {}, { env: { hash: '#r=last_30_days;f.branch=CALL%20555-0100%20NOW', failData: ['q_br'] } })
  assert.equal(findAll(env.root, 'ic-chip').length, 0)
  assert.equal(select(env).value, '')
})

test('a restored link value is dropped when no tile follows the filters', async () => {
  const env = await open(TILES().slice(1, 2), {}, { env: { hash: '#r=last_30_days;f.branch=East' } })
  assert.equal(findAll(env.root, 'ic-chip').length, 0)
  assert.equal(select(env).value, '')
})

test('a dropped restored value is posted back to the wrapper and the chip is gone', async () => {
  const two = [
    base('a', 'stat', { qid: 'q_br', follow_filters: true, column: 'qty', measures: [SUM], comparison: 'none' }),
    base('b', 'stat', { qid: 'q_br2', follow_filters: true, column: 'qty', measures: [SUM], comparison: 'none' }),
  ]
  const env = await open(two, { q_br2: snap(BR_ROWS) }, { env: { hash: '#r=last_30_days;f.branch=Zed' } })
  assert.equal(findAll(env.root, 'ic-chip').length, 0)
  assert.equal(select(env).value, '')
  assert.ok(!/f\.branch/.test(lastState(env).hash), lastState(env).hash)
})

test('a controlled member visual with no rows keeps its empty state; a stat is a real zero', async () => {
  const tiles = [
    base('s0', 'stat', { qid: 'q_none', column: 'qty', measures: [SUM], comparison: 'none' }),
    base('rb0', 'ranked_bar', { qid: 'q_none', x: 'branch', by_columns: ['branch'], y: YS, measures: [SUM] }),
  ]
  const env = await open(tiles, { q_none: snap([]) }, { filters: [] })
  assert.equal(statValue(env, 's0'), '0')
  assert.ok(text(tile(env.root, 'rb0')).includes('No rows for this period'))
})

test('a controlled stat shows the measure of its own column', async () => {
  const KG = { ...SUM, column: 'kg', label: 'Kg', kind: 'sum' }
  const rows = ALL_DAYS.map((d) => ({ metric_time__day: d, qty: 1, kg: 10 }))
  const tiles = [base('s2', 'stat', { qid: 'q_two_m', column: 'kg', measures: [SUM, KG], comparison: 'none' })]
  const env = await open(tiles, { q_two_m: snap(rows) }, { filters: [] })
  assert.equal(statValue(env, 's2'), '70')
})

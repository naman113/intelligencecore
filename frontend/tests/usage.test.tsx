import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import type { ReactElement } from 'react'
import ReactDOMServer from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import {
  budgetView,
  fmtDay,
  fmtMs,
  fmtOffset,
  fmtTokens,
  fmtUsd,
  isNested,
  modelCallsMissing,
  rootAgent,
  traceOffsets,
  unpricedNote,
  usageExportName,
  usageExportPath,
  usageSummaryPath,
  usageTracePath,
  type RunTrace,
  type UsageBudget,
  type UsageNumbers,
  type UsageSummary,
  type UsageTopRun,
} from '../src/lib/usage'
import { BudgetTile, SpendTile, TopRunsTable, UsageView } from '../src/pages/Usage'
import { TraceView, traceMeta } from '../src/components/Trace'
import { COMPONENT_LABEL } from '../src/components/HealthAlerts'

const html = (el: ReactElement) => ReactDOMServer.renderToStaticMarkup(<MemoryRouter>{el}</MemoryRouter>)

/* ── Fixtures ──────────────────────────────────────────────────────────── */

const nums = (o: Partial<UsageNumbers> = {}): UsageNumbers => ({
  calls: 12, input: 1000, output: 200, cache_read: 5000, cache_write: 300, tokens: 6500, cost_usd: 0.1234,
  unpriced_calls: 0, ...o,
})

const budget = (o: Partial<UsageBudget> = {}): UsageBudget => ({
  limit_usd: 10, spent_usd: 1.2, pct: 12, state: 'ok', resets_at: '2026-10-26T00:00:00+05:30', ...o,
})
const OFF = budget({ limit_usd: null, pct: null, state: 'off' })
const WARN = budget({ spent_usd: 8.5, pct: 85, state: 'warn' })
const PAUSED = budget({ spent_usd: 10.4, pct: 104, state: 'paused' })

const runRow: UsageTopRun = {
  trace: 'run:42', run_id: 42, feature: 'chat', feature_label: 'Chat answer', thread_id: 'ui:owner',
  started_at: '2026-10-25T10:00:00Z', calls: 7, tokens: 120000, cost_usd: 0.53, models: ['claude-opus-5-5'],
}
const watchRow: UsageTopRun = {
  trace: 'watch:7:investigation', run_id: null, feature: 'watch_investigation', feature_label: 'Watch investigation',
  thread_id: null, started_at: '2026-10-25T09:00:00Z', calls: 3, tokens: 40000, cost_usd: 0.21, models: ['claude-sonnet-5-5'],
}

const summary = (o: Partial<UsageSummary> = {}): UsageSummary => ({
  price_table_date: '2026-10-25',
  timezone: 'Asia/Kolkata',
  installs: ['int', 'client-fleet'],
  install: null,
  period: 'month',
  since: '2026-10-01T00:00:00+05:30',
  today: nums(),
  month: nums({ calls: 300, tokens: 2_400_000, cost_usd: 12.5 }),
  period_totals: nums({ calls: 300, tokens: 2_400_000, cost_usd: 12.5, unpriced_calls: 2 }),
  budget: budget(),
  by_model: [
    { key: 'claude-opus-5-5', label: 'claude-opus-5-5', ...nums({ cost_usd: 9.1 }) },
    { key: 'mystery-model', label: 'mystery-model', ...nums({ cost_usd: 0, unpriced_calls: 2 }) },
  ],
  by_feature: [{ key: 'chat', label: 'Chat answer', ...nums() }],
  by_agent: [{ key: 'supervisor', label: 'supervisor', ...nums() }],
  by_chat: [
    { key: 'ui:owner', label: 'ui:owner', ...nums() },
    { key: null, label: '(no chat)', ...nums({ calls: 3 }) },
  ],
  top_runs: [runRow, watchRow],
  daily: [
    { day: '2026-10-20', tokens: 1000, cost_usd: 0.12 },
    { day: '2026-10-21', tokens: 3000, cost_usd: 0.4 },
    { day: '2026-10-22', tokens: 2000, cost_usd: 0.2 },
  ],
  ...o,
})

const view = (s: UsageSummary) =>
  html(
    <UsageView
      summary={s}
      period="month"
      install={null}
      onPeriod={() => {}}
      onInstall={() => {}}
      onExport={() => {}}
    />,
  )

const totals = {
  model_calls: 2, tool_calls: 2, input: 1200, output: 3400, cache_read: 52000, cache_write: 4100, tokens: 60700,
  cost_usd: 0.183, unpriced_calls: 0, model_ms: 41000, tool_ms: 9000, wall_ms: 52000,
}
const fullTrace: RunTrace = {
  run_id: 42,
  trace: 'run:42',
  totals,
  events: [
    { kind: 'model', at: '2026-10-25T10:00:00.120Z', ms: 4100, agent: 'supervisor', model: 'claude-opus-5-5',
      feature: 'chat', input: 10, output: 200, cache_read: 5000, cache_write: 0, tokens: 5210, cost_usd: 0.0123, error: null },
    { kind: 'tool', at: '2026-10-25T10:00:04.300Z', ms: 9000, agent: 'supervisor', name: 'delegate_to_data_agent',
      summary: 'Total sales by region last month', error: null },
    { kind: 'model', at: '2026-10-25T10:00:04.500Z', ms: 2000, agent: 'data', model: 'claude-sonnet-5-5',
      feature: 'chat', input: 5, output: 90, cache_read: 0, cache_write: 4100, tokens: 4195, cost_usd: 0.02, error: null },
    { kind: 'tool', at: '2026-10-25T10:00:07.000Z', ms: 340, agent: 'data', name: 'run_sql',
      summary: 'SELECT region, sum(total) FROM orders GROUP BY 1', error: 'statement timeout' },
  ],
}
const emptyTrace: RunTrace = {
  run_id: 43, trace: 'run:43', events: [],
  totals: { ...totals, model_calls: 0, tool_calls: 0, input: 0, output: 0, cache_read: 0, cache_write: 0, tokens: 0, cost_usd: 0, model_ms: 0, tool_ms: 0, wall_ms: 0 },
}
const oldTrace: RunTrace = {
  run_id: 7,
  trace: 'run:7',
  totals: { ...emptyTrace.totals, tool_calls: 1, tool_ms: 340 },
  events: [
    { kind: 'tool', at: '2026-09-01T10:00:00Z', ms: 340, agent: null, name: 'run_sql', summary: 'SELECT 1', error: null },
  ],
}

/* ── Formatters ────────────────────────────────────────────────────────── */

test('fmtTokens shortens counts', () => {
  assert.equal(fmtTokens(0), '0')
  assert.equal(fmtTokens(950), '950')
  assert.equal(fmtTokens(1000), '1k')
  assert.equal(fmtTokens(1200), '1.2k')
  assert.equal(fmtTokens(12345), '12.3k')
  assert.equal(fmtTokens(123456), '123k')
  assert.equal(fmtTokens(999_600), '1M')
  assert.equal(fmtTokens(3_400_000), '3.4M')
  assert.equal(fmtTokens(2_100_000_000), '2.1B')
  assert.equal(fmtTokens(null), '—')
  assert.equal(fmtTokens(Number.NaN), '—')
})

test('fmtUsd keeps small values readable', () => {
  assert.equal(fmtUsd(0.0123), '$0.0123')
  assert.equal(fmtUsd(0.005), '$0.0050')
  assert.equal(fmtUsd(0.1234), '$0.12')
  assert.equal(fmtUsd(1.234), '$1.23')
  assert.equal(fmtUsd(123.4), '$123')
  assert.equal(fmtUsd(1234.5), '$1,235')
  assert.equal(fmtUsd(0), '$0.00')
  assert.equal(fmtUsd(0.00001), '<$0.0001')
  assert.equal(fmtUsd(undefined), '—')
})

test('fmtMs, fmtOffset, fmtDay, unpricedNote', () => {
  assert.equal(fmtMs(340), '340 ms')
  assert.equal(fmtMs(4100), '4.1 s')
  assert.equal(fmtMs(125_000), '2 min 5 s')
  assert.equal(fmtMs(null), '—')
  assert.equal(fmtOffset(0), '+0.0 s')
  assert.equal(fmtOffset(4180), '+4.2 s')
  assert.equal(fmtDay('2026-10-25'), '25 Oct 2026')
  assert.equal(fmtDay('2026-10-05', false), '5 Oct')
  assert.equal(unpricedNote(0), '')
  assert.equal(unpricedNote(1), '1 call unpriced')
  assert.equal(unpricedNote(3), '3 calls unpriced')
})

test('usage routes carry period and install', () => {
  assert.equal(usageSummaryPath('month', null), '/api/usage/summary?period=month')
  assert.equal(usageSummaryPath('7d', 'client-fleet'), '/api/usage/summary?period=7d&install=client-fleet')
  assert.equal(usageExportPath('30d', null), '/api/usage/export.csv?period=30d')
  assert.equal(usageExportPath('today', 'int'), '/api/usage/export.csv?period=today&install=int')
  assert.equal(usageExportName('month'), 'usage-month.csv')
  assert.equal(usageTracePath('watch:7:investigation'), '/api/usage/trace?trace=watch%3A7%3Ainvestigation')
})

test('budgetView: off, ok, warn, paused', () => {
  assert.equal(budgetView(OFF).headline, 'No daily budget set')
  assert.equal(budgetView(budget()).headline, '$1.20 of $10.00')
  assert.equal(budgetView(budget()).note, '')
  assert.match(budgetView(WARN).note, /Past 80%/)
  assert.equal(budgetView(WARN).tone, 'signal')
  assert.equal(budgetView(PAUSED).tone, 'broken')
  assert.match(budgetView(PAUSED).note, /paused until the budget resets/)
  assert.match(budgetView(PAUSED).note, /Chat keeps working/)
})

/* ── Usage view ────────────────────────────────────────────────────────── */

test('budget tile: off points at Setup, no meter', () => {
  const h = html(<BudgetTile budget={OFF} />)
  assert.match(h, /data-budget-state="off"/)
  assert.match(h, /No daily budget set/)
  assert.match(h, /href="\/system\/setup"/)
  assert.match(h, /Setup › Tuning/)
  assert.ok(!h.includes('class="meter"'))
})

test('budget tile: ok is blue with spend of limit', () => {
  const h = html(<BudgetTile budget={budget()} />)
  assert.match(h, /\$1\.20 of \$10\.00/)
  assert.match(h, /class="meter-fill" style="width:12%"/)
  assert.match(h, /12% used/)
  assert.ok(!/Past 80%|paused/i.test(h))
})

test('budget tile: warn says so in plain text, never gold or red', () => {
  const h = html(<BudgetTile budget={WARN} />)
  assert.match(h, /data-budget-state="warn"/)
  assert.match(h, /Past 80% of today&#x27;s budget/)
  assert.match(h, /class="meter-fill" style="width:85%"/)
  assert.ok(!/owed|broken/.test(h))
})

test('budget tile: paused is broken and caps the meter', () => {
  const h = html(<BudgetTile budget={PAUSED} />)
  assert.match(h, /data-budget-state="paused"/)
  assert.match(h, /pill pill-broken">paused/)
  assert.match(h, /meter-fill is-broken" style="width:100%"/)
  assert.match(h, /104% used/)
  assert.match(h, /Chat keeps working/)
  assert.ok(!/owed/.test(h))
})

test('spend tile draws a sparkline of daily cost', () => {
  const h = html(<SpendTile daily={summary().daily} />)
  assert.match(h, /<polyline/)
  assert.match(h, /\$0\.72/)
  assert.match(h, /20 Oct to 22 Oct/)
  assert.match(h, /peak \$0\.40 on 21 Oct/)
  assert.match(html(<SpendTile daily={[]} />), /Not enough history yet/)
})

test('usage view: tiles, controls, breakdowns, top runs, no gold', () => {
  const h = view(summary())
  assert.match(h, /data-tile="today"/)
  assert.match(h, /data-tile="month"/)
  assert.match(h, /data-tile="budget"/)
  assert.match(h, /data-tile="daily"/)
  assert.match(h, /\$12\.50/)
  assert.match(h, /2\.4M tokens/)
  for (const t of ['Today', '7 days', 'This month', '30 days']) assert.ok(h.includes(`>${t}</button>`), t)
  assert.match(h, /aria-selected="true" class="tab is-active">This month/)
  assert.match(h, /<option value="" selected="">All installs<\/option>/)
  assert.match(h, /<option value="client-fleet">client-fleet<\/option>/)
  assert.match(h, /Export CSV/)
  for (const s of ['By model', 'By feature', 'By agent', 'By chat', 'Top 20 most expensive runs']) assert.ok(h.includes(s), s)
  assert.match(h, /claude-opus-5-5/)
  assert.match(h, /Chat answer/)
  assert.match(h, /\(no chat\)/)
  assert.match(h, /2 calls unpriced/)
  assert.match(h, /since 1 Oct 2026/)
  assert.match(h, /Asia\/Kolkata/)
  assert.match(h, /class="table-scroll"/)
  assert.ok(!h.includes('data-budget-banner'))
  assert.ok(!/owed/i.test(h))
})

test('usage view: paused budget raises a broken banner, warn does not', () => {
  const paused = view(summary({ budget: PAUSED }))
  assert.match(paused, /banner banner-broken" data-budget-banner="paused"/)
  assert.match(paused, /overnight exploration, playbooks and watch investigations/)
  assert.ok(!/owed/i.test(paused))
  const warn = view(summary({ budget: WARN }))
  assert.ok(!warn.includes('data-budget-banner'))
  assert.match(warn, /Past 80%/)
  const off = view(summary({ budget: OFF }))
  assert.match(off, /No daily budget set/)
})

test('usage view: empty period reads as empty, not broken', () => {
  const h = view(summary({ by_model: [], by_feature: [], by_agent: [], by_chat: [], top_runs: [], installs: [] }))
  assert.match(h, /No model calls in this period/)
  assert.match(h, /No AI work in this period/)
  assert.ok(!h.includes('banner-broken'))
})

test('usage view keeps a selected install that has no calls in the period', () => {
  const h = html(
    <UsageView
      summary={summary({ installs: ['int'] })}
      period="7d"
      install="client-fleet"
      onPeriod={() => {}}
      onInstall={() => {}}
      onExport={() => {}}
      exporting
      exportError="Not found."
    />,
  )
  assert.match(h, /<option value="client-fleet" selected="">/)
  assert.match(h, /Exporting…/)
  assert.match(h, /banner banner-broken">Not found\./)
})

test('top runs: run rows link to Runs, run-less rows expand inline', () => {
  const closed = html(<TopRunsTable runs={[runRow, watchRow]} />)
  assert.match(closed, /href="\/system\/runs\/42"/)
  assert.match(closed, /Run #42/)
  assert.match(closed, /aria-expanded="false"[^>]*>Show trace/)
  assert.ok(!closed.includes('/system/runs/null'))
  assert.ok(!closed.includes('data-trace-expand'))
  assert.match(closed, /Watch investigation/)

  const open = html(<TopRunsTable runs={[runRow, watchRow]} initialOpen="watch:7:investigation" />)
  assert.match(open, /aria-expanded="true"[^>]*>Hide trace/)
  assert.match(open, /data-trace-expand="watch:7:investigation"/)
  assert.match(open, /colSpan="7"|colspan="7"/)
  assert.match(open, /Reading the trace/)
})

/* ── Trace ─────────────────────────────────────────────────────────────── */

test('trace: totals and ordered timeline', () => {
  const h = html(<TraceView trace={fullTrace} />)
  assert.match(h, /data-trace-totals/)
  assert.match(h, /60\.7k/)
  assert.match(h, /in 1\.2k · out 3\.4k · cache read 52k · cache write 4\.1k/)
  assert.match(h, /\$0\.18/)
  assert.match(h, /41\.0 s/)
  assert.match(h, /9\.0 s/)
  assert.match(h, /of 52\.0 s wall time/)
  const order = ['+0.0 s', '+4.2 s', '+4.4 s', '+6.9 s'].map((t) => h.indexOf(t))
  assert.ok(order.every((i) => i >= 0), String(order))
  assert.deepEqual([...order].sort((a, b) => a - b), order)
  assert.equal(h.split('data-kind="model"').length - 1, 2)
  assert.equal(h.split('data-kind="tool"').length - 1, 2)
  assert.match(h, /pill pill-signal">model/)
  assert.match(h, /class="pill">tool/)
  assert.match(h, /5\.2k tokens · in 10 · out 200 · cache read 5k · \$0\.0123 est\./)
  assert.match(h, /SELECT region, sum\(total\) FROM orders/)
  assert.match(h, /trace-error">statement timeout/)
  assert.match(h, /340 ms/)
  assert.ok(!h.includes('data-trace-note'))
  assert.ok(!/owed/i.test(h))
})

test('trace: delegate rows are nested under the supervisor', () => {
  const h = html(<TraceView trace={fullTrace} />)
  assert.equal(h.split('trace-row is-nested').length - 1, 2)
  assert.equal(rootAgent(fullTrace.events), 'supervisor')
  assert.equal(isNested(fullTrace.events[2]!, 'supervisor'), true)
  assert.equal(isNested(fullTrace.events[1]!, 'supervisor'), false)
  assert.equal(rootAgent([{ ...fullTrace.events[3]!, agent: 'watch' }]), 'watch')
  assert.deepEqual(traceOffsets(fullTrace.events), [0, 4180, 4380, 6880])
})

test('trace: empty run', () => {
  const h = html(<TraceView trace={emptyTrace} />)
  assert.match(h, /Nothing recorded/)
  assert.ok(!h.includes('data-trace-totals'))
  assert.equal(traceMeta(emptyTrace), '0 tool calls')
})

test('trace: a run from before usage tracking shows tools and a quiet note', () => {
  assert.equal(modelCallsMissing(oldTrace), true)
  assert.equal(modelCallsMissing(fullTrace), false)
  const h = html(<TraceView trace={oldTrace} />)
  assert.match(h, /data-trace-note="no-model-calls"/)
  assert.match(h, /Model calls were not recorded for this run/)
  assert.match(h, /not recorded/)
  assert.match(h, /run_sql/)
  assert.match(h, /data-kind="tool"/)
  assert.ok(!h.includes('data-kind="model"'))
  assert.ok(!h.includes('banner-broken'))
  assert.equal(traceMeta(oldTrace), '1 tool call')
  assert.equal(traceMeta(fullTrace), '$0.18 est.')
})

/* ── Wiring ────────────────────────────────────────────────────────────── */

test('route, health label and no gold in the new sources', () => {
  assert.match(readFileSync('src/App.tsx', 'utf8'), /path="system\/usage" element=\{<Usage \/>\}/)
  assert.equal(COMPONENT_LABEL.llm_budget, 'AI spend budget')
  const runs = readFileSync('src/pages/Runs.tsx', 'utf8')
  assert.ok(runs.indexOf('<RunTraceSection') < runs.indexOf('title="Tool calls"'))
  for (const f of ['src/pages/Usage.tsx', 'src/pages/usage.css', 'src/components/Trace.tsx', 'src/components/trace.css', 'src/lib/usage.ts']) {
    const src = readFileSync(f, 'utf8')
    assert.ok(!/--owed|is-owed|pill-owed|banner-owed|card-owed|btn-owed|tone="owed"/.test(src), f)
  }
})

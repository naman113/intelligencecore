import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import type { ReactElement } from 'react'
import ReactDOMServer from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import {
  activityPath,
  busy,
  countEntries,
  durationText,
  elapsedSince,
  filterQuery,
  mergePages,
  outcomeLine,
  parseRunId,
  readFilters,
  refLabel,
  runRoute,
  runsHref,
  type ActivityItem,
  type ActivityNow,
} from '../src/lib/runs'
import { AgentFacts, CountsTable, FeedRow, FilterBar, JobFacts, NowPanel, SkillChips } from '../src/pages/Runs'
import { KpiDetailView } from '../src/pages/Kpis'
import { DashboardDetailView } from '../src/pages/Dashboards'
import type { Watch } from '../src/lib/types'
import { dashboardDetail, dashboards, kpiSeries } from './fixtures'

/* Quick 008: the Runs control center. */

const html = (el: ReactElement) => ReactDOMServer.renderToStaticMarkup(<MemoryRouter>{el}</MemoryRouter>)
const iso = (minsAgo: number) => new Date(Date.now() - minsAgo * 60_000).toISOString()

const item = (o: Partial<ActivityItem> = {}): ActivityItem => ({
  uid: 'run-11', source: 'agent', id: 11, kind: 'playbook', kind_label: 'Playbook', family: 'playbooks',
  title: 'Data exploration (playbook)', trigger: 'schedule (daily 02:00)', status: 'done',
  summary: 'Recorded 3 insights', error: null, question: null, at: iso(5), created_at: iso(5),
  started_at: iso(5), ended_at: iso(1), last_at: iso(1), duration_ms: 240_000, repeats: 1, counts: {},
  step: null, skills_used: [], thread_id: 'playbook-global', chat: null, case_id: null, playbook_id: 3,
  playbook_name: 'explore_data', task_run_id: null, trace_id: 'run:11', ref_type: 'playbook', ref_id: '3',
  ...o,
})

const job = (o: Partial<ActivityItem> = {}): ActivityItem =>
  item({
    uid: 'job-5', source: 'job', id: 5, kind: 'watch_check', kind_label: 'Watch check', family: 'watches',
    title: 'Watch check: Stale orders', trigger: 'every 15 min', summary: 'stale = 3 · alerts when > 5',
    duration_ms: 30, repeats: 40, routine: true, counts: { value: 3 }, thread_id: null, playbook_id: null,
    playbook_name: null, trace_id: null, ref_type: 'watch', ref_id: '7', ...o,
  })

const now = (o: Partial<ActivityNow> = {}): ActivityNow => ({
  running: [], queued: [], failed: [], counts: { running: 0, queued: 0, failed_24h: 0, done_24h: 12 }, ...o,
})

/* ── lib ───────────────────────────────────────────────────────────────── */

test('route ids: numbers are agent runs, job-N background runs', () => {
  assert.deepEqual(parseRunId('42'), { source: 'agent', id: 42 })
  assert.deepEqual(parseRunId('job-7'), { source: 'job', id: 7 })
  assert.equal(parseRunId('live-3'), null)
  assert.equal(parseRunId(undefined), null)
  assert.equal(runRoute(item(), '?kind=playbooks'), '/system/runs/11?kind=playbooks')
  assert.equal(runRoute(job()), '/system/runs/job-5')
  assert.equal(runRoute(job({ id: null, uid: 'live-2' })), '/system/runs')
})

test('filters round-trip through the URL and the API path', () => {
  const f = readFilters(new URLSearchParams('kind=watches&ref=watch%3A7&junk=1'))
  assert.deepEqual(f, { kind: 'watches', status: '', since: '', ref: 'watch:7' })
  assert.equal(filterQuery(f), '?kind=watches&ref=watch%3A7')
  assert.equal(filterQuery({ kind: '', status: '', since: '', ref: '' }), '')
  assert.equal(activityPath(f, '2026-10-26T02:00:00Z'),
    '/api/activity?kind=watches&ref=watch%3A7&before=2026-10-26T02%3A00%3A00Z&limit=50')
  assert.equal(runsHref('playbook:explore_data'), '/system/runs?ref=playbook%3Aexplore_data')
  assert.equal(refLabel('playbook:explore_data'), 'playbook explore data')
  assert.equal(refLabel('watch:7'), 'watch 7')
})

test('durations and elapsed time read like a person wrote them', () => {
  assert.equal(durationText(420), '420 ms')
  assert.equal(durationText(4200), '4.2 s')
  assert.equal(durationText(42_000), '42 s')
  assert.equal(durationText(240_000), '4 min')
  assert.equal(durationText(250_000), '4 min 10 s')
  assert.equal(durationText(3_900_000), '1 h 5 min')
  assert.equal(durationText(null), '')
  assert.equal(elapsedSince(new Date(Date.now() - 90_000).toISOString()), '1 min 30 s')
})

test('outcome line: live step while running, the error when failed', () => {
  assert.equal(outcomeLine(item({ status: 'running', step: 'Running query 2' })), 'Running query 2')
  assert.equal(outcomeLine(item({ status: 'queued' })), 'Waiting for a free worker')
  assert.equal(outcomeLine(item({ status: 'failed', error: 'boom' })), 'boom')
  assert.equal(outcomeLine(item()), 'Recorded 3 insights')
  assert.equal(outcomeLine(item({ summary: '*15,250* orders, see `sales_orders`' })), '15,250 orders, see sales_orders')
})

test('counts, merging pages and the busy flag', () => {
  assert.deepEqual(countEntries({ rows_deleted: 3, empty: '', nothing: null, nested: { a: 1 } }), [
    ['rows deleted', '3'],
    ['nested', '{"a":1}'],
  ])
  const merged = mergePages([item(), job()], [job(), item({ uid: 'run-9', id: 9 })])
  assert.deepEqual(merged.map((i) => i.uid), ['run-11', 'job-5', 'run-9'])
  assert.equal(busy(now()), false)
  assert.equal(busy(now({ counts: { running: 0, queued: 1, failed_24h: 0, done_24h: 0 } })), true)
})

/* ── feed ──────────────────────────────────────────────────────────────── */

test('feed row: title, trigger, duration, link that keeps the filters', () => {
  const out = html(<FeedRow item={item()} search="?kind=playbooks" />)
  assert.match(out, /Data exploration \(playbook\)/)
  assert.match(out, /schedule \(daily 02:00\)/)
  assert.match(out, /4 min/)
  assert.match(out, /href="\/system\/runs\/11\?kind=playbooks"/)
  assert.match(out, /pill-settled/)
  assert.doesNotMatch(out, /owed/)
})

test('feed row: collapsed routine runs show the count and the last time', () => {
  const out = html(<FeedRow item={job()} />)
  assert.match(out, /×40/)
  assert.match(out, /last 1m ago/)
  assert.match(out, /href="\/system\/runs\/job-5"/)
})

test('feed row: a failure is red and says why', () => {
  const out = html(<FeedRow item={job({ status: 'failed', error: 'The check query failed', repeats: 1 })} />)
  assert.match(out, /is-failed/)
  assert.match(out, /runs-line is-error/)
  assert.match(out, /The check query failed/)
  assert.match(out, /pill-broken/)
})

test('skills used show as chips on agent runs', () => {
  const out = html(<FeedRow item={item({ skills_used: ['exploratory-data-analysis', 'kpi-selection'] })} />)
  assert.match(out, /data-skills/)
  assert.match(out, /exploratory-data-analysis/)
  assert.match(out, /kpi-selection/)
  assert.equal(html(<SkillChips skills={[]} />), '')
  assert.doesNotMatch(html(<FeedRow item={item()} />), /data-skills/)
})

test('a run in flight with no row yet is shown but not linked', () => {
  const out = html(<FeedRow item={job({ id: null, uid: 'live-2', status: 'running', repeats: 1 })} />)
  assert.doesNotMatch(out, /href=/)
  assert.match(out, /running/)
})

/* ── now panel ─────────────────────────────────────────────────────────── */

test('running now: live step, elapsed time, waiting runs, counts', () => {
  const out = html(
    <NowPanel
      now={now({
        running: [item({ status: 'running', step: 'Reading the KPI history', started_at: iso(2), ended_at: null })],
        queued: [item({ uid: 'run-12', id: 12, status: 'queued', title: 'Chat: Fuel costs' })],
        counts: { running: 1, queued: 1, failed_24h: 0, done_24h: 4 },
      })}
    />,
  )
  assert.match(out, /Running now/)
  assert.match(out, /1 running · 1 waiting/)
  assert.match(out, /Reading the KPI history/)
  assert.match(out, /runs-pulse/)
  assert.match(out, /2 min/)
  assert.match(out, /Chat: Fuel costs/)
  assert.match(out, /waiting/)
  assert.match(out, /No failures in the last 24 hours/)
})

test('failed recently: errors first, with repeat counts', () => {
  const out = html(
    <NowPanel
      now={now({
        failed: [job({ status: 'failed', error: 'relation gone', repeats: 3 })],
        counts: { running: 0, queued: 0, failed_24h: 3, done_24h: 9 },
      })}
    />,
  )
  assert.match(out, /Nothing is running right now. 9 runs finished in the last 24 hours/)
  assert.match(out, /3 in 24 h/)
  assert.match(out, /relation gone/)
  assert.match(out, /×3/)
  assert.match(out, /dot-broken/)
})

test('now panel shows nothing until loaded, an error if the call failed', () => {
  assert.equal(html(<NowPanel now={null} />), '')
  assert.match(html(<NowPanel now={null} error="bad ops key" />), /bad ops key/)
})

/* ── filters ───────────────────────────────────────────────────────────── */

test('filter bar: family chips, status and window, the object filter can be cleared', () => {
  const out = html(
    <FilterBar filters={{ kind: 'watches', status: 'failed', since: '24h', ref: 'watch:7' }} onChange={() => {}} />,
  )
  for (const label of ['Everything', 'Chat &amp; messages', 'Playbooks', 'Watches', 'Dashboards', 'Data intake', 'Housekeeping'])
    assert.match(out, new RegExp(label))
  assert.match(out, /aria-pressed="true"[^>]*>Watches/)
  assert.match(out, /Only watch 7/)
  assert.match(out, /Last 24 hours/)
})

/* ── details ───────────────────────────────────────────────────────────── */

test('agent detail: playbook, trigger, skills, result', () => {
  const out = html(<AgentFacts item={item({ skills_used: ['forecasting'] })} />)
  assert.match(out, /href="\/playbooks"/)
  assert.match(out, /schedule \(daily 02:00\)/)
  assert.match(out, /forecasting/)
  assert.match(out, /Recorded 3 insights/)
})

test('agent detail: a chat run opens its chat, a case run links the case', () => {
  const chat = html(
    <AgentFacts
      item={item({ kind: 'chat', kind_label: 'Chat', title: 'Chat: Fuel costs', chat: 'ui:owner:abc', playbook_name: null, question: 'Why is fuel up?' })}
      onOpenChat={() => {}}
    />,
  )
  assert.match(chat, /Open chat/)
  assert.match(chat, /Fuel costs/)
  assert.match(chat, /Why is fuel up\?/)
  const kase = html(<AgentFacts item={item({ case_id: 9 })} />)
  assert.match(kase, /href="\/cases\/9"/)
})

test('job detail: the object it was about, the agent run it queued, repeats', () => {
  const out = html(
    <JobFacts
      item={job({ task_run_id: 41, ref: { type: 'watch', id: '7', label: 'Stale orders', to: '/kpis/7' } })}
    />,
  )
  assert.match(out, /href="\/kpis\/7"/)
  assert.match(out, /Stale orders/)
  assert.match(out, /href="\/system\/runs\/41"/)
  assert.match(out, /40 times with this outcome/)
  const failed = html(<JobFacts item={job({ status: 'failed', error: 'relation gone', repeats: 1 })} />)
  assert.match(failed, /relation gone/)
  assert.match(failed, /Took/)
})

test('counts table', () => {
  const out = html(<CountsTable counts={{ queries: 3, failed: 1 }} />)
  assert.match(out, /queries/)
  assert.match(out, /data-counts/)
  assert.equal(html(<CountsTable counts={{}} />), '')
})

/* ── links from other pages ────────────────────────────────────────────── */

test('KPI and dashboard details link to their runs', () => {
  const w = {
    id: 4, kind: 'kpi', title: 'Overdue invoices', condition: 'x', status: 'active', compile_error: null,
    last_checked_at: new Date().toISOString(),
  } as unknown as Watch
  const kpi = html(
    <KpiDetailView watch={w} series={kpiSeries} events={[]} dashboards={dashboards} onPin={() => {}} onToggle={() => {}} onAskKpi={() => {}} />,
  )
  assert.match(kpi, /href="\/system\/runs\?ref=watch%3A4"/)
  const dash = html(
    <DashboardDetailView
      data={dashboardDetail}
      base="https://d.x"
      configured
      draftUrls={{}}
      previews={{}}
      onRollback={() => {}}
      onRotate={() => {}}
      onFix={() => {}}
      onOpenDraft={() => {}}
    />,
  )
  assert.match(dash, new RegExp(`href="/system/runs\\?ref=dashboard%3A${dashboardDetail.dashboard.id}"`))
})

test('Playbooks, Watches and Insights link to the Runs page filtered', () => {
  const src = (p: string) => readFileSync(`src/pages/${p}`, 'utf8')
  assert.match(src('Playbooks.tsx'), /runsHref\(`playbook:\$\{p\.id\}`\)/)
  assert.match(src('Watches.tsx'), /runsHref\(`watch:\$\{w\.id\}`\)/)
  assert.match(src('Insights.tsx'), /runsHref\('playbook:explore_data'\)/)
  assert.match(src('InsightsPage.tsx'), /runsHref\('playbook:explore_data'\)/)
})

test('the Runs page never uses gold', () => {
  const css = readFileSync('src/pages/runs.css', 'utf8')
  const page = readFileSync('src/pages/Runs.tsx', 'utf8')
  assert.doesNotMatch(css, /--owed/)
  assert.doesNotMatch(page, /useOwed|pill-owed|btn-owed|card-owed|--owed|tone="owed"/)
})

// Quick 013: the nightly forecast step has its own filter and a readable label
test('filter bar offers Forecasts and forecast refs read as words', () => {
  const out = html(
    <FilterBar filters={{ kind: 'forecasts', status: '', since: '', ref: 'forecast:3' }} onChange={() => {}} />,
  )
  assert.match(out, /aria-pressed="true"[^>]*>Forecasts/)
  assert.match(out, /Only forecast 3/)
  assert.equal(activityPath(readFilters(new URLSearchParams('kind=forecasts'))), '/api/activity?kind=forecasts&limit=50')
})

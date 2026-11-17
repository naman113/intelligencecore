import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ReactDOMServer from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { InsightsView, SummaryStrip, sortInsights } from '../src/pages/InsightsPage'
import { insightWords, normalizeInsight, type Insight } from '../src/lib/contracts'
import { alertKicker, caseOwed, isUnseenAlert } from '../src/lib/owed'
import { phase7Insights } from './fixtures'

const render = (
  result: { available: boolean; items: Insight[] },
  opts: { onDismiss?: (i: Insight) => void; filtered?: boolean } = {},
) =>
  ReactDOMServer.renderToStaticMarkup(
    <MemoryRouter>
      <InsightsView result={result} onMakeKpi={() => {}} onDismiss={opts.onDismiss} filtered={opts.filtered} />
    </MemoryRouter>,
  )

const one = (raw: Record<string, unknown>) => normalizeInsight({ title: 'T', finding: 'F.', ...raw })!

test('unavailable is calm, not an error', () => {
  const html = render({ available: false, items: [] })
  assert.match(html, /Insights appear after the nightly exploration playbook runs/)
  assert.match(html, /href="\/playbooks"/)
  assert.ok(!html.includes('banner-broken'))
})

test('available but empty, and empty under a filter', () => {
  assert.match(render({ available: true, items: [] }), /Nothing noticed yet/)
  assert.match(render({ available: true, items: [] }, { filtered: true }), /Nothing matches these filters/)
})

test('SQL is escaped text in a pre, inside a closed Evidence section', () => {
  const i = normalizeInsight({ id: 1, title: 'T', finding: 'b', evidence_sql: 'SELECT "<b>"' })!
  const html = render({ available: true, items: [i] })
  assert.match(html, /<pre[^>]*>SELECT &quot;&lt;b&gt;&quot;<\/pre>/)
  assert.ok(!html.includes('<b>'))
  assert.match(html, /<details class="ins-evidence"><summary>Evidence<\/summary>/)
  assert.ok(!/<details[^>]*open/.test(html))
  assert.match(html, />Copy</)
})

test('phase 7 envelope renders case and kpi links, severity as a marker', () => {
  const items = phase7Insights.items.map(normalizeInsight).filter((x): x is Insight => !!x)
  const html = render({ available: true, items })
  assert.match(html, /<a class="ins-case" title="Exploration 2026-10-24" href="\/cases\/9">Case #9<\/a>/)
  assert.match(html, /<a href="\/kpis\/4">Overdue invoices<\/a>/)
  assert.match(html, /<span class="pill pill-broken">Critical<\/span>/)
  assert.match(html, /<span class="pill pill-warn">Warning<\/span>/)
  assert.match(html, /class="ins-card ins-sev-critical"/)
  assert.match(html, /Make this a KPI/)
  assert.match(html, /SELECT region, sum\(total\)/)
  // Positional sample rows still become a table under their column names.
  assert.match(html, /<th>region<\/th><th>sum<\/th>/)
  assert.match(html, /<td>South<\/td><td>100<\/td>/)
  assert.match(html, /4 rows/)
})

test('info is neutral; warning is not gold', () => {
  const html = render({ available: true, items: [one({ id: 'a', severity: 'info' })] })
  assert.match(html, /<span class="pill">Info<\/span>/)
  const css = readFileSync('src/pages/ic.css', 'utf8').split(/\r?\n/).filter((l) => l.includes('.ins-'))
  const src = readFileSync('src/pages/InsightsPage.tsx', 'utf8') + css.join('\n')
  assert.ok(css.length > 10)
  assert.ok(!/card-owed|--owed|pill-owed/.test(src))
})

test('plain words: what we found, why it matters, next step, key figures', () => {
  const i = one({
    id: 'n',
    title: 'South orders fell 30% in October',
    finding: 'Orders fell from 300 to 210. Two accounts stopped ordering. Caveat: October has one fewer working day.',
    summary: 'South orders fell by almost a third.',
    impact: 'About 90 fewer orders a month.',
    next_step: 'Call the two accounts.',
    key_figures: [{ label: 'Orders in Oct', value: '210' }, { label: 'Change', value: -30 }],
    severity: 'critical',
  })
  const html = render({ available: true, items: [i] })
  assert.match(html, /<h3 class="ins-title">South orders fell 30% in October<\/h3>/)
  assert.match(html, /<dt>What we found<\/dt><dd>South orders fell by almost a third\.<\/dd>/)
  assert.match(html, /<dt>Why it matters<\/dt><dd>About 90 fewer orders a month\.<\/dd>/)
  assert.match(html, /<dt>Suggested next step<\/dt><dd>Call the two accounts\.<\/dd>/)
  assert.match(html, /<span class="ins-fig-label">Orders in Oct<\/span><span class="ins-fig-value">210<\/span>/)
  assert.match(html, /<span class="ins-fig-value">-30<\/span>/)
  assert.match(html, /<p class="ins-caveat">Caveat: October has one fewer working day\.<\/p>/)
  // The full finding is still there, in the evidence.
  assert.match(html, /Two accounts stopped ordering/)
})

test('fallback: the first sentence of the finding, the caveat split off', () => {
  const i = one({ finding: 'Late payers rose to 14. Most are in the North. Caveat: 3 invoices are disputed.' })
  assert.deepEqual(insightWords(i), { summary: 'Late payers rose to 14.', caveat: '3 invoices are disputed.' })
  const html = render({ available: true, items: [i] })
  assert.match(html, /<dt>What we found<\/dt><dd>Late payers rose to 14\.<\/dd>/)
  assert.ok(!html.includes('Why it matters'))
  assert.deepEqual(insightWords(one({ finding: 'Just one.' })), { summary: 'Just one.', caveat: null })
  assert.equal(insightWords(one({ finding: 'Value 1.5 kept. Next.' })).summary, 'Value 1.5 kept.')
})

test('case with status; Dismiss only while the case is open', () => {
  const open = one({ id: 'o', case_id: 4, case_status: 'open' })
  const closed = one({ id: 'c', case_id: 5, case_status: 'closed' })
  const none = one({ id: 'x' })
  const html = render({ available: true, items: [open] }, { onDismiss: () => {} })
  assert.match(html, /Case #4 · open/)
  assert.match(html, />Dismiss</)
  const h2 = render({ available: true, items: [closed, none] }, { onDismiss: () => {} })
  assert.match(h2, /Case #5 · closed/)
  assert.ok(!h2.includes('>Dismiss<'))
  assert.equal((h2.match(/ins-case/g) ?? []).length, 1)
  assert.match(render({ available: true, items: [one({ case_id: 6, case_status: 'pending_ack' })] }), /Case #6 · waiting for you/)
})

test('newest day first, then the most severe, then newest', () => {
  const items = [
    one({ id: 'old-crit', severity: 'critical', created_at: '2026-10-20T09:00:00Z' }),
    one({ id: 'new-info', severity: 'info', created_at: '2026-10-24T11:00:00Z' }),
    one({ id: 'new-warn', severity: 'warning', created_at: '2026-10-24T10:00:00Z' }),
    one({ id: 'new-crit', severity: 'critical', created_at: '2026-10-24T09:00:00Z' }),
  ]
  assert.deepEqual(sortInsights(items).map((i) => i.key), ['new-crit', 'new-warn', 'new-info', 'old-crit'])
})

test('summary strip: counts by severity, open cases, last run', () => {
  const html = ReactDOMServer.renderToStaticMarkup(
    <SummaryStrip
      s={{ days: 30, bySeverity: { critical: 2, warning: 3, info: 5 }, openCases: 1, lastRun: { id: 1, status: 'failed', startedAt: '2026-10-24T02:00:00Z', endedAt: null, error: null }, playbook: null }}
    />,
  )
  assert.match(html, /<b>2<\/b> critical/)
  assert.match(html, /<b>3<\/b> warning/)
  assert.match(html, /<b>5<\/b> info/)
  assert.match(html, /<b>1<\/b> open case</)
  assert.match(html, /Last 30 days · last exploration .*\(failed\)/)
  const none = ReactDOMServer.renderToStaticMarkup(
    <SummaryStrip s={{ days: 30, bySeverity: { critical: 0, warning: 0, info: 0 }, openCases: 0, lastRun: null, playbook: null }} />,
  )
  assert.match(none, /no exploration run yet/)
  assert.match(none, /open cases/)
})

test('critical insight cases are owed until seen; warning ones are not', () => {
  const base = { id: 1, title: 'x', status: 'open' as const, summary: null, close_reason: null, created_at: '', closed_at: null, open_exp: 0, violated_exp: 0, satisfied_exp: 0 }
  const crit = { ...base, origin: 'insight' as const, links: { severity: 'critical' }, seen_at: null }
  assert.equal(isUnseenAlert(crit), true)
  assert.equal(caseOwed(crit), true)
  assert.equal(alertKicker(crit), 'Critical insight')
  assert.equal(isUnseenAlert({ ...crit, seen_at: '2026-10-25T00:00:00Z' }), false)
  assert.equal(isUnseenAlert({ ...crit, links: { severity: 'warning' } }), false)
  assert.equal(isUnseenAlert({ ...crit, status: 'closed' as const }), false)
  assert.equal(isUnseenAlert({ ...base, origin: 'chat' as const, seen_at: null }), false)
  assert.equal(alertKicker({ origin: 'watch', fire_count: 3 }), 'KPI alert · fired 3 times')
})

test('uses the asks template; dismiss uses the existing case route', () => {
  const src = readFileSync('src/pages/InsightsPage.tsx', 'utf8')
  assert.ok(src.includes('asks.insightToKpi'))
  assert.ok(src.includes('/api/cases/${i.caseId}/dismiss'))
})

test('phase 17: Unverified chip on raw-SQL insights; measure insights show their measure', () => {
  const raw = one({ id: 'r', lane: 'raw_sql', verified: false })
  const meas = one({ id: 'm', lane: 'measure', verified: true, measure: 'revenue' })
  const old = one({ id: 'o' })
  assert.match(render({ available: true, items: [raw] }), /title="Found with raw SQL, not a governed measure">Unverified</)
  const hm = render({ available: true, items: [meas] })
  assert.match(hm, /Measure: revenue/)
  assert.ok(!hm.includes('Unverified'))
  assert.ok(!render({ available: true, items: [old] }).includes('Unverified'))
})

import { test } from 'node:test'
import assert from 'node:assert/strict'
import ReactDOMServer from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { CaseRow, ExpectationRow, OriginLinks, isFindingCase, noteEvidence, originLabel, startedIn, timelineLabel } from '../src/pages/Cases'
import { OwedColumnView } from '../src/components/OwedColumn'
import { caseOwed, computeOwed, isUnseenAlert, type OwedRaw } from '../src/lib/owed'
import type { CaseSummary, Expectation, TimelineEvent } from '../src/lib/types'

/* Quick 006: cases from findings on the Cases page and in the owed column. */

const base: CaseSummary = {
  id: 7, title: 'KPI alert: Daily revenue below 50k', status: 'open', summary: 'Fired once.',
  close_reason: null, created_at: new Date().toISOString(), closed_at: null,
  open_exp: 0, violated_exp: 0, satisfied_exp: 0,
}
const alert = (over: Partial<CaseSummary> = {}): CaseSummary => ({ ...base, origin: 'watch', seen_at: null, fire_count: 1, ...over })

const html = (el: JSX.Element) => ReactDOMServer.renderToStaticMarkup(<MemoryRouter>{el}</MemoryRouter>)

test('only an open, unseen KPI-alert case is owed among finding cases', () => {
  assert.equal(isUnseenAlert(alert()), true)
  assert.equal(caseOwed(alert()), true)
  assert.equal(caseOwed(alert({ seen_at: '2026-10-25T10:00:00Z' })), false)
  assert.equal(caseOwed(alert({ status: 'closed' })), false)
  assert.equal(caseOwed({ ...base, origin: 'insight' }), false)
  assert.equal(caseOwed({ ...base, origin: 'chat' }), false)
  assert.equal(caseOwed({ ...base, origin: null, violated_exp: 1 }), true)
  assert.equal(caseOwed({ ...base, status: 'pending_ack' }), true)
})

const raw = (over: Partial<OwedRaw> = {}): OwedRaw => ({
  actions: [], chats: [], pendingChats: [], ack: [], violatedCases: [], unmatched: [],
  missingGroups: [], onboarding: null, sources: null, rules: null, dashboards: [], ...over,
})

test('alert cases count once in cases and total, in both profiles', () => {
  for (const profile of ['intelligence', 'second_brain'] as const) {
    const o = computeOwed(raw({ alertCases: [alert(), alert({ id: 8 })] }), profile)
    assert.equal(o.alertCases.length, 2)
    assert.equal(o.counts.cases, 2)
    assert.equal(o.counts.total, 2)
  }
  const none = computeOwed(raw(), 'intelligence')
  assert.equal(none.alertCases.length, 0)
  assert.equal(none.counts.total, 0)
})

test('one gold card per alert case with Open case and Got it', () => {
  const owed = { ...computeOwed(raw({ alertCases: [alert({ fire_count: 3 })] }), 'intelligence'), loading: false, reload: async () => {} }
  const out = html(
    <OwedColumnView owed={owed} base={null} configured={false} onDecide={() => {}} onNavigate={() => {}} onAsk={() => {}} onSeen={() => {}} />,
  )
  const cards = out.split('<article').slice(1)
  assert.equal(cards.length, 1)
  assert.match(cards[0], /data-owed="case-alert"/)
  assert.match(cards[0], /KPI alert · fired 3 times/)
  assert.match(cards[0], />Daily revenue below 50k</)
  assert.match(cards[0], />Open case</)
  assert.match(cards[0], />Got it</)
  assert.match(cards[0], /closes by\s+itself/)
})

test('case rows show origin and gold only when owed', () => {
  const gold = html(<CaseRow c={alert({ fire_count: 2 })} selected={false} onOpen={() => {}} />)
  assert.match(gold, /is-owed/)
  assert.match(gold, />KPI alert</)
  assert.match(gold, /fired 2 times/)
  assert.match(gold, /not seen yet/)
  const insight = html(<CaseRow c={{ ...base, title: 'South orders fell 30%', origin: 'insight' }} selected={false} onOpen={() => {}} />)
  assert.ok(!insight.includes('is-owed'))
  assert.match(insight, />Insight</)
  const triage = html(<CaseRow c={{ ...base, title: 'Send the invoice' }} selected={false} onOpen={() => {}} />)
  assert.ok(!triage.includes('class="pill">'), 'a message case keeps its old row (no origin pill)')
})

test('origin labels and finding-case detection', () => {
  assert.equal(originLabel('watch'), 'KPI alert')
  assert.equal(originLabel('insight'), 'Insight')
  assert.equal(originLabel('chat'), 'From chat')
  assert.equal(originLabel('exploration'), 'Exploration notes')
  assert.equal(originLabel(null), null)
  assert.equal(isFindingCase('watch') && isFindingCase('insight') && isFindingCase('chat'), true)
  assert.equal(isFindingCase('exploration') || isFindingCase(null), false)
})

test('timeline entries are labelled by what they are', () => {
  const ev = (over: Partial<TimelineEvent>): TimelineEvent => ({
    id: 1, source: 'system', content: 'x', occurred_at: '2026-10-25T10:00:00Z', provenance: 'self', actor: null, ...over,
  })
  assert.equal(timelineLabel(ev({ kind: 'watch' })), 'Alert fired')
  assert.equal(timelineLabel(ev({ kind: 'investigation' })), 'Investigation')
  assert.equal(timelineLabel(ev({ kind: 'closing' })), 'Closed')
  assert.equal(timelineLabel(ev({ kind: 'manual' })), 'Your note')
  assert.equal(timelineLabel(ev({ kind: 'something-new' }), 'Intelligence Corner'), 'Intelligence Corner')
  assert.equal(timelineLabel(ev({ provenance: 'external', actor: 'Priya', kind: 'triage note' })), 'Priya')
})

test('a watch case links back to its KPI and says how close it is to closing', () => {
  const watch = { id: 12, title: 'Daily revenue', kind: 'kpi', status: 'active', normal_streak: 1, recovery_checks: 3 }
  const out = html(<OriginLinks origin="watch" links={{ watch_id: 12 }} watch={watch} open />)
  assert.match(out, /href="\/kpis\/12"/)
  assert.match(out, /KPI #12: Daily revenue/)
  assert.match(out, /Back to normal for 1 check of 3; it closes itself at 3\./)
  const crossed = html(<OriginLinks origin="watch" links={{ watch_id: 12 }} watch={{ ...watch, normal_streak: 0 }} open />)
  assert.match(crossed, /Still past its alert line\. It closes itself after 3 normal checks in a row\./)
  const gone = html(<OriginLinks origin="watch" links={{ watch_id: 12, watch_title: 'Daily revenue', watch_kind: 'kpi' }} watch={null} open={false} />)
  assert.match(gone, /The watch was deleted\./)
  assert.match(gone, /KPI #12/)
})

test('insight, dashboard and chat links; nothing for a message case', () => {
  const ins = html(<OriginLinks origin="insight" links={{ insight_key: 'note:41' }} watch={null} open />)
  assert.match(ins, /href="\/insights"/)
  assert.match(ins, /Insight note:41/)
  const chat = html(
    <OriginLinks origin="chat" links={{ dashboard_slug: 'sales', thread_id: 'ui:owner' }} watch={null} open onOpenChat={() => {}} />,
  )
  assert.match(chat, /href="\/dashboards\/sales"/)
  assert.match(chat, />Open the chat</)
  assert.equal(html(<OriginLinks origin={null} links={{}} watch={null} open />), '')
})

test('agent-written titles and summaries are escaped', () => {
  const out = html(<CaseRow c={alert({ title: '<img src=x onerror=alert(1)>' })} selected={false} onOpen={() => {}} />)
  assert.ok(!out.includes('<img'))
})

test('finding notes fold their query and sample away; other notes do not', () => {
  const t = {
    id: 1, source: 'exploration', provenance: 'self' as const, actor: null, occurred_at: '2026-10-24T06:00:00Z',
    kind: 'exploration',
    content: 'Late payers up' + String.fromCharCode(10, 10) + 'Up 12%.' + String.fromCharCode(10, 10) + 'Evidence SQL:' + String.fromCharCode(10) + 'SELECT 1',
    meta: { evidence_sql: 'SELECT 1', evidence: { row_count: 2, columns: ['n'], sample: [{ n: 1 }] } },
  }
  const ev = noteEvidence(t)!
  assert.equal(ev.sql, 'SELECT 1')
  assert.ok(!ev.text!.includes('Evidence SQL'))
  assert.equal(ev.evidence!.rowCount, 2)
  assert.equal(noteEvidence({ ...t, kind: 'watch' }), null)
  assert.equal(noteEvidence({ ...t, meta: null }), null)
})

/* Quick 024: where a case started is named, never the raw number or JID; a request the
 * assistant owes names the assistant instead of "nobody assigned". */

test('the case header names the chat, never its number or JID', () => {
  assert.equal(startedIn({ origin_chat_jid: '15550100001', origin_chat_name: 'WhatsApp · Asha' }), 'WhatsApp · Asha')
  assert.equal(startedIn({ origin_chat_jid: '120363000000000001@g.us', origin_chat_name: 'WhatsApp · Site B Ops' }), 'WhatsApp · Site B Ops')
  // an older gateway (no name) or a lookup that found nothing: the channel only
  assert.equal(startedIn({ origin_chat_jid: '15550100001' }), 'WhatsApp chat')
  assert.equal(startedIn({ origin_chat_jid: '15550100001', origin_chat_name: '  ' }), 'WhatsApp chat')
  assert.equal(startedIn({ origin_chat_jid: '120363000000000001@g.us', origin_chat_name: null }), 'WhatsApp group')
  assert.ok(!/[0-9]{6}/.test(startedIn({ origin_chat_jid: '99887766554433@lid' })))
})

const exp = (over: Partial<Expectation> = {}): Expectation => ({
  id: 5, description: 'Find the engine specs on the web', status: 'open', due_at: null,
  reminder_count: 0, satisfy_hint: null, owner: null, ...over,
})

test('an assistant commitment names the assistant, a person keeps their name', () => {
  const mine = html(<ExpectationRow exp={exp({ owed_by: 'assistant' })} brand="Intelligence Corner" onDismiss={() => {}} pending={false} />)
  assert.match(mine, /data-owed-by="assistant"[^>]*>Intelligence Corner</)
  assert.ok(!mine.includes('nobody assigned'))
  const met = html(<ExpectationRow exp={exp({ owed_by: 'assistant', status: 'satisfied' })} brand="Second Brain" onDismiss={() => {}} pending={false} />)
  assert.match(met, />Second Brain</)
  assert.ok(!met.includes('Stop chasing'), 'nothing to stop once it is met')
  const person = html(<ExpectationRow exp={exp({ owner: 'Vinu' })} onDismiss={() => {}} pending={false} />)
  assert.match(person, />Vinu</)
  const nobody = html(<ExpectationRow exp={exp()} onDismiss={() => {}} pending={false} />)
  assert.match(nobody, /nobody assigned/)
})

import { test } from 'node:test'
import assert from 'node:assert/strict'
import ReactDOMServer from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { OwedColumnView, canApproveDraft } from '../src/components/OwedColumn'
import { computeOwed, type OwedRaw } from '../src/lib/owed'
import type { ContextSource } from '../src/lib/ictypes'
import { action, dashboards, pubBare, pubWithUrl, taskAction } from './fixtures'

const empty: OwedRaw = {
  actions: [],
  chats: [],
  pendingChats: [],
  ack: [],
  violatedCases: [],
  unmatched: [],
  missingGroups: [],
  onboarding: null,
  sources: null,
  rules: null,
  dashboards: [],
}

function render(over: Partial<OwedRaw>, o: { base?: string | null } = {}) {
  const owed = { ...computeOwed({ ...empty, ...over }, 'intelligence'), loading: false, reload: async () => {} }
  return ReactDOMServer.renderToStaticMarkup(
    <MemoryRouter>
      <OwedColumnView
        owed={owed}
        base={o.base === undefined ? 'https://d.x' : o.base}
        configured={false}
        onDecide={() => {}}
        onNavigate={() => {}}
        onAsk={() => {}}
      />
    </MemoryRouter>,
  )
}

const cards = (html: string) => html.split('<article').slice(1)

test('80 context proposals in 6 tables render one grouped card', () => {
  const acts = Array.from({ length: 80 }, (_, n) =>
    action(1000 + n, 'context_change', { op: 'propose', kind: 'column', ref: `w:public.t${n % 6}.c${n}`, text: 'x' }),
  )
  const html = render({ actions: acts, sources: [{ name: 'w', pending: 80 }] as unknown as ContextSource[] })
  const cs = cards(html)
  assert.equal(cs.length, 1)
  assert.match(cs[0], /data-owed="context"/)
  assert.match(html, /80 descriptions waiting across 6 tables/)
  assert.match(html, /Review on Schema/)
  assert.ok(!html.includes('Review rules'))
})

test('draft card: approve, reject, open draft link only when usable', () => {
  const html = render({ actions: [pubWithUrl] })
  assert.match(html, /data-owed="draft"/)
  assert.match(html, /<button[^>]*disabled=""[^>]*>Approve/)
  assert.match(html, /Reject/)
  assert.match(html, /href="https:\/\/d\.x\/d\/sales\/v\/4\/\?p=seedtoken"/)
  assert.match(html, /rel="noopener noreferrer"/)
  assert.match(html, /target="_blank"/)
  const bare = render({ actions: [pubBare] })
  assert.ok(!bare.includes('?p='))
  assert.ok(!bare.includes('Open draft'))
  assert.ok(!bare.includes('Approve'))
  assert.match(bare, /Reject/)
  assert.match(bare, /<a[^>]*href="\/dashboards\/sales"[^>]*>Review</)
})

test('broken dashboard card', () => {
  const broken = { ...dashboards[0], current_last_error: 'relation "x" does not exist ' + 'y'.repeat(400) }
  const html = render({ dashboards: [broken] })
  assert.match(html, /data-owed="broken-dashboard"/)
  assert.match(html, /Ask the agent to fix/)
  assert.match(html, />Open</)
  assert.ok(html.includes('…'))
})

test('every card has data-owed and a button, in the documented order', () => {
  const html = render({
    actions: [taskAction, pubWithUrl, action(5, 'context_change', { op: 'propose', kind: 'table', ref: 'w:public.t', text: 'x' })],
    dashboards: [{ ...dashboards[0], current_last_error: 'boom' }],
    missingGroups: ['llm'],
    onboarding: { status: 'active' } as never,
    pendingChats: [{ jid: 'a', status: 'pending', subject: 'Team' } as never],
    ack: [{ id: 1, title: 'Case', summary: null } as never],
    violatedCases: [{ id: 2, title: 'Late', violated_exp: 1 } as never],
    alertCases: [{ id: 3, title: 'KPI alert: Revenue', status: 'open', origin: 'watch', summary: null } as never],
    unmatched: [{ actor_raw: '+1', event_count: 2 } as never],
  })
  const cs = cards(html)
  const kinds = cs.map((c) => /data-owed="([^"]+)"/.exec(c)?.[1])
  assert.deepEqual(kinds, [
    'setup', 'interview', 'case-alert', 'draft', 'context', 'broken-dashboard', 'approval', 'chat', 'case-ack', 'case-missed', 'unknown-sender',
  ])
  for (const c of cs) assert.match(c, /<button/)
})

test('nothing owed renders the neutral state', () => {
  const html = render({})
  assert.match(html, /Nothing waiting on you/)
  assert.ok(!html.includes('card-owed'))
})

test('agent text is escaped', () => {
  const evil = action(9, 'publish_dashboard', {
    slug: 'sales', version: 2, title: '<img src=x onerror=alert(1)>', change_note: '<script>alert(2)</script>',
  })
  const html = render({
    actions: [evil],
    dashboards: [{ ...dashboards[0], current_last_error: '<b>bad</b>' }],
  })
  assert.ok(!html.includes('<img'))
  assert.ok(!html.includes('<script'))
  assert.ok(!html.includes('<b>bad'))
  assert.match(html, /&lt;script&gt;/)
})

test('UI-12: WhatsApp gold card only when failing', () => {
  assert.ok(!render({}).includes('WhatsApp connection failing'))
  const html = render({ whatsappFailing: true, whatsappDetail: 'HTTP 401' })
  assert.match(html, /data-owed="whatsapp"/)
  assert.match(html, /WhatsApp connection failing/)
  assert.match(html, /HTTP 401/)
})

test('WR-15: canApproveDraft needs a link and an opened id', () => {
  assert.equal(canApproveDraft(null, new Set([1]), 1), false)
  assert.equal(canApproveDraft('https://d.x/y', new Set(), 1), false)
  assert.equal(canApproveDraft('https://d.x/y', new Set([1]), 1), true)
})

test('Phase 13.1: a failing forecast is one card and hides the empty state', () => {
  const html = render({ forecasts: [{ id: 4, name: 'Daily diesel', status: 'active', last_error: 'no rows' } as never] })
  const cs = cards(html)
  assert.equal(cs.length, 1)
  assert.match(cs[0], /data-owed="failing-forecast"/)
  assert.match(html, /Forecast failing/)
  assert.match(html, /Daily diesel/)
  assert.match(html, /href="\/forecasts\/4"/)
  assert.match(html, /Ask the agent why/)
  assert.doesNotMatch(html, /Nothing waiting on you/)
})

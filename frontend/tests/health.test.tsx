import { test } from 'node:test'
import assert from 'node:assert/strict'
import ReactDOMServer from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { OwedColumnView } from '../src/components/OwedColumn'
import { computeOwed, type OwedRaw } from '../src/lib/owed'
import { loadHealthAlerts, type HealthAlert } from '../src/lib/health'
import { taskAction } from './fixtures'

const empty: OwedRaw = {
  actions: [], chats: [], pendingChats: [], ack: [], violatedCases: [], unmatched: [],
  missingGroups: [], onboarding: null, sources: null, rules: null, dashboards: [],
}
const alert = (over: Partial<HealthAlert> = {}): HealthAlert => ({
  id: 1, component: 'sentinel', title: 'The sentinel has stopped', detail: 'No heartbeat for 5 minutes',
  created_at: '2026-10-25T10:00:00Z', resolved_at: null, ...over,
})
const view = (raw: OwedRaw, profile: 'intelligence' | 'second_brain' = 'intelligence') => {
  const owed = { ...computeOwed(raw, profile), loading: false, reload: async () => {} }
  return ReactDOMServer.renderToStaticMarkup(
    <MemoryRouter>
      <OwedColumnView owed={owed} base={null} configured={false} onDecide={() => {}} onNavigate={() => {}} onAsk={() => {}} />
    </MemoryRouter>,
  )
}

for (const p of ['intelligence', 'second_brain'] as const) {
  test(`computeOwed counts health alerts (${p})`, () => {
    const a = computeOwed({ ...empty, actions: [taskAction], healthAlerts: [alert()] }, p)
    const b = computeOwed({ ...empty, actions: [taskAction] }, p)
    assert.equal(a.healthAlerts?.length, 1)
    assert.equal(a.counts.health, 1)
    assert.equal(a.counts.total, b.counts.total + 1)
  })
}

test('old literal without healthAlerts gives none', () => {
  const o = computeOwed(empty, 'intelligence')
  assert.deepEqual(o.healthAlerts, [])
  assert.equal(o.counts.health, 0)
})

test('health card renders first, gold kicker, button, no empty state', () => {
  const html = view({ ...empty, missingGroups: ['llm'], healthAlerts: [alert()] })
  assert.match(html, /data-owed="health"/)
  assert.ok(html.indexOf('data-owed="health"') < html.indexOf('data-owed="setup"'))
  assert.equal(html.match(/data-owed="/)?.index, html.indexOf('data-owed="health"'))
  assert.match(html, /System health/)
  assert.match(html, /The sentinel has stopped/)
  assert.match(html, /No heartbeat for 5 minutes/)
  assert.match(html, /Open health/)
  assert.doesNotMatch(html, /Nothing waiting on you/)
})

test('no alerts and nothing owed keeps the empty state', () => {
  const html = view(empty)
  assert.match(html, /Nothing waiting on you/)
  assert.doesNotMatch(html, /data-owed="health"/)
})

test('alert title is escaped', () => {
  const html = view({ ...empty, healthAlerts: [alert({ title: '<script>x()</script>' })] })
  assert.doesNotMatch(html, /<script>x/)
  assert.match(html, /&lt;script&gt;/)
})

test('loadHealthAlerts settles to [] when the API rejects', async () => {
  const g = globalThis as { fetch?: unknown }
  const orig = g.fetch
  g.fetch = async () => { throw new Error('down') }
  try {
    assert.deepEqual(await loadHealthAlerts(), [])
  } finally {
    g.fetch = orig
  }
})

test('health card separator is a real middle dot', () => {
  const html = view({ ...empty, healthAlerts: [alert()] })
  assert.ok(html.includes('· since '))
  assert.ok(!html.includes('�'))
})

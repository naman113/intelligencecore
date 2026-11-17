import { test } from 'node:test'
import assert from 'node:assert/strict'
import { computeOwed, loadOwed, type OwedRaw } from '../src/lib/owed'
import { action, dashboards, ctxRules, pubWithUrl, taskAction } from './fixtures'
import type { ContextSource } from '../src/lib/ictypes'

const chat = (jid: string, status: string) => ({ jid, status }) as never
const kase = (id: number, violated = 0) => ({ id, violated_exp: violated }) as never

function raw(over: Partial<OwedRaw> = {}): OwedRaw {
  return {
    actions: [taskAction],
    chats: [chat('a', 'pending'), chat('b', 'active')],
    pendingChats: [chat('a', 'pending')],
    ack: [kase(1)],
    violatedCases: [kase(2, 1)],
    unmatched: [{ id: 1 } as never],
    missingGroups: ['llm'],
    onboarding: { status: 'active' } as never,
    sources: null,
    rules: null,
    dashboards: [],
    ...over,
  }
}

const ctx = (id: number, kind: string, ref: string) =>
  action(id, 'context_change', { op: 'propose', kind, ref, text: 'x' })

test('second_brain counts match the legacy formulas and new fields are inert', () => {
  const r = raw({ actions: [taskAction, pubWithUrl, ctx(1, 'table', 'w:public.t')], dashboards })
  const o = computeOwed(r, 'second_brain')
  const legacyTotal = r.actions.length + 1 + 1 + 1 + 1 + 1 + 1
  assert.equal(o.counts.approvals, 3)
  assert.equal(o.counts.chats, 1)
  assert.equal(o.counts.cases, 2)
  assert.equal(o.counts.people, 1)
  assert.equal(o.counts.setup, 1)
  assert.equal(o.counts.onboarding, 1)
  assert.equal(o.counts.total, legacyTotal)
  assert.equal(o.contextProposals.length, 0)
  assert.equal(o.dashboardDrafts.length, 0)
  assert.equal(o.brokenDashboards.length, 0)
  assert.equal(o.counts.schema + o.counts.rules + o.counts.dashboards + o.counts.approvalsAll, 0)
})

test('intelligence: exact context counts, drafts and broken dashboards', () => {
  const proposals = [
    ctx(1, 'table', 'w:public.a'),
    ctx(2, 'column', 'w:public.a.id'),
    ctx(3, 'column', 'w:public.b.id'),
    ctx(4, 'rule', 'rule:margin'),
  ]
  const sources = [{ name: 'w', pending: 150 }] as unknown as ContextSource[]
  const rules = [{ ...ctxRules[0], pending: { action_id: 4 } }] as never
  const broken = { ...dashboards[0], current_last_error: 'boom' }
  const o = computeOwed(
    raw({ actions: [taskAction, pubWithUrl, ...proposals], sources, rules, dashboards: [broken, dashboards[1]] }),
    'intelligence',
  )
  assert.equal(o.contextProposals.length, 4)
  assert.equal(o.dashboardDrafts.length, 1)
  assert.equal(o.brokenDashboards.length, 1)
  assert.equal(o.contextPendingExact, 150)
  assert.equal(o.rulesPendingExact, 1)
  assert.equal(o.contextTables, 2)
  assert.equal(o.counts.schema, 149)
  assert.equal(o.counts.rules, 1)
  assert.equal(o.counts.dashboards, 2)
  assert.equal(o.counts.approvalsAll, 2 + 150)
  assert.equal(o.counts.total, 152 + 1 + 1 + 1 + 1 + 1 + 1 + 1)
})

test('intelligence: null sources and rules fall back to the action list', () => {
  const o = computeOwed(
    raw({ actions: [ctx(1, 'table', 'w:public.a'), ctx(2, 'kpi', 'kpi:dso')] }),
    'intelligence',
  )
  assert.equal(o.contextPendingExact, 2)
  assert.equal(o.rulesPendingExact, 1)
  assert.equal(o.counts.schema, 1)
  assert.equal(o.counts.approvalsAll, 2)
})

function stubFetch(fail: (url: string) => boolean = () => false, bodyFor?: (url: string) => unknown) {
  const urls: string[] = []
  const g = globalThis as unknown as { fetch: unknown }
  const prev = g.fetch
  g.fetch = async (input: unknown) => {
    const url = String(input)
    urls.push(url)
    if (fail(url)) return new Response('nope', { status: 500 })
    let body: unknown = bodyFor ? bodyFor(url) : undefined
    if (body !== undefined) return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
    body = []
    if (url.includes('/api/chats')) body = { chats: [] }
    else if (url.includes('/api/config/status')) body = {}
    else if (url.includes('/api/onboarding')) body = null
    else if (url.includes('/api/context/sources')) body = { sources: [] }
    else if (url.includes('/api/context/rules')) body = { rules: [] }
    return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
  }
  return { urls, restore: () => void (g.fetch = prev) }
}

test('second_brain makes exactly the seven legacy requests plus health', async () => {
  const s = stubFetch()
  try {
    await loadOwed('second_brain')
  } finally {
    s.restore()
  }
  assert.equal(s.urls.length, 8) // Phase 11: + /api/health/alerts
  assert.ok(s.urls.includes('/api/actions?status=proposed&limit=100'))
  assert.ok(!s.urls.some((u) => u.includes('/api/context') || u.includes('/api/dashboards')))
})

test('intelligence adds three requests and survives their failure', async () => {
  const s = stubFetch((u) => u.includes('/api/context') || u.includes('/api/dashboards'))
  let res: OwedRaw
  try {
    res = await loadOwed('intelligence')
  } finally {
    s.restore()
  }
  assert.equal(s.urls.length, 13) // Phase 11: + /api/health/alerts; Phase 13.1: + /api/forecasts
  assert.ok(s.urls.includes('/api/actions?status=proposed&exclude_kind=context_change&limit=100'))
  assert.ok(s.urls.includes('/api/actions?status=proposed&kind=context_change&limit=100'))
  assert.ok(!s.urls.includes('/api/actions?status=proposed&limit=100'))
  assert.ok(s.urls.some((u) => u.includes('/api/context/sources')))
  assert.ok(s.urls.some((u) => u.includes('/api/context/rules?status=all&limit=1000')))
  assert.ok(s.urls.some((u) => u.includes('/api/dashboards?limit=100')))
  assert.equal(res.sources, null)
  assert.equal(res.rules, null)
  assert.deepEqual(res.dashboards, [])
})

const pubDraft = action(5, 'publish_dashboard', { slug: 'sales', draft_url: '/d/sales/v/4/?p=t' })

test('WR-01: 100 newer context proposals do not crowd out a dashboard draft', async () => {
  const ctxRows = Array.from({ length: 100 }, (_, i) => ctx(1000 + i, 'column', 'w:public.t.c' + i))
  const s = stubFetch(
    () => false,
    (u) =>
      u.includes('exclude_kind=context_change') ? [pubDraft] : u.includes('kind=context_change') ? ctxRows : undefined,
  )
  let res: OwedRaw
  try {
    res = await loadOwed('intelligence')
  } finally {
    s.restore()
  }
  const o = computeOwed(res, 'intelligence')
  assert.ok(o.dashboardDrafts.some((a) => a.id === 5))
  assert.ok(o.counts.dashboards >= 1)
})

test('older gateway ignoring filters: merged actions are deduped and id-descending', async () => {
  const mixed = [pubDraft, ctx(9, 'table', 'w:public.t'), taskAction]
  const s = stubFetch(() => false, (u) => (u.includes('/api/actions') ? mixed : undefined))
  let res: OwedRaw
  try {
    res = await loadOwed('intelligence')
  } finally {
    s.restore()
  }
  const ids = res.actions.map((a) => a.id)
  assert.equal(new Set(ids).size, ids.length)
  assert.deepEqual(ids, [...ids].sort((a, b) => b - a))
  assert.equal(ids.length, 3)
})

test('one action window failing keeps the other rows', async () => {
  const s = stubFetch(
    (u) => u.includes('kind=context_change&limit') && !u.includes('exclude_kind'),
    (u) => (u.includes('exclude_kind=context_change') ? [pubDraft] : undefined),
  )
  let res: OwedRaw
  try {
    res = await loadOwed('intelligence')
  } finally {
    s.restore()
  }
  assert.deepEqual(res.actions.map((a) => a.id), [5])
})

test('UI-12: whatsapp is owed only when failing under intelligence', () => {
  const quiet = computeOwed(raw({ missingGroups: [] }), 'intelligence')
  assert.equal(quiet.counts.whatsapp, 0)
  assert.equal(quiet.whatsappFailing, false)
  const bad = computeOwed(raw({ missingGroups: [], whatsappFailing: true, whatsappDetail: 'HTTP 401' }), 'intelligence')
  assert.equal(bad.counts.whatsapp, 1)
  assert.equal(bad.whatsappFailing, true)
  assert.equal(bad.counts.total, quiet.counts.total + 1)
  const legacy = computeOwed(raw({ whatsappFailing: true }), 'second_brain')
  assert.equal(legacy.counts.whatsapp, 0)
  assert.equal(legacy.whatsappFailing, false)
})

test('UI-12: loadOwed leaves optional whatsapp out of missingGroups', async () => {
  const g = globalThis as unknown as { fetch: unknown }
  const old = g.fetch
  g.fetch = async (url: string) => {
    const body = String(url).includes('/api/config/status')
      ? { whatsapp: { state: 'not_configured', detail: '', optional: true }, llm: { state: 'not_configured', detail: '', optional: false } }
      : []
    return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
  }
  try {
    const r = await loadOwed('intelligence')
    assert.deepEqual(r.missingGroups, ['llm'])
    assert.equal(r.whatsappFailing, false)
  } finally {
    g.fetch = old
  }
})

// Phase 13.1
const fc = (id: number, over: Record<string, unknown> = {}) =>
  ({ id, name: `F${id}`, status: 'active', last_error: null, ...over }) as never

test('Phase 13.1: failing forecasts are one owed item each, intelligence only', () => {
  const forecasts = [fc(1, { last_error: 'boom' }), fc(2), fc(3, { status: 'paused', last_error: 'old' })]
  const base = computeOwed(raw(), 'intelligence')
  const o = computeOwed(raw({ forecasts }), 'intelligence')
  assert.equal(o.failingForecasts.length, 1)
  assert.equal(o.counts.forecasts, 1)
  assert.equal(o.counts.total, base.counts.total + 1)
  const again = computeOwed(raw({ forecasts: [...forecasts] }), 'intelligence')
  assert.equal(again.counts.forecasts, 1)
  const sb = computeOwed(raw({ forecasts }), 'second_brain')
  assert.equal(sb.failingForecasts.length, 0)
  assert.equal(sb.counts.forecasts, 0)
  assert.equal(sb.counts.total, computeOwed(raw(), 'second_brain').counts.total)
})

test('Phase 13.1: loadOwed requests forecasts only under intelligence', async () => {
  const s = stubFetch((u) => u.includes('/api/forecasts'))
  try {
    const r = await loadOwed('intelligence')
    assert.deepEqual(r.forecasts, [])
    assert.ok(s.urls.includes('/api/forecasts?limit=50'))
  } finally {
    s.restore()
  }
  const s2 = stubFetch()
  try {
    await loadOwed('second_brain')
    assert.ok(!s2.urls.some((u) => u.includes('/api/forecasts')))
  } finally {
    s2.restore()
  }
})

test('Phase 18: unconverted dashboards are owed once, intelligence only', () => {
  const unconv = { ...dashboards[0]!, needs_conversion: true }
  const both = { ...dashboards[1]!, needs_conversion: true, current_last_error: 'boom' }
  const base = computeOwed(raw({ dashboards: [] }), 'intelligence').counts.dashboards
  const o = computeOwed(raw({ dashboards: [unconv, both] }), 'intelligence')
  assert.equal(o.unconvertedDashboards.length, 2)
  assert.equal(o.counts.dashboards - base, 2)
  const sb = computeOwed(raw({ dashboards: [unconv] }), 'second_brain')
  assert.equal(sb.unconvertedDashboards.length, 0)
  assert.equal(sb.counts.dashboards, 0)
})

// Phase 15 (SRC-10): counts.sources from open source_* alerts
test('counts.sources counts source_ alerts only, without double counting', () => {
  const al = (component: string) => ({ id: 1, component, title: 't', detail: '', created_at: '2026-10-26T00:00:00Z', resolved_at: null })
  const alerts = [al('source_stale:erp'), al('source_failing:erp'), al('llm')]
  const i = computeOwed(raw({ healthAlerts: alerts }), 'intelligence')
  assert.equal(i.counts.sources, 2)
  assert.equal(i.counts.health, 3)
  assert.equal(i.counts.total - computeOwed(raw({}), 'intelligence').counts.total, 3)
  const s = computeOwed(raw({ healthAlerts: alerts }), 'second_brain')
  assert.equal(s.counts.sources, 0)
})

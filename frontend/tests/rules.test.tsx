import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ReactDOMServer from 'react-dom/server'
import { RulesView, type RulesTab } from '../src/pages/Rules'
import type { ContextRule } from '../src/lib/ictypes'

const noop = () => {}
const handlers = { onApprove: noop, onReject: noop, onEdit: noop, onSave: noop }
const base = { tables: [], updated_at: '2026-10-24T00:00:00Z', pending: null }
const RULES: ContextRule[] = [
  { ...base, kind: 'rule', ref: 'rule:agent', title: 'Agent rule', text: 'Approved by agent', status: 'approved', origin: 'agent', tables: ['b.public.invoices'] },
  { ...base, kind: 'rule', ref: 'rule:human', title: 'Human rule', text: 'Written by owner', status: 'approved', origin: 'human' },
  {
    ...base, kind: 'kpi', ref: 'kpi:dso', title: 'DSO', text: '', status: 'proposed', origin: null,
    pending: { action_id: 9, text: 'Days sales outstanding', before: 'Old DSO', created_at: '2026-10-24T00:00:00Z', origin: 'agent', edited_by: null },
  },
  {
    ...base, kind: 'kpi', ref: 'kpi:edited', title: 'Edited', text: 'x', status: 'approved', origin: 'human',
    pending: { action_id: 10, text: 'Mine', before: null, created_at: '2026-10-24T00:00:00Z', origin: 'human', edited_by: 'owner' },
  },
]

const render = (tab: RulesTab, rules = RULES, withAsk = true) =>
  ReactDOMServer.renderToStaticMarkup(
    <RulesView rules={rules} tab={tab} onTab={noop} editing={null} handlers={handlers} onAsk={withAsk ? noop : undefined} />,
  )

test('two tabs with pending counts', () => {
  const html = render('rule')
  assert.match(html, /Rules<span class="tab-count">0/)
  assert.match(html, /KPI definitions<span class="tab-count">2/)
})

test('row: title, mono ref, text, table chips', () => {
  const html = render('rule')
  assert.match(html, /Agent rule/)
  assert.match(html, /rule:agent/)
  assert.match(html, /Approved by agent/)
  assert.match(html, /b\.public\.invoices/)
})

test('gold only with pending; shows proposed, Before, buttons', () => {
  const kpi = render('kpi')
  assert.match(kpi, /ic-gold/)
  assert.match(kpi, /Days sales outstanding/)
  assert.match(kpi, /Before: <span class="ic-body">Old DSO/)
  assert.match(kpi, />Approve</)
  assert.match(kpi, />Edit</)
  assert.doesNotMatch(render('rule'), /ic-gold/)
})

test('proposed by agent chip rules', () => {
  const rule = render('rule')
  assert.equal((rule.match(/proposed by agent/g) ?? []).length, 1)
  assert.match(render('kpi'), /proposed by agent/)
  assert.doesNotMatch(render('rule', [RULES[1]]), /proposed by agent/)
})

test('edited by you', () => {
  assert.match(render('kpi'), /edited by you/)
})

test('composers per tab, disabled when empty', () => {
  const rule = render('rule')
  assert.match(rule, /<button class="btn btn-sm btn-primary" disabled="">Add a rule/)
  const kpi = render('kpi')
  assert.match(kpi, /Define a KPI/)
})

test('empty states keep the composer', () => {
  const html = render('rule', [])
  assert.match(html, /No rules yet/)
  assert.match(html, /Add a rule/)
  assert.match(render('kpi', []), /No KPI definitions yet/)
})

test('page routes composers through the agent', () => {
  const src = readFileSync('src/pages/Rules.tsx', 'utf8')
  assert.match(src, /asks\.rule/)
  assert.match(src, /asks\.kpiDefinition/)
})

// Phase 12 (WR-11)
test('edited by owner chip follows origin human, never on gold rows', () => {
  const rule = render('rule')
  assert.equal((rule.match(/edited by owner/g) ?? []).length, 1)
  assert.doesNotMatch(render('rule', [RULES[1]]), /proposed by agent/)
  assert.match(render('rule', [RULES[1]]), /edited by owner/)
  assert.doesNotMatch(render('rule', [RULES[0]]), /edited by owner/)
  assert.doesNotMatch(render('kpi', [RULES[3]]), /edited by owner|proposed by agent/)
})

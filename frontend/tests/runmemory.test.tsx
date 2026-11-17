import { test } from 'node:test'
import assert from 'node:assert/strict'
import type { ReactElement } from 'react'
import ReactDOMServer from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import {
  graphKindLabel,
  graphScopeLabel,
  memoryItemCount,
  memoryMeta,
  retiredLabel,
  type RunMemory,
} from '../src/lib/runs'
import { RunMemoryView } from '../src/pages/Runs'

/* Quick 019: the memory a run used, on the Runs detail. */

const html = (el: ReactElement) => ReactDOMServer.renderToStaticMarkup(<MemoryRouter>{el}</MemoryRouter>)

const memory = (o: Partial<RunMemory> = {}): RunMemory => ({
  run_id: 12,
  recorded: true,
  used: {
    lessons: [{ id: 31, text: 'Filter VehicleTelemetry by plate first.', status: 'active', status_reason: null }],
    learnings: [
      { id: 7, kind: 'fact', source: 'dashboard', text: '[fact, from dashboard] Acme pays net-30', status: 'superseded', status_reason: 'merged' },
      { id: 8, kind: 'preference', source: 'person:3', text: '[preference, from person:3] Reports in lakh', status: 'deleted', status_reason: null },
    ],
    graph: [
      { uuid: 'e-1', kind: 'fact', group_id: 'global', block: 'global', text: 'Northwind ships with BlueDart' },
      { uuid: 'n-1', kind: 'entity', group_id: 'chat_123_g_us', block: 'group', text: 'Depot: the diesel yard' },
    ],
    cases: [{ id: 19, text: 'Case #19: Collect payment' }],
    group: { purpose: true, members: false },
    chars: 640,
    budget_chars: 6000,
    dropped: 2,
  },
  stored: [
    { id: 40, kind: 'lesson', content: 'To count drops, filter by plate.', source: 'dashboard', outcome: 'new', status: 'active', status_reason: null, updated_at: null },
    { id: 7, kind: 'fact', content: 'Acme pays net-45', source: 'dashboard', outcome: 'updated', status: 'active', status_reason: null, updated_at: null },
  ],
  ...o,
})

test('helpers: counts, header line, labels', () => {
  const m = memory()
  assert.equal(memoryItemCount(m.used), 7)
  assert.equal(memoryMeta(m), '7 items · learned 2')
  assert.equal(memoryMeta(memory({ recorded: false, used: null, stored: [] })), 'none recorded')
  assert.equal(memoryMeta(null), undefined)
  assert.equal(graphKindLabel('episode'), 'Episode')
  assert.equal(graphKindLabel('entity'), 'Entity')
  assert.equal(graphKindLabel(undefined), 'Fact')
  assert.equal(graphScopeLabel({ block: 'group', group_id: 'chat_1' }), 'this group')
  assert.equal(graphScopeLabel({ block: 'global', group_id: 'global' }), 'whole business')
  assert.equal(retiredLabel('active'), '')
  assert.equal(retiredLabel('deleted'), 'deleted since')
  assert.equal(retiredLabel('superseded'), 'superseded since')
})

test('memory panel: lessons, learnings, graph, cases, group, budget, learned', () => {
  const out = html(<RunMemoryView memory={memory()} status="done" />)
  for (const t of ['Lessons it was told to follow', 'Learnings', 'Knowledge graph', 'Open cases', 'Group context', 'Learned from this run']) {
    assert.ok(out.includes(t), t)
  }
  assert.ok(out.includes('Filter VehicleTelemetry by plate first.'))
  assert.ok(out.includes('Acme pays net-30') && !out.includes('[fact, from dashboard]'), 'the stored tag is not repeated')
  assert.ok(out.includes('superseded since') && out.includes('deleted since'))
  assert.ok(out.includes('Northwind ships with BlueDart') && out.includes('>Entity<') && out.includes('this group'))
  assert.ok(out.includes('href="/cases/19"'))
  assert.ok(out.includes('what the group is for') && !out.includes('who is in the group'))
  assert.ok(out.includes('2 more items matched but did not fit the memory budget (640 of 6,000 characters used)'))
  assert.ok(out.includes('To count drops, filter by plate.') && out.includes('>new<') && out.includes('updated an earlier one'))
  assert.ok(!out.includes('owed'), 'nothing on the Runs page is owed, so no gold')
})

test('memory panel: nothing recorded, still running, nothing matched', () => {
  const none = html(<RunMemoryView memory={memory({ recorded: false, used: null, stored: [] })} status="done" />)
  assert.ok(none.includes('data-memory-none') && none.includes('No memory was recorded for this run'))
  assert.ok(none.includes('Nothing new was kept from this run.'))
  const running = html(<RunMemoryView memory={memory({ recorded: false, used: null, stored: [] })} status="running" />)
  assert.ok(running.includes('It gathers memory when it starts working on the message.'))
  assert.ok(running.includes('It stores what it learned after it replies.'))
  const empty = memory({
    used: { lessons: [], learnings: [], graph: [], cases: [], group: { purpose: false, members: false }, chars: 0, budget_chars: 6000, dropped: 0 },
    stored: [],
  })
  const pb = html(<RunMemoryView memory={memory({ recorded: false, used: null, stored: [] })} status="done" kind="playbook" />)
  assert.ok(pb.includes('Not recorded for playbook runs'))
  const e = html(<RunMemoryView memory={empty} status="done" />)
  assert.ok(e.includes('data-memory-empty') && !e.includes('data-memory-dropped'))
})

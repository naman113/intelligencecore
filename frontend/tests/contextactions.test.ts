import { test } from 'node:test'
import assert from 'node:assert/strict'
import { approveMany, editThenApprove, groupSchema, hideTable } from '../src/lib/contextActions'
import type { ContextColumn, ContextPending, ContextTable } from '../src/lib/ictypes'

function recorder(failAt = -1) {
  const calls: [string, unknown][] = []
  return {
    calls,
    post: async (path: string, body?: unknown) => {
      calls.push([path, body])
      if (calls.length - 1 === failAt) throw new Error('409 not proposed')
      return {}
    },
  }
}

test('editThenApprove: edit first, then approve', async () => {
  const r = recorder()
  await editThenApprove(7, 'Customers who buy from us.', r)
  assert.deepEqual(r.calls, [
    ['/api/actions/7/edit', { text: 'Customers who buy from us.' }],
    ['/api/actions/7/approve', undefined],
  ])
})

test('editThenApprove: failed edit never approves', async () => {
  const r = recorder(0)
  await assert.rejects(editThenApprove(7, 'x', r), /409/)
  assert.equal(r.calls.length, 1)
})

function bulkRecorder(failIds: number[] = [], throwAt = -1) {
  const calls: [string, unknown][] = []
  return {
    calls,
    post: async (path: string, body?: unknown) => {
      calls.push([path, body])
      if (calls.length - 1 === throwAt) throw new Error('500 boom')
      const ids = (body as { ids: number[] }).ids
      return { results: ids.map((id) => (failIds.includes(id) ? { id, ok: false, error: 'action is not in a proposed state' } : { id, ok: true, status: 'executed' })) }
    },
  }
}

test('approveMany: one bulk call, per-item results', async () => {
  const r = bulkRecorder()
  const out = await approveMany([3, 4, 5], r)
  assert.equal(out.approved, 3)
  assert.equal(out.error, undefined)
  assert.deepEqual(out.results.map((x) => x.id), [3, 4, 5])
  assert.deepEqual(r.calls, [['/api/actions/bulk-approve', { ids: [3, 4, 5] }]])
})

test('approveMany: batches of 100', async () => {
  const r = bulkRecorder()
  const ids = Array.from({ length: 250 }, (_, i) => i + 1)
  const out = await approveMany(ids, r)
  assert.equal(r.calls.length, 3)
  assert.equal(out.approved, 250)
  assert.equal(out.results.length, 250)
})

test('approveMany: a failed item is reported and the rest still approve', async () => {
  const r = bulkRecorder([4])
  const out = await approveMany([3, 4, 5], r)
  assert.equal(out.approved, 2)
  assert.match(out.error ?? '', /1 failed \(first: #4 action is not in a proposed state/)
  assert.deepEqual(out.results.map((x) => x.ok), [true, false, true])
})

test('approveMany: a failed request marks its batch failed', async () => {
  const r = bulkRecorder([], 0)
  const out = await approveMany([3, 4], r)
  assert.equal(out.approved, 0)
  assert.match(out.error ?? '', /2 failed/)
  assert.match(out.results[0].error ?? '', /500/)
})

test('hideTable posts ref and flag', async () => {
  const r = recorder()
  await hideTable('business_db.public.audit', true, r)
  assert.deepEqual(r.calls, [['/api/schema/tables/hide', { ref: 'business_db.public.audit', hidden: true }]])
})

const pend = (id: number): ContextPending => ({ action_id: id, text: 't', before: null, created_at: '2026-10-24T00:00:00Z' })
const table = (ref: string, pending: ContextPending | null = null): ContextTable => ({
  ref, schema: 'public', table: ref.split('.').pop()!, object_kind: 'table', description: null,
  status: 'proposed', approved_at: null, pending,
})
const col = (ref: string, table_ref: string, pending: ContextPending | null = null): ContextColumn => ({
  ref, table_ref, column: ref.split('.').pop()!, data_type: 'text', is_nullable: true, is_primary_key: false,
  references_ref: null, description: null, status: 'proposed', approved_at: null, pending,
})

test('groupSchema: groups, synthesizes, orders pending ids', () => {
  const g = groupSchema(
    [table('b.public.zeta'), table('b.public.alpha', pend(1))],
    [col('b.public.alpha.x', 'b.public.alpha', pend(2)), col('b.public.orphan.y', 'b.public.orphan', pend(3))],
  )
  assert.deepEqual(g.map((x) => x.table.ref), ['b.public.alpha', 'b.public.orphan', 'b.public.zeta'])
  assert.deepEqual(g[0].pendingIds, [1, 2])
  assert.equal(g[0].pendingCount, 2)
  assert.equal(g[1].table.status, 'undocumented')
  assert.equal(g[1].columns.length, 1)
  assert.equal(g[2].pendingCount, 0)
})

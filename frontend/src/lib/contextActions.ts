import { post } from './api'
import type { ContextColumn, ContextTable } from './ictypes'

/* Owner decisions on the context store. Plumbing only: every call is an
 * existing consent route. Bulk approve is one server-side route that runs the single
 * approve path per proposal, so each keeps its own approval record. */

export interface PostDeps {
  post: (path: string, body?: unknown) => Promise<unknown>
}
const real: PostDeps = { post: (p, b) => post(p, b) }

export async function editThenApprove(actionId: number, text: string, deps: PostDeps = real): Promise<void> {
  await deps.post('/api/actions/' + actionId + '/edit', { text })
  await deps.post('/api/actions/' + actionId + '/approve')
}

export interface BulkItem {
  id: number
  ok: boolean
  status?: string
  error?: string
}
export interface BulkResult {
  approved: number
  error?: string
  results: BulkItem[]
}
const BULK_BATCH = 100

/** Approve many proposals through the server-side bulk route. The server runs the same
 * per-item approval as the single route (own approval record each) with bounded
 * concurrency, and answers with one result per id. A failed item does not stop the rest. */
export async function approveMany(ids: number[], deps: PostDeps = real): Promise<BulkResult> {
  const results: BulkItem[] = []
  for (let i = 0; i < ids.length; i += BULK_BATCH) {
    const batch = ids.slice(i, i + BULK_BATCH)
    try {
      const res = (await deps.post('/api/actions/bulk-approve', { ids: batch })) as { results?: BulkItem[] }
      results.push(...(res.results ?? []))
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      results.push(...batch.map((id) => ({ id, ok: false, error: msg })))
    }
  }
  const approved = results.filter((r) => r.ok).length
  const failed = results.filter((r) => !r.ok)
  if (!failed.length) return { approved, results }
  return { approved, error: `${failed.length} failed (first: #${failed[0].id} ${failed[0].error ?? failed[0].status ?? 'not approved'})`, results }
}

export async function hideTable(ref: string, hidden: boolean, deps: PostDeps = real): Promise<unknown> {
  return deps.post('/api/schema/tables/hide', { ref, hidden })
}

export interface SchemaGroup {
  table: ContextTable
  columns: ContextColumn[]
  pendingIds: number[]
  pendingCount: number
}

export function groupSchema(tables: ContextTable[], columns: ContextColumn[]): SchemaGroup[] {
  const map = new Map<string, { table: ContextTable; columns: ContextColumn[] }>()
  for (const t of tables) map.set(t.ref, { table: t, columns: [] })
  for (const c of columns) {
    let g = map.get(c.table_ref)
    if (!g) {
      // Phase 11: IN-02 (real refs are business_db.schema.table; keep legacy source:schema.table)
      const rest = c.table_ref.includes(':')
        ? c.table_ref.split(':').slice(1).join(':')
        : c.table_ref.startsWith('business_db.')
          ? c.table_ref.split('.').slice(1).join('.')
          : c.table_ref
      const parts = rest.split('.')
      g = {
        table: {
          ref: c.table_ref,
          schema: parts.length > 1 ? parts[0] : '',
          table: parts[parts.length - 1],
          object_kind: 'table',
          description: null,
          status: 'undocumented',
          approved_at: null,
          pending: null,
        },
        columns: [],
      }
      map.set(c.table_ref, g)
    }
    g.columns.push(c)
  }
  return [...map.values()]
    .sort((a, b) => (a.table.ref < b.table.ref ? -1 : a.table.ref > b.table.ref ? 1 : 0))
    .map((g) => {
      const pendingIds: number[] = []
      if (g.table.pending) pendingIds.push(g.table.pending.action_id)
      for (const c of g.columns) if (c.pending) pendingIds.push(c.pending.action_id)
      return { ...g, pendingIds, pendingCount: pendingIds.length }
    })
}

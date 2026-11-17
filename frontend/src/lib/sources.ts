import { del, get, post } from './api'

/* Phase 15 (SRC-02, SRC-10): shapes and calls for /api/sources. No type here
 * carries a secret; credentials are shown only as handles. */

export type CredentialHandle = {
  handle: string
  name: string
  kind: 'postgres' | 'mysql' | 'mssql'
  display: { host?: string; port?: number; database?: string; user?: string }
  read_only: boolean | null
  verify_status: string | null
  verify_detail: string | null
  verified_at: string | null
}

export type SourceTableRow = {
  warehouse_schema: string
  warehouse_table: string
  remote_schema: string | null
  remote_table: string
  mode: 'import' | 'live'
  cursor_column: string | null
  merge_key: string[] | null
  status: string
  last_success_at: string | null
  rows_total: number | null
  backfill_done: boolean
  backfill_progress: number | null
  /** Phase 17 (CONS-05): set while an approved mode switch is in progress. */
  pending_mode?: 'import' | 'live' | null
  last_error: string | null
}

/** Phase 17 (CONS-05): mode text for a table, including a switch in progress. */
export function modeLabel(t: Pick<SourceTableRow, 'mode' | 'pending_mode'>): string {
  return t.pending_mode && t.pending_mode !== t.mode ? `${t.mode}, switching to ${t.pending_mode}` : t.mode
}

export type SourceRow = {
  name: string
  kind: string
  status: string
  credential: string | null
  display: CredentialHandle['display'] | null
  warehouse_schema: string | null
  schedule: Record<string, unknown> | null
  freshness_minutes: number | null
  next_sync_at: string | null
  last_run: {
    id: number
    kind: string
    status: string
    started_at: string | null
    ended_at: string | null
    rows_loaded: number
    error: string | null
  } | null
  last_success_at: string | null
  backfill: { done: boolean; progress: number | null }
  /** Phase 15 (15-13) server flags; absent on an older server, read as false. */
  stale?: boolean
  failing?: boolean
  unreachable?: boolean
  tables: SourceTableRow[]
}

export type SyncRun = {
  id: number
  kind: string
  status: string
  started_at: string | null
  ended_at: string | null
  duration_ms: number | null
  rows_loaded: number
  tables: unknown[]
  error: string | null
}

export type CredentialBody = {
  name: string
  kind: string
  host: string
  port: number
  database: string
  user: string
  password: string
  options?: Record<string, unknown>
}

export const listCredentials = () => get<{ credentials: CredentialHandle[] }>('/api/sources/credentials')
export const saveCredential = (body: CredentialBody) => post<CredentialHandle>('/api/sources/credentials', body)
export const verifyCredential = (name: string) =>
  post<CredentialHandle>(`/api/sources/credentials/${encodeURIComponent(name)}/verify`)
export const deleteCredential = (name: string) => del(`/api/sources/credentials/${encodeURIComponent(name)}`)
export const loadSources = () => get<{ sources: SourceRow[] }>('/api/sources')
export const loadSourceRuns = (name: string) =>
  get<{ runs: SyncRun[] }>(`/api/sources/${encodeURIComponent(name)}/runs`)

/* Phase 15 (15-14): the sources list for the Data sources page. */
export async function loadSourceRows(): Promise<SourceRow[]> {
  const r = await get<{ sources?: SourceRow[] }>('/api/sources')
  return (r?.sources ?? []).map((x) => ({
    ...x,
    stale: x.stale === true,
    failing: x.failing === true,
    unreachable: x.unreachable === true,
    tables: x.tables ?? [],
  }))
}

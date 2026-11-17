/* Hand-written shapes for the Intelligence Corner routes (dashboards, context,
 * KPI history). Phase 6/7 shapes live in contracts.ts, not here. */

export interface DashboardListItem {
  id: number
  slug: string
  title: string
  current_version: number | null
  current_status: string | null
  latest_version: number | null
  latest_status: string | null
  version_count: number
  current_last_error: string | null
  last_refreshed_at: string | null
  updated_at: string
  /* Phase 18 */
  current_format?: 'spec' | 'html' | null
  needs_conversion?: boolean
}

/* Phase 18: chart-spec summary shapes (text only; spec JSON is never rendered). */
export interface SpecTileRef {
  id: string
  type: string
  title: string | null
  binding: 'measure' | 'query' | 'none'
  ref: string | null
}

export interface QueryRef {
  id: string
  source: string
}

export type VersionStatus = 'draft' | 'published' | 'retired'

export interface DashboardVersion {
  version: number
  status: VersionStatus
  title: string
  change_note: string | null
  author: string | null
  created_at: string
  published_at: string | null
  retired_at: string | null
  last_error: string | null
  published_by: number | null
  published_by_name: string | null
  path: string
  /* Phase 18 */
  format?: 'spec' | 'html'
  /* Phase 22: the stored spec's version (1 when absent), null for HTML versions */
  spec_version?: number | null
  tiles?: SpecTileRef[]
  tiles_added?: string[]
  tiles_removed?: string[]
  tiles_changed?: string[]
  measures?: string[]
  queries_used?: QueryRef[]
}

export interface DashboardDetail {
  dashboard: {
    id: number
    slug: string
    title: string
    view_token: string
    token_rotated_at: string | null
    current_version: number | null
    created_at: string
    updated_at: string
    path: string
  }
  versions: DashboardVersion[]
}

export interface DashboardQuery {
  id: string
  sql: string
  refresh_minutes?: number | null
  title?: string
  /** Phase 13.1: non-SQL sources */
  source?: string
  forecast_id?: number
  watch_id?: number
}

export interface ContextPending {
  action_id: number
  text: string | null
  before: string | null
  created_at: string
  origin?: string | null
  edited_by?: string | null
}

export interface ContextSource {
  name: string
  kind: string
  description: string | null
  status: string
  tables_approved: number
  columns_approved: number
  pending: number
}

export interface ContextTable {
  ref: string
  schema: string
  table: string
  object_kind: string
  description: string | null
  status: string
  approved_at: string | null
  pending: ContextPending | null
}

export interface ContextColumn {
  ref: string
  table_ref: string
  column: string
  data_type: string
  is_nullable: boolean
  is_primary_key: boolean
  references_ref: string | null
  description: string | null
  status: string
  approved_at: string | null
  pending: ContextPending | null
}

export interface ContextRule {
  kind: string
  ref: string
  title: string
  text: string
  tables: string[]
  status: string
  updated_at: string
  origin?: string | null
  pending: ContextPending | null
}

export interface KpiPoint {
  t: string
  metric: string | null
  value: number | null
  min?: number | null
  max?: number | null
  n?: number
}

export interface KpiSeries {
  watch_id: number
  title: string
  kind: string
  status: string
  metric: string | null
  metrics: string[]
  days: number
  resolution: string
  timezone: string
  points: KpiPoint[]
  latest: { t: string; metric: string | null; value: number | null } | null
}

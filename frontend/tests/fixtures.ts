/* Shared fixtures for the Intelligence Corner UI tests. */
import type {
  ContextColumn,
  ContextRule,
  ContextSource,
  ContextTable,
  DashboardDetail,
  DashboardListItem,
  KpiSeries,
} from '../src/lib/ictypes'
import type { Action } from '../src/lib/types'

export function action(id: number, kind: string, payload: unknown, status: Action['status'] = 'proposed'): Action {
  return {
    id,
    case_id: null,
    playbook_id: null,
    kind,
    payload,
    tier: null,
    status,
    created_at: '2026-10-24T06:00:00Z',
    executed_at: null,
  }
}

/* ---- context_change ---- */
export const ctxTable = action(101, 'context_change', {
  _summary: 'Describe table public.customers',
  kind: 'table',
  ref: 'business_db.public.customers',
  text: 'One row per customer account.',
  title: 'Describe table public.customers',
  before: null,
  origin: 'agent',
})
export const ctxColumn = action(102, 'context_change', {
  _summary: 'Describe column customers.region_id',
  kind: 'column',
  ref: 'business_db.public.customers.region_id',
  text: 'The sales region the customer belongs to.',
  title: 'Describe customers.region_id',
  before: 'region',
  origin: 'agent',
})
export const ctxEdited = action(103, 'context_change', {
  kind: 'column',
  ref: 'business_db.public.customers.tier',
  text: 'Pricing tier: gold, silver or bronze.',
  before: null,
  origin: 'agent',
  edited_by: 'owner',
  agent_text: 'Tier of the customer.',
})
export const ctxHide = action(
  104,
  'context_change',
  {
    op: 'hide',
    kind: 'table',
    ref: 'business_db.public.audit_log',
    origin: 'owner', // real: contextstore.set_table_hidden payload
    rejected_action_ids: [7, 8],
    _summary: 'Hid table audit_log',
  },
  'executed',
)
export const ctxScript = action(105, 'context_change', {
  kind: 'rule',
  ref: 'rule:margin',
  text: '<script>alert(1)</script>',
  before: null,
})

/* ---- publish_dashboard ---- */
export const pubWithUrl = action(201, 'publish_dashboard', {
  slug: 'sales',
  version: 4,
  title: 'Sales overview',
  change_note: 'Added daily orders chart',
  draft_url: '/d/sales/v/4/?p=seedtoken',
  queries_added: ['orders_by_day'],
  queries_removed: ['old_q'],
  queries_changed: ['revenue'],
})
export const pubAbsolute = action(202, 'publish_dashboard', {
  slug: 'sales',
  version: 4,
  title: 'Sales overview',
  change_note: 'Rebuilt layout',
  draft_url: 'http://localhost:9082/d/sales/v/4/?p=abstoken',
  queries_added: [],
})
export const pubBare = action(203, 'publish_dashboard', {
  slug: 'sales',
  draft_version: 5,
  title: 'Sales overview',
  change_note: 'Tweaked colours',
})
export const pubRollback = action(
  204,
  'publish_dashboard',
  {
    op: 'rollback',
    slug: 'sales',
    title: 'Sales overview',
    version: 2,
    from_version: 3,
    change_note: null,
    note: 'bad numbers',
    decided_via: 'ops_api',
  },
  'executed',
)
export const pubJs = action(205, 'publish_dashboard', {
  slug: 'x',
  version: 1,
  title: 'Evil',
  draft_url: 'javascript:alert(1)//d/x/v/1/?p=a',
})

/* ---- other kinds ---- */
export const waAction = action(301, 'whatsapp_message', { recipient: 'Ana', phone: '+100', body: 'Hello there' })
export const taskAction = action(302, 'agent_task', { title: 'Follow up', instruction: 'Check invoices', thread_id: 't1' })

/* ---- dashboards ---- */
export const dashboards: DashboardListItem[] = [
  {
    id: 1,
    slug: 'sales',
    title: 'Sales overview',
    current_version: 3,
    current_status: 'published',
    latest_version: 4,
    latest_status: 'draft',
    version_count: 4,
    current_last_error: null,
    last_refreshed_at: '2026-10-24T05:30:00Z',
    updated_at: '2026-10-24T05:30:00Z',
  },
  {
    id: 2,
    slug: 'stock',
    title: 'Stock levels',
    current_version: 1,
    current_status: 'published',
    latest_version: 1,
    latest_status: 'published',
    version_count: 1,
    current_last_error: null,
    last_refreshed_at: null,
    updated_at: '2026-10-23T05:30:00Z',
  },
  {
    id: 3,
    slug: 'cash',
    title: 'Cash',
    current_version: 2,
    current_status: 'published',
    latest_version: 2,
    latest_status: 'published',
    version_count: 2,
    current_last_error: 'query timed out',
    last_refreshed_at: '2026-10-22T05:30:00Z',
    updated_at: '2026-10-22T05:30:00Z',
  },
]

export const dashboardDetail: DashboardDetail = {
  dashboard: {
    id: 1,
    slug: 'sales',
    title: 'Sales overview',
    view_token: 'viewtoken123',
    token_rotated_at: null,
    current_version: 3,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-24T05:30:00Z',
    path: '/d/sales/',
  },
  versions: [
    {
      version: 4,
      status: 'draft',
      title: 'Sales overview',
      change_note: 'Added daily orders chart',
      author: 'agent',
      created_at: '2026-10-24T05:00:00Z',
      published_at: null,
      retired_at: null,
      last_error: null,
      published_by: null,
      published_by_name: null,
      path: '/d/sales/v/4/',
    },
    {
      version: 3,
      status: 'published',
      title: 'Sales overview',
      change_note: 'Regional split',
      author: 'agent',
      created_at: '2026-10-20T05:00:00Z',
      published_at: '2026-10-20T06:00:00Z',
      retired_at: null,
      last_error: null,
      published_by: 1,
      published_by_name: 'Sam Owner',
      path: '/d/sales/v/3/',
    },
    {
      version: 2,
      status: 'retired',
      title: 'Sales overview',
      change_note: 'Totals',
      author: 'agent',
      created_at: '2026-10-10T05:00:00Z',
      published_at: '2026-10-10T06:00:00Z',
      retired_at: '2026-10-20T06:00:00Z',
      last_error: null,
      published_by: 1,
      published_by_name: 'Sam Owner',
      path: '/d/sales/v/2/',
    },
    {
      version: 1,
      status: 'retired',
      title: 'Sales',
      change_note: null,
      author: 'agent',
      created_at: '2026-10-01T05:00:00Z',
      published_at: '2026-10-01T06:00:00Z',
      retired_at: '2026-10-10T06:00:00Z',
      last_error: null,
      published_by: null,
      published_by_name: null,
      path: '/d/sales/v/1/',
    },
  ],
}

/* ---- context store ---- */
import liveSources from './fixtures/live/sources.json'
import liveTables from './fixtures/live/tables.json'
import liveColumns from './fixtures/live/columns.json'
import liveRules from './fixtures/live/rules.json'

const pending = {
  action_id: 55,
  text: 'Revised description',
  before: 'Old description',
  created_at: '2026-10-24T05:00:00Z',
  origin: 'agent',
  edited_by: null,
}

// Phase 11: WR-14 -- built from live p11 API responses (fixtures/live/*.json,
// captured by 11-capture-context-fixtures.sh); only marked overrides differ.
export const ctxSources: ContextSource[] = [
  // override: pending 0 so the "nothing waiting, no gold" cases have a source
  { ...(liveSources.sources[0] as ContextSource), pending: 0 },
]
export const ctxTables: ContextTable[] = [
  // override: the live table is approved with no pending; add a live-shaped pending
  { ...(liveTables.tables[0] as ContextTable), pending },
  // override: undocumented sibling table with the live key set
  { ...(liveTables.tables[0] as ContextTable), ref: 'business_db.public.sales_orders', table: 'sales_orders', description: null, status: 'proposed', approved_at: null, pending: null },
]
export const ctxColumns: ContextColumn[] = liveColumns.columns as ContextColumn[]
export const ctxRules: ContextRule[] = liveRules.rules as ContextRule[]

/* ---- KPI series ---- */
export const kpiSeries: KpiSeries = {
  watch_id: 4,
  title: 'Overdue invoices',
  kind: 'kpi',
  status: 'active',
  metric: 'overdue_total',
  metrics: ['overdue_total'],
  days: 30,
  resolution: 'day',
  timezone: 'UTC',
  points: Array.from({ length: 30 }, (_, i) => ({
    t: `2026-09-${String(i + 1).padStart(2, '0')}T00:00:00Z`,
    metric: 'overdue_total',
    value: 100 + i * 3,
  })),
  latest: { t: '2026-09-30T00:00:00Z', metric: 'overdue_total', value: 187 },
}

/* ---- insights ---- */
export const genericInsights = [
  { id: 4, finding: 'Revenue in South fell 18%\nDriven by two accounts', evidence_sql: 'SELECT 1', severity: 'medium', case_id: 9, created_at: '2026-10-24T06:00:00Z' },
  { id: 5, text: 'Stock turns slowed', at: '2026-10-23T06:00:00Z' },
]
export const insightsBare = genericInsights
export const insightsEnvelope = { insights: genericInsights }

const noteItem = {
  type: 'note',
  id: 'note:12',
  origin: 'exploration',
  title: 'Revenue in South fell 18%',
  finding: 'Driven by two accounts',
  severity: 'critical',
  evidence_sql: 'SELECT region, sum(total) FROM invoices GROUP BY 1',
  evidence: { row_count: 4, columns: ['region', 'sum'], sample: [['South', 100]], truncated: false },
  case_id: 9,
  case_title: 'Exploration 2026-10-24',
  watch_id: null,
  watch_title: null,
  task_run_id: 377,
  created_at: '2026-10-24T06:00:00+00:00',
}
const watchItem = {
  type: 'watch_event',
  id: 'watch_event:19',
  origin: 'watch',
  title: 'Overdue invoices crossed threshold',
  finding: 'Overdue total is 187',
  severity: 'warning',
  evidence_sql: null,
  evidence: { row_count: 1, columns: ['overdue_total'], sample: [[187]], truncated: false },
  case_id: null,
  case_title: null,
  watch_id: 4,
  watch_title: 'Overdue invoices',
  task_run_id: null,
  created_at: '2026-10-24T05:00:00+00:00',
}
// Phase 15 (15-13): source_change fixtures (15-14 adds its own section after this one)
const srcBase = {
  name: 'erp_my',
  kind: 'mysql',
  credential: 'cred:erp_my',
  display: { host: 'db.example', port: 3306, database: 'erp' },
  warehouse_schema: 'erp_my',
  tables: [
    { remote_schema: null, remote_table: 'orders', warehouse_table: 'orders', mode: 'import',
      cursor: 'updated_at', merge_key: ['id'], est_rows: 120000, est_mb: 48, reconcile: true,
      cursor_indexed: false },
    { remote_schema: null, remote_table: 'customers', warehouse_table: 'customers', mode: 'live',
      cursor: null, merge_key: [], est_rows: 900, est_mb: 1, reconcile: false, cursor_indexed: null },
  ],
  backfill_start: '2025-01-01',
  schedule: { interval_minutes: 60, window: [6, 22], reconcile_hour: 2 },
  caps: { max_rows_per_run: 500000, max_seconds_per_run: 600 },
  estimated: { rows: 120900, storage_mb: 49, backfill_runs: 1 },
  schema_policy: 'additive',
  freshness_minutes: 120,
  warnings: ['Check the clock'],
  _summary: 'Connect mysql source erp_my',
}
export const srcDb = action(31, 'source_change', { ...srcBase, password: 'hunter2-secret' })
export const srcCsv = action(32, 'source_change', {
  name: 'budget',
  kind: 'csv',
  credential: null,
  display: {},
  tables: [{ remote_table: 'budget', warehouse_table: 'budget', mode: 'import', cursor: null,
    merge_key: [], est_rows: 40, est_mb: 0, reconcile: false, cursor_indexed: null }],
  csv: { file: 'budget.csv', sha256: 'abcdef0123456789abcdef', on_reupload: 'replace' },
  estimated: { rows: 40, storage_mb: 0, backfill_runs: 1 },
  caps: {},
  schedule: {},
})
// End Phase 15 (15-13)
// Phase 15 (15-14): sources list fixtures
const srcTable = {
  warehouse_schema: 'erp_my', warehouse_table: 'orders', remote_schema: null, remote_table: 'orders',
  mode: 'import' as const, cursor_column: 'updated_at', merge_key: ['id'], status: 'active',
  last_success_at: null, rows_total: 10, backfill_done: false, backfill_progress: 0.42, last_error: null,
}
export const sourceRows = [
  { name: 'business_db', kind: 'business_db', status: 'active', credential: null, display: null,
    warehouse_schema: null, schedule: null, freshness_minutes: null, next_sync_at: null, last_run: null,
    last_success_at: null, backfill: { done: true, progress: null }, stale: false, failing: false,
    unreachable: false,
    tables: [{ ...srcTable, warehouse_schema: 'public', warehouse_table: 'customers', mode: 'live' as const,
      backfill_done: true, backfill_progress: null }] },
  { name: 'erp_my', kind: 'mysql', status: 'active', credential: 'cred:erp_my', display: {},
    warehouse_schema: 'erp_my', schedule: {}, freshness_minutes: 120, next_sync_at: null,
    last_run: { id: 1, kind: 'incremental', status: 'failed', started_at: '2026-10-25T01:00:00Z',
      ended_at: '2026-10-25T01:01:00Z', rows_loaded: 0, error: 'timeout <b>x</b>' },
    last_success_at: null, backfill: { done: false, progress: 0.42 }, stale: true, failing: true,
    unreachable: true, tables: [srcTable] },
]
// End Phase 15 (15-14)

export const phase7Insights = {
  origin: 'exploration',
  items: [noteItem, watchItem],
  next_before: null as string | null,
}
// Phase 15 gap 2 (15-19): mixed-case remote names next to their warehouse names
export const srcMixed = action(33, 'source_change', {
  ...srcBase,
  tables: [
    { remote_schema: null, remote_table: 'OrderLines', warehouse_table: 'order_lines', mode: 'import',
      cursor: 'UpdatedAt', merge_key: ['OrderLineID'], est_rows: 100, est_mb: 1, reconcile: false,
      cursor_indexed: true },
    { remote_schema: null, remote_table: 'LookupCodes', warehouse_table: 'lookup_codes', mode: 'live',
      cursor: null, merge_key: [], est_rows: 10, est_mb: 0, reconcile: false, cursor_indexed: null },
  ],
})
// End Phase 15 gap 2 (15-19)

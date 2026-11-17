/* Every shape this phase does not own is parsed here. Phase 10 fixes this module if Phase 6/7 shapes differ.
 *
 * Assumed contracts (see 08-04-SUMMARY.md):
 *  - GET /api/insights?limit=N (Phase 7): { origin, items: [...], next_before }; bare array and { insights } tolerated
 *  - proposed publish_dashboard payload (Phase 6): { slug, version | draft_version, title, change_note,
 *    draft_url?, path?, queries_added?, queries_removed?, queries_changed? }
 *  - POST /api/dashboards/{slug}/versions/{n}/preview -> { slug, version, preview_token, expires_at, path }
 */
import { ApiError, asObj, get, post } from './api'
import type { DashboardQuery, QueryRef, SpecTileRef } from './ictypes'

type Obj = Record<string, unknown>

const str = (v: unknown): string | null => (typeof v === 'string' && v.trim() ? v : null)
const num = (v: unknown): number | null => {
  if (typeof v === 'number' && Number.isFinite(v)) return v
  if (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v))) return Number(v)
  return null
}
const strList = (v: unknown): string[] =>
  Array.isArray(v) ? v.filter((x) => typeof x === 'string' || typeof x === 'number').map(String) : []

/* ---------------------------------------------------------------- insights */

export interface Insight {
  key: string
  kind: 'note' | 'watch_event' | null
  title: string
  body: string
  severity: string | null
  evidenceSql: string | null
  at: string | null
  caseId: number | null
  caseTitle: string | null
  watchId: number | null
  watchTitle: string | null
  /* Quick 006: the plain-words card. All optional: older insights fall back to the
   * first sentence of the finding (see insightWords). */
  summary?: string | null
  impact?: string | null
  nextStep?: string | null
  keyFigures?: { label: string; value: string }[]
  caseStatus?: string | null
  evidence?: InsightEvidenceView | null
  /* Phase 17: which lane found it; raw_sql findings are unverified. */
  lane?: 'measure' | 'raw_sql' | null
  verified?: boolean | null
  measure?: string | null
}

export interface InsightEvidenceView {
  rowCount: number | null
  columns: string[]
  sample: Record<string, unknown>[]
  truncated: boolean
}

export function evidenceOf(v: unknown): InsightEvidenceView | null {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return null
  const o = v as Obj
  const columns = strList(o.columns)
  // Rows arrive as objects (record_insight) or as positional arrays (older envelopes).
  const sample: Record<string, unknown>[] = []
  for (const r of Array.isArray(o.sample) ? o.sample : []) {
    if (Array.isArray(r)) sample.push(Object.fromEntries(r.map((x, k) => [columns[k] ?? `col ${k + 1}`, x])))
    else if (r && typeof r === 'object') sample.push(r as Record<string, unknown>)
  }
  return {
    rowCount: num(o.row_count),
    columns: columns.length ? columns : sample[0] ? Object.keys(sample[0]) : [],
    sample: sample.slice(0, 10),
    truncated: o.truncated === true,
  }
}

function figuresOf(v: unknown): { label: string; value: string }[] {
  if (!Array.isArray(v)) return []
  const out: { label: string; value: string }[] = []
  for (const f of v.slice(0, 6)) {
    if (f && typeof f === 'object' && !Array.isArray(f)) {
      const label = str((f as Obj).label)
      const value = (f as Obj).value
      if (label && (typeof value === 'string' || typeof value === 'number')) out.push({ label, value: String(value) })
    } else if (typeof f === 'string' && f.includes(':')) {
      const [label, ...rest] = f.split(':')
      if (label!.trim() && rest.join(':').trim()) out.push({ label: label!.trim(), value: rest.join(':').trim() })
    }
  }
  return out
}

const SENTENCE = /(?<=[.!?])\s+(?=[A-Z0-9"(])/

/* Quick 006: what the card says in words. Uses the agent's plain-words fields when
 * present; otherwise the first sentence of the finding is the summary. A trailing
 * "Caveat: ..." sentence is split out so the card can show it muted. */
export function insightWords(i: Insight): { summary: string; caveat: string | null } {
  const sentences = (i.body || '').trim().split(SENTENCE).filter(Boolean)
  const cav = sentences.find((t) => /^caveat\b/i.test(t.trim())) ?? null
  const rest = sentences.filter((t) => t !== cav)
  const summary = (i.summary && i.summary.trim()) || rest[0] || i.body || ''
  return { summary: summary.trim(), caveat: cav ? cav.trim().replace(/^caveat\s*:\s*/i, '') : null }
}

export function normalizeInsight(raw: unknown): Insight | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const o = raw as Obj
  const text = str(o.finding) ?? str(o.text) ?? str(o.summary) ?? str(o.content)
  const explicit = str(o.title)
  if (!text && !explicit) return null
  let title: string
  let body: string
  if (explicit) {
    title = explicit.trim()
    body = text ? text.trim() : ''
  } else {
    const lines = text!.trim().split('\n')
    title = lines[0]!.trim()
    body = lines.slice(1).join('\n').trim()
  }
  const at = str(o.created_at) ?? str(o.at) ?? str(o.occurred_at)
  const id = o.id
  const key = typeof id === 'string' || typeof id === 'number' ? String(id) : `${at ?? ''}:${title}`
  const kind = o.type === 'note' || o.type === 'watch_event' ? o.type : null
  return {
    key,
    kind,
    title,
    body,
    severity: str(o.severity),
    evidenceSql: str(o.evidence_sql) ?? str(o.sql),
    at,
    caseId: num(o.case_id),
    caseTitle: str(o.case_title),
    watchId: num(o.watch_id),
    watchTitle: str(o.watch_title),
    summary: explicit ? str(o.summary) : null,
    impact: str(o.impact),
    nextStep: str(o.next_step),
    keyFigures: figuresOf(o.key_figures),
    caseStatus: str(o.case_status),
    evidence: evidenceOf(o.evidence),
    ...(o.lane === 'measure' || o.lane === 'raw_sql' ? { lane: o.lane } : {}),
    ...(typeof o.verified === 'boolean' ? { verified: o.verified } : {}),
    ...(str(o.measure) ? { measure: str(o.measure) } : {}),
  }
}

export async function fetchInsights(
  limit: number,
  filters: { severity?: string; case?: string } = {},
): Promise<{ available: boolean; items: Insight[] }> {
  try {
    const q = new URLSearchParams({ limit: String(limit) })
    if (filters.severity) q.set('severity', filters.severity)
    if (filters.case) q.set('case', filters.case)
    const res = await get<unknown>(`/api/insights?${q.toString()}`)
    const o = res && typeof res === 'object' ? (res as Obj) : {}
    const list = Array.isArray(res)
      ? res
      : Array.isArray(o.items)
        ? o.items
        : Array.isArray(o.insights)
          ? o.insights
          : []
    const items = (list as unknown[]).map(normalizeInsight).filter((x): x is Insight => x !== null)
    return { available: true, items }
  } catch (e) {
    if (e instanceof ApiError && (e.status === 404 || e.status === 405)) {
      return { available: false, items: [] }
    }
    throw e
  }
}

/* Quick 006: the Insights page summary strip. null on an older gateway. */
export interface InsightsSummary {
  days: number
  bySeverity: { critical: number; warning: number; info: number }
  openCases: number
  lastRun: { id: number | null; status: string; startedAt: string | null; endedAt: string | null; error: string | null } | null
  /** Quick 023: the explore_data playbook, for the Run exploration now button. */
  playbook: { id: number; status: string } | null
}

export async function fetchInsightsSummary(): Promise<InsightsSummary | null> {
  try {
    const o = asObj(await get<unknown>('/api/insights/summary'))
    const by = asObj(o.by_severity)
    const run = o.last_run && typeof o.last_run === 'object' ? (o.last_run as Obj) : null
    return {
      days: num(o.days) ?? 30,
      bySeverity: { critical: num(by.critical) ?? 0, warning: num(by.warning) ?? 0, info: num(by.info) ?? 0 },
      openCases: num(o.open_cases) ?? 0,
      lastRun: run
        ? { id: num(run.id), status: str(run.status) ?? '', startedAt: str(run.started_at), endedAt: str(run.ended_at), error: str(run.error) }
        : null,
      playbook: o.playbook && typeof o.playbook === 'object' && num((o.playbook as Obj).id) !== null
        ? { id: num((o.playbook as Obj).id) as number, status: str((o.playbook as Obj).status) ?? '' }
        : null,
    }
  } catch (e) {
    if (e instanceof ApiError && (e.status === 404 || e.status === 405 || e.status === 422)) return null
    throw e
  }
}

/* ---------------------------------------------------------- context_change */

export interface ContextChangeView {
  op: 'propose' | 'hide' | 'unhide'
  kind: string | null
  ref: string | null
  title: string | null
  text: string | null
  before: string | null
  editedBy: string | null
  agentText: string | null
  origin: string | null
  rejectedCount: number
}

export function readContextChange(payload: unknown): ContextChangeView {
  const p = asObj<Obj>(payload)
  const op = p.op === 'hide' || p.op === 'unhide' ? p.op : 'propose'
  return {
    op,
    kind: str(p.kind),
    ref: str(p.ref),
    title: str(p.title),
    text: str(p.text),
    before: str(p.before),
    editedBy: str(p.edited_by),
    agentText: str(p.agent_text),
    origin: str(p.origin),
    rejectedCount: Array.isArray(p.rejected_action_ids) ? p.rejected_action_ids.length : 0,
  }
}

/* ------------------------------------------------------- source_change (Phase 15) */

export interface SourceChangeTable {
  remote: string
  warehouse: string
  mode: string
  cursor: string | null
  mergeKey: string[]
  estRows: number
  estMb: number
  reconcile: boolean
  cursorIndexed: boolean | null
}

/* Phase 17 (CONS-05): source_change op 'mode' (17-07 modeflip.propose). */
export interface SourceModeTable {
  table: string
  from: string
  to: string
  cursor: string | null
  mergeKey: string[]
  estRows: number
  estMb: number | null
}

export interface SourceModeDependents {
  table: string
  watches: number
  dashboards: number
  insights: number
  model: number
}

export interface SourceModeChangeView {
  name: string
  tables: SourceModeTable[]
  notes: string[]
  freshness: string | null
  dependents: SourceModeDependents[]
}

export interface SourceChangeView {
  modeChange: SourceModeChangeView | null
  name: string
  kind: string
  credential: string | null
  host: string | null
  port: number | null
  database: string | null
  tables: SourceChangeTable[]
  backfillStart: string | null
  intervalMinutes: number | null
  window: string | null
  maxRows: number
  maxSeconds: number
  estRows: number
  estStorageMb: number
  estBackfillRuns: number
  schemaPolicy: string | null
  freshnessMinutes: number | null
  warnings: string[]
  csvFile: string | null
  csvHash: string | null
  csvOnReupload: string | null
  summary: string | null
}

/* Whitelist reader: only these keys are ever read, so a stray password key cannot reach the screen. */
export function readSourceChange(payload: unknown): SourceChangeView {
  const p = asObj<Obj>(payload)
  const disp = asObj<Obj>(p.display)
  const sched = asObj<Obj>(p.schedule)
  const caps = asObj<Obj>(p.caps)
  const est = asObj<Obj>(p.estimated)
  const csv = asObj<Obj>(p.csv)
  const win =
    Array.isArray(sched.window) && sched.window.length === 2
      ? `${sched.window[0]}:00-${sched.window[1]}:00`
      : str(sched.window)
  const tables = (Array.isArray(p.tables) ? p.tables : []).map((t): SourceChangeTable => {
    const o = asObj<Obj>(t)
    const rs = str(o.remote_schema)
    const rt = str(o.remote_table) ?? ''
    return {
      remote: rs ? `${rs}.${rt}` : rt,
      warehouse: str(o.warehouse_table) ?? rt,
      mode: str(o.mode) ?? 'import',
      cursor: str(o.cursor),
      mergeKey: strList(o.merge_key),
      estRows: num(o.est_rows) ?? 0,
      estMb: num(o.est_mb) ?? 0,
      reconcile: o.reconcile === true,
      cursorIndexed: typeof o.cursor_indexed === 'boolean' ? o.cursor_indexed : null,
    }
  })
  let modeChange: SourceModeChangeView | null = null
  if (p.op === 'mode') {
    const deps = asObj<Obj>(p.dependents)
    modeChange = {
      name: str(p.name) ?? '',
      tables: (Array.isArray(p.tables) ? p.tables : []).map((t): SourceModeTable => {
        const o = asObj<Obj>(t)
        const ws = str(o.schema) ?? str(o.warehouse_schema)
        const wt = str(o.table) ?? str(o.warehouse_table) ?? ''
        return {
          table: ws ? `${ws}.${wt}` : wt,
          from: str(o.from) ?? '',
          to: str(o.to) ?? '',
          cursor: str(o.cursor),
          mergeKey: strList(o.merge_key),
          estRows: num(o.est_rows) ?? 0,
          estMb: num(o.est_mb) ?? null,
        }
      }),
      notes: strList(p.notes),
      freshness: str(p.freshness),
      dependents: Object.keys(deps).map((k) => {
        const d = asObj<Obj>(deps[k])
        return { table: k, watches: num(d.watches) ?? 0, dashboards: num(d.dashboards) ?? 0,
          insights: num(d.insights) ?? 0, model: num(d.model_objects) ?? num(d.model) ?? 0 }
      }),
    }
  }
  return {
    modeChange,
    name: str(p.name) ?? '',
    kind: str(p.kind) ?? '',
    credential: str(p.credential),
    host: str(disp.host),
    port: num(disp.port),
    database: str(disp.database),
    tables,
    backfillStart: str(p.backfill_start),
    intervalMinutes: num(sched.interval_minutes),
    window: win,
    maxRows: num(caps.max_rows_per_run) ?? 0,
    maxSeconds: num(caps.max_seconds_per_run) ?? 0,
    estRows: num(est.rows) ?? 0,
    estStorageMb: num(est.storage_mb) ?? 0,
    estBackfillRuns: num(est.backfill_runs) ?? 0,
    schemaPolicy: str(p.schema_policy),
    freshnessMinutes: num(p.freshness_minutes),
    warnings: strList(p.warnings),
    csvFile: str(csv.file),
    csvHash: str(csv.sha256),
    csvOnReupload: str(csv.on_reupload),
    summary: str(p._summary),
  }
}

/* -------------------------------------------------------- publish_dashboard */

export interface PublishDashboardView {
  op: 'propose' | 'publish' | 'rollback'
  slug: string | null
  version: number | null
  fromVersion: number | null
  title: string | null
  changeNote: string | null
  note: string | null
  draftUrl: string | null
  draftPath: string | null
  queriesAdded: string[]
  queriesRemoved: string[]
  queriesChanged: string[]
  hasQueryDiff: boolean
  summary: string | null
  /* Phase 18 */
  format: 'spec' | 'html' | null
  tilesAdded: string[]
  tilesRemoved: string[]
  tilesChanged: string[]
  hasTileDiff: boolean
  tiles: SpecTileRef[]
  measures: string[]
  queryRefs: QueryRef[]
}

/* Phase 18: defensive readers for tile and query summaries. */
export function readTileRefs(v: unknown): SpecTileRef[] {
  if (!Array.isArray(v)) return []
  const out: SpecTileRef[] = []
  for (const t of v) {
    if (out.length >= 24) break
    if (!t || typeof t !== 'object' || Array.isArray(t)) continue
    const o = t as Obj
    const id = str(o.id)
    if (!id) continue
    const binding = o.binding === 'measure' || o.binding === 'query' ? o.binding : 'none'
    out.push({ id, type: str(o.type) ?? 'tile', title: str(o.title), binding, ref: str(o.ref) })
  }
  return out
}

export function readQueryRefs(v: unknown): QueryRef[] {
  if (!Array.isArray(v)) return []
  const out: QueryRef[] = []
  for (const q of v) {
    if (!q || typeof q !== 'object' || Array.isArray(q)) continue
    const o = q as Obj
    const id = str(o.id)
    if (id) out.push({ id, source: str(o.source) ?? 'sql' })
  }
  return out
}

export function readPublishDashboard(payload: unknown): PublishDashboardView {
  const p = asObj<Obj>(payload)
  const op = p.op === 'publish' || p.op === 'rollback' ? p.op : 'propose'
  const slug = str(p.slug)
  const version = num(p.version) ?? num(p.draft_version)
  const path = str(p.path)
  const draftUrl = str(p.draft_url) ?? (path && path.includes('?p=') ? path : null)
  let draftPath: string | null = null
  if (draftUrl) {
    try {
      draftPath = new URL(draftUrl, 'http://relative.invalid').pathname
    } catch {
      draftPath = null
    }
  }
  if (!draftPath && path) draftPath = path.split('?')[0] || null
  if (!draftPath && slug && version !== null) draftPath = `/d/${slug}/v/${version}/`
  return {
    op,
    slug,
    version,
    fromVersion: op === 'propose' ? null : num(p.from_version),
    title: str(p.title),
    changeNote: str(p.change_note),
    note: str(p.note),
    draftUrl,
    draftPath,
    queriesAdded: strList(p.queries_added),
    queriesRemoved: strList(p.queries_removed),
    queriesChanged: strList(p.queries_changed),
    hasQueryDiff: ['queries_added', 'queries_removed', 'queries_changed'].some((k) => k in p),
    summary: str(p._summary),
    format: p.format === 'spec' || p.format === 'html' ? p.format : null,
    tilesAdded: strList(p.tiles_added),
    tilesRemoved: strList(p.tiles_removed),
    tilesChanged: strList(p.tiles_changed),
    hasTileDiff: ['tiles_added', 'tiles_removed', 'tiles_changed'].some((k) => k in p),
    tiles: readTileRefs(p.tiles),
    measures: strList(p.measures),
    queryRefs: readQueryRefs(p.queries),
  }
}

export interface QueryDiff {
  added: string[]
  removed: string[]
  changed: string[]
}

export function queryDiff(draft: DashboardQuery[], current: DashboardQuery[]): QueryDiff {
  const d = new Map(draft.map((q) => [q.id, q]))
  const c = new Map(current.map((q) => [q.id, q]))
  const added: string[] = []
  const removed: string[] = []
  const changed: string[] = []
  for (const [id, q] of d) {
    const o = c.get(id)
    if (!o) added.push(id)
    else if (
      o.sql !== q.sql ||
      (o.refresh_minutes ?? null) !== (q.refresh_minutes ?? null) ||
      (o.source ?? null) !== (q.source ?? null) || // Phase 13.1
      (o.forecast_id ?? null) !== (q.forecast_id ?? null) ||
      (o.watch_id ?? null) !== (q.watch_id ?? null)
    )
      changed.push(id)
  }
  for (const id of c.keys()) if (!d.has(id)) removed.push(id)
  return { added: added.sort(), removed: removed.sort(), changed: changed.sort() }
}

/* Reads only the queries. The version's agent-authored HTML is never returned or kept. */
export async function fetchVersionQueries(slug: string, version: number): Promise<DashboardQuery[]> {
  const res = asObj<Obj>(
    await get<unknown>(`/api/dashboards/${encodeURIComponent(slug)}/versions/${version}`),
  )
  const qs = asObj<unknown>(res.queries)
  if (!Array.isArray(qs)) return []
  const out: DashboardQuery[] = []
  for (const q of qs as unknown[]) {
    if (!q || typeof q !== 'object') continue
    const o = q as Obj
    const id = str(o.id)
    if (!id) continue
    const title = str(o.title)
    const source = str(o.source) // Phase 13.1
    const forecastId = num(o.forecast_id)
    const watchId = num(o.watch_id)
    out.push({
      id,
      sql: typeof o.sql === 'string' ? o.sql : '',
      refresh_minutes: num(o.refresh_minutes),
      ...(title ? { title } : {}),
      ...(source ? { source } : {}),
      ...(forecastId !== null ? { forecast_id: forecastId } : {}),
      ...(watchId !== null ? { watch_id: watchId } : {}),
    })
  }
  return out
}

/* ----------------------------------------------------------------- preview */

export type PreviewResult =
  | { available: false }
  | { available: true; path: string; token: string; expiresAt: string | null }

export async function mintPreview(slug: string, version: number): Promise<PreviewResult> {
  try {
    const r = asObj<Obj>(
      await post<unknown>(`/api/dashboards/${encodeURIComponent(slug)}/versions/${version}/preview`),
    )
    const token = str(r.preview_token)
    if (!token) return { available: false }
    return {
      available: true,
      path: str(r.path) ?? `/d/${slug}/v/${version}/`,
      token,
      expiresAt: str(r.expires_at),
    }
  } catch (e) {
    if (e instanceof ApiError && (e.status === 404 || e.status === 405)) return { available: false }
    throw e
  }
}

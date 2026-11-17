import { get } from './api'

/* Quick 008: the Runs control center. One feed over the agent's task runs and
 * the background work the loops record (watch checks, dashboard refreshes, the
 * scheduler, triage, data intake, housekeeping). Shapes mirror runsapi.py;
 * titles and kind labels come from the backend, so this file only routes,
 * filters and formats. */

export type RunSource = 'agent' | 'job'
export type ActivityStatus = 'queued' | 'running' | 'done' | 'failed'

export interface ActivityItem {
  uid: string
  source: RunSource
  id: number | null
  kind: string
  kind_label: string
  family: string
  title: string
  name?: string
  trigger: string
  status: ActivityStatus
  summary: string | null
  error: string | null
  question: string | null
  at: string
  created_at: string | null
  started_at: string | null
  ended_at: string | null
  last_at: string | null
  duration_ms: number | null
  repeats: number
  routine?: boolean
  counts: Record<string, unknown>
  step: string | null
  skills_used?: string[]
  thread_id: string | null
  chat: string | null
  case_id: number | null
  playbook_id: number | null
  playbook_name: string | null
  task_run_id: number | null
  trace_id: string | null
  ref_type: string | null
  ref_id: string | null
  dashboard_slug?: string | null
  message?: string | null
  ref?: RefLink | null
}

export interface RefLink {
  type: string
  id: string
  label: string | null
  to: string | null
}

export interface ActivityPage {
  items: ActivityItem[]
  next_before: string | null
}

export interface ActivityNow {
  running: ActivityItem[]
  queued: ActivityItem[]
  failed: ActivityItem[]
  counts: { running: number; queued: number; failed_24h: number; done_24h: number }
}

export interface RunFilters {
  kind: string
  status: string
  since: string
  ref: string
}

export const FAMILIES: { value: string; label: string }[] = [
  { value: '', label: 'Everything' },
  { value: 'agent', label: 'Chat & messages' },
  { value: 'playbooks', label: 'Playbooks' },
  { value: 'watches', label: 'Watches' },
  { value: 'dashboards', label: 'Dashboards' },
  { value: 'triage', label: 'Triage' },
  { value: 'intake', label: 'Data intake' },
  { value: 'cases', label: 'Cases' },
  { value: 'forecasts', label: 'Forecasts' },
  { value: 'housekeeping', label: 'Housekeeping' },
  { value: 'system', label: 'System' },
]

export const STATUSES: { value: string; label: string }[] = [
  { value: '', label: 'Any status' },
  { value: 'running', label: 'Running' },
  { value: 'queued', label: 'Waiting' },
  { value: 'failed', label: 'Failed' },
  { value: 'done', label: 'Done' },
]

export const WINDOWS: { value: string; label: string }[] = [
  { value: '', label: 'Any time' },
  { value: '1h', label: 'Last hour' },
  { value: '24h', label: 'Last 24 hours' },
  { value: '7d', label: 'Last 7 days' },
  { value: '30d', label: 'Last 30 days' },
]

const FILTER_KEYS: (keyof RunFilters)[] = ['kind', 'status', 'since', 'ref']

export function readFilters(params: URLSearchParams): RunFilters {
  return {
    kind: params.get('kind') ?? '',
    status: params.get('status') ?? '',
    since: params.get('since') ?? '',
    ref: params.get('ref') ?? '',
  }
}

export function filterQuery(f: RunFilters, extra: Record<string, string> = {}): string {
  const q = new URLSearchParams()
  for (const k of FILTER_KEYS) if (f[k]) q.set(k, f[k])
  for (const [k, v] of Object.entries(extra)) if (v) q.set(k, v)
  const s = q.toString()
  return s ? `?${s}` : ''
}

export function activityPath(f: RunFilters, before = '', limit = 50): string {
  return `/api/activity${filterQuery(f, { before, limit: String(limit) })}`
}

export const loadActivity = (f: RunFilters, before = '') => get<ActivityPage>(activityPath(f, before))
export const loadNow = () => get<ActivityNow>('/api/activity/now')

/** Route of one item on the Runs page: agent runs keep their numeric id. */
export function runRoute(item: Pick<ActivityItem, 'source' | 'id' | 'uid'>, search = ''): string {
  if (item.source === 'agent' && item.id != null) return `/system/runs/${item.id}${search}`
  if (item.id != null) return `/system/runs/job-${item.id}${search}`
  return `/system/runs${search}`
}

/** `123` is an agent run, `job-45` a background run. */
export function parseRunId(id: string | undefined): { source: RunSource; id: number } | null {
  if (!id) return null
  const job = /^job-(\d+)$/.exec(id)
  if (job) return { source: 'job', id: Number(job[1]) }
  if (/^\d+$/.test(id)) return { source: 'agent', id: Number(id) }
  return null
}

/** Deep link from other pages: the Runs page filtered on one object. */
export function runsHref(ref: string, extra: Partial<RunFilters> = {}): string {
  return `/system/runs${filterQuery({ kind: '', status: '', since: '', ...extra, ref })}`
}

const REF_WORDS: Record<string, string> = {
  watch: 'watch',
  playbook: 'playbook',
  dashboard: 'dashboard',
  case: 'case',
  document: 'document',
  forecast: 'forecast',
  health: 'health check',
  loop: 'loop',
  task_run: 'run',
}

export function refLabel(ref: string): string {
  const [type, ...rest] = ref.split(':')
  const id = rest.join(':')
  return `${REF_WORDS[type] ?? type} ${id.replace(/_/g, ' ')}`.trim()
}

export function durationText(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(ms)) return ''
  if (ms < 1000) return `${Math.round(ms)} ms`
  const s = ms / 1000
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)} s`
  const m = Math.floor(s / 60)
  const rest = Math.round(s % 60)
  if (m < 60) return rest ? `${m} min ${rest} s` : `${m} min`
  const h = Math.floor(m / 60)
  return `${h} h ${m % 60} min`
}

/** How long something has been running, from its start time. */
export function elapsedSince(ts: string | null | undefined, now = Date.now()): string {
  if (!ts) return ''
  const t = new Date(ts).getTime()
  if (Number.isNaN(t)) return ''
  return durationText(Math.max(0, now - t))
}

export function statusLabel(status: string): string {
  return status === 'queued' ? 'waiting' : status
}

/** Chat-formatting marks (*bold*, `code`) read as noise in a one-line preview. */
export function plainText(text: string | null | undefined): string {
  return (text || '').replace(/[*`]+/g, '').replace(/\s+/g, ' ').trim()
}

/** The one line under a title: what came out of it. */
export function outcomeLine(item: ActivityItem): string {
  if (item.status === 'running') return plainText(item.step || item.summary) || 'Working on it'
  if (item.status === 'queued') return 'Waiting for a free worker'
  return plainText(item.error || item.summary || item.question)
}

/** Counts worth showing (no zero noise, no long values). */
export function countEntries(counts: Record<string, unknown> | null | undefined): [string, string][] {
  if (!counts) return []
  const out: [string, string][] = []
  for (const [k, v] of Object.entries(counts)) {
    if (v === null || v === undefined || v === '') continue
    out.push([k.replace(/_/g, ' '), typeof v === 'object' ? JSON.stringify(v) : String(v)])
  }
  return out
}

export function busy(now: ActivityNow | null | undefined): boolean {
  return !!now && (now.counts.running > 0 || now.counts.queued > 0)
}

/** Merge a fresh first page with older pages already loaded (newest first, no repeats). */
export function mergePages(first: ActivityItem[], older: ActivityItem[]): ActivityItem[] {
  const seen = new Set(first.map((i) => i.uid))
  return [...first, ...older.filter((i) => !seen.has(i.uid))]
}

/* Quick 019: the memory a run used (GET /api/runs/{id}/memory, shapes mirror
 * runsapi.memory_view): which lessons, learnings, knowledge-graph items, open
 * cases and group context fed its memory message, and what it stored after. */

export type MemoryStatus = 'active' | 'superseded' | 'deleted' | string

export interface UsedLesson {
  id: number | null
  text: string
  status?: MemoryStatus | null
  status_reason?: string | null
}

export interface UsedLearning extends UsedLesson {
  kind: string | null
  source: string | null
}

export interface UsedGraphItem {
  uuid: string | null
  kind: 'fact' | 'entity' | 'episode' | string
  group_id: string | null
  block: 'global' | 'group' | string
  text: string
}

export interface UsedCase {
  id: number | null
  text: string
}

export interface MemoryUsed {
  lessons: UsedLesson[]
  learnings: UsedLearning[]
  graph: UsedGraphItem[]
  cases: UsedCase[]
  group: { purpose: boolean; members: boolean }
  chars: number
  budget_chars: number
  dropped: number
}

export interface StoredLearning {
  id: number
  kind: string
  content: string
  source: string | null
  outcome: 'new' | 'updated'
  status: MemoryStatus
  status_reason: string | null
  updated_at: string | null
}

export interface RunMemory {
  run_id: number
  recorded: boolean
  used: MemoryUsed | null
  stored: StoredLearning[]
}

export const loadRunMemory = (id: number) => get<RunMemory>(`/api/runs/${id}/memory`)

/** How many memory items reached the run (group purpose and roster count once each). */
export function memoryItemCount(used: MemoryUsed | null | undefined): number {
  if (!used) return 0
  return (
    (used.lessons?.length ?? 0) +
    (used.learnings?.length ?? 0) +
    (used.graph?.length ?? 0) +
    (used.cases?.length ?? 0) +
    (used.group?.purpose ? 1 : 0) +
    (used.group?.members ? 1 : 0)
  )
}

/** The Memory panel's header line. */
export function memoryMeta(m: RunMemory | null | undefined): string | undefined {
  if (!m) return undefined
  if (!m.recorded || !m.used) return m.stored.length ? `learned ${m.stored.length}` : 'none recorded'
  const n = memoryItemCount(m.used)
  const parts = [n === 1 ? '1 item' : `${n} items`]
  if (m.stored.length) parts.push(`learned ${m.stored.length}`)
  return parts.join(' · ')
}

/* Same words as the Memory page's search results (memgraph.recallKind). */
const GRAPH_KIND: Record<string, string> = { fact: 'Fact', entity: 'Entity', episode: 'Episode' }

export function graphKindLabel(kind: string | null | undefined): string {
  return GRAPH_KIND[kind ?? ''] ?? (kind ? kind[0].toUpperCase() + kind.slice(1) : 'Fact')
}

export function kindLabel(kind: string | null | undefined): string {
  const k = kind || 'fact'
  return k[0].toUpperCase() + k.slice(1)
}

export function graphScopeLabel(item: Pick<UsedGraphItem, 'block' | 'group_id'>): string {
  if (item.block === 'group') return 'this group'
  if (item.group_id && item.group_id !== 'global') return item.group_id.replace(/_/g, ' ')
  return 'whole business'
}

/** A used learning or lesson that is no longer live, in words ('' when it still is). */
export function retiredLabel(status: MemoryStatus | null | undefined): string {
  if (!status || status === 'active') return ''
  if (status === 'deleted') return 'deleted since'
  return `${status} since`
}

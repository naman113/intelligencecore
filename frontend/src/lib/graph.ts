import { get, post } from './api'
import { plural } from './format'

/* Quick 009: the knowledge graph's real state, from GET /api/memory/status.
 * The gateway pings a connected graph and attempts a due reconnect on every
 * read, so this is never a stale "connected". */

export type GraphState = 'connected' | 'empty' | 'not_connected' | 'disabled'

export interface BackfillCounts {
  sent: number
  already: number
  failed: number
}

export interface GraphStatus {
  state: GraphState | string
  reason?: string
  host?: string
  connected_since?: string | null
  last_attempt?: string | null
  next_retry_in?: number | null
  failures?: number
  episodes?: number | null
  scopes?: number | null
  /* Quick 014: what extraction made of the episodes, and how it went */
  entities?: number | null
  facts?: number | null
  extraction?: {
    ok?: number
    failed?: number
    duplicates?: number
    last_error?: string
    last_error_at?: string | null
    last_ok_at?: string | null
    last?: { name?: string; group_id?: string; entities?: number; facts?: number } | null
  }
  rebuild?: {
    running?: boolean
    phase?: string
    total?: number
    done?: number
    failed?: number
    finished_at?: string | null
    result?: { ingested?: number; failed?: number; stopped?: string | null; after?: Record<string, { entities?: number; facts?: number }> | null }
  }
  pending?: { learnings?: number; cases?: number }
  backfill?: {
    running?: boolean
    started_at?: string
    finished_at?: string | null
    stopped?: string | null
    learnings?: BackfillCounts
    cases?: BackfillCounts
  }
}

export const loadGraphStatus = () => get<GraphStatus>('/api/memory/status')
export const reconnectGraph = () => post<GraphStatus>('/api/memory/reconnect', {})
export const startGraphBackfill = (limit = 50) =>
  post<GraphStatus & { started: boolean }>(`/api/memory/backfill?limit=${limit}`, {})

export function pendingTotal(s: GraphStatus | null): number {
  return (s?.pending?.learnings ?? 0) + (s?.pending?.cases ?? 0)
}

export interface GraphLine {
  tone: 'settled' | 'owed' | 'broken' | 'neutral'
  label: string
  detail: string
}

/* One plain line: "Knowledge graph: <label>" plus why. */
export function describeGraph(s: GraphStatus | null, error?: string | null): GraphLine {
  if (!s) {
    return { tone: 'neutral', label: 'checking', detail: error ? `status unavailable: ${error}` : '' }
  }
  switch (s.state) {
    case 'connected': {
      const n = s.episodes
      const linked =
        s.entities != null && s.facts != null
          ? `: ${plural(s.entities, 'entity', 'entities')} linked by ${plural(s.facts, 'fact')}`
          : ''
      const detail =
        n == null
          ? 'connected'
          : `${plural(n, 'episode')}${s.scopes ? ` in ${plural(s.scopes, 'scope')}` : ''}${linked}`
      return { tone: 'settled', label: 'connected', detail }
    }
    case 'empty':
      return {
        tone: 'owed',
        label: 'empty',
        detail: 'connected, but nothing has been written yet. The interview, closed cases and learned facts write here.',
      }
    case 'disabled':
      return { tone: 'neutral', label: 'turned off', detail: s.reason || 'disabled on the Setup page' }
    default: {
      const retry =
        s.next_retry_in != null && s.next_retry_in > 0 ? ` Retrying by itself in ${s.next_retry_in} s.` : ''
      return {
        tone: 'broken',
        label: 'not connected',
        detail: `${s.reason || 'unknown reason'}.${retry}`,
      }
    }
  }
}

/* Quick 014: extraction failures are never silent. Counted since the
 * gateway started; the reason is the last failure's first line. */
export function extractionNote(s: GraphStatus | null): string {
  const x = s?.extraction
  if (!x?.failed) return ''
  return `${plural(x.failed, 'episode')} could not be extracted since the last restart${
    x.last_error ? ` (last: ${x.last_error})` : ''
  }. They are retried by the backfill; System > Runs lists them.`
}

export function rebuildNote(s: GraphStatus | null): string {
  const r = s?.rebuild
  if (!r || (!r.running && !r.finished_at)) return ''
  if (r.running) return `Rebuilding the graph: ${r.phase ?? 'working'}${r.total ? `, ${r.done ?? 0} of ${r.total} episodes` : ''}.`
  const res = r.result ?? {}
  if (res.stopped) return `Last rebuild stopped: ${res.stopped}.`
  return `Last rebuild: ${plural(res.ingested ?? 0, 'episode')} re-ingested${res.failed ? `, ${res.failed} failed` : ''}.`
}

export function backfillNote(s: GraphStatus | null): string {
  const b = s?.backfill
  if (!b || (!b.running && !b.finished_at)) return ''
  const sent = (b.learnings?.sent ?? 0) + (b.cases?.sent ?? 0)
  const already = (b.learnings?.already ?? 0) + (b.cases?.already ?? 0)
  const failed = (b.learnings?.failed ?? 0) + (b.cases?.failed ?? 0)
  if (b.running) return `Copying into the graph: ${sent} sent so far.`
  const parts = [`${sent} sent`]
  if (already) parts.push(`${already} already there`)
  if (failed) parts.push(`${failed} failed`)
  return `Last copy: ${parts.join(', ')}${b.stopped ? ` (stopped: ${b.stopped})` : ''}.`
}

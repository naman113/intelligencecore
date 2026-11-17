/* Context-pack upload helpers. Plumbing only: the UI forwards the file and a
 * constant instruction; the agent (context specialist + skill) does the reading
 * and proposing. The filename is never put in the instruction. */
import { asObj } from './api'
import { asks } from './ask'
import type { Action } from './types'

export const PACK_ACCEPT = '.md,.txt,.json,.yaml,.yml'
export const PACK_EXTS = ['.md', '.txt', '.json', '.yaml', '.yml']
export const PACK_MAX_BYTES = 2_000_000

export function validatePack(file: { name: string; size: number }): string | null {
  const lower = file.name.toLowerCase()
  if (!PACK_EXTS.some((e) => lower.endsWith(e))) {
    return 'Choose a ' + PACK_EXTS.join(', ') + ' file.'
  }
  if (file.size > PACK_MAX_BYTES) {
    return 'That file is over 2 MB. Split it into smaller packs.'
  }
  return null
}

export function buildPackForm(file: Blob, name: string, operator: string): FormData {
  const form = new FormData()
  form.append('file', file, name)
  form.append('operator', operator)
  form.append('instruction', asks.importContextPack())
  return form
}

export const BULK_KINDS = ['context_change', 'create_watch', 'hide_table']

export interface ImportGroup {
  runId: number
  ids: number[]
  kinds: string[]
}

export function importGroups(actions: Action[]): ImportGroup[] {
  const byRun = new Map<number, ImportGroup>()
  for (const a of actions) {
    if (a.status !== 'proposed' || a.task_run_id == null) continue
    if (!BULK_KINDS.includes(a.kind)) continue
    const runId = Number(a.task_run_id)
    const g = byRun.get(runId) ?? { runId, ids: [], kinds: [] }
    g.ids.push(a.id)
    if (!g.kinds.includes(a.kind)) g.kinds.push(a.kind)
    byRun.set(runId, g)
  }
  return [...byRun.values()].filter((g) => g.ids.length >= 2).sort((a, b) => b.runId - a.runId)
}

export function hideTableText(payload: unknown): { ref: string; reason: string } {
  const p = asObj<Record<string, unknown>>(payload)
  return { ref: String(p.ref ?? ''), reason: String(p.reason ?? '') }
}

import type { ConfigResponse } from './types'

/* Phase 13 (GOLIVE-01): model-name suggestions come from the backend catalog
 * (/api/config options), never a hard-coded Claude list. */
export function modelSuggestions(cfg?: ConfigResponse | null): string[] {
  const current: string[] = []
  const legacy: string[] = []
  for (const fields of Object.values(cfg?.config ?? {})) {
    for (const f of fields) {
      for (const o of f.options ?? []) {
        const bucket = o.group === 'current' ? current : legacy
        if (!current.includes(o.value) && !legacy.includes(o.value)) bucket.push(o.value)
      }
    }
  }
  return ['auto', ...current, ...legacy, 'gpt-4o', 'gpt-4o-mini']
}

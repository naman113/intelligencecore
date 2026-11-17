import { get } from './api'

/* Phase 11: GOLIVE-05. Open system-health alerts from plan 11-02's API. */

export interface HealthAlert {
  id: number
  component: string
  title: string
  detail: string
  created_at: string
  resolved_at: string | null
}

export async function loadHealthAlerts(): Promise<HealthAlert[]> {
  try {
    const r = await get<{ alerts?: HealthAlert[] }>('/api/health/alerts?include_resolved=false&limit=20')
    return r?.alerts ?? []
  } catch {
    return []
  }
}

/* Phase 11: GOLIVE-05 health page */
export interface HealthCheck {
  state: 'ok' | 'failing' | 'starting' | 'unknown' // Phase 14 (WH-08)
  detail: string
}
export interface HealthOverview {
  alerts: HealthAlert[]
  checks: Record<string, HealthCheck>
}

export async function loadHealthOverview(): Promise<HealthOverview> {
  const r = await get<Partial<HealthOverview>>('/api/health/alerts?include_resolved=true&limit=20')
  return { alerts: r?.alerts ?? [], checks: r?.checks ?? {} }
}

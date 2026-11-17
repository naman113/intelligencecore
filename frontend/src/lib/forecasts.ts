/* Phase 13.1: forecast definitions. Types and loaders; owed state is still computed only in lib/owed.tsx. */
import { get, patch, post } from './api'

export type ForecastStatus = 'active' | 'paused' | 'retired'

export interface ForecastBrief {
  id: number
  name: string
  grain: string
  horizon: number
  season: number | null
  kind: string
  status: ForecastStatus
  last_run_at: string | null
  last_ok_at: string | null
  next_run_at: string | null
  last_error: string | null
  failing_since: string | null
  method: string | null
  wape: number | null
  mae: number | null
  coverage: number | null
}

export interface ForecastActual {
  period: string
  value: number
}
export interface ForecastPoint {
  period: string
  mean: number
  low: number
  high: number
}
export interface ForecastChange {
  previous_as_of: string | null
  method_changed: boolean
  previous_method: string | null
  total_forecast_now: number | null
  total_forecast_before: number | null
  last_actual_vs_previous_forecast: {
    period: string
    actual: number
    forecast: number
    miss_pct: number | null
  } | null
}
export interface ForecastDetail {
  forecast_id: number
  name: string
  grain: string
  horizon: number
  season: number | null
  kind: string
  status: ForecastStatus
  sql: string
  last_error: string | null
  failing_since: string | null
  next_run_at: string | null
  as_of: string | null
  method: string | null
  baseline: string | null
  mae: number | null
  mape: number | null
  wape: number | null
  baseline_wape: number | null
  coverage: number | null
  notes: string[]
  actuals: ForecastActual[]
  forecast: ForecastPoint[]
  change: ForecastChange | null
}

type Obj = Record<string, unknown>

const obj = (v: unknown): Obj => {
  if (typeof v === 'string') {
    try {
      return obj(JSON.parse(v))
    } catch {
      return {}
    }
  }
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Obj) : {}
}
const arr = (v: unknown): unknown[] => {
  if (typeof v === 'string') {
    try {
      return arr(JSON.parse(v))
    } catch {
      return []
    }
  }
  return Array.isArray(v) ? v : []
}
const num = (v: unknown): number | null => {
  if (typeof v === 'number' && Number.isFinite(v)) return v
  if (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v))) return Number(v)
  return null
}
const str = (v: unknown): string | null => (typeof v === 'string' && v.trim() ? v : null)
const status = (v: unknown): ForecastStatus => (v === 'paused' || v === 'retired' ? v : 'active')

export function decodeForecastBrief(v: unknown): ForecastBrief {
  const o = obj(v)
  return {
    id: num(o.id) ?? num(o.forecast_id) ?? 0,
    name: str(o.name) ?? 'Forecast',
    grain: str(o.grain) ?? 'day',
    horizon: num(o.horizon) ?? 0,
    season: num(o.season),
    kind: str(o.kind) ?? 'quantity',
    status: status(o.status),
    last_run_at: str(o.last_run_at),
    last_ok_at: str(o.last_ok_at),
    next_run_at: str(o.next_run_at),
    last_error: str(o.last_error),
    failing_since: str(o.failing_since),
    method: str(o.method),
    wape: num(o.wape),
    mae: num(o.mae),
    coverage: num(o.coverage),
  }
}

export function decodeForecastList(v: unknown): ForecastBrief[] {
  const list = Array.isArray(v) ? v : arr(obj(v).forecasts)
  return list.map(decodeForecastBrief)
}

export function decodeForecastDetail(v: unknown): ForecastDetail {
  const o = obj(v)
  const ch = o.change == null ? null : obj(o.change)
  const lav = ch && ch.last_actual_vs_previous_forecast ? obj(ch.last_actual_vs_previous_forecast) : null
  return {
    forecast_id: num(o.forecast_id) ?? num(o.id) ?? 0,
    name: str(o.name) ?? 'Forecast',
    grain: str(o.grain) ?? 'day',
    horizon: num(o.horizon) ?? 0,
    season: num(o.season),
    kind: str(o.kind) ?? 'quantity',
    status: status(o.status),
    sql: typeof o.sql === 'string' ? o.sql : '',
    last_error: str(o.last_error),
    failing_since: str(o.failing_since),
    next_run_at: str(o.next_run_at),
    as_of: str(o.as_of),
    method: str(o.method),
    baseline: str(o.baseline),
    mae: num(o.mae),
    mape: num(o.mape),
    wape: num(o.wape),
    baseline_wape: num(o.baseline_wape),
    coverage: num(o.coverage),
    notes: arr(o.notes).filter((x) => typeof x === 'string') as string[],
    actuals: arr(o.actuals).flatMap((a) => {
      const r = obj(a)
      const value = num(r.value)
      return value === null ? [] : [{ period: String(r.period ?? ''), value }]
    }),
    forecast: arr(o.forecast).flatMap((a) => {
      const r = obj(a)
      const mean = num(r.mean)
      if (mean === null) return []
      return [{ period: String(r.period ?? ''), mean, low: num(r.low) ?? mean, high: num(r.high) ?? mean }]
    }),
    change: ch
      ? {
          previous_as_of: str(ch.previous_as_of),
          method_changed: ch.method_changed === true,
          previous_method: str(ch.previous_method),
          total_forecast_now: num(ch.total_forecast_now),
          total_forecast_before: num(ch.total_forecast_before),
          last_actual_vs_previous_forecast: lav
            ? {
                period: String(lav.period ?? ''),
                actual: num(lav.actual) ?? 0,
                forecast: num(lav.forecast) ?? 0,
                miss_pct: num(lav.miss_pct),
              }
            : null,
        }
      : null,
  }
}

/** A definition is failing only while it is active and the last night left an error. */
export function isFailing(f: { status: string; last_error: string | null }): boolean {
  return f.status === 'active' && !!f.last_error && f.last_error.trim() !== ''
}

export async function loadForecasts(includeRetired = false): Promise<ForecastBrief[]> {
  const r = await get<unknown>(`/api/forecasts?limit=50${includeRetired ? '&status=all' : ''}`)
  return decodeForecastList(r)
}
export async function loadForecast(id: number): Promise<ForecastDetail> {
  return decodeForecastDetail(await get<unknown>(`/api/forecasts/${id}`))
}
export const setForecastStatus = (id: number, s: ForecastStatus) =>
  patch<unknown>(`/api/forecasts/${id}`, { status: s })
export const runForecastNow = (id: number) => post<unknown>(`/api/forecasts/${id}/run`)

const METHODS: Record<string, string> = {
  seasonal_naive: 'same period last season',
  moving_avg: 'moving average',
  naive: 'last value',
  ets: 'trend and season smoothing',
  arima: 'ARIMA',
}
export const methodLabel = (m: string | null): string =>
  m ? (METHODS[m] ?? m.replace(/_/g, ' ')) : 'not run yet'

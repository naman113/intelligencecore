/* Semantic model (Phase 16): types and defensive readers. Every field is
 * agent- or database-written, so nothing is trusted: unknown shapes become
 * null or empty arrays, numbers are parsed safely, and strings are only ever
 * rendered as text nodes. */

export interface ModelTableRow {
  name: string
  relation: string
  objectKind: string
  role: string | null
  grain: string | null
  primaryKey: string[]
  aggTimeDimension: string | null
  label: string | null
  description: string | null
  derivedSql: string | null
  inWarehouse: boolean | null
  status: string | null
}

export interface ModelRelationshipRow {
  name: string
  fromRelation: string
  fromColumns: string[]
  toRelation: string
  toColumns: string[]
  cardinality: string | null
  origin: string | null
  matchRate: number | null
  description: string | null
}

export interface ModelMeasureRow {
  name: string
  label: string | null
  description: string | null
  type: string
  baseRelation: string | null
  agg: string | null
  expr: string | null
  aggTimeDimension: string | null
  filters: unknown[]
  unit: string | null
  format: string | null
  numerator: unknown
  denominator: unknown
  inputMetrics: unknown[]
  compiledSql: string | null
  compileError: string | null
}

export interface ModelPending {
  actionId: number
  object: string | null
  name: string | null
  op: string | null
  summary: string | null
}

export interface ModelSample {
  day: string
  value: number | null
}

export interface ModelChange {
  summary: string | null
  object: string | null
  name: string | null
  op: string | null
  definition: Record<string, unknown>
  before: Record<string, unknown> | null
  sql: string | null
  samples: ModelSample[]
  sampleValue: number | null
  sampleRange: { start: string; end: string } | null
  fromRows: number | null
  fromNonnull: number | null
  matched: number | null
  matchRate: number | null
  duplicateKeys: number | null
  nullKeys: number | null
  rowCount: number | null
  distinctKeys: number | null
  keyUnique: boolean | null
  rowsPerDay: number | null
  note: string | null
  columns: string[]
  previewRows: Record<string, unknown>[]
  error: string | null
}

const isObj = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v)

const str = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null)

export function num(v: unknown): number | null {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  if (typeof v === 'string' && v.trim() !== '') {
    const n = Number(v)
    return Number.isFinite(n) ? n : null
  }
  return null
}

const strList = (v: unknown): string[] =>
  Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : [])
const rows = (v: unknown): Record<string, unknown>[] => arr(v).filter(isObj)

export function readModelTables(raw: unknown): ModelTableRow[] {
  return rows(raw).map((t) => ({
    name: str(t.name) ?? '',
    relation: str(t.relation) ?? '',
    objectKind: str(t.object_kind) ?? 'base',
    role: str(t.role),
    grain: str(t.grain),
    primaryKey: strList(t.primary_key),
    aggTimeDimension: str(t.agg_time_dimension),
    label: str(t.label),
    description: str(t.description),
    derivedSql: str(t.derived_sql),
    inWarehouse: typeof t.in_warehouse === 'boolean' ? t.in_warehouse : null,
    status: str(t.status),
  }))
}

export function readModelRelationships(raw: unknown): ModelRelationshipRow[] {
  return rows(raw).map((r) => ({
    name: str(r.name) ?? '',
    fromRelation: str(r.from_relation) ?? '',
    fromColumns: strList(r.from_columns),
    toRelation: str(r.to_relation) ?? '',
    toColumns: strList(r.to_columns),
    cardinality: str(r.cardinality),
    origin: str(r.origin),
    matchRate: num(r.match_rate),
    description: str(r.description),
  }))
}

export function readModelMeasures(raw: unknown): ModelMeasureRow[] {
  return rows(raw).map((m) => ({
    name: str(m.name) ?? '',
    label: str(m.label),
    description: str(m.description),
    type: str(m.type) ?? '',
    baseRelation: str(m.base_relation),
    agg: str(m.agg),
    expr: str(m.expr),
    aggTimeDimension: str(m.agg_time_dimension),
    filters: arr(m.filter),
    unit: str(m.unit),
    format: str(m.format),
    numerator: m.numerator ?? null,
    denominator: m.denominator ?? null,
    inputMetrics: arr(m.input_metrics),
    compiledSql: str(m.compiled_sql),
    compileError: str(m.compile_error),
  }))
}

export function readModelPending(raw: unknown): ModelPending[] {
  return rows(raw).map((p) => ({
    actionId: num(p.action_id) ?? 0,
    object: str(p.object),
    name: str(p.name),
    op: str(p.op),
    summary: str(p.summary),
  }))
}

export function readModelChange(payload: unknown): ModelChange {
  const p = isObj(payload) ? payload : {}
  const ev = isObj(p.evidence) ? p.evidence : {}
  const range = isObj(ev.sample_range) ? ev.sample_range : null
  const samples: ModelSample[] = rows(ev.samples).map((s) => ({
    day: str(s.day) ?? '',
    value: num(s.value),
  }))
  const start = range ? str(range.start) : null
  const end = range ? str(range.end) : null
  return {
    summary: str(p._summary),
    object: str(p.object),
    name: str(p.name),
    op: str(p.op),
    definition: isObj(p.definition) ? p.definition : {},
    before: isObj(p.before) ? p.before : null,
    sql: str(ev.sql),
    samples,
    sampleValue: num(ev.sample_value),
    sampleRange: start && end ? { start, end } : null,
    fromRows: num(ev.from_rows),
    fromNonnull: num(ev.from_nonnull),
    matched: num(ev.matched),
    matchRate: num(ev.match_rate),
    duplicateKeys: num(ev.duplicate_keys),
    nullKeys: num(ev.null_keys),
    rowCount: num(ev.row_count),
    distinctKeys: num(ev.distinct_keys),
    keyUnique: typeof ev.key_unique === 'boolean' ? ev.key_unique : null,
    rowsPerDay: num(ev.rows_per_day),
    note: str(ev.note),
    columns: strList(ev.columns),
    previewRows: rows(ev.preview_rows),
    error: str(ev.error),
  }
}

export function fmtNum(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return '—'
  return n.toLocaleString('en-US', { maximumFractionDigits: 2 })
}

/** "99.8% (2,180 of 2,184 keys match)". */
export function formatMatchRate(rate: number | null, matched: number | null, nonnull: number | null): string {
  if (rate == null) return 'Not measured'
  const pct = `${(rate * 100).toLocaleString('en-US', { maximumFractionDigits: 1 })}%`
  if (matched == null || nonnull == null) return pct
  return `${pct} (${fmtNum(matched)} of ${fmtNum(nonnull)} keys match)`
}

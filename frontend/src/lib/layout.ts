/* Owner layout editor state (Phase 22, DCAP-15). Pure: no React, no network.
 * The data is an ordered tile-id list per section plus one width per tile, exactly what
 * POST /api/dashboards/{slug}/layout takes, so drag-and-drop can be added later without
 * changing it. */

export type Width = 'quarter' | 'third' | 'half' | 'two_thirds' | 'full'
export const WIDTHS: Width[] = ['quarter', 'third', 'half', 'two_thirds', 'full']
export const WIDTH_LABEL: Record<Width, string> = {
  quarter: 'Quarter',
  third: 'Third',
  half: 'Half',
  two_thirds: 'Two thirds',
  full: 'Full',
}

export const PRESET_LABEL: Record<string, string> = {
  today: 'Today',
  yesterday: 'Yesterday',
  last_7_days: 'Last 7 days',
  last_30_days: 'Last 30 days',
  last_90_days: 'Last 90 days',
  this_month: 'This month',
  last_month: 'Last month',
  this_quarter: 'This quarter',
  this_fy: 'This financial year',
}
export const ALL_PRESETS = Object.keys(PRESET_LABEL)

export const TYPE_LABEL: Record<string, string> = {
  stat: 'Stat',
  table: 'Table',
  text: 'Text',
  line: 'Line',
  area: 'Area',
  bar: 'Bars',
  stacked_bar: 'Stacked bars',
  sparkline: 'Sparkline',
  combo: 'Bars and line',
  paired_bar: 'In and out bars',
  aging: 'Ageing',
  bullet: 'Bullet',
  waterfall: 'Waterfall',
  calendar: 'Calendar',
  donut: 'Donut',
  ranked_bar: 'Ranked bars',
  funnel: 'Funnel',
}

/** C-01 defaults when a tile carries no width. */
const DEFAULT_WIDTH: Record<string, Width> = {
  stat: 'quarter',
  sparkline: 'quarter',
  text: 'full',
  table: 'full',
  calendar: 'full',
}
export const MAX_SECTION_TILES = 16

export interface LTile {
  id: string
  title: string
  type: string
  width: Width
  hidden: boolean
}
export interface LSection {
  title: string
  tiles: LTile[]
}
export interface LayoutState {
  sections: LSection[]
  defaultRange: string | null
  /** Offered ranges, or null for all nine. */
  presets: string[] | null
  hasDateControl: boolean
}
export interface MoveResult {
  state: LayoutState
  reason: string | null
}

export interface LayoutChanges {
  base_version: number
  sections: { title: string | null; tiles: string[] }[]
  widths: Record<string, Width>
  titles: Record<string, string>
  hidden: string[]
  default_range: string | null
  note: string | null
}

interface RawTile {
  id?: unknown
  type?: unknown
  title?: unknown
  width?: unknown
  hidden?: unknown
}
interface RawSpec {
  tiles?: RawTile[]
  sections?: { title?: unknown; tiles?: unknown }[]
  date_control?: { default?: unknown; presets?: unknown } | null
}

function isWidth(w: unknown): w is Width {
  return typeof w === 'string' && (WIDTHS as string[]).includes(w)
}

export function fromSpec(spec: unknown): LayoutState {
  const s = (spec ?? {}) as RawSpec
  const byId = new Map<string, LTile>()
  for (const t of s.tiles ?? []) {
    const id = String(t.id ?? '')
    const type = String(t.type ?? '')
    byId.set(id, {
      id,
      type,
      title: typeof t.title === 'string' ? t.title : '',
      width: isWidth(t.width) ? t.width : (DEFAULT_WIDTH[type] ?? 'half'),
      hidden: t.hidden === true,
    })
  }
  const sections: LSection[] = (s.sections ?? []).map((sec) => ({
    title: typeof sec.title === 'string' ? sec.title : '',
    tiles: (Array.isArray(sec.tiles) ? sec.tiles : [])
      .map((id) => byId.get(String(id)))
      .filter((t): t is LTile => !!t),
  }))
  const dc = s.date_control
  return {
    sections,
    defaultRange: dc && typeof dc.default === 'string' ? dc.default : null,
    presets: dc && Array.isArray(dc.presets) ? (dc.presets as string[]) : null,
    hasDateControl: !!dc,
  }
}

export function offeredRanges(st: LayoutState): string[] {
  return st.presets ?? ALL_PRESETS
}

function locate(st: LayoutState, id: string): [number, number] | null {
  for (let i = 0; i < st.sections.length; i++) {
    const j = st.sections[i].tiles.findIndex((t) => t.id === id)
    if (j >= 0) return [i, j]
  }
  return null
}

function clone(st: LayoutState): LayoutState {
  return { ...st, sections: st.sections.map((s) => ({ ...s, tiles: s.tiles.map((t) => ({ ...t })) })) }
}

const same = (st: LayoutState, reason: string | null = null): MoveResult => ({ state: st, reason })

export function canMoveUp(st: LayoutState, id: string): boolean {
  const at = locate(st, id)
  if (!at) return false
  return !(at[0] === 0 && at[1] === 0)
}
export function canMoveDown(st: LayoutState, id: string): boolean {
  const at = locate(st, id)
  if (!at) return false
  const last = st.sections.length - 1
  return !(at[0] === last && at[1] === st.sections[last].tiles.length - 1)
}

const EMPTY_REASON = 'A section needs at least one tile.'
const FULL_REASON = `A section holds at most ${MAX_SECTION_TILES} tiles.`

export function moveUp(st: LayoutState, id: string): MoveResult {
  const at = locate(st, id)
  if (!at || !canMoveUp(st, id)) return same(st)
  const [si, ti] = at
  const next = clone(st)
  if (ti > 0) {
    const t = next.sections[si].tiles
    ;[t[ti - 1], t[ti]] = [t[ti], t[ti - 1]]
    return same(next)
  }
  if (st.sections[si].tiles.length === 1) return same(st, EMPTY_REASON)
  if (st.sections[si - 1].tiles.length >= MAX_SECTION_TILES) return same(st, FULL_REASON)
  next.sections[si - 1].tiles.push(next.sections[si].tiles.splice(ti, 1)[0])
  return same(next)
}

export function moveDown(st: LayoutState, id: string): MoveResult {
  const at = locate(st, id)
  if (!at || !canMoveDown(st, id)) return same(st)
  const [si, ti] = at
  const next = clone(st)
  const len = st.sections[si].tiles.length
  if (ti < len - 1) {
    const t = next.sections[si].tiles
    ;[t[ti + 1], t[ti]] = [t[ti], t[ti + 1]]
    return same(next)
  }
  if (len === 1) return same(st, EMPTY_REASON)
  if (st.sections[si + 1].tiles.length >= MAX_SECTION_TILES) return same(st, FULL_REASON)
  next.sections[si + 1].tiles.unshift(next.sections[si].tiles.splice(ti, 1)[0])
  return same(next)
}

export function moveToSection(st: LayoutState, id: string, target: number): MoveResult {
  const at = locate(st, id)
  if (!at || target < 0 || target >= st.sections.length || at[0] === target) return same(st)
  if (st.sections[at[0]].tiles.length === 1) return same(st, EMPTY_REASON)
  if (st.sections[target].tiles.length >= MAX_SECTION_TILES) return same(st, FULL_REASON)
  const next = clone(st)
  next.sections[target].tiles.push(next.sections[at[0]].tiles.splice(at[1], 1)[0])
  return same(next)
}

function patchTile(st: LayoutState, id: string, f: (t: LTile) => void): LayoutState {
  const next = clone(st)
  for (const s of next.sections) for (const t of s.tiles) if (t.id === id) f(t)
  return next
}
export const setWidth = (st: LayoutState, id: string, w: Width) => patchTile(st, id, (t) => (t.width = w))
export const setTitle = (st: LayoutState, id: string, title: string) => patchTile(st, id, (t) => (t.title = title))
export const setHidden = (st: LayoutState, id: string, hidden: boolean) =>
  patchTile(st, id, (t) => (t.hidden = hidden))
export function setSectionTitle(st: LayoutState, index: number, title: string): LayoutState {
  const next = clone(st)
  if (next.sections[index]) next.sections[index].title = title
  return next
}
export function setDefaultRange(st: LayoutState, range: string | null): LayoutState {
  return { ...clone(st), defaultRange: range }
}

/** True when anything differs from the base. */
export function isChanged(st: LayoutState, base: LayoutState): boolean {
  return JSON.stringify(st) !== JSON.stringify(base)
}

/** Exactly the C-13 body. Titles only where changed; widths for every tile. */
export function toChanges(st: LayoutState, baseVersion: number, base?: LayoutState): LayoutChanges {
  const orig = new Map((base ? base.sections.flatMap((s) => s.tiles) : []).map((t) => [t.id, t]))
  const widths: Record<string, Width> = {}
  const titles: Record<string, string> = {}
  const hidden: string[] = []
  for (const t of st.sections.flatMap((s) => s.tiles)) {
    widths[t.id] = t.width
    if (t.hidden) hidden.push(t.id)
    const o = orig.get(t.id)
    if (!o || o.title !== t.title) titles[t.id] = t.title
  }
  return {
    base_version: baseVersion,
    sections: st.sections.map((s) => ({
      title: s.title.trim() === '' ? null : s.title,
      tiles: s.tiles.map((t) => t.id),
    })),
    widths,
    titles,
    hidden,
    default_range: st.hasDateControl ? st.defaultRange : null,
    note: null,
  }
}

/** The route's 422 detail arrives as JSON text; show the first finding's path and message. */
export function layoutErrorText(message: string): string {
  try {
    const body = JSON.parse(message) as { findings?: { path?: string; message?: string }[] }
    const f = body.findings?.[0]
    if (f && f.message) return f.path ? `${f.path}: ${f.message}` : f.message
  } catch {
    /* plain text */
  }
  return message
}

import type { Watch } from './types'

/* Pure helpers for following the KPI page when it is zoomed or narrow (Quick 041).
 * No React and no DOM here: the effects in pages/Kpis.tsx measure, these decide. */

// The id of the detail heading, so the page can find it, scroll to it and focus it.
export const KPI_TITLE_ID = 'kpi-detail-title'

// Breathing room (px) between the sticky top bar and the revealed heading; equals --s3.
export const REVEAL_GAP = 12

export interface KpiStep {
  id: number
  title: string
}

export interface KpiNeighbours {
  prev: KpiStep | null
  next: KpiStep | null
  position: number
  total: number
}

// The one list order (KPIs first, then ascending id), shared by the list and the step links.
export function kpiOrder(watches: Watch[]): Watch[] {
  return [...watches].sort(
    (a, b) => (a.kind === 'kpi' ? 0 : 1) - (b.kind === 'kpi' ? 0 : 1) || a.id - b.id,
  )
}

// Previous/next KPI in list order, 1-based position; no wrap-around, so the ends are real ends.
export function kpiNeighbours(ordered: Watch[], id: number | null): KpiNeighbours | null {
  if (id === null) return null
  const i = ordered.findIndex((w) => w.id === id)
  if (i < 0) return null
  const step = (w: Watch | undefined): KpiStep | null => (w ? { id: w.id, title: w.title } : null)
  return { prev: step(ordered[i - 1]), next: step(ordered[i + 1]), position: i + 1, total: ordered.length }
}

// Where to scroll the window so the heading sits just under the top bar; null when already in view.
export function revealScrollTop(m: {
  headingTop: number
  headingBottom: number
  viewportHeight: number
  headerHeight: number
  scrollY: number
  gap?: number
}): number | null {
  if (m.headingTop >= m.headerHeight && m.headingBottom <= m.viewportHeight) return null
  return Math.max(0, Math.round(m.scrollY + m.headingTop - m.headerHeight - (m.gap ?? REVEAL_GAP)))
}

// Whether a navigation should scroll and/or focus: never on first load or a poll (same key).
export function planReveal(s: {
  previousKey: string | null
  key: string
  hasDetail: boolean
  scrollTop: number | null
  focusInDetail: boolean
}): { scroll: boolean; focus: boolean } {
  if (s.previousKey === null || s.previousKey === s.key || !s.hasDetail) {
    return { scroll: false, focus: false }
  }
  return { scroll: s.scrollTop !== null, focus: !s.focusInDetail }
}

// Smooth scrolling is motion, so reduced-motion users get an instant jump.
export function revealBehavior(reducedMotion: boolean): ScrollBehavior {
  return reducedMotion ? 'auto' : 'smooth'
}

// Scroll position that keeps an item inside the sticky list's own scroll area; null when visible.
export function listScrollTop(m: {
  itemTop: number
  itemBottom: number
  scrollTop: number
  height: number
}): number | null {
  if (m.itemTop < m.scrollTop) return m.itemTop
  if (m.itemBottom > m.scrollTop + m.height) return m.itemBottom - m.height
  return null
}

// Which KPI the page opens: the one in the URL, or on a bare /kpis the first row of the list, so the page starts at "1 of N".
export function pickKpi(ordered: Watch[], paramId: string | undefined): Watch | undefined {
  return paramId ? ordered.find((w) => String(w.id) === paramId) : ordered[0]
}

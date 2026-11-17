import { forceCenter, forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY } from 'd3-force'
import type { SimulationLinkDatum, SimulationNodeDatum } from 'd3-force'
import { get } from './api'

/* Quick 014: the Memory page's graph view — types, loaders and the pure logic
 * (colours, legend counts, filters, neighbourhoods, search, layout) the
 * interactive component draws from. Everything here is testable without a
 * DOM; MemoryGraph.tsx only renders and handles pointer events.
 *
 * Read-only by construction: the API takes a scope, a search text and caps,
 * never a query. */

export interface GraphLink {
  kind: 'case' | 'kpi' | 'table' | 'column' | string
  id: string | number
  label: string
  href: string
}

export interface MemNode {
  id: string
  kind: 'entity' | 'episode'
  group: string
  name: string
  type: string
  labels?: string[]
  summary?: string
  content?: string
  source?: string
  created_at?: string | null
  valid_at?: string | null
  degree?: number
  links?: GraphLink[]
}

export interface MemEdge {
  id: string
  kind: 'fact' | 'mentions'
  group?: string
  source: string
  target: string
  name: string
  fact?: string
  valid_at?: string | null
  invalid_at?: string | null
  expired_at?: string | null
  created_at?: string | null
  superseded?: boolean
}

export interface RecallItem {
  kind: 'fact' | 'entity' | 'episode' | string
  fact: string
  uuid?: string
  group_id?: string
  name?: string
  type?: string
  source?: string
  valid_at?: string | null
  created_at?: string | null
  invalid_at?: string | null
  superseded?: boolean
  source_uuid?: string
  target_uuid?: string
}

export interface ScopeOption {
  id: string
  label: string
}

export interface GraphView {
  available: boolean
  scope: string
  scopes: ScopeOption[]
  nodes: MemNode[]
  edges: MemEdge[]
  matches: { nodes: string[]; edges: string[]; results?: RecallItem[] }
  truncated: boolean
  totals?: Record<string, { episodes: number; entities: number; facts: number }>
}

export interface FactWithOther extends MemEdge {
  other?: { id: string; name: string; type: string } | null
}

export interface NodeDetail {
  node: MemNode
  facts_out: FactWithOther[]
  facts_in: FactWithOther[]
  episodes: MemNode[]
  links: GraphLink[]
  mentions?: MemNode[]
  neighbours: { nodes: MemNode[]; edges: MemEdge[] }
}

export interface GraphQuery {
  scope: string
  q?: string
  limit?: number
  episodes?: boolean
  superseded?: boolean
}

export const GRAPH_LIMIT = 300

export function graphUrl(p: GraphQuery): string {
  const qs = new URLSearchParams()
  qs.set('scope', p.scope || 'everything')
  if (p.q && p.q.trim()) qs.set('q', p.q.trim())
  qs.set('limit', String(p.limit ?? GRAPH_LIMIT))
  qs.set('episodes', p.episodes ? 'true' : 'false')
  qs.set('superseded', p.superseded === false ? 'false' : 'true')
  return `/api/memory/graph?${qs.toString()}`
}

export const loadGraph = (p: GraphQuery) => get<GraphView>(graphUrl(p))
export const loadNode = (group: string, id: string) =>
  get<NodeDetail>(`/api/memory/graph/node/${encodeURIComponent(id)}?group=${encodeURIComponent(group)}`)

/* ── colour ──────────────────────────────────────────────────────────────
 * Seven categorical hues in a fixed order, validated for the dark canvas
 * (dataviz validator, surface #0B111B: every adjacent pair passes the CVD
 * and normal-vision floors). Yellow is left out on purpose: gold means "a
 * human owes something" in this app. Types past the seven fold into one
 * neutral, and every node always carries its name, so colour is never the
 * only way to tell types apart. A type keeps its colour whatever is filtered. */

export const CANVAS = '#0B111B'
export const OTHER_COLOR = '#7B8494'
export const EPISODE_COLOR = '#64748B'
export const EDGE_COLOR = '#3A4556'
export const EDGE_LIT = '#A7B3C6'

export const TYPE_ORDER = ['Machine', 'EventType', 'Rule', 'Table', 'Case', 'Metric', 'Person'] as const
const PALETTE = ['#3987e5', '#d95926', '#199e70', '#9085e9', '#d55181', '#008300', '#e66767']

export function typeColor(type: string): string {
  if (type === 'Episode') return EPISODE_COLOR
  const i = (TYPE_ORDER as readonly string[]).indexOf(type)
  return i >= 0 ? PALETTE[i] : OTHER_COLOR
}

export const TYPE_LABEL: Record<string, string> = {
  EventType: 'Event type',
  Entity: 'Other',
  Episode: 'Episode',
}

export function typeLabel(type: string): string {
  return TYPE_LABEL[type] ?? type
}

export function relLabel(name: string): string {
  return (name || 'RELATES_TO').toUpperCase()
}

export function shortLabel(name: string, max = 10): string {
  const s = (name || '').trim()
  return s.length > max ? `${s.slice(0, max - 1)}…` : s
}

export function nodeRadius(n: MemNode): number {
  if (n.kind === 'episode') return 9
  const d = n.degree ?? 0
  return Math.min(26, 13 + Math.sqrt(d) * 3)
}

/* ── legend ───────────────────────────────────────────────────────────── */

export interface LegendRow {
  key: string
  label: string
  count: number
  color: string
}

export function labelLegend(nodes: MemNode[]): LegendRow[] {
  const counts = new Map<string, number>()
  for (const n of nodes) counts.set(n.type, (counts.get(n.type) ?? 0) + 1)
  const rank = (t: string) => {
    if (t === 'Episode') return 99
    const i = (TYPE_ORDER as readonly string[]).indexOf(t)
    return i >= 0 ? i : 50
  }
  return [...counts.entries()]
    .sort((a, b) => rank(a[0]) - rank(b[0]) || b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([type, count]) => ({ key: type, label: typeLabel(type), count, color: typeColor(type) }))
}

export function relationLegend(edges: MemEdge[]): LegendRow[] {
  const counts = new Map<string, number>()
  for (const e of edges) {
    const k = e.kind === 'mentions' ? 'MENTIONS' : relLabel(e.name)
    counts.set(k, (counts.get(k) ?? 0) + 1)
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([name, count]) => ({ key: name, label: name, count, color: EDGE_LIT }))
}

/* ── filters ──────────────────────────────────────────────────────────── */

export interface ViewFilter {
  hiddenTypes: Set<string>
  hiddenRels: Set<string>
  showEpisodes: boolean
  showSuperseded: boolean
}

export function edgeKey(e: MemEdge): string {
  return e.kind === 'mentions' ? 'MENTIONS' : relLabel(e.name)
}

export function visible(nodes: MemNode[], edges: MemEdge[], f: ViewFilter): { nodes: MemNode[]; edges: MemEdge[] } {
  const keep = nodes.filter(
    (n) => !f.hiddenTypes.has(n.type) && (f.showEpisodes || n.kind !== 'episode'),
  )
  const ids = new Set(keep.map((n) => n.id))
  const kept = edges.filter(
    (e) =>
      ids.has(e.source) &&
      ids.has(e.target) &&
      !f.hiddenRels.has(edgeKey(e)) &&
      (f.showSuperseded || !e.superseded),
  )
  return { nodes: keep, edges: kept }
}

/* ── neighbourhoods and merging ───────────────────────────────────────── */

export function neighbourhood(ids: Iterable<string>, edges: MemEdge[]): { nodes: Set<string>; edges: Set<string> } {
  const seeds = new Set(ids)
  const nodes = new Set(seeds)
  const lit = new Set<string>()
  for (const e of edges) {
    if (seeds.has(e.source) || seeds.has(e.target)) {
      nodes.add(e.source)
      nodes.add(e.target)
      lit.add(e.id)
    }
  }
  return { nodes, edges: lit }
}

/* Merge an expanded neighbourhood into the view without duplicating nodes
 * or edges (the first copy of a node wins, so positions stay put). */
export function mergeInto(
  base: { nodes: MemNode[]; edges: MemEdge[] },
  extra: { nodes: MemNode[]; edges: MemEdge[] },
): { nodes: MemNode[]; edges: MemEdge[]; added: number } {
  const have = new Set(base.nodes.map((n) => n.id))
  const nodes = [...base.nodes]
  let added = 0
  for (const n of extra.nodes) {
    if (!have.has(n.id)) {
      have.add(n.id)
      nodes.push(n)
      added += 1
    }
  }
  const haveE = new Set(base.edges.map((e) => e.id))
  const edges = [...base.edges]
  for (const e of extra.edges) {
    if (!haveE.has(e.id) && have.has(e.source) && have.has(e.target)) {
      haveE.add(e.id)
      edges.push(e)
    }
  }
  return { nodes, edges, added }
}

/* "Search for element in the graph": loaded nodes whose name (or episode
 * text) contains the words, best first — exact, then prefix, then contains. */
export function findNodes(nodes: MemNode[], text: string, max = 20): MemNode[] {
  const q = text.trim().toLowerCase()
  if (!q) return []
  const scored: [number, MemNode][] = []
  for (const n of nodes) {
    const name = (n.name || '').toLowerCase()
    let s = -1
    if (name === q) s = 0
    else if (name.startsWith(q)) s = 1
    else if (name.includes(q)) s = 2
    else if (n.kind === 'episode' && (n.content || '').toLowerCase().includes(q)) s = 3
    else if ((n.summary || '').toLowerCase().includes(q)) s = 4
    if (s >= 0) scored.push([s, n])
  }
  scored.sort((a, b) => a[0] - b[0] || (b[1].degree ?? 0) - (a[1].degree ?? 0))
  return scored.slice(0, max).map(([, n]) => n)
}

export function displayName(n: MemNode): string {
  if (n.kind === 'episode') return episodeTitle(n)
  return n.name || '(unnamed)'
}

export function episodeTitle(n: Pick<MemNode, 'name' | 'source'>): string {
  const name = n.name || ''
  const m = /^case_(\d+)_close/.exec(name)
  if (m) return `Case #${m[1]} closed`
  if (/^learning_\d+/.test(name)) return n.source?.startsWith('reflection:') ? `Learned ${n.source.slice(11)}` : 'Learning'
  if (n.source === 'onboarding' || /^onboarding_business/.test(name)) return 'Onboarding interview'
  if (n.source === 'chat_onboarding' || /^onboarding_chat/.test(name)) return 'Chat setup'
  return n.source || name || 'Episode'
}

/* What a recall item is, in words, for the search results list. */
export function recallKind(item: RecallItem): string {
  if (item.kind === 'entity') return item.type ? typeLabel(item.type) : 'Entity'
  if (item.kind === 'episode') return 'Episode'
  return 'Fact'
}

/* ── layout (d3-force) ────────────────────────────────────────────────── */

export interface SimNode extends SimulationNodeDatum {
  id: string
  node: MemNode
  r: number
}

export interface SimLink extends SimulationLinkDatum<SimNode> {
  id: string
  edge: MemEdge
}

export function buildSimulation(
  nodes: MemNode[],
  edges: MemEdge[],
  width: number,
  height: number,
  previous?: Map<string, { x: number; y: number; fx?: number | null; fy?: number | null }>,
) {
  const simNodes: SimNode[] = nodes.map((n, i) => {
    const prev = previous?.get(n.id)
    const angle = (i / Math.max(1, nodes.length)) * Math.PI * 2
    const spread = Math.min(width, height) * 0.35
    return {
      id: n.id,
      node: n,
      r: nodeRadius(n),
      x: prev?.x ?? width / 2 + Math.cos(angle) * spread * (0.4 + (i % 7) / 10),
      y: prev?.y ?? height / 2 + Math.sin(angle) * spread * (0.4 + (i % 5) / 8),
      fx: prev?.fx ?? undefined,
      fy: prev?.fy ?? undefined,
    }
  })
  const byId = new Map(simNodes.map((n) => [n.id, n]))
  const simLinks: SimLink[] = edges
    .filter((e) => byId.has(e.source) && byId.has(e.target))
    .map((e) => ({ id: e.id, edge: e, source: e.source, target: e.target }))
  const sim = forceSimulation<SimNode>(simNodes)
    .force(
      'link',
      forceLink<SimNode, SimLink>(simLinks)
        .id((d) => d.id)
        .distance((l) => (l.edge.kind === 'mentions' ? 55 : 95))
        .strength(0.6),
    )
    .force('charge', forceManyBody<SimNode>().strength(-260).distanceMax(500))
    .force('collide', forceCollide<SimNode>().radius((d) => d.r + 6))
    .force('center', forceCenter(width / 2, height / 2))
    .force('x', forceX<SimNode>(width / 2).strength(0.04))
    .force('y', forceY<SimNode>(height / 2).strength(0.06))
    .stop()
  return { sim, simNodes, simLinks, byId }
}

/* Run the layout to (near) rest without animating — the first frame shows a
 * settled graph instead of 300 nodes flying in. */
export function settle(sim: ReturnType<typeof buildSimulation>['sim'], ticks = 220): void {
  for (let i = 0; i < ticks; i++) sim.tick()
}

/* An arrow from a's edge to b's edge (not centre to centre), so the
 * arrowhead sits on the target circle. */
export function edgeLine(
  a: { x?: number; y?: number; r: number },
  b: { x?: number; y?: number; r: number },
): { x1: number; y1: number; x2: number; y2: number } {
  const ax = a.x ?? 0
  const ay = a.y ?? 0
  const bx = b.x ?? 0
  const by = b.y ?? 0
  const dx = bx - ax
  const dy = by - ay
  const len = Math.hypot(dx, dy) || 1
  const ux = dx / len
  const uy = dy / len
  return { x1: ax + ux * a.r, y1: ay + uy * a.r, x2: bx - ux * (b.r + 3), y2: by - uy * (b.r + 3) }
}

/* Zoom about a point: keep the graph point under the cursor fixed. */
export function zoomAt(
  t: { k: number; x: number; y: number },
  px: number,
  py: number,
  factor: number,
): { k: number; x: number; y: number } {
  const k = Math.max(0.15, Math.min(4, t.k * factor))
  const f = k / t.k
  return { k, x: px - (px - t.x) * f, y: py - (py - t.y) * f }
}

/* The transform that puts graph point (gx, gy) at the viewport centre. */
export function centreOn(
  gx: number,
  gy: number,
  width: number,
  height: number,
  k: number,
): { k: number; x: number; y: number } {
  return { k, x: width / 2 - gx * k, y: height / 2 - gy * k }
}

/* Dates on a fact, in the panel's words. */
export function factWhen(e: Pick<MemEdge, 'valid_at' | 'invalid_at' | 'created_at'>): string {
  const day = (s?: string | null) => (s ? String(s).slice(0, 10) : '')
  const parts: string[] = []
  if (e.valid_at) parts.push(`valid from ${day(e.valid_at)}`)
  if (e.invalid_at) parts.push(`superseded ${day(e.invalid_at)}`)
  if (e.created_at) parts.push(`recorded ${day(e.created_at)}`)
  return parts.join(' · ')
}

import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent as RPointerEvent } from 'react'
import { Link } from 'react-router-dom'
import { ago, isoTitle, plural } from '../lib/format'
import { useResource, useStored } from '../lib/hooks'
import {
  CANVAS,
  EDGE_COLOR,
  EDGE_LIT,
  buildSimulation,
  centreOn,
  displayName,
  edgeKey,
  edgeLine,
  factWhen,
  findNodes,
  labelLegend,
  loadGraph,
  loadNode,
  mergeInto,
  neighbourhood,
  relationLegend,
  settle,
  shortLabel,
  typeColor,
  typeLabel,
  visible,
  zoomAt,
  type FactWithOther,
  type GraphLink,
  type GraphView,
  type LegendRow,
  type MemEdge,
  type MemNode,
  type NodeDetail,
  type SimLink,
  type SimNode,
} from '../lib/memgraph'
import './memgraph.css'

/* Quick 014: the knowledge graph drawn as connections — the owner's words:
 * "connections is what make graph important". A read-only node-link view in
 * the spirit of FalkorDB Browser's graph view (dark canvas, coloured circles
 * with names, thin arrowed edges, Labels and Relationships lists), built here
 * so the owner never needs the database's own admin tool. Interactions: zoom
 * (wheel), pan (drag the background), drag a node (it stays where dropped),
 * hover to light a node's neighbourhood, click for the side panel and to pull
 * the node's neighbours in. */

const EMPTY: GraphView = {
  available: true, scope: 'everything', scopes: [], nodes: [], edges: [],
  matches: { nodes: [], edges: [] }, truncated: false,
}

type Transform = { k: number; x: number; y: number }

/* Up to this many nodes every name is written under its node; past it only
 * the hovered, selected, matched and lit ones are. */
const SMALL_GRAPH = 60

/* ── canvas ─────────────────────────────────────────────────────────────── */

interface CanvasProps {
  nodes: MemNode[]
  edges: MemEdge[]
  selected: string | null
  matched: Set<string>
  matchedEdges: Set<string>
  focus: { id: string; seq: number } | null
  onSelect: (n: MemNode | null) => void
  height: number
}

function fitTransform(simNodes: SimNode[], w: number, h: number): Transform {
  if (!simNodes.length) return { k: 1, x: 0, y: 0 }
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity
  for (const n of simNodes) {
    x0 = Math.min(x0, (n.x ?? 0) - n.r)
    y0 = Math.min(y0, (n.y ?? 0) - n.r)
    x1 = Math.max(x1, (n.x ?? 0) + n.r)
    y1 = Math.max(y1, (n.y ?? 0) + n.r)
  }
  const pad = 40
  const k = Math.max(0.2, Math.min(1.6, Math.min((w - pad * 2) / (x1 - x0 || 1), (h - pad * 2) / (y1 - y0 || 1))))
  return { k, x: w / 2 - ((x0 + x1) / 2) * k, y: h / 2 - ((y0 + y1) / 2) * k }
}

function GraphCanvas({ nodes, edges, selected, matched, matchedEdges, focus, onSelect, height }: CanvasProps) {
  const wrap = useRef<HTMLDivElement>(null)
  const svg = useRef<SVGSVGElement>(null)
  const [width, setWidth] = useState(900)
  const [t, setT] = useState<Transform>({ k: 1, x: 0, y: 0 })
  const tRef = useRef(t)
  tRef.current = t
  const [, setFrame] = useState(0)
  const [hover, setHover] = useState<string | null>(null)
  const [hoverEdge, setHoverEdge] = useState<string | null>(null)
  const positions = useRef(new Map<string, { x: number; y: number; fx?: number | null; fy?: number | null }>())
  const simRef = useRef<ReturnType<typeof buildSimulation> | null>(null)
  const drag = useRef<{ id: string; sx: number; sy: number; moved: boolean } | null>(null)
  const pan = useRef<{ sx: number; sy: number; t: Transform; moved: boolean } | null>(null)
  const fitted = useRef(false)

  useEffect(() => {
    const el = wrap.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver((entries) => {
      const w = Math.round(entries[0]?.contentRect.width ?? 900)
      if (w > 0) setWidth(w)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // (Re)build the simulation when the drawn set changes; known nodes keep
  // their place, new ones settle in around them.
  const key = useMemo(() => nodes.map((n) => n.id).join('|') + '#' + edges.map((e) => e.id).join('|'), [nodes, edges])
  useEffect(() => {
    let raf = 0
    const fresh = positions.current.size === 0
    const s = buildSimulation(nodes, edges, width, height, positions.current)
    simRef.current = s
    const known = s.simNodes.filter((n) => positions.current.has(n.id)).length
    if (fresh || known < s.simNodes.length / 2) settle(s.sim, 220)
    const save = () => {
      for (const n of s.simNodes) positions.current.set(n.id, { x: n.x ?? 0, y: n.y ?? 0, fx: n.fx, fy: n.fy })
    }
    save()
    if (!fitted.current && s.simNodes.length) {
      fitted.current = true
      setT(fitTransform(s.simNodes, width, height))
    }
    s.sim.on('tick', () => {
      save()
      if (!raf) raf = requestAnimationFrame(() => { raf = 0; setFrame((f) => f + 1) })
    })
    s.sim.alpha(fresh ? 0.08 : 0.35).restart()
    setFrame((f) => f + 1)
    return () => {
      s.sim.stop()
      if (raf) cancelAnimationFrame(raf)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, height])

  // Wheel zoom about the cursor (native listener: React's wheel is passive).
  useEffect(() => {
    const el = svg.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const r = el.getBoundingClientRect()
      setT((cur) => zoomAt(cur, e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY * 0.0015)))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  // Centre on a node when search or a panel link asks for it.
  useEffect(() => {
    if (!focus) return
    const n = simRef.current?.byId.get(focus.id)
    if (!n) return
    const from = tRef.current
    const to = centreOn(n.x ?? 0, n.y ?? 0, width, height, Math.max(from.k, 1.1))
    const t0 = performance.now()
    let raf = 0
    const step = (now: number) => {
      const p = Math.min(1, (now - t0) / 320)
      const e = 1 - Math.pow(1 - p, 3)
      setT({ k: from.k + (to.k - from.k) * e, x: from.x + (to.x - from.x) * e, y: from.y + (to.y - from.y) * e })
      if (p < 1) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus?.seq])

  const toGraph = (clientX: number, clientY: number) => {
    const r = svg.current!.getBoundingClientRect()
    const cur = tRef.current
    return { x: (clientX - r.left - cur.x) / cur.k, y: (clientY - r.top - cur.y) / cur.k }
  }

  const onNodeDown = (e: RPointerEvent, id: string) => {
    e.stopPropagation()
    svg.current?.setPointerCapture(e.pointerId)
    drag.current = { id, sx: e.clientX, sy: e.clientY, moved: false }
  }
  const onBgDown = (e: RPointerEvent) => {
    svg.current?.setPointerCapture(e.pointerId)
    pan.current = { sx: e.clientX, sy: e.clientY, t: tRef.current, moved: false }
  }
  const onMove = (e: RPointerEvent) => {
    const d = drag.current
    if (d) {
      if (!d.moved && Math.hypot(e.clientX - d.sx, e.clientY - d.sy) < 4) return
      const s = simRef.current
      const n = s?.byId.get(d.id)
      if (!s || !n) return
      if (!d.moved) s.sim.alphaTarget(0.25).restart()
      d.moved = true
      const g = toGraph(e.clientX, e.clientY)
      n.fx = g.x
      n.fy = g.y
      return
    }
    const p = pan.current
    if (p) {
      const dx = e.clientX - p.sx
      const dy = e.clientY - p.sy
      if (!p.moved && Math.hypot(dx, dy) < 3) return
      p.moved = true
      setT({ ...p.t, x: p.t.x + dx, y: p.t.y + dy })
    }
  }
  const onUp = () => {
    const d = drag.current
    if (d) {
      drag.current = null
      simRef.current?.sim.alphaTarget(0)
      if (!d.moved) {
        const n = simRef.current?.byId.get(d.id)
        if (n) onSelect(n.node)
      }
      return
    }
    const p = pan.current
    pan.current = null
    if (p && !p.moved) onSelect(null)
  }

  const s = simRef.current
  const small = nodes.length <= SMALL_GRAPH
  const focusIds = hover ?? selected
  const lit = useMemo(
    () => (focusIds ? neighbourhood([focusIds], edges) : null),
    [focusIds, edges],
  )
  const searchLit = useMemo(
    () => (matched.size ? neighbourhood(matched, edges) : null),
    [matched, edges],
  )
  const dimNode = (id: string) =>
    (lit && !lit.nodes.has(id)) || (!lit && searchLit && !searchLit.nodes.has(id))
  const edgeLit = (id: string) => Boolean(lit?.edges.has(id) || matchedEdges.has(id) || hoverEdge === id)
  const dimEdge = (id: string) =>
    (lit && !lit.edges.has(id)) || (!lit && searchLit && !searchLit.edges.has(id) && !matchedEdges.has(id))

  const zoomBy = (f: number) => setT((cur) => zoomAt(cur, width / 2, height / 2, f))
  const fit = () => s && setT(fitTransform(s.simNodes, width, height))

  return (
    <div className="mg-stage" ref={wrap} style={{ height }}>
      <svg
        ref={svg}
        className="mg-svg"
        width={width}
        height={height}
        role="img"
        aria-label={`Knowledge graph: ${plural(nodes.length, 'node')}, ${plural(edges.length, 'connection')}`}
        style={{ background: CANVAS }}
        onPointerDown={onBgDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onUp}
      >
        <defs>
          <marker id="mg-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M0,1 L9,5 L0,9 z" fill={EDGE_COLOR} />
          </marker>
          <marker id="mg-arrow-lit" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M0,1 L9,5 L0,9 z" fill={EDGE_LIT} />
          </marker>
        </defs>
        <g transform={`translate(${t.x},${t.y}) scale(${t.k})`}>
          {s?.simLinks.map((l: SimLink) => {
            const a = l.source as SimNode
            const b = l.target as SimNode
            if (typeof a !== 'object' || typeof b !== 'object') return null
            const ln = edgeLine(a, b)
            const on = edgeLit(l.id)
            const dim = dimEdge(l.id)
            const mention = l.edge.kind === 'mentions'
            return (
              <g key={l.id} className={`mg-edge${dim ? ' is-dim' : ''}`} data-edge={l.id}>
                <line
                  {...ln}
                  stroke={on ? EDGE_LIT : EDGE_COLOR}
                  strokeWidth={on ? 1.6 : 1}
                  strokeDasharray={mention ? '3 3' : l.edge.superseded ? '6 4' : undefined}
                  markerEnd={`url(#${on ? 'mg-arrow-lit' : 'mg-arrow'})`}
                />
                <line
                  {...ln}
                  stroke="transparent"
                  strokeWidth={9}
                  className="mg-hit"
                  onPointerEnter={() => setHoverEdge(l.id)}
                  onPointerLeave={() => setHoverEdge((h) => (h === l.id ? null : h))}
                >
                  <title>{l.edge.fact || edgeKey(l.edge)}</title>
                </line>
              </g>
            )
          })}
          {s?.simLinks.map((l: SimLink) => {
            if (!edgeLit(l.id) || l.edge.kind === 'mentions') return null
            const a = l.source as SimNode
            const b = l.target as SimNode
            if (typeof a !== 'object' || typeof b !== 'object') return null
            return (
              <text
                key={`t-${l.id}`}
                className={`mg-edge-label${l.edge.superseded ? ' is-superseded' : ''}`}
                x={((a.x ?? 0) + (b.x ?? 0)) / 2}
                y={((a.y ?? 0) + (b.y ?? 0)) / 2 - 3}
                textAnchor="middle"
              >
                {edgeKey(l.edge)}
              </text>
            )
          })}
          {s?.simNodes.map((n: SimNode) => {
            const node = n.node
            const color = typeColor(node.type)
            const isSel = selected === n.id
            const isHit = matched.has(n.id)
            const showName =
              isSel || hover === n.id || isHit || (lit?.nodes.has(n.id) && t.k > 0.7) || (small && t.k > 0.6)
            return (
              <g
                key={n.id}
                className={`mg-node${dimNode(n.id) ? ' is-dim' : ''}${isSel ? ' is-selected' : ''}${isHit ? ' is-match' : ''}`}
                transform={`translate(${n.x ?? 0},${n.y ?? 0})`}
                data-node={n.id}
                onPointerDown={(e) => onNodeDown(e, n.id)}
                onPointerEnter={() => setHover(n.id)}
                onPointerLeave={() => setHover((h) => (h === n.id ? null : h))}
              >
                <title>{`${displayName(node)} (${typeLabel(node.type)})`}</title>
                {isHit && <circle r={n.r + 5} className="mg-ring" />}
                {node.kind === 'episode' ? (
                  <rect x={-n.r} y={-n.r} width={n.r * 2} height={n.r * 2} rx={3} fill={color} className="mg-shape" />
                ) : (
                  <circle r={n.r} fill={color} className="mg-shape" />
                )}
                {node.kind !== 'episode' && t.k >= 0.45 && (
                  <text className="mg-inner" textAnchor="middle" dy="0.32em">
                    {shortLabel(node.name, Math.max(4, Math.round(n.r / 2.6)))}
                  </text>
                )}
                {showName && (
                  <text className="mg-name" textAnchor="middle" y={n.r + 13}>
                    {shortLabel(displayName(node), 42)}
                  </text>
                )}
              </g>
            )
          })}
        </g>
      </svg>
      <div className="mg-zoom">
        <button className="btn btn-sm btn-icon" onClick={() => zoomBy(1.25)} aria-label="Zoom in">+</button>
        <button className="btn btn-sm btn-icon" onClick={() => zoomBy(0.8)} aria-label="Zoom out">−</button>
        <button className="btn btn-sm" onClick={fit}>Fit</button>
      </div>
    </div>
  )
}

/* ── sidebar lists ──────────────────────────────────────────────────────── */

export function LegendList({
  title,
  rows,
  hidden,
  onToggle,
  kind,
}: {
  title: string
  rows: LegendRow[]
  hidden: Set<string>
  onToggle: (key: string) => void
  kind: 'label' | 'relationship'
}) {
  return (
    <div className="mg-legend" data-legend={kind}>
      <div className="mg-legend-title">{title}</div>
      {rows.length === 0 && <div className="dimmer mg-legend-empty">none yet</div>}
      {rows.map((r) => (
        <button
          key={r.key}
          className={`mg-legend-row${hidden.has(r.key) ? ' is-off' : ''}`}
          onClick={() => onToggle(r.key)}
          aria-pressed={!hidden.has(r.key)}
          title={hidden.has(r.key) ? `Show ${r.label}` : `Hide ${r.label}`}
        >
          <span className={kind === 'label' ? 'mg-dot' : 'mg-dot mg-dot-rel'} style={{ background: r.color }} />
          <span className="mg-legend-label">{r.label}</span>
          <span className="mg-legend-count">{r.count}</span>
        </button>
      ))}
    </div>
  )
}

/* ── side panel ─────────────────────────────────────────────────────────── */

function LinkChips({ links }: { links?: GraphLink[] }) {
  if (!links?.length) return null
  const label = (l: GraphLink) =>
    l.kind === 'case' ? `Open ${l.label}` : l.kind === 'kpi' ? `KPI: ${l.label}` : `Table ${l.label}`
  return (
    <div className="btn-row" style={{ flexWrap: 'wrap' }}>
      {links.map((l) => (
        <Link key={`${l.kind}:${l.id}`} className="btn btn-sm" to={l.href} data-link={l.kind}>
          {label(l)}
        </Link>
      ))}
    </div>
  )
}

function FactRow({ f, dir, onPick }: { f: FactWithOther; dir: 'out' | 'in'; onPick: (id: string) => void }) {
  return (
    <li className={`mg-fact${f.superseded ? ' is-superseded' : ''}`} data-fact={f.id}>
      <div className="mg-fact-head">
        <span className="mono mg-rel">{dir === 'out' ? `${edgeKey(f)} →` : `← ${edgeKey(f)}`}</span>
        {f.other && (
          <button className="mg-other" onClick={() => onPick(f.other!.id)} title="Show this node">
            <span className="mg-dot" style={{ background: typeColor(f.other.type) }} />
            {f.other.name}
          </button>
        )}
      </div>
      <div className={f.superseded ? 'mg-struck' : undefined}>{f.fact}</div>
      <div className="dimmer mg-when">
        {f.superseded && <span className="pill">superseded</span>} {factWhen(f)}
      </div>
    </li>
  )
}

export function NodePanel({
  detail,
  loading,
  error,
  onClose,
  onPick,
}: {
  detail: NodeDetail | null
  loading?: boolean
  error?: string | null
  onClose: () => void
  onPick: (id: string) => void
}) {
  const n = detail?.node
  return (
    <aside className="mg-panel" aria-label="Node details">
      <div className="mg-panel-head">
        {n && <span className="mg-dot" style={{ background: typeColor(n.type) }} />}
        <strong className="grow">{n ? displayName(n) : loading ? 'Loading…' : 'Node'}</strong>
        <button className="btn btn-sm btn-quiet" onClick={onClose} aria-label="Close">
          ×
        </button>
      </div>
      {error && <div className="banner banner-broken">{error}</div>}
      {n && (
        <div className="mg-panel-body">
          <div className="dimmer mg-meta">
            {typeLabel(n.type)}
            {n.group !== 'global' ? ` · ${n.group}` : ''}
            {n.created_at && (
              <>
                {' · '}
                <span title={isoTitle(n.created_at)}>added {ago(n.created_at)}</span>
              </>
            )}
          </div>
          {n.summary && <p className="mg-summary">{n.summary}</p>}
          {n.kind === 'episode' && <p className="mg-episode-text">{n.content}</p>}
          <LinkChips links={detail!.links} />
          {detail!.facts_out.length > 0 && (
            <>
              <div className="mg-panel-sub">Facts from it ({detail!.facts_out.length})</div>
              <ul className="mg-facts">
                {detail!.facts_out.map((f) => (
                  <FactRow key={f.id} f={f} dir="out" onPick={onPick} />
                ))}
              </ul>
            </>
          )}
          {detail!.facts_in.length > 0 && (
            <>
              <div className="mg-panel-sub">Facts about it ({detail!.facts_in.length})</div>
              <ul className="mg-facts">
                {detail!.facts_in.map((f) => (
                  <FactRow key={f.id} f={f} dir="in" onPick={onPick} />
                ))}
              </ul>
            </>
          )}
          {n.kind === 'entity' && (
            <>
              <div className="mg-panel-sub">Mentioned in ({detail!.episodes.length})</div>
              {detail!.episodes.length === 0 && <div className="dimmer">No episode mentions it.</div>}
              <ul className="mg-facts">
                {detail!.episodes.map((ep) => (
                  <li key={ep.id} className="mg-fact" data-episode={ep.id}>
                    <div className="mg-fact-head">
                      <span className="pill pill-signal">{displayName(ep)}</span>
                      <LinkChips links={ep.links} />
                    </div>
                    <div className="mg-episode-text">{ep.content}</div>
                    {ep.created_at && (
                      <div className="dimmer mg-when" title={isoTitle(ep.created_at)}>
                        added {ago(ep.created_at)}
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </>
          )}
          {n.kind === 'episode' && (detail!.mentions?.length ?? 0) > 0 && (
            <>
              <div className="mg-panel-sub">Mentions ({detail!.mentions!.length})</div>
              <div className="btn-row" style={{ flexWrap: 'wrap' }}>
                {detail!.mentions!.map((m) => (
                  <button key={m.id} className="mg-other" onClick={() => onPick(m.id)}>
                    <span className="mg-dot" style={{ background: typeColor(m.type) }} />
                    {m.name}
                  </button>
                ))}
              </div>
            </>
          )}
        </div>
      )}
    </aside>
  )
}

/* ── the section ────────────────────────────────────────────────────────── */

export function MemoryGraph({ height = 560 }: { height?: number }) {
  const [scope, setScope] = useStored<string>('memory.graph.scope', 'everything')
  const [showEpisodes, setShowEpisodes] = useStored<boolean>('memory.graph.episodes', false)
  const [showSuperseded, setShowSuperseded] = useStored<boolean>('memory.graph.superseded', true)
  const [hiddenTypes, setHiddenTypes] = useState<Set<string>>(() => new Set())
  const [hiddenRels, setHiddenRels] = useState<Set<string>>(() => new Set())
  const [text, setText] = useState('')
  const [serverQ, setServerQ] = useState('')
  const [selected, setSelected] = useState<{ id: string; group: string } | null>(null)
  const [focus, setFocus] = useState<{ id: string; seq: number } | null>(null)
  const [extra, setExtra] = useState<{ nodes: MemNode[]; edges: MemEdge[] }>({ nodes: [], edges: [] })

  const view = useResource(
    () => loadGraph({ scope, q: serverQ, episodes: showEpisodes, superseded: showSuperseded }),
    { deps: [scope, serverQ, showEpisodes, showSuperseded] },
  )
  useEffect(() => setExtra({ nodes: [], edges: [] }), [scope, showEpisodes, showSuperseded])

  const detail = useResource<NodeDetail | null>(
    () => (selected ? loadNode(selected.group, selected.id) : Promise.resolve(null)),
    { deps: [selected?.id, selected?.group] },
  )
  // A click pulls the node's neighbourhood into the view (expand on click).
  useEffect(() => {
    const nb = detail.data?.neighbours
    if (nb) setExtra((cur) => mergeInto(cur, nb))
  }, [detail.data])

  const data = view.data ?? EMPTY
  const merged = useMemo(() => mergeInto(data, extra), [data, extra])
  const filter = { hiddenTypes, hiddenRels, showEpisodes, showSuperseded }
  const shown = useMemo(
    () => visible(merged.nodes, merged.edges, filter),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [merged, hiddenTypes, hiddenRels, showEpisodes, showSuperseded],
  )
  // The lists count what the toggles let through (not what a type or
  // relationship filter hides), so a hidden entry still shows its count.
  const listed = useMemo(
    () => visible(merged.nodes, merged.edges, { hiddenTypes: new Set(), hiddenRels: new Set(), showEpisodes, showSuperseded }),
    [merged, showEpisodes, showSuperseded],
  )
  const labels = useMemo(() => labelLegend(listed.nodes), [listed])
  const rels = useMemo(() => relationLegend(listed.edges), [listed])

  const local = useMemo(() => findNodes(shown.nodes, text, 8), [shown.nodes, text])
  const matched = useMemo(() => {
    const m = new Set<string>(text.trim() ? local.map((n) => n.id) : [])
    if (serverQ && serverQ === text.trim()) data.matches.nodes.forEach((id) => m.add(id))
    return m
  }, [local, text, serverQ, data.matches.nodes])
  const matchedEdges = useMemo(
    () => new Set(serverQ && serverQ === text.trim() ? data.matches.edges : []),
    [serverQ, text, data.matches.edges],
  )

  const select = useCallback((n: MemNode | null) => {
    setSelected(n ? { id: n.id, group: n.group } : null)
  }, [])
  const pick = (id: string) => {
    const n = merged.nodes.find((x) => x.id === id)
    if (n) {
      select(n)
      setFocus({ id, seq: Date.now() })
    }
  }
  const toggle = (set: Set<string>, key: string, apply: (s: Set<string>) => void) => {
    const next = new Set(set)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    apply(next)
  }
  const onSearchKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      // find and centre (the panel opens on a click, not on Enter)
      setServerQ(text.trim())
      if (local[0]) setFocus({ id: local[0].id, seq: Date.now() })
    }
    if (e.key === 'Escape') {
      setText('')
      setServerQ('')
    }
  }
  // After a server search lands, centre on its first match if nothing local did.
  useEffect(() => {
    if (!serverQ || local.length) return
    const first = data.matches.nodes.find((id) => shown.nodes.some((n) => n.id === id))
    if (first) setFocus({ id: first, seq: Date.now() })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data])

  const entities = shown.nodes.filter((n) => n.kind === 'entity').length
  const facts = shown.edges.filter((e) => e.kind === 'fact').length

  return (
    <section className="panel mg" data-memory-graph>
      <div className="panel-head">
        <h2 className="panel-title">Knowledge graph — connections</h2>
        <span className="dimmer mono" style={{ fontSize: 12 }}>
          {plural(entities, 'entity', 'entities')} · {plural(facts, 'fact')}
          {data.truncated ? ' · most connected shown' : ''}
        </span>
        <div className="panel-spacer" />
        <select
          className="select mg-scope"
          value={scope}
          onChange={(e) => {
            setScope(e.target.value)
            setSelected(null)
          }}
          aria-label="Scope"
        >
          {(data.scopes.length ? data.scopes : [{ id: 'everything', label: 'Everything' }]).map((s) => (
            <option key={s.id} value={s.id}>
              {s.label}
            </option>
          ))}
        </select>
        <label className="check mg-toggle">
          <input type="checkbox" checked={showEpisodes} onChange={(e) => setShowEpisodes(e.target.checked)} />
          Episodes
        </label>
        <label className="check mg-toggle">
          <input type="checkbox" checked={showSuperseded} onChange={(e) => setShowSuperseded(e.target.checked)} />
          Superseded facts
        </label>
      </div>
      <div className="mg-body">
        <aside className="mg-side">
          <input
            className="input mg-search"
            placeholder="Search for element in the graph"
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onSearchKey}
            aria-label="Search for element in the graph"
          />
          {text.trim() && (
            <div className="mg-found">
              {local.length === 0 ? (
                <div className="dimmer">
                  {serverQ === text.trim() && view.loading ? 'Searching…' : 'Not in view. Press Enter to search the whole graph.'}
                </div>
              ) : (
                local.map((n) => (
                  <button key={n.id} className="mg-found-row" onClick={() => pick(n.id)}>
                    <span className="mg-dot" style={{ background: typeColor(n.type) }} />
                    <span className="mg-legend-label">{displayName(n)}</span>
                    <span className="mg-legend-count">{typeLabel(n.type)}</span>
                  </button>
                ))
              )}
            </div>
          )}
          <LegendList
            title="Labels"
            kind="label"
            rows={labels}
            hidden={hiddenTypes}
            onToggle={(k) => toggle(hiddenTypes, k, setHiddenTypes)}
          />
          <LegendList
            title="Relationships"
            kind="relationship"
            rows={rels}
            hidden={hiddenRels}
            onToggle={(k) => toggle(hiddenRels, k, setHiddenRels)}
          />
        </aside>
        <div className="mg-main">
          {view.error && <div className="banner banner-broken">{view.error}</div>}
          {!view.data && view.loading ? (
            <div className="mg-empty" style={{ height }}>Drawing the graph…</div>
          ) : !data.available ? (
            <div className="mg-empty" style={{ height }}>The knowledge graph is not connected.</div>
          ) : shown.nodes.length === 0 ? (
            <div className="mg-empty" style={{ height }}>
              Nothing to draw yet. Facts, closed cases and the onboarding interview become connected entities here.
            </div>
          ) : (
            <GraphCanvas
              nodes={shown.nodes}
              edges={shown.edges}
              selected={selected?.id ?? null}
              matched={matched}
              matchedEdges={matchedEdges}
              focus={focus}
              onSelect={select}
              height={height}
            />
          )}
        </div>
        {selected && (
          <NodePanel
            detail={detail.data}
            loading={detail.loading}
            error={detail.error}
            onClose={() => setSelected(null)}
            onPick={pick}
          />
        )}
      </div>
    </section>
  )
}

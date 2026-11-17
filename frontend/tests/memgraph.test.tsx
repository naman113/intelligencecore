import { test } from 'node:test'
import assert from 'node:assert/strict'
import ReactDOMServer from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { LegendList, NodePanel } from '../src/components/MemoryGraph'
import {
  CANVAS,
  OTHER_COLOR,
  TYPE_ORDER,
  buildSimulation,
  centreOn,
  edgeLine,
  episodeTitle,
  factWhen,
  findNodes,
  graphUrl,
  labelLegend,
  mergeInto,
  neighbourhood,
  recallKind,
  relationLegend,
  settle,
  shortLabel,
  typeColor,
  visible,
  zoomAt,
  type MemEdge,
  type MemNode,
  type NodeDetail,
} from '../src/lib/memgraph'

/* Quick 014: the Memory graph view. The interactive canvas needs a browser;
 * everything it draws from is pure and tested here, plus the side panel and
 * the legend lists rendered to static markup. */

const ent = (id: string, name: string, type: string, degree = 1): MemNode => ({
  id, kind: 'entity', group: 'global', name, type, degree, links: [],
})
const fact = (id: string, source: string, target: string, name: string, extra: Partial<MemEdge> = {}): MemEdge => ({
  id, kind: 'fact', source, target, name, fact: `${source} ${name} ${target}`, ...extra,
})

const machine = ent('m', 'Excavator machine code 11', 'Machine', 3)
const drop = ent('d', 'fuel drop', 'EventType', 4)
const rule = ent('r', 'fuel drop definition', 'Rule', 2)
const win = ent('w', 'fuel drop detection window', 'Rule', 1)
const cse = ent('c', 'Case #7', 'Case', 2)
const pref = ent('p', 'fuel shown in litres', 'Preference', 0)
const ep: MemNode = {
  id: 'e', kind: 'episode', group: 'global', name: 'case_7_close', type: 'Episode',
  content: 'Case #7 ("Excavator 11 fuel drops"), closed 2026-10-20: Excavator machine code 11 logged 32 drops.',
  source: 'case_close', links: [],
}
const nodes = [machine, drop, rule, win, cse, pref, ep]
const edges: MemEdge[] = [
  fact('f1', 'm', 'd', 'HAS_ISSUE'),
  fact('f2', 'd', 'r', 'DEFINED_AS'),
  fact('f3', 'w', 'd', 'APPLIES_TO'),
  fact('f4', 'c', 'm', 'INVOLVES'),
  fact('f5', 'c', 'd', 'INVOLVES', { superseded: true, invalid_at: '2026-10-21T00:00:00Z' }),
  { id: 'm:e:m', kind: 'mentions', source: 'e', target: 'm', name: 'MENTIONS' },
]

test('graph url carries scope, search, caps and toggles — never a query', () => {
  const u = graphUrl({ scope: 'case:7', q: ' fuel drop ', episodes: true, superseded: false })
  assert.match(u, /^\/api\/memory\/graph\?/)
  assert.match(u, /scope=case%3A7/)
  assert.match(u, /q=fuel\+drop/)
  assert.match(u, /limit=300/)
  assert.match(u, /episodes=true/)
  assert.match(u, /superseded=false/)
  assert.doesNotMatch(graphUrl({ scope: 'everything' }), /q=/)
})

test('types keep a fixed colour, the rest fold into one neutral, never gold', () => {
  assert.equal(typeColor('Machine'), '#3987e5')
  assert.equal(typeColor('Rule'), typeColor('Rule'))
  assert.equal(new Set(TYPE_ORDER.map(typeColor)).size, TYPE_ORDER.length)
  assert.equal(typeColor('Preference'), OTHER_COLOR)
  assert.equal(typeColor('Entity'), OTHER_COLOR)
  for (const t of TYPE_ORDER) assert.notEqual(typeColor(t).toLowerCase(), '#d4952b')
  assert.equal(CANVAS, '#0B111B')
})

test('labels legend counts types in the fixed order; relationships by count', () => {
  const labels = labelLegend(nodes)
  assert.deepEqual(
    labels.map((r) => [r.label, r.count]),
    [['Machine', 1], ['Event type', 1], ['Rule', 2], ['Case', 1], ['Preference', 1], ['Episode', 1]],
  )
  const rels = relationLegend(edges)
  assert.deepEqual(rels[0], { key: 'INVOLVES', label: 'INVOLVES', count: 2, color: rels[0].color })
  assert.ok(rels.some((r) => r.key === 'MENTIONS'))
})

test('filters hide types, relationships, episodes and superseded facts', () => {
  const base = { hiddenTypes: new Set<string>(), hiddenRels: new Set<string>(), showEpisodes: true, showSuperseded: true }
  assert.equal(visible(nodes, edges, base).edges.length, 6)
  const noEp = visible(nodes, edges, { ...base, showEpisodes: false })
  assert.ok(!noEp.nodes.some((n) => n.kind === 'episode'))
  assert.ok(!noEp.edges.some((e) => e.kind === 'mentions'))
  assert.ok(!visible(nodes, edges, { ...base, showSuperseded: false }).edges.some((e) => e.id === 'f5'))
  const noRule = visible(nodes, edges, { ...base, hiddenTypes: new Set(['Rule']) })
  assert.ok(!noRule.edges.some((e) => e.id === 'f2' || e.id === 'f3'))
  assert.ok(!visible(nodes, edges, { ...base, hiddenRels: new Set(['INVOLVES']) }).edges.some((e) => e.name === 'INVOLVES'))
})

test('neighbourhood: machine <-> fuel drop <-> definition <-> case are connected', () => {
  const nb = neighbourhood(['d'], edges)
  assert.deepEqual([...nb.nodes].sort(), ['c', 'd', 'm', 'r', 'w'])
  assert.ok(nb.edges.has('f1') && nb.edges.has('f2') && nb.edges.has('f3'))
  assert.ok(!neighbourhood(['p'], edges).edges.size)
})

test('expanding merges a neighbourhood without duplicates', () => {
  const out = mergeInto({ nodes: [machine], edges: [] }, { nodes: [machine, drop], edges: [edges[0], edges[1]] })
  assert.equal(out.added, 1)
  assert.deepEqual(out.nodes.map((n) => n.id), ['m', 'd'])
  assert.deepEqual(out.edges.map((e) => e.id), ['f1'])   // f2's target is not drawn yet
})

test('search for element: exact, prefix, contains, then episode text', () => {
  assert.deepEqual(findNodes(nodes, 'fuel drop').map((n) => n.id), ['d', 'r', 'w', 'e'])
  assert.deepEqual(findNodes(nodes, 'excavator').map((n) => n.id), ['m', 'e'])
  assert.deepEqual(findNodes(nodes, '32 drops').map((n) => n.id), ['e'])
  assert.deepEqual(findNodes(nodes, '   '), [])
})

test('labels and words', () => {
  assert.equal(shortLabel('fuel drop definition', 8), 'fuel dr…')
  assert.equal(episodeTitle({ name: 'case_7_close', source: 'case_close' }), 'Case #7 closed')
  assert.equal(episodeTitle({ name: 'learning_3_r0', source: 'reflection:fact' }), 'Learned fact')
  assert.equal(recallKind({ kind: 'entity', fact: 'x', type: 'EventType' }), 'Event type')
  assert.equal(recallKind({ kind: 'episode', fact: 'x' }), 'Episode')
  assert.equal(recallKind({ kind: 'fact', fact: 'x' }), 'Fact')
  assert.equal(
    factWhen({ valid_at: '2026-10-01T00:00:00Z', invalid_at: '2026-10-21T00:00:00Z', created_at: '2026-10-02T00:00:00Z' }),
    'valid from 2026-10-01 · superseded 2026-10-21 · recorded 2026-10-02',
  )
})

test('layout settles inside the canvas and keeps known positions', () => {
  const s = buildSimulation(nodes, edges, 800, 500)
  settle(s.sim, 150)
  for (const n of s.simNodes) {
    assert.ok(Number.isFinite(n.x) && Number.isFinite(n.y))
    assert.ok(Math.abs((n.x ?? 0) - 400) < 800 && Math.abs((n.y ?? 0) - 250) < 600)
  }
  assert.equal(s.simLinks.length, edges.length)
  const prev = new Map([['m', { x: 11, y: 22, fx: 11, fy: 22 }]])
  const again = buildSimulation(nodes, edges, 800, 500, prev)
  assert.equal(again.byId.get('m')!.x, 11)
  assert.equal(again.byId.get('m')!.fx, 11)
})

test('geometry: arrows end on the target circle; zoom keeps the cursor point', () => {
  const ln = edgeLine({ x: 0, y: 0, r: 10 }, { x: 100, y: 0, r: 20 })
  assert.deepEqual(ln, { x1: 10, y1: 0, x2: 77, y2: 0 })
  const t = zoomAt({ k: 1, x: 0, y: 0 }, 100, 50, 2)
  assert.equal(t.k, 2)
  assert.equal((100 - t.x) / t.k, 100)
  assert.equal((50 - t.y) / t.k, 50)
  assert.equal(zoomAt({ k: 4, x: 0, y: 0 }, 0, 0, 10).k, 4)
  assert.deepEqual(centreOn(10, 20, 200, 100, 2), { k: 2, x: 80, y: 10 })
})

const html = (el: JSX.Element) => ReactDOMServer.renderToStaticMarkup(<MemoryRouter>{el}</MemoryRouter>)

test('legend list renders dots, names, counts and the hidden state', () => {
  const out = html(
    <LegendList title="Labels" kind="label" rows={labelLegend(nodes)} hidden={new Set(['Rule'])} onToggle={() => {}} />,
  )
  assert.match(out, /data-legend="label"/)
  assert.match(out, /Labels/)
  assert.match(out, /Event type/)
  assert.match(out, /aria-pressed="false"[^>]*title="Show Rule"/)
  const rel = html(
    <LegendList title="Relationships" kind="relationship" rows={relationLegend(edges)} hidden={new Set()} onToggle={() => {}} />,
  )
  assert.match(rel, /mg-dot mg-dot-rel/)
  assert.match(rel, /HAS_ISSUE/)
})

test('node panel: facts in and out with dates, superseded struck, episodes, links', () => {
  const detail: NodeDetail = {
    node: { ...drop, summary: 'A drop of at least 40 L or 12.5% within 5 minutes.', created_at: '2026-10-20T10:00:00Z' },
    facts_out: [{ ...edges[1], fact: 'A fuel drop is defined as a fall of 40 L or 12.5% within 5 minutes.', valid_at: '2026-10-20T00:00:00Z', other: { id: 'r', name: 'fuel drop definition', type: 'Rule' } }],
    facts_in: [
      { ...edges[0], fact: 'Excavator machine code 11 had 32 confirmed fuel drops.', other: { id: 'm', name: 'Excavator machine code 11', type: 'Machine' } },
      { ...edges[4], fact: 'Case #7 was about fuel drops on DG set 2.', other: { id: 'c', name: 'Case #7', type: 'Case' } },
    ],
    episodes: [{ ...ep, links: [{ kind: 'case', id: 7, label: 'Case #7', href: '/cases/7' }] }],
    links: [{ kind: 'kpi', id: 3, label: 'fuel drop', href: '/kpis/3' }],
    neighbours: { nodes: [], edges: [] },
  }
  const out = html(<NodePanel detail={detail} onClose={() => {}} onPick={() => {}} />)
  assert.match(out, /fuel drop/)
  assert.match(out, /Event type/)
  assert.match(out, /Facts from it \(1\)/)
  assert.match(out, /DEFINED_AS →/)
  assert.match(out, /Facts about it \(2\)/)
  assert.match(out, /← HAS_ISSUE/)
  assert.match(out, /valid from 2026-10-20/)
  assert.match(out, /class="mg-struck">Case #7 was about fuel drops on DG set 2.</)
  assert.match(out, /superseded/)
  assert.match(out, /Mentioned in \(1\)/)
  assert.match(out, /Excavator machine code 11 logged 32 drops\./)   // full episode text
  assert.match(out, /href="\/cases\/7"/)
  assert.match(out, /href="\/kpis\/3"/)
  assert.match(out, /KPI: fuel drop/)
})

test('node panel for an episode lists what it mentions', () => {
  const out = html(
    <NodePanel
      detail={{ node: ep, facts_out: [], facts_in: [], episodes: [ep], links: [], mentions: [machine, drop], neighbours: { nodes: [], edges: [] } }}
      onClose={() => {}}
      onPick={() => {}}
    />,
  )
  assert.match(out, /Case #7 closed/)
  assert.match(out, /Mentions \(2\)/)
  assert.match(out, /Excavator machine code 11/)
  const loading = html(<NodePanel detail={null} loading onClose={() => {}} onPick={() => {}} />)
  assert.match(loading, /Loading…/)
})

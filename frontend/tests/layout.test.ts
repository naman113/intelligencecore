import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  canMoveDown,
  canMoveUp,
  fromSpec,
  isChanged,
  layoutErrorText,
  moveDown,
  moveToSection,
  moveUp,
  setDefaultRange,
  setHidden,
  setSectionTitle,
  setTitle,
  setWidth,
  toChanges,
} from '../src/lib/layout'

const spec = {
  spec_version: 2,
  date_control: { default: 'last_7_days', presets: ['last_7_days', 'last_30_days'] },
  sections: [{ title: 'Top', tiles: ['a', 'b'] }, { tiles: ['c', 'd'] }],
  tiles: [
    { id: 'a', type: 'stat', title: 'A' },
    { id: 'b', type: 'stat', title: 'B', width: 'third' },
    { id: 'c', type: 'table', title: 'C', hidden: true },
    { id: 'd', type: 'line', title: 'D' },
  ],
}
const ids = (st: ReturnType<typeof fromSpec>) => st.sections.map((s) => s.tiles.map((t) => t.id))

test('fromSpec: defaults per tile type, hidden, section titles, range', () => {
  const st = fromSpec(spec)
  assert.deepEqual(ids(st), [['a', 'b'], ['c', 'd']])
  const t = Object.fromEntries(st.sections.flatMap((s) => s.tiles).map((x) => [x.id, x]))
  assert.equal(t.a.width, 'quarter')
  assert.equal(t.b.width, 'third')
  assert.equal(t.c.width, 'full')
  assert.equal(t.d.width, 'half')
  assert.equal(t.c.hidden, true)
  assert.equal(st.sections[1].title, '')
  assert.equal(st.defaultRange, 'last_7_days')
  assert.equal(st.hasDateControl, true)
})

test('moves within a section and across edges', () => {
  const st = fromSpec(spec)
  assert.deepEqual(ids(moveDown(st, 'a').state), [['b', 'a'], ['c', 'd']])
  assert.deepEqual(ids(moveUp(st, 'b').state), [['b', 'a'], ['c', 'd']])
  assert.deepEqual(ids(moveDown(st, 'b').state), [['a'], ['b', 'c', 'd']])
  assert.deepEqual(ids(moveUp(st, 'c').state), [['a', 'b', 'c'], ['d']])
})

test('first and last positions are no-ops and disabled', () => {
  const st = fromSpec(spec)
  assert.equal(canMoveUp(st, 'a'), false)
  assert.equal(canMoveDown(st, 'd'), false)
  assert.equal(canMoveUp(st, 'b'), true)
  assert.equal(moveUp(st, 'a').state, st)
  assert.equal(moveDown(st, 'd').state, st)
})

test('the last tile of a section cannot leave it', () => {
  const one = fromSpec({ ...spec, sections: [{ tiles: ['a'] }, { tiles: ['b', 'c', 'd'] }] })
  const r = moveDown(one, 'a')
  assert.equal(r.state, one)
  assert.match(r.reason ?? '', /at least one tile/)
  assert.ok(moveToSection(one, 'a', 1).reason)
  assert.equal(moveUp(one, 'b').reason, null)
})

test('moveToSection appends to the target', () => {
  const st = fromSpec(spec)
  assert.deepEqual(ids(moveToSection(st, 'a', 1).state), [['b'], ['c', 'd', 'a']])
  assert.equal(moveToSection(st, 'a', 0).state, st)
})

test('setters do not mutate', () => {
  const st = fromSpec(spec)
  const snap = JSON.stringify(st)
  setWidth(st, 'a', 'full')
  setTitle(st, 'a', 'X')
  setHidden(st, 'a', true)
  setSectionTitle(st, 0, 'Y')
  setDefaultRange(st, 'last_30_days')
  assert.equal(JSON.stringify(st), snap)
  assert.equal(isChanged(st, st), false)
  assert.equal(isChanged(setWidth(st, 'a', 'full'), st), true)
})

test('toChanges is exactly the C-13 body', () => {
  const base = fromSpec(spec)
  let st = setTitle(base, 'a', 'Total')
  st = setWidth(st, 'b', 'full')
  st = setHidden(st, 'd', true)
  st = setSectionTitle(st, 1, '')
  st = setDefaultRange(st, 'last_30_days')
  st = moveDown(st, 'a').state
  const c = toChanges(st, 3, base)
  assert.deepEqual(
    Object.keys(c).sort(),
    ['base_version', 'default_range', 'hidden', 'note', 'sections', 'titles', 'widths'],
  )
  assert.equal(c.base_version, 3)
  assert.deepEqual(c.sections, [
    { title: 'Top', tiles: ['b', 'a'] },
    { title: null, tiles: ['c', 'd'] },
  ])
  assert.deepEqual(Object.keys(c.widths).sort(), ['a', 'b', 'c', 'd'])
  assert.equal(c.widths.b, 'full')
  assert.deepEqual(c.titles, { a: 'Total' })
  assert.deepEqual(c.hidden, ['c', 'd'])
  assert.equal(c.default_range, 'last_30_days')
})

test('no date control gives default_range null', () => {
  const st = fromSpec({ ...spec, date_control: undefined })
  assert.equal(st.hasDateControl, false)
  assert.equal(toChanges(st, 1, st).default_range, null)
})

test('layoutErrorText shows the first finding', () => {
  assert.equal(
    layoutErrorText(JSON.stringify({ findings: [{ rule: 'layout', path: 'widths.a', message: 'bad width' }] })),
    'widths.a: bad width',
  )
  assert.equal(layoutErrorText('plain'), 'plain')
})

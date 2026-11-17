import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  KPI_TITLE_ID,
  REVEAL_GAP,
  kpiNeighbours,
  kpiOrder,
  listScrollTop,
  pickKpi,
  planReveal,
  revealBehavior,
  revealScrollTop,
} from '../src/lib/kpinav'
import type { Watch } from '../src/lib/types'

const mk = (o: Partial<Watch>) =>
  ({ id: 1, kind: 'kpi', title: 'T', status: 'active', ...o }) as unknown as Watch

test('constants', () => {
  assert.equal(KPI_TITLE_ID, 'kpi-detail-title')
  assert.equal(REVEAL_GAP, 12)
})

test('kpiOrder: kpis first then ascending id, input not mutated', () => {
  const input = [mk({ id: 1, kind: 'condition' }), mk({ id: 7 }), mk({ id: 3 })]
  const out = kpiOrder(input)
  assert.deepEqual(out.map((w) => w.id), [3, 7, 1])
  assert.deepEqual(input.map((w) => w.id), [1, 7, 3])
})

test('kpiNeighbours', () => {
  const ordered = [mk({ id: 3, title: 'C' }), mk({ id: 7, title: 'G' }), mk({ id: 1, title: 'A' })]
  const mid = kpiNeighbours(ordered, 7)
  assert.deepEqual(mid, { prev: { id: 3, title: 'C' }, next: { id: 1, title: 'A' }, position: 2, total: 3 })
  const first = kpiNeighbours(ordered, 3)
  assert.equal(first?.prev, null)
  assert.equal(first?.position, 1)
  const last = kpiNeighbours(ordered, 1)
  assert.equal(last?.next, null)
  assert.equal(last?.position, 3)
  const one = kpiNeighbours([mk({ id: 5 })], 5)
  assert.equal(one?.prev, null)
  assert.equal(one?.next, null)
  assert.equal(one?.position, 1)
  assert.equal(one?.total, 1)
  assert.equal(kpiNeighbours(ordered, 99), null)
  assert.equal(kpiNeighbours(ordered, null), null)
})

const base = { viewportHeight: 800, headerHeight: 60, scrollY: 1000 }

test('revealScrollTop', () => {
  assert.equal(revealScrollTop({ ...base, headingTop: 100, headingBottom: 130 }), null)
  assert.equal(revealScrollTop({ ...base, headingTop: 900, headingBottom: 930 }), 1828)
  assert.equal(revealScrollTop({ ...base, headingTop: 30, headingBottom: 60 }), 958)
  assert.equal(
    revealScrollTop({ ...base, scrollY: 100, headingTop: -400, headingBottom: -370 }),
    0,
  )
  assert.equal(typeof revealScrollTop({ ...base, headingTop: 780, headingBottom: 820 }), 'number')
  assert.equal(
    revealScrollTop({ ...base, headerHeight: 0, headingTop: 0, headingBottom: 30 }),
    null,
  )
})

test('planReveal', () => {
  const s = { previousKey: 'a', key: 'b', hasDetail: true, scrollTop: 500 as number | null, focusInDetail: false }
  assert.deepEqual(planReveal({ ...s, previousKey: null }), { scroll: false, focus: false })
  assert.deepEqual(planReveal({ ...s, previousKey: 'b' }), { scroll: false, focus: false })
  assert.deepEqual(planReveal({ ...s, hasDetail: false }), { scroll: false, focus: false })
  assert.deepEqual(planReveal(s), { scroll: true, focus: true })
  assert.deepEqual(planReveal({ ...s, scrollTop: null }), { scroll: false, focus: true })
  assert.deepEqual(planReveal({ ...s, scrollTop: null, focusInDetail: true }), { scroll: false, focus: false })
  assert.deepEqual(planReveal({ ...s, focusInDetail: true }), { scroll: true, focus: false })
})

test('revealBehavior', () => {
  assert.equal(revealBehavior(true), 'auto')
  assert.equal(revealBehavior(false), 'smooth')
})

test('listScrollTop', () => {
  const v = { scrollTop: 100, height: 300 }
  assert.equal(listScrollTop({ ...v, itemTop: 150, itemBottom: 200 }), null)
  assert.equal(listScrollTop({ ...v, itemTop: 40, itemBottom: 90 }), 40)
  assert.equal(listScrollTop({ ...v, itemTop: 420, itemBottom: 470 }), 170)
})

test('pickKpi: bare /kpis opens the first in list order; an id opens that KPI', () => {
  const ordered = kpiOrder([mk({ id: 1, kind: 'condition' }), mk({ id: 7 }), mk({ id: 3 })])
  assert.equal(pickKpi(ordered, undefined)?.id, 3)
  assert.equal(pickKpi(ordered, '7')?.id, 7)
  assert.equal(pickKpi(ordered, '1')?.id, 1)
  assert.equal(pickKpi(ordered, '99'), undefined)
  assert.equal(pickKpi([], undefined), undefined)
  const steps = kpiNeighbours(ordered, pickKpi(ordered, undefined)?.id ?? null)
  assert.equal(steps?.position, 1)
})

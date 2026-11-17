import { test } from 'node:test'
import assert from 'node:assert/strict'
import ReactDOMServer from 'react-dom/server'
import { GraphStatusLine } from '../src/components/GraphStatus'
import { backfillNote, describeGraph, extractionNote, pendingTotal, rebuildNote, type GraphStatus } from '../src/lib/graph'

/* Quick 009: the graph line says connected / empty / not connected (reason),
 * never the old ambiguous "empty (or unreachable)". */

const render = (s: GraphStatus | null, error?: string) =>
  ReactDOMServer.renderToStaticMarkup(<GraphStatusLine status={s} error={error} />)

test('describeGraph states', () => {
  assert.deepEqual(describeGraph({ state: 'connected', episodes: 12, scopes: 3 }), {
    tone: 'settled', label: 'connected', detail: '12 episodes in 3 scopes',
  })
  const empty = describeGraph({ state: 'empty', episodes: 0 })
  assert.equal(empty.label, 'empty')
  assert.equal(empty.tone, 'owed')
  const down = describeGraph({
    state: 'not_connected', next_retry_in: 42,
    reason: "ResponseError: Received 5 arguments to procedure 'db.idx.fulltext.createNodeIndex', expected at most 1",
  })
  assert.equal(down.label, 'not connected')
  assert.equal(down.tone, 'broken')
  assert.match(down.detail, /createNodeIndex/)
  assert.match(down.detail, /Retrying by itself in 42 s/)
  assert.equal(describeGraph({ state: 'disabled', reason: 'off' }).label, 'turned off')
  assert.equal(describeGraph(null).label, 'checking')
})

test('pending and backfill notes', () => {
  assert.equal(pendingTotal({ state: 'connected', pending: { learnings: 2, cases: 1 } }), 3)
  assert.equal(pendingTotal(null), 0)
  assert.equal(backfillNote({ state: 'connected' }), '')
  assert.match(
    backfillNote({ state: 'connected', backfill: { running: true, learnings: { sent: 2, already: 0, failed: 0 } } }),
    /2 sent so far/,
  )
  assert.equal(
    backfillNote({
      state: 'connected',
      backfill: {
        running: false, finished_at: '2026-10-25T10:00:00Z',
        learnings: { sent: 3, already: 1, failed: 0 }, cases: { sent: 1, already: 0, failed: 1 },
      },
    }),
    'Last copy: 4 sent, 1 already there, 1 failed.',
  )
})

test('line renders the state, reconnect and copy buttons', () => {
  const up = render({ state: 'connected', episodes: 5, scopes: 1, pending: { learnings: 2 } })
  assert.match(up, /data-graph-status="connected"/)
  assert.match(up, /Knowledge graph/)
  assert.match(up, /5 episodes in 1 scope/)
  assert.match(up, /Reconnect/)
  assert.match(up, /Copy 2 items into the graph/)
  assert.doesNotMatch(up, /unreachable/)

  const down = render({ state: 'not_connected', reason: 'ConnectionError: Error 111', next_retry_in: 30, pending: { learnings: 2 } })
  assert.match(down, /not connected/)
  assert.match(down, /Error 111/)
  assert.match(down, /Reconnect/)
  assert.doesNotMatch(down, /Copy 2/)            // nothing to copy into while down

  const off = render({ state: 'disabled', reason: 'turned off on the Setup page' })
  assert.doesNotMatch(off, /Reconnect/)
})

test('learnings filter always offers a Lesson tab, in a fixed order', async () => {
  const { learningKinds } = await import('../src/pages/Memory')
  const l = (kind: string) => ({ id: 1, thread_id: null, kind, content: 'x', created_at: '', updated_at: '', revisions: 0 })
  assert.deepEqual(learningKinds([]), ['lesson'])
  assert.deepEqual(learningKinds([l('recipe'), l('fact')]), ['fact', 'recipe', 'lesson'])
  assert.deepEqual(learningKinds([l('lesson'), l('preference'), l('zeta')]), ['preference', 'lesson', 'zeta'])
})

/* Quick 014: entities and facts on the line; extraction failures never silent. */
test('connected line counts entities and facts; failures and rebuilds show', () => {
  const s: GraphStatus = {
    state: 'connected', episodes: 8, scopes: 1, entities: 24, facts: 30,
    extraction: { ok: 7, failed: 2, last_error: 'BadRequestError: model not found' },
    rebuild: { running: false, finished_at: '2026-10-26T04:00:00Z', result: { ingested: 8, failed: 0 } },
  }
  assert.equal(describeGraph(s).detail, '8 episodes in 1 scope: 24 entities linked by 30 facts')
  assert.match(extractionNote(s), /2 episodes could not be extracted/)
  assert.match(extractionNote(s), /model not found/)
  assert.equal(extractionNote({ state: 'connected', extraction: { ok: 3, failed: 0 } }), '')
  assert.equal(rebuildNote(s), 'Last rebuild: 8 episodes re-ingested.')
  assert.match(rebuildNote({ state: 'connected', rebuild: { running: true, phase: 'ingesting', total: 8, done: 3 } }), /3 of 8/)
  const html = render(s)
  assert.match(html, /data-graph-extraction="failed"/)
  assert.match(html, /24 entities linked by 30 facts/)
})

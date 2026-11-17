import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import ReactDOMServer from 'react-dom/server'
import { PendingBlock, syncDraft } from '../src/components/PendingBlock'
import type { ContextPending } from '../src/lib/ictypes'

const mk = (id: number, text: string | null): ContextPending => ({
  action_id: id, text, before: null, created_at: '2026-10-25T00:00:00Z', origin: 'agent', edited_by: null,
})
const noop = () => {}
const handlers = { onApprove: noop, onReject: noop, onEdit: noop, onSave: noop }

test('syncDraft keeps the owner edit for the same proposal', () => {
  assert.deepEqual(syncDraft({ id: 5, text: 'owner edit' }, mk(5, 'agent text')), { id: 5, text: 'owner edit' })
})
test('syncDraft resets to the new proposal text', () => {
  assert.deepEqual(syncDraft({ id: 5, text: 'owner edit' }, mk(9, 'new proposal')), { id: 9, text: 'new proposal' })
  assert.deepEqual(syncDraft({ id: 5, text: 'x' }, mk(9, null)), { id: 9, text: '' })
})
test('every PendingBlock call site is keyed by action_id', () => {
  const walk = (d: string): string[] =>
    readdirSync(d).flatMap((f) => {
      const p = join(d, f)
      return statSync(p).isDirectory() ? walk(p) : [p]
    })
  let n = 0
  for (const f of walk('src').filter((f) => /\.tsx$/.test(f))) {
    if (/PendingBlock\.tsx$/.test(f)) continue
    for (const m of readFileSync(f, 'utf8').matchAll(/<PendingBlock[^>]*/g)) {
      n++
      assert.match(m[0], /key=\{[^}]*action_id/, f)
    }
  }
  assert.ok(n >= 3)
})
test('editing textarea shows the current pending text', () => {
  const html = ReactDOMServer.renderToStaticMarkup(
    <PendingBlock pending={mk(9, 'new proposal')} editing={9} handlers={handlers} />,
  )
  assert.match(html, /<textarea[^>]*>new proposal<\/textarea>/)
})

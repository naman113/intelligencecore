import { test } from 'node:test'
import assert from 'node:assert/strict'
import { groupSchema } from '../src/lib/contextActions'
import { ctxSources, ctxTables, ctxColumns, ctxRules, ctxTable, ctxColumn, ctxEdited, ctxHide } from './fixtures'
import type { ContextColumn } from '../src/lib/ictypes'

const REF = /^business_db\.[^.]+\.[^.]+(\.[^.]+)?$/

test('table and column refs are business_db.<schema>.<table>[.<column>]', () => {
  for (const t of ctxTables) assert.match(t.ref, REF)
  for (const c of ctxColumns) {
    assert.match(c.ref, REF)
    assert.match(c.table_ref, REF)
  }
})

test('action payload refs use the real format, hide keeps origin owner', () => {
  for (const a of [ctxTable, ctxColumn, ctxEdited, ctxHide]) {
    assert.match((a.payload as { ref: string }).ref, REF)
  }
  assert.equal((ctxHide.payload as { origin: string }).origin, 'owner')
})

test('no fixture mentions the invented warehouse source', () => {
  const all = JSON.stringify([ctxSources, ctxTables, ctxColumns, ctxRules, ctxTable, ctxColumn, ctxEdited, ctxHide])
  assert.ok(!all.includes('warehouse'))
})

test('source is business_db and pending is an integer', () => {
  assert.equal(ctxSources[0].name, 'business_db')
  for (const s of ctxSources) assert.ok(Number.isInteger(s.pending))
})

test('rule origin is agent, human or null', () => {
  for (const r of ctxRules) assert.ok(r.origin === 'agent' || r.origin === 'human' || r.origin === null)
})

test('groupSchema parses a real business_db ref (IN-02)', () => {
  const col = { ...ctxColumns[0], table_ref: 'business_db.public.orphan_t' } as ContextColumn
  const t = groupSchema([], [col])[0].table
  assert.equal(t.schema, 'public')
  assert.equal(t.table, 'orphan_t')
})

test('groupSchema still parses a legacy source:schema.table ref', () => {
  const col = { ...ctxColumns[0], table_ref: 'src:schema.t' } as ContextColumn
  const t = groupSchema([], [col])[0].table
  assert.equal(t.schema, 'schema')
  assert.equal(t.table, 't')
})

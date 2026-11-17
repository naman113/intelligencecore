import { test } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import ReactDOMServer from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import {
  PACK_ACCEPT,
  PACK_MAX_BYTES,
  buildPackForm,
  importGroups,
  validatePack,
} from '../src/lib/contextpack'
import * as cp from '../src/lib/contextpack'
import { asks } from '../src/lib/ask'
import { ContextPackUploadView } from '../src/components/ContextPackUpload'
import { ImportBar } from '../src/pages/Approvals'
import type { Action } from '../src/lib/types'

const render = (el: React.ReactElement) =>
  ReactDOMServer.renderToStaticMarkup(<MemoryRouter>{el}</MemoryRouter>)

const act = (id: number, kind: string, run: number | null): Action =>
  ({ id, kind, status: 'proposed', task_run_id: run, payload: {} }) as unknown as Action

test('constants and validation', () => {
  assert.equal(PACK_ACCEPT, '.md,.txt,.json,.yaml,.yml')
  assert.equal(PACK_MAX_BYTES, 2_000_000)
  assert.equal(validatePack({ name: 'a.yaml', size: 10 }), null)
  assert.ok(validatePack({ name: 'a.exe', size: 10 }))
  assert.ok(validatePack({ name: 'a.md', size: PACK_MAX_BYTES + 1 }))
  assert.equal('packSafeName' in cp, false)
})

test('buildPackForm keeps operator and a filename-free instruction', () => {
  const name = 'my pack (v2).yaml'
  const f = buildPackForm(new Blob(['x']), name, 'ops')
  assert.equal(f.get('operator'), 'ops')
  assert.equal(f.get('instruction'), asks.importContextPack())
  assert.ok(!String(f.get('instruction')).includes(name))
  assert.equal((f.get('file') as File).name, name)
})

test('upload view states', () => {
  const off = render(<ContextPackUploadView skill={{ enabled: false }} />)
  assert.ok(off.includes('type="file"') && off.includes('accept=".md,.txt,.json,.yaml,.yml"'))
  assert.ok(off.includes('Upload') && off.includes('Enable skill'))
  assert.ok(off.includes('runs in your main Ask chat') && off.includes('Nothing is applied until you approve'))
  assert.ok(render(<ContextPackUploadView skill={{ enabled: true }} />).includes('enabled'))
  const none = render(<ContextPackUploadView skill={null} />)
  assert.ok(!none.includes('Enable skill') && !none.includes('skill: enabled'))
})

test('component sources', () => {
  const src = fs.readFileSync('src/components/ContextPackUpload.tsx', 'utf8')
  assert.ok(src.includes('getOperator()') && !src.includes("'owner'"))
  for (const p of ['src/pages/Schema.tsx', 'src/pages/ContextSources.tsx'])
    assert.ok(fs.readFileSync(p, 'utf8').includes('<ContextPackUpload />'))
})

test('importGroups', () => {
  const list = [
    act(1, 'context_change', 41),
    act(2, 'context_change', 41),
    act(3, 'context_change', 41),
    act(4, 'hide_table', 41),
    act(5, 'publish_dashboard', 41),
    act(6, 'create_watch', 50),
    act(7, 'context_change', null),
    act(8, 'create_watch', 60),
    act(9, 'hide_table', 60),
  ]
  const g = importGroups(list)
  assert.deepEqual(g.map((x) => x.runId), [60, 41])
  assert.deepEqual(g[1].ids, [1, 2, 3, 4])
  const html = render(<ImportBar group={g[1]} onDone={() => {}} />)
  assert.ok(html.includes('Approve all 4 from this import'))
})

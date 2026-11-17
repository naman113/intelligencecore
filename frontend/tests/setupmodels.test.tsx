import { test } from 'node:test'
import assert from 'node:assert/strict'
import ReactDOMServer from 'react-dom/server'
import { ModelField } from '../src/pages/Setup'
import type { ConfigField } from '../src/lib/types'

const base: ConfigField = {
  key: 'supervisor_model', label: 'Supervisor model', value: 'claude-opus-5-5', default: 'claude-opus-5-5',
  secret: false, required: false, set: false, source: 'default',
  options: [
    { value: 'claude-opus-5-5', note: 'Opus note', group: 'current' },
    { value: 'claude-fable-5-1', note: 'Fable needs 30-day retention', group: 'current' },
    { value: 'claude-sonnet-4-5', note: 'Old', group: 'legacy' },
  ],
}
const html = (f: ConfigField) =>
  ReactDOMServer.renderToStaticMarkup(<ModelField f={f} onChange={() => {}} />)

test('default selection shows groups, note and effective model', () => {
  const h = html(base)
  assert.match(h, /\(default: claude-opus-5-5\)/)
  assert.match(h, /optgroup label="Current"/)
  assert.match(h, /optgroup label="Legacy/)
  assert.match(h, /claude-fable-5-1/)
  assert.match(h, /Other model ID/)
  assert.match(h, /Opus note/)
  assert.match(h, /Effective: claude-opus-5-5/)
  assert.match(h, /set in default/)
})

test('saved fable shows its 30-day note', () => {
  const h = html({ ...base, source: 'db', value: 'claude-fable-5-1' })
  assert.match(h, /<option value="claude-fable-5-1" selected/)
  assert.match(h, /30-day/)
})

test('unknown saved id renders the Other input', () => {
  const h = html({ ...base, source: 'db', value: 'claude-custom-x' })
  assert.match(h, /<input[^>]*value="claude-custom-x"/)
})

import { modelSuggestions } from '../src/lib/models'
import type { ConfigResponse } from '../src/lib/types'

test('modelSuggestions without config', () => {
  assert.deepEqual(modelSuggestions(undefined), ['auto', 'gpt-4o', 'gpt-4o-mini'])
})

test('modelSuggestions orders current before legacy, no duplicates', () => {
  const opts = [
    { value: 'claude-sonnet-4-5', note: '', group: 'legacy' as const },
    { value: 'claude-opus-5-5', note: '', group: 'current' as const },
    { value: 'claude-sonnet-5-5', note: '', group: 'current' as const },
  ]
  const mk = (key: string): ConfigField => ({ ...base, key, options: opts })
  const cfg: ConfigResponse = { groups: {}, optional: [], config: { llm: [mk('a'), mk('b')] } }
  assert.deepEqual(modelSuggestions(cfg), [
    'auto', 'claude-opus-5-5', 'claude-sonnet-5-5', 'claude-sonnet-4-5', 'gpt-4o', 'gpt-4o-mini',
  ])
})

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { renderToStaticMarkup } from 'react-dom/server'
import { AskThreadView, OperatorField } from '../src/components/AskPanel'
import type { AskThread } from '../src/lib/ask'

const mk = (over: Partial<AskThread> = {}): AskThread => ({
  thread_id: 'ui:owner',
  messages: [],
  pending: false,
  last_run: null,
  ...over,
})

test('thread escapes html and linkifies http(s)', () => {
  const h = renderToStaticMarkup(
    <AskThreadView
      thread={mk({
        messages: [
          { id: 1, role: 'user', content: 'first', created_at: '' },
          { id: 2, role: 'assistant', content: '<img src=x onerror=alert(1)> see https://a.example.com/x', created_at: '' },
        ],
      })}
    />,
  )
  assert.ok(h.includes('&lt;img') && !h.includes('<img'))
  assert.ok(h.includes('rel="noopener noreferrer"') && h.includes('target="_blank"'))
  assert.ok(h.indexOf('ask-msg ask-msg-user') < h.indexOf('ask-msg ask-msg-assistant'))
})

test('thread pending and failed', () => {
  assert.ok(renderToStaticMarkup(<AskThreadView thread={mk({ pending: true })} />).includes('Working on it'))
  const h = renderToStaticMarkup(
    <AskThreadView thread={mk({ last_run: { id: 1, status: 'failed', error: 'boom' } })} />,
  )
  assert.ok(h.includes('banner-broken') && h.includes('boom'))
})

test('operator field', () => {
  const h = renderToStaticMarkup(<OperatorField value="owner" error={null} />)
  assert.ok(h.includes('You are') && h.includes('value="owner"') && h.includes('maxLength="32"'))
  assert.ok(!h.includes('Use lowercase'))
  const e = renderToStaticMarkup(<OperatorField value="x" error="Use lowercase letters, - or _ (up to 32)" />)
  assert.ok(e.includes('Use lowercase letters, - or _ (up to 32)'))
})

test('ask.css has no gold', () => {
  assert.ok(!readFileSync('src/components/ask.css', 'utf8').includes('--owed'))
})

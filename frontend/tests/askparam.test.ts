// Phase 22 (22-12, DCAP-14): the ?ask= prefill reader.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readAskParam, takeAskParam, withoutAskParam } from '../src/lib/askparam'

test('reads the Explain text the wrapper builds', () => {
  assert.equal(
    readAskParam('?ask=Explain%20%22Kg%20in%22%20on%20the%20dashboard%20daily-operations'),
    'Explain "Kg in" on the dashboard daily-operations',
  )
})

test('missing or blank values give null', () => {
  assert.equal(readAskParam(''), null)
  assert.equal(readAskParam('?other=1'), null)
  assert.equal(readAskParam('?ask='), null)
  assert.equal(readAskParam('?ask=%20%20%09'), null)
  assert.equal(readAskParam('?ask=%00%01'), null)
})

test('trims, strips control characters and bidi overrides, and cuts at 500 characters', () => {
  assert.equal(readAskParam('?ask=%20%20hello%20'), 'hello')
  assert.equal(readAskParam('?ask=a%00b%0Ac%E2%80%AEd'), 'a b c d')
  const out = readAskParam('?ask=' + 'x'.repeat(600))
  assert.equal(out, 'x'.repeat(500))
  const emoji = readAskParam('?ask=' + encodeURIComponent('\u{1F600}'.repeat(600)))
  assert.equal(Array.from(emoji as string).length, 500)
})

test('a malformed escape does not throw', () => {
  // the decoder turns the broken escape into U+FFFD and keeps the rest as text
  assert.equal(readAskParam('?ask=%E0%A4%A'), '�%A')
})

test('takeAskParam prefills once, never sends, and drops ask from the address bar', () => {
  const opened: string[] = []
  const replaced: string[] = []
  const win = {
    location: { search: '?ask=Explain%20x&t=1', pathname: '/overview', hash: '#h' },
    history: { state: { k: 1 }, replaceState: (_s: unknown, _t: string, url: string) => replaced.push(url) },
  }
  assert.equal(takeAskParam(win, (t) => opened.push(t)), true)
  assert.deepEqual(opened, ['Explain x'])
  assert.deepEqual(replaced, ['/overview?t=1#h'])
  const none = { ...win, location: { ...win.location, search: '?t=1' } }
  assert.equal(takeAskParam(none, (t) => opened.push(t)), false)
  assert.equal(opened.length, 1)
  assert.equal(replaced.length, 1)
})

test('withoutAskParam keeps the rest of the query', () => {
  assert.equal(withoutAskParam('?ask=hi&t=1'), '?t=1')
  assert.equal(withoutAskParam('?ask=hi'), '')
  assert.equal(withoutAskParam('?t=1'), '?t=1')
})

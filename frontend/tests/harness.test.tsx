import { test } from 'node:test'
import assert from 'node:assert/strict'
import ReactDOMServer from 'react-dom/server'
import { Empty } from '../src/components/ui'

test('renders Empty to static markup', () => {
  const html = ReactDOMServer.renderToStaticMarkup(<Empty title="Nothing here" />)
  assert.ok(html.includes('Nothing here'))
})

test('localStorage shim round-trips', () => {
  localStorage.setItem('k', 'v')
  assert.equal(localStorage.getItem('k'), 'v')
  assert.equal(localStorage.getItem('missing'), null)
  localStorage.removeItem('k')
  assert.equal(localStorage.getItem('k'), null)
})

test('matchMedia exists and is false', () => {
  assert.equal(window.matchMedia('(max-width: 1px)').matches, false)
})

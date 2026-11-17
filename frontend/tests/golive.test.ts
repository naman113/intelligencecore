import { test } from 'node:test'
import assert from 'node:assert/strict'
import { guessDashboardsBase } from '../src/lib/dashlinks'

const loc = (port: string, hostname = 'localhost') => ({ protocol: 'http:', hostname, port })

test('two-digit stack ports map 9NN0 to 9NN2', () => {
  assert.equal(guessDashboardsBase(loc('9110')), 'http://localhost:9112')
  assert.equal(guessDashboardsBase(loc('9000')), 'http://localhost:9002')
  assert.equal(guessDashboardsBase(loc('9080')), 'http://localhost:9082')
  assert.equal(guessDashboardsBase(loc('9120')), 'http://localhost:9122')
})
test('existing guesses unchanged', () => {
  assert.equal(guessDashboardsBase(loc('8090')), 'http://localhost:8091')
  assert.equal(guessDashboardsBase(loc('', 'app.acme.com')), 'http://dash.acme.com')
  assert.equal(guessDashboardsBase(loc('9111')), null)
  assert.equal(guessDashboardsBase(loc('5173')), null)
})

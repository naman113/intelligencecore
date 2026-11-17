import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  currentLink,
  dashboardsBase,
  draftLink,
  guessDashboardsBase,
  normalizeBase,
  payloadDraftLink,
  versionLink,
} from '../src/lib/dashlinks'

test('normalizeBase', () => {
  assert.equal(normalizeBase(' https://dash.example.com/ '), 'https://dash.example.com')
  assert.equal(normalizeBase('javascript:x'), null)
  assert.equal(normalizeBase(''), null)
})

test('guessDashboardsBase', () => {
  assert.equal(guessDashboardsBase({ protocol: 'https:', hostname: 'app.acme.com', port: '' }), 'https://dash.acme.com')
  assert.equal(guessDashboardsBase({ protocol: 'http:', hostname: 'localhost', port: '8090' }), 'http://localhost:8091')
  assert.equal(guessDashboardsBase({ protocol: 'http:', hostname: 'localhost', port: '9080' }), 'http://localhost:9082')
  for (const [a, b] of [['9100', '9102'], ['9000', '9002'], ['9010', '9012'], ['9110', '9112'], ['9120', '9122'], ['9990', '9992']]) {
    assert.equal(guessDashboardsBase({ protocol: 'http:', hostname: 'localhost', port: a }), `http://localhost:${b}`)
  }
  for (const port of ['9101', '91000', '900']) {
    assert.equal(guessDashboardsBase({ protocol: 'http:', hostname: 'localhost', port }), null)
  }
  assert.equal(guessDashboardsBase({ protocol: 'http:', hostname: 'localhost', port: '5173' }), null)
})

test('dashboardsBase prefers configured', () => {
  const loc = { protocol: 'http:', hostname: 'localhost', port: '9080' }
  assert.equal(dashboardsBase('https://d.x/', loc), 'https://d.x')
  assert.equal(dashboardsBase('', loc), 'http://localhost:9082')
  assert.equal(dashboardsBase('bad', loc), 'http://localhost:9082')
})

test('dashboardsBase and payloadDraftLink on the p10 stack', () => {
  const loc = { protocol: 'http:', hostname: '127.0.0.1', port: '9100' }
  assert.equal(dashboardsBase('', loc), 'http://127.0.0.1:9102')
  assert.equal(payloadDraftLink('/d/sales/v/2/?p=tok', 'http://127.0.0.1:9102', false), 'http://127.0.0.1:9102/d/sales/v/2/?p=tok')
})

test('link builders', () => {
  assert.equal(currentLink('https://d.x', 'sales', 't0k'), 'https://d.x/d/sales/?t=t0k')
  assert.equal(versionLink('https://d.x', 'sales', 3, 't0k'), 'https://d.x/d/sales/v/3/?t=t0k')
  assert.equal(draftLink('https://d.x', '/d/sales/v/4/', 'p1'), 'https://d.x/d/sales/v/4/?p=p1')
  assert.equal(currentLink('https://d.x', 'a b', 't&k'), 'https://d.x/d/a%20b/?t=t%26k')
  assert.equal(currentLink(null, 'sales', 't'), null)
  assert.equal(currentLink('https://d.x', 'sales', ''), null)
  assert.equal(versionLink(null, 'sales', 1, 't'), null)
  assert.equal(draftLink('https://d.x', '/d/sales/v/4/', ''), null)
})

test('payloadDraftLink origin rule', () => {
  assert.equal(payloadDraftLink('/d/sales/v/4/?p=abc', 'https://d.x', false), 'https://d.x/d/sales/v/4/?p=abc')
  assert.equal(payloadDraftLink('/d/sales/v/4/?p=abc', null, false), null)
  const abs = 'http://localhost:9082/d/sales/v/4/?p=abc'
  assert.equal(payloadDraftLink(abs, null, false), null)
  assert.equal(payloadDraftLink(abs, 'https://guess.x', false), null)
  assert.equal(payloadDraftLink(abs, 'http://localhost:9082', false), abs)
  assert.equal(payloadDraftLink('https://evil.example/d/x/v/1/?p=a', 'https://d.x', false), null)
  assert.equal(payloadDraftLink(abs, 'https://d.x', true), 'https://d.x/d/sales/v/4/?p=abc')
})

test('payloadDraftLink rejects bad input', () => {
  for (const bad of [
    'javascript:alert(1)//d/x/v/1/?p=a',
    'ftp://h/d/x/v/1/?p=a',
    '/admin/?p=a',
    '/d/sales/v/4/',
    '/d/sales/v/x/?p=a',
    '//evil.example/d/x/v/1/?p=a',
    '',
    null,
  ]) {
    assert.equal(payloadDraftLink(bad, 'https://d.x', false), null, String(bad))
  }
  const r = payloadDraftLink('/d/s/v/1/?p=a%26b%23c', 'https://d.x', false)
  assert.equal(r, 'https://d.x/d/s/v/1/?p=a%26b%23c')
})

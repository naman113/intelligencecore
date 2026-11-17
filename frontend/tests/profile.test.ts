import { test } from 'node:test'
import assert from 'node:assert/strict'
import { PROFILE_STORAGE, normalizeProfile, readCachedProfile, resolveProfile } from '../src/lib/profile'

test('normalizeProfile', () => {
  assert.equal(normalizeProfile('second_brain'), 'second_brain')
  assert.equal(normalizeProfile(' Second_Brain '), 'second_brain')
  for (const v of ['bogus', '', undefined]) assert.equal(normalizeProfile(v), 'intelligence')
})

test('readCachedProfile', () => {
  localStorage.removeItem(PROFILE_STORAGE)
  assert.equal(readCachedProfile(), null)
  localStorage.setItem(PROFILE_STORAGE, 'Second_Brain')
  assert.equal(readCachedProfile(), 'second_brain')
  localStorage.removeItem(PROFILE_STORAGE)
})

const base = { health: null, healthFailed: false, cached: null, timedOut: false }

test('resolveProfile server wins over cache', () => {
  assert.deepEqual(
    resolveProfile({ ...base, health: { ok: true, ui_profile: 'second_brain' }, cached: 'intelligence' }),
    { profile: 'second_brain', resolved: true },
  )
})

test('resolveProfile cache while waiting', () => {
  assert.deepEqual(resolveProfile({ ...base, cached: 'second_brain' }), {
    profile: 'second_brain',
    resolved: true,
  })
})

test('resolveProfile unresolved with nothing', () => {
  assert.deepEqual(resolveProfile(base), { profile: 'intelligence', resolved: false })
})

test('resolveProfile fallbacks resolve to intelligence', () => {
  const want = { profile: 'intelligence', resolved: true }
  assert.deepEqual(resolveProfile({ ...base, healthFailed: true }), want)
  assert.deepEqual(resolveProfile({ ...base, timedOut: true }), want)
  assert.deepEqual(resolveProfile({ ...base, health: { ok: true } }), want)
})

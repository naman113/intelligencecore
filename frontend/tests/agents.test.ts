import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

test('Agents reply ceiling allows 32000 tokens', () => {
  const src = readFileSync('src/pages/Agents.tsx', 'utf8')
  assert.ok(src.includes('Tokens, 256–32000'))
  assert.ok(src.includes('max={32000}'))
  assert.ok(src.includes('min={256}'))
  assert.ok(!src.includes('8192'))
})

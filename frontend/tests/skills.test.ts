import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { loadSkillBody, skillDetailPath } from '../src/lib/skills'

test('skillDetailPath encodes the name', () => {
  assert.equal(skillDetailPath('context-pack-import'), '/api/skills/context-pack-import')
  assert.equal(skillDetailPath('a b/c'), '/api/skills/a%20b%2Fc')
})

test('loadSkillBody fetches the detail route once', async () => {
  const calls: string[] = []
  const fake = (async (p: string) => {
    calls.push(p)
    return { body: '# Hello' }
  }) as <T>(path: string) => Promise<T>
  assert.deepEqual(await loadSkillBody(fake, 'x'), { id: 'x', body: '# Hello' })
  assert.deepEqual(calls, ['/api/skills/x'])
})

test('loadSkillBody falls back for empty bodies', async () => {
  const empty = (async () => ({ body: '' })) as <T>(path: string) => Promise<T>
  const missing = (async () => ({})) as <T>(path: string) => Promise<T>
  assert.equal((await loadSkillBody(empty, 'x')).body, '(empty SKILL.md)')
  assert.equal((await loadSkillBody(missing, 'x')).body, '(empty SKILL.md)')
})

test('Skills page wires the View button and keeps a body-free list', () => {
  const src = readFileSync('src/pages/Skills.tsx', 'utf8')
  assert.ok(src.includes("get<SkillRow[]>('/api/skills')"))
  assert.ok(src.includes('loadSkillBody'))
  assert.ok(src.includes('View'))
})

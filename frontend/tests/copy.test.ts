import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

function walk(dir: string): string[] {
  const out: string[] = []
  for (const n of readdirSync(dir)) {
    const p = join(dir, n)
    if (statSync(p).isDirectory()) out.push(...walk(p))
    else out.push(p)
  }
  return out
}

const files = walk('src').filter((f) => /\.tsx?$/.test(f))
const all = files.map((f) => [f, readFileSync(f, 'utf8')] as const)
const text = (name: string) => all.find(([f]) => f.split('\\').join('/').endsWith(name))![1]

test('no approval promises the backend does not keep', () => {
  const banned = [
    'anything it creates waits for your approval',
    'define one and it will propose it for your approval',
    'It will propose a KPI for your approval',
  ].map((s) => s.toLowerCase())
  for (const [f, t] of all) {
    for (const b of banned) assert.ok(!t.toLowerCase().includes(b), `${f} contains "${b}"`)
  }
})

test('copy says what waits and that a KPI watch can start at once', () => {
  const ask = text('components/AskPanel.tsx')
  assert.ok(ask.includes('Descriptions, rules and dashboard drafts wait for your approval'))
  assert.ok(ask.includes('can start right away'))
  assert.ok(text('pages/Kpis.tsx').includes('it may start watching right away'))
  assert.ok(text('pages/Overview.tsx').includes('it may start watching right away'))
  assert.ok(text('pages/InsightsPage.tsx').includes('check KPIs for the result'))
  assert.ok(text('lib/ask.ts').includes('propose it for my approval'))
})

test('insight to KPI note points to Approvals (Phase 10 gap closure)', () => {
  const t = text('pages/InsightsPage.tsx')
  assert.ok(t.includes('Look for its KPI proposal on Approvals'))
  assert.ok(t.includes('once you approve it, check KPIs for the result'))
})

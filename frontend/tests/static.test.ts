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

const FORBIDDEN = ['dangerouslySetInnerHTML', 'srcdoc', 'srcDoc', '<iframe', '.innerHTML']

test('no raw-HTML rendering anywhere under src/', () => {
  for (const f of walk('src')) {
    if (!/\.(tsx?|jsx?|html)$/.test(f)) continue
    const text = readFileSync(f, 'utf8')
    for (const bad of FORBIDDEN) {
      assert.ok(!text.includes(bad), `${f} contains forbidden "${bad}"`)
    }
  }
})

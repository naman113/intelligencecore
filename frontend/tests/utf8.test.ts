import { test } from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

// Phase 11: a stray Latin-1 byte once shipped as U+FFFD on the health card.
const EXT = /\.(ts|tsx|css|html|json|svg)$/

function walk(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) out.push(...walk(p))
    else if (EXT.test(name)) out.push(p)
  }
  return out
}

test('every frontend source file decodes as strict UTF-8', () => {
  const files = [...walk('src'), ...(existsSync('index.html') ? ['index.html'] : [])]
  const dec = new TextDecoder('utf-8', { fatal: true })
  const bad: string[] = []
  for (const f of files) {
    try {
      dec.decode(readFileSync(f))
    } catch {
      bad.push(f.replaceAll(String.fromCharCode(92), '/'))
    }
  }
  assert.deepEqual(bad, [])
})

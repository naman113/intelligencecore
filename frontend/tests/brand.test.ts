import { test } from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { BRANDS, applyBrand } from '../src/lib/brand'

function fakeDoc(missing: string[] = []) {
  const els: Record<string, Record<string, string>> = {}
  const raw = {
    title: '',
    querySelector(sel: string) {
      if (missing.includes(sel)) return null
      els[sel] ??= {}
      return { setAttribute: (k: string, v: string) => void (els[sel][k] = v) }
    },
  }
  return { doc: raw as unknown as Document, els, raw }
}

test('applyBrand intelligence sets all head fields', () => {
  const { doc, els, raw } = fakeDoc()
  applyBrand('intelligence', doc)
  assert.equal(raw.title, 'Intelligence Corner')
  assert.equal(els['link[rel="icon"]'].href, BRANDS.intelligence.icon)
  assert.equal(els['link[rel="manifest"]'].href, '/manifest-intelligence.webmanifest')
  assert.equal(els['link[rel="apple-touch-icon"]'].href, '/apple-touch-icon-ic.png')
  assert.equal(els['meta[name="apple-mobile-web-app-title"]'].content, 'Intelligence Corner')
})

test('applyBrand second_brain restores, and skips missing elements', () => {
  const { doc, els, raw } = fakeDoc(['link[rel="manifest"]'])
  applyBrand('second_brain', doc)
  assert.equal(raw.title, 'Second Brain')
  assert.equal(els['link[rel="icon"]'].href, BRANDS.second_brain.icon)
  assert.equal(els['link[rel="apple-touch-icon"]'].href, '/apple-touch-icon.png')
  assert.equal(BRANDS.second_brain.manifest, '/manifest.webmanifest')
})

test('second brain icon equals index.html favicon byte for byte', () => {
  const html = readFileSync('index.html', 'utf8')
  const m = html.match(/<link rel="icon" href="([^"]+)"/)
  assert.ok(m)
  assert.equal(BRANDS.second_brain.icon, m[1])
})

test('intelligence icon has no gold and differs', () => {
  const i = BRANDS.intelligence.icon
  assert.ok(i.startsWith('data:image/svg+xml,'))
  assert.ok(!/D4952B/i.test(i))
  assert.notEqual(i, BRANDS.second_brain.icon)
})

test('copy and wordmarks', () => {
  const signin = readFileSync('src/pages/SignIn.tsx', 'utf8').replace(/\s+/g, ' ')
  assert.ok(signin.includes('brand.signInLede'))
  assert.ok(BRANDS.second_brain.signInLede.startsWith('This instance is unlocked with its ops key'))
  assert.deepEqual(BRANDS.intelligence.wordmark, ['INTELLIGENCE', 'CORNER'])
  assert.deepEqual(BRANDS.second_brain.wordmark, ['SECOND BRAIN'])
  // The byline is plain configurable text; empty by default for both profiles.
  assert.equal(BRANDS.intelligence.byline, '')
  assert.equal(BRANDS.second_brain.byline, '')
  const shell = readFileSync('src/components/AppShell.tsx', 'utf8')
  assert.ok(shell.includes('brand.byline &&'))
})

function pngSize(file: string): [number, number] {
  const b = readFileSync(file)
  return [b.readUInt32BE(16), b.readUInt32BE(20)]
}

test('intelligence manifest and generated icons', () => {
  const m = JSON.parse(readFileSync('public/manifest-intelligence.webmanifest', 'utf8'))
  assert.equal(m.name, 'Intelligence Corner')
  assert.equal(m.short_name, 'Intelligence')
  assert.equal(m.start_url, '/')
  assert.equal(m.display, 'standalone')
  assert.equal(m.icons.length, 3)
  const want = [
    ['/icon-ic-192.png', 192, 'any'],
    ['/icon-ic-512.png', 512, 'any'],
    ['/icon-ic-maskable-512.png', 512, 'maskable'],
  ] as const
  want.forEach(([src, n, purpose], i) => {
    assert.equal(m.icons[i].src, src)
    assert.equal(m.icons[i].sizes, `${n}x${n}`)
    assert.equal(m.icons[i].purpose, purpose)
    assert.deepEqual(pngSize('public' + src), [n, n])
  })
  assert.deepEqual(pngSize('public/apple-touch-icon-ic.png'), [180, 180])
  assert.ok(existsSync('public' + BRANDS.intelligence.manifest))
  assert.ok(existsSync('public' + BRANDS.intelligence.appleIcon))
})

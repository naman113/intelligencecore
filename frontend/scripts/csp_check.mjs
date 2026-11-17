// Phase 12 (WR-12): walk every UI route in headless Chrome (CDP, no dependencies) and count CSP violations.
// Usage: node scripts/csp_check.mjs [--list] [--secrets <path>]
// OPS_KEY comes from the environment, else from the OPS_KEY= line of the --secrets file. It is never printed
// and never typed into a page: it is seeded through localStorage. The UI profile is never changed.
// UI_URL defaults to http://127.0.0.1:9120 and must be loopback.
import { spawn } from 'node:child_process'
import { existsSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const args = process.argv.slice(2)
const LIST = args.includes('--list')
const si = args.indexOf('--secrets')
const SECRETS = si >= 0 ? args[si + 1] || '' : ''

const ROUTE_RE = /\bto:\s*'(\/[^']*)'/g
function routesFrom(file) {
  const src = readFileSync(join(HERE, file), 'utf8')
  return [...src.matchAll(ROUTE_RE)].map((m) => m[1])
}
function staticRoutes() {
  const nav = routesFrom('../src/lib/nav.ts')
  const shell = routesFrom('../src/components/AppShell.tsx')
  return [...new Set(['/', ...nav, ...shell, '/context/schema?source=business_db', '/setup/interview'])]
}

if (LIST) {
  for (const r of staticRoutes()) console.log(r)
  process.exit(0)
}

const UI = (process.env.UI_URL || 'http://127.0.0.1:9120').replace(/\/$/, '')
const host = new URL(UI).hostname
if (!['localhost', '127.0.0.1', '[::1]'].includes(host)) {
  console.error('FAIL setup: UI_URL must be loopback')
  process.exit(2)
}
const KEY_STORAGE = 'secondbrain.opskey' // KEY_STORAGE in src/lib/api.ts
const EXPECTED_CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; manifest-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"

function readKey() {
  if (process.env.OPS_KEY) return process.env.OPS_KEY
  if (!SECRETS) return ''
  try {
    const m = readFileSync(SECRETS, 'utf8').match(/^OPS_KEY=(.*)$/m)
    if (m) return m[1].replace(/\r$/, '').trim()
  } catch { /* fall through */ }
  return ''
}
const KEY = readKey()
if (!KEY) {
  console.error('FAIL setup: set OPS_KEY or pass --secrets <path>')
  process.exit(2)
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function api(path) {
  try {
    const r = await fetch(UI + path, { headers: { 'x-ops-key': KEY } })
    return r.ok ? await r.json() : null
  } catch { return null }
}
function firstRow(json) {
  if (Array.isArray(json)) return json[0]
  if (json && typeof json === 'object') for (const v of Object.values(json)) if (Array.isArray(v)) return v[0]
  return undefined
}

const routes = staticRoutes()
const skips = []
async function detail(label, path, pick, build) {
  const row = firstRow(await api(path))
  const v = row && pick(row)
  if (v === undefined || v === null || v === '') skips.push(`skip ${label} no rows`)
  else routes.push(build(encodeURIComponent(String(v))))
}
await detail('/system/runs/:id', '/api/runs?limit=1', (r) => r.id, (v) => `/system/runs/${v}`)
await detail('/dashboards/:slug', '/api/dashboards', (r) => r.slug, (v) => `/dashboards/${v}`)
await detail('/kpis/:id', '/api/watches', (r) => r.id, (v) => `/kpis/${v}`)
await detail('/documents/:id', '/api/documents?limit=1', (r) => r.id, (v) => `/documents/${v}`)
await detail('/cases/:id', '/api/cases?limit=1', (r) => r.id, (v) => `/cases/${v}`)
for (const s of skips) console.log(s)

const CHROME = [
  process.env.CHROME,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
].filter(Boolean).find((p) => existsSync(p))
if (!CHROME) {
  console.error('FAIL setup: no Chrome or Edge found (set CHROME)')
  process.exit(2)
}
const PORT = 9344
const profileDir = join(tmpdir(), 'ic-csp-check-' + process.pid)
let chrome = null
let ws = null
let nextId = 1
const pending = new Map()
function send(method, params = {}) {
  const id = nextId++
  ws.send(JSON.stringify({ id, method, params }))
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject })
    setTimeout(() => { if (pending.delete(id)) reject(new Error('timeout ' + method)) }, 30000)
  })
}
async function ev(expr) {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true })
  if (r.exceptionDetails) throw new Error('eval failed')
  return r.result.value
}

const consoleErrors = []
let bad = 0
try {
  rmSync(profileDir, { recursive: true, force: true })
  chrome = spawn(CHROME, [
    '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${profileDir}`,
    '--no-first-run', '--disable-gpu', '--no-default-browser-check', 'about:blank',
  ], { stdio: 'ignore' })
  let version = null
  for (let i = 0; i < 60 && !version; i++) {
    try { version = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json() } catch { await sleep(500) }
  }
  if (!version) throw new Error('Chrome did not start')
  const t = await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: 'PUT' })).json()
  ws = new WebSocket(t.webSocketDebuggerUrl)
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('ws error')) })
  ws.onmessage = (m) => {
    const d = JSON.parse(m.data)
    if (d.id && pending.has(d.id)) {
      const p = pending.get(d.id); pending.delete(d.id)
      d.error ? p.reject(new Error(d.error.message)) : p.resolve(d.result)
    } else if (d.method === 'Runtime.consoleAPICalled' && d.params.type === 'error') {
      consoleErrors.push(d.params.args.map((a) => a.value ?? a.description ?? '').join(' ').slice(0, 200))
    } else if (d.method === 'Log.entryAdded' && d.params.entry.level === 'error') {
      consoleErrors.push(String(d.params.entry.text).slice(0, 200))
    }
  }
  await send('Page.enable')
  await send('Runtime.enable')
  await send('Log.enable')
  await send('Page.addScriptToEvaluateOnNewDocument', {
    source: "window.__csp = []; document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI), true)",
  })
  // Seed the key on the UI origin before the app boots.
  await send('Page.navigate', { url: UI + '/manifest.webmanifest' })
  await sleep(800)
  await ev(`localStorage.setItem(${JSON.stringify(KEY_STORAGE)}, ${JSON.stringify(KEY)})`)

  const head = await fetch(UI + '/')
  const csp = head.headers.get('content-security-policy')
  if (csp === EXPECTED_CSP) console.log('PASS header Content-Security-Policy on /')
  else { bad++; console.log('FAIL header Content-Security-Policy on / does not match the nginx policy') }

  for (const route of routes) {
    consoleErrors.length = 0
    await send('Page.navigate', { url: UI + route })
    await sleep(2000)
    let viol = []
    let rendered = false
    try {
      viol = await ev('window.__csp || []')
      rendered = await ev("!!document.getElementById('root') && document.getElementById('root').children.length > 0")
    } catch (e) {
      viol = ['eval: ' + e.message]
    }
    const ok = viol.length === 0 && rendered
    if (!ok) bad++
    console.log(`${ok ? 'PASS' : 'FAIL'} ${route} violations=${viol.length} rendered=${rendered} consoleErrors=${consoleErrors.length}`)
    for (const v of viol) console.log('  violation: ' + v)
  }
} catch (e) {
  bad++
  console.log('FAIL walk: ' + e.message)
} finally {
  try { ws?.close() } catch { /* ignore */ }
  chrome?.kill()
  await sleep(300)
  try { rmSync(profileDir, { recursive: true, force: true }) } catch { /* ignore */ }
}
console.log(bad === 0 ? 'OK: zero CSP violations' : `FAILED: ${bad} problem(s)`)
process.exit(bad === 0 ? 0 : 1)

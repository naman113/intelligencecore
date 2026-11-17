// Phase 10 live wiring check over CDP, zero dependencies (Node 24).
// Usage: node scripts/live_wiring_check.mjs <ui_base> <api_base> --marker <text> --slug <slug> --action <id>
//          --operator <label> [--dash <dash_base>] [--secrets <path>]
// Output: PASS <m> / FAIL <m>: <why> / INFO <m>. Exit 0 all pass, 1 any FAIL, 2 usage, 3 no browser.
// The ops key comes from env OPS_KEY or the --secrets file. It is set through localStorage only and is
// never printed or typed into a page. Every printed URL has ?t= and ?p= values redacted.
//
// Rules that keep this valid while later phases change what is owed:
// - Approve ONLY on the Approvals page (one click). Never approve from the Overview card or the owed
//   column: Phase 12's WR-15 may replace that button with "Review".
// - Assert presence and deltas, never absolute counts or exact button sets.
import { spawn } from 'node:child_process'
import { existsSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const rawArgs = process.argv.slice(2)
const opt = {}
const pos = []
for (let i = 0; i < rawArgs.length; i++) {
  if (rawArgs[i].startsWith('--')) opt[rawArgs[i].slice(2)] = rawArgs[++i] || ''
  else pos.push(rawArgs[i])
}
const [UI_ARG, API_ARG] = pos
if (!UI_ARG || !API_ARG || !opt.marker || !opt.slug || !opt.action || !opt.operator) {
  console.error('usage: node scripts/live_wiring_check.mjs <ui_base> <api_base> --marker <text> --slug <slug> --action <id> --operator <label> [--dash <dash_base>] [--secrets <path>]')
  process.exit(2)
}
const UI = UI_ARG.replace(/\/$/, '')
const API = API_ARG.replace(/\/$/, '')
const MARKER = opt.marker
const SLUG = opt.slug
const ACTION = Number(opt.action)
const OPERATOR = opt.operator
const DASH = (opt.dash || UI.replace(/:(\d+)$/, (_m, p) => `:${Number(p) + 2}`)).replace(/\/$/, '')
const PORT = 9334

function readKey() {
  if (process.env.OPS_KEY) return process.env.OPS_KEY
  if (!opt.secrets) return ''
  try {
    const m = readFileSync(opt.secrets, 'utf8').match(/^OPS_KEY=(.*)$/m)
    if (m) return m[1].replace(/\r$/, '').trim()
  } catch { /* fall through */ }
  return ''
}
const KEY = readKey()
if (!KEY) {
  console.error('FAIL setup: set OPS_KEY or pass --secrets <path>')
  process.exit(2)
}

const redact = (s) => String(s ?? '').replace(/([?&][tp]=)[^&\s"']+/g, '$1<redacted>')
let failed = 0
const pass = (m) => console.log('PASS ' + m)
const fail = (m, why = '') => { failed++; console.log('FAIL ' + m + (why ? ': ' + redact(why) : '')) }
const info = (m) => console.log('INFO ' + redact(m))
const check = (ok, m, why) => (ok ? pass(m) : fail(m, typeof why === 'function' ? why() : why))
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function api(method, path, body) {
  const r = await fetch(API + path, {
    method,
    headers: { 'x-ops-key': KEY, 'content-type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  })
  const text = await r.text()
  let json = null
  try { json = JSON.parse(text) } catch { /* not json */ }
  return { status: r.status, json }
}
const payloadOf = (a) => (typeof a.payload === 'string' ? JSON.parse(a.payload) : a.payload) || {}

const chromeCandidates = [
  process.env.CHROME,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
].filter(Boolean)
const CHROME = chromeCandidates.find((p) => existsSync(p))
if (!CHROME) {
  console.error('FAIL setup: no Chrome or Edge found (set CHROME)')
  process.exit(3)
}

const profileDir = join(tmpdir(), 'ic-p10-wiring-' + process.pid)
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
  if (r.exceptionDetails) throw new Error('eval: ' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text))
  return r.result.value
}
async function waitFor(expr, label, ms = 15000) {
  const end = Date.now() + ms
  let last
  while (Date.now() < end) {
    try {
      last = await ev(expr)
      if (last) return last
    } catch (e) { last = e.message }
    await sleep(250)
  }
  throw new Error('timed out waiting for ' + label + (typeof last === 'string' ? ' (' + last + ')' : ''))
}
async function goto(url) {
  await send('Page.navigate', { url })
  await sleep(300)
  await waitFor("document.readyState === 'complete'", 'load')
}
async function click(js, label) {
  const ok = await ev(`(() => { const el = (${js}); if (!el) return false; el.scrollIntoView({block:'center'}); el.click(); return true })()`)
  if (!ok) throw new Error('nothing to click: ' + label)
  await sleep(300)
}

async function startChrome() {
  rmSync(profileDir, { recursive: true, force: true })
  chrome = spawn(CHROME, [
    '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${profileDir}`,
    '--no-first-run', '--disable-gpu', '--no-default-browser-check',
    '--window-size=1280,900', 'about:blank',
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
    }
  }
  await send('Page.enable')
  await send('Runtime.enable')
  await send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false })
  // Any in-page confirm/alert is accepted, as phone_walkthrough does.
  ws.addEventListener('message', (m) => {
    const d = JSON.parse(m.data)
    if (d.method === 'Page.javascriptDialogOpening') send('Page.handleJavaScriptDialog', { accept: true }).catch(() => {})
  })
}

const railBadge = (href) =>
  `(() => { const s = document.querySelector('nav.rail a[href="${href}"] span.nav-owed'); return s ? (parseInt(s.textContent, 10) || 0) : 0 })()`

// The approvals card for ACTION: the panel containing "#<id>" in its mono id span.
const CARD = `Array.from(document.querySelectorAll('section.panel')).find(s => Array.from(s.querySelectorAll('span.mono')).some(m => m.textContent.trim() === '#${ACTION}'))`

async function main() {
  const act = (await api('GET', '/api/actions?status=proposed&kind=publish_dashboard&limit=200')).json || []
  const row = act.find((a) => a.id === ACTION)
  if (!row) throw new Error(`action ${ACTION} is not a proposed publish_dashboard`)
  const P = payloadOf(row)
  const addedIds = P.queries_added || []
  const removedIds = P.queries_removed || []
  const M = Math.max(0, ...act.map((a) => a.id))

  await startChrome()

  // ---- 1. sign in by localStorage, desktop viewport
  await goto(UI + '/')
  await ev(`localStorage.setItem('secondbrain.opskey', ${JSON.stringify(KEY)}); localStorage.setItem('secondbrain.operator', ${JSON.stringify(OPERATOR)}); localStorage.removeItem('secondbrain.ui_profile'); true`)
  await goto(UI + '/')
  await waitFor("!!document.querySelector('.ov')", 'overview root')

  // ---- 2. Overview insight
  const insightRow = `Array.from(document.querySelectorAll('.ic-row')).find(r => r.textContent.includes(${JSON.stringify(MARKER)}))`
  try {
    await waitFor(`!!(${insightRow})`, 'insight on Overview', 20000)
    pass('Overview shows the newest insight')
  } catch (e) { fail('Overview shows the newest insight', e.message) }

  // ---- 3. insight to KPI
  try {
    await click(BTN(insightRow, 'Make this a KPI'), 'Make this a KPI')
    let msg = null
    let runId = null
    for (let i = 0; i < 30 && !msg; i++) {
      const r = await api('GET', `/api/ask?operator=${encodeURIComponent(OPERATOR)}&limit=10`)
      const ms = r.json?.messages || []
      msg = ms.find((m) => m.role === 'user' && m.content.includes('into a KPI') && /(note|watch_event):\d+/.test(m.content))
      runId = r.json?.last_run?.id ?? null
      if (!msg) await sleep(500)
    }
    check(!!msg, `insight to KPI posted through /api/ask on ui:${OPERATOR}`, 'no user message naming the insight')
    if (runId != null) info('ask run id ' + runId)
  } catch (e) { fail('insight to KPI posted through /api/ask', e.message) }

  // ---- 4. owed column (scoped to .owed-list; the Overview dashboards list shows v1 under the same title)
  const title = P.title || SLUG
  const owedEntry = `Array.from(document.querySelectorAll('.owed-list [data-owed="draft"]')).find(c => ((c.querySelector('.owed-title')||{}).textContent || '').includes(${JSON.stringify(title)}))`
  let owedFound = false
  try {
    await goto(UI + '/')
    await waitFor(`!!(${owedEntry})`, 'owed draft card', 25000)
    owedFound = true
    pass('owed column shows the real draft')
  } catch (e) { fail('owed column shows the real draft', e.message) }
  if (owedFound) {
    const href = await ev(`(() => { const c = ${owedEntry}; const a = c && Array.from(c.querySelectorAll('a')).find(x => x.textContent.trim() === 'Open draft'); return a ? a.getAttribute('href') : null })()`)
    const prefix = `${DASH}/d/${SLUG}/v/`
    check(!!href && href.startsWith(prefix) && href.includes('?p='), 'owed draft link on the dashboards origin', () => `href=${href} want prefix ${prefix}`)
    if (href) {
      try {
        const r = await fetch(href)
        const csp = r.headers.get('content-security-policy')
        check(r.status === 200 && !!csp, 'draft link opens with CSP', `status ${r.status} csp ${csp ? 'present' : 'missing'} for ${redact(href)}`)
      } catch (e) { fail('draft link opens with CSP', e.message) }
    }
  }

  // ---- 5. rail badge (Dashboards counts only drafts and broken dashboards)
  const D0 = await ev(railBadge('/dashboards'))
  check(D0 >= 1, 'rail badge counts the owed draft', `Dashboards badge ${D0}`)
  info('Overview total badge ' + (await ev(railBadge('/'))))

  // ---- 6. Approvals rendering
  await goto(UI + '/approvals')
  let cardOk = false
  try {
    await waitFor(`!!(${CARD})`, 'approvals card', 20000)
    cardOk = true
  } catch (e) { fail('Approvals shows the draft card', e.message) }
  if (cardOk) {
    const txt = await ev(`${CARD}.textContent`)
    check(txt.includes(P.title), 'Approvals card shows the title', P.title)
    check(txt.includes(P.change_note), 'Approvals card shows the change note', P.change_note)
    check(removedIds.length > 0, 'payload has a removed query (check is not vacuous)', JSON.stringify(removedIds))
    check(addedIds.length > 0, 'payload has an added query', JSON.stringify(addedIds))
    const pills = await ev(`Array.from((${CARD}).querySelectorAll('span.pill.mono')).map(p => p.textContent.trim())`)
    for (const id of addedIds) check(pills.includes('+' + id), `Approvals chip +${id}`, JSON.stringify(pills))
    for (const id of removedIds) {
      check(pills.includes('\u2212' + id) || pills.includes('-' + id), `Approvals chip \u2212${id}`, JSON.stringify(pills))
    }
    const link = await ev(`(() => { const a = Array.from((${CARD}).querySelectorAll('a')).find(x => x.textContent.trim() === 'Open draft'); return a ? a.getAttribute('href') : null })()`)
    check(!!link && link.startsWith(`${DASH}/d/${SLUG}/v/`), 'Approvals Open draft link on the dashboards origin', () => String(link))
    check(!txt.includes('Set the dashboards address on Setup'), 'Approvals card has no "Set the dashboards address" fallback')

    // ---- 7. approve (the one approve click, on /approvals only)
    try {
      await click(BTN(CARD, 'Approve'), 'Approve')
      let done = false
      for (let i = 0; i < 40 && !done; i++) {
        const r = await api('GET', '/api/actions?status=executed&limit=50')
        done = Array.isArray(r.json) && r.json.some((a) => a.id === ACTION)
        if (!done) await sleep(500)
      }
      const d = (await api('GET', `/api/dashboards/${SLUG}`)).json
      const cur = d?.dashboard?.current_version ?? d?.current_version
      check(done && cur === P.version, 'approve from Approvals runs the real publish executor', `executed=${done} current_version=${cur} want ${P.version}`)
    } catch (e) { fail('approve from Approvals runs the real publish executor', e.message) }

    // ---- 7b. badge drops
    const end = Date.now() + 25000
    let D1 = D0
    let E = 0
    while (Date.now() < end) {
      D1 = await ev(railBadge('/dashboards')).catch(() => D0)
      const now = (await api('GET', '/api/actions?status=proposed&kind=publish_dashboard&limit=200')).json || []
      E = now.filter((a) => a.id > M).length
      if (D1 <= D0 - 1 + E) break
      await sleep(1500)
    }
    check(D1 <= D0 - 1 + E, 'rail badge drops after the draft is approved', `before ${D0} after ${D1} new drafts ${E}`)
  }

  // ---- 8. Insights page
  try {
    await goto(UI + '/insights')
    await waitFor(`document.body.textContent.includes(${JSON.stringify(MARKER)})`, 'insight on /insights', 20000)
    pass('Insights page lists the insight')
  } catch (e) { fail('Insights page lists the insight', e.message) }
}

const BTN = (scope, text) =>
  `Array.from((${scope}).querySelectorAll('button, a')).find(b => b.textContent.trim() === ${JSON.stringify(text)})`

try {
  await main()
} catch (e) {
  fail('wiring check aborted', e.message)
} finally {
  try { ws?.close() } catch { /* ignore */ }
  try { chrome?.kill() } catch { /* ignore */ }
  await sleep(800)
  try { rmSync(profileDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 }) } catch { /* ignore */ }
}
console.log(`wiring: ${failed} failed`)
process.exit(failed > 0 ? 1 : 0)

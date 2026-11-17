// Phone walkthrough for Phase 8, driven over the Chrome DevTools Protocol with no dependencies.
// Usage: node scripts/phone_walkthrough.mjs <ui_base> <api_base> [outdir] [--secrets <path>]
// OPS_KEY comes from the environment, else from the OPS_KEY= line of the --secrets file. It is never printed
// and never typed into a page: it is set through localStorage.
import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const rawArgs = process.argv.slice(2)
let SECRETS = ''
const posArgs = []
for (let i = 0; i < rawArgs.length; i++) {
  if (rawArgs[i] === '--secrets') SECRETS = rawArgs[++i] || ''
  else posArgs.push(rawArgs[i])
}
const [UI_ARG, API_ARG, OUT_ARG] = posArgs
if (!UI_ARG || !API_ARG) {
  console.error('usage: node scripts/phone_walkthrough.mjs <ui_base> <api_base> [outdir] [--secrets <path>]')
  process.exit(2)
}
const UI = UI_ARG.replace(/\/$/, '')
const API = API_ARG.replace(/\/$/, '')
const OUT = OUT_ARG || join(tmpdir(), 'ic-p8-shots')
const PORT = 9333

function readKey() {
  if (process.env.OPS_KEY) return process.env.OPS_KEY
  if (!SECRETS) return ''
  try {
    const m = readFileSync(SECRETS, 'utf8').match(/^OPS_KEY=(.*)$/m)
    if (m) return m[1].replace(/\r$/, '').trim()
  } catch {
    /* fall through */
  }
  return ''
}
const KEY = readKey()
if (!KEY) {
  console.error('FAIL setup: set OPS_KEY or pass --secrets <path>')
  process.exit(2)
}

let passed = 0
let failed = 0
const pass = (m) => { passed++; console.log('PASS ' + m) }
const fail = (m, why = '') => { failed++; console.log('FAIL ' + m + (why ? ': ' + why : '')) }
const info = (m) => console.log('INFO ' + m)
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
let startProfile = null
async function setProfile(p) { await api('PUT', '/api/config', { values: { ui_profile: p } }) }

const chromeCandidates = [
  process.env.CHROME,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
].filter(Boolean)
const CHROME = chromeCandidates.find((p) => existsSync(p))
if (!CHROME) {
  console.error('FAIL setup: no Chrome or Edge found (set CHROME)')
  process.exit(2)
}

const profileDir = join(tmpdir(), 'ic-p8-walk-' + process.pid)
let chrome = null
let ws = null
let nextId = 1
const pending = new Map()
const waiters = []

function send(method, params = {}) {
  const id = nextId++
  ws.send(JSON.stringify({ id, method, params }))
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject })
    setTimeout(() => {
      if (pending.delete(id)) reject(new Error('timeout ' + method))
    }, 30000)
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
    } catch (e) {
      last = e.message
    }
    await sleep(250)
  }
  throw new Error('timed out waiting for ' + label + (typeof last === 'string' ? ' (' + last + ')' : ''))
}

const paths = new Set()
async function notePath() {
  try {
    const p = await ev('(window.__paths || []).concat([location.pathname])')
    for (const x of p) paths.add(x)
  } catch { /* page mid-navigation */ }
}
async function goto(url) {
  await notePath()
  await send('Page.navigate', { url })
  await waitFor("document.readyState === 'complete'", 'load')
}
async function click(js, label) {
  const ok = await ev(`(() => { const el = (${js}); if (!el) return false; el.scrollIntoView({block:'center'}); el.click(); return true })()`)
  if (!ok) throw new Error('nothing to click: ' + label)
  await sleep(300)
  await notePath()
}
const BTN_BY_TEXT = (scope, text, exact = true) =>
  `Array.from(${scope}.querySelectorAll('button, a')).find(b => ${exact ? `b.textContent.trim() === ${JSON.stringify(text)}` : `b.textContent.trim().startsWith(${JSON.stringify(text)})`})`

async function shot(name) {
  mkdirSync(OUT, { recursive: true })
  const r = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(join(OUT, name), Buffer.from(r.data, 'base64'))
}
async function phone(w, h) {
  await send('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 2, mobile: true })
  await send('Emulation.setTouchEmulationEnabled', { enabled: true })
}

const RECORDER = `(() => {
  window.__seen = { titles: [], wordmarks: [], icons: [] };
  window.__paths = [location.pathname];
  const snap = () => {
    const s = window.__seen;
    const t = document.title; if (s.titles[s.titles.length - 1] !== t) s.titles.push(t);
    const w = Array.from(document.querySelectorAll('.wordmark')).map(e => e.textContent).join('|');
    if (s.wordmarks[s.wordmarks.length - 1] !== w) s.wordmarks.push(w);
    const l = document.querySelector('link[rel="icon"]'); const i = l ? l.getAttribute('href') : '';
    if (s.icons[s.icons.length - 1] !== i) s.icons.push(i);
  };
  for (const k of ['pushState', 'replaceState']) {
    const o = history[k];
    history[k] = function () { const r = o.apply(this, arguments); window.__paths.push(location.pathname); return r };
  }
  window.addEventListener('popstate', () => window.__paths.push(location.pathname));
  const start = () => { snap(); new MutationObserver(snap).observe(document, { childList: true, subtree: true, characterData: true, attributes: true }) };
  if (document.documentElement) start(); else document.addEventListener('DOMContentLoaded', start);
})()`

const SB_ICON_MARK = 'D4952B'

async function layoutChecks(width) {
  const r = await ev(`(() => {
    const out = {};
    out.sw = document.documentElement.scrollWidth; out.iw = innerWidth;
    const tiles = document.querySelector('.ov-tiles');
    out.cols = tiles ? getComputedStyle(tiles).gridTemplateColumns.trim().split(/\\s+/).length : 0;
    const owed = document.querySelector('.ov-owed'), main = document.querySelector('.ov-main');
    out.owedFirst = !!(owed && main && owed.getBoundingClientRect().top < main.getBoundingClientRect().top);
    out.cards = Array.from(document.querySelectorAll('[data-owed]')).map(c => {
      const btns = Array.from(c.querySelectorAll('button, a.btn'));
      let good = false;
      for (const b of btns) { b.scrollIntoView({block:'center'}); const q = b.getBoundingClientRect();
        if (q.left >= 0 && q.right <= innerWidth && q.height >= 32 && q.width > 0) good = true }
      return { kind: c.getAttribute('data-owed'), good };
    });
    out.poly = !!document.querySelector('.ov-tile svg polyline');
    return out;
  })()`)
  check(r.sw <= r.iw + 1, `overview no horizontal overflow at ${width}`, `scrollWidth ${r.sw} > ${r.iw}`)
  check(r.cols === 1, `tiles in one column at ${width}`, `columns ${r.cols}`)
  check(r.owedFirst, `owed column comes first at ${width}`)
  check(r.cards.length > 0 && r.cards.every((c) => c.good), `every owed card has a visible tappable button at ${width} (${r.cards.length} cards)`,
    () => JSON.stringify(r.cards.filter((c) => !c.good)))
  return r
}

async function startChrome() {
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
    } else if (d.method) {
      for (const w of waiters.splice(0)) w(d)
    }
  }
  await send('Page.enable')
  await send('Runtime.enable')
  await send('Page.addScriptToEvaluateOnNewDocument', { source: RECORDER })
}

async function main() {
  try {
    const hr = await fetch(API + '/health')
    const hj = await hr.json()
    if (hj.ui_profile === 'intelligence' || hj.ui_profile === 'second_brain') startProfile = hj.ui_profile
  } catch { /* unknown */ }
  info(startProfile ? 'starting ui_profile ' + startProfile : 'starting ui_profile unknown (not restored)')
  await setProfile('intelligence')
  await startChrome()

  // ---- 1. load at 390
  await phone(390, 844)
  await goto(UI + '/')
  await ev(`localStorage.setItem('secondbrain.opskey', ${JSON.stringify(KEY)}); localStorage.removeItem('secondbrain.ui_profile'); true`)
  await goto(UI + '/')
  await waitFor("!!document.querySelector('.ov')", 'overview root')
  pass('1 overview loads at 390x844')

  // ---- 2. brand
  await waitFor("document.title === 'Intelligence Corner' && !!document.querySelector('.topbar .wordmark') && document.querySelector('.topbar .wordmark').textContent.trim().length > 0", 'brand applied').catch(() => {})
  const brand = await ev(`({
    title: document.title,
    manifest: (document.querySelector('link[rel=manifest]')||{}).href || '',
    icon: (document.querySelector('link[rel=icon]')||{}).href || '',
    wordmark: (document.querySelector('.topbar .wordmark')||{}).textContent || ''
  })`)
  check(brand.title === 'Intelligence Corner', '2 browser title is Intelligence Corner', brand.title)
  check(brand.manifest.endsWith('manifest-intelligence.webmanifest'), '2 manifest link is the Intelligence Corner manifest', brand.manifest)
  check(brand.icon !== '' && !brand.icon.toUpperCase().includes(SB_ICON_MARK) && !decodeURIComponent(brand.icon).toUpperCase().includes(SB_ICON_MARK),
    '2 favicon is the Intelligence Corner icon with no gold')
  check(brand.wordmark.trim().toUpperCase() === 'INTELLIGENCE CORNER', '2 topbar wordmark reads INTELLIGENCE CORNER', brand.wordmark)
  await click(BTN_BY_TEXT("document.querySelector('.topbar')", 'Ask'), 'Ask button')
  const you = await waitFor("(document.getElementById('ask-operator')||{}).value", 'You are field')
  check(you === 'owner', '2 Ask panel opens with You are = owner', you)
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
  await waitFor("!document.querySelector('.ask-sheet')", 'Ask panel closed')
  pass('2 Escape closes the Ask panel')

  // ---- 3. layout
  await waitFor("document.querySelectorAll('[data-owed]').length > 0", 'owed cards')
  await sleep(800)
  const lay = await layoutChecks(390)
  check(lay.poly, '3 a tile draws a sparkline polyline (seeded watch)')
  await ev('window.scrollTo(0, 0)')
  await shot('overview-390.png')

  // ---- 4. schema approval
  await click(`document.querySelector('[data-owed="context"] a, [data-owed="context"] button') && ${BTN_BY_TEXT("document.querySelector('[data-owed=\"context\"]')", 'Review on Schema')}`, 'Review on Schema')
  await waitFor("location.pathname === '/context/schema'", 'schema route')
  pass('4 Review on Schema opens /context/schema')
  await waitFor("!!document.querySelector('[data-action-id]')", 'gold schema row')
  const aid = Number(await ev("document.querySelector('[data-action-id]').getAttribute('data-action-id')"))
  await shot('schema-390.png')
  await click(BTN_BY_TEXT(`document.querySelector('[data-action-id="${aid}"]')`, 'Approve'), 'schema Approve')
  let ex = false
  for (let i = 0; i < 40 && !ex; i++) {
    const r = await api('GET', '/api/actions?status=executed&limit=300')
    ex = Array.isArray(r.json) && r.json.some((a) => a.id === aid)
    if (!ex) await sleep(500)
  }
  check(ex, `4 schema description approved at phone width (action ${aid} executed)`)

  // ---- 5. draft approval
  const pend = await api('GET', '/api/actions?status=proposed&limit=300')
  const seeded = (pend.json || []).find((a) => {
    const p = typeof a.payload === 'string' ? JSON.parse(a.payload) : a.payload
    return a.kind === 'publish_dashboard' && String(p?.draft_url || '').includes('verify-p8-seed')
  })
  if (!seeded) {
    fail('5 seeded publish_dashboard is proposed', 'run: KEEP=1 bash 08-live-check.sh p8')
  } else {
    const sp = typeof seeded.payload === 'string' ? JSON.parse(seeded.payload) : seeded.payload
    await click("document.querySelector('button[aria-label=\"Open menu\"]')", 'Open menu')
    await click(BTN_BY_TEXT("document.querySelector('nav.rail')", 'Approvals', false), 'Approvals nav')
    await waitFor("location.pathname === '/approvals'", 'approvals route')
    await waitFor(`Array.from(document.querySelectorAll('section.panel')).some(s => s.textContent.includes('verify-p8 draft'))`, 'seeded card')
    const card = `Array.from(document.querySelectorAll('section.panel')).find(s => s.textContent.includes('verify-p8 draft'))`
    const txt = await ev(`${card}.textContent`)
    check(txt.includes(sp.title) && txt.includes('verify-p8 draft') && txt.includes('orders_by_day'), '5 draft card shows title, change note and added query')
    const link = await ev(`(() => { const a = Array.from((${card}).querySelectorAll('a')).find(x => x.textContent.trim() === 'Open draft'); return a ? { href: a.getAttribute('href'), rel: a.getAttribute('rel') } : null })()`)
    if (link) {
      check(link.href.includes(`/d/${sp.slug}/v/${sp.version}/?p=verify-p8-seed`), '5 Open draft anchor carries the payload draft_url', link.href)
      check(/noopener/.test(link.rel || '') && /noreferrer/.test(link.rel || ''), '5 Open draft anchor has rel noopener noreferrer', link.rel)
    } else {
      const btn = await ev(`!!Array.from((${card}).querySelectorAll('button')).find(x => x.textContent.trim() === 'Open draft')`)
      const note = txt.includes('previews are enabled')
      check(btn || note, '5 draft affordance rendered (button or previews note)')
      info('5 draft affordance rendered as ' + (btn ? 'button' : 'note'))
    }
    await ev(`(${card}).scrollIntoView({block:'start'})`)
    await shot('approvals-390.png')
    await click(BTN_BY_TEXT(card, 'Approve'), 'draft Approve')
    let ex2 = false
    for (let i = 0; i < 40 && !ex2; i++) {
      const r = await api('GET', '/api/actions?status=executed&limit=300')
      ex2 = Array.isArray(r.json) && r.json.some((a) => a.id === seeded.id)
      if (!ex2) await sleep(500)
    }
    check(ex2, `5 dashboard draft approved at phone width (action ${seeded.id} executed; not published on p8)`)
  }

  // ---- 6. never chats or people
  await notePath()
  const bad = [...paths].filter((p) => p.startsWith('/chats') || p.startsWith('/people'))
  check(bad.length === 0, '6 never visited /chats or /people', bad.join(','))
  info('paths visited: ' + [...paths].join(' '))

  // ---- 7. KPIs and insights
  const ws2 = await api('GET', '/api/watches')
  // Phase 10: the 08 harness names the thread verify-<stack>-live, so match any stack.
  const wid = (ws2.json || []).find((w) => /^verify-p\d+-live$/.test(w.thread_id))?.id
  if (!wid) fail('7 seeded watch found', 'no verify-pN-live watch')
  else {
    await goto(`${UI}/kpis/${wid}`)
    try {
      await waitFor(`Array.from(document.querySelectorAll('.ic-trend svg polyline')).some(p => (p.getAttribute('points')||'').trim().split(/\\s+/).length >= 2)`, 'trend polyline')
      pass('7 KPIs page draws a trend polyline with 2+ points')
    } catch (e) { fail('7 KPIs trend polyline', e.message) }
    await sleep(400)
    await shot('kpis-390.png')
  }
  await goto(UI + '/insights')
  try {
    await waitFor("document.body.textContent.includes('No insights yet') || document.body.textContent.includes('Nothing noticed yet') || !!document.querySelector('.ic-list article')", 'insights state')
    const broken = await ev("!!document.querySelector('.banner-broken')")
    check(!broken, '7 Insights shows a calm state with no broken banner')
  } catch (e) { fail('7 Insights calm state', e.message) }

  // ---- 8. 360 layout
  await phone(360, 740)
  await goto(UI + '/')
  await waitFor("!!document.querySelector('.ov')", 'overview root 360')
  await waitFor("document.querySelectorAll('[data-owed]').length > 0", 'owed cards 360')
  await sleep(800)
  await layoutChecks(360)
  await ev('window.scrollTo(0, 0)')
  await shot('overview-360.png')
  await phone(390, 844)

  // ---- 9. profile switch
  await setProfile('second_brain')
  await goto(UI + '/')
  await waitFor("document.querySelector('nav.rail') && document.querySelector('nav.rail').textContent.includes('Today')", 'Second Brain rail')
  const sb = await ev(`({ rail: document.querySelector('nav.rail').textContent, wm: (document.querySelector('.topbar .wordmark')||{}).textContent || '', title: document.title })`)
  check(!sb.rail.includes('Overview'), '9 Second Brain rail has no Overview')
  check(sb.wm.trim().toUpperCase() === 'SECOND BRAIN', '9 Second Brain wordmark', sb.wm)
  check(sb.title === 'Second Brain', '9 Second Brain title', sb.title)
  pass('9 profile switch reflected in the UI with no rebuild')

  // ---- 10. fresh Second Brain load
  await ev("localStorage.removeItem('secondbrain.ui_profile'); true")
  await goto(UI + '/')
  await waitFor("document.querySelector('nav.rail') && document.querySelector('nav.rail').textContent.includes('Today')", 'fresh Second Brain rail')
  let seen = await ev('window.__seen')
  check(!seen.titles.some((t) => t.includes('Intelligence Corner')), '10 fresh Second Brain load never showed an Intelligence Corner title', JSON.stringify(seen.titles))
  check(!seen.wordmarks.some((t) => t.includes('INTELLIGENCE')), '10 fresh Second Brain load never showed an INTELLIGENCE wordmark (shell)', JSON.stringify(seen.wordmarks))
  const icon = await ev("(document.querySelector('link[rel=icon]')||{}).href || ''")
  check(decodeURIComponent(icon).toUpperCase().includes(SB_ICON_MARK), '10 final favicon is the Second Brain icon')
  check(!seen.icons.some((i) => i && !decodeURIComponent(i).toUpperCase().includes(SB_ICON_MARK) && decodeURIComponent(i).includes('M23 8H9v14')), '10 favicon never switched to the Intelligence Corner icon')
  await shot('second-brain-390.png')
  await ev("localStorage.removeItem('secondbrain.ui_profile'); localStorage.removeItem('secondbrain.opskey'); true")
  await goto(UI + '/')
  await waitFor("!!document.querySelector('.signin')", 'sign-in screen')
  await waitFor("document.querySelector('.signin .wordmark') && document.querySelector('.signin .wordmark').textContent.trim().length > 0", 'sign-in wordmark')
  seen = await ev('window.__seen')
  const swm = await ev("document.querySelector('.signin .wordmark').textContent")
  check(swm.trim().toUpperCase() === 'SECOND BRAIN', '10 sign-in wordmark reads SECOND BRAIN', swm)
  check(!seen.wordmarks.some((t) => t.includes('INTELLIGENCE')), '10 sign-in never showed an INTELLIGENCE wordmark', JSON.stringify(seen.wordmarks))
  check(!seen.titles.some((t) => t.includes('Intelligence Corner')), '10 sign-in never showed an Intelligence Corner title', JSON.stringify(seen.titles))

  // ---- restore
  await setProfile('intelligence')
  await ev(`localStorage.setItem('secondbrain.opskey', ${JSON.stringify(KEY)}); true`)
  await goto(UI + '/')
  await waitFor("document.querySelector('nav.rail') && document.querySelector('nav.rail').textContent.includes('Overview')", 'Overview rail back')
  pass('10 Intelligence Corner profile restored, Overview is back')
}

try {
  await main()
} catch (e) {
  fail('walkthrough aborted', e.message)
} finally {
  try {
    if (startProfile) {
      await setProfile(startProfile)
      info('restored ui_profile ' + startProfile)
    }
  } catch { /* best effort */ }
  try { ws?.close() } catch { /* ignore */ }
  try { chrome?.kill() } catch { /* ignore */ }
  await sleep(800)
  try { rmSync(profileDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 }) } catch { /* ignore */ }
}
console.log(`walkthrough: ${passed} passed, ${failed} failed`)
process.exit(failed > 0 ? 1 : 0)

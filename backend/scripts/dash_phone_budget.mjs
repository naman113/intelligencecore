// Dev-machine harness (not product code): the phone performance budget for the v2 dashboard renderer
// (Phase 22, DCAP-01). It saves a 40-tile, 12-chart v2 spec over the stack's governed measures
// through the ops route, publishes it, and measures in headless Chrome (over CDP) the time from the
// wrapper's navigation start to the renderer's performance mark `ic-first-screen`, on an emulated
// mid-range phone, per network profile, cold (cache disabled) and warm (second load).
//
// Profiles:  4g      150 ms RTT, 4 Mbps down, 1.5 Mbps up, CPU x4, 412 x 915 mobile viewport
//            slow4g  Lighthouse "Slow 4G": 150 ms, 1.6 Mbps down, 750 Kbps up, CPU x4, same viewport
// Budget:    2000 ms; the run exits 0 only when the 4g cold value is under the budget.
// Output:    one JSON line per profile {profile, cold_ms, warm_ms, budget_ms, pass}
// Never prints tokens, the ops key or query strings (every print path goes through redact()).
//
// Usage: node dash_phone_budget.mjs [--secrets <file with OPS_KEY=...>] [--keep] [--wait-ms N]
//          [--url <loopback wrapper URL>]   measure an existing page instead of saving a dashboard
//          [--print-spec [--measures a,b,c]] [--print-profiles] [--evaluate '<json>']   (pure, no stack)
// Env:   BASE_URL (http://127.0.0.1:9281)  DASH_URL (http://127.0.0.1:9282)  both must be loopback
//        PG_CONTAINER (ic-p28-postgres-1)  OPS_KEY (or --secrets)  CHROME (browser path)
// Exit:  0 pass, 1 fail, 2 usage / unhealthy / no measures, 3 no browser.
import { spawn, execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const argv = process.argv.slice(2);
const flagVal = (name) => { const i = argv.indexOf(name); return i >= 0 ? argv[i + 1] : undefined; };

// ---------------------------------------------------------------- pure parts

export const BUDGET_MS = 2000;
export const MARK = 'ic-first-screen';

// CDP Network.emulateNetworkConditions takes bytes per second; Emulation.setCPUThrottlingRate a slowdown factor.
const kbps = (n) => Math.round((n * 1000) / 8);
export const PROFILES = {
  '4g': { latency: 150, down: kbps(4000), up: kbps(1500), cpu: 4, width: 412, height: 915, scale: 2.625 },
  slow4g: { latency: 150, down: kbps(1600), up: kbps(750), cpu: 4, width: 412, height: 915, scale: 2.625 },
};

// 20 stat + 12 line + 8 text = 40 tiles, 12 of them charts, 4 sections of 10, every number under the
// date control and bound to a governed measure (measure names are rotated over the given list).
export function buildPhoneSpec(measureNames) {
  const names = [...new Set(measureNames)].slice(0, 6);
  if (!names.length) throw new Error('no measure names');
  const pick = (i) => names[i % names.length];
  const tiles = [];
  for (let i = 1; i <= 20; i++) {
    tiles.push({ id: `s${String(i).padStart(2, '0')}`, type: 'stat', title: `Stat ${i}`, measure: { name: pick(i) },
      date: 'control', format: 'number', comparison: 'previous_period', good_direction: 'up' });
  }
  for (let i = 1; i <= 12; i++) {
    tiles.push({ id: `l${String(i).padStart(2, '0')}`, type: 'line', title: `Trend ${i}`, measure: { name: pick(i + 3) },
      date: 'control', format: 'number' });
  }
  for (let i = 1; i <= 8; i++) {
    tiles.push({ id: `x${String(i).padStart(2, '0')}`, type: 'text', title: `Note ${i}`, text: `Phone budget note ${i}.` });
  }
  const ids = tiles.map((t) => t.id);
  const sections = [0, 1, 2, 3].map((k) => ({ title: `Section ${k + 1}`, tiles: ids.slice(k * 10, k * 10 + 10) }));
  return {
    spec_version: 2,
    title: 'Phone budget probe',
    number_locale: 'en-US',
    date_control: { default: 'last_30_days', presets: null, horizon_days: 400, fy_start_month: 4 },
    sections,
    tiles,
  };
}

export function countCharts(spec) {
  const charts = new Set(['line', 'area', 'bar', 'stacked_bar', 'sparkline', 'combo', 'paired_bar', 'aging',
    'bullet', 'waterfall', 'calendar', 'donut', 'ranked_bar', 'funnel']);
  return spec.tiles.filter((t) => charts.has(t.type)).length;
}

// Frame mark time as an offset from the wrapper's navigation start (both are epoch milliseconds).
export function elapsedMs(topTimeOrigin, frameTimeOrigin, markStart) {
  if (![topTimeOrigin, frameTimeOrigin, markStart].every((n) => typeof n === 'number' && isFinite(n))) return null;
  return Math.round(frameTimeOrigin + markStart - topTimeOrigin);
}

// results: [{profile, cold_ms, warm_ms}] -> {ok, lines}; ok only when the 4g cold value is under the budget.
export function evaluate(results) {
  const lines = results.map((r) => ({
    profile: r.profile,
    cold_ms: r.cold_ms,
    warm_ms: r.warm_ms,
    budget_ms: BUDGET_MS,
    pass: typeof r.cold_ms === 'number' && r.cold_ms < BUDGET_MS,
  }));
  const g4 = lines.find((l) => l.profile === '4g');
  return { ok: !!g4 && g4.pass, lines };
}

if (argv.includes('--print-spec')) {
  const names = (flagVal('--measures') || 'm_a,m_b,m_c').split(',').filter(Boolean);
  process.stdout.write(JSON.stringify(buildPhoneSpec(names)) + '\n');
  process.exit(0);
}
if (argv.includes('--print-profiles')) {
  process.stdout.write(JSON.stringify({ profiles: PROFILES, budget_ms: BUDGET_MS, mark: MARK }) + '\n');
  process.exit(0);
}
if (argv.includes('--evaluate')) {
  process.stdout.write(JSON.stringify(evaluate(JSON.parse(flagVal('--evaluate') || '[]'))) + '\n');
  process.exit(0);
}

// ---------------------------------------------------------------- config, guards, redaction

const BASE = (process.env.BASE_URL || 'http://127.0.0.1:9281').replace(/\/$/, '');
const DASH = (process.env.DASH_URL || 'http://127.0.0.1:9282').replace(/\/$/, '');
const PG = process.env.PG_CONTAINER || 'ic-p28-postgres-1';
const KEEP = argv.includes('--keep');
const WAIT_MS = Number(flagVal('--wait-ms')) || 180000;
const DIRECT_URL = flagVal('--url');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const secrets = new Set();

function redact(s) {
  let t = String(s).replace(/([?&](?:t|p)=)[^&\s"']+/g, '$1REDACTED');
  for (const v of secrets) if (v) t = t.split(v).join('REDACTED');
  return t;
}
const say = (s) => console.log(redact(s));
const warn = (s) => console.error(redact(s));
function usage(msg) { warn(msg); process.exit(2); }

for (const u of [BASE, DASH, ...(DIRECT_URL ? [DIRECT_URL] : [])]) {
  let host = '';
  try { host = new URL(u).hostname; } catch { usage(`REFUSED: ${u} is not a URL`); }
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
if (!KEY && !DIRECT_URL) usage('REFUSED: no OPS_KEY (env or --secrets <file>)');
if (KEY) secrets.add(KEY);

async function api(method, p, body) {
  const r = await fetch(BASE + p, { method, headers: { 'x-ops-key': KEY, 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await r.text();
  let json = null; try { json = JSON.parse(text); } catch { /* not json */ }
  return { status: r.status, json, text };
}

function psql(sql) {
  return execFileSync('docker', ['exec', PG, 'psql', '-U', 'agent', '-d', 'agent_memory', '-v', 'ON_ERROR_STOP=1', '-At', '-c', sql],
    { env: { ...process.env, MSYS_NO_PATHCONV: '1' }, stdio: ['ignore', 'pipe', 'pipe'] }).toString();
}

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

// ---------------------------------------------------------------- the CDP measurement

// One fresh browser per profile. Returns {cold_ms, warm_ms} (null when the mark never appeared).
async function measureProfile(browser, pageUrl, name) {
  const prof = PROFILES[name];
  const ud = fs.mkdtempSync(path.join(os.tmpdir(), 'ic-phone-'));
  const child = spawn(browser, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--remote-debugging-port=0', `--user-data-dir=${ud}`, '--disable-background-networking',
    '--disable-features=Translate,OptimizationHints', 'about:blank'], { stdio: 'ignore' });
  const exited = new Promise((r) => child.on('exit', r));
  let ws; const pending = new Map(); let nextId = 0; const listeners = [];
  const send = (method, params = {}, sessionId) => new Promise((res) => {
    const id = ++nextId; pending.set(id, res);
    ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
  const out = { cold_ms: null, warm_ms: null };
  try {
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

    let cacheDisabled = true;
    let pageSession = null; let frameSession = null;
    // The same conditions apply to the page and to every out-of-process frame that attaches.
    async function throttle(sid) {
      await send('Network.enable', {}, sid);
      await send('Network.setCacheDisabled', { cacheDisabled }, sid);
      await send('Network.emulateNetworkConditions', { offline: false, latency: prof.latency,
        downloadThroughput: prof.down, uploadThroughput: prof.up }, sid);
      await send('Emulation.setCPUThrottlingRate', { rate: prof.cpu }, sid);
    }
    listeners.push((m) => {
      if (m.method === 'Target.attachedToTarget' && m.sessionId === pageSession && (m.params.targetInfo || {}).type === 'iframe') {
        const cs = m.params.sessionId;
        (async () => {
          await throttle(cs);
          await send('Runtime.enable', {}, cs);
          frameSession = cs;
          await send('Runtime.runIfWaitingForDebugger', {}, cs);
        })();
      }
    });

    const tl = await send('Target.getTargets');
    const pageT = (tl.result?.targetInfos || []).find((t) => t.type === 'page');
    const a = await send('Target.attachToTarget', { targetId: pageT.targetId, flatten: true });
    pageSession = a.result.sessionId;
    await send('Page.enable', {}, pageSession);
    await send('Runtime.enable', {}, pageSession);
    await send('Emulation.setDeviceMetricsOverride', { width: prof.width, height: prof.height, deviceScaleFactor: prof.scale, mobile: true }, pageSession);
    await send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: true, flatten: true }, pageSession);

    async function loadOnce() {
      frameSession = null;
      await throttle(pageSession);
      await send('Page.navigate', { url: pageUrl }, pageSession);
      const end = Date.now() + 45000;
      while (Date.now() < end) {
        await sleep(250);
        if (!frameSession) continue;
        const fr = await send('Runtime.evaluate', { returnByValue: true, expression:
          `(() => { const m = performance.getEntriesByName(${JSON.stringify(MARK)})[0]; return m ? { o: performance.timeOrigin, s: m.startTime } : null; })()` }, frameSession);
        const fv = fr.result?.result?.value;
        if (!fv) continue;
        const tr = await send('Runtime.evaluate', { returnByValue: true, expression: 'performance.timeOrigin' }, pageSession);
        return elapsedMs(tr.result?.result?.value, fv.o, fv.s);
      }
      return null;
    }

    cacheDisabled = true;               // cold: nothing cached
    out.cold_ms = await loadOnce();
    cacheDisabled = false;              // warm: one priming load, then the measured second load
    await loadOnce();
    out.warm_ms = await loadOnce();
    await send('Browser.close');
    try { ws.close(); } catch { /* ignore */ }
    await Promise.race([exited, sleep(15000)]);
  } finally {
    try { ws?.close(); } catch { /* ignore */ }
    if (child.exitCode === null) child.kill();
    await sleep(300);
    try { fs.rmSync(ud, { recursive: true, force: true }); } catch { /* ignore */ }
  }
  return out;
}

// ---------------------------------------------------------------- main

let exitCode = 1; let slug = '';
try {
  const browser = findBrowser();
  if (!browser) { say(JSON.stringify({ skip: 'no Chrome/Edge found' })); process.exit(3); }
  let pageUrl = DIRECT_URL;
  if (!pageUrl) {
    const h = await fetch(`${BASE}/health`).catch(() => null);
    if (!h || h.status !== 200) usage(`unhealthy: ${BASE}/health`);
    const ms = await api('GET', '/api/model/measures');
    // a fixed, now or distinct measure cannot follow the date control (compile_measures_v2 refuses it)
    const BINDABLE = new Set(['sum', 'avg', 'ratio', 'formula', 'min', 'max', 'closing', 'opening']);
    const names = (ms.json?.measures || [])
      .filter((m) => m.name && !m.compile_error && BINDABLE.has(m.kind)).map((m) => m.name);
    if (ms.status !== 200 || !names.length) usage('no governed measures to bind: approve a semantic model first');
    const spec = buildPhoneSpec(names);
    if (spec.tiles.length !== 40 || countCharts(spec) !== 12) usage('internal: the probe spec must have 40 tiles and 12 charts');
    slug = 'phonebudget-' + crypto.randomBytes(5).toString('hex');
    const sv = await api('POST', `/api/dashboards/${slug}/versions`, { spec, change_note: 'phone budget probe' });
    if (sv.status !== 200) usage(`save failed: HTTP ${sv.status} ${sv.text.slice(0, 300)}`);
    const version = sv.json.version;
    const until = Date.now() + WAIT_MS; let ready = false; let lastErr = '';
    while (Date.now() < until && !ready) {
      const vr = await api('GET', `/api/dashboards/${slug}/versions/${version}`);
      const snaps = vr.json?.snapshots || [];
      lastErr = snaps.map((s) => s.error).filter(Boolean).join('; ');
      ready = snaps.length > 0 && snaps.every((s) => s.refreshed_at && !s.error);
      if (!ready) await sleep(3000);
    }
    if (!ready) { say(`FAIL snapshots did not refresh in ${WAIT_MS} ms ${lastErr.slice(0, 200)}`); throw Object.assign(new Error('snapshots'), { code: 1 }); }
    const person = psql('SELECT id FROM people WHERE is_primary AND active LIMIT 1').trim();
    if (!person) usage('no primary active person: create one before publishing');
    const pub = await api('POST', `/api/dashboards/${slug}/publish`, { version, decided_by: Number(person) });
    if (pub.status !== 200) usage(`publish failed: HTTP ${pub.status} ${pub.text.slice(0, 200)}`);
    const dash = await api('GET', `/api/dashboards/${slug}`);
    const vt = dash.json?.dashboard?.view_token;
    if (!vt) usage('no view token after publish');
    secrets.add(vt);
    pageUrl = `${DASH}/d/${slug}/?t=${vt}`;
  }
  const results = [];
  for (const name of Object.keys(PROFILES)) {
    const r = await measureProfile(browser, pageUrl, name);
    results.push({ profile: name, cold_ms: r.cold_ms, warm_ms: r.warm_ms });
  }
  const ev = evaluate(results);
  for (const l of ev.lines) say(JSON.stringify(l));
  exitCode = ev.ok ? 0 : 1;
} catch (e) {
  if (e && e.code === 1) exitCode = 1;
  else { warn('phone budget probe failed: ' + (e && e.message ? e.message : e)); exitCode = 2; }
} finally {
  try { if (slug && !KEEP) psql(`DELETE FROM dashboards WHERE slug = '${slug.replace(/[^a-z0-9-]/g, '')}'`); } catch { /* ignore */ }
}
process.exit(exitCode);

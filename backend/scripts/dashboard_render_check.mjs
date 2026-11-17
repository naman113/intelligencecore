// Dev-machine render check for a dashboard draft or version on the dashboards origin.
// Not part of the product (LATER-03 stays deferred). Never prints the URL's query string.
// Phase 12: the page is a wrapper around an opaque-origin sandboxed frame, which is a
// separate out-of-process CDP target; the chart evaluation runs in that child session.
// Phase 18: --expect-tiles N also requires at least N .ic-tile elements, a chart when a chart tile
// exists and no .ic-fail element; the JSON line then adds tiles, failed.
// Usage: node dashboard_render_check.mjs <url> [--wait-ms 20000] [--expect-tiles N]
// Prints one JSON line; exit 0 ok, 1 not ok, 2 usage/protocol failure, 3 no browser.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const args = process.argv.slice(2);
const url = args[0];
let waitMs = 20000;
const wi = args.indexOf('--wait-ms');
if (wi >= 0) waitMs = Number(args[wi + 1]) || 20000;
const ti = args.indexOf('--expect-tiles');
const expectTiles = ti >= 0 ? Number(args[ti + 1]) || 0 : 0;
if (!url || !/^https?:\/\//.test(url)) {
  console.error('usage: node dashboard_render_check.mjs <url> [--wait-ms N]');
  process.exit(2);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function findBrowser() {
  const cands = [process.env.CHROME,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'];
  for (const c of cands) if (c && fs.existsSync(c)) return c;
  const sep = process.platform === 'win32' ? ';' : ':';
  for (const n of ['google-chrome', 'chromium', 'chromium-browser']) {
    for (const d of (process.env.PATH || '').split(sep)) {
      const p = path.join(d, n);
      if (d && fs.existsSync(p)) return p;
    }
  }
  return null;
}

const browser = findBrowser();
if (!browser) {
  console.log(JSON.stringify({ skip: 'no Chrome/Edge found' }));
  process.exit(3);
}

const EXPR = `(() => {
  const inst = (typeof Chart !== 'undefined' && Chart.instances) ? Object.values(Chart.instances) : [];
  let maxPoints = 0;
  for (const c of inst) for (const d of (c.data && c.data.datasets) || []) maxPoints = Math.max(maxPoints, (d.data || []).length);
  const asOf = [...document.querySelectorAll('*')].find((e) => e.children.length === 0 && /^As of/.test((e.textContent || '').trim()));
  return { tiles: document.querySelectorAll('.ic-tile').length, failed: document.querySelectorAll('.ic-fail').length,
    chartTiles: document.querySelectorAll('.ic-chart, .ic-spark').length, charts: inst.length, maxPoints, tableRows: document.querySelectorAll('table tr').length,
    asOf: asOf ? asOf.textContent.trim() : '', title: document.title,
    bodyText: (document.body ? document.body.innerText : '').slice(0, 300) };
})()`;

let child; let ud; let code = 2;
const errors = [];
try {
  ud = fs.mkdtempSync(path.join(os.tmpdir(), 'ic-render-'));
  child = spawn(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--no-default-browser-check', '--remote-debugging-port=0', `--user-data-dir=${ud}`, 'about:blank'],
  { stdio: 'ignore' });
  let port = null;
  for (let i = 0; i < 30 && !port; i++) {
    await sleep(500);
    try { port = fs.readFileSync(path.join(ud, 'DevToolsActivePort'), 'utf8').split('\n')[0].trim(); } catch { /* wait */ }
  }
  if (!port) throw new Error('DevToolsActivePort not found');
  const tr = await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: 'PUT' });
  const target = await tr.json();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('ws error')); });
  let id = 0; const pending = new Map();
  const send = (method, params = {}, sessionId) => new Promise((res) => {
    const i = ++id; pending.set(i, res);
    ws.send(JSON.stringify({ id: i, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
  const argText = (a) => String(a.value ?? a.description ?? a.type);
  const frameSessions = [];
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
    if (m.method === 'Target.attachedToTarget') {
      const ti = m.params.targetInfo || {};
      if (ti.type === 'iframe') {
        frameSessions.push(m.params.sessionId);
        send('Runtime.enable', {}, m.params.sessionId);
        send('Log.enable', {}, m.params.sessionId);
      }
      return;
    }
    if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails; errors.push('exception: ' + (d.exception?.description || d.text));
    } else if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
      errors.push('console: ' + m.params.args.map(argText).join(' '));
    } else if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') {
      const e = m.params.entry;
      if (!(e.url || '').endsWith('/favicon.ico')) errors.push('log: ' + e.text + (e.url ? ' ' + e.url.split('?')[0] : ''));
    }
  };
  await send('Runtime.enable'); await send('Log.enable'); await send('Page.enable');
  await send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });
  await send('Page.navigate', { url });
  const start = Date.now(); let first = null; let last = null;
  while (Date.now() - start < waitMs) {
    await sleep(500);
    // Prefer the sandboxed frame session; fall back to the page target for unwrapped URLs.
    const sess = frameSessions.length ? frameSessions[frameSessions.length - 1] : undefined;
    const r = await send('Runtime.evaluate', { expression: EXPR, returnByValue: true }, sess);
    last = r.result?.result?.value || last;
    if (last && (expectTiles ? last.tiles >= expectTiles && (!last.chartTiles || last.charts >= 1)
      : (last.charts >= 1 || last.tableRows >= 2))) {
      first = first ?? Date.now();
      if (Date.now() - first >= 2000) break;
    }
  }
  const v = last || { charts: 0, maxPoints: 0, tableRows: 0, asOf: '', title: '', bodyText: '' };
  let ok = (v.charts >= 1 || v.tableRows >= 2) && errors.length === 0;
  if (expectTiles) {
    ok = (v.tiles || 0) >= expectTiles && !(v.failed) && (!v.chartTiles || v.charts >= 1) && errors.length === 0;
    console.log(JSON.stringify({ ok, tiles: v.tiles || 0, charts: v.charts, failed: v.failed || 0,
      errors: errors.slice(0, 10).map((e) => e.slice(0, 200)) }));
    code = ok ? 0 : 1;
    ws.close();
    throw Object.assign(new Error('done'), { done: true });
  }
  console.log(JSON.stringify({ charts: v.charts, maxPoints: v.maxPoints, tableRows: v.tableRows, asOf: v.asOf,
    title: v.title, errors: errors.slice(0, 10).map((e) => e.slice(0, 200)), ok }));
  code = ok ? 0 : 1;
  ws.close();
} catch (e) {
  if (!e.done) {
    console.error('render check failed: ' + e.message);
    code = 2;
  }
} finally {
  if (child) child.kill();
  await sleep(500);
  if (ud) try { fs.rmSync(ud, { recursive: true, force: true }); } catch { /* ignore */ }
}
process.exit(code);

// Dev-machine harness (not product code): reads the numbers a rendered draft shows, for the dashboard
// eval's numbers gate (22-19). Opens the draft WRAPPER page (the frame route answers 404 to a top-level document) in headless Chrome over CDP at 1280 x 8000
// (so every tile intersects and mounts), waits for the performance mark ic-first-screen plus 3 s, and
// prints ONE JSON line: [{tile, col, row, series, value}] for every element with dataset.icValue
// (tile from the closest ancestor with dataset.icTile; col, row and series are null/'' when absent).
// Env:  DRAFT_URL  the wrapper URL of the draft, e.g. http://127.0.0.1:9332/d/<slug>/v/<n>/?p=<token>
//       (loopback only; never printed)   CHROME  browser path (optional)
// Exit: 0 ok, 1 page error, 2 usage, 3 no Chrome/Edge found.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const url = process.env.DRAFT_URL || '';
let host = '';
try { host = new URL(url).hostname; } catch { /* handled below */ }
if (!['127.0.0.1', 'localhost', '[::1]'].includes(host)) {
  console.error('REFUSED: DRAFT_URL is missing or not loopback');
  process.exit(2);
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

const browser = findBrowser();
if (!browser) { console.error('no Chrome/Edge found'); process.exit(3); }

const COLLECT = `(() => {
  const out = [];
  for (const el of document.querySelectorAll('[data-ic-value]')) {
    const t = el.closest('[data-ic-tile]');
    const n = Number(el.dataset.icValue);
    if (!t || !Number.isFinite(n)) continue;
    out.push({ tile: t.dataset.icTile, col: el.dataset.icCol || null, row: el.dataset.icRow || '',
      series: el.dataset.icSeries === undefined ? null : el.dataset.icSeries, value: n });
  }
  return JSON.stringify(out);
})()`;

const ud = fs.mkdtempSync(path.join(os.tmpdir(), 'ic-evalgate-'));
const child = spawn(browser, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--remote-debugging-port=0', `--user-data-dir=${ud}`, '--disable-background-networking', 'about:blank'],
  { stdio: 'ignore' });
let code = 0;
let ws;
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
  const pending = new Map(); let nextId = 0; let pageError = ''; let pageSid = null; let frameSid = null;
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
    if (m.method === 'Target.attachedToTarget' && m.sessionId === pageSid && (m.params.targetInfo || {}).type === 'iframe') {
      const cs = m.params.sessionId;
      (async () => { await send('Runtime.enable', {}, cs); frameSid = cs; await send('Runtime.runIfWaitingForDebugger', {}, cs); })();
      return;
    }
    if (m.method === 'Runtime.exceptionThrown') pageError = pageError || 'uncaught exception in the page';
  };
  const send = (method, params = {}, sessionId) => new Promise((res) => {
    const id = ++nextId; pending.set(id, res);
    ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
  const tl = await send('Target.getTargets');
  const pageT = (tl.result?.targetInfos || []).find((t) => t.type === 'page');
  const a = await send('Target.attachToTarget', { targetId: pageT.targetId, flatten: true });
  const sid = a.result.sessionId; pageSid = sid;
  for (const dom of ['Runtime.enable', 'Page.enable']) await send(dom, {}, sid);
  await send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 8000, deviceScaleFactor: 1, mobile: false }, sid);
  // The agent page runs in a sandboxed iframe of the wrapper: attach to it and read values there.
  await send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: true, flatten: true }, sid);
  const nav = await send('Page.navigate', { url }, sid);
  if (nav.result?.errorText) throw new Error('navigation failed: ' + nav.result.errorText);
  const evalJs = async (expr) => {
    if (!frameSid) return undefined;
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true }, frameSid);
    if (r.result?.exceptionDetails) throw new Error('evaluate failed');
    return r.result?.result?.value;
  };
  let marked = false;
  const end = Date.now() + 60000;
  while (Date.now() < end && !marked) {
    await sleep(500);
    marked = await evalJs("performance.getEntriesByName('ic-first-screen').length > 0").catch(() => false);
  }
  if (!marked) throw new Error('ic-first-screen never fired');
  await sleep(3000);
  const text = await evalJs(COLLECT);
  if (pageError) throw new Error(pageError);
  console.log(text);
} catch (e) {
  console.error('gate probe failed: ' + (e && e.message ? e.message : e));
  code = 1;
} finally {
  try { ws && ws.close(); } catch { /* ignore */ }
  child.kill();
  await sleep(300);
  try { fs.rmSync(ud, { recursive: true, force: true }); } catch { /* ignore */ }
}
process.exit(code);

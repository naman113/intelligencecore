// Dev-machine harness (not product code), Quick 005: opens System > Usage and a run's trace
// in headless Chrome/Edge (CDP) against a live stack, checks they render from the ledger, and
// saves screenshots. Read-only: it never asks the agent anything (no LLM spend).
// Never prints the ops key.
// Usage: node usage_page_check.mjs [--secrets <file with OPS_KEY=...>] [--shots <dir>] [--run <id>]
// Env:   UI_URL (http://127.0.0.1:9180) API_URL (http://127.0.0.1:9181) OPS_KEY CHROME
// Exit:  0 pass, 1 fail, 2 usage/unhealthy, 3 no browser.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const args = process.argv.slice(2);
const opt = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : null; };
const UI = process.env.UI_URL || 'http://127.0.0.1:9180';
const API = process.env.API_URL || 'http://127.0.0.1:9181';
const SHOTS = opt('--shots');
let KEY = process.env.OPS_KEY || '';
if (opt('--secrets')) {
  const m = fs.readFileSync(opt('--secrets'), 'utf8').match(/^OPS_KEY=(.+)$/m);
  KEY = m ? m[1].trim() : '';
}
if (!KEY) { console.log('need OPS_KEY or --secrets'); process.exit(2); }
if (SHOTS) fs.mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const api = async (p) => {
  const r = await fetch(API + p, { headers: { 'x-ops-key': KEY } });
  return { status: r.status, json: r.headers.get('content-type')?.includes('json') ? await r.json() : null };
};

let fails = 0;
const check = (name, ok) => { console.log(`${ok ? 'PASS' : 'FAIL'} ${name}`); if (!ok) fails++; };

function findBrowser() {
  const cands = [process.env.CHROME, 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'];
  for (const c of cands) if (c && fs.existsSync(c)) return c;
  return null;
}
const browser = findBrowser();
if (!browser) { console.log(JSON.stringify({ skip: 'no Chrome/Edge found' })); process.exit(3); }
let child; let ud; let ws; let sid;
const pending = new Map(); let nextId = 0;
const send = (method, params = {}, sessionId = sid) => new Promise((res) => {
  const id = ++nextId; pending.set(id, res);
  ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
});
async function startBrowser() {
  ud = fs.mkdtempSync(path.join(os.tmpdir(), 'ic-usage-'));
  child = spawn(browser, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--remote-debugging-port=0', `--user-data-dir=${ud}`, '--window-size=1440,900', 'about:blank'], { stdio: 'ignore' });
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
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  };
  const t = await send('Target.createTarget', { url: 'about:blank' }, null);
  const a = await send('Target.attachToTarget', { targetId: t.result.targetId, flatten: true }, null);
  sid = a.result.sessionId;
  await send('Page.enable'); await send('Runtime.enable');
  await viewport(1440, 900);
  await send('Emulation.setFocusEmulationEnabled', { enabled: true });
}
const viewport = (width, height, mobile = false) =>
  send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile });
async function js(expr) {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) throw new Error('js: ' + (r.result.exceptionDetails.exception?.description || expr).slice(0, 300));
  return r.result?.result?.value;
}
async function waitFor(expr, ms = 30000, every = 400) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    try { const v = await js(expr); if (v) return v; } catch { /* page busy */ }
    await sleep(every);
  }
  return null;
}
async function shot(name, full = false) {
  if (!SHOTS) return;
  let clip;
  if (full) {
    const h = await js('Math.min(document.documentElement.scrollHeight, 4000)');
    const w = await js('document.documentElement.clientWidth');
    clip = { x: 0, y: 0, width: w, height: h, scale: 1 };
  }
  const r = await send('Page.captureScreenshot', { format: 'png', ...(clip ? { clip, captureBeyondViewport: true } : {}) });
  fs.writeFileSync(path.join(SHOTS, name), Buffer.from(r.result.data, 'base64'));
  console.log(`shot ${name}`);
}
const q = (s) => JSON.stringify(s);

try {
  if ((await api('/health')).status !== 200) { console.log('stack unhealthy'); process.exit(2); }
  const summary = (await api('/api/usage/summary?period=today')).json;
  check('summary has ledger rows', !!summary && summary.today.calls > 0);
  const runId = Number(opt('--run')) || summary?.top_runs.find((r) => r.run_id)?.run_id;
  const trace = runId ? (await api(`/api/runs/${runId}/trace`)).json : null;
  check('run trace has model and tool events', !!trace && trace.totals.model_calls > 0);

  await startBrowser();
  await send('Page.navigate', { url: UI + '/' });
  await waitFor(`document.readyState === 'complete'`);
  await js(`localStorage.setItem('secondbrain.opskey', ${q(KEY)}); true`);

  await send('Page.navigate', { url: UI + '/system/usage' });
  check('usage page renders tiles', await waitFor(`document.querySelectorAll('[data-tile]').length >= 3`));
  check('usage page renders by-model table', await waitFor(
    `[...document.querySelectorAll('.usage-table td')].some((td) => td.textContent.includes('claude-'))`));
  check('rail has Usage under System', await js(
    `[...document.querySelectorAll('a')].some((a) => a.getAttribute('href') === '/system/usage')`));
  check('no gold on the usage page body', await js(
    `!document.querySelector('[data-usage-page] .owed, [data-usage-page] [class*="owed"]')`));
  await sleep(500);
  await shot('usage-page.png', true);

  if (runId) {
    await send('Page.navigate', { url: `${UI}/system/runs/${runId}` });
    check('run trace renders', await waitFor(`!!document.querySelector('[data-trace-totals]') && document.querySelectorAll('.trace-row').length > 0`));
    await sleep(500);
    await shot('run-trace.png', true);
  }

  await viewport(390, 844, true);
  await send('Page.navigate', { url: UI + '/system/usage' });
  check('usage page renders at 390 px', await waitFor(`document.querySelectorAll('[data-tile]').length >= 3`));
  const overflow = await js('document.documentElement.scrollWidth - document.documentElement.clientWidth');
  check(`no horizontal page scroll at 390 px (${overflow})`, overflow <= 1);
  await sleep(400);
  await shot('usage-phone.png');
} catch (e) {
  console.log('FAIL harness: ' + e.message);
  fails++;
} finally {
  try { child?.kill(); } catch { /* gone */ }
}
console.log(`usage page check: ${fails} failure(s)`);
process.exit(fails ? 1 : 0);

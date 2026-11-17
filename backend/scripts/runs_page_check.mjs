// Dev-machine harness (not product code), Quick 008: opens System > Runs in headless
// Chrome/Edge (CDP) against a live stack, checks the control center renders what the API
// says (running now, failed recently, the feed with kind labels, a background-run detail,
// filters, phone width) and saves screenshots. Read-only: it never asks the agent anything
// (no LLM spend). Never prints the ops key.
// Usage: node runs_page_check.mjs [--secrets <file with OPS_KEY=...>] [--shots <dir>] [--job <id>]
// Env:   UI_URL (http://127.0.0.1:9160) API_URL (http://127.0.0.1:9161) OPS_KEY CHROME
// Exit:  0 pass, 1 fail, 2 usage/unhealthy, 3 no browser.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const args = process.argv.slice(2);
const opt = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : null; };
const UI = process.env.UI_URL || 'http://127.0.0.1:9160';
const API = process.env.API_URL || 'http://127.0.0.1:9161';
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
const viewport = (width, height, mobile = false) =>
  send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile });
async function startBrowser() {
  ud = fs.mkdtempSync(path.join(os.tmpdir(), 'ic-runs-'));
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
}
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
const text = (sel) => `[...document.querySelectorAll(${q(sel)})].map((e) => e.textContent).join('\\n')`;

try {
  if ((await api('/health')).status !== 200) { console.log('stack unhealthy'); process.exit(2); }
  const feed = (await api('/api/activity?limit=50')).json;
  const now = (await api('/api/activity/now')).json;
  check('feed has agent and background runs', !!feed && feed.items.some((i) => i.source === 'agent')
    && feed.items.some((i) => i.source === 'job'));
  const jobId = Number(opt('--job')) || feed?.items.find((i) => i.kind === 'watch_investigation')?.id
    || feed?.items.find((i) => i.source === 'job')?.id;

  await startBrowser();
  await send('Page.navigate', { url: UI + '/' });
  await waitFor(`document.readyState === 'complete'`);
  await js(`localStorage.setItem('secondbrain.opskey', ${q(KEY)}); true`);

  await send('Page.navigate', { url: UI + '/system/runs' });
  check('running now and failed recently render', await waitFor(`!!document.querySelector('[data-runs-now]')`));
  check('failed count matches the API', await waitFor(
    `(document.querySelector('[data-count="failed"]')||{}).textContent === ${q(`${now.counts.failed_24h} in 24 h`)}`));
  check('feed lists every title the API returned (first 10)', await waitFor(
    `${q(feed.items.slice(0, 10).map((i) => i.title))}.every((t) => ${text('.runs-row .list-title')}.includes(t))`));
  for (const label of ['Watch check:', 'Dashboard refresh:', 'Chat:', '(playbook)']) {
    check(`feed shows a "${label}" run`, await js(`${text('.runs-row .list-title')}.includes(${q(label)})`));
  }
  check('no gold on the runs page', await js(`!document.querySelector('.runs-now [class*="owed"], .runs-row[class*="owed"], .runs-filters [class*="owed"]')`));

  await send('Page.navigate', { url: UI + '/system/runs?kind=watches' });
  check('kind filter narrows the feed to watches', await waitFor(
    `(() => { const t = [...document.querySelectorAll('.runs-row .list-title')].map((e) => e.textContent);
      return t.length > 0 && t.every((x) => x.startsWith('Watch')); })()`));

  if (jobId) {
    await send('Page.navigate', { url: `${UI}/system/runs/job-${jobId}` });
    check('background run detail renders', await waitFor(`${text('.split-detail .kv-key')}.includes('Started by')`));
    await sleep(1500);
    await shot('runs-desktop.png', true);
  }

  await viewport(390, 844, true);
  await send('Page.navigate', { url: UI + '/system/runs' });
  check('runs page renders at 390 px', await waitFor(`document.querySelectorAll('.runs-row').length > 0`));
  const overflow = await js('document.documentElement.scrollWidth - document.documentElement.clientWidth');
  check(`no horizontal page scroll at 390 px (${overflow})`, overflow <= 1);
  await sleep(600);
  await shot('runs-phone.png', true);
} catch (e) {
  console.log('FAIL harness: ' + e.message);
  fails++;
} finally {
  try { child?.kill(); } catch { /* gone */ }
}
console.log(`runs page check: ${fails} failure(s)`);
process.exit(fails ? 1 : 0);

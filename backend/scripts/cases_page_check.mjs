// Dev-machine harness (not product code), Quick 024: opens the Cases page and case details
// in headless Chrome/Edge (CDP) against a live stack, checks that an answered request reads
// "answered by the assistant", that an assistant commitment names the assistant, and that a
// case header names its chat instead of printing a number or JID; saves screenshots.
// Read-only: it never asks the agent anything (no LLM spend). Never prints the ops key.
// Usage: node cases_page_check.mjs [--secrets <file with OPS_KEY=...>] [--shots <dir>]
//                                  [--answered <id>] [--open <id>] [--cleaned <id>]
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
const check = (name, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${!ok && detail ? `: ${detail}` : ''}`);
  if (!ok) fails++;
};

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
  ud = fs.mkdtempSync(path.join(os.tmpdir(), 'ic-cases-'));
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
  await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  await send('Emulation.setFocusEmulationEnabled', { enabled: true });
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
async function shot(name) {
  if (!SHOTS) return;
  const h = await js('Math.min(document.documentElement.scrollHeight, 2400)');
  const w = await js('document.documentElement.clientWidth');
  const r = await send('Page.captureScreenshot', { format: 'png', clip: { x: 0, y: 0, width: w, height: h, scale: 1 }, captureBeyondViewport: true });
  fs.writeFileSync(path.join(SHOTS, name), Buffer.from(r.result.data, 'base64'));
  console.log(`shot ${name}`);
}
const q = (s) => JSON.stringify(s);
const detailText = `(document.querySelector('.split-detail')?.innerText || '')`;

async function openCase(id, name, expect) {
  await send('Page.navigate', { url: `${UI}/cases/${id}` });
  const ok = await waitFor(`${detailText}.includes('#${id}')`);
  check(`case #${id} renders`, !!ok);
  await sleep(800);
  const text = await js(detailText);
  const started = await js(`document.querySelector('[data-started-in]')?.textContent || ''`);
  check(`case #${id} header names the chat (${started.trim()})`,
    !!started && !/[0-9]{6,}|@g\.us|@lid|@s\.whatsapp/.test(started), started);
  for (const [label, needle] of expect) check(`case #${id} ${label}`, text.includes(needle), needle);
  await shot(name);
}

try {
  if ((await api('/health')).status !== 200) { console.log('stack unhealthy'); process.exit(2); }
  const cases = (await api('/api/cases')).json || [];
  const answered = Number(opt('--answered')) || cases.find((c) => c.close_reason === 'answered by the assistant in the chat')?.id;
  const open = Number(opt('--open')) || cases.find((c) => c.status === 'open' && c.open_exp > 0)?.id;
  const cleaned = Number(opt('--cleaned')) || null;
  check('an answered case exists', !!answered);

  await startBrowser();
  await send('Page.navigate', { url: UI + '/' });
  await waitFor(`document.readyState === 'complete'`);
  await js(`localStorage.setItem('secondbrain.opskey', ${q(KEY)}); localStorage.setItem('secondbrain.cases.filter', '""'); true`);

  await send('Page.navigate', { url: UI + '/cases' });
  check('cases list renders', await waitFor(`document.querySelectorAll('.split-list .list-row').length > 0`));
  await sleep(800);
  await shot('cases-page.png');

  if (answered) {
    await openCase(answered, 'case-answered.png', [
      ['says why it closed', 'Closed because: answered by the assistant in the chat'],
      ['names the assistant as owing the commitment', 'Intelligence Corner'],
    ]);
    check(`case #${answered} has no "nobody assigned" commitment`, !(await js(`${detailText}.includes('nobody assigned')`)));
  }
  if (open) {
    await openCase(open, 'case-open-commitment.png', [
      ['is still open', 'Close it'],
      ['shows the assistant owing it', 'Intelligence Corner'],
    ]);
  }
  if (cleaned) {
    await openCase(cleaned, 'case-cleaned-up.png', [
      ['says why it closed', 'Closed because: answered by the assistant in the chat'],
    ]);
  }
} catch (e) {
  console.log('FAIL harness: ' + e.message);
  fails++;
} finally {
  try { child?.kill(); } catch { /* gone */ }
}
console.log(`cases page check: ${fails} failure(s)`);
process.exit(fails ? 1 : 0);

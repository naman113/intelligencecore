// Dev-machine check of the Setup page model picker (GOLIVE-01). Not part of the product.
// Never prints the ops key. Saves nothing: the model select is only changed in local page state.
// Usage: OPS_KEY=<key> node setup_render_check.mjs <ui-url> [--wait-ms 20000]
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
if (!url || !/^https?:\/\//.test(url)) {
  console.error('usage: OPS_KEY=<key> node setup_render_check.mjs <ui-url> [--wait-ms N]');
  process.exit(2);
}
const opsKey = process.env.OPS_KEY || '';
if (!opsKey) {
  console.error('OPS_KEY env var is required');
  process.exit(2);
}
const base = url.replace(/\/+$/, '');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function findBrowser() {
  const cands = [process.env.CHROME,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Microsoft/Edge/Application/msedge.exe'];
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

const HAS_CURRENT = `!!document.querySelector('select optgroup[label="Current"]')`;

const EXPR = `(() => {
  const selects = [...document.querySelectorAll('select')];
  const modelSelects = selects.filter((s) => s.querySelector('optgroup[label="Current"]'));
  const first = modelSelects[0];
  const cur = first ? first.querySelector('optgroup[label="Current"]') : null;
  const current = cur ? [...cur.querySelectorAll('option')].map((o) => o.value) : [];
  const groups = first ? [...first.querySelectorAll('optgroup')].map((g) => g.label) : [];
  const other = first ? [...first.querySelectorAll('option')].some((o) => o.value === '__other__' && /^Other model ID/.test(o.textContent.trim())) : false;
  const effective = [...document.querySelectorAll('span[data-effective="true"]')]
    .filter((e) => /^Effective: \\S+ \\u00b7 set in (default|env|db)$/.test(e.textContent.trim())).length;
  const notes = document.querySelectorAll('span[data-model-note="true"]').length;
  const effortDefaults = selects.filter((s) => !s.querySelector('optgroup[label="Current"]') && s.options.length
    && /^\\((default|use \\.env): /.test(s.options[0].textContent.trim())).length;
  return { modelSelects: modelSelects.length, current, groups, other, effective, notes, effortDefaults };
})()`;

const FABLE_EXPR = `(async () => {
  const s = [...document.querySelectorAll('select')].find((x) => x.querySelector('optgroup[label="Current"]'));
  if (!s) return false;
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
  setter.call(s, 'claude-fable-5-1');
  s.dispatchEvent(new Event('change', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 500));
  return [...document.querySelectorAll('span[data-model-note="true"]')].some((e) => e.textContent.includes('30-day data retention'));
})()`;

let child; let ud; let code = 2;
const errors = [];
try {
  ud = fs.mkdtempSync(path.join(os.tmpdir(), 'ic-setup-'));
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
  const send = (method, params = {}) => new Promise((res) => {
    const i = ++id; pending.set(i, res);
    ws.send(JSON.stringify({ id: i, method, params }));
  });
  const argText = (a) => String(a.value ?? a.description ?? a.type);
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
    if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails; errors.push('exception: ' + (d.exception?.description || d.text));
    } else if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
      errors.push('console: ' + m.params.args.map(argText).join(' '));
    }
  };
  await send('Runtime.enable'); await send('Page.enable');
  await send('Page.navigate', { url: base + '/' });
  await sleep(1500);
  // The key goes straight into the throwaway profile's localStorage; it is never logged.
  await send('Runtime.evaluate', { expression: `localStorage.setItem('secondbrain.opskey', ${JSON.stringify(opsKey)})` });
  errors.length = 0;
  await send('Page.navigate', { url: base + '/system/setup' });
  const start = Date.now();
  while (Date.now() - start < waitMs) {
    await sleep(500);
    const r = await send('Runtime.evaluate', { expression: HAS_CURRENT, returnByValue: true });
    if (r.result?.result?.value === true) break;
  }
  await sleep(500);
  const r = await send('Runtime.evaluate', { expression: EXPR, returnByValue: true });
  const v = r.result?.result?.value || { modelSelects: 0, current: [], groups: [], other: false, effective: 0, notes: 0, effortDefaults: 0 };
  const f = await send('Runtime.evaluate', { expression: FABLE_EXPR, returnByValue: true, awaitPromise: true });
  const fableNote = f.result?.result?.value === true;
  const need = ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-fable-5-1'];
  const ok = need.every((m) => v.current.includes(m))
    && v.groups.includes('Current') && v.groups.includes('Legacy (still available)')
    && v.other && v.modelSelects >= 6 && v.effective >= 6 && v.notes >= 1 && fableNote
    && v.effortDefaults >= 6 && errors.length === 0;
  console.log(JSON.stringify({ ok, ...v, fableNote, errors: errors.slice(0, 10).map((e) => e.slice(0, 200)) }));
  code = ok ? 0 : 1;
  ws.close();
} catch (e) {
  console.error('setup render check failed: ' + e.message);
  code = 2;
} finally {
  if (child) child.kill();
  await sleep(500);
  if (ud) try { fs.rmSync(ud, { recursive: true, force: true }); } catch { /* ignore */ }
}
process.exit(code);

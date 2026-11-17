// Dev-machine harness (not product code): proves Quick 003 in a real headless browser (CDP,
// same setup as dashboard_nav_check.mjs) against a demo stack with an LLM key:
//   two chats keep separate histories; Approvals and Dashboards open while an answer runs and
//   another chat answers meanwhile; the working animation and live status line are visible;
//   the Ask button shows "answer ready" when an answer lands with the panel closed; a failed
//   answer (token budget forced low) says so with Retry, and Retry answers; reduced motion
//   stops the animation. Samples API latency idle vs during a long dashboard build.
// Never prints the ops key. Demo stacks only: it changes reactive_token_budget and restores it.
// --no-llm: when the LLM is unavailable, the UI checks run on answers staged straight in the stack's
// Postgres (a 'running' task run the worker never claims, then its reply), the failed-run check uses
// the real provider error, and checks that need a real agent print SKIP.
// Usage: node ask_chats_check.mjs --secrets <file with OPS_KEY=...> [--shots <dir>] [--no-llm]
// Env:   UI_URL (http://127.0.0.1:9160) API_URL (http://127.0.0.1:9161) CHROME (browser path)
//        PG_CONTAINER (ic-p16-postgres-1, --no-llm only)
// Exit:  0 pass, 1 fail, 2 usage/unhealthy, 3 no browser.
import { execFileSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const args = process.argv.slice(2);
const opt = (n) => { const i = args.indexOf(n); return i >= 0 ? args[i + 1] : null; };
const UI = process.env.UI_URL || 'http://127.0.0.1:9160';
const API = process.env.API_URL || 'http://127.0.0.1:9161';
const SHOTS = opt('--shots');
let KEY = process.env.OPS_KEY || '';
if (opt('--secrets')) {
  const m = /^OPS_KEY=(.*)$/m.exec(fs.readFileSync(opt('--secrets'), 'utf8'));
  KEY = m ? m[1].trim() : KEY;
}
if (!KEY) { console.log('usage: --secrets <file> (OPS_KEY)'); process.exit(2); }
if (SHOTS) fs.mkdirSync(SHOTS, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const NO_LLM = args.includes('--no-llm');
const PG = process.env.PG_CONTAINER || 'ic-p16-postgres-1';
const psql = (sql) => execFileSync('docker', ['exec', PG, 'psql', '-U', 'agent', '-d', 'agent_memory', '-tAqc', sql],
  { encoding: 'utf8' }).trim();
const lit = (v) => "'" + String(v).replace(/'/g, "''") + "'";
const skip = (name, why) => console.log(`SKIP ${name} :: ${why}`);
const results = [];
const check = (name, ok, detail = '') => {
  results.push({ name, ok: !!ok });
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ' :: ' + detail : ''}`);
};

async function api(method, p, body) {
  const t = Date.now();
  const r = await fetch(API + p, { method, headers: { 'x-ops-key': KEY, 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await r.text();
  let json = null; try { json = JSON.parse(text); } catch { /* not json */ }
  return { status: r.status, json, ms: Date.now() - t };
}

// ---- browser (from dashboard_nav_check.mjs)
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
  ud = fs.mkdtempSync(path.join(os.tmpdir(), 'ic-ask-'));
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
  // A target made over CDP can start in the background: hidden pages pause polling and
  // throttle timers, which is the product's intended behaviour, not what a user sees.
  await send('Emulation.setFocusEmulationEnabled', { enabled: true });
  await send('Target.activateTarget', { targetId: t.result.targetId }, null);
  await send('Page.bringToFront');
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
  const r = await send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(path.join(SHOTS, name), Buffer.from(r.result.data, 'base64'));
  console.log(`shot ${name}`);
}
const q = (s) => JSON.stringify(s);
const clickText = (sel, text) => js(`(() => { const el = [...document.querySelectorAll(${q(sel)})]
  .find((e) => e.textContent.trim().startsWith(${q(text)})); if (!el) return false; el.click(); return true; })()`);
const askButton = `[...document.querySelectorAll('.rail-foot button')].find((b) => b.textContent.trim().startsWith('Ask'))`;
async function type(text) {
  return js(`(() => { const el = document.querySelector('.ask-composer textarea'); if (!el) return false;
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(el, ${q(text)});
    el.dispatchEvent(new Event('input', { bubbles: true })); return true; })()`);
}
async function ask(text) {
  if (!(await type(text))) throw new Error('no composer');
  await sleep(150);
  await js(`document.querySelector('.ask-composer button[type=submit]').click()`);
}
const lastRun = async (chat) => (await api('GET', `/api/ask?limit=5&chat=${encodeURIComponent(chat)}`)).json;
async function waitRunDone(chat, ms = 420000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const t = await lastRun(chat);
    if (t && t.run && (t.run.status === 'done' || t.run.status === 'failed')) return t;
    await sleep(1500);
  }
  return null;
}
const activeChat = () => js(`localStorage.getItem('secondbrain.askChat')`);

// ---- latency sampler
function sampler() {
  const out = []; let on = true;
  (async () => {
    while (on) {
      for (const p of ['/api/actions?status=proposed', '/api/dashboards', '/api/watches', '/api/ask/chats', '/health']) {
        try { out.push((await api('GET', p)).ms); } catch { out.push(99999); }
      }
      await sleep(1500);
    }
  })();
  return { stop: () => { on = false; return out; } };
}
const stats = (xs) => { const s = [...xs].sort((a, b) => a - b); return s.length
  ? { n: s.length, median: s[Math.floor(s.length / 2)], p95: s[Math.floor(s.length * 0.95)], max: s[s.length - 1] } : {}; };

const Q_MAIN = 'How many customers are there? Answer in one sentence.';
const Q_DASH = 'Build a dashboard: monthly order count and total order value for the last 12 months.';
const Q_SIDE = 'Which product category has the most products? One sentence.';
const Q_FAIL = 'How many products are there in total?';

async function main() {
  const h = await api('GET', '/health');
  if (h.status !== 200) { console.log('stack unhealthy'); process.exit(2); }
  // Re-runs start from the main chat only: earlier extra chats are hidden once idle.
  for (const c of (await api('GET', '/api/ask/chats?operator=owner')).json.chats) {
    if (c.is_main) continue;
    await waitRunDone(c.id, 600000);
    await api('DELETE', `/api/ask/chats/${encodeURIComponent(c.id)}`);
  }
  await startBrowser();
  await send('Page.navigate', { url: UI + '/' });
  await waitFor(`document.readyState === 'complete'`);
  await js(`localStorage.setItem('secondbrain.opskey', ${q(KEY)}); localStorage.removeItem('secondbrain.askChat'); true`);
  await send('Page.navigate', { url: UI + '/' });
  check('app loads signed in', await waitFor(`!!document.querySelector('.rail-foot') && !!(${askButton})`, 30000));
  console.log('visibility: ' + (await js('document.visibilityState')));

  // Panel is non-modal: the page under it stays clickable.
  await js(`${askButton}.click()`);
  check('ask panel opens', await waitFor(`!!document.querySelector('.ask-sheet')`));
  const free = await js(`(() => { const el = document.elementFromPoint(400, 450); return !!el && !el.closest('.ask-sheet')
    && !document.querySelector('.ask-scrim') && document.querySelector('.shell').classList.contains('ask-docked'); })()`);
  check('panel docks beside the page with no scrim', free);

  // Chat 1 (main).
  const idle = sampler(); await sleep(6000); const idleMs = idle.stop();
  await ask(Q_MAIN);
  check('main chat shows the working indicator', await waitFor(`!!document.querySelector('.ask-live .ask-pulse i')`, 15000));
  const doneMain = await waitRunDone('ui:owner', 300000);
  check('main chat answers', doneMain && doneMain.run.status === 'done', doneMain?.run?.status);
  await waitFor(`!document.querySelector('.ask-live')`, 20000);  // the owner sees it (marks it read)
  await sleep(1500);

  // Chat 2: a long dashboard build.
  await clickText('.ask-bar button', 'New chat');
  await waitFor(`document.querySelector('.ask-titles h2')?.textContent === 'New chat'`, 5000);
  await ask(Q_DASH);
  const chat2 = await waitFor(`(() => { const c = localStorage.getItem('secondbrain.askChat'); return c && c.split(':').length === 3 ? c : null; })()`, 15000);
  check('a second chat is created on first message', !!chat2, chat2 ? 'ui:<op>:<key>' : '');
  const steps = new Set();
  const live = await waitFor(`(() => { const s = document.querySelector('.ask-live .ask-step'); return s ? s.textContent : null; })()`, 15000);
  if (live) steps.add(live);
  // Wait until the agent is past the first step so the screenshot shows a real one.
  await waitFor(`(() => { const s = document.querySelector('.ask-live .ask-step')?.textContent; return s && s !== 'Reading your question' && s !== 'Starting'; })()`, 60000, 500);
  const elapsedText = await js(`document.querySelector('.ask-live .ask-elapsed')?.textContent || ''`);
  steps.add(await js(`document.querySelector('.ask-live .ask-step')?.textContent || ''`));
  const anim = await js(`(() => { const i = document.querySelector('.ask-pulse i'); return i ? getComputedStyle(i).animationName : null; })()`);
  check('animation and live status line visible during a run', live && anim === 'ask-breathe' && /\d+:\d\d/.test(elapsedText),
    `step="${[...steps].pop()}" elapsed=${elapsedText} animation=${anim}`);
  await shot('01-working-status.png');
  const secs = (t) => t.split(':').reduce((n, x) => n * 60 + Number(x), 0);
  const e1 = await js(`document.querySelector('.ask-live .ask-elapsed')?.textContent || ''`);
  await sleep(3200);
  const e2 = await js(`document.querySelector('.ask-live .ask-elapsed')?.textContent || ''`);
  check('elapsed time ticks while the answer runs', e1 && e2 && secs(e2) - secs(e1) >= 2, `${e1} -> ${e2}`);

  const busy = sampler();
  const stepTimer = setInterval(async () => {
    try { const s = await js(`document.querySelector('.ask-live .ask-step')?.textContent || ''`); if (s) steps.add(s); } catch { /* */ }
  }, 1000);

  // Navigate while it runs.
  await js(`document.querySelector('a.nav-item[href="/approvals"]').click()`);
  const onApprovals = await waitFor(`location.pathname === '/approvals' && !!document.querySelector('.page-title')`, 15000);
  const stillLive = await js(`!!document.querySelector('.ask-sheet .ask-live')`);
  check('Approvals opens while the answer runs (panel keeps working)', onApprovals && stillLive);
  await sleep(1500);
  await shot('02-approvals-while-running.png');
  await js(`document.querySelector('a.nav-item[href="/dashboards"]').click()`);
  const onDash = await waitFor(`location.pathname === '/dashboards' && !!document.querySelector('.page-title')`, 15000);
  check('Dashboards opens while the answer runs', onDash && (await js(`!!document.querySelector('.ask-sheet .ask-live')`)));
  await sleep(1500);
  await shot('03-dashboards-while-running.png');

  // Another chat answers meanwhile.
  await clickText('.ask-bar button', 'Chats');
  await waitFor(`!!document.querySelector('.ask-chats')`, 5000);
  await js(`[...document.querySelectorAll('.ask-chat-pick')].find((b) => b.querySelector('.pill'))?.click()`);
  await waitFor(`localStorage.getItem('secondbrain.askChat') === 'ui:owner' && !document.querySelector('.ask-loading')`, 10000);
  await ask(Q_SIDE);
  const side = await waitRunDone('ui:owner', 300000);
  await waitFor(`!document.querySelector('.ask-live')`, 20000);
  let seen = false;
  for (let i = 0; i < 20 && !seen; i++) {
    const m = (await api('GET', '/api/ask/chats?operator=owner')).json.chats.find((c) => c.is_main);
    seen = m && !m.unread && m.last_assistant_id === side?.messages?.at(-1)?.id;
    if (!seen) await sleep(500);
  }
  check('an answer shown in the open chat is marked read', seen);
  const dashState = (await lastRun(chat2))?.run?.status;
  check('another chat answers while the dashboard build runs', side?.run?.status === 'done' && (dashState === 'running' || dashState === 'done'),
    `main=${side?.run?.status} dashboard chat=${dashState}`);

  // Close the panel; the build finishes in the background.
  await clickText('.ask-bar button', 'Close');
  await sleep(500);
  const pulse = await js(`!!${askButton}.querySelector('.ask-dot-working')`);
  const done2 = await waitRunDone(chat2, 600000);
  clearInterval(stepTimer);
  const busyMs = busy.stop();
  check('dashboard chat finishes with the panel closed', done2 && done2.run.status !== undefined, done2?.run?.status);
  const ready = await waitFor(`(() => { const b = ${askButton}; const c = b && b.querySelector('.ask-count'); return c ? c.textContent : null; })()`, 30000);
  check('Ask button shows "answer ready" (and pulsed while working)', ready === '1' && pulse, `count=${ready} pulse=${pulse}`);
  await shot('04-answer-ready.png');

  // Chat list + separate histories.
  await js(`${askButton}.click()`);
  await waitFor(`!!document.querySelector('.ask-sheet')`);
  await clickText('.ask-bar button', 'Chats');
  await waitFor(`document.querySelectorAll('.ask-chat').length >= 2`, 5000);
  const list = await js(`[...document.querySelectorAll('.ask-chat')].map((li) => ({ t: li.querySelector('.ask-chat-title')?.textContent,
    m: li.querySelector('.ask-chat-meta')?.textContent, main: !!li.querySelector('.pill') }))`);
  check('chat list shows both chats titled from their first message', list.length === 2 && list.some((c) => c.t.startsWith('Build a dashboard'))
    && list.some((c) => c.main), JSON.stringify(list));
  check('the finished chat is marked "Answer ready" in the list', list.some((c) => c.t.startsWith('Build a dashboard') && c.m === 'Answer ready'));
  await shot('05-chat-list.png');
  await js(`[...document.querySelectorAll('.ask-chat-pick')].find((b) => b.textContent.includes('Build a dashboard'))?.click()`);
  await waitFor(`document.querySelectorAll('.ask-msg').length >= 2 && document.querySelector('.ask-titles h2')?.textContent.startsWith('Build')`, 10000);
  const hist2 = await js(`[...document.querySelectorAll('.ask-msg-user')].map((e) => e.textContent)`);
  await clickText('.ask-bar button', 'Chats');
  await waitFor(`!!document.querySelector('.ask-chats')`, 5000);
  await js(`[...document.querySelectorAll('.ask-chat-pick')].find((b) => b.querySelector('.pill'))?.click()`);
  await waitFor(`localStorage.getItem('secondbrain.askChat') === 'ui:owner' && document.querySelectorAll('.ask-msg-user').length >= 2`, 10000);
  const hist1 = await js(`[...document.querySelectorAll('.ask-msg-user')].map((e) => e.textContent)`);
  check('two chats keep separate histories', hist2.length === 1 && hist2[0] === Q_DASH && hist1.includes(Q_MAIN) && hist1.includes(Q_SIDE)
    && !hist1.includes(Q_DASH), `chat2=${hist2.length} msg(s), main=${hist1.length}`);
  const badgeGone = await waitFor(`!${askButton}.querySelector('.ask-count')`, 10000);
  check('"answer ready" clears once the chat has been seen', !!badgeGone || (await js(`!${askButton}.querySelector('.ask-count')`)));

  // Failed run with Retry.
  const put = await api('PUT', '/api/config', { values: { reactive_token_budget: '800' } });
  check('force a low token budget (demo stack)', put.status === 200);
  await clickText('.ask-bar button', 'New chat');
  await ask(Q_FAIL);
  const failed = await waitFor(`(() => { const f = document.querySelector('.ask-failed'); return f ? f.textContent : null; })()`, 180000, 800);
  check('a failed answer says so plainly with Retry', failed && /token budget/.test(failed) && /Retry/.test(failed), failed || '');
  await shot('06-failed-retry.png');
  await api('PUT', '/api/config', { values: { reactive_token_budget: '' } });
  await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] });
  await clickText('.ask-failed button', 'Retry');
  const reduced = await waitFor(`(() => { const i = document.querySelector('.ask-pulse i'); return i ? getComputedStyle(i).animationName : null; })()`, 20000, 300);
  check('reduced motion turns the animation off', reduced === 'none', `animation=${reduced}`);
  await send('Emulation.setEmulatedMedia', { features: [] });
  const chat3 = await activeChat();
  const retried = await waitRunDone(chat3, 300000);
  await waitFor(`!document.querySelector('.ask-live') && !document.querySelector('.ask-failed') && document.querySelectorAll('.ask-msg-assistant').length >= 1`, 30000);
  const users = await js(`document.querySelectorAll('.ask-msg-user').length`);
  check('Retry answers without repeating the question', retried?.run?.status === 'done' && users === 1, `status=${retried?.run?.status} user msgs=${users}`);
  await shot('07-retried.png');

  const a = stats(idleMs); const b = stats(busyMs);
  console.log(`latency idle ${JSON.stringify(a)} ms; during runs ${JSON.stringify(b)} ms`);
  check('page APIs stay fast during long runs (p95 < 1000 ms)', b.p95 !== undefined && b.p95 < 1000);
  console.log(`steps seen: ${JSON.stringify([...steps])}`);
}

// ---- --no-llm: answers staged in Postgres; the real provider error is the failed run
async function stageRun(chat, text) {
  const mid = psql(`INSERT INTO messages (thread_id, role, content) VALUES (${lit(chat)}, 'user', ${lit(text)}) RETURNING id`);
  return psql(`INSERT INTO task_runs (thread_id, message_id, status, started_at) VALUES (${lit(chat)}, ${Number(mid)}, 'running', now()) RETURNING id`);
}
function finishRun(chat, runId, reply) {
  psql(`INSERT INTO messages (thread_id, role, content) VALUES (${lit(chat)}, 'assistant', ${lit(reply)})`);
  psql(`UPDATE task_runs SET status = 'done', ended_at = now(), result_summary = 'staged' WHERE id = ${Number(runId)}`);
}
const pick = (expr) => js(`(() => { const b = [...document.querySelectorAll('.ask-chat-pick')].find(${expr}); if (!b) return false; b.click(); return true; })()`);

async function mainNoLlm() {
  const h = await api('GET', '/health');
  if (h.status !== 200) { console.log('stack unhealthy'); process.exit(2); }
  for (const c of (await api('GET', '/api/ask/chats?operator=owner')).json.chats) {
    if (c.is_main) continue;
    psql(`UPDATE task_runs SET status = 'failed', ended_at = now(), error = 'staged run cleared' WHERE thread_id = ${lit(c.id)} AND status IN ('queued', 'running')`);
    await api('DELETE', `/api/ask/chats/${encodeURIComponent(c.id)}`);
  }
  const Q_STAGED = 'Build a dashboard: monthly order count and total order value (staged run).';
  const Q_MAIN_STAGED = 'How many customers are there? (staged run)';
  // Main chat: one staged question and answer, so the two histories differ.
  const r0 = await stageRun('ui:owner', Q_MAIN_STAGED);
  finishRun('ui:owner', r0, 'There are 240 customers (staged answer).');
  const chat = (await api('POST', '/api/ask/chats', { operator: 'owner' })).json.id;

  await startBrowser();
  await send('Page.navigate', { url: UI + '/' });
  await waitFor(`document.readyState === 'complete'`);
  await js(`localStorage.setItem('secondbrain.opskey', ${q(KEY)}); localStorage.setItem('secondbrain.askChat', 'ui:owner'); true`);
  await send('Page.navigate', { url: UI + '/' });
  check('app loads signed in', await waitFor(`!!document.querySelector('.rail-foot') && !!(${askButton})`, 30000));
  console.log('visibility: ' + (await js('document.visibilityState')));
  await js(`${askButton}.click()`);
  check('ask panel opens', await waitFor(`!!document.querySelector('.ask-sheet')`));
  check('panel docks beside the page with no scrim', await js(`(() => { const el = document.elementFromPoint(400, 450);
    return !!el && !el.closest('.ask-sheet') && !document.querySelector('.ask-scrim')
      && document.querySelector('.shell').classList.contains('ask-docked'); })()`));

  // A running answer in a second chat (staged: the worker never claims a 'running' row).
  const runId = await stageRun(chat, Q_STAGED);
  await clickText('.ask-bar button', 'Chats');
  await waitFor(`[...document.querySelectorAll('.ask-chat-title')].some((t) => t.textContent.startsWith('Build a dashboard'))`, 15000);
  await pick(`(b) => b.textContent.includes('Build a dashboard') && !b.querySelector('.pill')`);
  const live = await waitFor(`document.querySelector('.ask-live .ask-step')?.textContent`, 20000);
  const anim = await js(`(() => { const i = document.querySelector('.ask-pulse i'); return i ? getComputedStyle(i).animationName : null; })()`);
  check('animation and status line visible during a run', live && anim === 'ask-breathe', `step="${live}" animation=${anim}`);
  const secs = (t) => t.split(':').reduce((n, x) => n * 60 + Number(x), 0);
  const e1 = await js(`document.querySelector('.ask-live .ask-elapsed')?.textContent || ''`);
  await sleep(3200);
  const e2 = await js(`document.querySelector('.ask-live .ask-elapsed')?.textContent || ''`);
  check('elapsed time ticks while the answer runs', e1 && e2 && secs(e2) - secs(e1) >= 2, `${e1} -> ${e2}`);
  await shot('01-working-status.png');
  skip('live tool steps from a real agent run', 'LLM unavailable (provider usage limit); passed 23/23 before the Phase 13 merge');

  await js(`document.querySelector('a.nav-item[href="/approvals"]').click()`);
  const onApprovals = await waitFor(`location.pathname === '/approvals' && !!document.querySelector('.page-title')`, 15000);
  check('Approvals opens while the answer runs (panel keeps working)', onApprovals && (await js(`!!document.querySelector('.ask-sheet .ask-live')`)));
  await sleep(1000);
  await shot('02-approvals-while-running.png');
  await js(`document.querySelector('a.nav-item[href="/dashboards"]').click()`);
  const onDash = await waitFor(`location.pathname === '/dashboards' && !!document.querySelector('.page-title')`, 15000);
  check('Dashboards opens while the answer runs', onDash && (await js(`!!document.querySelector('.ask-sheet .ask-live')`)));
  await sleep(1000);
  await shot('03-dashboards-while-running.png');
  skip('another chat answers while a long run is going', 'LLM unavailable; passed before the Phase 13 merge');

  // Back to the main chat, then close: the staged answer lands with the panel closed.
  await clickText('.ask-bar button', 'Chats');
  await waitFor(`!!document.querySelector('.ask-chats')`, 5000);
  await pick(`(b) => !!b.querySelector('.pill')`);
  await waitFor(`localStorage.getItem('secondbrain.askChat') === 'ui:owner' && document.querySelectorAll('.ask-msg-assistant').length >= 1`, 10000);
  await sleep(1500);
  await clickText('.ask-bar button', 'Close');
  const pulse = await waitFor(`!!${askButton}.querySelector('.ask-dot-working')`, 25000);
  finishRun(chat, runId, 'Your dashboard draft is ready for your approval (staged answer).');
  const ready = await waitFor(`(() => { const c = ${askButton}.querySelector('.ask-count'); return c ? c.textContent : null; })()`, 30000);
  check('Ask button shows "answer ready" (and pulsed while working)', ready === '1' && pulse, `count=${ready} pulse=${!!pulse}`);
  await shot('04-answer-ready.png');

  await js(`${askButton}.click()`);
  await waitFor(`!!document.querySelector('.ask-sheet')`);
  await clickText('.ask-bar button', 'Chats');
  await waitFor(`document.querySelectorAll('.ask-chat').length >= 2`, 5000);
  await sleep(800);
  const list = await js(`[...document.querySelectorAll('.ask-chat')].map((li) => ({ t: li.querySelector('.ask-chat-title')?.textContent,
    m: li.querySelector('.ask-chat-meta')?.textContent, main: !!li.querySelector('.pill') }))`);
  check('chat list shows both chats titled from their first message', list.length === 2 && list.some((c) => !c.main && c.t.startsWith('Build a dashboard'))
    && list.some((c) => c.main), JSON.stringify(list));
  check('the finished chat is marked "Answer ready" in the list', list.some((c) => !c.main && c.m === 'Answer ready'));
  await shot('05-chat-list.png');
  await pick(`(b) => b.textContent.includes('Build a dashboard')`);
  await waitFor(`document.querySelectorAll('.ask-msg-assistant').length >= 1 && document.querySelector('.ask-titles h2')?.textContent.startsWith('Build')`, 10000);
  const hist2 = await js(`[...document.querySelectorAll('.ask-msg-user')].map((e) => e.textContent)`);
  await clickText('.ask-bar button', 'Chats');
  await waitFor(`!!document.querySelector('.ask-chats')`, 5000);
  await pick(`(b) => !!b.querySelector('.pill')`);
  await waitFor(`localStorage.getItem('secondbrain.askChat') === 'ui:owner' && !document.querySelector('.ask-loading')`, 10000);
  await sleep(800);
  const hist1 = await js(`[...document.querySelectorAll('.ask-msg-user')].map((e) => e.textContent)`);
  check('two chats keep separate histories', hist2.length === 1 && hist2[0] === Q_STAGED && hist1.includes(Q_MAIN_STAGED) && !hist1.includes(Q_STAGED),
    `chat2=${hist2.length} msg(s), main=${hist1.length}`);
  check('"answer ready" clears once the chat has been seen', !!(await waitFor(`!${askButton}.querySelector('.ask-count')`, 10000)));

  // The real provider error is the failed run.
  await clickText('.ask-bar button', 'New chat');
  await ask('How many products are there in total?');
  const failed = await waitFor(`(() => { const f = document.querySelector('.ask-failed'); return f ? f.textContent : null; })()`, 60000, 800);
  check('a failed answer says so plainly with Retry', failed && /Retry/.test(failed) && !/BadRequestError|\{/.test(failed), failed || '');
  await shot('06-failed-retry.png');
  const chat3 = await activeChat();
  const before = (await lastRun(chat3))?.run?.id;
  await clickText('.ask-failed button', 'Retry');
  await sleep(1500);
  const again = await waitFor(`(() => { const f = document.querySelector('.ask-failed'); return f && !document.querySelector('.ask-live') ? f.textContent : null; })()`, 60000, 600);
  const after = (await lastRun(chat3))?.run;
  const users = await js(`document.querySelectorAll('.ask-msg-user').length`);
  check('Retry runs the same message again (new run, question not repeated)', after && after.id > before && users === 1 && !!again,
    `run ${before} -> ${after?.id} (${after?.status}), user msgs=${users}`);
  await shot('07-retried.png');
  skip('Retry ends in an answer', 'LLM unavailable; passed before the Phase 13 merge');

  // Reduced motion, on a staged running answer.
  const r2 = await stageRun(chat3, 'Staged question for the reduced-motion check.');
  await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] });
  const reduced = await waitFor(`(() => { const i = document.querySelector('.ask-pulse i'); return i ? getComputedStyle(i).animationName : null; })()`, 30000, 500);
  check('reduced motion turns the animation off', reduced === 'none', `animation=${reduced}`);
  await send('Emulation.setEmulatedMedia', { features: [] });
  finishRun(chat3, r2, 'Staged answer.');
  skip('page API latency during a long agent run', 'LLM unavailable; idle p95 ~20 ms, during runs p95 19-20 ms before the merge');
}

let code = 1;
try {
  await (NO_LLM ? mainNoLlm() : main());
  code = results.every((r) => r.ok) ? 0 : 1;
} catch (e) {
  console.log('ERROR ' + (e && e.message));
  code = 1;
} finally {
  try { await api('PUT', '/api/config', { values: { reactive_token_budget: '' } }); } catch { /* best effort */ }
  try { child?.kill(); } catch { /* */ }
  await sleep(500);
  try { fs.rmSync(ud, { recursive: true, force: true }); } catch { /* */ }
  console.log(`${results.filter((r) => r.ok).length}/${results.length} checks passed`);
  process.exit(code);
}

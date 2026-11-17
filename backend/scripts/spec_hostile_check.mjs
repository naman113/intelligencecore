// Dev-machine harness (not product code; LATER-03 stays deferred): SPEC-07, the proof that a
// chart-spec dashboard cannot be made to run agent script, load anything off the dashboards origin
// or reach the network, whatever text the agent puts in the spec or the data (T-12-51).
//
// It saves a spec with hostile text in EVERY free-text field and in the data values, opens it as a
// draft (preview token) and as a published version (view token) in headless Chrome over CDP, and
// passes a leg only when all seven oracles hold:
//   1 script inventory   frame scripts hash (sha256-base64) to exactly {prelude, Chart.js, renderer}
//   2 browser issues     zero CSP / Trusted Types issues, zero security log entries
//   3 requests           every request is on the dashboards origin with an allowed path
//   4 sink               zero requests and zero TCP connections on a local sink server
//   5 NetLog             zero occurrences of the per-run nonce host in the browser NetLog
//   6 DOM                text only; hostile strings present as text; Chart.js labels equal the strings
//   7 top frame          unchanged, one page target; frame CSP header has the 18-04 shape
// Never prints tokens, the ops key or query strings (every print path goes through redact()).
//
// Phase 22: --spec-version 2 runs the v2 leg (ic-render-2.0.0): two hostile dashboards that together cover
//   all 17 tile types, each opened as draft and published, plus oracle 8 (wrapper URL hash and Ask link, C-12).
// Usage: node spec_hostile_check.mjs [--spec-version 1|2] [--print-spec] [--nonce <lowercase alnum>] [--keep]
//          [--wait-ms N] [--secrets <file with OPS_KEY=...>]
// Env:   BASE_URL (http://127.0.0.1:9281)  DASH_URL (http://127.0.0.1:9282)  both must be loopback
//        PG_CONTAINER (ic-p28-postgres-1)  OPS_KEY (or --secrets)  CHROME (browser path)
// Exit:  0 pass, 1 fail, 2 usage / unhealthy / no primary person, 3 no browser.
import { spawn, execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';

const argv = process.argv.slice(2);
const HOST_SUFFIX = '.ic-hostile.invalid';
const flagVal = (name) => { const i = argv.indexOf(name); return i >= 0 ? argv[i + 1] : undefined; };

// ---------------------------------------------------------------- the hostile spec (pure)

const NOT_TEXT_FIELDS = [
  'spec_version', 'queries[].id', 'queries[].refresh_minutes', 'layout.rows[][]', 'tiles[].id',
  'tiles[].type', 'tiles[].query', 'tiles[].format', 'tiles[].currency', 'tiles[].grain',
  'tiles[].x', 'tiles[].series', 'tiles[].column', 'tiles[].y[].column', 'tiles[].band.low',
  'tiles[].band.high', 'tiles[].columns[].column', 'tiles[].columns[].format', 'tiles[].compare',
  'tiles[].good_direction', 'measure.name', 'measure.by[]', 'measure.filters[].op',
];

const sqlLit = (s) => "'" + s.replace(/'/g, "''") + "'";

// sink is an origin such as http://127.0.0.1:41234 ; the host:port of it is the short form.
function buildHostile(nonce, sink) {
  const host = nonce + HOST_SUFFIX;
  const hp = new URL(sink).host;
  const P = [
    `<img src=x onerror="fetch('${sink}/a')"> ${host}`,
    `</script><script>fetch('${sink}/b')</script> ${host}`,
    `<a href="javascript:fetch('${sink}/c')">${host}</a>`,
    `<a href="${sink}/d">click</a> ${host}`,
    `<link rel=dns-prefetch href=//${host}>`,
    `<link rel=preconnect href=//${host}>`,
    `<meta http-equiv=refresh content="0;url=//${host}">`,
    `<svg onload=fetch('${sink}/e')> ${host}`,
    `<style>@import url(//${host}/x.css)</style>`,
    `{{constructor.constructor('x')()}} <b>${host}`,
    `"><img src=//${host}/q>`,
    `<div style="background:url(//${host}/z)">${hp}</div>`,
  ];
  const TAGS = ['b', 'i', 'u', 's', 'em', 'q'];
  let n = 0;
  const log = { dom: [], chart: [], dataLabels: [] };
  // A hostile string for a field with this cap; always keeps "<" and the nonce host or the sink.
  function fit(cap, kind) {
    const i = n++;
    let s = P[i % P.length];
    if (s.length > cap) {
      const tag = TAGS[i % TAGS.length];
      s = `<${tag}>${host}`;
      if (s.length > cap) s = `<${tag}>${hp}`;
    }
    s = s.trimEnd();
    log[kind].push(s);
    return s;
  }
  const dataPayloads = [
    `<img src=//${host}/d1 onerror=alert(1)>`,
    `</td><script src=//${host}/d2></script>`,
    `<a href="//${host}/d3">x</a><svg onload=alert(1)>`,
  ];
  log.dataLabels = dataPayloads.slice();
  const sql = 'SELECT ' + sqlLit(dataPayloads[0]) + ' AS label, 10 AS v, 12 AS w, 8 AS lo, 14 AS hi' +
    ' UNION ALL SELECT ' + sqlLit(dataPayloads[1]) + ', 20, 15, 16, 24' +
    ' UNION ALL SELECT ' + sqlLit(dataPayloads[2]) + ', 30, 18, 25, 35';
  const Q = 'hostile_rows';
  const title = (kind = 'dom') => fit(120, kind);
  const note = () => fit(300, 'dom');
  const spec = {
    spec_version: 1,
    title: fit(200, 'dom'),
    subtitle: fit(300, 'dom'),
    queries: [{ id: Q, sql, refresh_minutes: 5 }],
    layout: { rows: [['t_stat', 't_spark'], ['t_line', 't_area'], ['t_bar', 't_stack'], ['t_table'], ['t_text']] },
    tiles: [],
  };
  spec.tiles.push({ id: 't_stat', type: 'stat', title: title(), query: Q, column: 'v', format: 'number',
    compare: 'previous_row', compare_label: fit(60, 'dom'), good_direction: 'up', unit: fit(24, 'chart'), note: note() });
  spec.tiles.push({ id: 't_spark', type: 'sparkline', title: title(), query: Q, x: 'label',
    y: [{ column: 'v', label: fit(60, 'chart') }], note: note() });
  spec.tiles.push({ id: 't_line', type: 'line', title: title(), query: Q, x: 'label',
    y: [{ column: 'v', label: fit(60, 'chart') }, { column: 'w', label: fit(60, 'chart'), dashed: true }],
    band: { low: 'lo', high: 'hi', label: fit(60, 'chart') },
    x_label: fit(60, 'chart'), y_label: fit(60, 'chart'), unit: fit(24, 'chart'), note: note() });
  spec.tiles.push({ id: 't_area', type: 'area', title: title(), query: Q, x: 'label',
    y: [{ column: 'v', label: fit(60, 'chart') }], band: { low: 'lo', high: 'hi', label: fit(60, 'chart') },
    x_label: fit(60, 'chart'), y_label: fit(60, 'chart'), note: note() });
  spec.tiles.push({ id: 't_bar', type: 'bar', title: title(), query: Q, x: 'label',
    y: [{ column: 'v', label: fit(60, 'chart') }], x_label: fit(60, 'chart'), y_label: fit(60, 'chart'), note: note() });
  spec.tiles.push({ id: 't_stack', type: 'stacked_bar', title: title(), query: Q, x: 'label',
    y: [{ column: 'v', label: fit(60, 'chart') }, { column: 'w', label: fit(60, 'chart') }],
    x_label: fit(60, 'chart'), y_label: fit(60, 'chart'), note: note() });
  spec.tiles.push({ id: 't_table', type: 'table', title: title(), query: Q, max_rows: 10,
    columns: [{ column: 'label', label: fit(60, 'dom') }, { column: 'v', label: fit(60, 'dom'), unit: fit(24, 'chart') },
      { column: 'w', label: fit(60, 'dom') }], note: note() });
  spec.tiles.push({ id: 't_text', type: 'text', title: title(), text: fit(900, 'dom') + '\n' + fit(900, 'dom'), note: note() });
  return { spec, expect: log, queryId: Q };
}

// ---------------------------------------------------------------- the hostile v2 specs (pure)

// Phase 22 (DCAP-01): spec version 2 has 17 tile types, 14 of them charts, and one spec holds at
// most 12 chart tiles, so the hostile v2 set is two dashboards (parts) that together cover all 17.
const V2_TYPES = ['stat', 'line', 'area', 'bar', 'stacked_bar', 'sparkline', 'text', 'table', 'combo',
  'paired_bar', 'aging', 'bullet', 'waterfall', 'calendar', 'donut', 'ranked_bar', 'funnel'];

const NOT_TEXT_FIELDS_V2 = [
  'spec_version', 'number_locale', 'date_control.default', 'date_control.presets[]',
  'date_control.horizon_days', 'date_control.fy_start_month', 'filters[].id', 'filters[].dimension',
  'queries[].id', 'queries[].refresh_minutes', 'sections[].tiles[]', 'tiles[].id', 'tiles[].type',
  'tiles[].width', 'tiles[].hidden', 'tiles[].query', 'tiles[].format', 'tiles[].currency',
  'tiles[].date', 'tiles[].grain', 'tiles[].x', 'tiles[].series', 'tiles[].column', 'tiles[].y[].column',
  'tiles[].y[].dashed', 'tiles[].y[].tone', 'tiles[].y[].as', 'tiles[].band.low', 'tiles[].band.high',
  'tiles[].target.value', 'tiles[].target.column', 'tiles[].columns[].column', 'tiles[].columns[].format',
  'tiles[].columns[].currency', 'tiles[].compare', 'tiles[].comparison', 'tiles[].good_direction',
  'tiles[].pick', 'tiles[].ghost', 'tiles[].bands[]', 'tiles[].ends', 'tiles[].top', 'tiles[].other',
  'tiles[].max_rows', 'tiles[].totals', 'tiles[].sort.column', 'tiles[].sort.dir',
];

const TEXT_KEYS_V2 = new Set(['title', 'subtitle', 'description', 'label', 'note', 'text', 'unit',
  'x_label', 'y_label', 'compare_label']);

// Every string leaf under a free-text key, as a generalised path (tiles are named by type).
function textFieldPaths(spec) {
  const out = new Set();
  const walk = (node, p) => {
    if (Array.isArray(node)) { for (const v of node) walk(v, p + '[]'); return; }
    if (node && typeof node === 'object') {
      for (const [k, v] of Object.entries(node)) walk(v, p + '.' + k);
      return;
    }
    const last = p.slice(p.lastIndexOf('.') + 1).replace(/\[\]$/, '');
    if (typeof node === 'string' && TEXT_KEYS_V2.has(last)) out.add(p);
  };
  for (const [k, v] of Object.entries(spec)) {
    if (k === 'queries') continue;
    if (k === 'tiles') for (const t of v) walk(t, `tiles[${t.type}]`);
    else walk(v, k);
  }
  return [...out].sort();
}

function buildHostileV2(nonce, sink) {
  const host = nonce + HOST_SUFFIX;
  const hp = new URL(sink).host;
  const P = [
    `<img src=x onerror="fetch('${sink}/a')"> ${host}`,
    `</script><script>fetch('${sink}/b')</script> ${host}`,
    `<a href="javascript:fetch('${sink}/c')">${host}</a>`,
    `<a href="${sink}/d">click</a> ${host}`,
    `<link rel=dns-prefetch href=//${host}>`,
    `<meta http-equiv=refresh content="0;url=//${host}">`,
    `<svg onload=fetch('${sink}/e')> ${host}`,
    `<style>@import url(//${host}/x.css)</style>`,
    `{{constructor.constructor('x')()}} <b>${host}`,
    `"><img src=//${host}/q>`,
    `<div style="background:url(//${host}/z)">${hp}</div>`,
  ];
  const TAGS = ['b', 'i', 'u', 's', 'em', 'q'];
  let n = 0;
  let log = null;
  // A hostile string for a field with this cap; always keeps "<" and the nonce host or the sink.
  function fit(cap, kind) {
    const i = n++;
    let s = P[i % P.length];
    if (s.length > cap) {
      const tag = TAGS[i % TAGS.length];
      s = `<${tag}>${host}`;
      if (s.length > cap) s = `<${tag}>${hp}`;
    }
    s = s.trimEnd();
    log[kind].push(s);
    return s;
  }
  const dataPayloads = [
    `<img src=//${host}/d1 onerror=alert(1)>`,
    `</td><script src=//${host}/d2></script>`,
    `<a href="//${host}/d3">x</a><svg onload=alert(1)>`,
  ];
  const sqlRows = 'SELECT ' + sqlLit(dataPayloads[0]) + ' AS label, 10 AS v, 12 AS w, 8 AS lo, 14 AS hi' +
    ' UNION ALL SELECT ' + sqlLit(dataPayloads[1]) + ', 20, 15, 16, 24' +
    ' UNION ALL SELECT ' + sqlLit(dataPayloads[2]) + ', 30, 18, 25, 35';
  const dayRows = [];
  for (let d = 1; d <= 9; d++) dayRows.push(`SELECT '2026-10-0${d}' AS day, ${d * 3} AS v`);
  const sqlDays = dayRows.join(' UNION ALL ');
  const Q = 'hostile_rows';
  const QD = 'hostile_days';
  const ser = (col, extra = {}) => ({ column: col, label: fit(60, 'chart'), ...extra });
  const common = (id, type, extra = {}) => ({ id, type, title: fit(120, 'dom'), note: fit(300, 'dom'), query: Q, ...extra });
  const chartX = { x: 'label' };
  const axes = () => ({ x_label: fit(60, 'chart'), y_label: fit(60, 'chart') });
  const target = () => ({ value: 18, label: fit(60, 'chart') });
  const T = {
    stat: () => common('t_stat', 'stat', { column: 'v', format: 'number', compare: 'previous_row', compare_label: fit(60, 'dom'),
      good_direction: 'up', unit: fit(24, 'chart'), target: target() }),
    sparkline: () => common('t_spark', 'sparkline', { ...chartX, y: [ser('v')] }),
    line: () => common('t_line', 'line', { ...chartX, y: [ser('v'), ser('w', { dashed: true })],
      band: { low: 'lo', high: 'hi', label: fit(60, 'chart') }, ...axes(), unit: fit(24, 'chart'), target: target() }),
    area: () => common('t_area', 'area', { ...chartX, y: [ser('v')], band: { low: 'lo', high: 'hi', label: fit(60, 'chart') }, ...axes() }),
    bar: () => common('t_bar', 'bar', { ...chartX, y: [ser('v')], ...axes(), target: target() }),
    stacked_bar: () => common('t_stack', 'stacked_bar', { ...chartX, y: [ser('v'), ser('w')], ...axes() }),
    combo: () => common('t_combo', 'combo', { ...chartX, y: [ser('v', { as: 'bar' }), ser('w', { as: 'line' })], ...axes(),
      unit: fit(24, 'chart'), target: target() }),
    paired_bar: () => common('t_paired', 'paired_bar', { ...chartX, y: [ser('v', { tone: 'inward' }), ser('w', { tone: 'outward' })], ...axes() }),
    aging: () => common('t_aging', 'aging', { ...chartX, y: [ser('v')], ...axes() }),
    bullet: () => common('t_bullet', 'bullet', { column: 'v', target: target(), bands: [10, 20, 30] }),
    waterfall: () => common('t_water', 'waterfall', { ...chartX, y: [ser('v')], ...axes() }),
    calendar: () => common('t_cal', 'calendar', { query: QD, x: 'day', y: [ser('v')] }),
    donut: () => common('t_donut', 'donut', { ...chartX, y: [ser('v')] }),
    ranked_bar: () => common('t_ranked', 'ranked_bar', { ...chartX, y: [ser('v')], top: 5, other: false, ...axes() }),
    funnel: () => common('t_funnel', 'funnel', { ...chartX, y: [ser('v')] }),
    table: () => common('t_table', 'table', { max_rows: 10, totals: true,
      columns: [{ column: 'label', label: fit(60, 'dom') }, { column: 'v', label: fit(60, 'dom'), unit: fit(24, 'chart') },
        { column: 'w', label: fit(60, 'dom') }], sort: { column: 'v', dir: 'desc' } }),
    // the v2 renderer draws a text tile's text and note only, never its title: the title is listed as chart text
    // (allowed where the renderer reuses spec strings) and not as a string that must appear in the DOM
    text: () => ({ id: 't_text', type: 'text', title: fit(120, 'chart'), text: fit(900, 'dom') + '\n' + fit(900, 'dom'), note: fit(300, 'dom') }),
  };
  const mk = (name, order, queryIds) => {
    log = { dom: [], chart: [], dataLabels: dataPayloads.slice(), seriesLabels: [] };
    const spec = {
      spec_version: 2,
      title: fit(200, 'dom'),
      subtitle: fit(300, 'dom'),
      number_locale: 'en-US',
      date_control: { default: 'last_7_days', presets: null, horizon_days: 400, fy_start_month: 4 },
      filters: [{ id: 'region', dimension: 'orders.region', label: fit(60, 'chart') }],
      queries: [{ id: Q, sql: sqlRows, refresh_minutes: 5 }, { id: QD, sql: sqlDays, refresh_minutes: 5 }]
        .filter((q) => queryIds.includes(q.id)),
      sections: [],
      tiles: order.map((t) => T[t]()),
    };
    const ids = spec.tiles.map((t) => t.id);
    const half = Math.ceil(ids.length / 2);
    spec.sections = [
      { title: fit(60, 'dom'), description: fit(200, 'dom'), tiles: ids.slice(0, half) },
      { title: fit(60, 'dom'), description: fit(200, 'dom'), tiles: ids.slice(half) },
    ];
    for (const t of spec.tiles) {
      if (['line', 'area', 'bar', 'stacked_bar'].includes(t.type)) for (const y of t.y) log.seriesLabels.push(y.label);
    }
    return { name, spec, expect: log, queryIds };
  };
  const parts = [
    mk('a', ['stat', 'sparkline', 'line', 'area', 'bar', 'stacked_bar', 'combo', 'paired_bar', 'aging',
      'bullet', 'waterfall', 'calendar', 'donut', 'table', 'text'], [Q, QD]),
    mk('b', ['stat', 'ranked_bar', 'funnel', 'text'], [Q]),
  ];
  const covered = [...new Set(parts.flatMap((p) => p.spec.tiles.map((t) => t.type)))].sort();
  const fieldPaths = [...new Set(parts.flatMap((p) => textFieldPaths(p.spec)))].sort();
  return { parts, fieldPaths, covered };
}

const SPEC_VERSION = Number(flagVal('--spec-version') || 1);
if (SPEC_VERSION !== 1 && SPEC_VERSION !== 2) { process.stderr.write('REFUSED: --spec-version must be 1 or 2\n'); process.exit(2); }

if (argv.includes('--print-spec')) {
  const nonce = flagVal('--nonce') || 'testnonce1';
  let printed;
  if (SPEC_VERSION === 2) {
    const h2 = buildHostileV2(nonce, 'http://127.0.0.1:1');
    printed = { parts: h2.parts.map((p) => ({ name: p.name, spec: p.spec, expect: p.expect })), field_paths: h2.fieldPaths,
      covered_types: h2.covered, not_text_fields: NOT_TEXT_FIELDS_V2 };
  } else { printed = buildHostile(nonce, 'http://127.0.0.1:1').spec; }
  process.stdout.write(JSON.stringify(printed) + '\n');
  process.exit(0);
}

// ---------------------------------------------------------------- config, guards, redaction

const BASE = (process.env.BASE_URL || 'http://127.0.0.1:9281').replace(/\/$/, '');
const DASH = (process.env.DASH_URL || 'http://127.0.0.1:9282').replace(/\/$/, '');
const PG = process.env.PG_CONTAINER || 'ic-p28-postgres-1';
const KEEP = argv.includes('--keep');
const WAIT_MS = Number(flagVal('--wait-ms')) || 180000;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const secrets = new Set();
const NONCE_RAW = flagVal('--nonce');

function redact(s) {
  let t = String(s).replace(/([?&](?:t|p)=)[^&\s"']+/g, '$1REDACTED');
  for (const v of secrets) if (v) t = t.split(v).join('REDACTED');
  return t;
}
const say = (s) => console.log(redact(s));
const warn = (s) => console.error(redact(s));
function usage(msg) { warn(msg); process.exit(2); }

for (const u of [BASE, DASH]) {
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
if (!KEY) usage('REFUSED: no OPS_KEY (env or --secrets <file>)');
secrets.add(KEY);

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

function httpGet(url, headers = {}) {
  return new Promise((resolve, reject) => {
    http.get(url, { headers }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString('utf8') }));
    }).on('error', reject);
  });
}

const sha256b64 = (s) => 'sha256-' + crypto.createHash('sha256').update(s, 'utf8').digest('base64');

// ---------------------------------------------------------------- vendored pins

const vendor = JSON.parse(fs.readFileSync(new URL('../../dashboards/vendor.json', import.meta.url), 'utf8'));
const lib = (name) => vendor.libraries.find((l) => l.name === name);
const LIB_HASH = lib('chart.js').files.find((f) => f.path === lib('chart.js').entry).csp_hash;
const RENDER_HASH = lib('ic-render').files.find((f) => f.path === lib('ic-render').entry).csp_hash;
const libV = (name, ver) => vendor.libraries.find((l) => l.name === name && l.version === ver);
const entryHash = (l) => l.files.find((f) => f.path === l.entry).csp_hash;
const V2_LIB_HASH = entryHash(libV('ic-chartlib', '1.0.0'));
const V2_RENDER_HASH = entryHash(libV('ic-render', '2.0.0'));
const HOSTILE_RE = /ic-hostile|127\.0\.0\.1|[<>]/;

// ---------------------------------------------------------------- sink and browser

let hits = 0; let conns = 0;
const sink = http.createServer((req, res) => { hits++; res.statusCode = 204; res.end(); });
sink.on('connection', (sock) => { conns++; sock.on('error', () => {}); });
await new Promise((r) => sink.listen(0, '127.0.0.1', r));
const SINK = `http://127.0.0.1:${sink.address().port}`;

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

// One fresh browser per leg so each leg has its own NetLog. Returns the oracle results.
async function runLeg(browser, name, pageUrl, ctx) {
  const ud = fs.mkdtempSync(path.join(os.tmpdir(), 'ic-spec-'));
  const netlog = path.join(ud, 'netlog.json');
  const child = spawn(browser, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--remote-debugging-port=0', `--user-data-dir=${ud}`, `--log-net-log=${netlog}`,
    '--net-log-capture-mode=IncludeSensitive', '--disable-background-networking',
    '--disable-features=Translate,OptimizationHints', 'about:blank'], { stdio: 'ignore' });
  const exited = new Promise((r) => child.on('exit', r));
  const R = { name, oracles: {} };
  const setO = (k, ok, detail) => { R.oracles[k] = { ok, detail: detail || '' }; };
  let ws; const pending = new Map(); let nextId = 0; const listeners = [];
  const send = (method, params = {}, sessionId) => new Promise((res) => {
    const id = ++nextId; pending.set(id, res);
    ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
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

    // observation state
    const requests = []; const issues = []; const scripts = new Map(); const frameSessions = new Set();
    let dataResponses = 0; let pageSession = null;
    listeners.push((m) => {
      const sid = m.sessionId;
      if (m.method === 'Network.requestWillBeSent') requests.push(m.params.request.url);
      else if (m.method === 'Network.responseReceived' && /\/data\/[a-z0-9_]+$/.test(m.params.response.url.split('?')[0])) dataResponses++;
      else if (m.method === 'Audits.issueAdded') {
        if (m.params.issue.code === 'ContentSecurityPolicyIssue') issues.push('csp-issue ' + JSON.stringify(m.params.issue.details?.contentSecurityPolicyIssueDetails?.violatedDirective || ''));
      } else if (m.method === 'Log.entryAdded') {
        if (m.params.entry.source === 'security') issues.push('security-log ' + String(m.params.entry.text).slice(0, 120));
      } else if (m.method === 'Debugger.scriptParsed') {
        if (!scripts.has(sid)) scripts.set(sid, []);
        scripts.get(sid).push(m.params.scriptId);
      } else if (m.method === 'Target.attachedToTarget' && sid === pageSession && (m.params.targetInfo || {}).type === 'iframe') {
        const cs = m.params.sessionId; frameSessions.add(cs);
        (async () => {
          for (const dom of ['Network.enable', 'Debugger.enable', 'Runtime.enable', 'Log.enable', 'Audits.enable', 'Page.enable']) await send(dom, {}, cs);
          await send('Runtime.runIfWaitingForDebugger', {}, cs);
        })();
      }
    });

    await send('Target.setDiscoverTargets', { discover: true });
    const tl = await send('Target.getTargets');
    const pageT = (tl.result?.targetInfos || []).find((t) => t.type === 'page');
    const a = await send('Target.attachToTarget', { targetId: pageT.targetId, flatten: true });
    pageSession = a.result.sessionId;
    for (const dom of ['Network.enable', 'Runtime.enable', 'Log.enable', 'Audits.enable', 'Page.enable']) await send(dom, {}, pageSession);
    await send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: true, flatten: true }, pageSession);
    // v2 mounts charts when they scroll into view: a tall viewport puts every tile on screen
    if (ctx.v2) await send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 20000, deviceScaleFactor: 1, mobile: false }, pageSession);
    hits = 0; conns = 0;
    await send('Page.navigate', { url: pageUrl }, pageSession);
    const end = Date.now() + 25000;
    while (Date.now() < end && dataResponses < ctx.nQueries) await sleep(500);
    await sleep(3500);

    // oracle 1: script inventory (before any Runtime.evaluate)
    const frameSid = [...frameSessions][0];
    if (!frameSid) setO('1 scripts', false, 'the spec frame was not an out-of-process frame target');
    else {
      const hashes = [];
      for (const id of scripts.get(frameSid) || []) {
        const r = await send('Debugger.getScriptSource', { scriptId: id }, frameSid);
        if (r.result) hashes.push(sha256b64(r.result.scriptSource));
      }
      const want = new Set([ctx.preludeHash, ctx.libHash || LIB_HASH, ctx.renderHash || RENDER_HASH]);
      const got = new Set(hashes);
      const same = got.size === want.size && [...want].every((h) => got.has(h)) && hashes.length === 3;
      setO('1 scripts', same, same ? '' : `parsed ${hashes.length} scripts, ${got.size} distinct, unexpected=${[...got].filter((h) => !want.has(h)).length} missing=${[...want].filter((h) => !got.has(h)).length}`);
    }
    // oracle 2: browser issues
    setO('2 issues', issues.length === 0, issues.slice(0, 3).join(' | '));
    // oracle 3: requests
    const bad = [];
    const slugRe = ctx.slug.replace(/[^a-z0-9-]/g, '');
    const okPath = ctx.v2
      ? new RegExp(`^/d/${slugRe}/(v/\\d+/)?(frame|spec|data/[a-z][a-z0-9_]*)?$|^/d/_lib/ic-chartlib-1\\.0\\.0/chartlib\\.min\\.js$|^/d/_lib/ic-render-2\\.0\\.0/render\\.js$`)
      : new RegExp(`^/d/${slugRe}/(v/\\d+/)?(frame|spec|data/[a-z][a-z0-9_]*)?$|^/d/_lib/chart\\.js-4\\.5\\.1/chart\\.umd\\.min\\.js$|^/d/_lib/ic-render-1\\.0\\.0/render\\.js$`);
    for (const u of requests) {
      if (u.startsWith('about:')) continue;
      let p; try { p = new URL(u); } catch { bad.push('unparsable'); continue; }
      if (p.origin !== DASH || !okPath.test(p.pathname)) bad.push(p.origin + p.pathname);
    }
    setO('3 requests', bad.length === 0 && requests.length > 0, bad.length ? 'off-contract: ' + bad.slice(0, 3).join(', ') : `${requests.length} requests`);
    // oracle 4: sink
    setO('4 sink', hits === 0 && conns === 0, `requests=${hits} connections=${conns}`);

    // oracle 6 and 7: DOM and top frame
    const domExpr = `(() => {
      const d = document; const nonce = ${JSON.stringify(ctx.nonce.toLowerCase())}; const v2 = ${ctx.v2 ? 'true' : 'false'};
      const forbidden = d.querySelectorAll('a,img,link,iframe,object,embed,meta[http-equiv],base,form').length;
      let attrHit = 0; let onAttr = 0;
      for (const el of d.querySelectorAll('*')) for (const at of el.attributes) {
        // v2 renderer: aria-label (screen-reader summaries and control labels) and data-ic-row (a table row's
        // values for drill) are set through DOM properties and carry text only; every other attribute, and every attribute on the v1 legs, must be nonce-free
        if (String(at.value).toLowerCase().includes(nonce) && !(v2 && (at.name === 'aria-label' || at.name === 'data-ic-row'))) attrHit++;
        if (/^on/i.test(at.name)) onAttr++;
      }
      const inst = (typeof Chart === 'function' && Chart.instances) ? Object.values(Chart.instances) : [];
      return { scripts: d.scripts.length, forbidden, attrHit, onAttr, text: d.body.textContent,
        charts: inst.map((c) => ({ labels: c.data.labels || [], ds: c.data.datasets.map((x) => x.label) })),
        tiles: d.querySelectorAll('.ic-tile').length, fail: d.querySelectorAll('.ic-fail').length };
    })()`;
    const dr = frameSid ? await send('Runtime.evaluate', { expression: domExpr, returnByValue: true }, frameSid) : null;
    const dv = dr?.result?.result?.value;
    if (!dv) setO('6 dom', false, 'could not evaluate in the frame');
    else {
      const probs = [];
      if (dv.scripts !== 3) probs.push(`document.scripts=${dv.scripts}`);
      if (dv.forbidden) probs.push(`forbidden elements=${dv.forbidden}`);
      if (dv.attrHit) probs.push(`attributes carrying the nonce=${dv.attrHit}`);
      if (dv.onAttr) probs.push(`on* attributes=${dv.onAttr}`);
      if (dv.fail) probs.push('ic-fail rendered');
      if (dv.tiles < ctx.nTiles) probs.push(`tiles=${dv.tiles}`);
      const lines = ctx.expect.dom.flatMap((s) => s.split('\n'));
      for (const s of lines) if (!dv.text.includes(s)) probs.push('missing text: ' + s.slice(0, 40));
      for (const s of ctx.expect.dataLabels) if (!dv.text.includes(s)) probs.push('missing data text: ' + s.slice(0, 40));
      const dataSet = new Set(ctx.expect.dataLabels);
      const known = new Set([...ctx.expect.dom, ...ctx.expect.chart, ...ctx.expect.dataLabels, 'label', 'v', 'w', 'lo', 'hi', 'low', '']);  // 'low' = the renderer's own band-base dataset
      if (!dv.charts.length) probs.push('no charts');
      let seenSeries = new Set();
      for (const c of dv.charts) {
        // v2 charts add the renderer's own words (Other, Previous period, ...): only hostile-looking labels must be exact
        // v2 labels may be the spec's own strings (series, target, axis) or a data label plus the renderer's share suffix (donut, funnel)
        const v2Label = (l) => ctx.v2 && (known.has(l) || ctx.expect.dataLabels.some((d) => String(l).startsWith(d) && /^ \([0-9.,]+%( of first)?\)$/.test(String(l).slice(d.length))));
        for (const l of c.labels) if (!dataSet.has(l) && !v2Label(l) && (!ctx.v2 || HOSTILE_RE.test(String(l)))) probs.push('chart label not from the data');
        for (const l of c.ds) { if (!known.has(l) && (!ctx.v2 || HOSTILE_RE.test(String(l)))) probs.push('unexpected dataset label'); seenSeries.add(l); }
      }
      for (const s of ctx.expect.seriesLabels) if (!seenSeries.has(s)) probs.push('series label not in a chart: ' + s.slice(0, 30));
      setO('6 dom', probs.length === 0, [...new Set(probs)].slice(0, 4).join('; '));
    }
    const top = await send('Runtime.evaluate', { expression: 'location.href', returnByValue: true }, pageSession);
    const topUrl = top.result?.result?.value || '';
    const tg = await send('Target.getTargets');
    const pages = (tg.result?.targetInfos || []).filter((t) => t.type === 'page').length;
    const frameRes = await httpGet(ctx.frameUrl, { 'sec-fetch-dest': 'iframe' });
    const csp = String(frameRes.headers['content-security-policy'] || '');
    const probs7 = [];
    if (!topUrl.startsWith(DASH + '/')) probs7.push('top page left the dashboards origin');
    if (pages !== 1) probs7.push(`page targets=${pages}`);
    if (csp !== ctx.expectCsp) probs7.push('frame CSP differs from the 18-04 shape: ' + csp.slice(0, 400));
    setO('7 top+csp', probs7.length === 0, probs7.join('; '));

    // oracle 8 (v2): drive the date button, a preset, the info buttons and a stat value, then read the wrapper
    if (ctx.v2 && frameSid) {
      const clickExpr = `(() => {
        const d = document; const out = { range: 0, preset: 0, info: 0, stat: 0 };
        const rb = d.querySelector('button.ic-range'); if (rb) { rb.click(); out.range = 1; }
        const pb = d.querySelector('button.ic-preset:not(.ic-on)'); if (pb) { pb.click(); out.preset = 1; }
        for (const b of d.querySelectorAll('button.ic-info')) { b.click(); out.info++; }
        const sv = d.querySelector('.ic-stat-value'); if (sv) { sv.click(); out.stat = 1; }
        return out;
      })()`;
      const cr = await send('Runtime.evaluate', { expression: clickExpr, returnByValue: true }, frameSid);
      await sleep(1200);
      const topExpr = `(() => { const a = document.getElementById('ic-ask');
        return { hash: location.hash, base: document.body.getAttribute('data-ask-base') || '',
          ask: a ? { hidden: a.hidden, href: a.getAttribute('href') || '', kids: a.querySelectorAll('*').length, text: a.textContent } : null,
          extra: document.querySelectorAll('img,link,object,embed,meta[http-equiv],base,form').length }; })()`;
      const tr = await send('Runtime.evaluate', { expression: topExpr, returnByValue: true }, pageSession);
      const tv = tr.result?.result?.value;
      const clicks = cr.result?.result?.value;
      const p8 = [];
      if (!tv || !clicks) p8.push('could not drive the page');
      else {
        if (!clicks.range || !clicks.preset) p8.push(`date control not driven range=${clicks.range} preset=${clicks.preset}`);
        if (!/^#[A-Za-z0-9_=,.:;%~-]{0,600}$/.test(tv.hash)) p8.push('top URL hash outside the C-12 grammar');
        const configured = (process.env.APP_PUBLIC_URL || tv.base || '').replace(/\/$/, '');
        if (process.env.APP_PUBLIC_URL && tv.base && tv.base !== configured) p8.push('wrapper data-ask-base differs from APP_PUBLIC_URL');
        const a = tv.ask;
        if (a && !a.hidden) {
          if (!configured) p8.push('Ask link shown with no app_public_url configured');
          else if (!a.href.startsWith(configured + '/?ask=')) p8.push('Ask link href does not start with app_public_url');
          if (a.kids) p8.push('Ask link holds elements, not text');
        }
        if (tv.extra) p8.push(`wrapper holds ${tv.extra} forbidden elements`);
      }
      setO('8 wrapper', p8.length === 0, p8.join('; ') || `hash=${tv ? tv.hash.length : 0} chars, ask ${tv && tv.ask && !tv.ask.hidden ? 'shown' : 'absent'}`);
    }

    // close the browser, wait for exit, then read the NetLog (oracle 5)
    await send('Browser.close');
    try { ws.close(); } catch { /* ignore */ }
    await Promise.race([exited, sleep(15000)]);
    if (child.exitCode === null) child.kill();
    await sleep(500);
    let nl = '';
    try { nl = fs.readFileSync(netlog, 'utf8').toLowerCase(); } catch { /* handled below */ }
    if (!nl) setO('5 netlog', false, 'NetLog file missing or empty');
    else {
      // the nonce is also part of this run's own dashboard slug (on-origin /d/<slug>/ URLs); those are expected
      const slugLc = String(ctx.slug).toLowerCase();
      const count = nl.split(slugLc).join('').split(ctx.nonce.toLowerCase()).length - 1;
      setO('5 netlog', count === 0, `${nl.length} bytes, nonce occurrences=${count}`);
    }
  } catch (e) {
    R.error = String(e.message || e);
    setO('run', false, R.error);
  } finally {
    try { ws?.close(); } catch { /* ignore */ }
    if (child.exitCode === null) child.kill();
    await sleep(300);
    try { fs.rmSync(ud, { recursive: true, force: true }); } catch { /* ignore */ }
  }
  R.ok = Object.values(R.oracles).every((o) => o.ok) && Object.keys(R.oracles).length >= (ctx.v2 ? 8 : 7);
  for (const [k, o] of Object.entries(R.oracles)) say(`${o.ok ? 'PASS' : 'FAIL'} ${name} oracle ${k} ${o.detail}`);
  say(`${R.ok ? 'PASS' : 'FAIL'} leg ${name}`);
  return R;
}

// ---------------------------------------------------------------- main

let exitCode = 1; let slug = ''; const slugs = [];
try {
  const nonce = NONCE_RAW || crypto.randomBytes(6).toString('hex');
  if (!/^[a-z0-9]{6,40}$/.test(nonce)) usage('REFUSED: --nonce must be 6-40 lowercase letters or digits');
  const h = await fetch(`${BASE}/health`).catch(() => null);
  if (!h || h.status !== 200) usage(`unhealthy: ${BASE}/health`);
  const browser = findBrowser();
  if (!browser) { say(JSON.stringify({ skip: 'no Chrome/Edge found' })); process.exit(3); }

  const V2 = SPEC_VERSION === 2;
  const jobs = V2
    ? buildHostileV2(nonce, SINK).parts.map((p) => ({ spec: p.spec, expect: p.expect, tag: p.name + '-', slug: `spechostile-${nonce}-${p.name}` }))
    : (() => {
      const h = buildHostile(nonce, SINK);
      h.expect.seriesLabels = h.spec.tiles.filter((t) => ['line', 'area', 'bar', 'stacked_bar'].includes(t.type))
        .flatMap((t) => t.y.map((y) => y.label));
      return [{ spec: h.spec, expect: h.expect, tag: '', slug: 'spechostile-' + nonce }];
    })();
  const queryId = 'hostile_rows';
  const legs = [];
  let person = '';
  async function context(frameUrl) {
    const fr = await httpGet(frameUrl, { 'sec-fetch-dest': 'iframe' });
    if (fr.status !== 200) throw new Error(`frame HTTP ${fr.status}`);
    const inline = [...fr.body.matchAll(/<script>([\s\S]*?)<\/script>/g)];
    const style = fr.body.match(/<style>([\s\S]*?)<\/style>/);
    if (!inline.length || !style) throw new Error('frame has no inline prelude or stylesheet');
    const preludeHash = sha256b64(inline[0][1]);
    const cssHash = sha256b64(style[1]);
    const hostHdr = new URL(DASH).host;
    const libHash = V2 ? V2_LIB_HASH : LIB_HASH;
    const renderHash = V2 ? V2_RENDER_HASH : RENDER_HASH;
    const expectCsp = `default-src 'none'; script-src '${preludeHash}' '${libHash}' '${renderHash}'; style-src '${cssHash}'; ` +
      `connect-src 'self' http://${hostHdr} https://${hostHdr}; base-uri 'none'; form-action 'none'; ` +
      "require-trusted-types-for 'script'; trusted-types 'none'; frame-ancestors 'self'; sandbox allow-scripts";
    return { preludeHash, expectCsp, libHash, renderHash };
  }
  for (const job of jobs) {
    const { spec, expect } = job;
    slug = job.slug;
    slugs.push(slug);
    const sv = await api('POST', `/api/dashboards/${slug}/versions`, { spec, change_note: 'SPEC-07 hostile check' + (V2 ? ' v2' : '') });
    if (sv.status !== 200) usage(`save failed: HTTP ${sv.status} ${sv.text.slice(0, 300)}`);
    const version = sv.json.version;

    // wait for every snapshot to refresh
    const nQueries = spec.queries.length;
    const until = Date.now() + WAIT_MS; let ready = false; let lastErr = '';
    while (Date.now() < until && !ready) {
      const vr = await api('GET', `/api/dashboards/${slug}/versions/${version}`);
      const snaps = vr.json?.snapshots || [];
      lastErr = snaps.map((s) => s.error).filter(Boolean).join('; ');
      ready = snaps.length >= nQueries && snaps.every((s) => s.refreshed_at && !s.error);
      if (!ready) await sleep(3000);
    }
    if (!ready) { say(`FAIL snapshots did not refresh in ${WAIT_MS} ms ${lastErr.slice(0, 200)}`); throw Object.assign(new Error('snapshots'), { code: 1 }); }

    const pv = await api('POST', `/api/dashboards/${slug}/versions/${version}/preview`, { ttl_minutes: 30 });
    const ptok = pv.json?.preview_token;
    if (pv.status !== 200 || !ptok) usage(`preview failed: HTTP ${pv.status} ${pv.text.slice(0, 200)}`);
    secrets.add(ptok);
    const draftBase = `/d/${slug}/v/${version}/`;
    const base = { nonce, slug, expect, nQueries, nTiles: spec.tiles.length, v2: V2 };
    const dframe = `${DASH}${draftBase}frame?p=${ptok}`;
    legs.push(await runLeg(browser, job.tag + 'draft', `${DASH}${draftBase}?p=${ptok}`, { ...base, frameUrl: dframe, ...(await context(dframe)) }));

    if (!person) person = psql('SELECT id FROM people WHERE is_primary AND active LIMIT 1').trim();
    if (!person) usage('no primary active person: create one before running the published leg');
    const pub = await api('POST', `/api/dashboards/${slug}/publish`, { version, decided_by: Number(person) });
    if (pub.status !== 200) usage(`publish failed: HTTP ${pub.status} ${pub.text.slice(0, 200)}`);
    const dash = await api('GET', `/api/dashboards/${slug}`);
    const vt = dash.json?.dashboard?.view_token;
    if (!vt) usage('no view token after publish');
    secrets.add(vt);
    const pframe = `${DASH}/d/${slug}/frame?t=${vt}`;
    legs.push(await runLeg(browser, job.tag + 'published', `${DASH}/d/${slug}/?t=${vt}`, { ...base, frameUrl: pframe, ...(await context(pframe)) }));
  }

  const ok = legs.every((l) => l.ok);
  say(JSON.stringify({ ok, legs: legs.map((l) => ({ name: l.name, ok: l.ok, oracles: Object.fromEntries(Object.entries(l.oracles).map(([k, o]) => [k, o.ok])) })),
    not_text_fields: SPEC_VERSION === 2 ? NOT_TEXT_FIELDS_V2 : NOT_TEXT_FIELDS, query: queryId, slug: slugs.join(',') }));
  exitCode = ok ? 0 : 1;
} catch (e) {
  if (e && e.code === 1) exitCode = 1;
  else { warn('spec hostile check failed: ' + (e && e.message ? e.message : e)); exitCode = 2; }
} finally {
  try { if (!KEEP) for (const sl of slugs) psql(`DELETE FROM dashboards WHERE slug = '${sl.replace(/[^a-z0-9-]/g, '')}'`); } catch { /* ignore */ }
  sink.close();
}
process.exit(exitCode);

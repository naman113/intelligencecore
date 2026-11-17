/*
 * Intelligence Corner chart-spec renderer 1.0.0
 * Engineer-written plumbing. Never edited in place after release: any byte change
 * is a new ic-render-<version> directory (old directories are kept because
 * published dashboard versions reference them).
 * Draws a render-form spec from ./spec and ./data/<qid> with Chart.js. Every agent
 * or data string reaches the page through textContent only.
 */
(function (root) {
  'use strict';

  var PALETTE = ['#2B7FD4', '#5BA0E0', '#164A82', '#A0AEC0', '#718096', '#4A5568'];
  var GOLD = '#D4952B';
  var GOOD = '#3FB27F';
  var MUTED = '#A0AEC0';
  var DASH = '\u2013';
  var MAX_SERIES = 12;
  var ISO_RE = /^(\d{4})-(\d{2})-(\d{2})/;
  var QID_RE = /^[a-z][a-z0-9_]{0,47}$/;

  function toNum(v) {
    if (typeof v === 'number') { return isFinite(v) ? v : null; }
    if (typeof v === 'string' && v.trim() !== '') {
      var n = Number(v);
      return isFinite(n) ? n : null;
    }
    return null;
  }

  function nf(opts, locale) {
    try { return new Intl.NumberFormat(locale, opts); } catch (e) { return null; }
  }

  function formatValue(v, format, opts) {
    opts = opts || {};
    if (v === null || v === undefined) { return DASH; }
    var n = toNum(v);
    if (n === null) { return String(v); }
    var loc = opts.locale;
    var out;
    var f;
    switch (format) {
      case 'integer':
        f = nf({ maximumFractionDigits: 0 }, loc);
        out = f ? f.format(n) : String(Math.round(n));
        break;
      case 'decimal1':
        f = nf({ minimumFractionDigits: 1, maximumFractionDigits: 1 }, loc);
        out = f ? f.format(n) : n.toFixed(1);
        break;
      case 'decimal2':
        f = nf({ minimumFractionDigits: 2, maximumFractionDigits: 2 }, loc);
        out = f ? f.format(n) : n.toFixed(2);
        break;
      case 'percent':
        f = nf({ maximumFractionDigits: 1 }, loc);
        out = (f ? f.format(n * 100) : String(n * 100)) + '%';
        break;
      case 'percent_value':
        f = nf({ maximumFractionDigits: 1 }, loc);
        out = (f ? f.format(n) : String(n)) + '%';
        break;
      case 'currency':
        f = nf({ style: 'currency', currency: opts.currency || 'USD' }, loc);
        if (!f) { f = nf({ maximumFractionDigits: 2 }, loc); }
        out = f ? f.format(n) : String(n);
        break;
      case 'compact':
        f = nf({ notation: 'compact', maximumFractionDigits: 1 }, loc);
        out = f ? f.format(n) : String(n);
        break;
      default:
        f = nf({ maximumFractionDigits: 2 }, loc);
        out = f ? f.format(n) : String(n);
    }
    if (opts.unit) { out += ' ' + opts.unit; }
    return out;
  }

  function delta(cur, prev) {
    var a = toNum(cur);
    var b = toNum(prev);
    if (a === null || b === null || b === 0) { return null; }
    return (a - b) / Math.abs(b);
  }

  function isStale(refreshedAt, refreshMinutes, nowMs) {
    if (!refreshedAt) { return false; }
    var t = Date.parse(refreshedAt);
    if (!isFinite(t)) { return false; }
    var limit = (2 * (toNum(refreshMinutes) || 15) + 5) * 60000;
    return nowMs - t > limit;
  }

  function mondayOf(y, m, d) {
    var t = new Date(Date.UTC(y, m - 1, d));
    var dow = (t.getUTCDay() + 6) % 7;
    t.setUTCDate(t.getUTCDate() - dow);
    return t.toISOString().slice(0, 10);
  }

  function periodKey(v, grain) {
    if (typeof v !== 'string') { return v === null || v === undefined ? '' : String(v); }
    var m = ISO_RE.exec(v);
    if (!m) { return v; }
    var y = +m[1];
    var mo = +m[2];
    var d = +m[3];
    switch (grain) {
      case 'week': return mondayOf(y, mo, d);
      case 'month': return m[1] + '-' + m[2];
      case 'quarter': return m[1] + '-Q' + (Math.floor((mo - 1) / 3) + 1);
      case 'year': return m[1];
      default: return v.slice(0, 10);
    }
  }

  function xLabel(v, grain) {
    return periodKey(v, grain);
  }

  function pivot(rows, x, series, y, grain) {
    var labels = [];
    var labelIdx = {};
    var names = [];
    var nameIdx = {};
    var dropped = 0;
    var cells = {};
    (rows || []).forEach(function (r) {
      var lab = xLabel(r[x], grain);
      if (!Object.prototype.hasOwnProperty.call(labelIdx, lab)) {
        labelIdx[lab] = labels.length;
        labels.push(lab);
      }
      var name = series ? String(r[series] === null || r[series] === undefined ? DASH : r[series]) : String(y);
      if (!Object.prototype.hasOwnProperty.call(nameIdx, name)) {
        if (names.length >= MAX_SERIES) {
          nameIdx[name] = -1;
          dropped += 1;
        } else {
          nameIdx[name] = names.length;
          names.push(name);
        }
      }
      var si = nameIdx[name];
      if (si >= 0) {
        cells[si + '|' + labelIdx[lab]] = toNum(r[y]);
      }
    });
    var datasets = names.map(function (name, si) {
      var data = labels.map(function (l, li) {
        var c = cells[si + '|' + li];
        return c === undefined ? null : c;
      });
      return { label: name, data: data };
    });
    return { labels: labels, datasets: datasets, dropped: dropped };
  }

  function pickStat(rows, tile, todayIso) {
    rows = rows || [];
    var idx = rows.length - 1;
    if (idx >= 0 && tile.pick === 'last_complete' && tile.grain && tile.x) {
      var lastKey = periodKey(rows[idx][tile.x], tile.grain);
      if (lastKey === periodKey(todayIso, tile.grain)) { idx -= 1; }
    }
    if (idx < 0) { return { value: null, previous: null, period: null }; }
    var row = rows[idx];
    var previous = null;
    var cmp = tile.compare;
    if (cmp === 'previous_row') {
      previous = idx > 0 ? rows[idx - 1][tile.column] : null;
    } else if (cmp && typeof cmp === 'object' && cmp.column) {
      previous = row[cmp.column];
    }
    var period = tile.x ? xLabel(row[tile.x], tile.grain) : null;
    return { value: row[tile.column], previous: previous === undefined ? null : previous, period: period };
  }

  function owed(value, cond) {
    var a = toNum(value);
    var b = cond ? toNum(cond.value) : null;
    if (a === null || b === null) { return false; }
    switch (cond.op) {
      case '>': return a > b;
      case '>=': return a >= b;
      case '<': return a < b;
      case '<=': return a <= b;
      case '=': return a === b;
      case '!=': return a !== b;
      default: return false;
    }
  }

  var core = {
    formatValue: formatValue, delta: delta, isStale: isStale, pivot: pivot,
    pickStat: pickStat, xLabel: xLabel, periodKey: periodKey, owed: owed,
    PALETTE: PALETTE, GOLD: GOLD, GOOD: GOOD, MUTED: MUTED
  };

  if (typeof document === 'undefined') {
    root.ICRenderCore = core;
    return;
  }

  /* ---------------------------------------------------------------- DOM part */

  var ALLOWED = {
    main: 1, header: 1, section: 1, div: 1, h1: 1, h2: 1, p: 1, span: 1,
    canvas: 1, table: 1, thead: 1, tbody: 1, tr: 1, th: 1, td: 1
  };

  function el(tag, className, text) {
    if (ALLOWED[tag] !== 1) { throw new Error('tag not allowed'); }
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function clear(node) {
    while (node.firstChild) { node.removeChild(node.firstChild); }
  }

  function getJSON(rel) {
    return fetch(rel + location.search, { cache: 'no-store', credentials: 'omit' }).then(function (r) {
      if (!r.ok) {
        var err = new Error('HTTP ' + r.status);
        err.status = r.status;
        throw err;
      }
      return r.json();
    });
  }

  function fmtOpts(tile) {
    return { currency: tile.currency || undefined, unit: tile.unit || undefined };
  }

  function fmtWhen(iso) {
    var t = Date.parse(iso);
    if (!isFinite(t)) { return String(iso); }
    try {
      return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(t));
    } catch (e) {
      return new Date(t).toISOString().slice(0, 16);
    }
  }

  function rgba(hex, a) {
    var n = parseInt(hex.slice(1), 16);
    return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
  }

  function applyDefaults() {
    var C = root.Chart;
    if (!C || !C.defaults) { return; }
    C.defaults.color = MUTED;
    C.defaults.borderColor = '#1E2D42';
    C.defaults.font.family = 'system-ui, sans-serif';
  }

  function buildDatasets(tile, rows) {
    var kind = tile.type;
    var labels;
    var sets = [];
    var ys = tile.y || [];
    if (tile.series && ys.length) {
      var pv = pivot(rows, tile.x, tile.series, ys[0].column, tile.grain);
      labels = pv.labels;
      sets = pv.datasets.map(function (d) { return { label: d.label, data: d.data, dashed: false }; });
    } else {
      labels = rows.map(function (r) { return xLabel(r[tile.x], tile.grain); });
      sets = ys.map(function (y) {
        return {
          label: y.label || y.column,
          data: rows.map(function (r) { return toNum(r[y.column]); }),
          dashed: !!y.dashed
        };
      });
    }
    var datasets = sets.map(function (s, i) {
      var color = PALETTE[i % PALETTE.length];
      var d = { label: s.label, data: s.data, borderColor: color, borderWidth: 2 };
      if (kind === 'bar' || kind === 'stacked_bar') {
        d.backgroundColor = color;
        d.borderWidth = 0;
      } else {
        d.backgroundColor = kind === 'area' ? rgba(color, 0.25) : color;
        d.fill = kind === 'area';
        d.pointRadius = kind === 'sparkline' ? 0 : 2;
        d.tension = 0.25;
        if (s.dashed) { d.borderDash = [6, 4]; }
      }
      return d;
    });
    if (tile.band && (kind === 'line' || kind === 'area')) {
      var lowD = {
        label: 'low', data: rows.map(function (r) { return toNum(r[tile.band.low]); }),
        borderColor: 'transparent', backgroundColor: 'transparent', pointRadius: 0, fill: false, ic_hide: true
      };
      var highD = {
        label: tile.band.label || 'Range', data: rows.map(function (r) { return toNum(r[tile.band.high]); }),
        borderColor: 'transparent', backgroundColor: 'rgba(91,160,224,0.18)', pointRadius: 0, fill: '-1'
      };
      datasets.push(lowD, highD);
    }
    return { labels: labels, datasets: datasets };
  }

  function chartOptions(tile, datasetCount) {
    var kind = tile.type;
    var fo = fmtOpts(tile);
    var horizontal = !!tile.horizontal;
    var valueAxis = horizontal ? 'x' : 'y';
    var spark = kind === 'sparkline';
    var stacked = kind === 'stacked_bar';
    var grid = { color: '#1E2D42' };
    var scales = {
      x: { stacked: stacked, grid: grid, ticks: { color: MUTED, maxRotation: 0, autoSkip: true } },
      y: { stacked: stacked, grid: grid, ticks: { color: MUTED } }
    };
    scales[valueAxis].ticks.callback = function (value) { return formatValue(value, tile.format, fo); };
    scales[valueAxis].beginAtZero = kind === 'bar' || stacked;
    if (tile.x_label) { scales[horizontal ? 'y' : 'x'].title = { display: true, text: String(tile.x_label), color: MUTED }; }
    if (tile.y_label) { scales[valueAxis].title = { display: true, text: String(tile.y_label), color: MUTED }; }
    if (spark) {
      scales.x.display = false;
      scales.y.display = false;
    }
    var opts = {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        colors: { enabled: false },
        legend: {
          display: !spark && datasetCount > 1,
          labels: {
            color: MUTED,
            filter: function (item, data) { return !data.datasets[item.datasetIndex].ic_hide; }
          }
        },
        tooltip: {
          enabled: !spark,
          callbacks: {
            label: function (ctx) {
              return (ctx.dataset.label || '') + ': ' + formatValue(ctx.parsed[valueAxis], tile.format, fo);
            }
          }
        }
      },
      scales: scales
    };
    if (horizontal) { opts.indexAxis = 'y'; }
    return opts;
  }

  function chartType(tile) {
    if (tile.type === 'bar' || tile.type === 'stacked_bar') { return 'bar'; }
    return 'line';
  }

  function chartClass(tile) {
    return tile.horizontal ? 'ic-chart ic-chart-tall' : 'ic-chart';
  }

  function drawChart(rec, rows) {
    var holder = el('div', chartClass(rec.tile));
    var canvas = el('canvas');
    holder.appendChild(canvas);
    rec.body.appendChild(holder);
    var built = buildDatasets(rec.tile, rows);
    rec.chart = new root.Chart(canvas, {
      type: chartType(rec.tile),
      data: { labels: built.labels, datasets: built.datasets },
      options: chartOptions(rec.tile, built.datasets.length)
    });
  }

  function drawSpark(rec, rows) {
    var tile = rec.tile;
    var holder = el('div', 'ic-spark');
    var canvas = el('canvas');
    holder.appendChild(canvas);
    rec.body.appendChild(holder);
    var st = {
      type: 'sparkline', x: tile.x, grain: tile.grain, format: tile.format,
      y: [{ column: tile.column, label: tile.title || tile.column, dashed: false }]
    };
    var built = buildDatasets(st, rows);
    rec.chart = new root.Chart(canvas, {
      type: 'line',
      data: { labels: built.labels, datasets: built.datasets },
      options: chartOptions(st, 1)
    });
  }

  function drawStat(rec, rows) {
    var tile = rec.tile;
    var fo = fmtOpts(tile);
    var picked = pickStat(rows, tile, new Date().toISOString());
    var box = el('div', 'ic-stat');
    var valueCls = 'ic-stat-value' + (owed(picked.value, tile.owed_when) ? ' ic-owed' : '');
    box.appendChild(el('div', valueCls, formatValue(picked.value, tile.format, fo)));
    if (picked.period) { box.appendChild(el('span', 'ic-stat-period', picked.period)); }
    if (tile.compare && tile.compare !== 'none') {
      var dl = delta(picked.value, picked.previous);
      if (dl !== null) {
        var dir = dl > 0 ? 'up' : (dl < 0 ? 'down' : 'flat');
        var cls = 'ic-flat';
        if (dir !== 'flat' && tile.good_direction && tile.good_direction !== 'none') {
          cls = dir === tile.good_direction ? 'ic-good' : 'ic-bad';
        }
        var arrow = dir === 'up' ? '\u25B2' : (dir === 'down' ? '\u25BC' : '\u25AC');
        var pct = Math.abs(dl * 100);
        var txt = arrow + ' ' + (dl < 0 ? '-' : (dl > 0 ? '+' : '')) + pct.toFixed(1) + '%';
        if (tile.compare_label) { txt += ' ' + tile.compare_label; }
        box.appendChild(el('span', 'ic-delta ' + cls, txt));
      }
    }
    rec.body.appendChild(box);
    if (tile.sparkline && tile.x && rows.length > 1) { drawSpark(rec, rows); }
  }

  function drawTable(rec, rows) {
    var tile = rec.tile;
    var cols = tile.columns || [];
    var wrap = el('div', 'ic-table-wrap');
    var table = el('table', 'ic-table');
    var thead = el('thead');
    var hr = el('tr');
    cols.forEach(function (c) { hr.appendChild(el('th', null, c.label || c.column)); });
    thead.appendChild(hr);
    table.appendChild(thead);
    var tbody = el('tbody');
    var max = toNum(tile.max_rows) || 20;
    rows.slice(0, max).forEach(function (r) {
      var tr = el('tr');
      cols.forEach(function (c) {
        var v = r[c.column];
        var isNum = toNum(v) !== null && typeof v !== 'boolean';
        var text;
        if (isNum) {
          text = formatValue(v, c.format || 'number', { currency: c.currency || undefined, unit: c.unit || undefined });
        } else {
          text = v === null || v === undefined ? DASH : String(v);
        }
        tr.appendChild(el('td', isNum ? 'ic-num' : null, text));
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    rec.body.appendChild(wrap);
    if (rows.length > max) { rec.body.appendChild(el('p', 'ic-more', (rows.length - max) + ' more rows not shown')); }
  }

  function destroyChart(rec) {
    if (rec.chart) {
      try { rec.chart.destroy(); } catch (e) { rec.chart = null; }
      rec.chart = null;
    }
  }

  function drawTile(rec, snap, refreshMinutes) {
    var tile = rec.tile;
    destroyChart(rec);
    clear(rec.body);
    clear(rec.status);
    if (tile.type === 'text') {
      rec.body.appendChild(el('p', 'ic-text', tile.text));
      return false;
    }
    var problem = false;
    if (!snap) {
      rec.status.appendChild(el('p', 'ic-empty', 'Waiting for the first refresh'));
      return false;
    }
    var rows = snap.rows || [];
    if (snap.error) {
      rec.status.appendChild(el('p', 'ic-error', 'Data feed failed: ' + snap.error));
      problem = true;
    }
    if (!snap.error && isStale(snap.refreshed_at, refreshMinutes, Date.now())) {
      rec.status.appendChild(el('p', 'ic-stale', 'Data is stale'));
      problem = true;
    }
    if (!rows.length) {
      if (!snap.error) {
        rec.status.appendChild(el('p', 'ic-empty', snap.refreshed_at ? 'No rows for this period' : 'Waiting for the first refresh'));
      }
      return problem;
    }
    try {
      switch (tile.type) {
        case 'stat': drawStat(rec, rows); break;
        case 'table': drawTable(rec, rows); break;
        case 'line': case 'area': case 'bar': case 'stacked_bar': case 'sparkline':
          drawChart(rec, rows); break;
        default: break;
      }
    } catch (e) {
      rec.status.appendChild(el('p', 'ic-error', 'Tile could not be drawn'));
      problem = true;
    }
    return problem;
  }

  function start() {
    applyDefaults();
    var rootEl = document.getElementById('ic-root');
    if (!rootEl) { return; }
    getJSON('./spec').then(function (spec) {
      build(rootEl, spec);
    }).catch(function (e) {
      clear(rootEl);
      rootEl.appendChild(el('p', 'ic-fail', 'Dashboard unavailable' + (e && e.status ? ' (HTTP ' + e.status + ')' : '')));
    });
  }

  function build(rootEl, spec) {
    clear(rootEl);
    document.title = String(spec.title || 'Dashboard');
    var head = el('header');
    head.appendChild(el('h1', 'ic-title', spec.title));
    if (spec.subtitle) { head.appendChild(el('p', 'ic-sub', spec.subtitle)); }
    var asof = el('p', 'ic-asof');
    head.appendChild(asof);
    rootEl.appendChild(head);

    var byId = {};
    (spec.tiles || []).forEach(function (t) { byId[t.id] = t; });
    var recs = [];
    var main = el('section');
    (spec.rows || []).forEach(function (row) {
      var rowEl = el('div', 'ic-row');
      row.forEach(function (id) {
        var tile = byId[id];
        if (!tile) { return; }
        var box = el('div', 'ic-tile');
        if (tile.title) { box.appendChild(el('h2', 'ic-tile-title', tile.title)); }
        var body = el('div');
        var status = el('div');
        box.appendChild(body);
        box.appendChild(status);
        if (tile.note) { box.appendChild(el('p', 'ic-note', tile.note)); }
        rowEl.appendChild(box);
        recs.push({ tile: tile, body: body, status: status, chart: null });
      });
      main.appendChild(rowEl);
    });
    rootEl.appendChild(main);

    var refresh = spec.refresh || {};
    var snaps = {};
    var qids = [];
    recs.forEach(function (r) {
      var q = r.tile.qid;
      if (q && QID_RE.test(q) && qids.indexOf(q) < 0) { qids.push(q); }
    });

    function refreshMin(q) { return toNum(refresh[q]) || 15; }

    function redraw() {
      var anyProblem = false;
      recs.forEach(function (r) {
        var q = r.tile.qid;
        if (r.tile.type !== 'text' && (!q || !QID_RE.test(q))) {
          destroyChart(r);
          clear(r.body);
          clear(r.status);
          r.status.appendChild(el('p', 'ic-error', 'Tile data unavailable'));
          anyProblem = true;
          return;
        }
        if (drawTile(r, q ? snaps[q] : null, q ? refreshMin(q) : 15)) { anyProblem = true; }
      });
      var oldestAsOf = null;
      var oldestRef = null;
      Object.keys(snaps).forEach(function (q) {
        var s = snaps[q];
        if (!s) { return; }
        if (s.data_as_of) {
          var a = Date.parse(s.data_as_of);
          if (isFinite(a) && (oldestAsOf === null || a < oldestAsOf)) { oldestAsOf = a; }
        }
        if (s.refreshed_at) {
          var b = Date.parse(s.refreshed_at);
          if (isFinite(b) && (oldestRef === null || b < oldestRef)) { oldestRef = b; }
        }
      });
      var text = 'Waiting for the first refresh';
      if (oldestAsOf !== null) {
        text = 'Data as of ' + fmtWhen(new Date(oldestAsOf).toISOString());
      } else if (oldestRef !== null) {
        text = 'Refreshed ' + fmtWhen(new Date(oldestRef).toISOString());
      }
      asof.textContent = text;
      asof.className = 'ic-asof' + (anyProblem ? ' ic-owed' : '');
    }

    function load(q) {
      getJSON('./data/' + q).then(function (snap) {
        snaps[q] = snap;
      }).catch(function (e) {
        var prev = snaps[q] || { rows: [], columns: [], refreshed_at: null };
        snaps[q] = {
          rows: prev.rows, columns: prev.columns, refreshed_at: prev.refreshed_at,
          data_as_of: prev.data_as_of,
          error: 'could not load data' + (e && e.status ? ' (HTTP ' + e.status + ')' : '')
        };
      }).then(function () {
        redraw();
        var ms = Math.max(60, refreshMin(q) * 60) * 1000;
        setTimeout(function () { load(q); }, ms);
      });
    }

    redraw();
    qids.forEach(load);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})(this);

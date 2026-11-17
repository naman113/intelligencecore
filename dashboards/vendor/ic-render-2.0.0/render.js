/*
 * Intelligence Corner chart-spec renderer 2.0.0
 * Engineer-written plumbing. Draws render_version 2 specs from ./spec and
 * ./data/<qid>. Never edited in place after release: any byte change is a new
 * ic-render-<version> directory (ic-render-1.0.0 stays for v1 specs).
 * This file holds the pure core first (dates, URL state, re-aggregation,
 * formulas, comparisons, formatting, tables, themes); the DOM follows it and is
 * only reached when a document exists. Every agent or data string reaches the
 * page through textContent only.
 */
(function (root) {
  'use strict';

  var core = {};
  var DASH = '\u2013';
  var ISO_RE = /^(\d{4})-(\d{2})-(\d{2})$/;
  var DAY_COL = 'metric_time__day';

  // ---------------------------------------------------------------- dates (C-04)

  var PRESETS = ['today', 'yesterday', 'last_7_days', 'last_30_days', 'last_90_days',
    'this_month', 'last_month', 'this_quarter', 'this_fy'];
  var PRESET_LABELS = {
    today: 'Today',
    yesterday: 'Yesterday',
    last_7_days: 'Last 7 days',
    last_30_days: 'Last 30 days',
    last_90_days: 'Last 90 days',
    this_month: 'This month',
    last_month: 'Last month',
    this_quarter: 'This quarter',
    this_fy: 'This financial year'
  };
  var ROLLING = { last_7_days: 7, last_30_days: 30, last_90_days: 90 };
  var CAL_MONTHS = { this_month: 1, this_quarter: 3, this_fy: 12 };

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  function isIso(s) {
    if (typeof s !== 'string') { return false; }
    var m = ISO_RE.exec(s);
    if (!m) { return false; }
    var y = +m[1];
    var mo = +m[2];
    var d = +m[3];
    var t = new Date(Date.UTC(y, mo - 1, d));
    return t.getUTCFullYear() === y && t.getUTCMonth() === mo - 1 && t.getUTCDate() === d;
  }

  function toMs(s) {
    var m = ISO_RE.exec(s);
    return Date.UTC(+m[1], +m[2] - 1, +m[3]);
  }

  function fromMs(ms) {
    var d = new Date(ms);
    return d.getUTCFullYear() + '-' + pad2(d.getUTCMonth() + 1) + '-' + pad2(d.getUTCDate());
  }

  function addDays(s, n) { return fromMs(toMs(s) + n * 86400000); }

  function daysBetween(a, b) { return Math.round((toMs(b) - toMs(a)) / 86400000); }

  function eachDay(from, to) {
    var out = [];
    var n = daysBetween(from, to);
    for (var i = 0; i <= n; i++) { out.push(addDays(from, i)); }
    return out;
  }

  function daysInMonth(y, m) { return new Date(Date.UTC(y, m, 0)).getUTCDate(); }

  function addMonths(s, months) {
    var m = ISO_RE.exec(s);
    var total = +m[1] * 12 + (+m[2] - 1) + months;
    var y = Math.floor(total / 12);
    var mo = total - y * 12 + 1;
    return y + '-' + pad2(mo) + '-' + pad2(Math.min(+m[3], daysInMonth(y, mo)));
  }

  function firstOfMonth(s) { return s.slice(0, 8) + '01'; }

  function fyStart(today, fy) {
    var y = +today.slice(0, 4);
    var mo = +today.slice(5, 7);
    return (mo < fy ? y - 1 : y) + '-' + pad2(fy) + '-01';
  }

  function currentRange(preset, today, fy) {
    if (preset === 'today') { return [today, today]; }
    if (preset === 'yesterday') { var y = addDays(today, -1); return [y, y]; }
    if (ROLLING[preset]) { return [addDays(today, -(ROLLING[preset] - 1)), today]; }
    if (preset === 'this_month') { return [firstOfMonth(today), today]; }
    if (preset === 'last_month') {
      var first = firstOfMonth(today);
      return [addMonths(first, -1), addDays(first, -1)];
    }
    if (preset === 'this_quarter') {
      var q = 3 * Math.floor((+today.slice(5, 7) - 1) / 3) + 1;
      return [today.slice(0, 4) + '-' + pad2(q) + '-01', today];
    }
    if (preset === 'this_fy') { return [fyStart(today, fy || 4), today]; }
    return null;
  }

  function previousRange(from, to, preset) {
    if (CAL_MONTHS[preset]) {
      var pf = addMonths(from, -CAL_MONTHS[preset]);
      var pt = addDays(pf, daysBetween(from, to));
      var cap = addDays(from, -1);
      return [pf, pt < cap ? pt : cap];
    }
    if (preset === 'last_month') { return [addMonths(from, -1), addDays(from, -1)]; }
    var n = daysBetween(from, to) + 1;
    return [addDays(from, -n), addDays(to, -n)];
  }

  function lastYearRange(from, to) { return [addMonths(from, -12), addMonths(to, -12)]; }

  function presetRange(preset, period, today, fy) {
    var cur = currentRange(preset, today, fy);
    if (!cur) { return null; }
    if (period === 'previous') { return previousRange(cur[0], cur[1], preset); }
    if (period === 'last_year') { return lastYearRange(cur[0], cur[1]); }
    return cur;
  }

  function horizonStart(today, days) { return addDays(today, -((days || 1) - 1)); }

  function bucketFor(from, to) {
    var n = daysBetween(from, to) + 1;
    if (n <= 45) { return 'day'; }
    if (n <= 200) { return 'week'; }
    return 'month';
  }

  function bucketKey(day, bucket) {
    if (bucket === 'month') { return firstOfMonth(day); }
    if (bucket === 'week') {
      var dow = new Date(toMs(day)).getUTCDay();
      return addDays(day, -((dow + 6) % 7));
    }
    return day;
  }

  var dayFormatters = {};
  function todayIn(tz, nowMs) {
    var key = tz || 'UTC';
    var f = dayFormatters[key];
    if (!f) {
      try {
        f = new Intl.DateTimeFormat('en-US', { timeZone: key, year: 'numeric', month: '2-digit', day: '2-digit' });
      } catch (e) {
        f = new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', year: 'numeric', month: '2-digit', day: '2-digit' });
      }
      dayFormatters[key] = f;
    }
    var parts = f.formatToParts(new Date(nowMs));
    var y = '';
    var m = '';
    var d = '';
    for (var i = 0; i < parts.length; i++) {
      if (parts[i].type === 'year') { y = parts[i].value; }
      else if (parts[i].type === 'month') { m = parts[i].value; }
      else if (parts[i].type === 'day') { d = parts[i].value; }
    }
    return y + '-' + m + '-' + d;
  }

  function asOfDay(ts, tz) {
    if (typeof ts !== 'string' || !ts) { return null; }
    var s = ts.replace(' ', 'T').replace(/(\.\d{3})\d+/, '$1').replace(/([+-]\d{2})$/, '$1:00');
    var ms = Date.parse(s);
    if (!isFinite(ms)) { return null; }
    return todayIn(tz, ms);
  }

  // ---------------------------------------------------------------- URL state (C-12)

  var HASH_RE = /^[A-Za-z0-9_=,.:;%~-]*$/;
  var FILTER_ID_RE = /^[a-z][a-z0-9_]{0,47}$/;
  var MAX_HASH = 600;

  // A NULL member travels as the reserved token ~null (the hash grammar never carries the NUL
  // key itself); a real value starting with ~ is escaped, so the token cannot be forged.
  var NULL_TOKEN = '~null';

  function escValue(v) {
    if (v === '\u0000') { return NULL_TOKEN; }
    return encodeURIComponent(String(v)).replace(/[!'()*~]/g, function (c) {
      return '%' + c.charCodeAt(0).toString(16).toUpperCase();
    });
  }

  function encodeState(st) {
    var parts = [];
    var r = st && st.range;
    if (r && r.preset) { parts.push('r=' + r.preset); }
    else if (r && r.from && r.to) { parts.push('r=' + r.from + '..' + r.to); }
    var f = (st && st.filters) || {};
    var ids = Object.keys(f).sort();
    for (var i = 0; i < ids.length; i++) {
      var vals = f[ids[i]];
      if (!vals || !vals.length) { continue; }
      parts.push('f.' + ids[i] + '=' + vals.map(escValue).join(','));
    }
    return parts.join(';');
  }

  function decodeState(hash) {
    if (typeof hash !== 'string') { return null; }
    var h = hash.charAt(0) === '#' ? hash.slice(1) : hash;
    if (h.length > MAX_HASH || !HASH_RE.test(h)) { return null; }
    var out = { range: null, filters: {} };
    if (h === '') { return out; }
    var parts = h.split(';');
    for (var i = 0; i < parts.length; i++) {
      var p = parts[i];
      var eq = p.indexOf('=');
      if (eq < 1) { return null; }
      var key = p.slice(0, eq);
      var val = p.slice(eq + 1);
      if (key === 'r') {
        if (out.range) { return null; }
        if (PRESETS.indexOf(val) >= 0) { out.range = { preset: val }; continue; }
        var m = /^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$/.exec(val);
        if (!m || !isIso(m[1]) || !isIso(m[2]) || m[1] > m[2]) { return null; }
        out.range = { from: m[1], to: m[2] };
      } else if (key.slice(0, 2) === 'f.') {
        var id = key.slice(2);
        if (!FILTER_ID_RE.test(id) || Object.prototype.hasOwnProperty.call(out.filters, id) || val === '') { return null; }
        var raw = val.split(',');
        if (raw.length > 50) { return null; }
        var vals = [];
        for (var j = 0; j < raw.length; j++) {
          if (raw[j] === NULL_TOKEN) { vals.push('\u0000'); continue; }
          var dv;
          try { dv = decodeURIComponent(raw[j]); } catch (e) { return null; }
          if (dv.indexOf('\u0000') >= 0) { return null; }
          vals.push(dv);
        }
        out.filters[id] = vals;
      } else {
        return null;
      }
    }
    return out;
  }

  // The hash the frame posts: always inside the wrapper's pattern and 600 characters. A state that
  // is too long drops its filters first, then falls back to the preset.
  function safeHash(st) {
    var full = encodeState(st);
    if (full.length <= MAX_HASH) { return full; }
    var r = st && st.range;
    var bare = encodeState({ range: r, filters: {} });
    if (bare.length <= MAX_HASH) { return bare; }
    return encodeState({ range: r && r.preset ? r : null, filters: {} });
  }

  core.safeHash = safeHash;
  core.PRESETS = PRESETS;
  core.PRESET_LABELS = PRESET_LABELS;
  core.isIso = isIso;
  core.addDays = addDays;
  core.addMonths = addMonths;
  core.daysBetween = daysBetween;
  core.eachDay = eachDay;
  core.todayIn = todayIn;
  core.asOfDay = asOfDay;
  core.presetRange = presetRange;
  core.previousRange = previousRange;
  core.lastYearRange = lastYearRange;
  core.horizonStart = horizonStart;
  core.bucketFor = bucketFor;
  core.bucketKey = bucketKey;
  core.encodeState = encodeState;
  core.decodeState = decodeState;

  // ---------------------------------------------------------------- re-aggregation (C-03, C-06, C-07)

  function toNum(v) {
    if (typeof v === 'number') { return isFinite(v) ? v : null; }
    if (typeof v === 'string' && v.trim() !== '') {
      var n = Number(v);
      return isFinite(n) ? n : null;
    }
    return null;
  }

  function has(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }

  // Non-numbers count as 0: used only for sum kinds and for parts.
  function sumCol(rows, col) {
    var t = 0;
    for (var i = 0; i < rows.length; i++) {
      var n = toNum(rows[i][col]);
      if (n !== null) { t += n; }
    }
    return t;
  }

  // Tree walk over num / in / op only (C-06). Plain arithmetic, nothing is parsed or run.
  function evalNode(node, inputs, depth) {
    if (!node || typeof node !== 'object' || depth > 12) { return null; }
    if (has(node, 'num')) { return toNum(node.num); }
    if (has(node, 'in')) { return inputs ? toNum(inputs[node['in']]) : null; }
    if (has(node, 'op')) {
      var a = evalNode(node.a, inputs, depth + 1);
      var b = evalNode(node.b, inputs, depth + 1);
      if (a === null || b === null) { return null; }
      var out = null;
      if (node.op === '+') { out = a + b; }
      else if (node.op === '-') { out = a - b; }
      else if (node.op === '*') { out = a * b; }
      else if (node.op === '/') { out = b === 0 ? null : a / b; }
      return out !== null && isFinite(out) ? out : null;
    }
    return null;
  }

  function evalFormula(tree, inputs) { return evalNode(tree, inputs, 1); }

  function partCols(meta) {
    var p = meta.parts || {};
    var c = meta.column;
    if (meta.kind === 'avg') { return { sum: p.sum || c + '__sum', count: p.count || c + '__count' }; }
    if (meta.kind === 'ratio') { return { num: p.num || c + '__num', den: p.den || c + '__den' }; }
    return p;
  }

  function dayOf(row, dayCol) {
    var v = row[dayCol];
    return typeof v === 'string' ? v.slice(0, 10) : '';
  }

  function rowsIn(rows, from, to, dayCol) {
    var out = [];
    for (var i = 0; i < rows.length; i++) {
      var d = dayOf(rows[i], dayCol);
      if (d !== '' && d >= from && d <= to) { out.push(rows[i]); }
    }
    return out;
  }

  // Aggregates rows already limited to the range. Returns {value, parts}.
  function aggregate(meta, rows, dayCol) {
    var k = meta.kind;
    var c = meta.column;
    var i;
    var cols;
    if (k === 'sum') { return { value: sumCol(rows, c), parts: null }; }
    if (k === 'min' || k === 'max') {
      var best = null;
      for (i = 0; i < rows.length; i++) {
        var n = toNum(rows[i][c]);
        if (n === null) { continue; }
        if (best === null || (k === 'min' ? n < best : n > best)) { best = n; }
      }
      return { value: best, parts: null };
    }
    if (k === 'avg') {
      cols = partCols(meta);
      var s = sumCol(rows, cols.sum);
      var cnt = sumCol(rows, cols.count);
      return { value: cnt === 0 ? null : s / cnt, parts: { sum: s, count: cnt } };
    }
    if (k === 'ratio') {
      cols = partCols(meta);
      var num = sumCol(rows, cols.num);
      var den = sumCol(rows, cols.den);
      return { value: den === 0 ? null : num / den, parts: { num: num, den: den } };
    }
    if (k === 'formula') {
      var inputs = {};
      var p = meta.parts || {};
      var aliases = Object.keys(p);
      for (i = 0; i < aliases.length; i++) { inputs[aliases[i]] = sumCol(rows, p[aliases[i]]); }
      return { value: evalFormula(meta.formula, inputs), parts: inputs };
    }
    if (k === 'closing' || k === 'opening') {
      var pick = null;
      for (i = 0; i < rows.length; i++) {
        var d = dayOf(rows[i], dayCol);
        if (d === '') { continue; }
        if (pick === null || (k === 'closing' ? d > pick : d < pick)) { pick = d; }
      }
      if (pick === null) { return { value: null, parts: null }; }
      var same = [];
      for (i = 0; i < rows.length; i++) {
        if (dayOf(rows[i], dayCol) === pick) { same.push(rows[i]); }
      }
      return { value: sumCol(same, c), parts: null };
    }
    return { value: null, parts: null };
  }

  function presetValue(ctx) {
    if (!ctx.preset || !ctx.presetRows) { return { na: 'custom' }; }
    var period = ctx.period || 'current';
    for (var i = 0; i < ctx.presetRows.length; i++) {
      var pr = ctx.presetRows[i];
      if (pr.preset === ctx.preset && pr.period === period) {
        if (pr.today !== ctx.today) { return { na: 'nodata' }; }
        return { value: toNum(pr.value), parts: null };
      }
    }
    return { na: 'nodata' };
  }

  // Full result of one range: {value, parts} or {na}.
  function rangeAgg(meta, rows, range, ctx) {
    ctx = ctx || {};
    rows = rows || [];
    var dayCol = ctx.dayCol || DAY_COL;
    if (meta.kind === 'now') {
      // One server-computed row per tile; more than one is ambiguous and not combinable.
      if (rows.length > 1) { return { na: 'nodata' }; }
      return { value: rows.length ? toNum(rows[0][meta.column]) : null, parts: null };
    }
    if (ctx.horizonStart && range.from < ctx.horizonStart) { return { na: 'range' }; }
    var to = range.to;
    if (ctx.asOf) {
      if (range.from > ctx.asOf) { return { na: 'nodata' }; }
      if (to > ctx.asOf) { to = ctx.asOf; }
    }
    if (meta.kind === 'distinct') {
      if (range.from === range.to) {
        var day = rowsIn(rows, range.from, range.from, dayCol);
        return { value: day.length ? sumCol(day, meta.column) : 0, parts: null };
      }
      return presetValue(ctx);
    }
    return aggregate(meta, rowsIn(rows, range.from, to, dayCol), dayCol);
  }

  function rangeValue(meta, rows, range, ctx) {
    var r = rangeAgg(meta, rows, range, ctx);
    return r.na ? { na: r.na } : r.value;
  }

  function copyCtx(ctx, over) {
    var out = {};
    var k;
    for (k in (ctx || {})) { if (has(ctx, k)) { out[k] = ctx[k]; } }
    for (k in over) { if (has(over, k)) { out[k] = over[k]; } }
    return out;
  }

  function bucketEnd(key, bucket) {
    if (bucket === 'month') { return addDays(addMonths(key, 1), -1); }
    if (bucket === 'week') { return addDays(key, 6); }
    return key;
  }

  function bucketSeries(meta, rows, range, bucket, ctx) {
    var days = eachDay(range.from, range.to);
    var seen = {};
    var out = [];
    var inner = copyCtx(ctx, { preset: null });
    for (var i = 0; i < days.length; i++) {
      var key = bucketKey(days[i], bucket);
      if (seen[key]) { continue; }
      seen[key] = true;
      var from = key < range.from ? range.from : key;
      var end = bucketEnd(key, bucket);
      var to = end > range.to ? range.to : end;
      var r = rangeAgg(meta, rows, { from: from, to: to }, inner);
      out.push({ key: key, from: from, to: to, value: r.na ? null : r.value, na: r.na || null, parts: r.parts || null });
    }
    return out;
  }

  // A NULL dimension value is its own member: it never merges with the text 'null' and shows
  // as 'Not set'. `key` is what filters and icRow carry (NULL_KEY for NULL, else the text).
  var NULL_KEY = '\u0000';
  var NULL_LABEL = 'Not set';

  // The text the eval gate reads for a member key: NULL shows as its label, never the NUL key.
  function keyText(k) { return k === NULL_KEY ? NULL_LABEL : String(k); }

  function memberKey(v) { return v === null || v === undefined ? NULL_KEY : String(v); }

  function memberValues(meta, rows, range, keyCol, ctx) {
    var order = [];
    var groups = {};
    rows = rows || [];
    for (var i = 0; i < rows.length; i++) {
      var k = memberKey(rows[i][keyCol]);
      if (!has(groups, k)) { groups[k] = []; order.push(k); }
      groups[k].push(rows[i]);
    }
    var out = [];
    for (var j = 0; j < order.length; j++) {
      var label = order[j] === NULL_KEY ? NULL_LABEL : order[j];
      if (meta.kind === 'distinct') {
        out.push({ member: label, key: order[j], value: null, na: 'custom', parts: null });
        continue;
      }
      var r = rangeAgg(meta, groups[order[j]], range, ctx);
      out.push({ member: label, key: order[j], value: r.na ? null : r.value, na: r.na || null, parts: r.parts || null });
    }
    return out;
  }

  function combines(meta) {
    return !meta || (meta.kind !== 'distinct' && meta.kind !== 'now' && meta.kind !== 'fixed');
  }

  function sumParts(items, name) {
    var t = 0;
    var seen = false;
    for (var i = 0; i < items.length; i++) {
      var n = items[i].parts ? toNum(items[i].parts[name]) : null;
      if (n !== null) { t += n; seen = true; }
    }
    return seen ? t : null;
  }

  // C-03 "Combining members".
  function combineMembers(meta, items) {
    if (!combines(meta)) { return null; }
    var k = meta ? meta.kind : 'sum';
    var i;
    var n;
    if (k === 'min' || k === 'max') {
      var best = null;
      for (i = 0; i < items.length; i++) {
        n = toNum(items[i].value);
        if (n === null) { continue; }
        if (best === null || (k === 'min' ? n < best : n > best)) { best = n; }
      }
      return best;
    }
    if (k === 'avg') {
      var s = sumParts(items, 'sum');
      var c = sumParts(items, 'count');
      return s === null || c === null || c === 0 ? null : s / c;
    }
    if (k === 'ratio') {
      var nu = sumParts(items, 'num');
      var de = sumParts(items, 'den');
      return nu === null || de === null || de === 0 ? null : nu / de;
    }
    if (k === 'formula') {
      var inputs = {};
      var aliases = Object.keys(meta.parts || {});
      for (i = 0; i < aliases.length; i++) { inputs[aliases[i]] = sumParts(items, aliases[i]); }
      return evalFormula(meta.formula, inputs);
    }
    var t = 0;
    var seen = false;
    for (i = 0; i < items.length; i++) {
      n = toNum(items[i].value);
      if (n !== null) { t += n; seen = true; }
    }
    return seen ? t : null;
  }

  // C-07: the n largest members by value (stable), then Other when the kind combines.
  function topOther(meta, members, n, other) {
    var idx = [];
    for (var i = 0; i < members.length; i++) { idx.push({ m: members[i], i: i }); }
    idx.sort(function (x, y) {
      var a = toNum(x.m.value);
      var b = toNum(y.m.value);
      if (a === null && b === null) { return x.i - y.i; }
      if (a === null) { return 1; }
      if (b === null) { return -1; }
      return b - a || x.i - y.i;
    });
    var top = [];
    var rest = [];
    for (var j = 0; j < idx.length; j++) { (j < n ? top : rest).push(idx[j].m); }
    if (!rest.length) { return { members: top, other: null, dropped: 0 }; }
    if (other && combines(meta)) {
      var item = { member: 'Other', value: combineMembers(meta, rest), parts: null, other: true };
      return { members: top.concat([item]), other: item, dropped: 0 };
    }
    return { members: top, other: null, dropped: rest.length };
  }

  function filterRows(rows, filters) {
    var cols = Object.keys(filters || {}).filter(function (c) {
      return filters[c] && filters[c].length;
    });
    if (!cols.length) { return rows; }
    return rows.filter(function (row) {
      for (var i = 0; i < cols.length; i++) {
        if (filters[cols[i]].indexOf(memberKey(row[cols[i]])) < 0) { return false; }
      }
      return true;
    });
  }

  // C-04 comparison of a range with its previous period or the same days last year.
  function compareValue(meta, rows, range, kind, ctx) {
    var period = kind === 'last_year' ? 'last_year' : 'previous';
    var cur = rangeAgg(meta, rows, range, copyCtx(ctx, { period: 'current' }));
    if (cur.na) { return { na: cur.na }; }
    var pr = period === 'last_year'
      ? lastYearRange(range.from, range.to)
      : previousRange(range.from, range.to, (ctx && ctx.preset) || null);
    var prevTo = pr[1];
    // Data lags today (as_of before the range end): the current side stops at as_of, so cut the
    // comparison by the same number of days. Count-distinct and now values are server-computed.
    if (ctx && ctx.asOf && range.to > ctx.asOf && meta.kind !== 'distinct' && meta.kind !== 'now') {
      prevTo = addDays(prevTo, -daysBetween(ctx.asOf, range.to));
      if (prevTo < pr[0]) { prevTo = pr[0]; }
    }
    var prev = rangeAgg(meta, rows, { from: pr[0], to: prevTo }, copyCtx(ctx, { period: period }));
    if (prev.na) { return { na: prev.na }; }
    var a = toNum(cur.value);
    var b = toNum(prev.value);
    var delta = a === null || b === null || b === 0 ? null : (a - b) / Math.abs(b);
    return { current: cur.value, previous: prev.value, delta: delta };
  }

  // Total of one measure column over the underlying rows (never the displayed order or page).
  function totalValue(meta, rows, range, rowsAre, keyCol, ctx) {
    if (!combines(meta) || !meta) { return null; }
    if (rowsAre === 'members') {
      return combineMembers(meta, memberValues(meta, rows, range, keyCol, ctx));
    }
    var v = rangeValue(meta, rows, range, ctx);
    return v !== null && typeof v === 'object' ? null : v;
  }

  core.toNum = toNum;
  core.sumCol = sumCol;
  core.evalFormula = evalFormula;
  core.rangeValue = rangeValue;
  core.bucketSeries = bucketSeries;
  core.memberValues = memberValues;
  core.memberKey = memberKey;
  core.NULL_KEY = NULL_KEY;
  core.NULL_LABEL = NULL_LABEL;
  core.combineMembers = combineMembers;
  core.topOther = topOther;
  core.filterRows = filterRows;
  core.compareValue = compareValue;
  core.totalValue = totalValue;

  // ---------------------------------------------------------------- formatting (UI-SPEC 7 and 8)

  var nfCache = {};
  var dfCache = {};

  function nf(locale, key, opts) {
    var ck = locale + '|' + key;
    if (!has(nfCache, ck)) {
      try { nfCache[ck] = new Intl.NumberFormat(locale, opts); } catch (e) { nfCache[ck] = null; }
    }
    return nfCache[ck];
  }

  function formatValue(v, format, opts) {
    opts = opts || {};
    if (v === null || v === undefined) { return DASH; }
    var n = toNum(v);
    if (n === null) { return String(v); }
    var loc = opts.locale;
    var compact = !!opts.compact || format === 'compact';
    var f;
    var out;
    var cur = opts.currency || 'USD';
    if (format === 'percent' || format === 'percent_value') {
      f = nf(loc, 'p1', { maximumFractionDigits: 1 });
      out = (f ? f.format(format === 'percent' ? n * 100 : n) : String(n)) + '%';
    } else if (format === 'currency') {
      f = compact
        ? nf(loc, 'cc|' + cur, { style: 'currency', currency: cur, notation: 'compact', maximumFractionDigits: 1 })
        : nf(loc, 'c|' + cur, { style: 'currency', currency: cur });
      out = f ? f.format(n) : String(n);
    } else {
      var o;
      var key;
      if (format === 'integer') { key = 'i'; o = { maximumFractionDigits: 0 }; }
      else if (format === 'decimal1') { key = 'd1'; o = { minimumFractionDigits: 1, maximumFractionDigits: 1 }; }
      else if (format === 'decimal2') { key = 'd2'; o = { minimumFractionDigits: 2, maximumFractionDigits: 2 }; }
      else { key = 'n'; o = { maximumFractionDigits: 2 }; }
      if (compact) {
        key = 'k';
        o = { notation: 'compact', maximumFractionDigits: 1 };
      }
      f = nf(loc, key, o);
      out = f ? f.format(n) : String(n);
    }
    if (opts.unit) { out += ' ' + opts.unit; }
    return out;
  }

  var DATE_OPTS = {
    axis: { day: 'numeric', month: 'short' },
    tooltip: { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' },
    month: { month: 'short', year: 'numeric' }
  };

  function formatDate(iso, kind, locale) {
    if (!isIso(iso)) { return String(iso); }
    var k = has(DATE_OPTS, kind) ? kind : 'axis';
    var ck = locale + '|' + k;
    if (!has(dfCache, ck)) {
      var o = {};
      for (var p in DATE_OPTS[k]) { if (has(DATE_OPTS[k], p)) { o[p] = DATE_OPTS[k][p]; } }
      o.timeZone = 'UTC';
      try { dfCache[ck] = new Intl.DateTimeFormat(locale, o); } catch (e) { dfCache[ck] = null; }
    }
    var f = dfCache[ck];
    return f ? f.format(new Date(toMs(iso))) : iso;
  }

  function formatAsOf(ts, locale, tz) {
    if (typeof ts !== 'string' || !ts) { return ''; }
    var s = ts.replace(' ', 'T').replace(/(\.\d{3})\d+/, '$1').replace(/([+-]\d{2})$/, '$1:00');
    var ms = Date.parse(s);
    if (!isFinite(ms)) { return ''; }
    var o = { dateStyle: 'medium', timeStyle: 'short' };
    if (tz) { o.timeZone = tz; }
    try { return new Intl.DateTimeFormat(locale, o).format(new Date(ms)); } catch (e) {
      return new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' }).format(new Date(ms));
    }
  }

  // good_direction: up | down | none. Returns null when there is nothing to compare.
  function deltaParts(cur, prev, good) {
    var a = toNum(cur);
    var b = toNum(prev);
    if (a === null || b === null || b === 0) { return null; }
    var d = (a - b) / Math.abs(b);
    var pct = d * 100;
    if (Math.abs(pct).toFixed(1) === '0.0') { return { text: '0.0%', cls: 'ic-flat' }; }
    var up = d > 0;
    var text = (up ? '\u25B2 +' : '\u25BC -') + Math.abs(pct).toFixed(1) + '%';
    var cls = 'ic-flat';
    if (good === 'up') { cls = up ? 'ic-good' : 'ic-bad'; }
    else if (good === 'down') { cls = up ? 'ic-bad' : 'ic-good'; }
    return { text: text, cls: cls };
  }

  // ---------------------------------------------------------------- tables (UI-SPEC 11)

  // Stable sort; numbers numerically, strings with localeCompare, nulls last in both directions.
  function sortRows(rows, col, dir) {
    var idx = [];
    for (var i = 0; i < rows.length; i++) { idx.push({ r: rows[i], i: i }); }
    var sign = dir === 'desc' ? -1 : 1;
    idx.sort(function (x, y) {
      var a = x.r[col];
      var b = y.r[col];
      var an = a === null || a === undefined || a === '';
      var bn = b === null || b === undefined || b === '';
      if (an && bn) { return x.i - y.i; }
      if (an) { return 1; }
      if (bn) { return -1; }
      var na = toNum(a);
      var nb = toNum(b);
      var c = na !== null && nb !== null ? na - nb : String(a).localeCompare(String(b));
      return c === 0 ? x.i - y.i : sign * c;
    });
    return idx.map(function (e) { return e.r; });
  }

  function pageRows(rows, page, size) {
    var total = rows.length;
    var start = Math.max(0, (page - 1) * size);
    var slice = rows.slice(start, start + size);
    return {
      rows: slice,
      from: slice.length ? start + 1 : 0,
      to: slice.length ? start + slice.length : 0,
      total: total
    };
  }

  // model: {rows, range, rowsAre: 'buckets' | 'members', keyCol, ctx}; rows are the underlying
  // data (daily rows for measure columns), never the displayed order or page.
  // metas: {column: measure meta}. Returns [{column, value}], value null for a dash.
  function totalsRow(columns, model, metas) {
    var out = [];
    for (var i = 0; i < columns.length; i++) {
      var col = columns[i];
      var meta = metas && has(metas, col.column) ? metas[col.column] : null;
      var value = null;
      if (meta) {
        value = totalValue(meta, model.rows, model.range, model.rowsAre, model.keyCol, model.ctx);
      } else if (col.total) {
        var seen = false;
        var t = 0;
        var src = model.queryRows || model.rows || [];
        for (var j = 0; j < src.length; j++) {
          var n = toNum(src[j][col.column]);
          if (n !== null) { t += n; seen = true; }
        }
        value = seen ? t : null;
      }
      out.push({ column: col.column, value: value });
    }
    return out;
  }

  function dueState(date, today, warnDays) {
    if (typeof date !== 'string') { return null; }
    var d = date.slice(0, 10);
    if (!isIso(d) || !isIso(today)) { return null; }
    if (d < today) { return { state: 'overdue', text: 'Overdue' }; }
    var n = daysBetween(today, d);
    if (n > (warnDays || 0)) { return null; }
    if (n === 0) { return { state: 'soon', text: 'Due today' }; }
    return { state: 'soon', text: 'Due in ' + n + (n === 1 ? ' day' : ' days') };
  }

  // ---------------------------------------------------------------- themes and tones (UI-SPEC 1, 6)

  var THEMES = {
    light: {
      page: '#F5F6F8', surface: '#FFFFFF', border: '#DDE1E7', grid: '#E6E8EC',
      ink: '#14181F', ink2: '#4A5463', muted: '#6B7684',
      good: '#1A7F37', bad: '#C62F2F', owed: '#9A6100',
      inward: '#2A78D6', outward: '#9A4F94', neutral: '#445160', accent: '#0AA19B',
      seq: ['#CDE2FB', '#86B6EF', '#3987E5', '#256ABF', '#104281'],
      ord: ['#86B6EF', '#3987E5', '#256ABF', '#104281'],
      bands: ['#E6E8EC', '#D5D9E0', '#C3C9D2']
    },
    dark: {
      page: '#080C14', surface: '#0E1420', border: '#1E2D42', grid: '#1E2D42',
      ink: '#F0F4F8', ink2: '#A0AEC0', muted: '#8A96A6',
      good: '#3FB27F', bad: '#E05252', owed: '#D4952B',
      inward: '#3987E5', outward: '#B8409A', neutral: '#5C6375', accent: '#1F9E93',
      seq: ['#184F95', '#256ABF', '#3987E5', '#6DA7EC', '#B7D3F6'],
      ord: ['#184F95', '#256ABF', '#3987E5', '#6DA7EC'],
      bands: ['#141D2B', '#1A2536', '#22304A']
    }
  };

  var TONE_ORDER = ['inward', 'outward', 'accent', 'neutral'];

  function toneColor(theme, tone) {
    var t = has(THEMES, theme) ? THEMES[theme] : THEMES.dark;
    return TONE_ORDER.indexOf(tone) >= 0 ? t[tone] : t.neutral;
  }

  // Fixed order inward, outward, accent, neutral; the 5th and later are neutral.
  function assignTones(count, explicit) {
    var out = [];
    for (var i = 0; i < count; i++) {
      var e = explicit && explicit[i];
      if (e && TONE_ORDER.indexOf(e) >= 0) { out.push(e); }
      else { out.push(TONE_ORDER[Math.min(i, TONE_ORDER.length - 1)]); }
    }
    return out;
  }

  core.formatValue = formatValue;
  core.formatDate = formatDate;
  core.formatAsOf = formatAsOf;
  core.deltaParts = deltaParts;
  core.sortRows = sortRows;
  core.pageRows = pageRows;
  core.totalsRow = totalsRow;
  core.dueState = dueState;
  core.THEMES = THEMES;
  core.toneColor = toneColor;
  core.assignTones = assignTones;

  // The tone of the n-th member in the unfiltered order (neutral when unknown or past the fourth).
  function toneAt(i) { return i < 0 ? 'neutral' : TONE_ORDER[Math.min(i, TONE_ORDER.length - 1)]; }

  // ---------------------------------------------------------------- fixed tiles and chart models (UI-SPEC 5, 6)

  var MAX_COLOURED = 4;
  var ISO_PREFIX = /^(\d{4})-(\d{2})-(\d{2})/;

  // Timestamp text (also the Postgres form with a space and a short offset) to epoch ms, or null.
  function tsMs(ts) {
    if (typeof ts !== 'string' || !ts) { return null; }
    var s = ts.replace(' ', 'T').replace(/(\.\d{3})\d+/, '$1').replace(/([+-]\d{2})$/, '$1:00');
    var ms = Date.parse(s);
    return isFinite(ms) ? ms : null;
  }

  function isStale(refreshedAt, refreshMinutes, nowMs) {
    var t = tsMs(refreshedAt);
    if (t === null) { return false; }
    var limit = (2 * (toNum(refreshMinutes) || 15) + 5) * 60000;
    return nowMs - t > limit;
  }

  function periodKey(v, grain) {
    if (typeof v !== 'string') { return v === null || v === undefined ? '' : String(v); }
    var m = ISO_PREFIX.exec(v);
    if (!m) { return v; }
    switch (grain) {
      case 'week': return bucketKey(m[0], 'week');
      case 'month': return m[1] + '-' + m[2];
      case 'quarter': return m[1] + '-Q' + (Math.floor((+m[2] - 1) / 3) + 1);
      case 'year': return m[1];
      default: return v.slice(0, 10);
    }
  }

  // With a locale, day and week keys read as axis dates and month keys as "Nov 2026".
  function xLabel(v, grain, locale) {
    var k = periodKey(v, grain);
    if (!locale) { return k; }
    if (/^\d{4}-\d{2}$/.test(k)) { return formatDate(k + '-01', 'month', locale); }
    if (isIso(k)) { return formatDate(k, 'axis', locale); }
    return k;
  }

  function rawKey(v) {
    return v === null || v === undefined ? '' : String(v);
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

  function pickStat(rows, tile, todayIso, locale) {
    rows = rows || [];
    var idx = rows.length - 1;
    if (idx >= 0 && tile.pick === 'last_complete' && tile.grain && tile.x) {
      var lastKey = periodKey(rows[idx][tile.x], tile.grain);
      if (lastKey === periodKey(todayIso, tile.grain)) { idx -= 1; }
    }
    if (idx < 0) { return { value: null, previous: null, period: null, key: '' }; }
    var row = rows[idx];
    var previous = null;
    var cmp = tile.compare;
    if (cmp === 'previous_row') {
      previous = idx > 0 ? rows[idx - 1][tile.column] : null;
    } else if (cmp && typeof cmp === 'object' && cmp.column) {
      previous = row[cmp.column];
    }
    return {
      value: row[tile.column],
      previous: previous === undefined ? null : previous,
      period: tile.x ? xLabel(row[tile.x], tile.grain, locale) : null,
      key: tile.x ? rawKey(row[tile.x]) : ''
    };
  }

  // At most four coloured series; the rest fold into one Other series (summed per label) when
  // opts.fold is not false, else they are dropped and counted.
  function pivot(rows, x, series, y, grain, opts) {
    opts = opts || {};
    var maxS = opts.max || MAX_COLOURED;
    var labels = [];
    var keys = [];
    var labelIdx = {};
    var order = [];
    var cells = {};
    rows = rows || [];
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var pk = periodKey(r[x], grain);
      if (!has(labelIdx, pk)) {
        labelIdx[pk] = labels.length;
        labels.push(xLabel(r[x], grain, opts.locale));
        keys.push(rawKey(r[x]));
      }
      var mk = series ? memberKey(r[series]) : String(y);
      if (!has(cells, mk)) { cells[mk] = {}; order.push(mk); }
      cells[mk][labelIdx[pk]] = toNum(r[y]);
    }
    function column(mk) {
      return labels.map(function (l, li) {
        return has(cells[mk], li) ? cells[mk][li] : null;
      });
    }
    var datasets = [];
    var shown = order.slice(0, maxS);
    var rest = order.slice(maxS);
    shown.forEach(function (mk) {
      datasets.push({ label: mk === NULL_KEY ? NULL_LABEL : mk, key: mk, data: column(mk), other: false });
    });
    var dropped = 0;
    if (rest.length) {
      if (opts.fold !== false) {
        var data = labels.map(function (l, li) {
          var t = 0;
          var seen = false;
          for (var k = 0; k < rest.length; k++) {
            if (has(cells[rest[k]], li) && cells[rest[k]][li] !== null) { t += cells[rest[k]][li]; seen = true; }
          }
          return seen ? t : null;
        });
        datasets.push({ label: 'Other', key: null, data: data, other: true });
      } else {
        dropped = rest.length;
      }
    }
    return { labels: labels, keys: keys, datasets: datasets, dropped: dropped };
  }

  function metaFor(tile, col) {
    var ms = tile.measures || [];
    for (var i = 0; i < ms.length; i++) {
      if (ms[i].column === col) { return ms[i]; }
    }
    return { kind: 'sum', column: col };
  }

  function groupBy(rows, col) {
    var out = {};
    for (var i = 0; i < rows.length; i++) {
      var k = memberKey(rows[i][col]);
      if (!has(out, k)) { out[k] = []; }
      out[k].push(rows[i]);
    }
    return out;
  }

  // Largest value first, stable, nulls last.
  function rankDesc(items) {
    var idx = items.map(function (m, i) { return { m: m, i: i }; });
    idx.sort(function (a, b) {
      var x = toNum(a.m.value);
      var y = toNum(b.m.value);
      if (x === null && y === null) { return a.i - b.i; }
      if (x === null) { return 1; }
      if (y === null) { return -1; }
      return y - x || a.i - b.i;
    });
    return idx.map(function (e) { return e.m; });
  }

  // C-07: the compiled Other row, confirmed from the data. A top+other binding ends in an Other row only
  // when more than `top` members exist, so the row is Other only when there are top + 1 rows and the
  // last one is named Other in the member column.
  function compiledOther(tile, rows, col) {
    if (!tile.other_row || !rows || !rows.length || !col) { return null; }
    var top = toNum(tile.other_top);
    if (top === null || rows.length !== top + 1) { return null; }
    var last = rows[rows.length - 1];
    return last && has(last, col) && String(last[col]) === 'Other' ? last : null;
  }

  // Natural order of member keys (numbers inside text compare as numbers; the null member last).
  function naturalOrder(items) {
    function cmp(a, b) {
      if (a === NULL_KEY || b === NULL_KEY) { return a === b ? 0 : (a === NULL_KEY ? 1 : -1); }
      var pa = String(a).match(/\d+|\D+/g) || [];
      var pb = String(b).match(/\d+|\D+/g) || [];
      for (var i = 0; i < pa.length && i < pb.length; i++) {
        var da = /^\d/.test(pa[i]);
        var db = /^\d/.test(pb[i]);
        if (da && db) {
          var d = Number(pa[i]) - Number(pb[i]);
          if (d !== 0) { return d; }
        } else if (pa[i] !== pb[i]) {
          return pa[i] < pb[i] ? -1 : 1;
        }
      }
      return pa.length - pb.length;
    }
    var idx = items.map(function (m, i) { return { m: m, i: i }; });
    idx.sort(function (a, b) { return cmp(a.m.key, b.m.key) || a.i - b.i; });
    return idx.map(function (e) { return e.m; });
  }

  // raw keeps the real values (tooltips, the hidden table); data is what is plotted.
  function finishModel(model, count) {
    var cur = model.sets.filter(function (s) { return !s.ghost; });
    model.sets.forEach(function (s) { s.raw = s.data.slice(); });
    if (model.percent) {
      for (var i = 0; i < count; i++) {
        var t = 0;
        cur.forEach(function (s) { var n = toNum(s.raw[i]); if (n !== null) { t += n; } });
        cur.forEach(function (s) {
          var v = toNum(s.raw[i]);
          s.data[i] = v === null ? null : (t === 0 ? 0 : v / t * 100);
        });
      }
    }
    model.totals = [];
    for (var j = 0; j < count; j++) {
      var tot = 0;
      cur.forEach(function (s) { var n = toNum(s.raw[j]); if (n !== null) { tot += n; } });
      model.totals.push(tot);
    }
    return model;
  }

  function bucketLabels(cells, bucket, locale) {
    var labels = [];
    var tips = [];
    cells.forEach(function (c) {
      if (bucket === 'month') {
        labels.push(formatDate(c.key, 'month', locale));
        tips.push(formatDate(c.key, 'month', locale));
      } else if (bucket === 'week') {
        labels.push(formatDate(c.key, 'axis', locale));
        tips.push('Week of ' + formatDate(c.key, 'tooltip', locale));
      } else {
        labels.push(formatDate(c.key, 'axis', locale));
        tips.push(formatDate(c.key, 'tooltip', locale));
      }
    });
    return { labels: labels, tips: tips };
  }

  // Controlled time chart: one bucketSeries per y (or per member of `series`) over the range.
  function timeModel(tile, rows, range, ctx, locale) {
    rows = rows || [];
    var bucket = bucketFor(range.from, range.to);
    var ys = (tile.y || []).slice(0, tile.type === 'sparkline' ? 1 : 6);
    var entries = [];
    var dropped = 0;
    function run(meta, rws) { return bucketSeries(meta, rws, range, bucket, ctx); }
    if (tile.series && ys.length) {
      var meta = metaFor(tile, ys[0].column);
      var ranked = rankDesc(memberValues(meta, rows, range, tile.series, ctx));
      var groups = groupBy(rows, tile.series);
      var shown = ranked.slice(0, MAX_COLOURED);
      var rest = ranked.slice(MAX_COLOURED);
      var tones = assignTones(shown.length);
      // Under a page filter the tone stays with the member: it follows the unfiltered order.
      if (ctx && ctx.toneKeys) {
        tones = shown.map(function (m) { return toneAt(ctx.toneKeys.indexOf(m.key)); });
      }
      shown.forEach(function (m, i) {
        var g = groups[m.key] || [];
        entries.push({ label: m.member, col: ys[0].column, key: m.key, tone: tones[i], dashed: false,
          other: false, meta: meta, rows: g, series: run(meta, g) });
      });
      if (rest.length) {
        if (combines(meta)) {
          var restSeries = rest.map(function (m) { return run(meta, groups[m.key] || []); });
          var merged = restSeries[0].map(function (b0, bi) {
            var items = restSeries.map(function (rs) { return rs[bi]; });
            var allNa = items.every(function (it) { return it.na; });
            return { key: b0.key, from: b0.from, to: b0.to, value: allNa ? null : combineMembers(meta, items),
              na: allNa ? b0.na : null, parts: null };
          });
          entries.push({ label: 'Other', col: ys[0].column, key: null, tone: 'neutral', dashed: false,
            other: true, meta: meta, rows: null, series: merged });
        } else {
          dropped = rest.length;
        }
      }
    } else {
      var tn = assignTones(ys.length, ys.map(function (y) { return y.tone; }));
      ys.forEach(function (y, i) {
        var m = metaFor(tile, y.column);
        entries.push({ label: y.label || y.column, col: y.column, key: null, tone: tn[i], dashed: !!y.dashed,
          other: false, meta: m, rows: rows, series: run(m, rows) });
      });
    }
    var cellSrc = entries.length ? entries[0].series : run({ kind: 'sum', column: '' }, []);
    var cells = cellSrc.map(function (b) {
      return { key: b.key, from: b.from, to: b.to, row: bucket === 'day' ? b.from : b.from + '..' + b.to };
    });
    var bl = bucketLabels(cells, bucket, locale);
    var sets = entries.map(function (e) {
      return { label: e.label, col: e.col, key: e.key, tone: e.tone, dashed: e.dashed, other: e.other,
        ghost: false, data: e.series.map(function (b) { return toNum(b.value); }) };
    });
    var na = null;
    if (entries.length && entries.every(function (e) { return e.series.every(function (b) { return b.na; }); })) {
      na = entries[0].series[0].na;
    }
    var ghostKind = tile.type === 'sparkline' || tile.percent ? 'none' : (tile.ghost || 'none');
    var ghostNa = false;
    if (ghostKind !== 'none' && !na) {
      var pr = ghostKind === 'last_year'
        ? lastYearRange(range.from, range.to)
        : previousRange(range.from, range.to, (ctx && ctx.preset) || null);
      if (ctx && ctx.horizonStart && pr[0] < ctx.horizonStart) {
        ghostNa = true;
      } else {
        var glabel = ghostKind === 'last_year' ? 'Same days last year' : 'Previous period';
        var base = entries.filter(function (e) { return !e.other; });
        var multi = base.length > 1;
        base.forEach(function (e) {
          var gs = bucketSeries(e.meta, e.rows, { from: pr[0], to: pr[1] }, bucket, ctx);
          var data = cells.map(function (c, i) { return gs[i] ? toNum(gs[i].value) : null; });
          sets.push({ label: multi ? e.label + ': ' + glabel : glabel, col: e.col, key: e.key, tone: e.tone,
            dashed: true, other: false, ghost: true, of: entries.indexOf(e), data: data });
        });
      }
    }
    var tmodel = finishModel({
      bucket: bucket, labels: bl.labels, tips: bl.tips, cells: cells, sets: sets, percent: !!tile.percent,
      na: na, ghostNa: ghostNa, dropped: dropped, series: !!tile.series, axisLabel: 'Period'
    }, cells.length);
    var ttc = !tile.series ? targetCol(tile) : null;
    if (ttc && !tile.percent) {
      tmodel.target = bucketSeries(metaFor(tile, ttc), rows, range, bucket, ctx).map(function (b) { return toNum(b.value); });
    }
    return tmodel;
  }

  // Controlled chart whose x is a by dimension: one bar per member over the range.
  function memberModel(tile, rows, range, ctx, locale) {
    rows = rows || [];
    var ys = (tile.y || []).slice(0, 6);
    var first = ys.length ? metaFor(tile, ys[0].column) : { kind: 'sum', column: '' };
    var base = ys.length ? rankDesc(memberValues(first, rows, range, tile.x, ctx)) : [];
    // More than 12 members: the top 11 plus Other when every measure combines, else the top 12.
    var canFold = ys.every(function (y) { return combines(metaFor(tile, y.column)); });
    var cap = base.length > 12 && canFold ? 11 : 12;
    var shown = base.slice(0, cap);
    var rest = base.slice(cap);
    var tones = assignTones(ys.length, ys.map(function (y) { return y.tone; }));
    var dropped = 0;
    var withOther = rest.length > 0 && canFold;
    if (rest.length && !withOther) { dropped = rest.length; }
    var labels = shown.map(function (m) { return m.member; });
    var cells = shown.map(function (m) { return { key: m.key, row: m.key }; });
    var allNa = shown.length > 0;
    var sets = ys.map(function (y, yi) {
      var meta = metaFor(tile, y.column);
      var mv = yi === 0 ? base : memberValues(meta, rows, range, tile.x, ctx);
      var byKey = {};
      mv.forEach(function (m) { byKey[m.key] = m; });
      var data = shown.map(function (m) {
        var it = byKey[m.key];
        if (it && !it.na) { allNa = false; }
        return it && !it.na ? toNum(it.value) : null;
      });
      if (withOther) {
        data.push(combineMembers(meta, rest.map(function (m) { return byKey[m.key]; })));
      }
      return { label: y.label || y.column, col: y.column, key: null, tone: tones[yi], dashed: !!y.dashed,
        other: false, ghost: false, data: data };
    });
    if (withOther) {
      labels.push('Other');
      cells.push({ key: null, row: '', other: true });
    }
    var mmodel = finishModel({
      bucket: null, labels: labels, tips: null, cells: cells, sets: sets, percent: !!tile.percent,
      na: allNa ? 'nodata' : null, ghostNa: false, dropped: dropped, series: false, axisLabel: 'Category'
    }, labels.length);
    mmodel.members = true;
    var mtc = targetCol(tile);
    if (mtc && !tile.percent) {
      var mtm = metaFor(tile, mtc);
      var mtb = {};
      memberValues(mtm, rows, range, tile.x, ctx).forEach(function (m) { mtb[m.key] = m; });
      var mtd = shown.map(function (m) { var it = mtb[m.key]; return it && !it.na ? toNum(it.value) : null; });
      if (withOther) { mtd.push(combineMembers(mtm, rest.map(function (m) { return mtb[m.key]; }))); }
      mmodel.target = mtd;
    }
    return mmodel;
  }

  // Fixed tile (own window): the rows are drawn as returned.
  function fixedModel(tile, rows, locale) {
    rows = rows || [];
    var ys = tile.y || [];
    var labels;
    var keys;
    var sets = [];
    var dropped = 0;
    var series = false;
    var fixedOther = null;
    if (tile.series && ys.length) {
      series = true;
      var pv = pivot(rows, tile.x, tile.series, ys[0].column, tile.grain,
        { locale: locale, fold: !(tile.measures || []).length || metaFor(tile, ys[0].column).kind === 'sum' });
      labels = pv.labels;
      keys = pv.keys;
      dropped = pv.dropped;
      var tones = assignTones(pv.datasets.filter(function (d) { return !d.other; }).length);
      var ti = 0;
      sets = pv.datasets.map(function (d) {
        return { label: d.label, col: ys[0].column, key: d.key, tone: d.other ? 'neutral' : tones[ti++],
          dashed: false, other: d.other, ghost: false, data: d.data };
      });
    } else {
      labels = rows.map(function (r) { return xLabel(r[tile.x], tile.grain, locale); });
      keys = rows.map(function (r) { return rawKey(r[tile.x]); });
      fixedOther = compiledOther(tile, rows, tile.x);
      var use = ys.slice(0, MAX_COLOURED);
      dropped = Math.max(0, ys.length - MAX_COLOURED);
      var tn = assignTones(use.length, use.map(function (y) { return y.tone; }));
      sets = use.map(function (y, i) {
        return { label: y.label || y.column, col: y.column, key: null, tone: tn[i], dashed: !!y.dashed,
          other: false, ghost: false, data: rows.map(function (r) { return toNum(r[y.column]); }) };
      });
    }
    var model = {
      bucket: null, labels: labels, tips: null,
      cells: keys.map(function (k, ki) {
        return fixedOther && rows[ki] === fixedOther ? { key: null, row: '', other: true } : { key: k, row: k };
      }),
      sets: sets, percent: !!tile.percent, na: null, ghostNa: false, dropped: dropped, series: series,
      axisLabel: tile.x_label || 'Period'
    };
    if (tile.band && (tile.type === 'line' || tile.type === 'area')) {
      model.band = {
        low: rows.map(function (r) { return toNum(r[tile.band.low]); }),
        high: rows.map(function (r) { return toNum(r[tile.band.high]); }),
        label: tile.band.label || 'Range'
      };
    }
    var ftc = !series ? targetCol(tile) : null;
    if (ftc && !tile.percent) { model.target = rows.map(function (r) { return toNum(r[ftc]); }); }
    return finishModel(model, labels.length);
  }

  function hexAlpha(hex, a) {
    var n = parseInt(hex.slice(1), 16);
    return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
  }

  function fmtOptsOf(tile, locale, compact) {
    return { locale: locale, currency: tile.currency || undefined, unit: tile.unit || undefined, compact: !!compact };
  }

  var FONT = 'system-ui, -apple-system, Segoe UI, Roboto, sans-serif';

  function deltaText(cur, prev) {
    var d = deltaParts(cur, prev, 'none');
    return d ? d.text.replace(/^[\u25B2\u25BC] /, '') : null;
  }

  // Chart.js configuration from a model; colours only from THEMES by tone.
  function chartConfig(tile, model, theme, locale) {
    var th = has(THEMES, theme) ? THEMES[theme] : THEMES.dark;
    var kind = tile.type;
    var isBar = kind === 'bar' || kind === 'stacked_bar';
    var stacked = kind === 'stacked_bar';
    var spark = kind === 'sparkline';
    var horizontal = !!tile.horizontal && isBar;
    var vAxis = horizontal ? 'x' : 'y';
    var cAxis = horizontal ? 'y' : 'x';
    var fo = fmtOptsOf(tile, locale, false);
    var foc = fmtOptsOf(tile, locale, true);
    var datasets = model.sets.map(function (s) {
      var base = toneColor(theme, s.tone);
      var d = { label: s.label, data: s.data };
      if (isBar) {
        d.backgroundColor = s.ghost ? hexAlpha(base, 0.4) : base;
        if (!s.ghost && !s.other && (model.cells || []).some(function (c) { return c && c.other; })) {
          // the compiled or folded Other bar is neutral, the rest keep the series tone
          d.backgroundColor = model.cells.map(function (c) { return c && c.other ? th.neutral : base; });
        }
        d.borderColor = d.backgroundColor;
        d.borderWidth = 0;
        d.borderRadius = 3;
        d.borderSkipped = 'start';
        d.categoryPercentage = 0.8;
        d.barPercentage = 0.9;
        if (stacked) { d.stack = s.ghost ? 'ghost' : 'current'; }
      } else {
        var line = s.ghost ? hexAlpha(base, 0.4) : base;
        var area = kind === 'area' && !s.ghost;
        d.borderColor = line;
        d.backgroundColor = area ? hexAlpha(base, 0.25) : line;
        d.fill = area;
        d.borderWidth = 2;
        d.pointRadius = 0;
        d.pointHoverRadius = 4;
        d.tension = 0;
        if (s.ghost || s.dashed) { d.borderDash = [6, 4]; }
      }
      return d;
    });
    if (model.band) {
      datasets.push(
        { label: 'low', data: model.band.low, borderColor: 'transparent', backgroundColor: 'transparent',
          pointRadius: 0, fill: false, ic_hide: true },
        { label: model.band.label, data: model.band.high, borderColor: 'transparent',
          backgroundColor: hexAlpha(th.inward, 0.18), pointRadius: 0, fill: '-1' }
      );
    }
    var tgIndex = -1;
    if (model.target && !horizontal) {
      tgIndex = datasets.length;
      datasets.push({ type: 'line', label: 'Target', data: model.target, order: 0, borderColor: th.ink,
        backgroundColor: th.ink, borderWidth: 1.5, borderDash: [4, 4], pointRadius: 0, pointHoverRadius: 0,
        tension: 0, fill: false });
    }
    var scales = {};
    scales[cAxis] = { stacked: stacked, grid: { display: !isBar, color: th.grid }, border: { display: false },
      ticks: { color: th.muted, maxRotation: 0, autoSkip: true } };
    scales[vAxis] = { stacked: stacked, beginAtZero: isBar, grid: { color: th.grid }, border: { display: false },
      ticks: { color: th.muted, callback: function (v) { return formatValue(v, tile.format, foc); } } };
    if (model.percent) {
      scales[vAxis].max = 100;
      scales[vAxis].ticks.callback = function (v) { return formatValue(v, 'percent_value', { locale: locale }); };
    }
    if (tile.x_label) { scales[cAxis].title = { display: true, text: String(tile.x_label), color: th.muted }; }
    if (tile.y_label) { scales[vAxis].title = { display: true, text: String(tile.y_label), color: th.muted }; }
    if (spark) {
      scales.x.display = false;
      scales.y.display = false;
    }
    var options = {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        colors: { enabled: false },
        legend: {
          display: !spark && datasets.length > 1,
          labels: {
            color: th.muted,
            filter: function (item, data) { return !data.datasets[item.datasetIndex].ic_hide; }
          }
        },
        tooltip: {
          enabled: !spark,
          callbacks: {
            title: function (items) {
              var i = items && items.length ? items[0].dataIndex : 0;
              if (model.tips) { return model.tips[i]; }
              return items && items.length ? items[0].label : '';
            },
            label: function (c) {
              var s = model.sets[c.datasetIndex];
              if (!s) { return (c.datasetIndex === tgIndex ? 'Target: ' : '') + String(c.formattedValue); }
              var raw = s.raw ? s.raw[c.dataIndex] : c.parsed[vAxis];
              var out = s.label + ': ' + formatValue(raw, tile.format, fo);
              if ((stacked || model.percent) && !s.ghost) {
                var tot = model.totals ? model.totals[c.dataIndex] : 0;
                var n = toNum(raw);
                if (tot && n !== null) { out += ' (' + Math.round(n / tot * 100) + '% of total)'; }
              }
              return out;
            },
            afterBody: function (items) {
              if (!items || !items.length) { return ''; }
              var i = items[0].dataIndex;
              var g = null;
              for (var k = 0; k < model.sets.length; k++) {
                if (model.sets[k].ghost && model.sets[k].of === 0) { g = model.sets[k]; break; }
              }
              if (!g || !model.sets[0]) { return ''; }
              var t = deltaText(model.sets[0].raw[i], g.raw[i]);
              return t ? 'vs previous: ' + t : '';
            }
          }
        }
      },
      scales: scales
    };
    if (horizontal) { options.indexAxis = 'y'; }
    var tgv = targetVal(tile);
    if (tgv !== null && !spark && !model.percent) {
      options.plugins.annotation = { annotations: { target: targetAnnotation(th, vAxis, tgv, targetLabel(tile), false) } };
      scales[vAxis].suggestedMax = tgv;
    }
    return {
      type: isBar ? 'bar' : 'line',
      data: { labels: model.labels, datasets: datasets },
      options: options
    };
  }

  function chartSummary(tile, model, locale) {
    var s = null;
    for (var i = 0; i < model.sets.length; i++) {
      if (!model.sets[i].ghost) { s = model.sets[i]; break; }
    }
    var latest = null;
    if (s) {
      for (var j = s.raw.length - 1; j >= 0; j--) {
        var n = toNum(s.raw[j]);
        if (n !== null) { latest = n; break; }
      }
    }
    return (tile.title || tile.id) + ': ' + model.labels.length + ' points, latest ' +
      formatValue(latest, tile.format, fmtOptsOf(tile, locale, false));
  }

  core.tsMs = tsMs;
  core.isStale = isStale;
  core.periodKey = periodKey;
  core.xLabel = xLabel;
  core.owed = owed;
  core.pickStat = pickStat;
  core.pivot = pivot;
  core.bucketEnd = bucketEnd;
  core.combines = combines;
  core.timeModel = timeModel;
  core.memberModel = memberModel;
  core.fixedModel = fixedModel;
  core.timeChartConfig = chartConfig;
  core.fixedChartConfig = chartConfig;
  core.chartSummary = chartSummary;

  // ---------------------------------------------------------------- governed tables (UI-SPEC 11, C-03, C-11)

  var TIME_COL_RE = /^metric_time__([a-z]+)$/;

  function colMeta(tile, name) {
    var ms = tile.measures || [];
    for (var i = 0; i < ms.length; i++) {
      if (ms[i].column === name) { return ms[i]; }
    }
    return null;
  }

  function bucketText(key, bucket, locale) {
    return bucket === 'month' ? formatDate(key, 'month', locale) : formatDate(key, 'axis', locale);
  }

  // Stable sort of table entries by one value column; nulls last either way.
  function sortEnts(ents, col, dir) {
    var wrapped = ents.map(function (e) {
      var o = {};
      o[col] = e.vals[col];
      o.__e = e;
      return o;
    });
    return sortRows(wrapped, col, dir).map(function (w) { return w.__e; });
  }

  function partsPresent(meta, rows) {
    if (!rows.length) { return false; }
    var cols = [];
    var p = partCols(meta);
    if (meta.kind === 'formula') {
      var mp = meta.parts || {};
      Object.keys(mp).forEach(function (a) { cols.push(mp[a]); });
    } else {
      Object.keys(p).forEach(function (k) { cols.push(p[k]); });
    }
    if (!cols.length) { return false; }
    for (var i = 0; i < cols.length; i++) {
      if (!has(rows[0], cols[i])) { return false; }
    }
    return true;
  }

  // Total of one measure column over the rows of a fixed (snapshot) table.
  function fixedTotal(tile, meta, rows, keyCol) {
    var k = meta.kind;
    var c = meta.column;
    var i;
    if (k === 'sum') { return sumCol(rows, c); }
    if (k === 'min' || k === 'max') { return aggregate(meta, rows, '').value; }
    if (k === 'avg' || k === 'ratio' || k === 'formula') {
      return partsPresent(meta, rows) ? aggregate(meta, rows, '').value : null;
    }
    if (k === 'closing' || k === 'opening') {
      var tk = tile.grain ? 'metric_time__' + tile.grain : null;
      if (!tk) {
        // One row per member (a balance per warehouse): each member's own value, added (C-03).
        var own = rows.map(function (r) { return { value: toNum(r[c]), parts: null }; });
        return own.length ? combineMembers(meta, own) : null;
      }
      var groups = keyCol ? groupBy(rows, keyCol) : { all: rows };
      var items = [];
      Object.keys(groups).forEach(function (g) {
        var grp = groups[g];
        var pick = null;
        for (i = 0; i < grp.length; i++) {
          var t = rawKey(grp[i][tk]);
          if (t === '') { continue; }
          if (pick === null || (k === 'closing' ? t > pick : t < pick)) { pick = t; }
        }
        if (pick === null) { return; }
        var same = grp.filter(function (r) { return rawKey(r[tk]) === pick; });
        items.push({ value: sumCol(same, c), parts: null });
      });
      return items.length ? combineMembers(meta, items) : null;
    }
    return null;
  }

  function colLabel(c, metas) {
    var m = has(metas, c.column) ? metas[c.column] : null;
    return c.label || (m && m.label) || c.column;
  }

  function tableModel(tile, snap, st, ctx) {
    st = st || {};
    ctx = ctx || {};
    var locale = st.locale;
    var cols = tile.columns || [];
    var rows = filterRows((snap && snap.rows) || [], st.filters || {});
    var metas = {};
    cols.forEach(function (c) {
      var m = colMeta(tile, c.column);
      if (m) { metas[c.column] = m; }
    });
    var byCols = tile.by_columns || [];
    var keyCol = byCols.length ? byCols[0] : null;
    var measureBound = (tile.measures || []).length > 0;
    var range = st.range || null;
    var controlled = !!st.controlled && !!range && measureBound;
    var today = st.today || ctx.today || todayIn('UTC', Date.now());
    var mcols = cols.filter(function (c) { return has(metas, c.column); });
    var ents = [];
    var bucket = null;
    var mode = 'fixed';
    var grain = tile.grain || null;
    var na = null;
    function noteNa(list) {
      if (na !== null || !list.length) { return; }
      for (var q = 0; q < list.length; q++) { if (!list[q].na) { return; } }
      na = list[0].na;
    }

    if (controlled && !keyCol) {
      mode = 'buckets';
      bucket = bucketFor(range.from, range.to);
      var series = {};
      mcols.forEach(function (c) {
        series[c.column] = bucketSeries(metas[c.column], rows, range, bucket, ctx);
        noteNa(series[c.column]);
      });
      var skel = mcols.length ? series[mcols[0].column]
        : bucketSeries({ kind: 'sum', column: '' }, [], range, bucket, ctx);
      skel.forEach(function (b, bi) {
        var vals = {};
        var deltas = {};
        cols.forEach(function (c) {
          if (has(metas, c.column)) {
            var cur = series[c.column][bi].value;
            vals[c.column] = cur;
            if (c.delta) {
              deltas[c.column] = deltaParts(cur, bi > 0 ? series[c.column][bi - 1].value : null, c.good_direction);
            }
          } else if (c.column === DAY_COL) {
            vals[c.column] = b.key;
          } else {
            vals[c.column] = null;
          }
        });
        ents.push({ vals: vals, deltas: deltas, icRow: bucket === 'day' ? b.from : b.from + '..' + b.to,
          icSeries: null, bucketKey: b.key });
      });
      ents.reverse();
    } else if (controlled) {
      mode = 'members';
      var mv = {};
      var byKey = {};
      mcols.forEach(function (c) {
        mv[c.column] = memberValues(metas[c.column], rows, range, keyCol, ctx);
        noteNa(mv[c.column]);
        byKey[c.column] = {};
        mv[c.column].forEach(function (m) { byKey[c.column][m.key] = m; });
      });
      var groups = groupBy(rows, keyCol);
      var order = mcols.length ? mv[mcols[0].column] : [];
      order.forEach(function (m0) {
        var vals = {};
        var deltas = {};
        cols.forEach(function (c) {
          if (has(metas, c.column)) {
            var it = byKey[c.column][m0.key];
            vals[c.column] = it && !it.na ? it.value : null;
            if (c.delta) {
              var res = compareValue(metas[c.column], groups[m0.key] || [], range, 'previous_period', ctx);
              deltas[c.column] = res.na ? null : deltaParts(res.current, res.previous, c.good_direction);
            }
          } else if (c.column === keyCol) {
            vals[c.column] = m0.member;
          } else {
            vals[c.column] = null;
          }
        });
        ents.push({ vals: vals, deltas: deltas, icRow: m0.member, icSeries: null, bucketKey: null });
      });
      if (mcols.length) { ents = sortEnts(ents, mcols[0].column, 'desc'); }
    } else {
      // C-07: the compiled Other row is the last row of the snapshot; it is never a member.
      var allRows = (snap && snap.rows) || [];
      var otherRow = compiledOther(tile, allRows, keyCol);
      rows.forEach(function (row, ri) {
        var vals = {};
        var deltas = {};
        cols.forEach(function (c) {
          vals[c.column] = has(row, c.column) ? row[c.column] : null;
          if (c.delta) {
            deltas[c.column] = deltaParts(vals[c.column], ri > 0 ? rows[ri - 1][c.column] : null, c.good_direction);
          }
        });
        var icRow = '';
        var icSeries = null;
        if (measureBound) {
          if (grain) {
            icRow = rawKey(row['metric_time__' + grain]);
            if (keyCol) { icSeries = keyText(memberKey(row[keyCol])); }
          } else if (keyCol) {
            icRow = keyText(memberKey(row[keyCol]));
          }
        } else if (cols.length) {
          icRow = keyText(memberKey(row[cols[0].column]));
        }
        ents.push({ vals: vals, deltas: deltas, icRow: icRow, icSeries: icSeries, bucketKey: null,
          other: !!otherRow && row === otherRow });
      });
    }

    var sorted = ents;
    if (tile.sort && tile.sort.column && cols.some(function (c) { return c.column === tile.sort.column; })) {
      sorted = sortEnts(sorted, tile.sort.column, tile.sort.dir === 'asc' ? 'asc' : 'desc');
    }
    if (st.sort && st.sort.column && st.sort.dir) {
      sorted = sortEnts(sorted, st.sort.column, st.sort.dir);
    }
    if (sorted.some(function (e) { return e.other; })) {
      sorted = sorted.filter(function (e) { return !e.other; }).concat(sorted.filter(function (e) { return e.other; }));
    }

    var size = tile.max_rows || 25;
    var pages = Math.max(1, Math.ceil(sorted.length / size));
    var page = Math.min(Math.max(1, st.page || 1), pages);
    var pg = pageRows(sorted, page, size);
    var pageInfo = { page: page, pages: pages, size: size, from: pg.from, to: pg.to, total: pg.total,
      text: 'Rows ' + pg.from + '-' + pg.to + ' of ' + pg.total };

    var barMax = 0;
    var barCol = null;
    cols.forEach(function (c) { if (c.bar && !barCol) { barCol = c.column; } });
    if (barCol) {
      pg.rows.forEach(function (e) {
        var n = toNum(e.vals[barCol]);
        if (n !== null && Math.abs(n) > barMax) { barMax = Math.abs(n); }
      });
    }

    function fmtOf(c) {
      return {
        fmt: c.format || tile.format || 'number',
        opts: { locale: locale, currency: c.currency || tile.currency || undefined, unit: c.unit || tile.unit || undefined }
      };
    }

    function isDim(name) { return byCols.indexOf(name) >= 0; }

    function makeCell(c, ent) {
      var raw = ent.vals[c.column];
      var f = fmtOf(c);
      var cell = { kind: 'value', col: c.column, raw: raw, text: DASH, num: false, ic: false, owed: false,
        due: null, barPct: null };
      if (c.due) {
        var s = typeof raw === 'string' ? raw.slice(0, 10) : '';
        cell.text = isIso(s) ? formatDate(s, 'axis', locale) : (raw === null || raw === undefined ? DASH : String(raw));
        cell.due = dueState(raw, today, c.due.warn_days);
        return cell;
      }
      if (c.column === DAY_COL && ent.bucketKey) {
        cell.text = bucketText(ent.bucketKey, bucket, locale);
        return cell;
      }
      var tm = TIME_COL_RE.exec(c.column);
      if (tm && typeof raw === 'string') {
        cell.text = xLabel(raw, tm[1], locale);
        return cell;
      }
      if (raw === null || raw === undefined) { return cell; }
      var n = toNum(raw);
      if (n !== null && !isDim(c.column)) {
        cell.text = formatValue(n, f.fmt, f.opts);
        cell.num = true;
        cell.ic = !ent.other;
        cell.owed = owed(n, c.owed_when);
        if (barCol === c.column && barMax > 0) { cell.barPct = Math.round(Math.abs(n) / barMax * 1000) / 10; }
        return cell;
      }
      cell.text = String(raw);
      return cell;
    }

    var outCols = [];
    cols.forEach(function (c) {
      outCols.push({ kind: 'value', key: c.column, label: colLabel(c, metas), sortable: true });
      if (c.delta) { outCols.push({ kind: 'delta', key: c.column, label: colLabel(c, metas) + ' change', sortable: false }); }
    });

    var outRows = pg.rows.map(function (e) {
      var cells = [];
      cols.forEach(function (c) {
        var cell = makeCell(c, e);
        cells.push(cell);
        if (c.delta) {
          var d = e.deltas[c.column];
          cells.push({ kind: 'delta', col: c.column, text: d ? d.text : DASH, cls: d ? d.cls : 'ic-flat', num: true });
        }
      });
      return { cells: cells, icRow: e.icRow, icSeries: e.icSeries, other: !!e.other };
    });
    outCols.forEach(function (oc, ci) {
      oc.num = outRows.some(function (r) { return r.cells[ci] && r.cells[ci].num; });
    });

    var totalVals = null;
    var totals = null;
    if (tile.totals) {
      if (st.totalsCache) {
        totalVals = st.totalsCache;
      } else if (mode === 'fixed') {
        totalVals = cols.map(function (c) {
          if (has(metas, c.column)) { return fixedTotal(tile, metas[c.column], rows, keyCol); }
          if (c.total) {
            var seen = false;
            var t = 0;
            rows.forEach(function (r) {
              var n = toNum(r[c.column]);
              if (n !== null) { t += n; seen = true; }
            });
            return seen ? t : null;
          }
          return null;
        });
      } else {
        totalVals = totalsRow(cols, { rows: rows, range: range, rowsAre: mode === 'members' ? 'members' : 'buckets',
          keyCol: keyCol, ctx: ctx, queryRows: [] }, metas).map(function (x) { return x.value; });
      }
      totals = [];
      cols.forEach(function (c, ci) {
        var f = fmtOf(c);
        var v = totalVals[ci];
        var text = ci === 0 ? 'Total' : (toNum(v) === null ? DASH : formatValue(v, f.fmt, f.opts));
        totals.push({ kind: 'value', text: text, num: ci !== 0 && toNum(v) !== null });
        if (c.delta) { totals.push({ kind: 'delta', text: '', num: false }); }
      });
    }

    var caption = null;
    var bases = [];
    var seenBasis = {};
    var nBasis = 0;
    cols.forEach(function (c) {
      var m = has(metas, c.column) ? metas[c.column] : null;
      if (m && m.date_basis) {
        bases.push(colLabel(c, metas) + ' (' + m.date_basis + ')');
        if (!has(seenBasis, m.date_basis)) { seenBasis[m.date_basis] = true; nBasis += 1; }
      }
    });
    if (nBasis > 1) { caption = 'Each column counts by its own date: ' + bases.join(', ') + '.'; }

    return { mode: mode, bucket: bucket, columns: outCols, rows: outRows, totals: totals, totalVals: totalVals,
      na: na, caption: caption, pageInfo: pageInfo, sort: st.sort || null };
  }

  core.tableModel = tableModel;
  core.fixedTotal = fixedTotal;
  core.colMeta = colMeta;

  // ---------------------------------------------------------------- the nine new visuals and targets (UI-SPEC 5, 6)

  var SUMLIKE = { sum: 1, min: 1, max: 1, closing: 1, opening: 1 };
  var WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  var CAL_MAX_DAYS = 400;

  function themeOf(theme) { return has(THEMES, theme) ? THEMES[theme] : THEMES.dark; }

  function targetCol(tile) {
    var t = tile.target;
    return t && typeof t === 'object' && typeof t.column === 'string' && t.column ? t.column : null;
  }

  function targetVal(tile) {
    var t = tile.target;
    return t && typeof t === 'object' ? toNum(t.value) : null;
  }

  // "Target set by owner" only when the spec itself states the value.
  function targetLabel(tile) { return targetVal(tile) !== null ? 'Target set by owner' : 'Target'; }

  function targetAnnotation(th, scaleID, value, text, solid) {
    return {
      type: 'line',
      scaleID: scaleID,
      value: value,
      borderColor: th.ink,
      borderWidth: solid ? 3 : 1.5,
      borderDash: solid ? [] : [4, 4],
      label: { display: true, content: text, position: 'end', color: th.ink, backgroundColor: th.surface,
        font: { size: 11 } }
    };
  }

  // The target of a stat: {value, owner} or null. A column is re-aggregated by its own kind.
  function statTarget(tile, rows, range, ctx, today, locale) {
    var t = tile.target;
    if (!t || typeof t !== 'object') { return null; }
    var v = targetVal(tile);
    if (v !== null) { return { value: v, owner: true }; }
    var col = targetCol(tile);
    if (!col) { return null; }
    var n;
    if (range) {
      var rv = rangeValue(metaFor(tile, col), rows, range, ctx);
      n = rv !== null && typeof rv === 'object' ? null : toNum(rv);
    } else {
      n = toNum(pickStat(rows, { column: col, x: tile.x, grain: tile.grain, pick: tile.pick, compare: 'none' }, today, locale).value);
    }
    return n === null ? null : { value: n, owner: false };
  }

  function statProgress(value, target) {
    var v = toNum(value);
    if (v === null || !target || !(target.value > 0)) { return null; }
    var pct = v / target.value * 100;
    return { pct: pct, width: Math.max(0, Math.min(100, pct)) };
  }

  // ---- data for the category visuals: controlled (range) or fixed (rows as returned)

  function vizData(tile, rows, range, ctx, locale) {
    rows = rows || [];
    var ys = (tile.y || []).slice(0, 4);
    var tones = assignTones(ys.length, ys.map(function (y) { return y.tone; }));
    var V = { labels: [], tips: null, cells: [], ys: [], bucket: null, na: null, items: null, itemMeta: null,
      target: null, axisLabel: tile.x_label || 'Category' };
    var tc = targetCol(tile);
    function mk(y, i, data) {
      return { label: y.label || y.column, col: y.column, tone: tones[i], as: y.as || null, dashed: !!y.dashed, data: data };
    }
    if (!range) {
      V.labels = rows.map(function (r) { return xLabel(r[tile.x], tile.grain, locale); });
      var lastRow = compiledOther(tile, rows, tile.x);
      V.cells = rows.map(function (r) {
        var k = rawKey(r[tile.x]);
        return r === lastRow ? { key: k, row: k, other: true } : { key: k, row: k };
      });
      V.ys = ys.map(function (y, i) { return mk(y, i, rows.map(function (r) { return toNum(r[y.column]); })); });
      if (tc) { V.target = rows.map(function (r) { return toNum(r[tc]); }); }
      return V;
    }
    if (!tile.x || tile.x === DAY_COL) {
      var rg = range;
      var bucket = tile.type === 'calendar' ? 'day' : bucketFor(range.from, range.to);
      if (tile.type === 'calendar') {
        var lo = addDays(range.to, -(CAL_MAX_DAYS - 1));
        rg = { from: range.from < lo ? lo : range.from, to: range.to };
      }
      var all = ys.map(function (y) { return bucketSeries(metaFor(tile, y.column), rows, rg, bucket, ctx); });
      var src = all.length ? all[0] : bucketSeries({ kind: 'sum', column: '' }, [], rg, bucket, ctx);
      V.cells = src.map(function (b) {
        return { key: b.key, row: bucket === 'day' ? b.from : b.from + '..' + b.to };
      });
      var bl = bucketLabels(V.cells.map(function (c, i) { return { key: src[i].key }; }), bucket, locale);
      V.labels = bl.labels;
      V.tips = bl.tips;
      V.bucket = bucket;
      V.axisLabel = 'Period';
      V.ys = ys.map(function (y, i) { return mk(y, i, all[i].map(function (b) { return toNum(b.value); })); });
      if (all.length && all.every(function (s) { return s.every(function (b) { return b.na; }); })) { V.na = all[0][0].na; }
      if (tc) {
        V.target = bucketSeries(metaFor(tile, tc), rows, rg, bucket, ctx).map(function (b) { return toNum(b.value); });
      }
      return V;
    }
    // x is a by dimension: members over the range; ranked_bar and donut largest first, the rest in returned order
    var first = ys.length ? metaFor(tile, ys[0].column) : { kind: 'sum', column: '' };
    var mv0 = ys.length ? memberValues(first, rows, range, tile.x, ctx) : [];
    mv0 = tile.type === 'ranked_bar' || tile.type === 'donut' ? rankDesc(mv0) : naturalOrder(mv0);
    V.items = mv0;
    V.itemMeta = first;
    V.toneKeys = ctx && ctx.toneKeys ? ctx.toneKeys : null;
    V.labels = mv0.map(function (m) { return m.member; });
    V.cells = mv0.map(function (m) { return { key: m.key, row: m.member }; });
    var anyValue = false;
    V.ys = ys.map(function (y, i) {
      var mv = i === 0 ? mv0 : memberValues(metaFor(tile, y.column), rows, range, tile.x, ctx);
      var by = {};
      mv.forEach(function (m) { by[m.key] = m; });
      return mk(y, i, mv0.map(function (m0) {
        var it = by[m0.key];
        if (it && !it.na) { anyValue = true; }
        return it && !it.na ? toNum(it.value) : null;
      }));
    });
    if (mv0.length && !anyValue) { V.na = mv0[0].na || 'nodata'; }
    if (tc) {
      var tmeta = metaFor(tile, tc);
      var tby = {};
      memberValues(tmeta, rows, range, tile.x, ctx).forEach(function (m) { tby[m.key] = m; });
      V.target = mv0.map(function (m0) { var it = tby[m0.key]; return it && !it.na ? toNum(it.value) : null; });
    }
    return V;
  }

  function fixedItemMeta(tile) {
    var y0 = tile.y && tile.y.length ? tile.y[0].column : null;
    var m = y0 ? colMeta(tile, y0) : null;
    if (!m) { return null; }
    return has(SUMLIKE, m.kind) ? m : { kind: 'fixed', column: m.column };
  }

  function itemsOf(tile, V) {
    if (V.items) { return { meta: V.itemMeta, items: V.items }; }
    return {
      meta: fixedItemMeta(tile),
      items: V.labels.map(function (l, i) {
        var it = { member: l, key: V.cells[i].key, value: V.ys.length ? V.ys[0].data[i] : null, parts: null };
        if (V.cells[i].other) { it.fixedOther = true; }
        return it;
      })
    };
  }

  function barsHeight(n) { return Math.min(400, Math.max(120, n * 28 + 40)); }

  function calendarDays(V) {
    var sums = {};
    var order = [];
    V.cells.forEach(function (c, i) {
      var d = typeof c.row === 'string' ? c.row.slice(0, 10) : '';
      if (!isIso(d)) { return; }
      var n = V.ys.length ? V.ys[0].data[i] : null;
      if (n === null) { return; }
      if (!has(sums, d)) { sums[d] = 0; order.push(d); }
      sums[d] += n;
    });
    order.sort();
    return order.slice(-CAL_MAX_DAYS).map(function (d) { return { date: d, v: sums[d] }; });
  }

  // The normalised labels, cells and series a visual draws (also the source of its hidden table).
  function vizView(tile, V, locale) {
    var view = { labels: V.labels, cells: V.cells, axisLabel: V.axisLabel, folded: false, dropped: 0, height: null,
      series: V.ys.map(function (y) { return { label: y.label, col: y.col, data: y.data }; }), days: null };
    var type = tile.type;
    var res;
    if (type === 'ranked_bar' || type === 'donut') {
      var src = itemsOf(tile, V);
      // The compiled Other row (C-07) stays last and is never ranked as a member.
      var fo = src.items.filter(function (m) { return m.fixedOther; })[0] || null;
      var regular = src.items.filter(function (m) { return !m.fixedOther; });
      var nfo = fo ? 1 : 0;
      if (type === 'ranked_bar' && V.items && !tile.top && regular.length + nfo > 12) {
        // Controlled members without top: the top 11 plus Other, or the top 12 when the kind does not combine.
        res = combines(src.meta) ? topOther(src.meta, regular, 11 - nfo, true) : topOther(src.meta, regular, 12 - nfo, false);
      } else if (type === 'ranked_bar') {
        res = topOther(src.meta, regular, Math.min(50 - nfo, tile.top || 50), !!tile.other);
      } else if (regular.length + nfo > 5) {
        res = combines(src.meta) ? topOther(src.meta, regular, 4, true) : topOther(src.meta, regular, 5 - nfo, false);
      } else {
        res = topOther(src.meta, regular, 5, false);
      }
      if (fo) {
        var fitem = { member: 'Other', value: fo.value, parts: null, other: true };
        if (res.other && combines(src.meta)) {
          var merged = { member: 'Other', value: combineMembers(src.meta, [res.other, fitem]), parts: null, other: true };
          res = { members: res.members.slice(0, -1).concat([merged]), other: merged, dropped: res.dropped };
        } else {
          res = { members: res.members.concat([fitem]), other: fitem, dropped: res.dropped };
        }
      }
      var mem = res.members;
      view.labels = mem.map(function (m) { return m.member; });
      view.cells = mem.map(function (m) {
        return m.other ? { key: null, row: '', other: true } : { key: m.key, row: keyText(m.key) };
      });
      view.series = [{ label: V.ys.length ? V.ys[0].label : '', col: V.ys.length ? V.ys[0].col : '',
        data: mem.map(function (m) { return toNum(m.value); }) }];
      view.folded = !!res.other;
      view.dropped = res.dropped;
      view.axisLabel = 'Category';
    }
    if (type === 'calendar') {
      var days = calendarDays(V);
      view.days = days;
      view.labels = days.map(function (d) { return formatDate(d.date, 'tooltip', locale); });
      view.cells = days.map(function (d) { return { key: d.date, row: d.date }; });
      view.series = [{ label: V.ys.length ? V.ys[0].label : '', col: V.ys.length ? V.ys[0].col : '',
        data: days.map(function (d) { return d.v; }) }];
      view.axisLabel = 'Day';
    }
    if (type === 'ranked_bar' || type === 'funnel') { view.height = barsHeight(view.labels.length); }
    if (type === 'calendar') { view.height = 7 * 18 + 44; }
    return view;
  }

  // Pseudo model so the shared hidden table and chart summary also serve the new visuals.
  function vizPseudo(tile, view) {
    return {
      axisLabel: view.axisLabel || 'Category', labels: view.labels, cells: view.cells, series: false,
      sets: view.series.map(function (s) {
        return { label: s.label, col: s.col, key: null, other: false, ghost: false, raw: s.data, data: s.data };
      })
    };
  }

  // ---- Chart.js configs

  function vTick(tile, locale) {
    var foc = fmtOptsOf(tile, locale, true);
    return function (v) { return formatValue(v, tile.format, foc); };
  }

  function vOptions(th, tile, locale, o) {
    o = o || {};
    var cAxis = o.horizontal ? 'y' : 'x';
    var vAxis = o.horizontal ? 'x' : 'y';
    var scales = {};
    scales[cAxis] = { grid: { display: false, color: th.grid }, border: { display: false },
      ticks: { color: th.muted, maxRotation: 0, autoSkip: true } };
    scales[vAxis] = { beginAtZero: true, grid: { color: th.grid }, border: { display: false },
      ticks: { color: th.muted, callback: o.tick || vTick(tile, locale) } };
    if (o.stacked) { scales.x.stacked = true; scales.y.stacked = true; }
    var options = {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        colors: { enabled: false },
        legend: { display: !!o.legend, labels: { color: th.muted } },
        tooltip: { enabled: true, callbacks: o.callbacks || {} }
      },
      scales: scales
    };
    if (o.horizontal) { options.indexAxis = 'y'; }
    return options;
  }

  function addTarget(th, tile, options, datasets, V, vAxis) {
    var tv = targetVal(tile);
    var idx = -1;
    if (tv !== null) {
      options.plugins.annotation = { annotations: { target: targetAnnotation(th, vAxis, tv, targetLabel(tile), false) } };
      options.scales[vAxis].suggestedMax = tv;
    } else if (V && V.target) {
      idx = datasets.length;
      datasets.push({ type: 'line', label: 'Target', data: V.target, order: 0, borderColor: th.ink, backgroundColor: th.ink,
        borderWidth: 1.5, borderDash: [4, 4], pointRadius: 0, pointHoverRadius: 0, tension: 0, fill: false });
    }
    return idx;
  }

  function tipTitle(V) {
    return function (items) {
      var i = items && items.length ? items[0].dataIndex : 0;
      if (V.tips) { return V.tips[i]; }
      return items && items.length ? items[0].label : '';
    };
  }

  function seriesLabel(tile, V, locale, tgIdx) {
    var fo = fmtOptsOf(tile, locale, false);
    return function (c) {
      var s = V.ys[c.datasetIndex];
      if (!s) { return (c.datasetIndex === tgIdx ? 'Target: ' : '') + String(c.formattedValue); }
      return s.label + ': ' + formatValue(s.data[c.dataIndex], tile.format, fo);
    };
  }

  function comboConfig(tile, V, theme, locale) {
    var th = themeOf(theme);
    var datasets = V.ys.map(function (y) {
      var base = toneColor(theme, y.tone);
      if (y.as === 'line') {
        var d = { type: 'line', label: y.label, data: y.data, order: 0, borderColor: base, backgroundColor: base,
          borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0, fill: false };
        if (y.dashed) { d.borderDash = [6, 4]; }
        return d;
      }
      return { type: 'bar', label: y.label, data: y.data, order: 1, backgroundColor: base, borderColor: base,
        borderWidth: 0, borderRadius: 3, borderSkipped: 'start', categoryPercentage: 0.8, barPercentage: 0.9 };
    });
    var tg = { idx: -1 };
    var options = vOptions(th, tile, locale, { legend: V.ys.length > 1 || !!V.target });
    tg.idx = addTarget(th, tile, options, datasets, V, 'y');
    options.plugins.legend.display = datasets.length > 1;
    options.plugins.tooltip.callbacks = { title: tipTitle(V), label: seriesLabel(tile, V, locale, tg.idx) };
    return { type: 'bar', data: { labels: V.labels, datasets: datasets }, options: options };
  }

  function pairedConfig(tile, V, theme, locale) {
    var th = themeOf(theme);
    var fo = fmtOptsOf(tile, locale, false);
    var foc = fmtOptsOf(tile, locale, true);
    var ys = V.ys.slice(0, 2);
    var tones = assignTones(2, ys.map(function (y) { return y.tone; }));
    var datasets = ys.map(function (y, i) {
      var base = toneColor(theme, tones[i]);
      var data = y.data;
      if (tile.mirror && i === 1) {
        data = y.data.map(function (v) { return v === null ? null : -v; });
      }
      return { label: y.label, data: data, backgroundColor: base, borderColor: base, borderWidth: 0,
        borderRadius: 3, borderSkipped: 'start', categoryPercentage: 0.8, barPercentage: 0.9 };
    });
    var options = vOptions(th, tile, locale, {
      legend: true, stacked: !!tile.mirror,
      tick: function (v) { return formatValue(tile.mirror ? Math.abs(v) : v, tile.format, foc); },
      callbacks: {
        title: tipTitle(V),
        label: function (c) {
          var y = ys[c.datasetIndex];
          return y ? y.label + ': ' + formatValue(y.data[c.dataIndex], tile.format, fo) : String(c.formattedValue);
        }
      }
    });
    return { type: 'bar', data: { labels: V.labels, datasets: datasets }, options: options };
  }

  function agingConfig(tile, V, theme, locale) {
    var th = themeOf(theme);
    var y = V.ys[0] || { label: '', data: [] };
    var colors = V.labels.map(function (l, i) { return th.ord[Math.min(i, th.ord.length - 1)]; });
    var options = vOptions(th, tile, locale, { legend: false, callbacks: { title: tipTitle(V), label: seriesLabel(tile, V, locale, -1) } });
    return {
      type: 'bar',
      data: { labels: V.labels, datasets: [{ label: y.label, data: y.data, backgroundColor: colors, borderColor: colors,
        borderWidth: 0, borderRadius: 3, borderSkipped: 'start', categoryPercentage: 0.8, barPercentage: 0.9 }] },
      options: options
    };
  }

  // B = {actual, target, owner}; bands come from the tile.
  function bulletConfig(tile, B, theme, locale) {
    var th = themeOf(theme);
    var fo = fmtOptsOf(tile, locale, false);
    var bands = (tile.bands || []).map(toNum).filter(function (n) { return n !== null; }).slice(0, 3);
    var datasets = [];
    var lo = 0;
    bands.forEach(function (hi, i) {
      datasets.push({ label: 'Band ' + (i + 1), data: [[lo, hi]], backgroundColor: th.bands[i], borderColor: th.bands[i],
        borderWidth: 0, grouped: false, barPercentage: 1, categoryPercentage: 0.9, order: 2 });
      lo = hi;
    });
    var actualIdx = datasets.length;
    datasets.push({ label: tile.title || 'Actual', data: [B.actual], backgroundColor: th.inward, borderColor: th.inward,
      borderWidth: 0, grouped: false, barPercentage: 0.35, categoryPercentage: 0.9, order: 1 });
    var maxv = Math.max(toNum(B.actual) || 0, toNum(B.target) || 0, bands.length ? bands[bands.length - 1] : 0);
    var options = vOptions(th, tile, locale, { horizontal: true, legend: false, callbacks: {
      title: function () { return tile.title || ''; },
      label: function (c) {
        var out = 'Actual: ' + formatValue(B.actual, tile.format, fo);
        var tgt = toNum(B.target);
        var act = toNum(B.actual);
        if (tgt && act !== null) { out += ' (' + Math.round(act / tgt * 100) + '% of target)'; }
        return out;
      }
    } });
    options.interaction = { mode: 'nearest', intersect: true };
    options.plugins.tooltip.filter = function (item) { return item.datasetIndex === actualIdx; };
    options.scales.x.suggestedMax = maxv * 1.05;
    options.scales.y.display = false;
    if (toNum(B.target) !== null) {
      options.plugins.annotation = { annotations: { target: targetAnnotation(th, 'x', toNum(B.target),
        B.owner ? 'Target set by owner' : 'Target', true) } };
    }
    return { type: 'bar', data: { labels: [tile.title || ''], datasets: datasets }, options: options };
  }

  // Start and end of every bar; with first_last the first and last rows are totals from 0.
  function waterfallSteps(values, ends) {
    var out = [];
    var run = 0;
    var n = values.length;
    values.forEach(function (raw, i) {
      var v = toNum(raw);
      var total = ends !== 'none' && (i === 0 || i === n - 1);
      if (v === null) { out.push({ value: null, start: null, end: null, total: total }); return; }
      if (total) {
        out.push({ value: v, start: 0, end: v, total: true });
        if (i === 0) { run = v; }
      } else {
        out.push({ value: v, start: run, end: run + v, total: false });
        run += v;
      }
    });
    return out;
  }

  function waterfallConfig(tile, V, theme, locale) {
    var th = themeOf(theme);
    var fo = fmtOptsOf(tile, locale, false);
    var y = V.ys[0] || { label: '', data: [] };
    var steps = waterfallSteps(y.data, tile.ends === 'none' ? 'none' : 'first_last');
    var colors = steps.map(function (s) {
      if (s.total) { return th.neutral; }
      return s.value !== null && s.value < 0 ? th.outward : th.inward;
    });
    var options = vOptions(th, tile, locale, { legend: false, callbacks: {
      title: tipTitle(V),
      label: function (c) {
        var s = steps[c.dataIndex];
        if (!s || s.value === null) { return String(c.formattedValue); }
        var text = formatValue(s.value, tile.format, fo);
        return y.label + ': ' + (!s.total && s.value > 0 ? '+' + text : text);
      }
    } });
    return {
      type: 'bar',
      data: { labels: V.labels, datasets: [{ label: y.label,
        data: steps.map(function (s) { return s.value === null ? null : [s.start, s.end]; }),
        backgroundColor: colors, borderColor: colors, borderWidth: 0, borderRadius: 3, borderSkipped: false,
        categoryPercentage: 0.8, barPercentage: 0.9 }] },
      options: options
    };
  }

  function calendarColor(th, v, min, max) {
    if (!(max > min)) { return th.seq[0]; }
    var i = Math.min(4, Math.floor((v - min) / (max - min) * 5));
    return th.seq[Math.max(0, i)];
  }

  function calendarConfig(tile, V, theme, locale) {
    var th = themeOf(theme);
    var fo = fmtOptsOf(tile, locale, false);
    var days = calendarDays(V);
    var weeks = [];
    var data = [];
    if (days.length) {
      var w = bucketKey(days[0].date, 'week');
      var last = bucketKey(days[days.length - 1].date, 'week');
      while (w <= last) { weeks.push(w); w = addDays(w, 7); }
    }
    var min = null;
    var max = null;
    days.forEach(function (d) {
      if (min === null || d.v < min) { min = d.v; }
      if (max === null || d.v > max) { max = d.v; }
      var dow = new Date(toMs(d.date)).getUTCDay();
      data.push({ x: bucketKey(d.date, 'week'), y: ((dow + 6) % 7) + 1, v: d.v, d: d.date });
    });
    var nWeeks = Math.max(1, weeks.length);
    return {
      type: 'matrix',
      data: { datasets: [{
        label: V.ys.length ? V.ys[0].label : '',
        data: data,
        backgroundColor: function (c) { return calendarColor(th, c.raw ? c.raw.v : 0, min, max); },
        borderWidth: 0,
        width: function (c) { var a = c.chart.chartArea; return a ? Math.max(4, a.width / nWeeks - 2) : 10; },
        height: function (c) { var a = c.chart.chartArea; return a ? Math.max(4, a.height / 7 - 2) : 10; }
      }] },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          colors: { enabled: false },
          legend: { display: false },
          tooltip: { enabled: true, callbacks: {
            title: function (items) { return items && items.length ? formatDate(items[0].raw.d, 'tooltip', locale) : ''; },
            label: function (c) { return (V.ys.length ? V.ys[0].label : '') + ': ' + formatValue(c.raw.v, tile.format, fo); }
          } }
        },
        scales: {
          x: { type: 'category', labels: weeks, offset: true, grid: { display: false }, border: { display: false },
            ticks: { color: th.muted, maxRotation: 0, autoSkip: true,
              callback: function (v, i) { return weeks[i] ? formatDate(weeks[i], 'axis', locale) : ''; } } },
          y: { type: 'linear', min: 0.5, max: 7.5, reverse: true, offset: false, grid: { display: false },
            border: { display: false },
            ticks: { color: th.muted, stepSize: 1, callback: function (v) { return WEEKDAYS[v - 1] || ''; } } }
        }
      }
    };
  }

  function donutConfig(tile, V, theme, locale) {
    var th = themeOf(theme);
    var fo = fmtOptsOf(tile, locale, false);
    var view = vizView(tile, V, locale);
    var vals = view.series[0].data;
    var total = 0;
    vals.forEach(function (v) { if (v !== null && v > 0) { total += v; } });
    var ramp = th.ord.slice().reverse();
    var colors = view.cells.map(function (c, i) {
      if (c.other) { return th.neutral; }
      var ri = V.toneKeys ? V.toneKeys.indexOf(c.key) : i;
      return (ri >= 0 && ramp[ri]) || th.neutral;
    });
    var labels = view.labels.map(function (l, i) {
      var pct = total > 0 && vals[i] !== null ? Math.round(vals[i] / total * 100) : 0;
      return l + ' (' + pct + '%)';
    });
    return {
      type: 'doughnut',
      data: { labels: labels, datasets: [{ label: view.series[0].label, data: vals, backgroundColor: colors,
        borderColor: th.surface, borderWidth: 2 }] },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        cutout: '55%',
        plugins: {
          colors: { enabled: false },
          legend: { display: true, position: 'bottom', labels: { color: th.muted } },
          tooltip: { enabled: true, callbacks: {
            label: function (c) { return view.labels[c.dataIndex] + ': ' + formatValue(vals[c.dataIndex], tile.format, fo); }
          } }
        }
      }
    };
  }

  function rankedConfig(tile, V, theme, locale) {
    var th = themeOf(theme);
    var fo = fmtOptsOf(tile, locale, false);
    var view = vizView(tile, V, locale);
    var vals = view.series[0].data;
    var colors = view.cells.map(function (c) { return c.other ? th.neutral : th.inward; });
    var options = vOptions(th, tile, locale, { horizontal: true, legend: false, callbacks: {
      title: function (items) { return items && items.length ? items[0].label : ''; },
      label: function (c) { return view.series[0].label + ': ' + formatValue(vals[c.dataIndex], tile.format, fo); }
    } });
    options.scales.y.ticks.autoSkip = false;
    if (tile.cumulative) {
      var total = 0;
      vals.forEach(function (v) { if (v !== null && v > 0) { total += v; } });
      var cum = 0;
      var idx = -1;
      for (var i = 0; i < vals.length; i++) {
        if (vals[i] !== null && vals[i] > 0) { cum += vals[i]; }
        if (total > 0 && cum / total >= 0.8) { idx = i; break; }
      }
      if (idx >= 0) {
        var ann = targetAnnotation(th, 'y', idx, '80% of total', false);
        ann.borderDash = [6, 4];
        options.plugins.annotation = { annotations: { cumulative: ann } };
      }
    }
    return {
      type: 'bar',
      data: { labels: view.labels, datasets: [{ label: view.series[0].label, data: vals, backgroundColor: colors,
        borderColor: colors, borderWidth: 0, borderRadius: 3, borderSkipped: 'start', categoryPercentage: 0.8,
        barPercentage: 0.9 }] },
      options: options
    };
  }

  function funnelConfig(tile, V, theme, locale) {
    var th = themeOf(theme);
    var fo = fmtOptsOf(tile, locale, false);
    var y = V.ys[0] || { label: '', data: [] };
    var first = toNum(y.data[0]);
    var labels = V.labels.map(function (l, i) {
      var v = toNum(y.data[i]);
      if (first === null || first === 0 || v === null) { return l; }
      return l + ' (' + Math.round(v / first * 100) + '% of first)';
    });
    var colors = V.labels.map(function (l, i) { return th.ord[Math.min(i, th.ord.length - 1)]; });
    var options = vOptions(th, tile, locale, { horizontal: true, legend: false, callbacks: {
      title: function (items) { return items && items.length ? items[0].label : ''; },
      label: function (c) { return y.label + ': ' + formatValue(y.data[c.dataIndex], tile.format, fo); }
    } });
    options.scales.y.ticks.autoSkip = false;
    return {
      type: 'bar',
      data: { labels: labels, datasets: [{ label: y.label, data: y.data, backgroundColor: colors, borderColor: colors,
        borderWidth: 0, borderRadius: 3, borderSkipped: 'start', categoryPercentage: 0.8, barPercentage: 0.9 }] },
      options: options
    };
  }

  // Actual and target of a bullet tile: fixed uses the last row, controlled the range value.
  function bulletValues(tile, rows, range, ctx) {
    rows = rows || [];
    var out = { actual: null, target: null, owner: false, na: null };
    var tv = targetVal(tile);
    var tc = targetCol(tile);
    if (range) {
      var a = rangeValue(metaFor(tile, tile.column), rows, range, ctx);
      if (a !== null && typeof a === 'object') { out.na = a.na; return out; }
      out.actual = toNum(a);
      if (tv !== null) { out.target = tv; out.owner = true; }
      else if (tc) {
        var t = rangeValue(metaFor(tile, tc), rows, range, ctx);
        out.target = t !== null && typeof t === 'object' ? null : toNum(t);
      }
      return out;
    }
    var row = rows.length ? rows[rows.length - 1] : null;
    out.actual = row ? toNum(row[tile.column]) : null;
    if (tv !== null) { out.target = tv; out.owner = true; }
    else if (tc && row) { out.target = toNum(row[tc]); }
    return out;
  }

  var VIZ_BUILDERS = {
    combo: comboConfig, paired_bar: pairedConfig, aging: agingConfig, bullet: bulletConfig,
    waterfall: waterfallConfig, calendar: calendarConfig, donut: donutConfig, ranked_bar: rankedConfig,
    funnel: funnelConfig
  };

  core.themeOf = themeOf;
  core.statTarget = statTarget;
  core.statProgress = statProgress;
  core.targetAnnotation = targetAnnotation;
  core.vizData = vizData;
  core.vizView = vizView;
  core.vizPseudo = vizPseudo;
  core.waterfallSteps = waterfallSteps;
  core.calendarDays = calendarDays;
  core.bulletValues = bulletValues;
  core.comboConfig = comboConfig;
  core.pairedConfig = pairedConfig;
  core.agingConfig = agingConfig;
  core.bulletConfig = bulletConfig;
  core.waterfallConfig = waterfallConfig;
  core.calendarConfig = calendarConfig;
  core.donutConfig = donutConfig;
  core.rankedConfig = rankedConfig;
  core.funnelConfig = funnelConfig;
  core.VIZ_BUILDERS = VIZ_BUILDERS;
  core.barsHeight = barsHeight;

  // CORE-END (later sections of the pure core are inserted above this line)

  root.ICRenderCore = core;
  if (typeof document === 'undefined') {
    return;
  }

  /* ---------------------------------------------------------------- DOM part */

  var ALLOWED = {
    main: 1, header: 1, section: 1, div: 1, h1: 1, h2: 1, h3: 1, p: 1, span: 1, canvas: 1, table: 1,
    caption: 1, thead: 1, tbody: 1, tfoot: 1, tr: 1, th: 1, td: 1, button: 1, select: 1, option: 1,
    input: 1, label: 1
  };
  var WIDTH_CLASS = {
    quarter: 'ic-w-quarter', third: 'ic-w-third', half: 'ic-w-half',
    two_thirds: 'ic-w-two_thirds', full: 'ic-w-full'
  };
  var QID_RE = /^[a-z][a-z0-9_]{0,47}$/;
  var CHART_TYPES = { line: 1, area: 1, bar: 1, stacked_bar: 1, sparkline: 1 };

  function el(tag, className, text) {
    if (ALLOWED[tag] !== 1) { throw new Error('tag not allowed'); }
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function btn(className, text) {
    var b = el('button', className, text);
    b.type = 'button';
    return b;
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

  function naText(code) {
    if (code === 'custom') { return 'Not available for a custom range'; }
    if (code === 'range') { return 'Not available for this range'; }
    return 'No data yet';
  }

  function applyChartDefaults(theme) {
    var C = root.Chart;
    if (!C || !C.defaults) { return; }
    var th = THEMES[theme] || THEMES.dark;
    C.defaults.color = th.muted;
    C.defaults.borderColor = th.grid;
    if (C.defaults.font) {
      C.defaults.font.family = FONT;
      C.defaults.font.size = 12;
    }
  }

  // The hidden table that follows a chart canvas. Cells of current-range series carry the
  // raw value for the eval gate; ghost, Other and target cells carry none.
  function srTable(tile, model, locale) {
    var fo = fmtOptsOf(tile, locale, false);
    var table = el('table', 'ic-sr');
    table.appendChild(el('caption', null, tile.title || tile.id));
    var thead = el('thead');
    var hr = el('tr');
    hr.appendChild(el('th', null, model.axisLabel || 'Period'));
    model.sets.forEach(function (s) { hr.appendChild(el('th', null, s.label)); });
    thead.appendChild(hr);
    table.appendChild(thead);
    var tbody = el('tbody');
    model.labels.forEach(function (lab, i) {
      var tr = el('tr');
      tr.appendChild(el('th', null, lab));
      model.sets.forEach(function (s) {
        var raw = s.raw[i];
        var td = el('td', null, formatValue(raw, tile.format, fo));
        var n = toNum(raw);
        var cell = model.cells[i];
        if (!s.ghost && !s.other && n !== null && !(cell && cell.other)) {
          td.dataset.icValue = String(n);
          if (s.col) { td.dataset.icCol = s.col; }
          if (cell && cell.row !== undefined && cell.row !== null) { td.dataset.icRow = keyText(cell.row); }
          if (model.series && s.key !== undefined && s.key !== null) { td.dataset.icSeries = keyText(s.key); }
        }
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    return table;
  }

  function deltaLabel(kind, range) {
    if (kind === 'last_year') { return 'vs same days last year'; }
    var p = range.preset;
    if (p === 'this_month' || p === 'last_month') { return 'vs previous month'; }
    if (p === 'this_quarter') { return 'vs previous quarter'; }
    if (p === 'this_fy') { return 'vs previous year'; }
    var n = daysBetween(range.from, range.to) + 1;
    return n === 1 ? 'vs previous day' : 'vs previous ' + n + ' days';
  }

  // ---- page filters, drill and the wrapper messages (pure parts)

  var MAX_FILTER_VALUES = 50;

  // The measure a tile is ranked by: its first measure, else a plain sum of its first value column.
  function firstMeta(tile) {
    var ms = tile.measures || [];
    if (ms.length) { return ms[0]; }
    var col = tile.column || (tile.y && tile.y.length ? tile.y[0].column : '');
    return { kind: 'sum', column: col };
  }

  // The values of one filter column: members of the first source ordered by its total (largest
  // first), then members only later sources hold, in the order they appear. At most 50.
  // sources: [{meta, rows, range, ctx}] in tile order. A NULL member has the key NULL_KEY.
  function filterOptions(sources, column, cap) {
    var limit = cap || MAX_FILTER_VALUES;
    var out = [];
    var seen = {};
    var ranked = false;
    for (var i = 0; i < sources.length; i++) {
      var rows = sources[i].rows || [];
      if (!rows.length || !has(rows[0], column)) { continue; }
      var mv = memberValues(sources[i].meta, rows, sources[i].range, column, sources[i].ctx);
      if (!ranked) { mv = rankDesc(mv); ranked = true; }
      for (var j = 0; j < mv.length; j++) {
        if (has(seen, mv[j].key)) { continue; }
        seen[mv[j].key] = true;
        out.push({ key: mv[j].key, label: mv[j].member, value: mv[j].value });
      }
    }
    return out.slice(0, limit);
  }

  // Two-level drill applies to a controlled ranked_bar or donut with two by columns, x being the first.
  function isDrillable(tile) {
    var b = tile.by_columns || [];
    return (tile.type === 'ranked_bar' || tile.type === 'donut') && tile.follow === 'control'
      && b.length === 2 && tile.x === b[0];
  }

  // Level 2: the rows of one first-level member, and the tile reading its second by column.
  function drillLevel2(tile, rows, key) {
    var b = tile.by_columns || [];
    var t2 = {};
    Object.keys(tile).forEach(function (k) { t2[k] = tile[k]; });
    t2.x = b[1];
    var sub = (rows || []).filter(function (r) { return memberKey(r[b[0]]) === key; });
    return { tile: t2, rows: sub };
  }

  // A tile title for the Ask message: control and bidi characters out, no lone surrogate,
  // at most 120 code points.
  function cleanTitle(t) {
    var cps = Array.from(String(t === null || t === undefined ? '' : t).replace(/[\u0000-\u001F\u007F-\u009F\u202A-\u202E\u2066-\u2069]/g, ''));
    cps = cps.filter(function (cp) { return !(cp.length === 1 && cp >= '\uD800' && cp <= '\uDFFF'); });
    return cps.slice(0, 120).join('').trim();
  }

  core.filterOptions = filterOptions;
  core.isDrillable = isDrillable;
  core.drillLevel2 = drillLevel2;
  core.cleanTitle = cleanTitle;
  core.firstMeta = firstMeta;

  function build(rootEl, spec) {
    clear(rootEl);
    var locale = spec.number_locale || 'en-US';
    var formTz = spec.timezone || 'UTC';
    var dc = spec.date_control || null;
    var refresh = spec.refresh || {};
    document.title = String(spec.title || 'Dashboard');

    var mqLight = root.matchMedia ? root.matchMedia('(prefers-color-scheme: light)') : null;
    var mqPhone = root.matchMedia ? root.matchMedia('(max-width: 639px)') : null;
    var theme = mqLight && mqLight.matches ? 'light' : 'dark';
    applyChartDefaults(theme);

    var snaps = {};
    var recs = [];
    var sectionRecs = [];
    var marked = false;
    var sheet = null;
    var lastTap = null;
    var state = { range: null, focus: null, filters: {} };

    // The wrapper channel: the one place the frame talks to its parent. It carries a range, filter
    // values and a tile title the viewer already sees. The frame never listens.
    function postToWrapper(msg) {
      try { parent.postMessage(msg, '*'); } catch (e) { return; }
    }

    function onStateChange(st) {
      postToWrapper({ ic: 1, type: 'state', hash: safeHash({ range: st.range, filters: st.filters }) });
      return st;
    }

    function askAbout(tile) {
      var title = cleanTitle(tile.title);
      if (!title) { return; }
      postToWrapper({ ic: 1, type: 'ask', tile: tile.id, title: title });
    }

    var filterDefs = (spec.filters || []).slice(0, 3);
    function filterDef(id) {
      for (var i = 0; i < filterDefs.length; i++) { if (filterDefs[i].id === id) { return filterDefs[i]; } }
      return null;
    }
    // Filter values that still wait for the data to confirm them (restored from a link).
    var unchecked = {};

    if (dc) {
      var decoded = decodeState(location.hash);
      var r0 = decoded && decoded.range;
      if (r0 && r0.preset && (dc.presets || PRESETS).indexOf(r0.preset) < 0) { r0 = null; }
      // A shared link must not name a range outside the horizon: it would build millions of days.
      if (r0 && !r0.preset && (r0.from < horizonStart(todayForm(), dc.horizon_days) || r0.to > todayForm())) { r0 = null; }
      state.range = r0 || { preset: dc.default };
      // Restored filters: only known ids, at most 50 values; the data confirms the values later.
      var fdec = decoded && decoded.filters ? decoded.filters : {};
      Object.keys(fdec).forEach(function (id) {
        if (filterDef(id)) {
          state.filters[id] = fdec[id].slice(0, MAX_FILTER_VALUES);
          unchecked[id] = true;
        }
      });
    }

    // ---- header
    var head = el('header', 'ic-head');
    head.appendChild(el('h1', 'ic-title', spec.title));
    if (spec.subtitle) { head.appendChild(el('p', 'ic-sub', spec.subtitle)); }
    var asofEl = el('p', 'ic-asof', 'Waiting for the first refresh');
    var bar = el('div', 'ic-bar');
    head.appendChild(asofEl);
    head.appendChild(bar);
    rootEl.appendChild(head);

    function refreshMin(q) { return toNum(refresh[q]) || 15; }
    function todayForm() { return todayIn(formTz, Date.now()); }
    function isControlled(rec) { return !!dc && rec.tile.follow === 'control'; }
    function rangeIsDefault() { return !state.range || state.range.preset === dc.default; }

    // ---- first screen mark
    function firstScreenCheck() {
      if (marked) { return; }
      var any = false;
      for (var i = 0; i < recs.length; i++) {
        if (recs[i].sectionIndex === 0) {
          any = true;
          if (!recs[i].settled) { return; }
        }
      }
      if (!any) { return; }
      marked = true;
      if (root.performance && root.performance.mark) { root.performance.mark('ic-first-screen'); }
    }

    function settle(rec) {
      rec.settled = true;
      firstScreenCheck();
    }

    // ---- lazy charts
    function unmountChart(rec) {
      if (rec.chart) {
        try { rec.chart.destroy(); } catch (e) { rec.chart = null; }
        rec.chart = null;
      }
      if (rec.slot) {
        clear(rec.slot.holder);
        if (rec.slot.sr) {
          try { rec.body.removeChild(rec.slot.sr); } catch (e2) { rec.slot.sr = null; }
          rec.slot.sr = null;
        }
      }
    }

    function mountChart(rec) {
      var slot = rec.slot;
      if (!slot || rec.chart || !rec.visible) { return; }
      var built = slot.make();
      var canvas = el('canvas');
      canvas.role = 'img';
      canvas.ariaLabel = built.label;
      slot.holder.appendChild(canvas);
      rec.chart = new root.Chart(canvas, built.config);
      slot.sr = built.table;
      rec.body.appendChild(slot.sr);
      rec.wasMounted = true;
      settle(rec);
    }

    function placeChart(rec, cls, make, height) {
      var holder = el('div', cls);
      if (height) { holder.style.height = height + 'px'; }
      rec.body.appendChild(holder);
      rec.slot = { holder: holder, make: make, sr: null };
      mountChart(rec);
    }

    // ---- tile geometry
    function tileCtx(rec) {
      var snap = rec.snap;
      var zone = (snap && snap.timezone) || formTz;
      var today = todayIn(zone, Date.now());
      var pq = rec.tile.presets_qid;
      return {
        dayCol: DAY_COL,
        today: today,
        asOf: asOfDay(snap && snap.data_as_of, zone),
        horizonStart: rec.tile.horizon_days ? horizonStart(today, rec.tile.horizon_days) : null,
        presetRows: pq && snaps[pq] ? (snaps[pq].rows || []) : null,
        preset: null,
        zone: zone,
        toneKeys: rec.toneKeys || null
      };
    }

    function tileRange(ctx) {
      var r = state.range;
      if (r.preset) {
        var pr = presetRange(r.preset, 'current', ctx.today, dc.fy_start_month);
        ctx.preset = r.preset;
        return { from: pr[0], to: pr[1], preset: r.preset };
      }
      ctx.preset = null;
      return { from: r.from, to: r.to, preset: null };
    }

    function rangeText(range) {
      if (range.preset) { return PRESET_LABELS[range.preset]; }
      return range.from === range.to
        ? formatDate(range.from, 'axis', locale)
        : formatDate(range.from, 'axis', locale) + ' ' + DASH + ' ' + formatDate(range.to, 'axis', locale);
    }

    // ---- page filters
    function colFilters() {
      var out = {};
      filterDefs.forEach(function (f) {
        var v = state.filters[f.id];
        if (v && v.length) { out[f.column] = v; }
      });
      return out;
    }

    function hasActiveFilters() { return Object.keys(colFilters()).length > 0; }

    function followsFilters(rec) { return !!dc && rec.tile.follow_filters === true && isControlled(rec); }

    // The values of one filter over the loaded snapshots of the tiles that follow it.
    function filterOptionsFor(f) {
      var sources = [];
      recs.forEach(function (rec) {
        if (!followsFilters(rec) || !rec.snap) { return; }
        var ctx = tileCtx(rec);
        var range = tileRange(ctx);
        sources.push({ meta: firstMeta(rec.tile), rows: rec.snap.rows || [], range: range, ctx: ctx });
      });
      return filterOptions(sources, f.column, MAX_FILTER_VALUES);
    }

    function valueText(key) { return key === NULL_KEY ? NULL_LABEL : String(key); }

    function crossFilter(column, key) {
      if (!dc || key === null || key === undefined) { return false; }
      for (var i = 0; i < filterDefs.length; i++) {
        var f = filterDefs[i];
        if (f.column !== column) { continue; }
        var known = filterOptionsFor(f).some(function (o) { return o.key === key; });
        if (!known) { return false; }
        var next = {};
        Object.keys(state.filters).forEach(function (id) { next[id] = state.filters[id]; });
        next[f.id] = [key];
        setState({ filters: next });
        return true;
      }
      return false;
    }

    function hasFilterColumn(col) {
      return filterDefs.some(function (f) { return f.column === col; });
    }

    // Drops restored values the data does not hold, once every followed snapshot has loaded.
    function reconcileFilters() {
      var changed = false;
      filterDefs.forEach(function (f) {
        if (!unchecked[f.id]) { return; }
        var followers = recs.filter(followsFilters);
        // A follower whose feed errored or never refreshed counts as loaded with no rows; one still
        // loading (no snapshot yet) holds the check back. With no follower nothing can confirm a value.
        var ready = followers.every(function (rec) {
          var sn = rec.tile.qid ? snaps[rec.tile.qid] : null;
          return !!sn && (!!rec.snap || !!sn.error || !sn.refreshed_at);
        });
        if (!ready) { return; }
        delete unchecked[f.id];
        var known = {};
        filterOptionsFor(f).forEach(function (o) { known[o.key] = true; });
        if (!followers.length) { known = {}; }
        var have = state.filters[f.id] || [];
        var keep = have.filter(function (v) { return has(known, v); });
        if (keep.length !== have.length) {
          changed = true;
          if (keep.length) { state.filters[f.id] = keep; } else { delete state.filters[f.id]; }
        }
      });
      return changed;
    }

    // ---- tap a date, drill
    function focusOn(from, to, text) {
      var prev = state.focus ? state.focus.prev : state.range;
      setState({ range: { from: from, to: to }, focus: { prev: prev, text: text } });
    }

    function tapBucket(rec, model, idx) {
      var cell = model.cells[idx];
      if (!cell || !model.bucket) { return; }
      if (mqPhone && mqPhone.matches) {
        var tapKey = rec.tile.id + ':' + idx;
        if (lastTap !== tapKey) { lastTap = tapKey; return; }
        lastTap = null;
      }
      var lo = horizonStart(todayForm(), dc.horizon_days);
      var hi = todayForm();
      var from = cell.key;
      var to = model.bucket === 'day' ? cell.key : bucketEnd(cell.key, model.bucket);
      var text;
      if (model.bucket === 'day') { text = formatDate(cell.key, 'tooltip', locale); }
      else if (model.bucket === 'month') { text = formatDate(cell.key, 'month', locale); }
      else { text = formatDate(from, 'axis', locale) + ' ' + DASH + ' ' + formatDate(to, 'axis', locale); }
      if (from < lo) { from = lo; }
      if (to > hi) { to = hi; }
      if (from > to) { return; }
      focusOn(from, to, text);
    }

    function makeBuilder(rec, model, interactive) {
      return function () {
        var tile = rec.tile;
        var cfg = chartConfig(tile, model, theme, locale);
        if (interactive && tile.type !== 'sparkline') {
          cfg.options.onClick = function (evt, els, chart) {
            var hits = chart.getElementsAtEventForMode(evt, 'nearest', { intersect: true }, true);
            if (!hits || !hits.length) { return; }
            tapBucket(rec, model, hits[0].index);
          };
        } else if (!interactive && tile.type !== 'sparkline' && tile.x && tile.x !== DAY_COL && hasFilterColumn(tile.x)) {
          cfg.options.onClick = function (evt, els, chart) {
            var hits = chart.getElementsAtEventForMode(evt, 'nearest', { intersect: true }, true);
            if (!hits || !hits.length) { return; }
            var cell = model.cells[hits[0].index];
            if (cell && cell.key !== null && cell.key !== undefined) { crossFilter(tile.x, cell.key); }
          };
        }
        return { config: cfg, label: chartSummary(tile, model, locale), table: srTable(tile, model, locale) };
      };
    }

    function chartClass(tile) {
      if (tile.type === 'sparkline') { return 'ic-spark'; }
      return tile.horizontal ? 'ic-chart ic-chart-tall' : 'ic-chart';
    }

    function addNotes(rec, model) {
      if (model.ghostNa) { rec.body.appendChild(el('p', 'ic-na', 'Comparison not available for this range')); }
      if (model.dropped) {
        rec.body.appendChild(el('p', 'ic-legend-note', model.dropped + (model.members ? ' more not shown' : ' more series not shown')));
      }
    }

    // ---- tile drawing
    function addStatTarget(rec, value, target) {
      var pr = statProgress(value, target);
      if (!pr) { return; }
      var track = el('div', 'ic-bullet');
      var fill = el('span', 'ic-cellbar');
      fill.style.width = pr.width + '%';
      track.appendChild(fill);
      rec.body.appendChild(track);
      rec.body.appendChild(el('p', 'ic-stat-period', Math.round(pr.pct) + '% of target'));
      if (target.owner) { rec.body.appendChild(el('p', 'ic-stat-period', 'Target set by owner')); }
    }

    function drawStatControlled(rec, rows) {
      var tile = rec.tile;
      var meta = tile.column ? metaFor(tile, tile.column)
        : ((tile.measures && tile.measures[0]) || { kind: 'sum', column: tile.column });
      var ctx = tileCtx(rec);
      var range = tileRange(ctx);
      var fo = fmtOptsOf(tile, locale, true);
      var cur = rangeValue(meta, rows, range, ctx);
      if (cur !== null && typeof cur === 'object') {
        rec.body.appendChild(el('p', 'ic-na', naText(cur.na)));
        return;
      }
      var box = el('div', 'ic-stat');
      var isOwed = owed(cur, tile.owed_when);
      var value = el('div', isOwed ? 'ic-stat-value ic-owed' : 'ic-stat-value', formatValue(cur, tile.format, fo));
      var n = toNum(cur);
      if (n !== null) {
        value.dataset.icValue = String(n);
        value.dataset.icCol = meta.column;
        value.dataset.icRow = range.from + '..' + range.to;
      }
      value.addEventListener('click', function () { askAbout(tile); });
      box.appendChild(value);
      if (isOwed) { box.appendChild(el('span', 'ic-owed-badge', 'Owed')); }
      box.appendChild(el('span', 'ic-stat-period', rangeText(range)));
      var cmp = tile.comparison || 'previous_period';
      var kinds = cmp === 'both' ? ['previous_period', 'last_year']
        : (cmp === 'previous_period' || cmp === 'last_year' ? [cmp] : []);
      var msgs = [];
      kinds.forEach(function (k) {
        var res = compareValue(meta, rows, range, k, ctx);
        if (res.na) {
          if (msgs.indexOf(naText(res.na)) < 0) { msgs.push(naText(res.na)); }
          return;
        }
        var dp = deltaParts(res.current, res.previous, tile.good_direction);
        if (dp) { box.appendChild(el('span', 'ic-delta ' + dp.cls, dp.text + ' ' + deltaLabel(k, range))); }
      });
      rec.body.appendChild(box);
      addStatTarget(rec, cur, statTarget(tile, rows, range, ctx, ctx.today, locale));
      msgs.forEach(function (m) { rec.body.appendChild(el('p', 'ic-na', m)); });
      if (tile.sparkline) {
        var model = timeModel({ type: 'sparkline', id: tile.id, title: tile.title, format: tile.format,
          currency: tile.currency, unit: tile.unit, measures: tile.measures,
          y: [{ column: meta.column, label: tile.title || meta.column }] }, rows, range, ctx, locale);
        if (!model.na && model.labels.length > 1) {
          placeChart(rec, 'ic-spark', makeBuilder(rec, model, false));
        }
      }
    }

    function drawStatFixed(rec, rows) {
      var tile = rec.tile;
      var ctx = tileCtx(rec);
      var picked = pickStat(rows, tile, ctx.today, locale);
      var box = el('div', 'ic-stat');
      var isOwed = owed(picked.value, tile.owed_when);
      var value = el('div', isOwed ? 'ic-stat-value ic-owed' : 'ic-stat-value',
        formatValue(picked.value, tile.format, fmtOptsOf(tile, locale, true)));
      var n = toNum(picked.value);
      if (n !== null) {
        value.dataset.icValue = String(n);
        value.dataset.icCol = tile.column;
        value.dataset.icRow = picked.key;
      }
      value.addEventListener('click', function () { askAbout(tile); });
      box.appendChild(value);
      if (isOwed) { box.appendChild(el('span', 'ic-owed-badge', 'Owed')); }
      if (picked.period) { box.appendChild(el('span', 'ic-stat-period', picked.period)); }
      if (tile.compare && tile.compare !== 'none') {
        var dp = deltaParts(picked.value, picked.previous, tile.good_direction);
        if (dp) {
          box.appendChild(el('span', 'ic-delta ' + dp.cls,
            tile.compare_label ? dp.text + ' ' + tile.compare_label : dp.text));
        }
      }
      rec.body.appendChild(box);
      addStatTarget(rec, picked.value, statTarget(tile, rows, null, ctx, ctx.today, locale));
      if (tile.sparkline && tile.x && rows.length > 1) {
        var st = { type: 'sparkline', id: tile.id, title: tile.title, x: tile.x, grain: tile.grain,
          format: tile.format, currency: tile.currency, unit: tile.unit, measures: tile.measures,
          y: [{ column: tile.column, label: tile.title || tile.column }] };
        placeChart(rec, 'ic-spark', makeBuilder({ tile: st, body: rec.body }, fixedModel(st, rows, locale), false));
      }
    }

    function drawChartControlled(rec, rows) {
      var tile = rec.tile;
      var ctx = tileCtx(rec);
      var range = tileRange(ctx);
      var timey = !tile.x || tile.x === DAY_COL;
      var model = timey ? timeModel(tile, rows, range, ctx, locale) : memberModel(tile, rows, range, ctx, locale);
      if (model.na) {
        rec.body.appendChild(el('p', 'ic-na', naText(model.na)));
        return;
      }
      placeChart(rec, chartClass(tile), makeBuilder(rec, model, timey));
      addNotes(rec, model);
    }

    function drawChartFixed(rec, rows) {
      var model = fixedModel(rec.tile, rows, locale);
      placeChart(rec, chartClass(rec.tile), makeBuilder(rec, model, false));
      addNotes(rec, model);
    }

    // ---- the nine new visuals
    function drillChip(rec) {
      var chip = el('span', 'ic-chip', rec.drill.label);
      var x = btn('ic-chip-x', 'Clear');
      x.ariaLabel = 'Back to all';
      x.addEventListener('click', function () {
        rec.drill = null;
        redraw(rec);
      });
      chip.appendChild(x);
      rec.body.appendChild(chip);
    }

    // A tap on a category: drill into it (ranked bar and donut with two by columns), else filter by it.
    function tapMember(rec, tile, view, idx) {
      var cell = view.cells[idx];
      if (!cell || cell.other || cell.key === null || cell.key === undefined) { return; }
      if (isDrillable(rec.tile) && !rec.drill && tile === rec.tile) {
        rec.drill = { key: cell.key, label: view.labels[idx] };
        redraw(rec);
        return;
      }
      crossFilter(tile.x, cell.key);
    }

    function drawViz(rec, rows) {
      var tile = rec.tile;
      var ctx = tileCtx(rec);
      var range = isControlled(rec) ? tileRange(ctx) : null;
      if (rec.drill && range && isDrillable(tile)) {
        var l2 = drillLevel2(tile, rows, rec.drill.key);
        if (l2.rows.length) {
          tile = l2.tile;
          rows = l2.rows;
          ctx.toneKeys = null;
          drillChip(rec);
        } else {
          rec.drill = null;
        }
      }
      var builder = VIZ_BUILDERS[tile.type];
      if (tile.type === 'bullet') {
        var B = bulletValues(tile, rows, range, ctx);
        if (B.na) {
          rec.body.appendChild(el('p', 'ic-na', naText(B.na)));
          return;
        }
        var bview = { axisLabel: 'Measure', labels: [tile.title || tile.column], cells: [{ key: null, row: '' }],
          series: [{ label: tile.title || tile.column, col: tile.column, data: [B.actual] }] };
        placeChart(rec, 'ic-bullet', function () {
          var pm = vizPseudo(tile, bview);
          return { config: builder(tile, B, theme, locale), label: chartSummary(tile, pm, locale), table: srTable(tile, pm, locale) };
        });
        return;
      }
      var V = vizData(tile, rows, range, ctx, locale);
      if (V.na) {
        rec.body.appendChild(el('p', 'ic-na', naText(V.na)));
        return;
      }
      var view = vizView(tile, V, locale);
      var tappable = !!V.bucket && tile.type !== 'calendar';
      var memberTappable = !V.bucket && tile.type !== 'calendar' && !!tile.x && tile.x !== DAY_COL
        && ((!!range && isDrillable(rec.tile)) || hasFilterColumn(tile.x));
      placeChart(rec, 'ic-chart', function () {
        var cfg = builder(tile, V, theme, locale);
        if (tappable) {
          cfg.options.onClick = function (evt, els, chart) {
            var hits = chart.getElementsAtEventForMode(evt, 'nearest', { intersect: true }, true);
            if (!hits || !hits.length) { return; }
            tapBucket(rec, V, hits[0].index);
          };
        } else if (memberTappable) {
          cfg.options.onClick = function (evt, els, chart) {
            var hits = chart.getElementsAtEventForMode(evt, 'nearest', { intersect: true }, true);
            if (!hits || !hits.length) { return; }
            tapMember(rec, tile, view, hits[0].index);
          };
        }
        var pm = vizPseudo(tile, view);
        return { config: cfg, label: chartSummary(tile, pm, locale), table: srTable(tile, pm, locale) };
      }, view.height);
      if (view.folded) { rec.body.appendChild(el('p', 'ic-legend-note', 'Smaller parts are grouped as Other')); }
      if (view.dropped) { rec.body.appendChild(el('p', 'ic-legend-note', view.dropped + ' more not shown')); }
    }

    // ---- tables
    function drawTable(rec) {
      var tile = rec.tile;
      var ctx = tileCtx(rec);
      var ctrl = isControlled(rec) && (tile.measures || []).length > 0;
      var range = ctrl ? tileRange(ctx) : null;
      var tb = rec.tbl || (rec.tbl = { sort: null, page: 1, totals: null });
      tb.page = 1;
      tb.totals = null;
      var capBox = el('div');
      var wrap = el('div', 'ic-table-wrap');
      var pager = null;
      rec.body.appendChild(capBox);
      rec.body.appendChild(wrap);
      var naBox = el('div');
      rec.body.appendChild(naBox);

      function paint() {
        var m = tableModel(tile, rec.fsnap || rec.snap, { controlled: ctrl, range: range, sort: tb.sort, page: tb.page,
          locale: locale, today: ctx.today, totalsCache: tb.totals }, ctx);
        tb.totals = m.totalVals;
        tb.page = m.pageInfo.page;
        if (m.caption && !capBox.firstChild) { capBox.appendChild(el('p', 'ic-basis', m.caption)); }
        clear(naBox);
        if (m.na) { naBox.appendChild(el('p', 'ic-na', naText(m.na))); }
        clear(wrap);
        var table = el('table', 'ic-table');
        var thead = el('thead');
        var hr = el('tr');
        m.columns.forEach(function (oc) {
          var th = el('th', oc.num ? 'ic-num' : null);
          if (oc.sortable) {
            var active = tb.sort && tb.sort.column === oc.key ? tb.sort.dir : null;
            var tri = active === 'desc' ? ' \u25BC' : (active === 'asc' ? ' \u25B2' : '');
            var b = btn('ic-sort', oc.label + tri);
            b.addEventListener('click', function () {
              var cur = tb.sort && tb.sort.column === oc.key ? tb.sort.dir : null;
              if (!cur) { tb.sort = { column: oc.key, dir: 'desc' }; }
              else if (cur === 'desc') { tb.sort = { column: oc.key, dir: 'asc' }; }
              else { tb.sort = null; }
              tb.page = 1;
              paint();
            });
            th.appendChild(b);
          } else {
            th.textContent = oc.label;
          }
          hr.appendChild(th);
        });
        thead.appendChild(hr);
        table.appendChild(thead);
        var tbody = el('tbody');
        m.rows.forEach(function (r) {
          var tr = el('tr');
          r.cells.forEach(function (cell) {
            var cls = cell.num ? 'ic-num' : '';
            if (cell.kind === 'delta') {
              var dtd = el('td', 'ic-num');
              dtd.appendChild(el('span', 'ic-delta ' + cell.cls, cell.text));
              tr.appendChild(dtd);
              return;
            }
            var overdue = cell.due && cell.due.state === 'overdue';
            if (cell.owed || overdue) { cls += ' ic-owed'; }
            var td = el('td', cls.trim() || null);
            if (cell.barPct !== null || cell.owed || cell.due) {
              if (cell.barPct !== null) {
                var bar = el('span', 'ic-cellbar');
                bar.style.width = cell.barPct + '%';
                td.appendChild(bar);
              }
              td.appendChild(el('span', null, cell.text));
              if (cell.owed) { td.appendChild(el('span', 'ic-owed-badge', 'Owed')); }
              if (overdue) { td.appendChild(el('span', 'ic-owed-badge', 'Overdue')); }
              else if (cell.due) { td.appendChild(el('span', 'ic-def', ' ' + cell.due.text)); }
            } else {
              td.textContent = cell.text;
            }
            if (cell.ic) {
              td.dataset.icValue = String(toNum(cell.raw));
              td.dataset.icCol = cell.col;
              td.dataset.icRow = r.icRow;
              if (r.icSeries !== null) { td.dataset.icSeries = r.icSeries; }
            }
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        if (m.totals) {
          var tfoot = el('tfoot');
          var tr2 = el('tr', 'ic-total');
          m.totals.forEach(function (t) { tr2.appendChild(el('td', t.num ? 'ic-num' : null, t.text)); });
          tfoot.appendChild(tr2);
          table.appendChild(tfoot);
        }
        wrap.appendChild(table);
        var pi = m.pageInfo;
        if (pi.total > pi.size) {
          if (!pager) {
            pager = el('div', 'ic-pager');
            rec.body.appendChild(pager);
          }
          clear(pager);
          var prev = btn('ic-prev', 'Previous');
          prev.disabled = pi.page <= 1;
          prev.addEventListener('click', function () { tb.page = Math.max(1, tb.page - 1); paint(); });
          var next = btn('ic-next', 'Next');
          next.disabled = pi.page >= pi.pages;
          next.addEventListener('click', function () { tb.page = tb.page + 1; paint(); });
          pager.appendChild(prev);
          pager.appendChild(el('span', null, pi.text));
          pager.appendChild(next);
        }
      }
      paint();
    }

    function drawBody(rec, rows) {
      var tile = rec.tile;
      if (tile.type === 'stat') {
        if (isControlled(rec)) { drawStatControlled(rec, rows); } else { drawStatFixed(rec, rows); }
      } else if (tile.type === 'table') {
        drawTable(rec);
      } else if (has(VIZ_BUILDERS, tile.type)) {
        drawViz(rec, rows);
      } else if (CHART_TYPES[tile.type] === 1) {
        if (isControlled(rec)) { drawChartControlled(rec, rows); } else { drawChartFixed(rec, rows); }
      } else {
        rec.body.appendChild(el('p', 'ic-empty', 'Not available yet'));
      }
    }

    function renderTags(rec) {
      var tile = rec.tile;
      if (!rec.meta) { return; }
      clear(rec.meta);
      if (tile.follow === 'now') { rec.meta.appendChild(el('span', 'ic-now', 'now')); }
      if (dc && tile.follow === 'fixed' && tile.type !== 'text' && !rangeIsDefault()) {
        rec.meta.appendChild(el('span', 'ic-fixed', 'Fixed period'));
      }
      if (dc && tile.type !== 'text') {
        filterDefs.forEach(function (f) {
          var on = state.filters[f.id];
          if (on && on.length && !(tile.follow_filters === true && isControlled(rec))) {
            rec.meta.appendChild(el('span', 'ic-fixed', 'Not filtered by ' + f.label));
          }
        });
      }
      if (rec.snap && rec.snap.data_as_of && rec.asofText) {
        rec.meta.appendChild(el('span', 'ic-tile-asof', rec.asofText));
      }
      if (rec.info) { rec.meta.appendChild(rec.info); }
    }

    // The member order of the unfiltered rows (series column or donut slices): tones follow it.
    function toneOrder(rec, allRows) {
      var tile = rec.tile;
      var col = tile.series || (tile.type === 'donut' ? tile.x : null);
      if (!col || !(tile.y && tile.y.length)) { return null; }
      var ctx = tileCtx(rec);
      var range = tileRange(ctx);
      var mv = rankDesc(memberValues(metaFor(tile, tile.y[0].column), allRows, range, col, ctx));
      return mv.map(function (m) { return m.key; });
    }

    function redraw(rec) {
      var tile = rec.tile;
      unmountChart(rec);
      clear(rec.body);
      clear(rec.status);
      rec.slot = null;
      rec.settled = false;
      if (tile.type === 'text') {
        rec.body.appendChild(el('p', 'ic-text', tile.text));
        settle(rec);
        return;
      }
      if (!tile.qid || !QID_RE.test(tile.qid)) {
        rec.status.appendChild(el('p', 'ic-error', 'Tile data unavailable'));
        settle(rec);
        return;
      }
      var snap = snaps[tile.qid];
      if (!snap || (tile.presets_qid && !snaps[tile.presets_qid])) { return; }
      rec.snap = snap;
      renderTags(rec);
      var rows = snap.rows || [];
      rec.fsnap = null;
      rec.toneKeys = null;
      if (followsFilters(rec) && hasActiveFilters()) {
        var kept = filterRows(rows, colFilters());
        rec.fsnap = {};
        Object.keys(snap).forEach(function (k) { rec.fsnap[k] = snap[k]; });
        rec.fsnap.rows = kept;
        try { rec.toneKeys = toneOrder(rec, rows); } catch (e0) { rec.toneKeys = null; }
        rows = kept;
      }
      if (snap.error) {
        rec.status.appendChild(el('p', 'ic-error', 'Data feed failed: ' + snap.error));
      } else if (isStale(snap.refreshed_at, refreshMin(tile.qid), Date.now())) {
        rec.status.appendChild(el('p', 'ic-stale', 'Data is stale'));
      }
      // A refreshed, error-free controlled tile with no rows is a real zero (C-03), not 'no data'.
      if (!rows.length && !(snap.refreshed_at && !snap.error && isControlled(rec) && !isMemberTile(rec.tile))) {
        if (!snap.error) {
          rec.status.appendChild(el('p', 'ic-empty',
            snap.refreshed_at ? 'No rows for this period' : 'Waiting for the first refresh'));
        }
        settle(rec);
        return;
      }
      try {
        drawBody(rec, rows);
      } catch (e) {
        clear(rec.body);
        rec.slot = null;
        rec.status.appendChild(el('p', 'ic-error', 'Tile could not be drawn'));
      }
      if (!rec.slot) { settle(rec); }
    }

    // A member visual (one value per member) with no rows has nothing to draw: it is not a zero.
    function isMemberTile(tile) {
      if (tile.type === 'stat') { return false; }
      if (tile.type === 'table') { return (tile.by_columns || []).length > 0; }
      if (tile.type === 'ranked_bar' || tile.type === 'donut') { return true; }
      return !!tile.x && tile.x !== DAY_COL && !TIME_COL_RE.test(tile.x);
    }

    // ---- freshness
    function updateFreshness() {
      var newest = null;
      var newestTs = null;
      var newestRef = null;
      var newestRefTs = null;
      var problem = false;
      Object.keys(snaps).forEach(function (q) {
        var s = snaps[q];
        if (!s) { return; }
        var a = tsMs(s.data_as_of);
        if (a !== null && (newest === null || a > newest)) { newest = a; newestTs = s.data_as_of; }
        var b = tsMs(s.refreshed_at);
        if (b !== null && (newestRef === null || b > newestRef)) { newestRef = b; newestRefTs = s.refreshed_at; }
        if (s.error || isStale(s.refreshed_at, refreshMin(q), Date.now())) { problem = true; }
      });
      var text = 'Waiting for the first refresh';
      if (newestTs) { text = 'Data as of ' + formatAsOf(newestTs, locale, formTz); }
      else if (newestRefTs) { text = 'Refreshed ' + formatAsOf(newestRefTs, locale, formTz); }
      asofEl.textContent = text;
      asofEl.className = problem ? 'ic-asof ic-owed' : 'ic-asof';
      recs.forEach(function (rec) {
        var s = rec.tile.qid ? snaps[rec.tile.qid] : null;
        rec.asofText = null;
        if (s && newest !== null) {
          var t = tsMs(s.data_as_of);
          var slack = (2 * refreshMin(rec.tile.qid) + 5) * 60000;
          if (t !== null && newest - t > slack) {
            rec.asofText = 'As of ' + formatAsOf(s.data_as_of, locale, s.timezone || formTz);
          }
        }
        renderTags(rec);
      });
    }

    // ---- date control
    function setState(next) {
      if (next.range) { state.range = next.range; }
      if (Object.prototype.hasOwnProperty.call(next, 'focus')) { state.focus = next.focus; }
      if (next.filters) { state.filters = next.filters; }
      refreshBar();
      recs.forEach(function (rec) {
        if (isControlled(rec)) { redraw(rec); } else { renderTags(rec); }
      });
      onStateChange(state);
    }

    function closeSheet() {
      sheet = null;
      refreshBar();
    }

    function buildSheet() {
      var s = el('div', 'ic-sheet');
      s.role = 'dialog';
      s.ariaModal = 'true';
      s.ariaLabel = 'Choose dates';
      var presets = dc.presets && dc.presets.length ? dc.presets : PRESETS;
      presets.forEach(function (p) {
        var on = !!state.range && state.range.preset === p;
        var b = btn(on ? 'ic-preset ic-on' : 'ic-preset', PRESET_LABELS[p]);
        b.ariaPressed = on ? 'true' : 'false';
        b.addEventListener('click', function () {
          setState({ range: { preset: p }, focus: null });
          closeSheet();
        });
        s.appendChild(b);
      });
      s.appendChild(el('p', null, 'Custom range'));
      var cr = state.range.preset
        ? presetRange(state.range.preset, 'current', todayForm(), dc.fy_start_month)
        : [state.range.from, state.range.to];
      var min = horizonStart(todayForm(), dc.horizon_days);
      var max = todayForm();
      function dateInput(label, v) {
        var inp = el('input', 'ic-date');
        inp.type = 'date';
        inp.min = min;
        inp.max = max;
        inp.value = v;
        inp.ariaLabel = label;
        return inp;
      }
      var fromIn = dateInput('From', cr[0]);
      var toIn = dateInput('To', cr[1]);
      s.appendChild(fromIn);
      s.appendChild(toIn);
      var msg = null;
      var apply = btn('ic-apply', 'Apply');
      apply.addEventListener('click', function () {
        var f = fromIn.value;
        var t = toIn.value;
        if (isIso(f) && isIso(t) && f <= t && f >= min && t <= max) {
          setState({ range: { from: f, to: t }, focus: null });
          closeSheet();
          return;
        }
        if (!msg) {
          msg = el('p', 'ic-na', 'Pick dates inside the stored period');
          s.appendChild(msg);
        }
      });
      s.appendChild(apply);
      var close = btn('ic-close', 'Close');
      close.addEventListener('click', closeSheet);
      s.appendChild(close);
      return s;
    }

    var rangeBtn = null;
    function refreshBar() {
      if (!dc) { return; }
      var had = null;
      var ae = document.activeElement;
      if (ae && bar.contains(ae)) {
        had = { tag: ae.tagName, cls: ae.className, label: ae.ariaLabel || ae.getAttribute('aria-label') };
      }
      clear(bar);
      var cr = state.range.preset
        ? presetRange(state.range.preset, 'current', todayForm(), dc.fy_start_month)
        : [state.range.from, state.range.to];
      var label = state.range.preset ? PRESET_LABELS[state.range.preset] : 'Custom range';
      rangeBtn.textContent = label + ' \u00B7 ' + rangeText({ from: cr[0], to: cr[1], preset: null });
      bar.appendChild(rangeBtn);
      if (state.focus) {
        var chip = el('span', 'ic-chip', 'Showing ' + state.focus.text);
        var x = btn('ic-chip-x', 'Clear');
        x.ariaLabel = 'Clear the focus';
        x.addEventListener('click', function () {
          var f = state.focus;
          if (f) { setState({ range: f.prev, focus: null }); }
        });
        chip.appendChild(x);
        bar.appendChild(chip);
      }
      filterDefs.forEach(function (f) { addFilterControl(f); });
      lastSig = filterSig();
      if (sheet) { bar.appendChild(sheet); }
      if (had) {
        var cand = bar.querySelectorAll(had.tag);
        for (var ci = 0; ci < cand.length; ci++) {
          var cl = cand[ci].ariaLabel || cand[ci].getAttribute('aria-label');
          if (cand[ci].className === had.cls && cl === had.label) { cand[ci].focus(); break; }
        }
      }
    }

    var lastSig = '';
    function filterSig() {
      return filterDefs.map(function (f) {
        return f.id + ':' + filterOptionsFor(f).map(function (o) { return o.key; }).join('|');
      }).join('/');
    }

    function addFilterControl(f) {
      var opts = filterOptionsFor(f);
      var cur = state.filters[f.id] || [];
      var sel = el('select', 'ic-filter');
      sel.ariaLabel = f.label;
      var all = el('option', null, 'All ' + f.label);
      all.value = '';
      sel.appendChild(all);
      opts.forEach(function (o) {
        var op = el('option', null, o.label);
        op.value = o.key;
        sel.appendChild(op);
      });
      sel.value = cur.length ? cur[0] : '';
      sel.addEventListener('change', function () {
        var v = sel.value;
        var next = {};
        Object.keys(state.filters).forEach(function (id) { next[id] = state.filters[id]; });
        if (v === '') {
          delete next[f.id];
        } else {
          var known = filterOptionsFor(f).some(function (o) { return o.key === v; });
          if (!known) { return; }
          next[f.id] = [v];
        }
        setState({ filters: next });
      });
      bar.appendChild(sel);
      if (cur.length) {
        var chip = el('span', 'ic-chip', f.label + ': ' + cur.map(valueText).join(', '));
        var x = btn('ic-chip-x', 'Clear');
        x.ariaLabel = 'Clear ' + f.label;
        x.addEventListener('click', function () {
          var next = {};
          Object.keys(state.filters).forEach(function (id) { if (id !== f.id) { next[id] = state.filters[id]; } });
          setState({ filters: next });
        });
        chip.appendChild(x);
        bar.appendChild(chip);
      }
    }

    if (dc) {
      rangeBtn = btn('ic-range');
      rangeBtn.ariaHasPopup = 'dialog';
      rangeBtn.addEventListener('click', function () {
        sheet = sheet ? null : buildSheet();
        refreshBar();
      });
      refreshBar();
    }

    // ---- sections and tiles
    var byId = {};
    (spec.tiles || []).forEach(function (t) { byId[t.id] = t; });
    (spec.sections || []).forEach(function (sec, si) {
      var secEl = el('section', 'ic-section');
      var shead = el('div', 'ic-section-head');
      if (sec.title) { shead.appendChild(el('h2', 'ic-section-title', sec.title)); }
      secEl.appendChild(shead);
      if (sec.description) { secEl.appendChild(el('p', 'ic-section-desc', sec.description)); }
      var grid = el('div', 'ic-grid');
      secEl.appendChild(grid);
      sectionRecs.push({ el: secEl, head: shead, grid: grid, title: sec.title || 'section', index: si,
        toggle: null, collapsed: false });
      (sec.tiles || []).forEach(function (id) {
        var tile = byId[id];
        if (!tile) { return; }
        var box = el('div', 'ic-tile ' + (WIDTH_CLASS[tile.width] || WIDTH_CLASS.half));
        box.dataset.icTile = tile.id;
        var rec = { tile: tile, el: box, meta: null, info: null, defBox: null, body: el('div'),
          status: el('div'), chart: null, slot: null, visible: false, wasMounted: false, settled: false,
          snap: null, sectionIndex: si, asofText: null };
        if (tile.type !== 'text') {
          var thead = el('div', 'ic-tile-head');
          thead.appendChild(el('h2', 'ic-tile-title', tile.title));
          rec.meta = el('span');
          thead.appendChild(rec.meta);
          box.appendChild(thead);
          var withInfo = (tile.measures || []).filter(function (m) { return m.label || m.description; });
          if (withInfo.length) {
            rec.defBox = el('div');
            rec.info = btn('ic-info', 'i');
            rec.info.ariaLabel = 'What is this number?';
            rec.info.ariaExpanded = 'false';
            rec.info.addEventListener('click', function () {
              var open = rec.defBox.firstChild !== null && rec.defBox.firstChild !== undefined;
              clear(rec.defBox);
              if (!open) {
                withInfo.forEach(function (m) {
                  var line = m.label && m.description ? m.label + ': ' + m.description : (m.label || m.description);
                  rec.defBox.appendChild(el('p', 'ic-def', line));
                });
              }
              rec.info.ariaExpanded = open ? 'false' : 'true';
              askAbout(tile);
            });
          }
          if (rec.defBox) { box.appendChild(rec.defBox); }
        }
        box.appendChild(rec.body);
        box.appendChild(rec.status);
        if (tile.note) { box.appendChild(el('p', 'ic-note', tile.note)); }
        grid.appendChild(box);
        recs.push(rec);
      });
      rootEl.appendChild(secEl);
    });

    // ---- phones: sections after the first collapse behind a button
    function setCollapsed(sr, on) {
      sr.collapsed = on;
      sr.grid.hidden = on;
      if (on) { sr.el.classList.add('ic-collapsed'); } else { sr.el.classList.remove('ic-collapsed'); }
      if (sr.toggle) {
        sr.toggle.ariaExpanded = on ? 'false' : 'true';
        sr.toggle.textContent = on ? 'Show' : 'Hide';
        sr.toggle.ariaLabel = (on ? 'Show ' : 'Hide ') + sr.title;
      }
    }

    function applyPhone(phone) {
      sectionRecs.forEach(function (sr) {
        if (sr.index === 0) { return; }
        if (phone && !sr.toggle) {
          sr.toggle = btn('ic-section-toggle');
          sr.toggle.addEventListener('click', function () { setCollapsed(sr, !sr.collapsed); });
          sr.head.appendChild(sr.toggle);
          setCollapsed(sr, true);
        } else if (!phone && sr.toggle) {
          sr.head.removeChild(sr.toggle);
          sr.toggle = null;
          setCollapsed(sr, false);
        }
      });
    }
    applyPhone(!!mqPhone && mqPhone.matches);
    if (mqPhone && mqPhone.addEventListener) {
      mqPhone.addEventListener('change', function () { applyPhone(mqPhone.matches); });
    }

    // ---- theme follows the device
    if (mqLight && mqLight.addEventListener) {
      mqLight.addEventListener('change', function () {
        theme = mqLight.matches ? 'light' : 'dark';
        applyChartDefaults(theme);
        recs.forEach(function (rec) {
          if (rec.chart) {
            unmountChart(rec);
            mountChart(rec);
          }
        });
      });
    }

    // ---- observers: draw near the viewport, destroy far away
    function recOf(target) {
      for (var i = 0; i < recs.length; i++) { if (recs[i].el === target) { return recs[i]; } }
      return null;
    }
    if (root.IntersectionObserver) {
      var nearObs = new root.IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          var rec = recOf(en.target);
          if (rec && en.isIntersecting) {
            rec.visible = true;
            mountChart(rec);
          }
        });
      }, { rootMargin: '300px 0px' });
      var farObs = new root.IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          var rec = recOf(en.target);
          if (!rec) { return; }
          if (en.isIntersecting) {
            if (rec.wasMounted) {
              rec.visible = true;
              mountChart(rec);
            }
          } else {
            rec.visible = false;
            unmountChart(rec);
          }
        });
      }, { rootMargin: '1500px 0px' });
      recs.forEach(function (rec) {
        nearObs.observe(rec.el);
        farObs.observe(rec.el);
      });
    } else {
      recs.forEach(function (rec) { rec.visible = true; });
    }

    // ---- data
    function redrawFor(q) {
      recs.forEach(function (rec) {
        if (rec.tile.qid === q || rec.tile.presets_qid === q) { redraw(rec); }
      });
      var dropped = reconcileFilters();
      if (dropped) {
        recs.forEach(function (rec) { if (followsFilters(rec)) { redraw(rec); } });
        recs.forEach(renderTags);
      }
      if (dc && (dropped || filterSig() !== lastSig)) { refreshBar(); }
      if (dropped) { onStateChange(state); }
      updateFreshness();
    }

    function load(qid) {
      getJSON('./data/' + qid).then(function (snap) {
        snaps[qid] = snap;
      }).catch(function (e) {
        var prev = snaps[qid] || { rows: [], columns: [], refreshed_at: null };
        snaps[qid] = {
          rows: prev.rows, columns: prev.columns, refreshed_at: prev.refreshed_at,
          data_as_of: prev.data_as_of, timezone: prev.timezone,
          error: 'could not load data' + (e && e.status ? ' (HTTP ' + e.status + ')' : '')
        };
      }).then(function () {
        redrawFor(qid);
        setTimeout(function () { load(qid); }, Math.max(60, refreshMin(qid) * 60) * 1000);
      });
    }

    recs.forEach(function (rec) {
      if (rec.tile.type === 'text') { redraw(rec); }
    });
    var qids = [];
    function addQid(q) { if (q && QID_RE.test(q) && qids.indexOf(q) < 0) { qids.push(q); } }
    [true, false].forEach(function (firstOnly) {
      recs.forEach(function (rec) {
        if ((rec.sectionIndex === 0) === firstOnly) {
          addQid(rec.tile.qid);
          addQid(rec.tile.presets_qid);
        }
      });
    });
    recs.forEach(function (rec) {
      if (rec.tile.type !== 'text' && (!rec.tile.qid || !QID_RE.test(rec.tile.qid))) { redraw(rec); }
    });
    // restored filter values nothing can confirm (no tile follows the filters) are dropped at once
    if (reconcileFilters()) { recs.forEach(renderTags); refreshBar(); onStateChange(state); }
    qids.forEach(load);
  }

  function start() {
    var rootEl = document.getElementById('ic-root');
    if (!rootEl) { return; }
    getJSON('./spec').then(function (spec) {
      build(rootEl, spec);
    }).catch(function (e) {
      clear(rootEl);
      rootEl.appendChild(el('p', 'ic-fail', 'Dashboard unavailable' + (e && e.status ? ' (HTTP ' + e.status + ')' : '')));
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})(this);

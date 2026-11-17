---
name: dashboard-authoring
description: House style for live business dashboards: the one-file HTML contract, Chart.js 4 usage, phone-first layout and the gold-means-owed colour rule. Use before writing or revising any dashboard.
license: Proprietary
---

# Dashboard authoring (house style)

This is procedure for the dashboard writer. Every write is a draft that waits for owner approval. Never say a dashboard is published.

## When to use and workflow

Use this before writing or revising any dashboard page.

1. Clarify the ask: which business question, which period, which breakdowns. Ask one short question if it is ambiguous, otherwise decide and state the assumption.
2. Find the tables and columns with `search_business_context`. Do not guess column names. Also search "dashboard idea": approved rules named `rule:dashboard-idea-...` come from the client's context pack and hold a question, a layout and ready queries (id, refresh_minutes, sql). Start from those queries and still run `preview_dashboard` on them. When it returns nothing, list the columns with `run_sql` on `information_schema.columns` for `table_schema = 'public'` and copy identifiers exactly as listed.
3. Write one aggregated SELECT per query. Aggregate in SQL: at most 500 rows come back (`sql_row_limit`), at most 20 queries per dashboard, `refresh_minutes` between 5 and 1440. Every query is read-only and passes the SQL guardrails.
4. Write the page from the example below. Keep it one file.
5. Run `preview_dashboard` and fix every finding. Re-run until it is clean, at most 3 rounds. Use its sample rows instead of extra `run_sql` calls.
6. Save with `save_dashboard_version` plus a one-line change note (what changed and why).
7. Tell the user the draft awaits their approval. Never say it is live or published.

You have three dashboard tools. `get_dashboard(slug)` returns the current code: start from it when revising, and change only what was asked. `preview_dashboard` lints the page and samples every query. `save_dashboard_version` stores the draft for owner approval.

## File contract

- One UTF-8 HTML5 file starting `<!doctype html>`, at most 256 KB (262,144 bytes).
- All CSS in one `<style>`, all JS in inline `<script>`. Images only as `data:` URIs. System font stack. No web fonts, no `<link rel=stylesheet>`, no `@import`.
- Data comes only from `fetch('./data/' + id + location.search, { cache: 'no-store' })`. The `?t=` view token rides on the page URL: forward it unchanged through `location.search` and never store, rewrite, log or send it anywhere else. Read `location` only as `location.search`.
- Response shape: `{query_id, columns, rows, refreshed_at, error}`. Rows are objects keyed by column name. Numerics are JSON numbers, dates are ISO strings, NULL is null.
- Before the first refresh, rows is empty and refreshed_at is null. After a failed refresh the last good rows come back plus `error` (a short message).
- Re-fetch no faster than every 60 seconds. One timer, set with a function (never a string).
- Alias every column uniquely: rows are objects keyed by column name, so duplicate names collapse.
- Query declaration is a JSON list `[{"id", "sql", "refresh_minutes"}]`. The id matches `[a-z][a-z0-9_]{0,47}` and is unique. The SQL is frozen per version: a change is a new version.
- Forbidden: `innerHTML`, `outerHTML` and `insertAdjacentHTML` (use `textContent`), any write to `location` (read only `location.search`), `location.assign`, `replace` and `reload`, `top`, `parent`, `opener`, `atob`, `String.fromCharCode`, `unescape`, URLs built at runtime from strings, `<meta http-equiv>`, inline `on...=` handlers (use `addEventListener`), `javascript:` URLs, `eval`, `new Function`, string `setTimeout`, `<base>`, `<iframe>`, `<object>`, `<embed>`, forms, meta refresh, `localStorage`, `sessionStorage`, `indexedDB`, cookies, `window.open`, `postMessage`, `document.write`, links other than a `#fragment`. Also forbidden, because the linter allowlists the shape of the code: a global (`window`, `self`, `globalThis`, `document`, `frames`) used other than as a dotted member access, so write `document.getElementById(...)` and never put a global in a variable; a computed key that is not an integer literal or a plain dotted name, so index with `rows[i]` or `datasets[0]`, never a string or a built key, and alias every SQL column to a plain name so dotted access works; and two string literals joined with `+`, so write whole strings, because URLs and API names must not be built at runtime. Also forbidden (Phase 12 sandbox rules): dotted self-references (`window.window`, `self.self`, `globalThis.globalThis`, `.top`, `.parent`, `.frames`); walking back from an element (`ownerDocument`, `defaultView`, `.view`, `parentNode`, `getRootNode`, `composedPath`, `contentWindow`, `contentDocument`); `dispatchEvent`; any computed write `x[k] = ...` (use a `Map` with `m.set(k, v)`, `push`, or build a new array with `map`); template literals with `${...}` and `.concat(` (join a variable and a whole literal with `+`); and WebRTC names.
- No absolute URL of any kind (no http, https, ws, wss or ftp) and no `//host` reference. The one exception is the W3C SVG namespace as an `xmlns` value on inline SVG, which is not a request. The only absolute path allowed is the pinned chart library below. A value starting with a backslash form (`/\host`, `\/host`, `\\host`) counts as `//host`.
- Inline SVG holds only chart shapes: `g`, `path`, `rect`, `circle`, `ellipse`, `line`, `polyline`, `polygon`, `text`, `tspan`, `defs`, `linearGradient`, `radialGradient`, `stop`, `clipPath`, `use`. No `<title>`, `<desc>`, `<style>`, `<script>` or HTML inside `<svg>` (use `aria-label` or `<text>`), and the text `http-equiv` may not appear anywhere.
- Any byte change is a new version. Never patch a saved version in place.

## Pinned chart library

The only external script is this exact line. Copy it character for character:

    <script src="/d/_lib/chart.js-4.5.1/chart.umd.min.js" integrity="sha256-SERKgtTty1vsDxll+qzd4Y2cF9swY9BCq62i9wXJ9Uo="></script>

- It exposes the global `Chart`.
- The path is absolute because pages are served at two depths.
- It is same origin, so do not add `crossorigin`.
- The integrity value is also the CSP hash-source that lets the browser run the library.
- Never retype, shorten or edit it. One wrong character silently blocks every chart. Copy it from this section or from `get_dashboard`.
- Put it once in `<head>`. No second script tag with a src.

## Chart.js 4 essentials

- Create a chart with `new Chart(canvas, {type, data: {labels, datasets}, options})`.
- Put each canvas alone inside a `<div style="position:relative;height:260px">` with `responsive: true` and `maintainAspectRatio: false`. Without the wrapper height, phones collapse or grow the chart.
- Always set `plugins: { colors: { enabled: false } }` and an explicit `borderColor` and `backgroundColor` on every dataset. The default Colors plugin cycles orange and yellow, which read as the gold owed colour.
- Use `type: 'category'` x axes with ISO `YYYY-MM-DD` labels built in SQL with `to_char(day, 'YYYY-MM-DD')`. No date adapter is vendored, so `type: 'time'` throws.
- Sparkline: `pointRadius: 0`, legend off, both axes `display: false`. Use `spanGaps: true` when days are missing.
- Horizontal bars: `indexAxis: 'y'`. Doughnut only for 5 parts or fewer.
- Set `animation: false` in options.
- Format numbers with `Intl.NumberFormat` in tick and tooltip callbacks, never hand-built strings.
- On refresh, mutate `chart.data.labels` and `chart.data.datasets[i].data`, then call `chart.update()`. Never create a second Chart on the same canvas.
- Set `Chart.defaults.font.family` to the system stack and `Chart.defaults.color` to the muted text colour. Set `Chart.defaults.borderColor` to the line colour.
- Keep scripts small and plain: no frameworks, no build step, no modules.

## Layout

- Phone first: a single column, then let the grid widen. Tile grid: `grid-template-columns: repeat(auto-fit, minmax(160px, 1fr))`.
- Order top to bottom: title, an "As of {refreshed_at}" line, KPI tiles, one primary trend chart, at most three breakdowns, one top-N table. At most 6 charts per page.
- A KPI tile shows the value, its unit, its period, the change versus the previous period, and optionally a sparkline.
- Every number states its unit and period ("Orders, last 30 days", "Revenue, month to date").
- Numerals use `font-variant-numeric: tabular-nums` and are right-aligned in tables.
- Show an explicit empty state ("no data yet") when rows is empty, and an explicit error state when `error` is set. A blank chart is a bug.
- Beyond 6 categories, bucket the tail into "Other".
- Titles say what the page answers, in business words, not table names.

## Colour rules

- Ground `#080C14`, surface `#0E1420`, line `#1E2D42`, text `#F0F4F8`, muted text `#A0AEC0`.
- Series palette is blues and greys only: `#2B7FD4`, `#5BA0E0`, `#164A82`, `#A0AEC0`, `#718096`, `#4A5568`.
- Comparisons: current period solid `#2B7FD4`, previous period dashed grey `#718096`.
- Red `#E5534B` only for a genuinely bad terminal value (for example a stock-out), never for a series.
- Gold `#D4952B` appears if and only if the viewer owes an action: a failed or stale data feed (`error` is set, or `refreshed_at` is too old for its `refresh_minutes`), or an item explicitly awaiting their decision. Gold is never a chart series, never decoration, never a hover state, and never just "money owed to us": an overdue receivable is not gold unless this page is where the viewer must act on it.
- Rule of thumb: if nobody owes anything, there is no gold on the page.
- Green is not part of the series palette. Use it only for a small delta indicator when up is good.

## KPI history

`get_kpi_history(watch_id, days)` returns a watch's recorded trend: raw points, or hour, day or week buckets carrying the closing value plus min, max and n. An unknown id lists the existing watches. Use it to see how a KPI moved before you design a trend tile.

To chart a KPI watch, declare a history source instead of SQL, as an inline entry in the query list (no `sql` key):

    {"id": "trend", "source": "kpi_history", "watch_id": 12, "days": 30, "refresh_minutes": 15}

- Rows arrive as `day`, `value`, `min`, `max`, `n`, `metric`: daily buckets in the operator timezone, latest metric only, ascending.
- Use `day` as the category labels and `value` as the series. Fetch it like any query: `data('trend')`.
- `preview_dashboard` samples it. A watch that belongs to another person's private chat is refused.
- Dashboards never read the history store directly: the refresh loop snapshots it, so the page still only fetches `./data/{id}`.
- For a trend of a plain business table, keep using a daily `GROUP BY` query as in the example.

## Forecast panel

When the brief names a forecast definition (or get_forecast lists one), declare `{"id": "fc", "source": "forecast", "forecast_id": N, "refresh_minutes": 60}` instead of SQL. Rows: `period, actual, forecast, low, high, method, wape, as_of` (history rows have actual, future rows forecast, low and high). Style as below; caption with the method and its backtest WAPE ("typical miss"). The SQL panel below is the fallback when no definition exists.

A dashboard refreshes with SQL only, so a forecast panel is a simple forecast computed in one query: a straight-line trend (`regr_slope`, `regr_intercept`) over the last 26 complete weeks, and a band of plus or minus 1.28 times the standard deviation of the past misses (about 80 percent). It refreshes itself with the data. Rows: `week, actual, forecast, low, high`, with actual null in future weeks and the rest null in past weeks.

```sql
WITH hist AS (
  SELECT date_trunc('week', o.order_date)::date AS wk, sum(l.qty * l.unit_price) AS v
  FROM sales_orders o JOIN order_lines l ON l.order_id = o.id
  WHERE o.status <> 'cancelled' AND o.order_date >= (date_trunc('week', current_date) - INTERVAL '26 weeks')::date
    AND o.order_date < date_trunc('week', current_date)::date
  GROUP BY 1),
fit AS (SELECT regr_slope(v, (wk - DATE '2000-01-03') / 7) AS b, regr_intercept(v, (wk - DATE '2000-01-03') / 7) AS a FROM hist),
band AS (SELECT stddev_samp(h.v - (f.a + f.b * (h.wk - DATE '2000-01-03') / 7)) AS sd FROM hist h CROSS JOIN fit f),
fc AS (
  SELECT g::date AS wk, f.a + f.b * (g::date - DATE '2000-01-03') / 7 AS p, s.sd FROM fit f CROSS JOIN band s
  CROSS JOIN generate_series(date_trunc('week', current_date), date_trunc('week', current_date) + INTERVAL '7 weeks', INTERVAL '1 week') AS g)
SELECT to_char(wk, 'YYYY-MM-DD') AS week, round(v) AS actual, NULL::numeric AS forecast, NULL::numeric AS low, NULL::numeric AS high FROM hist
UNION ALL
SELECT to_char(wk, 'YYYY-MM-DD'), NULL, round(p), round(p - 1.28 * sd), round(p + 1.28 * sd) FROM fc
ORDER BY 1
```

- Other methods, same row shape: a moving average (`p` is `avg(v)` of the last 8 weeks, a flat line) when there is no clear trend; the same week last year scaled by growth (`p` is the week 52 weeks earlier times the last 8 weeks over the same 8 weeks a year before; needs 60 weeks of history) when the business is seasonal. If the brief names the method the forecasting backtest picked, use that one.
- Chart: actual solid `#2B7FD4`; forecast dashed `#5BA0E0` (`borderDash: [6, 4]`); the band as a `low` dataset plus a `high` dataset with `fill: '-1'`, `backgroundColor: 'rgba(91,160,224,0.18)'`, transparent borders, `pointRadius: 0`.
- Title or caption says "Simple forecast", the method and the band ("straight-line trend of the last 26 weeks; shaded band = typical miss"). Never call it a prediction or a target.

## Example

A complete minimal page and its query declaration. Adapt it; do not add features it does not need.

```html
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>South region orders</title>
<style>
:root{--ground:#080C14;--surface:#0E1420;--line:#1E2D42;--t1:#F0F4F8;--t2:#A0AEC0;--owed:#D4952B}
body{margin:0;padding:16px;background:var(--ground);color:var(--t1);font:15px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:12px 0}
.tile,.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px}
.big{font-size:28px;font-variant-numeric:tabular-nums}.muted{color:var(--t2);font-size:13px}
.chart{position:relative;height:260px}.owed{color:var(--owed)}
</style>
<script src="/d/_lib/chart.js-4.5.1/chart.umd.min.js" integrity="sha256-SERKgtTty1vsDxll+qzd4Y2cF9swY9BCq62i9wXJ9Uo="></script>
</head><body>
<h1>South region orders</h1><p id="asof" class="muted"></p>
<div class="tiles"><div class="tile"><div class="muted">Orders, last 30 days</div><div id="total" class="big">-</div></div></div>
<div class="card"><p id="empty" class="muted"></p><div class="chart"><canvas id="trend"></canvas></div></div>
<script>
Chart.defaults.color = '#A0AEC0';
Chart.defaults.borderColor = '#1E2D42';
Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
const fmt = new Intl.NumberFormat();
async function data(id) {
  const r = await fetch('./data/' + id + location.search, { cache: 'no-store' });
  if (!r.ok) throw new Error('data ' + id + ': HTTP ' + r.status);
  return r.json();
}
function owed(msg) { const p = document.getElementById('asof'); p.className = 'owed'; p.textContent = msg; }
const REFRESH_MINUTES = 60;
let chart = null;
function load() {
  return data('south_daily').then(d => {
    const asof = document.getElementById('asof');
    const stale = d.refreshed_at && (Date.now() - Date.parse(d.refreshed_at)) > 2 * REFRESH_MINUTES * 60000;
    if (d.error) owed('Refresh failed: ' + d.error);
    else if (stale) owed('Data is stale. As of ' + d.refreshed_at);
    else { asof.className = 'muted'; asof.textContent = 'As of ' + (d.refreshed_at || 'not refreshed yet'); }
    const empty = document.getElementById('empty');
    empty.textContent = d.rows.length ? '' : 'no data yet';
    document.getElementById('total').textContent = fmt.format(d.rows.reduce((s, r) => s + r.orders, 0));
    const labels = d.rows.map(r => r.day);
    const values = d.rows.map(r => r.orders);
    if (chart) {
      chart.data.labels = labels;
      chart.data.datasets[0].data = values;
      chart.update();
      return;
    }
    chart = new Chart(document.getElementById('trend'), {
      type: 'line',
      data: { labels: labels, datasets: [{ label: 'Orders per day', data: values,
        borderColor: '#2B7FD4', backgroundColor: 'rgba(43,127,212,0.15)', fill: true, tension: 0.25, pointRadius: 0 }] },
      options: { responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { colors: { enabled: false }, legend: { display: false } },
        scales: { x: { type: 'category', ticks: { maxTicksLimit: 6 } }, y: { beginAtZero: true } } }
    });
  }).catch(e => owed(e.message));
}
load();
setInterval(load, 60000);
</script></body></html>
```

```json
[{"id": "south_daily",
  "sql": "SELECT to_char(o.order_date, 'YYYY-MM-DD') AS day, count(*) AS orders FROM sales_orders o JOIN customers c ON c.id = o.customer_id JOIN regions r ON r.id = c.region_id WHERE r.name = 'South' AND o.order_date > current_date - 30 GROUP BY 1 ORDER BY 1",
  "refresh_minutes": 60}]
```

## Pre-save checklist

- The file starts with `<!doctype html>` and is under 256 KB.
- The library line is copied verbatim, appears once, and is the only script with a src.
- No absolute URLs, no `//host`, no inline `on...=` handlers, no storage APIs, no eval.
- Data only through `./data/{id}` with `location.search` forwarded; every id is declared in the query list.
- Every query is one aggregated SELECT, at most 500 rows, `refresh_minutes` between 5 and 1440, at most 20 queries.
- No global except as a dotted member access, no computed key except `rows[i]` or `datasets[0]`, and no `'a' + 'b'` joined literals (write whole strings). No computed writes, no `${}` templates, no `.concat(`.
- Every column is aliased uniquely, a KPI watch trend uses the `kpi_history` source, and no `innerHTML` anywhere.
- Each canvas sits in a `position:relative` wrapper with a fixed height, `colors: { enabled: false }` set, explicit dataset colours.
- Empty state, error state and an "As of" line are all present, and every number has a unit and a period.
- No gold unless the viewer owes an action, and all `preview_dashboard` findings are fixed.

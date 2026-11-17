# Dashboard file contract

The rules every agent-authored dashboard version follows, and what the serving origin guarantees in return.
Versioned with the code. Phases 3 (storage), 4 (serving), 5 (authoring skill) and 6 (validators) all build against it.
Since Phase 18 new versions are chart specs (sections 10 and 11); sections 1 to 9 and the HTML example describe legacy HTML versions, which render only while the `dashboard_legacy_html` setting is on.

## 1. One file
UTF-8 HTML5 starting `<!doctype html>`, at most 256 KB (262,144 bytes). All CSS in `<style>`, all JS in inline `<script>`.
Images only as `data:` URIs. System font stack, no web fonts, no `<link rel=stylesheet>`, no `@import`.

## 2. Chart library
The only allowed external script is this exact line, copied verbatim (never retype the hash):
`<script src="/d/_lib/chart.js-4.5.1/chart.umd.min.js" integrity="sha256-SERKgtTty1vsDxll+qzd4Y2cF9swY9BCq62i9wXJ9Uo="></script>`
- It exposes the global `Chart`. The path is absolute because pages live at two depths. Write the line exactly as above: the server adds `crossorigin="anonymous"` at serve time (the frame is an opaque origin, so SRI needs CORS mode).
- The `integrity` value lets the browser verify the bytes. It is also the CSP hash-source that allows the library (Chrome 59+, Firefox 116+, Safari 15.6+). One wrong character blocks the library and charts fail silently.
- Use `category` axes with ISO `YYYY-MM-DD` labels. The time scale needs a date adapter that is not vendored.
- A future library version gets a new path and hash. Existing versions keep theirs.

## 3. Data
- Data comes only from `fetch('./data/' + id + location.search, {cache: 'no-store'})`.
- The view token rides as `?t=<view_token>` on the page URL. The page forwards its query string unchanged and never stores it.
- No other fetch, XHR, WebSocket, EventSource or sendBeacon target is allowed. Re-fetch no faster than every 60 s.
- Response: `200 application/json` with `{"query_id", "columns": [...], "rows": [{col: value}], "refreshed_at": ISO-8601 or null, "data_as_of": ISO-8601 or null, "timezone": IANA zone or null, "error": str or null}`.
- `timezone` (Phase 22) is the zone the day buckets were computed in, the operator timezone when it is set, null when the database's own zone applied.
- `data_as_of` (Phase 17) is how old the data is: the oldest last successful sync among the imported tables the query reads, the refresh time for live tables, the newest KPI observation for kpi_history and null for forecasts. Queries that read only imported tables re-run after their source syncs; between syncs their `refresh_minutes` is a confirmation cadence, so `refreshed_at` means confirmed current and `data_as_of` carries the real age. A failed refresh keeps its normal retry backoff even after a sync.
- Before the first refresh rows is empty and refreshed_at is null. After a failed refresh the last good rows stay and error is set.
- Numerics are JSON numbers, dates are ISO-8601 strings, NULL is null.
- At most `sql_row_limit` rows (default 500), so aggregate in SQL.
- `error` is a short, sanitized category message (no SQL text, DSN or host). The full error is logged server-side only.
- Server side, the dashboards nginx `log_format` and the gateway access log mask the `t` query parameter (`t=REDACTED`), so the view token never reaches logs. Phase 4 asserts this in a test.
- Show `refreshed_at` on the page. Retired versions and drafts older than 24 h stop refreshing.

## 4. Query declaration
- Shape: `[{"id", "sql", "refresh_minutes"}]`, at most 20 queries per version.
- `id` matches `^[a-z][a-z0-9_]{0,47}$` (written `[a-z][a-z0-9_]{0,47}`) and is unique per version.
- `sql` is one SELECT accepted by `guardrails.validate_readonly_sql`. It runs read-only via `guardrails.run_readonly_sql` on the refresh loop, never on the request path.
- `refresh_minutes` is an integer from 5 to 1440 (default 15). SQL is frozen per version.
- `{"id", "source": "kpi_history", "watch_id", "days" (1-400, default 30), "refresh_minutes"}` (no `sql`) charts a KPI watch's recorded history, refreshed from the working-memory store by the refresh loop, never from the business DB.
- Its rows are `{day, value, min, max, n, metric}` daily buckets (operator timezone, latest metric only). `./data/{query_id}` stays the only data path.
- `{"id", "source": "forecast", "forecast_id", "refresh_minutes"}` (no `sql`) charts an owner-approved forecast definition, refreshed from forecast_points in the working-memory store by the refresh loop, never from the business DB.
- Its rows are `{period, actual, forecast, low, high, method, wape, as_of}` from the latest successful nightly run: history rows first (`actual` set, the rest null), then future rows (`forecast`, `low`, `high` set, `actual` null), at most 400 rows. `wape` is a percentage from 0 to 100 (11.6 means 11.6%), not a fraction.

## 5. URLs and errors
- `slug` matches `^[a-z0-9][a-z0-9-]{0,62}$`. It cannot start with `_`, so `/d/_lib/` stays reserved for the library.
- A draft is never served under the dashboard view token. Preview uses a short-lived, version-scoped preview token minted by an ops-key route, and only after the Phase 6 validators pass. View tokens are per dashboard and rotatable (DASH-04), so a leak is bounded.
- Current published version: `/d/{slug}/?t=...`. Pinned approved versions: `/d/{slug}/v/{n}/?t=...`. Drafts: `/d/{slug}/v/{n}/?p=<preview_token>` only.
- Those page URLs are a constant wrapper. The agent HTML is served at `.../frame` (same query string) inside the wrapper's sandboxed frame. `./data/{id}` resolves next to `/frame`. A top-level open of `/frame` is a 404.
- Data resolves relative to the page: `/d/{slug}/data/{qid}` and `/d/{slug}/v/{n}/data/{qid}`. A pinned page reads its own version's snapshots.
- A spec version also serves `./spec` (`/d/{slug}/spec`, `/d/{slug}/v/{n}/spec`): JSON, same token gate and same 404 rules as `./data/{id}`.
- `/d/{slug}` redirects to `/d/{slug}/`, keeping the query string, before any lookup.
- A wrong or missing token, unknown slug or version, or undeclared qid all return the same 404.

## 6. Security rules (CSP)
- Chart-spec versions (Phase 18) run no agent JavaScript, style or markup: the frame is constant app-owned bytes under the section 11 CSP with Trusted Types, so T-12-51 is closed for them (proved by backend/scripts/spec_hostile_check.mjs: no off-origin DNS lookup or connection and no script other than the renderer, library and prelude).
- Inline scripts are hashed per version at save time, so any byte change is a new version. The stored script hashes are the sha256 of each inline script plus the library's integrity hash (`sha256-SERKgtTty1vsDxll+qzd4Y2cF9swY9BCq62i9wXJ9Uo=`).
- The browser, not the linter, is the control. The wrapper renders the page in an opaque-origin `sandbox="allow-scripts"` frame: never allow-same-origin, allow-top-navigation, allow-popups, allow-modals, allow-downloads or allow-forms. The wrapper CSP is `frame-src 'self'`, `child-src 'self'`, `frame-ancestors 'none'`. WebRTC is disabled by a server-inserted prelude (CSP cannot).
- Forbidden (navigation and exfiltration): any write to `location` or `location.href`, `location.assign`/`replace`/`reload`, `window.open`, `top`/`parent`/`opener`, `postMessage`, `document.write`, any `<a href>` other than a `#fragment`, `<meta http-equiv>`, and URL strings built at runtime (`atob`, `String.fromCharCode`, `unescape`, or concatenation that produces a scheme). `location` may only be read as `location.search`.
- The one sanctioned `postMessage` (Phase 22): a spec v2 frame posts the two section 13 messages (`state`, `ask`) to its own wrapper, and nothing else does. HTML versions and v1 specs still may not call it.
- Allowlist rules the validators enforce on the shape of the code: the globals `window`, `self`, `globalThis`, `document` and `frames` are legal only as a dotted member access (`document.getElementById(...)`), never assigned, passed, parenthesised or indexed; a computed member access may only use an integer literal or a plain dotted name (`rows[i]`, `datasets[0].data`); and two string literals may not be joined with `+`. An alias plus a computed key reconstitutes any banned name, and a spliced literal reconstitutes any URL, so the control is on the shape of the code, not on a list of names.
- As defence in depth the linter also refuses dotted self-references, DOM back-references (`ownerDocument`, `defaultView`, `.view`, `parentNode`, `getRootNode`, `composedPath`), `dispatchEvent`, computed member writes and runtime-built strings (template literals with `${`, `.concat(`).
- What the linter guarantees (AGENT-07): it parses the file with a spec-conformant HTML5 parser (html5lib, which handles `<svg>`/`<math>` foreign content and integration points as browsers do), checks that tree, and `/frame` serves the canonical serialisation of the same tree, so the browser parses exactly the bytes that were checked; markup the parser did not build as an element is never served as one. A file whose canonical form changes when re-parsed is refused (`parser_differential`) and never served; an inline script that the save-time hasher reads differently is refused too. Text rules that depend on no parser run on the stored file and again on the served bytes: no `http-equiv`, no `<link`, no resource-hint rel value, no URL scheme except the W3C `xmlns` values. Inside `<svg>` only chart shapes are allowed (no `title`, `desc`, `style`, `script`, `foreignObject` or HTML; no `<math>`), there is no markup inside `<style>`, and a value (attribute, `url(`, CSS or JS string) starting with `//` or a backslash form (`/\`, `\/`, `\\`) is refused as protocol-relative.
- Residual T-12-51 for legacy HTML versions (closed for chart specs; accepted only while `dashboard_legacy_html` is on, originally accepted by the owner for internal use on 2026-10-25): a static resource hint (`<link rel=preconnect>`, dns-prefetch, prefetch, prerender, modulepreload) is governed by neither CSP nor the sandbox, which is why the rules above refuse them. A navigation attempt from agent JavaScript still makes the browser resolve the target hostname (DNS, and Chrome opens a bare socket) before the wrapper's `frame-src` blocks it, and a hostname label can carry data. The linter's JavaScript rules are the only barrier against such an attempt.
- Forbidden: inline `on...=` handlers (use addEventListener), `javascript:` URLs, eval, new Function, string setTimeout, `<base>`, `<iframe>`, `<object>`, `<embed>`, forms, meta refresh, localStorage, sessionStorage, IndexedDB, cookies.
- Content header (the `/frame` response): `default-src 'none'; script-src <inline hashes> 'sha256-SERKgtTty1vsDxll+qzd4Y2cF9swY9BCq62i9wXJ9Uo=' <prelude hash>; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'; sandbox allow-scripts`. At serve time the request's own dashboards origin (http and https) is added to connect-src next to `'self'`.
- Plus `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff` (on every `/d/` response) and `Cross-Origin-Opener-Policy: same-origin`. Data routes and `/d/_lib/` send `Access-Control-Allow-Origin: *` (no credentials exist on this origin).
- script-src is the inline hashes plus the library hash and the prelude hash and nothing else. Never add `'self'`: it would let any same-origin script URL run and make the library pin optional.

## 7. No external URLs
- The file contains no http:, https:, ws:, wss: or ftp: URL and no protocol-relative `//host`.
- The only exception is W3C namespace URIs as `xmlns` values on inline SVG.
- The rule applies to the authored file, not to the vendored library.

## 8. Origins and ports
- Locally the dashboards container serves on 8091. On VMs it is `dash.{domain}`. Test stacks use 90N2.
- It serves `/d/_lib/` itself and proxies only `/d/` to `app:8080`. Port 8080 stays loopback-only.
- Dashboard HTML never renders on the gateway origin (8080, where /dashboard keeps the ops key in localStorage) or the UI origin (8090 or `app.`). The gateway refuses `/d/` HTML that did not arrive via the dashboards proxy.
- The ops UI never frames dashboards; the wrapper denies all framing (`frame-ancestors 'none'`).

## 9. Lifecycle
Agents only save drafts. A draft runs only under a preview token after validators pass (section 5). A human approval publishes. Every version stays live at its own URL, and rollback is a pointer flip.

## Example
```html
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Invoiced by customer</title>
<style>body{font:14px/1.4 system-ui,sans-serif;margin:16px} .owed{color:#b8860b}</style>
<script src="/d/_lib/chart.js-4.5.1/chart.umd.min.js" integrity="sha256-SERKgtTty1vsDxll+qzd4Y2cF9swY9BCq62i9wXJ9Uo="></script>
</head><body>
<h1>Invoiced by customer</h1><p id="asof"></p><canvas id="c"></canvas>
<script>
async function data(id) {
  const r = await fetch('./data/' + id + location.search, { cache: 'no-store' });
  if (!r.ok) throw new Error('data ' + id + ': HTTP ' + r.status);
  return r.json();
}
data('invoiced_by_customer').then(d => {
  document.getElementById('asof').textContent = 'As of ' + (d.refreshed_at || 'not refreshed yet');
  new Chart(document.getElementById('c'), { type: 'bar', data: {
    labels: d.rows.map(r => r.customer),
    datasets: [{ label: 'Invoiced', data: d.rows.map(r => r.invoiced) }] } });
}).catch(e => { const p = document.getElementById('asof'); p.className = 'owed'; p.textContent = e.message; });
</script></body></html>
```
```json
[{"id": "invoiced_by_customer",
  "sql": "SELECT c.name AS customer, sum(i.amount) AS invoiced FROM invoices i JOIN customers c ON c.id = i.customer_id GROUP BY c.name ORDER BY invoiced DESC LIMIT 20",
  "refresh_minutes": 15}]
```

## 10. Chart spec (Phase 18)
A version is a declarative chart spec; fixed, app-owned renderer code draws it, so no agent-written script, style or markup runs anywhere. The JSON Schema is `docs/dashboard-spec.v1.schema.json`, generated from `backend/app/dashboardspec.py` and pinned by a test.
- `spec_version` 1, `title`, optional `subtitle`, `queries`, `layout`, `tiles`. Unknown keys are refused at every depth.
- `queries` use the section 4 shapes (SQL, `kpi_history`, `forecast`). Ids never start with `m_`, which is reserved for compiled measures.
- `layout.rows`: 1 to 12 rows of 1 to 4 tile ids; every tile appears exactly once.
- Tile types: `stat` (with comparison, `good_direction`, `owed_when`, optional sparkline), `line`, `area`, `bar`, `stacked_bar`, `sparkline`, `table`, `text`.
- Each tile except `text` is bound to exactly one of `query` (a declared query id) or `measure`, never both. A measure binding is `name`, `by` (0 to 2 dimensions), `grain`, `filters`, `window` and `top`.
- `window` is `{last_days}`, `{last_periods}` (needs a grain) or `{start, end}` (end exclusive). `last_days: N` covers exactly N calendar days ending today (today and the N-1 days before it; the SQL is `>= CURRENT_DATE - (N-1) AND < CURRENT_DATE + 1`, with `CURRENT_DATE` the database session date). Until Quick 045 it covered N+1 days. Range tokens such as `this_month` are refused because they would freeze on the save date.
- Measure tiles compile at save time into SQL that is frozen with the version, stored as `m_` query entries with `source: measure`, ordered (and limited for `top`).
- Formats: `number`, `integer`, `decimal1`, `decimal2`, `percent`, `percent_value`, `currency` (needs a `currency` code), `compact`.
- Every string is plain text and renders only as text. Control and bidirectional characters are refused; markup is not interpreted.
- There are no colour fields. The renderer owns the palette; gold appears only for a failed or stale feed or a stat whose `owed_when` holds.
- Limits: 24 tiles, 6 chart tiles, 20 queries including compiled ones, 64 KB of JSON.

## 11. Renderer and serving (Phase 18)
- A spec version's `/frame` is one constant, app-owned page per renderer (`ic-render-1.0.0`): the WebRTC prelude, one hashed stylesheet, the Chart.js tag and the renderer tag, both with `integrity` and `crossorigin`. No agent string is ever in it, so every spec of a renderer gets identical bytes.
- Its CSP is exactly: `default-src 'none'`; `script-src` with only the prelude, Chart.js and renderer hashes; `style-src` with only the renderer stylesheet hash; `connect-src 'self'` plus the plain request host; `base-uri 'none'`; `form-action 'none'`; `require-trusted-types-for 'script'`; `trusted-types 'none'`; `frame-ancestors 'self'`; `sandbox allow-scripts`. The CSP comes from the pinned renderer, never from stored hashes.
- The renderer is vendored and pinned next to Chart.js in `dashboards/vendor.json` and `backend/app/dashboardrender.py` (`ic-render-1.0.0`, served under `/d/_lib/`). The Dashboards origin probe fetches it and fails with a rebuild hint when it is missing or its bytes differ from the pin.
- `./spec` returns the render form as JSON: tiles, layout, query ids, column names and refresh minutes. It carries no SQL, measure binding or filter values. It has CORS for the opaque frame and the data CSP.
- The renderer shows `data_as_of` from `./data/{qid}` (wired by Phase 17, which merged second); no renderer change was needed.
- An HTML version is a legacy format. While `dashboard_legacy_html` is off (default) its `/frame` is a constant needs-conversion page with no script; when on it is served as in v1.0. Legacy frames are always `no-store`, so turning the setting off takes effect on the next load.

## Spec example
```json
{
  "spec_version": 1, "title": "Sales overview", "subtitle": "Northwind Supply",
  "queries": [
    {"id": "open_invoices", "sql": "SELECT c.name AS customer, sum(i.amount) AS open_amount FROM invoices i JOIN customers c ON c.id = i.customer_id GROUP BY c.name ORDER BY 2 DESC", "refresh_minutes": 30}
  ],
  "layout": {"rows": [["net", "trend"], ["regions", "open"], ["about"]]},
  "tiles": [
    {"id": "net", "type": "stat", "title": "Net sales this month", "measure": {"name": "net_sales", "grain": "month", "window": {"last_periods": 3}}, "pick": "last_complete", "compare": "previous_row", "compare_label": "vs last month", "good_direction": "up", "sparkline": true, "format": "compact"},
    {"id": "trend", "type": "line", "title": "Net sales by month and region", "measure": {"name": "net_sales", "grain": "month", "by": ["regions.name"], "window": {"last_periods": 13}}, "format": "compact"},
    {"id": "regions", "type": "bar", "title": "Top regions, last 90 days", "measure": {"name": "net_sales", "by": ["regions.name"], "window": {"last_days": 90}, "top": 8}, "horizontal": true, "format": "compact"},
    {"id": "open", "type": "table", "title": "Open invoices by customer", "query": "open_invoices", "columns": [{"column": "customer", "label": "Customer"}, {"column": "open_amount", "label": "Open amount", "format": "currency", "currency": "USD"}], "max_rows": 10},
    {"id": "about", "type": "text", "title": "About these numbers", "text": "Net sales exclude cancelled and returned orders.\nOpen invoices refresh every 30 minutes."}
  ]
}
```

## 12. Chart spec v2 (Phase 22)
Spec v2 is authored beside v1; v1 specs keep working and render on `ic-render-1.0.0`. The JSON Schema is `docs/dashboard-spec.v2.schema.json`, generated from `backend/app/dashboardspec.py` and pinned by a test.
- `spec_version` 2, `title`, optional `subtitle`, optional `number_locale` (`en-IN`, `en-US`, `en-GB`, `en-AE`, `en-SG`, `en-AU`, `en-CA`; filled from the operator timezone when absent), `date_control`, `filters`, `queries`, `sections`, `tiles`. Unknown keys are refused at every depth.
- `sections` replace `layout.rows`: up to 10 sections of up to 16 tile ids, each with an optional `title`. A tile has a `width`: `quarter`, `third`, `half`, `two_thirds` or `full`. The grid is 12 columns on a desktop, 6 on a tablet and 2 on a phone.
- `date_control` has a `default` preset, optional `presets`, `horizon_days` (default 400) and `fy_start_month` (1 to 12, default 4; `this_fy` starts there). Presets: `today`, `yesterday`, `last_7_days`, `last_30_days`, `last_90_days`, `this_month`, `last_month`, `this_quarter`, `this_fy`. They resolve at view time in the operator timezone, never at save time.
- `filters`: up to 3 page filters (dimension id and label). A tile follows a filter only when it opts in with `follow_filters`.
- Tile types: `stat`, `table`, `text` and the charts `line`, `area`, `bar`, `stacked_bar`, `sparkline`, `combo`, `paired_bar`, `aging`, `bullet`, `waterfall`, `calendar`, `donut`, `ranked_bar`, `funnel`.
- A tile binds to one `query` or to `measure` (one governed measure) or `measures` (several, on `table`, `line`, `area`, `bar`, `stacked_bar`, `combo` and `paired_bar` tiles), never more than one form. `date: "control"` puts the tile under the date control.
- Tones are roles, never colours: `inward`, `outward`, `neutral`, `accent`. The renderer owns the palette and gold keeps its meaning from section 10.
- Controlled tiles store a daily horizon with parts (a sum and a count for an average, so any range re-aggregates in the browser) and read stored rows only; the browser never queries the database. Each column of a multi-measure tile counts by its own date, and count-distinct measures get stored presets instead of parts.
- Limits: 40 tiles, 12 charts, 30 queries including compiled ones, 128 KB of JSON (`MAX_SPEC_BYTES_V2` = 131072).
- Example (one stat under control, one paired bar, one multi-measure table):
```json
{
  "spec_version": 2, "title": "Weighbridge overview", "number_locale": "en-IN",
  "date_control": {"default": "last_7_days", "presets": ["today", "last_7_days", "this_month"]},
  "queries": [
    {"id": "in_out", "sql": "SELECT material AS material, sum(tons_in) AS inward, sum(tons_out) AS outward FROM trips GROUP BY material ORDER BY 2 DESC LIMIT 10", "refresh_minutes": 30}
  ],
  "sections": [
    {"title": "Today", "tiles": ["tons"]},
    {"title": "Materials", "tiles": ["flow", "detail"]}
  ],
  "tiles": [
    {"id": "tons", "type": "stat", "title": "Tons weighed", "date": "control", "measure": {"name": "tons"}, "comparison": "previous_period", "good_direction": "up", "format": "compact"},
    {"id": "flow", "type": "paired_bar", "title": "Inward and outward by material", "query": "in_out", "x": "material", "y": [{"column": "inward", "tone": "inward"}, {"column": "outward", "tone": "outward"}]},
    {"id": "detail", "type": "table", "title": "Detail", "date": "control", "measures": {"names": ["tons", "trips"]}}
  ]
}
```

## 13. Renderer 2.0.0 and the wrapper channel (Phase 22)
- Spec v2 versions are pinned to `ic-render-2.0.0` and `ic-chartlib-1.0.0` (Chart.js 4.5.1, chartjs-plugin-annotation 3.1.0 and chartjs-chart-matrix 3.1.0, concatenated verbatim), both in `dashboards/vendor.json` and `backend/app/dashboardrender.py`. Renderer SRI: `sha384-NJqLOp+bZ2kX/sU7ZxCl09DOBh82IIAyRhYtMvAw1JUfVvsp/VheSLlR7kHiSQt5`; renderer CSP hash: `sha256-6KBMppvU9IcLm4c7RbW9qochU+xRg2+fXeqGqKVSNDg=`. Bundle SRI: `sha384-7q0capCcXwQ/7HFlYmsT+it43lwnQZmCY1cj/hs4Gh0NaXsfiWckUxcQBtWg52GG`; bundle CSP hash: `sha256-515ub9x4JPVTtBbkxrV9rLXo4+Qvz0LRoAy16ivCYy0=`. `renderer_for()` picks the pin from `spec_version`.
- The v2 `/frame` has the section 11 CSP shape: `script-src` holds only the prelude, the bundle and the renderer hashes. Legacy HTML frames never admit the bundle hash.
- Frame contract: the renderer reads `location.search` and `location.hash` only, fetches only `./spec` and `./data/<qid>`, builds DOM with text only, and creates only an allowlisted set of tags. It never listens for messages. It calls `parent.postMessage` at exactly one call site.
- Wrapper channel: only spec v2 versions get the spec wrapper; v1 specs and legacy HTML keep the original wrapper byte for byte and no channel. The wrapper accepts a message only from its own frame and only these two shapes, exact keys, bounded values:
  - `{ic: 1, type: "state", hash: <up to 600 characters of [A-Za-z0-9_=,.:;%~-]>}` updates the wrapper URL hash (`history.replaceState`), so the date range and filters survive a reload.
  - `{ic: 1, type: "ask", tile: <tile id>, title: <1 to 120 code points of plain text>}` shows an "Ask about" link to `app_public_url` + `/?ask=...`. The link is absent when `app_public_url` is empty. The app opens the Ask panel prefilled and never sends it.
- `./data/{qid}` carries `timezone`, the zone the day buckets were computed in (section 3). Reads run `SET LOCAL TimeZone` inside the read-only transaction when `dashboard_local_days` is on and the operator zone is not UTC (UTC sets nothing); this covers refresh, preview samples and the save dry run.
- v2 snapshots have their own caps: `dashboard_snapshot_rows` (default 10000) and `dashboard_snapshot_bytes` (default 2 MB). Above either, the refresh fails with "too much data for one tile (over N rows or M MB)" and the last good rows stay.
- Owner layout tweak: `POST /api/dashboards/{slug}/layout` saves an operator draft of a published v2 version that changes only sections, widths, hidden tiles, titles and the default range, with the base queries verbatim. It writes one executed `dashboard_layout` actions row; the draft is published separately through `POST /api/dashboards/{slug}/publish`.

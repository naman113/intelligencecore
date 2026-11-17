---
name: dashboard-authoring
description: House style for live business dashboards as chart specs: governed measures first, phone-first layout, gold means owed. Use before writing, revising or converting any dashboard.
license: Proprietary
---

# Dashboard authoring (house style)

This is procedure for the dashboard writer. A dashboard is a chart spec: one JSON object that fixed, app-owned renderer code draws. You never write HTML, CSS or JavaScript, and the tools refuse them. Every write is a draft that waits for owner approval. Never say a dashboard is published.

## When to use and workflow

Use this before writing, revising or converting any dashboard.

1. Clarify the ask: which business question, which period, which breakdowns. Ask one short question if it is ambiguous, otherwise decide and state the assumption.
2. When revising or converting, call `get_dashboard(slug)` first. A spec comes back as JSON: change only what was asked. An HTML dashboard comes back as format html: see "Converting".
3. Call `get_model` with no arguments first (a name search finds only names containing the word and can miss related measures, such as the in and out measures of a stock). Then check each measure you rely on with `query_measure`. Every number a measure covers is bound to that measure. A movement ask (stock, balance) binds the stock measure and its in and out measures, split by the same dimension.
4. When the ask matches a recipe (see "Recipes"), load it: `use_skill("dashboard-authoring", part="recipes/<name>.md")`. Adapt it to the client's own measure names; never invent one.
5. For figures no measure covers, find tables with `search_business_context` (also "dashboard idea": approved rules named `rule:dashboard-idea-...` hold a question, a layout and ready queries). When it returns nothing, list columns with `run_sql` on `information_schema.columns` for `table_schema = 'public'` and copy identifiers exactly. Declared queries: one aggregated SELECT `{"id", "sql", "refresh_minutes"}` (5 to 1440), at most 500 rows, unique snake_case aliases, ids never start with `m_`, read-only.
6. Write the spec with `spec_version` 2 and call `preview_dashboard(spec)`. Fix every error and either fix or explain every warning, at most 3 preview rounds (the tool refuses more). Use its sample rows instead of extra `run_sql` calls.
7. Save with `save_dashboard_version(slug, change_note, preview_id)`: pass the `preview_id` of your last preview without errors; send `spec` instead only if you changed it after that preview. The change note is one line saying what changed and why.
8. Tell the user the draft awaits their approval. Never say it is live, and paste no link.

Tools: `get_dashboard`, `preview_dashboard`, `save_dashboard_version`, `get_model`, `query_measure`. Pass the spec as a JSON object.

## Say what is not possible

If the owner asks for any of these, say so plainly in your first sentence ("That is not possible yet"), offer the closest alternative, then build the alternative. Never build a workaround silently or promise it later.

- Colours, a colour per section, CSS or code of any kind: the renderer owns the palette. Offer tones (see "Tones and colour").
- Two value axes, gauges or dials, 3D: offer two tiles, a `bullet` or a stat with a target.
- A pie or donut above 5 parts: offer a sorted `ranked_bar` with Other.
- A drag-and-drop builder, live queries, or saved per-viewer views on the server: dashboards show stored snapshots; the viewer's date and filter choices stay in their browser.
- More than 40 tiles or 12 chart tiles: offer a second dashboard or tables.
- A filter over more than 50 values: offer a smaller dimension (3 filters at most).
- A distinct count over a custom range or split by a dimension under the date control: offer a fixed-window tile or a preset.

## Spec v2 in brief

- Top level: `spec_version` 2, `title`, optional `subtitle`, `number_locale`, `date_control`, `filters`, `queries`, `sections`, `tiles`. Unknown keys are refused at every depth. Every string is plain text.
- `sections`: 1 to 10, each `{title, description, tiles}` listing 1 to 16 tile ids; every tile appears in exactly one section once. Tile and query ids match `[a-z][a-z0-9_]{0,47}`.
- Tile `width`: quarter, third, half, two_thirds, full. Four stats across is quarter each; the renderer stacks on a phone.
- Limits: 40 tiles, 12 chart tiles, 30 queries, 128 KB of JSON. Page `filters`: at most 3, 50 values each, and only with a `date_control`.
- Each tile except `text` has exactly one of `query`, `measure` or `measures` (2 to 6 names, for tables, `paired_bar`, `combo` and time charts).
- Types: `stat`, `line`, `area`, `bar`, `stacked_bar`, `sparkline`, `table`, `text`, `combo`, `paired_bar`, `aging`, `bullet`, `waterfall`, `calendar`, `donut`, `ranked_bar`, `funnel`.
- Formats: `number`, `integer`, `decimal1`, `decimal2`, `percent`, `percent_value`, `currency` (alone, with `currency` such as INR), `compact`; optional `unit`.
- Measure-bound tiles fill `x`, `y` and `columns` themselves. For any other field, load `part="reference/spec-v2.md"` only when a field is unclear.

## Pick the visual

One line per question; the full table is in `part="reference/chart-guide.md"`.

- Trend: `line` (few periods: `bar`). Biggest: `ranked_bar`, top N plus Other. Made of: sorted `bar`, a few totals `stacked_bar`, one total with at most 5 parts `donut`.
- Better or worse than last period: `stat` with comparison. On target: `bullet` or a `stat` with `target`. Flow: `waterfall`. Overdue ages: `aging`. Many columns to look up: `table` with sort, totals, due and `bar`.
- Never: pie, gauge, dial, dual axis, 3D, a truncated axis, a colour per bar, red and green as the only cue.
- Put 2 to 4 stats in the first row, one chart per row, a table last, a `text` note at the end.

## Date control

- Set `date_control` when the page is about a period. Default from the owner's words: "daily" means `last_7_days`, "monthly review" means `this_month`. Presets are from today, yesterday, last_7_days, last_30_days, last_90_days, this_month, last_month, this_quarter, this_fy.
- A tile follows it with `"date": "control"`: a measure or `measures` binding with no `window`, and `grain` absent or day. Stat tiles may add `comparison` (previous_period, last_year, both); line, area and bar may add `ghost: previous_period` or `last_year`.
- Tiles with `date` fixed (the default) keep their own `window` and never move.
- Distinct-count measures follow presets only: no `by`, no page filters, one per tile. A measure with no date (a balance, kind now) or an offset ratio cannot follow the control or filters.
- `horizon_days` 400 covers a last-year comparison up to 35 days. Use 732 for `this_fy` comparisons or a last-year ghost on weekly views. Tiles with a `by` dimension keep 90 days.
- A "by week, compared with last year" ask: `line` or `bar` tiles of the governed measures with `date: "control"` and `ghost: "last_year"`, default `last_90_days` (46 to 200 days draws weekly buckets, so no grain) and `horizon_days` 732. The ghost lines each week up with the same days last year: never build a raw-SQL week-number or year-on-year query for it.
- `follow_filters: true` makes a tile follow the page filters; `by` plus followed filter dimensions are at most 2.
- Click-to-filter: declare a page filter on the dimension; tapping a bar of that dimension sets the filter for tiles with `follow_filters`.
- Under the control, `top` is refused in the binding: use `ranked_bar` with `top` (1 to 50) and `other`.
- Every date control gives the viewer the presets plus a Custom range (from and to). Tapping a day (or week or month) on a `date: "control"` chart focuses the whole page on it, with a chip that clears it. So "let me pick dates" and "tap a day for detail" are met by a date control plus a control-bound chart. Never tell the owner these are not possible.

## Measures first

Governed measures are the numbers the owner can trust. Always start with `get_model`.

- A binding is `{"name", "by", "grain", "filters", "window", "top"}`. `by` lists up to 2 dimensions. `grain` is day, week, month, quarter or year.
- Fixed windows: `{"last_days": N}` (exactly N calendar days ending today, counted in the operator's time zone), `{"last_periods": N}` (needs a grain) or `{"start", "end"}` (end exclusive). Never use range tokens such as `this_month` in a window: they freeze on the save date.
- A stat has no `by`; use `pick: "last_complete"` with a grain so a half-finished month is not the latest value.
- For several figures in one table use `measures`. Each column counts by its own date (a receipt on its receipt date, a dispatch on its dispatch date); the renderer says so, and your title should too.
- Use raw SQL only when no measure, alone or combined, expresses the figure, and call it unverified in your reply. A daily raw-SQL table must cover the same days as `last_days` (`current_date - 6` to `current_date` for 7).
- When `query_measure` says NOT MODELED, either declare SQL and say the figure is unverified, or propose the measure (follow the data-modeling skill).

## Tones and colour

- There are no colour fields. Series and deltas carry a `tone`: inward, outward, neutral or accent. Inward is stock or money coming in, outward is going out.
- Red and green only mean good and bad. Set `good_direction` (up, down, none) on every comparison; use none when neither is clearly good.
- Gold means the viewer owes an action. Set `owed_when` only on a number they must act on (overdue invoices above zero); if nobody owes anything, no gold on the page.
- A colour ask ("make the outward bars red") gets the outward tone, not a colour, and a reply that says: there is no colour field, red is kept for bad news and problems, so the outward tone is used instead.

## Numbers, dates and titles

- Set `number_locale` from the client's locale (en-IN shows lakh and crore); leave it out when unknown, the platform fills it.
- Every fixed tile title states the unit and the period ("Orders, last 30 days"). Tiles under the control say "by day" or similar and the unit; the control shows the period.
- Titles say what the tile answers, in business words, never table names. Beyond 6 categories group the tail as Other (`other` with `top`).

## Recipes

Load with `use_skill("dashboard-authoring", part="recipes/<name>.md")`.

- `daily-operations`: rolls or lots in and out, GRNs, dispatches by day.
- `receivables`: open and overdue money, ageing, top debtors.
- `payables-gst`: bills to pay and GST due.
- `fleet-compliance`: fuel use and spend by day, certificates due.
- `b2b-order-book`: orders and value with a last-year ghost.

## KPI history

`get_kpi_history(watch_id, days)` returns a watch's recorded trend. To chart a KPI watch, declare an entry in `queries` with no `sql` key:

    {"id": "trend", "source": "kpi_history", "watch_id": 12, "days": 30, "refresh_minutes": 15}

Rows are `day`, `value`, `min`, `max`, `n`, `metric` (daily, ascending). Bind a `line` tile with `"query": "trend"`, `"x": "day"` and `"y": [{"column": "value"}]`. A watch from another person's private chat is refused. For a plain business table prefer a measure.

## Forecast panel

When the brief names a forecast definition (or get_forecast lists one), declare `{"id": "fc", "source": "forecast", "forecast_id": N, "refresh_minutes": 60}` instead of SQL. Rows: `period, actual, forecast, low, high, method, wape, as_of` (history rows have actual, future rows forecast, low and high). Style it as a `line` tile with `x: "period"`, y entries for `actual` and for `forecast` with `"dashed": true`, and `"band": {"low": "low", "high": "high"}`; caption with the method and its backtest WAPE ("typical miss").

When no forecast definition exists, load `use_skill("dashboard-authoring", part="reference/forecast-panel.md")` for the SQL forecast panel. Call it a simple forecast, never a prediction or a target.

## Converting

HTML dashboards (format html, `needs_conversion`) and spec v1 dashboards become v2: read the code and queries, keep the figures, prefer a measure for any number the model covers, keep a declared query for the rest, and save as the next version of the same slug. The owner approves it; never delete the old version and never say it is live.

## Example

A date-controlled page: stats, a trend with a previous-period ghost, a ranking, a SQL table and a note.

```json
{
  "spec_version": 2, "title": "Stores overview", "number_locale": "en-IN",
  "date_control": {"default": "last_30_days", "presets": ["last_7_days", "last_30_days", "this_month"]},
  "queries": [
    {"id": "by_customer", "sql": "SELECT c.name AS customer, sum(r.kg) AS kg FROM rolls r JOIN customers c ON c.id = r.customer_id GROUP BY c.name ORDER BY 2 DESC", "refresh_minutes": 60}
  ],
  "sections": [
    {"title": "At a glance", "tiles": ["kg", "rolls"]},
    {"title": "Trend and ranking", "tiles": ["trend", "rank", "list", "about"]}
  ],
  "tiles": [
    {"id": "kg", "type": "stat", "title": "Kg in", "width": "half", "date": "control", "measure": {"name": "kg_in"}, "comparison": "previous_period", "good_direction": "up", "format": "compact", "unit": "kg"},
    {"id": "rolls", "type": "stat", "title": "Rolls in", "width": "half", "date": "control", "measure": {"name": "rolls_in"}, "comparison": "previous_period", "good_direction": "up", "format": "integer"},
    {"id": "trend", "type": "line", "title": "Kg in by day", "date": "control", "measure": {"name": "kg_in"}, "ghost": "previous_period", "format": "compact", "unit": "kg"},
    {"id": "rank", "type": "ranked_bar", "title": "Kg in by customer", "date": "control", "measure": {"name": "kg_in", "by": ["customers.name"]}, "top": 8, "format": "compact", "unit": "kg"},
    {"id": "list", "type": "table", "title": "Kg by customer, all time (unverified)", "query": "by_customer", "columns": [{"column": "customer", "label": "Customer"}, {"column": "kg", "label": "Kg", "format": "compact", "bar": true, "total": true}], "totals": true},
    {"id": "about", "type": "text", "title": "About", "text": "Kg in counts rolls by weigh date."}
  ]
}
```

## Pre-save checklist

- One JSON object, `spec_version` 2; every tile in exactly one section once; at most 40 tiles, 12 chart tiles, 30 queries.
- Numbers a measure covers are bound to it; raw-SQL figures are called unverified in the reply.
- Date-controlled tiles have `date: "control"` and no `window`; fixed tiles have a `last_days`, `last_periods` or `start`/`end` window and a period in the title; no range tokens.
- `good_direction` on every comparison; `owed_when` only where the viewer must act; no colours.
- Multi-measure tables say each column counts by its own date.
- Titles state unit and period; the number locale matches the client.
- Anything not possible was said so first, with the closest alternative built.
- Plain text only: no markup, scripts, colours or links. All `preview_dashboard` errors are fixed and warnings fixed or explained.

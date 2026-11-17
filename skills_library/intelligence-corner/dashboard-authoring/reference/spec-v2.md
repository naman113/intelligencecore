Load this only when a field is unclear; SKILL.md covers the common cases.

# Spec v2 field reference

Every key is checked; unknown keys are refused at every depth. Every string is plain text. There is no colour, CSS, style or code field.

## Top level

- `spec_version`: 2.
- `title` (200 chars), `subtitle` (300, optional).
- `number_locale`: one of en-IN, en-US, en-GB, en-AE, en-SG, en-AU, en-CA. Optional; the platform fills the client's locale.
- `date_control`: `{default, presets, horizon_days, fy_start_month}`. `default` and `presets` are from today, yesterday, last_7_days, last_30_days, last_90_days, this_month, last_month, this_quarter, this_fy. `default` is last_7_days; `presets` is a list of 1 to 9 that must contain the default (null = all nine); `horizon_days` 31 to 732 (400); `fy_start_month` 1 to 12 (4).
- Viewer features of a date control: presets plus a Custom range (from/to); tapping a day, week or month bucket on a `date: "control"` chart focuses the whole page on it, with a chip that clears it.
- `filters`: 0 to 3 of `{id, dimension, label}`; need a date_control. Use a small dimension (under 50 values).
- `queries`: 0 to 30 declared SQL queries `{id, sql, refresh_minutes}`; ids never start with m_.
- `sections`: 1 to 10 of `{title, description, tiles}`; each section lists 1 to 16 tile ids; every tile appears in exactly one section once.
- `tiles`: 1 to 40.

## Tile fields

Common: `id`, `type`, `note`, `width` (quarter, third, half, two_thirds, full), `hidden`.
Bound tiles (all but `text`) also take `title` (120) and exactly one of `query`, `measure`, `measures`, plus `format`, `currency`, `unit`, `date` (`fixed` default, or `control`), `follow_filters`, and `target` on stat, bullet, line, bar, combo: `{value or column, label}`.

- `measure`: `{name, by (0..2), grain, filters, window, top, other, refresh_minutes}`.
- `measures`: `{names (2..6), by, grain, filters, window, top, refresh_minutes}`. Allowed on table, paired_bar, combo and the time charts.

## Rules under date: "control"

- Needs spec `date_control`, a `measure` or `measures` binding, no `window`, and `grain` absent or day.
- `follow_filters` needs `date: "control"`. `by` plus followed filter dimensions are at most 2.
- `top` is refused in the binding; use `ranked_bar.top` instead.
- A distinct-count measure may not have `by` or `follow_filters`; one per tile. A fixed-kind measure (offset ratios, flagged) and a now-kind measure (no date) cannot follow the control or page filters.
- `other` needs `top` and exactly one `by`, and a sum measure.

## Tile types

- `stat`: `column`, `pick`, `compare`, `comparison` (previous_period, last_year, both, none; control only), `good_direction`, `sparkline`, `owed_when {op, value}`.
- `line`, `area`: `x`, `y` (1 to 6 refs), `series`, `x_label`, `y_label`, `band {low, high}`, `ghost` (none, previous_period, last_year).
- `bar`: as line plus `horizontal`. `stacked_bar`: plus `percent`.
- `sparkline`: one `y`. `text`: `text` up to 2,000 chars, no binding.
- `table`: `columns` 1 to 12 of `{column, label, format, currency, unit, owed_when, delta, good_direction, bar, total, due {warn_days 0..90}}`; `max_rows` 1 to 100 (25); `sort {column, dir}`; `totals`. One `bar: true` column at most.
- `combo`: `y` 2 to 4 refs each with `as` bar or line; one axis.
- `paired_bar`: `y` exactly 2 (default tones inward, outward), `mirror`.
- `aging`, `donut`, `funnel`, `waterfall` (`ends`), `calendar`: `x`, one `y`.
- `bullet`: `column`, required `target`, `bands`.
- `ranked_bar`: `x`, one `y`, `top` 1 to 50, `other`, `cumulative`.
- Series ref: `{column, label, dashed, tone (inward, outward, neutral, accent), as}`.

Measure-bound tiles get `x`, `y` and `columns` filled in automatically; set them only to override.

## Complete example

```json
{
  "spec_version": 2,
  "title": "Overview",
  "number_locale": "en-IN",
  "date_control": {"default": "last_30_days", "presets": ["last_7_days", "last_30_days", "this_month"]},
  "filters": [{"id": "customer", "dimension": "customers.name", "label": "Customer"}],
  "queries": [
    {"id": "recent", "sql": "SELECT c.name AS customer, sum(r.kg) AS kg FROM rolls r JOIN customers c ON c.id = r.customer_id GROUP BY c.name ORDER BY 2 DESC", "refresh_minutes": 60}
  ],
  "sections": [
    {"title": "Headlines", "tiles": ["kg", "trend"]},
    {"title": "Detail", "tiles": ["rank", "list", "about"]}
  ],
  "tiles": [
    {"id": "kg", "type": "stat", "title": "Kg in", "date": "control", "measure": {"name": "kg_in"}, "comparison": "both", "good_direction": "up", "format": "compact", "unit": "kg", "follow_filters": true},
    {"id": "trend", "type": "line", "title": "Kg in by day", "date": "control", "measure": {"name": "kg_in"}, "ghost": "previous_period", "target": {"value": 500, "label": "Owner target"}, "format": "compact", "unit": "kg"},
    {"id": "rank", "type": "ranked_bar", "title": "Kg in by customer", "date": "control", "measure": {"name": "kg_in", "by": ["customers.name"]}, "top": 8, "format": "compact", "unit": "kg"},
    {"id": "list", "type": "table", "title": "Kg by customer, all time", "query": "recent", "columns": [{"column": "customer", "label": "Customer"}, {"column": "kg", "label": "Kg", "format": "compact", "bar": true}], "totals": true},
    {"id": "about", "type": "text", "title": "About", "text": "Kg in counts rolls by weigh date."}
  ]
}
```

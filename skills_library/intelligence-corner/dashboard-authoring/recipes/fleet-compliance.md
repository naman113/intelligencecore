# Recipe: fleet and compliance (a page to check every morning)

Questions it answers: how much fuel did we burn and spend, what does fuel cost per km, and which certificates (insurance, fitness, permit, pollution) are due.

## Measures it needs

Find with get_model: litres of fuel (`fuel_litres`), fuel spend (`fuel_spend`) and a per-km measure (`fuel_per_km`). Per km must be a ratio measure (litres over km) so the page re-aggregates its components for any range; never average the daily ratios. Use the client's own names; never invent one. Certificates usually have no measure: use one declared read-only query (aggregated, at most 500 rows) and call it unverified.

## Layout

- date_control default `last_7_days`.
- Section 1, "Fuel": `stat` tiles for litres, spend and per km, `date: "control"`, `comparison: "previous_period"`, `good_direction: "down"` (less fuel, less cost is good).
- Section 2, "Fuel by day": a `line` of fuel_litres and a `line` of fuel_spend under the control. Two separate tiles, because litres and rupees are two units and a chart has one axis.
- Section 3, "Certificates due": a query-bound `table` sorted by due date ascending, with `due: {"warn_days": 30}` on the due-date column. The renderer marks a certificate overdue or due within 30 days; gold appears only for overdue or inside the window, with the word. Use the owner's own window if they gave one.

## Date control

Default `last_7_days`; the certificates table is a fixed tile (`date: "fixed"`), because a due date does not move with the range.

## Complete example

```json
{
  "spec_version": 2,
  "title": "Fleet and compliance",
  "number_locale": "en-IN",
  "date_control": {"default": "last_7_days"},
  "queries": [
    {"id": "certs", "sql": "SELECT v.reg_no AS vehicle, c.kind AS certificate, to_char(c.due_date, 'YYYY-MM-DD') AS due_date FROM certificates c JOIN vehicles v ON v.id = c.vehicle_id ORDER BY c.due_date", "refresh_minutes": 60}
  ],
  "sections": [
    {"title": "Fuel", "tiles": ["litres", "spend", "per_km"]},
    {"title": "Fuel by day", "tiles": ["litres_day", "spend_day"]},
    {"title": "Certificates due", "tiles": ["certs_table"]}
  ],
  "tiles": [
    {"id": "litres", "type": "stat", "title": "Fuel litres", "date": "control", "measure": {"name": "fuel_litres"}, "comparison": "previous_period", "good_direction": "down", "format": "compact", "unit": "L"},
    {"id": "spend", "type": "stat", "title": "Fuel spend", "date": "control", "measure": {"name": "fuel_spend"}, "comparison": "previous_period", "good_direction": "down", "format": "currency", "currency": "INR"},
    {"id": "per_km", "type": "stat", "title": "Fuel per km", "date": "control", "measure": {"name": "fuel_per_km"}, "comparison": "previous_period", "good_direction": "down", "format": "decimal2", "unit": "L/km"},
    {"id": "litres_day", "type": "line", "title": "Fuel litres by day", "date": "control", "measure": {"name": "fuel_litres"}, "format": "compact", "unit": "L"},
    {"id": "spend_day", "type": "line", "title": "Fuel spend by day (INR)", "date": "control", "measure": {"name": "fuel_spend"}, "format": "compact"},
    {"id": "certs_table", "type": "table", "title": "Certificates by due date", "query": "certs", "columns": [{"column": "vehicle", "label": "Vehicle"}, {"column": "certificate", "label": "Certificate"}, {"column": "due_date", "label": "Due", "due": {"warn_days": 30}}], "sort": {"column": "due_date", "dir": "asc"}, "max_rows": 25}
  ]
}
```

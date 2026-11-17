# Recipe: daily operations (stores, mills, warehouses)

Questions it answers: what came in and went out today and this week, how many rolls or lots, how many receipts (GRNs) and dispatches, and which day was odd.

## Measures it needs

Call get_model and find the client's own measures for: rolls (or lots) in, kg in, GRNs, rolls out, kg out, dispatches. Never invent a measure name. If one is missing, bind the others and say the gap out loud; raw SQL only for a figure no measure covers, and call it unverified (propose the measure with the data-modeling skill instead when it will be reused).

## Layout

- date_control default `last_7_days` (the owner said "daily"). Keep the default presets so the owner can switch to this month or last month.
- Section 1, "Today at a glance": four `stat` tiles (rolls in, kg in, rolls out, dispatches), all `date: "control"`, `comparison: "previous_period"`, `good_direction: "up"` for volumes. Four across on a desktop, two across on a phone.
- Section 2, "Kg in and out by day": one `paired_bar` over `measures: {"names": ["kg_in", "kg_out"]}`. Tones default to inward (kg in) and outward (kg out); never ask for colours. Same unit, one axis.
- Section 3, "Day by day": one `table` over `measures` (rolls in, kg in, GRNs, rolls out, dispatches), one row per day. Say in the title that each column counts by its own date (a GRN counts on its receipt date, a dispatch on its dispatch date), so columns can differ for the same day. The owner can tap a day for the detail rows.
- No gold on this page: nobody owes an action here, so no `owed_when`.
- Titles state the unit and the period is the control, so say "by day" and the unit (kg).

## Date control

Default `last_7_days`, `horizon_days` left at its default (400). If the owner wants a longer history by day, raise `horizon_days` (up to 732), not the number of tiles.

## Complete example

```json
{
  "spec_version": 2,
  "title": "Daily operations",
  "number_locale": "en-IN",
  "date_control": {"default": "last_7_days"},
  "sections": [
    {"title": "At a glance", "tiles": ["s_rolls_in", "s_kg_in", "s_rolls_out", "s_disp"]},
    {"title": "Kg in and out", "tiles": ["flow"]},
    {"title": "Day by day", "tiles": ["days"]}
  ],
  "tiles": [
    {"id": "s_rolls_in", "type": "stat", "title": "Rolls in", "date": "control", "measure": {"name": "rolls_in"}, "comparison": "previous_period", "good_direction": "up", "format": "integer"},
    {"id": "s_kg_in", "type": "stat", "title": "Kg in", "date": "control", "measure": {"name": "kg_in"}, "comparison": "previous_period", "good_direction": "up", "format": "compact", "unit": "kg"},
    {"id": "s_rolls_out", "type": "stat", "title": "Rolls out", "date": "control", "measure": {"name": "rolls_out"}, "comparison": "previous_period", "good_direction": "up", "format": "integer"},
    {"id": "s_disp", "type": "stat", "title": "Dispatches", "date": "control", "measure": {"name": "dispatches"}, "comparison": "previous_period", "good_direction": "up", "format": "integer"},
    {"id": "flow", "type": "paired_bar", "title": "Kg in and out by day", "date": "control", "measures": {"names": ["kg_in", "kg_out"]}, "format": "compact", "unit": "kg"},
    {"id": "days", "type": "table", "title": "Day by day (each column counts by its own date)", "date": "control", "measures": {"names": ["rolls_in", "kg_in", "grns", "rolls_out", "dispatches"]}, "max_rows": 10}
  ]
}
```

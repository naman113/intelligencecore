# Recipe: payables and GST

Questions it answers: how much GST is payable this month compared with last month, and how much do we owe suppliers right now.

## Measures it needs

Find with get_model: a GST payable measure (`gst_payable`, output tax less input credit by invoice date) and an open payables measure (`payables_open`, no date, kind now). Use the client's own names; never invent one. Raw SQL only for what no measure covers, called unverified.

## Layout

- Section 1, "GST": a `stat` of gst_payable under the date control with `comparison: "previous_period"`. With the default range `this_month` the previous period is last month. `good_direction` is `down` (paying less tax is good) or `none` when the owner does not see it as good or bad. Never use `up`.
- A second `stat` of gst_payable with `comparison: "last_year"` only when the owner asks for the year-on-year view and the horizon reaches back a year.
- Section 2, "Suppliers": a `stat` of payables_open (a now stat: no comparison, the balance as of the last refresh). Gold is not used here unless the owner names a due date they must act on; if they do, set `owed_when` on a separate overdue-payables measure the model already has, never on the total.
- No other chart is needed. If the owner also wants the trend, add one `line` of gst_payable under the control.

## Date control

Default `this_month`, presets `this_month`, `last_month`, `this_quarter`, `this_fy` (the quarter and financial year follow `fy_start_month`, 4 for April unless the client differs). The default presets must contain the default.

## Complete example

```json
{
  "spec_version": 2,
  "title": "Payables and GST",
  "number_locale": "en-IN",
  "date_control": {"default": "this_month", "presets": ["this_month", "last_month", "this_quarter", "this_fy"]},
  "sections": [
    {"title": "GST", "tiles": ["gst"]},
    {"title": "Suppliers", "tiles": ["payables"]}
  ],
  "tiles": [
    {"id": "gst", "type": "stat", "title": "GST payable", "date": "control", "measure": {"name": "gst_payable"}, "comparison": "previous_period", "good_direction": "down", "format": "currency", "currency": "INR"},
    {"id": "payables", "type": "stat", "title": "Open payables to suppliers", "date": "control", "measure": {"name": "payables_open"}, "format": "currency", "currency": "INR"}
  ]
}
```

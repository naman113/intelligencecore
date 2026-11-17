# Recipe: B2B order book (orders and value, compared with last year)

Questions it answers: how many orders and how much value over the last quarter, week by week, and how that compares with the same weeks last year.

## Measures it needs

Find with get_model: an order count (`orders`) and an order value (`order_value`), both with an order date. Use the client's own names; never invent one. Every tile binds these measures: the `last_year` ghost and the stat `comparison` are the year-on-year view, so never add a raw-SQL same-week-last-year or year-on-year % query.

## Layout

- date_control default `last_90_days`. For a range of 46 to 200 days the renderer draws weekly buckets on its own, so there is no grain to set.
- Section 1, "Order book": two `stat` tiles (orders, order value), `date: "control"`, `comparison: "previous_period"`, `good_direction: "up"`.
- Section 2, "Trend": one `line` of order_value and one of orders, both `date: "control"` with `ghost: "last_year"`, each drawn with a grey line labelled "Same days last year".

## Date control and horizon

Set `horizon_days: 732` so the stored data reaches back a year plus the longest range: a last-year comparison needs the horizon to cover the year-ago range as well as the selected one. With the default horizon of 400 days a 90-day range can still ghost last year, but a 365-day range cannot; the tile then says "Comparison not available for this range". Tell the owner this limit in a note when they ask for year-on-year over a long range.

## Complete example

```json
{
  "spec_version": 2,
  "title": "B2B order book",
  "number_locale": "en-IN",
  "date_control": {"default": "last_90_days", "horizon_days": 732},
  "sections": [
    {"title": "Order book", "tiles": ["s_orders", "s_value"]},
    {"title": "Trend", "tiles": ["value_line", "orders_line"]}
  ],
  "tiles": [
    {"id": "s_orders", "type": "stat", "title": "Orders", "date": "control", "measure": {"name": "orders"}, "comparison": "previous_period", "good_direction": "up", "format": "integer"},
    {"id": "s_value", "type": "stat", "title": "Order value", "date": "control", "measure": {"name": "order_value"}, "comparison": "last_year", "good_direction": "up", "format": "currency", "currency": "INR"},
    {"id": "value_line", "type": "line", "title": "Order value (INR), with last year", "date": "control", "measure": {"name": "order_value"}, "ghost": "last_year", "format": "compact"},
    {"id": "orders_line", "type": "line", "title": "Orders, with last year", "date": "control", "measure": {"name": "orders"}, "ghost": "last_year", "format": "integer"}
  ]
}
```

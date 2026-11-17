# Recipe: receivables (who owes me money and how old is it)

Questions it answers: how much is open, how much is overdue, how old is it, and which customers owe the most.

## Measures it needs

Find with get_model: an open-receivables measure (`receivables_open`), an overdue measure (`receivables_overdue`), an age-bucket dimension on invoices (`invoices.age_bucket`) and a customer name dimension (`customers.name`). Use the client's own names; never invent one. These measures have no date (kind now: the balance as of the latest refresh). Raw SQL only for what no measure covers, called unverified.

## Layout

- Section 1, "Owed to us": two `stat` tiles. Open receivables (a now stat, no comparison). Overdue receivables with `owed_when: {"op": ">", "value": 0}`: gold appears only here, only when something is overdue, and always with the word Overdue. Set `owed_when` on the overdue stat only, never on the open total and never on a table column. Build the page from these measures alone: the ranked bar already shows who owes the most, so add no raw-SQL customer table unless the owner asks, and call it unverified if you do.
- Section 2, "How old is it": one `aging` tile of the overdue measure `by: ["invoices.age_bucket"]`. Buckets are drawn in the order the query returns them, so the bucket dimension must sort oldest last.
- Section 3, "Who owes the most": one `ranked_bar` of receivables_open `by: ["customers.name"]` with `top: 8` on the tile. Put no `window`, `grain`, `top` or `other` in the measure binding: receivables measures have no date, so a window or grain is refused, and Other needs a sum measure. The browser draws the 8 largest and says "N more not shown".

## Date control

This example has no date_control and leaves the tiles on the default `date: "fixed"` with no window. On a page that does have a date control, these tiles may take `date: "control"` and then show "now" (the balance does not move with the range); say so in a note so the owner is not surprised.

## Complete example

```json
{
  "spec_version": 2,
  "title": "Receivables",
  "number_locale": "en-IN",
  "sections": [
    {"title": "Owed to us", "tiles": ["open", "overdue"]},
    {"title": "How old is it", "tiles": ["aging"]},
    {"title": "Who owes the most", "tiles": ["top_customers"]}
  ],
  "tiles": [
    {"id": "open", "type": "stat", "title": "Open receivables", "measure": {"name": "receivables_open"}, "format": "currency", "currency": "INR"},
    {"id": "overdue", "type": "stat", "title": "Overdue receivables", "measure": {"name": "receivables_overdue"}, "owed_when": {"op": ">", "value": 0}, "format": "currency", "currency": "INR"},
    {"id": "aging", "type": "aging", "title": "Overdue by age (INR)", "measure": {"name": "receivables_overdue", "by": ["invoices.age_bucket"]}, "format": "compact"},
    {"id": "top_customers", "type": "ranked_bar", "title": "Open receivables by customer, top 8 (INR)", "measure": {"name": "receivables_open", "by": ["customers.name"]}, "top": 8, "format": "compact"}
  ]
}
```

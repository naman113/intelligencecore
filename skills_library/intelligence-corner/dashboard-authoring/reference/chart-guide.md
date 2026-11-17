# Chart selection guide

Pick the tile from the question the owner is asking. The "Avoid" column is a rule, not a taste.

| Question type | Use | Avoid |
|---|---|---|
| How is it moving? (trend) | `line`; `bar` columns for few periods; time runs left to right | pie; stacked area when parts must be read; two value axes |
| Who or what is biggest? (ranking) | `ranked_bar` (sorted horizontal bar), top N plus Other; bars start at zero | a colour per bar; a truncated axis |
| What is it made of? (composition) | a sorted `bar` of shares; `stacked_bar` for a few totals; `donut` only for one total and at most 5 parts | several pies side by side; negative values in a donut |
| How is it spread? (distribution) | bars over pre-binned buckets (`aging`) | averages alone |
| Are two things related? (relationship) | not available yet; say so | dual-axis line plus column |
| Better or worse than plan or last period? (variance) | `stat` delta with arrow and sign; previous period as a grey `ghost` | red and green as the only cue |
| Are we on target? (progress) | `bullet`; `stat` with a `target` | gauge, dial, traffic light |
| Where did it come from and go? (flow) | `waterfall` | stacked bars |
| Where? (geography) | a sorted `bar` by region | a map of raw totals |
| One headline number | `stat` with comparison and sparkline, rounded | a bare number |
| Exact values, many columns | `table` with sort, totals, due and in-cell bars | a chart when the owner wants to look things up |

## Rules that apply everywhere

- Bars start at zero. One unit per chart. The same unit uses the same scale across the page.
- At most 4 coloured series; a fifth becomes Other (neutral) or the chart splits.
- Time runs left to right, categories top to bottom.
- Actual is solid, previous period is grey (`ghost`), a target is a dashed ink line.

## Never

- No dual axis (a `combo` takes one format and one unit). No pie above 5 parts. No gauge. No 3D. No colour for nominal bars.
- Red and green mean good and bad only. Set `good_direction` on every stat or delta column with a comparison; use `none` when neither direction is clearly good. Inward is not green and outward is not red.
- Gold means owed, only, and always with a word ("Owed", "Overdue"). Use `owed_when` and `due` only on a number the viewer must act on. If nobody owes anything, the page has no gold.
- The agent picks roles, never colours: a series `tone` is `inward`, `outward`, `neutral` or `accent`. Use inward and outward for flows, neutral for Other and transfers, accent for the one series the owner asked to watch. A tone on a single-series chart carries no meaning, so leave it off. If the owner asks for a colour ("make the outward bars red"), refuse it, explain that red means bad, and offer the tone.
- Orange is not a series colour.

## Tile notes

- `ranked_bar`: sorted largest first; `top` is applied in the browser under a date control and says "N more not shown"; `other: true` adds an Other row for sum measures only; `cumulative: true` adds a running share.
- `paired_bar`: two series side by side per bucket (default tones inward and outward); `mirror: true` draws the second below the axis.
- `combo`: bars plus lines on one axis, 2 to 4 series, at least one of each.
- `aging`: buckets in returned order, oldest last; add `owed_when` on the related stat, not on the buckets.
- `bullet`: actual against a required `target`, optional `bands` (1 to 3 ascending numbers).
- `waterfall`: opening, steps, closing (`ends: first_last`); `none` for steps only.
- `calendar`: one value per day over a day column.
- `donut`: one total, at most 5 parts; more fold into Other in the renderer and preview warns.
- `funnel`: stages in returned order.
- `sparkline`: a compact trend; put it in a row of stats, never alone.
- `ghost` (`line`, `area`, `bar`): `previous_period` or `last_year`, only under `date: "control"`.

## Caps

At most 12 chart tiles, 40 tiles, 30 queries, 10 sections and 131,072 bytes of JSON. Keep the first screen to four stats.

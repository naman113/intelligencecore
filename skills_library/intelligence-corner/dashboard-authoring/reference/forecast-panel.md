## Forecast panel

When the brief names a forecast definition (or get_forecast lists one), declare `{"id": "fc", "source": "forecast", "forecast_id": N, "refresh_minutes": 60}` instead of SQL. Rows: `period, actual, forecast, low, high, method, wape, as_of` (history rows have actual, future rows forecast, low and high). Style it as a `line` tile with `x: "period"`, y entries for `actual` and for `forecast` with `"dashed": true`, and `"band": {"low": "low", "high": "high"}`; caption with the method and its backtest WAPE ("typical miss"). The SQL panel below is the fallback when no definition exists.

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
- Chart: a `line` tile with `x: "week"`, y entries `actual` and `forecast` (`"dashed": true`), and `"band": {"low": "low", "high": "high"}`.
- Title or note says "Simple forecast", the method and the band ("straight-line trend of the last 26 weeks; shaded band = typical miss"). Never call it a prediction or a target.

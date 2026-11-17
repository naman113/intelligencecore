import { test } from 'node:test'
import assert from 'node:assert/strict'
import ReactDOMServer from 'react-dom/server'
import { Sparkline, TrendChart, seriesValues } from '../src/components/Sparkline'
import { kpiSeries } from './fixtures'

const r = (n: React.ReactElement) => ReactDOMServer.renderToStaticMarkup(n)

test('sparkline draws one polyline in the signal colour', () => {
  const html = r(<Sparkline values={[1, 3, 2]} />)
  assert.equal(html.split('<polyline').length - 1, 1)
  assert.match(html, /var\(--signal-hi\)/)
  assert.equal(html.match(/points="([^"]+)"/)![1]!.trim().split(' ').length, 3)
})

test('sparkline with fewer than 2 values has no polyline', () => {
  assert.ok(!r(<Sparkline values={[1]} />).includes('<polyline'))
  assert.ok(r(<Sparkline values={[]} />).includes('<svg'))
})

test('trend chart: polyline, labels, aria, no gold', () => {
  const html = r(<TrendChart points={kpiSeries.points} metric="overdue_total" />)
  assert.equal(html.split('<polyline').length - 1, 1)
  assert.match(html, /role="img"/)
  assert.match(html, /aria-label="30-day trend of overdue_total"/)
  assert.match(html, />100</)
  assert.match(html, />187</)
  assert.ok(!/owed|d4952b/i.test(html))
})

test('trend chart draws a band when min/max exist and skips nulls', () => {
  const pts = [
    { t: '2026-09-01T00:00:00Z', metric: 'm', value: 1, min: 0, max: 2 },
    { t: '2026-09-02T00:00:00Z', metric: 'm', value: null },
    { t: '2026-09-03T00:00:00Z', metric: 'm', value: 3, min: 2, max: 4 },
  ]
  const html = r(<TrendChart points={pts} metric="m" />)
  assert.match(html, /<polygon/)
  assert.equal(html.match(/<polyline points="([^"]+)"/)![1]!.split(' ').length, 2)
})

test('trend chart with <2 values', () => {
  assert.match(r(<TrendChart points={[]} metric="m" />), /Not enough history yet/)
})

test('seriesValues keeps the primary metric non-null values', () => {
  assert.equal(seriesValues(kpiSeries).length, 30)
  assert.deepEqual(
    seriesValues({
      ...kpiSeries,
      metric: 'm',
      points: [
        { t: 'a', metric: 'm', value: 1 },
        { t: 'b', metric: 'm', value: null },
        { t: 'c', metric: 'x', value: 5 },
        { t: 'd', metric: 'm', value: 2 },
      ],
    }),
    [1, 2],
  )
})

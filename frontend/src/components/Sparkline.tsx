import type { KpiPoint, KpiSeries } from '../lib/ictypes'
import { shortDate } from '../lib/format'

/* Dependency-free charts. Neutral and blue only: a trend is never "owed". */

export function seriesValues(series: KpiSeries): number[] {
  return series.points
    .filter((p) => p.metric === series.metric && p.value !== null && Number.isFinite(p.value))
    .map((p) => p.value as number)
}

function scale(values: number[], lo: number, hi: number, a: number, b: number): number[] {
  const span = hi - lo
  return values.map((v) => (span === 0 ? (a + b) / 2 : b - ((v - lo) / span) * (b - a)))
}

export function Sparkline({
  values,
  width = 120,
  height = 32,
}: {
  values: number[]
  width?: number
  height?: number
}) {
  const pad = 2
  const ok = values.length >= 2
  let pts = ''
  if (ok) {
    const lo = Math.min(...values)
    const hi = Math.max(...values)
    const ys = scale(values, lo, hi, pad, height - pad)
    const step = (width - 2 * pad) / (values.length - 1)
    pts = ys.map((y, i) => `${(pad + i * step).toFixed(1)},${y.toFixed(1)}`).join(' ')
  }
  return (
    <svg
      className="spark"
      viewBox={`0 0 ${width} ${height}`}
      width="100%"
      height={height}
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      {ok && (
        <polyline
          points={pts}
          fill="none"
          stroke="var(--signal-hi)"
          strokeWidth="1.5"
          strokeLinejoin="round"
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
        />
      )}
    </svg>
  )
}

export function TrendChart({
  points,
  metric,
  height = 180,
}: {
  points: KpiPoint[]
  metric: string | null
  height?: number
}) {
  const sel = points.filter(
    (p) => p.metric === metric && p.value !== null && Number.isFinite(p.value),
  )
  if (sel.length < 2) {
    return <p className="dimmer">Not enough history yet</p>
  }
  const W = 600
  const left = 56
  const right = 12
  const top = 12
  const bottom = 28
  const vals = sel.map((p) => p.value as number)
  const lows = sel.map((p) => (typeof p.min === 'number' ? p.min : (p.value as number)))
  const highs = sel.map((p) => (typeof p.max === 'number' ? p.max : (p.value as number)))
  const hasBand = sel.some((p) => typeof p.min === 'number' && typeof p.max === 'number')
  const lo = Math.min(...vals, ...lows)
  const hi = Math.max(...vals, ...highs)
  const step = (W - left - right) / (sel.length - 1)
  const X = (i: number) => left + i * step
  const ys = scale(vals, lo, hi, top, height - bottom)
  const line = ys.map((y, i) => `${X(i).toFixed(1)},${y.toFixed(1)}`).join(' ')
  let band = ''
  if (hasBand) {
    const hy = scale(highs, lo, hi, top, height - bottom)
    const ly = scale(lows, lo, hi, top, height - bottom)
    band =
      hy.map((y, i) => `${X(i).toFixed(1)},${y.toFixed(1)}`).join(' ') +
      ' ' +
      ly
        .map((y, i) => `${X(i).toFixed(1)},${y.toFixed(1)}`)
        .reverse()
        .join(' ')
  }
  const fmt = (n: number) => (Math.abs(n) >= 1000 ? n.toLocaleString() : String(Math.round(n * 100) / 100))
  return (
    <svg
      className="trend"
      viewBox={`0 0 ${W} ${height}`}
      width="100%"
      role="img"
      aria-label={`30-day trend of ${metric ?? 'value'}`}
    >
      {hasBand && <polygon points={band} fill="var(--signal)" fillOpacity="0.14" />}
      <polyline
        points={line}
        fill="none"
        stroke="var(--signal-hi)"
        strokeWidth="2"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      <text x={left - 6} y={top + 4} textAnchor="end" fontSize="11" fill="var(--t3)">
        {fmt(hi)}
      </text>
      <text x={left - 6} y={height - bottom} textAnchor="end" fontSize="11" fill="var(--t3)">
        {fmt(lo)}
      </text>
      <text x={left} y={height - 8} textAnchor="start" fontSize="11" fill="var(--t3)">
        {shortDate(sel[0]!.t)}
      </text>
      <text x={W - right} y={height - 8} textAnchor="end" fontSize="11" fill="var(--t3)">
        {shortDate(sel[sel.length - 1]!.t)}
      </text>
    </svg>
  )
}

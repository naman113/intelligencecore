import { useState } from 'react'
import { Link } from 'react-router-dom'
import { get } from '../lib/api'
import { ago, dateTime } from '../lib/format'
import { useResource } from '../lib/hooks'
import { runsHref } from '../lib/runs'
import type { InsightItem, InsightsResponse } from '../lib/types'
import { Async, Empty, PageHeader, Section, StatusPill, Tabs } from '../components/ui'

/* Insights: what the system noticed while studying approved data. Read-only.
 * All text is rendered as React text nodes; SQL sits in a <pre>. */

type Origin = 'exploration' | 'investigation' | 'all'

const ORIGIN_OPTIONS: { value: Origin; label: string }[] = [
  { value: 'exploration', label: 'From exploration' },
  { value: 'investigation', label: 'From investigations' },
  { value: 'all', label: 'All' },
]

const NONE = (
  <Empty title="No insights yet">
    Activate the explore_data playbook on Playbooks. Findings appear here the morning after it
    runs.
  </Empty>
)

function Evidence({ item }: { item: InsightItem }) {
  const ev = item.evidence
  const cols = ev?.columns ?? []
  const sample = ev?.sample ?? []
  return (
    <>
      {ev && (
        <div className="dimmer">
          {ev.row_count} rows · columns {cols.join(', ')}
          {ev.truncated ? ' · truncated' : ''}
          {ev.relative_dates ? ' · uses a relative date' : ''}
        </div>
      )}
      {item.evidence_sql && (
        <details>
          <summary>Reproducible SQL</summary>
          <pre className="mono">{item.evidence_sql}</pre>
          {sample.length > 0 && (
            <table className="table">
              <thead>
                <tr>
                  {cols.map((c) => (
                    <th key={c}>{c}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {sample.map((row, i) => (
                  <tr key={i}>
                    {cols.map((c) => (
                      <td key={c}>{String(row[c] ?? '')}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </details>
      )}
    </>
  )
}

function Finding({ item }: { item: InsightItem }) {
  return (
    <div className="stack" style={{ padding: '12px 16px' }}>
      <div className="row">
        <strong>{item.title}</strong>
        <StatusPill tone="neutral" label={item.severity} />
        <StatusPill tone="neutral" label={item.type === 'note' ? 'Finding' : 'KPI alert'} />
        <span className="dimmer" title={dateTime(item.created_at)}>
          {ago(item.created_at)}
        </span>
      </div>
      <p>{item.finding}</p>
      <Evidence item={item} />
      <div className="row dimmer">
        {item.case_id != null && <Link to={`/cases/${item.case_id}`}>Case #{item.case_id}</Link>}
        {item.watch_id != null && <Link to="/watches">Watch: {item.watch_title}</Link>}
      </div>
    </div>
  )
}

interface More {
  key: string
  items: InsightItem[]
  next: string | null
}

export default function Insights() {
  const [origin, setOrigin] = useState<Origin>('exploration')
  const [severity, setSeverity] = useState('')
  const [extra, setExtra] = useState<More | null>(null)
  const [busy, setBusy] = useState(false)

  const key = `${origin}|${severity}`
  const base = `/api/insights?origin=${origin}&limit=50${severity ? `&severity=${severity}` : ''}`
  const { data, error, loading, reload } = useResource(() => get<InsightsResponse>(base), {
    interval: 60_000,
    deps: [origin, severity],
  })

  const more = extra && extra.key === key ? extra : null
  const items = [...(data?.items ?? []), ...(more?.items ?? [])]
  const next = more ? more.next : (data?.next_before ?? null)

  const older = async () => {
    if (!next) return
    setBusy(true)
    try {
      const r = await get<InsightsResponse>(`${base}&before=${encodeURIComponent(next)}`)
      setExtra({ key, items: [...(more?.items ?? []), ...r.items], next: r.next_before })
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <PageHeader
        title="Insights"
        lede="What the system noticed while studying your approved data. Every finding carries the SQL that reproduces it. Turning one into a KPI happens through Approvals."
        actions={
          <Link className="btn btn-sm" to={runsHref('playbook:explore_data')}>
            Exploration runs
          </Link>
        }
      />
      <div className="stack">
        <div className="row">
          <Tabs value={origin} onChange={setOrigin} options={ORIGIN_OPTIONS} />
          <select
            value={severity}
            onChange={(e) => setSeverity(e.target.value)}
            aria-label="Severity"
          >
            <option value="">Any severity</option>
            <option value="info">info</option>
            <option value="warning">warning</option>
            <option value="critical">critical</option>
          </select>
        </div>
        <Async data={data} error={error} loading={loading} reload={reload} empty={NONE}>
          {() =>
            items.length === 0 ? (
              NONE
            ) : (
              <Section title="Findings" meta={`${items.length}`} flush>
                {items.map((it) => (
                  <Finding key={it.id} item={it} />
                ))}
                {next && (
                  <div style={{ padding: '12px 16px' }}>
                    <button className="btn btn-sm" onClick={older} disabled={busy}>
                      Load older
                    </button>
                  </div>
                )}
              </Section>
            )
          }
        </Async>
      </div>
    </>
  )
}

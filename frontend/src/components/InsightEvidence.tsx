import { useState } from 'react'
import { dateTime, isoTitle } from '../lib/format'
import type { InsightEvidenceView } from '../lib/contracts'
import './insight-evidence.css'

/* Quick 006: the collapsible evidence behind a finding, shared by the Insights page and
 * the Cases timeline. Closed by default (native <details>, keyboard accessible). Agent
 * and data text are React text nodes only; the SQL sits in a <pre> that scrolls
 * sideways. */

/* Severity as a marker: critical red, warning amber (not gold: gold means "you owe
 * this"), info neutral. The word is always printed, so colour is never the only signal. */
export const SEVERITY_LOOK: Record<string, { pill: string; label: string }> = {
  critical: { pill: 'pill pill-broken', label: 'Critical' },
  warning: { pill: 'pill pill-warn', label: 'Warning' },
  info: { pill: 'pill', label: 'Info' },
}

export function SeverityPill({ severity }: { severity: string | null }) {
  if (!severity) return null
  const look = SEVERITY_LOOK[severity] ?? { pill: 'pill', label: severity }
  return <span className={look.pill}>{look.label}</span>
}

export function SqlBlock({ sql }: { sql: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(sql)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      setCopied(false)
    }
  }
  return (
    <div className="ins-sql">
      <div className="ins-sql-head">
        <span className="field-label" style={{ marginBottom: 0 }}>
          Query
        </span>
        <button type="button" className="btn btn-quiet btn-sm" onClick={() => void copy()}>
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <pre className="mono">{sql}</pre>
    </div>
  )
}

const cell = (v: unknown) =>
  v === null || v === undefined ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v)

export function SampleTable({ evidence }: { evidence: InsightEvidenceView }) {
  if (!evidence.sample.length) return null
  const cols = evidence.columns.length ? evidence.columns : Object.keys(evidence.sample[0] ?? {})
  return (
    <div className="ins-table-wrap">
      <table className="table ins-table">
        <thead>
          <tr>
            {cols.map((c) => (
              <th key={c}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {evidence.sample.map((row, i) => (
            <tr key={i}>
              {cols.map((c) => (
                <td key={c}>{cell(row[c])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function EvidenceDetails({
  finding,
  sql,
  evidence,
  at,
}: {
  finding?: string | null
  sql?: string | null
  evidence?: InsightEvidenceView | null
  at?: string | null
}) {
  if (!finding && !sql && !evidence) return null
  const n = evidence?.rowCount
  return (
    <details className="ins-evidence">
      <summary>Evidence</summary>
      <div className="ins-evidence-body">
        {finding && <p className="ic-body">{finding}</p>}
        {sql && <SqlBlock sql={sql} />}
        {evidence && <SampleTable evidence={evidence} />}
        {(n != null || at) && (
          <p className="ic-meta">
            {n != null && `${n} ${n === 1 ? 'row' : 'rows'}${evidence?.truncated ? ' (truncated)' : ''}`}
            {n != null && at && ' · '}
            {at && (
              <span title={isoTitle(at)}>checked {dateTime(at)}</span>
            )}
          </p>
        )}
      </div>
    </details>
  )
}

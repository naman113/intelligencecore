import { asObj } from '../lib/api'
import { truncate } from '../lib/format'
import type { Action, ExtractedField } from '../lib/types'
import { hideTableText } from '../lib/contextpack'
import { readContextChange, readPublishDashboard, readSourceChange } from '../lib/contracts'
import { SourceChangePanel } from './payloads/SourceChange' // Phase 15 (SRC-04)
import { ContextChangeDetail } from './payloads/ContextChange'
import { PublishDashboardPanel } from './payloads/PublishDashboard'
import { ModelChangeDetail } from './payloads/ModelChange' // Phase 16
import { readModelChange } from '../lib/model' // Phase 16
import {
  LearningApprovalDetail,
  learningApprovalTitle,
  readLearningApproval,
} from './payloads/LearningApproval'

/* How a proposed action is shown to the person deciding on it.
 *
 * The old dashboard truncated the raw JSON at 180 characters, which for an
 * extracted invoice cut off inside the title before the first field — the
 * operator approved without seeing a single amount, date or confidence. The
 * whole point of a human gate is that the human sees what they are agreeing to,
 * so each kind gets a rendering that shows the parts that matter.
 */

const LOW_CONFIDENCE = 0.8

export function actionTitle(action: Action): string {
  const p = asObj<Record<string, unknown>>(action.payload)
  switch (action.kind) {
    case 'record_extracted_data': {
      const rec = asObj<Record<string, unknown>>(p.record)
      return String(p.title || rec.doc_type || 'Extracted record') || 'Extracted record'
    }
    case 'whatsapp_message':
      return 'WhatsApp message'
    case 'agent_task':
      return String(p.title || 'Follow-up task')
    case 'context_change': {
      const c = readContextChange(p)
      if (c.op === 'hide') return `Hide ${c.kind ?? 'item'} ${c.ref ?? ''}`.trim()
      if (c.op === 'unhide') return `Restore ${c.kind ?? 'item'} ${c.ref ?? ''}`.trim()
      return c.title ?? `Describe ${c.kind ?? 'item'} ${c.ref ?? ''}`.trim()
    }
    case 'hide_table': {
      const h = hideTableText(p)
      return `Hide table ${h.ref} from the agents`
    }
    case 'publish_dashboard': {
      const d = readPublishDashboard(p)
      const v = d.version != null ? ` v${d.version}` : ''
      if (d.op === 'rollback') return `Roll back "${d.title ?? d.slug ?? ''}" to${v}`
      return `Publish "${d.title ?? d.slug ?? ''}"${v}`
    }
    case 'model_change': {
      // Phase 16
      const m = readModelChange(p)
      return m.summary ?? `Model ${m.object ?? 'item'} ${m.name ?? ''}`.trim()
    }
    // Phase 15 (SRC-04)
    case 'source_change': {
      const c = readSourceChange(p)
      return c.summary ?? `Connect source ${c.name}`
    }
    case 'activate_learning': // Quick 020
      return learningApprovalTitle(readLearningApproval(p))
    default:
      return String(p._summary || action.kind.replace(/_/g, ' '))
  }
}

/** What the action would do, in one line. */
export function actionLine(action: Action): string {
  const p = asObj<Record<string, unknown>>(action.payload)
  switch (action.kind) {
    case 'whatsapp_message':
      return truncate(String(p.body ?? ''), 160)
    case 'agent_task':
      return truncate(String(p.instruction ?? ''), 160)
    case 'record_extracted_data': {
      const fields = fieldsOf(action)
      const low = fields.filter(isLow).length
      const base = `${fields.length} field${fields.length === 1 ? '' : 's'} to record`
      return low ? `${base} · ${low} to check` : base
    }
    case 'context_change': {
      const c = readContextChange(p)
      return truncate(c.text ?? c.ref ?? String(p._summary ?? ''), 160)
    }
    case 'hide_table':
      return truncate(hideTableText(p).reason, 160)
    case 'publish_dashboard': {
      const d = readPublishDashboard(p)
      return truncate(d.changeNote ?? d.note ?? d.summary ?? d.title ?? '', 160)
    }
    case 'model_change': {
      // Phase 16
      const m = readModelChange(p)
      return truncate(`${m.op ?? 'change'} ${m.object ?? 'item'} ${m.name ?? ''}`.trim(), 160)
    }
    // Phase 15 (SRC-04)
    case 'source_change': {
      const c = readSourceChange(p)
      if (c.kind === 'csv') return `CSV file ${c.csvFile ?? ''}`
      const modes = Array.from(new Set(c.tables.map((t) => t.mode))).join(', ')
      return `${c.tables.length} table${c.tables.length === 1 ? '' : 's'}${modes ? ` (${modes})` : ''}`
    }
    case 'activate_learning': {
      // Quick 020
      const v = readLearningApproval(p)
      return truncate(`${v.content} · from ${v.originLabel}`, 160)
    }
    default:
      return truncate(String(p._summary ?? JSON.stringify(p)), 160)
  }
}

function isLow(f: ExtractedField): boolean {
  return f.confidence != null && Number(f.confidence) < LOW_CONFIDENCE
}

export function fieldsOf(action: Action): ExtractedField[] {
  const p = asObj<Record<string, unknown>>(action.payload)
  const rec = asObj<Record<string, unknown>>(p.record)
  const raw = (rec.fields ?? rec.data ?? []) as unknown
  if (Array.isArray(raw)) return raw as ExtractedField[]
  if (raw && typeof raw === 'object') {
    return Object.entries(raw as Record<string, unknown>).map(([name, value]) => ({ name, value }))
  }
  return []
}

/** Full detail, for the approvals queue and the case timeline. */
export function ActionDetail({ action }: { action: Action }) {
  const p = asObj<Record<string, unknown>>(action.payload)

  if (action.kind === 'record_extracted_data') {
    const fields = fieldsOf(action)
    if (fields.length) return <ExtractedTable fields={fields} />
  }

  if (action.kind === 'whatsapp_message') {
    return (
      <div className="stack-2">
        <div className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>
          To {String(p.recipient ?? 'requester')} · <span className="mono">{String(p.phone ?? '')}</span>
        </div>
        <blockquote className="code">{String(p.body ?? '')}</blockquote>
      </div>
    )
  }

  if (action.kind === 'agent_task') {
    return (
      <div className="stack-2">
        <div className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>
          Runs on thread <span className="mono">{String(p.thread_id ?? '')}</span>
        </div>
        <blockquote className="code">{String(p.instruction ?? '')}</blockquote>
      </div>
    )
  }

  if (action.kind === 'context_change') {
    return <ContextChangeDetail view={readContextChange(p)} />
  }

  if (action.kind === 'hide_table') {
    const h = hideTableText(p)
    return (
      <div className="stack-2">
        <div>
          Hide table <span className="mono">{h.ref}</span> from the agents
        </div>
        {h.reason && <div className="dim">{h.reason}</div>}
      </div>
    )
  }

  if (action.kind === 'publish_dashboard') {
    return <PublishDashboardPanel action={action} />
  }

  if (action.kind === 'model_change') {
    // Phase 16
    return <ModelChangeDetail payload={p} />
  }
  // Phase 15 (SRC-04)
  if (action.kind === 'source_change') {
    return <SourceChangePanel view={readSourceChange(p)} />
  }
  if (action.kind === 'activate_learning') {
    return <LearningApprovalDetail view={readLearningApproval(p)} /> // Quick 020
  }

  const { _summary, ...rest } = p
  return (
    <div className="stack-2">
      {_summary != null && <div>{String(_summary)}</div>}
      <pre className="code">{JSON.stringify(rest, null, 2)}</pre>
    </div>
  )
}

/* Every field, its value, how sure the extractor was, and the sentence it came
 * from. Low-confidence rows are marked in gold because checking them is exactly
 * the work being asked of the person reading. */
export function ExtractedTable({ fields }: { fields: ExtractedField[] }) {
  const low = fields.filter(isLow).length
  return (
    <div className="stack-2">
      {low > 0 && (
        <div className="banner banner-owed">
          {low} value{low === 1 ? '' : 's'} the extractor was unsure about — check{' '}
          {low === 1 ? 'it' : 'them'} against the source before approving.
        </div>
      )}
      <div className="table-scroll">
        <table className="table">
          <thead>
            <tr>
              <th>Field</th>
              <th>Value</th>
              <th>Confidence</th>
              <th>From the document</th>
            </tr>
          </thead>
          <tbody>
            {fields.map((f, i) => {
              const name = f.name ?? f.field ?? ''
              const quote = f.source_quote ?? f.quote ?? ''
              const conf = f.confidence == null ? null : Number(f.confidence)
              const flag = isLow(f)
              return (
                <tr key={`${name}-${i}`} style={flag ? { background: 'var(--owed-wash)' } : undefined}>
                  <td className="mono dim nowrap">{name}</td>
                  <td style={{ fontWeight: 500 }}>{String(f.value ?? '')}</td>
                  <td className="mono nowrap" style={{ color: flag ? 'var(--owed-hi)' : 'var(--t3)' }}>
                    {conf == null ? '—' : `${Math.round(conf * 100)}%`}
                  </td>
                  <td className="dimmer" style={{ fontSize: 'var(--fs-tiny)', maxWidth: 340 }}>
                    {quote}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

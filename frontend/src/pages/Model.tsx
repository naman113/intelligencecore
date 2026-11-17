import { useState } from 'react'
import { Link } from 'react-router-dom'
import { get } from '../lib/api'
import { asks } from '../lib/ask'
import { useResource, useStored } from '../lib/hooks'
import {
  formatMatchRate,
  readModelMeasures,
  readModelPending,
  readModelRelationships,
  readModelTables,
  type ModelMeasureRow,
  type ModelPending,
  type ModelRelationshipRow,
  type ModelTableRow,
} from '../lib/model'
import { useAsk } from '../components/AskPanel'
import { Empty, ErrorNote, Loading, PageHeader, Section, StatusPill, Tabs } from '../components/ui'
import { Composer } from './Rules'
import './ic.css'

/* The semantic model, read-only. Tables, relationships and measures are
 * approved by the owner on Approvals; this page only shows them. The two
 * buttons message the agent: there is no write route. Every string here is
 * agent-written, so it renders as a text node. */

export type ModelTab = 'tables' | 'relationships' | 'measures'

const small = { fontSize: 'var(--fs-tiny)' }

const json = (v: unknown): string => {
  try {
    return JSON.stringify(v) ?? ''
  } catch {
    return ''
  }
}

function TablesTab({ tables }: { tables: ModelTableRow[] }) {
  if (!tables.length) return <Empty title="No tables modeled yet" />
  return (
    <div className="table-scroll">
      <table className="table">
        <thead>
          <tr>
            <th>Table</th>
            <th>Role</th>
            <th>Grain</th>
            <th>Primary key</th>
            <th>Time dimension</th>
            <th>Kind</th>
            <th>In warehouse</th>
          </tr>
        </thead>
        <tbody>
          {tables.map((t) => (
            <tr key={t.name}>
              <td>
                <div className="mono">{t.name}</div>
                <div className="dimmer" style={small}>
                  {t.relation}
                </div>
                {t.description && (
                  <div className="dim" style={small}>
                    {t.description}
                  </div>
                )}
              </td>
              <td>{t.role ?? '—'}</td>
              <td>{t.grain ?? '—'}</td>
              <td className="mono">{t.primaryKey.join(', ') || '—'}</td>
              <td className="mono">{t.aggTimeDimension ?? '—'}</td>
              <td>{t.objectKind}</td>
              <td>{t.inWarehouse == null ? '—' : t.inWarehouse ? 'yes' : 'no'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function RelationshipsTab({ rels }: { rels: ModelRelationshipRow[] }) {
  if (!rels.length) return <Empty title="No relationships modeled yet" />
  return (
    <div className="table-scroll">
      <table className="table">
        <thead>
          <tr>
            <th>From</th>
            <th>To</th>
            <th>Origin</th>
            <th>Match rate</th>
          </tr>
        </thead>
        <tbody>
          {rels.map((r) => (
            <tr key={r.name}>
              <td className="mono">{`${r.fromRelation.replace(/^public\./, '')}.${r.fromColumns.join(', ')}`}</td>
              <td className="mono">{`${r.toRelation.replace(/^public\./, '')}.${r.toColumns.join(', ')}`}</td>
              <td>{(r.origin ?? '').replace(/_/g, ' ') || '—'}</td>
              <td>{r.matchRate == null ? '—' : formatMatchRate(r.matchRate, null, null)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function MeasuresTab({ measures }: { measures: ModelMeasureRow[] }) {
  if (!measures.length) return <Empty title="No measures modeled yet" />
  return (
    <div className="stack-2">
      {measures.map((m) => {
        const def =
          m.type === 'simple'
            ? `${m.agg ?? ''}(${m.expr ?? ''}) on ${m.baseRelation ?? ''}`
            : m.type === 'ratio'
              ? `${json(m.numerator)} / ${json(m.denominator)}`
              : `${m.expr ?? ''} from ${json(m.inputMetrics)}`
        return (
          <div className="card stack-2" key={m.name}>
            <div className="row" style={{ gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <span className="mono">{m.name}</span>
              <StatusPill tone="neutral" label={m.type} />
              {m.unit && <span className="dimmer">{m.unit}</span>}
            </div>
            {(m.label || m.description) && <div className="dim">{m.label ?? m.description}</div>}
            <div className="mono" style={small}>
              {def}
            </div>
            {m.filters.length > 0 && (
              <div className="dimmer" style={small}>
                Filters: {json(m.filters)}
              </div>
            )}
            {m.compileError ? (
              <div className="dim">{m.compileError}</div>
            ) : (
              m.compiledSql && <pre className="code">{m.compiledSql}</pre>
            )}
          </div>
        )
      })}
    </div>
  )
}

export function ModelView({
  tables,
  relationships,
  measures,
  pending,
  tab,
  onTab,
  onModel,
  onAsk,
  sent,
}: {
  tables: ModelTableRow[]
  relationships: ModelRelationshipRow[]
  measures: ModelMeasureRow[]
  pending: ModelPending[]
  tab: ModelTab
  onTab: (t: ModelTab) => void
  onModel?: () => void
  onAsk?: (text: string) => void
  sent?: boolean
}) {
  const empty = !tables.length && !relationships.length && !measures.length
  const modelBtn = onModel && (
    <button className="btn btn-sm btn-primary" onClick={onModel}>
      Model my data
    </button>
  )
  return (
    <>
      {pending.length > 0 && (
        <p className="ic-meta">
          {pending.length} model change{pending.length === 1 ? '' : 's'} waiting on <Link to="/approvals">Approvals</Link>
        </p>
      )}
      {empty ? (
        <Empty title="Nothing is modeled yet" action={modelBtn}>
          Ask the agent to propose table roles, relationships and measures. You approve each one before it is used.
        </Empty>
      ) : (
        <>
          <Tabs<ModelTab>
            value={tab}
            onChange={onTab}
            options={[
              { value: 'tables', label: 'Tables', count: tables.length },
              { value: 'relationships', label: 'Relationships', count: relationships.length },
              { value: 'measures', label: 'Measures', count: measures.length },
            ]}
          />
          {tab === 'tables' && <TablesTab tables={tables} />}
          {tab === 'relationships' && <RelationshipsTab rels={relationships} />}
          {tab === 'measures' && <MeasuresTab measures={measures} />}
          {modelBtn && <div className="btn-row">{modelBtn}</div>}
        </>
      )}
      {onAsk && (
        <Section title="Ask for a change">
          <Composer
            label="Ask for a change"
            placeholder="For example: add a gross margin measure, or retire the old customer count"
            onSend={onAsk}
            sent={sent}
          />
        </Section>
      )}
    </>
  )
}

export default function Model() {
  const { send } = useAsk()
  const [tab, setTab] = useStored<ModelTab>('model.tab', 'tables')
  const [sent, setSent] = useState(false)
  const opts = { interval: 30_000 }
  const t = useResource(() => get<{ tables: unknown }>('/api/model/tables'), opts)
  const r = useResource(() => get<{ relationships: unknown }>('/api/model/relationships'), opts)
  const m = useResource(() => get<{ measures: unknown }>('/api/model/measures'), opts)
  const p = useResource(() => get<{ pending: unknown }>('/api/model/pending'), opts)
  const error = t.error || r.error || m.error || p.error
  const loading = (t.loading && !t.data) || (r.loading && !r.data) || (m.loading && !m.data)

  return (
    <div className="ic-page">
      <PageHeader
        title="Model"
        lede="The tables, relationships and measures the agent answers numbers from. Read-only here: changes arrive as proposals on Approvals."
      />
      {error && (
        <ErrorNote
          message={error}
          onRetry={() => void Promise.all([t.reload(), r.reload(), m.reload(), p.reload()])}
        />
      )}
      {loading ? (
        <Loading label="Reading the model" />
      ) : (
        <ModelView
          tables={readModelTables(t.data?.tables)}
          relationships={readModelRelationships(r.data?.relationships)}
          measures={readModelMeasures(m.data?.measures)}
          pending={readModelPending(p.data?.pending)}
          tab={tab}
          onTab={(x) => {
            setSent(false)
            setTab(x)
          }}
          onModel={() => void send(asks.modelData()).then((ok) => setSent(ok))}
          onAsk={(text) => void send(asks.modelChange(text)).then((ok) => setSent(ok))}
          sent={sent}
        />
      )}
    </div>
  )
}

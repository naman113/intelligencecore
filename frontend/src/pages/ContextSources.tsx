import { useState } from 'react'
import { Link } from 'react-router-dom'
import { get } from '../lib/api'
import { asks } from '../lib/ask'
import { useResource } from '../lib/hooks'
import type { ContextSource } from '../lib/ictypes'
import { useAsk } from '../components/AskPanel'
import { ContextPackUpload } from '../components/ContextPackUpload'
import SourcesPanel from '../components/SourcesPanel'
import { CredentialsForm } from '../components/CredentialsForm'
import { Empty, ErrorNote, Loading, PageHeader, Section, StatusPill } from '../components/ui'
import './ic.css'

/* Data sources: what the agent has documented for each source. The DB-poller
 * page ("Tables watched for new rows") is a different thing and stays. */

export function SourcesView({
  sources,
  onDiscover,
  note,
}: {
  sources: ContextSource[]
  onDiscover: (name: string) => void
  note?: string | null
}) {
  return (
    <div>
      {note && <div className="banner">{note}</div>}
      {sources.length === 0 ? (
        <Empty title="No data sources yet">Connect a business database in Setup, then ask the agent to discover it.</Empty>
      ) : (
        <div className="ic-list">
          {sources.map((s) => {
            const waiting = typeof s.pending === 'number' ? s.pending : 0
            const gold = waiting > 0
            return (
              <div className={`ic-row${gold ? ' ic-gold' : ''}`} key={s.name}>
                <span>
                  <strong>{s.name}</strong> <span className="ic-meta">{s.kind}</span> <StatusPill status={s.status} />
                </span>
                {s.description && <span className="ic-body">{s.description}</span>}
                <span className="ic-meta">
                  {s.tables_approved} tables and {s.columns_approved} columns described
                </span>
                {gold && (
                  <span className="ic-owed-line">
                    {waiting} waiting on you · <Link to="/context/schema">Review on Schema</Link>
                  </span>
                )}
                <span className="btn-row">
                  <button className="btn btn-sm" onClick={() => onDiscover(s.name)}>
                    Discover schema
                  </button>
                </span>
              </div>
            )
          })}
        </div>
      )}
      <p className="ic-meta">
        <Link to="/data-sources">Tables watched for new rows</Link>
      </p>
    </div>
  )
}

export default function ContextSources() {
  const { send } = useAsk()
  const [note, setNote] = useState<string | null>(null)
  const res = useResource(() => get<{ sources: ContextSource[] }>('/api/context/sources'), { interval: 30_000 })
  return (
    <div className="ic-page">
      <PageHeader title="Data sources" lede="What the agent knows about each source, and what is waiting for you." />
      <ContextPackUpload />
      {/* Phase 15 (SRC-02): owner-only credentials form */}
      <Section title="Add credentials">
        <CredentialsForm />
      </Section>
      {/* Phase 15 (SRC-10): sources panel */}
      <SourcesPanel />
      {/* End Phase 15 (SRC-10) */}
      {res.error && <ErrorNote message={res.error} onRetry={() => void res.reload()} />}
      {res.loading && !res.data ? (
        <Loading label="Reading data sources" />
      ) : (
        <SourcesView
          sources={res.data?.sources ?? []}
          note={note}
          onDiscover={(name) =>
            void send(asks.discoverSchema(name)).then((ok) =>
              setNote(ok ? 'Sent to the agent — new descriptions will appear on Schema for approval' : null),
            )
          }
        />
      )}
    </div>
  )
}

import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { get, post } from '../lib/api'
import { asks, safeSource } from '../lib/ask'
import { approveMany, editThenApprove, groupSchema, hideTable, type SchemaGroup } from '../lib/contextActions'
import { useAction, useResource, useStored } from '../lib/hooks'
import type { ContextColumn, ContextSource, ContextTable } from '../lib/ictypes'
import { useOwed } from '../lib/owed'
import { useAsk } from '../components/AskPanel'
import { ContextPackUpload } from '../components/ContextPackUpload'
import { PendingBlock, type PendingHandlers } from '../components/PendingBlock'
import { ConfirmButton, Empty, ErrorNote, Loading, PageHeader, StatusPill, Tabs } from '../components/ui'
import './ic.css'

/* Schema: what the agent discovered. A row is gold iff it has a pending
 * proposal (the owner owes a decision), including a revision of an approved
 * row. Gold rows carry data-action-id for the phone walkthrough. */

export type SchemaFilter = 'waiting' | 'all' | 'undocumented' | 'hidden'

export interface SchemaHandlers extends PendingHandlers {
  onApproveAll: (ids: number[]) => void
  onHide: (ref: string, hidden: boolean) => void
}

function ColumnRow({
  c,
  editing,
  busy,
  handlers,
}: {
  c: ContextColumn
  editing: number | null
  busy?: boolean
  handlers: SchemaHandlers
}) {
  const gold = c.pending !== null
  return (
    <div className={`ic-row${gold ? ' ic-gold' : ''}`} data-action-id={gold ? c.pending!.action_id : undefined}>
      <span>
        <span className="mono">{c.column}</span>{' '}
        <span className="ic-meta">
          {c.data_type}
          {c.is_primary_key ? ' · primary key' : ''}
        </span>
      </span>
      {c.description && <span className="ic-body">{c.description}</span>}
      {c.pending && <PendingBlock key={c.pending.action_id} pending={c.pending} editing={editing} busy={busy} handlers={handlers} />}
    </div>
  )
}

function GroupView({
  g,
  filter,
  editing,
  busy,
  handlers,
}: {
  g: SchemaGroup
  filter: SchemaFilter
  editing: number | null
  busy?: boolean
  handlers: SchemaHandlers
}) {
  const t = g.table
  const gold = t.pending !== null
  const hidden = t.status === 'hidden'
  const cols = g.columns.filter((c) =>
    filter === 'waiting' ? c.pending !== null : filter === 'undocumented' ? c.status === 'undocumented' : true,
  )
  return (
    <div className="ic-list">
      <div className={`ic-row${gold ? ' ic-gold' : ''}`} data-action-id={gold ? t.pending!.action_id : undefined}>
        <span>
          <strong className="mono ic-ref">{t.ref}</strong> <StatusPill status={t.status} />
        </span>
        {t.description && <span className="ic-body">{t.description}</span>}
        {t.pending && <PendingBlock key={t.pending.action_id} pending={t.pending} editing={editing} busy={busy} handlers={handlers} />}
        <span className="btn-row">
          {g.pendingCount >= 2 && (
            <ConfirmButton
              className="btn btn-sm btn-primary"
              confirmLabel={`Approve all ${g.pendingCount}?`}
              disabled={busy}
              onConfirm={() => handlers.onApproveAll(g.pendingIds)}
            >
              {`Approve all ${g.pendingCount}`}
            </ConfirmButton>
          )}
          {hidden ? (
            <button className="btn btn-sm" disabled={busy} onClick={() => handlers.onHide(t.ref, false)}>
              Unhide
            </button>
          ) : (
            <ConfirmButton
              className="btn btn-sm"
              confirmLabel="Hide it?"
              disabled={busy}
              onConfirm={() => handlers.onHide(t.ref, true)}
            >
              Hide table
            </ConfirmButton>
          )}
        </span>
      </div>
      {cols.length > 0 && (
        <div className="ic-cols">
          {cols.map((c) => (
            <ColumnRow key={c.ref} c={c} editing={editing} busy={busy} handlers={handlers} />
          ))}
        </div>
      )}
    </div>
  )
}

export function SchemaView({
  groups,
  filter,
  onFilter,
  editing,
  handlers,
  busy,
  error,
  note,
  onDiscover,
}: {
  groups: SchemaGroup[]
  filter: SchemaFilter
  onFilter: (f: SchemaFilter) => void
  editing: number | null
  handlers: SchemaHandlers
  busy?: boolean
  error?: string | null
  note?: string | null
  onDiscover?: () => void
}) {
  const waiting = groups.reduce((n, g) => n + g.pendingCount, 0)
  const isHidden = (g: SchemaGroup) => g.table.status === 'hidden'
  const isUndoc = (g: SchemaGroup) =>
    g.table.status === 'undocumented' || g.columns.some((c) => c.status === 'undocumented')
  const shown = groups.filter((g) =>
    filter === 'waiting'
      ? g.pendingCount > 0
      : filter === 'hidden'
        ? isHidden(g)
        : filter === 'undocumented'
          ? isUndoc(g) && !isHidden(g)
          : true,
  )
  return (
    <div>
      {onDiscover && (
        <div className="btn-row">
          <button className="btn btn-sm" onClick={onDiscover}>
            Discover schema
          </button>
        </div>
      )}
      {note && <div className="banner">{note}</div>}
      {error && <div className="banner banner-broken">{error}</div>}
      <Tabs<SchemaFilter>
        value={filter}
        onChange={onFilter}
        options={[
          { value: 'waiting', label: 'Waiting on you', count: waiting },
          { value: 'all', label: 'All', count: groups.length },
          {
            value: 'undocumented',
            label: 'Undocumented',
            count: groups.filter((g) => isUndoc(g) && !isHidden(g)).length,
          },
          { value: 'hidden', label: 'Hidden', count: groups.filter(isHidden).length },
        ]}
      />
      {shown.length === 0 ? (
        <Empty title={filter === 'waiting' ? 'Nothing waiting on you' : 'No tables here'}>
          {filter === 'waiting'
            ? 'Ask the agent to discover the schema and it will propose descriptions for your approval.'
            : 'Nothing matches this filter.'}
        </Empty>
      ) : (
        shown.map((g) => (
          <GroupView key={g.table.ref} g={g} filter={filter} editing={editing} busy={busy} handlers={handlers} />
        ))
      )}
    </div>
  )
}

export default function Schema() {
  const [params] = useSearchParams()
  const { send } = useAsk()
  const owed = useOwed()
  const act = useAction()
  const [filter, setFilter] = useStored<SchemaFilter>('schema.filter', 'waiting')
  const [editing, setEditing] = useState<number | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [bulkError, setBulkError] = useState<string | null>(null)

  const sources = useResource(() => get<{ sources: ContextSource[] }>('/api/context/sources'), { interval: 30_000 })
  // WR-06: never trust ?source= verbatim; it ends up in an agent instruction.
  const source = safeSource(params.get('source'), sources.data ? sources.data.sources.map((x) => x.name) : null)
  const q = '?source=' + encodeURIComponent(source) + '&status=all&limit=1000'
  const tables = useResource(() => get<{ tables: ContextTable[] }>('/api/context/tables' + q), {
    interval: 15_000,
    deps: [source],
  })
  const columns = useResource(() => get<{ columns: ContextColumn[] }>('/api/context/columns' + q), {
    interval: 15_000,
    deps: [source],
  })

  const refresh = async () => {
    await Promise.all([tables.reload(), columns.reload(), owed.reload()])
  }
  const decide = (fn: () => Promise<unknown>) =>
    void act.run(fn).then(async (ok) => {
      if (ok) setEditing(null)
      await refresh()
    })

  const handlers: SchemaHandlers = {
    onApprove: (id) => decide(() => post('/api/actions/' + id + '/approve')),
    onReject: (id) => decide(() => post('/api/actions/' + id + '/reject')),
    onEdit: setEditing,
    onSave: (id, text) => decide(() => editThenApprove(id, text)),
    onApproveAll: (ids) => {
      setBulkError(null)
      decide(async () => {
        const r = await approveMany(ids)
        if (r.error) setBulkError(`Approved ${r.approved} of ${ids.length}; ${r.error}`)
      })
    },
    onHide: (ref, hidden) => decide(() => hideTable(ref, hidden)),
  }

  const allGroups = groupSchema(tables.data?.tables ?? [], columns.data?.columns ?? [])
  // Quick 014: the Memory graph links a table entity here as ?table=<ref>.
  const focus = params.get('table')
  const groups = focus ? allGroups.filter((g) => g.table.ref === focus) : allGroups
  const all = sources.data?.sources ?? []

  return (
    <div className="ic-page">
      <PageHeader title="Schema" lede="The tables and columns the agent found. Approve, edit or hide what it proposes." />
      <ContextPackUpload />
      {all.length > 1 && (
        <p className="ic-meta">
          Source: <span className="mono">{source}</span> ·{' '}
          {all.map((s) => (
            <a key={s.name} href={'/context/schema?source=' + encodeURIComponent(s.name)} style={{ marginRight: 8 }}>
              {s.name}
            </a>
          ))}
        </p>
      )}
      {focus && (
        <p className="ic-meta" data-schema-focus={focus}>
          Showing <span className="mono">{focus}</span> from the knowledge graph ·{' '}
          <a href={'/context/schema?source=' + encodeURIComponent(source)}>Show all tables</a>
        </p>
      )}
      {tables.error && <ErrorNote message={tables.error} onRetry={() => void tables.reload()} />}
      {tables.loading && !tables.data ? (
        <Loading label="Reading the schema" />
      ) : (
        <SchemaView
          groups={groups}
          filter={focus ? 'all' : filter}
          onFilter={setFilter}
          editing={editing}
          handlers={handlers}
          busy={act.pending}
          error={act.error ?? bulkError}
          note={note}
          onDiscover={() =>
            void send(asks.discoverSchema(source)).then((ok) =>
              setNote(ok ? 'Sent to the agent — new descriptions will appear here for approval' : null),
            )
          }
        />
      )}
    </div>
  )
}

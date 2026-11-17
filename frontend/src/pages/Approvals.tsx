import { useState } from 'react'
import { Link } from 'react-router-dom'
import { get, post } from '../lib/api'
import { ago, isoTitle, titleCase } from '../lib/format'
import { useAction, useResource, useStored } from '../lib/hooks'
import { approveMany } from '../lib/contextActions'
import { importGroups, type ImportGroup } from '../lib/contextpack'
import { loadProposedActions, useOwed } from '../lib/owed'
import { useProfile } from '../lib/profile'
import type { Action } from '../lib/types'
import { ActionDetail, actionLine, actionTitle } from '../components/ActionPayload'
import { Async, Empty, PageHeader, StatusPill, Tabs } from '../components/ui'

/* The human gate. Everything with a side effect the system wasn't allowed to
 * take alone lands here, and approving is the moment it actually happens — so
 * each row shows what would be done, not a truncated blob of JSON. */

type Filter = 'proposed' | 'executed' | 'rejected' | 'failed'

const FILTERS: { value: Filter; label: string }[] = [
  { value: 'proposed', label: 'Waiting on you' },
  { value: 'executed', label: 'Done' },
  { value: 'rejected', label: 'Rejected' },
  { value: 'failed', label: 'Failed' },
]

export default function Approvals() {
  const owed = useOwed()
  const intel = useProfile().profile === 'intelligence'
  const [filter, setFilter] = useStored<Filter>('approvals.filter', 'proposed')

  const { data, error, loading, reload } = useResource(
    () =>
      intel && filter === 'proposed'
        ? loadProposedActions(true)
        : get<Action[]>(`/api/actions?status=${filter}&limit=100`),
    { interval: 15_000, deps: [filter, intel] },
  )

  return (
    <>
      <PageHeader
        title="Approvals"
        lede="Actions the assistant drafted but is not allowed to take on its own. Approving one carries it out immediately."
        actions={
          <Tabs
            value={filter}
            onChange={setFilter}
            options={FILTERS.map((f) => ({
              ...f,
              count: f.value === 'proposed' ? (intel ? owed.counts.approvalsAll : owed.counts.approvals) : undefined,
            }))}
          />
        }
      />

      {intel && filter === 'proposed' && owed.contextProposals.length > 0 && (
        <p className="dimmer" style={{ fontSize: 'var(--fs-small)' }}>
          Schema descriptions are quicker to review on <Link to="/context/schema">Schema</Link>.
        </p>
      )}

      <Async
        data={data}
        error={error}
        loading={loading}
        reload={reload}
        empty={
          filter === 'proposed' ? (
            <div className="panel">
              <Empty title="Nothing waiting on you">
                When the assistant drafts something it can't send by itself — an extracted
                invoice, an email, a task to run — it stops here and waits.
              </Empty>
            </div>
          ) : (
            <div className="panel">
              <Empty title={`No ${filter} actions`} />
            </div>
          )
        }
      >
        {(actions) => (
          <div className="stack">
            {filter === 'proposed' &&
              importGroups(actions).map((g) => (
                <ImportBar
                  key={g.runId}
                  group={g}
                  onDone={() => {
                    void reload()
                    void owed.reload()
                  }}
                />
              ))}
            {actions.map((a) => (
              <ActionRow
                key={a.id}
                action={a}
                onDecided={() => {
                  void reload()
                  void owed.reload()
                }}
              />
            ))}
          </div>
        )}
      </Async>
    </>
  )
}

export function ActionRow({ action, onDecided }: { action: Action; onDecided: () => void }) {
  const [open, setOpen] = useState(action.status === 'proposed')
  const act = useAction()
  const pendingDecision = action.status === 'proposed'

  const decide = async (verb: 'approve' | 'reject') => {
    const ok = await act.run(() => post(`/api/actions/${action.id}/${verb}`))
    if (ok) onDecided()
  }

  return (
    <section className={`panel${pendingDecision ? '' : ''}`}>
      <div className="panel-head">
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="row" style={{ gap: 'var(--s2)', flexWrap: 'wrap' }}>
            <h2 className="panel-title">{actionTitle(action)}</h2>
            <StatusPill status={action.status} />
            {action.tier && (
              <span className="pill" title="The autonomy tier the playbook was granted">
                {titleCase(action.tier)}
              </span>
            )}
          </div>
          <div className="dimmer" style={{ fontSize: 'var(--fs-tiny)', marginTop: 3 }}>
            <span className="mono">#{action.id}</span> · {action.kind.replace(/_/g, ' ')}
            {action.playbook_name ? ` · ${action.playbook_name}` : ''}
            {action.case_id ? (
              <>
                {' · '}
                <Link to={`/cases/${action.case_id}`}>{action.case_title || `Case #${action.case_id}`}</Link>
              </>
            ) : null}
            {' · '}
            <span title={isoTitle(action.created_at)}>{ago(action.created_at)}</span>
          </div>
        </div>

        {pendingDecision ? (
          <div className="btn-row">
            <button className="btn btn-owed btn-sm" disabled={act.pending} onClick={() => decide('approve')}>
              Approve
            </button>
            <button className="btn btn-sm" disabled={act.pending} onClick={() => decide('reject')}>
              Reject
            </button>
          </div>
        ) : (
          <button className="btn btn-quiet btn-sm" onClick={() => setOpen((v) => !v)}>
            {open ? 'Hide' : 'Details'}
          </button>
        )}
      </div>

      {act.error && (
        <div className="panel-body" style={{ paddingBottom: 0 }}>
          <div className="banner banner-broken">{act.error}</div>
        </div>
      )}

      {open ? (
        <div className="panel-body">
          <ActionDetail action={action} />
        </div>
      ) : (
        <div className="panel-body dim">{actionLine(action)}</div>
      )}
    </section>
  )
}

export function ImportBar({
  group,
  onDone,
  approve = approveMany,
}: {
  group: ImportGroup
  onDone: () => void
  approve?: (ids: number[]) => Promise<{ approved: number; error?: string; results?: unknown[] }>
}) {
  const [note, setNote] = useState<string | null>(null)
  const act = useAction()
  const go = async () => {
    setNote(null)
    await act.run(async () => {
      const r = await approve(group.ids)
      if (r.error) setNote(`Approved ${r.approved} of ${group.ids.length}; ${r.error}`)
      onDone()
    })
  }
  return (
    <div className="banner">
      <span>
        From one import (task #{group.runId}): {group.ids.length} proposals. Each approval is recorded on its own.{' '}
      </span>
      <button className="btn btn-owed btn-sm" disabled={act.pending} onClick={go}>
        Approve all {group.ids.length} from this import
      </button>
      {note && <div className="banner banner-broken">{note}</div>}
      {act.error && <div className="banner banner-broken">{act.error}</div>}
    </div>
  )
}

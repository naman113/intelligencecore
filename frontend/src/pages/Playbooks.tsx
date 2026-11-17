import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { asObj, get, patch } from '../lib/api'
import { runsHref } from '../lib/runs'
import { titleCase, truncate } from '../lib/format'
import { useAction, useResource } from '../lib/hooks'
import type { Playbook } from '../lib/types'
import { FailedRunNote, RunNowView, isActiveRun, runPlaybookNow } from '../components/RunNow'
import { Async, ConfirmButton, Empty, PageHeader, Section, StatusPill } from '../components/ui'

/* Playbooks are the standing work: a trigger, a goal, and a tier that decides
 * how far the agent may go without asking. The tier is enforced inside the tool
 * functions, not in the prompt — so what it says here is what actually binds. */

const TIERS: Record<string, { label: string; blurb: string }> = {
  notify: {
    label: 'Tells you only',
    blurb: 'Can message you and escalate to you. Never contacts anyone else.',
  },
  autonomous_message: {
    label: 'Messages people',
    blurb: 'Can chase commitments and post in the originating group on its own.',
  },
  draft: {
    label: 'Drafts for approval',
    blurb: 'Writes actions and stops. Nothing leaves until you approve it.',
  },
}

function triggerLabel(trigger: unknown): string {
  const t = asObj<Record<string, unknown>>(trigger)
  if (t.type === 'cron') {
    const hour = String(t.hour ?? '?').padStart(2, '0')
    // Quick 047: scheduled reports carry a minute and their own zone
    const minute = String(typeof t.minute === 'number' ? t.minute : 0).padStart(2, '0')
    const zone = typeof t.tz === 'string' && t.tz ? ` (${t.tz})` : ''
    return `Every day at ${hour}:${minute}${zone}`
  }
  if (typeof t.type === 'string') return titleCase(t.type)
  return '—'
}

export default function Playbooks() {
  // Quick 023: refresh every 5 s only while some playbook run is queued or running.
  const [fast, setFast] = useState(false)
  const { data, error, loading, reload } = useResource(() => get<Playbook[]>('/api/playbooks'), {
    interval: fast ? 5_000 : 30_000,
  })
  const anyActive = (data ?? []).some((p) => isActiveRun(p.last_run?.status))
  useEffect(() => setFast(anyActive), [anyActive])
  const act = useAction()
  const [starting, setStarting] = useState<number | null>(null)
  const [runErr, setRunErr] = useState<{ id: number; message: string } | null>(null)
  const runNow = async (id: number) => {
    setStarting(id)
    setRunErr(null)
    try {
      await runPlaybookNow(id)
    } catch (e) {
      setRunErr({ id, message: e instanceof Error ? e.message : 'Could not start the run.' })
    }
    await reload()
    setStarting(null)
  }

  const set = async (id: number, status: Playbook['status']) => {
    const ok = await act.run(() => patch(`/api/playbooks/${id}`, { status }))
    if (ok) void reload()
  }

  const drafts = (data ?? []).filter((p) => p.status === 'draft').length

  return (
    <>
      <PageHeader
        title="Playbooks"
        lede="Standing instructions that run on a schedule. A playbook the system proposed does nothing at all until you activate it."
        actions={
          <Link className="btn btn-sm" to="/system/runs?kind=playbooks">
            Playbook runs
          </Link>
        }
      />

      <div className="stack">
        {act.error && <div className="banner banner-broken">{act.error}</div>}

        {drafts > 0 && (
          <div className="banner banner-owed">
            {drafts} playbook{drafts === 1 ? '' : 's'} written and waiting. Read the goal and the
            tier before activating — the tier is what it is allowed to do to people.
          </div>
        )}

        <Section title="All playbooks" flush>
          <Async
            data={data}
            error={error}
            loading={loading}
            reload={reload}
            empty={<Empty title="No playbooks yet" />}
          >
            {(list) => (
              <div className="table-scroll">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Runs</th>
                      <th>Goal</th>
                      <th>Allowed to</th>
                      <th>Status</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((p) => {
                      const tier = TIERS[p.autonomy_tier || ''] ?? {
                        label: p.autonomy_tier || 'unset',
                        blurb: 'Unknown tier — the tool layer denies everything by default.',
                      }
                      return (
                        <tr
                          key={p.id}
                          style={p.status === 'draft' ? { boxShadow: 'inset 2px 0 0 var(--owed)' } : undefined}
                        >
                          <td>
                            <strong>{p.name}</strong>
                            {p.proposed_by && (
                              <div className="dimmer" style={{ fontSize: 'var(--fs-micro)' }}>
                                written by {p.proposed_by}
                              </div>
                            )}
                          </td>
                          <td className="dim nowrap">{triggerLabel(p.trigger)}</td>
                          <td className="dim" style={{ maxWidth: 320 }}>
                            {truncate(p.goal, 140)}
                          </td>
                          <td style={{ maxWidth: 220 }}>
                            <div>{tier.label}</div>
                            <div className="dimmer" style={{ fontSize: 'var(--fs-micro)' }}>
                              {tier.blurb}
                            </div>
                          </td>
                          <td>
                            <StatusPill
                              status={p.status}
                              tone={p.status === 'draft' ? 'owed' : undefined}
                              label={p.status === 'draft' ? 'not running' : p.status}
                            />
                            {p.last_run?.status === 'failed' && (
                              <div>
                                <FailedRunNote error={p.last_run.error} />
                              </div>
                            )}
                          </td>
                          <td>
                            <div className="btn-row">
                              <Link className="btn btn-quiet btn-sm" to={runsHref(`playbook:${p.id}`)}>
                                Runs
                              </Link>
                              {p.status !== 'active' && (
                                <button
                                  className="btn btn-owed btn-sm"
                                  disabled={act.pending}
                                  onClick={() => void set(p.id, 'active')}
                                >
                                  Activate
                                </button>
                              )}
                              {p.status === 'active' && asObj<Record<string, unknown>>(p.trigger).type === 'cron' && (
                                <RunNowView
                                  status={p.last_run?.status}
                                  runId={p.last_run?.id}
                                  pending={starting === p.id}
                                  error={runErr?.id === p.id ? runErr.message : null}
                                  onRun={() => void runNow(p.id)}
                                />
                              )}
                              {p.status === 'active' && (
                                <button
                                  className="btn btn-sm"
                                  disabled={act.pending}
                                  onClick={() => void set(p.id, 'draft')}
                                >
                                  Pause
                                </button>
                              )}
                              {p.status !== 'archived' && (
                                <ConfirmButton
                                  className="btn btn-quiet btn-sm"
                                  confirmLabel="Archive it?"
                                  onConfirm={() => void set(p.id, 'archived')}
                                >
                                  Archive
                                </ConfirmButton>
                              )}
                            </div>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Async>
        </Section>
      </div>
    </>
  )
}

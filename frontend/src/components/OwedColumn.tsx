import { useState, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { patch, post } from '../lib/api'
import { readPublishDashboard } from '../lib/contracts'
import { payloadDraftLink, useDashboardsBase } from '../lib/dashlinks'
import { plural, truncate } from '../lib/format'
import { asks } from '../lib/ask'
import { useAction } from '../lib/hooks'
import { alertBody, alertKicker, useOwed, type Owed } from '../lib/owed'
import type { Chat } from '../lib/types'
import { actionLine, actionTitle } from './ActionPayload'
import { useAsk } from './AskPanel'
import '../pages/today.css'

/* The gold column on Overview: everything a human owes, from the one poll in
 * lib/owed.tsx. Gold appears here and nowhere else on Overview. Every card root
 * carries data-owed="<kind>" and at least one button. Agent text is rendered as
 * React text nodes only. */

export type OwedKind =
  | 'setup'
  | 'whatsapp'
  | 'interview'
  | 'draft'
  | 'context'
  | 'broken-dashboard'
  | 'failing-forecast' // Phase 13.1
  | 'approval'
  | 'chat'
  | 'case-alert' // Quick 006
  | 'case-ack'
  | 'case-missed'
  | 'unknown-sender'
  | 'health' // Phase 11

const GROUP_NAMES: Record<string, string> = {
  llm: 'The model',
  business_db: 'Your database',
  whatsapp: 'WhatsApp',
  memory_graph: 'Graph memory',
  tracing: 'Tracing',
  tuning: 'Tuning',
  interface: 'Interface',
  warehouse: 'The warehouse', // Phase 14 (WH-08)
}
const pretty = (g: string) => GROUP_NAMES[g] ?? g.replace(/_/g, ' ')

function sinceTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

/* WR-15: Approve on a dashboard draft is only offered once the owner has opened
 * the draft (link exists and its id is in the opened set). */
export function canApproveDraft(link: string | null, opened: ReadonlySet<number>, id: number): boolean {
  return link !== null && opened.has(id)
}

function chatName(c: Chat): string {
  if (c.subject) return c.subject
  return c.is_group ? 'A group with no name yet' : 'A new direct chat'
}

function Card({
  kind,
  kicker,
  title,
  index,
  children,
  actions,
}: {
  kind: OwedKind
  kicker: string
  title: ReactNode
  index: number
  children?: ReactNode
  actions: ReactNode
}) {
  return (
    <article
      data-owed={kind}
      className="card card-owed rise"
      style={{ animationDelay: `${Math.min(index, 7) * 60}ms` }}
    >
      <div className="owed-kicker">{kicker}</div>
      <div className="owed-title">{title}</div>
      {children && <div className="owed-body">{children}</div>}
      <div className="owed-actions">{actions}</div>
    </article>
  )
}

export interface OwedColumnViewProps {
  owed: Owed
  base: string | null
  configured: boolean
  onDecide: (id: number, verb: 'approve' | 'reject') => void
  onNavigate: (to: string) => void
  onAsk: (message: string) => void
  onChat?: (jid: string, status: 'active' | 'ignored') => void
  onAck?: (caseId: number) => void
  /** Quick 006: the owner acknowledged a KPI-alert case. */
  onSeen?: (caseId: number) => void
  busy?: boolean
  error?: string | null
}

export function OwedColumnView({
  owed,
  base,
  configured,
  onDecide,
  onNavigate,
  onAsk,
  onChat,
  onAck,
  onSeen,
  busy = false,
  error = null,
}: OwedColumnViewProps) {
  const [opened, setOpened] = useState<ReadonlySet<number>>(() => new Set())
  let i = -1
  const health = owed.healthAlerts ?? [] // Phase 11: GOLIVE-05
  const others = owed.proposedActions.filter(
    (a) => a.kind !== 'context_change' && a.kind !== 'publish_dashboard',
  )
  const nothing =
    owed.counts.total === 0 &&
    health.length === 0 &&
    owed.dashboardDrafts.length === 0 &&
    owed.contextProposals.length === 0 &&
    owed.brokenDashboards.length === 0 &&
    owed.failingForecasts.length === 0

  if (nothing) {
    return (
      <div className="owed-list">
        {error && <div className="banner banner-broken">{error}</div>}
        <div className="rewarded">
          <div className="rewarded-title">
            <span className="dot dot-settled" /> Nothing waiting on you
          </div>
          <p className="rewarded-body">
            Anything the agents want approved will appear right here.
          </p>
        </div>
      </div>
    )
  }

  const ctxCount = Math.max(owed.counts.schema, owed.contextProposals.length - owed.rulesPendingExact, 0)

  return (
    <div className="owed-list">
      {error && <div className="banner banner-broken">{error}</div>}

      {/* Phase 11: GOLIVE-05 health alerts first */}
      {health.map((h) => (
        <Card
          key={`health${h.id}`}
          kind="health"
          index={++i}
          kicker="System health"
          title={h.title}
          actions={
            <button className="btn btn-owed btn-sm" onClick={() => onNavigate('/system/health')}>
              Open health
            </button>
          }
        >
          {h.detail}
          {h.created_at ? ` \u00b7 since ${sinceTime(h.created_at)}` : ''}
        </Card>
      ))}

      {owed.missingGroups.length > 0 && (
        <Card
          kind="setup"
          index={++i}
          kicker="Not connected"
          title={`${owed.missingGroups.map(pretty).join(', ')} ${
            owed.missingGroups.length === 1 ? 'is' : 'are'
          } not set up`}
          actions={
            <button className="btn btn-owed btn-sm" onClick={() => onNavigate('/system/setup')}>
              Open setup
            </button>
          }
        >
          It stays idle until {owed.missingGroups.length === 1 ? 'this is' : 'these are'} connected.
        </Card>
      )}

      {owed.whatsappFailing && (
        <Card
          kind="whatsapp"
          index={++i}
          kicker="Connection"
          title="WhatsApp connection failing"
          actions={
            <button className="btn btn-owed btn-sm" onClick={() => onNavigate('/system/whatsapp')}>
              Open WhatsApp
            </button>
          }
        >
          {owed.whatsappDetail || 'The last connection test failed.'}
        </Card>
      )}

      {owed.onboarding && owed.onboarding.status !== 'complete' && (
        <Card
          kind="interview"
          index={++i}
          kicker={owed.onboarding.status === 'active' ? 'Interview in progress' : 'First run'}
          title="Tell it about your business"
          actions={
            <button className="btn btn-owed btn-sm" onClick={() => onNavigate('/setup/interview')}>
              {owed.onboarding.status === 'active' ? 'Continue' : 'Start the interview'}
            </button>
          }
        >
          A few minutes of conversation is what everything else builds on.
        </Card>
      )}

      {/* Quick 006: one card per open KPI-alert case (repeat fires attach to it). */}
      {owed.alertCases.map((c) => (
        <Card
          key={`alert${c.id}`}
          kind="case-alert"
          index={++i}
          kicker={alertKicker(c)}
          title={c.title.replace(/^KPI alert: /, '')}
          actions={
            <>
              <button className="btn btn-owed btn-sm" onClick={() => onNavigate(`/cases/${c.id}`)}>
                Open case
              </button>
              {onSeen && (
                <button className="btn btn-sm" disabled={busy} onClick={() => onSeen(c.id)}>
                  Got it
                </button>
              )}
            </>
          }
        >
          {truncate(alertBody(c), 260)}
        </Card>
      ))}

      {owed.dashboardDrafts.map((a) => {
        const v = readPublishDashboard(a.payload)
        const link = payloadDraftLink(v.draftUrl, base, configured)
        return (
          <Card
            key={`draft${a.id}`}
            kind="draft"
            index={++i}
            kicker="Dashboard draft"
            title={v.title || v.slug || actionTitle(a)}
            actions={
              <>
                {link ? (
                  <button
                    className="btn btn-owed btn-sm"
                    disabled={busy || !canApproveDraft(link, opened, a.id)}
                    title={canApproveDraft(link, opened, a.id) ? undefined : 'Open the draft first'}
                    onClick={() => onDecide(a.id, 'approve')}
                  >
                    Approve
                  </button>
                ) : (
                  <Link
                    className="btn btn-owed btn-sm"
                    to={v.slug ? `/dashboards/${encodeURIComponent(v.slug)}` : '/dashboards'}
                  >
                    Review
                  </Link>
                )}
                <button className="btn btn-sm" disabled={busy} onClick={() => onDecide(a.id, 'reject')}>
                  Reject
                </button>
                {link && (
                  <a
                    className="btn btn-quiet btn-sm"
                    href={link}
                    rel="noopener noreferrer"
                    target="_blank"
                    onClick={() => setOpened((s) => new Set(s).add(a.id))}
                  >
                    Open draft
                  </a>
                )}
              </>
            }
          >
            {v.changeNote || actionLine(a)}
          </Card>
        )
      })}

      {owed.contextProposals.length > 0 && (
        <Card
          kind="context"
          index={++i}
          kicker="Context waiting"
          title={`${plural(Math.max(ctxCount, 0), 'description')} waiting across ${plural(owed.contextTables, 'table')}`}
          actions={
            <>
              <button className="btn btn-owed btn-sm" onClick={() => onNavigate('/context/schema')}>
                Review on Schema
              </button>
              {owed.counts.rules > 0 && (
                <button className="btn btn-sm" onClick={() => onNavigate('/context/rules')}>
                  Review rules
                </button>
              )}
            </>
          }
        >
          The agent proposed these from your data. Nothing is used until you approve it.
        </Card>
      )}

      {owed.brokenDashboards.map((d) => (
        <Card
          key={`broken${d.id}`}
          kind="broken-dashboard"
          index={++i}
          kicker="Dashboard failing"
          title={d.title}
          actions={
            <>
              <button
                className="btn btn-owed btn-sm"
                disabled={busy}
                onClick={() =>
                  onAsk(asks.fixDashboard(d.slug, d.current_version ?? 0))
                }
              >
                Ask the agent to fix
              </button>
              <button className="btn btn-sm" onClick={() => onNavigate(`/dashboards/${d.slug}`)}>
                Open
              </button>
            </>
          }
        >
          {truncate(d.current_last_error, 160)}
        </Card>
      ))}

      {/* Phase 13.1: one gold item per failing forecast definition */}
      {owed.failingForecasts.map((f) => (
        <Card
          key={`fcst${f.id}`}
          kind="failing-forecast"
          index={++i}
          kicker="Forecast failing"
          title={f.name}
          actions={
            <>
              <button
                className="btn btn-owed btn-sm"
                disabled={busy}
                onClick={() => onAsk(asks.explainForecast({ id: f.id, name: f.name }))}
              >
                Ask the agent why
              </button>
              <Link className="btn btn-sm" to={`/forecasts/${f.id}`}>
                Open
              </Link>
            </>
          }
        >
          {truncate(f.last_error, 160)}
        </Card>
      ))}

      {others.map((a) => (
        <Card
          key={`act${a.id}`}
          kind="approval"
          index={++i}
          kicker={a.kind === 'record_extracted_data' ? 'Approve this data' : 'Approve this action'}
          title={actionTitle(a)}
          actions={
            <>
              <button className="btn btn-owed btn-sm" disabled={busy} onClick={() => onDecide(a.id, 'approve')}>
                Approve
              </button>
              <button className="btn btn-sm" disabled={busy} onClick={() => onDecide(a.id, 'reject')}>
                Reject
              </button>
              <button className="btn btn-quiet btn-sm" onClick={() => onNavigate('/approvals')}>
                See everything
              </button>
            </>
          }
        >
          {actionLine(a)}
        </Card>
      ))}

      {owed.pendingChats.map((c) => (
        <Card
          key={`chat${c.jid}`}
          kind="chat"
          index={++i}
          kicker="New chat"
          title={chatName(c)}
          actions={
            onChat ? (
              <>
                <button className="btn btn-owed btn-sm" disabled={busy} onClick={() => onChat(c.jid, 'active')}>
                  Activate
                </button>
                <button className="btn btn-sm" disabled={busy} onClick={() => onChat(c.jid, 'ignored')}>
                  Ignore
                </button>
              </>
            ) : (
              <button className="btn btn-owed btn-sm" onClick={() => onNavigate('/chats')}>
                Open chats
              </button>
            )
          }
        >
          It's listening but staying silent here until you say it can act.
        </Card>
      ))}

      {owed.ackCases.map((c) => (
        <Card
          key={`ack${c.id}`}
          kind="case-ack"
          index={++i}
          kicker="Ready to close"
          title={c.title}
          actions={
            <>
              {onAck && (
                <button className="btn btn-owed btn-sm" disabled={busy} onClick={() => onAck(c.id)}>
                  Confirm it's done
                </button>
              )}
              <button
                className={onAck ? 'btn btn-sm' : 'btn btn-owed btn-sm'}
                onClick={() => onNavigate(`/cases/${c.id}`)}
              >
                Open case
              </button>
            </>
          }
        >
          {c.summary || 'It thinks this is finished and wants you to confirm before closing.'}
        </Card>
      ))}

      {owed.violatedCases.map((c) => (
        <Card
          key={`vio${c.id}`}
          kind="case-missed"
          index={++i}
          kicker={`${plural(c.violated_exp, 'commitment')} missed`}
          title={c.title}
          actions={
            <button className="btn btn-owed btn-sm" onClick={() => onNavigate(`/cases/${c.id}`)}>
              Open case
            </button>
          }
        >
          Chasing hasn't worked. Someone promised something and the date has passed.
        </Card>
      ))}

      {owed.unmatched.map((u) => (
        <Card
          key={`un${u.actor_raw}`}
          kind="unknown-sender"
          index={++i}
          kicker="Unknown sender"
          title={u.actor_raw}
          actions={
            <button className="btn btn-owed btn-sm" onClick={() => onNavigate('/people')}>
              Add them
            </button>
          }
        >
          {plural(u.event_count, 'message')} it can't attribute to anyone.
        </Card>
      ))}
    </div>
  )
}

export function OwedColumn() {
  const owed = useOwed()
  const { base, configured } = useDashboardsBase()
  const act = useAction()
  const navigate = useNavigate()
  const ask = useAsk()

  const after = async (fn: () => Promise<unknown>) => {
    const ok = await act.run(fn)
    if (ok) await owed.reload()
  }

  return (
    <OwedColumnView
      owed={owed}
      base={base}
      configured={configured}
      busy={act.pending}
      error={act.error}
      onDecide={(id, verb) => void after(() => post(`/api/actions/${id}/${verb}`))}
      onNavigate={(to) => navigate(to)}
      onAsk={(message) => void ask.send(message)}
      onChat={(jid, status) =>
        void after(() => patch(`/api/chats/${encodeURIComponent(jid)}`, { status }))
      }
      onAck={(id) => void after(() => post(`/api/cases/${id}/ack`))}
      onSeen={(id) => void after(() => post(`/api/cases/${id}/seen`))}
    />
  )
}

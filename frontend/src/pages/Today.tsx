import { Link, useNavigate } from 'react-router-dom'
import { type ReactNode } from 'react'
import { get, post, patch } from '../lib/api'
import { useAction, useResource } from '../lib/hooks'
import { alertBody, alertKicker, useOwed } from '../lib/owed'
import { clock, hourOf, longDate, plural, threadLabel, truncate } from '../lib/format'
import type { Action, Chat, Run } from '../lib/types'
import { actionLine, actionTitle } from '../components/ActionPayload'
import { Loading } from '../components/ui'
import './today.css'

/* Two questions, one page: what did it do without me, and what is it waiting
 * on me for. Everything else in the product is a place you go when the answer
 * to one of those needs following up. */

interface FeedItem {
  key: string
  at: string
  title: ReactNode
  detail?: string
  failed?: boolean
}

async function settle<T>(p: Promise<T>, fallback: T): Promise<T> {
  try {
    return await p
  } catch {
    return fallback
  }
}

function startOfToday(): number {
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  return d.getTime()
}

export default function Today() {
  const owed = useOwed()
  const navigate = useNavigate()

  const feed = useResource(
    async () => {
      const [executed, runs] = await Promise.all([
        settle(get<Action[]>('/api/actions?status=executed&limit=60'), []),
        settle(get<Run[]>('/api/runs?limit=60'), []),
      ])
      return { executed, runs }
    },
    { interval: 15_000 },
  )

  const subjects = new Map(owed.chats.filter((c) => c.subject).map((c) => [c.jid, c.subject!]))
  const cutoff = startOfToday()

  const items: FeedItem[] = []
  for (const a of feed.data?.executed ?? []) {
    const at = a.executed_at || a.created_at
    if (!at || new Date(at).getTime() < cutoff) continue
    items.push({
      key: `a${a.id}`,
      at,
      title: actionTitle(a),
      detail: actionLine(a),
    })
  }
  for (const r of feed.data?.runs ?? []) {
    const at = r.ended_at || r.created_at
    if (!at || new Date(at).getTime() < cutoff) continue
    if (r.status !== 'done' && r.status !== 'failed') continue
    items.push({
      key: `r${r.id}`,
      at,
      failed: r.status === 'failed',
      title: (
        <Link to={`/system/runs/${r.id}`}>
          {r.status === 'failed' ? 'Could not answer in ' : 'Answered in '}
          {threadLabel(r.thread_id, subjects)}
        </Link>
      ),
      detail: truncate(r.error || r.result_summary || '', 120),
    })
  }
  items.sort((a, b) => new Date(b.at).getTime() - new Date(a.at).getTime())

  const total = owed.counts.total
  const busy = feed.loading && !feed.data

  return (
    <>
      <header className="today-head">
        <h1 className="today-date">{longDate(new Date())}</h1>
        <p className="today-line">
          {items.length > 0
            ? `${plural(items.length, 'thing')} handled since midnight.`
            : 'Nothing handled yet today.'}{' '}
          {total > 0 ? (
            <span style={{ color: 'var(--owed-hi)' }}>{plural(total, 'thing')} waiting on you.</span>
          ) : (
            'Nothing waiting on you.'
          )}
        </p>
      </header>

      <div className="divide">
        <section className="side side-left" aria-labelledby="handled-head">
          <div className="side-head">
            <span className="eyebrow" id="handled-head">
              Handled alone
            </span>
          </div>

          {busy ? (
            <Loading label="Reading today" />
          ) : items.length === 0 ? (
            <p className="dimmer" style={{ fontSize: 'var(--fs-small)', maxWidth: '40ch' }}>
              Quiet so far. Messages answered, reminders sent and documents read will appear here
              as they happen — <Link to="/system/runs">see everything it has ever run</Link>.
            </p>
          ) : (
            <Timeline items={items} />
          )}
        </section>

        <div className="divide-rule" aria-hidden="true" />

        <section className="side side-right" aria-labelledby="owed-head">
          <div className="side-head">
            <span className="eyebrow" id="owed-head">
              Needs you
            </span>
            {total > 0 && <span className="side-count">{total}</span>}
          </div>

          {owed.loading && total === 0 ? (
            <Loading label="Checking" />
          ) : total === 0 ? (
            <div className="rewarded">
              <div className="rewarded-title">
                <span className="dot dot-settled" /> Nothing needs you
              </div>
              <p className="rewarded-body">
                Everything it met today, it finished. You'll see anything it can't decide alone
                right here.
              </p>
            </div>
          ) : (
            <Queue navigate={navigate} />
          )}
        </section>
      </div>
    </>
  )
}

/* ── The log ───────────────────────────────────────────────────────────── */

function Timeline({ items }: { items: FeedItem[] }) {
  const out: ReactNode[] = []
  let lastHour: number | null = null

  for (const item of items) {
    const h = hourOf(item.at)
    if (h !== lastHour) {
      lastHour = h
      out.push(
        <div className="tl-hour" key={`h${h}-${item.key}`}>
          <span className="tl-hour-num">{String(h ?? 0).padStart(2, '0')}</span>
          <span className="tl-hour-rule" />
        </div>,
      )
    }
    out.push(
      <div className={`tl-item${item.failed ? ' tl-failed' : ''}`} key={item.key}>
        <time className="tl-time">{clock(item.at)}</time>
        <div className="tl-body">
          <div className="tl-title">{item.title}</div>
          {item.detail && <div className="tl-detail">{item.detail}</div>}
        </div>
      </div>,
    )
  }
  return <>{out}</>
}

/* ── The queue ─────────────────────────────────────────────────────────── */

function OwedCard({
  kicker,
  title,
  index,
  children,
  actions,
}: {
  kicker: string
  title: ReactNode
  index: number
  children?: ReactNode
  actions?: ReactNode
}) {
  return (
    <article
      className="card card-owed rise"
      style={{ animationDelay: `${Math.min(index, 7) * 60}ms` }}
    >
      <div className="owed-kicker">{kicker}</div>
      <div className="owed-title">{title}</div>
      {children && <div className="owed-body">{children}</div>}
      {actions && <div className="owed-actions">{actions}</div>}
    </article>
  )
}

function Queue({ navigate }: { navigate: ReturnType<typeof useNavigate> }) {
  const owed = useOwed()
  const act = useAction()
  let i = -1

  const after = async (fn: () => Promise<unknown>) => {
    const ok = await act.run(fn)
    if (ok) await owed.reload()
  }

  return (
    <div className="owed-list">
      {act.error && <div className="banner banner-broken">{act.error}</div>}

      {/* Nothing else matters if the agent can't reach its own dependencies. */}
      {owed.missingGroups.length > 0 && (
        <OwedCard
          index={++i}
          kicker="Not connected"
          title={`${owed.missingGroups.map(pretty).join(', ')} ${
            owed.missingGroups.length === 1 ? 'is' : 'are'
          } not set up`}
          actions={
            <button className="btn btn-owed btn-sm" onClick={() => navigate('/system/setup')}>
              Open setup
            </button>
          }
        >
          It stays idle until {owed.missingGroups.length === 1 ? 'this is' : 'these are'} connected.
        </OwedCard>
      )}

      {owed.onboarding && owed.onboarding.status !== 'complete' && (
        <OwedCard
          index={++i}
          kicker={owed.onboarding.status === 'active' ? 'Interview in progress' : 'First run'}
          title="Tell it about your business"
          actions={
            <button className="btn btn-owed btn-sm" onClick={() => navigate('/setup/interview')}>
              {owed.onboarding.status === 'active' ? 'Continue' : 'Start the interview'}
            </button>
          }
        >
          It knows nothing about your company yet, and it can't read a history that predates it.
          A few minutes of conversation is what everything else builds on.
        </OwedCard>
      )}

      {/* Quick 006: one card per open KPI-alert or critical-insight case the owner has not acknowledged. */}
      {owed.alertCases.map((c) => (
        <OwedCard
          key={`alert${c.id}`}
          index={++i}
          kicker={alertKicker(c)}
          title={c.title.replace(/^KPI alert: /, '')}
          actions={
            <>
              <Link className="btn btn-owed btn-sm" to={`/cases/${c.id}`}>
                Open case
              </Link>
              <button
                className="btn btn-sm"
                disabled={act.pending}
                onClick={() => after(() => post(`/api/cases/${c.id}/seen`))}
              >
                Got it
              </button>
            </>
          }
        >
          {alertBody(c)}
        </OwedCard>
      ))}

      {owed.proposedActions.map((a) => (
        <OwedCard
          key={`act${a.id}`}
          index={++i}
          kicker={a.kind === 'record_extracted_data' ? 'Approve this data' : 'Approve this action'}
          title={actionTitle(a)}
          actions={
            <>
              <button
                className="btn btn-owed btn-sm"
                disabled={act.pending}
                onClick={() => after(() => post(`/api/actions/${a.id}/approve`))}
              >
                Approve
              </button>
              <button
                className="btn btn-sm"
                disabled={act.pending}
                onClick={() => after(() => post(`/api/actions/${a.id}/reject`))}
              >
                Reject
              </button>
              <Link className="btn btn-quiet btn-sm" to="/approvals">
                See everything
              </Link>
            </>
          }
        >
          {actionLine(a)}
        </OwedCard>
      ))}

      {owed.pendingChats.map((c) => (
        <OwedCard
          key={`chat${c.jid}`}
          index={++i}
          kicker="New chat"
          title={chatName(c)}
          actions={
            <>
              <button
                className="btn btn-owed btn-sm"
                disabled={act.pending}
                onClick={() => after(() => patch(`/api/chats/${encodeURIComponent(c.jid)}`, { status: 'active' }))}
              >
                Activate
              </button>
              <button
                className="btn btn-sm"
                disabled={act.pending}
                onClick={() => after(() => patch(`/api/chats/${encodeURIComponent(c.jid)}`, { status: 'ignored' }))}
              >
                Ignore
              </button>
            </>
          }
        >
          It's listening but staying silent here until you say it can act.
        </OwedCard>
      ))}

      {owed.ackCases.map((c) => (
        <OwedCard
          key={`ack${c.id}`}
          index={++i}
          kicker="Ready to close"
          title={c.title}
          actions={
            <>
              <button
                className="btn btn-owed btn-sm"
                disabled={act.pending}
                onClick={() => after(() => post(`/api/cases/${c.id}/ack`))}
              >
                Confirm it's done
              </button>
              <Link className="btn btn-sm" to={`/cases/${c.id}`}>
                Open case
              </Link>
            </>
          }
        >
          {c.summary || 'It thinks this is finished and wants you to confirm before closing.'}
        </OwedCard>
      ))}

      {owed.violatedCases.map((c) => (
        <OwedCard
          key={`vio${c.id}`}
          index={++i}
          kicker={`${plural(c.violated_exp, 'commitment')} missed`}
          title={c.title}
          actions={
            <Link className="btn btn-owed btn-sm" to={`/cases/${c.id}`}>
              Open case
            </Link>
          }
        >
          Chasing hasn't worked. Someone promised something and the date has passed.
        </OwedCard>
      ))}

      {owed.unmatched.map((u) => (
        <OwedCard
          key={`un${u.actor_raw}`}
          index={++i}
          kicker="Unknown sender"
          title={u.actor_raw}
          actions={
            <Link className="btn btn-owed btn-sm" to="/people">
              Add them
            </Link>
          }
        >
          {plural(u.event_count, 'message')} it can't attribute to anyone, so nothing it says can
          be chased or credited.
        </OwedCard>
      ))}
    </div>
  )
}

function chatName(c: Chat): string {
  if (c.subject) return c.subject
  return c.is_group ? 'A group with no name yet' : 'A new direct chat'
}

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
function pretty(group: string): string {
  return GROUP_NAMES[group] ?? group.replace(/_/g, ' ')
}

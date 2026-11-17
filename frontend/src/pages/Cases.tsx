import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { get, post } from '../lib/api'
import { ownsChat } from '../lib/ask'
import { BRANDS } from '../lib/brand'
import { ago, dateTime, isGroupJid, isoTitle, overdueBy, plural, titleCase, truncate } from '../lib/format'
import { useAction, useResource, useStored } from '../lib/hooks'
import { caseOwed, isUnseenAlert, useOwed } from '../lib/owed'
import { useProfile } from '../lib/profile'
import type { CaseDetail, CaseLinks, CaseOrigin, CaseStatus, CaseSummary, Expectation, TimelineEvent } from '../lib/types'
import { ActionDetail, actionTitle } from '../components/ActionPayload'
import { EvidenceDetails } from '../components/InsightEvidence'
import { evidenceOf } from '../lib/contracts'
import { useAsk } from '../components/AskPanel'
import { Async, ConfirmButton, Empty, PageHeader, Section, StatusPill, Tabs } from '../components/ui'
import { ChevronLeft } from '../components/icons'
import '../components/split.css'

/* A case is one thread of work the assistant is holding: who asked, what was
 * promised, what has happened since. Expectations are the part that matters —
 * they are the commitments it will chase on its own.
 *
 * Quick 006: a case can also come from what the system found in the data — a
 * critical KPI alert, a warning or critical insight, or the owner asking the agent in chat.
 * Those carry an origin, link back to where they came from, and keep a timeline
 * of notes (fires, the investigation, how it ended). Gold rows come only from
 * lib/owed.tsx (caseOwed). */

type Filter = '' | CaseStatus

const FILTERS: { value: Filter; label: string }[] = [
  { value: '', label: 'All' },
  { value: 'open', label: 'Open' },
  { value: 'pending_ack', label: 'Ready to close' },
  { value: 'closed', label: 'Closed' },
  { value: 'dismissed', label: 'Dismissed' },
]

const ORIGIN_LABEL: Record<CaseOrigin, string> = {
  watch: 'KPI alert',
  insight: 'Insight',
  exploration: 'Exploration notes',
  chat: 'From chat',
}

/** Cases opened from a data finding (they have no people or commitments to chase). */
export function isFindingCase(origin: string | null | undefined): boolean {
  return origin === 'watch' || origin === 'insight' || origin === 'chat'
}

export function originLabel(origin: string | null | undefined): string | null {
  return origin && origin in ORIGIN_LABEL ? ORIGIN_LABEL[origin as CaseOrigin] : null
}

const KIND_LABEL: Record<string, string> = {
  watch: 'Alert fired',
  investigation: 'Investigation',
  exploration: 'Insight',
  chat: 'From chat',
  closing: 'Closed',
  manual: 'Your note',
  triage: 'Note',
}

/** Who or what a timeline entry is: a person for a message, else what the note is. */
export function timelineLabel(t: TimelineEvent, brand = 'Second Brain'): string {
  if (t.provenance !== 'self') return t.actor || 'Unknown sender'
  return (t.kind && KIND_LABEL[t.kind]) || brand
}

/** Notes worth reading in full (an investigation) get more room than a chat line. */
/* Quick 006: a finding note keeps its query and sample in meta; the timeline shows the
 * words and folds the evidence away (the same block as the Insights page). */
export function noteEvidence(t: TimelineEvent): { text: string | null; sql: string | null; evidence: ReturnType<typeof evidenceOf> } | null {
  const m = t.meta
  if (!m || (t.kind !== 'exploration' && t.kind !== 'chat')) return null
  const sql = typeof m.evidence_sql === 'string' && m.evidence_sql.trim() ? m.evidence_sql : null
  const evidence = evidenceOf(m.evidence)
  if (!sql && !evidence) return null
  const cut = (t.content ?? '').indexOf('\n\nEvidence SQL:')
  return { text: cut >= 0 ? (t.content ?? '').slice(0, cut) : t.content, sql, evidence }
}

function contentCap(t: TimelineEvent): number {
  return t.kind === 'investigation' || t.kind === 'chat' || t.kind === 'exploration' ? 1500 : 400
}

export interface OriginLinksProps {
  origin: string | null | undefined
  links: CaseLinks
  watch: CaseDetail['watch']
  open: boolean
  onOpenChat?: (thread: string) => void
}

/* Where a finding case came from, with links back: the KPI (and how close the case
 * is to closing itself), the insight, the dashboard, the chat. */
export function OriginLinks({ origin, links, watch, open, onOpenChat }: OriginLinksProps) {
  const label = originLabel(origin)
  if (!label) return null
  const wid = watch?.id ?? links.watch_id
  const wtitle = watch?.title ?? links.watch_title
  const kpi = (watch?.kind ?? links.watch_kind) === 'kpi'
  const thread = links.thread_id
  return (
    <div className="stack-2" data-origin={origin ?? ''}>
      <div className="row wrap" style={{ gap: 'var(--s2)' }}>
        <span className="field-label" style={{ marginBottom: 0 }}>
          Came from
        </span>
        <span className="pill">{label}</span>
        {wid != null && (
          <Link to={kpi ? `/kpis/${wid}` : '/watches'}>
            {kpi ? 'KPI' : 'Watch'} #{wid}
            {wtitle ? `: ${wtitle}` : ''}
          </Link>
        )}
        {links.insight_key && <Link to="/insights">Insight {links.insight_key}</Link>}
        {links.dashboard_slug && (
          <Link to={`/dashboards/${encodeURIComponent(links.dashboard_slug)}`}>
            Dashboard {links.dashboard_slug}
          </Link>
        )}
        {origin === 'chat' && thread && onOpenChat && (
          <button className="btn btn-quiet btn-sm" onClick={() => onOpenChat(thread)}>
            Open the chat
          </button>
        )}
      </div>
      {origin === 'watch' && (
        <div className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>
          {!watch
            ? 'The watch was deleted.'
            : !open
              ? `The watch is ${watch.status === 'active' ? 'still checking' : watch.status}.`
              : watch.status !== 'active'
                ? `The watch is ${watch.status}; this case closes within a minute.`
                : watch.normal_streak > 0
                  ? `Back to normal for ${plural(watch.normal_streak, 'check')} of ${
                      watch.recovery_checks ?? 3
                    }; it closes itself at ${watch.recovery_checks ?? 3}.`
                  : `Still past its alert line. It closes itself after ${plural(
                      watch.recovery_checks ?? 3,
                      'normal check',
                    )} in a row.`}
        </div>
      )}
    </div>
  )
}

export default function Cases() {
  const { id } = useParams()
  const navigate = useNavigate()
  const owed = useOwed()
  const [filter, setFilter] = useStored<Filter>('cases.filter', '')

  const list = useResource(
    () => get<CaseSummary[]>(`/api/cases${filter ? `?status=${filter}` : ''}`),
    { interval: 15_000, deps: [filter] },
  )

  return (
    <>
      <PageHeader
        title="Cases"
        lede="Work the assistant is holding: KPI alerts, important findings, things you asked it to track, and the commitments people made in chats."
        actions={<Tabs value={filter} onChange={setFilter} options={FILTERS} />}
      />

      <div className={`split${id ? ' has-detail' : ''}`}>
        <div className="panel split-list">
          <Async
            data={list.data}
            error={list.error}
            loading={list.loading}
            reload={list.reload}
            empty={
              <Empty title="No cases yet">
                Cases open by themselves when a KPI alert fires, when exploration finds something
                worth a look (a warning or critical finding), when you ask the agent to open one, or when someone asks for or promises
                something in a chat the assistant is active in.
              </Empty>
            }
          >
            {(cases) => cases.map((c) => <CaseRow key={c.id} c={c} selected={String(c.id) === id} onOpen={() => navigate(`/cases/${c.id}`)} />)}
          </Async>
        </div>

        <div className="split-detail">
          <Link className="btn btn-quiet btn-sm back-link" to="/cases">
            <ChevronLeft size={13} /> All cases
          </Link>
          {id ? (
            <Detail
              id={Number(id)}
              onChanged={() => {
                void list.reload()
                void owed.reload()
              }}
            />
          ) : (
            <div className="panel">
              <Empty title="Pick a case">
                Its timeline, where it came from and anything it is tracking appear here.
              </Empty>
            </div>
          )}
        </div>
      </div>
    </>
  )
}

export function CaseRow({ c, selected, onOpen }: { c: CaseSummary; selected: boolean; onOpen: () => void }) {
  const origin = originLabel(c.origin)
  return (
    <button
      className={`list-row${selected ? ' is-selected' : ''}${caseOwed(c) ? ' is-owed' : ''}`}
      onClick={onOpen}
      data-case-origin={c.origin ?? 'message'}
    >
      <div className="list-title">{c.title}</div>
      <div className="list-meta">
        <StatusPill status={c.status} />
        {origin && <span className="pill">{origin}</span>}
        {isUnseenAlert(c) && <span style={{ color: 'var(--owed-hi)' }}>not seen yet</span>}
        {(c.fire_count ?? 0) > 1 && <span>fired {c.fire_count} times</span>}
        {c.violated_exp > 0 && <span style={{ color: 'var(--owed-hi)' }}>{c.violated_exp} missed</span>}
        {c.open_exp > 0 && <span>{c.open_exp} open</span>}
        {c.satisfied_exp > 0 && <span>{c.satisfied_exp} met</span>}
        <span className="dimmer">{ago(c.created_at)}</span>
      </div>
    </button>
  )
}

function Detail({ id, onChanged }: { id: number; onChanged: () => void }) {
  const detail = useResource(() => get<CaseDetail>(`/api/cases/${id}`), {
    interval: 20_000,
    deps: [id],
  })
  const act = useAction()
  const ask = useAsk()
  const { profile } = useProfile()
  const brand = BRANDS[profile].name
  const [note, setNote] = useState('')

  const after = async (fn: () => Promise<unknown>) => {
    const ok = await act.run(fn)
    if (ok) {
      await detail.reload()
      onChanged()
    }
    return ok
  }

  /* Manual case note — the one way a human tells the system a case fact.
   * Goes through the same note write path as triage, so it shows up on the
   * timeline (and in the assistant's context) like any other note. */
  const addNote = async () => {
    const text = note.trim()
    if (!text) return
    const ok = await after(() => post(`/api/cases/${id}/note`, { text }))
    if (ok) setNote('')
  }

  return (
    <Async data={detail.data} error={detail.error} loading={detail.loading} reload={detail.reload}>
      {(d) => {
        const c = d.case
        const finding = isFindingCase(c.origin)
        const unseen = isUnseenAlert(c)
        const met = d.expectations.filter((e) => e.status === 'satisfied').length
        const links: CaseLinks = (c.links && typeof c.links === 'object' ? c.links : {}) as CaseLinks
        const isOpen = c.status === 'open' || c.status === 'pending_ack'
        return (
          <div className="stack">
            <section className="panel">
              <div className="panel-head">
                <div className="grow">
                  <div className="row wrap" style={{ gap: 'var(--s2)' }}>
                    <h2 className="panel-title">{c.title}</h2>
                    <StatusPill status={c.status} />
                  </div>
                  <div className="dimmer" style={{ fontSize: 'var(--fs-tiny)', marginTop: 3 }}>
                    <span className="mono">#{c.id}</span> · opened{' '}
                    <span title={isoTitle(c.created_at)}>{ago(c.created_at)}</span>
                    {c.closed_at && ` · closed ${ago(c.closed_at)}`}
                  </div>
                </div>
                <div className="btn-row">
                  {unseen && (
                    <button className="btn btn-owed btn-sm" disabled={act.pending} onClick={() => after(() => post(`/api/cases/${id}/seen`))}>
                      Got it
                    </button>
                  )}
                  {c.status === 'pending_ack' && (
                    <button className="btn btn-owed btn-sm" disabled={act.pending} onClick={() => after(() => post(`/api/cases/${id}/ack`))}>
                      Confirm it's done
                    </button>
                  )}
                  {c.status === 'open' && (
                    <button className="btn btn-primary btn-sm" disabled={act.pending} onClick={() => after(() => post(`/api/cases/${id}/close`))}>
                      Close it
                    </button>
                  )}
                  {isOpen && (
                    <ConfirmButton onConfirm={() => void after(() => post(`/api/cases/${id}/dismiss`))} confirmLabel="Dismiss it?">
                      Dismiss
                    </ConfirmButton>
                  )}
                </div>
              </div>

              <div className="panel-body stack-3">
                {act.error && <div className="banner banner-broken">{act.error}</div>}
                {c.summary && <p className="dim">{c.summary}</p>}
                {c.close_reason && (
                  <p className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>
                    Closed because: {c.close_reason}
                  </p>
                )}

                <OriginLinks
                  origin={c.origin}
                  links={links}
                  watch={d.watch ?? null}
                  open={isOpen}
                  onOpenChat={(t) => {
                    if (ownsChat(t)) ask.openChat(t)
                    else ask.open()
                  }}
                />

                {(c.aliases?.length ?? 0) > 0 && (
                  <div className="row wrap" style={{ gap: 'var(--s2)' }}>
                    <span className="field-label" style={{ marginBottom: 0 }}>
                      Also known as
                    </span>
                    {c.aliases!.map((a) => (
                      <span className="pill" key={a}>
                        {a}
                      </span>
                    ))}
                  </div>
                )}

                {(!finding || d.participants.length > 0) && (
                  <div className="row wrap" style={{ gap: 'var(--s2)' }}>
                    <span className="field-label" style={{ marginBottom: 0 }}>
                      People
                    </span>
                    {d.participants.length ? (
                      d.participants.map((p) => (
                        <span className="pill" key={p.id}>
                          {p.name}
                          {p.role ? ` · ${p.role}` : ''}
                        </span>
                      ))
                    ) : (
                      <span className="dimmer">
                        Nobody registered —{' '}
                        <Link to="/people">add them</Link> so it knows who to chase.
                      </span>
                    )}
                  </div>
                )}

                {/* Where the case started, by name (Quick 024): never the raw
                    number or JID. A finding case's thread is shown by OriginLinks
                    instead. */}
                {c.origin_chat_jid && !finding && (
                  <div className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }} data-started-in>
                    Started in {startedIn(c)}
                  </div>
                )}
              </div>
            </section>

            {(!finding || d.expectations.length > 0) && (
              <Section
                title="Commitments"
                meta={d.expectations.length ? `${met} of ${d.expectations.length} met` : undefined}
                flush
              >
                {d.expectations.length === 0 ? (
                  <Empty title="No commitments on this case">
                    Nothing here was phrased as a promise, so there is nothing to chase.
                  </Empty>
                ) : (
                  <div>
                    {d.expectations.map((e) => (
                      <ExpectationRow
                        key={e.id}
                        exp={e}
                        brand={brand}
                        onDismiss={() => void after(() => post(`/api/expectations/${e.id}/dismiss`))}
                        pending={act.pending}
                      />
                    ))}
                  </div>
                )}
              </Section>
            )}

            {(d.facts?.length ?? 0) > 0 && (
              <Section title="Facts" flush>
                <div>
                  {d.facts.map((f) => (
                    <div
                      key={f.field}
                      className="row wrap"
                      style={{ gap: 'var(--s2)', padding: 'var(--s3) var(--s4)', borderBottom: '1px solid var(--line)' }}
                    >
                      <span className="mono dim">{f.field}</span>
                      <span className="grow" style={{ color: 'var(--t1)' }}>
                        {f.value}
                        {f.src && (
                          <span className="dimmer" style={{ fontSize: 'var(--fs-tiny)', marginLeft: 'var(--s2)' }}>
                            per {f.src}
                          </span>
                        )}
                      </span>
                      <span className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }} title={isoTitle(f.observed_at)}>
                        {dateTime(f.observed_at)}
                      </span>
                    </div>
                  ))}
                </div>
              </Section>
            )}

            {d.actions.length > 0 && (
              <Section title="What it did about it" flush>
                <div>
                  {d.actions.map((a) => (
                    <div key={a.id} style={{ padding: 'var(--s4)', borderBottom: '1px solid var(--line)' }}>
                      <div className="row wrap" style={{ gap: 'var(--s2)' }}>
                        <strong>{actionTitle(a)}</strong>
                        <StatusPill status={a.status} />
                        {a.tier && <span className="pill">{titleCase(a.tier)}</span>}
                        <span className="dimmer grow" style={{ textAlign: 'right', fontSize: 'var(--fs-tiny)' }}>
                          {ago(a.executed_at || a.created_at)}
                        </span>
                      </div>
                      <div style={{ marginTop: 'var(--s2)' }}>
                        <ActionDetail action={a} />
                      </div>
                    </div>
                  ))}
                </div>
              </Section>
            )}

            <Section
              title={finding ? 'Notes' : 'Timeline'}
              meta={plural(d.timeline.length, finding ? 'note' : 'event')}
              flush
              actions={
                <div className="row" style={{ gap: 'var(--s2)' }}>
                  <input
                    className="input"
                    style={{ maxWidth: 220, padding: '2px 8px', fontSize: 'var(--fs-small)' }}
                    placeholder="Tell it something about this case"
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') void addNote()
                    }}
                  />
                  <button
                    className="btn btn-sm"
                    disabled={act.pending || !note.trim()}
                    onClick={() => void addNote()}
                  >
                    Add note
                  </button>
                </div>
              }
            >
              {d.timeline.length === 0 ? (
                <Empty title="No events linked to this case yet" />
              ) : (
                <div>
                  {d.timeline.map((t) => {
                    const ev = noteEvidence(t)
                    return (
                    <div key={t.id} style={{ padding: 'var(--s3) var(--s4)', borderBottom: '1px solid var(--line)' }} data-kind={t.kind ?? ''}>
                      <div className="row" style={{ gap: 'var(--s2)', fontSize: 'var(--fs-tiny)' }}>
                        <span className={`dot ${t.provenance === 'self' ? 'dot-signal' : ''}`} />
                        <strong style={{ color: 'var(--t1)' }}>{timelineLabel(t, brand)}</strong>
                        <span className="dimmer">{t.source}</span>
                        <span className="dimmer grow" style={{ textAlign: 'right' }} title={isoTitle(t.occurred_at)}>
                          {dateTime(t.occurred_at)}
                        </span>
                      </div>
                      <div className="dim" style={{ marginTop: 3, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
                        {truncate(ev ? ev.text : t.content, contentCap(t))}
                      </div>
                      {ev && (
                        <div style={{ marginTop: 'var(--s2)' }}>
                          <EvidenceDetails sql={ev.sql} evidence={ev.evidence} at={t.occurred_at} />
                        </div>
                      )}
                    </div>
                    )
                  })}
                </div>
              )}
            </Section>
          </div>
        )
      }}
    </Async>
  )
}

/** Quick 024: where a case started, as people say it. The API names the chat
 *  (origin_chat_name); a raw number or JID is never shown. */
export function startedIn(c: { origin_chat_jid?: string | null; origin_chat_name?: string | null }): string {
  const name = typeof c.origin_chat_name === 'string' ? c.origin_chat_name.trim() : ''
  if (name) return name
  return isGroupJid(c.origin_chat_jid) ? 'WhatsApp group' : 'WhatsApp chat'
}

export function ExpectationRow({
  exp,
  onDismiss,
  pending,
  brand = 'the assistant',
}: {
  exp: Expectation
  onDismiss: () => void
  pending: boolean
  /** Quick 024: who an assistant commitment is owed by (the product's name). */
  brand?: string
}) {
  const late = exp.status === 'violated' ? overdueBy(exp.due_at) : ''
  const assistantOwes = exp.owed_by === 'assistant'
  return (
    <div
      style={{
        padding: 'var(--s3) var(--s4)',
        borderBottom: '1px solid var(--line)',
        boxShadow: exp.status === 'violated' ? 'inset 2px 0 0 var(--owed)' : undefined,
      }}
    >
      <div className="row-top" style={{ gap: 'var(--s3)' }}>
        <div className="grow">
          <div style={{ color: 'var(--t1)' }}>{exp.description}</div>
          <div className="list-meta">
            <StatusPill status={exp.status} />
            <span>
              {exp.owner ? (
                exp.owner
              ) : assistantOwes ? (
                <span title="Asked of the assistant in the chat; its reply there meets this." data-owed-by="assistant">
                  {brand}
                </span>
              ) : (
                <span style={{ color: 'var(--owed-hi)' }}>nobody assigned — it can't chase this</span>
              )}
            </span>
            {exp.due_at && <span title={isoTitle(exp.due_at)}>due {dateTime(exp.due_at)}</span>}
            {late && <span style={{ color: 'var(--owed-hi)' }}>{late}</span>}
            {exp.reminder_count > 0 && <span>{plural(exp.reminder_count, 'reminder')} sent</span>}
          </div>
        </div>
        {(exp.status === 'open' || exp.status === 'violated') && (
          <button className="btn btn-quiet btn-sm" disabled={pending} onClick={onDismiss}>
            Stop chasing
          </button>
        )}
      </div>
    </div>
  )
}

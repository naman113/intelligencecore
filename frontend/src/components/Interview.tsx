import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { asObj, get, post } from '../lib/api'
import { useAction, useResource } from '../lib/hooks'
import type { OnboardingState, OnboardingTurn } from '../lib/types'
import { ConfirmButton, Empty, ErrorNote, Loading } from './ui'
import { Send } from './icons'
import './interview.css'

/* One interviewer, two subjects: the business (once per deployment) and each
 * group chat. The interview ends when the model emits a structured profile —
 * not when it says something that sounds final — so "finished" here is a fact
 * the backend reports, never a guess made in the UI.
 */

type Turn = { role: 'user' | 'assistant'; content: string }

/* Two things the GET route hands back that need care before rendering.
 *
 * The transcript arrives as a jsonb STRING — asyncpg does not decode it unless
 * a codec is registered, and the POST route only parses it because it has to
 * feed the model. Mapping over that string yields one bubble per character.
 *
 * And the route returns the newest session whatever its state, including an
 * abandoned one. Replaying an abandoned transcript would show a conversation
 * the model has no memory of: the next reply starts a fresh session, so the
 * interviewer would open with "what does the business do?" underneath ten turns
 * that appear to have already answered it. Only `active` is resumable. */
function turnsOf(raw: unknown): Turn[] {
  if (Array.isArray(raw)) return raw as Turn[]
  if (typeof raw === 'string') {
    try {
      const parsed = JSON.parse(raw)
      return Array.isArray(parsed) ? (parsed as Turn[]) : []
    } catch {
      return []
    }
  }
  return []
}

const RESUMABLE = new Set(['active', 'complete'])

export function Interview({
  kind,
  jid,
  subject,
  onComplete,
}: {
  kind: 'business' | 'chat'
  jid?: string
  subject?: string
  onComplete?: () => void
}) {
  const query = `/api/onboarding?kind=${kind}${jid ? `&jid=${encodeURIComponent(jid)}` : ''}`
  const state = useResource(() => get<OnboardingState>(query), { deps: [query] })

  const [turns, setTurns] = useState<Turn[] | null>(null)
  const [draft, setDraft] = useState('')
  const [done, setDone] = useState(false)
  const [profile, setProfile] = useState<Record<string, unknown> | null>(null)
  const [replyError, setReplyError] = useState<string | null>(null)
  const act = useAction()
  const threadRef = useRef<HTMLDivElement>(null)

  // Server state is the source of truth on first load; local state takes over
  // once the operator starts typing, so a poll can't drop a turn mid-sentence.
  useEffect(() => {
    if (state.data && turns === null) {
      const resumable = RESUMABLE.has(state.data.status)
      const p = resumable ? asObj<Record<string, unknown>>(state.data.profile) : null
      setTurns(resumable ? turnsOf(state.data.transcript) : [])
      setDone(state.data.status === 'complete')
      // asObj yields {} for null, which would render an empty "here's what it
      // learned" panel on a session that learned nothing.
      setProfile(p && Object.keys(p).length ? p : null)
    }
  }, [state.data, turns])

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight, behavior: 'smooth' })
  }, [turns])

  async function send(message?: string) {
    setReplyError(null)
    if (message) setTurns((t) => [...(t ?? []), { role: 'user', content: message }])
    setDraft('')

    await act.run(async () => {
      const res = await post<OnboardingTurn>('/api/onboarding', {
        kind,
        ...(jid ? { jid } : {}),
        ...(message ? { message } : {}),
      })
      // The route answers 200 with an `error` field when the model call itself
      // fails — a missing key, a provider outage — so it has to be read here.
      if (res.error) {
        setReplyError(res.error)
        return res
      }
      if (res.transcript) setTurns(turnsOf(res.transcript))
      else if (res.reply) setTurns((t) => [...(t ?? []), { role: 'assistant', content: res.reply! }])
      if (res.done) {
        setDone(true)
        setProfile(res.profile ?? null)
        onComplete?.()
      }
      return res
    })
  }

  /* The escape hatch for a model that says "I'll save what we have" without
   * actually emitting the profile: the backend forces the tool call, so this
   * always ends in a saved-and-applied profile or a visible error. */
  async function finalize() {
    setReplyError(null)
    await act.run(async () => {
      const res = await post<OnboardingTurn>('/api/onboarding/finalize', {
        kind,
        ...(jid ? { jid } : {}),
      })
      if (res.error) {
        setReplyError(res.error)
        return res
      }
      if (res.transcript) setTurns(turnsOf(res.transcript))
      setDone(true)
      setProfile(res.profile ?? null)
      onComplete?.()
      return res
    })
  }

  async function reset() {
    await act.run(() => post('/api/onboarding/reset', { kind, ...(jid ? { jid } : {}) }))
    setTurns([])
    setDone(false)
    setProfile(null)
    await state.reload()
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (draft.trim() && !act.pending) void send(draft.trim())
    }
  }

  if (state.error && !state.data) return <ErrorNote message={state.error} onRetry={state.reload} />
  if (turns === null) return <Loading label="Opening the interview" />

  const empty = turns.length === 0

  return (
    <div className="iv">
      {empty && !act.pending ? (
        <Empty
          title={kind === 'business' ? 'It knows nothing about you yet' : `About ${subject || 'this chat'}`}
          action={
            <button className="btn btn-owed" onClick={() => void send()}>
              Start the interview
            </button>
          }
        >
          {kind === 'business'
            ? 'It will ask about your company, who does what, and what actually matters day to day. Answer in your own words — it follows up on what you say.'
            : 'It will ask what this chat is for, who matters in it and what it should watch for. It stays silent here until you have answered.'}
        </Empty>
      ) : (
        <>
          <div className="iv-thread" ref={threadRef}>
            {turns.map((t, i) => (
              <div key={i} className={`iv-turn is-${t.role}`}>
                <div>
                  <div className="iv-who" style={{ textAlign: t.role === 'user' ? 'right' : 'left' }}>
                    {t.role === 'user' ? 'You' : 'Second Brain'}
                  </div>
                  <div className="iv-bubble">{t.content}</div>
                </div>
              </div>
            ))}
            {act.pending && (
              <div className="iv-turn is-assistant">
                <div className="row dimmer">
                  <span className="spin" /> thinking…
                </div>
              </div>
            )}
          </div>

          {replyError && (
            <div style={{ padding: '0 var(--s5) var(--s3)' }}>
              <ErrorNote message={replyError} />
            </div>
          )}

          {done ? (
            <div style={{ padding: 'var(--s4) var(--s5)', borderTop: '1px solid var(--line)' }}>
              <div className="row" style={{ color: 'var(--settled-hi)', marginBottom: 'var(--s2)' }}>
                <span className="dot dot-settled" />
                <strong>Interview complete</strong>
              </div>
              <p className="dim" style={{ marginBottom: 'var(--s3)' }}>
                What it learned is now in memory, and every agent reads from there. You can add
                more at any time — it picks up where this left off and updates what it saved.
              </p>
              {profile && (
                <details>
                  <summary style={{ cursor: 'pointer', color: 'var(--signal-hi)', fontSize: 'var(--fs-small)' }}>
                    See what it wrote down
                  </summary>
                  <pre className="code" style={{ marginTop: 'var(--s2)' }}>
                    {JSON.stringify(profile, null, 2)}
                  </pre>
                </details>
              )}
              <div className="btn-row" style={{ marginTop: 'var(--s3)' }}>
                <button className="btn btn-primary btn-sm" onClick={() => setDone(false)}>
                  Add more
                </button>
                <ConfirmButton
                  className="btn btn-sm"
                  confirmLabel="Start over?"
                  onConfirm={() => void reset()}
                >
                  Do it again
                </ConfirmButton>
              </div>
            </div>
          ) : (
            <>
              <div className="iv-compose">
                <textarea
                  className="textarea grow"
                  placeholder="Answer in your own words…"
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={onKeyDown}
                  disabled={act.pending}
                />
                <button
                  className="btn btn-primary"
                  disabled={act.pending || !draft.trim()}
                  onClick={() => void send(draft.trim())}
                >
                  <Send size={14} /> Send
                </button>
              </div>
              <div className="iv-hint">
                Enter sends · Shift+Enter starts a new line ·{' '}
                <ConfirmButton
                  className="btn btn-quiet btn-sm"
                  confirmLabel="Save and apply what it has so far?"
                  onConfirm={() => void finalize()}
                >
                  Save to memory now
                </ConfirmButton>
                {' · '}
                <ConfirmButton
                  className="btn btn-quiet btn-sm"
                  confirmLabel="Discard it?"
                  onConfirm={() => void reset()}
                >
                  Start over
                </ConfirmButton>
              </div>
            </>
          )}
        </>
      )}
      {act.error && (
        <div style={{ padding: '0 var(--s5) var(--s4)' }}>
          <ErrorNote message={act.error} />
        </div>
      )}
    </div>
  )
}

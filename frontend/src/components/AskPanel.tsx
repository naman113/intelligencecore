import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAction, useResource } from '../lib/hooks'
import { useProfile } from '../lib/profile'
import { ago, bytes, isoTitle } from '../lib/format'
import { download } from '../lib/api'
import {
  createChat,
  deleteChat,
  elapsed,
  failureText,
  fetchChats,
  fetchThread,
  fileTypeLabel,
  getOperator,
  getStoredChat,
  isActive,
  linkify,
  mainChat,
  markChatRead,
  postAsk,
  readyCount,
  renameChat,
  replyingTo,
  retryRun,
  runSeconds,
  setOperator,
  storeChat,
  whatsappNote,
  type AskAttachment,
  type AskChat,
  type AskRun,
  type AskThread,
} from '../lib/ask'
import { ConfirmButton } from './ui'
import { Check, Doc, Download, Picture, Sheet } from './icons'
import './ask.css'

/* The in-app channel to the agent. The panel is the system channel: blue and
 * neutral tokens only, never gold. Agent text is rendered as React text nodes
 * (never innerHTML); only http(s) URLs become links.
 *
 * Quick 003: several chats, and the panel no longer blocks the app. It docks
 * beside the page (no scrim), keeps polling while any answer runs, and the Ask
 * buttons say when an answer is ready in a chat the owner is not looking at. */

const NEW_CHAT = 'new'

interface AskApi {
  isOpen: boolean
  open: (prefill?: string) => void
  openChat: (chat: string) => void
  close: () => void
  send: (message: string) => Promise<boolean>
  /** An answer is being worked on in some chat. */
  working: boolean
  /** Chats with an answer the owner has not seen (outside the open chat). */
  ready: number
}

const AskContext = createContext<AskApi>({
  isOpen: false,
  open: () => {},
  openChat: () => {},
  close: () => {},
  send: async () => false,
  working: false,
  ready: 0,
})

export const useAsk = () => useContext(AskContext)

/* ── presentational pieces (rendered in tests without a router) ────────── */

/** Calm working indicator plus what the agent is doing and for how long. */
export function LiveStatus({ step, seconds }: { step: string | null | undefined; seconds: number }) {
  const text = step || 'Working on it…'
  return (
    <div className="ask-live" role="status" aria-live="polite">
      <span className="ask-pulse" aria-hidden="true">
        <i />
        <i />
        <i />
      </span>
      <span className="ask-step" key={text}>
        {text}
      </span>
      <span className="ask-elapsed" title="Time this answer has been running">
        {elapsed(seconds)}
      </span>
    </div>
  )
}

/** Ticks the elapsed time locally between polls, from the server's clock. */
function TickingStatus({ run, now }: { run: AskRun | null | undefined; now?: string }) {
  const base = runSeconds(run, now)
  const [extra, setExtra] = useState(0)
  useEffect(() => {
    setExtra(0)
    const started = Date.now()
    const t = window.setInterval(() => setExtra((Date.now() - started) / 1000), 1000)
    return () => window.clearInterval(t)
  }, [base, run?.id])
  return <LiveStatus step={run?.step} seconds={base + extra} />
}

/* Quick 007: a file the reply delivered. The download goes through the ops-key
 * fetch in lib/api (a plain link cannot carry the key); a file retention has
 * removed says so instead of offering a broken link. */
const FILE_ICONS: Record<string, typeof Doc> = {
  spreadsheet: Sheet,
  image: Picture,
}

export function FileCard({ file }: { file: AskAttachment }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const Icon = FILE_ICONS[file.kind] ?? Doc
  const available = file.available && !!file.url
  const wa = whatsappNote(file)
  const save = async () => {
    if (!file.url) return
    setBusy(true)
    setError(null)
    try {
      await download(file.url, file.name)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'The download failed.')
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className={`ask-file${available ? '' : ' is-gone'}`} data-kind={file.kind}>
      <span className="ask-file-icon" aria-hidden="true">
        <Icon size={18} />
      </span>
      <div className="ask-file-body">
        <div className="ask-file-name" title={file.name}>
          {file.name}
        </div>
        <div className="ask-file-meta">
          <span className="ask-file-type">{fileTypeLabel(file.name)}</span>
          {available ? (
            file.bytes != null && <span>{bytes(file.bytes)}</span>
          ) : (
            <span>No longer available</span>
          )}
        </div>
        {wa && (
          <div className="ask-file-wa">
            <Check size={12} /> {wa}
          </div>
        )}
        {error && (
          <div className="ask-file-error" role="alert">
            {error}
          </div>
        )}
      </div>
      {available && (
        <button
          type="button"
          className="btn btn-sm ask-file-get"
          disabled={busy}
          onClick={() => void save()}
          aria-label={`Download ${file.name}`}
        >
          <Download size={13} /> {busy ? 'Downloading…' : 'Download'}
        </button>
      )}
    </div>
  )
}

export function AskThreadView({
  thread,
  onRetry,
  retrying,
  onOpenApprovals,
  ticking,
}: {
  thread: AskThread | null
  onRetry?: (runId: number) => void
  retrying?: boolean
  onOpenApprovals?: () => void
  ticking?: boolean
}) {
  const box = useRef<HTMLDivElement>(null)
  const run = thread?.run ?? null
  const active = run ? isActive(run) : !!thread?.pending
  const failed = run ? run.status === 'failed' : thread?.last_run?.status === 'failed'
  const count = thread?.messages.length ?? 0
  // Keep the newest message and the working line in view as they arrive.
  useEffect(() => {
    box.current?.scrollTo({ top: box.current.scrollHeight })
  }, [thread?.thread_id, count, active, failed])
  if (!thread) return <div className="ask-thread" />
  const error = run ? run.error : thread.last_run?.error
  const failedId = run ? run.id : Number(thread.last_run?.id)
  const waiting = !active && !failed ? thread.waiting_approval ?? 0 : 0
  return (
    <div className="ask-thread" ref={box}>
      {thread.messages.length === 0 && !active && (
        <p className="ask-empty">Ask about your data, or ask for a dashboard, a KPI or a rule.</p>
      )}
      {thread.messages.map((m, i) => (
        <div key={m.id} className={`ask-msg ask-msg-${m.role}`}>
          {replyingTo(thread.messages, i) !== null && (
            <div className="ask-replying">Replying to: {replyingTo(thread.messages, i)}</div>
          )}
          <div className="ask-text">
            {linkify(m.content).map((p, i) =>
              typeof p === 'string' ? (
                <span key={i}>{p}</span>
              ) : (
                <a key={i} href={p.href} target="_blank" rel="noopener noreferrer">
                  {p.text}
                </a>
              ),
            )}
          </div>
          {m.role === 'assistant' && (m.attachments?.length ?? 0) > 0 && (
            <div className="ask-files">
              {m.attachments!.map((a) => (
                <FileCard key={`${a.run_id}/${a.name}`} file={a} />
              ))}
            </div>
          )}
        </div>
      ))}
      {active &&
        (ticking ? (
          <TickingStatus run={run} now={thread.now} />
        ) : (
          <LiveStatus step={run?.step} seconds={runSeconds(run, thread.now)} />
        ))}
      {active && run && (run.queued_behind ?? 0) > 0 && (
        <p className="ask-hint">
          {run.queued_behind} more message{run.queued_behind === 1 ? '' : 's'} in this chat after this one.
        </p>
      )}
      {failed && (
        <div className="banner banner-broken ask-failed" role="alert">
          <span className="grow">{failureText(error)}</span>
          {onRetry && Number.isFinite(failedId) && (
            <button className="btn btn-sm" disabled={retrying} onClick={() => onRetry(failedId)}>
              Retry
            </button>
          )}
        </div>
      )}
      {waiting > 0 && (
        <div className="ask-waiting">
          <span className="dot dot-signal" aria-hidden="true" />
          <span className="grow">
            Waiting for your approval: {waiting} item{waiting === 1 ? '' : 's'}
          </span>
          {onOpenApprovals && (
            <button className="btn btn-quiet btn-sm" onClick={onOpenApprovals}>
              Open Approvals
            </button>
          )}
        </div>
      )}
    </div>
  )
}

export function OperatorField({
  value,
  error,
  onChange,
  onCommit,
}: {
  value: string
  error: string | null
  onChange?: (v: string) => void
  onCommit?: () => void
}) {
  return (
    <div className="ask-operator">
      <label htmlFor="ask-operator">You are</label>
      <input
        id="ask-operator"
        type="text"
        value={value}
        maxLength={32}
        onChange={(e) => onChange?.(e.target.value)}
        onBlur={() => onCommit?.()}
        onKeyDown={(e) => {
          if (e.key === 'Enter') onCommit?.()
        }}
      />
      {error && <div className="ask-hint">{error}</div>}
    </div>
  )
}

/** Blue badges for the Ask buttons: a soft pulse while working, a count when ready. */
export function AskBadges({ working, ready }: { working: boolean; ready: number }) {
  return (
    <>
      {working && ready === 0 && <span className="ask-dot ask-dot-working" aria-hidden="true" />}
      {ready > 0 && <span className="ask-count">{ready}</span>}
    </>
  )
}

export function askButtonLabel(working: boolean, ready: number): string | undefined {
  if (ready > 0) return `Ask (${ready} answer${ready === 1 ? '' : 's'} ready)`
  if (working) return 'Ask (an answer is being worked on)'
  return undefined
}

function chatState(c: AskChat): { cls: string; meta: string } {
  if (isActive(c.run)) return { cls: 'ask-dot ask-dot-working', meta: c.run?.step || 'Working' }
  if (c.unread) return { cls: 'dot dot-signal', meta: 'Answer ready' }
  if (c.run?.status === 'failed') return { cls: 'dot dot-broken', meta: 'Failed' }
  return { cls: 'dot', meta: ago(c.last_activity_at) }
}

export function ChatList({
  chats,
  active,
  busy,
  onPick,
  onRename,
  onDelete,
}: {
  chats: AskChat[]
  active: string
  busy?: boolean
  onPick?: (id: string) => void
  onRename?: (id: string, title: string) => void
  onDelete?: (id: string) => void
}) {
  const [editing, setEditing] = useState<string | null>(null)
  const [name, setName] = useState('')
  return (
    <ul className="ask-chats" aria-label="Chats">
      {chats.map((c) => {
        const st = chatState(c)
        const running = isActive(c.run)
        return (
          <li key={c.id} className={`ask-chat${c.id === active ? ' is-active' : ''}`}>
            {editing === c.id ? (
              <form
                className="ask-chat-edit"
                onSubmit={(e) => {
                  e.preventDefault()
                  onRename?.(c.id, name.trim())
                  setEditing(null)
                }}
              >
                <input
                  className="input"
                  value={name}
                  maxLength={80}
                  autoFocus
                  aria-label="Chat name"
                  onChange={(e) => setName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Escape') {
                      e.stopPropagation()
                      setEditing(null)
                    }
                  }}
                />
                <button type="submit" className="btn btn-sm" disabled={busy}>
                  Save
                </button>
              </form>
            ) : (
              <>
                <button className="ask-chat-pick" onClick={() => onPick?.(c.id)} aria-current={c.id === active}>
                  <span className={st.cls} aria-hidden="true" />
                  <span className="ask-chat-title truncate">{c.title}</span>
                  {c.is_main && <span className="pill">main</span>}
                  <span className="ask-chat-meta truncate" title={isoTitle(c.last_activity_at)}>
                    {st.meta}
                  </span>
                </button>
                <div className="ask-chat-tools">
                  <button
                    className="btn btn-quiet btn-sm"
                    disabled={busy}
                    onClick={() => {
                      setName(c.named ? c.title : '')
                      setEditing(c.id)
                    }}
                  >
                    Rename
                  </button>
                  {!c.is_main && (
                    <ConfirmButton
                      className="btn btn-quiet btn-sm"
                      confirmLabel="Delete?"
                      disabled={busy || running}
                      onConfirm={() => onDelete?.(c.id)}
                    >
                      Delete
                    </ConfirmButton>
                  )}
                </div>
              </>
            )}
          </li>
        )
      })}
    </ul>
  )
}

const OPERATOR_HINT = 'Use lowercase letters, - or _ (up to 32)'

/* ── provider ──────────────────────────────────────────────────────────── */

export function AskProvider({ children }: { children: ReactNode }) {
  const { profile, resolved } = useProfile()
  const enabled = resolved && profile === 'intelligence'
  const navigate = useNavigate()
  const [isOpen, setOpen] = useState(false)
  const [showList, setShowList] = useState(false)
  const [draft, setDraft] = useState('')
  const [operator, setOp] = useState(getOperator())
  const [operatorField, setOperatorField] = useState(getOperator())
  const [operatorError, setOperatorError] = useState<string | null>(null)
  const [chat, setChatState] = useState<string>(getStoredChat())
  const action = useAction()
  const listAction = useAction()
  const panelRef = useRef<HTMLElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const marked = useRef<Record<string, number>>({})

  const setChat = useCallback((id: string) => {
    setChatState(id)
    if (id !== NEW_CHAT) storeChat(id)
  }, [])

  // The chat list is polled whenever the profile has Ask, open or not: it is
  // what tells the Ask button that an answer finished in the background.
  const [listFast, setListFast] = useState(false)
  const chats = useResource(fetchChats, {
    enabled,
    interval: listFast ? 4000 : 20000,
    deps: [enabled, operator, listFast],
  })
  const chatList = useMemo(() => chats.data?.chats ?? [], [chats.data])
  const working = chatList.some((c) => isActive(c.run))
  useEffect(() => setListFast(working), [working])
  const current = chatList.find((c) => c.id === chat) ?? null

  const realChat = chat !== NEW_CHAT ? chat : null
  const [threadFast, setThreadFast] = useState(false)
  const thread = useResource(() => fetchThread(50, realChat ?? undefined), {
    enabled: enabled && isOpen && !!realChat,
    interval: threadFast ? 2000 : 15000,
    deps: [enabled, isOpen, realChat, threadFast],
  })
  const view = realChat && thread.data?.thread_id === realChat ? thread.data : null
  // A stored chat that was deleted elsewhere (the server says 404) falls back to
  // the main chat. Not judged from the list: it lags a chat created a moment ago.
  useEffect(() => {
    if (realChat && thread.error && /deleted|not found/i.test(thread.error)) setChat(mainChat(operator))
  }, [thread.error, realChat, operator, setChat])
  const threadActive = !!view && (view.run ? isActive(view.run) : view.pending)
  useEffect(() => setThreadFast(threadActive), [threadActive])
  // When an answer lands, refresh the list too (title, read state, badges).
  const wasActive = useRef(false)
  useEffect(() => {
    if (wasActive.current && !threadActive) void chats.reload()
    wasActive.current = threadActive
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadActive])

  // The owner has seen the open chat up to its newest answer.
  useEffect(() => {
    if (!isOpen || showList || !view || !realChat) return
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return
    const last = view.messages.reduce(
      (n, m) => (m.role === 'assistant' && Number(m.id) > n ? Number(m.id) : n),
      0,
    )
    if (!last || (marked.current[realChat] ?? 0) >= last) return
    marked.current[realChat] = last
    void markChatRead(realChat, last)
      .then(() => chats.reload())
      .catch(() => {
        delete marked.current[realChat]
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen, showList, view, realChat])

  const open = useCallback(
    (prefill?: string) => {
      if (prefill !== undefined) setDraft(prefill)
      setShowList(false)
      setOpen(true)
      void chats.reload() // fresh read state and titles, not the last background poll
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [chats.reload],
  )
  const openChat = useCallback(
    (id: string) => {
      setChat(id)
      setShowList(false)
      setOpen(true)
      void chats.reload()
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [setChat, chats.reload],
  )
  const close = useCallback(() => setOpen(false), [])

  const post = useCallback(
    async (message: string, target: string) => {
      let id = target
      if (id === NEW_CHAT) id = (await createChat()).id
      await postAsk(message, id)
      setChat(id)
      return id
    },
    [setChat],
  )

  // Composer: always the chat on screen (a busy chat queues the message).
  const sendHere = useCallback(
    async (message: string) => {
      const ok = await action.run(() => post(message, chat))
      if (ok) {
        setDraft('')
        void thread.reload()
        void chats.reload()
      }
      return ok
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [action.run, post, chat, thread.reload, chats.reload],
  )

  // Page create buttons: the open chat, or a new chat when the open chat is
  // still answering, so a click never waits behind a long run.
  const send = useCallback(
    async (message: string) => {
      const busy = current ? isActive(current.run) : threadActive
      const ok = await action.run(() => post(message, busy ? NEW_CHAT : chat))
      if (ok) {
        setShowList(false)
        setOpen(true)
        void thread.reload()
        void chats.reload()
      }
      return ok
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [action.run, post, chat, current, threadActive, thread.reload, chats.reload],
  )

  const retry = useCallback(
    async (runId: number) => {
      if (!realChat) return
      const ok = await action.run(() => retryRun(realChat, runId))
      if (ok) {
        void thread.reload()
        void chats.reload()
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [action.run, realChat, thread.reload, chats.reload],
  )

  const newChat = () => {
    setChat(NEW_CHAT)
    setShowList(false)
    setDraft('')
    window.setTimeout(() => inputRef.current?.focus(), 0)
  }

  const rename = (id: string, title: string) =>
    void listAction.run(async () => {
      await renameChat(id, title)
      await chats.reload()
    })
  const remove = (id: string) =>
    void listAction.run(async () => {
      await deleteChat(id)
      if (id === chat) setChat(mainChat(operator))
      await chats.reload()
    })

  useEffect(() => {
    if (!isOpen) return
    const onKey = (e: KeyboardEvent) => {
      // Non-modal: Escape closes only when focus is inside the panel.
      if (e.key === 'Escape' && panelRef.current?.contains(document.activeElement)) setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [isOpen])

  const commitOperator = () => {
    if (setOperator(operatorField)) {
      const next = getOperator()
      setOperatorField(next)
      setOperatorError(null)
      setOp(next)
      setChat(getStoredChat())
    } else {
      setOperatorError(OPERATOR_HINT)
      setOperatorField(operator)
    }
  }

  const ready = readyCount(chatList, isOpen && !showList ? realChat : null)
  const api = useMemo(
    () => ({ isOpen, open, openChat, close, send, working, ready }),
    [isOpen, open, openChat, close, send, working, ready],
  )
  const title =
    chat === NEW_CHAT ? 'New chat' : current?.title ?? (chat === mainChat(operator) ? 'Main chat' : 'New chat')
  const othersReady = readyCount(chatList, realChat)

  return (
    <AskContext.Provider value={api}>
      {children}
      {isOpen && enabled && (
        <aside className="ask-sheet" aria-label="Ask the agent" ref={panelRef}>
          <header className="ask-head">
            <div className="ask-bar">
              <button
                className={`btn btn-quiet btn-sm${showList ? ' is-on' : ''}`}
                onClick={() => {
                  if (!showList) void chats.reload()
                  setShowList((v) => !v)
                }}
                aria-expanded={showList}
              >
                Chats
                <AskBadges working={false} ready={othersReady} />
              </button>
              <div className="grow ask-titles">
                <div className="eyebrow">Ask the agent</div>
                <h2 className="truncate" title={title}>
                  {showList ? 'Your chats' : title}
                </h2>
              </div>
              <button className="btn btn-quiet btn-sm" onClick={newChat}>
                New chat
              </button>
              <button className="btn btn-quiet btn-sm" onClick={close} aria-label="Close ask panel">
                Close
              </button>
            </div>
            {!showList && (
              <p className="ask-note">
                Requests go to the agent. Descriptions, rules and dashboard drafts wait for your approval; a KPI watch you ask for can start right away. You can keep using the app while it works.
              </p>
            )}
          </header>
          {showList ? (
            <div className="ask-listview">
              <ChatList
                chats={chatList}
                active={chat}
                busy={listAction.pending}
                onPick={(id) => {
                  setChat(id)
                  setShowList(false)
                }}
                onRename={rename}
                onDelete={remove}
              />
              {listAction.error && <div className="banner banner-broken">{listAction.error}</div>}
              <OperatorField
                value={operatorField}
                error={operatorError}
                onChange={setOperatorField}
                onCommit={commitOperator}
              />
            </div>
          ) : (
            <>
              {realChat && !view ? (
                <div className="ask-thread">
                  <div className="ask-loading">
                    <span className="spin" aria-hidden="true" /> Loading the chat…
                  </div>
                </div>
              ) : (
                <AskThreadView
                  thread={view ?? { thread_id: NEW_CHAT, messages: [], pending: false, last_run: null }}
                  ticking
                  retrying={action.pending}
                  onRetry={(id) => void retry(id)}
                  onOpenApprovals={() => {
                    navigate('/approvals')
                    if (window.matchMedia('(max-width: 1099px)').matches) setOpen(false)
                  }}
                />
              )}
              {action.error && <div className="banner banner-broken ask-error">{action.error}</div>}
              <form
                className="ask-composer"
                onSubmit={(e) => {
                  e.preventDefault()
                  if (draft.trim()) void sendHere(draft.trim())
                }}
              >
                <textarea
                  ref={inputRef}
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && draft.trim()) {
                      e.preventDefault()
                      void sendHere(draft.trim())
                    }
                  }}
                  rows={3}
                  placeholder={threadActive ? 'Message the agent (it will answer after the current one)' : 'Message the agent'}
                  aria-label="Message"
                />
                <div className="ask-actions">
                  <span className="ask-hint grow">Ctrl+Enter to send</span>
                  <button type="submit" className="btn btn-sm" disabled={action.pending || !draft.trim()}>
                    Send
                  </button>
                </div>
              </form>
            </>
          )}
        </aside>
      )}
    </AskContext.Provider>
  )
}

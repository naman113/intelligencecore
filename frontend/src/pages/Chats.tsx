import { useState } from 'react'
import { get, patch, post } from '../lib/api'
import { ago, isoTitle, phone as fmtPhone } from '../lib/format'
import { useAction, useResource, useStored } from '../lib/hooks'
import { useOwed } from '../lib/owed'
import type { Chat, ChatMember } from '../lib/types'
import { Interview as InterviewPanel } from '../components/Interview'
import { Async, Empty, Modal, PageHeader, StatusPill, Tabs } from '../components/ui'

/* Being added to a group is not permission to act in it. Anyone can add a
 * number to any chat, so a new one is recorded, listened to, and answered by
 * nobody until a person here says otherwise. This page is that gate — and,
 * because the same act supplies the context, the place each group gets
 * explained. */

type Filter = 'pending' | 'active' | 'ignored' | 'all'

export default function Chats() {
  const owed = useOwed()
  const [filter, setFilter] = useStored<Filter>('chats.filter', 'pending')
  const [interviewing, setInterviewing] = useState<Chat | null>(null)

  const { data, error, loading, reload } = useResource(
    () => get<{ chats: Chat[] }>('/api/chats'),
    { interval: 15_000 },
  )

  const all = data?.chats ?? []
  const counts = {
    pending: all.filter((c) => c.status === 'pending').length,
    active: all.filter((c) => c.status === 'active').length,
    ignored: all.filter((c) => c.status === 'ignored').length,
    all: all.length,
  }
  const shown = filter === 'all' ? all : all.filter((c) => c.status === filter)

  const refresh = () => {
    void reload()
    void owed.reload()
  }

  return (
    <>
      <PageHeader
        title="Chats"
        lede="Every conversation the assistant can see. It reads all of them; it only speaks in the ones you activate."
        actions={
          <Tabs
            value={filter}
            onChange={setFilter}
            options={[
              { value: 'pending', label: 'Waiting', count: counts.pending },
              { value: 'active', label: 'Active', count: counts.active },
              { value: 'ignored', label: 'Ignored', count: counts.ignored },
              { value: 'all', label: 'All', count: counts.all },
            ]}
          />
        }
      />

      <Async
        data={shown}
        error={error}
        loading={loading}
        reload={reload}
        empty={
          <div className="panel">
            {filter === 'pending' ? (
              <Empty title="No chats waiting">
                When someone adds the assistant to a group, it appears here first — silent — until
                you say what the group is for.
              </Empty>
            ) : (
              <Empty title={`No ${filter} chats`} />
            )}
          </div>
        }
      >
        {(chats) => (
          <div className="stack-3">
            {chats.map((c) => (
              <ChatCard
                key={c.jid}
                chat={c}
                onChanged={refresh}
                onInterview={() => setInterviewing(c)}
              />
            ))}
          </div>
        )}
      </Async>

      <Modal
        open={Boolean(interviewing)}
        onClose={() => {
          setInterviewing(null)
          refresh()
        }}
        title={interviewing?.subject || 'About this chat'}
        width={760}
      >
        {interviewing && (
          <InterviewPanel
            kind="chat"
            jid={interviewing.jid}
            subject={interviewing.subject || undefined}
            onComplete={refresh}
          />
        )}
      </Modal>
    </>
  )
}

/* One roster row. Registered members show their name; unregistered ones get a
 * quick-add — type a name, and the number is registered as a person right here
 * instead of a copy-paste round-trip through the People tab. The backend
 * re-resolves past events on create, so history attributes itself too. */
function MemberRow({ member: m, onRegistered }: { member: ChatMember; onRegistered: () => void }) {
  const act = useAction()
  const [adding, setAdding] = useState(false)
  const [name, setName] = useState('')

  async function add() {
    const trimmed = name.trim()
    if (trimmed.length < 2) return
    const ok = await act.run(() =>
      post('/api/people', { name: trimmed, whatsapp: m.number }),
    )
    if (ok) {
      setAdding(false)
      setName('')
      onRegistered()
    }
  }

  return (
    <div className="row wrap" style={{ gap: 'var(--s2)' }}>
      {m.name ? (
        <span>{m.name}</span>
      ) : (
        <span className="mono dimmer">
          {fmtPhone(m.number)}{' '}
          <span style={{ fontFamily: 'var(--body)' }}>(not registered)</span>
        </span>
      )}
      {m.is_admin && <span className="pill">admin</span>}
      {!m.name && !adding && (
        <button className="btn btn-quiet btn-sm" onClick={() => setAdding(true)}>
          Add person
        </button>
      )}
      {adding && (
        <>
          <input
            className="input"
            style={{ maxWidth: 180, padding: '2px 8px', fontSize: 'var(--fs-small)' }}
            placeholder="Their name"
            value={name}
            autoFocus
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void add()
              if (e.key === 'Escape') setAdding(false)
            }}
          />
          <button className="btn btn-primary btn-sm" disabled={act.pending || name.trim().length < 2} onClick={() => void add()}>
            {act.pending ? 'Adding…' : 'Add'}
          </button>
          <button className="btn btn-quiet btn-sm" onClick={() => { setAdding(false); setName('') }}>
            Cancel
          </button>
        </>
      )}
      {act.error && <span className="dimmer" style={{ color: 'var(--broken-hi, #c00)', fontSize: 'var(--fs-tiny)' }}>{act.error}</span>}
    </div>
  )
}

function ChatCard({
  chat,
  onChanged,
  onInterview,
}: {
  chat: Chat
  onChanged: () => void
  onInterview: () => void
}) {
  const act = useAction()
  const [editing, setEditing] = useState(false)
  const [purpose, setPurpose] = useState(chat.purpose || '')
  const [showMembers, setShowMembers] = useState(false)
  const [members, setMembers] = useState<ChatMember[] | null>(null)
  const membersAct = useAction()

  /* The roster is fetched lazily on first expand — the chat list polls every
   * 15s and must stay cheap; counts ride on the list, people come on demand. */
  async function loadMembers() {
    await membersAct.run(async () => {
      const res = await get<{ members: ChatMember[] }>(
        `/api/chats/${encodeURIComponent(chat.jid)}/members`,
      )
      setMembers(res.members)
      return res
    })
  }

  function toggleMembers() {
    const next = !showMembers
    setShowMembers(next)
    if (next && members === null) void loadMembers()
  }

  async function syncMembers() {
    await membersAct.run(() =>
      post(`/api/chats/${encodeURIComponent(chat.jid)}/sync-members`),
    )
    await loadMembers()
    onChanged()
  }

  /* The PATCH route treats a missing status as 'pending', so the current status
   * always travels with a purpose edit — otherwise saving a description would
   * quietly silence an active group. */
  const save = async (status: Chat['status'], nextPurpose?: string) => {
    const ok = await act.run(() =>
      patch(`/api/chats/${encodeURIComponent(chat.jid)}`, {
        status,
        ...(nextPurpose !== undefined ? { purpose: nextPurpose } : {}),
      }),
    )
    if (ok) {
      setEditing(false)
      onChanged()
    }
  }

  const pending = chat.status === 'pending'

  return (
    <section className={`panel${pending ? '' : ''}`} style={pending ? { boxShadow: 'inset 2px 0 0 var(--owed)' } : undefined}>
      <div className="panel-head">
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="row wrap" style={{ gap: 'var(--s2)' }}>
            <h2 className="panel-title truncate">
              {chat.subject || (chat.is_group ? 'Unnamed group' : 'Direct chat')}
            </h2>
            <StatusPill status={chat.status} label={chat.status === 'pending' ? 'waiting on you' : chat.status} />
            <span className="pill">{chat.is_group ? 'group' : 'direct'}</span>
            {chat.is_group && (
              <button
                className="pill"
                style={{ cursor: 'pointer' }}
                title="Who is in this group (synced from WhatsApp)"
                onClick={toggleMembers}
              >
                {chat.member_count
                  ? `${chat.member_count} members${chat.known_count ? ` · ${chat.known_count} known` : ''}`
                  : 'members?'}
              </button>
            )}
            {chat.awaiting_purpose && <span className="pill pill-signal">asked on WhatsApp</span>}
          </div>
          <div className="dimmer mono truncate" style={{ fontSize: 'var(--fs-micro)', marginTop: 3 }}>
            {chat.jid}
          </div>
        </div>
        <div className="btn-row">
          {chat.status !== 'active' && (
            <button className="btn btn-owed btn-sm" disabled={act.pending} onClick={() => void save('active')}>
              Activate
            </button>
          )}
          {chat.status === 'active' && (
            <button className="btn btn-sm" disabled={act.pending} onClick={() => void save('ignored')}>
              Silence it
            </button>
          )}
          {chat.status === 'pending' && (
            <button className="btn btn-sm" disabled={act.pending} onClick={() => void save('ignored')}>
              Ignore
            </button>
          )}
          {chat.status === 'ignored' && (
            <span className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>silenced</span>
          )}
        </div>
      </div>

      <div className="panel-body stack-3">
        {act.error && <div className="banner banner-broken">{act.error}</div>}

        {showMembers && (
          <div className="stack-2" style={{ borderBottom: '1px solid var(--line)', paddingBottom: 'var(--s3)' }}>
            {membersAct.error && <div className="banner banner-broken">{membersAct.error}</div>}
            {members === null ? (
              <p className="dimmer">Loading members…</p>
            ) : members.length === 0 ? (
              <p className="dimmer">Members not synced yet.</p>
            ) : (
              <div className="stack-2">
                {members.map((m) => (
                  <MemberRow key={m.number} member={m} onRegistered={() => { void loadMembers(); onChanged() }} />
                ))}
              </div>
            )}
            <div className="row">
              <button className="btn btn-quiet btn-sm" disabled={membersAct.pending} onClick={() => void syncMembers()}>
                {membersAct.pending ? 'Refreshing…' : 'Refresh from WhatsApp'}
              </button>
              {chat.members_synced_at && (
                <span className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }} title={isoTitle(chat.members_synced_at)}>
                  synced {ago(chat.members_synced_at)}
                </span>
              )}
            </div>
          </div>
        )}

        {editing ? (
          <div className="stack-2">
            <label className="field" style={{ display: 'block', marginBottom: 0 }}>
              <span className="field-label">What this chat is for</span>
              <textarea
                className="textarea"
                style={{ fontFamily: 'var(--body)', fontSize: 'var(--fs-small)', minHeight: 72 }}
                value={purpose}
                onChange={(e) => setPurpose(e.target.value)}
                placeholder="Site B daily operations — material requests, machine breakdowns, and the day's dispatch plan."
              />
            </label>
            <div className="btn-row">
              <button className="btn btn-primary btn-sm" disabled={act.pending} onClick={() => void save(chat.status, purpose)}>
                Save
              </button>
              <button className="btn btn-quiet btn-sm" onClick={() => { setEditing(false); setPurpose(chat.purpose || '') }}>
                Cancel
              </button>
            </div>
          </div>
        ) : chat.purpose ? (
          <p className="dim">{chat.purpose}</p>
        ) : (
          <p className="dimmer">
            {pending
              ? "It doesn't know what this chat is for yet. Say what it's about, and it can tell a material request from small talk."
              : 'No description yet.'}
          </p>
        )}

        <div className="row wrap" style={{ gap: 'var(--s2)' }}>
          {!editing && (
            <button className="btn btn-sm" onClick={() => setEditing(true)}>
              {chat.purpose ? 'Edit description' : 'Describe it'}
            </button>
          )}
          <button className="btn btn-sm" onClick={onInterview}>
            Talk it through
          </button>
          <span className="grow" />
          <span className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }} title={isoTitle(chat.last_seen_at)}>
            last message {ago(chat.last_seen_at)}
          </span>
        </div>
      </div>
    </section>
  )
}

import { useState } from 'react'
import { get, patch, post } from '../lib/api'
import { phone as fmtPhone, plural, truncate } from '../lib/format'
import { useAction, useResource } from '../lib/hooks'
import { useOwed } from '../lib/owed'
import type { Person, UnmatchedSender } from '../lib/types'
import { Async, Empty, Field, FormAction, Modal, PageHeader, Section } from '../components/ui'
import { Star } from '../components/icons'

/* Identity is exact-match and human-managed: a normalized phone, a lowercased
 * email, or a case-insensitive alias. Nothing is guessed and nothing is created
 * automatically, which is why unmatched senders queue up here instead of
 * quietly becoming half-known people. */

interface Draft {
  name: string
  aliases: string
  email: string
  whatsapp: string
  whatsappAlt: string
  role: string
}

const BLANK: Draft = { name: '', aliases: '', email: '', whatsapp: '', whatsappAlt: '', role: '' }

const splitNumbers = (s: string) => s.split(',').map((n) => n.trim()).filter(Boolean)

export default function People() {
  const owed = useOwed()
  const [draft, setDraft] = useState<Draft>(BLANK)
  const [editing, setEditing] = useState<Person | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const act = useAction()

  const people = useResource(() => get<Person[]>('/api/people'), { interval: 30_000 })
  const unmatched = useResource(() => get<UnmatchedSender[]>('/api/people/unmatched'), {
    interval: 30_000,
  })

  const refresh = () => {
    void people.reload()
    void unmatched.reload()
    void owed.reload()
  }

  async function create() {
    if (draft.name.trim().length < 2) {
      setNote('A name is required.')
      return
    }
    setNote(null)
    const ok = await act.run(async () => {
      const res = await post<{ created: number; reresolved_events: number }>('/api/people', {
        name: draft.name.trim(),
        aliases: draft.aliases.split(',').map((s) => s.trim()).filter(Boolean),
        email: draft.email.trim() || null,
        whatsapp: draft.whatsapp.trim() || null,
        whatsapp_alt: splitNumbers(draft.whatsappAlt),
        role: draft.role.trim() || null,
      })
      setDraft(BLANK)
      if (res.reresolved_events > 0) {
        setNote(`Added — and ${plural(res.reresolved_events, 'past message')} now attributed to them.`)
      } else {
        setNote('Added.')
      }
      return res
    })
    if (ok) refresh()
  }

  return (
    <>
      <PageHeader
        title="People"
        lede="Who the assistant knows. It matches senders exactly — a number, an email or an alias — and will not guess."
      />

      <div className="stack">
        {unmatched.data && unmatched.data.length > 0 && (
          <Section
            title="Senders it can't place"
            meta={`${unmatched.data.length} waiting`}
            actions={
              <button
                className="btn btn-sm"
                disabled={act.pending}
                onClick={() => void act.run(() => post('/api/people/reresolve')).then(refresh)}
              >
                Re-check all
              </button>
            }
            flush
          >
            <div className="table-scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th>Sender</th>
                    <th>Seen in</th>
                    <th>Messages</th>
                    <th>Last thing they said</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {unmatched.data.map((u) => (
                    <tr key={u.actor_raw}>
                      <td className="mono">{u.actor_raw}</td>
                      <td className="dim">{(u.sources || []).join(', ')}</td>
                      <td className="mono">{u.event_count}</td>
                      <td className="dim">{truncate(u.last_content, 80)}</td>
                      <td>
                        <button
                          className="btn btn-owed btn-sm"
                          onClick={() => {
                            const isEmail = (u.sources || [])[0] === 'email' || u.actor_raw.includes('@')
                            setDraft({
                              ...BLANK,
                              email: isEmail ? u.actor_raw : '',
                              whatsapp: isEmail ? '' : u.actor_raw,
                            })
                            document.getElementById('person-name')?.focus()
                            document
                              .getElementById('add-person')
                              ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
                          }}
                        >
                          Add them
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="dimmer" style={{ fontSize: 'var(--fs-tiny)', padding: 'var(--s3) var(--s4)' }}>
              Until someone here is a registered person, nothing they promise can be chased and
              nothing they ask can be credited to them.
            </p>
          </Section>
        )}

        <div id="add-person">
          <Section title="Add someone">
            <div className="form-grid">
              <Field label="Name">
                <input
                  id="person-name"
                  className="input"
                  value={draft.name}
                  onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                  placeholder="Priya Nair"
                />
              </Field>
              <Field label="Role">
                <input
                  className="input"
                  value={draft.role}
                  onChange={(e) => setDraft({ ...draft, role: e.target.value })}
                  placeholder="Accounts"
                />
              </Field>
              <Field label="WhatsApp" hint="Digits only, with country code">
                <input
                  className="input mono"
                  value={draft.whatsapp}
                  onChange={(e) => setDraft({ ...draft, whatsapp: e.target.value })}
                  placeholder="919876543210"
                />
              </Field>
              <Field label="Other numbers" hint="Comma separated — replies go to the main one">
                <input
                  className="input mono"
                  value={draft.whatsappAlt}
                  onChange={(e) => setDraft({ ...draft, whatsappAlt: e.target.value })}
                  placeholder="919000000104"
                />
              </Field>
              <Field label="Email">
                <input
                  className="input"
                  value={draft.email}
                  onChange={(e) => setDraft({ ...draft, email: e.target.value })}
                  placeholder="priya@example.com"
                />
              </Field>
              <Field label="Also called" hint="Comma separated">
                <input
                  className="input"
                  value={draft.aliases}
                  onChange={(e) => setDraft({ ...draft, aliases: e.target.value })}
                  placeholder="Priya, PN"
                />
              </Field>
              <FormAction>
                <button className="btn btn-primary" disabled={act.pending} onClick={create}>
                  Add person
                </button>
              </FormAction>
            </div>
            {act.error && <div className="banner banner-broken" style={{ marginTop: 'var(--s3)' }}>{act.error}</div>}
            {note && !act.error && <div className="banner" style={{ marginTop: 'var(--s3)' }}>{note}</div>}
          </Section>
        </div>

        <Section title="Everyone it knows" meta={people.data ? `${people.data.length}` : undefined} flush>
          <Async
            data={people.data}
            error={people.error}
            loading={people.loading}
            reload={people.reload}
            empty={
              <Empty title="Nobody registered yet">
                Start with whoever the assistant reports to, then the people it will be chasing.
              </Empty>
            }
          >
            {(list) => (
              <div className="table-scroll">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Role</th>
                      <th>WhatsApp</th>
                      <th>Email</th>
                      <th>Also called</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((p) => (
                      <tr key={p.id} style={p.active ? undefined : { opacity: 0.5 }}>
                        <td>
                          <div className="row" style={{ gap: 'var(--s2)' }}>
                            <strong>{p.name}</strong>
                            {p.is_primary && (
                              <span className="pill pill-signal" title="The person it reports to">
                                <Star size={11} /> primary
                              </span>
                            )}
                            {!p.active && <span className="pill">inactive</span>}
                          </div>
                        </td>
                        <td className="dim">{p.role || '—'}</td>
                        <td className="mono dim">
                          {p.whatsapp ? fmtPhone(p.whatsapp) : '—'}
                          {(p.whatsapp_alt || []).length > 0 && (
                            <div className="dimmer" style={{ fontSize: 'var(--fs-micro)' }}>
                              also {(p.whatsapp_alt || []).map(fmtPhone).join(', ')}
                            </div>
                          )}
                        </td>
                        <td className="dim">{p.email || '—'}</td>
                        <td className="dim">{(p.aliases || []).join(', ') || '—'}</td>
                        <td>
                          <div className="btn-row">
                            <button className="btn btn-sm" onClick={() => setEditing(p)}>
                              Edit
                            </button>
                            {!p.is_primary && p.active && (
                              <button
                                className="btn btn-sm"
                                title="Onboarding questions, digests and escalations go to this person"
                                onClick={() =>
                                  void act.run(() => post(`/api/people/${p.id}/primary`)).then(refresh)
                                }
                              >
                                Make primary
                              </button>
                            )}
                            <button
                              className="btn btn-quiet btn-sm"
                              onClick={() =>
                                void act
                                  .run(() => patch(`/api/people/${p.id}`, { active: !p.active }))
                                  .then(refresh)
                              }
                            >
                              {p.active ? 'Deactivate' : 'Reactivate'}
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Async>
        </Section>
      </div>

      <EditPerson person={editing} onClose={() => setEditing(null)} onSaved={refresh} />
    </>
  )
}

function EditPerson({
  person,
  onClose,
  onSaved,
}: {
  person: Person | null
  onClose: () => void
  onSaved: () => void
}) {
  const act = useAction()
  const [draft, setDraft] = useState<Draft>(BLANK)
  const [loadedFor, setLoadedFor] = useState<number | null>(null)

  if (person && loadedFor !== person.id) {
    setLoadedFor(person.id)
    setDraft({
      name: person.name,
      aliases: (person.aliases || []).join(', '),
      email: person.email || '',
      whatsapp: person.whatsapp || '',
      whatsappAlt: (person.whatsapp_alt || []).join(', '),
      role: person.role || '',
    })
  }

  async function save() {
    if (!person) return
    const ok = await act.run(() =>
      patch(`/api/people/${person.id}`, {
        name: draft.name.trim(),
        aliases: draft.aliases.split(',').map((s) => s.trim()).filter(Boolean),
        email: draft.email.trim() || null,
        whatsapp: draft.whatsapp.trim() || null,
        whatsapp_alt: splitNumbers(draft.whatsappAlt),
        role: draft.role.trim() || null,
      }),
    )
    if (ok) {
      onSaved()
      onClose()
    }
  }

  return (
    <Modal open={Boolean(person)} onClose={onClose} title={person ? `Edit ${person.name}` : ''}>
      <div className="stack-3">
        <Field label="Name">
          <input className="input" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
        </Field>
        <Field label="Role">
          <input className="input" value={draft.role} onChange={(e) => setDraft({ ...draft, role: e.target.value })} />
        </Field>
        <Field label="WhatsApp">
          <input className="input mono" value={draft.whatsapp} onChange={(e) => setDraft({ ...draft, whatsapp: e.target.value })} />
        </Field>
        <Field label="Other numbers" hint="Comma separated — replies go to the main one">
          <input className="input mono" value={draft.whatsappAlt} onChange={(e) => setDraft({ ...draft, whatsappAlt: e.target.value })} />
        </Field>
        <Field label="Email">
          <input className="input" value={draft.email} onChange={(e) => setDraft({ ...draft, email: e.target.value })} />
        </Field>
        <Field label="Also called" hint="Comma separated">
          <input className="input" value={draft.aliases} onChange={(e) => setDraft({ ...draft, aliases: e.target.value })} />
        </Field>
        {act.error && <div className="banner banner-broken">{act.error}</div>}
        <div className="btn-row">
          <button className="btn btn-primary" disabled={act.pending} onClick={save}>
            Save changes
          </button>
          <button className="btn btn-quiet" onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </Modal>
  )
}

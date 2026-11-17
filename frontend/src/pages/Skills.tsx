import { useState } from 'react'
import { del, get, patch, post } from '../lib/api'
import { useAction, useResource } from '../lib/hooks'
import type { LibraryEntry, LibraryResponse, SkillRow } from '../lib/types'
import { loadSkillBody } from '../lib/skills'
import { Async, ConfirmButton, Empty, ErrorNote, PageHeader, Section, StatusPill } from '../components/ui'

/* Skills are optional procedures the assistant can pull in on demand.
 * Installed is not enabled: enabling is the consent step that puts a skill in
 * the supervisor's index, and nothing happens before it. The whole catalog is
 * ~150 metadata rows, so it loads once and filters client-side — browsing the
 * full vendor list is the point, not a search box over a void. */

function entryId(e: LibraryEntry): string {
  return typeof e.id === 'string' && e.id ? e.id : `${e.source ?? ''}/${e.name ?? ''}`
}

export default function Skills() {
  const { data, error, loading, reload } = useResource(() => get<SkillRow[]>('/api/skills'), {
    interval: 30_000,
  })
  // Static vendored catalog: fetched once, never polled.
  const lib = useResource(() => get<LibraryResponse>('/api/skills/library'))
  const act = useAction()
  const search = useAction()

  const [q, setQ] = useState('')
  const [preview, setPreview] = useState<{ id: string; body: string } | null>(null)

  // Create-a-skill area. Draft-with-AI fills the editor; nothing installs
  // until Create, and the server blocks off-mission skills either way.
  const draft = useAction()
  const [goal, setGoal] = useState('')
  const [cName, setCName] = useState('')
  const [cDesc, setCDesc] = useState('')
  const [cBody, setCBody] = useState('')
  const [cTriage, setCTriage] = useState(false)

  const installed = new Set((data ?? []).map((s) => s.name))
  const enabledCount = (data ?? []).filter((s) => s.enabled).length

  // Only the hand-curated shelf is shown: the rest of the vendored catalog
  // is written for terminal coding agents and would just be noise here.
  const catalog = (lib.data?.skills ?? []).filter((s) => s.relevant === true)
  const needle = q.trim().toLowerCase()
  const filtered = catalog.filter((s) => {
    if (!needle) return true
    return `${entryId(s)} ${s.description ?? ''} ${s.compat ?? ''}`.toLowerCase().includes(needle)
  })

  const runDraft = async () => {
    const ok = await draft.run(async () => {
      const d = await post<{ name: string; description: string; body: string }>(
        '/api/skills/draft',
        { goal },
      )
      setCName(d.name || '')
      setCDesc(d.description || '')
      setCBody(d.body || '')
    })
    return ok
  }

  const createSkill = async () => {
    const ok = await act.run(() =>
      post('/api/skills', {
        name: cName.trim(),
        description: cDesc.trim(),
        body: cBody,
        scope: cTriage ? 'triage' : 'agent',
      }),
    )
    if (ok) {
      setGoal('')
      setCName('')
      setCDesc('')
      setCBody('')
      setCTriage(false)
      void reload()
    }
  }

  const setEnabled = async (name: string, enabled: boolean) => {
    const ok = await act.run(() => patch(`/api/skills/${encodeURIComponent(name)}`, { enabled }))
    if (ok) void reload()
  }

  const remove = async (name: string) => {
    const ok = await act.run(() => del(`/api/skills/${encodeURIComponent(name)}`))
    if (ok) void reload()
  }

  const showPreview = async (entry: LibraryEntry) => {
    const id = entryId(entry)
    await search.run(async () => {
      // id is "source/name", which is exactly the library detail route's path.
      const detail = await get<LibraryEntry & { body?: string }>(`/api/skills/library/${id}`)
      setPreview({ id, body: detail.body || '(empty SKILL.md)' })
    })
  }

  const viewInstalled = async (name: string) => {
    await search.run(async () => {
      setPreview(await loadSkillBody(get, name))
    })
  }

  const install = async (entry: LibraryEntry) => {
    const ok = await act.run(() => post('/api/skills/install', { id: entryId(entry) }))
    if (ok) void reload()
  }

  return (
    <>
      <PageHeader
        title="Skills"
        lede="Optional procedures the assistant can consult mid-task. Installing does nothing by itself — a skill only reaches the assistant once you enable it here."
      />

      <div className="stack">
        {act.error && <div className="banner banner-broken">{act.error}</div>}

        <Section
          title="Installed"
          meta={data ? `${data.length} installed · ${enabledCount} enabled` : undefined}
          flush
        >
          <Async
            data={data}
            error={error}
            loading={loading}
            reload={reload}
            empty={
              <Empty title="No skills installed">
                Search the vendored library below, preview a skill, and install it. It stays off
                until you enable it.
              </Empty>
            }
          >
            {(list) => (
              <div className="table-scroll">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Source</th>
                      <th>License</th>
                      <th>Description</th>
                      <th>Status</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((s) => (
                      <tr key={s.id}>
                        <td>
                          <strong>{s.name}</strong>
                          {s.compat === 'coding-agent' && (
                            <span className="pill" style={{ marginLeft: 6 }} title="Written for coding agents; some steps may assume tools this assistant does not have.">
                              coding-agent
                            </span>
                          )}
                          {s.scope === 'triage' && (
                            <span className="pill" style={{ marginLeft: 6 }} title="Owner triage policy: applied when the assistant judges incoming messages, steering what becomes a case.">
                              triage policy
                            </span>
                          )}
                        </td>
                        <td className="dim nowrap">{s.source}</td>
                        <td className="dim nowrap">{s.license || '—'}</td>
                        <td className="dim" style={{ maxWidth: 340 }}>
                          {s.description || '—'}
                        </td>
                        <td>
                          <StatusPill
                            status={s.enabled ? 'active' : undefined}
                            label={s.enabled ? 'enabled' : 'off'}
                          />
                        </td>
                        <td>
                          <div className="btn-row">
                            <button
                              className="btn btn-quiet btn-sm"
                              disabled={act.pending}
                              onClick={() => void viewInstalled(s.name)}
                            >
                              View
                            </button>
                            {!s.enabled && (
                              <button
                                className="btn btn-owed btn-sm"
                                disabled={act.pending}
                                onClick={() => void setEnabled(s.name, true)}
                              >
                                Enable
                              </button>
                            )}
                            {s.enabled && (
                              <button
                                className="btn btn-sm"
                                disabled={act.pending}
                                onClick={() => void setEnabled(s.name, false)}
                              >
                                Disable
                              </button>
                            )}
                            <ConfirmButton
                              className="btn btn-quiet btn-sm"
                              confirmLabel="Delete it?"
                              disabled={act.pending}
                              onConfirm={() => void remove(s.name)}
                            >
                              Delete
                            </ConfirmButton>
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

        <Section
          title="Library"
          meta={
            lib.data?.library_available
              ? `${filtered.length} of ${catalog.length} — hand-picked for this assistant`
              : undefined
          }
        >
          <div className="row" style={{ flexWrap: 'wrap', gap: 8 }}>
            <input
              className="input"
              placeholder="Filter the curated skills…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              style={{ maxWidth: 320 }}
            />
          </div>

          {(search.error || lib.error) && (
            <div style={{ marginTop: 12 }}>
              <ErrorNote message={search.error || lib.error || ''} />
            </div>
          )}

          {lib.data && !lib.data.library_available && (
            <div style={{ marginTop: 12 }}>
              <Empty title="No library on this install">
                The vendored skills library (skills_library/) is not present here, so there is
                nothing to browse. Custom skills can still be added via the API.
              </Empty>
            </div>
          )}

          {lib.data?.library_available && (
            <div className="table-scroll" style={{ marginTop: 12 }}>
              {filtered.length === 0 ? (
                <Empty title="Nothing matched" />
              ) : (
                <table className="table">
                  <thead>
                    <tr>
                      <th>Skill</th>
                      <th>Source</th>
                      <th>License</th>
                      <th>Description</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {filtered.map((s) => {
                      const id = entryId(s)
                      const name = typeof s.name === 'string' ? s.name : id.split('/', 2)[1] || id
                      return (
                        <tr key={id}>
                          <td className="nowrap">
                            <strong>{name}</strong>
                            {s.compat === 'coding-agent' && (
                              <span className="pill" style={{ marginLeft: 6 }} title="Written for coding agents; some steps may assume tools this assistant does not have.">
                                coding-agent
                              </span>
                            )}
                          </td>
                          <td className="dim nowrap">{s.source || '—'}</td>
                          <td className="dim nowrap">{s.license || '—'}</td>
                          <td className="dim" style={{ maxWidth: 420 }}>
                            {s.description || '—'}
                          </td>
                          <td>
                            <div className="btn-row">
                              <button
                                className="btn btn-sm"
                                disabled={search.pending}
                                onClick={() => void showPreview(s)}
                              >
                                Preview
                              </button>
                              {installed.has(name) ? (
                                <span className="dimmer" style={{ fontSize: 'var(--fs-micro)' }}>
                                  installed
                                </span>
                              ) : (
                                <button
                                  className="btn btn-owed btn-sm"
                                  disabled={act.pending}
                                  onClick={() => void install(s)}
                                >
                                  Install
                                </button>
                              )}
                            </div>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </Section>

        <Section
          title="Create a skill"
          meta="describe a procedure → the assistant drafts a SKILL.md with skill-creator → you review and create"
        >
          <div className="stack" style={{ gap: 10 }}>
            <div className="row" style={{ flexWrap: 'wrap', gap: 8 }}>
              <input
                className="input"
                placeholder="e.g. How we chase overdue invoices: tone, escalation steps, when to loop in Naman…"
                value={goal}
                onChange={(e) => setGoal(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && goal.trim()) void runDraft()
                }}
                style={{ flex: 1, minWidth: 280 }}
              />
              <button
                className="btn"
                disabled={draft.pending || !goal.trim()}
                onClick={() => void runDraft()}
              >
                {draft.pending ? 'Drafting…' : 'Draft with AI'}
              </button>
            </div>
            {draft.error && <ErrorNote message={draft.error} />}

            <div className="row" style={{ flexWrap: 'wrap', gap: 8 }}>
              <input
                className="input"
                placeholder="slug (lowercase-with-dashes)"
                value={cName}
                onChange={(e) => setCName(e.target.value)}
                style={{ maxWidth: 260 }}
              />
              <input
                className="input"
                placeholder="one-line description: what it does and when to use it"
                value={cDesc}
                onChange={(e) => setCDesc(e.target.value)}
                style={{ flex: 1, minWidth: 280 }}
              />
            </div>
            <textarea
              className="input mono"
              placeholder={'---\nname: my-skill\ndescription: what it does and when to use it\n---\n\nThe procedure, step by step…'}
              value={cBody}
              onChange={(e) => setCBody(e.target.value)}
              rows={10}
              style={{ width: '100%', resize: 'vertical' }}
            />
            <div className="row" style={{ gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <button
                className="btn btn-owed"
                disabled={act.pending || !cName.trim() || !cBody.trim()}
                onClick={() => void createSkill()}
              >
                Create skill
              </button>
              <label className="dim" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                <input
                  type="checkbox"
                  checked={cTriage}
                  onChange={(e) => setCTriage(e.target.checked)}
                />
                triage policy — this rule steers what becomes a case (owner-authored only)
              </label>
              <span className="dimmer" style={{ fontSize: 'var(--fs-micro)' }}>
                Created skills start disabled — enable above when you're happy with the text.
                Off-mission skills (coding/devops) are blocked by the server.
              </span>
            </div>
          </div>
        </Section>

        {preview && (
          <Section
            title={`Preview — ${preview.id}`}
            actions={
              <button className="btn btn-quiet btn-sm" onClick={() => setPreview(null)}>
                Close
              </button>
            }
          >
            {/* React text interpolation escapes; nothing here is interpreted as markup. */}
            <pre
              className="mono"
              style={{ maxHeight: 420, overflow: 'auto', whiteSpace: 'pre-wrap', margin: 0 }}
            >
              {preview.body}
            </pre>
          </Section>
        )}
      </div>
    </>
  )
}

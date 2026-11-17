import { Fragment, useState } from 'react'
import { Link } from 'react-router-dom'
import { del, get, patch, post } from '../lib/api'
import { ago, isoTitle, plural } from '../lib/format'
import { useAction, useResource } from '../lib/hooks'
import { runsHref } from '../lib/runs'
import { eventLabel, type Watch, type WatchEvent } from '../lib/types'
import { Async, ConfirmButton, Empty, Field, FormAction, PageHeader, Section, StatusPill } from '../components/ui'
import { Pause, Play, Trash } from '../components/icons'

/* A watch is a standing question against the business database: check this
 * every so often, and tell someone when the answer changes. The condition is
 * written in plain language and compiled to SQL once — the compile state is
 * shown because an uncompiled watch looks alive but is checking nothing. */

interface KpiProposal {
  title: string
  condition: string
  kind: string
  cadence_seconds: number
}

export default function Watches() {
  const [owner, setOwner] = useState('')
  const [title, setTitle] = useState('')
  const [condition, setCondition] = useState('')
  const [minutes, setMinutes] = useState('15')
  const [oneShot, setOneShot] = useState(false)
  const [proposals, setProposals] = useState<KpiProposal[] | null>(null)
  const [expanded, setExpanded] = useState<number | null>(null)

  const act = useAction()
  const { data, error, loading, reload } = useResource(() => get<Watch[]>('/api/watches'), {
    interval: 20_000,
  })

  async function create(from?: KpiProposal) {
    const body = from
      ? { ...from, thread_id: owner.trim() }
      : {
          thread_id: owner.trim(),
          title: title.trim(),
          condition: condition.trim(),
          cadence_seconds: Math.max(5, parseInt(minutes || '15', 10)) * 60,
          one_shot: oneShot,
        }
    if (!body.thread_id) {
      act.clearError()
      return alert('Add the number to notify first.')
    }
    const ok = await act.run(() => post('/api/watches', body))
    if (ok) {
      if (!from) {
        setTitle('')
        setCondition('')
      }
      void reload()
    }
  }

  async function propose() {
    await act.run(async () => {
      const res = await post<{ proposals: KpiProposal[] }>('/api/watches/propose-kpi')
      setProposals(res.proposals || [])
      return res
    })
  }

  return (
    <>
      <PageHeader
        title="Watches"
        lede="Standing questions it checks on a timer and reports on when the answer changes."
        actions={
          <Link className="btn btn-sm" to="/system/runs?kind=watches">
            Watch checks
          </Link>
        }
      />

      <div className="stack">
        {act.error && <div className="banner banner-broken">{act.error}</div>}

        <Section
          title="Add a watch"
          actions={
            <button className="btn btn-sm" disabled={act.pending} onClick={propose}>
              Suggest some from the knowledge base
            </button>
          }
        >
          <div className="form-grid">
            <Field label="Tell" hint="WhatsApp number, digits only">
              <input
                className="input mono"
                value={owner}
                onChange={(e) => setOwner(e.target.value)}
                placeholder="919876543210"
              />
            </Field>
            <Field label="Call it">
              <input
                className="input"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="Diesel stock"
              />
            </Field>
            <Field label="Watch for" hint="Plain language — it compiles this to SQL once">
              <input
                className="input"
                value={condition}
                onChange={(e) => setCondition(e.target.value)}
                placeholder="Diesel stock at any site drops below two days of cover"
                style={{ minWidth: 260 }}
              />
            </Field>
            <Field label="Check every" hint="Minutes">
              <input
                className="input mono"
                type="number"
                min={5}
                value={minutes}
                onChange={(e) => setMinutes(e.target.value)}
              />
            </Field>
            <FormAction>
              <div className="stack-2">
                <label className="check">
                  <input type="checkbox" checked={oneShot} onChange={(e) => setOneShot(e.target.checked)} />
                  Stop after it fires once
                </label>
                <button className="btn btn-primary" disabled={act.pending} onClick={() => void create()}>
                  Add watch
                </button>
              </div>
            </FormAction>
          </div>
        </Section>

        {proposals && (
          <Section title="Suggested watches" meta={plural(proposals.length, 'suggestion')} flush>
            {proposals.length === 0 ? (
              <Empty title="Nothing to suggest">
                The knowledge base has no KPIs described in it yet.
              </Empty>
            ) : (
              proposals.map((p, i) => (
                <div key={i} className="list-row" style={{ cursor: 'default' }}>
                  <div className="row-top">
                    <div className="grow">
                      <div className="list-title">{p.title}</div>
                      <div className="dimmer" style={{ fontSize: 'var(--fs-tiny)', marginTop: 2 }}>
                        {p.condition}
                      </div>
                    </div>
                    <button className="btn btn-sm" disabled={act.pending} onClick={() => void create(p)}>
                      Add it
                    </button>
                  </div>
                </div>
              ))
            )}
          </Section>
        )}

        <Section title="Watching now" meta={data ? plural(data.length, 'watch', 'watches') : undefined} flush>
          <Async
            data={data}
            error={error}
            loading={loading}
            reload={reload}
            empty={
              <Empty title="Nothing being watched">
                Add one above, or just ask on WhatsApp — "tell me when that order is marked
                delivered" creates one.
              </Empty>
            }
          >
            {(list) => (
              <div className="table-scroll">
                <table className="table">
                  <thead>
                    <tr>
                      <th>What it watches</th>
                      <th>Every</th>
                      <th>Fired</th>
                      <th>Ready</th>
                      <th>Last check</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((w) => (
                      <Fragment key={w.id}>
                        <tr>
                          <td>
                            <button
                              className="btn btn-quiet btn-sm"
                              style={{ padding: 0, minHeight: 0 }}
                              onClick={() => setExpanded(expanded === w.id ? null : w.id)}
                            >
                              <strong>{w.title}</strong>
                            </button>
                            {w.one_shot && <span className="pill" style={{ marginLeft: 6 }}>once</span>}
                            <div className="dimmer" style={{ fontSize: 'var(--fs-tiny)', maxWidth: 420 }}>
                              {w.condition}
                            </div>
                            <WatchFacts watch={w} />
                          </td>
                          <td className="mono dim nowrap">{Math.round(w.cadence_seconds / 60)}m</td>
                          <td className="mono">{w.fire_count}</td>
                          <td>
                            {w.compiled_sql ? (
                              <StatusPill tone="settled" label="compiled" title={w.compiled_sql} />
                            ) : w.compile_error ? (
                              <StatusPill tone="broken" label="won't compile" title={w.compile_error} />
                            ) : (
                              <StatusPill tone="neutral" label="not yet" />
                            )}
                          </td>
                          <td className="dim nowrap" title={isoTitle(w.last_checked_at)}>
                            <Link to={runsHref(`watch:${w.id}`)} title="This watch's checks on Runs">
                              {w.last_checked_at ? ago(w.last_checked_at) : 'never'}
                            </Link>
                          </td>
                          <td>
                            <div className="btn-row">
                              {w.status === 'paused' ? (
                                <button
                                  className="btn btn-sm"
                                  title="Resume"
                                  onClick={() =>
                                    void act.run(() => patch(`/api/watches/${w.id}`, { status: 'active' })).then(reload)
                                  }
                                >
                                  <Play size={13} />
                                </button>
                              ) : (
                                <button
                                  className="btn btn-sm"
                                  title="Pause"
                                  onClick={() =>
                                    void act.run(() => patch(`/api/watches/${w.id}`, { status: 'paused' })).then(reload)
                                  }
                                >
                                  <Pause size={13} />
                                </button>
                              )}
                              <ConfirmButton
                                confirmLabel="Delete?"
                                onConfirm={() => void act.run(() => del(`/api/watches/${w.id}`)).then(reload)}
                              >
                                <Trash size={13} />
                              </ConfirmButton>
                            </div>
                          </td>
                        </tr>
                        {expanded === w.id && (
                          <tr>
                            <td colSpan={6} style={{ background: 'var(--ground)' }}>
                              <WatchDetail watch={w} />
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    ))}
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

/* Phase 17: measure name, data age and the stale chip. All neutral. */
export function WatchFacts({ watch }: { watch: Watch }) {
  const m = watch.measure_ref
  if (!m && !watch.stale_since && !watch.data_as_of) return null
  return (
    <div className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>
      {m && (
        <span style={{ marginRight: 8 }}>
          Measure: {m.measure}
          {m.range ? ` (${m.range})` : ''}
        </span>
      )}
      {watch.stale_since && (
        <span className="pill" style={{ marginRight: 8 }} title={isoTitle(watch.stale_since)}>
          Stale data
        </span>
      )}
      {watch.data_as_of && (
        <span title={isoTitle(watch.data_as_of)}>Data as of {new Date(watch.data_as_of).toLocaleString()}</span>
      )}
    </div>
  )
}

function WatchDetail({ watch }: { watch: Watch }) {
  const { data, error, loading } = useResource(
    () => get<WatchEvent[]>(`/api/watches/${watch.id}/events?limit=20`),
    { deps: [watch.id] },
  )

  return (
    <div className="stack-3" style={{ padding: 'var(--s3) 0' }}>
      {watch.compile_error && (
        <div className="banner banner-broken">
          It could not turn this into a query: {watch.compile_error}
        </div>
      )}
      {watch.compiled_sql && (
        <div>
          <span className="field-label">The query it runs</span>
          <pre className="code">{watch.compiled_sql}</pre>
        </div>
      )}
      <div>
        <span className="field-label">What it reported</span>
        {loading ? (
          <span className="dimmer">loading…</span>
        ) : error ? (
          <span className="dimmer">{error}</span>
        ) : !data || data.length === 0 ? (
          <span className="dimmer">Nothing yet — it has not seen anything worth reporting.</span>
        ) : (
          <div className="stack-2" style={{ marginTop: 'var(--s2)' }}>
            {data.map((e, i) => {
              const label = eventLabel(e.kind)
              return (
                <div key={i} className="row-top" style={{ gap: 'var(--s2)' }}>
                  <span
                    className={`dot ${label ? '' : e.escalated ? 'dot-owed' : 'dot-signal'}`}
                    style={{ marginTop: 6 }}
                  />
                  <div className="grow">
                    {label && <div className="dim">{label}</div>}
                    <div>{e.summary}</div>
                    <div className="dimmer" style={{ fontSize: 'var(--fs-micro)' }}>
                      {label ? '' : `${e.severity} · `}
                      {ago(e.created_at)}
                      {!label && e.escalated ? ' · escalated to a person' : ''}
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

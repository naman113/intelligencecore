import { useState } from 'react'
import { get, post, put } from '../lib/api'
import { useAction, useResource } from '../lib/hooks'
import type { DbSource, DbSourceProposal } from '../lib/types'
import { Async, Empty, Field, FormAction, PageHeader, Section } from '../components/ui'

/* Tables the assistant watches for new rows. Anything that appears becomes an
 * event, which is what lets it notice a new order without anyone telling it.
 * Every read goes through the same read-only, AST-validated path as the rest of
 * the system — this page only chooses what to look at. */

export default function DataSources() {
  const [table, setTable] = useState('')
  const [watermark, setWatermark] = useState('')
  const [note, setNote] = useState<string | null>(null)
  const [proposals, setProposals] = useState<DbSourceProposal[] | null>(null)
  const [seconds, setSeconds] = useState('')

  const act = useAction()
  const sources = useResource(() => get<DbSource[]>('/api/db-sources'), { interval: 30_000 })
  const poll = useResource(() => get<{ seconds: number }>('/api/db-poll-seconds'), {
    interval: 60_000,
  })

  const pollValue = seconds !== '' ? seconds : poll.data ? String(poll.data.seconds) : ''

  async function add() {
    if (!table.trim()) return
    setNote(null)
    await act.run(async () => {
      const res = await post<{ error?: string; columns?: string[]; added?: string; watermark_column?: string; note?: string }>(
        '/api/db-sources/add',
        { table: table.trim(), watermark_column: watermark.trim() || null },
      )
      if (res.error) {
        setNote(res.columns?.length ? `${res.error} — columns are: ${res.columns.join(', ')}` : res.error)
      } else {
        setNote(`Watching ${res.added}${res.watermark_column ? `, ordered by ${res.watermark_column}` : ''}.`)
        setTable('')
        setWatermark('')
        void sources.reload()
      }
      return res
    })
  }

  async function remove(index: number) {
    const next = (sources.data ?? []).filter((_, i) => i !== index)
    const ok = await act.run(() => put('/api/db-sources', { sources: next }))
    if (ok) void sources.reload()
  }

  async function addProposed(p: DbSourceProposal) {
    const next = [
      ...(sources.data ?? []),
      { table: p.table, watermark_column: p.watermark_column, label: p.label },
    ]
    const ok = await act.run(() => put('/api/db-sources', { sources: next }))
    if (ok) {
      setProposals((list) => (list ?? []).filter((x) => x.table !== p.table))
      void sources.reload()
    }
  }

  async function propose() {
    await act.run(async () => {
      const res = await post<{ proposals: DbSourceProposal[] }>('/api/db-sources/propose')
      setProposals(res.proposals || [])
      return res
    })
  }

  async function savePoll() {
    const n = parseInt(pollValue, 10)
    if (!n || n < 5) {
      setNote('Check no more often than every 5 seconds.')
      return
    }
    const ok = await act.run(() => put('/api/db-poll-seconds', { seconds: n }))
    if (ok) {
      setNote(`Checking every ${n} seconds. Applied straight away.`)
      setSeconds('')
      void poll.reload()
    }
  }

  return (
    <>
      <PageHeader
        title="Data sources"
        lede="Tables it keeps an eye on. A new row in one of these becomes something it can notice, triage and act on."
      />

      <div className="stack">
        {act.error && <div className="banner banner-broken">{act.error}</div>}
        {note && !act.error && <div className="banner">{note}</div>}

        <Section
          title="Add a table"
          actions={
            <button className="btn btn-sm" disabled={act.pending} onClick={propose}>
              Suggest tables
            </button>
          }
        >
          <div className="form-grid">
            <Field label="Table">
              <input
                className="input mono"
                value={table}
                onChange={(e) => setTable(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && void add()}
                placeholder="Machines"
              />
            </Field>
            <Field label="Order by" hint="Left blank, it works this out">
              <input
                className="input mono"
                value={watermark}
                onChange={(e) => setWatermark(e.target.value)}
                placeholder="created_at"
              />
            </Field>
            <FormAction>
              <button className="btn btn-primary" disabled={act.pending} onClick={add}>
                Watch it
              </button>
            </FormAction>
            <Field label="Check every" hint="Seconds — applies live">
              <div className="row" style={{ gap: 'var(--s2)' }}>
                <input
                  className="input mono"
                  type="number"
                  min={5}
                  value={pollValue}
                  onChange={(e) => setSeconds(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && void savePoll()}
                />
                <button className="btn btn-sm" disabled={act.pending} onClick={savePoll}>
                  Set
                </button>
              </div>
            </Field>
          </div>
        </Section>

        {proposals && (
          <Section title="Suggested tables" flush>
            {proposals.length === 0 ? (
              <Empty title="Nothing new to suggest">
                It read the knowledge base and everything it recognised is already watched.
              </Empty>
            ) : (
              proposals.map((p) => (
                <div key={p.table} className="list-row" style={{ cursor: 'default', opacity: p.recommended ? 1 : 0.6 }}>
                  <div className="row-top">
                    <div className="grow">
                      <div className="list-title mono">{p.table}</div>
                      <div className="dimmer" style={{ fontSize: 'var(--fs-tiny)', marginTop: 2 }}>
                        {p.recommended ? 'Business data' : 'Might be a system table'} · ordered by{' '}
                        <span className="mono">{p.watermark_column}</span> · {p.reason}
                      </div>
                    </div>
                    <button className="btn btn-sm" disabled={act.pending} onClick={() => void addProposed(p)}>
                      Watch it
                    </button>
                  </div>
                </div>
              ))
            )}
          </Section>
        )}

        <Section title="Watching now" flush>
          <Async
            data={sources.data}
            error={sources.error}
            loading={sources.loading}
            reload={sources.reload}
            empty={
              <Empty title="No tables watched">
                Nothing in your database reaches the assistant on its own yet. Add one above, or
                let it suggest some.
              </Empty>
            }
          >
            {(list) => (
              <div className="table-scroll">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Table</th>
                      <th>Ordered by</th>
                      <th>Called</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((s, i) => (
                      <tr key={s.table}>
                        <td className="mono">{s.table}</td>
                        <td className="mono dim">{s.watermark_column}</td>
                        <td className="dim">{s.label}</td>
                        <td>
                          <button className="btn btn-quiet btn-sm" disabled={act.pending} onClick={() => void remove(i)}>
                            Stop watching
                          </button>
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
    </>
  )
}

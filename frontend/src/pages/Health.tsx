import { useState } from 'react'
import { get, post } from '../lib/api'
import { plural } from '../lib/format'
import { useAction, useResource } from '../lib/hooks'
import type { Stats } from '../lib/types'
import { loadHealthOverview } from '../lib/health'
import { loadGraphStatus } from '../lib/graph'
import { HealthAlertsPanel } from '../components/HealthAlerts'
import { GraphStatusLine } from '../components/GraphStatus'
import { Async, Empty, Field, FormAction, KeyValue, Meter, PageHeader, Section } from '../components/ui'

/* Is it alive, is it busy, and can I make it do something on demand. The test
 * controls are the same hooks the acceptance scripts use — driving the real
 * pipeline, not a simulation of it. */

interface HealthState {
  ok: boolean
  degraded?: boolean
  reason?: string
}

export default function Health() {
  const stats = useResource(() => get<Stats>('/api/stats'), { interval: 5_000 })
  const health = useResource(() => get<HealthState>('/health'), { interval: 10_000 })
  const healthAlerts = useResource(() => loadHealthOverview(), { interval: 15_000 })
  const graph = useResource(loadGraphStatus, { interval: 30_000 })

  return (
    <>
      <PageHeader title="Health" lede="What the machine is doing, and a way to poke the pipeline." />

      <div className="stack">
        {health.data && !health.data.ok && (
          <div className="banner banner-broken">
            <div>
              <strong>Running degraded.</strong> {health.data.reason}
            </div>
          </div>
        )}

        {/* Phase 11: GOLIVE-05 loops and business database */}
        <HealthAlertsPanel data={healthAlerts.data} error={healthAlerts.error} />
        {/* end Phase 11 */}

        {/* Quick 009: the graph's real state, not "empty (or unreachable)" */}
        <Section title="Memory">
          <GraphStatusLine status={graph.data} error={graph.error} reload={graph.reload} />
        </Section>

        <Async data={stats.data} error={stats.error} loading={stats.loading} reload={stats.reload}>
          {(s) => (
            <div className="stack">
              <Section title="This machine">
                <div className="stack-3">
                  <div>
                    <div className="row" style={{ marginBottom: 4 }}>
                      <span className="field-label" style={{ marginBottom: 0 }}>
                        Processor
                      </span>
                      <span className="grow" />
                      <span className="mono dim">{s.system.cpu_percent ?? '—'}%</span>
                    </div>
                    <Meter value={s.system.cpu_percent ?? 0} max={100} />
                  </div>
                  <div>
                    <div className="row" style={{ marginBottom: 4 }}>
                      <span className="field-label" style={{ marginBottom: 0 }}>
                        Memory
                      </span>
                      <span className="grow" />
                      <span className="mono dim">
                        {s.system.memory_used_gb ?? '—'} / {s.system.memory_total_gb ?? '—'} GB
                      </span>
                    </div>
                    <Meter
                      value={s.system.memory_percent ?? 0}
                      max={100}
                      tone={(s.system.memory_percent ?? 0) > 90 ? 'owed' : undefined}
                    />
                  </div>
                  <KeyValue
                    rows={[
                      ['Gateway', `${s.process.memory_mb ?? '—'} MB · ${s.process.threads ?? '—'} threads`],
                      [
                        'Queue',
                        Object.keys(s.queue).length
                          ? Object.entries(s.queue)
                              .map(([k, v]) => `${v} ${k}`)
                              .join(' · ')
                          : 'empty',
                      ],
                    ]}
                  />
                  {s.system.error && (
                    <p className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>
                      Machine stats unavailable: {s.system.error}
                    </p>
                  )}
                </div>
              </Section>

              <Section title="Containers" flush>
                {s.containers.length === 0 ? (
                  <Empty title="No container stats">
                    The gateway can't reach the Docker daemon from where it runs.
                  </Empty>
                ) : (
                  <div className="table-scroll">
                    <table className="table">
                      <thead>
                        <tr>
                          <th>Container</th>
                          <th>Processor</th>
                          <th>Memory</th>
                        </tr>
                      </thead>
                      <tbody>
                        {s.containers.map((c) => (
                          <tr key={c.name}>
                            <td className="mono">{c.name}</td>
                            <td className="mono dim">{c.cpu}</td>
                            <td className="mono dim">{c.mem}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Section>
            </div>
          )}
        </Async>

        <TestBench />
      </div>
    </>
  )
}

function TestBench() {
  const act = useAction()
  const [phone, setPhone] = useState('test-local')
  const [message, setMessage] = useState('')
  const [event, setEvent] = useState('')
  const [note, setNote] = useState<string | null>(null)

  const run = async (label: string, fn: () => Promise<unknown>) => {
    setNote(null)
    const ok = await act.run(fn)
    if (ok) setNote(label)
  }

  return (
    <Section title="Try it without WhatsApp">
      <div className="stack-3">
        <p className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>
          These drive the real pipeline. A reply to a made-up number will fail to send — read the
          answer under Runs instead.
        </p>

        <div className="form-grid">
          <Field label="From">
            <input className="input mono" value={phone} onChange={(e) => setPhone(e.target.value)} />
          </Field>
          <Field label="Message" hint="Goes through the agent and produces a reply">
            <input
              className="input"
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              placeholder="How many machines are down today?"
              style={{ minWidth: 240 }}
            />
          </Field>
          <FormAction>
            <button
              className="btn btn-primary"
              disabled={act.pending || !message.trim()}
              onClick={() =>
                void run('Queued. Watch Runs for the answer.', () =>
                  post('/api/test-message', { phone, message }),
                ).then(() => setMessage(''))
              }
            >
              Send it
            </button>
          </FormAction>
        </div>

        <div className="form-grid">
          <Field label="Something happened" hint="Goes to triage, not the agent — may open a case">
            <input
              className="input"
              value={event}
              onChange={(e) => setEvent(e.target.value)}
              placeholder="Priya said she'd send the vendor quote by Friday"
              style={{ minWidth: 240 }}
            />
          </Field>
          <FormAction>
            <button
              className="btn"
              disabled={act.pending || !event.trim()}
              onClick={() =>
                void run('Sent to triage. Check Cases in a moment.', () =>
                  post('/api/test-event', { content: event, actor_phone: phone }),
                ).then(() => setEvent(''))
              }
            >
              Feed it in
            </button>
          </FormAction>
        </div>

        <div className="row wrap">
          <button
            className="btn"
            disabled={act.pending}
            onClick={() =>
              void run('Deadlines moved into the past — the scheduler will notice on its next sweep.', async () => {
                const res = await post<{ expired: number }>('/api/test-expire-expectations', {})
                setNote(`${plural(res.expired, 'commitment')} now overdue. The scheduler picks them up next sweep.`)
                return res
              })
            }
          >
            Make every open commitment overdue
          </button>
          <span className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>
            Forces the chasing behaviour so you can watch it happen.
          </span>
        </div>

        {act.error && <div className="banner banner-broken">{act.error}</div>}
        {note && !act.error && <div className="banner">{note}</div>}
      </div>
    </Section>
  )
}

import { useState } from 'react'
import { get, post, put } from '../lib/api'
import { useAction, useResource } from '../lib/hooks'
import { useOwed } from '../lib/owed'
import { useProfile } from '../lib/profile'
import type { ConfigField, ConfigResponse, ConfigStatus } from '../lib/types'
import { Async, PageHeader, Section, StatusPill } from '../components/ui'
import { Copy } from '../components/icons'
import BlockedSenders from '../components/BlockedSenders'

/* Everything the assistant needs to reach the outside world. Saved here rather
 * than in a file because these override the environment live — the settings
 * cache is cleared on write, so a corrected key takes effect on the next task
 * instead of the next restart. */

/* `inline` is the same name in the middle of a sentence — without it the
 * missing-connection banner reads "idle until Your database is connected". */
const GROUP_COPY: Record<string, { title: string; inline: string; blurb: string }> = {
  llm: {
    title: 'The model',
    inline: 'the model',
    blurb: 'The reasoning behind every answer. Without this key nothing runs at all.',
  },
  business_db: {
    title: 'Your database',
    inline: 'your database',
    blurb: 'Where it reads your business data from. Read-only, always — it cannot write here.',
  },
  whatsapp: {
    title: 'WhatsApp',
    inline: 'WhatsApp',
    blurb: 'The connector that carries messages in and out.',
  },
  memory_graph: {
    title: 'Graph memory',
    inline: 'graph memory',
    blurb: 'Long-term memory of people and facts. It works without this, just with less recall.',
  },
  tracing: {
    title: 'Tracing',
    inline: 'tracing',
    blurb: 'Sends run traces to Langfuse for debugging. Entirely optional.',
  },
  tuning: {
    title: 'Tuning',
    inline: 'tuning',
    blurb: 'How hard it works and where it looks for dropped-in documents.',
  },
  interface: {
    title: 'Interface',
    inline: 'the interface',
    blurb:
      'Which product this app opens as: Intelligence Corner (dashboards, KPIs and insights first) or the classic Second Brain layout.',
  },
  // Phase 14 (WH-08)
  warehouse: {
    title: 'Warehouse',
    inline: 'the warehouse',
    blurb:
      'The separate database your business data is read through, by a read-only role. It is set up at install, so there is nothing to enter. Test proves the role cannot write or see credentials and that the live link to your database answers.',
  },
  // Phase 10
  dashboards: {
    title: 'Dashboards origin',
    inline: 'the dashboards origin',
    blurb:
      'The separate address where approved dashboards open. Test checks the chart library and that a published page arrives with its security policy.',
  },
  context_store: {
    title: 'Business context store',
    inline: 'the business context store',
    blurb:
      'Approved descriptions of your data, kept in your own database with vector search. Test checks the tables and the search index and changes nothing.',
  },
}

/* Backend labels carry their explanation in brackets — "Triage model (cheap
 * gate: triage, pollers, layer grouping)". As a label that wraps to three lines
 * and shoves every input in the row out of alignment, so the bracketed half
 * becomes the hint underneath. */
function splitLabel(label: string): { head: string; hint: string } {
  const i = label.indexOf('(')
  if (i < 0) return { head: label, hint: '' }
  return {
    head: label.slice(0, i).trim(),
    hint: label.slice(i).replace(/^\(|\)$/g, '').trim(),
  }
}

const STATE_LABEL: Record<string, string> = {
  ok: 'working',
  failing: 'not working',
  untested: 'not tested yet',
  not_configured: 'not set up',
}

export default function Setup() {
  const profile = useProfile()
  const owed = useOwed()
  const config = useResource(() => get<ConfigResponse>('/api/config'))
  const status = useResource(() => get<ConfigStatus>('/api/config/status'), { interval: 30_000 })

  const refresh = () => {
    void config.reload()
    void status.reload()
    void owed.reload()
  }

  return (
    <>
      <PageHeader
        title="Setup"
        lede="Connections the assistant needs. Saving applies straight away — nothing here needs a restart."
      />

      {owed.missingGroups.length > 0 && (
        <div className="banner banner-owed" style={{ marginBottom: 'var(--s5)' }}>
          It stays idle until{' '}
          {owed.missingGroups.map((g) => GROUP_COPY[g]?.inline ?? g.replace(/_/g, ' ')).join(' and ')}{' '}
          {owed.missingGroups.length === 1 ? 'is' : 'are'} connected.
        </div>
      )}

      <Async data={config.data} error={config.error} loading={config.loading} reload={config.reload}>
        {(cfg) => (
          <div className="stack">
            {Object.entries(cfg.groups).map(([group, label]) => (
              <GroupCard
                key={group}
                group={group}
                label={GROUP_COPY[group]?.title ?? label}
                blurb={GROUP_COPY[group]?.blurb}
                fields={cfg.config[group] ?? []}
                state={status.data?.[group]}
                onSaved={() => {
                  void refresh()
                  void profile.reload()
                }}
              />
            ))}
          </div>
        )}
      </Async>
    </>
  )
}

/* Phase 11: GOLIVE-02 */
export function FieldHint({ f, hint }: { f: ConfigField; hint: string }) {
  if (!hint && !f.set && !f.undecryptable) return null
  return (
    <span className="dimmer" style={{ fontSize: 'var(--fs-micro)', display: 'block', marginTop: 4 }}>
      {hint}
      {hint && f.set ? ' · ' : ''}
      {f.set ? `set in ${f.source}` : ''}
      {f.undecryptable && (
        <span data-undecryptable="true" style={{ display: 'block', marginTop: 4 }}>
          <StatusPill tone="broken" label="cannot be decrypted" />{' '}
          Saved value cannot be decrypted. Re-enter it, or restore the old SETTINGS_ENCRYPTION_KEY into .env.
        </span>
      )}
    </span>
  )
}

/* Phase 13: GOLIVE-01 model picker with cost/use note and effective model */
export function ModelField({ f, onChange, onClear }: { f: ConfigField; onChange: (value: string) => void; onClear?: () => void }) {
  const options = f.options ?? []
  const known = options.some((o) => o.value === f.value)
  const initial = f.source !== 'db' ? '' : known ? f.value : '__other__'
  const [sel, setSel] = useState<string>(initial)
  const shown = sel === '' ? f.value : sel
  const note = options.find((o) => o.value === shown)?.note
  const group = (g: 'current' | 'legacy') => options.filter((o) => o.group === g)
  return (
    <>
      <select
        className="select mono"
        value={sel}
        onChange={(e) => {
          setSel(e.target.value)
          if (e.target.value === '__other__') onClear?.()
          else onChange(e.target.value)
        }}
      >
        <option value="">{f.source === 'env' ? `(use .env: ${f.value})` : `(default: ${f.default ?? f.value})`}</option>
        <optgroup label="Current">
          {group('current').map((o) => (
            <option key={o.value} value={o.value}>{o.value}</option>
          ))}
        </optgroup>
        <optgroup label="Legacy (still available)">
          {group('legacy').map((o) => (
            <option key={o.value} value={o.value}>{o.value}</option>
          ))}
        </optgroup>
        <option value="__other__">Other model ID…</option>
      </select>
      {sel === '__other__' && (
        <input
          className="input mono"
          type="text"
          spellCheck={false}
          placeholder="model ID"
          defaultValue={f.source === 'db' && !known ? f.value : ''}
          onChange={(e) => {
            const v = e.target.value.trim()
            if (v) onChange(v)
            else onClear?.()
          }}
        />
      )}
      {note && (
        <span className="dimmer" data-model-note="true" style={{ fontSize: 'var(--fs-micro)', display: 'block', marginTop: 4 }}>
          {note}
        </span>
      )}
      <span className="dimmer" data-effective="true" style={{ fontSize: 'var(--fs-micro)', display: 'block', marginTop: 4 }}>
        {`Effective: ${f.value} · set in ${f.source}`}
      </span>
    </>
  )
}

function GroupCard({
  group,
  label,
  blurb,
  fields,
  state,
  onSaved,
}: {
  group: string
  label: string
  blurb?: string
  fields: ConfigField[]
  state?: { state: string; detail?: string; optional?: boolean }
  onSaved: () => void
}) {
  const [dirty, setDirty] = useState<Record<string, string>>({})
  const [note, setNote] = useState<string | null>(state?.detail ?? null)
  const act = useAction()

  const hasChanges = Object.keys(dirty).length > 0

  async function save() {
    if (!hasChanges) {
      setNote('Nothing changed.')
      return
    }
    const ok = await act.run(() => put('/api/config', { values: dirty }))
    if (ok) {
      setDirty({})
      setNote('Saved and applied.')
      onSaved()
    }
  }

  async function test() {
    setNote('Testing…')
    await act.run(async () => {
      const res = await post<{ ok: boolean; detail: string }>(`/api/config/test/${group}`)
      setNote(res.detail)
      onSaved()
      return res
    })
  }

  const tone =
    state?.state === 'ok'
      ? 'settled'
      : state?.state === 'failing'
        ? 'broken'
        : state?.state === 'not_configured' && !state?.optional
          ? 'owed'
          : 'neutral'

  return (
    <Section
      title={label}
      actions={
        <div className="btn-row">
          {state?.optional && <span className="pill">optional</span>}
          <StatusPill tone={tone} label={STATE_LABEL[state?.state ?? ''] ?? 'unknown'} title={state?.detail} />
        </div>
      }
    >
      <div className="stack-3">
        {blurb && <p className="dim">{blurb}</p>}

        <div className="form-grid" style={{ alignItems: 'start' }}>
          {fields.map((f) => {
            const { head, hint } = splitLabel(f.label)
            return (
              /* Keyed on the saved value too, so a value changed elsewhere
               * (Quick 017: "Allow this sender" adds to Allowed numbers)
               * remounts the field instead of keeping the stale text. */
              <label className="field" key={`${f.key}:${f.secret ? '' : f.value}`} style={{ display: 'block', marginBottom: 0 }}>
                <span className="field-label">
                  {head}
                  {f.required ? ' *' : ''}
                </span>
                {f.options && f.options.length > 0 ? (
                  <ModelField
                    f={f}
                    onChange={(v) => setDirty((d) => ({ ...d, [f.key]: v }))}
                    onClear={() => setDirty((d) => { const { [f.key]: _drop, ...rest } = d; return rest })}
                  />
                ) : f.choices && f.choices.length > 0 ? (
                  <select
                    className="select mono"
                    defaultValue={f.source === 'db' ? f.value : ''}
                    onChange={(e) => setDirty((d) => ({ ...d, [f.key]: e.target.value }))}
                  >
                    {/* Phase 13 (GOLIVE-01): name the code default */}
                    <option value="">{f.source === 'env' ? `(use .env: ${f.value})` : `(default: ${f.default ?? f.value})`}</option>
                    {f.choices.map((c) => (
                      <option key={c} value={c}>
                        {c}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    className="input mono"
                    type={f.secret ? 'password' : 'text'}
                    autoComplete="off"
                    spellCheck={false}
                    defaultValue={f.secret ? '' : f.value}
                    placeholder={
                      f.secret
                        ? f.undecryptable
                          ? 'cannot be decrypted: enter it again'
                          : f.set
                            ? f.value
                            : 'not set'
                        : ''
                    }
                    onChange={(e) => setDirty((d) => ({ ...d, [f.key]: e.target.value }))}
                  />
                )}
                <FieldHint f={f.options && f.options.length > 0 ? { ...f, set: false } : f} hint={hint} />
              </label>
            )
          })}
        </div>

        {group === 'whatsapp' && (
          <div className="banner">
            <div className="grow">
              <div className="field-label">Point the connector at</div>
              <code className="mono" style={{ userSelect: 'all' }}>
                {location.origin}/webhook/whatsapp
              </code>
              <div className="dimmer" style={{ fontSize: 'var(--fs-micro)', marginTop: 4 }}>
                Its secret is WEBHOOK_SECRET from the server's .env — deliberately not the key you
                signed in with.
              </div>
            </div>
            <button
              className="btn btn-sm"
              onClick={() => void navigator.clipboard?.writeText(`${location.origin}/webhook/whatsapp`)}
            >
              <Copy size={13} /> Copy
            </button>
          </div>
        )}

        {/* Quick 017: senders the allowlist dropped, with "Allow this sender" */}
        {group === 'whatsapp' && <BlockedSenders onAllowed={onSaved} />}

        {act.error && <div className="banner banner-broken">{act.error}</div>}

        <div className="row wrap">
          <button className="btn btn-primary btn-sm" disabled={act.pending || !hasChanges} onClick={save}>
            Save
          </button>
          <button className="btn btn-sm" disabled={act.pending} onClick={test}>
            Test the connection
          </button>
          {note && !act.error && (
            <span className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>
              {note}
            </span>
          )}
        </div>
      </div>
    </Section>
  )
}

import { useState } from 'react'
import { del, get, put } from '../lib/api'
import { useAction, useResource } from '../lib/hooks'
import { modelSuggestions } from '../lib/models'
import type { AgentConfig, AgentInfo, ConfigResponse } from '../lib/types'
import { Async, Empty, Field, PageHeader, Section, StatusPill } from '../components/ui'

/* What each agent is, what it can reach for, and what it is told.
 *
 * The tool list here can only be narrowed. The superset is defined in code and
 * is a hard ceiling — nothing typed on this page can grant a capability the
 * agent was not built with, which is the whole reason it is safe to expose. */

export default function Agents() {
  const { data, error, loading, reload } = useResource(() => get<AgentInfo[]>('/api/agents'))
  // Phase 13 (GOLIVE-01): model suggestions follow the backend catalog
  const cfg = useResource(() => get<ConfigResponse>('/api/config'))
  const models = modelSuggestions(cfg.data)

  return (
    <>
      <PageHeader
        title="Agents"
        lede="The specialists behind every answer, and the instructions each one works from."
      />

      <Async data={data} error={error} loading={loading} reload={reload} empty={<Empty title="No agents registered" />}>
        {(agents) => (
          <div className="stack">
            {agents.map((a) => (
              <AgentCard key={a.name} agent={a} models={models} onSaved={reload} />
            ))}
          </div>
        )}
      </Async>
    </>
  )
}

function AgentCard({ agent, models, onSaved }: { agent: AgentInfo; models: string[]; onSaved: () => void }) {
  const [editing, setEditing] = useState(false)

  return (
    <Section
      title={agent.name}
      meta={agent.model}
      actions={
        <div className="btn-row">
          {agent.customized && <StatusPill tone="signal" label="customised" />}
          {agent.config && (
            <button className="btn btn-sm" onClick={() => setEditing((v) => !v)}>
              {editing ? 'Close' : 'Change how it works'}
            </button>
          )}
        </div>
      }
    >
      <div className="stack-3">
        <p className="dim">{agent.role}</p>

        <div>
          <span className="field-label">What it can reach for</span>
          <div className="row wrap" style={{ gap: 'var(--s2)', marginTop: 4 }}>
            {agent.tools.length ? (
              agent.tools.map((t) => (
                <span className="pill" key={t.name} title={t.description}>
                  {t.name}
                </span>
              ))
            ) : (
              <span className="dimmer">Nothing — it answers from what it is given.</span>
            )}
          </div>
        </div>

        {agent.guardrails.length > 0 && (
          <div>
            <span className="field-label">Held to</span>
            <ul className="dimmer" style={{ margin: 0, paddingLeft: 18, fontSize: 'var(--fs-tiny)' }}>
              {agent.guardrails.map((g, i) => (
                <li key={i}>{g}</li>
              ))}
            </ul>
          </div>
        )}

        {!agent.config && (
          <p className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>
            A background service — there is nothing to configure here.
          </p>
        )}

        {editing && agent.config && agent.config_key && (
          <Editor
            configKey={agent.config_key}
            models={models}
            config={agent.config}
            superset={agent.tool_superset ?? []}
            customized={Boolean(agent.customized)}
            onSaved={() => {
              setEditing(false)
              onSaved()
            }}
          />
        )}
      </div>
    </Section>
  )
}

function Editor({
  configKey,
  models,
  config,
  superset,
  customized,
  onSaved,
}: {
  configKey: string
  models: string[]
  config: AgentConfig
  superset: string[]
  customized: boolean
  onSaved: () => void
}) {
  const [draft, setDraft] = useState<AgentConfig>({ ...config, tools: [...config.tools] })
  const [note, setNote] = useState<string | null>(null)
  const act = useAction()

  const toggle = (tool: string) =>
    setDraft((d) => ({
      ...d,
      tools: d.tools.includes(tool) ? d.tools.filter((t) => t !== tool) : [...d.tools, tool],
    }))

  async function save() {
    setNote(null)
    const ok = await act.run(() => put(`/api/agents/${configKey}/config`, draft))
    if (ok) {
      setNote('Saved. It applies from the next task onwards.')
      onSaved()
    }
  }

  async function reset() {
    const ok = await act.run(() => del(`/api/agents/${configKey}/config`))
    if (ok) onSaved()
  }

  return (
    <div className="stack-3" style={{ borderTop: '1px solid var(--line)', paddingTop: 'var(--s4)' }}>
      <Field label="What it is told">
        <textarea
          className="textarea"
          rows={12}
          value={draft.prompt}
          onChange={(e) => setDraft({ ...draft, prompt: e.target.value })}
        />
      </Field>

      <div className="form-grid">
        <Field label="Model">
          <input
            className="input mono"
            list={`models-${configKey}`}
            value={draft.model}
            onChange={(e) => setDraft({ ...draft, model: e.target.value })}
          />
          <datalist id={`models-${configKey}`}>
            {models.map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
        </Field>
        <Field label="Temperature" hint="0 is literal, 2 is loose">
          <input
            className="input mono"
            type="number"
            step="0.1"
            min={0}
            max={2}
            value={draft.temperature}
            onChange={(e) => setDraft({ ...draft, temperature: Number(e.target.value) })}
          />
        </Field>
        <Field label="Reply ceiling" hint="Tokens, 256–32000">
          <input
            className="input mono"
            type="number"
            min={256}
            max={32000}
            value={draft.max_tokens}
            onChange={(e) => setDraft({ ...draft, max_tokens: Number(e.target.value) })}
          />
        </Field>
        <Field label="Step limit" hint="How many turns before it gives up">
          <input
            className="input mono"
            type="number"
            min={1}
            max={100}
            value={draft.recursion_limit}
            onChange={(e) => setDraft({ ...draft, recursion_limit: Number(e.target.value) })}
          />
        </Field>
      </div>

      {superset.length > 0 && (
        <div>
          <span className="field-label">Tools it may use</span>
          <p className="dimmer" style={{ fontSize: 'var(--fs-micro)', marginBottom: 6 }}>
            You can take tools away here. The list itself is fixed in code and cannot be added to.
          </p>
          <div className="row wrap" style={{ gap: 'var(--s3)' }}>
            {superset.map((t) => (
              <label className="check" key={t}>
                <input type="checkbox" checked={draft.tools.includes(t)} onChange={() => toggle(t)} />
                <span className="mono">{t}</span>
              </label>
            ))}
          </div>
        </div>
      )}

      {act.error && <div className="banner banner-broken">{act.error}</div>}
      {note && !act.error && <div className="banner">{note}</div>}

      <div className="btn-row">
        <button className="btn btn-primary btn-sm" disabled={act.pending} onClick={save}>
          Save
        </button>
        <button className="btn btn-sm" disabled={act.pending || !customized} onClick={reset}>
          Put it back to default
        </button>
      </div>
    </div>
  )
}

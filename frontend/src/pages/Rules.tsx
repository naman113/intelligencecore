import { useState } from 'react'
import { get, post } from '../lib/api'
import { asks } from '../lib/ask'
import { editThenApprove } from '../lib/contextActions'
import { useAction, useResource, useStored } from '../lib/hooks'
import type { ContextRule } from '../lib/ictypes'
import { useOwed } from '../lib/owed'
import { useAsk } from '../components/AskPanel'
import { PendingBlock, type PendingHandlers } from '../components/PendingBlock'
import { Empty, ErrorNote, Loading, PageHeader, StatusPill, Tabs } from '../components/ui'
import './ic.css'

/* Rules & KPI definitions. Gold iff pending. The "proposed by agent" chip is
 * provenance, so it is a neutral pill. No direct write route exists: creating
 * a rule or a KPI definition messages the agent. */

export type RulesTab = 'rule' | 'kpi'

export function Composer({
  label,
  placeholder,
  onSend,
  sent,
}: {
  label: string
  placeholder: string
  onSend: (text: string) => void
  sent?: boolean
}) {
  const [text, setText] = useState('')
  return (
    <div className="card">
      <textarea
        className="textarea"
        aria-label={label}
        placeholder={placeholder}
        value={text}
        maxLength={1500}
        onChange={(e) => setText(e.target.value)}
      />
      <div className="btn-row">
        <button
          className="btn btn-sm btn-primary"
          disabled={!text.trim()}
          onClick={() => {
            onSend(text.trim())
            setText('')
          }}
        >
          {label}
        </button>
      </div>
      {sent && <p className="ic-meta">Sent to the agent — it will propose this for your approval</p>}
    </div>
  )
}

export function RulesView({
  rules,
  tab,
  onTab,
  editing,
  handlers,
  busy,
  error,
  sent,
  onAsk,
}: {
  rules: ContextRule[]
  tab: RulesTab
  onTab: (t: RulesTab) => void
  editing: number | null
  handlers: PendingHandlers
  busy?: boolean
  error?: string | null
  sent?: boolean
  onAsk?: (tab: RulesTab, text: string) => void
}) {
  const ofKind = (k: RulesTab) => rules.filter((r) => (r.kind === 'kpi' ? 'kpi' : 'rule') === k)
  const pend = (k: RulesTab) => ofKind(k).filter((r) => r.pending !== null).length
  const rows = ofKind(tab)
  return (
    <div>
      <Tabs<RulesTab>
        value={tab}
        onChange={onTab}
        options={[
          { value: 'rule', label: 'Rules', count: pend('rule') },
          { value: 'kpi', label: 'KPI definitions', count: pend('kpi') },
        ]}
      />
      {error && <div className="banner banner-broken">{error}</div>}
      {onAsk && (
        <Composer
          key={tab}
          label={tab === 'rule' ? 'Add a rule' : 'Define a KPI'}
          placeholder={
            tab === 'rule'
              ? 'Describe the rule, for example: cancelled orders never count as revenue'
              : 'Describe the KPI, for example: days sales outstanding, by region'
          }
          onSend={(t) => onAsk(tab, t)}
          sent={sent}
        />
      )}
      {rows.length === 0 ? (
        <Empty title={tab === 'rule' ? 'No rules yet' : 'No KPI definitions yet'}>
          Ask the agent and it will propose one for your approval.
        </Empty>
      ) : (
        <div className="ic-list">
          {rows.map((r) => {
            const gold = r.pending !== null
            const byAgent = (r.status === 'approved' && r.origin === 'agent') || r.pending?.origin === 'agent'
            const byOwner = r.status === 'approved' && r.origin === 'human' // Phase 12 (WR-11)
            return (
              <div
                className={`ic-row${gold ? ' ic-gold' : ''}`}
                key={r.ref}
                data-action-id={gold ? r.pending!.action_id : undefined}
              >
                <span>
                  <strong>{r.title || r.ref}</strong> <StatusPill status={r.status} />{' '}
                  {byAgent && !gold && <span className="pill">proposed by agent</span>}
                  {byOwner && !gold && <span className="pill">edited by owner</span>}
                </span>
                <span className="mono ic-meta ic-ref">{r.ref}</span>
                {r.text && <span className="ic-body">{r.text}</span>}
                {r.tables.length > 0 && (
                  <span className="ic-chips">
                    {r.tables.map((t) => (
                      <span className="pill mono" key={t}>
                        {t}
                      </span>
                    ))}
                  </span>
                )}
                {r.pending && (
                  <PendingBlock key={r.pending.action_id} pending={r.pending} editing={editing} busy={busy} handlers={handlers} />
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

export default function Rules() {
  const { send } = useAsk()
  const owed = useOwed()
  const act = useAction()
  const [tab, setTab] = useStored<RulesTab>('rules.tab', 'rule')
  const [editing, setEditing] = useState<number | null>(null)
  const [sent, setSent] = useState(false)

  const res = useResource(() => get<{ rules: ContextRule[] }>('/api/context/rules?status=all&limit=1000'), {
    interval: 15_000,
  })
  const decide = (fn: () => Promise<unknown>) =>
    void act.run(fn).then(async (ok) => {
      if (ok) setEditing(null)
      await Promise.all([res.reload(), owed.reload()])
    })
  const handlers: PendingHandlers = {
    onApprove: (id) => decide(() => post('/api/actions/' + id + '/approve')),
    onReject: (id) => decide(() => post('/api/actions/' + id + '/reject')),
    onEdit: setEditing,
    onSave: (id, text) => decide(() => editThenApprove(id, text)),
  }

  return (
    <div className="ic-page">
      <PageHeader title="Rules & KPI definitions" lede="What the business means by its numbers. The agent proposes, you decide." />
      {res.error && <ErrorNote message={res.error} onRetry={() => void res.reload()} />}
      {res.loading && !res.data ? (
        <Loading label="Reading rules" />
      ) : (
        <RulesView
          rules={res.data?.rules ?? []}
          tab={tab}
          onTab={(t) => {
            setSent(false)
            setTab(t)
          }}
          editing={editing}
          handlers={handlers}
          busy={act.pending}
          error={act.error}
          sent={sent}
          onAsk={(t, text) =>
            void send(t === 'rule' ? asks.rule(text) : asks.kpiDefinition(text)).then((ok) => setSent(ok))
          }
        />
      )}
    </div>
  )
}

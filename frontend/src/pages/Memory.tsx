import { useState, type KeyboardEvent } from 'react'
import { Link } from 'react-router-dom'
import { del, get } from '../lib/api'
import { ago, isoTitle, threadLabel, titleCase } from '../lib/format'
import { useAction, useResource, useStored } from '../lib/hooks'
import { useOwed } from '../lib/owed'
import type { GraphEpisode, GraphFact, Learning } from '../lib/types'
import { loadGraphStatus, type GraphStatus } from '../lib/graph'
import { Async, ConfirmButton, Empty, PageHeader, Section, StatusPill, Tabs } from '../components/ui'
import { GraphStatusLine } from '../components/GraphStatus'
import { MemoryGraph } from '../components/MemoryGraph'
import { Trash } from '../components/icons'
import { episodeTitle, recallKind } from '../lib/memgraph'

/* Two stores, one page.
 *
 * The knowledge graph is the ground truth every agent searches before acting —
 * seeded by the onboarding interview, grown by case distillations and chat
 * setup. Learnings are things it worked out on its own after tasks. An
 * operator asking "what does it know?" means both, so both live here; the
 * interview's "it's in memory now" points at the graph section specifically.
 */

function scopeLabel(groupId: string, subjects: Map<string, string>) {
  if (groupId === 'global') return 'everywhere'
  // chat scopes are the jid with every non-alphanumeric collapsed to '_', so
  // match subjects by applying the same collapse rather than un-munging.
  for (const [jid, subject] of subjects) {
    if (`chat_${jid.replace(/[^a-zA-Z0-9]/g, '_')}` === groupId) return subject
  }
  return groupId
}

/* Quick 009: the empty state says which it is — not connected (and why),
 * connected but empty, or nothing to show yet — instead of "(or unreachable)". */
function graphEmpty(status: GraphStatus | null) {
  if (status?.state === 'not_connected') {
    return (
      <Empty title="The knowledge graph is not connected">
        {status.reason ? `${status.reason}. ` : ''}It retries by itself; Reconnect tries now. Agents keep
        working without it and learnings are still kept below.
      </Empty>
    )
  }
  if (status?.state === 'disabled') {
    return <Empty title="The knowledge graph is turned off">Switch it on from the Setup page.</Empty>
  }
  return (
    <Empty title="Nothing in the graph yet">
      The onboarding interview, closing cases, chat setup and learned facts all write here.
    </Empty>
  )
}

function GraphSection({ subjects }: { subjects: Map<string, string> }) {
  const status = useResource(loadGraphStatus, { interval: 15_000 })
  const episodes = useResource(
    () => get<{ episodes: GraphEpisode[] }>('/api/memory/episodes?limit=100'),
    { interval: 60_000 },
  )
  const reloadAll = async () => {
    await status.reload()
    await episodes.reload()
  }
  const [q, setQ] = useState('')
  const [facts, setFacts] = useState<GraphFact[] | null>(null)
  const search = useAction()

  async function runSearch() {
    const query = q.trim()
    if (!query) return
    await search.run(async () => {
      const res = await get<{ facts: GraphFact[] }>(
        `/api/memory/search?q=${encodeURIComponent(query)}&limit=20`,
      )
      setFacts(res.facts ?? [])
      return res
    })
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter') void runSearch()
  }

  return (
    <Section title="Knowledge graph — what it was told and distilled" flush>
      <div style={{ padding: 'var(--s3) var(--s4)', borderBottom: '1px solid var(--line)' }}>
        <GraphStatusLine status={status.data} error={status.error} reload={reloadAll} />
      </div>
      <div className="row" style={{ padding: 'var(--s3) var(--s4)', borderBottom: '1px solid var(--line)' }}>
        <input
          className="input grow"
          placeholder="Ask it what it knows (facts, entities and episodes, the way agents search)…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKeyDown}
        />
        <button className="btn btn-sm" disabled={search.pending || !q.trim()} onClick={() => void runSearch()}>
          {search.pending ? 'Searching…' : 'Search'}
        </button>
      </div>

      {facts !== null && (
        <div style={{ borderBottom: '1px solid var(--line)' }}>
          {facts.length === 0 ? (
            <p className="dim" style={{ padding: 'var(--s3) var(--s4)', margin: 0 }}>
              Nothing surfaced for that — an agent asking the same thing would come up empty too.
            </p>
          ) : (
            facts.map((f, i) => (
              <div
                key={f.uuid || i}
                className="row-top"
                style={{ padding: 'var(--s2) var(--s4)' }}
                data-recall-kind={f.kind ?? 'fact'}
              >
                <span className="pill pill-signal nowrap" style={{ minWidth: 84, justifyContent: 'center' }}>
                  {recallKind({ kind: f.kind ?? 'fact', fact: f.fact, type: f.type })}
                </span>
                <div className="grow">
                  <div
                    style={{ whiteSpace: 'pre-wrap' }}
                    className={f.superseded ? 'dim' : undefined}
                  >
                    {f.superseded ? <s>{f.fact}</s> : f.fact}
                  </div>
                  <div className="dimmer" style={{ fontSize: 'var(--fs-micro)', marginTop: 2 }}>
                    {f.superseded && (
                      <>
                        <StatusPill tone="neutral" label="superseded" />{' '}
                      </>
                    )}
                    {f.kind === 'episode' && f.name ? `${episodeTitle({ name: f.name, source: f.source })} · ` : ''}
                    {f.group_id && f.group_id !== 'global' ? `in ${scopeLabel(f.group_id, subjects)} · ` : ''}
                    {f.created_at && <span title={isoTitle(f.created_at)}>learned {ago(f.created_at)}</span>}
                  </div>
                </div>
              </div>
            ))
          )}
        </div>
      )}

      <Async
        data={episodes.data?.episodes ?? null}
        error={episodes.error}
        loading={episodes.loading}
        reload={episodes.reload}
        empty={graphEmpty(status.data)}
      >
        {(list) => (
          <div>
            {list.map((ep) => (
              <div
                key={ep.uuid}
                className="row-top"
                style={{ padding: 'var(--s3) var(--s4)', borderBottom: '1px solid var(--line)' }}
              >
                <span className="pill pill-signal nowrap" style={{ minWidth: 84, justifyContent: 'center' }}>
                  {titleCase(ep.source || 'episode')}
                </span>
                <div className="grow">
                  {/* Quick 014: the full text, never a truncated line */}
                  <div style={{ whiteSpace: 'pre-wrap' }} data-episode-text>
                    {ep.content}
                  </div>
                  <div className="dimmer" style={{ fontSize: 'var(--fs-micro)', marginTop: 3 }}>
                    knows this {scopeLabel(ep.group_id, subjects) === 'everywhere' ? 'everywhere' : `in ${scopeLabel(ep.group_id, subjects)}`}
                    {ep.created_at && (
                      <>
                        {' · '}
                        <span title={isoTitle(ep.created_at)}>added {ago(ep.created_at)}</span>
                      </>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </Async>
    </Section>
  )
}

/* Quick 009: the Learnings filter. Known kinds in a fixed order, and always a
 * Lesson tab (approach and pitfalls kept from hard runs), so the owner can
 * find lessons even before the first one exists. */
export const KIND_ORDER = ['fact', 'preference', 'correction', 'recipe', 'lesson']

export function learningKinds(list: Learning[]): string[] {
  const present = new Set(list.map((l) => l.kind))
  present.add('lesson')
  const known = KIND_ORDER.filter((k) => present.has(k))
  const other = Array.from(present).filter((k) => !KIND_ORDER.includes(k)).sort()
  return [...known, ...other]
}

/* Quick 009: superseded learnings are kept for audit but never retrieved by
 * agents; show them as retired (and why) rather than as live knowledge.
 * Quick 020: a rejected one is retired too; a pending one is not used yet. */
function isRetired(l: Learning) {
  return l.status === 'superseded' || l.status === 'rejected' ||
    Boolean(l.status && l.status !== 'active' && l.status !== 'pending')
}

/* Quick 020: who a learning applies to. A preference belongs to the person who
 * stated it (the owner, one person, or the group it was said in); everything
 * else is shared. */
export function scopeText(l: Learning): string {
  const kind = l.scope_kind ?? (l.kind === 'preference' ? 'owner' : 'global')
  switch (kind) {
    case 'owner':
      return 'Owner only'
    case 'person':
      return `${l.scope_label || 'One person'} only`
    case 'group':
      return `Group: ${l.scope_label || 'a group'}`
    default:
      return 'Everyone'
  }
}

export function LearningRow({
  l,
  subjects,
  busy,
  onForget,
}: {
  l: Learning
  subjects: Map<string, string>
  busy: boolean
  onForget: () => void
}) {
  const pending = l.status === 'pending'
  return (
    <div className="row-top" style={{ padding: 'var(--s3) var(--s4)', borderBottom: '1px solid var(--line)' }}>
      <span className="pill pill-signal nowrap" style={{ minWidth: 84, justifyContent: 'center' }}>
        {titleCase(l.kind)}
      </span>
      <div className="grow" data-learning-status={l.status ?? 'active'} data-learning-scope={l.scope_kind ?? ''}>
        <div className={isRetired(l) || pending ? 'dim' : undefined}>{l.content}</div>
        <div className="dimmer" style={{ fontSize: 'var(--fs-micro)', marginTop: 3 }}>
          <span className="pill" title="Who this applies to">
            {scopeText(l)}
          </span>{' '}
          {pending && (
            <>
              <StatusPill status="pending" label="waiting for your approval" />{' '}
              not used until approved on <Link to="/approvals">Approvals</Link> ·{' '}
            </>
          )}
          {isRetired(l) && (
            <>
              <StatusPill tone="neutral" label={l.status === 'rejected' ? 'rejected' : 'superseded'} />{' '}
              {l.status_reason ? `${l.status_reason} · ` : 'merged into another learning · '}
            </>
          )}
          {l.thread_id && <>from {threadLabel(l.thread_id, subjects)} · </>}
          <span title={isoTitle(l.updated_at)}>updated {ago(l.updated_at)}</span>
          {l.revisions > 0 && ` · rewritten ${l.revisions}×`}
        </div>
      </div>
      <ConfirmButton confirmLabel="Forget it?" disabled={busy} onConfirm={onForget}>
        <Trash size={13} />
      </ConfirmButton>
    </div>
  )
}

export default function Memory() {
  const owed = useOwed()
  const act = useAction()
  const [kind, setKind] = useStored<string>('memory.kind', 'all')

  const { data, error, loading, reload } = useResource(
    () => get<Learning[]>('/api/learnings?limit=200'),
    { interval: 30_000 },
  )

  const subjects = new Map(owed.chats.filter((c) => c.subject).map((c) => [c.jid, c.subject!]))
  const kinds = learningKinds(data ?? [])
  const shown = kind === 'all' ? (data ?? []) : (data ?? []).filter((l) => l.kind === kind)

  return (
    <>
      <PageHeader
        title="Memory"
        lede="Everything it knows that didn't come from a live message: the knowledge graph it searches before acting, and the lessons it kept from finished work. Delete anything wrong — it keeps acting on it otherwise."
      />

      <MemoryGraph />

      <GraphSection subjects={subjects} />

      <Section
        title="Learnings — what it worked out on its own"
        flush
        actions={
          (data ?? []).length > 0 ? (
            <Tabs
              value={kind}
              onChange={setKind}
              options={[
                { value: 'all', label: 'All', count: data?.length },
                ...kinds.map((k) => ({
                  value: k,
                  label: titleCase(k),
                  count: (data ?? []).filter((l) => l.kind === k).length,
                })),
              ]}
            />
          ) : undefined
        }
      >
        <Async
          data={shown}
          error={error}
          loading={loading}
          reload={reload}
          empty={
            <Empty title="Nothing learned yet">
              This fills up as tasks finish and cases close — each entry is something it noticed
              once and expects to need again.
            </Empty>
          }
        >
          {(list) => (
            <div>
              {list.map((l) => (
                <LearningRow
                  key={l.id}
                  l={l}
                  subjects={subjects}
                  busy={act.pending}
                  onForget={() => void act.run(() => del(`/api/learnings/${l.id}`)).then(reload)}
                />
              ))}
            </div>
          )}
        </Async>
      </Section>

      {act.error && <div className="banner banner-broken" style={{ marginTop: 'var(--s3)' }}>{act.error}</div>}
    </>
  )
}

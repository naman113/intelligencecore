import { useEffect, useState, type ReactNode } from 'react'
import { Link, useLocation, useParams, useSearchParams } from 'react-router-dom'
import { download, get } from '../lib/api'
import { ago, bytes, dateTime, isoTitle, plural, truncate } from '../lib/format'
import { useAction, useResource } from '../lib/hooks'
import type { Artifact, ToolCall } from '../lib/types'
import {
  FAMILIES,
  STATUSES,
  WINDOWS,
  busy,
  countEntries,
  durationText,
  elapsedSince,
  filterQuery,
  graphKindLabel,
  graphScopeLabel,
  kindLabel,
  loadActivity,
  loadNow,
  loadRunMemory,
  memoryMeta,
  mergePages,
  retiredLabel,
  outcomeLine,
  parseRunId,
  readFilters,
  refLabel,
  runRoute,
  statusLabel,
  type ActivityItem,
  type ActivityNow,
  type MemoryStatus,
  type RunFilters,
  type RunMemory,
} from '../lib/runs'
import { Async, Empty, ErrorNote, KeyValue, Loading, PageHeader, Section, StatusPill, toneOf } from '../components/ui'
import { ChevronLeft, Cross, Download } from '../components/icons'
import { RunTraceSection, UsageTrace } from '../components/Trace'
import { useAsk } from '../components/AskPanel'
import '../components/split.css'
import './runs.css'

/* Quick 008: the control center for everything that runs. Agent runs (chat,
 * WhatsApp, uploads, the inbox, playbooks) and the background work the loops
 * record (watch checks, dashboard refreshes, the scheduler, triage, data intake,
 * housekeeping) in one feed, with what is running now and what failed recently
 * at the top. Blue is work in flight, red is broken; nothing here is owed, so
 * there is no gold on this page. */

const FAST = 3_000
const SLOW = 10_000

export default function Runs() {
  const { id } = useParams()
  const location = useLocation()
  const [params, setParams] = useSearchParams()
  const filters = readFilters(params)
  const fkey = filterQuery(filters)

  const [fast, setFast] = useState(false)
  const now = useResource(loadNow, { interval: fast ? FAST : SLOW })
  useEffect(() => setFast(busy(now.data)), [now.data])
  const feed = useResource(() => loadActivity(filters), {
    interval: fast ? 5_000 : 20_000,
    deps: [fkey],
  })

  const [older, setOlder] = useState<ActivityItem[]>([])
  const [cursor, setCursor] = useState<string | null | undefined>(undefined)
  const [olderBusy, setOlderBusy] = useState(false)
  const [olderError, setOlderError] = useState<string | null>(null)
  useEffect(() => {
    setOlder([])
    setCursor(undefined)
    setOlderError(null)
  }, [fkey])

  const next = cursor === undefined ? feed.data?.next_before ?? null : cursor
  const loadOlder = async () => {
    if (!next) return
    setOlderBusy(true)
    setOlderError(null)
    try {
      const page = await loadActivity(filters, next)
      setOlder((o) => [...o, ...page.items])
      setCursor(page.next_before)
    } catch (e) {
      setOlderError(e instanceof Error ? e.message : String(e))
    } finally {
      setOlderBusy(false)
    }
  }

  const setFilter = (key: keyof RunFilters, value: string) => {
    const p = new URLSearchParams(params)
    if (value) p.set(key, value)
    else p.delete(key)
    setParams(p, { replace: true })
  }

  const selected = parseRunId(id)
  const items = feed.data ? mergePages(feed.data.items, older) : null

  return (
    <>
      <PageHeader
        title="Runs"
        lede="Everything the system runs: answers, playbooks, watch checks, dashboard refreshes, data intake and housekeeping. What is running now and what failed come first."
      />

      <NowPanel now={now.data} error={now.error} search={location.search} selected={id} />

      <FilterBar filters={filters} onChange={setFilter} />

      <div className={`split runs-split${id ? ' has-detail' : ''}`}>
        <div className="panel split-list">
          <Async
            data={items}
            error={feed.error}
            loading={feed.loading}
            reload={feed.reload}
            empty={
              <Empty title={fkey ? 'Nothing matches these filters' : 'Nothing has run yet'}>
                {fkey ? 'Try a wider time window or another kind.' : undefined}
              </Empty>
            }
          >
            {(list) => (
              <div>
                {list.map((item) => (
                  <FeedRow key={item.uid} item={item} search={location.search} selected={id} />
                ))}
                {next && (
                  <div className="runs-more">
                    <button className="btn btn-sm" disabled={olderBusy} onClick={() => void loadOlder()}>
                      {olderBusy ? 'Loading…' : 'Show older'}
                    </button>
                    {olderError && <span className="runs-more-error">{olderError}</span>}
                  </div>
                )}
              </div>
            )}
          </Async>
        </div>

        <div className="split-detail">
          <Link className="btn btn-quiet btn-sm back-link" to={`/system/runs${location.search}`}>
            <ChevronLeft size={13} /> All runs
          </Link>
          {selected?.source === 'agent' ? (
            <AgentDetail id={selected.id} />
          ) : selected?.source === 'job' ? (
            <JobDetail id={selected.id} />
          ) : (
            <div className="panel">
              <Empty title="Pick a run">
                What it was, what started it, what came out, and for agent runs every tool call and file.
              </Empty>
            </div>
          )}
        </div>
      </div>
    </>
  )
}

/* ── presentational pieces (rendered in tests) ─────────────────────────── */

function toneDot(status: string): string {
  const t = toneOf(status)
  return t === 'neutral' || t === 'owed' ? 'dot' : `dot dot-${t}`
}

export function SkillChips({ skills }: { skills?: string[] | null }) {
  if (!skills || skills.length === 0) return null
  return (
    <div className="runs-skills" data-skills>
      {skills.map((s) => (
        <span key={s} className="pill runs-skill" title={`Used the ${s} skill`}>
          {s}
        </span>
      ))}
    </div>
  )
}

function ItemLink({
  item,
  search,
  className,
  children,
}: {
  item: ActivityItem
  search: string
  className: string
  children: ReactNode
}) {
  if (item.id == null) return <div className={className}>{children}</div>
  return (
    <Link className={className} to={runRoute(item, search)}>
      {children}
    </Link>
  )
}

export function FeedRow({ item, search = '', selected }: { item: ActivityItem; search?: string; selected?: string }) {
  const routeId = item.source === 'agent' ? String(item.id) : `job-${item.id}`
  const isSel = selected !== undefined && selected === routeId
  const repeated = item.repeats > 1
  const line = outcomeLine(item)
  return (
    <ItemLink
      item={item}
      search={search}
      className={`list-row runs-row${isSel ? ' is-selected' : ''}${item.status === 'failed' ? ' is-failed' : ''}`}
    >
      <div className="row" style={{ gap: 'var(--s2)' }}>
        <span className={toneDot(item.status)} />
        <span className="list-title truncate grow" title={item.title}>
          {item.title}
        </span>
        <StatusPill status={item.status} label={statusLabel(item.status)} />
      </div>
      <div className="list-meta">
        {item.trigger && <span>{item.trigger}</span>}
        {repeated && (
          <span className="mono runs-repeats" title={`Ran ${item.repeats} times with the same outcome`}>
            ×{item.repeats}
          </span>
        )}
        {!!item.duration_ms && item.status !== 'running' && <span>{durationText(item.duration_ms)}</span>}
        <span className="dimmer" title={isoTitle(repeated ? item.last_at : item.at)}>
          {repeated ? `last ${ago(item.last_at)}` : ago(item.at)}
        </span>
      </div>
      {line && <div className={`runs-line${item.status === 'failed' ? ' is-error' : ''}`}>{truncate(line, 140)}</div>}
      <SkillChips skills={item.skills_used} />
    </ItemLink>
  )
}

export function NowPanel({
  now,
  error,
  search = '',
  selected,
}: {
  now: ActivityNow | null
  error?: string | null
  search?: string
  selected?: string
}) {
  if (!now) {
    return error ? (
      <div className="runs-now-error">
        <ErrorNote message={error} />
      </div>
    ) : null
  }
  const { counts } = now
  const runningMeta = [
    `${counts.running} running`,
    counts.queued ? `${counts.queued} waiting` : '',
  ]
    .filter(Boolean)
    .join(' · ')
  const active = [...now.running, ...now.queued]
  return (
    <div className="runs-now" data-runs-now>
      <section className="panel runs-now-col">
        <div className="panel-head">
          <h2 className="panel-title">Running now</h2>
          <span className="dimmer mono" style={{ fontSize: 12 }} data-count="running">
            {runningMeta}
          </span>
        </div>
        {active.length === 0 ? (
          <div className="runs-now-empty">Nothing is running right now. {plural(counts.done_24h, 'run')} finished in the last 24 hours.</div>
        ) : (
          <div>
            {active.map((item) => (
              <ItemLink key={item.uid} item={item} search={search} className="runs-now-row">
                <span className={`dot ${item.status === 'running' ? 'dot-signal runs-pulse' : ''}`} />
                <div className="grow" style={{ minWidth: 0 }}>
                  <div className="runs-now-title truncate">{item.title}</div>
                  <div className="runs-now-meta truncate">
                    {item.status === 'queued' ? 'waiting' : outcomeLine(item)}
                  </div>
                </div>
                <span className="mono dimmer runs-now-time">
                  {item.status === 'running' ? elapsedSince(item.started_at || item.at) : ''}
                </span>
              </ItemLink>
            ))}
          </div>
        )}
      </section>

      <section className="panel runs-now-col">
        <div className="panel-head">
          <h2 className="panel-title">Failed recently</h2>
          <span className="dimmer mono" style={{ fontSize: 12 }} data-count="failed">
            {counts.failed_24h} in 24 h
          </span>
        </div>
        {now.failed.length === 0 ? (
          <div className="runs-now-empty">No failures in the last 24 hours.</div>
        ) : (
          <div>
            {now.failed.map((item) => (
              <ItemLink
                key={item.uid}
                item={item}
                search={search}
                className={`runs-now-row${selected === (item.source === 'agent' ? String(item.id) : `job-${item.id}`) ? ' is-selected' : ''}`}
              >
                <span className="dot dot-broken" />
                <div className="grow" style={{ minWidth: 0 }}>
                  <div className="runs-now-title truncate">
                    {item.title}
                    {item.repeats > 1 && <span className="mono dimmer"> ×{item.repeats}</span>}
                  </div>
                  <div className="runs-now-meta runs-error-text truncate">{item.error || item.summary}</div>
                </div>
                <span className="dimmer runs-now-time">{ago(item.last_at || item.at)}</span>
              </ItemLink>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}

export function FilterBar({
  filters,
  onChange,
}: {
  filters: RunFilters
  onChange: (key: keyof RunFilters, value: string) => void
}) {
  return (
    <div className="runs-filters" data-runs-filters>
      <div className="runs-chips" role="group" aria-label="Kind of run">
        {FAMILIES.map((f) => (
          <button
            key={f.value || 'all'}
            className={`runs-chip${filters.kind === f.value ? ' is-active' : ''}`}
            aria-pressed={filters.kind === f.value}
            onClick={() => onChange('kind', f.value)}
          >
            {f.label}
          </button>
        ))}
      </div>
      <div className="runs-selects">
        <select
          className="select"
          aria-label="Status"
          value={filters.status}
          onChange={(e) => onChange('status', e.target.value)}
        >
          {STATUSES.map((s) => (
            <option key={s.value || 'any'} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>
        <select
          className="select"
          aria-label="Time window"
          value={filters.since}
          onChange={(e) => onChange('since', e.target.value)}
        >
          {WINDOWS.map((w) => (
            <option key={w.value || 'any'} value={w.value}>
              {w.label}
            </option>
          ))}
        </select>
        {filters.ref && (
          <button className="runs-chip is-active runs-ref" onClick={() => onChange('ref', '')} title="Show every run">
            Only {refLabel(filters.ref)} <Cross size={11} />
          </button>
        )}
      </div>
    </div>
  )
}

/* ── detail panes ──────────────────────────────────────────────────────── */

function ResultValue({ item }: { item: ActivityItem }) {
  if (item.error) return <span className="runs-error-text">{item.error}</span>
  return <>{item.summary || '—'}</>
}

function timeRows(item: ActivityItem): [string, ReactNode][] {
  const rows: [string, ReactNode][] = []
  if (item.repeats > 1) {
    rows.push([
      'Ran',
      <span>
        {item.repeats} times with this outcome, first{' '}
        <span title={isoTitle(item.started_at)}>{dateTime(item.started_at)}</span>, last{' '}
        <span title={isoTitle(item.last_at)}>{dateTime(item.last_at)}</span>
      </span>,
    ])
  } else {
    rows.push(['Started', item.started_at ? <span title={isoTitle(item.started_at)}>{dateTime(item.started_at)}</span> : 'not yet'])
    rows.push(['Finished', item.ended_at ? <span title={isoTitle(item.ended_at)}>{dateTime(item.ended_at)}</span> : 'not yet'])
  }
  if (item.status === 'running') rows.push(['Running for', elapsedSince(item.started_at || item.at)])
  else if (item.duration_ms != null) rows.push([item.repeats > 1 ? 'Last run took' : 'Took', durationText(item.duration_ms)])
  return rows
}

export function AgentFacts({ item, onOpenChat }: { item: ActivityItem; onOpenChat?: (chat: string) => void }) {
  const rows: [string, ReactNode][] = [
    ['What', item.kind_label],
    ['Started by', item.trigger || '—'],
  ]
  if (item.playbook_name) rows.push(['Playbook', <Link to="/playbooks">{item.playbook_name}</Link>])
  if (item.chat) {
    rows.push([
      'Chat',
      <span className="row" style={{ gap: 'var(--s2)' }}>
        <span className="truncate">{item.title.replace(/^(Chat|Upload): /, '')}</span>
        {onOpenChat && (
          <button className="btn btn-sm" onClick={() => onOpenChat(item.chat!)}>
            Open chat
          </button>
        )}
      </span>,
    ])
  } else if (item.thread_id) {
    rows.push(['Thread', <span className="mono">{item.thread_id}</span>])
  }
  if (item.case_id != null) rows.push(['Case', <Link to={`/cases/${item.case_id}`}>Case #{item.case_id}</Link>])
  if (item.question) rows.push(['Asked', item.question])
  if (item.skills_used && item.skills_used.length) rows.push(['Skills used', <SkillChips skills={item.skills_used} />])
  if (item.created_at) rows.push(['Queued', <span title={isoTitle(item.created_at)}>{dateTime(item.created_at)}</span>])
  rows.push(...timeRows(item))
  if (item.step && item.status === 'running') rows.push(['Doing', item.step])
  rows.push(['Result', <ResultValue item={item} />])
  return <KeyValue rows={rows} />
}

export function JobFacts({ item }: { item: ActivityItem }) {
  const rows: [string, ReactNode][] = [
    ['What', item.kind_label],
    ['Started by', item.trigger || '—'],
  ]
  const ref = item.ref
  if (ref) {
    rows.push([
      'About',
      ref.to ? <Link to={ref.to}>{ref.label || refLabel(`${ref.type}:${ref.id}`)}</Link> : ref.label || refLabel(`${ref.type}:${ref.id}`),
    ])
  }
  if (item.task_run_id != null) {
    rows.push(['Agent run', <Link to={`/system/runs/${item.task_run_id}`}>Run #{item.task_run_id}</Link>])
  }
  rows.push(...timeRows(item))
  rows.push(['Result', item.summary || '—'])
  if (item.error) rows.push(['Error', <span className="runs-error-text">{item.error}</span>])
  return <KeyValue rows={rows} />
}

export function CountsTable({ counts }: { counts: Record<string, unknown> }) {
  const entries = countEntries(counts)
  if (!entries.length) return null
  return (
    <div className="runs-counts" data-counts>
      {entries.map(([k, v]) => (
        <div key={k} className="runs-count">
          <span className="field-label">{k}</span>
          <span className="mono">{truncate(v, 80)}</span>
        </div>
      ))}
    </div>
  )
}

/* ── Quick 019: the memory a run used, and what it learned ──────────────── */

function KindPill({ label }: { label: string }) {
  return (
    <span className="pill pill-signal nowrap runs-mem-kind">{label}</span>
  )
}

function Retired({ status, reason }: { status?: MemoryStatus | null; reason?: string | null }) {
  const label = retiredLabel(status)
  if (!label) return null
  return (
    <>
      {' '}
      <StatusPill tone="neutral" label={label} title={reason || undefined} />
    </>
  )
}

function MemRow({ pill, text, meta, retired }: { pill: string; text: ReactNode; meta?: ReactNode; retired?: boolean }) {
  return (
    <div className="row-top runs-mem-row">
      <KindPill label={pill} />
      <div className="grow" style={{ minWidth: 0 }}>
        <div className={`runs-mem-text${retired ? ' dim' : ''}`}>{text}</div>
        {meta ? <div className="runs-mem-meta">{meta}</div> : null}
      </div>
    </div>
  )
}

function MemGroup({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="runs-mem-group">
      <div className="field-label runs-mem-label">{title}</div>
      {children}
    </div>
  )
}

/** What fed one run's memory message and what it stored after (presentational). */
export function RunMemoryView({ memory, status, kind }: { memory: RunMemory; status?: string; kind?: string }) {
  const used = memory.recorded ? memory.used : null
  const stillGoing = status === 'running' || status === 'queued'
  return (
    <div data-run-memory>
      {!used ? (
        <div className="runs-mem-note" data-memory-none>
          {kind === 'playbook'
            ? 'Not recorded for playbook runs: a playbook reads knowledge-graph facts for its goal, separately from chat memory.'
            : stillGoing
              ? 'It gathers memory when it starts working on the message.'
              : 'No memory was recorded for this run. It was answered before the agent ran, or it ran before memory was recorded.'}
        </div>
      ) : (
        <>
          {used.lessons.length > 0 && (
            <MemGroup title="Lessons it was told to follow">
              {used.lessons.map((l, i) => (
                <MemRow
                  key={`l${l.id ?? i}`}
                  pill="Lesson"
                  text={l.text}
                  retired={!!retiredLabel(l.status)}
                  meta={
                    <>
                      {l.id != null && <span className="mono">#{l.id}</span>}
                      <Retired status={l.status} reason={l.status_reason} />
                    </>
                  }
                />
              ))}
            </MemGroup>
          )}
          {used.learnings.length > 0 && (
            <MemGroup title="Learnings">
              {used.learnings.map((l, i) => (
                <MemRow
                  key={`m${l.id ?? i}`}
                  pill={kindLabel(l.kind)}
                  text={l.text.replace(/^\[[^\]]*\]\s*/, '')}
                  retired={!!retiredLabel(l.status)}
                  meta={
                    <>
                      {l.id != null && <span className="mono">#{l.id}</span>}
                      {l.source ? <> · from {l.source}</> : null}
                      <Retired status={l.status} reason={l.status_reason} />
                    </>
                  }
                />
              ))}
            </MemGroup>
          )}
          {used.graph.length > 0 && (
            <MemGroup title="Knowledge graph">
              {used.graph.map((g, i) => (
                <MemRow key={`g${g.uuid ?? i}`} pill={graphKindLabel(g.kind)} text={g.text} meta={graphScopeLabel(g)} />
              ))}
            </MemGroup>
          )}
          {used.cases.length > 0 && (
            <MemGroup title="Open cases">
              {used.cases.map((c, i) => (
                <MemRow
                  key={`c${c.id ?? i}`}
                  pill="Case"
                  text={c.id != null ? <Link to={`/cases/${c.id}`}>{c.text}</Link> : c.text}
                />
              ))}
            </MemGroup>
          )}
          {(used.group.purpose || used.group.members) && (
            <MemGroup title="Group context">
              <div className="runs-mem-chips">
                {used.group.purpose && <span className="pill">what the group is for</span>}
                {used.group.members && <span className="pill">who is in the group</span>}
              </div>
            </MemGroup>
          )}
          {used.chars === 0 && (
            <div className="runs-mem-note" data-memory-empty>
              Nothing in memory matched this message, so the run started from the conversation alone.
            </div>
          )}
          {used.dropped > 0 && (
            <div className="runs-mem-note" data-memory-dropped>
              {plural(used.dropped, 'more item')} matched but did not fit the memory budget ({used.chars.toLocaleString()} of{' '}
              {used.budget_chars.toLocaleString()} characters used).
            </div>
          )}
        </>
      )}
      <MemGroup title="Learned from this run">
        {memory.stored.length === 0 ? (
          <div className="runs-mem-note" data-memory-stored="none">
            {stillGoing ? 'It stores what it learned after it replies.' : 'Nothing new was kept from this run.'}
          </div>
        ) : (
          memory.stored.map((s) => (
            <MemRow
              key={s.id}
              pill={kindLabel(s.kind)}
              text={s.content}
              retired={s.status !== 'active'}
              meta={
                <>
                  <span className="mono">#{s.id}</span>{' '}
                  <StatusPill
                    tone={s.outcome === 'new' ? 'settled' : 'neutral'}
                    label={s.outcome === 'new' ? 'new' : 'updated an earlier one'}
                  />
                  <Retired status={s.status} reason={s.status_reason} />
                </>
              }
            />
          ))
        )}
      </MemGroup>
    </div>
  )
}

function RunMemorySection({ id, status, kind }: { id: number; status?: string; kind?: string }) {
  const m = useResource(() => loadRunMemory(id), { interval: 15_000, deps: [id] })
  return (
    <Section title="Memory" meta={memoryMeta(m.data)} flush>
      {m.error && !m.data ? (
        <div style={{ padding: 'var(--s3)' }}>
          <ErrorNote message={m.error} onRetry={() => void m.reload()} />
        </div>
      ) : !m.data ? (
        <Loading label="Reading what it remembered" />
      ) : (
        <RunMemoryView memory={m.data} status={status} kind={kind} />
      )}
    </Section>
  )
}

function AgentDetail({ id }: { id: number }) {
  const ask = useAsk()
  const act = useAction()
  const run = useResource(() => get<ActivityItem>(`/api/activity/runs/${id}`), { interval: 5_000, deps: [id] })
  const tools = useResource(() => get<ToolCall[]>(`/api/runs/${id}/tools`), { interval: 10_000, deps: [id] })
  const artifacts = useResource(() => get<Artifact[]>(`/api/runs/${id}/artifacts`), { deps: [id] })
  const item = run.data

  return (
    <div className="stack">
      {run.error && !item && <ErrorNote message={run.error} onRetry={() => void run.reload()} />}
      {!item && run.loading && <Loading />}
      {item && (
        <Section title={item.title} meta={`Run #${item.id}`} actions={<StatusPill status={item.status} label={statusLabel(item.status)} />}>
          <AgentFacts item={item} onOpenChat={ask.openChat} />
        </Section>
      )}

      <RunTraceSection runId={id} />

      <RunMemorySection id={id} status={item?.status} kind={item?.kind} />

      <Section title="Tool calls" meta={tools.data ? `${tools.data.length}` : undefined} flush>
        <Async
          data={tools.data}
          error={tools.error}
          loading={tools.loading}
          reload={tools.reload}
          empty={
            <Empty title="No tools were called">
              It answered from the conversation alone, or it never got that far.
            </Empty>
          }
        >
          {(calls) => (
            <div>
              {calls.map((c, i) => (
                <div key={i} style={{ padding: 'var(--s3) var(--s4)', borderBottom: '1px solid var(--line)' }}>
                  <div className="row" style={{ gap: 'var(--s2)' }}>
                    <strong className="mono">{c.tool}</strong>
                    <span className="grow" />
                    <span className="mono dimmer" style={{ fontSize: 'var(--fs-micro)' }}>
                      {c.latency_ms != null ? `${c.latency_ms} ms` : ''}
                    </span>
                  </div>
                  <div className="stack-2" style={{ marginTop: 'var(--s2)' }}>
                    <div>
                      <span className="field-label">Asked for</span>
                      <pre className="code">{truncate(JSON.stringify(c.args, null, 2), 1500)}</pre>
                    </div>
                    <div>
                      <span className="field-label">Got back</span>
                      <pre className="code">{truncate(String(c.result_digest ?? ''), 1500)}</pre>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Async>
      </Section>

      {artifacts.data && artifacts.data.length > 0 && (
        <Section title="Files it produced" flush>
          <div>
            {artifacts.data.map((a) => (
              <div
                key={a.name}
                className="row"
                style={{ padding: 'var(--s3) var(--s4)', borderBottom: '1px solid var(--line)' }}
              >
                <span className="mono grow truncate">{a.name}</span>
                <span className="dimmer mono" style={{ fontSize: 'var(--fs-micro)' }}>
                  {bytes(a.bytes)}
                </span>
                <button
                  className="btn btn-sm"
                  disabled={act.pending}
                  onClick={() =>
                    void act.run(() =>
                      download(`/api/runs/${id}/artifacts/${encodeURIComponent(a.name)}`, a.name),
                    )
                  }
                >
                  <Download size={13} /> Download
                </button>
              </div>
            ))}
            {act.error && <div className="banner banner-broken" style={{ margin: 'var(--s3)' }}>{act.error}</div>}
          </div>
        </Section>
      )}
    </div>
  )
}

function JobDetail({ id }: { id: number }) {
  const job = useResource(() => get<ActivityItem>(`/api/activity/jobs/${id}`), {
    interval: 5_000,
    deps: [id],
  })
  const item = job.data
  if (job.error && !item) return <ErrorNote message={job.error} onRetry={() => void job.reload()} />
  if (!item) return <Loading />
  const hasCounts = countEntries(item.counts).length > 0
  return (
    <div className="stack">
      <Section title={item.title} meta="Background run" actions={<StatusPill status={item.status} label={statusLabel(item.status)} />}>
        <JobFacts item={item} />
      </Section>
      {hasCounts && (
        <Section title={item.repeats > 1 ? 'Numbers (latest, or totals where they add up)' : 'Numbers'}>
          <CountsTable counts={item.counts} />
        </Section>
      )}
      {item.trace_id && (
        <Section title="AI usage" flush>
          <UsageTrace trace={item.trace_id} />
        </Section>
      )}
    </div>
  )
}

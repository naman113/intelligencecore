import { useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import { get, post } from '../lib/api'
import { asks } from '../lib/ask'
import { mintPreview, readPublishDashboard, type PreviewResult } from '../lib/contracts'
import { currentLink, draftLink, payloadDraftLink, useDashboardsBase, versionLink } from '../lib/dashlinks'
import { ago, isoTitle } from '../lib/format'
import { useAction, useResource } from '../lib/hooks'
import { fromSpec, isChanged, layoutErrorText, toChanges, type LayoutState } from '../lib/layout'
import { runsHref } from '../lib/runs'
import type { DashboardDetail, DashboardListItem, DashboardVersion } from '../lib/ictypes'
import { useOwed } from '../lib/owed'
import type { Action } from '../lib/types'
import { useAsk } from '../components/AskPanel'
import { LayoutEditor } from '../components/LayoutEditor'
import { PublishDashboardPanel, SpecSummary } from '../components/payloads/PublishDashboard'
import { ConfirmButton, Empty, ErrorNote, Loading, PageHeader, StatusPill } from '../components/ui'
import './ic.css'

/* Dashboards: version history, links to the separate dashboards origin, rollback.
 * Page code never reads a version's source and never opens a version for
 * display. Creation goes through the agent. Gold is for pending drafts and
 * failing versions only. */

const REL = 'noopener noreferrer'

export function DashboardsListView({
  items,
  onAsk,
}: {
  items: DashboardListItem[]
  onAsk: () => void
}) {
  const askBtn = (
    <button className="btn btn-sm" onClick={onAsk}>
      Ask for a dashboard
    </button>
  )
  if (items.length === 0) {
    return (
      <Empty title="No dashboards yet" action={askBtn}>
        Ask the agent to build one and it will propose it for your approval.
      </Empty>
    )
  }
  return (
    <div className="ic-list">
      {items.map((d) => (
        <div className="ic-row" key={d.slug}>
          <Link className="ic-row-title" to={`/dashboards/${encodeURIComponent(d.slug)}`}>
            {d.title}
          </Link>
          <span className="ic-meta">
            {d.slug} · {d.current_version != null ? `v${d.current_version}` : 'not published yet'}{' '}
            {d.latest_status && <StatusPill status={d.latest_status} />} ·{' '}
            {d.last_refreshed_at ? `refreshed ${ago(d.last_refreshed_at)}` : 'not refreshed yet'}
          </span>
          {d.needs_conversion && <span className="ic-owed-line">needs conversion</span>}
          {d.current_last_error && <span className="ic-owed-line">{d.current_last_error}</span>}
        </div>
      ))}
    </div>
  )
}

export interface DashboardDetailProps {
  data: DashboardDetail
  base: string | null
  configured: boolean
  draftUrls: Record<number, string | null>
  previews: Record<number, PreviewResult>
  busy?: boolean
  error?: string | null
  rotatedLink?: string | null
  onRollback: (version: number) => void
  onRotate: () => void
  onFix: (version: number, error: string) => void
  /** Phase 18: ask the agent to convert an old HTML dashboard. */
  onConvert?: (version: number) => void
  /** Phase 18: true when the current version is HTML and legacy HTML is off. */
  needsConversion?: boolean
  onOpenDraft: (version: number) => void
  /** Phase 22: publish an operator-authored draft (the existing operator publish route). */
  onPublish?: (version: number) => void
  /** Phase 22: open Edit layout mode; the button shows only for a published spec v2 version. */
  onEditLayout?: () => void
  /** Phase 22: true while the layout editor itself is open (an open-failure banner does not count). */
  layoutOpen?: boolean
  /** Phase 22: the open layout editor, or the open-failure banner, rendered above the timeline. */
  editor?: ReactNode
  /** Phase 22: banner after a layout draft was saved. */
  savedNote?: { text: string; warnings: string[] } | null
}

/** True when the current published version is a spec v2 version (the only one the editor can change). */
export function canEditLayout(data: DashboardDetail): boolean {
  const cur = data.dashboard.current_version
  const v = data.versions.find((x) => x.version === cur)
  return !!v && v.status === 'published' && v.format === 'spec' && v.spec_version === 2
}

function Approver({ v }: { v: DashboardVersion }) {
  if (v.status !== 'published' && v.status !== 'retired') return null
  return <span className="ic-meta">{v.published_by_name ? `approved by ${v.published_by_name}` : 'approved'}</span>
}

function DraftLinks({ v, p }: { v: DashboardVersion; p: DashboardDetailProps }) {
  const pre = p.previews[v.version]
  const minted = pre && pre.available ? draftLink(p.base, pre.path, pre.token) : null
  const fromPayload = payloadDraftLink(p.draftUrls[v.version] ?? null, p.base, p.configured)
  const href = minted ?? fromPayload
  if (href) {
    return (
      <span className="btn-row">
        <a className="btn btn-sm" href={href} target="_blank" rel={REL}>
          Open draft
        </a>
        <button className="btn btn-sm" onClick={() => p.onOpenDraft(v.version)}>
          Fresh link
        </button>
      </span>
    )
  }
  if (pre && !pre.available) {
    return <span className="ic-meta">Draft preview opens once dashboard previews are enabled</span>
  }
  return (
    <button className="btn btn-sm" onClick={() => p.onOpenDraft(v.version)}>
      Open draft
    </button>
  )
}

export function DashboardDetailView(p: DashboardDetailProps) {
  const { dashboard, versions } = p.data
  const ordered = [...versions].sort((a, b) => b.version - a.version)
  const live = currentLink(p.base, dashboard.slug, dashboard.view_token)
  return (
    <div>
      <h2 className="panel-title">{dashboard.title}</h2>
      <p className="ic-meta">
        {dashboard.slug} ·{' '}
        {dashboard.current_version != null ? `current v${dashboard.current_version}` : 'not published yet'}
      </p>
      {p.error && (
        <div className="banner banner-broken">
          {p.error}
          {/People page/.test(p.error) && (
            <>
              {' '}
              <Link to="/people">open the People page</Link>
            </>
          )}
        </div>
      )}
      {p.rotatedLink && (
        <div className="banner">
          New link, shown once:{' '}
          <a href={p.rotatedLink} target="_blank" rel={REL}>
            {p.rotatedLink}
          </a>
        </div>
      )}
      {p.savedNote && (
        <div className="banner">
          {p.savedNote.text}
          {p.savedNote.warnings.length > 0 && (
            <ul>
              {p.savedNote.warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      {p.needsConversion && (
        <div className="banner banner-owed">
          This dashboard uses the old HTML format and is not shown to viewers until it is converted.{' '}
          <button
            className="btn btn-sm"
            onClick={() => p.onConvert?.(dashboard.current_version ?? ordered[0]?.version ?? 0)}
          >
            Ask the agent to convert
          </button>
        </div>
      )}
      <div className="btn-row">
        {live && (
          <a className="btn btn-sm" href={live} target="_blank" rel={REL}>
            Open live dashboard
          </a>
        )}
        <ConfirmButton onConfirm={p.onRotate} disabled={p.busy} className="btn btn-sm" confirmLabel="Rotate link?">
          Rotate link
        </ConfirmButton>
        <Link className="btn btn-quiet btn-sm" to={runsHref(`dashboard:${dashboard.id}`)}>
          Refresh runs
        </Link>
        {p.onEditLayout && !p.layoutOpen && canEditLayout(p.data) && (
          <button className="btn btn-sm" disabled={p.busy} onClick={p.onEditLayout}>
            Edit layout
          </button>
        )}
      </div>
      {p.editor}
      {!p.base && (
        <p className="ic-meta">
          <Link to="/system/setup">Set the dashboards address on Setup</Link> to open dashboards from here.
        </p>
      )}
      <div className="ic-timeline">
        {ordered.map((v) => {
          const link =
            v.status === 'published' || v.status === 'retired'
              ? versionLink(p.base, dashboard.slug, v.version, dashboard.view_token)
              : null
          return (
            <div key={v.version} className="ic-row">
              <span>
                <strong>v{v.version}</strong> <StatusPill status={v.status} />{' '}
                {v.format && <span className="pill">{v.format === 'spec' ? 'Spec' : 'HTML'}</span>} {v.title}
              </span>
              {v.format === 'spec' && (
                <SpecSummary
                  tilesAdded={v.tiles_added ?? []}
                  tilesRemoved={v.tiles_removed ?? []}
                  tilesChanged={v.tiles_changed ?? []}
                  hasTileDiff={(v.tiles_added ?? []).length + (v.tiles_removed ?? []).length + (v.tiles_changed ?? []).length > 0}
                  tiles={v.tiles ?? []}
                  measures={v.measures ?? []}
                  queryRefs={v.queries_used ?? []}
                />
              )}
              {v.change_note && <span className="ic-body">{v.change_note}</span>}
              <span className="ic-meta" title={isoTitle(v.created_at)}>
                {v.author} · {ago(v.created_at)}
              </span>
              <Approver v={v} />
              {v.last_error && (
                <div className="banner banner-owed">
                  {v.last_error}{' '}
                  <button className="btn btn-sm" onClick={() => p.onFix(v.version, v.last_error ?? '')}>
                    Ask the agent to fix
                  </button>
                </div>
              )}
              <span className="btn-row">
                {link && (
                  <a className="btn btn-sm" href={link} target="_blank" rel={REL}>
                    Open v{v.version}
                  </a>
                )}
                {!p.base && <span className="mono ic-meta">{v.path}</span>}
                {v.status === 'draft' && <DraftLinks v={v} p={p} />}
                {v.status === 'draft' && v.author === 'operator' && p.onPublish && (
                  <ConfirmButton
                    onConfirm={() => p.onPublish?.(v.version)}
                    disabled={p.busy}
                    className="btn btn-sm btn-primary"
                    confirmLabel="Publish?"
                  >
                    Publish
                  </ConfirmButton>
                )}
                {v.status === 'retired' && (
                  <ConfirmButton
                    onConfirm={() => p.onRollback(v.version)}
                    disabled={p.busy}
                    className="btn btn-sm"
                    confirmLabel="Roll back?"
                  >
                    Roll back to this
                  </ConfirmButton>
                )}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

export default function Dashboards() {
  const params = useParams()
  const slug = params.slug
  const { open } = useAsk()
  const owed = useOwed()
  const askNew = () => open(asks.dashboard(''))

  const list = useResource(() => get<DashboardListItem[]>('/api/dashboards?limit=100'), { interval: 30_000 })
  const items = list.data ?? []

  return (
    <div className="ic-page">
      <PageHeader
        title="Dashboards"
        lede="What the agent has built for you, with every version and who approved it."
        actions={
          <button className="btn btn-sm" onClick={askNew}>
            Ask for a dashboard
          </button>
        }
      />
      {list.error && <ErrorNote message={list.error} onRetry={() => void list.reload()} />}
      {list.loading && !list.data ? (
        <Loading label="Reading dashboards" />
      ) : slug ? (
        <div className="ic-split">
          <DashboardsListView items={items} onAsk={askNew} />
          <DashboardDetailContainer
            slug={slug}
            drafts={owed.dashboardDrafts}
            reloadOwed={owed.reload}
            reloadList={list.reload}
          />
        </div>
      ) : (
        <>
          {owed.dashboardDrafts.length > 0 && (
            <p className="ic-owed-line">{owed.dashboardDrafts.length} draft(s) waiting for your approval.</p>
          )}
          <DashboardsListView items={items} onAsk={askNew} />
        </>
      )}
    </div>
  )
}

function DashboardDetailContainer({
  slug,
  drafts,
  reloadOwed,
  reloadList,
}: {
  slug: string
  drafts: Action[]
  reloadOwed: () => Promise<void>
  reloadList: () => Promise<void>
}) {
  const { send } = useAsk()
  const owedNow = useOwed()
  const { base, configured } = useDashboardsBase()
  const act = useAction()
  const [previews, setPreviews] = useState<Record<number, PreviewResult>>({})
  const [rotated, setRotated] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const layoutAct = useAction()
  const [editing, setEditing] = useState<{ base: LayoutState; state: LayoutState; version: number } | null>(null)
  const [moveNotice, setMoveNotice] = useState<string | null>(null)
  const [saved, setSaved] = useState<{ text: string; warnings: string[] } | null>(null)

  const path = '/api/dashboards/' + encodeURIComponent(slug)
  const detail = useResource(() => get<DashboardDetail>(path), { interval: 30_000, deps: [slug] })

  const mine = drafts.filter((a) => readPublishDashboard(a.payload).slug === slug)
  const draftUrls: Record<number, string | null> = {}
  for (const a of mine) {
    const v = readPublishDashboard(a.payload)
    if (v.version != null && v.draftUrl) draftUrls[v.version] = v.draftUrl
  }

  const decide = (id: number, verb: 'approve' | 'reject') =>
    void act
      .run(() => post('/api/actions/' + id + '/' + verb))
      .then(async () => {
        await reloadOwed()
        await detail.reload()
        await reloadList()
      })

  const openEditor = () => {
    const cur = detail.data?.dashboard.current_version
    if (cur == null) return
    setSaved(null)
    layoutAct.clearError()
    void layoutAct
      .run(async () => {
        const v = await get<{ spec?: unknown }>(path + '/versions/' + cur)
        const st = fromSpec(v.spec)
        setMoveNotice(null)
        setEditing({ base: st, state: st, version: cur })
      })
  }
  const saveLayout = () => {
    if (!editing) return
    const body = toChanges(editing.state, editing.version, editing.base)
    void layoutAct
      .run(async () => {
        const r = await post<{ version: number; warnings?: { message?: string; fix?: string }[] }>(path + '/layout', body)
        setEditing(null)
        setSaved({
          text: `Saved draft v${r.version}. Publish it to show viewers.`,
          warnings: (r.warnings ?? []).map((w) => w.message ?? '').filter(Boolean),
        })
      })
      .then(async (ok) => {
        if (ok) await detail.reload()
      })
  }

  if (detail.error && !detail.data) return <ErrorNote message={detail.error} onRetry={() => void detail.reload()} />
  if (!detail.data) return <Loading label="Reading dashboard" />

  return (
    <div>
      {mine.map((a) => (
        <div className="card card-owed" key={a.id}>
          <PublishDashboardPanel action={a} />
          <div className="btn-row">
            <button className="btn btn-sm btn-primary" disabled={act.pending} onClick={() => decide(a.id, 'approve')}>
              Approve
            </button>
            <button className="btn btn-sm" disabled={act.pending} onClick={() => decide(a.id, 'reject')}>
              Reject
            </button>
          </div>
        </div>
      ))}
      {note && <div className="banner">{note}</div>}
      <DashboardDetailView
        data={detail.data}
        base={base}
        configured={configured}
        draftUrls={draftUrls}
        previews={previews}
        busy={act.pending}
        error={act.error}
        savedNote={saved}
        onPublish={(version) =>
          void act.run(() => post(path + '/publish', { version })).then(async (ok) => {
            if (ok) {
              setSaved(null)
              await detail.reload()
              await reloadList()
            }
          })
        }
        onEditLayout={openEditor}
        layoutOpen={editing != null}
        editor={
          editing ? (
            <LayoutEditor
              state={editing.state}
              changed={isChanged(editing.state, editing.base)}
              onChange={(next, n) => {
                setEditing({ ...editing, state: next })
                setMoveNotice(n ?? null)
              }}
              onSave={saveLayout}
              onCancel={() => {
                setEditing(null)
                layoutAct.clearError()
              }}
              busy={layoutAct.pending}
              error={layoutAct.error ? layoutErrorText(layoutAct.error) : null}
              notice={moveNotice}
            />
          ) : layoutAct.error ? (
            <div className="banner banner-broken">{layoutErrorText(layoutAct.error)}</div>
          ) : null
        }
        rotatedLink={rotated}
        onRollback={(version) =>
          void act.run(() => post(path + '/rollback', { version })).then(async (ok) => {
            if (ok) {
              await detail.reload()
              await reloadList()
            }
          })
        }
        onRotate={() =>
          void act
            .run(async () => {
              const r = await post<{ view_token?: string }>(path + '/rotate-token')
              const tok = r && typeof r.view_token === 'string' ? r.view_token : ''
              setRotated(currentLink(base, slug, tok))
            })
            .then(() => detail.reload())
        }
        onFix={(version) =>
          void send(asks.fixDashboard(slug, version)).then((ok) =>
            setNote(ok ? 'Sent to the agent. It will propose a fixed draft for your approval.' : null),
          )
        }
        needsConversion={owedNow.unconvertedDashboards.some((d) => d.slug === slug)}
        onConvert={(version) =>
          void send(asks.convertDashboard(slug, version)).then((ok) =>
            setNote(ok ? 'Sent to the agent. It will propose a converted draft for your approval.' : null),
          )
        }
        onOpenDraft={(version) =>
          void mintPreview(slug, version)
            .then((r) => setPreviews((s) => ({ ...s, [version]: r })))
            .catch(() => setPreviews((s) => ({ ...s, [version]: { available: false } })))
        }
      />
    </div>
  )
}

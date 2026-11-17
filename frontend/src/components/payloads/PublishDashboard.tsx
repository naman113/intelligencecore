import { useState } from 'react'
import { Link } from 'react-router-dom'
import { asObj, get } from '../../lib/api'
import {
  fetchVersionQueries,
  mintPreview,
  queryDiff,
  readPublishDashboard,
} from '../../lib/contracts'
import type { PreviewResult, PublishDashboardView, QueryDiff } from '../../lib/contracts'
import { draftLink, payloadDraftLink, useDashboardsBase } from '../../lib/dashlinks'
import type { DashboardDetail } from '../../lib/ictypes'
import type { Action } from '../../lib/types'

/* A dashboard draft waiting for approval. Neutral styling: the card around it
 * already carries the "owed" signal. Links go to the separate dashboards origin;
 * the draft's code is never fetched or shown here. */

const OUT = { target: '_blank', rel: 'noopener noreferrer' } as const

function Chips({ diff }: { diff: { added: string[]; removed: string[]; changed: string[] } }) {
  const none = !diff.added.length && !diff.removed.length && !diff.changed.length
  if (none) return <div className="dimmer">No query changes.</div>
  return (
    <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
      {diff.added.map((q) => (
        <span key={`a-${q}`} className="pill mono">{`+${q}`}</span>
      ))}
      {diff.removed.map((q) => (
        <span key={`r-${q}`} className="pill mono">{`−${q}`}</span>
      ))}
      {diff.changed.map((q) => (
        <span key={`c-${q}`} className="pill mono">{`~${q}`}</span>
      ))}
    </div>
  )
}

/* Phase 18: source label for a query ref. Declared SQL is unverified. */
export function sourceLabel(source: string): string {
  if (source === 'sql') return 'SQL, unverified'
  if (source === 'kpi_history') return 'KPI history'
  if (source === 'forecast') return 'Forecast'
  if (source === 'measure') return 'Measure'
  return source
}

export function SpecSummary({
  tilesAdded,
  tilesRemoved,
  tilesChanged,
  hasTileDiff,
  tiles,
  measures,
  queryRefs,
}: {
  tilesAdded: string[]
  tilesRemoved: string[]
  tilesChanged: string[]
  hasTileDiff: boolean
  tiles: { id: string; type: string; title: string | null }[]
  measures: string[]
  queryRefs: { id: string; source: string }[]
}) {
  return (
    <div className="stack-2">
      <div className="dimmer">Chart spec</div>
      {hasTileDiff &&
        (tilesAdded.length || tilesRemoved.length || tilesChanged.length ? (
          <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
            {tilesAdded.map((t) => (
              <span key={`ta-${t}`} className="pill mono">{`+${t}`}</span>
            ))}
            {tilesRemoved.map((t) => (
              <span key={`tr-${t}`} className="pill mono">{`−${t}`}</span>
            ))}
            {tilesChanged.map((t) => (
              <span key={`tc-${t}`} className="pill mono">{`~${t}`}</span>
            ))}
          </div>
        ) : (
          <div className="dimmer">No tile changes.</div>
        ))}
      {tiles.length > 0 && (
        <ul className="stack-2" style={{ margin: 0, paddingLeft: 18 }}>
          {tiles.map((t) => (
            <li key={t.id}>{`${t.type}: ${t.title ?? t.id}`}</li>
          ))}
        </ul>
      )}
      {measures.length > 0 && (
        <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
          <span className="dim">Measures</span>
          {measures.map((m) => (
            <span key={m} className="pill mono">{`${m} (governed)`}</span>
          ))}
        </div>
      )}
      {queryRefs.length > 0 && (
        <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
          <span className="dim">Queries</span>
          {queryRefs.map((q) => (
            <span key={q.id} className="pill mono">{`${q.id} · ${sourceLabel(q.source)}`}</span>
          ))}
        </div>
      )}
    </div>
  )
}

export function PublishDashboardDetail({
  view,
  diff,
  preview,
  base,
  configured,
  onOpenDraft,
  onShowDiff,
}: {
  view: PublishDashboardView
  diff: QueryDiff | null
  preview: PreviewResult | null
  base: string | null
  configured: boolean
  onOpenDraft?: () => void
  onShowDiff?: () => void
}) {
  if (view.op === 'publish' || view.op === 'rollback') {
    const line =
      view.op === 'rollback'
        ? `Rolled back to v${view.version ?? '?'}${view.fromVersion != null ? ` (was v${view.fromVersion})` : ''}`
        : `Published v${view.version ?? '?'}${view.fromVersion != null ? ` (replacing v${view.fromVersion})` : ''}`
    return (
      <div className="stack-2">
        <div>
          {line}
          {view.title ? ` · ${view.title}` : ''}
        </div>
        {(view.changeNote || view.note) && <div className="dim">{view.changeNote ?? view.note}</div>}
      </div>
    )
  }

  // Link priority: a freshly minted preview, then the payload's own URL, then a mint button.
  let href: string | null = null
  let fromPayload = false
  if (preview && preview.available && base) {
    href = draftLink(base, preview.path, preview.token)
  }
  if (!href) {
    href = payloadDraftLink(view.draftUrl, base, configured)
    fromPayload = href !== null
  }
  const notEnabled = !href && preview !== null && !preview.available

  const chips = view.hasQueryDiff
    ? { added: view.queriesAdded, removed: view.queriesRemoved, changed: view.queriesChanged }
    : diff

  return (
    <div className="stack-2">
      <div className="row" style={{ gap: 8, alignItems: 'baseline', flexWrap: 'wrap' }}>
        <strong>{view.title ?? view.slug ?? 'Dashboard'}</strong>
        <span className="mono dim">
          {view.version != null ? `v${view.version}` : ''} {view.slug ?? ''}
        </span>
      </div>
      {view.changeNote && <div className="dim">{view.changeNote}</div>}

      {(view.format === 'spec' || view.hasTileDiff) && (
        <SpecSummary
          tilesAdded={view.tilesAdded}
          tilesRemoved={view.tilesRemoved}
          tilesChanged={view.tilesChanged}
          hasTileDiff={view.hasTileDiff}
          tiles={view.tiles}
          measures={view.measures}
          queryRefs={view.queryRefs}
        />
      )}

      {chips ? (
        <Chips diff={chips} />
      ) : (
        <div>
          <button type="button" className="btn btn-quiet btn-sm" onClick={onShowDiff}>
            Show query changes
          </button>
        </div>
      )}

      <div className="row" style={{ gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        {href ? (
          <a className="btn btn-sm" href={href} {...OUT}>
            Open draft
          </a>
        ) : (
          <button type="button" className="btn btn-sm" onClick={onOpenDraft}>
            Open draft
          </button>
        )}
        {fromPayload && (
          <button type="button" className="btn btn-quiet btn-sm" onClick={onOpenDraft}>
            Fresh link
          </button>
        )}
      </div>

      {notEnabled && (
        <div className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>
          Draft preview opens once dashboard previews are enabled
          {view.draftPath ? (
            <>
              {' '}
              (<span className="mono">{view.draftPath}</span>)
            </>
          ) : null}
          .
        </div>
      )}
      {!href && !base && (
        <div className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>
          <Link to="/system/setup">Set the dashboards address on Setup</Link>
          {view.draftPath ? (
            <>
              {' '}
              <span className="mono">{view.draftPath}</span>
            </>
          ) : null}
        </div>
      )}
    </div>
  )
}

export function PublishDashboardPanel({ action }: { action: Action }) {
  const view = readPublishDashboard(action.payload)
  const { base, configured } = useDashboardsBase()
  const [preview, setPreview] = useState<PreviewResult | null>(null)
  const [diff, setDiff] = useState<QueryDiff | null>(null)
  const [error, setError] = useState<string | null>(null)

  if (view.op === 'propose' && (!view.slug || view.version == null)) {
    const { _summary, ...rest } = asObj<Record<string, unknown>>(action.payload)
    return (
      <div className="stack-2">
        {_summary != null && <div>{String(_summary)}</div>}
        <pre className="code">{JSON.stringify(rest, null, 2)}</pre>
      </div>
    )
  }

  const slug = view.slug ?? ''
  const version = view.version ?? 0

  async function openDraft() {
    setError(null)
    try {
      setPreview(await mintPreview(slug, version))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function showDiff() {
    setError(null)
    try {
      const detail = await get<DashboardDetail>(`/api/dashboards/${encodeURIComponent(slug)}`)
      const cur = detail.dashboard?.current_version ?? null
      const [draft, current] = await Promise.all([
        fetchVersionQueries(slug, version),
        cur != null ? fetchVersionQueries(slug, cur) : Promise.resolve([]),
      ])
      setDiff(queryDiff(draft, current))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="stack-2">
      <PublishDashboardDetail
        view={view}
        diff={diff}
        preview={preview}
        base={base}
        configured={configured}
        onOpenDraft={openDraft}
        onShowDiff={showDiff}
      />
      {error && <div className="banner banner-broken">{error}</div>}
    </div>
  )
}

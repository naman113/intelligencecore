import { plural } from '../../lib/format'
import type { SourceChangeView, SourceModeChangeView } from '../../lib/contracts'

/* A proposed data source, shown as what would be connected and how it would sync.
 * Text nodes only (names are agent-chosen); only the credential handle is shown, never a secret. */
export function SourceChangePanel({ view }: { view: SourceChangeView }) {
  if (view.modeChange) return <SourceModePanel view={view.modeChange} />  // Phase 17 (CONS-05)
  const isCsv = view.kind === 'csv'
  return (
    <div className="stack-2">
      <div>
        Connect {view.kind} source <span className="mono">{view.name}</span>
      </div>
      {isCsv ? (
        <div className="dim">
          CSV file <span className="mono">{view.csvFile ?? ''}</span> (sha{' '}
          <span className="mono">{(view.csvHash ?? '').slice(0, 12)}</span>)
          {view.csvOnReupload && <> · on re-upload: {view.csvOnReupload}</>}
        </div>
      ) : (
        <div className="dim">
          <span className="mono">{view.credential ?? ''}</span>
          {view.host && (
            <>
              {' '}· host <span className="mono">{view.host}</span>
            </>
          )}
          {view.database && (
            <>
              {' '}· database <span className="mono">{view.database}</span>
            </>
          )}
        </div>
      )}
      <div className="stack-2">
        {view.tables.map((t) => (
          <div key={`${t.remote}>${t.warehouse}`} className="stack-2">
            <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
              <span className="mono">{t.remote}</span>
              <span className="dimmer">→</span>
              <span className="mono">{t.warehouse}</span>
              <span className="pill">{t.mode}</span>
            </div>
            <div className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>
              cursor: {t.mode === 'live' ? 'n/a (read live)' : (t.cursor ?? 'none: replaced each run')}
              {t.mergeKey.length > 0 && <> · merge key: {t.mergeKey.join(', ')}</>}
              {' '}· about {t.estRows.toLocaleString('en-US')} rows, {t.estMb} MB
              {t.reconcile && <> · reconciled daily</>}
            </div>
            {t.cursor && t.cursorIndexed === false && (
              <div style={{ color: 'var(--gold, #b8860b)', fontSize: 'var(--fs-tiny)' }}>
                The cursor column {t.cursor} has no index, so each run may scan the whole table.
              </div>
            )}
          </div>
        ))}
      </div>
      {!isCsv && (
        <div className="dim">
          {view.backfillStart && <>Backfill from {view.backfillStart} · </>}
          every {view.intervalMinutes ?? '?'} min{view.window && <> within {view.window}</>} · up to{' '}
          {view.maxRows.toLocaleString('en-US')} rows / {Math.round(view.maxSeconds / 60)} min per run
        </div>
      )}
      <div className="dim">
        Estimated {view.estRows.toLocaleString('en-US')} rows, {view.estStorageMb} MB,{' '}
        {plural(view.estBackfillRuns, 'backfill run')}
      </div>
      <div className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>
        New columns appear; a type change fails that table.
      </div>
      {view.warnings.map((w) => (
        <div key={w} style={{ color: 'var(--gold, #b8860b)', fontSize: 'var(--fs-tiny)' }}>
          {w}
        </div>
      ))}
    </div>
  )
}

/* Phase 17 (CONS-05): a proposed storage mode switch. Text nodes only; approval is the existing
 * Approvals control, there is no button here. */
function SourceModePanel({ view }: { view: SourceModeChangeView }) {
  const sum = view.dependents.reduce(
    (a, d) => ({ w: a.w + d.watches, d: a.d + d.dashboards, i: a.i + d.insights, m: a.m + d.model }),
    { w: 0, d: 0, i: 0, m: 0 },
  )
  return (
    <div className="stack-2">
      <div>
        Switch storage mode for <span className="mono">{view.name}</span>
      </div>
      {view.tables.map((t, i) => (
        <div key={`${i}:${t.table}`} className="stack-2">
          <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
            <span className="mono">{t.table}</span>: <span className="pill">{t.from} -&gt; {t.to}</span>
          </div>
          <div className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>
            {t.to === 'import' && (
              <>
                cursor: {t.cursor ?? 'none: replaced each run'}
                {t.mergeKey.length > 0 && <> · key: {t.mergeKey.join(', ')} · </>}
              </>
            )}
            about {t.estRows.toLocaleString('en-US')} rows{t.estMb == null ? ', size unknown' : `, ${t.estMb} MB`}
          </div>
        </div>
      ))}
      {view.freshness && <div className="dim">{view.freshness}</div>}
      {view.notes.map((n) => (
        <div key={n} className="dim">{n}</div>
      ))}
      <div className="dim">
        Keeps working unchanged: {plural(sum.w, 'watch', 'watches')}, {plural(sum.d, 'dashboard')},{' '}
        {plural(sum.i, 'insight')}, {plural(sum.m, 'model object')}
      </div>
    </div>
  )
}

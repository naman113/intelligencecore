/* Quick 009: "Knowledge graph: connected / empty / not connected (reason)",
 * shown on Memory and Health, with Reconnect and a one-off copy of learnings
 * and closed cases that never reached the graph. */
import { plural } from '../lib/format'
import {
  backfillNote,
  describeGraph,
  extractionNote,
  rebuildNote,
  pendingTotal,
  reconnectGraph,
  startGraphBackfill,
  type GraphStatus,
} from '../lib/graph'
import { useAction } from '../lib/hooks'
import { StatusPill } from './ui'

export function GraphStatusLine({
  status,
  error,
  reload,
}: {
  status: GraphStatus | null
  error?: string | null
  reload?: () => void | Promise<void>
}) {
  const act = useAction()
  const line = describeGraph(status, error)
  const up = status?.state === 'connected' || status?.state === 'empty'
  const running = Boolean(status?.backfill?.running)
  const pending = pendingTotal(status)
  const note = backfillNote(status)
  const failedNote = extractionNote(status)
  const rebuilt = rebuildNote(status)
  const after = () => void reload?.()

  return (
    <div className="stack-3" data-graph-status={status?.state ?? 'unknown'}>
      <div className="btn-row" style={{ flexWrap: 'wrap' }}>
        <strong>Knowledge graph</strong>
        <StatusPill tone={line.tone} label={line.label} />
        <span className="dim" style={{ minWidth: 0 }}>
          {line.detail}
        </span>
        <span className="grow" />
        {status && status.state !== 'disabled' && (
          <button
            className="btn btn-sm"
            disabled={act.pending}
            onClick={() => void act.run(reconnectGraph).then(after)}
            title="Drop the connection and try again now"
          >
            {act.pending ? 'Working…' : 'Reconnect'}
          </button>
        )}
        {up && pending > 0 && (
          <button
            className="btn btn-sm"
            disabled={act.pending || running}
            onClick={() => void act.run(() => startGraphBackfill()).then(after)}
            title="Send learnings and closed cases written while the graph was down"
          >
            {running ? 'Copying…' : `Copy ${plural(pending, 'item')} into the graph`}
          </button>
        )}
      </div>
      {note && <span className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>{note}</span>}
      {rebuilt && <span className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>{rebuilt}</span>}
      {failedNote && (
        <span className="banner banner-broken" data-graph-extraction="failed">
          {failedNote}
        </span>
      )}
      {act.error && <span className="banner banner-broken">{act.error}</span>}
    </div>
  )
}

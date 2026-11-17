import { Link } from 'react-router-dom'
import { post } from '../lib/api'
import { runRoute } from '../lib/runs'
import { truncate } from '../lib/format'

/* Quick 023: "Run now" for a scheduled playbook. It queues the existing playbook on the
 * same worker queue as the nightly cron; nothing about the run itself changes.
 * RunNowView is presentational (rendered to static markup in tests); the pages own
 * the polling, because only they know which resource carries the run's status. */

export type RunStatus = 'queued' | 'running' | 'done' | 'failed' | string

export const isActiveRun = (s?: string | null): boolean => s === 'queued' || s === 'running'

export const runPlaybookNow = (playbookId: number) =>
  post<{ task_run_id: number }>(`/api/playbooks/${playbookId}/run`)

export function RunNowView({
  label = 'Run now',
  busyLabel = 'Running…',
  status,
  runId,
  pending = false,
  error = null,
  onRun,
}: {
  label?: string
  busyLabel?: string
  status?: RunStatus | null
  runId?: number | null
  pending?: boolean
  error?: string | null
  onRun: () => void
}) {
  const busy = pending || isActiveRun(status)
  return (
    <span className="runnow">
      <button className="btn btn-sm" disabled={busy} aria-busy={busy} onClick={onRun}>
        {busy ? busyLabel : label}
      </button>
      {busy && runId != null && (
        <Link className="runnow-link" to={runRoute({ source: 'agent', id: runId, uid: '' })}>
          See the run
        </Link>
      )}
      {error && (
        <span className="runnow-err" role="alert">
          {error}
        </span>
      )}
    </span>
  )
}

/** One short line for a failed run; the full text rides in the title. */
export function FailedRunNote({ error }: { error: string | null | undefined }) {
  const text = (error ?? '').trim()
  return (
    <span className="runnow-err" title={text || undefined}>
      Last run failed{text ? `: ${truncate(text, 120)}` : ''}. Run it again.
    </span>
  )
}

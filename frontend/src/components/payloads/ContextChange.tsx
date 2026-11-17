import { plural } from '../../lib/format'
import type { ContextChangeView } from '../../lib/contracts'

/* A proposed (or owner-made) change to the business context, shown as what it
 * replaces and what it becomes. Text nodes only: the text is agent-authored. */
export function ContextChangeDetail({ view }: { view: ContextChangeView }) {
  if (view.op === 'hide' || view.op === 'unhide') {
    const verb = view.op === 'hide' ? 'Hid' : 'Restored'
    return (
      <div className="stack-2">
        <div>
          {verb} {view.kind ?? 'item'} <span className="mono">{view.ref ?? ''}</span>
        </div>
        {view.rejectedCount > 0 && (
          <div className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>
            Cleared {plural(view.rejectedCount, 'pending proposal')}.
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="stack-2">
      <div className="row" style={{ gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <span className="pill">{view.kind ?? 'context'}</span>
        <span className="mono dim">{view.ref ?? ''}</span>
        {view.editedBy && <span className="pill">edited by you</span>}
      </div>
      <div className="stack-2">
        <div className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>
          Before
        </div>
        <blockquote className="code dim">{view.before ?? 'No description yet'}</blockquote>
      </div>
      <div className="stack-2">
        <div className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>
          After
        </div>
        <blockquote className="code">{view.text ?? ''}</blockquote>
      </div>
      {view.editedBy && view.agentText && (
        <details>
          <summary className="dimmer" style={{ fontSize: 'var(--fs-tiny)', cursor: 'pointer' }}>
            What the agent originally proposed
          </summary>
          <blockquote className="code dim">{view.agentText}</blockquote>
        </details>
      )}
    </div>
  )
}

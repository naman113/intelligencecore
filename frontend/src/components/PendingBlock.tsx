import { useState } from 'react'
import type { ContextPending } from '../lib/ictypes'

/* The gold body of a row that has a pending proposal: proposed text, the
 * current text as "Before", Approve / Reject / Edit. Agent text is rendered as
 * React text nodes only. */

export interface PendingHandlers {
  onApprove: (id: number) => void
  onReject: (id: number) => void
  onEdit: (id: number | null) => void
  onSave: (id: number, text: string) => void
}

// Phase 11: WR-10 (draft follows the proposal's action_id)
export function syncDraft(
  prev: { id: number; text: string },
  pending: ContextPending,
): { id: number; text: string } {
  if (prev.id === pending.action_id) return prev
  return { id: pending.action_id, text: pending.text ?? '' }
}

export function PendingBlock({
  pending,
  editing,
  busy,
  handlers,
}: {
  pending: ContextPending
  editing: number | null
  busy?: boolean
  handlers: PendingHandlers
}) {
  const id = pending.action_id
  const [draft, setDraft] = useState(() => ({ id: pending.action_id, text: pending.text ?? '' }))
  if (draft.id !== pending.action_id) setDraft(syncDraft(draft, pending))
  const text = draft.id === pending.action_id ? draft.text : (pending.text ?? '')
  const isEditing = editing === id
  return (
    <div className="ic-pending">
      <span className="btn-row">
        {pending.origin === 'agent' && <span className="pill">proposed by agent</span>}
        {pending.edited_by === 'owner' && <span className="pill">edited by you</span>}
      </span>
      {isEditing ? (
        <>
          <textarea
            className="textarea"
            aria-label="Edit proposed text"
            value={text}
            maxLength={1500}
            onChange={(e) => setDraft({ id, text: e.target.value })}
          />
          <span className="btn-row">
            <button className="btn btn-sm btn-primary" disabled={busy || !text.trim()} onClick={() => handlers.onSave(id, text)}>
              Save and approve
            </button>
            <button className="btn btn-sm" onClick={() => handlers.onEdit(null)}>
              Cancel
            </button>
          </span>
        </>
      ) : (
        <>
          <span className="ic-body">{pending.text ?? '(no text)'}</span>
          {pending.before && (
            <span className="ic-meta">
              Before: <span className="ic-body">{pending.before}</span>
            </span>
          )}
          <span className="btn-row">
            <button className="btn btn-sm btn-primary" disabled={busy} onClick={() => handlers.onApprove(id)}>
              Approve
            </button>
            <button className="btn btn-sm" disabled={busy} onClick={() => handlers.onReject(id)}>
              Reject
            </button>
            <button className="btn btn-sm" disabled={busy} onClick={() => handlers.onEdit(id)}>
              Edit
            </button>
          </span>
        </>
      )}
    </div>
  )
}

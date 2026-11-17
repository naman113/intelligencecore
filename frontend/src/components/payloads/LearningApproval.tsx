import { asObj } from '../../lib/api'

/* Quick 020: the approval card for a learning nobody could attribute.
 *
 * Something the assistant was told to remember in a group chat, by a sender
 * who is not registered, or during a run nobody asked for, is held: it is not
 * used until the owner approves it here. The card shows exactly what would be
 * remembered, where it came from, and where it would apply, because that is
 * the whole decision. */

export interface LearningApprovalView {
  learningId: number | null
  kind: string
  content: string
  origin: 'group' | 'sender' | 'other' | 'automatic' | string
  originLabel: string
  sender: string | null
  appliesTo: string
}

export function readLearningApproval(payload: unknown): LearningApprovalView {
  const p = asObj<Record<string, unknown>>(payload)
  const id = Number(p.learning_id)
  return {
    learningId: Number.isFinite(id) && id > 0 ? id : null,
    kind: String(p.kind || 'fact'),
    content: String(p.content ?? ''),
    origin: String(p.origin || 'sender'),
    originLabel: String(p.origin_label || 'a conversation nobody could attribute'),
    sender: p.sender ? String(p.sender) : null,
    appliesTo: String(p.applies_to || 'everyone'),
  }
}

export function learningApprovalTitle(v: LearningApprovalView): string {
  return `Remember this ${v.kind}?`
}

/** Where it came from, in one sentence. */
export function learningOrigin(v: LearningApprovalView): string {
  if (v.origin === 'group') return `Said in ${v.originLabel}, where the speaker is not known.`
  if (v.origin === 'automatic') return `Written during ${v.originLabel}.`
  if (v.origin === 'sender') return `From ${v.originLabel}, who is not registered on People.`
  return `From ${v.originLabel}, which is not attributed to a person.`
}

export function LearningApprovalDetail({ view }: { view: LearningApprovalView }) {
  return (
    <div className="stack-2" data-learning-approval={view.learningId ?? ''}>
      <blockquote className="code">{view.content}</blockquote>
      <div className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>
        {learningOrigin(view)}
        {view.sender && (
          <>
            {' '}
            <span className="mono">{view.sender}</span>
          </>
        )}
      </div>
      <div className="dim">
        It is not used until you decide. Approve: the assistant uses it from now on, for{' '}
        {view.appliesTo}. Reject: it is retired and never used.
      </div>
    </div>
  )
}

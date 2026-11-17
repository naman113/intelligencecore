import { get, post } from '../lib/api'
import { ago, isoTitle, phone as fmtPhone } from '../lib/format'
import { useAction, useResource } from '../lib/hooks'

/* Quick 017: messages the allowlist dropped, shown on Setup > WhatsApp with an
 * "Allow this sender" button. WhatsApp may hand the assistant a private ID
 * (LID) instead of a phone number, which no phone-number allowlist can match;
 * this is where the owner sees that and lets themselves (or anyone else) in.
 * Allowing is an owner action: the gateway adds the phone or private ID to
 * Allowed numbers through the ordinary config save path. Only the first few
 * characters of a blocked message are ever stored or shown. */

export interface BlockedSender {
  sender: string
  phone: string | null
  lid: string | null
  name: string | null
  snippet: string
  count: number
  first_at: string | null
  last_at: string | null
  chat_jid: string | null
  group_subject: string | null
}

export interface BlockedResponse {
  senders: BlockedSender[]
  allowlist_active: boolean
}

export const LID_HELP =
  "WhatsApp may hide a sender's number behind a private ID. If a message from you is blocked, allow it here."

/* What "Allow this sender" adds: the phone number when WhatsApp revealed it
 * (stable, and the private ID then maps to it), else the private ID. */
export function allowId(s: BlockedSender): string {
  return s.phone || s.lid || s.sender
}

export function senderLabel(s: BlockedSender): string {
  if (s.phone) return fmtPhone(s.phone)
  return `private ID ${s.lid || s.sender}`
}

export function allowSender(
  sender: string,
  send: (path: string, body: unknown) => Promise<unknown> = post,
): Promise<unknown> {
  return send('/api/whatsapp/blocked/allow', { sender })
}

export function BlockedSendersView({
  data,
  pending,
  onAllow,
}: {
  data: BlockedResponse | null
  pending?: boolean
  onAllow: (sender: string) => void
}) {
  const senders = data?.senders ?? []
  return (
    <div className="stack-2" data-blocked-senders="true">
      <div className="field-label">Messages blocked by the allowlist</div>
      <div className="dimmer" style={{ fontSize: 'var(--fs-micro)' }}>
        {LID_HELP}
      </div>
      {data && !data.allowlist_active && senders.length === 0 && (
        <div className="dim" style={{ fontSize: 'var(--fs-tiny)' }}>
          Allowed numbers is empty, so nobody is blocked.
        </div>
      )}
      {data && data.allowlist_active && senders.length === 0 && (
        <div className="dim" style={{ fontSize: 'var(--fs-tiny)' }}>
          Nothing blocked recently.
        </div>
      )}
      {senders.map((s) => (
        <div key={s.sender} className="row wrap" data-blocked={s.sender}>
          <div className="grow" style={{ minWidth: 200 }}>
            <div>
              <span className="mono">{senderLabel(s)}</span>
              {s.name ? <span className="dim">{` · ${s.name}`}</span> : null}
              {s.group_subject ? <span className="dim">{` · in ${s.group_subject}`}</span> : null}
            </div>
            <div className="dimmer" style={{ fontSize: 'var(--fs-micro)' }}>
              <span title={isoTitle(s.last_at)}>{ago(s.last_at)}</span>
              {s.count > 1 ? ` · ${s.count} messages` : ''}
              {s.snippet ? ` · “${s.snippet}”` : ''}
            </div>
          </div>
          <button
            className="btn btn-sm"
            data-allow={allowId(s)}
            disabled={pending}
            onClick={() => onAllow(allowId(s))}
          >
            Allow this sender
          </button>
        </div>
      ))}
    </div>
  )
}

export default function BlockedSenders({ onAllowed }: { onAllowed?: () => void }) {
  const blocked = useResource(() => get<BlockedResponse>('/api/whatsapp/blocked?limit=5'), {
    interval: 30_000,
  })
  const act = useAction()

  async function allow(sender: string) {
    const ok = await act.run(() => allowSender(sender))
    if (ok) {
      void blocked.reload()
      onAllowed?.()
    }
  }

  return (
    <>
      <BlockedSendersView data={blocked.data} pending={act.pending} onAllow={(s) => void allow(s)} />
      {act.error && <div className="banner banner-broken">{act.error}</div>}
    </>
  )
}

import { get } from '../lib/api'
import { phone as fmtPhone } from '../lib/format'
import { useResource } from '../lib/hooks'
import { Loading, PageHeader, Section } from '../components/ui'

/* Pairing lives here so nobody needs a terminal: the connector is loopback-only
 * on client VMs, so the QR is proxied through the gateway. The QR itself
 * rotates every ~20s connector-side; polling every 4s keeps the image fresh
 * and flips this page to "connected" moments after a successful scan. */

interface QrState {
  reachable: boolean
  connected: boolean
  qr: string | null
  user: { id: string; name: string | null } | null
}

export default function WhatsApp() {
  const { data, loading } = useResource(() => get<QrState>('/api/whatsapp/qr'), {
    interval: 4_000,
  })

  return (
    <>
      <PageHeader
        title="WhatsApp"
        lede="The assistant speaks through a linked WhatsApp account. Pair it once by scanning the code; the session survives restarts."
      />

      <Section title={data?.connected ? 'Linked and running' : 'Link a device'}>
        {!data && loading ? (
          <Loading label="Checking the connector" />
        ) : !data || !data.reachable ? (
          <div className="banner banner-broken">
            The WhatsApp connector is not reachable. Check that its container is
            running (Health page) and that the connector base URL is set on Setup.
          </div>
        ) : data.connected ? (
          <div className="stack-2">
            <p>
              ✅ Connected as <strong>{data.user?.name || 'linked account'}</strong>
              {data.user?.id && (
                <span className="mono dim"> · {fmtPhone(data.user.id.split(':')[0].split('@')[0])}</span>
              )}
            </p>
            <p className="dim">
              Messages flow. To move the assistant to a different number, log the
              current device out from that phone's WhatsApp → Linked devices, and
              this page will show a fresh code.
            </p>
          </div>
        ) : data.qr ? (
          <div className="stack-2" style={{ alignItems: 'center', textAlign: 'center' }}>
            <img
              src={data.qr}
              alt="WhatsApp pairing QR code"
              style={{ width: 320, maxWidth: '100%', borderRadius: 8, background: '#fff', padding: 8 }}
            />
            <p className="dim">
              On the phone that owns the assistant's number: WhatsApp → Settings →
              Linked devices → <strong>Link a device</strong>, then scan this code.
            </p>
            <p className="dimmer" style={{ fontSize: 'var(--fs-tiny)' }}>
              The code rotates every ~20 seconds — this page refreshes it automatically.
            </p>
          </div>
        ) : (
          <div className="stack-2">
            <p className="dim">
              <span className="spin" /> Waiting for the connector to issue a pairing
              code… this takes a few seconds after a restart or logout.
            </p>
          </div>
        )}
      </Section>
    </>
  )
}

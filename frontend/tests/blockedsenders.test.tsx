import { test, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import ReactDOMServer from 'react-dom/server'
import type { ReactElement, ReactNode } from 'react'
import {
  BlockedSendersView,
  LID_HELP,
  allowId,
  allowSender,
  senderLabel,
  type BlockedResponse,
  type BlockedSender,
} from '../src/components/BlockedSenders'
import { phone } from '../src/lib/format'

/* Quick 017: Setup > WhatsApp "Messages blocked by the allowlist". Numbers are made up. */

const lidOnly: BlockedSender = {
  sender: '987650000054321', phone: null, lid: '987650000054321', name: 'Someone',
  snippet: 'hi', count: 2, first_at: '2026-10-26T08:00:00Z', last_at: '2026-10-26T08:05:00Z',
  chat_jid: null, group_subject: null,
}
const withPhone: BlockedSender = {
  sender: '987650000011111', phone: '15550001111', lid: '987650000011111', name: null,
  snippet: 'are you there…', count: 1, first_at: null, last_at: '2026-10-26T08:01:00Z',
  chat_jid: '120363000000000000@g.us', group_subject: 'Ops',
}
const data: BlockedResponse = { senders: [lidOnly, withPhone], allowlist_active: true }

const html = (d: BlockedResponse | null) =>
  ReactDOMServer.renderToStaticMarkup(<BlockedSendersView data={d} onAllow={() => {}} />)

function buttons(node: ReactNode, out: ReactElement[] = []): ReactElement[] {
  if (Array.isArray(node)) {
    node.forEach((n) => buttons(n, out))
  } else if (node && typeof node === 'object' && 'props' in (node as ReactElement)) {
    const el = node as ReactElement<{ children?: ReactNode; onClick?: () => void }>
    if (el.type === 'button') out.push(el)
    buttons(el.props.children, out)
  }
  return out
}

const realFetch = globalThis.fetch
afterEach(() => {
  globalThis.fetch = realFetch
})

test('lists blocked senders with the plain-words help and one Allow button each', () => {
  const h = html(data)
  assert.match(h, /Messages blocked by the allowlist/)
  assert.ok(h.includes(ReactDOMServer.renderToStaticMarkup(<>{LID_HELP}</>)))
  assert.match(LID_HELP, /WhatsApp may hide a sender&?.*number behind a private ID\. If a message from you is blocked, allow it here\./)
  assert.match(h, /private ID 987650000054321/)
  assert.match(h, /\+1 55500 01111/)
  assert.match(h, /in Ops/)
  assert.match(h, /2 messages/)
  assert.match(h, /“hi”/)
  assert.equal(h.match(/Allow this sender/g)?.length, 2)
  assert.match(h, /data-allow="987650000054321"/)
  assert.match(h, /data-allow="15550001111"/, 'a known phone is what gets allowed')
})

test('empty states say why nothing is listed', () => {
  assert.match(html({ senders: [], allowlist_active: true }), /Nothing blocked recently/)
  assert.match(html({ senders: [], allowlist_active: false }), /Allowed numbers is empty, so nobody is blocked/)
  assert.doesNotMatch(html(null), /Allow this sender/)
})

test('the Allow button hands the sender id to onAllow', () => {
  const seen: string[] = []
  const tree = BlockedSendersView({ data, onAllow: (s) => seen.push(s) })
  const bs = buttons(tree)
  assert.equal(bs.length, 2)
  bs.forEach((b) => (b.props as { onClick: () => void }).onClick())
  assert.deepEqual(seen, ['987650000054321', '15550001111'])
  const off = buttons(BlockedSendersView({ data, pending: true, onAllow: () => {} }))
  assert.ok(off.every((b) => (b.props as { disabled?: boolean }).disabled), 'disabled while a save runs')
})

test('allowSender posts the id to the owner route', async () => {
  const calls: [string, unknown][] = []
  await allowSender('987650000054321', async (path, body) => {
    calls.push([path, body])
    return { allowed: '987650000054321', changed: true }
  })
  assert.deepEqual(calls, [['/api/whatsapp/blocked/allow', { sender: '987650000054321' }]])

  let seen: { url: string; init?: RequestInit } | null = null
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    seen = { url: String(url), init }
    return new Response(JSON.stringify({ allowed: '1', changed: true }), {
      status: 200, headers: { 'content-type': 'application/json' },
    })
  }) as typeof fetch
  await allowSender('15550001111')
  assert.ok(seen)
  const s = seen as { url: string; init?: RequestInit }
  assert.match(s.url, /\/api\/whatsapp\/blocked\/allow$/)
  assert.equal(s.init?.method, 'POST')
  assert.deepEqual(JSON.parse(String(s.init?.body)), { sender: '15550001111' })
})

test('labels and ids', () => {
  assert.equal(allowId(lidOnly), '987650000054321')
  assert.equal(allowId(withPhone), '15550001111')
  assert.equal(senderLabel(lidOnly), 'private ID 987650000054321')
  assert.equal(phone('987650000054321@lid'), 'private ID 987650000054321')
  assert.equal(phone('15550001111'), '+1 55500 01111')
})

test('Setup shows the list on the WhatsApp card only', () => {
  const src = fs.readFileSync('src/pages/Setup.tsx', 'utf8')
  assert.ok(src.includes("{group === 'whatsapp' && <BlockedSenders onAllowed={onSaved} />}"))
})

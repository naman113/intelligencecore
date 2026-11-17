import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { renderToStaticMarkup } from 'react-dom/server'
import { AskThreadView, FileCard } from '../src/components/AskPanel'
import { download, setKey } from '../src/lib/api'
import { fileTypeLabel, whatsappNote, type AskAttachment, type AskThread } from '../src/lib/ask'

/* Quick 007: files the agent delivers show under the reply as a download card,
 * the way WhatsApp users receive them. */

const file = (over: Partial<AskAttachment> = {}): AskAttachment => ({
  name: 'Fleet_Details_Report.xlsx',
  run_id: 10,
  kind: 'spreadsheet',
  mimetype: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  bytes: 6144,
  available: true,
  url: '/api/runs/10/artifacts/Fleet_Details_Report.xlsx',
  whatsapp: null,
  whatsapp_to: null,
  ...over,
})

const thread = (attachments: AskAttachment[]): AskThread => ({
  thread_id: 'ui:owner:yzlzcvgkih',
  messages: [
    { id: 1, role: 'user', content: 'Fleet report in Excel please', created_at: '' },
    { id: 2, role: 'assistant', content: 'The fleet report is attached.', created_at: '', attachments },
  ],
  pending: false,
  last_run: null,
})

test('reply shows a file card with type, name, size and Download', () => {
  const h = renderToStaticMarkup(<AskThreadView thread={thread([file()])} />)
  assert.ok(h.includes('ask-files') && h.includes('ask-file'))
  assert.ok(h.includes('Fleet_Details_Report.xlsx'))
  assert.ok(h.includes('XLSX') && h.includes('6.0 kB'))
  assert.ok(h.includes('Download') && h.includes('aria-label="Download Fleet_Details_Report.xlsx"'))
  assert.ok(h.includes('data-kind="spreadsheet"'))
  // the card sits inside the assistant bubble, after its text
  assert.ok(h.indexOf('ask-msg ask-msg-assistant') < h.indexOf('ask-files'))
  assert.ok(h.indexOf('The fleet report is attached.') < h.indexOf('ask-files'))
  // never a bare link: the artifact route needs the ops key
  assert.ok(!h.includes('href="/api/runs'))
})

test('a removed file says no longer available and offers no download', () => {
  const h = renderToStaticMarkup(<FileCard file={file({ available: false, url: null, bytes: null })} />)
  assert.ok(h.includes('No longer available') && h.includes('is-gone'))
  assert.ok(!h.includes('Download'))
})

test('WhatsApp line only once the WhatsApp send worked', () => {
  assert.ok(renderToStaticMarkup(<FileCard file={file({ whatsapp: 'sent', whatsapp_to: 'you' })} />).includes(
    'Also sent to WhatsApp',
  ))
  assert.ok(
    renderToStaticMarkup(<FileCard file={file({ whatsapp: 'sent', whatsapp_to: 'Priya' })} />).includes(
      'Also sent to Priya on WhatsApp',
    ),
  )
  assert.ok(!renderToStaticMarkup(<FileCard file={file({ whatsapp: 'failed' })} />).includes('WhatsApp'))
  assert.ok(!renderToStaticMarkup(<FileCard file={file()} />).includes('WhatsApp'))
  assert.equal(whatsappNote({ whatsapp: 'sent', whatsapp_to: null }), 'Also sent to WhatsApp')
  assert.equal(whatsappNote({ whatsapp: null, whatsapp_to: 'you' }), null)
})

test('icons and labels by type; user messages never show cards', () => {
  assert.equal(fileTypeLabel('report.pdf'), 'PDF')
  assert.equal(fileTypeLabel('notes'), 'FILE')
  const pdf = renderToStaticMarkup(<FileCard file={file({ name: 'r.pdf', kind: 'pdf' })} />)
  assert.ok(pdf.includes('PDF') && pdf.includes('data-kind="pdf"'))
  const t = thread([])
  t.messages[0].attachments = [file()]
  t.messages[1].attachments = []
  const h = renderToStaticMarkup(<AskThreadView thread={t} />)
  assert.ok(!h.includes('ask-file'))
})

test('download fetches with the ops key and saves a blob', async () => {
  const seen: { url: string; key: string | null }[] = []
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    seen.push({ url: String(url), key: new Headers(init?.headers).get('x-ops-key') })
    return { ok: true, status: 200, blob: async () => new Blob(['PK']) }
  }) as unknown as typeof fetch
  const g = globalThis as unknown as Record<string, unknown>
  const hadDoc = 'document' in g
  const saved: string[] = []
  if (!hadDoc)
    g.document = {
      createElement: () => ({
        set download(v: string) {
          saved.push(v)
        },
        href: '',
        click() {},
        remove() {},
      }),
      body: { appendChild() {} },
    }
  const create = URL.createObjectURL
  const revoke = URL.revokeObjectURL
  URL.createObjectURL = () => 'blob:x'
  URL.revokeObjectURL = () => {}
  try {
    setKey('k-123')
    await download('/api/runs/10/artifacts/Fleet_Details_Report.xlsx', 'Fleet_Details_Report.xlsx')
    assert.deepEqual(seen, [{ url: '/api/runs/10/artifacts/Fleet_Details_Report.xlsx', key: 'k-123' }])
    if (!hadDoc) assert.deepEqual(saved, ['Fleet_Details_Report.xlsx'])
  } finally {
    URL.createObjectURL = create
    URL.revokeObjectURL = revoke
    if (!hadDoc) delete g.document
  }
})

test('file card styles stay on the system channel (no gold)', () => {
  const css = readFileSync('src/components/ask.css', 'utf8')
  const block = css.slice(css.indexOf('Quick 007'))
  assert.ok(block.includes('.ask-file'))
  assert.ok(!/--owed/.test(block))
})

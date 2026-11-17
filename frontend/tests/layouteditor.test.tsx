import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ReactDOMServer from 'react-dom/server'
import { LayoutEditor } from '../src/components/LayoutEditor'
import { fromSpec } from '../src/lib/layout'

const spec = {
  date_control: { default: 'last_7_days', presets: ['last_7_days', 'last_30_days'] },
  sections: [{ title: 'Top', tiles: ['a', 'b'] }, { tiles: ['c'] }],
  tiles: [
    { id: 'a', type: 'stat', title: 'Kg in' },
    { id: 'b', type: 'paired_bar', title: 'In and out', hidden: true },
    { id: 'c', type: 'table', title: 'Rows' },
  ],
}
const noop = () => {}
function render(o: Record<string, unknown> = {}, sp: unknown = spec) {
  return ReactDOMServer.renderToStaticMarkup(
    <LayoutEditor state={fromSpec(sp)} onChange={noop} onSave={noop} onCancel={noop} {...o} />,
  )
}

test('sections, cards, labels, widths and controls', () => {
  const html = render()
  assert.equal((html.match(/class="lt-section"/g) ?? []).length, 2)
  assert.equal((html.match(/class="lt-card /g) ?? []).length, 3)
  assert.match(html, /maxLength="60"|maxlength="60"/i)
  assert.match(html, /maxlength="120"/i)
  assert.match(html, />Stat</)
  assert.match(html, />In and out bars</)
  assert.match(html, />Quarter</)
  assert.match(html, />Two thirds</)
  assert.match(html, /aria-label="Move up"/)
  assert.match(html, /aria-label="Move down"/)
  assert.match(html, /Show/)
  assert.match(html, /Move to section/)
  assert.match(html, /lt-w-quarter/)
  assert.match(html, /lt-hidden/)
  assert.match(html, />Hidden</)
})

test('first move up and last move down are disabled', () => {
  const cards = render().split('class="lt-card ').slice(1)
  assert.match(cards[0], /aria-label="Move up"[^>]*disabled|disabled=""[^>]*aria-label="Move up"/)
  assert.doesNotMatch(cards[0], /aria-label="Move down"[^>]*disabled/)
  assert.match(cards[2], /aria-label="Move down"[^>]*disabled|disabled=""[^>]*aria-label="Move down"/)
})

test('footer, default period only with a date control', () => {
  const html = render()
  assert.match(html, /Save as draft/)
  assert.match(html, />Cancel</)
  assert.match(html, /Layout only\. Ask the agent to change numbers or charts\./)
  assert.match(html, /Default period/)
  assert.match(html, /Last 30 days/)
  assert.doesNotMatch(html, /Last 90 days/)
  assert.doesNotMatch(render({}, { ...spec, date_control: undefined }), /Default period/)
})

test('same controls at every width: no drag markup, no gold', () => {
  assert.doesNotMatch(render(), /draggable/)
  assert.equal(readFileSync('src/components/LayoutEditor.tsx', 'utf8').includes('draggable'), false)
  assert.equal(readFileSync('src/components/layout-editor.css', 'utf8').includes('owed'), false)
})

test('error, notice, busy and unchanged save', () => {
  const html = render({ error: 'widths.a: bad', notice: 'A section needs at least one tile.', busy: true })
  assert.match(html, /banner banner-broken">widths\.a: bad/)
  assert.match(html, /at least one tile/)
  assert.match(html, /<button[^>]*disabled[^>]*>Save as draft/)
  assert.match(render({ changed: false }), /<button[^>]*disabled[^>]*>Save as draft/)
})

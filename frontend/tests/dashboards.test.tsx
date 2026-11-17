import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ReactDOMServer from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { DashboardDetailView, DashboardsListView, type DashboardDetailProps } from '../src/pages/Dashboards'
import { dashboardDetail, dashboards } from './fixtures'

const noop = () => {}
function detail(o: Partial<DashboardDetailProps> = {}) {
  return ReactDOMServer.renderToStaticMarkup(
    <MemoryRouter>
      <DashboardDetailView
        data={dashboardDetail}
        base="https://d.x"
        configured
        draftUrls={{}}
        previews={{}}
        onRollback={noop}
        onRotate={noop}
        onFix={noop}
        onOpenDraft={noop}
        {...o}
      />
    </MemoryRouter>,
  )
}

test('list: rows, versions, freshness, owed error, ask button', () => {
  const html = ReactDOMServer.renderToStaticMarkup(
    <MemoryRouter>
      <DashboardsListView items={dashboards} onAsk={noop} />
    </MemoryRouter>,
  )
  assert.match(html, /href="\/dashboards\/sales"/)
  assert.match(html, /v3/)
  assert.match(html, /not refreshed yet/)
  assert.match(html, /refreshed /)
  assert.match(html, /ic-owed-line">query timed out/)
  const empty = ReactDOMServer.renderToStaticMarkup(
    <MemoryRouter>
      <DashboardsListView items={[]} onAsk={noop} />
    </MemoryRouter>,
  )
  assert.match(empty, /No dashboards yet/)
  assert.match(empty, /Ask for a dashboard/)
})

test('timeline: newest first, change notes, approver or plain approved', () => {
  const html = detail()
  assert.ok(html.indexOf('<strong>v4') < html.indexOf('<strong>v3') && html.indexOf('<strong>v3') < html.indexOf('<strong>v1'))
  assert.match(html, /Regional split/)
  assert.match(html, /approved by Sam Owner/)
  assert.match(html, />approved</)
})

test('links: published and retired carry token, anchors are safe', () => {
  const html = detail()
  assert.match(html, /href="https:\/\/d\.x\/d\/sales\/v\/3\/\?t=viewtoken123"/)
  assert.match(html, /href="https:\/\/d\.x\/d\/sales\/v\/2\/\?t=viewtoken123"/)
  assert.match(html, /href="https:\/\/d\.x\/d\/sales\/\?t=viewtoken123"/)
  for (const a of html.match(/<a [^>]*target="_blank"[^>]*>/g) ?? []) assert.match(a, /rel="noopener noreferrer"/)
  assert.ok(!/v\/4\/\?t=/.test(html))
})

test('rollback only on retired versions', () => {
  const html = detail()
  assert.equal((html.match(/Roll back to this/g) ?? []).length, 2)
})

test('last_error is gold with a fix button', () => {
  const d = structuredClone(dashboardDetail)
  d.versions[1].last_error = 'query boom'
  const html = detail({ data: d })
  assert.match(html, /banner-owed">query boom/)
  assert.match(html, /Ask the agent to fix/)
})

test('draft row: payload url, mint button, previews disabled note', () => {
  const withUrl = detail({ draftUrls: { 4: '/d/sales/v/4/?p=seedtoken' } })
  assert.match(withUrl, /href="https:\/\/d\.x\/d\/sales\/v\/4\/\?p=seedtoken"/)
  assert.match(withUrl, /Fresh link/)
  const bare = detail()
  assert.match(bare, /<button[^>]*>Open draft<\/button>/)
  const off = detail({ previews: { 4: { available: false } } })
  assert.match(off, /Draft preview opens once dashboard previews are enabled/)
  const minted = detail({ previews: { 4: { available: true, path: '/d/sales/v/4/', token: 'mint1', expiresAt: null } } })
  assert.match(minted, /v\/4\/\?p=mint1/)
})

test('no base: paths as text and a Setup link', () => {
  const html = detail({ base: null, configured: false })
  assert.match(html, /\/d\/sales\/v\/3\//)
  assert.match(html, /href="\/system\/setup"/)
  assert.ok(!/\?t=/.test(html))
})

test('error: 422 links to People, 409 verbatim', () => {
  assert.match(detail({ error: 'Add yourself on the People page and mark yourself primary first, so the change records who decided.' }), /href="\/people"/)
  const h = detail({ error: 'version not retired' })
  assert.match(h, /version not retired/)
  assert.ok(!/href="\/people"/.test(h))
})

test('rotated link shown once from props', () => {
  assert.match(detail({ rotatedLink: 'https://d.x/d/sales/?t=new' }), /New link, shown once/)
})

test('source: no raw html, no version code, no /versions post', () => {
  const src = readFileSync('src/pages/Dashboards.tsx', 'utf8')
  assert.ok(!src.includes('dangerouslySetInnerHTML'))
  assert.ok(!src.includes('iframe'))
  assert.ok(!/post\([^)]*\/versions/.test(src))
  assert.ok(!/\.code\b/.test(src))
  assert.ok(src.includes('payloadDraftLink') && src.includes('/rollback') && src.includes('rotate-token'))
})

test('Phase 18: spec pills, tile list, chips, needs-conversion banner', () => {
  const data = {
    ...dashboardDetail,
    versions: dashboardDetail.versions.map((v, i) =>
      i === 0
        ? {
            ...v,
            format: 'spec' as const,
            tiles: [{ id: 't1', type: 'bar', title: 'Sales', binding: 'query' as const, ref: 'q1' }],
            tiles_added: ['t1'],
            measures: ['revenue'],
            queries_used: [{ id: 'q1', source: 'sql' }],
          }
        : { ...v, format: 'html' as const },
    ),
  }
  const html = detail({ data })
  assert.match(html, />Spec</)
  assert.match(html, />HTML</)
  assert.match(html, /bar: Sales/)
  assert.match(html, /\+t1/)
  assert.match(html, /SQL, unverified/)
  assert.ok(!html.includes('Ask the agent to convert'))
  const conv = detail({ needsConversion: true, onConvert: noop })
  assert.match(conv, /Ask the agent to convert/)
  assert.match(conv, /old HTML format/)
  const list = ReactDOMServer.renderToStaticMarkup(
    <MemoryRouter>
      <DashboardsListView items={[{ ...dashboards[0]!, needs_conversion: true }]} onAsk={noop} />
    </MemoryRouter>,
  )
  assert.match(list, /needs conversion/)
})

test('Phase 22: operator drafts show Publish, agent drafts do not', () => {
  const op = structuredClone(dashboardDetail)
  op.versions[0].author = 'operator'
  const html = detail({ data: op, onPublish: noop })
  assert.match(html, />Publish</)
  assert.doesNotMatch(detail({ onPublish: noop }), />Publish</)
})

test('Phase 22: Edit layout only for a published spec v2 current version', () => {
  assert.doesNotMatch(detail({ onEditLayout: noop }), /Edit layout/)
  const v2 = structuredClone(dashboardDetail)
  const cur = v2.versions.find((v) => v.version === v2.dashboard.current_version)!
  cur.format = 'spec'
  cur.spec_version = 2
  assert.match(detail({ data: v2, onEditLayout: noop }), /Edit layout/)
  assert.doesNotMatch(detail({ data: v2 }), /Edit layout/)
  // an open-failure banner in the editor slot must not hide the button (retry), the open editor does
  const banner = <div className="banner banner-broken">boom</div>
  assert.match(detail({ data: v2, onEditLayout: noop, editor: banner }), /Edit layout/)
  assert.doesNotMatch(detail({ data: v2, onEditLayout: noop, editor: banner, layoutOpen: true }), /Edit layout/)
  cur.spec_version = 1
  assert.doesNotMatch(detail({ data: v2, onEditLayout: noop }), /Edit layout/)
})

test('Phase 22: saved note lists warnings', () => {
  const html = detail({ savedNote: { text: 'Saved draft v5. Publish it to show viewers.', warnings: ['Tile too wide'] } })
  assert.match(html, /Saved draft v5\. Publish it to show viewers\./)
  assert.match(html, /<li>Tile too wide<\/li>/)
})

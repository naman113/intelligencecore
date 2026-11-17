import { test } from 'node:test'
import assert from 'node:assert/strict'
import ReactDOMServer from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { FailedRunNote, RunNowView, isActiveRun } from '../src/components/RunNow'
import { SummaryStrip } from '../src/pages/InsightsPage'

const html = (el: React.ReactElement) =>
  ReactDOMServer.renderToStaticMarkup(<MemoryRouter>{el}</MemoryRouter>)

const base = { days: 30, bySeverity: { critical: 0, warning: 0, info: 0 }, openCases: 0 }
const run = (status: string, error: string | null = null) => ({
  id: 24, status, startedAt: '2026-10-26T12:54:00Z', endedAt: null, error,
})

test('active means queued or running', () => {
  assert.ok(isActiveRun('queued') && isActiveRun('running'))
  assert.ok(!isActiveRun('done') && !isActiveRun('failed') && !isActiveRun(null))
})

test('idle: an enabled Run exploration now button, no run link', () => {
  const h = html(
    <SummaryStrip s={{ ...base, lastRun: run('done'), playbook: { id: 3, status: 'active' } }} run={{ onRun: () => {} }} />,
  )
  assert.match(h, /<button class="btn btn-sm" aria-busy="false">Run exploration now<\/button>/)
  assert.ok(!h.includes('See the run'))
})

test('busy: Exploring…, disabled, with a link to the run', () => {
  for (const status of ['queued', 'running']) {
    const h = html(
      <SummaryStrip s={{ ...base, lastRun: run(status), playbook: { id: 3, status: 'active' } }} run={{ onRun: () => {} }} />,
    )
    assert.match(h, /<button class="btn btn-sm" disabled="" aria-busy="true">Exploring…<\/button>/)
    assert.match(h, /href="\/system\/runs\/24"[^>]*>See the run</)
  }
})

test('pending click is busy before the run shows up', () => {
  const h = html(<RunNowView status="done" pending onRun={() => {}} />)
  assert.match(h, /disabled=""/)
})

test('failed run: the error is visible and a re-run is offered', () => {
  const h = html(
    <SummaryStrip
      s={{ ...base, lastRun: run('failed', 'RateLimitError: 429 You have no credits'), playbook: { id: 3, status: 'active' } }}
      run={{ onRun: () => {} }}
    />,
  )
  assert.match(h, /\(failed\)/)
  assert.match(h, /Last run failed: RateLimitError: 429 You have no credits\. Run it again\./)
  assert.match(h, /title="RateLimitError: 429 You have no credits"/)
  assert.match(h, />Run exploration now</)
  assert.match(html(<FailedRunNote error={null} />), /Last run failed\. Run it again\./)
})

test('no button when the playbook is not active or the control is omitted', () => {
  const draft = html(<SummaryStrip s={{ ...base, lastRun: null, playbook: { id: 3, status: 'draft' } }} run={{ onRun: () => {} }} />)
  assert.ok(!draft.includes('Run exploration now'))
  assert.ok(!html(<SummaryStrip s={{ ...base, lastRun: null, playbook: { id: 3, status: 'active' } }} />).includes('Run exploration now'))
})

test('a refusal message shows next to the button', () => {
  const h = html(<RunNowView error="Today's AI budget is used up." onRun={() => {}} />)
  assert.match(h, /role="alert">Today&#x27;s AI budget is used up\./)
})

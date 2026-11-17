import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { renderToStaticMarkup } from 'react-dom/server'
import {
  AskBadges,
  AskThreadView,
  ChatList,
  LiveStatus,
  askButtonLabel,
} from '../src/components/AskPanel'
import {
  createChat,
  deleteChat,
  elapsed,
  failureText,
  fetchChats,
  fetchThread,
  getStoredChat,
  markChatRead,
  ownsChat,
  postAsk,
  readyCount,
  renameChat,
  retryRun,
  runSeconds,
  setOperator,
  storeChat,
  type AskChat,
  type AskThread,
} from '../src/lib/ask'
import { threadLabel } from '../src/lib/format'

/* Quick 003: several Ask chats, a panel that does not block the app, and a
 * working indicator with the live step, elapsed time and Retry on failure. */

let calls: { url: string; method?: string; body?: string }[] = []
beforeEach(() => {
  localStorage.clear()
  calls = []
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    calls.push({ url: String(url), method: init?.method, body: init?.body as string })
    return { ok: true, status: 200, json: async () => ({}) }
  }) as unknown as typeof fetch
})

const chat = (over: Partial<AskChat> = {}): AskChat => ({
  id: 'ui:owner:abcdefghij',
  is_main: false,
  title: 'Fuel by barge',
  named: false,
  created_at: null,
  last_activity_at: null,
  last_assistant_id: null,
  unread: false,
  run: null,
  ...over,
})

const thread = (over: Partial<AskThread> = {}): AskThread => ({
  thread_id: 'ui:owner',
  messages: [{ id: 1, role: 'user', content: 'How much diesel?', created_at: '' }],
  pending: false,
  last_run: null,
  run: null,
  waiting_approval: 0,
  now: '2026-10-25T10:01:05Z',
  ...over,
})

test('chat api calls', async () => {
  await postAsk('hi', 'ui:owner:abcdefghij')
  assert.deepEqual(JSON.parse(calls[0].body!), { message: 'hi', operator: 'owner', chat: 'ui:owner:abcdefghij' })
  await fetchThread(50, 'ui:owner:abcdefghij')
  assert.equal(calls[1].url, '/api/ask?operator=owner&limit=50&chat=ui%3Aowner%3Aabcdefghij')
  await fetchChats()
  assert.equal(calls[2].url, '/api/ask/chats?operator=owner')
  await createChat()
  assert.equal(calls[3].method, 'POST')
  assert.deepEqual(JSON.parse(calls[3].body!), { operator: 'owner' })
  await renameChat('ui:owner:abcdefghij', 'Barges')
  assert.equal(calls[4].url, '/api/ask/chats/ui%3Aowner%3Aabcdefghij')
  assert.equal(calls[4].method, 'PATCH')
  await deleteChat('ui:owner:abcdefghij')
  assert.equal(calls[5].method, 'DELETE')
  await markChatRead('ui:owner', 12)
  assert.equal(calls[6].url, '/api/ask/chats/ui%3Aowner/read')
  assert.deepEqual(JSON.parse(calls[6].body!), { upto: 12 })
  await retryRun('ui:owner', 7)
  assert.equal(calls[7].url, '/api/ask/retry')
  assert.deepEqual(JSON.parse(calls[7].body!), { chat: 'ui:owner', run_id: 7 })
})

test('stored chat belongs to the operator', () => {
  assert.equal(getStoredChat(), 'ui:owner')
  storeChat('ui:owner:abcdefghij')
  assert.equal(getStoredChat(), 'ui:owner:abcdefghij')
  storeChat('ui:alice:abcdefghij')
  assert.equal(getStoredChat(), 'ui:owner')
  assert.ok(ownsChat('ui:owner') && !ownsChat('ui:owner:abc') && !ownsChat('15550000000'))
  setOperator('alice')
  assert.equal(getStoredChat(), 'ui:alice:abcdefghij')
  setOperator('owner')
})

test('elapsed and run seconds', () => {
  assert.equal(elapsed(7), '0:07')
  assert.equal(elapsed(65), '1:05')
  assert.equal(elapsed(3723), '1:02:03')
  assert.equal(elapsed(-3), '0:00')
  const run = { id: 1, status: 'running', error: null, step: null, started_at: '2026-10-25T10:00:00Z' }
  assert.equal(runSeconds(run, '2026-10-25T10:01:05Z'), 65)
  assert.equal(runSeconds({ ...run, started_at: null, created_at: '2026-10-25T10:01:00Z' }, '2026-10-25T10:01:05Z'), 5)
  assert.equal(runSeconds(null, '2026-10-25T10:01:05Z'), 0)
})

test('failure text is plain', () => {
  assert.match(failureText('token budget exceeded'), /used more processing than one answer is allowed/)
  assert.match(failureText('timed out after 600 s'), /time limit for one answer \(600 seconds\)/)
  assert.match(failureText('TimeoutError: '), /time limit/)
  assert.equal(failureText('KeyError: x'), 'This answer failed: KeyError: x')
  const limit = failureText(
    "BadRequestError: Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'You have reached your specified API usage limits. You will regain access on 2026-11-01 at 00:00 UTC.'}}",
  )
  assert.equal(
    limit,
    'This answer failed because the AI provider account has reached its usage limit (access returns on 2026-11-01 at 00:00 UTC). Raise the limit on the provider account, then Retry.',
  )
  assert.equal(failureText(null), 'This answer failed.')
})

test('ready count skips the chat on screen', () => {
  const chats = [chat({ unread: true }), chat({ id: 'ui:owner', is_main: true, unread: true }), chat({ id: 'ui:owner:qwertyuiop' })]
  assert.equal(readyCount(chats, null), 2)
  assert.equal(readyCount(chats, 'ui:owner'), 1)
  assert.equal(readyCount(null, null), 0)
})

test('live status: dots, step, elapsed, announced politely', () => {
  const h = renderToStaticMarkup(<LiveStatus step="Running query 2" seconds={65} />)
  assert.ok(h.includes('role="status"') && h.includes('aria-live="polite"'))
  assert.equal((h.match(/<i><\/i>/g) || []).length, 3)
  assert.ok(h.includes('aria-hidden="true"') && h.includes('Running query 2') && h.includes('1:05'))
  assert.ok(renderToStaticMarkup(<LiveStatus step={null} seconds={0} />).includes('Working on it'))
})

test('thread view while running shows the live step and queued messages', () => {
  const h = renderToStaticMarkup(
    <AskThreadView
      thread={thread({
        pending: true,
        run: { id: 4, status: 'running', error: null, step: 'Reading the rules for DailyFuelReports', started_at: '2026-10-25T10:00:00Z', queued_behind: 1 },
      })}
    />,
  )
  assert.ok(h.includes('ask-live') && h.includes('Reading the rules for DailyFuelReports') && h.includes('1:05'))
  assert.ok(h.includes('1 more message in this chat'))
  assert.ok(!h.includes('ask-failed'))
})

test('failed run says so plainly with Retry', () => {
  let retried = 0
  const t = thread({ run: { id: 9, status: 'failed', error: 'token budget exceeded', step: null } })
  const h = renderToStaticMarkup(<AskThreadView thread={t} onRetry={(id) => (retried = id)} />)
  assert.ok(h.includes('banner-broken') && h.includes('role="alert"'))
  assert.ok(h.includes('used more processing') && h.includes('>Retry</button>'))
  assert.ok(!h.includes('ask-live'))
  assert.equal(retried, 0)
  assert.ok(!renderToStaticMarkup(<AskThreadView thread={t} />).includes('Retry'))
})

test('waiting for approval chip after a finished answer', () => {
  const t = thread({ run: { id: 3, status: 'done', error: null, step: null }, waiting_approval: 2 })
  const h = renderToStaticMarkup(<AskThreadView thread={t} onOpenApprovals={() => {}} />)
  assert.ok(h.includes('Waiting for your approval: 2 items') && h.includes('Open Approvals'))
  assert.ok(!h.includes('owed'))
  const one = renderToStaticMarkup(<AskThreadView thread={{ ...t, waiting_approval: 1 }} />)
  assert.ok(one.includes('1 item<') || one.includes('1 item</span>'))
})

test('empty chat invites a question', () => {
  const h = renderToStaticMarkup(<AskThreadView thread={thread({ messages: [] })} />)
  assert.ok(h.includes('Ask about your data'))
})

test('chat list: titles, main pill, markers, delete rules', () => {
  const h = renderToStaticMarkup(
    <ChatList
      active="ui:owner"
      chats={[
        chat({ id: 'ui:owner', is_main: true, title: 'How much diesel?' }),
        chat({ unread: true }),
        chat({ id: 'ui:owner:qwertyuiop', title: 'Dashboard build', run: { id: 5, status: 'running', error: null, step: 'Checking the dashboard draft' } }),
        chat({ id: 'ui:owner:zxcvbnmasd', title: 'Broken', run: { id: 6, status: 'failed', error: 'x', step: null } }),
      ]}
    />,
  )
  assert.ok(h.includes('How much diesel?') && h.includes('>main</span>'))
  assert.ok(h.includes('dot dot-signal') && h.includes('Answer ready'))
  assert.ok(h.includes('ask-dot ask-dot-working') && h.includes('Checking the dashboard draft'))
  assert.ok(h.includes('dot dot-broken') && h.includes('Failed'))
  assert.equal((h.match(/>Rename</g) || []).length, 4)
  assert.equal((h.match(/>Delete</g) || []).length, 3) // never on the main chat
  assert.ok(/disabled=""[^>]*>Delete</.test(h)) // not while an answer runs
  assert.ok(h.includes('aria-current="true"'))
})

test('ask button badges are blue, never gold', () => {
  assert.equal(renderToStaticMarkup(<AskBadges working={false} ready={0} />), '')
  assert.ok(renderToStaticMarkup(<AskBadges working ready={0} />).includes('ask-dot-working'))
  const r = renderToStaticMarkup(<AskBadges working ready={2} />)
  assert.ok(r.includes('ask-count') && r.includes('>2<') && !r.includes('ask-dot-working'))
  assert.equal(askButtonLabel(false, 0), undefined)
  assert.equal(askButtonLabel(true, 0), 'Ask (an answer is being worked on)')
  assert.equal(askButtonLabel(false, 1), 'Ask (1 answer ready)')
})

test('ask.css: no scrim, docks beside the page, reduced motion stops the animation', () => {
  const css = readFileSync('src/components/ask.css', 'utf8')
  assert.ok(!css.includes('.ask-scrim') && !css.includes('--owed'))
  assert.ok(css.includes('.shell.ask-docked .page'))
  const reduced = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'))
  for (const sel of ['.ask-pulse i', '.ask-dot-working', '.ask-step']) assert.ok(reduced.includes(sel), sel)
  assert.ok(reduced.includes('animation: none'))
})

test('Ask threads get a readable label', () => {
  assert.equal(threadLabel('ui:owner'), 'Ask (main chat)')
  assert.equal(threadLabel('ui:owner:abcdefghij'), 'Ask chat')
})

// Quick 021: an answer shows which question it belongs to when another message sits
// between them (two questions sent before the first answer came back).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderToStaticMarkup } from 'react-dom/server'
import { AskThreadView } from '../src/components/AskPanel'
import { replyingTo, type AskMessage, type AskThread } from '../src/lib/ask'

const msg = (id: number, role: AskMessage['role'], content: string, reply_to?: number | null): AskMessage => ({
  id,
  role,
  content,
  created_at: '',
  ...(reply_to !== undefined ? { reply_to } : {}),
})

const thread = (messages: AskMessage[]): AskThread => ({
  thread_id: 'ui:owner',
  messages,
  pending: false,
  last_run: null,
})

const TWO_PENDING = [
  msg(1, 'user', 'is any machine actively consuming diesel right now on site'),
  msg(2, 'user', 'how many cases are open'),
  msg(3, 'assistant', 'About 10 machines are burning diesel.', 1),
  msg(4, 'assistant', 'There are 3 open cases.', 2),
]

test('two pending questions: each answer names its question', () => {
  assert.equal(replyingTo(TWO_PENDING, 2), 'is any machine actively consuming diesel right now…')
  assert.equal(replyingTo(TWO_PENDING, 3), 'how many cases are open')
  const h = renderToStaticMarkup(<AskThreadView thread={thread(TWO_PENDING)} />)
  assert.equal(h.split('ask-replying').length - 1, 2)
  assert.ok(h.includes('Replying to: how many cases are open'))
  assert.ok(h.indexOf('Replying to: how many') < h.indexOf('There are 3 open cases.'))
})

test('an answer right under its question shows nothing', () => {
  const plain = [msg(1, 'user', 'hi'), msg(2, 'assistant', 'Hello!', 1)]
  assert.equal(replyingTo(plain, 1), null)
  assert.ok(!renderToStaticMarkup(<AskThreadView thread={thread(plain)} />).includes('ask-replying'))
})

test('alerts, older rows and questions outside the loaded page show nothing', () => {
  const rows = [
    msg(5, 'user', 'q'),
    msg(6, 'assistant', 'Watchdog alert', null),
    msg(7, 'assistant', 'old answer'),
    msg(8, 'assistant', 'answer to an earlier page', 2),
    msg(9, 'user', 'user rows never get a label', 5),
  ]
  for (let i = 0; i < rows.length; i++) assert.equal(replyingTo(rows, i), null)
})

test('long first words are cut and the label is escaped', () => {
  const rows = [
    msg(1, 'user', `<b>${'x'.repeat(80)}</b> tail`),
    msg(2, 'user', 'second'),
    msg(3, 'assistant', 'a', 1),
  ]
  const label = replyingTo(rows, 2)!
  assert.ok(label.endsWith('…') && label.length <= 61)
  const h = renderToStaticMarkup(<AskThreadView thread={thread(rows)} />)
  assert.ok(h.includes('&lt;b&gt;') && !h.includes('<b>x'))
})

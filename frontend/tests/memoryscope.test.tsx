import { test } from 'node:test'
import assert from 'node:assert/strict'
import ReactDOMServer from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { actionLine, actionTitle } from '../src/components/ActionPayload'
import { ActionRow } from '../src/pages/Approvals'
import { LearningRow, scopeText } from '../src/pages/Memory'
import type { Action, Learning } from '../src/lib/types'

/* Quick 020: memory scope and write attribution. The approval card for a
 * learning nobody could attribute shows the fact, where it came from and where
 * it would apply, with Approve and Reject; the Memory page shows who each
 * learning applies to and whether it is waiting for approval. */

const render = (el: React.ReactElement) =>
  ReactDOMServer.renderToStaticMarkup(<MemoryRouter>{el}</MemoryRouter>)

function action(payload: Record<string, unknown>, status: Action['status'] = 'proposed'): Action {
  return {
    id: 41,
    case_id: null,
    playbook_id: null,
    kind: 'activate_learning',
    task_run_id: 7,
    payload: JSON.stringify(payload), // asyncpg hands jsonb back as a string
    tier: 'draft',
    status,
    created_at: '2026-10-26T10:00:00Z',
    executed_at: null,
  }
}

const groupFact = action({
  learning_id: 12,
  kind: 'fact',
  content: 'Diesel for site 4 is billed to the Pune office',
  origin: 'group',
  origin_label: 'the group "Site fuel"',
  thread_id: '120363000000000001@g.us',
  sender: null,
  scope: null,
  applies_to: 'everyone (facts, corrections, recipes and lessons are shared)',
})

const senderPref = action({
  learning_id: 13,
  kind: 'preference',
  content: 'Always reply to me in Gujarati from now on',
  origin: 'sender',
  origin_label: 'an unregistered sender (+919822222222)',
  sender: '919822222222',
  scope: 'wa:919822222222',
  applies_to: 'this sender only',
})

test('approval card: the fact, its group, Approve and Reject', () => {
  const html = render(<ActionRow action={groupFact} onDecided={() => {}} />)
  assert.ok(html.includes('Remember this fact?'))
  assert.ok(html.includes('Diesel for site 4 is billed to the Pune office'))
  assert.ok(html.includes('Said in the group &quot;Site fuel&quot;, where the speaker is not known.'))
  assert.ok(html.includes('>Approve<') && html.includes('>Reject<'))
  assert.ok(html.includes('Reject: it is retired and never used.'))
  assert.ok(html.includes('activate learning'))
})

test('approval card: an unknown sender and where a preference applies', () => {
  const html = render(<ActionRow action={senderPref} onDecided={() => {}} />)
  assert.ok(html.includes('Remember this preference?'))
  assert.ok(html.includes('who is not registered on People'))
  assert.ok(html.includes('919822222222'))
  assert.ok(html.includes('for this sender only'))
})

test('approval one-liners', () => {
  assert.equal(actionTitle(groupFact), 'Remember this fact?')
  assert.ok(actionLine(groupFact).startsWith('Diesel for site 4'))
  assert.ok(actionLine(groupFact).includes('from the group "Site fuel"'))
  const decided = render(<ActionRow action={action(JSON.parse(groupFact.payload as string), 'executed')} onDecided={() => {}} />)
  assert.ok(!decided.includes('>Approve<'))
})

function learning(o: Partial<Learning>): Learning {
  return {
    id: 1,
    thread_id: null,
    kind: 'preference',
    content: 'Always show amounts in lakhs',
    created_at: '2026-10-26T10:00:00Z',
    updated_at: '2026-10-26T10:00:00Z',
    revisions: 0,
    status: 'active',
    ...o,
  }
}

test('memory: scope in words', () => {
  assert.equal(scopeText(learning({ scope_kind: 'owner', scope_label: 'Owner' })), 'Owner only')
  assert.equal(scopeText(learning({ scope_kind: 'person', scope_label: 'Asha' })), 'Asha only')
  assert.equal(scopeText(learning({ scope_kind: 'group', scope_label: 'Site fuel' })), 'Group: Site fuel')
  assert.equal(scopeText(learning({ kind: 'fact', scope_kind: 'global', scope_label: 'Everyone' })), 'Everyone')
  // an older API without scope_kind: a preference is the owner's, the rest shared
  assert.equal(scopeText(learning({})), 'Owner only')
  assert.equal(scopeText(learning({ kind: 'recipe' })), 'Everyone')
})

test('memory: rows show the scope and a pending or rejected state', () => {
  const subjects = new Map<string, string>()
  const row = (l: Learning) =>
    render(<LearningRow l={l} subjects={subjects} busy={false} onForget={() => {}} />)
  const owner = row(learning({ scope: 'owner', scope_kind: 'owner', scope_label: 'Owner' }))
  assert.ok(owner.includes('Owner only') && owner.includes('data-learning-scope="owner"'))
  assert.ok(!owner.includes('waiting for your approval'))
  const pending = row(learning({ kind: 'fact', status: 'pending', scope_kind: 'global',
                                 scope_label: 'Everyone', content: 'Pune depot closes at 6 pm' }))
  assert.ok(pending.includes('waiting for your approval'))
  assert.ok(pending.includes('href="/approvals"'))
  assert.ok(pending.includes('Everyone'))
  const rejected = row(learning({ status: 'rejected', status_reason: 'rejected by the owner on Approvals (action #9)',
                                  scope_kind: 'group', scope_label: 'Site fuel' }))
  assert.ok(rejected.includes('>rejected<') && rejected.includes('action #9'))
  assert.ok(rejected.includes('Group: Site fuel'))
  assert.ok(!rejected.includes('superseded'))
  const person = row(learning({ scope_kind: 'person', scope_label: 'Asha' }))
  assert.ok(person.includes('Asha only'))
})

test('approval card: a run with no person and no sender', () => {
  const inbox = action({
    learning_id: 14,
    kind: 'fact',
    content: 'Supplier invoices from Acme are in USD',
    origin: 'other',
    origin_label: 'the document inbox',
    sender: null,
    applies_to: 'everyone (facts, corrections, recipes and lessons are shared)',
  })
  const html = render(<ActionRow action={inbox} onDecided={() => {}} />)
  assert.ok(html.includes('From the document inbox, which is not attributed to a person.'))
  assert.ok(!html.includes('registered on People'))
})

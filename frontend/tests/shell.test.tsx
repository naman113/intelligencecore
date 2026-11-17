import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import AppShell from '../src/components/AppShell'
import SignIn from '../src/pages/SignIn'
import { ProfileContext } from '../src/lib/profile'
import type { Profile } from '../src/lib/brand'

function render(el: React.ReactElement, profile: Profile, resolved: boolean) {
  return renderToStaticMarkup(
    <ProfileContext.Provider value={{ profile, resolved, health: null, reload: async () => {} }}>
      <MemoryRouter>{el}</MemoryRouter>
    </ProfileContext.Provider>,
  )
}
const noGold = (h: string) => {
  for (const t of ['card-owed', 'btn-owed', '--owed']) assert.ok(!h.includes(t), t)
}

test('shell second_brain', () => {
  const h = render(<AppShell />, 'second_brain', true)
  assert.ok(h.includes('SECOND BRAIN') && h.includes('Today') && h.includes('Watches'))
  for (const t of ['Overview', 'Business context', 'INTELLIGENCE']) assert.ok(!h.includes(t), t)
  noGold(h)
})

test('shell intelligence', () => {
  const h = render(<AppShell />, 'intelligence', true)
  assert.ok(h.includes('>INTELLIGENCE</div>') && h.includes('>CORNER</div>'))
  for (const t of ['Intelligence', 'Business context', 'Agents', 'Operations', 'Rules &amp; KPI definitions'])
    assert.ok(h.includes(t), t)
  assert.ok(h.includes('Watches')) // Quick 050: owner asked for the full watch list with pause in the menu
  noGold(h)
})

test('shell unresolved', () => {
  const h = render(<AppShell />, 'intelligence', false)
  for (const t of ['SECOND BRAIN', 'INTELLIGENCE', 'Today', 'Overview']) assert.ok(!h.includes(t), t)
  assert.ok(h.includes('Open menu') && h.includes('<main'))
})

test('signin brands', () => {
  const sb = render(<SignIn />, 'second_brain', true)
  assert.ok(sb.includes('SECOND BRAIN') && sb.includes('This instance is unlocked with its ops key'))
  const ic = render(<SignIn />, 'intelligence', true)
  assert.ok(ic.includes('INTELLIGENCE') && ic.includes('CORNER') && ic.includes('Intelligence Corner is unlocked with its ops key'))
  const un = render(<SignIn />, 'intelligence', false)
  assert.ok(!un.includes('INTELLIGENCE') && !un.includes('SECOND BRAIN'))
  noGold(sb + ic)
})

test('ask button only on intelligence', () => {
  const ic = render(<AppShell />, 'intelligence', true)
  assert.ok(ic.includes('>Ask</button>'))
  const sb = render(<AppShell />, 'second_brain', true)
  assert.ok(!sb.includes('>Ask<'))
  assert.ok(!render(<AppShell />, 'intelligence', false).includes('>Ask<'))
})

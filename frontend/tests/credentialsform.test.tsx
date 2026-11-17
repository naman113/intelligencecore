import { test } from 'node:test'
import assert from 'node:assert/strict'
import ReactDOMServer from 'react-dom/server'
import {
  CredentialsFormView,
  credentialPayload,
  emptyForm,
  formReducer,
} from '../src/components/CredentialsForm'
import type { CredentialHandle } from '../src/lib/sources'

const handle = (o: Partial<CredentialHandle> = {}): CredentialHandle => ({
  handle: 'cred:erp_my',
  name: 'erp_my',
  kind: 'mysql',
  display: { host: 'h.example', port: 3306, database: 'erp', user: 'ro' },
  read_only: true,
  verify_status: 'ok',
  verify_detail: null,
  verified_at: null,
  ...o,
})
const render = (values = emptyForm, handles: CredentialHandle[] = []) =>
  ReactDOMServer.renderToStaticMarkup(<CredentialsFormView values={values} handles={handles} />)

test('form has a password input and the chat note', () => {
  const html = render()
  assert.match(html, /type="password"/)
  assert.match(html, /autoComplete="new-password"|autocomplete="new-password"/)
  assert.match(html, /never in chat/)
  assert.match(html, /name="sslmode"/)
})

test('per-kind options', () => {
  assert.match(render({ ...emptyForm, kind: 'mysql' }), /Use TLS/)
  const ms = render({ ...emptyForm, kind: 'mssql' })
  assert.match(ms, /Trust server certificate/)
  assert.doesNotMatch(ms, /name="sslmode"/)
})

test('handle list shows handle and status, no secret', () => {
  const html = render(emptyForm, [handle()])
  assert.match(html, /cred:erp_my/)
  assert.match(html, /h\.example:3306\/erp/)
  assert.match(html, /verified/)
  assert.match(html, /read-only/)
  assert.doesNotMatch(html, /value="[^"]+"[^>]*type="password"/)
})

test('writable login shows the gold warning, failed shows detail', () => {
  const html = render(emptyForm, [handle({ read_only: false, verify_status: 'failed', verify_detail: 'refused' })])
  assert.match(html, /can write: live tables will be refused/)
  assert.match(html, /refused/)
})

test('payload and reducer clear the password after save', () => {
  let f = formReducer(emptyForm, { type: 'set', field: 'kind', value: 'mysql' })
  assert.equal(f.port, '3306')
  f = { ...f, name: ' erp ', host: 'h', database: 'd', user: 'u', password: 'secret-pw-123' }
  const p = credentialPayload(f)
  assert.equal(p.name, 'erp')
  assert.equal(p.port, 3306)
  assert.equal(p.password, 'secret-pw-123')
  assert.deepEqual(p.options, { ssl: false })
  assert.equal(formReducer(f, { type: 'saved' }).password, '')
})

import { useReducer } from 'react'
import { useAction, useResource } from '../lib/hooks'
import {
  deleteCredential,
  listCredentials,
  saveCredential,
  verifyCredential,
  type CredentialBody,
  type CredentialHandle,
} from '../lib/sources'
import { ErrorNote, Field, StatusPill } from './ui'

/* Phase 15 (SRC-02): the owner-only credentials form. The one sanctioned exception to
 * "UI buttons message the agent": connection details never go through chat. The agent
 * sees only cred:<name>. A stored secret is never shown again. */

export const DEFAULT_PORT: Record<string, number> = { postgres: 5432, mysql: 3306, mssql: 1433 }

export type FormValues = {
  name: string
  kind: 'postgres' | 'mysql' | 'mssql'
  host: string
  port: string
  database: string
  user: string
  password: string
  sslmode: string
  ssl: boolean
  encrypt: string
  trust: boolean
}

export const emptyForm: FormValues = {
  name: '',
  kind: 'postgres',
  host: '',
  port: '5432',
  database: '',
  user: '',
  password: '',
  sslmode: 'prefer',
  ssl: false,
  encrypt: 'yes',
  trust: false,
}

export type FormAction =
  | { type: 'set'; field: keyof FormValues; value: string | boolean }
  | { type: 'saved' }

export function formReducer(state: FormValues, a: FormAction): FormValues {
  if (a.type === 'saved') return { ...state, password: '' }
  if (a.field === 'kind') {
    const kind = a.value as FormValues['kind']
    const wasDefault = state.port === String(DEFAULT_PORT[state.kind])
    return { ...state, kind, port: wasDefault ? String(DEFAULT_PORT[kind]) : state.port }
  }
  return { ...state, [a.field]: a.value }
}

export function credentialPayload(f: FormValues): CredentialBody {
  const options: Record<string, unknown> =
    f.kind === 'postgres'
      ? { sslmode: f.sslmode }
      : f.kind === 'mysql'
        ? { ssl: f.ssl }
        : { encrypt: f.encrypt, trust_server_certificate: f.trust }
  return {
    name: f.name.trim(),
    kind: f.kind,
    host: f.host.trim(),
    port: Number(f.port),
    database: f.database.trim(),
    user: f.user.trim(),
    password: f.password,
    options,
  }
}

export function HandleList({
  handles,
  busy,
  onVerify,
  onDelete,
}: {
  handles: CredentialHandle[]
  busy?: boolean
  onVerify?: (name: string) => void
  onDelete?: (name: string) => void
}) {
  if (handles.length === 0) return <p className="ic-meta">No stored credentials yet.</p>
  return (
    <div className="ic-list">
      {handles.map((h) => {
        const d = h.display || {}
        return (
          <div className="ic-row" key={h.name}>
            <span>
              <strong>{h.handle}</strong> <span className="ic-meta">{h.kind}</span>{' '}
              <StatusPill
                status={h.verify_status === 'ok' ? 'verified' : h.verify_status === 'failed' ? 'failed' : 'unverified'}
                tone={h.verify_status === 'ok' ? 'settled' : h.verify_status === 'failed' ? 'broken' : 'neutral'}
              />
            </span>
            <span className="ic-meta">
              {d.host}:{d.port}/{d.database}
            </span>
            {h.read_only === true && <span className="ic-meta">read-only</span>}
            {h.read_only === false && (
              <span className="ic-owed-line">can write: live tables will be refused</span>
            )}
            {h.verify_detail && <span className="ic-meta">{h.verify_detail}</span>}
            <span className="btn-row">
              <button className="btn btn-sm" disabled={busy} onClick={() => onVerify?.(h.name)}>
                Verify
              </button>
              <button className="btn btn-sm" disabled={busy} onClick={() => onDelete?.(h.name)}>
                Remove
              </button>
            </span>
          </div>
        )
      })}
    </div>
  )
}

export function CredentialsFormView({
  values,
  handles,
  busy,
  error,
  onChange,
  onSubmit,
  onVerify,
  onDelete,
}: {
  values: FormValues
  handles: CredentialHandle[]
  busy?: boolean
  error?: string | null
  onChange?: (field: keyof FormValues, value: string | boolean) => void
  onSubmit?: () => void
  onVerify?: (name: string) => void
  onDelete?: (name: string) => void
}) {
  const set = (f: keyof FormValues) => (e: { target: { value: string } }) => onChange?.(f, e.target.value)
  const flag = (f: keyof FormValues) => (e: { target: { checked: boolean } }) => onChange?.(f, e.target.checked)
  return (
    <div>
      <p className="ic-meta">Connection details go only here, never in chat. The agent sees only cred:&lt;name&gt;.</p>
      {error && <ErrorNote message={error} />}
      <form
        onSubmit={(e) => {
          e.preventDefault()
          onSubmit?.()
        }}
        autoComplete="off"
      >
        <div className="row" style={{ gap: 'var(--s2)', flexWrap: 'wrap' }}>
          <Field label="Name" hint="letters, digits, underscores">
            <input className="input" name="name" value={values.name} onChange={set('name')} />
          </Field>
          <Field label="Kind">
            <select className="input" name="kind" value={values.kind} onChange={set('kind')}>
              <option value="postgres">postgres</option>
              <option value="mysql">mysql</option>
              <option value="mssql">mssql</option>
            </select>
          </Field>
          <Field label="Host">
            <input className="input" name="host" value={values.host} onChange={set('host')} />
          </Field>
          <Field label="Port">
            <input className="input" name="port" value={values.port} onChange={set('port')} />
          </Field>
          <Field label="Database">
            <input className="input" name="database" value={values.database} onChange={set('database')} />
          </Field>
          <Field label="User">
            <input className="input" name="user" value={values.user} onChange={set('user')} />
          </Field>
          <Field label="Password">
            <input
              className="input"
              type="password"
              name="password"
              autoComplete="new-password"
              value={values.password}
              onChange={set('password')}
            />
          </Field>
          {values.kind === 'postgres' && (
            <Field label="SSL mode">
              <select className="input" name="sslmode" value={values.sslmode} onChange={set('sslmode')}>
                {['disable', 'allow', 'prefer', 'require', 'verify-ca', 'verify-full'].map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </Field>
          )}
          {values.kind === 'mysql' && (
            <Field label="Use TLS">
              <input type="checkbox" name="ssl" checked={values.ssl} onChange={flag('ssl')} />
            </Field>
          )}
          {values.kind === 'mssql' && (
            <>
              <Field label="Encrypt">
                <select className="input" name="encrypt" value={values.encrypt} onChange={set('encrypt')}>
                  {['yes', 'no', 'strict'].map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Trust server certificate">
                <input type="checkbox" name="trust" checked={values.trust} onChange={flag('trust')} />
              </Field>
            </>
          )}
        </div>
        <button className="btn btn-sm" type="submit" disabled={busy}>
          Save and verify
        </button>
      </form>
      <h3 className="panel-title" style={{ marginTop: 'var(--s3)' }}>
        Stored credentials
      </h3>
      <HandleList handles={handles} busy={busy} onVerify={onVerify} onDelete={onDelete} />
    </div>
  )
}

export function CredentialsForm() {
  const [values, dispatch] = useReducer(formReducer, emptyForm)
  const res = useResource(() => listCredentials(), { interval: 30_000 })
  const act = useAction()
  const reload = () => void res.reload()
  return (
    <CredentialsFormView
      values={values}
      handles={res.data?.credentials ?? []}
      busy={act.pending}
      error={act.error ?? res.error}
      onChange={(field, value) => dispatch({ type: 'set', field, value })}
      onSubmit={() =>
        void act.run(async () => {
          await saveCredential(credentialPayload(values))
          dispatch({ type: 'saved' })
          reload()
        })
      }
      onVerify={(n) =>
        void act.run(async () => {
          await verifyCredential(n)
          reload()
        })
      }
      onDelete={(n) =>
        void act.run(async () => {
          await deleteCredential(n)
          reload()
        })
      }
    />
  )
}

export default CredentialsForm

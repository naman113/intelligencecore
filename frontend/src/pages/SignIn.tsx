import { useState, type FormEvent } from 'react'
import { get, setKey, clearKey } from '../lib/api'
import { BRANDS } from '../lib/brand'
import { useProfile } from '../lib/profile'
import './signin.css'

/* The key is the whole credential, so this screen's job is to say what it is
 * and where it came from — an operator who lost it needs to know to look at the
 * install output, not to guess a password. */
export default function SignIn() {
  const { profile, resolved } = useProfile()
  const brand = BRANDS[profile]
  const [value, setValue] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [checking, setChecking] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    const key = value.trim()
    if (!key) {
      setError('Enter the ops key to continue.')
      return
    }
    setChecking(true)
    setError(null)
    setKey(key)
    try {
      // Cheapest authenticated read there is; it also warms the Setup page.
      await get('/api/config/status')
      // The auth listener in App swaps this screen out.
    } catch (err) {
      clearKey()
      setError(err instanceof Error ? err.message : 'Could not reach the gateway.')
    } finally {
      setChecking(false)
    }
  }

  return (
    <div className="signin">
      <div className="signin-mark">
        {resolved &&
          brand.wordmark.map((line) => (
            <div className="wordmark" style={{ fontSize: 22 }} key={line}>
              {line}
            </div>
          ))}
        {resolved && brand.byline && (
          <div className="wordmark-sub">
            by {brand.byline}
          </div>
        )}
      </div>

      <form className="signin-card" onSubmit={submit}>
        <h1 className="signin-title">Sign in</h1>
        <p className="signin-lede">
          {resolved ? brand.signInLede : ''}
        </p>

        <label className="field" style={{ display: 'block', marginTop: 'var(--s5)' }}>
          <span className="field-label">Ops key</span>
          <input
            className="input mono"
            type="password"
            autoFocus
            autoComplete="current-password"
            spellCheck={false}
            placeholder="paste the key"
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
        </label>

        {error && (
          <div className="banner banner-broken" style={{ marginTop: 'var(--s3)' }}>
            {error}
          </div>
        )}

        <button className="btn btn-primary" type="submit" disabled={checking} style={{ width: '100%', marginTop: 'var(--s4)' }}>
          {checking ? 'Checking…' : 'Sign in'}
        </button>

        <p className="signin-foot">
          Lost it? On the server, run <code className="mono">agentctl status</code>, or read{' '}
          <code className="mono">OPS_KEY</code> from the <code className="mono">.env</code> file
          in the install directory.
        </p>
      </form>
    </div>
  )
}

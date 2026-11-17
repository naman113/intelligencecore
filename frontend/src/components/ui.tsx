import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Alert, Refresh } from './icons'

/* ── Page furniture ────────────────────────────────────────────────────── */

export function PageHeader({
  title,
  lede,
  actions,
}: {
  title: string
  lede?: ReactNode
  actions?: ReactNode
}) {
  return (
    <header className="page-head">
      <div>
        <h1 className="page-title">{title}</h1>
        {lede && <p className="page-lede">{lede}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  )
}

export function Section({
  title,
  meta,
  actions,
  children,
  flush,
}: {
  title: string
  meta?: ReactNode
  actions?: ReactNode
  children: ReactNode
  /** Set when the child renders its own table or list padding. */
  flush?: boolean
}) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h2 className="panel-title">{title}</h2>
        {meta && <span className="dimmer mono" style={{ fontSize: 12 }}>{meta}</span>}
        <div className="panel-spacer" />
        {actions}
      </div>
      <div className={flush ? '' : 'panel-body'}>{children}</div>
    </section>
  )
}

/* ── Status ────────────────────────────────────────────────────────────── */

export type Tone = 'neutral' | 'signal' | 'owed' | 'settled' | 'broken'

/* The gold rule, in one function.
 *
 * A status is `owed` only when the system has stopped and is waiting on a
 * person. `signal` is work in flight. `settled` and `broken` are terminal.
 * Anything a human already decided (rejected, dismissed, ignored) is neutral —
 * it is closed, and colouring it would compete with what still needs doing.
 */
export function toneOf(status: string | null | undefined): Tone {
  switch (status) {
    case 'proposed':
    case 'pending':
    case 'pending_ack':
    case 'violated':
    case 'not_configured':
      return 'owed'
    case 'running':
    case 'queued':
    case 'active':
    case 'open':
    case 'approved':
    case 'extracted':
      return 'signal'
    case 'done':
    case 'executed':
    case 'satisfied':
    case 'closed':
    case 'recorded':
    case 'ok':
      return 'settled'
    case 'failed':
    case 'failing':
      return 'broken'
    default:
      return 'neutral'
  }
}

export function StatusPill({
  status,
  tone,
  label,
  title,
}: {
  status?: string | null
  tone?: Tone
  label?: string
  title?: string
}) {
  const t = tone ?? toneOf(status)
  const cls = t === 'neutral' ? 'pill' : `pill pill-${t}`
  return (
    <span className={cls} title={title}>
      {label ?? (status || '').replace(/_/g, ' ')}
    </span>
  )
}

export function Meter({ value, max, tone }: { value: number; max: number; tone?: Tone }) {
  const pct = max > 0 ? Math.min(100, Math.round((value / max) * 100)) : 0
  const mod =
    tone === 'owed' ? ' is-owed' : tone === 'settled' ? ' is-settled' : tone === 'broken' ? ' is-broken' : ''
  return (
    <div className="meter" role="presentation">
      <div className={`meter-fill${mod}`} style={{ width: `${pct}%` }} />
    </div>
  )
}

/* ── States ────────────────────────────────────────────────────────────── */

export function Empty({
  title,
  children,
  action,
}: {
  title: string
  children?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="empty">
      <div className="empty-title">{title}</div>
      {children && <p className="empty-body">{children}</p>}
      {action && <div className="empty-action">{action}</div>}
    </div>
  )
}

export function Loading({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="row" style={{ padding: 'var(--s5)', color: 'var(--t3)' }}>
      <span className="spin" />
      <span>{label}…</span>
    </div>
  )
}

export function ErrorNote({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="banner banner-broken">
      <span style={{ marginTop: 1 }}>
        <Alert />
      </span>
      <span className="grow">{message}</span>
      {onRetry && (
        <button className="btn btn-sm" onClick={onRetry}>
          <Refresh size={13} /> Try again
        </button>
      )}
    </div>
  )
}

/** Panel-shaped wrapper that shows loading, error and empty in the right order. */
export function Async<T>({
  data,
  error,
  loading,
  reload,
  empty,
  children,
}: {
  data: T | null
  error: string | null
  loading: boolean
  reload?: () => void
  empty?: ReactNode
  children: (data: T) => ReactNode
}) {
  if (error && !data) return <ErrorNote message={error} onRetry={reload} />
  if (loading && !data) return <Loading />
  if (!data) return <>{empty ?? null}</>
  if (Array.isArray(data) && data.length === 0 && empty) return <>{empty}</>
  return <>{children(data)}</>
}

/* ── Controls ──────────────────────────────────────────────────────────── */

/** Two-step button for anything that cannot be undone. */
export function ConfirmButton({
  onConfirm,
  children,
  confirmLabel = 'Sure?',
  className = 'btn btn-sm btn-danger',
  disabled,
}: {
  onConfirm: () => void
  children: ReactNode
  confirmLabel?: string
  className?: string
  disabled?: boolean
}) {
  const [armed, setArmed] = useState(false)
  useEffect(() => {
    if (!armed) return
    const t = window.setTimeout(() => setArmed(false), 4000)
    return () => window.clearTimeout(t)
  }, [armed])

  return (
    <button
      className={className}
      disabled={disabled}
      onClick={() => {
        if (armed) {
          setArmed(false)
          onConfirm()
        } else {
          setArmed(true)
        }
      }}
    >
      {armed ? confirmLabel : children}
    </button>
  )
}

export function Modal({
  open,
  onClose,
  title,
  children,
  width = 640,
}: {
  open: boolean
  onClose: () => void
  title: string
  children: ReactNode
  width?: number
}) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    ref.current?.focus()
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(4,7,12,0.72)',
        zIndex: 60,
        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'center',
        padding: 'var(--s5)',
        overflowY: 'auto',
      }}
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        ref={ref}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="panel"
        style={{ width: '100%', maxWidth: width, marginTop: 'min(8vh, 64px)' }}
      >
        <div className="panel-head">
          <h2 className="panel-title">{title}</h2>
          <div className="panel-spacer" />
          <button className="btn btn-quiet btn-sm" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="panel-body">{children}</div>
      </div>
    </div>
  )
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: ReactNode
  children: ReactNode
}) {
  return (
    <label className="field" style={{ display: 'block' }}>
      <span className="field-label">{label}</span>
      {children}
      {hint && (
        <span className="dimmer" style={{ fontSize: 'var(--fs-micro)', display: 'block', marginTop: 4 }}>
          {hint}
        </span>
      )}
    </label>
  )
}

/** A control sitting in a form grid next to labelled fields. The blank label
 *  keeps it on the same line as the inputs rather than the labels. */
export function FormAction({ children }: { children: ReactNode }) {
  return (
    <div className="field" style={{ marginBottom: 0 }}>
      <span className="field-label" aria-hidden="true">
        &nbsp;
      </span>
      {children}
    </div>
  )
}

export function KeyValue({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <div className="kv">
      {rows.map(([k, v], i) => (
        <div key={`${k}-${i}`} style={{ display: 'contents' }}>
          <div className="kv-key">{k}</div>
          <div className="kv-val">{v}</div>
        </div>
      ))}
    </div>
  )
}

/** A filter strip that also reports how many are in each bucket. */
export function Tabs<T extends string>({
  value,
  onChange,
  options,
}: {
  value: T
  onChange: (v: T) => void
  options: { value: T; label: string; count?: number }[]
}) {
  return (
    <div className="tabs" role="tablist">
      {options.map((o) => (
        <button
          key={o.value}
          role="tab"
          aria-selected={o.value === value}
          className={`tab${o.value === value ? ' is-active' : ''}`}
          onClick={() => onChange(o.value)}
        >
          {o.label}
          {o.count !== undefined && <span className="tab-count">{o.count}</span>}
        </button>
      ))}
    </div>
  )
}

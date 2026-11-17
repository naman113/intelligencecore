import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { clearKey } from '../lib/api'
import { takeAskParam } from '../lib/askparam'
import { BRANDS } from '../lib/brand'
import { INTEL_NAV, type NavGroup } from '../lib/nav'
import { useOwed } from '../lib/owed'
import { useProfile } from '../lib/profile'
import { AskBadges, AskProvider, askButtonLabel, useAsk } from './AskPanel'
import './shell.css'

const NAV: NavGroup[] = [
  {
    label: null,
    items: [{ to: '/', label: 'Today', end: true, owed: (o) => o.counts.total }],
  },
  {
    label: 'Work',
    items: [
      { to: '/approvals', label: 'Approvals', owed: (o) => o.counts.approvals },
      { to: '/insights', label: 'Insights' }, // Phase 7: Insights
      { to: '/cases', label: 'Cases', owed: (o) => o.counts.cases },
      { to: '/documents', label: 'Documents' },
    ],
  },
  {
    label: 'Channels',
    items: [
      { to: '/chats', label: 'Chats', owed: (o) => o.counts.chats },
      { to: '/people', label: 'People', owed: (o) => o.counts.people },
    ],
  },
  {
    label: 'Knowledge',
    items: [
      { to: '/playbooks', label: 'Playbooks' },
      { to: '/skills', label: 'Skills' },
      { to: '/watches', label: 'Watches' },
      { to: '/data-sources', label: 'Data sources' },
      { to: '/memory', label: 'Memory' },
      { to: '/setup/interview', label: 'Interview', owed: (o) => o.counts.onboarding },
    ],
  },
  {
    label: 'System',
    items: [
      { to: '/system/runs', label: 'Runs' },
      { to: '/system/agents', label: 'Agents' },
      { to: '/system/logs', label: 'Logs' },
      { to: '/system/health', label: 'Health' },
      { to: '/system/setup', label: 'Setup', owed: (o) => o.counts.setup },
      { to: '/system/whatsapp', label: 'WhatsApp' },
    ],
  },
]

export default function AppShell() {
  return (
    <AskProvider>
      <AskParamOpener />
      <ShellInner />
    </AskProvider>
  )
}

// Phase 22: the dashboard's "Ask about this number" link lands here as ?ask=<text>. Read it once,
// prefill the Ask panel (never send), and take the parameter out of the address bar.
function AskParamOpener() {
  const ask = useAsk()
  useEffect(() => {
    takeAskParam(window, ask.open)
    // one shot on mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  return null
}

function ShellInner() {
  const owed = useOwed()
  const [open, setOpen] = useState(false)
  const location = useLocation()

  // Navigating on a phone should close the drawer; leaving it open over the
  // page the operator just asked for is the classic mobile-nav bug.
  useEffect(() => setOpen(false), [location.pathname])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  const { profile, resolved, health } = useProfile()
  const nav = !resolved ? [] : profile === 'intelligence' ? INTEL_NAV : NAV
  const brand = BRANDS[profile]
  const ask = useAsk()
  const showAsk = resolved && profile === 'intelligence'
  // Quick 003: the Ask panel docks beside the page instead of covering it.
  const askLabel = askButtonLabel(ask.working, ask.ready)

  return (
    <div className={`shell${showAsk && ask.isOpen ? ' ask-docked' : ''}`}>
      <div className="topbar">
        <button
          className="btn btn-quiet btn-icon"
          onClick={() => setOpen((v) => !v)}
          aria-label={open ? 'Close menu' : 'Open menu'}
          aria-expanded={open}
        >
          <MenuIcon />
        </button>
        <div className="wordmark" style={{ fontSize: 14 }}>
          {resolved ? brand.wordmark.join(' ') : ''}
        </div>
        <div className="grow" />
        {showAsk && (
          <button className="btn btn-quiet btn-sm ask-btn" onClick={() => ask.open()} aria-label={askLabel}>
            Ask
            <AskBadges working={ask.working} ready={ask.ready} />
          </button>
        )}
        {owed.counts.total > 0 && <span className="nav-owed">{owed.counts.total}</span>}
      </div>

      {open && <button className="scrim" onClick={() => setOpen(false)} aria-label="Close menu" />}

      <nav className={`rail${open ? ' is-open' : ''}`} aria-label="Sections">
        <NavLink to="/" className="rail-brand">
          {resolved &&
            brand.wordmark.map((line) => (
              <div className="wordmark" key={line}>
                {line}
              </div>
            ))}
          {resolved && brand.byline && (
            <div className="wordmark-sub">
              by {brand.byline}
            </div>
          )}
        </NavLink>

        <div className="rail-nav">
          {nav.map((group, i) => (
            <div className="nav-group" key={group.label ?? `g${i}`}>
              {group.label && <div className="nav-group-label">{group.label}</div>}
              {group.items.map((item) => {
                const count = item.owed ? item.owed(owed) : 0
                return (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    end={item.end}
                    className={({ isActive }) => `nav-item${isActive ? ' is-active' : ''}`}
                  >
                    <span className="nav-mark" />
                    <span className="nav-label">{item.label}</span>
                    {count > 0 && (
                      <span className="nav-owed" title={`${count} waiting on you`}>
                        {count}
                      </span>
                    )}
                  </NavLink>
                )
              })}
            </div>
          ))}
        </div>

        <div className="rail-foot">
          <span
            className={`dot ${health?.ok ? 'dot-settled' : health ? 'dot-broken' : ''}`}
            aria-hidden="true"
          />
          <span className="grow truncate" title={health?.reason || undefined}>
            {health?.ok ? 'Gateway healthy' : health ? 'Gateway degraded' : 'Checking…'}
          </span>
          {showAsk && (
            <button className="btn btn-quiet btn-sm ask-btn" onClick={() => ask.open()} aria-label={askLabel}>
              Ask
              <AskBadges working={ask.working} ready={ask.ready} />
            </button>
          )}
          <button className="btn btn-quiet btn-sm" onClick={clearKey}>
            Sign out
          </button>
        </div>
      </nav>

      <main className="page">
        <div className="page-inner">
          <Outlet />
        </div>
      </main>
    </div>
  )
}

function MenuIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
      <path d="M2 4.5h14M2 9h14M2 13.5h14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  )
}

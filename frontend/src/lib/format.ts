/* Turning backend values into something a person reads.
 *
 * Times are rendered in the browser's zone. The gateway has its own `timezone`
 * setting used for scheduling, but showing an operator a clock that isn't their
 * own is worse than showing one that occasionally differs from a cron entry —
 * every absolute time carries the full ISO string in a title attribute.
 */

export function ago(ts: string | null | undefined): string {
  if (!ts) return '—'
  const then = new Date(ts).getTime()
  if (Number.isNaN(then)) return '—'
  const s = (Date.now() - then) / 1000
  if (s < 0) return inFuture(-s)
  if (s < 45) return 'just now'
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  if (s < 604800) return `${Math.round(s / 86400)}d ago`
  return shortDate(ts)
}

function inFuture(s: number): string {
  if (s < 60) return 'in under a minute'
  if (s < 3600) return `in ${Math.round(s / 60)}m`
  if (s < 86400) return `in ${Math.round(s / 3600)}h`
  return `in ${Math.round(s / 86400)}d`
}

/** How overdue something is, phrased for a person who has to act on it. */
export function overdueBy(ts: string | null | undefined): string {
  if (!ts) return ''
  const s = (Date.now() - new Date(ts).getTime()) / 1000
  if (s <= 0) return ''
  if (s < 3600) return `${Math.round(s / 60)} minutes overdue`
  if (s < 86400) return `${Math.round(s / 3600)} hours overdue`
  const d = Math.round(s / 86400)
  return `${d} day${d === 1 ? '' : 's'} overdue`
}

export function clock(ts: string | null | undefined): string {
  if (!ts) return ''
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false })
}

export function shortDate(ts: string | null | undefined): string {
  if (!ts) return '—'
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
}

export function dateTime(ts: string | null | undefined): string {
  if (!ts) return '—'
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString(undefined, {
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false,
  })
}

export function isoTitle(ts: string | null | undefined): string {
  if (!ts) return ''
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? String(ts) : d.toISOString()
}

export function longDate(d: Date): string {
  return d.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' })
}

export function hourOf(ts: string | null | undefined): number | null {
  if (!ts) return null
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? null : d.getHours()
}

export function bytes(n: number | null | undefined): string {
  if (!n && n !== 0) return ''
  if (n < 1024) return `${n} B`
  if (n < 1048576) return `${(n / 1024).toFixed(1)} kB`
  return `${(n / 1048576).toFixed(1)} MB`
}

/* WhatsApp identifiers. A thread_id is a group JID (…@g.us) for a group and a
 * bare phone number for a direct chat — the one rule that makes replies,
 * history and echo detection land in the same place. The UI has to be able to
 * tell them apart to say "in Site B Ops" rather than printing a raw JID. */

export function isGroupJid(id: string | null | undefined): boolean {
  return typeof id === 'string' && id.endsWith('@g.us')
}

export function isJid(id: string | null | undefined): boolean {
  return typeof id === 'string' && id.includes('@')
}

/** A JID or phone shown the way an operator would say it out loud. */
export function threadLabel(id: string | null | undefined, subjects?: Map<string, string>): string {
  if (!id) return '—'
  if (id === 'dashboard') return 'Dashboard upload'
  // Quick 003: in-app Ask chats (ui:<operator>, ui:<operator>:<key>).
  if (id.startsWith('ui:')) return id.split(':').length > 2 ? 'Ask chat' : 'Ask (main chat)'
  if (isGroupJid(id)) {
    const subject = subjects?.get(id)
    return subject || 'Group chat'
  }
  if (isJid(id)) return phone(id.split('@')[0])
  return phone(id)
}

export function phone(n: string | null | undefined): string {
  if (!n) return '—'
  const digits = String(n).replace(/\D/g, '')
  if (!digits) return String(n)
  // Quick 017: a sender WhatsApp only identified by a private ID threads as
  // "<lid>@lid"; formatting it as +CC... would pass it off as a phone number.
  if (String(n).trim().toLowerCase().endsWith('@lid')) return `private ID ${digits}`
  // Country code plus a break before the subscriber part: long enough to scan,
  // short of pretending we know each country's grouping rules.
  if (digits.length > 10) {
    const cc = digits.slice(0, digits.length - 10)
    const rest = digits.slice(-10)
    return `+${cc} ${rest.slice(0, 5)} ${rest.slice(5)}`
  }
  return `+${digits}`
}

export function initials(name: string | null | undefined): string {
  if (!name) return '?'
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]!.toUpperCase())
    .join('')
}

/** Snake-case backend enums, spoken plainly. */
export function humanize(value: string | null | undefined): string {
  if (!value) return ''
  return value.replace(/_/g, ' ')
}

export function titleCase(value: string | null | undefined): string {
  const h = humanize(value)
  return h ? h[0]!.toUpperCase() + h.slice(1) : ''
}

export function truncate(text: string | null | undefined, n: number): string {
  const s = String(text ?? '')
  return s.length > n ? `${s.slice(0, n - 1)}…` : s
}

export function plural(n: number, one: string, many?: string): string {
  return `${n} ${n === 1 ? one : many || `${one}s`}`
}

// Phase 22 (22-12): the "Ask about this number" link lands on the app as /?ask=<text>. The text is
// only ever prefilled into the Ask panel for the owner to read and send; it is never sent for them.
// Pure, so it is tested without a DOM.

const MAX_ASK = 500
// control characters (including newlines) and bidi overrides
const STRIP = /[\u0000-\u001F\u007F-\u009F\u202A-\u202E\u2066-\u2069]/g

export function readAskParam(search: string): string | null {
  let raw: string | null
  try {
    raw = new URLSearchParams(search).get('ask')
  } catch {
    return null
  }
  if (raw === null) return null
  const cleaned = raw.replace(STRIP, ' ').replace(/\s+/g, ' ').trim()
  if (!cleaned) return null
  // cut by code points so no surrogate pair is split
  const cut = Array.from(cleaned).slice(0, MAX_ASK).join('').trim()
  return cut || null
}

// Read ?ask= once: hand the text to `open` (a prefill, never a send) and take `ask` out of the address bar.
export interface AskWindow {
  location: { search: string; pathname: string; hash: string }
  history: { state: unknown; replaceState: (state: unknown, title: string, url: string) => void }
}
export function takeAskParam(win: AskWindow, open: (text: string) => void): boolean {
  const text = readAskParam(win.location.search)
  if (text === null) return false
  open(text)
  const search = withoutAskParam(win.location.search)
  win.history.replaceState(win.history.state, '', win.location.pathname + search + win.location.hash)
  return true
}

// The address-bar search string without `ask` (the rest of the query is kept as it was).
export function withoutAskParam(search: string): string {
  const p = new URLSearchParams(search)
  if (!p.has('ask')) return search
  p.delete('ask')
  const rest = p.toString()
  return rest ? '?' + rest : ''
}

export type Profile = 'intelligence' | 'second_brain'

export interface Brand {
  name: string
  shortName: string
  wordmark: string[]
  /** Optional "by <name>" line under the rail wordmark; empty string shows nothing. */
  byline: string
  icon: string
  manifest: string
  appleIcon: string
  tagline: string
  signInLede: string
}

export const BRANDS: Record<Profile, Brand> = {
  second_brain: {
    name: 'Second Brain',
    shortName: 'Second Brain',
    wordmark: ['SECOND BRAIN'],
    byline: '',
    icon: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%23080C14'/%3E%3Crect x='14.4' y='7' width='1.4' height='18' fill='%232B7FD4'/%3E%3Ccircle cx='21' cy='12' r='2.6' fill='%23D4952B'/%3E%3C/svg%3E",
    manifest: '/manifest.webmanifest',
    appleIcon: '/apple-touch-icon.png',
    tagline: 'What your assistant handled, and what it needs you for.',
    signInLede:
      'This instance is unlocked with its ops key — the admin key printed once when the server was installed.',
  },
  intelligence: {
    name: 'Intelligence Corner',
    shortName: 'Intelligence',
    // Two lines: one line overflows the 188 px rail.
    wordmark: ['INTELLIGENCE', 'CORNER'],
    byline: '',
    // No gold: gold means a human owes something, never a brand mark.
    icon: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%23080C14'/%3E%3Cpath d='M23 8H9v14' fill='none' stroke='%232B7FD4' stroke-width='2.2' stroke-linecap='square'/%3E%3Crect x='12' y='11' width='4' height='4' fill='%23E6EDF7'/%3E%3C/svg%3E",
    manifest: '/manifest-intelligence.webmanifest',
    appleIcon: '/apple-touch-icon-ic.png',
    tagline: 'How the business is doing, what it noticed, and what it wants approved.',
    signInLede:
      'Intelligence Corner is unlocked with its ops key — the admin key printed once when the server was installed.',
  },
}

/** Apply the brand to the document head. Missing elements are skipped. */
export function applyBrand(profile: Profile, doc: Document = document): void {
  const b = BRANDS[profile]
  doc.title = b.name
  const set = (sel: string, attr: string, value: string) => {
    const el = doc.querySelector(sel)
    if (el) el.setAttribute(attr, value)
  }
  set('link[rel="icon"]', 'href', b.icon)
  set('link[rel="manifest"]', 'href', b.manifest)
  set('link[rel="apple-touch-icon"]', 'href', b.appleIcon)
  set('meta[name="apple-mobile-web-app-title"]', 'content', b.name)
}

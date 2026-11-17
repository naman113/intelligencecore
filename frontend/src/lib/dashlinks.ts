import { get } from './api'
import { useResource } from './hooks'

/* Links to the separate dashboards origin. Tokens are only ever held in
 * component state or the fetched payload: never stored, never logged. */

type Loc = { protocol: string; hostname: string; port: string }

export function normalizeBase(value: string | null | undefined): string | null {
  const v = String(value ?? '').trim()
  if (!/^https?:\/\//i.test(v)) return null
  try {
    const u = new URL(v)
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null
    return v.replace(/\/+$/, '')
  } catch {
    return null
  }
}

export function guessDashboardsBase(loc: Loc): string | null {
  const { protocol, hostname, port } = loc
  if (hostname.startsWith('app.')) {
    return `${protocol}//dash.${hostname.slice(4)}`
  }
  if (port === '8090') return `${protocol}//${hostname}:8091`
  // 9NN0 -> 9NN2 (ic.sh stacks: int 9000, pN 90N0 / 9NN0)
  const m = /^9(\d\d)0$/.exec(port)
  if (m) return `${protocol}//${hostname}:9${m[1]}2`
  return null
}

export function dashboardsBase(configured: string | null | undefined, loc?: Loc): string | null {
  const c = normalizeBase(configured)
  if (c) return c
  const l = loc ?? (typeof window !== 'undefined' ? window.location : undefined)
  return l ? guessDashboardsBase(l) : null
}

interface CfgField {
  key: string
  value: unknown
}

export function useDashboardsBase(): { base: string | null; configured: boolean; loading: boolean } {
  const { data, loading } = useResource(() =>
    get<{ config?: Record<string, CfgField[]> }>('/api/config'),
  )
  let value: string | null = null
  for (const fields of Object.values(data?.config ?? {})) {
    for (const f of fields ?? []) {
      if (f.key === 'dashboards_public_url' && typeof f.value === 'string') value = f.value
    }
  }
  const configured = normalizeBase(value) !== null
  return { base: dashboardsBase(value), configured, loading }
}

const enc = encodeURIComponent

export function currentLink(base: string | null, slug: string, token: string): string | null {
  if (!base || !token) return null
  return `${base}/d/${enc(slug)}/?t=${enc(token)}`
}

export function versionLink(
  base: string | null,
  slug: string,
  version: number,
  token: string,
): string | null {
  if (!base || !token) return null
  return `${base}/d/${enc(slug)}/v/${version}/?t=${enc(token)}`
}

/** `path` is the version path returned by the API, "/d/{slug}/v/{n}/". */
export function draftLink(base: string | null, path: string, token: string): string | null {
  if (!base || !token) return null
  const p = path.startsWith('/') ? path : `/${path}`
  return `${base}${p}?p=${enc(token)}`
}


/* Origin rule: the configured address wins; a relative path resolves against the
 * configured or guessed base; an absolute URL is linked only on that same origin,
 * never on an origin the payload chose (review WR-02). */
export function payloadDraftLink(
  draftUrl: string | null,
  base: string | null,
  configured: boolean,
): string | null {
  const raw = String(draftUrl ?? '').trim()
  if (!raw || raw.startsWith('//')) return null
  let url: URL
  let relative = false
  try {
    if (raw.startsWith('/')) {
      relative = true
      url = new URL(raw, 'http://relative.invalid')
    } else {
      url = new URL(raw)
    }
  } catch {
    return null
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null
  if (!/^\/d\/[^/?#]+\/v\/\d+\/$/.test(url.pathname)) return null
  const p = url.searchParams.get('p')
  if (!p) return null
  let origin: string | null
  if (relative) origin = base
  else if (configured && base) origin = base
  else {
    try {
      origin = base && new URL(base).origin === url.origin ? base : null
    } catch {
      return null
    }
  }
  if (!origin) return null
  return `${origin}${url.pathname}?p=${enc(p)}`
}

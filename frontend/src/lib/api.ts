/* The one place that talks to the gateway.
 *
 * Same-origin by design: nginx (and the Vite dev proxy) put /api on this
 * origin, so the ops key rides as a plain header with no CORS involved and
 * never appears in a URL.
 */

const KEY_STORAGE = 'secondbrain.opskey'

let opsKey: string = localStorage.getItem(KEY_STORAGE) || ''

type AuthListener = (signedIn: boolean) => void
const authListeners = new Set<AuthListener>()

export function getKey(): string {
  return opsKey
}

export function setKey(key: string): void {
  opsKey = key.trim()
  localStorage.setItem(KEY_STORAGE, opsKey)
  authListeners.forEach((fn) => fn(Boolean(opsKey)))
}

export function clearKey(): void {
  opsKey = ''
  localStorage.removeItem(KEY_STORAGE)
  authListeners.forEach((fn) => fn(false))
}

export function onAuthChange(fn: AuthListener): () => void {
  authListeners.add(fn)
  return () => authListeners.delete(fn)
}

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/* FastAPI puts the actionable message in `detail`. Surfacing the bare status
 * instead turns every failure into "error: 422" next to the button that caused
 * it, hiding a message the backend went to real trouble to produce. */
async function errorMessage(res: Response): Promise<string> {
  try {
    const body = await res.json()
    const detail = body?.detail
    if (typeof detail === 'string') return detail
    if (detail && typeof detail.message === 'string') return detail.message // Quick 023: {message, ...}
    if (detail) return JSON.stringify(detail)
  } catch {
    /* not JSON — fall through to the status line */
  }
  if (res.status === 401) return 'That ops key was not accepted.'
  if (res.status === 404) return 'Not found.'
  return `Request failed (HTTP ${res.status}).`
}

/* A 401 anywhere means the stored key is no longer good. Drop it once, here,
 * so every caller doesn't have to; the shell listens and shows sign-in. */
function handleUnauthorized(): void {
  if (opsKey) clearKey()
}

export async function api<T = unknown>(path: string, opts: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...opts,
    headers: {
      'x-ops-key': opsKey,
      'content-type': 'application/json',
      ...(opts.headers || {}),
    },
  })
  if (res.status === 401) {
    handleUnauthorized()
    throw new ApiError(await errorMessage(res), 401)
  }
  if (!res.ok) throw new ApiError(await errorMessage(res), res.status)
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export const get = <T>(path: string) => api<T>(path)
export const post = <T>(path: string, body: unknown = {}) =>
  api<T>(path, { method: 'POST', body: JSON.stringify(body) })
export const put = <T>(path: string, body: unknown) =>
  api<T>(path, { method: 'PUT', body: JSON.stringify(body) })
export const patch = <T>(path: string, body: unknown) =>
  api<T>(path, { method: 'PATCH', body: JSON.stringify(body) })
export const del = <T>(path: string) => api<T>(path, { method: 'DELETE' })

/* Multipart must NOT carry a JSON content-type — the browser has to set its own
 * boundary — so this one omits it and passes the FormData straight on. */
export async function upload<T>(path: string, form: FormData): Promise<T> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'x-ops-key': opsKey },
    body: form,
  })
  if (res.status === 401) {
    handleUnauthorized()
    throw new ApiError(await errorMessage(res), 401)
  }
  if (!res.ok) throw new ApiError(await errorMessage(res), res.status)
  return (await res.json()) as T
}

/* Artifact routes are ops-key gated and a plain <a href> cannot set a header,
 * so fetch the bytes and hand the browser a blob. */
export async function download(path: string, filename: string): Promise<void> {
  const res = await fetch(path, { headers: { 'x-ops-key': opsKey } })
  if (!res.ok) throw new ApiError(await errorMessage(res), res.status)
  const url = URL.createObjectURL(await res.blob())
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

/* EventSource cannot set headers, so the log stream is consumed by reading the
 * fetch body directly and splitting on the SSE record separator. */
export async function streamEvents(
  path: string,
  onEvent: (data: unknown) => void,
  signal: AbortSignal,
): Promise<void> {
  const res = await fetch(path, { headers: { 'x-ops-key': opsKey }, signal })
  if (!res.ok) throw new ApiError(await errorMessage(res), res.status)
  if (!res.body) throw new ApiError('This browser cannot read the log stream.', 0)

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const records = buffer.split('\n\n')
    buffer = records.pop() ?? ''
    for (const record of records) {
      if (!record.startsWith('data: ')) continue // keepalive comment
      try {
        onEvent(JSON.parse(record.slice(6)))
      } catch {
        /* a partial record: the next read completes it */
      }
    }
  }
}

/* asyncpg hands jsonb back as a STRING unless a codec is registered, so every
 * payload reader has to cope with both shapes. */
export function asObj<T = Record<string, unknown>>(value: unknown): T {
  if (value && typeof value === 'object') return value as T
  if (typeof value === 'string') {
    try {
      return JSON.parse(value) as T
    } catch {
      return {} as T
    }
  }
  return {} as T
}

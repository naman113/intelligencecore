import { useCallback, useEffect, useRef, useState } from 'react'

/* Polling that stops when nobody is looking.
 *
 * Several pages poll a handful of endpoints each. Left running in a background
 * tab that is open all day, they'd hammer a gateway that shares a process with
 * the agent loops — so the timer pauses on hide and refetches immediately on
 * show, which is also what the operator wants when they come back to the tab.
 */
export function useResource<T>(
  load: () => Promise<T>,
  opts: { interval?: number; deps?: unknown[]; enabled?: boolean } = {},
): {
  data: T | null
  error: string | null
  loading: boolean
  reload: () => Promise<void>
} {
  const { interval, deps = [], enabled = true } = opts

  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  // The loader is usually an inline lambda, which is a new function every
  // render; holding it in a ref keeps the effect keyed on `deps` alone.
  const loadRef = useRef(load)
  loadRef.current = load

  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  // Quick 013: a response that started before `deps` changed is dropped, so a slow
  // answer for the previous selection never lands after the current one.
  const generation = useRef(0)

  const reload = useCallback(async () => {
    const gen = generation.current
    try {
      const next = await loadRef.current()
      if (!alive.current || gen !== generation.current) return
      setData(next)
      setError(null)
    } catch (e) {
      if (!alive.current || gen !== generation.current) return
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (alive.current && gen === generation.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    generation.current += 1
    if (!enabled) {
      setLoading(false)
      return
    }
    setLoading(true)
    void reload()

    if (!interval) return

    let timer: number | undefined
    const start = () => {
      stop()
      timer = window.setInterval(() => void reload(), interval)
    }
    const stop = () => {
      if (timer !== undefined) window.clearInterval(timer)
      timer = undefined
    }
    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        void reload()
        start()
      } else {
        stop()
      }
    }

    if (document.visibilityState === 'visible') start()
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      stop()
      document.removeEventListener('visibilitychange', onVisibility)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, interval, reload, ...deps])

  return { data, error, loading, reload }
}

/* Mutations: one pending flag and one error message per call site, so a button
 * can disable itself and say what went wrong in place rather than in an alert. */
export function useAction(): {
  run: (fn: () => Promise<unknown>) => Promise<boolean>
  pending: boolean
  error: string | null
  clearError: () => void
} {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = useCallback(async (fn: () => Promise<unknown>) => {
    setPending(true)
    setError(null)
    try {
      await fn()
      return true
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      return false
    } finally {
      setPending(false)
    }
  }, [])

  return { run, pending, error, clearError: () => setError(null) }
}

/* Matches a CSS media query in JS, for the places where a layout genuinely
 * changes behaviour (list/detail split becoming a push) rather than just
 * reflowing. */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches)
  useEffect(() => {
    const mql = window.matchMedia(query)
    const onChange = () => setMatches(mql.matches)
    onChange()
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [query])
  return matches
}

/* Remembers a value between visits (filter selections, follow toggles). */
export function useStored<T>(key: string, initial: T): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(`secondbrain.${key}`)
      return raw === null ? initial : (JSON.parse(raw) as T)
    } catch {
      return initial
    }
  })
  const set = useCallback(
    (v: T) => {
      setValue(v)
      try {
        localStorage.setItem(`secondbrain.${key}`, JSON.stringify(v))
      } catch {
        /* private mode: keep the in-memory value */
      }
    },
    [key],
  )
  return [value, set]
}

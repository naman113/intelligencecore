import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { get } from './api'
import { applyBrand, type Profile } from './brand'
import { useResource } from './hooks'

export const PROFILE_STORAGE = 'secondbrain.ui_profile'

export interface Health {
  ok: boolean
  degraded?: boolean
  reason?: string
  ui_profile?: string
}

export function normalizeProfile(v: unknown): Profile {
  return String(v ?? '').trim().toLowerCase() === 'second_brain' ? 'second_brain' : 'intelligence'
}

export function readCachedProfile(): Profile | null {
  try {
    const raw = localStorage.getItem(PROFILE_STORAGE)
    return raw ? normalizeProfile(raw) : null
  } catch {
    return null
  }
}

export function resolveProfile(input: {
  health: Health | null
  healthFailed: boolean
  cached: Profile | null
  timedOut: boolean
}): { profile: Profile; resolved: boolean } {
  const { health, healthFailed, cached, timedOut } = input
  if (health && typeof health.ui_profile === 'string') {
    return { profile: normalizeProfile(health.ui_profile), resolved: true }
  }
  if (cached) return { profile: cached, resolved: true }
  if (health || healthFailed || timedOut) return { profile: 'intelligence', resolved: true }
  return { profile: 'intelligence', resolved: false }
}

export interface ProfileValue {
  profile: Profile
  resolved: boolean
  health: Health | null
  reload: () => Promise<void>
}

export const ProfileContext = createContext<ProfileValue>({
  profile: 'intelligence',
  resolved: true,
  health: null,
  reload: async () => {},
})

export function ProfileProvider({ children }: { children: ReactNode }) {
  const { data, error, reload } = useResource<Health>(() => get<Health>('/health'), {
    interval: 20_000,
  })
  const [timedOut, setTimedOut] = useState(false)
  const [cached] = useState<Profile | null>(() => readCachedProfile())
  useEffect(() => {
    const t = setTimeout(() => setTimedOut(true), 3000)
    return () => clearTimeout(t)
  }, [])
  const health = data ?? null
  const { profile, resolved } = resolveProfile({
    health,
    healthFailed: !!error && !data,
    cached,
    timedOut,
  })
  useEffect(() => {
    if (resolved) applyBrand(profile)
    // Cache only from a real /health answer, never from the fallback.
    if (typeof health?.ui_profile === 'string') {
      try {
        localStorage.setItem(PROFILE_STORAGE, profile)
      } catch {
        /* storage unavailable */
      }
    }
  }, [profile, resolved, health?.ui_profile])
  return (
    <ProfileContext.Provider
      value={{ profile, resolved, health, reload: async () => void (await reload()) }}
    >
      {children}
    </ProfileContext.Provider>
  )
}

export function useProfile(): ProfileValue {
  return useContext(ProfileContext)
}

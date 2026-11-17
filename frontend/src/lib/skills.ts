/* Phase 12 (UI-11): the View button on an installed skill row loads the full
 * SKILL.md on demand. The list request stays body-free; only this call
 * fetches a body, and the caller renders it as React text. */

export function skillDetailPath(name: string): string {
  return '/api/skills/' + encodeURIComponent(name)
}

export async function loadSkillBody(
  getFn: <T>(path: string) => Promise<T>,
  name: string,
): Promise<{ id: string; body: string }> {
  const row = await getFn<{ body?: string | null }>(skillDetailPath(name))
  return { id: name, body: row?.body || '(empty SKILL.md)' }
}

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { INTEL_NAV } from '../src/lib/nav'

test('INTEL_NAV structure', () => {
  assert.deepEqual(
    INTEL_NAV.map((g) => g.label),
    [null, 'Intelligence', 'Business context', 'Agents', 'Operations', 'System'],
  )
  assert.deepEqual(
    INTEL_NAV.map((g) => g.items.map((i) => i.label)),
    [
      ['Overview'],
      ['Dashboards', 'KPIs', 'Watches', 'Forecasts', 'Insights', 'Cases', 'Approvals'],
      ['Data sources', 'Schema', 'Rules & KPI definitions', 'Model', 'Learnings'],
      ['Playbooks', 'Skills', 'Agents'],
      ['Chats', 'People', 'Documents', 'Interview', 'WhatsApp'],
      ['Runs', 'Usage', 'Logs', 'Health', 'Setup'],
    ],
  )
  assert.deepEqual(
    INTEL_NAV.flatMap((g) => g.items.map((i) => i.to)),
    ['/', '/dashboards', '/kpis', '/watches', '/forecasts', '/insights', '/cases', '/approvals', '/context/sources', '/context/schema',
      '/context/rules', '/context/model', '/memory', '/playbooks', '/skills', '/system/agents', '/chats',
      '/people', '/documents', '/setup/interview', '/system/whatsapp', '/system/runs',
      '/system/usage', '/system/logs', '/system/health', '/system/setup'],
  )
  assert.equal(INTEL_NAV[0].items[0].end, true)
})

test('Second Brain NAV in AppShell is untouched', () => {
  const src = readFileSync('src/components/AppShell.tsx', 'utf8')
  const start = src.indexOf('const NAV: NavGroup[] = [')
  assert.ok(start >= 0)
  const block = src.slice(start)
  const item = /\{ to: '[^']+', label: '([^']+)'/g
  const labels = [...block.matchAll(item)].map((m) => m[1])
  const want = ['Today', 'Approvals', 'Insights', 'Cases', 'Documents', 'Chats', 'People', 'Playbooks', 'Skills',
    'Watches', 'Data sources', 'Memory', 'Interview', 'Runs', 'Agents', 'Logs', 'Health', 'Setup', 'WhatsApp']
  assert.deepEqual(labels.slice(0, want.length), want)
})

test('INTEL_NAV badges read the exact owed counts', async () => {
  const { INTEL_NAV: nav } = await import('../src/lib/nav')
  const o = {
    counts: { total: 9, dashboards: 2, approvalsAll: 7, schema: 3, rules: 1, approvals: 0, cases: 0, chats: 0, people: 0, onboarding: 0, setup: 0, whatsapp: 1 },
  } as never
  const by = (label: string) => nav.flatMap((g) => g.items).find((i) => i.label === label)?.owed?.(o)
  assert.equal(by('Overview'), 9)
  assert.equal(by('Dashboards'), 2)
  assert.equal(by('Approvals'), 7)
  assert.equal(by('Schema'), 3)
  assert.equal(by('Rules & KPI definitions'), 1)
  assert.equal(by('WhatsApp'), 1)
})

test('Phase 13.1: Forecasts badge reads counts.forecasts', () => {
  const item = INTEL_NAV.flatMap((g) => g.items).find((i) => i.label === 'Forecasts')
  assert.equal(item?.to, '/forecasts')
  assert.equal(item?.owed?.({ counts: { forecasts: 3 } } as never), 3)
})

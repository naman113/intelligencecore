import type { Owed } from './owed'

export interface NavEntry {
  to: string
  label: string
  owed?: (o: Owed) => number
  end?: boolean
}

export interface NavGroup {
  label: string | null
  items: NavEntry[]
}

export const INTEL_NAV: NavGroup[] = [
  {
    label: null,
    items: [{ to: '/', label: 'Overview', end: true, owed: (o) => o.counts.total }],
  },
  {
    label: 'Intelligence',
    items: [
      { to: '/dashboards', label: 'Dashboards', owed: (o) => o.counts.dashboards },
      { to: '/kpis', label: 'KPIs' },
      // Quick 050: every watch (KPI, user, scheduled checks) with pause and resume.
      { to: '/watches', label: 'Watches' },
      { to: '/forecasts', label: 'Forecasts', owed: (o) => o.counts.forecasts }, // Phase 13.1
      { to: '/insights', label: 'Insights' },
      // Quick 006: cases now open from KPI alerts and insights, not only from chats.
      { to: '/cases', label: 'Cases', owed: (o) => o.counts.cases },
      { to: '/approvals', label: 'Approvals', owed: (o) => o.counts.approvalsAll },
    ],
  },
  {
    label: 'Business context',
    items: [
      { to: '/context/sources', label: 'Data sources', owed: (o) => o.counts.sources ?? 0 },
      { to: '/context/schema', label: 'Schema', owed: (o) => o.counts.schema },
      { to: '/context/rules', label: 'Rules & KPI definitions', owed: (o) => o.counts.rules },
      { to: '/context/model', label: 'Model' },
      { to: '/memory', label: 'Learnings' },
    ],
  },
  {
    label: 'Agents',
    items: [
      { to: '/playbooks', label: 'Playbooks' },
      { to: '/skills', label: 'Skills' },
      { to: '/system/agents', label: 'Agents' },
    ],
  },
  {
    label: 'Operations',
    items: [
      { to: '/chats', label: 'Chats', owed: (o) => o.counts.chats },
      { to: '/people', label: 'People', owed: (o) => o.counts.people },
      { to: '/documents', label: 'Documents' },
      { to: '/setup/interview', label: 'Interview', owed: (o) => o.counts.onboarding },
      { to: '/system/whatsapp', label: 'WhatsApp', owed: (o) => o.counts.whatsapp },
    ],
  },
  {
    label: 'System',
    items: [
      { to: '/system/runs', label: 'Runs' },
      { to: '/system/usage', label: 'Usage' },
      { to: '/system/logs', label: 'Logs' },
      { to: '/system/health', label: 'Health' },
      { to: '/system/setup', label: 'Setup', owed: (o) => o.counts.setup },
    ],
  },
]

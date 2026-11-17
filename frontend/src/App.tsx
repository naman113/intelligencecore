import { useEffect, useState } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import AppShell from './components/AppShell'
import { getKey, onAuthChange } from './lib/api'
import { OwedProvider } from './lib/owed'
import { ProfileProvider, useProfile } from './lib/profile'

import SignIn from './pages/SignIn'
import Today from './pages/Today'
import Approvals from './pages/Approvals'
import Insights from './pages/Insights' // Phase 7: Insights
import Cases from './pages/Cases'
import Documents from './pages/Documents'
import Chats from './pages/Chats'
import People from './pages/People'
import Playbooks from './pages/Playbooks'
import Skills from './pages/Skills'
import Watches from './pages/Watches'
import DataSources from './pages/DataSources'
import Memory from './pages/Memory'
import Interview from './pages/Interview'
import Runs from './pages/Runs'
import Usage from './pages/Usage'
import Agents from './pages/Agents'
import Logs from './pages/Logs'
import Health from './pages/Health'
import Setup from './pages/Setup'
import WhatsApp from './pages/WhatsApp'
import Overview from './pages/Overview'
import Kpis from './pages/Kpis'
import Forecasts from './pages/Forecasts' // Phase 13.1
import InsightsPage from './pages/InsightsPage'
import Dashboards from './pages/Dashboards'
import ContextSources from './pages/ContextSources'
import Schema from './pages/Schema'
import Rules from './pages/Rules'
import Model from './pages/Model' // Phase 16

// Phase 8: the home route follows the product profile
function Home() {
  const { profile } = useProfile()
  return profile === 'intelligence' ? <Overview /> : <Today />
}

// Merge fix: Phase 7 and Phase 8 both own /insights. Intelligence Corner gets the
// Phase 8 page, Second Brain keeps the Phase 7 page.
function InsightsRoute() {
  const { profile } = useProfile()
  return profile === 'intelligence' ? <InsightsPage /> : <Insights />
}

export default function App() {
  const [signedIn, setSignedIn] = useState(() => Boolean(getKey()))

  // api.ts drops the stored key on any 401, so an expired or rotated key sends
  // the operator back here from wherever they were rather than leaving a page
  // of failed panels.
  useEffect(() => onAuthChange(setSignedIn), [])

  if (!signedIn) return (
      <ProfileProvider>
        <SignIn />
      </ProfileProvider>
    )

  return (
    <ProfileProvider>
    <OwedProvider>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Home />} />
          <Route path="approvals" element={<Approvals />} />
          <Route path="insights" element={<InsightsRoute />} /> {/* Phase 7 + 8: Insights */}
          <Route path="cases" element={<Cases />} />
          <Route path="cases/:id" element={<Cases />} />
          <Route path="documents" element={<Documents />} />
          <Route path="documents/:id" element={<Documents />} />
          <Route path="chats" element={<Chats />} />
          <Route path="people" element={<People />} />
          <Route path="playbooks" element={<Playbooks />} />
          <Route path="skills" element={<Skills />} />
          <Route path="watches" element={<Watches />} />
          <Route path="data-sources" element={<DataSources />} />
          <Route path="memory" element={<Memory />} />
          <Route path="setup/interview" element={<Interview />} />
          <Route path="system/runs" element={<Runs />} />
          <Route path="system/runs/:id" element={<Runs />} />
          <Route path="system/usage" element={<Usage />} />
          <Route path="system/agents" element={<Agents />} />
          <Route path="system/logs" element={<Logs />} />
          <Route path="system/health" element={<Health />} />
          <Route path="system/setup" element={<Setup />} />
          <Route path="system/whatsapp" element={<WhatsApp />} />
          {/* ── Phase 8: Intelligence Corner routes ── */}
          <Route path="today" element={<Today />} />
          <Route path="kpis" element={<Kpis />} />
          <Route path="kpis/:id" element={<Kpis />} />
          {/* Phase 13.1: forecasts */}
          <Route path="forecasts" element={<Forecasts />} />
          <Route path="forecasts/:id" element={<Forecasts />} />
          <Route path="dashboards" element={<Dashboards />} />
          <Route path="dashboards/:slug" element={<Dashboards />} />
          <Route path="context/sources" element={<ContextSources />} />
          <Route path="context/schema" element={<Schema />} />
          <Route path="context/rules" element={<Rules />} />
          {/* ── end Phase 8 ── */}
          {/* ── Phase 16: Model page ── */}
          <Route path="context/model" element={<Model />} />
          {/* ── end Phase 16 ── */}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </OwedProvider>
    </ProfileProvider>
  )
}

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

// Fonts ship inside the image. A client VM may have no outbound internet, and a
// dashboard that silently falls back to system sans on the machine it was
// installed on is not the design that was signed off.
// The wdth build, not the default wght-only one: the display face is set
// slightly expanded, which is what stops it reading like every other grotesque
// in every other dashboard.
import '@fontsource-variable/archivo/wdth.css'
import '@fontsource/ibm-plex-sans/400.css'
import '@fontsource/ibm-plex-sans/500.css'
import '@fontsource/ibm-plex-sans/600.css'
import '@fontsource/ibm-plex-mono/400.css'
import '@fontsource/ibm-plex-mono/500.css'

import './design/tokens.css'
import './design/base.css'
import './design/components.css'
import App from './App'
import { applyBrand } from './lib/brand'
import { readCachedProfile } from './lib/profile'

// Known profile on this device: set the manifest before first paint. No cache:
// index.html's head stays until ProfileProvider resolves.
const cached = readCachedProfile()
if (cached) applyBrand(cached)

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {/* Opted in now so the console stays clean and the v7 upgrade is a no-op. */}
    <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <App />
    </BrowserRouter>
  </StrictMode>,
)

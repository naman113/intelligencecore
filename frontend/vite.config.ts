import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev server proxies to the gateway so `npm run dev` is same-origin exactly like
// the nginx container is. Without this the ops-key header trips CORS preflight
// against a backend that has no CORS middleware at all.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8080', changeOrigin: true },
      '/health': { target: 'http://localhost:8080', changeOrigin: true },
    },
  },
  build: { outDir: 'dist', sourcemap: false },
})

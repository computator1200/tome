import path from 'path'
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // Dev proxy target for /api and /files. Set VITE_API_TARGET in .env.development
  // (defaults to :8000). On this machine the backend runs on :8077 because
  // :8000 is taken by Docker Desktop.
  const env = loadEnv(mode, process.cwd(), '')
  const apiTarget = env.VITE_API_TARGET || 'http://localhost:8000'

  return {
    // Deployed under /tome behind Caddy; relative to the domain root.
    base: env.VITE_BASE || '/',
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    server: {
      host: '0.0.0.0', // listen on LAN so other devices can connect
      proxy: {
        '/api': apiTarget,
        '/files': apiTarget,
      },
    },
  }
})

import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// the API server the dev proxy talks to; override with PLAINSOLID_API=http://127.0.0.1:8330
const API = process.env.PLAINSOLID_API ?? 'http://127.0.0.1:8321';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5199,
    strictPort: true,
    proxy: {
      '/api': { target: API, changeOrigin: true, ws: true },
    },
  },
  build: {
    outDir: '../src/plainsolid/static',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1500,
  },
});

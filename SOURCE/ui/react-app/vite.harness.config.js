// Standalone build config for the #2607 CloudWelcome Playwright proof.
//
// Deliberately NOT a variant of vite.config.js's `base: '/static/react/'`
// (that base plus its `/static` dev-proxy rule collide when serving a
// second, non-desktop-backend entry point in dev mode). This produces a
// small standalone static bundle for cloud-welcome-harness.html only,
// served same-origin alongside the real /v1/cloud-welcome/* routes by
// tests/e2e/web/cloud_welcome/backend_harness.py -- no dev-server proxy
// needed. `npm run build` (the real production build) is untouched: it
// still uses vite.config.js and its default single index.html input.
//
// Build once with:
//   npx vite build --config vite.harness.config.js
import { defineConfig } from 'vite';
import path from 'path';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    outDir: '../../tests/e2e/web/cloud_welcome/harness_dist',
    emptyOutDir: true,
    rollupOptions: {
      input: path.resolve(__dirname, 'cloud-welcome-harness.html'),
    },
  },
});

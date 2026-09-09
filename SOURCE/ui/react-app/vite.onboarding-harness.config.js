// Standalone build config for the #4225 guest-first-run Playwright proof.
//
// Mirrors vite.harness.config.js (the #2607 CloudWelcome proof) rather than
// extending it: each harness builds exactly one entry point into its own
// dist, so the two proofs never share or clobber a bundle. Deliberately NOT a
// variant of vite.config.js's `base: '/static/react/'` -- this bundle is
// served from the root of a standalone static server alongside the stubbed
// /v1/* routes, with no dev-server proxy involved.
//
// `npm run build` (the real production build) is untouched: it still uses
// vite.config.js and its default single index.html input, so neither harness
// entry point can reach a shipped installer.
//
// Build once with:
//   npx vite build --config vite.onboarding-harness.config.js
import { defineConfig } from 'vite';
import path from 'path';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    outDir: '../../tests/e2e/web/onboarding_guest/harness_dist',
    emptyOutDir: true,
    rollupOptions: {
      input: path.resolve(__dirname, 'onboarding-guest-harness.html'),
    },
  },
});

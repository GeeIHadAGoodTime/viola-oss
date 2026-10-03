import { defineConfig, loadEnv } from 'vite'
import { execSync } from 'child_process'
import fs from 'fs'
import path from 'path'
import react from '@vitejs/plugin-react'
import { sentryVitePlugin } from '@sentry/vite-plugin'
import { resolveYouTubeEmbedUrl } from './src/utils/youtubeEmbed.js'

// Auto-detect HTTPS: if self-signed certs exist, proxy to https backend
const projectRoot = path.resolve(__dirname, '../..')
const certExists = fs.existsSync(path.join(projectRoot, 'data/secrets/viola_cert.pem'))
const scheme = certExists ? 'https' : 'http'
const wsScheme = certExists ? 'wss' : 'ws'
const backendUrl = process.env.VITE_BACKEND_URL || `${scheme}://127.0.0.1:8756`
const wsUrl = process.env.VITE_BACKEND_WS_URL || `${wsScheme}://127.0.0.1:8756`
const versionFile = path.join(projectRoot, 'core/constants.py')

function readViolaVersion() {
  try {
    const contents = fs.readFileSync(versionFile, 'utf8')
    const match = contents.match(/VIOLA_VERSION\s*=\s*["']([^"']+)["']/)
    return match?.[1] || '0.0.0'
  } catch {
    return '0.0.0'
  }
}

function readGitSha() {
  try {
    return execSync('git rev-parse --short HEAD', { cwd: projectRoot, stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim()
  } catch {
    return 'dev'
  }
}

// ViolaWake ONNX model assets for the in-tab wake word. Served from the repo's
// canonical TRACKED model locations (models/wake/, violawake_data/
// trained_models/) instead of duplicating the binaries under public/ —
// .gitignore excludes *.onnx there, and one source of truth beats a second
// tracked copy. (The ORT wasm runtime itself needs no plugin: the
// `onnxruntime-web/wasm` bundle references its .wasm via import.meta.url, so
// Vite emits it as a same-origin hashed asset automatically.)
function wakeModelAssets() {
  const wakeModels = {
    'melspectrogram.onnx': path.join(projectRoot, 'models/wake/melspectrogram.onnx'),
    'embedding_model.onnx': path.join(projectRoot, 'models/wake/embedding_model.onnx'),
    'temporal_cnn.onnx': path.join(projectRoot, 'violawake_data/trained_models/temporal_cnn.onnx'),
  }
  return {
    name: 'viola-wake-model-assets',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const m = (req.url || '').match(/\/wake\/([A-Za-z0-9_.-]+\.onnx)(\?.*)?$/)
        if (!m || !wakeModels[m[1]]) return next()
        const full = wakeModels[m[1]]
        if (!fs.existsSync(full)) return next()
        res.setHeader('Content-Type', 'application/octet-stream')
        res.setHeader('Cache-Control', 'no-cache')
        fs.createReadStream(full).pipe(res)
      })
    },
    generateBundle() {
      for (const [name, full] of Object.entries(wakeModels)) {
        if (fs.existsSync(full)) {
          this.emitFile({ type: 'asset', fileName: `wake/${name}`, source: fs.readFileSync(full) })
        }
      }
    },
  }
}

const violaVersion = readViolaVersion()
const gitSha = process.env.VITE_VIOLA_BUILD_SHA || readGitSha()
const sentryRelease = process.env.SENTRY_RELEASE || process.env.VITE_SENTRY_RELEASE || `viola-react@${violaVersion}+${gitSha}`
const sentryUploadEnabled = Boolean(process.env.SENTRY_AUTH_TOKEN && process.env.SENTRY_ORG && process.env.SENTRY_PROJECT)

// Browser errors use the committed capture-only DSN in sentryClient.js and
// POST to the same-origin desktop route. VITE_SENTRY_DSN is an optional
// override, not a release prerequisite. Requiring an off-VCS value here
// reintroduced the dependency removed by that relay architecture.
//
// Keep the explicit defines for configured overrides and release identity.
// The canonical release builder verifies the built bundle, same-origin route,
// and consent invariants; a missing/broken reporter still fails the release.
function resolveReporterEnv(mode) {
  const env = loadEnv(mode, projectRoot, 'VITE_')
  return {
    dsn: env.VITE_SENTRY_DSN || '',
    environment: env.VITE_SENTRY_ENVIRONMENT || (mode === 'production' ? 'production' : 'development'),
  }
}

export default defineConfig(({ mode }) => {
  const reporter = resolveReporterEnv(mode)
  const embedUrl = resolveYouTubeEmbedUrl(loadEnv(mode, projectRoot, 'VITE_').VITE_YOUTUBE_EMBED_URL)

  return {
    plugins: [
      react(),
      wakeModelAssets(),
      {
        name: 'viola-youtube-helper-config',
        generateBundle() {
          this.emitFile({ type: 'asset', fileName: 'youtube-embed.json', source: JSON.stringify({ url: embedUrl }) })
        },
      },
      sentryUploadEnabled && sentryVitePlugin({
        authToken: process.env.SENTRY_AUTH_TOKEN,
        org: process.env.SENTRY_ORG,
        project: process.env.SENTRY_PROJECT,
        telemetry: false,
        release: {
          name: sentryRelease,
          setCommits: false,
        },
        sourcemaps: {
          assets: '../static/react/assets/**',
          ignore: ['../static/react/assets/**/*.css'],
          filesToDeleteAfterUpload: ['../static/react/assets/**/*.map'],
        },
      }),
    ].filter(Boolean),
    base: '/static/react/',
    define: {
      'import.meta.env.VITE_YOUTUBE_EMBED_URL': JSON.stringify(embedUrl),
      'import.meta.env.VITE_VIOLA_VERSION': JSON.stringify(violaVersion),
      'import.meta.env.VITE_VIOLA_BUILD_SHA': JSON.stringify(gitSha),
      'import.meta.env.VITE_SENTRY_RELEASE': JSON.stringify(sentryRelease),
      // An empty override preserves the committed same-origin capture fallback.
      'import.meta.env.VITE_SENTRY_DSN': JSON.stringify(reporter.dsn),
      'import.meta.env.VITE_SENTRY_ENVIRONMENT': JSON.stringify(reporter.environment),
    },
    test: {
      environment: 'jsdom',
      globals: true,
      setupFiles: './src/test/setup.js',
      css: false,
      // #3583: the vitest default (5000ms) flakes SmartDisplay.test.jsx's
      // multi-step user-interaction tests (click -> type -> await modal render)
      // whenever the runner is under concurrent CPU load -- e.g. the pre-commit
      // dispatcher's ThreadPoolExecutor running many hooks at once, or several
      // Mergify-batch check-scripts sharing one runner. Reproduced: the same
      // test passes in isolation (~5.6s) but times out at exactly 5000ms when
      // several other test files/hooks contend for CPU. Widening the budget
      // (not narrowing scope, not skipping) is the hermetic fix.
      testTimeout: 15000,
      // #362/#3547 follow-on: the SAME contention class as #3583 above, one
      // layer over. That fix widened testTimeout but left hookTimeout at
      // vitest's 10000ms default, so the budget was only half-widened -- and
      // SmartDisplay.test.jsx does its heavy setup in `beforeEach`, not in the
      // test body. Adding two small auth test files (29 fast unit tests) was
      // enough extra suite-wide parallelism to tip that hook over 10s on a
      // loaded box: reproduced deterministically twice with the files present,
      // green twice with them absent, and both files pass in isolation in ~1s.
      // Nothing about the added tests is slow; the shared setup hook was simply
      // sitting just under an un-widened ceiling, so ANY future test file would
      // have tripped it. Same hermetic fix as #3583 -- widen the budget rather
      // than narrow scope or skip.
      hookTimeout: 15000,
    },
    build: {
      outDir: '../static/react',
      emptyOutDir: true,
      sourcemap: true,
      rollupOptions: {
        output: {
          entryFileNames: 'assets/[name]-[hash].js',
          chunkFileNames: 'assets/[name]-[hash].js',
          assetFileNames: 'assets/[name]-[hash].[ext]',
          // Sprint 5: Manual chunks for better caching and parallel loading
          manualChunks: {
            // React core - rarely changes, cache separately
            'vendor-react': ['react', 'react-dom'],
            // Prop types - dev dependency that gets bundled
            'vendor-proptypes': ['prop-types'],
          }
        }
      }
    },
    server: {
      port: 3000,
      host: true,
      proxy: {
        '/v1': { target: backendUrl, secure: false },
        '/ws': { target: wsUrl, ws: true, secure: false },
        '/health': { target: backendUrl, secure: false },
        '/config': { target: backendUrl, secure: false },
        '/bootstrap': { target: backendUrl, secure: false },
        '/auth': { target: backendUrl, secure: false },
        '/static': { target: backendUrl, secure: false }
      }
    }
  }
})

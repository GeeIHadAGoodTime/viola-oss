/**
 * featureSurface — which product features are available in the current build.
 *
 * The React bundle is served on three surfaces (see utils/runtimeSurface.js
 * and components/auth/cloudSurface.js):
 *
 *  1. Desktop app  — Qt shell, native webview, the user's own machine + files
 *     + credentials. `isDesktopApp()` is true. ALL features are available.
 *  2. Cloud SPA    — a plain browser on api.useviola.com under `/app`. This is
 *     the SECONDARY funnel to the desktop app (CLAUDE.md). The cloud backend
 *     intentionally does NOT serve desktop-only `/v1/*` routes — serving them
 *     would custody Tier-3 credentials or mount desktop-only handlers on cloud.
 *  3. Multiroom spoke — routed to SmartDisplay through SpokeWrapper with an
 *     explicit `isSpoke` prop. It is hub-backed, so it exposes the same
 *     SmartDisplay features as the desktop hub.
 *
 * This module is the single, centralized place that answers "should this
 * feature render in the web build?" It is built on top of the existing
 * `isCloudSurface()` gate — it does NOT introduce a new detection signal.
 *
 * Decision rule per feature (from
 * _diag/2026-05-29/helm_ship_proof/browser_app_feature_matrix.md):
 *  - REWIRE  — has a credential-less cloud-native equivalent; the client
 *    points at the cloud route instead of the desktop route. Stays AVAILABLE
 *    on the web; the rewire happens at the call site, not here.
 *  - HIDE    — fundamentally needs the user's credentials (Google/Microsoft
 *    calendar SYNC, BYOK) or local hardware (LAN speakers), or is
 *    desktop-shaped (device pairing, agent registry, local TTS). Hidden in
 *    the web build with an "Available in the desktop app →" upsell. Note:
 *    calendar CRUD itself (the local-primary provider) is CLOUD_NATIVE and
 *    NOT in DESKTOP_ONLY_FEATURES — only the Google/Microsoft OAuth sync
 *    layered on top stays desktop-only.
 *
 * Features listed in DESKTOP_ONLY_FEATURES are hidden only on the cloud SPA.
 * Multiroom spokes are authenticated windows into the hub, not credential-less
 * cloud clients, so they keep desktop parity. Any feature not listed is
 * available everywhere (the safe default for shared features such as music,
 * command, conversations, memory, settings).
 */

import { isCloudSurface } from '../components/auth/cloudSurface';

/**
 * Feature keys that are desktop-only and must be hidden in the cloud SPA.
 * Keep this list small and explicit; it is the canon source for the web
 * build's graceful-hide behavior.
 */
export const DESKTOP_ONLY_FEATURES = Object.freeze({
  // Background agent registry is a desktop-process surface (no cloud REST yet;
  // backend/cloud_route_manifest.py "agents" group has no registration spec).
  agents: 'agents',
  // First-run device-pairing onboarding + local-TTS narration. The cloud SPA
  // has its own GoTrue account front door (see main.jsx) — no device pairing.
  // Also HELD server-side: the desktop /v1/onboarding/* routes read/write the
  // process-global SettingsManager and persist BYOK keys server-side, which
  // would leak across tenants (cloud_route_manifest.py "onboarding" group).
  onboarding: 'onboarding',
  // LAN multiroom speakers — no browser-tab analog. Cloud room COORDINATION
  // exists (services/cloud_multiroom), but this hook/feature gates the
  // legacy desktop rooms surface, which stays unregistered on cloud
  // (cloud_route_manifest.py "rooms" group has no registration spec).
  rooms: 'rooms',
  // NOTE: `calendar` was removed 2026-07-06 — the self-hosted CalDAV work
  // (merges 8e4f705e, 41bbc7f1) shipped a tenant-safety-reviewed, registered
  // "calendar" cloud route group (local-provider CRUD; Google/CalDAV sync
  // stays desktop-only) — see CLOUD_DESKTOP_PARITY_MAP.md §2.10. Leaving it
  // listed here silently hid a shipped cloud capability behind the desktop
  // upsell. Ratchet: scripts/check_feature_surface_cloud_drift.py.

  // 2026-07-12 (#1064): four more SPA-dialed route groups 404'd silently on
  // the cloud surface (_diag/2026-07-11/browser_parity_closing_gate run1).
  // Each is genuinely desktop-only by architecture, not a missing wire-up:
  //   - payment_cards: ui/api/routes/payment_cards.py is the encrypted local
  //     payment vault, hard localhost-only guarded
  //     (backend/cloud_route_manifest.py "payment_cards" = LOCAL_ONLY). Card
  //     data is Tier-3 (memory/reference_local_only_storage_tier.md) — NEVER
  //     cloud, by rule, not by gap.
  //   - workbench: ui/api/routes/workbench.py reads/writes the desktop
  //     account-local filesystem (backend/cloud_route_manifest.py
  //     "workbench" = COMPANION_REQUIRED, needs an upload-backed cloud
  //     storage design before cloud exposure).
  //   - oauth_provider_linking: ui/consent_api.py's provider-linking flow
  //     (GET .../providers, POST .../session, .../revoke) persists OAuth
  //     tokens to the local EncryptedTokenVault (music/consent/service.py) —
  //     Tier-3, desktop-only. Distinct from calendar CRUD (cloud-native) and
  //     from the Tier-2 cloud_llm consent gate (services/cloud_consent);
  //     this key covers only the Google Calendar / Spotify / YouTube Music
  //     OAuth-linking session flow. ui/consent_api.py lives outside
  //     ui/api/routes/, so it has no RouteGroup entry for the drift gate to
  //     cross-reference — verified by direct architecture read, not the gate.
  //
  // NOTE: `memory_files` was REMOVED 2026-07-16 (#1182) — the exact
  // 2026-07-06 `calendar` drift shape (see the comment above `rooms`): the
  // cloud-native `/v1/memories` REPLACEMENT this key's own comment used to
  // point at (backend/cloud_route_manifest.py "memories" RouteGroup,
  // CLOUD_NATIVE, live on cloud since before #1064) is now what
  // MemoryPanel.jsx's Memory tab actually calls on the cloud SPA — see the
  // isCloudSurface() branch in MemoryPanel.jsx. The desktop-only VIOLA.md /
  // D-layout file view (ui/api/routes/memories.py, /api/memory/viola +
  // /api/memory/entries) stays desktop-only and ungated by a feature-surface
  // key (CustomizeTab.jsx now checks isCloudSurface() directly for its
  // VIOLA.md preview fetch, since that one desktop-file concept has no
  // cloud-native replacement at all). Ratchet:
  // tests/canary/test_browser_spa_route_wiring.py.
  payment_cards: 'payment_cards',
  workbench: 'workbench',
  oauth_provider_linking: 'oauth_provider_linking',

  // 2026-07-18 (#2610): the app-readiness audit (finding B15) found a whole
  // class of desktop-shaped settings/controls that render as normal enabled
  // buttons on the cloud SPA and then 404/no-op on click, because the backend
  // route they call is deliberately NOT served on cloud
  // (backend/cloud_route_manifest.py). Each key below gates one such surface
  // to an honest DesktopUpsell instead of a dead click. None of these has a
  // credential-less cloud-native equivalent — they are desktop-shaped by
  // architecture (local process / LAN / local filesystem / Tier-3 secrets):
  //   - extensions: ui/api/routes/extensions.py (manifest "extensions" =
  //     LOCAL_ONLY) mutates local MCP-server + plugin process state.
  //   - smarthome: ui/api/routes/smarthome.py (manifest "smarthome" =
  //     COMPANION_REQUIRED) scans the user's LAN for devices; HA setup needs a
  //     local companion. No RouteGroup serves it on cloud.
  //   - local_music: /v1/local/* (manifest "local_library"/"local_media" =
  //     COMPANION_REQUIRED) reads the desktop's own music filesystem.
  //   - music_services: the Spotify connect flow drives a local Chrome via CDP
  //     (/v1/spotify/* = "spotify_cdp" COMPANION_REQUIRED, /v1/browser/auth/* =
  //     "browser_auth" LOCAL_ONLY) — headless cloud has no local browser.
  //   - wake_models: /v1/wake/* (manifest "wake_training" = LOCAL_ONLY)
  //     switches/deletes on-disk wake-word models; the browser voice path uses
  //     a fixed cloud wake path, not these local model files.
  //   - weekly_review: /v1/ai/weekly-review/* (manifest "weekly_review" =
  //     LOCAL_ONLY) reads local user-model/memory artifacts.
  //   - desktop_ai: the local-model / ChatGPT-Plus(Codex) / BYOK provider
  //     connection config (/v1/connectors/profiles* = "connectors" LOCAL_ONLY,
  //     /v1/codex/auth/* = "codex_auth" LOCAL_ONLY, /v1/settings/detect-local-ai
  //     off ui/settings_api.py which is unwired on cloud). Cloud runs the
  //     managed model; BYOK keys are Tier-3, desktop-only.
  //   - system_controls: desktop OS controls — tray/boot/network-port/local
  //     updater/audio-device enumeration and the Advanced Settings window
  //     (diagnostics/reset over /v1/diagnostics/* + /v1/settings/reset, both
  //     unserved on cloud). A browser tab has no tray/boot hook/local port.
  //
  // 2026-07-31 (#4226, C-427): seven of the eight keys below were declared
  // here, given upsell copy, and then never passed to isFeatureAvailable /
  // isFeatureHidden by anything — the same declared-but-never-called shape as
  // C-401's `desktop_ai`, so each surface still rendered live on the cloud SPA
  // and 404'd or silently no-op'd on click. All seven are wired now, and
  // check_feature_surface_cloud_drift.py's UNWIRED_FEATURE_KEYS ledger is
  // EMPTY as a result — which is what makes the next unwired key fail that
  // gate outright instead of hiding behind a stale row.
  //
  // Ratchet: tests/canary/test_browser_spa_route_wiring.py +
  // scripts/check_feature_surface_cloud_drift.py, plus the behavioural pair
  // ui/react-app/src/components/SettingsModal.desktopOnlyPanels.test.jsx and
  // components/desktopOnlyPanels.cloudSurface.test.jsx. (An earlier revision
  // of this comment cited scripts/check_cloud_ui_desktop_route_gating.py,
  // which has never existed here — `git log --all` on that path is empty.)
  extensions: 'extensions',
  smarthome: 'smarthome',
  local_music: 'local_music',
  music_services: 'music_services',
  wake_models: 'wake_models',
  weekly_review: 'weekly_review',
  desktop_ai: 'desktop_ai',
  system_controls: 'system_controls',
});

/**
 * The friendly label + reason shown in the desktop upsell for each feature.
 */
export const DESKTOP_ONLY_FEATURE_COPY = Object.freeze({
  agents: {
    title: 'Background agents',
    reason: 'Background agents run on your desktop, alongside your files and tools.',
  },
  onboarding: {
    title: 'Guided setup',
    reason: 'Guided first-run setup and device pairing live in the desktop app.',
  },
  rooms: {
    title: 'Multi-room speakers',
    reason: 'Multi-room speaker sync runs over your local network from the desktop app.',
  },
  payment_cards: {
    title: 'Payment cards',
    reason: 'Saved cards are stored in an encrypted vault on your desktop, never in the cloud.',
  },
  workbench: {
    title: 'Workbench files',
    reason: 'Workbench files live on your desktop’s local disk. Open the desktop app to upload or manage them.',
  },
  oauth_provider_linking: {
    title: 'Connected accounts',
    reason: 'Linking Google Calendar, Spotify, or YouTube Music happens in the desktop app, where the sign-in tokens are stored securely on your machine.',
  },
  extensions: {
    title: 'Extensions & plugins',
    reason: 'MCP servers and plugins run inside the desktop app, next to your local tools. Manage them from Viola on your machine.',
  },
  smarthome: {
    title: 'Smart home',
    reason: 'Finding and connecting smart-home devices scans your local network, so it runs from the desktop app on your machine.',
  },
  local_music: {
    title: 'Local music files',
    reason: 'Playing music from your own files needs access to your machine’s disk, so it lives in the desktop app.',
  },
  music_services: {
    title: 'Connect music services',
    reason: 'Signing in to Spotify opens a secure browser window on your machine, so connecting music services happens in the desktop app.',
  },
  wake_models: {
    title: 'Wake-word models',
    reason: 'Custom wake-word models are stored and switched on your machine. Manage them in the desktop app.',
  },
  weekly_review: {
    title: 'Weekly review',
    reason: 'Your weekly review is generated from memory stored on your machine, so it runs in the desktop app.',
  },
  desktop_ai: {
    title: 'AI provider setup',
    reason: 'Running a local model, using your ChatGPT Plus sign-in, or bringing your own API key are set up in the desktop app, where those credentials stay on your machine. On the web, Viola runs on its built-in model.',
  },
  system_controls: {
    title: 'Desktop settings',
    reason: 'Tray, start-on-boot, the local network port, audio devices, diagnostics, and app updates are settings for the installed desktop app.',
  },
});

/**
 * True when the given feature is available in the current build.
 *
 * True on the desktop app and on hub-backed multiroom spokes for every
 * feature. On the cloud SPA, false for the desktop-only features above and
 * true for everything else.
 *
 * @param {string} feature - one of DESKTOP_ONLY_FEATURES values (or any key).
 * @param {object} [options]
 * @param {boolean} [options.isSpoke=false] - accepted for legacy callers;
 * multiroom spokes have full hub parity.
 * @returns {boolean}
 */
export function isFeatureAvailable(feature, options = {}) {
  void options;
  const isDesktopOnly = Object.prototype.hasOwnProperty.call(DESKTOP_ONLY_FEATURES, feature);
  if (!isCloudSurface()) {
    return true;
  }
  return !isDesktopOnly;
}

/**
 * Convenience inverse: true when the feature is hidden in the current build.
 * @param {string} feature
 * @param {object} [options]
 * @returns {boolean}
 */
export function isFeatureHidden(feature, options = {}) {
  return !isFeatureAvailable(feature, options);
}

export default {
  DESKTOP_ONLY_FEATURES,
  DESKTOP_ONLY_FEATURE_COPY,
  isFeatureAvailable,
  isFeatureHidden,
};

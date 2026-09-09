/**
 * Centralized configuration for React app.
 *
 * Contains API endpoints and other shared constants.
 */

// Base URLs
export const API_BASE = window.__VIOLA_BASE_URL__ || window.location.origin;
export const WS_URL = (window.__VIOLA_BASE_URL__ || window.location.origin).replace(/^http/, 'ws');

// API Endpoints
export const API = {
  AUTH: '/auth',
  CONSENT: '/api/v1/consent',
  COMMAND: '/v1/command',
  STATE: '/v1/state',
  SETTINGS: '/v1/settings',
  DIAGNOSTICS: '/v1/diagnostics',
  WEBSOCKET: '/ws',
};

// Feature flags
export const FEATURES = {
  MUSIC_PROVIDERS: true,
  SETTINGS: true,
  AUTH: true,
};

// Environment detection (Vite uses import.meta.env, not process.env)
export const IS_DEVELOPMENT = import.meta.env.DEV;

// Default values
export const DEFAULTS = {
  VOLUME: 75,
  RECONNECT_DELAY_MS: 2000,
  MAX_RECONNECT_ATTEMPTS: 5,
};

// Theme color palettes
const DARK_COLORS = {
  // Backgrounds
  bgVoid: '#000000',
  bgCard: '#0d0d0d',
  bgElevated: '#1a1a1a',
  bgSurface: '#141414',
  // Text
  textBright: '#fafafa',  // Soft white — reduces OLED bloom/halation on phones, still reads as white. WCAG: 19.6:1 on #000.
  textPrimary: 'rgba(255,255,255,0.85)',
  textSecondary: 'rgba(255,255,255,0.65)',
  textTertiary: 'rgba(255,255,255,0.55)',
  textMuted: 'rgba(255,255,255,0.45)',
  textDisabled: 'rgba(255,255,255,0.35)',
  textFaint: 'rgba(255,255,255,0.2)',
  // Status
  statusGreen: '#22c55e',
  statusYellow: '#eab308',
  statusRed: '#ef4444',
  // Borders
  borderSubtle: 'rgba(255,255,255,0.04)',
  borderLight: 'rgba(255,255,255,0.06)',
  borderHover: 'rgba(255,255,255,0.12)',
  divider: 'rgba(255,255,255,0.08)',
  // Glass effect
  glassBase: 'rgba(255,255,255,0.07)',
  glassHover: 'rgba(255,255,255,0.10)',
  glassActive: 'rgba(255,255,255,0.14)',
  // Accent
  accent: '#6B2E1B',
  accentGlow: 'rgba(107, 46, 27, 0.3)',
  accentHover: 'rgba(107, 46, 27, 0.07)',
  accentSubtle: 'rgba(107, 46, 27, 0.10)',
  accentActive: 'rgba(107, 46, 27, 0.16)',
  accentRing: 'rgba(107, 46, 27, 0.14)',
  accentBorder: 'rgba(107, 46, 27, 0.40)',
  // Bronze warmth — shared with onboarding (lamplight embers). The mahogany
  // accent is the brand color; these lighter bronzes give warm hairlines/glows.
  bronze: '#c98a68',
  bronzeDeep: '#7d3a22',
  bronzeHairline: 'rgba(201, 138, 104, 0.26)',
  emberGlow: 'rgba(201, 138, 104, 0.55)',
  // Shadows and overlays
  shadowLight: 'rgba(0,0,0,0.3)',
  shadowMedium: 'rgba(0,0,0,0.5)',
  shadowHeavy: 'rgba(0,0,0,0.65)',
  shadowDeep: 'rgba(0,0,0,0.8)',
  overlay: 'rgba(0,0,0,0.85)',
};

const LIGHT_COLORS = {
  // Backgrounds
  bgVoid: '#f5f5f7',
  bgCard: '#ffffff',
  bgElevated: '#f0f0f2',
  bgSurface: '#fafafa',
  // Text
  textBright: '#111111',
  textPrimary: 'rgba(0,0,0,0.87)',
  textSecondary: 'rgba(0,0,0,0.6)',
  textTertiary: 'rgba(0,0,0,0.5)',
  textMuted: 'rgba(0,0,0,0.38)',
  textDisabled: 'rgba(0,0,0,0.26)',
  textFaint: 'rgba(0,0,0,0.12)',
  // Status
  statusGreen: '#16a34a',
  statusYellow: '#ca8a04',
  statusRed: '#dc2626',
  // Borders
  borderSubtle: 'rgba(0,0,0,0.06)',
  borderLight: 'rgba(0,0,0,0.1)',
  borderHover: 'rgba(0,0,0,0.2)',
  divider: 'rgba(0,0,0,0.1)',
  // Glass effect
  glassBase: 'rgba(0,0,0,0.04)',
  glassHover: 'rgba(0,0,0,0.07)',
  glassActive: 'rgba(0,0,0,0.1)',
  // Accent
  accent: '#6B2E1B',
  accentGlow: 'rgba(107, 46, 27, 0.18)',
  accentHover: 'rgba(107, 46, 27, 0.07)',
  accentSubtle: 'rgba(107, 46, 27, 0.10)',
  accentActive: 'rgba(107, 46, 27, 0.16)',
  accentRing: 'rgba(107, 46, 27, 0.14)',
  accentBorder: 'rgba(107, 46, 27, 0.40)',
  // Bronze warmth — shared with onboarding (lamplight embers). Kept identical
  // across light/dark: the bronze reads warm on either surface.
  bronze: '#c98a68',
  bronzeDeep: '#7d3a22',
  bronzeHairline: 'rgba(201, 138, 104, 0.26)',
  emberGlow: 'rgba(201, 138, 104, 0.55)',
  // Shadows and overlays
  shadowLight: 'rgba(0,0,0,0.08)',
  shadowMedium: 'rgba(0,0,0,0.15)',
  shadowHeavy: 'rgba(0,0,0,0.2)',
  shadowDeep: 'rgba(0,0,0,0.3)',
  overlay: 'rgba(0,0,0,0.5)',
};

// Shared type system — one language with the marketing website and the
// first-run onboarding. `display` is the Cormorant Garamond serif (self-hosted
// via @font-face in styles/variables.css) for large display headings; `sans`
// is the UI workhorse for body text, labels, and dense controls. Theme-
// independent, so it lives outside the light/dark color swap. Components using
// inline styles read THEME.fonts.display / THEME.fonts.sans; CSS Modules use
// the matching var(--font-display) / var(--font-sans).
const FONTS = {
  display: "'Cormorant Garamond', Georgia, 'Times New Roman', serif",
  sans: "system-ui, -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', sans-serif",
};

// Mutable THEME object. Components import THEME and read THEME.colors.X at
// render time. applyTheme() swaps the color values in-place so every
// component picks up the new palette on next render without changing imports.
export const THEME = {
  colors: { ...DARK_COLORS },
  fonts: FONTS,
};

// Track current mode so callers can check it
let _currentThemeMode = 'dark';

const THEME_STORAGE_KEY = 'viola_theme_mode';

/**
 * Apply a theme mode ('dark', 'light', or 'system').
 * Mutates THEME.colors in-place and updates document background.
 * Caches the choice in localStorage for flash-free page loads.
 * Returns the resolved mode ('dark' or 'light').
 */
export function applyTheme(mode) {
  let resolved = mode;
  if (mode === 'system') {
    resolved = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  const colors = resolved === 'light' ? LIGHT_COLORS : DARK_COLORS;
  Object.assign(THEME.colors, colors);
  _currentThemeMode = mode;

  try { localStorage.setItem(THEME_STORAGE_KEY, mode); } catch { /* quota or private browsing */ }

  if (typeof document !== 'undefined') {
    document.documentElement.style.backgroundColor = colors.bgVoid;
    document.body.style.backgroundColor = colors.bgVoid;
    for (const [key, value] of Object.entries(colors)) {
      const cssVar = '--' + key.replace(/([A-Z])/g, '-$1').toLowerCase();
      document.documentElement.style.setProperty(cssVar, value);
    }
  }

  return resolved;
}

function _parseHexRgb(hex) {
  const clean = hex.replace('#', '');
  return {
    r: parseInt(clean.slice(0, 2), 16),
    g: parseInt(clean.slice(2, 4), 16),
    b: parseInt(clean.slice(4, 6), 16),
  };
}

export function setAccent(hex) {
  const { r, g, b } = _parseHexRgb(hex);
  const rgb = `${r}, ${g}, ${b}`;
  THEME.colors.accent = hex;
  THEME.colors.accentGlow = `rgba(${rgb}, 0.3)`;
  THEME.colors.accentHover = `rgba(${rgb}, 0.07)`;
  THEME.colors.accentSubtle = `rgba(${rgb}, 0.10)`;
  THEME.colors.accentActive = `rgba(${rgb}, 0.16)`;
  THEME.colors.accentRing = `rgba(${rgb}, 0.14)`;
  THEME.colors.accentBorder = `rgba(${rgb}, 0.40)`;
  if (typeof document !== 'undefined') {
    const root = document.documentElement.style;
    root.setProperty('--accent', hex);
    root.setProperty('--accent-rgb', rgb);
    root.setProperty('--accent-glow', `rgba(${rgb}, 0.3)`);
    root.setProperty('--accent-hover', `rgba(${rgb}, 0.07)`);
    root.setProperty('--accent-subtle', `rgba(${rgb}, 0.10)`);
    root.setProperty('--accent-active', `rgba(${rgb}, 0.16)`);
    root.setProperty('--accent-ring', `rgba(${rgb}, 0.14)`);
    root.setProperty('--accent-border', `rgba(${rgb}, 0.40)`);
    // Note: scrollbar tokens intentionally NOT updated — they stay neutral
    // (var(--glass-active) / var(--text-faint)) per the canonical scrollbar
    // style. Branding the scrollbar with the accent reads as cluttered.
    document.querySelectorAll('.chat-mode').forEach((el) => {
      el.style.setProperty('--chat-accent', hex);
      el.style.setProperty('--chat-accent-rgb', rgb);
      el.style.setProperty('--chat-accent-soft', `rgba(${rgb}, 0.18)`);
    });
  }
  return hex;
}

if (typeof window !== 'undefined') {
  window.setAccent = setAccent;
}

/**
 * Get the current theme mode string ('dark', 'light', or 'system').
 */
export function getCurrentThemeMode() {
  return _currentThemeMode;
}

// Early theme application on module load: read cached preference from
// localStorage and apply before React renders, preventing a flash of the
// wrong theme. The useEffect in SmartDisplay will re-apply once the
// authoritative backend setting arrives.
(function earlyThemeApply() {
  try {
    const cached = localStorage.getItem(THEME_STORAGE_KEY);
    if (cached && cached !== 'dark') {
      applyTheme(cached);
    }
  } catch { /* localStorage unavailable */ }
})();

// Music Provider Display Names (only implemented providers shown in UI)
export const PROVIDER_DISPLAY_NAMES = {
  spotify: 'Spotify',
  youtube_music: 'YouTube Music',
  local: 'Local Music',
};

// Client auth state shared between REST and WebSocket callers.
// The desktop shell injects the current API key into the page. LAN clients
// must use the explicit pairing flow instead of a silent /bootstrap/auth fetch.
let _clientApiKey = null;
let _backendReady = false;

/**
 * Record backend readiness so auth helpers stay quiet during startup churn.
 */
export function setBackendReady(isReady = true) {
  _backendReady = isReady;
}

function readInjectedApiKey() {
  if (typeof window === 'undefined') {
    return '';
  }
  return window.__VIOLA_API_KEY__ || '';
}

/**
 * Return the API key currently available to the React client.
 * Desktop injects it via window.__VIOLA_API_KEY__. External LAN browsers must
 * complete /bootstrap/request + /bootstrap/confirm explicitly.
 */
export async function getClientApiKey() {
  if (_clientApiKey !== null) {
    return _clientApiKey;
  }

  const injectedKey = readInjectedApiKey();
  if (injectedKey) {
    _clientApiKey = injectedKey;
    return _clientApiKey;
  }

  if (!_backendReady) {
    return '';
  }

  return '';
}

/**
 * Cache the client API key directly (used by WebSocket hook for consistency).
 */
export function cacheClientApiKey(key) {
  _clientApiKey = key || '';
}

/**
 * Clear the local cache. This does not mutate the desktop-injected global.
 */
export function clearCachedClientApiKey() {
  _clientApiKey = null;
}

/**
 * Get the API key synchronously (returns null if not yet visible).
 */
export function getClientApiKeySync() {
  if (_clientApiKey !== null) {
    return _clientApiKey;
  }
  const injectedKey = readInjectedApiKey();
  if (injectedKey) {
    _clientApiKey = injectedKey;
    return _clientApiKey;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Cloud GoTrue session — Viola Cloud browser auth.
//
// Two auth surfaces coexist:
//  - Desktop: the Qt shell injects window.__VIOLA_API_KEY__ (handled above).
//  - Cloud / LAN web client: the SPA signs in via GoTrue and holds a session
//    access token. AuthProvider mirrors that session here via setCloudSession()
//    so non-React REST + WebSocket callers can attach it without importing
//    React or the auth context.
//
// getCloudAccessToken() returns the live JWT or ''. REST callers attach it as
// `Authorization: Bearer <token>`. Browser WebSockets rely on the httpOnly
// compatibility session cookie set by the GoTrue proxy, so the JWT is never
// placed in a WebSocket URL.
// ---------------------------------------------------------------------------
let _cloudSession = null;

/**
 * Record (or clear) the active cloud GoTrue session. Called by AuthProvider on
 * every session change so the REST/WS layers see a consistent token.
 * @param {object|null} session - GoTrue session, or null to clear.
 */
export function setCloudSession(session) {
  _cloudSession = (session && typeof session.access_token === 'string' && session.access_token)
    ? session
    : null;
}

/**
 * Return the current cloud GoTrue session (or null).
 * @returns {object|null}
 */
export function getCloudSession() {
  return _cloudSession;
}

/**
 * Return the live cloud access token, or '' when there is no cloud session.
 * A token that is already past its `expires_at` is treated as absent —
 * AuthProvider's auto-refresh keeps a valid token in place under normal
 * operation, and a stale token would only earn a 401.
 * @returns {string}
 */
export function getCloudAccessToken() {
  if (!_cloudSession) return '';
  const expiresAtMs = (Number(_cloudSession.expires_at) || 0) * 1000;
  if (expiresAtMs > 0 && Date.now() >= expiresAtMs) {
    return '';
  }
  return _cloudSession.access_token || '';
}

// NOTE: getCloudPhoneBaseUrl() / isCloudPhoneRoutingActive() were REMOVED in the
// 2026-06-29 capstone. They drove a browser-held cloud-bearer phone fetch
// (cloudPhoneFetch) that was dead on the desktop (the cloud bearer is never in
// the browser — SEC-017 — so getCloudAccessToken() was always empty there). The
// desktop phone tab now hits its LOCAL backend, which proxies to the cloud with
// the server-side bearer (telephony/desktop_cloud_proxy.py). getCloudAccessToken
// above remains for the same-origin web client's authFetch.

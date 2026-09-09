import { useState, useEffect } from 'react'
import ReactDOM from 'react-dom/client'
import PropTypes from 'prop-types'
import { useTranslation } from 'react-i18next'
import * as Sentry from '@sentry/react'
import App from './App'
import { API_BASE, THEME, cacheClientApiKey, getClientApiKey, setBackendReady } from './config'
import { isCloudSurface } from './components/auth/cloudSurface'
import { isLocalDesktopBackend, setBackendSurface } from './utils/backendSurface'
import { isDesktopApp } from './utils/runtimeSurface'
import { initSentry } from './sentryClient'
import './styles/touch-hardening.css'
import './i18n'

const POLL_INTERVAL_MS = 500;
const TIMEOUT_MS = 30000;
const API_KEY_WAIT_MS = 3000;
const API_KEY_POLL_MS = 100;

function wait(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function shouldBypassDesktopApiKeyGate() {
  const params = new URLSearchParams(window.location.search);
  const mode = params.get('mode');
  // Cloud surface: a plain browser on Viola Cloud has no desktop API key by
  // design. The "Open Viola Desktop" wall does not apply — CloudAuthGate
  // (inside App) renders the account login / sign-up front door instead.
  if (isCloudSurface()) {
    return true;
  }
  return mode === 'review'
    || mode === 'speaker'
    || params.has('room')
    || params.has('spoke_token')
    // A scanned Add Room QR arrives with a pairing ticket and no credential
    // yet (#4434). It is a joining speaker, not a desktop window.
    || params.has('pair');
}

async function waitForClientApiKey(timeoutMs = API_KEY_WAIT_MS) {
  const start = Date.now();
  let apiKey = await getClientApiKey();
  while (!apiKey && Date.now() - start < timeoutMs) {
    await wait(API_KEY_POLL_MS);
    apiKey = await getClientApiKey();
  }
  return apiKey;
}

/**
 * Branded loading screen - matches SmartDisplay.jsx loading skeleton.
 * Pulsing gradient circle with play icon and "Starting Viola" text.
 */
function BrandedLoader({ message, isError = false }) {
  const { t } = useTranslation();
  const resolvedMessage = message || t('app.startup.initializing');

  return (
    <div style={{
      position: 'fixed',
      top: 0,
      left: 0,
      right: 0,
      bottom: 0,
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      justifyContent: 'center',
      gap: '24px',
      backgroundColor: THEME.colors.bgVoid,
      fontFamily: "'Segoe UI', 'SF Pro Display', -apple-system, sans-serif",
    }}>
      {/* Breathing logo animation */}
      <div
        data-essential-motion={isError ? undefined : 'pulse-slow'}
        style={{
          width: '96px',
          height: '96px',
          borderRadius: '50%',
          background: isError
            ? 'linear-gradient(135deg, rgba(239, 68, 68, 0.3) 0%, rgba(185, 28, 28, 0.2) 100%)'
            : 'linear-gradient(135deg, rgba(139, 92, 246, 0.3) 0%, rgba(59, 130, 246, 0.2) 100%)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          animation: isError ? 'none' : 'viola-pulse 2s ease-in-out infinite',
        }}
      >
        {isError ? (
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,0.8)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10" />
            <line x1="15" y1="9" x2="9" y2="15" />
            <line x1="9" y1="9" x2="15" y2="15" />
          </svg>
        ) : (
          <img
            src={`${import.meta.env.BASE_URL || '/'}viola_icon.png`}
            alt='Viola'
            style={{ width: '64px', height: '64px' }}
          />
        )}
      </div>
      {/* Loading text */}
      <div style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: '8px',
      }}>
        <span style={{
          color: THEME.colors.textPrimary,
          fontSize: '18px',
          fontWeight: 500,
          letterSpacing: '-0.5px',
        }}>
          {isError ? t('app.startup.failed_title') : t('app.startup.title')}
        </span>
        <span style={{
          color: THEME.colors.textMuted,
          fontSize: '14px',
          maxWidth: '300px',
          textAlign: 'center',
        }}>
          {resolvedMessage}
        </span>
      </div>
      <style>{`
        @keyframes viola-pulse {
          0%, 100% { opacity: 1; transform: scale(1); }
          50% { opacity: 0.4; transform: scale(0.85); }
        }
      `}</style>
    </div>
  );
}

BrandedLoader.propTypes = {
  message: PropTypes.string,
  isError: PropTypes.bool,
};

export function BrowserAuthGate({ onRetry, localBrowser = false, onConnected }) {
  const [key, setKey] = useState('');
  const [connecting, setConnecting] = useState(false);
  const [connectionError, setConnectionError] = useState('');

  async function connectLocal(event) {
    event.preventDefault();
    const candidate = key.trim();
    if (!candidate || connecting) return;
    setConnecting(true);
    setConnectionError('');
    try {
      // Do not let a pre-existing account/spoke cookie validate an incorrect
      // owner key. The backend's normal API-key authorization is authoritative.
      const response = await fetch(`${API_BASE}/v1/state`, {
        method: 'GET',
        cache: 'no-store',
        credentials: 'omit',
        redirect: 'error',
        headers: { 'X-API-Key': candidate },
      });
      if (!response.ok) {
        setConnectionError(response.status === 401 || response.status === 403
          ? 'That key was not accepted. Check your local API key and try again.'
          : 'Viola could not verify the key. Please try again.');
        return;
      }
      cacheClientApiKey(candidate);
      setKey('');
      onConnected();
    } catch {
      setConnectionError('Could not connect to your Viola server. Please try again.');
    } finally {
      setConnecting(false);
    }
  }
  return (
    <div style={{
      position: 'fixed',
      inset: 0,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      padding: '24px',
      backgroundColor: THEME.colors.bgVoid,
      fontFamily: "'Segoe UI', 'SF Pro Display', -apple-system, sans-serif",
      color: THEME.colors.textPrimary,
    }}>
      <main
        role="status"
        aria-live="polite"
        style={{
          width: 'min(520px, 100%)',
          display: 'grid',
          gap: '18px',
          textAlign: 'center',
        }}
      >
        <img
          src={`${import.meta.env.BASE_URL || '/'}viola_icon.png`}
          alt=""
          width="72"
          height="72"
          style={{ justifySelf: 'center' }}
        />
        <div style={{ display: 'grid', gap: '8px' }}>
          <h1 style={{ margin: 0, fontSize: '24px', fontWeight: 650 }}>
            {localBrowser ? 'Connect to your Viola' : 'Open Viola Desktop'}
          </h1>
          <p style={{
            margin: 0,
            color: THEME.colors.textSecondary,
            fontSize: '15px',
            lineHeight: 1.5,
          }}>
            {localBrowser
              ? 'Enter the local API key created by your Viola installation. No Viola account is needed.'
              : 'This browser was loaded without the desktop API key, so Viola blocked the dashboard before making private API calls.'}
          </p>
          <p style={{
            margin: 0,
            color: THEME.colors.textMuted,
            fontSize: '13px',
            lineHeight: 1.5,
          }}>
            {localBrowser ? <>Find it in <code>secrets/initial_api_key</code> inside your Viola data folder.
              The key stays in this page&apos;s memory; reconnect after reloading.</> : <>Use the desktop app for the full dashboard. To pair a trusted
            browser device, start the local pairing flow with
            <code style={{ color: THEME.colors.textSecondary }}> POST /bootstrap/request </code>
            and confirm it with
            <code style={{ color: THEME.colors.textSecondary }}> POST /bootstrap/confirm</code>.</>}
          </p>
        </div>
        {localBrowser ? (
          <form onSubmit={connectLocal} style={{ display: 'grid', gap: '12px', textAlign: 'left' }}>
            <label htmlFor="local-api-key">Local API key</label>
            <input id="local-api-key" type="password" autoComplete="off" spellCheck={false}
              value={key} onChange={(event) => setKey(event.target.value)} required disabled={connecting}
              style={{ minHeight: '44px', padding: '8px 12px', borderRadius: '8px' }} />
            {connectionError && <p role="alert">{connectionError}</p>}
            <button type="submit" disabled={connecting || !key.trim()} style={{ minHeight: '44px' }}>
              {connecting ? 'Connecting…' : 'Connect'}
            </button>
          </form>
        ) : <button
          type="button"
          onClick={onRetry}
          style={{
            justifySelf: 'center',
            minHeight: '40px',
            padding: '0 16px',
            border: `1px solid ${THEME.colors.borderLight}`,
            borderRadius: '8px',
            backgroundColor: THEME.colors.glassBase,
            color: THEME.colors.textPrimary,
            fontSize: '14px',
            fontWeight: 600,
            cursor: 'pointer',
          }}
        >
          Try Again
        </button>}
      </main>
    </div>
  );
}

BrowserAuthGate.propTypes = {
  onRetry: PropTypes.func.isRequired,
  localBrowser: PropTypes.bool,
  onConnected: PropTypes.func,
};

function AppCrashFallback() {
  const { t } = useTranslation();
  return (
    <BrandedLoader
      message={t('app.startup.failed_title')}
      isError
    />
  );
}

/**
 * Gate component that waits for backend readiness before rendering App.
 * Polls /health until 200, showing branded loader. Times out after 30s.
 */
export function BackendReadyGate({ children, apiKeyWaitMs = API_KEY_WAIT_MS }) {
  const { t } = useTranslation();
  const [ready, setReady] = useState(false);
  const [error, setError] = useState(null);
  const [missingApiKey, setMissingApiKey] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let timedOut = false;
    let pollTimeoutId;

    const timeoutId = window.setTimeout(() => {
      timedOut = true;
      setError(t('app.startup.timeout'));
      window.setTimeout(() => {
        window.location.reload();
      }, 3000);
    }, TIMEOUT_MS);

    async function checkHealth() {
      if (cancelled || timedOut) {
        return;
      }

      try {
        const response = await fetch(`${API_BASE}/health`, { method: 'GET', cache: 'no-store' });
        if (response.ok) {
          const health = typeof response.json === 'function' ? await response.json().catch(() => ({})) : {};
          setBackendSurface(health);
          setBackendReady(true);
          if (!shouldBypassDesktopApiKeyGate()) {
            const apiKey = await waitForClientApiKey(apiKeyWaitMs);
            if (!apiKey) {
              // DEFENSE-IN-DEPTH: do NOT latch permanently on a missed key
              // window. The desktop injects window.__VIOLA_API_KEY__ at
              // DocumentCreation, so on a normal reload the key is present
              // before first render; but if it is ever late (injection race,
              // slow render process), a permanent latch here turns the miss
              // into a dead "Open Viola Desktop" wall — and no socket — for the
              // whole call (call 49c7106f). Show the wall for guidance, but
              // keep re-checking for a late key and auto-recover when it
              // arrives. Stop the 30s reload timer so a genuine external browser
              // (key never comes) sees a stable wall instead of a reload loop.
              setMissingApiKey(true);
              window.clearTimeout(timeoutId);
              if (!cancelled && !timedOut) {
                pollTimeoutId = window.setTimeout(checkHealth, POLL_INTERVAL_MS);
              }
              return;
            }
            // A late key arrived after we had shown the wall — clear it.
            setMissingApiKey(false);
          }
          setReady(true);
          window.clearTimeout(timeoutId);
          return;
        }
      } catch {
        // Connection refused or network error - keep polling
      }

      if (!cancelled && !timedOut) {
        pollTimeoutId = window.setTimeout(checkHealth, POLL_INTERVAL_MS);
      }
    }

    checkHealth();
    return () => {
      cancelled = true;
      window.clearTimeout(timeoutId);
      if (pollTimeoutId !== undefined) {
        window.clearTimeout(pollTimeoutId);
      }
    };
  }, [apiKeyWaitMs, t]);

  if (error) {
    return <BrandedLoader message={error} isError />;
  }

  if (missingApiKey) {
    return <BrowserAuthGate onRetry={() => window.location.reload()}
      localBrowser={isLocalDesktopBackend() && !isDesktopApp()}
      onConnected={() => { setMissingApiKey(false); setReady(true); }} />;
  }

  if (!ready) {
    return <BrandedLoader message={t('app.startup.starting')} />;
  }

  return children;
}

BackendReadyGate.propTypes = {
  children: PropTypes.node.isRequired,
  apiKeyWaitMs: PropTypes.number,
};

const rootElement = document.getElementById('root');
initSentry();

if (rootElement) {
  const root = ReactDOM.createRoot(rootElement);

  root.render(
    <BackendReadyGate>
      <Sentry.ErrorBoundary fallback={AppCrashFallback} showDialog={false}>
        <App />
      </Sentry.ErrorBoundary>
    </BackendReadyGate>
  )
}

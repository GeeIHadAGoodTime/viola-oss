/**
 * Authentication hook and context for Viola Cloud account management.
 *
 * Provides:
 * - User login/logout state (shared via React Context)
 * - Session management
 * - Subscription status
 * - OAuth provider connections (system browser flow for Google)
 */

import { createContext, useContext, useState, useEffect, useCallback } from 'react';

const API_BASE = '/auth';

class AuthRequestError extends Error {
  constructor(message, { code, status, retryAfter, details } = {}) {
    super(message);
    this.name = 'AuthRequestError';
    this.code = code || 'auth_error';
    this.status = status || 0;
    this.retryAfter = retryAfter || null;
    this.details = details || null;
  }
}

function retryAfterLabel(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) return 'a few minutes';
  if (value < 60) return `${Math.ceil(value)} seconds`;
  const minutes = Math.ceil(value / 60);
  return minutes === 1 ? '1 minute' : `${minutes} minutes`;
}

function unwrapAuthData(json) {
  if (json && typeof json === 'object' && 'data' in json) {
    return json.data || {};
  }
  return json || {};
}

function authErrorFromResponse(response, json, fallbackMessage) {
  const retryHeader = response.headers?.get?.('Retry-After') || response.headers?.get?.('retry-after');
  const body = json && typeof json === 'object' ? json : {};
  const envelope = body.ok === false ? body : body.detail?.ok === false ? body.detail : null;
  const envelopeError = envelope?.error || null;
  const legacyDetail = body.detail;

  if (Array.isArray(legacyDetail)) {
    const first = legacyDetail[0] || {};
    return new AuthRequestError(first.msg || fallbackMessage, {
      code: 'validation_error',
      status: response.status,
      retryAfter: retryHeader,
      details: { validation_errors: legacyDetail },
    });
  }

  if (envelopeError) {
    return new AuthRequestError(envelopeError.message || fallbackMessage, {
      code: envelopeError.code || envelopeError.error_code,
      status: response.status,
      retryAfter: retryHeader || envelopeError.details?.retry_after,
      details: envelopeError.details || null,
    });
  }

  if (legacyDetail && typeof legacyDetail === 'object') {
    return new AuthRequestError(legacyDetail.message || legacyDetail.detail || fallbackMessage, {
      code: legacyDetail.error_code || legacyDetail.error || 'auth_error',
      status: response.status,
      retryAfter: retryHeader || legacyDetail.details?.retry_after,
      details: legacyDetail.details || null,
    });
  }

  if (typeof legacyDetail === 'string') {
    return new AuthRequestError(legacyDetail, {
      code: legacyDetail,
      status: response.status,
      retryAfter: retryHeader,
    });
  }

  return new AuthRequestError(fallbackMessage, {
    code: response.status >= 500 ? 'server_error' : 'auth_error',
    status: response.status,
    retryAfter: retryHeader,
  });
}

/**
 * Map raw JS/network errors to user-friendly messages.
 * Prevents internal error names and stack details from leaking to the UI.
 */
function friendlyAuthError(err) {
  if (err instanceof TypeError || err.name === 'NetworkError') {
    return 'Connection failed - check your internet connection.';
  }
  if (err instanceof AuthRequestError) {
    if (err.code === 'rate_limited') {
      return `Too many attempts - wait ${retryAfterLabel(err.retryAfter)} before trying again.`;
    }
    if (err.code === 'account_locked') {
      return `This account is temporarily locked. Wait ${retryAfterLabel(err.retryAfter)} and try again.`;
    }
    if (err.code === 'invalid_credentials') {
      return "We couldn't sign you in. Check your email and password.";
    }
    if (err.code === 'cloud_unreachable') {
      return "Can't reach useviola.com - check your connection and try again.";
    }
    if (err.code === 'server_error' || err.status >= 500) {
      return "Viola's account service hit a server error. Try again in a few minutes.";
    }
    if (err.code === 'validation_error' && err.details?.validation_errors?.length) {
      const fieldMessages = err.details.validation_errors.map((v) => {
        const field = (v.loc?.[v.loc.length - 1] || 'field').toString();
        const niceField = field === 'password' ? 'Password' : field === 'email' ? 'Email' : field.charAt(0).toUpperCase() + field.slice(1);
        if (v.type === 'string_too_short' && v.ctx?.min_length) {
          return `${niceField} must be at least ${v.ctx.min_length} characters.`;
        }
        if (v.type === 'string_too_long' && v.ctx?.max_length) {
          return `${niceField} must be at most ${v.ctx.max_length} characters.`;
        }
        if (v.type === 'value_error' && field === 'email') {
          return 'Enter a valid email address.';
        }
        if (v.type === 'missing') {
          return `${niceField} is required.`;
        }
        return v.msg ? `${niceField}: ${v.msg}` : `${niceField} is invalid.`;
      });
      return fieldMessages.join(' ');
    }
    if (err.code === 'weak_password' || err.code === 'invalid_password' || err.code === 'password_too_common') {
      return err.message || 'Choose a stronger password.';
    }
    if (err.code === 'tos_acceptance_required' || err.code === 'age_verification_required') {
      return err.message || 'Please accept the required agreements.';
    }
    if (err.message && err.message.length < 200 && !err.message.includes(' at ') && !err.message.includes('\n')) {
      return err.message;
    }
    return 'Something went wrong. Please try again.';
  }
  if (err.name === 'Unauthorized' || (err.message || '').toLowerCase().includes('unauthorized')) {
    return 'Please sign in again';
  }
  // Only surface the message if it looks user-readable (short, no stack frames).
  const msg = err.message || '';
  if (msg && msg.length < 200 && !msg.includes(' at ') && !msg.includes('\n')) {
    return msg;
  }
  return 'Something went wrong. Please try again.';
}

function supportDetailsForError(err) {
  if (!(err instanceof AuthRequestError)) return null;
  return {
    code: err.code,
    status: err.status,
    retryAfter: err.retryAfter,
    details: err.details,
  };
}

const AuthContext = createContext(null);

function buildSubscription(user) {
  return {
    status: user?.subscription_status || 'free',
    planId: user?.plan_id || 'free',
    planFamily: user?.plan_family || 'free',
    hasPaidAccess: user?.has_paid_access || false,
    paymentProvider: user?.payment_provider || null,
  };
}

function useAuthInternal() {
  const [user, setUser] = useState(null);
  const [subscription, setSubscription] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [errorDetails, setErrorDetails] = useState(null);

  const setAuthError = useCallback((err) => {
    const message = friendlyAuthError(err);
    const details = supportDetailsForError(err);
    setError(message);
    setErrorDetails(details);
    return { message, details };
  }, []);

  // Define fetchCurrentUser with useCallback so it can be in dependency arrays
  const fetchCurrentUser = useCallback(async () => {
    try {
      setLoading(true);
      const response = await fetch(`${API_BASE}/me`, {
        credentials: 'include', // Include session cookie
      });

      if (response.ok) {
        const json = await response.json();
        const data = json.data || json; // Unwrap ResponseEnvelope
        setUser(data.user);
        setSubscription(buildSubscription(data.user));
      } else if (response.status === 401) {
        // Not logged in
        setUser(null);
        setSubscription(null);
      } else if (response.status === 404 || response.status === 503) {
        // Auth not configured or temporarily unavailable — treat as logged out
        setUser(null);
        setSubscription(null);
      } else {
        throw new Error('Failed to fetch user');
      }
    } catch (err) {
      setAuthError(err);
    } finally {
      setLoading(false);
    }
  }, [setAuthError]);

  // Fetch current user on mount
  useEffect(() => {
    fetchCurrentUser();
  }, [fetchCurrentUser]);

  /**
   * Register a new account
   */
  const register = useCallback(async (email, password) => {
    setError(null);
    setErrorDetails(null);
    try {
      const response = await fetch(`${API_BASE}/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password, coppa_age_confirmed: true, tos_accepted: true }),
        credentials: 'include',
      });

      const json = await response.json().catch(() => null);
      const data = unwrapAuthData(json);

      if (!response.ok) {
        throw authErrorFromResponse(response, json, "We couldn't create your account. Please try again.");
      }

      // Registration returns message-only (no user object) to prevent
      // email enumeration. User must verify email then log in.
      return { success: true, message: data.message };
    } catch (err) {
      const { message, details } = setAuthError(err);
      return { success: false, error: message, errorDetails: details };
    }
  }, [setAuthError]);

  /**
   * Login with email and password.
   *
   * If the account has TOTP MFA enabled, the backend returns
   * ``{mfa_required: true, mfa_token: "..."}`` instead of a full user/session
   * payload. Surface that to the caller so the UI can render a TOTP input;
   * use ``verifyMfaTotp`` to complete the second step.
   */
  const login = useCallback(async (email, password) => {
    setError(null);
    setErrorDetails(null);
    try {
      const response = await fetch(`${API_BASE}/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
        credentials: 'include',
      });

      const json = await response.json().catch(() => null);
      const data = unwrapAuthData(json);

      if (!response.ok) {
        throw authErrorFromResponse(response, json, "We couldn't sign you in. Check your email and password.");
      }

      if (data?.mfa_required && data?.mfa_token) {
        return { success: false, mfa_required: true, mfa_token: data.mfa_token };
      }

      // Path A's desktop /auth/login proxy returns a truncated user
      // ``{id, email, email_verified}`` — no subscription_status / plan_id /
      // has_paid_access — so calling ``buildSubscription(data.user)`` would
      // collapse a Pro user to free until the next refresh. Always
      // round-trip /auth/me so subscription state matches canonical truth.
      // (The local-DB-backed login would carry full state, but the React
      // app can't tell which path served the response, so we always refresh.)
      await fetchCurrentUser();
      return { success: true };
    } catch (err) {
      const { message, details } = setAuthError(err);
      return { success: false, error: message, errorDetails: details };
    }
  }, [fetchCurrentUser, setAuthError]);

  /**
   * Complete TOTP MFA challenge after a successful first-factor login.
   * Posts (mfa_token, code) to /auth/mfa/totp/authenticate; on success the
   * desktop hub mints a session via Set-Cookie and we refresh /me.
   */
  const verifyMfaTotp = useCallback(async (mfaToken, code) => {
    setError(null);
    setErrorDetails(null);
    try {
      const response = await fetch(`${API_BASE}/mfa/totp/authenticate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mfa_token: mfaToken, code }),
        credentials: 'include',
      });

      const json = await response.json().catch(() => null);
      const data = unwrapAuthData(json);

      if (!response.ok) {
        throw authErrorFromResponse(response, json, "That code didn't match. Try again or use a backup code.");
      }

      // The desktop MFA proxy returns ``{user: {id, email, email_verified}, session_token}``
      // — note ``user`` does NOT carry subscription_status / plan_id /
      // has_paid_access. Calling ``buildSubscription(data.user)`` here would
      // collapse a Pro user to free until the next refresh. Always
      // round-trip /auth/me so subscription state matches canonical truth.
      await fetchCurrentUser();
      return { success: true };
    } catch (err) {
      const { message, details } = setAuthError(err);
      return { success: false, error: message, errorDetails: details };
    }
  }, [fetchCurrentUser, setAuthError]);

  /**
   * Request a magic link login
   */
  const requestMagicLink = useCallback(async (email) => {
    setError(null);
    setErrorDetails(null);
    try {
      const response = await fetch(`${API_BASE}/magic-link/request`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email }),
        credentials: 'include',
      });

      const json = await response.json().catch(() => null);
      const data = unwrapAuthData(json);

      if (!response.ok) {
        throw authErrorFromResponse(response, json, "We couldn't send the sign-in link. Please try again.");
      }

      return { success: true, message: data.message };
    } catch (err) {
      const { message, details } = setAuthError(err);
      return { success: false, error: message, errorDetails: details };
    }
  }, [setAuthError]);

  /**
   * Logout current session
   */
  const logout = useCallback(async () => {
    setError(null);
    setErrorDetails(null);
    try {
      await fetch(`${API_BASE}/logout`, {
        method: 'POST',
        credentials: 'include',
      });

      setUser(null);
      setSubscription(null);
      return { success: true };
    } catch (err) {
      const { message, details } = setAuthError(err);
      return { success: false, error: message, errorDetails: details };
    }
  }, [setAuthError]);

  /**
   * Start OAuth flow using system browser (Google blocks embedded browsers per RFC 8252).
   *
   * 1. POST /auth/oauth/start to get nonce + auth_url
   * 2. Open auth_url in system browser via Qt bridge (or anchor fallback)
   * 3. Poll /auth/oauth/poll with nonce until success/expired
   */
  const startOAuthFlow = useCallback(async (provider) => {
    setError(null);
    setErrorDetails(null);
    try {
      // Retry with exponential backoff on 503 (backend not ready)
      let response;
      const MAX_RETRIES = 3;
      const BACKOFF_DELAYS = [2000, 4000, 8000];
      for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
        response = await fetch(`${API_BASE}/oauth/start`, {
          method: 'POST',
          credentials: 'include',
        });
        if (response.status !== 503 || attempt === MAX_RETRIES) break;
        await new Promise(r => setTimeout(r, BACKOFF_DELAYS[attempt]));
      }
      if (response.status === 503) {
        throw new Error('OAuth service unavailable after retries. Please try again later.');
      }
      const json = await response.json();
      const data = json.data || json;
      const { nonce, auth_url } = data;

      if (!nonce || !auth_url) {
        throw new Error('Failed to start OAuth flow');
      }

      // Open auth URL — prefer Qt bridge (system browser), fall back to window.open
      if (import.meta.env.DEV) {
        console.log('[OAuth] Opening auth URL, bridge available:', !!window.viola);
      }

      if (window.viola && typeof window.viola.openExternalUrl === 'function') {
        if (import.meta.env.DEV) {
          console.log('[OAuth] Using Qt bridge → system browser');
        }
        window.viola.openExternalUrl(auth_url);
      } else {
        // Qt bridge not ready — try window.open (NOTE: Google may block embedded browsers)
        if (import.meta.env.DEV) {
          console.warn('[OAuth] Qt bridge not available, trying window.open');
        }
        const win = window.open(auth_url, '_blank');
        if (!win) {
          if (import.meta.env.DEV) {
            console.warn('[OAuth] window.open blocked, retrying with noopener');
          }
          const retryWin = window.open(auth_url, '_blank', 'noopener,noreferrer');
          if (!retryWin) {
            throw new Error('OAuth popup was blocked. Please allow popups for this page and try again.');
          }
        }
        // Warn user: system browser is preferred for Google OAuth
        if (import.meta.env.DEV) {
          console.warn(
            '[OAuth] Opened via fallback — Google may show "browser not secure" error. ' +
            'Restart the app if login fails.'
          );
        }
      }

      // Poll for completion
      return new Promise((resolve) => {
        const pollInterval = setInterval(async () => {
          try {
            const pollResp = await fetch(
              `${API_BASE}/oauth/poll?login_nonce=${encodeURIComponent(nonce)}`,
              { credentials: 'include' }
            );
            const pollJson = await pollResp.json();
            const pollData = pollJson.data || pollJson;
            if (pollData.status === 'success') {
              clearInterval(pollInterval);
              await fetchCurrentUser();
              resolve({ success: true });
            } else if (pollData.status === 'expired' || pollData.status === 'error') {
              clearInterval(pollInterval);
              setError(pollData.message || 'Login failed. Please try again.');
              setErrorDetails({ code: pollData.status || 'oauth_error', status: 0, retryAfter: null, details: null });
              resolve({ success: false, error: pollData.message });
            }
          } catch (e) {
            if (import.meta.env.DEV) {
              console.error('[OAuth Poll] error:', e);
            }
          }
        }, 1500);

        // Hard timeout: 5 minutes
        setTimeout(() => {
          clearInterval(pollInterval);
          resolve({ success: false, error: 'Login timed out' });
        }, 5 * 60 * 1000);
      });
    } catch (err) {
      const { message, details } = setAuthError(err);
      return { success: false, error: message, errorDetails: details };
    }
  }, [fetchCurrentUser, setAuthError]);

  /**
   * Clear error state
   */
  const clearError = useCallback(() => {
    setError(null);
    setErrorDetails(null);
  }, []);

  return {
    // State
    user,
    subscription,
    loading,
    error,
    errorDetails,
    isLoggedIn: !!user,
    hasPaidAccess: subscription?.hasPaidAccess || false,

    // Actions
    login,
    verifyMfaTotp,
    register,
    logout,
    requestMagicLink,
    startOAuthFlow,
    refreshUser: fetchCurrentUser,
    clearError,
  };
}

export function AuthProvider({ children }) {
  const auth = useAuthInternal();
  return <AuthContext.Provider value={auth}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within AuthProvider');
  return ctx;
}

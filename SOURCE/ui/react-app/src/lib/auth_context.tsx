import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import type {
  AuthChangeEvent,
  Session,
  User,
} from '@supabase/auth-js';
import { ACCEPTED_PRIVACY_VERSION, ACCEPTED_TERMS_VERSION } from '../auth/legalVersions';
import { AuthContext as CloudFrontDoorContext } from '../auth/AuthProvider';
import { hydrateDesktopSessionFromCookie } from '../auth/authClient';
import { completeDesktopOAuth } from '../auth/desktopOAuth';
import { isDesktopApp } from '../utils/runtimeSurface';
import { externalOAuthAuthorizeUrl, gotrueClient } from './gotrue_client';

type AuthErrorDetails = {
  code: string;
  status: number;
  retryAfter: string | null;
  details: unknown;
};

type AuthActionResult = {
  success: boolean;
  message?: string;
  error?: string;
  errorDetails?: AuthErrorDetails | null;
  // Set by login() when the account has a verified TOTP factor and the
  // password sign-in (AAL1) must step up to AAL2 before it is complete. The
  // UI renders its TOTP prompt and calls verifyMfaTotp(factorId, code).
  mfaRequired?: boolean;
  factorId?: string | null;
};

type RegistrationConsents = {
  tosAccepted: boolean;
  legalEligibilityConfirmed: boolean;
  termsVersion?: string;
  privacyVersion?: string;
};

type PlanState = {
  planTier: string;
  planId: string;
  planFamily: string;
  subscriptionStatus: string;
  hasPaidAccess: boolean;
  paymentProvider: string | null;
};

type ViolaUser = User & {
  email_verified: boolean;
  subscription_status: string;
  plan_id: string;
  plan_family: string;
  has_paid_access: boolean;
  payment_provider: string | null;
};

type AuthContextValue = {
  user: ViolaUser | null;
  session: Session | null;
  subscription: {
    status: string;
    planId: string;
    planFamily: string;
    hasPaidAccess: boolean;
    paymentProvider: string | null;
  } | null;
  plan: PlanState;
  planTier: string;
  loading: boolean;
  error: string | null;
  errorDetails: AuthErrorDetails | null;
  isLoggedIn: boolean;
  hasPaidAccess: boolean;
  passwordRecovery: boolean;
  mfaPending: boolean;
  mfaFactorId: string | null;
  login: (email: string, password: string) => Promise<AuthActionResult>;
  verifyMfaTotp: (factorId: string, code: string) => Promise<AuthActionResult>;
  register: (email: string, password: string, consents: RegistrationConsents) => Promise<AuthActionResult>;
  logout: () => Promise<AuthActionResult>;
  requestMagicLink: (email: string) => Promise<AuthActionResult>;
  verifyEmailOtp: (email: string, token: string) => Promise<AuthActionResult>;
  requestPasswordReset: (email: string) => Promise<AuthActionResult>;
  updatePassword: (password: string) => Promise<AuthActionResult>;
  startOAuthFlow: (provider: string) => Promise<AuthActionResult>;
  refreshUser: () => Promise<void>;
  clearError: () => void;
};

const DEFAULT_PLAN: PlanState = {
  planTier: 'free',
  planId: 'free',
  planFamily: 'free',
  subscriptionStatus: 'free',
  hasPaidAccess: false,
  paymentProvider: null,
};

const AuthContext = createContext<AuthContextValue | null>(null);

function metadataString(metadata: Record<string, unknown>, key: string, fallback = ''): string {
  const value = metadata[key];
  return typeof value === 'string' && value.trim() ? value.trim() : fallback;
}

function metadataBoolean(metadata: Record<string, unknown>, key: string, fallback = false): boolean {
  const value = metadata[key];
  return typeof value === 'boolean' ? value : fallback;
}

export function decodePlanFromUser(user: User | null): PlanState {
  const metadata = (user?.app_metadata || {}) as Record<string, unknown>;
  const planTier = metadataString(metadata, 'plan_tier', 'free');
  const planFamily = metadataString(metadata, 'plan_family', planTier);
  const subscriptionStatus = metadataString(
    metadata,
    'subscription_status',
    planTier === 'free' ? 'free' : 'active',
  );
  const hasPaidAccess = metadataBoolean(
    metadata,
    'has_paid_access',
    planTier !== 'free' && subscriptionStatus !== 'free' && subscriptionStatus !== 'canceled',
  );

  return {
    planTier,
    planId: metadataString(metadata, 'plan_id', planTier),
    planFamily,
    subscriptionStatus,
    hasPaidAccess,
    paymentProvider: metadataString(metadata, 'payment_provider') || null,
  };
}

function buildViolaUser(user: User | null): ViolaUser | null {
  if (!user) return null;
  const plan = decodePlanFromUser(user);
  return {
    ...user,
    email_verified: Boolean(user.email_confirmed_at || user.confirmed_at),
    subscription_status: plan.subscriptionStatus,
    plan_id: plan.planId,
    plan_family: plan.planFamily,
    has_paid_access: plan.hasPaidAccess,
    payment_provider: plan.paymentProvider,
  };
}

function buildSubscription(user: ViolaUser | null) {
  if (!user) return null;
  return {
    status: user.subscription_status || 'free',
    planId: user.plan_id || 'free',
    planFamily: user.plan_family || 'free',
    hasPaidAccess: user.has_paid_access || false,
    paymentProvider: user.payment_provider || null,
  };
}

function decodeJwtPayload(token: string | null | undefined): Record<string, unknown> | null {
  if (!token || typeof atob !== 'function') return null;
  const segment = token.split('.')[1];
  if (!segment) return null;
  try {
    const normalized = segment.replace(/-/g, '+').replace(/_/g, '/');
    return JSON.parse(atob(normalized)) as Record<string, unknown>;
  } catch {
    return null;
  }
}

type MfaStepUp = { pending: boolean; factorId: string | null };

// A password sign-in only ever satisfies AAL1 ("Authenticator Assurance
// Level" 1 — a single factor). If the account has a VERIFIED TOTP factor,
// GoTrue requires an AAL1 -> AAL2 step-up (mfa.challenge + mfa.verify) before
// the session is fully authenticated. We derive that need synchronously from
// the session itself — the access token's `aal` claim plus the user's factor
// list — so the UI can gate on it without a network round-trip and without an
// async flash of the signed-in profile over a half-authenticated session.
function mfaStepUpForSession(session: Session | null): MfaStepUp {
  if (!session) return { pending: false, factorId: null };
  const factors = ((session.user?.factors || []) as Array<{
    id: string;
    status?: string;
    factor_type?: string;
  }>);
  const verifiedTotp = factors.find(
    (factor) => factor.factor_type === 'totp' && factor.status === 'verified',
  );
  if (!verifiedTotp) return { pending: false, factorId: null };
  const payload = decodeJwtPayload(session.access_token);
  const currentAal = typeof payload?.aal === 'string' ? payload.aal : 'aal1';
  // Fail-closed: a verified TOTP factor with anything other than a proven AAL2
  // token still needs the second factor (an undecodable token is treated as
  // not-yet-stepped-up rather than silently waved through).
  if (currentAal === 'aal2') return { pending: false, factorId: verifiedTotp.id };
  return { pending: true, factorId: verifiedTotp.id };
}

function retryAfterLabel(seconds: string | number | null | undefined) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) return 'a few minutes';
  if (value < 60) return `${Math.ceil(value)} seconds`;
  const minutes = Math.ceil(value / 60);
  return minutes === 1 ? '1 minute' : `${minutes} minutes`;
}

function authErrorDetails(err: unknown): AuthErrorDetails | null {
  if (!err || typeof err !== 'object') return null;
  const errorLike = err as {
    code?: string;
    status?: number;
    message?: string;
    details?: unknown;
  };
  const code = errorLike.code || 'auth_error';
  const status = typeof errorLike.status === 'number' ? errorLike.status : 0;
  return {
    code,
    status,
    retryAfter: null,
    details: errorLike.details || null,
  };
}

function friendlyAuthError(err: unknown): string {
  if (err instanceof TypeError || (err as { name?: string })?.name === 'NetworkError') {
    return 'Connection failed - check your internet connection.';
  }

  const errorLike = err as { code?: string; status?: number; message?: string; name?: string };
  const code = errorLike?.code || 'auth_error';
  const status = errorLike?.status || 0;
  const message = errorLike?.message || '';
  const lowerMessage = message.toLowerCase();

  if (status === 429 || code === 'over_request_rate_limit' || code === 'rate_limited') {
    return `Too many attempts - wait ${retryAfterLabel(null)} before trying again.`;
  }
  if (code === 'invalid_credentials' || lowerMessage.includes('invalid login credentials')) {
    return "We couldn't sign you in. Check your email and password.";
  }
  if (code === 'email_not_confirmed' || lowerMessage.includes('email not confirmed')) {
    return 'Check your email and verify your account before signing in.';
  }
  if (code === 'weak_password' || code === 'invalid_password' || lowerMessage.includes('password')) {
    return message || 'Choose a stronger password.';
  }
  if (status >= 500) {
    return "Viola's account service hit a server error. Try again in a few minutes.";
  }
  if (message && message.length < 200 && !message.includes(' at ') && !message.includes('\n')) {
    return message;
  }
  return 'Something went wrong. Please try again.';
}

const VIOLA_ACCOUNT_AUTH_URL = 'https://useviola.com/login';

export function authRedirectUrl(type?: string): string {
  const url = new URL(VIOLA_ACCOUNT_AUTH_URL);
  if (type) {
    url.searchParams.set('auth', type);
  }
  return url.toString();
}

function openExternalAuthUrl(url: string) {
  const violaBridge = (window as unknown as {
    viola?: { openExternalUrl?: (target: string) => void };
  }).viola;

  if (violaBridge?.openExternalUrl) {
    violaBridge.openExternalUrl(url);
    return;
  }

  const authWindow = window.open(url, '_blank');
  if (authWindow) return;

  const retryWindow = window.open(url, '_blank', 'noopener,noreferrer');
  if (!retryWindow) {
    throw new Error('OAuth popup was blocked. Please allow popups for this page and try again.');
  }
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [user, setUser] = useState<ViolaUser | null>(null);
  const [plan, setPlan] = useState<PlanState>(DEFAULT_PLAN);
  const [subscription, setSubscription] = useState<AuthContextValue['subscription']>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [errorDetails, setErrorDetails] = useState<AuthErrorDetails | null>(null);
  const [passwordRecovery, setPasswordRecovery] = useState(false);
  const [mfaPending, setMfaPending] = useState(false);
  const [mfaFactorId, setMfaFactorId] = useState<string | null>(null);

  // The cloud front door this store is nested inside on the cloud surface
  // (App.jsx `Dashboard()`), or null in the trees that mount this provider on
  // its own. Held in a ref so `refreshUser` keeps a stable identity across the
  // front door's per-session context churn — UpgradePanel's post-checkout
  // listener has it in an effect dependency list.
  const frontDoor = useContext(CloudFrontDoorContext) as
    { refreshSessionNow?: (() => Promise<void>) | null } | null;
  const frontDoorRef = useRef(frontDoor);
  useEffect(() => {
    frontDoorRef.current = frontDoor;
  }, [frontDoor]);

  const applySession = useCallback((nextSession: Session | null) => {
    const nextUser = buildViolaUser(nextSession?.user || null);
    const nextPlan = decodePlanFromUser(nextSession?.user || null);
    const stepUp = mfaStepUpForSession(nextSession);
    setSession(nextSession);
    setUser(nextUser);
    setPlan(nextPlan);
    setSubscription(buildSubscription(nextUser));
    setMfaPending(stepUp.pending);
    setMfaFactorId(stepUp.factorId);
  }, []);

  const setAuthError = useCallback((err: unknown) => {
    const message = friendlyAuthError(err);
    const details = authErrorDetails(err);
    setError(message);
    setErrorDetails(details);
    return { message, details };
  }, []);

  const refreshUser = useCallback(async () => {
    // Force a GoTrue token refresh so server-side app_metadata changes are
    // reflected. Plan/entitlement fields (plan_tier, has_paid_access, ...) live
    // in the access token's app_metadata, which the Stripe webhook updates on
    // the GoTrue user AFTER checkout completes. getSession() alone returns the
    // CACHED JWT, so it keeps showing the stale (Free) plan until an unrelated
    // refresh — a refresh mints a new access token whose app_metadata reflects
    // the upgrade, which is what lets the account tab show the paid plan on
    // return from checkout without re-login (#2609).
    //
    // On the cloud surface the front door (auth/AuthProvider) owns the single
    // rotating refresh token and mirrors every rotation back into this store,
    // so ASK IT to refresh rather than redeeming the same token here. Two
    // independent redemptions of one rotating token is the shape GoTrue's
    // reuse detection answers by revoking the whole family: the customer who
    // just paid gets signed out at the front door's next scheduled refresh
    // (candidate C-400). `refreshSessionNow` resolves only once this store has
    // the rotated pair, so the new plan is live when it returns. Where no such
    // owner exists (the desktop app, whose refreshes are serialized
    // server-side by auth/desktop_gotrue_proxy.py, and the standalone
    // ReviewPage/spoke trees) this store refreshes for itself, unchanged.
    const delegateRefresh = frontDoorRef.current?.refreshSessionNow;
    if (delegateRefresh) {
      await delegateRefresh();
      const { data: bridged } = await gotrueClient.getSession();
      applySession(bridged.session || null);
      return;
    }

    const { data, error: refreshError } = await gotrueClient.refreshSession();
    if (refreshError) {
      // A failed refresh (offline, or no/expired refresh token) must NOT sign
      // the user out — fall back to the cached session rather than dropping to
      // null and re-showing the sign-in form over a still-valid session.
      const { data: cached } = await gotrueClient.getSession();
      applySession(cached.session || null);
      return;
    }
    applySession(data.session || null);
  }, [applySession]);

  useEffect(() => {
    let mounted = true;

    // #2604 — persistent desktop sign-in. SEC-017 keeps the webview's GoTrue
    // tokens in memory only (never localStorage), so a fresh launch has no
    // in-memory session and getSession() returns null. On the desktop app the
    // session is persisted server-side (encrypted at rest) behind the opaque
    // httpOnly viola_session cookie that survives the relaunch; hydrate the
    // client from it before deciding signed-out, so the user is not forced to
    // re-enter credentials every launch. Any real signed-out state (no cookie /
    // after sign-out) resolves to null exactly as before. Never runs on the
    // cloud surface (no desktop session store there).
    const resolveInitialSession = async (): Promise<Session | null> => {
      const { data } = await gotrueClient.getSession();
      if (data.session) return data.session;
      if (!isDesktopApp()) return null;
      const hydrated = await hydrateDesktopSessionFromCookie();
      if (!hydrated.ok || !hydrated.session) return null;
      // Seed the auth-js client so its normal auto-refresh lifecycle takes over
      // (the returned pair is already rotated/valid from the store).
      const { data: setData } = await gotrueClient.setSession({
        access_token: hydrated.session.access_token,
        refresh_token: hydrated.session.refresh_token,
      });
      return setData.session || null;
    };

    resolveInitialSession()
      .then((initialSession) => {
        if (mounted) applySession(initialSession);
      })
      .catch((err) => {
        if (mounted) setAuthError(err);
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });

    const { data } = gotrueClient.onAuthStateChange((event: AuthChangeEvent, nextSession) => {
      if (event === 'PASSWORD_RECOVERY') {
        setPasswordRecovery(true);
      }
      if (event === 'SIGNED_OUT') {
        setPasswordRecovery(false);
      }
      applySession(nextSession);
      setLoading(false);
    });

    return () => {
      mounted = false;
      data.subscription.unsubscribe();
    };
  }, [applySession, setAuthError]);

  const clearError = useCallback(() => {
    setError(null);
    setErrorDetails(null);
  }, []);

  const login = useCallback(async (email: string, password: string): Promise<AuthActionResult> => {
    clearError();
    try {
      const { data, error: signInError } = await gotrueClient.signInWithPassword({
        email: email.trim(),
        password,
      });
      if (signInError) throw signInError;
      applySession(data.session || null);
      // GoTrue returns a session for an MFA-enrolled account, but only at AAL1.
      // Surface the required TOTP step-up so the UI can prompt for the second
      // factor instead of treating the half-authenticated session as done.
      const stepUp = mfaStepUpForSession(data.session || null);
      if (stepUp.pending && stepUp.factorId) {
        return { success: false, mfaRequired: true, factorId: stepUp.factorId };
      }
      return { success: true };
    } catch (err) {
      const { message, details } = setAuthError(err);
      return { success: false, error: message, errorDetails: details };
    }
  }, [applySession, clearError, setAuthError]);

  const verifyMfaTotp = useCallback(async (
    factorId: string,
    code: string,
  ): Promise<AuthActionResult> => {
    clearError();
    try {
      // challengeAndVerify does the GoTrue AAL2 step-up (mfa.challenge then
      // mfa.verify) and, on success, upgrades the client's stored session to
      // AAL2. Re-read it so React state reflects the fully-authenticated
      // (step-up cleared) session as canonical truth.
      const { error: verifyError } = await gotrueClient.mfa.challengeAndVerify({
        factorId,
        code: code.trim(),
      });
      if (verifyError) throw verifyError;
      const { data: sessionData } = await gotrueClient.getSession();
      applySession(sessionData.session || null);
      return { success: true };
    } catch (err) {
      const { message, details } = setAuthError(err);
      return { success: false, error: message, errorDetails: details };
    }
  }, [applySession, clearError, setAuthError]);

  const register = useCallback(
    async (
      email: string,
      password: string,
      consents: RegistrationConsents = { tosAccepted: false, legalEligibilityConfirmed: false },
    ): Promise<AuthActionResult> => {
      clearError();
      try {
        const { error: signUpError } = await gotrueClient.signUp({
          email: email.trim(),
          password,
          options: {
            emailRedirectTo: authRedirectUrl('verify'),
            data: {
              coppa_age_confirmed: consents.legalEligibilityConfirmed === true,
              legal_eligibility_confirmed: consents.legalEligibilityConfirmed === true,
              tos_accepted: consents.tosAccepted === true,
              terms_version: consents.termsVersion || ACCEPTED_TERMS_VERSION,
              privacy_version: consents.privacyVersion || ACCEPTED_PRIVACY_VERSION,
            },
          },
        });
        if (signUpError) throw signUpError;
        return {
          success: true,
          message: 'Account created. Check your email to verify, then sign in.',
        };
      } catch (err) {
        const { message, details } = setAuthError(err);
        return { success: false, error: message, errorDetails: details };
      }
    },
    [clearError, setAuthError],
  );

  const requestMagicLink = useCallback(async (email: string): Promise<AuthActionResult> => {
    clearError();
    try {
      const { error: otpError } = await gotrueClient.signInWithOtp({
        email: email.trim(),
        options: {
          shouldCreateUser: false,
          emailRedirectTo: authRedirectUrl('magic'),
        },
      });
      if (otpError) throw otpError;
      return { success: true, message: 'Magic link sent' };
    } catch (err) {
      const { message, details } = setAuthError(err);
      return { success: false, error: message, errorDetails: details };
    }
  }, [clearError, setAuthError]);

  const verifyEmailOtp = useCallback(async (
    email: string,
    token: string,
  ): Promise<AuthActionResult> => {
    clearError();
    try {
      const { data, error: otpError } = await gotrueClient.verifyOtp({
        email: email.trim(),
        token: token.trim(),
        type: 'magiclink',
      });
      if (otpError) throw otpError;
      applySession(data.session || null);
      return { success: true };
    } catch (err) {
      const { message, details } = setAuthError(err);
      return { success: false, error: message, errorDetails: details };
    }
  }, [applySession, clearError, setAuthError]);

  const requestPasswordReset = useCallback(async (email: string): Promise<AuthActionResult> => {
    clearError();
    try {
      const { error: resetError } = await gotrueClient.resetPasswordForEmail(email.trim(), {
        redirectTo: authRedirectUrl('recovery'),
      });
      if (resetError) throw resetError;
      return {
        success: true,
        message: 'If an account exists with that email, you will receive a reset link shortly.',
      };
    } catch (err) {
      const { message, details } = setAuthError(err);
      return { success: false, error: message, errorDetails: details };
    }
  }, [clearError, setAuthError]);

  const updatePassword = useCallback(async (password: string): Promise<AuthActionResult> => {
    clearError();
    try {
      const { data, error: updateError } = await gotrueClient.updateUser({ password });
      if (updateError) throw updateError;
      const { data: sessionData } = await gotrueClient.getSession();
      applySession(sessionData.session || null);
      setPasswordRecovery(false);
      return { success: true, message: 'Password updated. You can keep using Viola.' };
    } catch (err) {
      const { message, details } = setAuthError(err);
      return { success: false, error: message, errorDetails: details };
    }
  }, [applySession, clearError, setAuthError]);

  const logout = useCallback(async (): Promise<AuthActionResult> => {
    clearError();
    try {
      const { error: signOutError } = await gotrueClient.signOut({ scope: 'local' });
      if (signOutError) throw signOutError;
      applySession(null);
      setPasswordRecovery(false);
      return { success: true };
    } catch (err) {
      const { message, details } = setAuthError(err);
      return { success: false, error: message, errorDetails: details };
    }
  }, [applySession, clearError, setAuthError]);

  // Provider sign-in. Resolves `success: true` ONLY once a real session
  // exists — never on "we opened a browser tab".
  //
  // The old shape returned success the instant it opened the tab, while the
  // PKCE code_verifier stayed in this webview and the authorization code went
  // to the browser, so the exchange could never happen and the UI said it had.
  // Production GoTrue still holds four abandoned authorization codes from
  // three real people who were told they were signed in and were not.
  //
  // Desktop runs the RFC 8252 loopback flow (auth/desktopOAuth.ts), which
  // works precisely BECAUSE this webview never navigates away: SEC-017 keeps
  // auth-js's storage in the JS heap (lib/sessionStore.js), so the verifier
  // survives only as long as this page does. A browser surface, where the
  // flow requires a top-level redirect, destroys the verifier by definition —
  // so there is no honest provider sign-in to offer there, and this refuses
  // rather than pretending. That surface has its own front door.
  const startOAuthFlow = useCallback(async (provider: string): Promise<AuthActionResult> => {
    clearError();
    if (!isDesktopApp()) {
      const message = 'Provider sign-in is not available here. Use your email and password.';
      setError(message);
      return { success: false, error: message, errorDetails: null };
    }
    try {
      const outcome = await completeDesktopOAuth(
        provider,
        (url: string) => openExternalAuthUrl(externalOAuthAuthorizeUrl(url)),
      );
      if (outcome.status !== 'signed_in') {
        setError(outcome.message);
        setErrorDetails({ code: outcome.code, status: 0, retryAfter: null, details: null });
        return { success: false, error: outcome.message };
      }
      applySession(outcome.session);
      return { success: true };
    } catch (err) {
      const { message, details } = setAuthError(err);
      return { success: false, error: message, errorDetails: details };
    }
  }, [applySession, clearError, setAuthError]);

  const value = useMemo<AuthContextValue>(() => ({
    user,
    session,
    subscription,
    plan,
    planTier: plan.planTier,
    loading,
    error,
    errorDetails,
    isLoggedIn: !!session && !!user,
    hasPaidAccess: plan.hasPaidAccess,
    passwordRecovery,
    mfaPending,
    mfaFactorId,
    login,
    verifyMfaTotp,
    register,
    logout,
    requestMagicLink,
    verifyEmailOtp,
    requestPasswordReset,
    updatePassword,
    startOAuthFlow,
    refreshUser,
    clearError,
  }), [
    user,
    session,
    subscription,
    plan,
    loading,
    error,
    errorDetails,
    passwordRecovery,
    mfaPending,
    mfaFactorId,
    login,
    verifyMfaTotp,
    register,
    logout,
    requestMagicLink,
    verifyEmailOtp,
    requestPasswordReset,
    updatePassword,
    startOAuthFlow,
    refreshUser,
    clearError,
  ]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within AuthProvider');
  return ctx;
}

/**
 * Auth state when a provider is mounted, otherwise null.
 *
 * For a component that only wants to *describe* account state (e.g. the Cloud
 * Sync row in Settings, which is meaningless without an account) rather than
 * depend on it. `useAuth()` throws outside a provider, which would make an
 * incidental read of "am I signed in" a hard dependency for every mount and
 * every test of that component; this returns null there instead, and callers
 * treat null as signed out — the fail-closed reading.
 */
export function useOptionalAuth(): AuthContextValue | null {
  return useContext(AuthContext);
}

export function usePlan() {
  const { plan, planTier, hasPaidAccess, subscription } = useAuth();
  return { ...plan, planTier, hasPaidAccess, subscription };
}

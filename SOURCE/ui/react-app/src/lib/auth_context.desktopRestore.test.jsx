/**
 * Desktop sign-in restore (1.0.5 build 5, A2.7): the backend was signed in but
 * Settings > Account showed the Sign In form. The page restores its session
 * from the desktop cookie exactly once at launch; a transient failure there
 * (backend still starting, network or cloud blip) left it signed out for the
 * whole run. It now retries those, and the Account page can ask again.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import { AuthProvider, useAuth } from './auth_context';
import { hydrateDesktopSessionFromCookie } from '../auth/authClient';
import { gotrueClient } from './gotrue_client';

const SESSION = {
  access_token: 'synthetic-access',
  refresh_token: 'synthetic-refresh',
  user: { id: 'account-a', email: 'account@example.com', app_metadata: {}, user_metadata: {} },
};

vi.mock('./billingEntitlement', () => ({
  readBillingSubscription: vi.fn(() => new Promise(() => {})),
}));
vi.mock('../auth/authClient', () => ({ hydrateDesktopSessionFromCookie: vi.fn() }));
vi.mock('./gotrue_client', () => ({
  GOTRUE_STORAGE_KEY: 'synthetic-gotrue-session',
  externalOAuthAuthorizeUrl: vi.fn(),
  gotrueClient: {
    getSession: vi.fn(async () => ({ data: { session: null } })),
    setSession: vi.fn(async () => ({ data: { session: SESSION }, error: null })),
    signOut: vi.fn(async () => ({ error: null })),
    onAuthStateChange: vi.fn(() => ({ data: { subscription: { unsubscribe: vi.fn() } } })),
  },
}));

let api;
function Probe() {
  api = useAuth();
  return <span data-testid="state">{api.loading ? 'loading' : api.isLoggedIn ? `in:${api.user.email}` : 'out'}</span>;
}

const transientMiss = { ok: false, session: null, error: { status: 503, code: 'auth_service_unavailable' } };
const signedOutMiss = { ok: false, session: null, error: { status: 400, code: 'invalid_grant' } };
const hit = { ok: true, session: { access_token: 'synthetic-access', refresh_token: 'synthetic-refresh' }, error: null };

beforeEach(() => {
  localStorage.clear();
  window.viola = { __pending: true };
  vi.useFakeTimers();
});
afterEach(() => {
  cleanup();
  delete window.viola;
  vi.useRealTimers();
  vi.clearAllMocks();
});

it('retries a transient cookie-restore failure and ends up signed in', async () => {
  hydrateDesktopSessionFromCookie.mockResolvedValueOnce(transientMiss).mockResolvedValueOnce(hit);
  await act(async () => { render(<AuthProvider><Probe /></AuthProvider>); });
  expect(screen.getByTestId('state').textContent).toBe('out');

  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });

  expect(hydrateDesktopSessionFromCookie).toHaveBeenCalledTimes(2);
  expect(gotrueClient.setSession).toHaveBeenCalledTimes(1);
  expect(screen.getByTestId('state').textContent).toBe('in:account@example.com');
});

it('does not retry a real signed-out answer', async () => {
  hydrateDesktopSessionFromCookie.mockResolvedValue(signedOutMiss);
  await act(async () => { render(<AuthProvider><Probe /></AuthProvider>); });
  await act(async () => { await vi.advanceTimersByTimeAsync(60000); });

  expect(hydrateDesktopSessionFromCookie).toHaveBeenCalledTimes(1);
  expect(screen.getByTestId('state').textContent).toBe('out');
});

it('stops retrying after a bounded number of attempts', async () => {
  hydrateDesktopSessionFromCookie.mockResolvedValue(transientMiss);
  await act(async () => { render(<AuthProvider><Probe /></AuthProvider>); });
  await act(async () => { await vi.advanceTimersByTimeAsync(10 * 60000); });

  expect(hydrateDesktopSessionFromCookie).toHaveBeenCalledTimes(6);
  expect(screen.getByTestId('state').textContent).toBe('out');
});

it('restoreDesktopSession() lets the Account page ask again on demand', async () => {
  hydrateDesktopSessionFromCookie.mockResolvedValueOnce(signedOutMiss).mockResolvedValueOnce(hit);
  await act(async () => { render(<AuthProvider><Probe /></AuthProvider>); });
  expect(screen.getByTestId('state').textContent).toBe('out');

  let restored;
  await act(async () => { restored = await api.restoreDesktopSession(); });

  expect(restored).toBe(true);
  expect(screen.getByTestId('state').textContent).toBe('in:account@example.com');
});

it('never restores over a sign-out the user asked for', async () => {
  const intent = await import('../auth/logoutIntent');
  intent.beginLogoutIntent();
  hydrateDesktopSessionFromCookie.mockResolvedValue(hit);
  await act(async () => { render(<AuthProvider><Probe /></AuthProvider>); });

  let restored;
  await act(async () => { restored = await api.restoreDesktopSession(); });

  expect(restored).toBe(false);
  expect(gotrueClient.setSession).not.toHaveBeenCalled();
  expect(screen.getByTestId('state').textContent).toBe('out');
});

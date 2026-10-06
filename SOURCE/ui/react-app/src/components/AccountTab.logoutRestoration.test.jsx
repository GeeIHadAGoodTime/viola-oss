import { AuthApiError } from '@supabase/auth-js';
import { beforeEach, afterEach, it, expect, vi } from 'vitest';
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react';
import { UiStateProvider } from '../state/uiState';
import { AuthProvider as FrontDoor, __setInMemorySessionForTest } from '../auth/AuthProvider';
import { useAuth as useFrontDoor } from '../auth/useAuth';
import { AuthProvider as AppStore, useAuth as useAppStore } from '../hooks/useAuth';
import { gotrueClient } from '../lib/gotrue_client';
import { AccountTab } from './AccountTab';
import CloudAuthGate from './auth/CloudAuthGate';
import { beginInteractiveSignIn, completeInteractiveSignIn, waitForSessionRestoration } from '../auth/logoutIntent';
import { signOut as clientSignOut } from '../auth/authClient';

function jsonResponse(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, headers: { get: () => null }, text: async () => JSON.stringify(body), json: async () => body };
}
function jwt(payload) {
  const part = (value) => btoa(JSON.stringify(value)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  return `${part({ alg: 'none' })}.${part(payload)}.${part({ synthetic: true })}`;
}
let front;
let app;
function Probe() {
  front = useFrontDoor();
  app = useAppStore();
  return <div><span data-testid="front-status">{front.status}</span><span data-testid="sdk-signed-in">{String(app.isLoggedIn)}</span><span data-testid="sdk-loading">{String(app.loading)}</span></div>;
}
function desktopApp() {
  return render(<UiStateProvider><FrontDoor><AppStore><Probe /><AccountTab /></AppStore></FrontDoor></UiStateProvider>);
}

beforeEach(() => { localStorage.clear(); __setInMemorySessionForTest(null); window.viola = {}; });
afterEach(async () => { delete window.viola; vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it('reports the unconfirmed server outcome instead of claiming complete revocation on transport-status0', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Synthetic transport offline'); }));
  const result = await clientSignOut('synthetic-existing-session');
  // Existing local sign-out usability must be retained; server confirmation is separate.
  expect(result.ok).toBe(true);
  expect(result.remoteRevocationConfirmed).toBe(false);
});

it.each([['offline-logout', true, true, 'signedOut'], ['acknowledged-logout', true, false, 'signedOut'], ['ordinary-persistence', false, false, 'signedIn']])('desktop cookie reload respects logout outcome (%s)', async (_name, performLogout, offline, expectedStatus) => {
  const user = { id: 'synthetic-cookie-account', email: 'synthetic@example.invalid', factors: [], app_metadata: {}, user_metadata: {} };
  const exp = Math.floor(Date.now() / 1000) + 3600;
  const session = { access_token: jwt({ sub: user.id, exp, aal: 'aal1' }), refresh_token: 'synthetic-cookie-refresh', expires_in: 3600, expires_at: exp, token_type: 'bearer', user };
  const state = { cookieLive: true, offlineLogout: false, allowUserRead: false, deliveredLogout: 0, attemptedLogout: 0, hydrated: 0 };
  const fetchMock = vi.fn(async (input, init) => {
    const url = new URL(typeof input === 'string' ? input : input.url, 'http://localhost');
    if (url.pathname.endsWith('/auth/v1/logout')) {
      state.attemptedLogout += 1;
      if (state.offlineLogout) throw new TypeError('Synthetic transport failed before reaching backend');
      state.deliveredLogout += 1;
      state.cookieLive = false;
      return jsonResponse({}, 204);
    }
    if (url.pathname.endsWith('/auth/v1/token') && url.searchParams.get('grant_type') === 'refresh_token') {
      expect(init.credentials).toBe('same-origin');
      expect(JSON.parse(init.body || '{}')).not.toHaveProperty('refresh_token');
      state.hydrated += 1;
      return state.cookieLive ? jsonResponse(session) : jsonResponse({ error: 'invalid_grant' }, 400);
    }
    if (url.pathname.endsWith('/auth/v1/user')) {
      return state.allowUserRead ? jsonResponse(user) : jsonResponse({ message: 'Synthetic initial SDK user read unavailable' }, 503);
    }
    return jsonResponse({}, 200);
  });
  vi.stubGlobal('fetch', fetchMock);
  await gotrueClient.stopAutoRefresh();
  await gotrueClient.signOut({ scope: 'local' });
  state.cookieLive = true;
  state.deliveredLogout = 0;
  state.attemptedLogout = 0;
  let view = desktopApp();
  try {
    await waitFor(() => expect(screen.getByTestId('front-status')).toHaveTextContent('signedIn'));
    await waitFor(() => expect(screen.getByTestId('sdk-loading')).toHaveTextContent('false'));
    await waitFor(() => expect(screen.getByTestId('sdk-signed-in')).toHaveTextContent('false'));
    expect((await gotrueClient.getSession()).data.session).toBeNull();
    state.offlineLogout = offline;
    if (performLogout) {
      await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Sign Out' })); });
      await waitFor(() => expect(screen.getByTestId('front-status')).toHaveTextContent('signedOut'));
      expect(front.signOutFeedback?.error || null).toBeNull();
      expect(front.signOutFeedback?.pending || false).toBe(false);
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
      expect(state.attemptedLogout).toBe(1);
      expect(state.deliveredLogout).toBe(offline ? 0 : 1);
      expect(state.cookieLive).toBe(offline);
    }
    const hydrationCallsBeforeReload = state.hydrated;
    view.unmount();
    // A new document loses only the documented in-memory front-door state.
    __setInMemorySessionForTest(null);
    state.offlineLogout = false;
    state.allowUserRead = true;
    view = desktopApp();
    await waitFor(() => expect(screen.getByTestId('sdk-loading')).toHaveTextContent('false'));
    await waitFor(() => expect(screen.getByTestId('front-status')).not.toHaveTextContent('loading'));
    expect({ status: front.status, appSignedIn: app.isLoggedIn }).toEqual({ status: expectedStatus, appSignedIn: expectedStatus === 'signedIn' });
    if (performLogout) expect(state.hydrated).toBe(hydrationCallsBeforeReload);
  } finally {
    state.offlineLogout = false;
    view.unmount();
    await gotrueClient.signOut({ scope: 'local' });
  }
});

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
function syntheticSession(id = 'synthetic-restored') {
  const user = { id, email: `${id}@example.invalid`, factors: [], app_metadata: {}, user_metadata: {} };
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return { access_token: jwt({ sub: id, exp, aal: 'aal1' }), refresh_token: `synthetic-refresh-${id}`, expires_in: 3600, expires_at: exp, token_type: 'bearer', user };
}
async function installControlledServer({ holdCookie = false, holdUser = false, cleanupFails = false, samePair = false, holdPassword = false } = {}) {
  const old = syntheticSession();
  const next = samePair ? old : syntheticSession('synthetic-new-login');
  const releaseCookie = deferred();
  const releasePassword = deferred();
  const releaseUser = deferred();
  const state = { cookieCalls: 0, userCalls: 0, localLogouts: 0, globalLogouts: 0, passwordCalls: 0, offline: true, cleanupFails, holdUser };
  vi.stubGlobal('fetch', vi.fn(async (input, init) => {
    const url = new URL(typeof input === 'string' ? input : input.url, 'http://localhost');
    if (url.pathname.endsWith('/logout')) {
      if (url.searchParams.get('scope') === 'local') {
        state.localLogouts += 1;
        return state.cleanupFails ? jsonResponse({ message: 'Synthetic local cleanup refusal' }, 503) : jsonResponse({}, 204);
      }
      state.globalLogouts += 1;
      if (state.offline) throw new TypeError('Synthetic unreachable logout');
      return jsonResponse({}, 204);
    }
    if (url.pathname.endsWith('/token')) {
      if (url.searchParams.get('grant_type') === 'password') {
        state.passwordCalls += 1;
        if (holdPassword) await releasePassword.promise;
        if (state.failPassword) return jsonResponse({ message: 'Synthetic password request failed before any session write' }, 503);
        return jsonResponse(next);
      }
      state.cookieCalls += 1;
      if (holdCookie) await releaseCookie.promise;
      return jsonResponse(old);
    }
    if (url.pathname.endsWith('/user')) {
      state.userCalls += 1;
      if (state.holdUser) await releaseUser.promise;
      const header = init?.headers?.Authorization || init?.headers?.authorization || '';
      return jsonResponse(String(header).includes(next.access_token) ? next.user : old.user);
    }
    return jsonResponse({}, 200);
  }));
  await gotrueClient.stopAutoRefresh();
  state.cleanupFails = false;
  await gotrueClient.signOut({ scope: 'local' });
  state.cleanupFails = cleanupFails;
  state.localLogouts = 0;
  return { state, old, next, releaseCookie, releaseUser, releasePassword };
}

it('a cookie response already in flight cannot restore either provider after offline logout', async () => {
  const server = await installControlledServer({ holdCookie: true });
  const view = desktopApp();
  try {
    await waitFor(() => expect(server.state.cookieCalls).toBe(2));
    await act(async () => { expect((await front.signOut()).ok).toBe(true); });
    await act(async () => { server.releaseCookie.resolve(); });
    await waitFor(() => expect(app.loading).toBe(false));
    expect(front.status).toBe('signedOut');
    expect(app.isLoggedIn).toBe(false);
    expect((await gotrueClient.getSession()).data.session).toBeNull();
    expect(server.state.userCalls).toBe(0);
    expect(screen.getByText(/Server sign-out could not be confirmed/)).toBeInTheDocument();
  } finally { server.releaseCookie.resolve(); view.unmount(); await gotrueClient.signOut({ scope: 'local' }); }
});

it.each([false, true])('a stale admitted SDK hydration drains before a newer explicit login, including same-pair ABA (%s)', async (samePair) => {
  const server = await installControlledServer({ holdUser: true, samePair });
  let view = desktopApp();
  try {
    await waitFor(() => expect(server.state.userCalls).toBe(1));
    await waitFor(() => expect(front.status).toBe('signedIn'));
    let logout;
    let logoutDone = false;
    await act(async () => { logout = front.signOut().then((value) => { logoutDone = true; return value; }); });
    expect(logoutDone).toBe(false);
    // Both the SDK and restoration admission belong to the document, not a mount.
    view.unmount();
    view = desktopApp();
    await waitFor(() => expect(app.loading).toBe(false));
    let login;
    await act(async () => { login = app.login('synthetic@example.invalid', 'synthetic password'); });
    expect(server.state.passwordCalls).toBe(0);
    await act(async () => { server.state.holdUser = false; server.releaseUser.resolve(); await Promise.all([logout, login]); });
    expect(await login).toMatchObject({ success: true });
    expect(app.user.id).toBe(server.next.user.id);
    expect((await gotrueClient.getSession()).data.session.access_token).toBe(server.next.access_token);
    expect(server.state.localLogouts).toBe(1);
    expect(server.state.passwordCalls).toBe(1);
    // A fresh document can restore again only because the explicit login won.
    __setInMemorySessionForTest(null);
    expect(JSON.parse(localStorage.getItem('viola-logout-intent-v1')).blocked).toBe(false);
  } finally { server.releaseUser.resolve(); server.state.cleanupFails = false; view.unmount(); await gotrueClient.signOut({ scope: 'local' }); }
});

it('an admitted SDK hydration cleanup refusal is not acknowledged as successful logout and can be retried', async () => {
  const server = await installControlledServer({ holdUser: true, cleanupFails: true });
  const view = desktopApp();
  try {
    await waitFor(() => expect(server.state.userCalls).toBe(1));
    let logout;
    await act(async () => { logout = front.signOut(); });
    await act(async () => { server.state.holdUser = false; server.releaseUser.resolve(); await logout; });
    expect(await logout).toMatchObject({ ok: false });
    expect(front.signOutFeedback.error).toBeTruthy();
    expect(app.isLoggedIn).toBe(false);
    expect((await gotrueClient.getSession()).data.session).not.toBeNull();
    server.state.cleanupFails = false;
    let retry;
    await act(async () => { retry = await front.signOut(); });
    expect(retry.ok).toBe(true);
    expect((await gotrueClient.getSession()).data.session).toBeNull();
    expect(server.state.localLogouts).toBe(2);
  } finally { server.releaseUser.resolve(); server.state.cleanupFails = false; view.unmount(); await gotrueClient.signOut({ scope: 'local' }); }
});

it('a password grant admitted before a newer logout cannot repopulate the SDK after that logout', async () => {
  const server = await installControlledServer({ holdPassword: true });
  const view = desktopApp();
  try {
    await waitFor(() => expect(app.loading).toBe(false));
    let login;
    await act(async () => { login = app.login('synthetic@example.invalid', 'synthetic password'); });
    await waitFor(() => expect(server.state.passwordCalls).toBe(1));
    let logout;
    let acknowledged = false;
    await act(async () => { logout = front.signOut().then((result) => { acknowledged = true; return result; }); });
    expect(acknowledged).toBe(false);
    await act(async () => { server.releasePassword.resolve(); await Promise.all([login, logout]); });
    expect(await login).toMatchObject({ success: false });
    expect(await logout).toMatchObject({ ok: true });
    expect(front.status).toBe('signedOut');
    expect(app.isLoggedIn).toBe(false);
    expect((await gotrueClient.getSession()).data.session).toBeNull();
  } finally { server.releasePassword.resolve(); view.unmount(); await gotrueClient.signOut({ scope: 'local' }); }
});

function FrontTap() { front = useFrontDoor(); return null; }
it('actual cloud gate reports unconfirmed server logout while keeping the sign-in form available', async () => {
  delete window.viola;
  const server = await installControlledServer();
  const view = render(<UiStateProvider><FrontDoor><FrontTap /><CloudAuthGate><AppStore><Probe /><AccountTab /></AppStore></CloudAuthGate></FrontDoor></UiStateProvider>);
  try {
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Welcome back' })).toBeInTheDocument());
    await act(async () => { expect((await front.signIn('synthetic@example.invalid', 'synthetic password')).ok).toBe(true); });
    await waitFor(() => expect(app.isLoggedIn).toBe(true));
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Sign Out' })); });
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Welcome back' })).toBeInTheDocument());
    expect(screen.getByText(/Server sign-out could not be confirmed/)).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Sign-out incomplete' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sign in', exact: true })).toBeEnabled();
    expect((await gotrueClient.getSession()).data.session).toBeNull();
    expect(server.state.globalLogouts).toBe(1);
  } finally { view.unmount(); await gotrueClient.signOut({ scope: 'local' }); }
});

it('actual desktop UI warns when storage cannot preserve the sign-out choice over restart', async () => {
  const server = await installControlledServer();
  const view = desktopApp();
  let storageSpy;
  try {
    await waitFor(() => expect(app.isLoggedIn).toBe(true));
    const prototype = Object.getPrototypeOf(localStorage);
    const original = prototype.setItem;
    storageSpy = vi.spyOn(prototype, 'setItem').mockImplementation(function (key, value) {
      if (key === 'viola-logout-intent-v1') throw new Error('Synthetic write denied');
      return original.call(this, key, value);
    });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Sign Out' })); });
    await waitFor(() => expect(front.status).toBe('signedOut'));
    await waitFor(() => expect(app.isLoggedIn).toBe(false));
    expect(screen.getByText(/Automatic sign-in may return after a restart/)).toBeInTheDocument();
    expect((await gotrueClient.getSession()).data.session).toBeNull();
    expect(server.state.globalLogouts).toBe(1);
  } finally {
    storageSpy?.mockRestore();
    view.unmount();
    await gotrueClient.signOut({ scope: 'local' });
    // Reset only this test's nonsecret module fallback after restoring storage.
    completeInteractiveSignIn(beginInteractiveSignIn());
  }
});

it('a newer same-pair SDK login survives retirement of a slower earlier password grant', async () => {
  const server = await installControlledServer({ holdPassword: true, samePair: true });
  const view = desktopApp();
  try {
    await waitFor(() => expect(app.loading).toBe(false));
    let old;
    let latest;
    await act(async () => { old = app.login('synthetic@example.invalid', 'synthetic password'); });
    await waitFor(() => expect(server.state.passwordCalls).toBe(1));
    await act(async () => { latest = app.login('synthetic@example.invalid', 'synthetic password'); });
    expect(server.state.passwordCalls).toBe(1);
    await act(async () => { server.releasePassword.resolve(); await Promise.all([old, latest]); });
    expect(await old).toMatchObject({ success: false });
    expect(await latest).toMatchObject({ success: true });
    expect(app.user.id).toBe(server.next.user.id);
    expect((await gotrueClient.getSession()).data.session.access_token).toBe(server.next.access_token);
    expect(server.state.localLogouts).toBe(1);
    expect(server.state.passwordCalls).toBe(2);
    expect(JSON.parse(localStorage.getItem('viola-logout-intent-v1')).blocked).toBe(false);
  } finally { server.releasePassword.resolve(); view.unmount(); await gotrueClient.signOut({ scope: 'local' }); }
});

it.each([[204, true], [401, false]])('only a successful server response confirms revocation (%s)', async (status, confirmed) => {
  vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({}, status)));
  const result = await clientSignOut('synthetic-session');
  expect(result.ok).toBe(true);
  expect(result.remoteRevocationConfirmed).toBe(confirmed);
});

it.each([false, true])('logout does not call an admitted SDK write clean when a subscriber fails after storage (%s)', async (subscriberThrows) => {
  const server = await installControlledServer({ holdPassword: true });
  const view = desktopApp();
  let subscription;
  let login;
  let logout;
  try {
    await waitFor(() => expect(app.loading).toBe(false));
    subscription = gotrueClient.onAuthStateChange((event, session) => {
      if (subscriberThrows && event === 'SIGNED_IN' && session?.user?.id === server.next.user.id) {
        throw new Error('Synthetic admitted write subscriber refusal');
      }
    }).data.subscription;
    await act(async () => { login = app.login('synthetic@example.invalid', 'synthetic password'); });
    await waitFor(() => expect(server.state.passwordCalls).toBe(1));
    let acknowledged = false;
    await act(async () => { logout = front.signOut().then((result) => { acknowledged = true; return result; }); });
    expect(acknowledged).toBe(false);
    await act(async () => { server.releasePassword.resolve(); await Promise.all([login, logout]); });
    const observed = {
      loginSucceeded: (await login).success,
      logoutSucceeded: (await logout).ok,
      frontStatus: front.status,
      appSignedIn: app.isLoggedIn,
      sdkSessionPresent: Boolean((await gotrueClient.getSession()).data.session),
    };
    expect(observed).toEqual({ loginSucceeded: false, logoutSucceeded: !subscriberThrows, frontStatus: 'signedOut', appSignedIn: false, sdkSessionPresent: subscriberThrows });
    if (subscriberThrows) {
      expect((await logout).error.code).toBe('local_session_uncertain');
      expect((await logout).error.message).toContain('Restart Viola');
      expect(front.signOutFeedback.error.code).toBe('local_session_uncertain');
    }
  } finally {
    subscription?.unsubscribe();
    server.releasePassword.resolve();
    await Promise.allSettled([login, logout].filter(Boolean));
    view.unmount();
    await gotrueClient.signOut({ scope: 'local' });
  }
});

it('a failed password request preserves a different SDK owner that wrote while it was pending', async () => {
  const server = await installControlledServer({ holdPassword: true });
  const view = desktopApp();
  try {
    await waitFor(() => expect(app.isLoggedIn).toBe(true));
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Sign Out' })); });
    await waitFor(() => expect(app.isLoggedIn).toBe(false));
    expect((await gotrueClient.getSession()).data.session).toBeNull();
    server.state.failPassword = true;
    let login;
    await act(async () => { login = app.login('synthetic@example.invalid', 'synthetic password'); });
    await waitFor(() => expect(server.state.passwordCalls).toBe(1));
    await act(async () => {
      await gotrueClient.setSession({ access_token: server.next.access_token, refresh_token: server.next.refresh_token });
    });
    await act(async () => { server.releasePassword.resolve(); await login; });
    expect(await login).toMatchObject({ success: false });
    expect((await gotrueClient.getSession()).data.session?.access_token).toBe(server.next.access_token);
  } finally { server.releasePassword.resolve(); view.unmount(); await gotrueClient.signOut({ scope: 'local' }); }
});

it('a failed admitted password request with no SDK write still permits clean local logout', async () => {
  const server = await installControlledServer({ holdPassword: true });
  const view = desktopApp();
  let login;
  let logout;
  try {
    await waitFor(() => expect(app.loading).toBe(false));
    await act(async () => { await gotrueClient.signOut({ scope: 'local' }); });
    expect((await gotrueClient.getSession()).data.session).toBeNull();
    server.state.failPassword = true;
    await act(async () => { login = app.login('synthetic@example.invalid', 'synthetic password'); });
    await waitFor(() => expect(server.state.passwordCalls).toBe(1));
    await act(async () => { logout = front.signOut(); });
    await act(async () => { server.releasePassword.resolve(); await Promise.all([login, logout]); });
    expect(await login).toMatchObject({ success: false });
    expect(await logout).toMatchObject({ ok: true });
    expect((await gotrueClient.getSession()).data.session).toBeNull();
  } finally { server.releasePassword.resolve(); view.unmount(); await gotrueClient.signOut({ scope: 'local' }); }
});

it('retirement does not erase a different SDK pair written after its own write', async () => {
  const server = await installControlledServer({ holdPassword: true });
  const view = desktopApp();
  const subscriberDrain = deferred();
  let subscription;
  let login;
  let logout;
  let enteredSubscriber = false;
  try {
    await waitFor(() => expect(app.loading).toBe(false));
    subscription = gotrueClient.onAuthStateChange(async (event, session) => {
      if (event === 'SIGNED_IN' && session?.access_token === server.next.access_token) {
        enteredSubscriber = true;
        await subscriberDrain.promise;
      }
    }).data.subscription;
    await act(async () => { login = app.login('synthetic@example.invalid', 'synthetic password'); });
    await waitFor(() => expect(server.state.passwordCalls).toBe(1));
    await act(async () => { logout = front.signOut(); });
    await act(async () => { server.releasePassword.resolve(); });
    await waitFor(() => expect(enteredSubscriber).toBe(true));
    await act(async () => { await gotrueClient.setSession({ access_token: server.old.access_token, refresh_token: server.old.refresh_token }); });
    expect((await gotrueClient.getSession()).data.session?.access_token).toBe(server.old.access_token);
    await act(async () => { subscriberDrain.resolve(); await Promise.all([login, logout]); });
    expect((await gotrueClient.getSession()).data.session?.access_token).toBe(server.old.access_token);
  } finally {
    subscriberDrain.resolve();
    server.releasePassword.resolve();
    await Promise.allSettled([login, logout].filter(Boolean));
    subscription?.unsubscribe();
    view.unmount();
    await gotrueClient.signOut({ scope: 'local' });
  }
});

it('email-confirmation registration cannot erase uncertainty about a retained SDK pair', async () => {
  const server = await installControlledServer();
  const initialFetch = globalThis.fetch;
  vi.stubGlobal('fetch', vi.fn(async (input, init) => {
    const url = new URL(typeof input === 'string' ? input : input.url, 'http://localhost');
    if (url.pathname.endsWith('/signup')) return jsonResponse({ user: server.next.user, session: null });
    return initialFetch(input, init);
  }));
  const view = desktopApp();
  let subscription;
  try {
    await waitFor(() => expect(app.isLoggedIn).toBe(true));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Sign Out' })));
    await waitFor(() => expect(app.isLoggedIn).toBe(false));
    subscription = gotrueClient.onAuthStateChange((event) => {
      if (event === 'SIGNED_IN') throw new Error('Synthetic registration-prerequisite subscriber refusal');
    }).data.subscription;
    let login;
    await act(async () => { login = await app.login('synthetic@example.invalid', 'synthetic password'); });
    expect(login.success).toBe(false);
    subscription.unsubscribe(); subscription = null;
    expect((await gotrueClient.getSession()).data.session).not.toBeNull();
    let firstLogout;
    await act(async () => { firstLogout = await front.signOut(); });
    expect(firstLogout).toMatchObject({ ok: false, error: { code: 'local_session_uncertain' } });
    let registration;
    await act(async () => { registration = await app.register('synthetic-other@example.invalid', 'synthetic password', { tosAccepted: true, legalEligibilityConfirmed: true }); });
    expect(registration.success).toBe(true);
    expect((await gotrueClient.getSession()).data.session).not.toBeNull();
    let finalLogout;
    await act(async () => { finalLogout = await front.signOut(); });
    expect(finalLogout).toMatchObject({ ok: false, error: { code: 'local_session_uncertain' } });
    let acceptedLogin;
    await act(async () => { acceptedLogin = await app.login('synthetic@example.invalid', 'synthetic password'); });
    expect(acceptedLogin).toMatchObject({ success: true });
    await expect(waitForSessionRestoration()).resolves.toMatchObject({ success: true });
    expect((await gotrueClient.getSession()).data.session).not.toBeNull();
  } finally {
    subscription?.unsubscribe();
    view.unmount();
    await gotrueClient.signOut({ scope: 'local' });
  }
});

it('a retained cleanup retry cannot report clean logout after a different pair replaces its failed write', async () => {
  const server = await installControlledServer({ holdPassword: true, cleanupFails: true });
  const view = desktopApp();
  let login;
  let logout;
  try {
    await waitFor(() => expect(app.loading).toBe(false));
    await act(async () => { login = app.login('synthetic@example.invalid', 'synthetic password'); });
    await waitFor(() => expect(server.state.passwordCalls).toBe(1));
    await act(async () => { logout = front.signOut(); });
    await act(async () => { server.releasePassword.resolve(); await Promise.all([login, logout]); });
    expect(await logout).toMatchObject({ ok: false });
    expect((await gotrueClient.getSession()).data.session?.access_token).toBe(server.next.access_token);
    server.state.cleanupFails = false;
    await act(async () => { await gotrueClient.setSession({ access_token: server.old.access_token, refresh_token: server.old.refresh_token }); });
    let retry;
    await act(async () => { retry = await front.signOut(); });
    expect(retry).toMatchObject({ ok: false, error: { code: 'local_session_uncertain' } });
    expect((await gotrueClient.getSession()).data.session?.access_token).toBe(server.old.access_token);
  } finally { server.releasePassword.resolve(); server.state.cleanupFails = false; await Promise.allSettled([login, logout].filter(Boolean)); view.unmount(); await gotrueClient.signOut({ scope: 'local' }); }
});

it('automatic hydration retirement preserves a later unqueued SDK password writer', async () => {
  const server = await installControlledServer();
  const subscriberDrain = deferred();
  let entered = false;
  const subscription = gotrueClient.onAuthStateChange(async (event, session) => {
    if (event === 'SIGNED_IN' && session?.access_token === server.old.access_token) {
      entered = true;
      await subscriberDrain.promise;
    }
  }).data.subscription;
  const view = desktopApp();
  let logout;
  try {
    await waitFor(() => expect(entered).toBe(true));
    await act(async () => { logout = front.signOut(); });
    await act(async () => { await gotrueClient.signInWithPassword({ email: 'synthetic@example.invalid', password: 'synthetic password' }); });
    await act(async () => { subscriberDrain.resolve(); await logout; });
    expect((await gotrueClient.getSession()).data.session?.access_token).toBe(server.next.access_token);
    expect(await logout).toMatchObject({ ok: false, error: { code: 'local_session_uncertain' } });
  } finally { subscriberDrain.resolve(); await Promise.allSettled([logout].filter(Boolean)); subscription.unsubscribe(); view.unmount(); await gotrueClient.signOut({ scope: 'local' }); }
});

it('automatic hydration returned SDK error retains uncertainty after a post-storage auth subscriber failure', async () => {
  const server = await installControlledServer();
  const subscription = gotrueClient.onAuthStateChange((event, session) => {
    if (event === 'SIGNED_IN' && session?.access_token === server.old.access_token) {
      throw new AuthApiError('Synthetic post-storage auth error', 503);
    }
  }).data.subscription;
  const view = desktopApp();
  try {
    await waitFor(() => expect(app.loading).toBe(false));
    subscription.unsubscribe();
    expect((await gotrueClient.getSession()).data.session?.access_token).toBe(server.old.access_token);
    let logout;
    await act(async () => { logout = await front.signOut(); });
    expect(logout).toMatchObject({ ok: false, error: { code: 'local_session_uncertain' } });
    expect((await gotrueClient.getSession()).data.session?.access_token).toBe(server.old.access_token);
  } finally { subscription.unsubscribe(); view.unmount(); await gotrueClient.signOut({ scope: 'local' }); }
});

it('hydration notification failure cannot become clean logout after sessionless registration', async () => {
  const server = await installControlledServer({holdUser: true});
  const originalFetch = globalThis.fetch;
  vi.stubGlobal('fetch', vi.fn(async (input, init) => {
    const url = new URL(typeof input === 'string' ? input : input.url, 'http://localhost');
    if (url.pathname.endsWith('/signup')) return jsonResponse({user: server.next.user, session: null});
    return originalFetch(input, init);
  }));
  let notificationThrew = false;
  let subscription = gotrueClient.onAuthStateChange((event, session) => {
    if (event === 'SIGNED_IN' && session?.access_token === server.old.access_token) {
      notificationThrew = true;
      throw new Error('Synthetic hydration post-storage notification failure');
    }
  }).data.subscription;
  const view = desktopApp();
  let firstLogout;
  try {
    await waitFor(() => expect(server.state.userCalls).toBe(1));
    await act(async () => {firstLogout = front.signOut();});
    await act(async () => {server.state.holdUser = false; server.releaseUser.resolve(); await firstLogout;});
    expect(notificationThrew).toBe(true);
    expect((await firstLogout).ok).toBe(false);
    subscription.unsubscribe(); subscription = null;
    expect((await gotrueClient.getSession()).data.session?.access_token).toBe(server.old.access_token);
    let registration;
    await act(async () => {registration = await app.register('synthetic-confirm@example.invalid', 'synthetic password', {tosAccepted: true, legalEligibilityConfirmed: true});});
    expect(registration.success).toBe(true);
    expect((await gotrueClient.getSession()).data.session?.access_token).toBe(server.old.access_token);
    let finalLogout;
    await act(async () => {finalLogout = await front.signOut();});
    expect({logoutSucceeded: finalLogout.ok, sdkSessionPresent: Boolean((await gotrueClient.getSession()).data.session)}).toEqual({logoutSucceeded: false, sdkSessionPresent: true});
    expect(finalLogout.error?.code).toBe('local_session_uncertain');
  } finally {
    subscription?.unsubscribe(); server.releaseUser.resolve(); await Promise.allSettled([firstLogout].filter(Boolean)); view.unmount(); await gotrueClient.signOut({scope: 'local'});
  }
});

it.each(['returned', 'thrown'])('automatic hydration failure with no SDK write remains a clean miss (%s)', async (failure) => {
  const server = await installControlledServer();
  const originalFetch = globalThis.fetch;
  vi.stubGlobal('fetch', vi.fn(async (input, init) => {
    const url = new URL(typeof input === 'string' ? input : input.url, 'http://localhost');
    if (url.pathname.endsWith('/user')) {
      if (failure === 'thrown') throw new Error('Synthetic pre-storage user lookup failure');
      return jsonResponse({ message: 'Synthetic pre-storage user lookup refusal' }, 503);
    }
    return originalFetch(input, init);
  }));
  const view = desktopApp();
  try {
    await waitFor(() => expect(app.loading).toBe(false));
    expect((await gotrueClient.getSession()).data.session).toBeNull();
    let logout;
    await act(async () => { logout = await front.signOut(); });
    expect(logout).toMatchObject({ ok: true });
    expect((await gotrueClient.getSession()).data.session).toBeNull();
    expect(server.state.globalLogouts).toBe(1);
  } finally { view.unmount(); await gotrueClient.signOut({ scope: 'local' }); }
});

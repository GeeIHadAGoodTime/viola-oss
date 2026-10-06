import { beforeEach, afterEach, it, expect, vi } from 'vitest';
let intent;
beforeEach(async () => {
  localStorage.clear();
  vi.resetModules();
  intent = await import('./logoutIntent');
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it('keeps an ordinary fresh origin eligible for automatic restoration', () => {
  expect(intent.mayRestoreSession(intent.captureSessionRestoration())).toBe(true);
  expect(localStorage.length).toBe(0);
});
it('persists only a version, ordering nonces, action kind and two booleans', () => {
  intent.beginLogoutIntent();
  const data = JSON.parse(localStorage.getItem(intent.LOGOUT_INTENT_KEY));
  expect(Object.keys(data).sort()).toEqual(['blocked', 'kind', 'logoutRevision', 'revision', 'uncertain', 'version']);
  expect(data).toMatchObject({ version: 1, kind: 'logout', blocked: true, uncertain: true });
  expect(typeof data.revision).toBe('string');
});
it('keeps logout through a fresh module/document lifetime without storing credentials', async () => {
  intent.beginLogoutIntent();
  vi.resetModules();
  const fresh = await import('./logoutIntent');
  expect(fresh.mayRestoreSession(fresh.captureSessionRestoration())).toBe(false);
  expect(fresh.logoutIntentWarning()).toContain('could not be confirmed');
});
it('rejects a cookie response captured before logout', () => {
  const read = intent.captureSessionRestoration();
  intent.beginLogoutIntent();
  expect(intent.mayRestoreSession(read)).toBe(false);
});
it('rejects an old cookie response even after a newer successful login', () => {
  const read = intent.captureSessionRestoration();
  intent.beginLogoutIntent();
  const login = intent.beginInteractiveSignIn();
  expect(intent.completeInteractiveSignIn(login)).toBe(true);
  expect(intent.mayRestoreSession(read)).toBe(false);
  expect(intent.mayRestoreSession(intent.captureSessionRestoration())).toBe(true);
});
it('does not retire the barrier for a login that has not succeeded', () => {
  intent.beginLogoutIntent();
  intent.beginInteractiveSignIn();
  expect(intent.mayRestoreSession(intent.captureSessionRestoration())).toBe(false);
});
it('a newer logout invalidates a pending interactive login', () => {
  const login = intent.beginInteractiveSignIn();
  intent.beginLogoutIntent();
  expect(intent.completeInteractiveSignIn(login)).toBe(false);
});
it('newest interactive login wins independently of account/token identity', () => {
  intent.beginLogoutIntent();
  const old = intent.beginInteractiveSignIn();
  const current = intent.beginInteractiveSignIn();
  expect(intent.completeInteractiveSignIn(old)).toBe(false);
  expect(intent.completeInteractiveSignIn(current)).toBe(true);
  expect(intent.completeInteractiveSignIn(old)).toBe(false);
});
it('late remote acknowledgement cannot retire a newer login or logout', () => {
  const old = intent.beginLogoutIntent();
  const login = intent.beginInteractiveSignIn();
  intent.completeInteractiveSignIn(login);
  const current = intent.beginLogoutIntent();
  intent.confirmRemoteLogout(old, true);
  expect(intent.readLogoutIntent()).toMatchObject({ revision: current.revision, blocked: true, uncertain: true });
});
it('confirmed server logout removes uncertainty but keeps automatic restoration blocked', () => {
  const ticket = intent.beginLogoutIntent();
  intent.confirmRemoteLogout(ticket, true);
  expect(intent.logoutIntentWarning()).toBeNull();
  expect(intent.mayRestoreSession(intent.captureSessionRestoration())).toBe(false);
});
it.each(['bad json', JSON.stringify({ version: 2 }), JSON.stringify({ version: 1, revision: 'x', kind: 'login', blocked: 'false', uncertain: false })])('fails closed on malformed persisted intent (%s)', (raw) => {
  localStorage.setItem(intent.LOGOUT_INTENT_KEY, raw);
  expect(intent.mayRestoreSession(intent.captureSessionRestoration())).toBe(false);
  expect(intent.logoutIntentWarning()).toContain('could not save');
});
it('read denial blocks automatic restoration but a successful explicit login remains usable', () => {
  vi.spyOn(Object.getPrototypeOf(localStorage), 'getItem').mockImplementation(() => { throw new Error('synthetic denial'); });
  expect(intent.mayRestoreSession(intent.captureSessionRestoration())).toBe(false);
  const login = intent.beginInteractiveSignIn();
  expect(intent.completeInteractiveSignIn(login)).toBe(true);
  expect(intent.readLogoutIntent()).toMatchObject({ blocked: false, persistent: false });
});
it.each(['throw', 'discard'])('write failure keeps the current document signed out and warns that restart persistence is unavailable (%s)', (kind) => {
  vi.spyOn(Object.getPrototypeOf(localStorage), 'setItem').mockImplementation(() => { if (kind === 'throw') throw new Error('synthetic quota'); });
  const result = intent.beginLogoutIntent();
  expect(result.persistent).toBe(false);
  expect(intent.mayRestoreSession(intent.captureSessionRestoration())).toBe(false);
  expect(intent.logoutIntentWarning()).toContain('may return after a restart');
});
it('recognizes another document’s newer logout record', () => {
  const current = intent.captureSessionRestoration();
  localStorage.setItem(intent.LOGOUT_INTENT_KEY, JSON.stringify({ version: 1, revision: 'other-document', logoutRevision: 'other-document', kind: 'logout', blocked: true, uncertain: true }));
  expect(intent.mayRestoreSession(current)).toBe(false);
});
it('drains an admitted automatic SDK write before explicit account actions proceed', async () => {
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  const events = [];
  const restoration = intent.runSessionRestoration(async () => { events.push('write'); await held; events.push('cleanup'); });
  const next = intent.waitForSessionRestoration().then(() => events.push('explicit'));
  await Promise.resolve();
  expect(events).toEqual(['write']);
  release();
  await Promise.all([restoration, next]);
  expect(events).toEqual(['write', 'cleanup', 'explicit']);
});


it('pending or failed login does not cancel logout until a session is actually accepted', () => {
  const logout = intent.beginLogoutIntent();
  const login = intent.beginInteractiveSignIn();
  expect(intent.ownsLogoutIntent(logout)).toBe(true);
  intent.confirmRemoteLogout(logout, true);
  expect(intent.readLogoutIntent()).toMatchObject({ blocked: true, uncertain: false });
  expect(intent.completeInteractiveSignIn(login)).toBe(true);
  expect(intent.ownsLogoutIntent(logout)).toBe(false);
});

it('the actual cookie client sends no automatic request after a retained logout', async () => {
  intent.beginLogoutIntent();
  const { hydrateDesktopSessionFromCookie } = await import('./authClient');
  const fetchMock = vi.fn(async () => ({ ok: false, status: 400, headers: { get: () => null }, text: async () => '{}' }));
  vi.stubGlobal('fetch', fetchMock);
  expect(await hydrateDesktopSessionFromCookie()).toMatchObject({ ok: false, session: null });
  expect(fetchMock).not.toHaveBeenCalled();
});
it('the actual cookie client discards a response older than logout and a newer accepted login', async () => {
  const { hydrateDesktopSessionFromCookie } = await import('./authClient');
  let release;
  vi.stubGlobal('fetch', vi.fn(() => new Promise((resolve) => { release = resolve; })));
  const pending = hydrateDesktopSessionFromCookie();
  intent.beginLogoutIntent();
  intent.completeInteractiveSignIn(intent.beginInteractiveSignIn());
  release({ ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify({ access_token: 'synthetic-stale-access', refresh_token: 'synthetic-stale-refresh', expires_in: 3600, user: { id: 'synthetic-old' } }) });
  expect(await pending).toMatchObject({ ok: false, session: null });
});

it('a failed admitted SDK action cannot be called clean while a session remains', async () => {
  let present = true;
  const ticket = intent.beginInteractiveSignIn();
  await intent.runOwnedSessionAction(async (admit) => { admit(); return { success: false }; }, ticket, () => present);
  await expect(intent.waitForSessionRestoration()).rejects.toMatchObject({ code: 'local_session_uncertain' });
  // Presence is a failure witness, never permission to delete a different owner.
  expect(present).toBe(true);
  present = false;
  await expect(intent.waitForSessionRestoration()).resolves.toMatchObject({ success: false });
});
it('an action rejected before SDK admission does not invent a storage write', async () => {
  await intent.runOwnedSessionAction(async () => ({ success: false }), intent.beginInteractiveSignIn(), () => true);
  await expect(intent.waitForSessionRestoration()).resolves.toMatchObject({ success: false });
});
it('ordinary MFA step-up remains usable and does not poison later successful actions', async () => {
  const ticket = intent.beginInteractiveSignIn();
  await intent.runOwnedSessionAction(async (admit) => { admit(); return { success: false, mfaRequired: true }; }, ticket, () => true);
  await expect(intent.waitForSessionRestoration()).resolves.toMatchObject({ mfaRequired: true });
  await intent.runOwnedSessionAction(async (admit) => { admit(); return { success: true }; }, ticket, () => true);
  await expect(intent.waitForSessionRestoration()).resolves.toMatchObject({ success: true });
});

it('successful non-session work cannot erase uncertainty while SDK state remains', async () => {
  const present = () => true;
  await intent.runOwnedSessionAction(async (admit) => { admit(); return { success: false }; }, intent.beginInteractiveSignIn(), present);
  await expect(intent.waitForSessionRestoration()).rejects.toMatchObject({ code: 'local_session_uncertain' });
  await intent.runOwnedSessionAction(async (admit) => { admit(); return { success: true, message: 'Email verification required' }; }, intent.beginInteractiveSignIn(), present);
  await expect(intent.waitForSessionRestoration()).rejects.toMatchObject({ code: 'local_session_uncertain' });
  await intent.runOwnedSessionAction(async (admit, acceptSession) => { admit(); acceptSession(); return { success: true }; }, intent.beginInteractiveSignIn(), present);
  await expect(intent.waitForSessionRestoration()).resolves.toMatchObject({ success: true });
});

it('ordering metadata works when randomUUID is unavailable but getRandomValues exists', () => {
  let serial = 0;
  const getRandomValues = vi.fn((bytes) => { bytes.fill(++serial); return bytes; });
  vi.stubGlobal('crypto', { getRandomValues });
  let logout;
  let login;
  expect(() => { logout = intent.beginLogoutIntent(); login = intent.beginInteractiveSignIn(); }).not.toThrow();
  expect(getRandomValues).toHaveBeenCalledTimes(2);
  expect(login.revision).not.toBe(logout.revision);
  expect(intent.completeInteractiveSignIn(login)).toBe(true);
});
it.each([undefined, { randomUUID() { throw new Error('Synthetic unavailable nonce API'); } }])('nonce-only fallback does not make local logout unavailable (%s)', (cryptoValue) => {
  vi.stubGlobal('crypto', cryptoValue);
  vi.spyOn(Date, 'now').mockReturnValue(123456789);
  vi.spyOn(Math, 'random').mockReturnValue(0);
  let logout;
  let first;
  let second;
  expect(() => { logout = intent.beginLogoutIntent(); first = intent.beginInteractiveSignIn(); second = intent.beginInteractiveSignIn(); }).not.toThrow();
  expect(new Set([logout.revision, first.revision, second.revision]).size).toBe(3);
  expect(intent.completeInteractiveSignIn(first)).toBe(false);
  expect(intent.completeInteractiveSignIn(second)).toBe(true);
});

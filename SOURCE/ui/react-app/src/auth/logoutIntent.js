/**
 * Nonsecret, same-origin logout intent. No account identifiers or credentials
 * belong here. A failed network logout cannot delete an HttpOnly cookie, so
 * automatic restoration must honor local intent until a newer explicit login.
 * This is a restoration barrier, never evidence of server-side revocation.
 */
export const LOGOUT_INTENT_KEY = 'viola-logout-intent-v1';
let fallback = null;
let restorationDrain = Promise.resolve();
let restorationOutcome = restorationDrain;
let activeRestoration = null;
let pendingRestorationCleanup = null;
let uncertainSdkSession = null;
let fallbackRevision = 0;

// Automatic restoration and explicit SDK writes share document ownership.
// A logout cannot acknowledge while an earlier admitted write can repopulate
// the singleton, and a newer login waits for retired work's cleanup.
export function runSessionRestoration(task, captured) {
  const run = async () => {
    activeRestoration = captured || null;
    try { return await task(); } finally { activeRestoration = null; }
  };
  const result = restorationDrain.then(run, run);
  restorationOutcome = result;
  restorationDrain = result.catch(() => undefined);
  return result;
}

export function waitForSessionRestoration() {
  let settled = restorationOutcome;
  if (pendingRestorationCleanup && !activeRestoration) {
    const cleanup = pendingRestorationCleanup;
    settled = runSessionRestoration(async () => {
      await cleanup();
      if (pendingRestorationCleanup === cleanup) pendingRestorationCleanup = null;
    });
  }
  return settled.then((result) => {
    if (pendingRestorationCleanup) throw new Error('Previous session cleanup is incomplete');
    if (uncertainSdkSession?.()) {
      const error = new Error('A previous sign-in may still have a local session. Restart Viola or reload this page before trying again.');
      error.code = 'local_session_uncertain';
      throw error;
    }
    uncertainSdkSession = null;
    return result;
  });
}

export function runOwnedSessionAction(task, ticket, sessionPresent) {
  return runSessionRestoration(async () => {
    const cleanup = pendingRestorationCleanup;
    if (cleanup) {
      try { await cleanup(); } catch {
        return { success: false, error: 'Previous session cleanup could not be completed. Please retry.' };
      }
      if (pendingRestorationCleanup === cleanup) pendingRestorationCleanup = null;
    }
    let admitted = false;
    let acceptedSession = false;
    let outcome;
    try {
      outcome = await task(() => { admitted = true; }, () => { acceptedSession = true; });
      return outcome;
    } finally {
      // Failure after an admitted SDK call can follow a storage write. A
      // passive presence check proves uncertainty, never ownership to delete.
      if (admitted && outcome?.success !== true && !outcome?.mfaRequired && sessionPresent?.()) {
        uncertainSdkSession = sessionPresent;
      } else if (outcome?.success === true && (acceptedSession || !sessionPresent?.())) {
        uncertainSdkSession = null;
      }
    }
  }, { revision: ticket.revision, blocked: false });
}

export function retainSessionUncertainty(sessionPresent) {
  uncertainSdkSession = sessionPresent;
}

export function retainRestorationCleanup(cleanup, sessionPresent) {
  pendingRestorationCleanup = cleanup;
  if (sessionPresent) retainSessionUncertainty(sessionPresent);
}

export function mayApplySdkSession() {
  return !readLogoutIntent().blocked && (!activeRestoration || mayRestoreSession(activeRestoration));
}

function unavailable() {
  return { revision: 'unavailable', logoutRevision: null, kind: 'logout', blocked: true, uncertain: true, persistent: false };
}

export function readLogoutIntent() {
  let raw;
  try {
    raw = localStorage.getItem(LOGOUT_INTENT_KEY);
  } catch {
    return fallback?.value || unavailable();
  }
  if (fallback && raw === fallback.raw) return fallback.value;
  fallback = null;
  if (raw === null) return { revision: null, logoutRevision: null, kind: null, blocked: false, uncertain: false, persistent: true };
  try {
    const value = JSON.parse(raw);
    if (value.version !== 1 || typeof value.revision !== 'string'
      || !(value.logoutRevision === null || typeof value.logoutRevision === 'string')
      || !['logout', 'login'].includes(value.kind) || typeof value.blocked !== 'boolean'
      || typeof value.uncertain !== 'boolean') return unavailable();
    return { revision: value.revision, logoutRevision: value.logoutRevision, kind: value.kind, blocked: value.blocked, uncertain: value.uncertain, persistent: true };
  } catch {
    return unavailable();
  }
}

function writeIntent(value) {
  const record = { version: 1, revision: value.revision, logoutRevision: value.logoutRevision, kind: value.kind, blocked: value.blocked, uncertain: value.uncertain };
  const encoded = JSON.stringify(record);
  let previous = null;
  try { previous = localStorage.getItem(LOGOUT_INTENT_KEY); } catch { /* memory-only fallback */ }
  try {
    localStorage.setItem(LOGOUT_INTENT_KEY, encoded);
    if (localStorage.getItem(LOGOUT_INTENT_KEY) !== encoded) throw new Error('Intent was not retained');
    fallback = null;
    return { ...record, persistent: true };
  } catch {
    const result = { ...record, persistent: false };
    fallback = { raw: previous, value: result };
    return result;
  }
}

function revision() {
  // This nonce conveys ordering only; it is neither a credential nor identity.
  const cryptoRef = globalThis.crypto;
  try {
    if (typeof cryptoRef?.randomUUID === 'function') return cryptoRef.randomUUID();
    if (typeof cryptoRef?.getRandomValues === 'function') {
      const bytes = new Uint8Array(16);
      cryptoRef.getRandomValues(bytes);
      return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
    }
  } catch { /* Ordering metadata must not make local logout unavailable. */ }
  // Same feature fallback as existing request identifiers. This is never an
  // authentication secret; a counter also preserves same-document ordering.
  return `${Date.now().toString(16)}-${++fallbackRevision}-${Math.random().toString(16).slice(2)}`;
}

export function beginLogoutIntent() {
  const current = readLogoutIntent();
  if (current.kind === 'logout' && current.blocked && current.revision !== 'unavailable') return current;
  const next = revision();
  return writeIntent({ revision: next, logoutRevision: next, kind: 'logout', blocked: true, uncertain: true });
}

export function captureSessionRestoration() {
  return readLogoutIntent();
}

export function mayRestoreSession(captured) {
  const current = readLogoutIntent();
  return !captured.blocked && !current.blocked && captured.revision === current.revision;
}

export function beginInteractiveSignIn() {
  const current = readLogoutIntent();
  return writeIntent({ ...current, revision: revision(), kind: 'login' });
}

export function completeInteractiveSignIn(ticket) {
  const current = readLogoutIntent();
  if (!ownsInteractiveSignIn(ticket)) return false;
  writeIntent({ ...current, blocked: false, uncertain: false });
  return true;
}

export function ownsInteractiveSignIn(ticket) {
  const current = readLogoutIntent();
  return current.revision === ticket.revision && current.kind === 'login';
}

export function ownsLogoutIntent(ticket) {
  const current = readLogoutIntent();
  return current.blocked && current.logoutRevision === ticket.logoutRevision;
}

export function confirmRemoteLogout(ticket, confirmed) {
  const current = readLogoutIntent();
  if (!ownsLogoutIntent(ticket)) return;
  writeIntent({ ...current, uncertain: !confirmed });
}

export function logoutIntentWarning() {
  const current = readLogoutIntent();
  if (!current.blocked) return null;
  const messages = [];
  if (current.uncertain) messages.push('You are signed out locally. Server sign-out could not be confirmed.');
  if (!current.persistent) messages.push('This browser could not save your sign-out choice. Automatic sign-in may return after a restart.');
  return messages.join(' ') || null;
}

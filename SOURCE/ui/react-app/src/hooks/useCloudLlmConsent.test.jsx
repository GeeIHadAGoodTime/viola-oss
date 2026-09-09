/**
 * Ratchet for #362: the cloud browser must request EVERY consent the server's
 * agent gate requires, and must never report a partially-consented user as
 * consented.
 *
 * Bug shape this locks out (live prod, deployed SHA 0d13cdf2a, 2026-08-04):
 * `services/cloud_consent.py::can_execute_cloud_agent` requires `cloud_llm` AND
 * `data_retention`, but this hook requested `cloud_llm` alone and returned true
 * unconditionally, so a brand-new user who accepted the prompt was STILL refused
 *
 *   POST /api/v1/command -> cloud_consent_required, missing [data_retention]
 *
 * and never got prompted again, because the hook now claimed they were granted.
 * Every test below fails against that pre-fix shape.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { useCloudLlmConsent, REQUIRED_AGENT_CONSENTS, CLOUD_SYNC_SETTING_PATH } from './useCloudLlmConsent';
import { apiFetch } from './useViolaApi';

vi.mock('./useViolaApi', () => ({
  apiFetch: vi.fn(),
}));

const grantedRecord = { record: { granted: true } };
// The shape live prod actually hands the caller: apiFetch auto-unwraps the
// ResponseEnvelope, so the settings row arrives bare. Verified against
// api.useviola.com 2026-08-05:
//   GET /api/v1/cloud/settings/consent_cloud_sync
//     -> {"ok":true,"data":{"key":"consent_cloud_sync","value":true}}
const syncOn = { key: 'consent_cloud_sync', value: true };
const syncOff = { key: 'consent_cloud_sync', value: false };

// Cloud sync lives at a settings endpoint, not a consent record, so a test that
// wants "everything the agent needs is held" has to answer both shapes.
const respond = ({ consents = true, sync = true } = {}) => async (path) => (
  path === CLOUD_SYNC_SETTING_PATH
    ? (sync ? syncOn : syncOff)
    : { granted: typeof consents === 'function' ? consents(path) : consents }
);

describe('useCloudLlmConsent', () => {
  beforeEach(() => {
    apiFetch.mockReset();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('requires exactly the consents the server gate requires', () => {
    // Mirrors services/cloud_consent.py::can_execute_cloud_agent. If the server
    // gate gains a consent and this list does not, new users silently break
    // again, so the list is asserted rather than merely used.
    expect(REQUIRED_AGENT_CONSENTS).toEqual(['cloud_llm', 'data_retention']);
  });

  it('reads EVERY required consent, not cloud_llm alone', async () => {
    apiFetch.mockImplementation(respond());

    const { result } = renderHook(() => useCloudLlmConsent({ enabled: true }));

    await waitFor(() => expect(result.current.granted).toBe(true));
    for (const consentType of REQUIRED_AGENT_CONSENTS) {
      expect(apiFetch).toHaveBeenCalledWith(`/api/v1/consent/${consentType}`);
    }
    expect(apiFetch).toHaveBeenCalledWith(CLOUD_SYNC_SETTING_PATH);
  });

  it('reports NOT granted when cloud sync is off, whatever the consent records say', async () => {
    // Measured live 2026-08-05 on a fresh marked account with cloud_llm AND
    // data_retention granted: POST /v1/chat/threads still 403s
    // consent_required, so the chat composer cannot send anything. Reporting
    // that user as granted is what let the app tell them Viola was on while
    // every message they typed died.
    apiFetch.mockImplementation(respond({ sync: false }));

    const { result } = renderHook(() => useCloudLlmConsent({ enabled: true }));

    await waitFor(() => expect(result.current.granted).toBe(false));
  });

  it('reports NOT granted when only some required consents are held', async () => {
    // The exact live shape: cloud_llm granted, data_retention not. The pre-fix
    // hook read only cloud_llm and called this user consented, which is what
    // produced a dead turn with no prompt.
    apiFetch.mockImplementation(respond({ consents: (path) => path.endsWith('/cloud_llm') }));

    const { result } = renderHook(() => useCloudLlmConsent({ enabled: true }));

    await waitFor(() => expect(result.current.granted).toBe(false));
  });

  it('grant() grants every required consent AND cloud sync', async () => {
    // Cloud sync is the one the chat composer dies without, and it was
    // grantable only from a toggle buried in Settings that nothing pointed a
    // new user to.
    let syncGranted = false;
    apiFetch.mockImplementation(async (path, options) => {
      if (path === CLOUD_SYNC_SETTING_PATH) {
        if (options && options.method === 'PUT') { syncGranted = true; return { ...syncOn }; }
        return { key: 'consent_cloud_sync', value: syncGranted };
      }
      return options && options.method === 'PUT' ? grantedRecord : { granted: false };
    });

    const { result } = renderHook(() => useCloudLlmConsent({ enabled: true }));
    await waitFor(() => expect(result.current.granted).toBe(false));

    let outcome;
    await act(async () => { outcome = await result.current.grant(); });

    expect(outcome).toBe(true);
    for (const consentType of REQUIRED_AGENT_CONSENTS) {
      expect(apiFetch).toHaveBeenCalledWith(
        `/api/v1/consent/${consentType}`,
        expect.objectContaining({ method: 'PUT' }),
      );
    }
    expect(apiFetch).toHaveBeenCalledWith(
      CLOUD_SYNC_SETTING_PATH,
      expect.objectContaining({ method: 'PUT' }),
    );
    expect(result.current.granted).toBe(true);
  });

  it('grant() reports failure when cloud sync does not stick', async () => {
    // A 200 on the write is not evidence the setting stuck, and a user the app
    // calls consented is a user it will never re-prompt.
    apiFetch.mockImplementation(async (path, options) => {
      if (path === CLOUD_SYNC_SETTING_PATH) return { ...syncOff };
      return options && options.method === 'PUT' ? grantedRecord : { granted: false };
    });

    const { result } = renderHook(() => useCloudLlmConsent({ enabled: true }));
    await waitFor(() => expect(result.current.granted).toBe(false));

    let outcome;
    await act(async () => { outcome = await result.current.grant(); });

    expect(outcome).toBe(false);
    expect(result.current.granted).toBe(false);
  });

  it('grant() reports failure and stays NOT granted when one consent fails', async () => {
    // A half-granted user is exactly as blocked as an unconsented one. The
    // pre-fix hook returned true regardless, so the caller resumed a turn the
    // server refused and never re-prompted.
    apiFetch.mockImplementation(async (path, options) => {
      if (!options || options.method !== 'PUT') {
        return path === CLOUD_SYNC_SETTING_PATH ? syncOff : { granted: false };
      }
      if (path.endsWith('/data_retention')) throw new Error('server refused');
      return grantedRecord;
    });

    const { result } = renderHook(() => useCloudLlmConsent({ enabled: true }));
    await waitFor(() => expect(result.current.granted).toBe(false));

    let outcome;
    await act(async () => { outcome = await result.current.grant(); });

    expect(outcome).toBe(false);
    expect(result.current.granted).toBe(false);
    expect(result.current.error).toBeTruthy();
  });

  it('readGranted() is true the instant grant() resolves, before any re-render', async () => {
    // #4785, reproduced live on prod ca19433c9 with a freshly signed-up marked
    // account: clicking "Turn on Viola" wrote all three consents (200/200/200,
    // all confirmed granted in prod Postgres) and the modal RE-OPENED, twice out
    // of two attempts, with no error shown. The accept handler resumes the
    // interrupted turn in the SAME tick the grant lands, so a gate reading the
    // React state snapshot still saw the pre-grant value, re-intercepted, and
    // re-opened the prompt the user had just accepted. The only escape was to
    // click the DECLINE button after the grant had already landed.
    //
    // This asserts the synchronous read directly, WITHOUT act() flushing a
    // re-render first, which is precisely the window the loop lived in.
    let syncGranted = false;
    apiFetch.mockImplementation(async (path, options) => {
      if (path === CLOUD_SYNC_SETTING_PATH) {
        if (options && options.method === 'PUT') { syncGranted = true; return { ...syncOn }; }
        return { key: 'consent_cloud_sync', value: syncGranted };
      }
      return options && options.method === 'PUT' ? grantedRecord : { granted: false };
    });

    const { result } = renderHook(() => useCloudLlmConsent({ enabled: true }));
    await waitFor(() => expect(result.current.granted).toBe(false));

    const grant = result.current.grant;
    const readGranted = result.current.readGranted;
    expect(readGranted()).toBe(false);

    const outcome = await grant();

    expect(outcome).toBe(true);
    // No act(), no waitFor: the value a same-tick resume would read.
    expect(readGranted()).toBe(true);
  });

  it('readGranted() stays false when grant() fails, so the prompt can retry', async () => {
    apiFetch.mockImplementation(async (path, options) => {
      if (!options || options.method !== 'PUT') {
        return path === CLOUD_SYNC_SETTING_PATH ? syncOff : { granted: false };
      }
      if (path.endsWith('/data_retention')) throw new Error('server refused');
      return grantedRecord;
    });

    const { result } = renderHook(() => useCloudLlmConsent({ enabled: true }));
    await waitFor(() => expect(result.current.granted).toBe(false));

    const outcome = await result.current.grant();

    expect(outcome).toBe(false);
    expect(result.current.readGranted()).toBe(false);
  });

  it('a read failure never reports the user as granted', async () => {
    apiFetch.mockRejectedValue(new Error('network down'));

    const { result } = renderHook(() => useCloudLlmConsent({ enabled: true }));

    await waitFor(() => expect(result.current.error).toBeTruthy());
    expect(result.current.granted).toBeNull();
  });

  it('a consent_required refusal on the cloud-sync read reads as NOT granted, not unknown', async () => {
    // #4785, measured live on prod ca19433c9 with a freshly signed-up marked
    // account: GET /api/v1/cloud/settings/consent_cloud_sync 403s with
    // {"error":{"code":"consent_required"}} for exactly the brand-new users who
    // most need the prompt. That throw propagated out of refresh(), `granted`
    // stayed null, and shouldPromptForCloudConsent only prompts on an explicit
    // false -- so no prompt appeared at all and the music-stage composer
    // dispatched a turn the server refused with the raw
    // "Missing required cloud consents: cloud_llm, data_retention."
    const refusal = Object.assign(new Error('refused'), { status: 403, code: 'consent_required' });
    apiFetch.mockImplementation(async (path) => {
      if (path === CLOUD_SYNC_SETTING_PATH) throw refusal;
      return { granted: true };
    });

    const { result } = renderHook(() => useCloudLlmConsent({ enabled: true }));

    await waitFor(() => expect(result.current.granted).toBe(false));
    expect(result.current.readGranted()).toBe(false);
  });

  it('a NON-consent read failure still reads as unknown, so a blip never nags', async () => {
    // The other half of the same bargain: a 500 or a dead network is not the
    // server saying "not granted", so it must stay null and prompt nobody.
    const boom = Object.assign(new Error('server exploded'), { status: 500, code: 'internal_error' });
    apiFetch.mockImplementation(async (path) => {
      if (path === CLOUD_SYNC_SETTING_PATH) throw boom;
      return { granted: true };
    });

    const { result } = renderHook(() => useCloudLlmConsent({ enabled: true }));

    await waitFor(() => expect(result.current.error).toBeTruthy());
    expect(result.current.granted).toBeNull();
  });

  it('never touches the consent endpoint when disabled (desktop hub)', async () => {
    const { result } = renderHook(() => useCloudLlmConsent({ enabled: false }));

    await act(async () => { await Promise.resolve(); });

    expect(apiFetch).not.toHaveBeenCalled();
    expect(result.current.granted).toBeNull();
  });
});

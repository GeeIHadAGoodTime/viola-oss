/**
 * useCloudLlmConsent — read and grant EVERY cloud consent the managed agent requires.
 *
 * A brand-new free cloud-browser account defaults to NOT consented, so Viola's
 * agent is disabled for them and turns fall back silently. The desktop
 * onboarding consent panel writes only `ai_source`, never these consent records,
 * and it never runs in a plain browser. This hook is the browser-side surface
 * for the records themselves:
 *
 *   - GET  /api/v1/consent/<type>  -> current { granted } state
 *   - PUT  /api/v1/consent/<type>  -> grant with the accepted legal versions
 *
 * THE REQUIRED SET IS THE SERVER'S, NOT `cloud_llm` ALONE (#362)
 * -------------------------------------------------------------
 * `services/cloud_consent.py::can_execute_cloud_agent` gates every cloud agent
 * command on BOTH `cloud_llm` AND `data_retention`. This hook used to request
 * only `cloud_llm`, and `data_retention` had no grant surface anywhere in the
 * SPA (a repo-wide grep for it over `ui/react-app/src` returned zero hits), so a
 * new cloud user stayed refused forever even after doing exactly what the app
 * asked. Reproduced end to end against live prod on deployed SHA `0d13cdf2a`
 * with a freshly signed-up marked account, 2026-08-04:
 *
 *   POST /api/v1/command            -> cloud_consent_required
 *                                      missing [cloud_llm, data_retention]
 *   PUT  /api/v1/consent/cloud_llm  -> 200, granted   (all the SPA could do)
 *   POST /api/v1/command            -> STILL cloud_consent_required
 *                                      missing [data_retention]
 *
 * The only place a user could grant `data_retention` was the separate marketing
 * site's account page, `ViolaWebsite/js/account.js`, which the app never sends
 * them to.
 *
 * So the required set is derived from the server's gate and requested together:
 * partial consent IS the bug. `granted` is true only when EVERY required consent
 * is held, because a partially-consented user is exactly as blocked as an
 * unconsented one, and reporting them as consented is what produced a dead turn
 * with no prompt.
 *
 * The server rejects a grant whose terms/privacy versions don't match the
 * current canon, so we send the same ACCEPTED_* constants authClient.js sends at
 * signup. Only enable this on the cloud surface — the desktop hub does not use
 * the cloud consent endpoint.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { apiFetch } from './useViolaApi';
import { API } from '../config';
import { ACCEPTED_TERMS_VERSION, ACCEPTED_PRIVACY_VERSION } from '../auth/legalVersions';

/**
 * Every consent `can_execute_cloud_agent` requires, in the order the modal
 * discloses them. Keep in sync with
 * services/cloud_consent.py::can_execute_cloud_agent.
 */
export const REQUIRED_AGENT_CONSENTS = ['cloud_llm', 'data_retention'];

/**
 * The Tier-2 cloud-sync consent, which is a cloud SETTING rather than a consent
 * record and therefore lives at its own endpoint (services/sync/consent.py
 * exempts this one key from needing prior consent, which is what makes writing
 * it with a plain user bearer legitimate).
 *
 * It belongs in this hook because the chat-mode composer is dead without it,
 * and granting the two consent records alone does not revive it. Measured live
 * on deployed SHA `8f0b001562` with a freshly signed-up marked account,
 * 2026-08-05, after granting cloud_llm AND data_retention:
 *
 *   POST /v1/chat/threads -> 403 consent_required
 *                            "Enable cloud sync in Settings to use this feature."
 *
 * So a new user could accept the first-run prompt in chat mode, be told Viola
 * was on, and still not be able to send a single message. The only surface that
 * granted it was a toggle buried in Settings > Privacy & Data, which nothing
 * pointed them to. The modal's copy already discloses this ("Viola also keeps
 * the account data behind those replies, things like your conversation history
 * and settings, so features like sync, history, and support controls work").
 */
export const CLOUD_SYNC_SETTING_PATH = '/api/v1/cloud/settings/consent_cloud_sync';

const consentPath = (consentType) => `${API.CONSENT}/${consentType}`;

const readCloudSyncConsent = async () => {
  let data;
  try {
    data = await apiFetch(CLOUD_SYNC_SETTING_PATH);
  } catch (err) {
    // A `consent_required` refusal is the server STATING that this user has not
    // granted cloud sync. That is a definite "not granted", not a failed read,
    // and conflating the two is what left brand-new users with no prompt at all
    // (#4785): the route 403s for exactly the users who most need prompting, the
    // throw propagated out of `refresh()`, `granted` stayed `null`, and
    // `shouldPromptForCloudConsent` only prompts on an explicit `false`. So the
    // gate never fired and the composer dispatched a turn the server then
    // refused with the raw "Missing required cloud consents: cloud_llm,
    // data_retention." Measured live on prod ca19433c9 with a freshly
    // signed-up marked account, 2026-08-06:
    //
    //   GET /api/v1/cloud/settings/consent_cloud_sync
    //     -> 403 {"error":{"code":"consent_required", ...}}
    //   typed command on the music stage -> no prompt, and the stage shows
    //      "Missing required cloud consents: cloud_llm, data_retention."
    //
    // Every OTHER failure (network down, 500, a shape we cannot read) still
    // throws, so a transport blip keeps reading as unknown and never nags a
    // returning consented user.
    if (err && (err.code === 'consent_required' || err.status === 403)) return false;
    throw err;
  }
  // The settings envelope carries the value under `data.value`; anything else
  // (including a shape we do not recognise) must NOT read as granted.
  const value = data && data.data && 'value' in data.data ? data.data.value : (data || {}).value;
  return value === true || value === 'true';
};

export function useCloudLlmConsent({ enabled = true } = {}) {
  // null = unknown (not yet loaded); true/false once known. Consumers should
  // only prompt when this is explicitly false, so a returning consented user
  // and the brief loading window are never prompted.
  const [granted, setGranted] = useState(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const mountedRef = useRef(true);
  // The SAME value as `granted`, but readable SYNCHRONOUSLY (#4785).
  //
  // `granted` is React state, so it only reaches a gate closure on the next
  // render. `grant()` resolves and its caller immediately resumes the turn the
  // prompt interrupted -- in the same tick -- so a gate that reads the state
  // snapshot still sees the PRE-grant value and re-opens the prompt it just
  // closed. Measured live on prod ca19433c9 with a freshly signed-up marked
  // account, 2026-08-06: clicking "Turn on Viola" wrote all three consents
  // (cloud_llm 200, data_retention 200, consent_cloud_sync 200, all confirmed
  // granted in prod Postgres) and the modal re-opened, twice out of two
  // attempts, with no error shown. The only way through was to click the
  // DECLINE button after the grant had already landed. Gates must therefore
  // read `readGranted()`, never the `granted` snapshot.
  const grantedRef = useRef(null);
  const applyGranted = useCallback((value) => {
    grantedRef.current = value;
    if (mountedRef.current) setGranted(value);
  }, []);
  const readGranted = useCallback(() => grantedRef.current, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const refresh = useCallback(async () => {
    if (!enabled) return null;
    setLoading(true);
    setError(null);
    try {
      // Read EVERY required consent. A read failure must never read as granted.
      const states = await Promise.all([
        ...REQUIRED_AGENT_CONSENTS.map(async (consentType) => {
          const data = await apiFetch(consentPath(consentType));
          return !!(data && data.granted);
        }),
        readCloudSyncConsent(),
      ]);
      const isGranted = states.every(Boolean);
      applyGranted(isGranted);
      return isGranted;
    } catch (err) {
      if (mountedRef.current) setError(err);
      return null;
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, [enabled, applyGranted]);

  useEffect(() => {
    if (enabled) void refresh();
  }, [enabled, refresh]);

  const grant = useCallback(async () => {
    setSaving(true);
    setError(null);
    try {
      // Grant the WHOLE required set. Sequential rather than parallel so a
      // mid-way failure leaves a deterministic state and the throw names the
      // consent that actually failed.
      for (const consentType of REQUIRED_AGENT_CONSENTS) {
        // eslint-disable-next-line no-await-in-loop
        const result = await apiFetch(consentPath(consentType), {
          method: 'PUT',
          body: JSON.stringify({
            granted: true,
            terms_version: ACCEPTED_TERMS_VERSION,
            privacy_version: ACCEPTED_PRIVACY_VERSION,
          }),
        });
        const record = result && result.record ? result.record : result;
        if (!(record && record.granted)) {
          throw new Error(`cloud consent ${consentType} was not granted by the server`);
        }
      }
      // Same bargain, different endpoint: without this the user accepts the
      // prompt and the chat composer is still refused at thread creation.
      await apiFetch(CLOUD_SYNC_SETTING_PATH, {
        method: 'PUT',
        body: JSON.stringify({ value: true }),
      });
      if (!(await readCloudSyncConsent())) {
        // A 200 on the write is not evidence the setting stuck.
        throw new Error('cloud sync consent was not granted by the server');
      }
      // Synchronous, so a caller that resumes the interrupted turn in this same
      // tick cannot re-enter the gate on a pre-grant value (#4785).
      applyGranted(true);
      return true;
    } catch (err) {
      // A partial grant must NOT read as consented: the agent stays blocked, so
      // the caller has to be able to re-prompt instead of starting another dead
      // turn. The old code returned true unconditionally, which is how a
      // half-consented user got a dead turn and no second prompt.
      if (mountedRef.current) setError(err);
      applyGranted(false);
      return false;
    } finally {
      if (mountedRef.current) setSaving(false);
    }
  }, [applyGranted]);

  return { granted, loading, saving, error, refresh, grant, readGranted };
}

export default useCloudLlmConsent;

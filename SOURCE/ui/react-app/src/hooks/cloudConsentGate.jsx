/**
 * cloudConsentGate — ONE first-run consent gate, reachable from any component
 * that can start an agent turn.
 *
 * #4667 made the rule a shared predicate so "a new way to start a turn shipped
 * without a gate" would stop being a silent dead turn. It shared it as a
 * module-level FUNCTION, which only helps call sites that also happen to hold
 * SmartDisplay's `cloudSurfaceActive` / `cloudLlmConsent` / modal state. Every
 * turn entry point that lives in its own component -- the chat-mode composer
 * most importantly -- could not reach those, so it stayed ungated exactly like
 * the text composer had been, and the same bug shipped a second time.
 *
 * Reproduced live on deployed SHA `8f0b001562` (which already contains #4667)
 * with a freshly signed-up marked account, 2026-08-05:
 *
 *   POST /v1/command                -> ok:false cloud_consent_required
 *                                      missing [cloud_llm, data_retention]
 *   POST /v1/chat/threads           -> 403 consent_required
 *                                      "Enable cloud sync in Settings to use this feature."
 *
 * and the chat composer surfaced NOTHING for it: `ChatMode.sendText` awaited
 * `ensureThread()` outside its own try block, so the rejection escaped as an
 * unhandled promise and the user's typed message vanished with no reply, no
 * error, and no prompt.
 *
 * So the gate is a CONTEXT, not just a function. SmartDisplay owns the consent
 * state and the modal exactly as before and publishes one interceptor; any
 * descendant asks for it with `useCloudConsentGate()` and cannot reach the
 * turn-starting code path without going through it.
 *
 * Off the cloud surface (the desktop hub) and anywhere the provider is absent,
 * the default interceptor never intercepts, so nothing about the desktop turn
 * path changes.
 */

import { createContext, useContext } from 'react';
import PropTypes from 'prop-types';

/**
 * Should this agent turn be interrupted to ask for the cloud first-run consents?
 *
 * Only prompts when consent is KNOWN missing (=== false); a returning consented
 * user and the brief unknown/loading window (null) are never interrupted.
 */
export function shouldPromptForCloudConsent(cloudSurfaceActive, consentGranted) {
  return Boolean(cloudSurfaceActive) && consentGranted === false;
}

/**
 * The interceptor contract: called with what the user was trying to do, returns
 * true when it has taken over (prompt opened, turn deferred) and false when the
 * caller should proceed with the turn.
 *
 * Defaulting to "never intercept" keeps the desktop hub and every test that
 * renders a turn surface without the provider working unchanged.
 */
const CloudConsentGateContext = createContext(() => false);

export function CloudConsentGateProvider({ intercept, children }) {
  return (
    <CloudConsentGateContext.Provider value={intercept}>
      {children}
    </CloudConsentGateContext.Provider>
  );
}

CloudConsentGateProvider.propTypes = {
  intercept: PropTypes.func.isRequired,
  children: PropTypes.node,
};

/**
 * Returns `interceptTurn(pendingAction) -> boolean`.
 *
 * Call it FIRST in any function that starts an agent turn, and return early
 * when it returns true. `pendingAction` describes the turn so it can be resumed
 * verbatim once the user accepts, instead of being dropped.
 */
export function useCloudConsentGate() {
  return useContext(CloudConsentGateContext);
}

export default useCloudConsentGate;

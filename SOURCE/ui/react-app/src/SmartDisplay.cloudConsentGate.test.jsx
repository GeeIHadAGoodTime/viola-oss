/**
 * Ratchet for #362: every way of starting an agent turn on the cloud browser
 * shares ONE first-run consent gate.
 *
 * The gate existed only inline at push-to-talk and browser-wake. The text
 * composer had none, so a brand-new cloud user who typed instead of speaking was
 * never prompted: the command hit `can_execute_cloud_agent`, was refused for
 * consents they had no way to grant, and the stage rendered the pipeline's "No
 * handler matched your request" fallback. Verified live on deployed SHA
 * 0d13cdf2a with a freshly signed-up marked account, 2026-08-04.
 *
 * Sharing the predicate is what makes a missing gate visible instead of silent,
 * so this asserts the RULE and that the composer is actually wired through it.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { shouldPromptForCloudConsent } from './SmartDisplay';

describe('shouldPromptForCloudConsent', () => {
  it('prompts a cloud user whose consent is KNOWN missing', () => {
    expect(shouldPromptForCloudConsent(true, false)).toBe(true);
  });

  it('never prompts a consented user', () => {
    expect(shouldPromptForCloudConsent(true, true)).toBe(false);
  });

  it('never prompts during the unknown/loading window', () => {
    // null is "not yet loaded". Prompting there would interrupt a returning
    // consented user on every page load.
    expect(shouldPromptForCloudConsent(true, null)).toBe(false);
    expect(shouldPromptForCloudConsent(true, undefined)).toBe(false);
  });

  it('never prompts off the cloud surface (the desktop hub)', () => {
    // The desktop hub does not use the cloud consent endpoint at all.
    expect(shouldPromptForCloudConsent(false, false)).toBe(false);
    expect(shouldPromptForCloudConsent(false, null)).toBe(false);
  });
});

describe('SmartDisplay turn entry points', () => {
  const source = readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), 'SmartDisplay.jsx'),
    'utf-8',
  );

  it('routes the text composer through the gated submit, not a raw sendCommand', () => {
    // The pre-fix wiring was:
    //   onSubmitText={(text) => api.sendCommand(text).then(handleCommandResult)}
    // which bypassed the consent gate entirely.
    expect(source).toContain('onSubmitText={submitTextCommand}');
    expect(source).not.toMatch(/onSubmitText=\{\(text\)\s*=>\s*api\.sendCommand/);
  });

  it('evaluates the consent rule in exactly ONE place', () => {
    // Counting gated entry points was the previous shape of this assertion, and
    // it could only ever see entry points living in THIS file. The chat-mode
    // composer lives in its own component, so it shipped ungated while this
    // test stayed green. One evaluation site, published as context, is what
    // makes the rule reachable from any component.
    const gateCalls = source.match(/shouldPromptForCloudConsent\(/g) || [];
    expect(gateCalls.length).toBe(1);
    expect(source).toMatch(
      /const interceptCloudConsent = useCallback\(\(pendingAction\) => \{\s*if \(!shouldPromptForCloudConsent\(cloudSurfaceActive, readCloudLlmConsent\(\)\)\) return false;/,
    );
  });

  it('reads consent SYNCHRONOUSLY, never off the React state snapshot', () => {
    // #4785: accepting the prompt resumes the interrupted turn in the SAME tick
    // the grant lands, so `cloudLlmConsent.granted` (state) is still pre-grant
    // there. Reading it re-intercepted and re-opened the prompt the user had
    // just accepted, forever. Reproduced live on prod ca19433c9 with a freshly
    // signed-up marked account: three consent writes all returned 200, prod
    // Postgres confirmed every one granted, and the modal came back twice out of
    // two attempts with no error shown.
    expect(source).not.toMatch(/shouldPromptForCloudConsent\(cloudSurfaceActive, cloudLlmConsent\.granted\)/);
    expect(source).toContain('const readCloudLlmConsent = cloudLlmConsent.readGranted;');
  });

  it('publishes the gate so entry points in other components can reach it', () => {
    // Without this the rule is unreachable outside SmartDisplay, which is
    // exactly why the chat composer had no gate at all.
    expect(source).toContain('<CloudConsentGateProvider intercept={interceptCloudConsent}>');
  });

  it('routes every turn entry point in this file through the shared gate', () => {
    // Each of these starts an agent turn. The raw dispatch call must be
    // preceded by the gate, never reached directly.
    const entryPoints = [
      // the music-stage composer / onboarding suggestions
      /const submitTextCommand = useCallback\(\(text\) => \{\s*if \(interceptCloudConsent\(/,
      // push-to-talk
      /if \(!voice\.isRecording && interceptCloudConsent\(\{ kind: 'voice' \}\)\)/,
      // in-tab browser wake
      /if \(interceptCloudConsent\(\{ kind: 'voice' \}\)\) return;\s*beginVoiceTurn\(\);/,
      // type-anywhere on the stage, which streams instead of using the composer
      /if \(interceptCloudConsent\(\{ kind: 'text', text \}\)\) return;/,
    ];
    for (const pattern of entryPoints) {
      expect(source).toMatch(pattern);
    }
  });

  it('never hands a raw sendCommand to a child component as its turn starter', () => {
    // OnboardingOverlay took `sendCommand={(text) => api.sendCommand(text)...}`,
    // so tapping a suggested command as a brand-new user dispatched an
    // ungated turn that the server refused.
    expect(source).not.toMatch(/sendCommand=\{\(text\)\s*=>\s*api\.sendCommand/);
    expect(source).toContain('sendCommand={submitTextCommand}');
  });

  it('never re-inlines the old per-site consent rule', () => {
    // The duplicated inline shape is what let the text path ship ungated.
    expect(source).not.toMatch(/cloudSurfaceActive\s*&&\s*cloudLlmConsent\.granted\s*===\s*false/);
  });
});

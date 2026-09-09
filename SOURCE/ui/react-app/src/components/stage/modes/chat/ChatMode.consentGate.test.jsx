/**
 * Ratchet for #362, second instantiation: the chat-mode composer takes the same
 * first-run consent gate every other turn entry point takes.
 *
 * #4667 fixed the music-stage composer by extracting a shared predicate. The
 * chat composer lives in its own component, could not reach SmartDisplay's
 * consent state, and so shipped with no gate at all -- the same bug, one week
 * later, on the surface a new browser user is most likely to open first.
 *
 * Measured live on deployed SHA `8f0b001562` (which contains #4667) with a
 * freshly signed-up marked account, 2026-08-05:
 *
 *   POST /v1/chat/threads -> 403 consent_required
 *                            "Enable cloud sync in Settings to use this feature."
 *
 * and the composer showed the user nothing at all, because `sendText` awaited
 * `ensureThread()` outside its own try block while `onSend` does not catch.
 *
 * Every case below fails against that pre-fix shape.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const source = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), 'ChatMode.jsx'),
  'utf-8',
);

describe('chat-mode composer consent gate', () => {
  it('consumes the ONE shared gate rather than defining its own rule', () => {
    expect(source).toContain("import { useCloudConsentGate } from '../../../../hooks/cloudConsentGate'");
    expect(source).toContain('const interceptCloudConsent = useCloudConsentGate();');
    // A second copy of the rule is the thing that let these two surfaces drift
    // apart in the first place.
    expect(source).not.toMatch(/shouldPromptForCloudConsent\s*\(/);
    expect(source).not.toMatch(/cloud_llm|data_retention/);
  });

  it('gates sendText BEFORE it starts a turn, and resumes the same message', () => {
    // The gate has to run before any network call, or the user still burns a
    // refused turn; and it has to carry a resume, or accepting the prompt
    // silently drops what they typed.
    const sendText = source.slice(
      source.indexOf('const sendText = useCallback'),
      source.indexOf('const stopStreaming = useCallback'),
    );
    expect(sendText).toMatch(
      /if \(interceptCloudConsent\(\{[^}]*resume: \(\) => sendText\(clean\)[^}]*\}\)\)/,
    );
    const gateIndex = sendText.indexOf('interceptCloudConsent(');
    const sendIndex = sendText.indexOf('/send`');
    expect(gateIndex).toBeGreaterThan(-1);
    expect(sendIndex).toBeGreaterThan(-1);
    expect(gateIndex).toBeLessThan(sendIndex);
  });

  it('never awaits ensureThread outside the handler that reports its failure', () => {
    // The pre-fix shape was:
    //   const threadId = await ensureThread();   <- above the try
    //   ...
    //   try { ... } catch { ...show an error... }
    // so a 403 at thread creation escaped as an unhandled rejection and the
    // user saw nothing whatsoever.
    const sendText = source.slice(
      source.indexOf('const sendText = useCallback'),
      source.indexOf('const stopStreaming = useCallback'),
    );
    expect(sendText).toContain('const threadId = await ensureThread();');
    const tryIndex = sendText.indexOf('try {');
    const ensureIndex = sendText.indexOf('const threadId = await ensureThread();');
    expect(tryIndex).toBeGreaterThan(-1);
    expect(ensureIndex).toBeGreaterThan(tryIndex);
  });

  it('names the real reason a send was refused instead of a generic apology', () => {
    // "Something went wrong while sending that message" is the same class of
    // dishonesty as the pipeline's "No handler matched your request": the
    // server knew exactly why and said so.
    expect(source).toMatch(/err\?\.code === 'consent_required'/);
    expect(source).toMatch(/err\?\.code === 'cloud_consent_required'/);
    expect(source).toMatch(/Cloud Sync in Settings/);
  });
});

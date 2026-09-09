import { describe, it, expect, afterEach, vi } from 'vitest';
import { getPlatform, getStartupToggleCopy, getMicPermissionGuidance } from './platform';

function stubUserAgent(ua, platform = '') {
  vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(ua);
  vi.spyOn(navigator, 'platform', 'get').mockReturnValue(platform);
}

describe('platform detection + startup copy', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('detects macOS and labels the toggle "Start at Login" (issue #770)', () => {
    stubUserAgent('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)', 'MacIntel');
    expect(getPlatform()).toBe('mac');
    expect(getStartupToggleCopy().title).toBe('Start at Login');
  });

  it('detects Windows and keeps the boot wording', () => {
    stubUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'Win32');
    expect(getPlatform()).toBe('windows');
    expect(getStartupToggleCopy().title).toBe('Start on Windows Boot');
  });

  it('detects Linux and uses login wording', () => {
    stubUserAgent('Mozilla/5.0 (X11; Linux x86_64)', 'Linux x86_64');
    expect(getPlatform()).toBe('linux');
    expect(getStartupToggleCopy().title).toBe('Start at Login');
  });

  it('never labels a non-Windows host "Start on Windows Boot"', () => {
    stubUserAgent('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)', 'MacIntel');
    expect(getStartupToggleCopy().title).not.toMatch(/Windows/);
  });
});

// C-078: mic-permission-denied guidance during onboarding used to hardcode
// Windows-only copy regardless of host OS, so Mac and Linux users hitting a
// mic-denial were told to check Windows settings that do not exist on their
// machine. getMicPermissionGuidance() is the fix — it must branch by the same
// getPlatform() signal getStartupToggleCopy() already relies on.
describe('mic permission guidance (issue C-078)', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('points a Mac user at System Settings, never Windows copy', () => {
    stubUserAgent('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)', 'MacIntel');
    const guidance = getMicPermissionGuidance();
    expect(guidance).toMatch(/System Settings/);
    expect(guidance).not.toMatch(/Windows/);
  });

  it('keeps the Windows-settings wording on an actual Windows host', () => {
    stubUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'Win32');
    expect(getMicPermissionGuidance()).toMatch(/Windows microphone privacy settings/);
  });

  it('gives Linux a generic, honest answer rather than naming the wrong OS', () => {
    stubUserAgent('Mozilla/5.0 (X11; Linux x86_64)', 'Linux x86_64');
    const guidance = getMicPermissionGuidance();
    expect(guidance).not.toMatch(/Windows/);
    expect(guidance).not.toMatch(/System Settings/);
    expect(guidance).toMatch(/microphone privacy settings/);
  });

  it('falls back to OS-neutral copy on an unrecognized host', () => {
    stubUserAgent('SomeUnknownWebview/1.0', '');
    const guidance = getMicPermissionGuidance();
    expect(guidance).not.toMatch(/Windows/);
    expect(guidance).not.toMatch(/System Settings/);
  });

  it('reproduces the pre-fix bug shape as a failing assertion (documentation)', () => {
    // Pre-fix, useVoiceOnboarding.js:225 always returned this literal
    // regardless of host OS. Simulating that shape on a Mac host must fail
    // the same assertion the fixed code now passes above.
    const macUserAgent = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)';
    stubUserAgent(macUserAgent, 'MacIntel');
    const preFixHardcodedMessage =
      'Microphone access could not be confirmed. Check Windows microphone privacy settings and try again.';
    expect(preFixHardcodedMessage).toMatch(/Windows/); // the old bug shape
    expect(getMicPermissionGuidance()).not.toBe(preFixHardcodedMessage); // the fix
  });
});

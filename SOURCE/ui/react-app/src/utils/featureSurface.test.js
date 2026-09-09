import { afterEach, describe, expect, it } from 'vitest';
import {
  DESKTOP_ONLY_FEATURES,
  isFeatureAvailable,
  isFeatureHidden,
} from './featureSurface';

// featureSurface keys off the same signals as cloudSurface/runtimeSurface:
//  - window.viola present      -> desktop app  -> everything available
//  - no bridge, no spoke param -> cloud SPA     -> desktop-only features hidden
//  - ?mode=speaker / ?room=    -> multiroom spoke -> full hub parity

describe('featureSurface', () => {
  afterEach(() => {
    delete window.viola;
    window.history.replaceState({}, '', '/');
  });

  it('makes every feature available on the desktop app (Qt bridge present)', () => {
    window.viola = {};
    for (const feature of Object.values(DESKTOP_ONLY_FEATURES)) {
      expect(isFeatureAvailable(feature)).toBe(true);
      expect(isFeatureHidden(feature)).toBe(false);
    }
    expect(isFeatureAvailable('music')).toBe(true);
  });

  it('hides desktop-only features on the cloud SPA (no bridge, no spoke param)', () => {
    delete window.viola;
    window.history.replaceState({}, '', '/app');
    expect(isFeatureAvailable('agents')).toBe(false);
    expect(isFeatureAvailable('onboarding')).toBe(false);
    expect(isFeatureAvailable('rooms')).toBe(false);
    expect(isFeatureHidden('agents')).toBe(true);
  });

  // #1064: Billing & Payment Methods, Workbench uploads, and Google/Spotify/
  // YouTube Music OAuth linking all 404'd silently on the cloud SPA (each
  // backs onto a Tier-3 vault or the desktop's local filesystem). Same
  // hide-and-upsell treatment as agents/onboarding/rooms. (The Memory panel
  // was also on this list until #1182 rewired it onto /v1/memories -- see
  // the dedicated regression test below.)
  it('hides the #1064 desktop-only feature groups on the cloud SPA', () => {
    delete window.viola;
    window.history.replaceState({}, '', '/app');
    expect(isFeatureAvailable('payment_cards')).toBe(false);
    expect(isFeatureAvailable('workbench')).toBe(false);
    expect(isFeatureAvailable('oauth_provider_linking')).toBe(false);
    expect(isFeatureHidden('payment_cards')).toBe(true);
    expect(isFeatureHidden('workbench')).toBe(true);
    expect(isFeatureHidden('oauth_provider_linking')).toBe(true);
  });

  it('keeps shared/cloud-native features available on the cloud SPA', () => {
    delete window.viola;
    window.history.replaceState({}, '', '/app');
    // Anything not listed as desktop-only stays available (music, command,
    // conversations, memory, settings, playlists).
    expect(isFeatureAvailable('music')).toBe(true);
    expect(isFeatureAvailable('settings')).toBe(true);
    expect(isFeatureAvailable('conversations')).toBe(true);
  });

  // Regression for the 2026-07-06 "calendar doesn't work in the browser"
  // bug: `calendar` stayed in DESKTOP_ONLY_FEATURES for two days after the
  // self-hosted CalDAV work made the backend "calendar" route group live on
  // cloud (CLOUD_NATIVE, tenant-safety reviewed, safe_to_register=True).
  // `calendar` must NOT be in the desktop-only set, and must render on the
  // cloud SPA exactly like any other cloud-native feature.
  it('keeps calendar available on the cloud SPA (2026-07-06 regression)', () => {
    delete window.viola;
    window.history.replaceState({}, '', '/app');
    expect(DESKTOP_ONLY_FEATURES.calendar).toBeUndefined();
    expect(isFeatureAvailable('calendar')).toBe(true);
    expect(isFeatureHidden('calendar')).toBe(false);
  });

  // #1182: `memory_files` stayed listed here after the ticket's own DoD
  // required removing it once the Memory panel became cloud-native --
  // the exact 2026-07-06 `calendar` drift shape one level up. This is the
  // negative case for the Ratchet: reintroducing the key (or reverting
  // MemoryPanel.jsx's isCloudSurface() branch) would fail this.
  it('keeps memory available on the cloud SPA (#1182 regression)', () => {
    delete window.viola;
    window.history.replaceState({}, '', '/app');
    expect(DESKTOP_ONLY_FEATURES.memory_files).toBeUndefined();
    expect(isFeatureAvailable('memory_files')).toBe(true);
    expect(isFeatureHidden('memory_files')).toBe(false);
  });

  it('keeps desktop features available on hub-backed multiroom spokes', () => {
    delete window.viola;
    window.history.replaceState({}, '', '/?mode=speaker');
    expect(isFeatureAvailable('music', { isSpoke: true })).toBe(true);
    expect(isFeatureAvailable('calendar', { isSpoke: true })).toBe(true);
    expect(isFeatureAvailable('agents', { isSpoke: true })).toBe(true);
    expect(isFeatureAvailable('onboarding', { isSpoke: true })).toBe(true);
    expect(isFeatureAvailable('rooms', { isSpoke: true })).toBe(true);
    expect(isFeatureAvailable('payment_cards', { isSpoke: true })).toBe(true);
    expect(isFeatureAvailable('workbench', { isSpoke: true })).toBe(true);
    expect(isFeatureAvailable('oauth_provider_linking', { isSpoke: true })).toBe(true);
  });
});

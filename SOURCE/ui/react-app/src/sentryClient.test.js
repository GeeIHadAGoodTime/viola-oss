import { describe, expect, it } from 'vitest';
import { buildSentryFeedbackContext, sanitizeSentryContext } from './sentryClient';

describe('sentryClient', () => {
  it('redacts sensitive keys and URL query strings', () => {
    expect(sanitizeSentryContext({
      access_token: 'secret-token', // pragma: allowlist secret
      current_url: 'https://example.test/app?token=abc#frag',
      nested: {
        api_key: 'secret-key', // pragma: allowlist secret
      },
    })).toEqual({
      access_token: '[redacted]',
      current_url: 'https://example.test/app',
      nested: {
        api_key: '[redacted]',
      },
    });
  });

  it('keeps Sentry feedback context to safe oracle fields', () => {
    expect(buildSentryFeedbackContext({
      source: 'react_topbar',
      surface: 'react_web',
      ui_entrypoint: 'react_topbar',
      current_url: 'https://example.test/app?spoke_token=abc',
      display_mode: 'now_playing',
      stage_mode: 'music',
      recent_action: {
        kind: 'playback',
        provider: 'local',
        title: 'private title',
        artist: 'private artist',
        is_playing: true,
      },
      viewport: {
        width: 1280,
        height: 720,
      },
      player: {
        title: 'private title',
      },
    })).toEqual({
      source: 'react_topbar',
      surface: 'react_web',
      ui_entrypoint: 'react_topbar',
      current_url: 'https://example.test/app',
      display_mode: 'now_playing',
      stage_mode: 'music',
      recent_action: {
        kind: 'playback',
        provider: 'local',
        is_playing: true,
      },
      viewport: {
        width: 1280,
        height: 720,
      },
      screen_capture_metadata: {
        requested: false,
        provided: false,
        storage: 'metadata_only',
      },
    });
  });
});

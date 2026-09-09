import {
  normalizePlayerStatePayloadForTest,
  shouldRehydrateEmbeddedNowPlayingForTest,
} from './usePlayerState';

describe('usePlayerState state normalization', () => {
  it('preserves YouTube video metadata when a degraded broadcast repeats the same track', () => {
    const previous = {
      now_playing: {
        id: 'track-1',
        title: 'Song',
        artist: 'Artist',
        video_id: 'wK6Tx18Bcpw',
        provider: 'youtube_iframe',
        playback_mode: 'embedded_iframe_webview',
        capabilities: { requires_embedded_player: true },
      },
    };

    const normalized = normalizePlayerStatePayloadForTest({
      now_playing: {
        id: 'track-1',
        title: 'Song',
        artist: 'Artist',
      },
      is_playing: false,
    }, previous);

    expect(normalized.now_playing.video_id).toBe('wK6Tx18Bcpw');
    expect(normalized.now_playing.provider).toBe('youtube_iframe');
    expect(normalized.now_playing.capabilities.requires_embedded_player).toBe(true);
  });

  it('extracts a YouTube video id from a URL in incoming state', () => {
    const normalized = normalizePlayerStatePayloadForTest({
      now_playing: {
        id: 'track-2',
        title: 'Song',
        url: 'https://www.youtube.com/watch?v=wK6Tx18Bcpw',
      },
    });

    expect(normalized.now_playing.video_id).toBe('wK6Tx18Bcpw');
  });

  it('rehydrates embedded tracks when a broadcast lacks required video metadata', () => {
    expect(shouldRehydrateEmbeddedNowPlayingForTest({
      playback_mode: 'embedded_iframe_webview',
      now_playing: {
        id: 'track-3',
        title: 'Song',
      },
    })).toBe(true);

    expect(shouldRehydrateEmbeddedNowPlayingForTest({
      now_playing: {
        id: 'local-1',
        title: 'Local Song',
        provider: 'local',
      },
    })).toBe(false);
  });
});

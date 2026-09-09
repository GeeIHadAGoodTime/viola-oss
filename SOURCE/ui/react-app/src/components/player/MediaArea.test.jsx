import React from 'react';
import { render, screen } from '@testing-library/react';
import MediaArea from './MediaArea';
import styles from './MediaArea.module.css';

function YouTubeEmbed({ videoId }) {
  return <iframe title="youtube-test-embed" src={`/static/webviews/youtube_iframe_v3.html?video=${videoId}`} />;
}

function ProviderEmbed() {
  return <iframe title="provider-test-embed" src="/provider" />;
}

describe('MediaArea', () => {
  it('keeps the YouTube playback iframe mounted when a stale browser overlay is visible', () => {
    render(
      <MediaArea
        nowPlaying={{
          title: 'Song',
          video_id: 'wK6Tx18Bcpw',
          provider: 'youtube_iframe',
        }}
        browserOverlayVisible
        YouTubeEmbed={YouTubeEmbed}
        ProviderEmbed={ProviderEmbed}
        iframeRef={React.createRef()}
      />
    );

    expect(screen.getByTitle('youtube-test-embed')).toBeTruthy();
    expect(screen.queryByLabelText('Close browser overlay')).toBeNull();
  });
});

// #1530: non-YouTube (Spotify/browser/other-provider) sources sat as a fully
// static tile the whole time audio played -- YouTube and provider "player"
// embeds are live iframes with their own motion, but a plain album-art image
// never moved. The fix adds a breathing bronze glow + slow art zoom, gated on
// BOTH a real artUrl (nothing to animate on the placeholder) and isPlaying
// (paused art stays still, same as desktop Spotify/Apple Music). These tests
// pin that state wiring: which combinations turn the motion classes on.
describe('MediaArea now-playing motion (#1530)', () => {
  const commonProps = {
    YouTubeEmbed,
    ProviderEmbed,
    iframeRef: React.createRef(),
  };

  it('adds the motion classes to a Spotify album-art tile while playing', () => {
    render(
      <MediaArea
        {...commonProps}
        isPlaying
        nowPlaying={{
          title: 'Song',
          artist: 'Artist',
          provider: 'spotify',
          artwork_url: 'https://example.com/art.jpg',
        }}
      />
    );

    const img = screen.getByAltText('Album art');
    expect(img.classList.contains(styles.albumArtAnimated)).toBe(true);
    expect(img.parentElement.classList.contains(styles.artMotion)).toBe(true);
  });

  it('does NOT add motion classes to a Spotify album-art tile while paused', () => {
    render(
      <MediaArea
        {...commonProps}
        isPlaying={false}
        nowPlaying={{
          title: 'Song',
          artist: 'Artist',
          provider: 'spotify',
          artwork_url: 'https://example.com/art.jpg',
        }}
      />
    );

    const img = screen.getByAltText('Album art');
    expect(img.classList.contains(styles.albumArtAnimated)).toBe(false);
    expect(img.parentElement.classList.contains(styles.artMotion)).toBe(false);
  });

  it('adds the motion classes to any other thumbnail-only provider while playing (e.g. local library)', () => {
    render(
      <MediaArea
        {...commonProps}
        isPlaying
        nowPlaying={{
          title: 'Song',
          artist: 'Artist',
          provider: 'local',
          artwork_url: 'https://example.com/art.jpg',
        }}
      />
    );

    const img = screen.getByAltText('Album art');
    expect(img.classList.contains(styles.albumArtAnimated)).toBe(true);
    expect(img.parentElement.classList.contains(styles.artMotion)).toBe(true);
  });

  it('never adds motion classes to the placeholder tile (no artUrl), playing or not', () => {
    const { rerender } = render(
      <MediaArea {...commonProps} isPlaying nowPlaying={{ title: 'Song', provider: 'spotify' }} />
    );
    let img = screen.getByAltText('Album art');
    expect(img.classList.contains(styles.albumArtAnimated)).toBe(false);
    // No wrapping container is rendered for the bare placeholder case.
    expect(img.parentElement.classList.contains(styles.artMotion)).toBe(false);

    rerender(
      <MediaArea {...commonProps} isPlaying={false} nowPlaying={{ title: 'Song', provider: 'spotify' }} />
    );
    img = screen.getByAltText('Album art');
    expect(img.classList.contains(styles.albumArtAnimated)).toBe(false);
  });

  it('does not apply motion classes to the YouTube video embed path', () => {
    render(
      <MediaArea
        {...commonProps}
        isPlaying
        nowPlaying={{ title: 'Song', video_id: 'wK6Tx18Bcpw', provider: 'youtube_iframe' }}
      />
    );

    expect(screen.getByTitle('youtube-test-embed')).toBeTruthy();
    expect(screen.queryByAltText('Album art')).toBeNull();
  });
});

// #2571: `isBrowserAlbumArt` (provider === 'browser' && capabilities.embed_type
// === 'album_art') was dead code -- `isBrowserLoading`, checked earlier in the
// same render chain and gated only on `provider === 'browser' && !video_id`,
// always won first, so the album-art branch could never be reached by any
// prop combination. No backend path ever sends embed_type: 'album_art' for a
// browser-provider payload either, so there is no real "browser sourced,
// thumbnail-only, no embed" product state -- browser playback always
// resolves to either the YouTube embed (video_id present) or this loading
// state (video_id absent). Resolved by deleting the unreachable branch; these
// tests pin that the loading state is what actually renders, even when a
// (never-real) payload carries embed_type: 'album_art'.
describe('MediaArea browser-provider resolution (#2571)', () => {
  const commonProps = {
    YouTubeEmbed,
    ProviderEmbed,
    iframeRef: React.createRef(),
  };

  it('renders the loading state for a browser-provider item with no video_id yet', () => {
    render(
      <MediaArea
        {...commonProps}
        nowPlaying={{
          title: 'Song',
          provider: 'browser',
          artwork_url: 'https://example.com/art.jpg',
        }}
      />
    );

    expect(screen.getByText('Searching YouTube...')).toBeTruthy();
    expect(screen.queryByAltText('Album art')).toBeNull();
  });

  it('still renders the loading state even if a payload carries the never-real embed_type "album_art"', () => {
    render(
      <MediaArea
        {...commonProps}
        nowPlaying={{
          title: 'Song',
          provider: 'browser',
          artwork_url: 'https://example.com/art.jpg',
          capabilities: { embed_type: 'album_art' },
        }}
      />
    );

    expect(screen.getByText('Searching YouTube...')).toBeTruthy();
    expect(screen.queryByAltText('Album art')).toBeNull();
  });

  it('resolves a browser-provider item with a video_id to the YouTube embed, not album art', () => {
    render(
      <MediaArea
        {...commonProps}
        nowPlaying={{
          title: 'Song',
          provider: 'browser',
          video_id: 'wK6Tx18Bcpw',
          artwork_url: 'https://example.com/art.jpg',
          capabilities: { embed_type: 'album_art' },
        }}
      />
    );

    expect(screen.getByTitle('youtube-test-embed')).toBeTruthy();
    expect(screen.queryByText('Searching YouTube...')).toBeNull();
    expect(screen.queryByAltText('Album art')).toBeNull();
  });
});

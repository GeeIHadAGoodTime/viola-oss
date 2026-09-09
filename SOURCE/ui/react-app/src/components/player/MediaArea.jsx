/**
 * MediaArea — Album art, YouTube embed, provider embed, browser overlay target.
 *
 * Displays the appropriate visual for the current playback source.
 */
import { forwardRef } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';
import styles from './MediaArea.module.css';

// Placeholder image for when no album art is available — uses transparent fill
// so the parent's themed background shows through; ring + dot in mid-gray that
// reads on both light and dark surfaces.
const PLACEHOLDER_ART = 'data:image/svg+xml;utf8,' + encodeURIComponent(
  '<svg width="320" height="320" viewBox="0 0 320 320" xmlns="http://www.w3.org/2000/svg">'
  + '<rect width="320" height="320" fill="none"/>'
  + '<circle cx="160" cy="160" r="80" stroke="rgba(128,128,128,0.35)" stroke-width="4" fill="none"/>'
  + '<circle cx="160" cy="160" r="30" fill="rgba(128,128,128,0.35)"/>'
  + '</svg>'
);

const MediaArea = forwardRef(({
  nowPlaying,
  isSpoke,
  isPlaying,
  browserOverlayVisible,
  YouTubeEmbed,
  ProviderEmbed,
  iframeRef,
}, ref) => {
  const showVideoEmbed = nowPlaying?.video_id;
  const embedUrl = nowPlaying?.capabilities?.embed_url;
  const embedType = nowPlaying?.capabilities?.embed_type;
  const providerName = nowPlaying?.capabilities?.provider_name;
  const showProviderEmbed = !showVideoEmbed && embedType === 'player' && !!embedUrl;
  const showBrowserOverlay = browserOverlayVisible && !showVideoEmbed;
  const isSpotifyAlbumArt = !showVideoEmbed && !showProviderEmbed
    && nowPlaying?.provider === 'spotify'
    && !nowPlaying?.video_id;
  const artUrl = nowPlaying?.artwork_url || nowPlaying?.thumbnail_url;
  // Browser-sourced playback only ever resolves two ways: it has a
  // `video_id` (showVideoEmbed wins above) or it doesn't yet (this loading
  // state). There is no third "browser, thumbnail-only, no embed" product
  // state -- no backend code path ever sends `embed_type: 'album_art'` for
  // `provider: 'browser'` (see #2571), so this loading state is always the
  // right terminal render for a browser-provider item without a video yet.
  const isBrowserLoading = !showVideoEmbed && !showProviderEmbed
    && nowPlaying?.provider === 'browser'
    && !nowPlaying?.video_id;
  const hasWideEmbed = showVideoEmbed || showProviderEmbed;
  // Now-playing motion (#1530): YouTube and provider embeds already carry
  // their own motion (they're live video/widget iframes). The static
  // album-art tiles below (Spotify, or any other provider that only gives us
  // a thumbnail -- e.g. local library) are the actual gap -- a still frame
  // the whole time audio plays. Motion is gated on BOTH a real artUrl
  // (nothing to animate on the placeholder icon) and isPlaying (paused art
  // stays still, matching how desktop Spotify/Apple Music freeze their own
  // art on pause). isBrowserAlbumArt is NOT a live path for this today --
  // it's pre-existing unreachable dead code, shadowed by isBrowserLoading
  // (see #2571, found while investigating this ticket) -- but the motion
  // classes are applied there too for correctness if that branch is ever
  // fixed to be reachable.
  const motionEnabled = isPlaying && !!artUrl;
  const artMotionClass = motionEnabled ? ` ${styles.artMotion}` : '';
  const artAnimatedClass = motionEnabled ? ` ${styles.albumArtAnimated}` : '';

  return (
    <div
      ref={ref}
      className={`${styles.container} ${hasWideEmbed ? styles.wide : styles.square}`}
      style={{
        backgroundColor: 'var(--bg-elevated)',
        boxShadow: '0 12px 48px var(--shadow-medium)',
      }}
    >
      {showBrowserOverlay ? (
        <div className={styles.overlayPlaceholder}>
          <button
            onClick={() => {
              if (window.viola && window.viola.hideBrowserOverlay) {
                window.viola.hideBrowserOverlay();
              }
            }}
            aria-label="Close browser overlay"
            className={styles.overlayCloseBtn}
            style={{ border: `1px solid ${THEME.colors.borderLight}` }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
              <line x1="18" y1="6" x2="6" y2="18"/>
              <line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
      ) : showVideoEmbed ? (
        <YouTubeEmbed
          key={isSpoke ? 'spoke-yt-embed' : nowPlaying.video_id}
          videoId={nowPlaying.video_id}
          iframeRef={iframeRef}
          isSpoke={isSpoke}
        />
      ) : showProviderEmbed ? (
        <ProviderEmbed key={embedUrl} embedUrl={embedUrl} embedType={embedType} providerName={providerName} />
      ) : isBrowserLoading ? (
        <div className={styles.loadingState} style={{ backgroundColor: 'var(--bg-elevated)' }}>
          <div className={styles.searchDot} />
          <div className={styles.searchText} style={{ color: THEME.colors.textMuted }}>
            Searching YouTube...
          </div>
        </div>
      ) : isSpotifyAlbumArt && artUrl ? (
        <div className={`${styles.albumArtContainer}${artMotionClass}`}>
          <img
            src={artUrl}
            alt="Album art"
            className={`${styles.albumArt}${artAnimatedClass}`}
            onError={(e) => { e.target.src = PLACEHOLDER_ART; }}
          />
          <div className={styles.albumArtOverlay}>
            <div className={styles.albumArtTitle}>
              {nowPlaying?.title || ''}
            </div>
            {nowPlaying?.artist && (
              <div className={styles.albumArtArtist}>
                {nowPlaying.artist}
              </div>
            )}
          </div>
          <div className={styles.spotifyBadge}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="#1DB954">
              <path d="M12 0C5.4 0 0 5.4 0 12s5.4 12 12 12 12-5.4 12-12S18.66 0 12 0zm5.521 17.34c-.24.359-.66.48-1.021.24-2.82-1.74-6.36-2.101-10.561-1.141-.418.122-.779-.179-.899-.539-.12-.421.18-.78.54-.9 4.56-1.021 8.52-.6 11.64 1.32.42.18.479.659.301 1.02zm1.44-3.3c-.301.42-.841.6-1.262.3-3.239-1.98-8.159-2.58-11.939-1.38-.479.12-1.02-.12-1.14-.6-.12-.48.12-1.021.6-1.141C9.6 9.9 15 10.561 18.72 12.84c.361.181.54.78.241 1.2zm.12-3.36C15.24 8.4 8.82 8.16 5.16 9.301c-.6.179-1.2-.181-1.38-.721-.18-.601.18-1.2.72-1.381 4.26-1.26 11.28-1.02 15.721 1.621.539.3.719 1.02.419 1.56-.299.421-1.02.599-1.559.3z"/>
            </svg>
            <span className={styles.spotifyLabel}>Spotify</span>
          </div>
        </div>
      ) : artUrl ? (
        // Any other provider that only gives us a thumbnail (not a Spotify or
        // browser "album_art" embed specifically) -- same static-tile gap, so
        // it gets the same motion treatment when playing.
        <div className={`${styles.albumArtContainer}${artMotionClass}`}>
          <img
            src={artUrl}
            alt="Album art"
            className={`${styles.albumArtFull}${artAnimatedClass}`}
            onError={(e) => { e.target.src = PLACEHOLDER_ART; }}
          />
        </div>
      ) : (
        <img
          src={PLACEHOLDER_ART}
          alt="Album art"
          className={styles.albumArtFull}
        />
      )}
    </div>
  );
});

MediaArea.displayName = 'MediaArea';

MediaArea.propTypes = {
  nowPlaying: PropTypes.object,
  isSpoke: PropTypes.bool,
  isPlaying: PropTypes.bool,
  browserOverlayVisible: PropTypes.bool,
  YouTubeEmbed: PropTypes.elementType.isRequired,
  ProviderEmbed: PropTypes.elementType.isRequired,
  iframeRef: PropTypes.object,
};

export default MediaArea;

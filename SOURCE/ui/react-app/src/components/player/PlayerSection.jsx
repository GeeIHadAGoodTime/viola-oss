/**
 * PlayerSection — The main music player area containing media display,
 * track info, progress bar, transport controls, rating, and volume.
 */
import PropTypes from 'prop-types';
import { THEME } from '../../config';
import ErrorBoundary from '../ErrorBoundary';
import { isDesktopApp } from '../../utils/runtimeSurface';
import MediaArea from './MediaArea';
import MusicCommandInput from './MusicCommandInput';
import ProgressBar from './ProgressBar';
import TransportButton from './TransportButton';
import RatingButton from './RatingButton';
import VolumeControl from './VolumeControl';
import { ShuffleIcon, PrevIcon, PauseIcon, PlayIcon, NextIcon, RepeatIcon } from './TransportIcons';
import styles from './PlayerSection.module.css';

const PlayerSection = ({
  nowPlaying,
  isPlaying,
  isSpoke,
  browserOverlayVisible,
  displayProgress,
  displayPosition,
  displayDuration,
  volume,
  shuffleOn,
  repeatMode,
  rating,
  canResumePlayback,
  // Handlers
  onPlayPause,
  onNext,
  onPrevious,
  onSeek,
  onVolumeChange,
  onShuffleToggle,
  onRepeatCycle,
  onRating,
  onSubmitText,
  // Refs
  mediaAreaRef,
  iframeRef,
  // Components passed in (module-scope to avoid remount)
  YouTubeEmbed,
  ProviderEmbed,
  playerState,
}) => {
  return (
    <ErrorBoundary name="Now Playing">
      <div className={`viola-player-section now-playing-container ${styles.section}`} data-testid="now-playing">
        {/* Album Art / Embedded Video / Provider Embed */}
        <MediaArea
          ref={mediaAreaRef}
          nowPlaying={nowPlaying}
          isSpoke={isSpoke}
          isPlaying={isPlaying}
          browserOverlayVisible={browserOverlayVisible}
          YouTubeEmbed={YouTubeEmbed}
          ProviderEmbed={ProviderEmbed}
          iframeRef={iframeRef}
        />

        {/* Right side: Track Info + Progress + Controls + Rating */}
        <div className={`track-panel ${styles.trackPanel}`}>
          {/* Track Info Row with Rating */}
          <div className={`track-info-row ${styles.trackInfoRow}`}>
            <div className={styles.trackTextCol}>
              <div className={`track-title ${styles.trackTitle}`} style={{ color: THEME.colors.textBright }}>
                {nowPlaying?.title || 'Nothing playing'}
              </div>
              <div className={`track-artist ${styles.trackArtist}`} style={{ color: THEME.colors.textMuted }}>
                {nowPlaying ? (nowPlaying.artist || '\u00A0') : 'Ask me to play something'}
              </div>
            </div>

            <div className={styles.ratingRow}>
              <RatingButton type="like" active={rating} disabled={!nowPlaying} onClick={() => onRating('liked')} />
              <RatingButton type="dislike" active={rating} disabled={!nowPlaying} onClick={() => onRating('disliked')} />
            </div>
          </div>

          {/* Typed music request box — browser/mobile/spoke ONLY (#1504). The
              desktop Qt shell already has global type-anywhere capture
              (SmartDisplay's document keydown handler: any printable key with
              no input focused enters typing mode in Viola's reply area), so
              this box would be redundant chrome that collides with the hero
              surface there. Touch users on the browser /app funnel have no
              type-anywhere equivalent (iOS needs a focusable input), so they
              keep this box. isDesktopApp() detects the Qt bridge
              (window.viola) the same way the rest of the codebase does — see
              utils/runtimeSurface.js. Submits through the same /v1/command
              path a typed chat/command uses. Placed high in the panel so it
              never collides with the persistent BottomRow voice bar on short
              phone viewports. */}
          {onSubmitText && !isDesktopApp() && <MusicCommandInput onSubmit={onSubmitText} />}

          {/* Progress Bar */}
          <ProgressBar
            position={displayPosition}
            duration={displayDuration}
            progress={displayProgress}
            onSeek={onSeek}
          />

          {/* Transport Controls */}
          <div className={styles.transportRow}>
            <TransportButton onClick={onShuffleToggle} active={shuffleOn} toggle ariaLabel="Shuffle tracks">
              <ShuffleIcon />
            </TransportButton>
            <div className={styles.spacer} />
            <TransportButton onClick={onPrevious} ariaLabel="Previous track"><PrevIcon /></TransportButton>
            <TransportButton
              onClick={onPlayPause}
              ariaLabel={isPlaying ? 'Pause' : 'Play'}
              disabled={!isPlaying && !canResumePlayback}
            >
              {isPlaying ? <PauseIcon /> : <PlayIcon />}
            </TransportButton>
            <TransportButton onClick={onNext} ariaLabel="Next track"><NextIcon /></TransportButton>
            <div className={styles.spacer} />
            <TransportButton onClick={onRepeatCycle} active={repeatMode !== 'off'} ariaLabel={`Repeat: ${repeatMode}`}>
              <RepeatIcon mode={repeatMode} />
            </TransportButton>
            <div className={styles.spacer} />
            <VolumeControl volume={volume} onVolumeChange={onVolumeChange} />
          </div>
        </div>
      </div>
    </ErrorBoundary>
  );
};

PlayerSection.propTypes = {
  nowPlaying: PropTypes.object,
  isPlaying: PropTypes.bool,
  isSpoke: PropTypes.bool,
  browserOverlayVisible: PropTypes.bool,
  displayProgress: PropTypes.number.isRequired,
  displayPosition: PropTypes.number,
  displayDuration: PropTypes.number,
  volume: PropTypes.number.isRequired,
  shuffleOn: PropTypes.bool.isRequired,
  repeatMode: PropTypes.string.isRequired,
  rating: PropTypes.string,
  canResumePlayback: PropTypes.bool,
  onPlayPause: PropTypes.func.isRequired,
  onNext: PropTypes.func.isRequired,
  onPrevious: PropTypes.func.isRequired,
  onSeek: PropTypes.func.isRequired,
  onVolumeChange: PropTypes.func.isRequired,
  onShuffleToggle: PropTypes.func.isRequired,
  onRepeatCycle: PropTypes.func.isRequired,
  onRating: PropTypes.func.isRequired,
  onSubmitText: PropTypes.func,
  mediaAreaRef: PropTypes.object,
  iframeRef: PropTypes.object,
  YouTubeEmbed: PropTypes.elementType.isRequired,
  ProviderEmbed: PropTypes.elementType.isRequired,
  playerState: PropTypes.object,
};

export default PlayerSection;

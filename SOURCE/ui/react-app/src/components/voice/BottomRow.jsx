/* eslint react/jsx-uses-vars: "error" */
/**
 * BottomRow — AI response area, PTT button, spoke chat, and connection status.
 */
import PropTypes from 'prop-types';
import { useTranslation } from 'react-i18next';
import { THEME } from '../../config';
import ErrorBoundary from '../ErrorBoundary';
import UsageCapNotice from '../UsageCapNotice';
import PTTButton from './PTTButton';
import { describeError } from '../../utils/describeError';
import styles from './BottomRow.module.css';
import '../../i18n';

// The little label under the push-to-talk button. `wakeStatus` is a state name
// SmartDisplay picks for its own logic, so it is mapped to words rather than
// printed: it used to fall through raw, putting "degraded" and "enabled" on
// screen. An unrecognised state shows nothing, never the state's name.
const WAKE_STATUS_LABELS = {
  recording: 'recording',
  processing: 'processing',
  wake_listening: 'listening',
  degraded: 'limited',
  enabled: 'on',
  off: 'off',
};

const BottomRow = ({
  // AI Response state
  isTyping,
  typingInput,
  setTypingInput,
  isCommandLoading,
  lastResponse,
  mainThinking,
  voice,
  processingTooLong,
  // Managed-AI usage cap (C-077): when the turn was denied for hitting the
  // plan allowance, the response text alone is a dead end, so the notice below
  // gives the user a route to the upgrade surface.
  capDenial,
  onUpgradeFromCap,
  // PTT
  wakeStatus,
  handlePTTStart,
  handlePTTEnd,
  handleMicIntent,
  // Connection
  connected,
}) => {
  const { t } = useTranslation();

  return (
    <div className={`viola-bottom-row ${styles.container}`}>
      {/* AI Response / Typing Input */}
      <ErrorBoundary name="AI Response">
        <div
          className={styles.responseArea}
          role={voice.error ? 'alert' : 'status'}
          aria-live="polite"
          aria-atomic="true"
          style={{ color: isTyping ? THEME.colors.textPrimary : THEME.colors.textTertiary }}
        >
          {isTyping ? (
            <span style={{ color: THEME.colors.textPrimary }}>
              {typingInput}
              <span className={styles.cursor} style={{ backgroundColor: THEME.colors.textSecondary }} />
            </span>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', minWidth: 0 }}>
              {mainThinking && (
                <span
                  style={{
                    color: THEME.colors.textMuted,
                    fontSize: '0.82em',
                    fontStyle: 'italic',
                    lineHeight: 1.35,
                    opacity: 0.7,
                    overflow: 'hidden',
                    whiteSpace: 'nowrap',
                    textOverflow: 'ellipsis',
                  }}
                >
                  {mainThinking}
                </span>
              )}
              {isCommandLoading ? (
                <span className={styles.loadingIndicator} style={{ color: `${THEME.colors.statusYellow}CC` }}>
                  <span
                    className={styles.loadingSpinner}
                    aria-hidden="true"
                    style={{
                      border: '2px solid rgba(255,255,255,0.15)',
                      borderTopColor: `${THEME.colors.statusYellow}CC`,
                    }}
                  />
                  {t('voice.status.processing')}
                </span>
              ) : voice.isProcessing ? (
                <span className={styles.thinkingText} style={{ color: `${THEME.colors.statusYellow}CC` }}>
                  {processingTooLong ? t('voice.status.taking_longer') : t('voice.status.thinking')}
                </span>
              ) : voice.error ? (
                /*
                  Rendered through describeError rather than raw. This area is
                  the only thing a user sees when a voice turn fails, so it must
                  survive whatever the failure hands it: the API's failure
                  envelope is an OBJECT (`{code, message}`), and React throws
                  "Objects are not valid as a React child" on one, which the
                  surrounding ErrorBoundary turned into "AI Response couldn't
                  load" — losing the reason exactly when the user needed it.
                  Normalising at the point of render means no upstream caller
                  can ever crash this widget again.
                */
                <span style={{ color: `${THEME.colors.statusRed}CC` }}>{describeError(voice.error)}</span>
              ) : (
                <span>{lastResponse}</span>
              )}
            </div>
          )}
          {/*
            Outside the typing/response branch on purpose: the route stays put
            for the whole time the cap is in force, including while the user is
            composing the next message and while that turn is in flight, so it
            never vanishes from under someone reaching for it. SmartDisplay
            clears capDenial as soon as a turn comes back uncapped.
          */}
          <UsageCapNotice capDenial={capDenial} onUpgrade={onUpgradeFromCap} />
        </div>
      </ErrorBoundary>

      {/* PTT Button */}
      {/*
        #385: a voice turn stays "busy" (guard active in useVoiceWs) from the
        press through STT/agent/TTS and teardown — LONGER than isProcessing,
        which clears the instant the answer text lands while TTS is still
        playing. Disable the button for that whole busy window (except while
        actively recording, so the user can still release/stop) so a rapid
        second press meets a visible busy state instead of being silently
        dropped by the sessionActiveRef guard. data-voice-busy exposes the same
        truth for deterministic external drivers (the latency/feature-drive
        voice legs) to wait on rather than estimating teardown timers.
      */}
      <div
        id="ptt-button"
        className={styles.pttContainer}
        data-voice-busy={voice.isBusy ? 'true' : 'false'}
        data-voice-recording={voice.isRecording ? 'true' : 'false'}
      >
        <PTTButton
          active={voice.isRecording}
          disabled={voice.isBusy && !voice.isRecording}
          wakeStatus={wakeStatus}
          onMouseDown={handlePTTStart}
          onMouseUp={handlePTTEnd}
          onIntent={handleMicIntent}
        />
        <span
          className={styles.pttLabel}
          style={{
            color: voice.isRecording ? THEME.colors.textSecondary : THEME.colors.textFaint,
          }}
        >
          {voice.isRecording
            ? 'recording'
            : voice.isProcessing
              ? 'processing'
              : voice.isBusy
                ? 'responding'
                : (WAKE_STATUS_LABELS[wakeStatus] || '')}
        </span>
      </div>

      {/* Connection status */}
      <div
        className={styles.connectionStatus}
        role="status"
        aria-live="polite"
        aria-label={connected ? t('voice.status.connected') : t('voice.status.disconnected')}
      >
        <div
          className={styles.statusDot}
          aria-hidden="true"
          style={{
            backgroundColor: connected ? THEME.colors.statusGreen : THEME.colors.statusRed,
          }}
        />
        <span className={styles.statusLabel} style={{ color: THEME.colors.textMuted }}>
          {connected ? t('voice.status.connected') : t('voice.status.disconnected')}
        </span>
      </div>
    </div>
  );
};

BottomRow.propTypes = {
  isTyping: PropTypes.bool.isRequired,
  typingInput: PropTypes.string.isRequired,
  setTypingInput: PropTypes.func.isRequired,
  isCommandLoading: PropTypes.bool.isRequired,
  lastResponse: PropTypes.string.isRequired,
  mainThinking: PropTypes.string,
  voice: PropTypes.object.isRequired,
  processingTooLong: PropTypes.bool,
  capDenial: PropTypes.shape({
    plan: PropTypes.string,
    period: PropTypes.string,
    resetsAt: PropTypes.string,
  }),
  onUpgradeFromCap: PropTypes.func,
  wakeStatus: PropTypes.string.isRequired,
  handlePTTStart: PropTypes.func.isRequired,
  handlePTTEnd: PropTypes.func.isRequired,
  handleMicIntent: PropTypes.func,
  connected: PropTypes.bool.isRequired,
};

BottomRow.defaultProps = {
  mainThinking: '',
  capDenial: null,
  onUpgradeFromCap: () => {},
};

export default BottomRow;

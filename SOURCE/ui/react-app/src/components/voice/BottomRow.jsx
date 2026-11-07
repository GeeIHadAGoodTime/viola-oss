/* eslint react/jsx-uses-vars: "error" */
/**
 * BottomRow — AI response area, PTT button, spoke chat, and connection status.
 */
import { useEffect, useId, useRef } from 'react';
import PropTypes from 'prop-types';
import { useVoiceNotice } from '../../hooks/useVoiceNotice';
import { useTranslation } from 'react-i18next';
import { THEME } from '../../config';
import ErrorBoundary from '../ErrorBoundary';
import UsageCapNotice from '../UsageCapNotice';
import PTTButton from './PTTButton';
import { describeError } from '../../utils/describeError';
import styles from './BottomRow.module.css';
import '../../i18n';
import { renderStageMarkdown } from '../../utils/stageMarkdown';

// The little label under the push-to-talk button. `wakeStatus` is a state name
// SmartDisplay picks for its own logic, so it is mapped to words rather than
// printed: it used to fall through raw, putting "degraded" and "enabled" on
// screen. An unrecognised state shows nothing, never the state's name.
const WAKE_STATUS_LABELS = {
  recording: 'recording',
  processing: 'processing',
  responding: 'responding',
  // Waiting for the wake word, not recording: a bare "listening" read as if
  // the microphone were capturing the room the whole time.
  wake_listening: 'listening for wake word',
  degraded: 'limited',
  enabled: 'configured',
  muted: 'muted',
  paused: 'paused',
  starting: 'starting',
  unavailable: 'unavailable',
  stopped: 'not listening',
  unknown: 'status unknown',
  off: 'off',
};

const BottomRow = ({
  // AI Response state
  isTyping,
  typingInput,
  setTypingInput,
  setIsTyping,
  typingFieldRef,
  typingMaxChars,
  isCommandLoading,
  lastResponse,
  liveReply,
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
  const micStatusId = useId();
  const voiceNotice = useVoiceNotice(voice, lastResponse, isCommandLoading);
  const responseAreaRef = useRef(null);

  // A reply longer than the line scrolls; keep the words being said in view.
  useEffect(() => {
    const area = responseAreaRef.current;
    if (liveReply && area) area.scrollTop = area.scrollHeight;
  }, [liveReply]);

  // Typing is finished (sent, cancelled or emptied): give the keys back to the
  // stage, so hotkeys and type-anywhere work again.
  useEffect(() => {
    if (!isTyping) typingFieldRef?.current?.blur();
  }, [isTyping, typingFieldRef]);

  return (
    <div className={`viola-bottom-row ${styles.container}`}>
      {/* AI Response / Typing Input */}
      <ErrorBoundary name="AI Response">
        <div
          ref={responseAreaRef}
          className={styles.responseArea}
          role={voiceNotice ? 'alert' : 'status'}
          aria-live={isTyping ? 'off' : 'polite'}
          aria-atomic="true"
          aria-busy={Boolean(liveReply)}
          style={{ color: isTyping ? THEME.colors.textPrimary : THEME.colors.textTertiary }}
        >
          {/*
            A real text field, always there. Type-anywhere used to draw the
            letters into a span, so screen readers and Windows UI Automation
            found no text box on the stage and Ctrl+A had nothing to select
            (1.0.5 build 9 report). Until typing starts it stays out of sight
            and out of the way of fingers, but Tab and assistive tech reach it.
          */}
          <input
            ref={typingFieldRef}
            type="text"
            className={isTyping ? styles.typingField : styles.typingFieldIdle}
            data-testid="stage-typing-field"
            value={typingInput}
            onChange={(event) => {
              setTypingInput(event.target.value);
              setIsTyping(event.target.value.length > 0);
            }}
            maxLength={typingMaxChars}
            placeholder={t('voice.stage_field')}
            aria-label={t('voice.stage_field')}
            autoComplete="off"
          />
          {isTyping ? null : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', minWidth: 0 }}>
              {liveReply ? (
                /* Ahead of every "working" state: the answer has started. */
                <span data-testid="live-reply">{renderStageMarkdown(`"${liveReply}"`)}</span>
              ) : isCommandLoading ? (
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
              ) : (
                <>
                  {/*
                    Rendered through describeError rather than raw. The API's
                    failure envelope is an OBJECT (`{code, message}`), and React
                    throws "Objects are not valid as a React child" on one,
                    which the ErrorBoundary turned into "AI Response couldn't
                    load". Normalising here means no upstream caller can crash
                    this widget.

                    The notice sits ABOVE the reply, never instead of it: it
                    used to replace the reply line and, with nothing to clear
                    it, hid every later reply for 70+ minutes (build 9, High 1).
                  */}
                  {voiceNotice && (
                    <span style={{ color: `${THEME.colors.statusRed}CC` }}>{describeError(voiceNotice)}</span>
                  )}
                  <span>{renderStageMarkdown(lastResponse)}</span>
                </>
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
          statusDescriptionId={micStatusId}
          onMouseDown={handlePTTStart}
          onMouseUp={handlePTTEnd}
          onIntent={handleMicIntent}
        />
        <span
          id={micStatusId}
          role="status"
          aria-label="Microphone status"
          aria-live="polite"
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
  setIsTyping: PropTypes.func,
  typingFieldRef: PropTypes.shape({ current: PropTypes.any }),
  typingMaxChars: PropTypes.number,
  isCommandLoading: PropTypes.bool.isRequired,
  lastResponse: PropTypes.string.isRequired,
  liveReply: PropTypes.string,
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
  liveReply: '',
  capDenial: null,
  onUpgradeFromCap: () => {},
};

export default BottomRow;

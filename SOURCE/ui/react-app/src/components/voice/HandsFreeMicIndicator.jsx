/**
 * HandsFreeMicIndicator — persistent, unmistakable hot-mic status for the
 * browser hands-free wake word. When hands-free is ON the microphone is
 * continuously captured IN THE TAB (audio never leaves the device until wake
 * fires), and this pill makes that state obvious at all times:
 *   - listening: pulsing red dot + "Hands-free: listening for 'Viola'"
 *   - loading:   amber dot + "Hands-free: starting..."
 *   - error:     red text with the failure reason
 * Renders nothing when hands-free is off (no capture, nothing to disclose).
 */
import PropTypes from 'prop-types';
import { THEME } from '../../config';

export default function HandsFreeMicIndicator({ status, error, theme = THEME }) {
  if (status === 'off') return null;

  const listening = status === 'listening';
  const failed = status === 'error';
  const dotColor = failed
    ? theme.colors.statusRed
    : listening
      ? theme.colors.statusRed
      : theme.colors.statusYellow;
  const label = failed
    ? `Hands-free unavailable: ${error || 'wake word failed to start'}`
    : listening
      ? 'Hands-free on — mic is listening for "Viola"'
      : 'Hands-free: starting…';

  return (
    <div
      data-testid="hands-free-indicator"
      data-wake-status={status}
      role="status"
      aria-live="polite"
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 8,
        padding: '4px 12px',
        margin: '0 auto 6px',
        width: 'fit-content',
        borderRadius: 999,
        border: `1px solid ${failed ? theme.colors.statusRed : theme.colors.borderSubtle}`,
        backgroundColor: theme.colors.bgElevated,
        fontSize: 12,
        color: failed ? theme.colors.statusRed : theme.colors.textSecondary,
      }}
    >
      <span
        aria-hidden="true"
        style={{
          width: 8,
          height: 8,
          borderRadius: '50%',
          backgroundColor: dotColor,
          animation: listening ? 'viola-hot-mic-pulse 1.6s ease-in-out infinite' : 'none',
        }}
      />
      <span>{label}</span>
      <style>{`
        @keyframes viola-hot-mic-pulse {
          0%, 100% { opacity: 1; transform: scale(1); }
          50% { opacity: 0.35; transform: scale(0.75); }
        }
      `}</style>
    </div>
  );
}

HandsFreeMicIndicator.propTypes = {
  status: PropTypes.oneOf(['off', 'loading', 'listening', 'error']).isRequired,
  error: PropTypes.string,
  theme: PropTypes.object,
};

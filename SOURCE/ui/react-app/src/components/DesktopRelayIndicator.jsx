import PropTypes from 'prop-types';
import { THEME } from '../config';

// =========================================================================
// Desktop Relay Indicator
//
// The cloud->desktop auto-link: when a cloud user also has Viola running on
// their desktop, the cloud forwards the whole turn to that desktop, which
// runs it on the user's own machine, files, and LLM key. This is the default
// behaviour -- there is no toggle -- but it must be OBSERVABLE.
//
// This badge surfaces that: when the most recent command result carried
// `executed_on === "desktop"`, it renders a small "Connected to <device>"
// pill so the user knows their turn ran on their own machine.
//
// Driven entirely by the command result envelope (`result.data.executed_on`
// / `result.data.device_name`). It renders nothing when the turn ran in the
// cloud, so it is safe to mount unconditionally.
// =========================================================================

const DESKTOP_ICON = (
  <svg
    width="13"
    height="13"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <rect x="2" y="3" width="20" height="14" rx="2" />
    <line x1="8" y1="21" x2="16" y2="21" />
    <line x1="12" y1="17" x2="12" y2="21" />
  </svg>
);

/**
 * Extract the relay device name from a command result envelope.
 *
 * The cloud dispatcher stamps `executed_on: "desktop"` and `device_name`
 * onto the dispatch payload, which `success_response` nests under `data`.
 * Returns the device name when the turn ran on the desktop, else `null`.
 */
export function relayDeviceFromResult(result) {
  if (!result || typeof result !== 'object') return null;
  const data = result.data && typeof result.data === 'object' ? result.data : result;
  if ((data.executed_on || result.executed_on) !== 'desktop') return null;
  const name = data.device_name || result.device_name;
  return typeof name === 'string' && name.trim() ? name.trim() : 'Your desktop';
}

const DesktopRelayIndicator = ({ deviceName }) => {
  if (!deviceName) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      title={`This request ran on ${deviceName}, using your own machine and resources.`}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '6px',
        padding: '4px 10px',
        borderRadius: '999px',
        backgroundColor: `${THEME.colors.bgElevated}E6`,
        border: `1px solid ${THEME.colors.borderLight}`,
        color: THEME.colors.textSecondary,
        fontSize: '11px',
        fontWeight: 500,
        lineHeight: 1.2,
        whiteSpace: 'nowrap',
        maxWidth: '100%',
        boxSizing: 'border-box',
      }}
    >
      <span style={{ color: THEME.colors.accent, display: 'flex', alignItems: 'center' }}>
        {DESKTOP_ICON}
      </span>
      <span
        style={{
          overflow: 'hidden',
          textOverflow: 'ellipsis',
        }}
      >
        Connected to {deviceName}
      </span>
    </div>
  );
};

DesktopRelayIndicator.propTypes = {
  // The desktop device name to show, or null/empty to render nothing.
  deviceName: PropTypes.string,
};

export default DesktopRelayIndicator;

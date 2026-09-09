import PropTypes from 'prop-types';
import { THEME } from '../config';
import { DESKTOP_ONLY_FEATURE_COPY } from '../utils/featureSurface';

// =========================================================================
// Desktop Upsell
//
// The cloud browser SPA is the funnel to the desktop app. Some features
// fundamentally need the user's own credentials (Google calendar) or local
// hardware (LAN speakers), or are desktop-shaped (device pairing, background
// agents). Those are gracefully hidden in the web build (see
// utils/featureSurface.js) and replaced with this small card that explains
// why and links to the desktop download.
//
// Frontend-only, reversible, no backend deletion — it IS the funnel.
// =========================================================================

const DESKTOP_DOWNLOAD_URL = 'https://useviola.com/download';

const DesktopUpsell = ({ feature, compact = false }) => {
  const copy = DESKTOP_ONLY_FEATURE_COPY[feature] || {
    title: 'This feature',
    reason: 'This feature lives in the desktop app.',
  };

  return (
    <div
      role="note"
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'flex-start',
        gap: compact ? '6px' : '10px',
        padding: compact ? '12px 14px' : '18px 20px',
        borderRadius: '14px',
        backgroundColor: THEME.colors.bgElevated,
        border: `1px solid ${THEME.colors.borderLight}`,
        color: THEME.colors.textSecondary,
        maxWidth: '420px',
      }}
    >
      <span
        style={{
          color: THEME.colors.textBright,
          fontSize: compact ? '13px' : '15px',
          fontWeight: 600,
        }}
      >
        {copy.title}
      </span>
      <span
        style={{
          fontSize: compact ? '12px' : '13px',
          lineHeight: 1.45,
          color: THEME.colors.textSecondary,
        }}
      >
        {copy.reason}
      </span>
      <a
        href={DESKTOP_DOWNLOAD_URL}
        target="_blank"
        rel="noopener noreferrer"
        style={{
          marginTop: '2px',
          display: 'inline-flex',
          alignItems: 'center',
          gap: '4px',
          color: THEME.colors.accent,
          fontSize: compact ? '12px' : '13px',
          fontWeight: 600,
          textDecoration: 'none',
        }}
      >
        Available in the desktop app
        <span aria-hidden="true">&rarr;</span>
      </a>
    </div>
  );
};

DesktopUpsell.propTypes = {
  // Feature key — one of DESKTOP_ONLY_FEATURES values (agents, onboarding, rooms).
  feature: PropTypes.string.isRequired,
  // Render a tighter variant for inline placement.
  compact: PropTypes.bool,
};

export default DesktopUpsell;

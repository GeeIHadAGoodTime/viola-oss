import React from 'react';
import PropTypes from 'prop-types';
import { useTranslation } from 'react-i18next';
import { THEME } from '../config';
import '../i18n';

/**
 * Small, unobtrusive skip button for voice onboarding.
 * Positioned in bottom-left corner with subtle styling.
 *
 * It is live on every onboarding phase, so it is the one guaranteed exit from
 * first run. Its label is translated like the rest of the overlay.
 */
export default function OnboardingSkip({ visible, onSkip }) {
  const { t } = useTranslation();

  return (
    <button
      onClick={onSkip}
      aria-label={t('onboarding.skip.aria_label')}
      aria-hidden={!visible}
      tabIndex={visible ? 0 : -1}
      style={{
        position: 'fixed',
        bottom: '16px',
        left: '16px',
        padding: '8px 12px',
        fontSize: '12px',
        fontWeight: 400,
        color: THEME.colors.textSecondary,
        backgroundColor: THEME.colors.shadowMedium,
        border: 'none',
        borderRadius: '6px',
        cursor: 'pointer',
        opacity: visible ? 1 : 0,
        pointerEvents: visible ? 'auto' : 'none',
        transition: 'opacity 0.3s ease, background-color 0.15s ease, color 0.15s ease',
        zIndex: 100,
      }}
      onMouseOver={(e) => {
        e.currentTarget.style.backgroundColor = THEME.colors.shadowHeavy;
        e.currentTarget.style.color = THEME.colors.textPrimary;
      }}
      onMouseOut={(e) => {
        e.currentTarget.style.backgroundColor = THEME.colors.shadowMedium;
        e.currentTarget.style.color = THEME.colors.textSecondary;
      }}
    >
      {t('onboarding.skip.label')}
    </button>
  );
}

OnboardingSkip.propTypes = {
  visible: PropTypes.bool.isRequired,
  onSkip: PropTypes.func.isRequired,
};

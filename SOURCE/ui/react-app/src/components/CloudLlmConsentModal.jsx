import React from 'react';
import PropTypes from 'prop-types';
import Modal, { primaryButtonStyle, secondaryButtonStyle } from './Modal';
import { THEME } from '../config';

/**
 * CloudLlmConsentModal — first-run prompt shown on the cloud browser surface
 * before a new user's first agent turn, whether they type it or speak it.
 *
 * Grants every consent `services/cloud_consent.py::can_execute_cloud_agent`
 * requires (`cloud_llm` AND `data_retention`), so the copy discloses both. It
 * used to say "Turn on Viola's voice" and grant `cloud_llm` alone, which left a
 * consenting new user still refused for `data_retention` and never appeared at
 * all for someone who typed instead of speaking (#362).
 *
 * Plain-language copy, no em dashes (brand rule).
 */
export default function CloudLlmConsentModal({ isOpen, saving, error, onConsent, onClose }) {
  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Turn on Viola's AI"
      footer={(
        <>
          <button type="button" style={secondaryButtonStyle} onClick={onClose} disabled={saving}>
            Not now
          </button>
          <button type="button" style={primaryButtonStyle} onClick={onConsent} disabled={saving}>
            {saving ? 'Turning on...' : 'Turn on Viola'}
          </button>
        </>
      )}
    >
      <div style={{ color: THEME.colors.textSecondary, fontSize: '15px', lineHeight: 1.6 }}>
        <p style={{ marginTop: 0 }}>
          To answer you, Viola sends what you type or say to its managed AI so it can
          understand your request and reply. Speech is turned into text and processed the
          same way.
        </p>
        <p>
          Viola also keeps the account data behind those replies, things like your
          conversation history and settings, so features like sync, history, and support
          controls work.
        </p>
        <p>
          Turning this on lets you start using Viola right away. You can turn either one off
          any time in Settings.
        </p>
        {error ? (
          <p style={{ color: THEME.colors.statusRed, marginBottom: 0 }}>
            Something went wrong turning on Viola's AI. Please try again.
          </p>
        ) : null}
      </div>
    </Modal>
  );
}

CloudLlmConsentModal.propTypes = {
  isOpen: PropTypes.bool.isRequired,
  saving: PropTypes.bool,
  error: PropTypes.object,
  onConsent: PropTypes.func.isRequired,
  onClose: PropTypes.func.isRequired,
};

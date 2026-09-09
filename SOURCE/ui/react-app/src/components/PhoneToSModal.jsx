import React, { useState } from 'react';
import PropTypes from 'prop-types';
import Modal, { primaryButtonStyle, secondaryButtonStyle } from './Modal';
import { THEME } from '../config';
import { authFetch } from '../hooks/useViolaApi';

export const PHONE_TOS_AUDIT_NOTICE = 'Phone calls placed through Viola are recorded and transcribed by default for your audit trail, so Viola is not a black box: you can review what it said and did on your behalf. By default, the called party hears at the start of every call that the call may be recorded and transcribed for your records. You can change call recording, transcript retention, and proactive AI announcement at any time in Phone Settings.';

export default function PhoneToSModal({ isOpen, onClose, onAccepted = null, payload = null }) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const message = payload?.message || 'Accept the Phone Calling Terms of Service before making calls.';

  const handleAccept = async () => {
    setSubmitting(true);
    setError('');
    try {
      const response = await authFetch('/v1/phone/accept-tos', { method: 'POST' });
      const json = await response.json().catch(() => null);
      if (!response.ok || json?.ok === false) {
        const detail = json?.error?.message || json?.message || 'Could not save acceptance. Try again.';
        throw new Error(detail);
      }
      if (onAccepted) onAccepted(json?.data || json || { accepted: true });
      onClose();
    } catch (err) {
      setError(err?.message || 'Could not save acceptance. Try again.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Phone Calling Terms"
      footer={(
        <>
          <button
            type="button"
            onClick={onClose}
            style={secondaryButtonStyle}
            disabled={submitting}
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleAccept}
            style={{
              ...primaryButtonStyle,
              opacity: submitting ? 0.72 : 1,
              cursor: submitting ? 'default' : 'pointer',
            }}
            disabled={submitting}
          >
            {submitting ? 'Saving...' : 'I agree'}
          </button>
        </>
      )}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        <p style={{ margin: 0, color: THEME.colors.textPrimary, fontSize: 15, lineHeight: 1.5 }}>
          {message}
        </p>
        <div
          style={{
            border: `1px solid ${THEME.colors.borderLight}`,
            borderRadius: 8,
            padding: 14,
            background: THEME.colors.glassBase,
            color: THEME.colors.textSecondary,
            fontSize: 13,
            lineHeight: 1.5,
          }}
        >
          <div
            aria-label="Phone auditability default"
            style={{
              margin: '0 0 14px',
              padding: '0 0 0 12px',
              borderLeft: `3px solid ${THEME.colors.accent}`,
            }}
          >
            <p style={{
              margin: 0,
              color: THEME.colors.textPrimary,
              fontSize: 14,
              fontWeight: 700,
              lineHeight: 1.4,
            }}>
              Auditability by default
            </p>
            <p style={{ margin: '6px 0 0', color: THEME.colors.textSecondary }}>
              {PHONE_TOS_AUDIT_NOTICE}
            </p>
          </div>
          <p style={{ margin: '0 0 10px' }}>
            You authorize Viola to place user-initiated phone calls on your behalf. You are the principal and responsible party for the purpose, content, and legality of each call you direct Viola to place.
          </p>
          <p style={{ margin: '0 0 10px' }}>
            Phone calling is for personal, non-commercial use only. You are responsible for call content, instructions, recipient selection, required consent, and compliance with applicable law.
          </p>
          <p style={{ margin: '0 0 8px' }}>
            You may not use Viola phone calling for:
          </p>
          <ul style={{ margin: '0 0 10px 18px', padding: 0 }}>
            <li>telemarketing, sales outreach, political calls, fundraising, debt collection, surveys, mass calling, or proactive calling;</li>
            <li>emergency services, harassment, unlawful calls, or calls where required consent has not been obtained;</li>
            <li>calls to numbers on the National Do Not Call Registry.</li>
          </ul>
          <p style={{ margin: '0 0 10px' }}>
            Calls are recorded and transcribed by default for your records. Recording, transcript retention, and proactive AI announcement are controlled by toggles in Phone Settings. If the called party asks, Viola identifies as an automated assistant. Viola will not claim to be human or impersonate you.
          </p>
          <p style={{ margin: '0 0 10px' }}>
            You agree to indemnify Viola for claims arising from calls you direct Viola to place, except to the extent caused by Viola's gross negligence, willful misconduct, or breach of applicable law.
          </p>
          <p style={{ margin: 0 }}>
            Viola's liability for phone calling is limited to the cap in the general Viola Terms of Service.
          </p>
        </div>
        {error && (
          <div
            role="alert"
            style={{ color: THEME.colors.statusRed, fontSize: 13, lineHeight: 1.4 }}
          >
            {error}
          </div>
        )}
      </div>
    </Modal>
  );
}

PhoneToSModal.propTypes = {
  isOpen: PropTypes.bool.isRequired,
  onClose: PropTypes.func.isRequired,
  onAccepted: PropTypes.func,
  payload: PropTypes.shape({
    message: PropTypes.string,
  }),
};

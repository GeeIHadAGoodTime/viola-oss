import { useEffect, useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import Modal, { primaryButtonStyle, secondaryButtonStyle } from './Modal';
import { THEME } from '../config';

const MAX_MESSAGE_CHARS = 1200;
// Length ceilings mirror the backend's closed WebBugReportContext schema
// (ui/api/routes/public.py) so the form never collects more than the server
// will accept. contact:200, steps:1000, expected:500, actual:500.
const MAX_CONTACT_CHARS = 200;
const MAX_STEPS_CHARS = 1000;
const MAX_EXPECTED_CHARS = 500;
const MAX_ACTUAL_CHARS = 500;

// Soft nudge threshold: below this we gently suggest more detail, but never
// block the send. A one-word report still goes through.
const SOFT_MIN_MESSAGE_CHARS = 25;

// App version SHOWN on the form. Injected at build time from core/constants.py
// (VIOLA_VERSION) via vite.config.js define. The exact string rendered in the
// "Sending:" line below is the exact string attached to the report -- nothing
// the user cannot see is populated.
const APP_VERSION = import.meta.env.VITE_VIOLA_VERSION || '';

// Best-effort friendly OS label from the browser, SHOWN to the user before
// send. Same value rendered is the value attached; no hidden fingerprinting.
function detectOS() {
  if (typeof navigator === 'undefined') return '';
  const platform = navigator.userAgentData?.platform;
  if (platform) return platform;
  const ua = navigator.userAgent || '';
  if (/Windows/.test(ua)) return 'Windows';
  if (/Mac OS X|Macintosh/.test(ua)) return 'macOS';
  if (/Android/.test(ua)) return 'Android';
  if (/iPhone|iPad|iPod/.test(ua)) return 'iOS';
  if (/Linux/.test(ua)) return 'Linux';
  return '';
}

export default function BugReportModal({ isOpen, onClose, onSubmit }) {
  const [message, setMessage] = useState('');
  const [contact, setContact] = useState('');
  const [steps, setSteps] = useState('');
  const [expected, setExpected] = useState('');
  const [actual, setActual] = useState('');
  const [includeScreenContext, setIncludeScreenContext] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  const osLabel = useMemo(() => detectOS(), []);
  const sendingLine = useMemo(() => {
    const version = APP_VERSION ? ` v${APP_VERSION}` : '';
    const os = osLabel ? ` on ${osLabel}` : '';
    return `Sending: Viola${version}${os}`;
  }, [osLabel]);

  useEffect(() => {
    if (!isOpen) return;
    setMessage('');
    setContact('');
    setSteps('');
    setExpected('');
    setActual('');
    setIncludeScreenContext(true);
    setSubmitting(false);
    setError('');
  }, [isOpen]);

  const handleSubmit = async () => {
    const trimmed = message.trim();
    if (!trimmed) {
      setError('Add a short description.');
      return;
    }

    setSubmitting(true);
    setError('');
    try {
      await onSubmit({
        message: trimmed.slice(0, MAX_MESSAGE_CHARS),
        includeScreenContext,
        contact: contact.trim().slice(0, MAX_CONTACT_CHARS),
        steps: steps.trim().slice(0, MAX_STEPS_CHARS),
        expected: expected.trim().slice(0, MAX_EXPECTED_CHARS),
        actual: actual.trim().slice(0, MAX_ACTUAL_CHARS),
        // Shown === sent: these two static-line values are passed up verbatim so
        // the report carries exactly what the user saw in "Sending:" above.
        appVersion: APP_VERSION,
        os: osLabel,
      });
    } catch (submitError) {
      setError(submitError?.message || 'Bug report failed.');
      setSubmitting(false);
    }
  };

  const labelStyle = {
    display: 'flex',
    flexDirection: 'column',
    gap: '6px',
    color: THEME.colors.textSecondary,
    fontSize: '14px',
  };
  const fieldBaseStyle = {
    width: '100%',
    boxSizing: 'border-box',
    borderRadius: '8px',
    border: `1px solid ${THEME.colors.borderHover}`,
    backgroundColor: THEME.colors.bgElevated,
    color: THEME.colors.textPrimary,
    padding: '10px 12px',
    font: 'inherit',
    lineHeight: 1.45,
  };
  const smallTextareaStyle = {
    ...fieldBaseStyle,
    minHeight: '46px',
    resize: 'vertical',
  };
  const optionalTag = (
    <span style={{ color: THEME.colors.textMuted, fontWeight: 400 }}>(optional)</span>
  );

  return (
    <Modal
      isOpen={isOpen}
      onClose={submitting ? () => {} : onClose}
      title="Report a bug"
      footer={(
        <>
          <button
            type="button"
            style={secondaryButtonStyle}
            onClick={onClose}
            disabled={submitting}
          >
            Cancel
          </button>
          <button
            type="button"
            style={primaryButtonStyle}
            onClick={handleSubmit}
            disabled={submitting}
          >
            {submitting ? 'Sending...' : 'Submit report'}
          </button>
        </>
      )}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
        <label htmlFor="bug-report-message" style={labelStyle}>
          What broke?
          <textarea
            id="bug-report-message"
            value={message}
            onChange={(event) => {
              setMessage(event.target.value.slice(0, MAX_MESSAGE_CHARS));
              if (error) setError('');
            }}
            maxLength={MAX_MESSAGE_CHARS}
            rows={5}
            autoFocus
            placeholder="What were you trying to do?"
            style={{ ...fieldBaseStyle, minHeight: '126px', resize: 'vertical', padding: '12px' }}
          />
          {message.trim().length > 0 && message.trim().length < SOFT_MIN_MESSAGE_CHARS && (
            <span style={{ color: THEME.colors.textTertiary, fontSize: '12px', lineHeight: 1.4 }}>
              A sentence or two helps us reproduce it.
            </span>
          )}
        </label>

        <label htmlFor="bug-report-steps" style={labelStyle}>
          <span>What were you doing? {optionalTag}</span>
          <textarea
            id="bug-report-steps"
            value={steps}
            onChange={(event) => setSteps(event.target.value.slice(0, MAX_STEPS_CHARS))}
            maxLength={MAX_STEPS_CHARS}
            rows={2}
            placeholder="The steps that led up to it"
            style={smallTextareaStyle}
          />
        </label>

        <label htmlFor="bug-report-expected" style={labelStyle}>
          <span>What did you expect? {optionalTag}</span>
          <textarea
            id="bug-report-expected"
            value={expected}
            onChange={(event) => setExpected(event.target.value.slice(0, MAX_EXPECTED_CHARS))}
            maxLength={MAX_EXPECTED_CHARS}
            rows={2}
            placeholder="What should have happened"
            style={smallTextareaStyle}
          />
        </label>

        <label htmlFor="bug-report-actual" style={labelStyle}>
          <span>What actually happened? {optionalTag}</span>
          <textarea
            id="bug-report-actual"
            value={actual}
            onChange={(event) => setActual(event.target.value.slice(0, MAX_ACTUAL_CHARS))}
            maxLength={MAX_ACTUAL_CHARS}
            rows={2}
            placeholder="What you saw instead"
            style={smallTextareaStyle}
          />
        </label>

        <label htmlFor="bug-report-contact" style={labelStyle}>
          <span>Email {optionalTag}</span>
          <input
            id="bug-report-contact"
            type="email"
            value={contact}
            onChange={(event) => setContact(event.target.value.slice(0, MAX_CONTACT_CHARS))}
            maxLength={MAX_CONTACT_CHARS}
            placeholder="So we can follow up. Leave blank to stay anonymous."
            autoComplete="email"
            style={fieldBaseStyle}
          />
        </label>

        <label
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '10px',
            color: THEME.colors.textSecondary,
            fontSize: '14px',
            minHeight: '44px',
          }}
        >
          <input
            type="checkbox"
            checked={includeScreenContext}
            onChange={(event) => setIncludeScreenContext(event.target.checked)}
          />
          Include current screen context
        </label>

        <div
          data-testid="bug-report-sending-line"
          style={{ color: THEME.colors.textTertiary, fontSize: '12px', lineHeight: 1.4 }}
        >
          {sendingLine}
        </div>

        {error && (
          <div
            role="alert"
            style={{
              color: THEME.colors.statusRed,
              fontSize: '13px',
              lineHeight: 1.4,
            }}
          >
            {error}
          </div>
        )}
      </div>
    </Modal>
  );
}

BugReportModal.propTypes = {
  isOpen: PropTypes.bool.isRequired,
  onClose: PropTypes.func.isRequired,
  onSubmit: PropTypes.func.isRequired,
};

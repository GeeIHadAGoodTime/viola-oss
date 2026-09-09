/**
 * CallConsultation — Modal for mid-call user consultation.
 *
 * Shows when Viola's phone AI needs user guidance during a call.
 * Displays the question and lets the user type an answer.
 */
import { useCallback, useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../config';

export default function CallConsultation({ consultation, onReply, onTakeover = () => {}, onDismiss }) {
  const [answer, setAnswer] = useState('');

  const handleSubmit = useCallback(
    (e) => {
      e.preventDefault();
      if (answer.trim()) {
        onReply(consultation.call_id, answer.trim());
        setAnswer('');
      }
    },
    [answer, consultation, onReply]
  );

  const handleTakeover = useCallback(() => {
    if (consultation?.call_id) {
      onTakeover(consultation.call_id);
    }
  }, [consultation, onTakeover]);

  if (!consultation) return null;

  return (
    <div
      role="dialog"
      aria-live="polite"
      style={{
        position: 'relative',
        width: 'min(420px, calc(100vw - 40px))',
        maxWidth: '100%',
        flexShrink: 0,
        alignSelf: 'center',
        margin: 12,
        backgroundColor: THEME.colors.bgElevated,
        border: `1px solid ${THEME.colors.statusYellow}66`,
        borderRadius: 12,
        padding: 16,
        zIndex: 9999,
        boxShadow: `0 18px 48px ${THEME.colors.shadowHeavy}, 0 0 0 1px ${THEME.colors.statusYellow}12`,
        color: THEME.colors.textPrimary,
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, marginBottom: 8 }}>
        <span style={{ color: THEME.colors.statusYellow, fontWeight: 700, fontSize: 13 }}>
          Viola needs your input
        </span>
        <button
          type="button"
          aria-label="Dismiss consultation"
          onClick={onDismiss}
          style={{
            width: 28,
            height: 28,
            borderRadius: 7,
            backgroundColor: THEME.colors.glassBase,
            border: `1px solid ${THEME.colors.borderSubtle}`,
            color: THEME.colors.textMuted,
            cursor: 'pointer',
            fontSize: 14,
            fontWeight: 800,
            lineHeight: 1,
          }}
        >
          x
        </button>
      </div>

      <p style={{ color: THEME.colors.textPrimary, fontSize: 14, lineHeight: 1.45, margin: '8px 0 12px' }}>
        {consultation.question}
      </p>

      {consultation.urgency === 'high' && (
        <div
          style={{
            backgroundColor: `${THEME.colors.statusYellow}18`,
            color: THEME.colors.statusYellow,
            border: `1px solid ${THEME.colors.statusYellow}44`,
            padding: '4px 8px',
            borderRadius: 7,
            fontSize: 11,
            fontWeight: 800,
            marginBottom: 8,
            display: 'inline-block',
          }}
        >
          Needs a quick answer
        </div>
      )}

      <form onSubmit={handleSubmit} style={{ display: 'flex', gap: 8, alignItems: 'stretch' }}>
        <input
          type="text"
          value={answer}
          onChange={(e) => setAnswer(e.target.value)}
          placeholder="Type what Viola should say..."
          autoFocus
          style={{
            flex: 1,
            minWidth: 0,
            padding: '9px 12px',
            borderRadius: 8,
            border: `1px solid ${THEME.colors.statusYellow}55`,
            backgroundColor: THEME.colors.bgCard,
            color: THEME.colors.textPrimary,
            fontSize: 14,
            outline: 'none',
            boxShadow: `0 0 0 3px ${THEME.colors.statusYellow}18`,
          }}
        />
        <button
          type="submit"
          disabled={!answer.trim()}
          style={{
            flexShrink: 0,
            padding: '9px 16px',
            borderRadius: 8,
            border: 'none',
            backgroundColor: answer.trim() ? THEME.colors.accent : THEME.colors.glassBase,
            color: answer.trim() ? '#fff' : THEME.colors.textMuted,
            fontWeight: 700,
            cursor: answer.trim() ? 'pointer' : 'default',
          }}
        >
          Send
        </button>
        <button
          type="button"
          onClick={handleTakeover}
          style={{
            flexShrink: 0,
            padding: '9px 12px',
            borderRadius: 8,
            border: `1px solid ${THEME.colors.borderLight}`,
            backgroundColor: THEME.colors.glassBase,
            color: THEME.colors.textPrimary,
            fontWeight: 700,
            cursor: 'pointer',
          }}
        >
          Take over
        </button>
      </form>
    </div>
  );
}

CallConsultation.propTypes = {
  consultation: PropTypes.shape({
    call_id: PropTypes.string,
    question: PropTypes.string,
    urgency: PropTypes.string,
  }),
  onReply: PropTypes.func.isRequired,
  onTakeover: PropTypes.func,
  onDismiss: PropTypes.func.isRequired,
};

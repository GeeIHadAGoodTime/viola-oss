import PropTypes from 'prop-types';
import { THEME } from '../config';

function formatDuration(seconds) {
  const totalSeconds = Math.max(0, Math.floor(Number(seconds) || 0));
  const minutes = Math.floor(totalSeconds / 60);
  const remainder = totalSeconds % 60;
  return `${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`;
}

export default function CallSummaryCard({ call, onDismiss, onViewTranscript }) {
  if (!call) return null;

  const outcome = call.outcome || call.status || 'Call ended';
  const summary = call.summary || call.error || 'No summary was saved for this call.';
  const duration = formatDuration(call.duration_seconds);
  const recipient = call.phone_number || call.phone || '';

  return (
    <aside
      data-testid="call-summary-card"
      aria-label="Call ended summary"
      style={{
        width: 'min(520px, calc(100% - 24px))',
        padding: '13px 14px',
        borderRadius: 8,
        backgroundColor: THEME.colors.bgElevated,
        border: `1px solid ${THEME.colors.borderHover}`,
        boxShadow: `0 18px 45px ${THEME.colors.shadowMedium}`,
        color: THEME.colors.textPrimary,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12 }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ color: THEME.colors.textMuted, fontSize: 12, fontWeight: 700, marginBottom: 3 }}>
            Call ended
          </div>
          <div style={{ color: THEME.colors.textBright, fontSize: 15, fontWeight: 750, overflowWrap: 'anywhere' }}>
            {outcome}
          </div>
        </div>
        <button
          type="button"
          aria-label="Dismiss call summary"
          onClick={onDismiss}
          style={{
            width: 30,
            height: 30,
            borderRadius: 8,
            border: `1px solid ${THEME.colors.borderSubtle}`,
            backgroundColor: THEME.colors.bgCard,
            color: THEME.colors.textMuted,
            cursor: 'pointer',
            flexShrink: 0,
            fontSize: 16,
            lineHeight: 1,
          }}
        >
          X
        </button>
      </div>

      <div style={{ marginTop: 8, color: THEME.colors.textPrimary, fontSize: 13, lineHeight: 1.4 }}>
        {summary}
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginTop: 11 }}>
        <span style={{ color: THEME.colors.textMuted, fontSize: 12, fontVariantNumeric: 'tabular-nums' }}>
          {duration}{recipient ? ` - ${recipient}` : ''}
        </span>
        <button
          type="button"
          onClick={onViewTranscript}
          style={{
            marginLeft: 'auto',
            padding: '8px 10px',
            borderRadius: 8,
            border: 'none',
            backgroundColor: THEME.colors.accent,
            color: '#fff',
            cursor: 'pointer',
            fontSize: 12,
            fontWeight: 750,
          }}
        >
          View transcript
        </button>
      </div>
    </aside>
  );
}

CallSummaryCard.propTypes = {
  call: PropTypes.shape({
    call_id: PropTypes.string,
    phone_number: PropTypes.string,
    phone: PropTypes.string,
    status: PropTypes.string,
    outcome: PropTypes.string,
    summary: PropTypes.string,
    error: PropTypes.string,
    duration_seconds: PropTypes.number,
  }),
  onDismiss: PropTypes.func.isRequired,
  onViewTranscript: PropTypes.func.isRequired,
};

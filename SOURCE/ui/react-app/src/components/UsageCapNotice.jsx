/** Preserve capped work and open the existing account billing surface. */
import PropTypes from 'prop-types';
import { THEME } from '../config';

/**
 * Render an allowance reset as a plain date, or '' when it cannot be read.
 * @param {string} resetsAt - ISO-8601 timestamp from the cap state.
 * @returns {string}
 */
export function formatResetHint(resetsAt) {
  if (typeof resetsAt !== 'string' || !resetsAt.trim()) return '';
  const parsed = new Date(resetsAt.trim());
  if (Number.isNaN(parsed.getTime())) return '';
  return parsed.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

const UsageCapNotice = ({ capDenial, onUpgrade }) => {
  if (!capDenial) return null;

  const resetHint = formatResetHint(capDenial.resetsAt);

  return (
    <div
      data-testid="usage-cap-notice"
      style={{
        display: 'flex',
        alignItems: 'center',
        flexWrap: 'wrap',
        gap: '10px',
        marginTop: '8px',
        fontSize: '13px',
      }}
    >
      <span>You’ve used your included managed usage. Add more to continue.</span>
      <button
        type="button"
        data-testid="usage-cap-upgrade"
        onClick={onUpgrade}
        style={{
          padding: '10px 16px',
          minHeight: '44px',
          borderRadius: '18px',
          border: `1px solid ${THEME.colors.accentBorder}`,
          backgroundColor: THEME.colors.accentSubtle,
          color: THEME.colors.textPrimary,
          fontSize: '13px',
          fontWeight: 600,
          cursor: 'pointer',
        }}
      >
        Add more usage
      </button>
      {resetHint && (
        <span style={{ color: THEME.colors.textMuted }}>
          {`Your allowance resets ${resetHint}.`}
        </span>
      )}
      <span style={{ color: THEME.colors.textSecondary }}>
        You can also use local models or your own provider key on their existing terms. Your work is preserved.
      </span>
    </div>
  );
};

UsageCapNotice.propTypes = {
  capDenial: PropTypes.shape({
    plan: PropTypes.string,
    period: PropTypes.string,
    resetsAt: PropTypes.string,
  }),
  onUpgrade: PropTypes.func.isRequired,
};

UsageCapNotice.defaultProps = {
  capDenial: null,
};

export default UsageCapNotice;

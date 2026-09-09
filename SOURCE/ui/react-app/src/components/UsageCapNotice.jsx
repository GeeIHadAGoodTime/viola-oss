/**
 * UsageCapNotice — the tappable way out of a managed-AI usage-cap denial.
 *
 * When the plan allowance runs out, Viola answers the turn with cap copy
 * instead of a real answer. That copy names upgrading, but until this notice
 * existed there was nothing to tap: the user was told to pay us and handed no
 * route to do it (candidate C-077).
 *
 * The affordances, in the order the Terms prioritise them:
 *   1. the extra-usage top-up — the Terms name this the PRIMARY option once a
 *      managed allowance is reached, so ExtraUsageTopUpButton renders first when
 *      the top-up is actually purchasable on this deployment (it renders nothing
 *      otherwise, so there is never a dead button) (#4215);
 *   2. "Upgrade your plan" — opens the account settings panel, where the plan
 *      upgrade / billing-portal machinery already lives (C-077).
 */
import PropTypes from 'prop-types';
import { THEME } from '../config';
import ExtraUsageTopUpButton from './ExtraUsageTopUpButton';

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
      <ExtraUsageTopUpButton />
      <button
        type="button"
        data-testid="usage-cap-upgrade"
        onClick={onUpgrade}
        style={{
          padding: '7px 16px',
          borderRadius: '18px',
          border: `1px solid ${THEME.colors.accentBorder}`,
          backgroundColor: THEME.colors.accentSubtle,
          color: THEME.colors.textPrimary,
          fontSize: '13px',
          fontWeight: 600,
          cursor: 'pointer',
        }}
      >
        Upgrade your plan
      </button>
      {resetHint && (
        <span style={{ color: THEME.colors.textMuted }}>
          {`Your allowance resets ${resetHint}.`}
        </span>
      )}
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

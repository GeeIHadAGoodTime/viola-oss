import PropTypes from 'prop-types';
import SettingRow from './SettingRow';
import Toggle from './Toggle';

/**
 * The Stage 2 wake check (`wake_verification_enabled`, on by default): a second
 * listen on this device before Viola answers to her name. A value that was never
 * saved counts as on, the same as the app's own default.
 */
const WakeDoubleCheckRow = ({ enabled, onChange }) => (
  <SettingRow
    title="Double-check the wake word"
    description="Viola listens once more, on this device, before she answers to her name. Fewer false wakes; turn it off only if she misses you too often."
  >
    <Toggle
      checked={enabled !== false}
      onChange={(v) => onChange(Boolean(v))}
      ariaLabel="Double-check the wake word"
    />
  </SettingRow>
);

WakeDoubleCheckRow.propTypes = {
  enabled: PropTypes.bool,
  onChange: PropTypes.func.isRequired,
};

export default WakeDoubleCheckRow;

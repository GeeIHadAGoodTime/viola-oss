import { useCallback } from 'react';
import PropTypes from 'prop-types';
import Slider from './Slider';
import {
  WAKE_SENSITIVITY_MAX_POSITION,
  positionToWakeThreshold,
  wakeSensitivityLabel,
  wakeThresholdToPosition,
} from '../../utils/wakeSensitivity';

/**
 * "Wake sensitivity" control. Shows named positions, saves the wake score
 * threshold (`wake_sensitivity`). Moving right ("More sensitive") saves a LOWER
 * threshold; moving left ("Fewer false wakes") saves a higher, stricter one.
 * A saved value is only rewritten when the user moves the control.
 */
const WakeSensitivitySlider = ({ threshold, onChange }) => {
  const position = wakeThresholdToPosition(threshold);
  const handleChange = useCallback(
    (nextPosition) => {
      if (Math.round(nextPosition) === position) return;
      onChange(positionToWakeThreshold(nextPosition));
    },
    [onChange, position],
  );

  return (
    <Slider
      label="Wake sensitivity"
      tooltip="Move toward More sensitive if Viola misses you when you say her name. Move toward Fewer false wakes if she wakes up when nobody called her."
      value={position}
      onChange={handleChange}
      min={0}
      max={WAKE_SENSITIVITY_MAX_POSITION}
      step={1}
      valueLabel={wakeSensitivityLabel(position)}
      minLabel="Fewer false wakes"
      maxLabel="More sensitive"
    />
  );
};

WakeSensitivitySlider.propTypes = {
  threshold: PropTypes.oneOfType([PropTypes.number, PropTypes.string]),
  onChange: PropTypes.func.isRequired,
};

export default WakeSensitivitySlider;

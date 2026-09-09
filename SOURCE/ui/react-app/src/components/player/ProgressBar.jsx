/**
 * ProgressBar — Click-to-seek track progress bar.
 */
import PropTypes from 'prop-types';
import { THEME } from '../../config';
import styles from './ProgressBar.module.css';

const ProgressBar = ({ position, duration, progress, onSeek }) => {
  const handleClick = (e) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    const percentage = clickX / rect.width;
    if (duration > 0) {
      const seekPosition = Math.floor(percentage * duration);
      onSeek(seekPosition);
    }
  };

  return (
    <div className={`progress-bar-container ${styles.container}`}>
      <div
        role="progressbar"
        aria-valuenow={position || 0}
        aria-valuemin={0}
        aria-valuemax={duration || 100}
        aria-label="Track progress"
        className={`progress-bar ${styles.track}`}
        style={{ backgroundColor: THEME.colors.glassHover }}
        onClick={handleClick}
      >
        <div
          className={`progress-bar-fill ${styles.fill}`}
          style={{
            width: `${progress}%`,
            backgroundColor: THEME.colors.textBright,
          }}
        />
      </div>
    </div>
  );
};

ProgressBar.propTypes = {
  position: PropTypes.number,
  duration: PropTypes.number,
  progress: PropTypes.number.isRequired,
  onSeek: PropTypes.func.isRequired,
};

export default ProgressBar;

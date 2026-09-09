/**
 * RatingButton — Like/dislike button with animated active state.
 */
import PropTypes from 'prop-types';
import { THEME } from '../../config';
import { HeartIcon, BrokenHeartIcon } from './TransportIcons';
import styles from './RatingButton.module.css';

const RatingButton = ({ type, active, disabled, onClick }) => {
  const isLike = type === 'like';
  const activeColor = isLike ? THEME.colors.statusGreen : THEME.colors.statusRed;
  const isActive = active === (isLike ? 'liked' : 'disliked');

  return (
    <button
      onClick={onClick}
      disabled={disabled}
      aria-label={isLike ? 'Like track' : 'Dislike track'}
      className={`${styles.button} ${isActive ? styles.active : ''} ${disabled ? styles.disabled : ''}`}
      style={{
        color: isActive ? activeColor : THEME.colors.textDisabled,
        transform: isActive ? 'scale(1.1)' : 'scale(1)',
      }}
    >
      {isLike
        ? <HeartIcon filled={isActive} color={activeColor} />
        : <BrokenHeartIcon filled={isActive} color={activeColor} />
      }
    </button>
  );
};

RatingButton.propTypes = {
  type: PropTypes.oneOf(['like', 'dislike']).isRequired,
  active: PropTypes.string,
  disabled: PropTypes.bool,
  onClick: PropTypes.func.isRequired,
};

export default RatingButton;

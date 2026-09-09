/**
 * LoadingSkeleton - Shown while waiting for backend connection.
 */
import PropTypes from 'prop-types';
import { THEME } from '../../config';
import styles from './LoadingSkeleton.module.css';

const LoadingSkeleton = ({
  title = 'Starting Viola',
  subtitle = 'Connecting to backend...',
  detail = '',
}) => (
  <div className={styles.container} style={{ backgroundColor: THEME.colors.bgVoid }}>
    <div className={styles.logoRing}>
      <img src='/static/react/viola_icon.png' alt='Viola' className={styles.logo} />
    </div>
    <div className={styles.textBlock}>
      <span className={styles.title} style={{ color: THEME.colors.textPrimary }}>
        {title}
      </span>
      <span className={styles.subtitle} style={{ color: THEME.colors.textMuted }}>
        {subtitle}
      </span>
      {detail && (
        <span className={styles.detail} style={{ color: THEME.colors.textMuted }}>
          {detail}
        </span>
      )}
    </div>
  </div>
);

LoadingSkeleton.propTypes = {
  title: PropTypes.string,
  subtitle: PropTypes.string,
  detail: PropTypes.string,
};

export default LoadingSkeleton;

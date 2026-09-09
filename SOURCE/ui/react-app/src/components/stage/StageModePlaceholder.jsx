import PropTypes from 'prop-types';
import styles from './StageModePlaceholder.module.css';

export default function StageModePlaceholder({
  label,
  title,
  detail,
}) {
  return (
    <div className={styles.placeholder} data-testid={`stage-placeholder-${label}`}>
      <div className={styles.badge}>{label}</div>
      <div className={styles.title}>{title}</div>
      {detail && <div className={styles.detail}>{detail}</div>}
    </div>
  );
}

StageModePlaceholder.propTypes = {
  label: PropTypes.string.isRequired,
  title: PropTypes.string.isRequired,
  detail: PropTypes.string,
};

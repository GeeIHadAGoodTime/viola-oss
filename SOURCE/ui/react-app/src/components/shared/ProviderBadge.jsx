/**
 * ProviderBadge — Small badge shown for browser-provider tracks.
 */
import { THEME } from '../../config';
import styles from './ProviderBadge.module.css';

const ProviderBadge = () => (
  <div className={styles.badge} style={{ border: `1px solid ${THEME.colors.borderSubtle}` }}>
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,0.7)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" />
      <line x1="2" y1="12" x2="22" y2="12" />
      <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
    </svg>
  </div>
);

export default ProviderBadge;

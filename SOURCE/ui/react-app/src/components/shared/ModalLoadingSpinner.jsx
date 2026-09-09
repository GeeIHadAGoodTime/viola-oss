/**
 * ModalLoadingSpinner — Full-screen spinner for lazy-loaded modals.
 *
 * Carries role="dialog" + aria-modal + aria-busy so the brief window between
 * a menu click and the lazy chunk resolving is discoverable as "a modal is
 * opening" rather than reading as an empty page to a DOM/a11y scan (#1422:
 * a scan that only looked for role=dialog during this transient window
 * reported "the modal never mounted" even though the mount was in flight).
 */
import styles from './ModalLoadingSpinner.module.css';

const ModalLoadingSpinner = () => (
  <div
    className={styles.overlay}
    role="dialog"
    aria-modal="true"
    aria-busy="true"
    aria-label="Loading"
    data-testid="modal-loading-spinner"
  >
    <div className={styles.spinner} data-essential-motion="spin-fast" />
  </div>
);

export default ModalLoadingSpinner;

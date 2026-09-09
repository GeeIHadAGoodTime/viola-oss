/**
 * SubtleDivider — Gradient horizontal divider line.
 */
import { THEME } from '../../config';

const SubtleDivider = () => (
  <div style={{
    width: '100%',
    height: '1px',
    background: `linear-gradient(90deg, transparent 0%, ${THEME.colors.divider} 15%, ${THEME.colors.divider} 85%, transparent 100%)`,
  }} />
);

export default SubtleDivider;

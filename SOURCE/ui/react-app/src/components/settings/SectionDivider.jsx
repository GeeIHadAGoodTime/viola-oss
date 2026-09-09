import React from 'react';
import { THEME } from '../../config';

const theme = THEME;

/**
 * A horizontal divider line used between rows inside a Section.
 */
const SectionDivider = React.memo(() => (
  <div style={{ height: '1px', backgroundColor: theme.colors.borderSubtle, margin: '0 20px' }} />
));

export default SectionDivider;

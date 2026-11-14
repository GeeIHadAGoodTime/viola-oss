import React, { createContext, useContext } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';

const theme = THEME;

// Settings options the owner switched to "Coming soon" from the cloud
// (core/feature_switches.py). The settings payload carries them as
// `coming_soon`; it is absent when nothing is switched off, and then every
// wrapper below renders its children exactly as before.
export const NO_COMING_SOON = Object.freeze([]);

export const ComingSoonContext = createContext(NO_COMING_SOON);

export function useComingSoon(option) {
  const options = useContext(ComingSoonContext);
  return Array.isArray(options) && options.includes(option);
}

export function ComingSoonLabel() {
  return (
    <span
      data-testid="coming-soon-label"
      style={{
        display: 'inline-block',
        padding: '2px 8px',
        borderRadius: '999px',
        border: `1px solid ${theme.colors.borderLight}`,
        color: theme.colors.textSecondary,
        fontSize: '11px',
        fontWeight: 600,
        lineHeight: 1.4,
        whiteSpace: 'nowrap',
      }}
    >
      Coming soon
    </span>
  );
}

/**
 * Marks one Settings option "Coming soon" while the owner has it switched off:
 * a label next to it, and its controls disabled (not hidden). Otherwise it
 * renders its children unchanged.
 */
export function ComingSoon({ option, children }) {
  const off = useComingSoon(option);
  if (!off) return children ?? null;
  return (
    <div
      data-coming-soon={option}
      onClickCapture={(event) => {
        event.preventDefault();
        event.stopPropagation();
      }}
    >
      <div style={{ padding: '12px 20px 0' }}>
        <ComingSoonLabel />
      </div>
      <fieldset
        disabled
        aria-disabled="true"
        style={{ border: 0, margin: 0, padding: 0, minWidth: 0, opacity: 0.5, pointerEvents: 'none' }}
      >
        {children}
      </fieldset>
    </div>
  );
}

ComingSoon.propTypes = {
  option: PropTypes.string.isRequired,
  children: PropTypes.node,
};

export default ComingSoon;

import React, { useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';
import { Icons } from './Icons';

const theme = THEME;

/**
 * A collapsible section that is collapsed by default.
 * Used for advanced or less-frequently-accessed settings groups.
 * @param {Object} props
 * @param {string} props.title - Section heading text (rendered uppercase)
 * @param {React.ReactNode} props.children - Section content (shown when expanded)
 */
const AdvancedSection = React.memo(({ title, children, defaultExpanded = false }) => {
  const [isExpanded, setIsExpanded] = useState(defaultExpanded);
  const [hovered, setHovered] = useState(false);

  useEffect(() => {
    if (defaultExpanded) {
      setIsExpanded(true);
    }
  }, [defaultExpanded]);

  return (
    <div style={{ marginBottom: '28px' }}>
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
        aria-expanded={isExpanded}
        aria-label={title}
        style={{
          width: '100%',
          minHeight: '44px',
          boxSizing: 'border-box',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '12px 20px',
          border: 'none',
          backgroundColor: hovered ? theme.colors.glassBase : 'transparent',
          cursor: 'pointer',
          borderRadius: '12px',
          transition: 'background-color 0.15s ease',
        }}
      >
        <span style={{
          fontSize: '11px',
          fontWeight: 600,
          textTransform: 'uppercase',
          letterSpacing: '1px',
          color: theme.colors.textMuted,
        }}>
          {title}
        </span>
        <div style={{
          transform: isExpanded ? 'rotate(180deg)' : 'rotate(0deg)',
          transition: 'transform 0.2s ease',
          color: theme.colors.textMuted,
        }}>
          <Icons.ChevronDown />
        </div>
      </button>
      {isExpanded && (
        <div style={{
          backgroundColor: theme.colors.bgElevated,
          borderRadius: '16px',
          border: `1px solid ${theme.colors.borderSubtle}`,
          overflow: 'hidden',
          marginTop: '8px',
        }}>
          {children}
        </div>
      )}
    </div>
  );
});

AdvancedSection.propTypes = {
  title: PropTypes.string.isRequired,
  children: PropTypes.node.isRequired,
  defaultExpanded: PropTypes.bool,
};

export default AdvancedSection;

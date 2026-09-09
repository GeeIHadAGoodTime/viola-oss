import React, { useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';

const theme = THEME;

/**
 * A setting row with icon, title, description, and a control slot (children).
 * @param {Object} props
 * @param {React.ElementType} [props.icon] - Optional icon component
 * @param {string} props.title - Setting title
 * @param {string} [props.description] - Optional description below title
 * @param {React.ReactNode} [props.children] - Control element (toggle, select, etc.)
 * @param {function} [props.onClick] - Optional click handler for the entire row
 * @param {string} [props.tooltip] - Tooltip text for the title
 */
const SettingRow = React.memo(({ icon: Icon, title, description, children, onClick, tooltip }) => {
  const [hovered, setHovered] = useState(false);

  return (
    <div
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '16px 20px',
        backgroundColor: hovered ? theme.colors.glassBase : 'transparent',
        borderRadius: '12px',
        cursor: onClick ? 'pointer' : 'default',
        transition: 'background-color 0.15s ease',
        gap: '16px',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '16px', flex: 1, minWidth: 0 }}>
        {Icon && (
          <div style={{ color: theme.colors.textMuted, flexShrink: 0 }}>
            <Icon />
          </div>
        )}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div
            style={{ color: theme.colors.textPrimary, fontSize: '15px', fontWeight: 500 }}
            title={tooltip}
          >
            {title}
          </div>
          {description && (
            <div style={{ color: theme.colors.textMuted, fontSize: '13px', marginTop: '2px' }}>{description}</div>
          )}
        </div>
      </div>
      {children}
    </div>
  );
});

SettingRow.propTypes = {
  icon: PropTypes.elementType,
  title: PropTypes.string.isRequired,
  description: PropTypes.string,
  children: PropTypes.node,
  onClick: PropTypes.func,
  tooltip: PropTypes.string,
};

export default SettingRow;

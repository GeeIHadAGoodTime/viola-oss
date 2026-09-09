import React from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';

const theme = THEME;

/**
 * A simple key-value display row for informational settings.
 * @param {Object} props
 * @param {string} props.label - Left-aligned label text
 * @param {string|number} props.value - Right-aligned value text
 * @param {string} [props.valueColor] - Optional custom color for the value
 */
const InfoRow = ({ label, value, valueColor }) => (
  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '8px' }}>
    <span style={{ color: theme.colors.textMuted, fontSize: '13px' }}>{label}</span>
    <span style={{ color: valueColor || theme.colors.textSecondary, fontSize: '13px' }}>{value}</span>
  </div>
);

InfoRow.propTypes = {
  label: PropTypes.string.isRequired,
  value: PropTypes.oneOfType([PropTypes.string, PropTypes.number]).isRequired,
  valueColor: PropTypes.string,
};

export default InfoRow;

import React from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';

const theme = THEME;

function installStyle() {
  return {
    padding: '8px 12px',
    borderRadius: '8px',
    border: 'none',
    backgroundColor: theme.colors.accent,
    color: '#fff',
    fontSize: '12px',
    fontWeight: 600,
    cursor: 'pointer',
    whiteSpace: 'nowrap',
  };
}

const SuggestedExtensions = React.memo(({ catalog, onInstall }) => (
  <div style={{ display: 'grid', gap: '10px' }}>
    <div>
      <div style={{ color: theme.colors.textPrimary, fontSize: '15px', fontWeight: 600 }}>Suggested</div>
      <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginTop: '2px' }}>
        Curated local install templates. No network catalog fetch.
      </div>
    </div>

    {catalog.length === 0 ? (
      <div style={{ color: theme.colors.textMuted, fontSize: '13px', padding: '10px 0' }}>
        No suggestions available.
      </div>
    ) : (
      catalog.map((entry) => (
        <div
          key={entry.id || entry.name}
          style={{
            display: 'grid',
            gridTemplateColumns: 'minmax(0, 1fr) auto',
            gap: '12px',
            padding: '12px',
            borderRadius: '8px',
            border: `1px solid ${theme.colors.borderSubtle}`,
            backgroundColor: theme.colors.bgSurface,
          }}
        >
          <div style={{ minWidth: 0 }}>
            <div style={{ color: theme.colors.textPrimary, fontSize: '14px', fontWeight: 600 }}>{entry.name}</div>
            <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginTop: '4px', lineHeight: 1.4 }}>
              {entry.description}
            </div>
            <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginTop: '6px' }}>
              {entry.command} {(entry.args || []).join(' ')}
            </div>
          </div>
          <button type="button" onClick={() => onInstall(entry)} style={installStyle()}>Install</button>
        </div>
      ))
    )}
  </div>
));

SuggestedExtensions.propTypes = {
  catalog: PropTypes.arrayOf(PropTypes.object).isRequired,
  onInstall: PropTypes.func.isRequired,
};

export default SuggestedExtensions;

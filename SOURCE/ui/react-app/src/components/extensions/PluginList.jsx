import React from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';

const theme = THEME;

function buttonStyle({ danger = false } = {}) {
  return {
    padding: '7px 10px',
    borderRadius: '8px',
    border: `1px solid ${danger ? theme.colors.statusRed : theme.colors.borderLight}`,
    backgroundColor: 'transparent',
    color: danger ? theme.colors.statusRed : theme.colors.textSecondary,
    fontSize: '12px',
    fontWeight: 500,
    cursor: 'pointer',
    whiteSpace: 'nowrap',
  };
}

const PluginList = React.memo(({ plugins, onEnable, onDisable, onReload, onOpenFolder }) => (
  <div style={{ display: 'grid', gap: '10px' }}>
    <div>
      <div style={{ color: theme.colors.textPrimary, fontSize: '15px', fontWeight: 600 }}>Plugins</div>
      <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginTop: '2px' }}>
        Built-in and user plugins loaded from the local registry.
      </div>
    </div>

    {plugins.length === 0 ? (
      <div style={{ color: theme.colors.textMuted, fontSize: '13px', padding: '10px 0' }}>
        No plugins loaded.
      </div>
    ) : (
      plugins.map((plugin) => {
        const enabled = plugin.enabled !== false;
        return (
          <div
            key={plugin.name}
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
              <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
                <span style={{ color: theme.colors.textPrimary, fontSize: '14px', fontWeight: 600 }}>{plugin.name}</span>
                <span style={{ color: enabled ? theme.colors.statusGreen : theme.colors.textMuted, fontSize: '12px' }}>
                  {enabled ? 'enabled' : 'disabled'}
                </span>
                {plugin.kind && <span style={{ color: theme.colors.textMuted, fontSize: '12px' }}>{plugin.kind}</span>}
              </div>
              {plugin.description && (
                <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginTop: '4px' }}>
                  {plugin.description}
                </div>
              )}
              <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginTop: '4px' }}>
                {plugin.tool_count || plugin.capabilities?.length || 0} tools
              </div>
            </div>
            <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
              <button type="button" onClick={() => (enabled ? onDisable(plugin.name) : onEnable(plugin.name))} style={buttonStyle()}>
                {enabled ? 'Disable' : 'Enable'}
              </button>
              <button type="button" onClick={() => onOpenFolder(plugin)} style={buttonStyle()}>Open folder</button>
              <button type="button" onClick={() => onReload(plugin.name)} style={buttonStyle()}>Reload</button>
            </div>
          </div>
        );
      })
    )}
  </div>
));

PluginList.propTypes = {
  plugins: PropTypes.arrayOf(PropTypes.shape({
    name: PropTypes.string.isRequired,
    description: PropTypes.string,
    enabled: PropTypes.bool,
    kind: PropTypes.string,
    tool_count: PropTypes.number,
    capabilities: PropTypes.array,
  })).isRequired,
  onEnable: PropTypes.func.isRequired,
  onDisable: PropTypes.func.isRequired,
  onReload: PropTypes.func.isRequired,
  onOpenFolder: PropTypes.func.isRequired,
};

export default PluginList;

import React from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';

const theme = THEME;

function actionStyle({ danger = false, primary = false } = {}) {
  return {
    padding: '7px 10px',
    borderRadius: '8px',
    border: primary ? 'none' : `1px solid ${danger ? theme.colors.statusRed : theme.colors.borderLight}`,
    backgroundColor: primary ? theme.colors.accent : 'transparent',
    color: primary ? '#fff' : danger ? theme.colors.statusRed : theme.colors.textSecondary,
    fontSize: '12px',
    fontWeight: primary ? 600 : 500,
    cursor: 'pointer',
    whiteSpace: 'nowrap',
  };
}

function statusColor(status) {
  const normalized = String(status || '').toLowerCase();
  if (normalized.includes('connected')) return theme.colors.statusGreen;
  if (normalized.includes('disabled')) return theme.colors.textMuted;
  return theme.colors.statusYellow;
}

const MCPServerList = React.memo(({ servers, onDisable, onReconnect, onRemove, onRegister }) => (
  <div style={{ display: 'grid', gap: '10px' }}>
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px', alignItems: 'center' }}>
      <div>
        <div style={{ color: theme.colors.textPrimary, fontSize: '15px', fontWeight: 600 }}>MCP Servers</div>
        <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginTop: '2px' }}>
          Connected tool servers available to Viola.
        </div>
      </div>
      <button type="button" onClick={onRegister} style={actionStyle({ primary: true })}>Register</button>
    </div>

    {servers.length === 0 ? (
      <div style={{ color: theme.colors.textMuted, fontSize: '13px', padding: '10px 0' }}>
        No MCP servers registered.
      </div>
    ) : (
      servers.map((server) => {
        const status = server.status || (server.enabled === false ? 'disabled' : 'disconnected');
        const connected = String(status).toLowerCase().includes('connected');
        return (
          <div
            key={server.name}
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
              <div style={{ color: theme.colors.textPrimary, fontSize: '14px', fontWeight: 600 }}>{server.name}</div>
              <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', marginTop: '4px', fontSize: '12px' }}>
                <span style={{ color: statusColor(status) }}>{status}</span>
                <span style={{ color: theme.colors.textMuted }}>{server.tool_count || 0} tools</span>
              </div>
            </div>
            <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
              {connected ? (
                <button type="button" onClick={() => onDisable(server.name)} style={actionStyle()}>Disable</button>
              ) : (
                <button type="button" onClick={() => onReconnect(server.name)} style={actionStyle()}>Reconnect</button>
              )}
              <button type="button" onClick={() => onRemove(server.name)} style={actionStyle({ danger: true })}>Remove</button>
            </div>
          </div>
        );
      })
    )}
  </div>
));

MCPServerList.propTypes = {
  servers: PropTypes.arrayOf(PropTypes.shape({
    name: PropTypes.string.isRequired,
    status: PropTypes.string,
    tool_count: PropTypes.number,
    enabled: PropTypes.bool,
  })).isRequired,
  onDisable: PropTypes.func.isRequired,
  onReconnect: PropTypes.func.isRequired,
  onRemove: PropTypes.func.isRequired,
  onRegister: PropTypes.func.isRequired,
};

export default MCPServerList;

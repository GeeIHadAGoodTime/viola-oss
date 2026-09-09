import React, { useState, useRef, useCallback } from 'react';
import PropTypes from 'prop-types';
import { useDeviceDiscovery } from '../hooks/useDeviceDiscovery';
import { THEME } from '../config';
import RoomCalibrationPanel from './RoomCalibrationPanel';

const theme = THEME;

// SVG Icons for device panel
const Icons = {
  Link: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>
      <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>
    </svg>
  ),
  Unlink: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <path d="M18.84 12.25l1.72-1.71a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>
      <path d="M5.16 11.75l-1.72 1.71a5 5 0 0 0 7.07 7.07l1.72-1.71"/>
      <line x1="2" y1="2" x2="22" y2="22"/>
    </svg>
  ),
  Edit: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
      <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
    </svg>
  ),
  Check: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
      <polyline points="20 6 9 17 4 12"/>
    </svg>
  ),
  X: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <line x1="18" y1="6" x2="6" y2="18"/>
      <line x1="6" y1="6" x2="18" y2="18"/>
    </svg>
  ),
  Speaker: () => (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <rect x="4" y="2" width="16" height="20" rx="2"/>
      <circle cx="12" cy="14" r="4"/>
      <circle cx="12" cy="6" r="1"/>
    </svg>
  ),
};

// SVG icon for calibration
const CalibrationIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
    <circle cx="12" cy="12" r="3"/>
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
  </svg>
);

// Individual device card
const DeviceCard = ({ device, isLocal, onConnect, onDisconnect, onRename, onCalibrate, isCalibrating }) => {
  const [editMode, setEditMode] = useState(null); // 'connect' | 'rename'
  const [editValue, setEditValue] = useState('');
  const [actionLoading, setActionLoading] = useState(false);
  const [hovered, setHovered] = useState(false);
  const [showConfirm, setShowConfirm] = useState(false);
  const [connectError, setConnectError] = useState('');
  const connectTimeoutRef = useRef(null);
  const abortRef = useRef(null);

  const startConnect = () => {
    setEditMode('connect');
    setEditValue(device.name || device.host || '');
    setConnectError('');
  };

  const startRename = () => {
    setEditMode('rename');
    setEditValue(device.name || '');
  };

  const cancelEdit = useCallback(() => {
    // Cancel any in-flight connection
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    if (connectTimeoutRef.current) {
      clearTimeout(connectTimeoutRef.current);
      connectTimeoutRef.current = null;
    }
    setActionLoading(false);
    setEditMode(null);
    setEditValue('');
    setConnectError('');
  }, []);

  const confirmEdit = async () => {
    if (!editValue.trim()) return;
    setActionLoading(true);
    setConnectError('');

    if (editMode === 'connect') {
      // Set up 30-second connection timeout
      connectTimeoutRef.current = setTimeout(() => {
        if (abortRef.current) abortRef.current.abort();
        setActionLoading(false);
        setConnectError("Couldn't add this room. Make sure it is on the same network and try again.");
      }, 30000);
      abortRef.current = new AbortController();
    }

    const isConnect = editMode === 'connect';
    try {
      let result;
      if (isConnect) {
        result = await onConnect(device.device_id, editValue.trim());
      } else {
        result = await onRename(device.device_id, editValue.trim());
      }
      if (result.ok) {
        cancelEdit();
      } else if (isConnect) {
        setConnectError(result.error || "Couldn't add this room. Make sure it is on the same network and try again.");
        setActionLoading(false);
      } else {
        setActionLoading(false);
      }
    } catch (err) {
      if (err?.name !== 'AbortError') {
        setConnectError(err?.message || "Couldn't add this room. Make sure it is on the same network and try again.");
        setActionLoading(false);
      }
    } finally {
      if (connectTimeoutRef.current) {
        clearTimeout(connectTimeoutRef.current);
        connectTimeoutRef.current = null;
      }
    }
  };

  const handleDisconnect = () => setShowConfirm(true);

  const handleKeyDown = (e) => {
    if (e.key === 'Enter') confirmEdit();
    if (e.key === 'Escape') cancelEdit();
  };

  const statusColor = isLocal || device.is_connected
    ? theme.colors.statusGreen
    : theme.colors.textMuted;

  const statusText = isLocal
    ? 'Primary'
    : device.is_connected
      ? 'Connected'
      : 'Available';

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        backgroundColor: hovered ? theme.colors.bgElevated : 'transparent',
        borderRadius: '16px',
        border: `1px solid ${theme.colors.borderSubtle}`,
        padding: '16px 20px',
        marginBottom: '12px',
        transition: 'background-color 0.15s ease',
      }}
    >
      {/* Device info row */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
        <div style={{
          width: '10px',
          height: '10px',
          borderRadius: '50%',
          backgroundColor: statusColor,
          flexShrink: 0,
        }} />
        <div style={{
          color: isLocal || device.is_connected
            ? theme.colors.textPrimary
            : theme.colors.textSecondary,
        }}>
          <Icons.Speaker />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{
            color: theme.colors.textPrimary,
            fontSize: '15px',
            fontWeight: 500,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}>
            {device.name || device.device_id}
            {isLocal && (
              <span style={{
                color: theme.colors.textMuted,
                fontSize: '13px',
                fontWeight: 400,
                marginLeft: '8px',
              }}>
                (this device)
              </span>
            )}
          </div>
          <div style={{
            color: theme.colors.textMuted,
            fontSize: '13px',
            marginTop: '2px',
          }}>
            {!isLocal && device.host && !/^\d+\.\d+\.\d+\.\d+$/.test(device.host) ? `${device.host} · ` : ''}{statusText}
          </div>
        </div>
      </div>

      {/* Inline edit input (connect or rename) */}
      {editMode && (
        <div style={{ marginTop: '12px' }}>
          <div style={{
            display: 'flex',
            gap: '8px',
            alignItems: 'center',
          }}>
            <input
              type="text"
              value={editValue}
              onChange={(e) => setEditValue(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={editMode === 'connect' ? 'Room name' : 'New name'}
              autoFocus
              disabled={actionLoading}
              style={{
                flex: 1,
                padding: '10px 14px',
                borderRadius: '10px',
                border: `1px solid ${theme.colors.borderLight}`,
                backgroundColor: theme.colors.bgCard,
                color: theme.colors.textPrimary,
                fontSize: '14px',
                outline: 'none',
                boxSizing: 'border-box',
                opacity: actionLoading ? 0.6 : 1,
              }}
            />
            {!actionLoading && (
              <button
                onClick={confirmEdit}
                disabled={!editValue.trim()}
                style={{
                  padding: '8px 12px',
                  borderRadius: '8px',
                  border: 'none',
                  backgroundColor: theme.colors.accent,
                  color: 'white',
                  cursor: !editValue.trim() ? 'not-allowed' : 'pointer',
                  opacity: !editValue.trim() ? 0.5 : 1,
                  display: 'flex',
                  alignItems: 'center',
                  gap: '4px',
                  fontSize: '13px',
                }}
              >
                <Icons.Check />
                OK
              </button>
            )}
            {/* Cancel button — always visible; during connect shows 'Cancel' text */}
            <button
              onClick={cancelEdit}
              style={{
                padding: actionLoading ? '8px 12px' : '8px',
                borderRadius: '8px',
                border: `1px solid ${theme.colors.borderLight}`,
                backgroundColor: 'transparent',
                color: theme.colors.textSecondary,
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                gap: '4px',
                fontSize: '13px',
              }}
            >
              {actionLoading && editMode === 'connect' ? 'Cancel' : <Icons.X />}
            </button>
          </div>
          {/* Connecting... indicator and timeout error */}
          {actionLoading && editMode === 'connect' && !connectError && (
            <div style={{ marginTop: '6px', fontSize: '12px', color: theme.colors.textMuted }}>
              Connecting...
            </div>
          )}
          {connectError && (
            <div style={{ marginTop: '6px', fontSize: '12px', color: theme.colors.statusRed }}>
              {connectError}
            </div>
          )}
        </div>
      )}

      {/* Action buttons (hidden during edit or for local device) */}
      {!editMode && !isLocal && (
        <div style={{ marginTop: '12px', paddingLeft: '22px' }}>
          {device.is_connected ? (
            <>
              <div style={{ display: 'flex', gap: '8px' }}>
              <button
                onClick={startRename}
                style={{
                  padding: '6px 14px',
                  borderRadius: '8px',
                  border: `1px solid ${theme.colors.borderLight}`,
                  backgroundColor: 'transparent',
                  color: theme.colors.textSecondary,
                  fontSize: '13px',
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                }}
              >
                <Icons.Edit />
                Rename
              </button>
              <button
                onClick={() => onCalibrate && onCalibrate(device.device_id)}
                style={{
                  padding: '6px 14px',
                  borderRadius: '8px',
                  border: `1px solid ${isCalibrating ? theme.colors.accent + '60' : theme.colors.borderLight}`,
                  backgroundColor: isCalibrating ? theme.colors.accent + '15' : 'transparent',
                  color: isCalibrating ? theme.colors.accent : theme.colors.textSecondary,
                  fontSize: '13px',
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                }}
              >
                <CalibrationIcon />
                Calibrate
              </button>
              <button
                onClick={handleDisconnect}
                style={{
                  padding: '6px 14px',
                  borderRadius: '8px',
                  border: `1px solid ${theme.colors.statusRed}40`,
                  backgroundColor: 'transparent',
                  color: theme.colors.statusRed,
                  fontSize: '13px',
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                }}
              >
                <Icons.Unlink />
                Disconnect
              </button>
              </div>
              {showConfirm && (
                <div style={{ marginTop: '8px', paddingLeft: '22px', display: 'flex', alignItems: 'center', gap: '8px' }}>
                  <span style={{ fontSize: '13px', color: theme.colors.textMuted }}>Remove this room?</span>
                  <button
                    onClick={() => { onDisconnect(device.device_id); setShowConfirm(false); }}
                    style={{ padding: '4px 12px', borderRadius: '6px', border: 'none', backgroundColor: theme.colors.statusRed, color: '#fff', fontSize: '12px', cursor: 'pointer', fontWeight: 500 }}
                  >
                    Remove
                  </button>
                  <button
                    onClick={() => setShowConfirm(false)}
                    style={{ padding: '4px 12px', borderRadius: '6px', border: `1px solid ${theme.colors.borderLight}`, backgroundColor: 'transparent', color: theme.colors.textSecondary, fontSize: '12px', cursor: 'pointer' }}
                  >
                    Cancel
                  </button>
                </div>
              )}
            </>
          ) : (
            <button
              onClick={startConnect}
              style={{
                padding: '6px 14px',
                borderRadius: '8px',
                border: 'none',
                backgroundColor: theme.colors.accentSubtle,
                color: theme.colors.accent,
                fontSize: '13px',
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
              }}
            >
              <Icons.Link />
              Connect
            </button>
          )}
        </div>
      )}

      {/* Inline calibration panel */}
      {isCalibrating && (
        <RoomCalibrationPanel
          roomId={device.device_id}
          onClose={() => onCalibrate && onCalibrate(null)}
        />
      )}
    </div>
  );
};

DeviceCard.propTypes = {
  device: PropTypes.shape({
    device_id: PropTypes.string.isRequired,
    name: PropTypes.string,
    host: PropTypes.string,
    port: PropTypes.number,
    is_connected: PropTypes.bool,
  }).isRequired,
  isLocal: PropTypes.bool,
  onConnect: PropTypes.func.isRequired,
  onDisconnect: PropTypes.func.isRequired,
  onRename: PropTypes.func.isRequired,
  onCalibrate: PropTypes.func,
  isCalibrating: PropTypes.bool,
};

/**
 * Device discovery panel content for the Rooms modal.
 *
 * Shows the local device, discovered network devices, and
 * provides connect/disconnect/rename actions.
 */
export default function DeviceDiscoveryPanel({ localDevice, enabled }) {
  const [calibratingRoom, setCalibratingRoom] = useState(null);

  const {
    devices,
    loading,
    error,
    connectDevice,
    disconnectDevice,
    renameRoom,
    refresh,
    clearError,
  } = useDeviceDiscovery(enabled);

  const handleCalibrate = (deviceId) => {
    // Toggle: if already calibrating this room, close it; otherwise open it
    setCalibratingRoom((prev) => (prev === deviceId ? null : deviceId));
  };

  // No-op handlers for local device (no connect/disconnect/rename)
  const noop = () => ({ ok: false });

  if (loading && devices.length === 0) {
    return (
      <div style={{ textAlign: 'center', color: theme.colors.textMuted, padding: '40px' }}>
        Scanning for rooms...
      </div>
    );
  }

  return (
    <>
      {/* Error banner */}
      {error && (
        <div style={{
          padding: '12px 16px',
          backgroundColor: theme.colors.statusRed + '20',
          color: theme.colors.statusRed,
          borderRadius: '10px',
          marginBottom: '16px',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          gap: '8px',
        }}>
          <span style={{ flex: 1 }}>{error}</span>
          <button
            onClick={() => { refresh(); clearError(); }}
            style={{ background: 'none', border: `1px solid ${theme.colors.statusRed}60`, borderRadius: '6px', color: 'inherit', cursor: 'pointer', padding: '4px 10px', fontSize: '13px', fontWeight: 500 }}
          >
            Try again
          </button>
          <button
            onClick={clearError}
            style={{ background: 'none', border: 'none', color: 'inherit', cursor: 'pointer', padding: '4px 8px' }}
          >
            Dismiss
          </button>
        </div>
      )}

      {/* Scanning indicator — hidden when error is active */}
      {!error && <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: '8px',
        marginBottom: '16px',
        color: theme.colors.textMuted,
        fontSize: '13px',
      }}>
        <div
          data-essential-motion="pulse-slow"
          style={{
            width: '6px',
            height: '6px',
            borderRadius: '50%',
            backgroundColor: theme.colors.statusGreen,
            animation: 'devicePulse 2s ease-in-out infinite',
          }}
        />
        Scanning for rooms...
        <style>{`
          @keyframes devicePulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
          }
        `}</style>
      </div>}

      {/* Local device (always first) */}
      {localDevice && (
        <DeviceCard
          device={{
            device_id: localDevice.device_id || localDevice.id,
            name: localDevice.name,
            host: null,
            is_connected: true,
          }}
          isLocal
          onConnect={noop}
          onDisconnect={noop}
          onRename={noop}
        />
      )}

      {/* Discovered remote devices */}
      {devices.map((device) => (
        <DeviceCard
          key={device.device_id}
          device={device}
          onConnect={connectDevice}
          onDisconnect={disconnectDevice}
          onRename={renameRoom}
          onCalibrate={handleCalibrate}
          isCalibrating={calibratingRoom === device.device_id}
        />
      ))}

      {/* Empty state (no remote devices found) */}
      {devices.length === 0 && !loading && (
        <div style={{ textAlign: 'center', padding: '40px 20px' }}>
          <div style={{ color: theme.colors.textMuted, fontSize: '14px', marginBottom: '8px' }}>
            No other Viola rooms found
          </div>
          <div style={{ color: theme.colors.textMuted, fontSize: '13px' }}>
            Make sure the other device is powered on and on the same network.
          </div>
        </div>
      )}
    </>
  );
}

DeviceDiscoveryPanel.propTypes = {
  localDevice: PropTypes.shape({
    id: PropTypes.string,
    device_id: PropTypes.string,
    name: PropTypes.string,
  }),
  enabled: PropTypes.bool.isRequired,
};

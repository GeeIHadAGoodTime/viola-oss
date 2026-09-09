import React, { useState, useEffect, useId } from 'react';
import PropTypes from 'prop-types';
import { useRoomGroups } from '../hooks/useRoomGroups';
import { useOptimisticSliderValue } from '../hooks/useOptimisticSliderValue';
import DeviceDiscoveryPanel from './DeviceDiscoveryPanel';
import ConnectSpeakerPanel from './ConnectSpeakerPanel';
import DesktopUpsell from './DesktopUpsell';
import { isFeatureHidden } from '../utils/featureSurface';
import { THEME } from '../config';

const theme = THEME;

const formatRoomIdLabel = (roomId = '') => roomId
  .replace(/_/g, ' ')
  .replace(/\b\w/g, (char) => char.toUpperCase());

const getRoomLabel = (room = {}) => (
  room.name
  || room.display_name
  || room.label
  || room.room_name
  || formatRoomIdLabel(room.room_id || room.id || '')
);

// Icons
const Icons = {
  Close: () => (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
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
  Plus: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <line x1="12" y1="5" x2="12" y2="19"/>
      <line x1="5" y1="12" x2="19" y2="12"/>
    </svg>
  ),
  Trash: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <polyline points="3 6 5 6 21 6"/>
      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
    </svg>
  ),
  VolumeMute: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
      <line x1="23" y1="9" x2="17" y2="15"/>
      <line x1="17" y1="9" x2="23" y2="15"/>
    </svg>
  ),
  Volume: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
      <path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>
      <path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>
    </svg>
  ),
};

// Slider component for volume control.
//
// `onCommit` (optional) fires the moment the drag ends, so the settled value
// goes out at release instead of waiting out the caller's debounce window.
const VolumeSlider = ({ value, onChange, onCommit, min = 0, max = 100, label, showValue = true, ariaLabel }) => {
  const percentage = ((value - min) / (max - min)) * 100;

  return (
    <div style={{ width: '100%' }}>
      {label && (
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '6px' }}>
          <span style={{ color: theme.colors.textSecondary, fontSize: '13px' }}>{label}</span>
          {showValue && (
            <span style={{ color: theme.colors.textPrimary, fontSize: '13px', fontWeight: 500 }}>
              {value > 0 ? `+${value}` : value}
            </span>
          )}
        </div>
      )}
      <input
        type="range"
        min={min}
        max={max}
        value={value}
        onChange={(e) => onChange(parseInt(e.target.value))}
        onPointerUp={onCommit}
        onKeyUp={onCommit}
        onBlur={onCommit}
        aria-label={ariaLabel || label || 'Volume'}
        style={{
          width: '100%',
          height: '4px',
          borderRadius: '2px',
          background: `linear-gradient(to right, ${theme.colors.accent} ${percentage}%, ${theme.colors.glassActive} ${percentage}%)`,
          appearance: 'none',
          cursor: 'pointer',
        }}
      />
    </div>
  );
};

VolumeSlider.propTypes = {
  value: PropTypes.number.isRequired,
  onChange: PropTypes.func.isRequired,
  onCommit: PropTypes.func,
  min: PropTypes.number,
  max: PropTypes.number,
  label: PropTypes.string,
  showValue: PropTypes.bool,
  ariaLabel: PropTypes.string,
};

// Room member item in a group.
//
// The offset slider used to be bound straight to `member.volume_offset`, which
// comes off the server via useRoomGroups, so it could not move until a write
// came back and any response landing mid-drag snapped it back (#3003, same
// class as #2772). It now renders the user's own value and writes once.
export const RoomMemberItem = ({ member, onVolumeChange, onMuteToggle }) => {
  const [hovered, setHovered] = useState(false);
  const [offset, setOffset, commitOffset] = useOptimisticSliderValue(
    member.volume_offset ?? 0,
    onVolumeChange,
  );

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        padding: '12px 16px',
        backgroundColor: hovered ? theme.colors.glassBase : 'transparent',
        borderRadius: '10px',
        marginBottom: '8px',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <div style={{ color: member.is_muted ? theme.colors.textMuted : theme.colors.textPrimary }}>
            <Icons.Speaker />
          </div>
          <span style={{
            color: member.is_muted ? theme.colors.textMuted : theme.colors.textPrimary,
            fontSize: '14px',
            textDecoration: member.is_muted ? 'line-through' : 'none',
          }}>
            {getRoomLabel(member)}
          </span>
        </div>
        <button
          onClick={() => onMuteToggle(!member.is_muted)}
          style={{
            padding: '6px',
            borderRadius: '6px',
            border: 'none',
            backgroundColor: member.is_muted ? theme.colors.statusRed + '20' : 'transparent',
            color: member.is_muted ? theme.colors.statusRed : theme.colors.textMuted,
            cursor: 'pointer',
          }}
          title={member.is_muted ? 'Unmute' : 'Mute'}
        >
          {member.is_muted ? <Icons.VolumeMute /> : <Icons.Volume />}
        </button>
      </div>
      <VolumeSlider
        value={offset}
        onChange={setOffset}
        onCommit={commitOffset}
        min={-50}
        max={50}
        label="Volume Offset"
        ariaLabel={`${getRoomLabel(member)} volume offset`}
      />
    </div>
  );
};

RoomMemberItem.propTypes = {
  member: PropTypes.shape({
    room_id: PropTypes.string.isRequired,
    name: PropTypes.string,
    display_name: PropTypes.string,
    label: PropTypes.string,
    room_name: PropTypes.string,
    volume_offset: PropTypes.number,
    is_muted: PropTypes.bool,
  }).isRequired,
  onVolumeChange: PropTypes.func.isRequired,
  onMuteToggle: PropTypes.func.isRequired,
};

// Group card component.
//
// Master volume gets the same treatment as the per-room offset above (#3003).
// The "NN%" label beside the slider reads the same optimistic value, so the
// two cannot visibly disagree while the user is dragging.
export const GroupCard = ({ group, onDelete, onMasterVolumeChange, onRoomVolumeChange, onRoomMuteToggle }) => {
  const [expanded, setExpanded] = useState(false);
  // `?? 50`, not `|| 50`: a group really muted to 0 came back from the server
  // as a falsy 0 and rendered as 50, so the slider could not be left at zero —
  // and once the value is optimistic that stale 50 also reads as an external
  // change and drags the thumb back up.
  const [masterVolume, setMasterVolume, commitMasterVolume] = useOptimisticSliderValue(
    group.master_volume ?? 50,
    onMasterVolumeChange,
  );

  return (
    <div style={{
      backgroundColor: theme.colors.bgElevated,
      borderRadius: '16px',
      border: `1px solid ${theme.colors.borderSubtle}`,
      marginBottom: '16px',
      overflow: 'hidden',
    }}>
      {/* Header */}
      <div
        onClick={() => setExpanded(!expanded)}
        style={{
          padding: '16px 20px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          cursor: 'pointer',
          borderBottom: expanded ? `1px solid ${theme.colors.borderSubtle}` : 'none',
        }}
      >
        <div>
          <div style={{ color: theme.colors.textPrimary, fontSize: '16px', fontWeight: 500 }}>
            {group.group_name}
          </div>
          <div style={{ color: theme.colors.textMuted, fontSize: '13px', marginTop: '2px' }}>
            {group.members?.length || 0} rooms
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          {/* The whole header row toggles the card open/shut, so a drag that
              ends on the slider must not bubble a click up and collapse the
              card out from under the user mid-adjustment. */}
          <div style={{ width: '120px' }} onClick={(e) => e.stopPropagation()}>
            <VolumeSlider
              value={masterVolume}
              onChange={setMasterVolume}
              onCommit={commitMasterVolume}
              min={0}
              max={100}
              showValue={false}
              ariaLabel={`${group.group_name} volume`}
            />
          </div>
          <span style={{ color: theme.colors.textPrimary, fontSize: '14px', fontWeight: 500, width: '36px' }}>
            {masterVolume}%
          </span>
          <button
            onClick={(e) => {
              e.stopPropagation();
              if (window.confirm(`Delete "${group.group_name}" group?`)) {
                onDelete();
              }
            }}
            style={{
              padding: '6px',
              borderRadius: '6px',
              border: 'none',
              backgroundColor: 'transparent',
              color: theme.colors.statusRed,
              cursor: 'pointer',
            }}
            title="Delete group"
          >
            <Icons.Trash />
          </button>
        </div>
      </div>

      {/* Expanded content */}
      {expanded && (
        <div style={{ padding: '16px 20px' }}>
          <div style={{
            color: theme.colors.textMuted,
            fontSize: '11px',
            textTransform: 'uppercase',
            letterSpacing: '1px',
            marginBottom: '12px',
          }}>
            Room Volumes
          </div>
          {group.members?.map((member) => (
            <RoomMemberItem
              key={member.room_id}
              member={member}
              onVolumeChange={(offset) => onRoomVolumeChange(member.room_id, offset)}
              onMuteToggle={(muted) => onRoomMuteToggle(member.room_id, muted)}
            />
          ))}
        </div>
      )}
    </div>
  );
};

GroupCard.propTypes = {
  group: PropTypes.shape({
    group_id: PropTypes.string.isRequired,
    group_name: PropTypes.string.isRequired,
    master_volume: PropTypes.number,
    members: PropTypes.arrayOf(PropTypes.shape({
      room_id: PropTypes.string.isRequired,
      name: PropTypes.string,
      display_name: PropTypes.string,
      label: PropTypes.string,
      room_name: PropTypes.string,
      volume_offset: PropTypes.number,
      is_muted: PropTypes.bool,
    })),
  }).isRequired,
  onDelete: PropTypes.func.isRequired,
  onMasterVolumeChange: PropTypes.func.isRequired,
  onRoomVolumeChange: PropTypes.func.isRequired,
  onRoomMuteToggle: PropTypes.func.isRequired,
};

function roomPrefillName(prefill) {
  if (typeof prefill === 'string') return prefill;
  if (!prefill || typeof prefill !== 'object') return '';
  return prefill.room_name || prefill.roomName || prefill.target_room || prefill.targetRoom || '';
}

// Main modal component
export default function RoomGroupsModal({
  isOpen,
  onClose,
  availableRooms = [],
  initialTab = 'add-speaker',
  prefill = null,
}) {
  const {
    groups,
    loading,
    saving,
    error,
    createGroup,
    deleteGroup,
    setMasterVolume,
    setRoomVolume,
    setRoomMute,
    clearError,
  } = useRoomGroups();

  const [activeTab, setActiveTab] = useState(initialTab || 'add-speaker');
  const localDevice = availableRooms.find(r => r.is_local);
  const initialRoomName = roomPrefillName(prefill);
  const titleId = useId();
  // Rooms are LAN speakers paired to a desktop hub. None of the three panels
  // below has a cloud backend: `/v1/rooms/*` and `/v1/network/*` are both
  // unserved there (backend/cloud_route_manifest.py "rooms" has no registration
  // spec, "network" is LOCAL_ONLY), so the cloud SPA used to render pairing
  // instructions over a permanent 404 (#3553).
  const roomsHidden = isFeatureHidden('rooms');

  const [showCreateForm, setShowCreateForm] = useState(false);
  const [newGroupName, setNewGroupName] = useState('');
  const [selectedRooms, setSelectedRooms] = useState([]);

  // Close on Escape key
  useEffect(() => {
    const handleEscape = (e) => {
      if (e.key === 'Escape' && isOpen) {
        onClose();
      }
    };
    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, [isOpen, onClose]);

  useEffect(() => {
    if (isOpen && initialTab) {
      setActiveTab(initialTab);
    }
  }, [isOpen, initialTab]);

  const handleCreateGroup = async () => {
    if (!newGroupName || selectedRooms.length === 0) return;

    const result = await createGroup(newGroupName, selectedRooms);
    if (result.ok) {
      setNewGroupName('');
      setSelectedRooms([]);
      setShowCreateForm(false);
    }
  };

  const toggleRoomSelection = (roomId) => {
    setSelectedRooms(prev =>
      prev.includes(roomId)
        ? prev.filter(r => r !== roomId)
        : [...prev, roomId]
    );
  };

  if (!isOpen) return null;

  const isPhoneViewport = typeof window !== 'undefined' && window.innerWidth <= 480;
  const overlayPadding = isPhoneViewport
    ? 'env(safe-area-inset-top, 8px) 8px env(safe-area-inset-bottom, 8px)'
    : '20px';
  const modalMaxHeight = isPhoneViewport
    ? 'calc(100dvh - env(safe-area-inset-top, 8px) - env(safe-area-inset-bottom, 8px) - 16px)'
    : '80vh';

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        backgroundColor: theme.colors.overlay,
        zIndex: 1000,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: overlayPadding,
        fontFamily: "'Segoe UI', 'SF Pro Display', -apple-system, sans-serif",
      }}
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        style={{
          position: 'relative',
          width: '100%',
          maxWidth: isPhoneViewport ? 'none' : '560px',
          maxHeight: modalMaxHeight,
          backgroundColor: theme.colors.bgCard,
          borderRadius: isPhoneViewport ? '16px' : '24px',
          boxShadow: `0 24px 80px ${theme.colors.shadowDeep}`,
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: isPhoneViewport ? '16px' : '20px 24px',
          borderBottom: `1px solid ${theme.colors.borderLight}`,
        }}>
          <h2 id={titleId} style={{ margin: 0, fontSize: '20px', fontWeight: 600, color: theme.colors.textPrimary }}>
            Rooms
          </h2>
          <button
            onClick={onClose}
            aria-label="Close"
            style={{
              width: '44px',
              height: '44px',
              borderRadius: '10px',
              border: 'none',
              backgroundColor: 'transparent',
              color: theme.colors.textMuted,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            <Icons.Close />
          </button>
        </div>

        {roomsHidden ? (
          <div
            style={{ padding: isPhoneViewport ? '20px 12px' : '24px', display: 'flex', justifyContent: 'center' }}
            data-testid="rooms-desktop-only"
          >
            <DesktopUpsell feature="rooms" />
          </div>
        ) : (
          <>
        {/* Tab Bar */}
        <div style={{
          display: 'flex',
          gap: '4px',
          padding: '4px',
          margin: isPhoneViewport ? '12px 12px 0' : '16px 24px 0',
          backgroundColor: theme.colors.bgElevated,
          borderRadius: '12px',
        }}>
          {[['add-speaker', 'Add Room'], ['devices', 'Rooms'], ['groups', 'Groups']].map(([key, label]) => (
            <button
              key={key}
              onClick={() => setActiveTab(key)}
              style={{
                flex: 1,
                minHeight: '44px',
                padding: '10px 16px',
                borderRadius: '10px',
                border: 'none',
                backgroundColor: activeTab === key ? theme.colors.glassActive : 'transparent',
                color: activeTab === key ? theme.colors.textPrimary : theme.colors.textMuted,
                fontSize: '14px',
                fontWeight: activeTab === key ? 500 : 400,
                cursor: 'pointer',
                transition: 'all 0.15s ease',
              }}
            >
              {label}
            </button>
          ))}
        </div>

        {/* Content */}
        <div style={{ flex: 1, overflowY: 'auto', padding: isPhoneViewport ? '16px 12px' : '24px' }}>
          {activeTab === 'add-speaker' ? (
            <ConnectSpeakerPanel initialRoomName={initialRoomName} />
          ) : activeTab === 'devices' ? (
            <DeviceDiscoveryPanel
              localDevice={localDevice}
              enabled={isOpen && activeTab === 'devices'}
            />
          ) : loading ? (
            <div style={{ textAlign: 'center', color: theme.colors.textMuted, padding: '40px' }}>
              Loading groups...
            </div>
          ) : (
            <>
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
                }}>
                  <span>{error}</span>
                  <button
                    onClick={clearError}
                    style={{ background: 'none', border: 'none', color: 'inherit', cursor: 'pointer' }}
                  >
                    Dismiss
                  </button>
                </div>
              )}

              {/* Create new group form */}
              {showCreateForm ? (
                <div style={{
                  backgroundColor: theme.colors.bgElevated,
                  borderRadius: '16px',
                  border: `1px solid ${theme.colors.borderSubtle}`,
                  padding: '20px',
                  marginBottom: '16px',
                }}>
                  <div style={{ marginBottom: '16px' }}>
                    <label style={{ display: 'block', marginBottom: '8px', color: theme.colors.textSecondary, fontSize: '14px' }}>
                      Group Name
                    </label>
                    <input
                      type="text"
                      value={newGroupName}
                      onChange={(e) => setNewGroupName(e.target.value)}
                      placeholder="e.g., Downstairs, Party Mode"
                      style={{
                        width: '100%',
                        padding: '12px 16px',
                        borderRadius: '12px',
                        border: `1px solid ${theme.colors.borderLight}`,
                        backgroundColor: theme.colors.bgCard,
                        color: theme.colors.textPrimary,
                        fontSize: '14px',
                        outline: 'none',
                        boxSizing: 'border-box',
                      }}
                    />
                  </div>

                  <div style={{ marginBottom: '16px' }}>
                    <label style={{ display: 'block', marginBottom: '8px', color: theme.colors.textSecondary, fontSize: '14px' }}>
                      Select Rooms
                    </label>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
                      {availableRooms.map((room) => (
                        <button
                          key={room.id}
                          onClick={() => toggleRoomSelection(room.id)}
                          style={{
                            padding: '8px 14px',
                            borderRadius: '8px',
                            border: `1px solid ${selectedRooms.includes(room.id) ? theme.colors.accentBorder : theme.colors.borderLight}`,
                            backgroundColor: selectedRooms.includes(room.id) ? theme.colors.accentSubtle : 'transparent',
                            color: selectedRooms.includes(room.id) ? theme.colors.accent : theme.colors.textSecondary,
                            fontSize: '13px',
                            cursor: 'pointer',
                          }}
                        >
                          {getRoomLabel(room)}
                        </button>
                      ))}
                      {availableRooms.length === 0 && (
                        <span style={{ color: theme.colors.textMuted, fontSize: '13px' }}>
                          No rooms available yet
                        </span>
                      )}
                    </div>
                  </div>

                  <div style={{ display: 'flex', gap: '12px', justifyContent: 'flex-end' }}>
                    <button
                      onClick={() => {
                        setShowCreateForm(false);
                        setNewGroupName('');
                        setSelectedRooms([]);
                      }}
                      style={{
                        padding: '10px 16px',
                        borderRadius: '10px',
                        border: `1px solid ${theme.colors.borderLight}`,
                        backgroundColor: 'transparent',
                        color: theme.colors.textSecondary,
                        fontSize: '14px',
                        cursor: 'pointer',
                      }}
                    >
                      Cancel
                    </button>
                    <button
                      onClick={handleCreateGroup}
                      disabled={!newGroupName || selectedRooms.length === 0 || saving}
                      style={{
                        padding: '10px 16px',
                        borderRadius: '10px',
                        border: 'none',
                        backgroundColor: (!newGroupName || selectedRooms.length === 0) ? theme.colors.bgCard : theme.colors.accent,
                        color: (!newGroupName || selectedRooms.length === 0) ? theme.colors.textMuted : 'white',
                        fontSize: '14px',
                        fontWeight: 500,
                        cursor: (!newGroupName || selectedRooms.length === 0) ? 'not-allowed' : 'pointer',
                      }}
                    >
                      {saving ? 'Creating...' : 'Create group'}
                    </button>
                  </div>
                </div>
              ) : (
                <button
                  onClick={() => setShowCreateForm(true)}
                  style={{
                    width: '100%',
                    padding: '16px',
                    borderRadius: '16px',
                    border: `2px dashed ${theme.colors.borderLight}`,
                    backgroundColor: 'transparent',
                    color: theme.colors.textSecondary,
                    fontSize: '14px',
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    gap: '8px',
                    marginBottom: '16px',
                  }}
                >
                  <Icons.Plus />
                  Create group
                </button>
              )}

              {/* Existing groups */}
              {groups.length > 0 ? (
                groups.map((group) => (
                  <GroupCard
                    key={group.group_id}
                    group={group}
                    onDelete={() => deleteGroup(group.group_id)}
                    onMasterVolumeChange={(volume) => setMasterVolume(group.group_id, volume)}
                    onRoomVolumeChange={(roomId, offset) => setRoomVolume(group.group_id, roomId, offset)}
                    onRoomMuteToggle={(roomId, muted) => setRoomMute(group.group_id, roomId, muted)}
                  />
                ))
              ) : !showCreateForm && (
                <div style={{ textAlign: 'center', padding: '40px 20px' }}>
                  <div style={{ color: theme.colors.textMuted, fontSize: '14px', marginBottom: '8px' }}>
                    No groups yet
                  </div>
                  <div style={{ color: theme.colors.textMuted, fontSize: '13px' }}>
                    Create a group to play the same audio in more than one room.
                  </div>
                </div>
              )}
            </>
          )}
        </div>
          </>
        )}
      </div>
    </div>
  );
}

RoomGroupsModal.propTypes = {
  isOpen: PropTypes.bool.isRequired,
  onClose: PropTypes.func.isRequired,
  availableRooms: PropTypes.arrayOf(PropTypes.shape({
    id: PropTypes.string.isRequired,
    name: PropTypes.string,
    display_name: PropTypes.string,
    label: PropTypes.string,
    room_name: PropTypes.string,
  })),
  initialTab: PropTypes.string,
  prefill: PropTypes.oneOfType([PropTypes.string, PropTypes.object]),
};

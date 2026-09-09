import React, { useState, useEffect, useRef, useCallback } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';
import Slider from './Slider';
import SpeakerIcon from './SpeakerIcon';

const theme = THEME;

/**
 * Debounce helper for volume slider.
 */
function useDebouncedCallback(callback, delay) {
  const timerRef = useRef(null);
  const callbackRef = useRef(callback);
  callbackRef.current = callback;

  return useCallback((...args) => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => callbackRef.current(...args), delay);
  }, [delay]);
}

/**
 * A per-speaker volume and mute control row.
 * @param {Object} props
 * @param {Object} props.spoke - Spoke data object with id, room_name, volume, muted, is_hub
 * @param {function} props.onVolumeChange - Called with (spokeId, volume)
 * @param {function} props.onMuteToggle - Called with (spokeId, muted)
 */
// How long to ignore poll-driven spoke.volume after a local drag/tap, so the
// 5s /api/v1/rooms poll landing between the optimistic setSpokes and the
// backend committing the new volume can't snap the slider back to the old
// value. Comfortably longer than the 200ms debounce plus a round trip.
const VOLUME_SETTLE_WINDOW_MS = 2500;

const SpokeRow = React.memo(({ spoke, onVolumeChange, onMuteToggle }) => {
  const [localVolume, setLocalVolume] = useState(spoke.volume);
  const settleUntilRef = useRef(0);

  // Sync localVolume when spoke data changes from external source — unless
  // we're still inside the settle window of a local change we made ourselves.
  useEffect(() => {
    if (Date.now() < settleUntilRef.current) return;
    setLocalVolume(spoke.volume);
  }, [spoke.volume]);

  const debouncedVolumeChange = useDebouncedCallback((value) => {
    onVolumeChange(spoke.id, value);
  }, 200);

  const handleVolumeChange = (value) => {
    const clamped = Math.max(0, Math.min(100, Math.round(value)));
    setLocalVolume(clamped);
    settleUntilRef.current = Date.now() + VOLUME_SETTLE_WINDOW_MS;
    debouncedVolumeChange(clamped);
  };

  const isDisabled = spoke.is_hub;

  return (
    <div style={{
      padding: '16px 20px',
      opacity: isDisabled ? 0.5 : 1,
    }}>
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        marginBottom: '10px',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <div style={{
            width: '32px',
            height: '32px',
            borderRadius: '8px',
            backgroundColor: theme.colors.bgCard,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: spoke.muted ? theme.colors.textMuted : theme.colors.textSecondary,
          }}>
            <SpeakerIcon muted={spoke.muted} />
          </div>
          <div>
            <div style={{ color: theme.colors.textPrimary, fontSize: '14px', fontWeight: 500 }}>
              {spoke.room_name}
            </div>
            <div style={{ color: theme.colors.textMuted, fontSize: '11px' }}>
              {spoke.is_hub ? 'Hub' : 'Speaker'}
            </div>
          </div>
        </div>
        {!isDisabled && (
          <button
            onClick={() => onMuteToggle(spoke.id, !spoke.muted)}
            aria-label={spoke.muted ? 'Unmute speaker' : 'Mute speaker'}
            style={{
              width: '36px',
              height: '36px',
              borderRadius: '10px',
              border: `1px solid ${spoke.muted ? theme.colors.statusRed + '40' : theme.colors.borderLight}`,
              backgroundColor: spoke.muted ? theme.colors.statusRed + '15' : 'transparent',
              color: spoke.muted ? theme.colors.statusRed : theme.colors.textMuted,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              transition: 'all 0.15s ease',
            }}
          >
            <SpeakerIcon muted={spoke.muted} />
          </button>
        )}
      </div>
      {!isDisabled && (
        <div style={{ opacity: spoke.muted ? 0.4 : 1, transition: 'opacity 0.15s ease' }}>
          <Slider
            label="Volume"
            value={localVolume}
            onChange={handleVolumeChange}
            min={0}
            max={100}
          />
        </div>
      )}
    </div>
  );
});

SpokeRow.propTypes = {
  spoke: PropTypes.object.isRequired,
  onVolumeChange: PropTypes.func.isRequired,
  onMuteToggle: PropTypes.func.isRequired,
};

export { useDebouncedCallback };
export default SpokeRow;

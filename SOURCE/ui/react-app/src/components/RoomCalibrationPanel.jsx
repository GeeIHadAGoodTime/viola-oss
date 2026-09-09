import React, { useState, useEffect, useCallback } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../config';
import { apiFetch } from '../hooks/useViolaApi';
import { useOptimisticSliderValue } from '../hooks/useOptimisticSliderValue';

export default function RoomCalibrationPanel({ roomId, onClose }) {
  const [serverOffsetMs, setServerOffsetMs] = useState(0);
  const [loading, setLoading] = useState(true);

  // Fetch current offset
  useEffect(() => {
    const fetchCalibration = async () => {
      try {
        const data = await apiFetch(`/api/v1/multiroom/${encodeURIComponent(roomId)}/calibration`);
        setServerOffsetMs(data?.offset_ms || 0);
      } catch (e) {
        // Default to 0
      } finally {
        setLoading(false);
      }
    };
    fetchCalibration();
  }, [roomId]);

  // Send offset update. The slider itself is optimistic, so the PUT settles
  // once at the end of a drag: it used to fire one write per input tick, which
  // is the write-spam half of #2772/#3003 on a slider that spans 400 steps.
  const putOffset = useCallback(async (value) => {
    try {
      await apiFetch(`/api/v1/multiroom/${encodeURIComponent(roomId)}/calibration`, {
        method: 'PUT',
        body: JSON.stringify({ offset_ms: value }),
      });
    } catch (e) {
      // Ignore - best effort
    }
  }, [roomId]);

  const [offsetMs, setOffsetMs, commitOffsetMs] = useOptimisticSliderValue(serverOffsetMs, putOffset);

  const resetOffset = useCallback(() => {
    setOffsetMs(0);
    commitOffsetMs();
  }, [setOffsetMs, commitOffsetMs]);

  if (loading) {
    return (
      <div style={{ padding: '12px', color: THEME.colors.textMuted, fontSize: '13px' }}>
        Loading calibration...
      </div>
    );
  }

  return (
    <div style={{
      padding: '16px',
      backgroundColor: THEME.colors.bgSurface,
      borderRadius: '12px',
      marginTop: '8px',
    }}>
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        marginBottom: '12px',
      }}>
        <span style={{ color: THEME.colors.textSecondary, fontSize: '13px', fontWeight: 500 }}>
          Audio Sync Offset
        </span>
        <div style={{ display: 'flex', gap: '8px' }}>
          <button
            onClick={resetOffset}
            style={{
              padding: '4px 10px',
              borderRadius: '6px',
              border: `1px solid ${THEME.colors.borderLight}`,
              backgroundColor: 'transparent',
              color: THEME.colors.textMuted,
              fontSize: '11px',
              cursor: 'pointer',
            }}
          >
            Reset to 0
          </button>
          {onClose && (
            <button
              onClick={onClose}
              style={{
                padding: '4px 10px',
                borderRadius: '6px',
                border: 'none',
                backgroundColor: 'transparent',
                color: THEME.colors.textMuted,
                fontSize: '11px',
                cursor: 'pointer',
              }}
            >
              Done
            </button>
          )}
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
        <span style={{ color: THEME.colors.textMuted, fontSize: '11px', width: '40px' }}>-200ms</span>
        <input
          type="range"
          min={-200}
          max={200}
          value={offsetMs}
          onChange={(e) => setOffsetMs(parseInt(e.target.value, 10))}
          onPointerUp={commitOffsetMs}
          onKeyUp={commitOffsetMs}
          onBlur={commitOffsetMs}
          aria-label="Audio sync offset"
          style={{
            flex: 1,
            height: '4px',
            appearance: 'none',
            background: `linear-gradient(to right, ${THEME.colors.accent} ${((offsetMs + 200) / 400) * 100}%, ${THEME.colors.glassActive} ${((offsetMs + 200) / 400) * 100}%)`,
            borderRadius: '2px',
            cursor: 'pointer',
          }}
        />
        <span style={{ color: THEME.colors.textMuted, fontSize: '11px', width: '40px', textAlign: 'right' }}>+200ms</span>
      </div>

      <div style={{ textAlign: 'center', marginTop: '8px' }}>
        <span style={{
          color: offsetMs === 0 ? THEME.colors.textMuted : THEME.colors.accent,
          fontSize: '16px',
          fontWeight: 500,
        }}>
          {offsetMs > 0 ? '+' : ''}{offsetMs}ms
        </span>
      </div>

      <p style={{
        color: THEME.colors.textMuted,
        fontSize: '11px',
        marginTop: '8px',
        lineHeight: 1.4,
      }}>
        Adjust until this room's sound is in sync with your main speaker.
        Positive values delay playback; negative values advance it.
      </p>
    </div>
  );
}

RoomCalibrationPanel.propTypes = {
  roomId: PropTypes.string.isRequired,
  onClose: PropTypes.func,
};

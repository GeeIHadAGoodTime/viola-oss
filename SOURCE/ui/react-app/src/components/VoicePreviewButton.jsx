import { useCallback, useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../config';
import { apiFetch } from '../hooks/useViolaApi';

// Hear a voice before saving it: Viola says a short sample on this device in the
// voice, speed and volume on screen. Nothing is saved until Save.
export default function VoicePreviewButton({ voice, language, rate, volume }) {
  const [state, setState] = useState('idle');
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const play = useCallback(async () => {
    setState('playing');
    let played = false;
    try {
      const result = await apiFetch('/v1/voice/preview', {
        method: 'POST',
        body: JSON.stringify({ voice, language, rate, volume }),
      });
      played = Boolean(result?.data?.played ?? result?.played);
    } catch {
      played = false;
    }
    if (mounted.current) setState(played ? 'idle' : 'failed');
  }, [voice, language, rate, volume]);

  return (
    <div style={{ marginTop: '10px', display: 'flex', alignItems: 'center', gap: '10px' }}>
      <button
        type="button"
        onClick={play}
        disabled={state === 'playing'}
        data-testid="voice-preview-button"
        style={{
          padding: '6px 14px',
          borderRadius: '8px',
          border: `1px solid ${THEME.colors.textMuted}`,
          background: 'transparent',
          color: THEME.colors.textSecondary,
          cursor: state === 'playing' ? 'default' : 'pointer',
          fontSize: '13px',
        }}
      >
        {state === 'playing' ? 'Playing...' : 'Play preview'}
      </button>
      {state === 'failed' && (
        <span role="status" style={{ color: THEME.colors.textMuted, fontSize: '12px' }}>
          Viola could not play this voice here.
        </span>
      )}
    </div>
  );
}

VoicePreviewButton.propTypes = {
  voice: PropTypes.string,
  language: PropTypes.string,
  rate: PropTypes.number,
  volume: PropTypes.number,
};

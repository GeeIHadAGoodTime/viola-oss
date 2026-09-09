import React, { useState, useEffect, useCallback } from 'react';
import { THEME } from '../../config';
import { apiFetch } from '../../hooks/useViolaApi';
import { isFeatureAvailable } from '../../utils/featureSurface';
import Section from './Section';
import SectionDivider from './SectionDivider';
import SpokeRow from './SpokeRow';

const theme = THEME;
const SPEAKERS_INITIAL_POLL_DELAY_MS = 500;

/**
 * Speakers section that fetches connected spokes and provides volume/mute controls.
 * Polls every 5 seconds while mounted. Only renders if remote spokes are present.
 */
const SpeakersSection = React.memo(() => {
  const [spokes, setSpokes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Fetch spokes shortly after mount, then continue polling every 5 seconds
  const fetchSpokes = useCallback(async () => {
    // Spokes are LAN speakers held by the desktop hub's room registry, which
    // the cloud surface does not serve (#3553), so the 5s poll stays silent
    // there instead of repeating a 404 for as long as the panel is mounted.
    if (!isFeatureAvailable('rooms')) {
      setSpokes([]);
      setLoading(false);
      setError(null);
      return;
    }
    try {
      const data = await apiFetch('/api/v1/rooms');
      // apiFetch auto-unwraps ResponseEnvelope; rooms endpoint returns array directly
      if (Array.isArray(data)) {
        setSpokes(data);
      } else if (Array.isArray(data?.data)) {
        setSpokes(data.data);
      }
      setError(null);
    } catch {
      setError('Could not reach speakers — check your network connection');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let intervalId = null;
    const timeoutId = setTimeout(() => {
      fetchSpokes();
      intervalId = setInterval(fetchSpokes, 5000);
    }, SPEAKERS_INITIAL_POLL_DELAY_MS);

    return () => {
      clearTimeout(timeoutId);
      if (intervalId) clearInterval(intervalId);
    };
  }, [fetchSpokes]);

  const handleVolumeChange = useCallback(async (spokeId, volume) => {
    try {
      await apiFetch(`/api/v1/rooms/${spokeId}/volume`, {
        method: 'PUT',
        body: JSON.stringify({ volume }),
      });
      setSpokes(prev => prev.map(s =>
        s.id === spokeId ? { ...s, volume } : s
      ));
    } catch {
      // Silently fail — spoke may have disconnected
    }
  }, []);

  const handleMuteToggle = useCallback(async (spokeId, muted) => {
    try {
      await apiFetch(`/api/v1/rooms/${spokeId}/mute`, {
        method: 'PUT',
        body: JSON.stringify({ muted }),
      });
      setSpokes(prev => prev.map(s =>
        s.id === spokeId ? { ...s, muted } : s
      ));
    } catch {
      // Silently fail — spoke may have disconnected
    }
  }, []);

  // Only show section if there are connected spokes (excluding hub)
  const remoteSpokes = spokes.filter(s => !s.is_hub);
  if (loading) {
    return (
      <Section title="Speakers">
        <div style={{
          padding: '12px 20px 16px',
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          color: theme.colors.textMuted,
          fontSize: '13px',
        }}>
          <svg width="14" height="14" viewBox="0 0 24 24" data-essential-motion="spin" fill="none" stroke="currentColor" strokeWidth="2" style={{ animation: 'spin 1s linear infinite' }}>
            <path d="M21 12a9 9 0 11-6.219-8.56" />
          </svg>
          Loading speakers...
        </div>
      </Section>
    );
  }
  if (remoteSpokes.length === 0) return null;

  return (
    <Section title="Speakers">
      {error && (
        <div style={{ padding: '12px 20px', color: theme.colors.statusRed, fontSize: '13px' }}>
          {error}
        </div>
      )}
      {remoteSpokes.map((spoke, index) => (
        <React.Fragment key={spoke.id}>
          {index > 0 && <SectionDivider />}
          <SpokeRow
            spoke={spoke}
            onVolumeChange={handleVolumeChange}
            onMuteToggle={handleMuteToggle}
          />
        </React.Fragment>
      ))}
    </Section>
  );
});

export default SpeakersSection;

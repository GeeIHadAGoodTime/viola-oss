import React, { useState, useEffect, useCallback, useRef } from 'react';
import { authFetch } from '../hooks/useViolaApi';
import { formatTimeDisplay } from '../utils/timeFormat';
import { useSettings } from '../hooks/useSettings';
import { THEME } from '../config';

const API_BASE = window.location.origin;

function formatTime(ts, timeFormat = 'auto') {
  if (!ts) return '\u2014';
  const date = new Date(ts * 1000);
  const datePart = date.toLocaleDateString();
  const timePart = formatTimeDisplay(date, timeFormat);
  return `${datePart} ${timePart}`;
}

function classificationColor(classification) {
  const colors = THEME.colors;
  const classColors = {
    true_positive: colors.statusGreen,
    false_positive: colors.statusRed,
    near_miss: colors.statusYellow,
    ambiguous: colors.textMuted,
    pending: colors.accent,
  };
  return classColors[classification] || colors.textSecondary;
}

function buttonStyle(overrides = {}) {
  const colors = THEME.colors;
  return {
    background: colors.bgElevated,
    color: colors.textPrimary,
    border: `1px solid ${colors.borderHover}`,
    borderRadius: 4,
    padding: '6px 12px',
    cursor: 'pointer',
    fontSize: 12,
    ...overrides,
  };
}

function compactButtonStyle(overrides = {}) {
  return buttonStyle({
    padding: '4px 8px',
    fontSize: 11,
    marginRight: 4,
    ...overrides,
  });
}

function selectStyle(overrides = {}) {
  const colors = THEME.colors;
  return {
    background: colors.bgElevated,
    color: colors.textPrimary,
    border: `1px solid ${colors.borderHover}`,
    borderRadius: 4,
    padding: '3px 4px',
    fontSize: 11,
    ...overrides,
  };
}

function StatCard({ label, value, color }) {
  const colors = THEME.colors;
  return (
    <div style={{
      background: colors.bgCard,
      border: `1px solid ${colors.borderLight}`,
      borderRadius: 8,
      padding: '12px 16px',
      minWidth: 120,
      textAlign: 'center',
    }}>
      <div style={{ fontSize: 24, fontWeight: 700, color: color || colors.textPrimary }}>{value}</div>
      <div style={{ fontSize: 12, color: colors.textMuted, marginTop: 4 }}>{label}</div>
    </div>
  );
}

function ClipRow({ clip, selected, onSelect, onPlay, onClassify, playing, timeFormat }) {
  const colors = THEME.colors;

  return (
    <tr style={{
      borderBottom: `1px solid ${selected ? colors.accentBorder : colors.divider}`,
      background: selected ? colors.accentActive : 'transparent',
    }}>
      <td style={{ padding: '8px 4px' }}>
        <input
          type="checkbox"
          checked={selected}
          onChange={() => onSelect(clip.id)}
        />
      </td>
      <td style={{ padding: '8px 4px', fontFamily: 'monospace', fontSize: 12 }}>
        {clip.id}
      </td>
      <td style={{ padding: '8px 4px', fontSize: 12 }}>
        {formatTime(clip.timestamp, timeFormat)}
      </td>
      <td style={{ padding: '8px 4px', fontFamily: 'monospace' }}>
        {clip.confidence_score.toFixed(3)}
      </td>
      <td style={{ padding: '8px 4px' }}>
        <span style={{
          color: classificationColor(clip.classification),
          fontWeight: 600,
          fontSize: 12,
        }}>
          {clip.classification}
        </span>
      </td>
      <td style={{ padding: '8px 4px', fontSize: 11, color: colors.textMuted }}>
        {clip.classification_method}
      </td>
      <td style={{ padding: '8px 4px' }}>
        <span style={{ color: clip.reviewed ? colors.statusGreen : colors.textMuted, fontSize: 12 }}>
          {clip.reviewed ? 'Yes' : 'No'}
        </span>
      </td>
      <td style={{ padding: '8px 4px' }}>
        <button
          onClick={() => onPlay(clip.id)}
          style={compactButtonStyle({
            background: playing === clip.id ? colors.statusRed : colors.bgElevated,
            color: playing === clip.id ? colors.textBright : colors.textPrimary,
            border: `1px solid ${playing === clip.id ? colors.statusRed : colors.borderHover}`,
          })}
        >
          {playing === clip.id ? 'Stop' : 'Play'}
        </button>
        <select
          value={clip.classification}
          onChange={(e) => onClassify(clip.id, e.target.value)}
          style={selectStyle()}
        >
          <option value="true_positive">TP</option>
          <option value="false_positive">FP</option>
          <option value="ambiguous">Ambiguous</option>
          <option value="near_miss">Near Miss</option>
        </select>
      </td>
    </tr>
  );
}

export default function ReviewPage() {
  const colors = THEME.colors;
  const { settings: userSettings } = useSettings();
  const timeFormat = userSettings?.time_display_format || 'auto';
  const [stats, setStats] = useState(null);
  const [clips, setClips] = useState([]);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState(null);
  const [filter, setFilter] = useState('');
  const [selected, setSelected] = useState(new Set());
  const [playing, setPlaying] = useState(null);
  const [page, setPage] = useState(0);
  const audioRef = useRef(null);
  const PAGE_SIZE = 50;

  const fetchStats = useCallback(async () => {
    try {
      const res = await authFetch('/v1/review/stats', { credentials: 'include' });
      const json = await res.json();
      if (json.success) setStats(json.data);
    } catch (e) {
      console.error('Failed to fetch stats:', e);
    }
  }, []);

  const fetchClips = useCallback(async () => {
    setLoading(true);
    setFetchError(null);
    try {
      const params = new URLSearchParams({
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      });
      if (filter) params.set('classification', filter);

      const res = await authFetch(`/v1/review/clips?${params}`, { credentials: 'include' });
      const json = await res.json();
      if (json.success) setClips(json.data.clips);
    } catch (e) {
      console.error('Failed to fetch clips:', e);
      setFetchError('Failed to load review clips. Please refresh and try again.');
    } finally {
      setLoading(false);
    }
  }, [filter, page]);

  useEffect(() => { fetchStats(); }, [fetchStats]);
  useEffect(() => { fetchClips(); }, [fetchClips]);

  const stopCurrentAudio = useCallback(() => {
    if (audioRef.current?.audio) {
      audioRef.current.audio.pause();
    }
    if (audioRef.current?.blobUrl) {
      URL.revokeObjectURL(audioRef.current.blobUrl);
    }
    audioRef.current = null;
    setPlaying(null);
  }, []);

  const handlePlay = useCallback(async (clipId) => {
    if (playing === clipId) {
      stopCurrentAudio();
      return;
    }
    stopCurrentAudio();

    try {
      const response = await authFetch(`/v1/review/clips/${clipId}/audio`, {
        credentials: 'include',
      });
      if (!response.ok) {
        // Keep the raw status out of the Error message (issue #768); carry it
        // on .status for the log below.
        const error = new Error('Could not play this clip.');
        error.status = response.status;
        throw error;
      }

      const blob = await response.blob();
      const blobUrl = URL.createObjectURL(blob);
      const audio = new Audio(blobUrl);
      audio.onended = () => {
        URL.revokeObjectURL(blobUrl);
        audioRef.current = null;
        setPlaying(null);
      };
      audio.onerror = () => {
        URL.revokeObjectURL(blobUrl);
        audioRef.current = null;
        setPlaying(null);
      };
      audioRef.current = { audio, blobUrl };
      setPlaying(clipId);
      await audio.play();
    } catch (e) {
      stopCurrentAudio();
      console.error('Failed to play clip:', e);
    }
  }, [playing, stopCurrentAudio]);

  const handleClassify = useCallback(async (clipId, classification) => {
    try {
      await authFetch(`/v1/review/clips/${clipId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ classification, review_result: 'reclassified' }),
      });
      fetchClips();
      fetchStats();
    } catch (e) {
      console.error('Failed to classify:', e);
    }
  }, [fetchClips, fetchStats]);

  const handleSelect = useCallback((clipId) => {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(clipId)) next.delete(clipId);
      else next.add(clipId);
      return next;
    });
  }, []);

  const handleSelectAll = useCallback(() => {
    if (selected.size === clips.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(clips.map(c => c.id)));
    }
  }, [clips, selected]);

  const handleBatch = useCallback(async (action, classification) => {
    if (selected.size === 0) return;
    try {
      const body = { action, clip_ids: [...selected] };
      if (classification) body.classification = classification;
      await authFetch('/v1/review/clips/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify(body),
      });
      setSelected(new Set());
      fetchClips();
      fetchStats();
    } catch (e) {
      console.error('Batch action failed:', e);
    }
  }, [selected, fetchClips, fetchStats]);

  const handleExport = useCallback(async () => {
    try {
      const response = await authFetch('/v1/review/export', {
        credentials: 'include',
      });
      if (!response.ok) {
        // Keep the raw status out of the Error message (issue #768); carry it
        // on .status for the log below.
        const error = new Error('Could not export the review dataset.');
        error.status = response.status;
        throw error;
      }

      const blob = await response.blob();
      const blobUrl = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = blobUrl;
      link.download = 'viola-review-export.zip';
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(blobUrl);
    } catch (e) {
      console.error('Failed to export review dataset:', e);
    }
  }, []);

  useEffect(() => {
    return () => {
      stopCurrentAudio();
    };
  }, [stopCurrentAudio]);

  return (
    <div style={{
      background: colors.bgSurface,
      color: colors.textPrimary,
      minHeight: '100vh',
      padding: 24,
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    }}>
      <h1 style={{ fontSize: 22, marginBottom: 16, fontWeight: 600, color: colors.textBright }}>
        Wake Word Data Review
      </h1>

      {/* Stats Dashboard */}
      {stats && (
        <div style={{ display: 'flex', gap: 12, marginBottom: 24, flexWrap: 'wrap' }}>
          <StatCard label="Total Clips" value={stats.total} />
          <StatCard label="True Positive" value={stats.by_classification?.true_positive || 0} color={colors.statusGreen} />
          <StatCard label="False Positive" value={stats.by_classification?.false_positive || 0} color={colors.statusRed} />
          <StatCard label="Near Miss" value={stats.by_classification?.near_miss || 0} color={colors.statusYellow} />
          <StatCard label="Ambiguous" value={stats.by_classification?.ambiguous || 0} color={colors.textMuted} />
          <StatCard label="Unreviewed" value={stats.unreviewed} color={colors.accent} />
          <StatCard label="Storage" value={`${stats.storage?.total_mb || 0} MB`} />
        </div>
      )}

      {/* Controls */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 16, alignItems: 'center', flexWrap: 'wrap' }}>
        <select
          value={filter}
          onChange={(e) => { setFilter(e.target.value); setPage(0); }}
          style={selectStyle({ padding: '6px 8px', fontSize: 12 })}
        >
          <option value="">All Classifications</option>
          <option value="true_positive">True Positive</option>
          <option value="false_positive">False Positive</option>
          <option value="near_miss">Near Miss</option>
          <option value="ambiguous">Ambiguous</option>
          <option value="pending">Pending</option>
        </select>

        {selected.size > 0 && (
          <>
            <span style={{ fontSize: 12, color: colors.textMuted }}>{selected.size} selected</span>
            <button onClick={() => handleBatch('confirm')} style={buttonStyle()}>Confirm All</button>
            <button onClick={() => handleBatch('reclassify', 'true_positive')} style={buttonStyle()}>Mark TP</button>
            <button onClick={() => handleBatch('reclassify', 'false_positive')} style={buttonStyle()}>Mark FP</button>
            <button onClick={() => handleBatch('discard')} style={buttonStyle({
              background: colors.statusRed,
              color: colors.textBright,
              border: `1px solid ${colors.statusRed}`,
            })}>
              Discard
            </button>
          </>
        )}

        <div style={{ flex: 1 }} />
        <button onClick={handleExport} style={buttonStyle()}>Export Dataset</button>
        <button onClick={() => { fetchClips(); fetchStats(); }} style={buttonStyle()}>Refresh</button>
      </div>

      {/* Clip Table */}
      <div style={{
        overflowX: 'auto',
        background: colors.bgCard,
        border: `1px solid ${colors.borderLight}`,
        borderRadius: 8,
      }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr style={{ borderBottom: `2px solid ${colors.borderHover}`, textAlign: 'left', color: colors.textSecondary }}>
              <th style={{ padding: '8px 4px' }}>
                <input
                  type="checkbox"
                  checked={selected.size === clips.length && clips.length > 0}
                  onChange={handleSelectAll}
                />
              </th>
              <th style={{ padding: '8px 4px' }}>ID</th>
              <th style={{ padding: '8px 4px' }}>Time</th>
              <th style={{ padding: '8px 4px' }}>Score</th>
              <th style={{ padding: '8px 4px' }}>Classification</th>
              <th style={{ padding: '8px 4px' }}>Method</th>
              <th style={{ padding: '8px 4px' }}>Reviewed</th>
              <th style={{ padding: '8px 4px' }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={8} style={{ padding: 24, textAlign: 'center', color: colors.textMuted }}>Loading...</td></tr>
            ) : fetchError ? (
              <tr><td colSpan={8} style={{ padding: 24, textAlign: 'center', color: colors.statusRed }}>{fetchError}</td></tr>
            ) : clips.length === 0 ? (
              <tr><td colSpan={8} style={{ padding: 24, textAlign: 'center', color: colors.textMuted }}>No clips found</td></tr>
            ) : clips.map(clip => (
              <ClipRow
                key={clip.id}
                clip={clip}
                selected={selected.has(clip.id)}
                onSelect={handleSelect}
                onPlay={handlePlay}
                onClassify={handleClassify}
                playing={playing}
                timeFormat={timeFormat}
              />
            ))}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      <div style={{ display: 'flex', gap: 8, marginTop: 16, justifyContent: 'center' }}>
        <button
          onClick={() => setPage(p => Math.max(0, p - 1))}
          disabled={page === 0}
          style={{ ...buttonStyle(), opacity: page === 0 ? 0.4 : 1 }}
        >
          Previous
        </button>
        <span style={{ padding: '6px 12px', fontSize: 13, color: colors.textMuted }}>
          Page {page + 1}
        </span>
        <button
          onClick={() => setPage(p => p + 1)}
          disabled={clips.length < PAGE_SIZE}
          style={{ ...buttonStyle(), opacity: clips.length < PAGE_SIZE ? 0.4 : 1 }}
        >
          Next
        </button>
      </div>
    </div>
  );
}

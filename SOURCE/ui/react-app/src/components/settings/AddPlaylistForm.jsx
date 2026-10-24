import { useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../../config';

// The settings "Add playlist" form. It used to write an unread
// `saved_playlists` draft key that nothing ever saved or played, so a pasted
// link silently vanished. It now saves through the same playlist store as
// "Sync from YouTube" (POST /v1/settings/playlists), so the playlist appears
// under Saved Playlists and plays when the user says its name.

const PLAYLIST_LINK_RE = /(youtube\.com|youtu\.be|open\.spotify\.com)\//i;

export function isPlaylistLink(value) {
  const text = String(value || '').trim();
  if (!text) return false;
  return text.toLowerCase().startsWith('spotify:') || PLAYLIST_LINK_RE.test(text);
}

export default function AddPlaylistForm({ onAdd, theme = THEME }) {
  const [name, setName] = useState('');
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState(null);

  const inputStyle = {
    flex: 1,
    minWidth: 0,
    padding: '12px 16px',
    minHeight: '44px',
    borderRadius: '12px',
    border: `1px solid ${theme.colors.borderLight}`,
    backgroundColor: theme.colors.bgCard,
    color: theme.colors.textPrimary,
    fontSize: '14px',
    outline: 'none',
  };

  const handleAdd = async () => {
    const trimmedName = name.trim();
    const trimmedUrl = url.trim();
    if (!trimmedName) {
      setStatus({ ok: false, message: 'Give the playlist a name. You will say this name to play it.' });
      return;
    }
    if (!isPlaylistLink(trimmedUrl)) {
      setStatus({ ok: false, message: 'Paste a YouTube or Spotify playlist link.' });
      return;
    }
    setBusy(true);
    setStatus(null);
    let saved = false;
    try {
      saved = Boolean(await onAdd(trimmedName, trimmedUrl));
    } catch {
      saved = false;
    }
    setBusy(false);
    if (saved) {
      setStatus({
        ok: true,
        message: `Saved "${trimmedName}". Say "play my ${trimmedName} playlist" to play it.`,
      });
      setName('');
      setUrl('');
    } else {
      setStatus({
        ok: false,
        message: "Couldn't save that playlist. Check the link is a YouTube or Spotify playlist and try again.",
      });
    }
  };

  return (
    <div style={{ marginBottom: '16px' }}>
      <div style={{ marginBottom: '8px', color: theme.colors.textSecondary, fontSize: '14px' }}>
        Add a playlist
      </div>
      <div style={{ color: theme.colors.textMuted, fontSize: '12px', marginBottom: '8px', lineHeight: 1.4 }}>
        Save a YouTube or Spotify playlist under a name, then ask Viola to play it by that name.
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
        <input
          type="text"
          aria-label="Playlist name"
          placeholder="Name, e.g. workout"
          value={name}
          onChange={(event) => setName(event.target.value)}
          style={{ ...inputStyle, flex: '1 1 140px' }}
        />
        <input
          type="url"
          aria-label="Playlist link"
          placeholder="YouTube or Spotify playlist link"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !busy) handleAdd();
          }}
          style={{ ...inputStyle, flex: '2 1 220px' }}
        />
        <button
          type="button"
          onClick={handleAdd}
          disabled={busy}
          style={{
            padding: '12px 20px',
            minHeight: '44px',
            borderRadius: '12px',
            border: 'none',
            backgroundColor: theme.colors.accent,
            color: theme.colors.textBright || '#ffffff',
            fontSize: '14px',
            fontWeight: 500,
            cursor: busy ? 'not-allowed' : 'pointer',
            opacity: busy ? 0.6 : 1,
            whiteSpace: 'nowrap',
          }}
        >
          {busy ? 'Saving...' : 'Add'}
        </button>
      </div>
      {status && (
        <div
          role={status.ok ? 'status' : 'alert'}
          style={{
            marginTop: '8px',
            fontSize: '13px',
            lineHeight: 1.4,
            color: status.ok ? theme.colors.statusGreen : theme.colors.statusRed,
          }}
        >
          {status.message}
        </div>
      )}
    </div>
  );
}

AddPlaylistForm.propTypes = {
  onAdd: PropTypes.func.isRequired,
  theme: PropTypes.object,
};

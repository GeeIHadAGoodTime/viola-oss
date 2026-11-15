// The one test for "this text is an identifier, not a song title".
// Mirrors models/track_title.py: the backend sends "" for such a title and
// marks the item capabilities.title_pending; this keeps a stray id off the
// screen even if one ever arrives.
export const LOADING_TITLE = 'Loading...';

export function looksLikeTrackId(value) {
  if (typeof value !== 'string') return false;
  const cleaned = value.trim().toLowerCase();
  return cleaned.startsWith('spotify:') || cleaned.includes('open.spotify.com/');
}

// The title to show: '' for an id.
export function displayTitle(value) {
  if (typeof value !== 'string' || looksLikeTrackId(value)) return '';
  return value;
}

// True when a track is playing but its name has not arrived yet.
export function isTitlePending(track) {
  if (!track) return false;
  return Boolean(track.capabilities?.title_pending) || !displayTitle(track.title).trim();
}

// Text for the stage title line: the real title, a neutral loading state, or
// null when nothing is playing.
export function stageTitle(track) {
  if (!track) return null;
  const title = displayTitle(track.title).trim();
  return title || LOADING_TITLE;
}

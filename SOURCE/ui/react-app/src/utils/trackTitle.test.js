import { describe, expect, it } from 'vitest';
import { displayTitle, isTitlePending, looksLikeTrackId, stageTitle } from './trackTitle';

describe('trackTitle', () => {
  it('treats Spotify uris and links as ids', () => {
    expect(looksLikeTrackId('spotify:track:7AgMX97ljLm9BzszyyXEYZ')).toBe(true);
    expect(looksLikeTrackId('https://open.spotify.com/track/7AgMX97ljLm9BzszyyXEYZ')).toBe(true);
    expect(looksLikeTrackId('Jazz (We\'ve Got)')).toBe(false);
  });

  it('never shows an id as the title', () => {
    expect(displayTitle('spotify:track:abc')).toBe('');
    expect(stageTitle({ title: 'spotify:track:abc' })).toBe('Loading...');
  });

  it('shows Loading... for a pending title and the title once it arrives', () => {
    expect(stageTitle({ title: '', capabilities: { title_pending: true } })).toBe('Loading...');
    expect(isTitlePending({ title: '', capabilities: { title_pending: true } })).toBe(true);
    expect(stageTitle({ title: 'Jazz' })).toBe('Jazz');
    expect(isTitlePending({ title: 'Jazz' })).toBe(false);
    expect(stageTitle(null)).toBe(null);
  });
});

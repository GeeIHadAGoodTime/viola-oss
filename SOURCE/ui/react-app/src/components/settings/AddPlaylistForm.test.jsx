import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import AddPlaylistForm, { isPlaylistLink } from './AddPlaylistForm';

// "Add Playlist URL" used to write an unread draft key, so the link was never
// saved or playable. The form must save through the playlist store (onAdd ->
// useSettings.addPlaylist -> POST /v1/settings/playlists) and say what it did.

function fill(name, url) {
  fireEvent.change(screen.getByLabelText('Playlist name'), { target: { value: name } });
  fireEvent.change(screen.getByLabelText('Playlist link'), { target: { value: url } });
}

describe('AddPlaylistForm', () => {
  it('saves the name and link through onAdd and tells the user how to play it', async () => {
    const onAdd = vi.fn(() => Promise.resolve(true));
    render(<AddPlaylistForm onAdd={onAdd} />);
    fill('Workout', 'https://www.youtube.com/playlist?list=PL123');
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    await waitFor(() => expect(onAdd).toHaveBeenCalledWith('Workout', 'https://www.youtube.com/playlist?list=PL123'));
    expect(await screen.findByRole('status')).toHaveTextContent('Say "play my Workout playlist" to play it.');
    expect(screen.getByLabelText('Playlist name')).toHaveValue('');
    expect(screen.getByLabelText('Playlist link')).toHaveValue('');
  });

  it('accepts Spotify links', async () => {
    const onAdd = vi.fn(() => Promise.resolve(true));
    render(<AddPlaylistForm onAdd={onAdd} />);
    fill('Road trip', 'https://open.spotify.com/playlist/abc');
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    await waitFor(() => expect(onAdd).toHaveBeenCalledTimes(1));
  });

  it('says plainly when the save fails and keeps what was typed', async () => {
    const onAdd = vi.fn(() => Promise.resolve(false));
    render(<AddPlaylistForm onAdd={onAdd} />);
    fill('Chill', 'https://www.youtube.com/playlist?list=PL9');
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    expect(await screen.findByRole('alert')).toHaveTextContent("Couldn't save that playlist.");
    expect(screen.getByLabelText('Playlist name')).toHaveValue('Chill');
  });

  it('asks for a name and a real playlist link before saving', () => {
    const onAdd = vi.fn();
    render(<AddPlaylistForm onAdd={onAdd} />);
    fill('', 'https://www.youtube.com/playlist?list=PL9');
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Give the playlist a name.');
    fill('Mix', 'https://example.com/list');
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Paste a YouTube or Spotify playlist link.');
    expect(onAdd).not.toHaveBeenCalled();
  });

  it('recognises the link shapes the backend accepts', () => {
    expect(isPlaylistLink('https://youtu.be/x?list=PL1')).toBe(true);
    expect(isPlaylistLink('spotify:playlist:abc')).toBe(true);
    expect(isPlaylistLink('https://music.youtube.com/playlist?list=PL1')).toBe(true);
    expect(isPlaylistLink('workout')).toBe(false);
    expect(isPlaylistLink('')).toBe(false);
  });
});

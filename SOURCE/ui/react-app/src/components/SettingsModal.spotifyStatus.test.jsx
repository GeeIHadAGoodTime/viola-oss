import { describe, expect, it } from 'vitest';
import { spotifySourceDescription, spotifyStatusProblem, spotifyWindowSignInLabel } from './SettingsModal';

describe('Spotify status in Settings', () => {
  it('says Connected only when the desktop says the player works', () => {
    expect(spotifySourceDescription({ logged_in: true, state: 'connected' })).toBe('Connected');
    // Build 9 said Connected here: a cached cookie the player could not use.
    expect(spotifySourceDescription({
      logged_in: false,
      state: 'session_rejected',
      cookie_bridge_cached: true,
      state_message: "Spotify didn't accept your sign-in.",
    })).toBe('Not working');
  });

  it('shows the checking state while the sign-in is tried in the player', () => {
    expect(spotifySourceDescription({ logged_in: false, state: 'checking' })).toBe('Checking your sign-in...');
    expect(spotifyStatusProblem({ logged_in: false, state: 'checking', state_message: 'x' })).toBe('');
  });

  it('shows the same words the assistant uses when something is wrong', () => {
    const message = "Spotify didn't accept your sign-in. Sign in to Spotify again in Settings > Music.";
    expect(spotifyStatusProblem({ logged_in: false, state: 'session_rejected', state_message: message })).toBe(message);
  });

  it('offers the separate window in plain words', () => {
    expect(spotifyWindowSignInLabel({ state: 'session_rejected' })).toBe('Sign in again');
    expect(spotifyWindowSignInLabel({ state: 'not_signed_in' })).toBe('Sign in using a separate window');
    expect(spotifySourceDescription({ logged_in: false, state: 'signing_in' })).toBe('Waiting for you to sign in...');
    expect(spotifyStatusProblem({ logged_in: false, state: 'signing_in', state_message: 'x' })).toBe('');
  });

  it('treats never signed in as a plain invitation, not a problem', () => {
    expect(spotifySourceDescription({ logged_in: false, state: 'not_signed_in', state_message: 'x' }))
      .toBe('Sign in to play Spotify');
    expect(spotifyStatusProblem({ logged_in: false, state: 'not_signed_in', state_message: 'x' })).toBe('');
    expect(spotifySourceDescription(null)).toBe('Sign in to play Spotify');
  });
});

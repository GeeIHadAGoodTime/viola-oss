import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '../test/test-utils';
import { CalendarSettings, GOOGLE_CONNECT_NOTE } from './AccountTab';
import { GOOGLE_NOT_SET_UP_MESSAGE } from '../hooks/useCalendarProviders';
import { apiFetch } from '../hooks/useViolaApi';

vi.mock('../hooks/useViolaApi', () => ({
  apiFetch: vi.fn(),
}));

function mockBackend({ connected = false, step }) {
  let linked = connected;
  apiFetch.mockImplementation((path) => {
    if (path === '/v1/calendar/status') {
      return Promise.resolve({
        providers: [{ provider: 'google', configured: linked }],
      });
    }
    if (path.endsWith('/providers')) {
      return Promise.resolve({ ok: true, providers: [] });
    }
    if (path.endsWith('/session')) {
      if (step?.authorization_url) linked = true;
      return Promise.resolve({ ok: true, session: { steps: [step] } });
    }
    return Promise.resolve({});
  });
}

describe('Settings > Connections > Google', () => {
  beforeEach(() => {
    apiFetch.mockReset();
    window.viola = { openExternalUrl: vi.fn() };
    window.history.replaceState({}, '', '/');
  });

  afterEach(() => {
    delete window.viola;
  });

  it('explains the unverified-app screen and what Viola can do before connecting', async () => {
    mockBackend({ connected: false });

    render(<CalendarSettings />);

    expect(await screen.findByText('Google')).toBeInTheDocument();
    const note = screen.getByTestId('google-unverified-note');
    expect(note).toHaveTextContent("Google hasn't verified this app");
    expect(note).toHaveTextContent('Advanced');
    expect(note).toHaveTextContent('cannot read your inbox');
    expect(note).toHaveTextContent('stays on this computer');
    expect(GOOGLE_CONNECT_NOTE).not.toMatch(/—/);
  });

  it('hides the note once Google is connected', async () => {
    mockBackend({ connected: true });

    render(<CalendarSettings />);

    expect(await screen.findByText('Connected: Calendar and email')).toBeInTheDocument();
    expect(screen.queryByTestId('google-unverified-note')).not.toBeInTheDocument();
  });

  it('says the build has no Google sign-in instead of a generic retry message', async () => {
    mockBackend({ connected: false, step: { status: 'unavailable', authorization_url: null } });

    render(<CalendarSettings />);
    fireEvent.click(await screen.findByRole('button', { name: 'Connect' }));

    expect(await screen.findByText(GOOGLE_NOT_SET_UP_MESSAGE)).toBeInTheDocument();
    expect(window.viola.openExternalUrl).not.toHaveBeenCalled();
  });

  it('opens Google sign-in in the system browser', async () => {
    const url = 'https://accounts.google.com/o/oauth2/v2/auth?client_id=x&code_challenge_method=S256';
    mockBackend({ connected: false, step: { status: 'pending', authorization_url: url } });

    render(<CalendarSettings />);
    fireEvent.click(await screen.findByRole('button', { name: 'Connect' }));

    await waitFor(() => expect(window.viola.openExternalUrl).toHaveBeenCalledWith(url));
  });
});

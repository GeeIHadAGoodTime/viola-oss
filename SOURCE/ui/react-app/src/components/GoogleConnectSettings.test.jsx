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

  it('says Viola can send email you approve and use your calendar, and does not promise Gmail reading', async () => {
    mockBackend({ connected: false });

    render(<CalendarSettings />);

    expect(await screen.findByText('Google')).toBeInTheDocument();
    const note = screen.getByTestId('google-connect-note');
    expect(note).toHaveTextContent('calendar events');
    expect(note).toHaveTextContent('Viola can send email you approve and use your calendar');
    expect(note).toHaveTextContent('stays on this computer');
    // Google verified Calendar and Gmail sending: connecting is no longer "under review".
    expect(note).not.toHaveTextContent('still reviewing Viola');
    expect(note).not.toHaveTextContent('limits how many people can connect');
    // Gmail reading (gmail.modify) is off in 1.0.5: no later step, no unverified screen promised.
    expect(note).not.toHaveTextContent(/reading|organizing|read, organize/i);
    expect(note).not.toHaveTextContent("Google hasn't verified this app");
    expect(note).not.toHaveTextContent('cannot read your inbox');
    expect(screen.getByText(/^Not connected\./)).not.toHaveTextContent(/read|organize/i);
    expect(GOOGLE_CONNECT_NOTE).not.toMatch(/—/);
  });

  it('hides the note once Google is connected', async () => {
    mockBackend({ connected: true });

    render(<CalendarSettings />);

    expect(await screen.findByText('Connected: Calendar and email')).toBeInTheDocument();
    expect(screen.queryByTestId('google-connect-note')).not.toBeInTheDocument();
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

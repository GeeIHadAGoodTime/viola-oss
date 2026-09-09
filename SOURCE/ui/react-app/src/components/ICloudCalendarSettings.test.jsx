import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '../test/test-utils';
import ICloudCalendarSettings from './ICloudCalendarSettings';
import { apiFetch } from '../hooks/useViolaApi';

vi.mock('../hooks/useViolaApi', () => ({
  apiFetch: vi.fn(),
}));

function statusResponse(providers) {
  return Promise.resolve({ connected: providers.length > 0, providers });
}

describe('ICloudCalendarSettings', () => {
  beforeEach(() => {
    apiFetch.mockReset();
    delete window.viola;
    window.history.replaceState({}, '', '/');
  });

  afterEach(() => {
    delete window.viola;
    window.history.replaceState({}, '', '/');
  });

  it('shows a desktop upsell on the cloud SPA instead of the connect form', async () => {
    window.history.replaceState({}, '', '/app');
    apiFetch.mockImplementation(() => statusResponse([]));

    render(<ICloudCalendarSettings />);

    expect(await screen.findByRole('note')).toBeInTheDocument();
    expect(screen.queryByText('iCloud Calendar')).not.toBeInTheDocument();
  });

  it('renders "Not connected" and a Connect button when no CalDAV account is stored', async () => {
    window.viola = {};
    apiFetch.mockImplementation((path) => {
      if (path === '/v1/calendar/status') {
        return statusResponse([{ provider: 'local', configured: true }]);
      }
      return Promise.resolve({});
    });

    render(<ICloudCalendarSettings />);

    expect(await screen.findByText('iCloud Calendar')).toBeInTheDocument();
    expect(screen.getByText('Not connected')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Connect' })).toBeInTheDocument();
  });

  it('renders "Connected" when the backend reports a configured caldav provider', async () => {
    window.viola = {};
    apiFetch.mockImplementation((path) => {
      if (path === '/v1/calendar/status') {
        return statusResponse([{ provider: 'caldav', configured: true }]);
      }
      return Promise.resolve({});
    });

    render(<ICloudCalendarSettings />);

    expect(await screen.findByText('Connected')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Disconnect/ })).toBeInTheDocument();
  });

  it('submits the Apple ID + app-specific password and shows connected calendars on success', async () => {
    window.viola = {};
    apiFetch.mockImplementation((path, options) => {
      if (path === '/v1/calendar/status') {
        return statusResponse([{ provider: 'local', configured: true }]);
      }
      if (path === '/v1/calendar/caldav/connect') {
        const body = JSON.parse(options.body);
        expect(body).toMatchObject({
          username: 'jay@icloud.com',
          password: 'app-specific-pw', // pragma: allowlist secret
          icloud: true,
        });
        return Promise.resolve({
          provider: 'caldav',
          calendars: [{ name: 'Home', calendar_id: 'home' }],
          message: 'Connected 1 calendar(s).',
        });
      }
      return Promise.resolve({});
    });

    render(<ICloudCalendarSettings />);

    fireEvent.click(await screen.findByRole('button', { name: 'Connect' }));

    fireEvent.change(screen.getByLabelText('Apple ID'), { target: { value: 'jay@icloud.com' } });
    fireEvent.change(screen.getByLabelText('App-specific password'), {
      target: { value: 'app-specific-pw' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));

    await waitFor(() => expect(screen.getByText('Connected')).toBeInTheDocument());
    expect(screen.getByText(/1 calendar synced: Home/)).toBeInTheDocument();
  });

  it('shows a friendly message when the backend rejects the connect attempt', async () => {
    window.viola = {};
    apiFetch.mockImplementation((path) => {
      if (path === '/v1/calendar/status') {
        return statusResponse([]);
      }
      if (path === '/v1/calendar/caldav/connect') {
        const err = new Error("We couldn't complete that request. Please try again.");
        err.status = 400;
        err.code = 'caldav_connect_failed';
        return Promise.reject(err);
      }
      return Promise.resolve({});
    });

    render(<ICloudCalendarSettings />);

    fireEvent.click(await screen.findByRole('button', { name: 'Connect' }));
    fireEvent.change(screen.getByLabelText('Apple ID'), { target: { value: 'jay@icloud.com' } });
    fireEvent.change(screen.getByLabelText('App-specific password'), { target: { value: 'wrong' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));

    expect(await screen.findByText(/app-specific password/)).toBeInTheDocument();
    expect(screen.getByText('Not connected')).toBeInTheDocument();
  });

  it('disconnects a connected account after confirmation', async () => {
    window.viola = {};
    apiFetch.mockImplementation((path) => {
      if (path === '/v1/calendar/status') {
        return statusResponse([{ provider: 'caldav', configured: true }]);
      }
      if (path === '/v1/calendar/caldav/disconnect') {
        return Promise.resolve({ message: 'CalDAV account disconnected.' });
      }
      return Promise.resolve({});
    });

    render(<ICloudCalendarSettings />);

    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect' }));
    const disconnectButtons = screen.getAllByRole('button', { name: 'Disconnect' });
    fireEvent.click(disconnectButtons[disconnectButtons.length - 1]);

    await waitFor(() => expect(screen.getByText('Not connected')).toBeInTheDocument());
  });
});

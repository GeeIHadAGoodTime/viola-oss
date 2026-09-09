/**
 * A speaker whose credential stopped working must be able to re-join (#4434).
 *
 * Credentials are shorter-lived and individually revocable now, so a device
 * can be holding one the hub no longer accepts. It must land on the pairing
 * gate rather than a silently dead speaker screen — and a still-valid
 * credential past half-life must be renewed underneath the user so a speaker
 * in normal use never hits the wall.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor } from '../test/test-utils';
import SpokeSessionBoundary from './SpokeSessionBoundary';

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function Speaker() {
  return <div data-testid="speaker-ui">playing</div>;
}

describe('SpokeSessionBoundary', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('keeps the speaker on screen while it is still paired', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(200, { ok: true, data: { paired: true, device_id: 'dev' } })),
    );

    render(
      <SpokeSessionBoundary room="kitchen" spokeToken="vspk1.dev.1.sig">
        <Speaker />
      </SpokeSessionBoundary>,
    );

    expect(screen.getByTestId('speaker-ui')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('speaker-ui')).toBeInTheDocument());
    expect(screen.queryByTestId('spoke-pairing-gate')).toBeNull();
  });

  it('sends the credential it holds so the hub can answer for this device', async () => {
    const fetchSpy = vi.fn(async () => jsonResponse(200, { ok: true, data: { paired: true } }));
    vi.stubGlobal('fetch', fetchSpy);

    render(
      <SpokeSessionBoundary room="kitchen" spokeToken="vspk1.dev.1.sig">
        <Speaker />
      </SpokeSessionBoundary>,
    );

    await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
    const [url, request] = fetchSpy.mock.calls[0];
    expect(url).toBe('/bootstrap/spoke-session');
    expect(request.headers).toMatchObject({ 'X-Spoke-Token': 'vspk1.dev.1.sig' });
  });

  it('drops to the pairing gate when the hub says this device is no longer paired', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url) => {
        if (url === '/bootstrap/spoke-session') {
          return jsonResponse(200, { ok: true, data: { paired: false } });
        }
        return jsonResponse(200, { ok: true, data: { pairing_session_id: 'session-1' } });
      }),
    );

    render(
      <SpokeSessionBoundary room="kitchen" spokeToken="vspk1.revoked.1.sig">
        <Speaker />
      </SpokeSessionBoundary>,
    );

    expect(await screen.findByTestId('spoke-pairing-gate')).toBeInTheDocument();
    expect(screen.queryByTestId('speaker-ui')).toBeNull();
  });

  it('keeps the speaker running when the hub cannot be reached', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('network down');
      }),
    );

    render(
      <SpokeSessionBoundary room="kitchen" spokeToken="vspk1.dev.1.sig">
        <Speaker />
      </SpokeSessionBoundary>,
    );

    await waitFor(() => expect(screen.getByTestId('speaker-ui')).toBeInTheDocument());
    expect(screen.queryByTestId('spoke-pairing-gate')).toBeNull();
  });

  it('carries a renewed credential into the address bar', async () => {
    const replaceState = vi.spyOn(window.history, 'replaceState');
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(200, {
          ok: true,
          data: { paired: true, renewed: true, spoke_token: 'vspk1.dev.2.freshsig' },
        }),
      ),
    );

    render(
      <SpokeSessionBoundary room="kitchen" spokeToken="vspk1.dev.1.sig">
        <Speaker />
      </SpokeSessionBoundary>,
    );

    await waitFor(() => expect(replaceState).toHaveBeenCalled());
    const [, , nextUrl] = replaceState.mock.calls[0];
    expect(nextUrl).toContain('spoke_token=vspk1.dev.2.freshsig');
    expect(screen.getByTestId('speaker-ui')).toBeInTheDocument();
  });
});

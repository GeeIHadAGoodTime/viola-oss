/**
 * The scanned-QR join path (#4434).
 *
 * The QR carries a single-use pairing ticket instead of a live credential, so
 * the gate has to exchange it silently for pairing to stay "one scan" — and
 * has to degrade to the PIN flow (rather than stranding the device) when the
 * ticket is a photograph of a QR that was already used or has expired.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '../test/test-utils';
import SpokePairingGate from './SpokePairingGate';

const originalLocation = window.location;

function stubLocation() {
  const replace = vi.fn();
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { ...originalLocation, replace, search: '', href: 'http://192.168.1.23:8756/' },
  });
  return replace;
}

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

describe('SpokePairingGate', () => {
  let replace;

  beforeEach(() => {
    replace = stubLocation();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    Object.defineProperty(window, 'location', { configurable: true, value: originalLocation });
  });

  it('exchanges a scanned ticket without ever showing a code prompt', async () => {
    const fetchSpy = vi.fn(async (url) => {
      if (url === '/bootstrap/claim') {
        return jsonResponse(200, { ok: true, data: { spoke_token: 'vspk1.dev.1.sig', device_id: 'dev' } });
      }
      return jsonResponse(200, { ok: true, data: { pairing_session_id: 'session-1' } });
    });
    vi.stubGlobal('fetch', fetchSpy);

    render(<SpokePairingGate room="kitchen" ticket="vpair1.ticket.1.sig" />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith('/?spoke_token=vspk1.dev.1.sig&room=kitchen'));

    const claim = fetchSpy.mock.calls.find(([url]) => url === '/bootstrap/claim');
    expect(claim).toBeTruthy();
    expect(JSON.parse(claim[1].body)).toEqual({ ticket: 'vpair1.ticket.1.sig' });
    // The user was never asked to type anything.
    expect(screen.queryByTestId('spoke-pairing-input')).toBeNull();
  });

  it('falls back to the code prompt when the scanned ticket is spent', async () => {
    // i.e. someone scanned a photograph of the screen after the real device paired.
    const fetchSpy = vi.fn(async (url) => {
      if (url === '/bootstrap/claim') {
        return jsonResponse(410, { detail: 'That pairing link has already been used.' });
      }
      return jsonResponse(200, { ok: true, data: { pairing_session_id: 'session-1' } });
    });
    vi.stubGlobal('fetch', fetchSpy);

    render(<SpokePairingGate room="kitchen" ticket="vpair1.spent.1.sig" />);

    expect(await screen.findByTestId('spoke-pairing-input')).toBeInTheDocument();
    expect(await screen.findByTestId('spoke-pairing-banner')).toHaveTextContent(/already been used/i);
    expect(replace).not.toHaveBeenCalled();
  });

  it('goes straight to the code prompt when there is no ticket at all', async () => {
    const fetchSpy = vi.fn(async () => jsonResponse(200, { ok: true, data: { pairing_session_id: 'session-1' } }));
    vi.stubGlobal('fetch', fetchSpy);

    render(<SpokePairingGate room="kitchen" />);

    expect(await screen.findByTestId('spoke-pairing-input')).toBeInTheDocument();
    expect(fetchSpy.mock.calls.some(([url]) => url === '/bootstrap/claim')).toBe(false);
  });
});

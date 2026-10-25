import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '../test/test-utils';
import ConnectSpeakerPanel from './ConnectSpeakerPanel';

// Helper: build a fetch mock that always returns the same JSON payload.
function mockSuccessfulFetch(payload) {
  const fetchSpy = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => payload,
    text: async () => '',
  });
  vi.stubGlobal('fetch', fetchSpy);
  return fetchSpy;
}

describe('ConnectSpeakerPanel', () => {
  beforeEach(() => {
    window.__VIOLA_API_KEY__ = 'desktop-key'; // pragma: allowlist secret
    // Pairing is a desktop-hub capability, so every case below is the desktop
    // surface. The Qt bridge is what isCloudSurface() keys off (#3553).
    window.viola = {};
  });

  afterEach(() => {
    vi.restoreAllMocks();
    delete window.viola;
  });

  it('loads the local LAN address with the desktop API key and renders a QR link', async () => {
    const fetchSpy = mockSuccessfulFetch({
      ip: '192.168.1.23',
      port: 8756,
      spoke_url: 'http://192.168.1.23:8756/?room=',
      pairing_code: 'ABCD',
    });

    const { container } = render(<ConnectSpeakerPanel />);

    await screen.findByText('http://192.168.1.23:8756/?room=new-room');
    expect(screen.getByText('ABCD')).toBeInTheDocument();
    expect(container.querySelector('svg path[d]')).toBeInTheDocument();

    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
    const [url, request] = fetchSpy.mock.calls[0];
    expect(url).toBe('/v1/network/local-address');
    // authFetch must include the desktop API key. Content-Type is not
    // required on this GET — and adding it forces a CORS preflight in
    // some browser configurations, so the panel deliberately does not
    // send one.
    expect(request.headers).toMatchObject({
      'X-API-Key': 'desktop-key',
    });
  });

  it('renders the QR and pairing code even when the hub only knows its localhost address', async () => {
    mockSuccessfulFetch({
      ip: '127.0.0.1',
      port: 8756,
      spoke_url: 'http://127.0.0.1:8756/?room=',
      pairing_code: 'maple',
    });

    const { container } = render(<ConnectSpeakerPanel />);

    // QR + pairing code BOTH render despite localhost-only data.
    await screen.findByTestId('connect-speaker-pairing-code');
    expect(screen.getByText('maple')).toBeInTheDocument();
    expect(container.querySelector('svg path[d]')).toBeInTheDocument();
    expect(screen.getByTestId('connect-speaker-qr')).toBeInTheDocument();
    // Localhost hint surfaces, but the QR is NOT hidden behind an error.
    expect(screen.getByTestId('connect-speaker-localhost-hint')).toBeInTheDocument();
  });

  it('does not show device-discovery framing in the success path', async () => {
    mockSuccessfulFetch({
      ip: '192.168.1.23',
      port: 8756,
      spoke_url: 'http://192.168.1.23:8756/?room=',
      pairing_code: 'ABCD',
    });

    render(<ConnectSpeakerPanel />);

    await screen.findByText('ABCD');

    // The fail-closed device-discovery copy must never appear in success.
    expect(screen.queryByText("Couldn't find this device on your network.")).toBeNull();
    expect(screen.queryByText(/Finding this device on your network/i)).toBeNull();
  });

  it('does not leave the loading copy in the DOM after the fetch resolves', async () => {
    mockSuccessfulFetch({
      ip: '192.168.1.23',
      port: 8756,
      spoke_url: 'http://192.168.1.23:8756/?room=',
      pairing_code: 'ABCD',
    });

    render(<ConnectSpeakerPanel />);

    await screen.findByText('ABCD');
    // Spinner / preparing copy is gone post-load.
    expect(screen.queryByTestId('connect-speaker-loading')).toBeNull();
    expect(screen.queryByText(/Preparing your room code/i)).toBeNull();
  });

  it('renders QR for partial payloads that omit pairing_code (no auth gating regression)', async () => {
    mockSuccessfulFetch({
      ip: '192.168.1.23',
      port: 8756,
      spoke_url: 'http://192.168.1.23:8756/?room=',
      // No pairing_code (older hub builds, transient codec failures).
    });

    const { container } = render(<ConnectSpeakerPanel />);

    await screen.findByText('http://192.168.1.23:8756/?room=new-room');
    expect(container.querySelector('svg path[d]')).toBeInTheDocument();
    // No pairing_code block when codec did not produce a word.
    expect(screen.queryByTestId('connect-speaker-pairing-code')).toBeNull();
  });

  it('hides the pairing code in the link and reveals it only on request', async () => {
    // The Add Room screen is photographed and screen-shared, so the code in
    // the link must not be legible by default (#4434).
    mockSuccessfulFetch({
      ip: '192.168.1.23',
      port: 8756,
      spoke_url: 'http://192.168.1.23:8756/?pair=vpair1.tickettickettick.1785500000.abc123&room=',
      pairing_code: 'ABCD',
      pairing_ticket_param: 'pair',
      pairing_ticket_expires_in: 300,
    });

    const { user } = render(<ConnectSpeakerPanel />);

    const urlBlock = await screen.findByTestId('connect-speaker-url');
    expect(urlBlock.textContent).not.toContain('vpair1.tickettickettick.1785500000.abc123');
    expect(urlBlock.textContent).toContain('192.168.1.23:8756');
    expect(urlBlock.textContent).toContain('room=new-room');

    await user.click(screen.getByTestId('connect-speaker-url-reveal'));

    expect(screen.getByTestId('connect-speaker-url').textContent).toContain(
      'vpair1.tickettickettick.1785500000.abc123',
    );
  });

  it('still renders the QR when the link is masked, so pairing stays one scan', async () => {
    mockSuccessfulFetch({
      ip: '192.168.1.23',
      port: 8756,
      spoke_url: 'http://192.168.1.23:8756/?pair=vpair1.ticket.1785500000.abc123&room=',
      pairing_code: 'ABCD',
      pairing_ticket_param: 'pair',
      pairing_ticket_expires_in: 300,
    });

    const { container } = render(<ConnectSpeakerPanel />);

    await screen.findByTestId('connect-speaker-qr');
    expect(container.querySelector('svg path[d]')).toBeInTheDocument();
  });

  it('offers no reveal control when the link carries nothing secret', async () => {
    mockSuccessfulFetch({
      ip: '192.168.1.23',
      port: 8756,
      spoke_url: 'http://192.168.1.23:8756/?room=',
      pairing_code: 'ABCD',
    });

    render(<ConnectSpeakerPanel />);

    await screen.findByText('http://192.168.1.23:8756/?room=new-room');
    expect(screen.queryByTestId('connect-speaker-url-reveal')).toBeNull();
  });

  it('shows a retry prompt when the authenticated lookup returns 401', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 401,
        json: async () => ({ detail: 'unauthorized' }),
        text: async () => 'unauthorized',
      }),
    );

    render(<ConnectSpeakerPanel />);

    // Auth-error surface — addressable, NOT a "couldn't find device" message.
    expect(await screen.findByTestId('connect-speaker-auth-error')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
    // The legacy device-discovery error string must be gone for good.
    expect(screen.queryByText("Couldn't find this device on your network.")).toBeNull();
  });

  // 1.0.5 RC b4: the panel showed http://<lan-ip>:8756 while the hub listened
  // only on 127.0.0.1, so the phone hung. The hub now reports whether a phone
  // can reach it; when it cannot, the panel must show why and no link at all.
  it('shows the reason and no QR, link or code when phones cannot reach the hub', async () => {
    mockSuccessfulFetch({
      ip: '',
      port: 8756,
      spoke_url: '',
      pairing_code: '',
      lan_access: {
        reachable: false,
        reason_code: 'auth_disabled',
        message: "Phones can't connect while Viola's security checks are turned off on this computer.",
      },
    });

    render(<ConnectSpeakerPanel />);

    expect(await screen.findByTestId('connect-speaker-lan-unavailable')).toBeInTheDocument();
    expect(screen.getByTestId('connect-speaker-lan-message')).toHaveTextContent('security checks are turned off');
    expect(screen.queryByTestId('connect-speaker-qr')).toBeNull();
    expect(screen.queryByTestId('connect-speaker-url')).toBeNull();
    expect(screen.queryByTestId('connect-speaker-pairing-code')).toBeNull();
    expect(screen.getByTestId('connect-speaker-lan-retry')).toBeInTheDocument();
  });

  it('never invents a link from ip and port when the hub says it is unreachable', async () => {
    mockSuccessfulFetch({
      ip: '192.168.1.23',
      port: 8756,
      spoke_url: '',
      pairing_code: '',
      lan_access: { reachable: false, reason_code: 'listen_failed', message: 'Port busy.' },
    });

    render(<ConnectSpeakerPanel />);

    await screen.findByTestId('connect-speaker-lan-unavailable');
    expect(screen.queryByText(/192\.168\.1\.23/)).toBeNull();
  });

  it('explains a firewall block instead of letting the phone hang', async () => {
    mockSuccessfulFetch({
      ip: '192.168.1.23',
      port: 8756,
      spoke_url: 'http://192.168.1.23:8756/?room=',
      pairing_code: 'ABCD',
      lan_access: {
        reachable: true,
        via: 'lan_listener',
        firewall: { state: 'blocked', message: 'Windows Firewall is blocking Viola on this network.' },
      },
    });

    render(<ConnectSpeakerPanel />);

    expect(await screen.findByTestId('connect-speaker-firewall-blocked')).toHaveTextContent(
      'Windows Firewall is blocking Viola',
    );
    expect(screen.getByTestId('connect-speaker-qr')).toBeInTheDocument();
  });

  it('turns LAN access off from the panel and drops the link', async () => {
    const fetchSpy = vi.fn().mockImplementation(async (url) => ({
      ok: true,
      status: 200,
      json: async () => (url === '/v1/network/lan-access'
        ? { ok: true, data: { reachable: false, reason_code: 'lan_access_off' } }
        : {
          ip: '192.168.1.23',
          port: 8756,
          spoke_url: 'http://192.168.1.23:8756/?room=',
          pairing_code: 'ABCD',
          lan_access: { reachable: true, via: 'lan_listener', firewall: null },
        }),
      text: async () => '',
    }));
    vi.stubGlobal('fetch', fetchSpy);

    render(<ConnectSpeakerPanel />);

    const off = await screen.findByTestId('connect-speaker-lan-off');
    off.click();

    expect(await screen.findByTestId('connect-speaker-lan-unavailable')).toBeInTheDocument();
    const post = fetchSpy.mock.calls.find(([url]) => url === '/v1/network/lan-access');
    expect(post).toBeTruthy();
    expect(post[1].method).toBe('POST');
    expect(JSON.parse(post[1].body)).toEqual({ enabled: false });
    expect(screen.queryByTestId('connect-speaker-qr')).toBeNull();
  });
});

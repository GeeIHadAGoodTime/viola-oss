import React from 'react';
import { cleanup, render, waitFor } from '@testing-library/react';

function installCapturingWebSocket() {
  const urls = [];

  class CapturingWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;

    constructor(url) {
      this.url = url;
      this.readyState = CapturingWebSocket.OPEN;
      this.binaryType = 'blob';
      urls.push(url);
    }

    send() {}

    close() {
      this.readyState = CapturingWebSocket.CLOSED;
    }
  }

  CapturingWebSocket.prototype.CONNECTING = 0;
  CapturingWebSocket.prototype.OPEN = 1;
  CapturingWebSocket.prototype.CLOSING = 2;
  CapturingWebSocket.prototype.CLOSED = 3;

  global.WebSocket = CapturingWebSocket;
  return urls;
}

async function renderHookClient() {
  const { useWebSocket } = await import('./useWebSocket');

  function TestComponent() {
    useWebSocket(() => {});
    return null;
  }

  render(<TestComponent />);
}

describe('useWebSocket', () => {
  beforeEach(() => {
    vi.resetModules();
    cleanup();
    window.__VIOLA_BASE_URL__ = 'http://localhost:8756';
    window.__VIOLA_API_KEY__ = 'desktop-key'; // pragma: allowlist secret
    window.__VIOLA_WS_AUTH_TOKEN__ = '';
    window.history.replaceState({}, '', '/');
  });

  afterEach(() => {
    cleanup();
  });

  it('uses the seeded desktop ws token for the first connection', async () => {
    const urls = installCapturingWebSocket();
    window.__VIOLA_WS_AUTH_TOKEN__ = 'seeded-token';
    global.fetch = vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: async () => ({}),
      })
    );

    await renderHookClient();

    await waitFor(() => expect(urls).toHaveLength(1));
    expect(urls[0]).toContain('token=seeded-token');
    expect(urls[0]).not.toContain('api_key=');
    expect(window.__VIOLA_WS_AUTH_TOKEN__).toBe('');
  });

  it('mints a short-lived ws token when no seeded token is present', async () => {
    const urls = installCapturingWebSocket();
    global.fetch = vi.fn((url) => {
      if (String(url).endsWith('/v1/ws/auth')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({ ok: true, data: { token: 'fresh-token' }, error: null }),
        });
      }
      return Promise.resolve({
        ok: true,
        json: async () => ({}),
      });
    });

    await renderHookClient();

    await waitFor(() => expect(urls).toHaveLength(1));
    expect(urls[0]).toContain('token=fresh-token');
    expect(urls[0]).not.toContain('api_key=');
    expect(global.fetch).toHaveBeenCalledWith(
      'http://localhost:8756/v1/ws/auth',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-API-Key': 'desktop-key' }),
      }),
    );
  });

  it('never falls back to api_key query auth when ws token minting fails (SEC-005)', async () => {
    // The raw API key must never land in a URL (server logs, proxy logs,
    // browser history). When the mint fails the socket connects without
    // credentials and the server rejects it — the mint failure is the bug.
    const urls = installCapturingWebSocket();
    global.fetch = vi.fn(() =>
      Promise.resolve({
        ok: false,
        json: async () => ({}),
      })
    );

    await renderHookClient();

    await waitFor(() => expect(urls).toHaveLength(1));
    expect(urls[0]).not.toContain('api_key=');
    expect(urls[0]).not.toContain('token=');
  });

  it('forwards scoped spoke token from scanned room URLs', async () => {
    const urls = installCapturingWebSocket();
    window.__VIOLA_API_KEY__ = '';
    window.history.replaceState({}, '', '/?spoke_token=qr-token&room=kitchen');
    global.fetch = vi.fn(() =>
      Promise.resolve({
        ok: false,
        json: async () => ({}),
      })
    );

    await renderHookClient();

    await waitFor(() => expect(urls).toHaveLength(1));
    const wsUrl = new URL(urls[0]);
    expect(wsUrl.pathname).toBe('/ws/events');
    expect(wsUrl.searchParams.get('room')).toBe('kitchen');
    expect(wsUrl.searchParams.get('spoke_token')).toBe('qr-token');
    expect(wsUrl.searchParams.has('api_key')).toBe(false);
  });

  it('routes token-only spoke URLs through the LAN spoke token without minting desktop auth', async () => {
    const urls = installCapturingWebSocket();
    window.__VIOLA_API_KEY__ = 'desktop-key'; // pragma: allowlist secret
    window.history.replaceState({}, '', '/?spoke_token=qr-token');
    global.fetch = vi.fn(() =>
      Promise.resolve({
        ok: false,
        json: async () => ({}),
      })
    );

    await renderHookClient();

    await waitFor(() => expect(urls).toHaveLength(1));
    const wsUrl = new URL(urls[0]);
    expect(wsUrl.pathname).toBe('/ws/events');
    expect(wsUrl.searchParams.get('room')).toBe('speaker');
    expect(wsUrl.searchParams.get('spoke_token')).toBe('qr-token');
    expect(wsUrl.searchParams.has('token')).toBe(false);
    expect(wsUrl.searchParams.has('api_key')).toBe(false);
    expect(global.fetch).not.toHaveBeenCalledWith(
      'http://localhost:8756/v1/ws/auth',
      expect.anything(),
    );
  });

  it('reschedules a reconnect when the socket fails to construct (webview-reload gap)', async () => {
    // Regression for call 977101ac: after a webview reload the only /ws/events
    // socket disconnected (code 1001) and NO socket ever reconnected. A connect
    // attempt that fails BEFORE the WebSocket object exists (buildSocketUrl
    // rejecting, or `new WebSocket` throwing) attaches no onclose handler, so
    // pre-fix no reconnect was ever scheduled and the socket stayed down. The
    // fix routes every such failure through the same 2s reconnect path.
    vi.useFakeTimers();
    try {
      let attempts = 0;
      const urls = [];

      class FlakyWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        static CLOSING = 2;
        static CLOSED = 3;

        constructor(url) {
          attempts += 1;
          urls.push(url);
          if (attempts === 1) {
            // The reloaded tab's first connect attempt fails outright.
            throw new Error('construct failed (webview reload)');
          }
          this.url = url;
          this.readyState = FlakyWebSocket.OPEN;
          this.binaryType = 'blob';
        }

        send() {}

        close() {
          this.readyState = FlakyWebSocket.CLOSED;
        }
      }
      FlakyWebSocket.prototype.OPEN = 1;
      FlakyWebSocket.prototype.CLOSED = 3;
      global.WebSocket = FlakyWebSocket;
      global.fetch = vi.fn(() => Promise.resolve({ ok: true, json: async () => ({}) }));

      await renderHookClient();

      // First attempt threw synchronously; without the fix nothing more happens.
      await vi.advanceTimersByTimeAsync(0);
      expect(attempts).toBe(1);

      // The fix scheduled a 2s reconnect; advancing past it re-attempts and
      // this time the socket constructs successfully.
      await vi.advanceTimersByTimeAsync(2100);
      expect(attempts).toBeGreaterThanOrEqual(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('backs off exponentially on repeated reconnect failures, then resets after a real connect', async () => {
    // Regression for #2775: a fixed 2000ms reconnect forever hammers a down
    // server (e.g. /v1/ws/auth mint failing) with unbounded /ws/events +
    // /v1/ws/auth traffic. The delay must double (2s, 4s, 8s, ...) on each
    // consecutive failure, and reset back to 2s once a connection actually
    // succeeds (so a LATER, unrelated outage starts backing off from
    // scratch instead of picking up wherever a prior outage left off).
    vi.useFakeTimers();
    try {
      let attempts = 0;
      const instances = [];
      // Attempts 1 and 2 fail immediately after construction; attempt 3
      // succeeds. Attempt 3 is then killed manually (below) to prove the
      // reconnect that follows uses the RESET base delay, not the next step
      // of the earlier exponential sequence (which would be 8s).
      const failOnConstruct = new Set([1, 2]);

      class SeqWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        static CLOSING = 2;
        static CLOSED = 3;

        constructor(url) {
          attempts += 1;
          this.attemptNum = attempts;
          this.url = url;
          this.readyState = SeqWebSocket.CONNECTING;
          this.binaryType = 'blob';
          instances.push(this);
          setTimeout(() => {
            if (failOnConstruct.has(this.attemptNum)) {
              this.readyState = SeqWebSocket.CLOSED;
              if (this.onclose) this.onclose({ code: 1006, reason: 'unreachable' });
            } else {
              this.readyState = SeqWebSocket.OPEN;
              if (this.onopen) this.onopen();
            }
          }, 0);
        }

        send() {}

        close() {
          this.readyState = SeqWebSocket.CLOSED;
        }
      }
      SeqWebSocket.prototype.OPEN = 1;
      SeqWebSocket.prototype.CLOSED = 3;
      global.WebSocket = SeqWebSocket;
      // onopen also probes /health for the restart-detection check — keep it
      // resolving so that path doesn't throw.
      global.fetch = vi.fn(() => Promise.resolve({ ok: true, json: async () => ({}) }));

      await renderHookClient();

      // Attempt 1 fails almost immediately.
      await vi.advanceTimersByTimeAsync(0);
      expect(attempts).toBe(1);

      // Before the base 2s delay elapses, no reconnect yet.
      await vi.advanceTimersByTimeAsync(1900);
      expect(attempts).toBe(1);
      // Past 2s, attempt 2 fires (and immediately fails too).
      await vi.advanceTimersByTimeAsync(200);
      expect(attempts).toBe(2);

      // The next delay must have doubled to ~4s, not stayed at 2s: nothing
      // happens at 3.9s past attempt 2...
      await vi.advanceTimersByTimeAsync(3900);
      expect(attempts).toBe(2);
      // ...and attempt 3 fires just past the 4s mark, succeeding (onopen).
      await vi.advanceTimersByTimeAsync(200);
      expect(attempts).toBe(3);
      await vi.advanceTimersByTimeAsync(0); // let onopen's /health probe settle

      // Kill the now-connected socket (server restart / network drop) and
      // prove the FOLLOWING reconnect uses the reset base delay (2s), not
      // the next step of the pre-reset exponential sequence (8s).
      const connected = instances[2];
      connected.readyState = SeqWebSocket.CLOSED;
      connected.onclose({ code: 1006, reason: 'dropped after connect' });

      await vi.advanceTimersByTimeAsync(1900);
      expect(attempts).toBe(3); // not yet — still short of the 2s base delay
      await vi.advanceTimersByTimeAsync(200);
      expect(attempts).toBe(4); // fired at ~2s, proving the reset (not 8s)
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not put cloud access tokens in websocket URLs', async () => {
    const urls = installCapturingWebSocket();
    window.__VIOLA_API_KEY__ = '';
    const { setCloudSession } = await import('../config');
    setCloudSession({
      access_token: 'cloud-access-jwt',
      expires_at: Math.floor(Date.now() / 1000) + 3600,
    });
    global.fetch = vi.fn(() =>
      Promise.resolve({
        ok: false,
        json: async () => ({}),
      })
    );

    await renderHookClient();

    await waitFor(() => expect(urls).toHaveLength(1));
    expect(urls[0]).not.toContain('access_token=');
    expect(urls[0]).not.toContain('cloud-access-jwt');
  });
});

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, cleanup, render, waitFor } from '@testing-library/react';

// getWebSocketAuthToken is mocked so the hook never hits a real /v1/ws/auth.
vi.mock('../lib/ws_auth', () => ({
  getWebSocketAuthToken: vi.fn(() => Promise.resolve('ticket-123')),
}));

import { useAgentBrowserStream } from './useAgentBrowserStream';
import { getWebSocketAuthToken } from '../lib/ws_auth';

// A controllable fake WebSocket: tests reach in via `instances` to fire events.
function installFakeWebSocket() {
  const instances = [];

  class FakeWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;

    constructor(url) {
      this.url = url;
      this.readyState = FakeWebSocket.CONNECTING;
      this.binaryType = 'blob';
      this.sent = [];
      this.closed = null;
      this.onopen = null;
      this.onmessage = null;
      this.onerror = null;
      this.onclose = null;
      instances.push(this);
    }

    send(data) {
      this.sent.push(data);
    }

    close(code, reason) {
      this.readyState = FakeWebSocket.CLOSED;
      this.closed = { code, reason };
    }

    // --- test helpers ---
    _open() {
      this.readyState = FakeWebSocket.OPEN;
      if (this.onopen) this.onopen({});
    }

    _emitJson(obj) {
      if (this.onmessage) this.onmessage({ data: JSON.stringify(obj) });
    }

    _emitBinary(arrayBuffer) {
      if (this.onmessage) this.onmessage({ data: arrayBuffer });
    }

    _emitClose(code, reason) {
      this.readyState = FakeWebSocket.CLOSED;
      if (this.onclose) this.onclose({ code, reason });
    }
  }

  FakeWebSocket.prototype.CONNECTING = 0;
  FakeWebSocket.prototype.OPEN = 1;
  FakeWebSocket.prototype.CLOSING = 2;
  FakeWebSocket.prototype.CLOSED = 3;

  global.WebSocket = FakeWebSocket;
  return instances;
}

// Render the hook into a probe component that exposes the latest result.
function renderStream(initialEnabled) {
  const ref = { current: null };

  // eslint-disable-next-line react/prop-types
  function Probe({ enabled }) {
    ref.current = useAgentBrowserStream({ enabled });
    return null;
  }

  const utils = render(<Probe enabled={initialEnabled} />);
  // Spread utils first so our typed `rerender` wins over RTL's raw one.
  return { ...utils, ref, rerender: (enabled) => utils.rerender(<Probe enabled={enabled} />) };
}

describe('useAgentBrowserStream', () => {
  let createSpy;
  let revokeSpy;
  let urlCounter;

  beforeEach(() => {
    urlCounter = 0;
    createSpy = vi.fn(() => `blob:frame-${(urlCounter += 1)}`);
    revokeSpy = vi.fn();
    global.URL.createObjectURL = createSpy;
    global.URL.revokeObjectURL = revokeSpy;
    window.__VIOLA_BASE_URL__ = 'http://localhost:8756';
    getWebSocketAuthToken.mockClear();
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it('does not open a socket while disabled', async () => {
    const instances = installFakeWebSocket();
    renderStream(false);
    // Give any async connect a tick.
    await act(async () => { await Promise.resolve(); });
    expect(instances).toHaveLength(0);
  });

  it('opens /ws/agent-browser with the ws-auth ticket as a query token', async () => {
    const instances = installFakeWebSocket();
    renderStream(true);

    await waitFor(() => expect(instances).toHaveLength(1));
    const ws = instances[0];
    expect(ws.url).toBe('ws://localhost:8756/ws/agent-browser?token=ticket-123');
    expect(ws.binaryType).toBe('arraybuffer');
    expect(getWebSocketAuthToken).toHaveBeenCalled();
  });

  it('turns binary frames into image/jpeg blob URLs and crossfades + revokes', async () => {
    const instances = installFakeWebSocket();
    const { ref } = renderStream(true);
    await waitFor(() => expect(instances).toHaveLength(1));
    const ws = instances[0];

    act(() => { ws._open(); });
    act(() => { ws._emitBinary(new ArrayBuffer(8)); });

    await waitFor(() => expect(ref.current.frameSrc).toBe('blob:frame-1'));
    expect(ref.current.frameFade).toBe(null);
    // createObjectURL was given a Blob whose type is image/jpeg.
    const blobArg = createSpy.mock.calls[0][0];
    expect(blobArg).toBeInstanceOf(Blob);
    expect(blobArg.type).toBe('image/jpeg');

    vi.useFakeTimers();
    act(() => { ws._emitBinary(new ArrayBuffer(8)); });
    // New frame is current, old becomes the fade layer.
    expect(ref.current.frameSrc).toBe('blob:frame-2');
    expect(ref.current.frameFade).toBe('blob:frame-1');
    // The old URL is revoked after the crossfade delay.
    act(() => { vi.advanceTimersByTime(400); });
    expect(revokeSpy).toHaveBeenCalledWith('blob:frame-1');
  });

  it('exposes ready / session metadata on stream_ready', async () => {
    const instances = installFakeWebSocket();
    const { ref } = renderStream(true);
    await waitFor(() => expect(instances).toHaveLength(1));
    const ws = instances[0];

    act(() => { ws._open(); });
    act(() => {
      ws._emitJson({
        type: 'stream_ready',
        session_id: 'abcd1234-3',
        viewer_id: 3,
        frame_format: 'jpeg',
      });
    });

    await waitFor(() => expect(ref.current.ready).toBe(true));
    expect(ref.current.status).toBe('ready');
    expect(ref.current.sessionId).toBe('abcd1234-3');
    expect(ref.current.viewerId).toBe(3);
    expect(ref.current.frameFormat).toBe('jpeg');
  });

  it('surfaces a terminal stream_error (no_agent_browser) and stops reconnecting', async () => {
    const instances = installFakeWebSocket();
    vi.useFakeTimers();
    const { ref } = renderStream(true);
    await vi.waitFor(() => expect(instances).toHaveLength(1));
    const ws = instances[0];

    act(() => { ws._open(); });
    act(() => {
      ws._emitJson({
        type: 'stream_error',
        code: 'no_agent_browser',
        message: 'Viola is not browsing right now.',
      });
    });

    expect(ref.current.streamError).toEqual({
      code: 'no_agent_browser',
      message: 'Viola is not browsing right now.',
    });
    expect(ref.current.status).toBe('error');

    // Server closes after the error; the hook must NOT reconnect.
    act(() => { ws._emitClose(1011, 'No agent browser to stream'); });
    act(() => { vi.advanceTimersByTime(5000); });
    expect(instances).toHaveLength(1);
  });

  it('maps a 1008 plan close code to a plan_tier_required stage error', async () => {
    const instances = installFakeWebSocket();
    vi.useFakeTimers();
    const { ref } = renderStream(true);
    await vi.waitFor(() => expect(instances).toHaveLength(1));
    const ws = instances[0];

    act(() => { ws._open(); });
    act(() => { ws._emitClose(1008, 'plan_tier_required'); });

    expect(ref.current.streamError).toEqual({
      code: 'plan_tier_required',
      message: 'plan_tier_required',
    });
    expect(ref.current.status).toBe('error');

    // Terminal: no reconnect spin.
    act(() => { vi.advanceTimersByTime(5000); });
    expect(instances).toHaveLength(1);
  });

  it('reconnects after a transient close while still enabled', async () => {
    const instances = installFakeWebSocket();
    vi.useFakeTimers();
    renderStream(true);
    await vi.waitFor(() => expect(instances).toHaveLength(1));
    const ws = instances[0];

    act(() => { ws._open(); });
    // A non-policy close (e.g. network blip) should schedule a reconnect.
    act(() => { ws._emitClose(1006, 'abnormal'); });
    act(() => { vi.advanceTimersByTime(2100); });
    await vi.waitFor(() => expect(instances.length).toBeGreaterThanOrEqual(2));
  });

  // #1061: on the real /ws/agent-browser route, EVERY pre-accept rejection
  // (kill-switch, origin, auth, plan-tier) reaches the browser identically —
  // the handshake never opens and the close code is opaque (uvicorn collapses
  // a pre-accept `websocket.close` to a bare HTTP 403, so the browser only
  // ever sees an abnormal closure with no distinguishing code/reason; see the
  // hook's module docstring). Simulate that here: `_emitClose` WITHOUT a prior
  // `_open()`. Pre-fix, the hook retried this every RECONNECT_DELAY_MS forever
  // and left the stage stuck on "Connecting to browser view" — never showing
  // any of BrowserMode's STREAM_ERROR_MESSAGES. This proves it now gives up
  // after a bounded number of attempts and surfaces an honest error instead.
  it('stops retrying and surfaces stream_unavailable after repeated never-opened closes (#1061)', async () => {
    const instances = installFakeWebSocket();
    vi.useFakeTimers();
    const { ref } = renderStream(true);

    // Three consecutive handshake-level rejections, each never reaching onopen.
    for (let attempt = 0; attempt < 3; attempt += 1) {
      await vi.waitFor(() => expect(instances.length).toBe(attempt + 1));
      act(() => { instances[attempt]._emitClose(1006, ''); });
      if (attempt < 2) {
        act(() => { vi.advanceTimersByTime(2100); });
      }
    }

    expect(ref.current.streamError).toEqual({
      code: 'stream_unavailable',
      message: 'Live browsing could not connect.',
    });
    expect(ref.current.status).toBe('error');

    // Terminal: no further reconnect attempts, no infinite hammering.
    act(() => { vi.advanceTimersByTime(10_000); });
    expect(instances).toHaveLength(3);
  });

  it('a never-opened close still retries below the cap (does not go terminal on the first rejection)', async () => {
    const instances = installFakeWebSocket();
    vi.useFakeTimers();
    const { ref } = renderStream(true);
    await vi.waitFor(() => expect(instances).toHaveLength(1));

    act(() => { instances[0]._emitClose(1006, ''); });
    // Not yet terminal — a single rejection could still be a transient blip.
    expect(ref.current.streamError).toBe(null);

    act(() => { vi.advanceTimersByTime(2100); });
    await vi.waitFor(() => expect(instances.length).toBe(2));
  });

  it('sendInput sends agent_browser_input only when the socket is OPEN', async () => {
    const instances = installFakeWebSocket();
    const { ref } = renderStream(true);
    await waitFor(() => expect(instances).toHaveLength(1));
    const ws = instances[0];

    // Before open: nothing sent, returns false.
    let sent;
    act(() => { sent = ref.current.sendInput({ type: 'mouse_click', x_ratio: 0.5 }); });
    expect(sent).toBe(false);
    expect(ws.sent).toHaveLength(0);

    act(() => { ws._open(); });
    act(() => { sent = ref.current.sendInput({ type: 'text_input', text: 'hi' }); });
    expect(sent).toBe(true);
    expect(JSON.parse(ws.sent[ws.sent.length - 1])).toEqual({
      action: 'agent_browser_input',
      payload: { type: 'text_input', text: 'hi' },
    });
  });

  it('flags input_blocked without closing the stream', async () => {
    const instances = installFakeWebSocket();
    const { ref } = renderStream(true);
    await waitFor(() => expect(instances).toHaveLength(1));
    const ws = instances[0];

    act(() => { ws._open(); });
    act(() => { ws._emitJson({ type: 'input_blocked', code: 'credential_input_blocked' }); });

    await waitFor(() => expect(ref.current.inputBlocked).toBe(true));
    // Stream stays open: still no terminal error, socket not closed by us.
    expect(ref.current.streamError).toBe(null);
    expect(ws.closed).toBe(null);
  });

  it('closes the socket and clears state when disabled', async () => {
    const instances = installFakeWebSocket();
    const { ref, rerender } = renderStream(true);
    await waitFor(() => expect(instances).toHaveLength(1));
    const ws = instances[0];

    act(() => { ws._open(); });
    act(() => { ws._emitBinary(new ArrayBuffer(8)); });
    await waitFor(() => expect(ref.current.frameSrc).toBe('blob:frame-1'));

    await act(async () => { rerender(false); });

    await waitFor(() => expect(ref.current.frameSrc).toBe(null));
    expect(ws.closed).not.toBe(null);
    expect(ref.current.status).toBe('idle');
    // The shown frame URL was revoked on teardown.
    expect(revokeSpy).toHaveBeenCalledWith('blob:frame-1');
  });

  it('cleans up the socket and frame URL on unmount', async () => {
    const instances = installFakeWebSocket();
    const { ref, unmount } = renderStream(true);
    await waitFor(() => expect(instances).toHaveLength(1));
    const ws = instances[0];

    act(() => { ws._open(); });
    act(() => { ws._emitBinary(new ArrayBuffer(8)); });
    await waitFor(() => expect(ref.current.frameSrc).toBe('blob:frame-1'));

    unmount();
    expect(ws.closed).not.toBe(null);
    expect(revokeSpy).toHaveBeenCalledWith('blob:frame-1');
  });
});

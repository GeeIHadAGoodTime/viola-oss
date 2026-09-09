import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import useCallAudio, { fetchCallHistory, getCallTranscript } from './useCallAudio';
import { authFetch } from './useViolaApi';

vi.mock('./useViolaApi', () => ({
  authFetch: vi.fn(),
}));

function installWebSocketMock() {
  const sockets = [];

  class TestWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;

    constructor(url) {
      this.url = url;
      this.readyState = TestWebSocket.OPEN;
      this.binaryType = 'blob';
      this.send = vi.fn();
      sockets.push(this);
    }

    close() {
      this.readyState = TestWebSocket.CLOSED;
      if (this.onclose) this.onclose();
    }
  }

  TestWebSocket.prototype.CONNECTING = 0;
  TestWebSocket.prototype.OPEN = 1;
  TestWebSocket.prototype.CLOSING = 2;
  TestWebSocket.prototype.CLOSED = 3;

  global.WebSocket = TestWebSocket;
  return sockets;
}

describe('useCallAudio', () => {
  beforeEach(() => {
    authFetch.mockReset();
    window.__VIOLA_BASE_URL__ = 'http://localhost:8756';
    window.__VIOLA_API_KEY__ = 'desktop-key'; // pragma: allowlist secret
  });

  it('appends transcripts and replaces trailing partials for the same role', async () => {
    const sockets = installWebSocketMock();
    authFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ token: 'ws-token' }),
    });

    const { result } = renderHook(() => useCallAudio('call-123'));

    await act(async () => {
      await result.current.startListening();
    });
    expect(sockets).toHaveLength(1);

    act(() => {
      sockets[0].onopen();
      sockets[0].onmessage({
        data: JSON.stringify({
          type: 'transcript',
          role: 'viola',
          text: 'I am',
          partial: true,
          ts: 1,
        }),
      });
    });
    expect(result.current.transcripts).toEqual([
      { role: 'viola', text: 'I am', partial: true, ts: 1 },
    ]);

    act(() => {
      sockets[0].onmessage({
        data: JSON.stringify({
          type: 'transcript',
          role: 'viola',
          text: 'I am checking',
          partial: true,
          ts: 2,
        }),
      });
    });
    expect(result.current.transcripts).toEqual([
      { role: 'viola', text: 'I am checking', partial: true, ts: 2 },
    ]);

    act(() => {
      sockets[0].onmessage({
        data: JSON.stringify({
          type: 'transcript',
          role: 'viola',
          text: 'I am checking.',
          partial: false,
          ts: 3,
        }),
      });
      sockets[0].onmessage({
        data: JSON.stringify({
          type: 'transcript',
          role: 'them',
          text: 'Thank you',
          partial: false,
          ts: 4,
        }),
      });
    });

    expect(result.current.transcripts).toEqual([
      { role: 'viola', text: 'I am checking.', partial: false, ts: 3 },
      { role: 'them', text: 'Thank you', partial: false, ts: 4 },
    ]);
  });

  it('guards against a concurrent double-invoke of startListening: no leaked socket, wsRef stays consistent (#2770)', async () => {
    const sockets = installWebSocketMock();
    authFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ token: 'ws-token' }),
    });

    const { result } = renderHook(() => useCallAudio('call-123'));

    // Simulate a rapid double-click on "Listen": fire startListening() twice
    // before either invocation's async auth-token mint has resolved. Before
    // the fix, both invocations saw wsRef.current as null (it's only
    // assigned after the await) and both opened a socket, with the second
    // overwriting wsRef.current and orphaning the first.
    await act(async () => {
      await Promise.all([
        result.current.startListening(),
        result.current.startListening(),
      ]);
    });

    // Exactly one socket should exist — the second invocation must bail out
    // synchronously via the in-flight guard instead of racing past it.
    expect(sockets).toHaveLength(1);

    act(() => {
      sockets[0].onopen();
    });
    expect(result.current.isListening).toBe(true);

    // stopListening must reliably close the one live socket — this would
    // fail under the old bug if a second, orphaned socket's close/error
    // handler had already nulled out wsRef out from under the live one.
    act(() => {
      result.current.stopListening();
    });
    expect(sockets[0].readyState).toBe(3); // CLOSED
    expect(result.current.isListening).toBe(false);

    // wsRef must have been cleaned up correctly (not left stuck by a
    // corrupted ref) — a subsequent startListening opens a fresh socket.
    await act(async () => {
      await result.current.startListening();
    });
    expect(sockets).toHaveLength(2);
  });

  it('hangs up through the direct phone call DELETE endpoint', async () => {
    authFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, ended: true }),
    });

    const { result } = renderHook(() => useCallAudio('call/with space'));
    let response;

    await act(async () => {
      response = await result.current.endCall('call/with space');
    });

    expect(authFetch).toHaveBeenCalledWith('/v1/phone/calls/call%2Fwith%20space', {
      method: 'DELETE',
    });
    expect(response).toEqual({ ok: true, ended: true });
  });

  it('posts operator messages and appends an optimistic system transcript', async () => {
    authFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, data: { message: 'Note delivered to agent.' } }),
    });

    const { result } = renderHook(() => useCallAudio('call-123'));
    let response;

    await act(async () => {
      response = await result.current.sendOperatorMessage('  Refuse fluoride add-ons  ', 'call/with space');
    });

    expect(authFetch).toHaveBeenCalledWith('/v1/phone/calls/call%2Fwith%20space/operator-message', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: 'Refuse fluoride add-ons' }),
    });
    expect(result.current.transcripts).toHaveLength(1);
    expect(result.current.transcripts[0]).toMatchObject({
      role: 'system',
      text: '[operator] Refuse fluoride add-ons',
      partial: false,
      _optimistic: true,
    });
    expect(response).toEqual({ ok: true, data: { message: 'Note delivered to agent.' } });
  });

  it('requests owner takeover through the conference endpoint', async () => {
    authFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, data: { conference_status: 'conference_dialing' } }),
    });

    const { result } = renderHook(() => useCallAudio('call-123'));
    let response;

    await act(async () => {
      response = await result.current.requestOwnerTakeover(
        'call/with space',
        'Recipient requested the owner join this call.'
      );
    });

    expect(authFetch).toHaveBeenCalledWith('/v1/phone/calls/call%2Fwith%20space/takeover', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: 'Recipient requested the owner join this call.' }),
    });
    expect(response).toEqual({ ok: true, data: { conference_status: 'conference_dialing' } });
  });

  it('fetches paginated call history from the phone endpoint', async () => {
    authFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        ok: true,
        error: null,
        data: {
          count: 1,
          calls: [{ call_id: 'call-1', phone_number: '+1 555 0100' }],
        },
      }),
    });

    const result = await fetchCallHistory(25, 50);

    expect(authFetch).toHaveBeenCalledWith('/v1/phone/history?limit=25&offset=50');
    expect(result).toEqual({
      count: 1,
      calls: [{ call_id: 'call-1', phone_number: '+1 555 0100' }],
    });
  });

  it('fetches a call transcript from the owner-scoped phone endpoint', async () => {
    authFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        ok: true,
        error: null,
        data: {
          call_id: 'call/with space',
          transcript: [{ role: 'them', text: 'Hello' }],
        },
      }),
    });

    const result = await getCallTranscript('call/with space');

    expect(authFetch).toHaveBeenCalledWith('/v1/phone/calls/call%2Fwith%20space/transcript');
    expect(result).toEqual({
      call_id: 'call/with space',
      transcript: [{ role: 'them', text: 'Hello' }],
    });
  });
});

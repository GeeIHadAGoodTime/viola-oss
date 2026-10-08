import { useEffect } from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SpokeWrapper from './SpokeWrapper';
import { getWebSocketAuthToken } from '../lib/ws_auth';

const policy = vi.hoisted(() => ({ enabled: true }));
vi.mock('../SmartDisplay', () => ({ default: function Display({ onVoiceCaptureEnabledChange }) {
  useEffect(() => { onVoiceCaptureEnabledChange?.(policy.enabled); }, [onVoiceCaptureEnabledChange]);
  return <>
    <button onClick={() => onVoiceCaptureEnabledChange?.(false)}>Apply voice mute</button>
    <button onClick={() => onVoiceCaptureEnabledChange?.(true)}>Enable voice</button>
    <button onClick={() => { onVoiceCaptureEnabledChange?.(false); onVoiceCaptureEnabledChange?.(true); }}>Replace principal</button>
  </>;
} }));
vi.mock('../hooks/useAuth', () => ({ AuthProvider: ({ children }) => children }));
vi.mock('../hooks/useFullscreen', () => ({ useFullscreen: () => ({ isSupported: false }) }));
vi.mock('../hooks/useWakeLock', () => ({ useWakeLock: () => ({ isSupported: false }) }));
vi.mock('../lib/ws_auth', () => ({ getWebSocketAuthToken: vi.fn() }));
vi.mock('../utils/ttsPlayback', () => ({ prewarmTtsContext: vi.fn(), playTtsPcm: vi.fn(), isTtsFrame: () => false, decodeTtsFrame: vi.fn() }));
vi.mock('../utils/spokeAudioEngine', () => ({ SpokeAudioEngine: class {
  start() { return Promise.resolve(); }
  stop() {}
} }));
let grant, stop, contexts, sockets, getUserMedia;
class Context {
  constructor() { contexts.push(this); this.sampleRate = 16000; }
  createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
  createScriptProcessor() { return { connect() {}, disconnect() {} }; }
  close() { return Promise.resolve(); }
}
class Socket {
  static OPEN = 1;
  constructor() { sockets.push(this); this.readyState = 0; }
  send() {}
  close() { this.readyState = 3; }
}
beforeEach(() => {
  policy.enabled = true; localStorage.clear(); contexts = []; sockets = []; stop = vi.fn();
  getWebSocketAuthToken.mockReset().mockResolvedValue('synthetic-token');
  getUserMedia = vi.fn(() => new Promise(resolve => { grant = resolve; }));
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia } });
  vi.stubGlobal('AudioContext', Context); vi.stubGlobal('WebSocket', Socket);
});
afterEach(() => { vi.unstubAllGlobals(); delete navigator.mediaDevices; });
async function connect() {
  render(<SpokeWrapper room="synthetic-room" />);
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Connect as Speaker/ })); });
}
async function grantPermission() {
  const trackStop = stop;
  const stream = { active: true, getTracks: () => [{ stop: trackStop }] };
  trackStop.mockImplementation(() => { stream.active = false; });
  await act(async () => { grant(stream); });
}

describe('real spoke wrapper and continuous capture hook', () => {
  it('cancels permission when the user switches the mic back off before it connects', async () => {
    await connect();
    fireEvent.click(screen.getByRole('button', { name: /wake word mic/ }));
    expect(getUserMedia).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole('button', { name: /wake word mic/ }));
    await grantPermission();
    expect(stop).toHaveBeenCalledOnce(); expect(contexts).toHaveLength(0); expect(sockets).toHaveLength(0);
    expect(screen.getByText('Wake OFF')).toBeInTheDocument();
  });

  it('does not restore a saved wake preference through a globally disabled voice gate', async () => {
    policy.enabled = false; localStorage.setItem('viola_spoke_mic', 'true');
    await connect();
    expect(getUserMedia).not.toHaveBeenCalled();
    expect(screen.getByText('Wake OFF')).toBeInTheDocument();
  });

  it('stops hook-owned capture on the real muted-policy handoff despite stream echo', async () => {
    await connect(); fireEvent.click(screen.getByRole('button', { name: /wake word mic/ }));
    await grantPermission();
    act(() => { sockets[0].readyState = 1; sockets[0].onopen(); });
    expect(screen.getByText('Wake ON')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Apply voice mute' }));
    expect(stop).toHaveBeenCalledOnce(); expect(sockets[0].readyState).toBe(3);
    expect(screen.getByText('Wake OFF')).toBeInTheDocument();
  });
  it('retires the old principal even when the next enabled policy arrives in the same batch', async () => {
    await connect(); fireEvent.click(screen.getByRole('button', { name: /wake word mic/ }));
    await grantPermission();
    act(() => { sockets[0].readyState = 1; sockets[0].onopen(); });
    fireEvent.click(screen.getByRole('button', { name: 'Replace principal' }));
    expect(stop).toHaveBeenCalledOnce(); expect(sockets[0].readyState).toBe(3);
  });

  it('respects a saved opt-in when the known global policy is deliberately re-enabled', async () => {
    policy.enabled = false; localStorage.setItem('viola_spoke_mic', 'true');
    await connect(); expect(getUserMedia).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Enable voice' }));
    expect(getUserMedia).toHaveBeenCalledOnce();
    await grantPermission();
    act(() => { sockets[0].readyState = 1; sockets[0].onopen(); });
    expect(screen.getByText('Wake ON')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /wake word mic/ }));
    expect(stop).toHaveBeenCalledOnce(); expect(screen.getByText('Wake OFF')).toBeInTheDocument();
  });

});

it('independent review: restarts an enabled principal changed while permission is pending', async () => {
  await connect();
  fireEvent.click(screen.getByRole('button', { name: /wake word mic/ }));
  expect(getUserMedia).toHaveBeenCalledTimes(1);
  const obsoleteGrant = grant;
  fireEvent.click(screen.getByRole('button', { name: 'Replace principal' }));
  await act(async () => {});
  // The old request was retired, so saved enabled intent needs a new owner.
  expect(getUserMedia).toHaveBeenCalledTimes(2);
  const oldStop = vi.fn();
  await act(async () => { obsoleteGrant({ active: true, getTracks: () => [{ stop: oldStop }] }); });
  expect(oldStop).toHaveBeenCalledOnce();
  expect(contexts).toHaveLength(0);
});


it.each(['permission', 'auth', 'handshake'])('starts a fresh principal while its predecessor waits for %s', async (stage) => {
  let completeAuth;
  if (stage === 'auth') getWebSocketAuthToken.mockReturnValueOnce(new Promise(resolve => { completeAuth = resolve; }));
  await connect();
  fireEvent.click(screen.getByRole('button', { name: /wake word mic/ }));
  const oldGrant = grant;
  if (stage !== 'permission') await grantPermission();
  const oldOpen = sockets[0]?.onopen;
  fireEvent.click(screen.getByRole('button', { name: 'Replace principal' }));
  expect(getUserMedia).toHaveBeenCalledTimes(2);
  if (stage === 'permission') {
    const oldStop = vi.fn();
    await act(async () => { oldGrant({ active: true, getTracks: () => [{ stop: oldStop }] }); });
    expect(oldStop).toHaveBeenCalledOnce(); expect(contexts).toHaveLength(0);
  } else {
    expect(stop).toHaveBeenCalledOnce();
  }
  if (stage === 'auth') {
    await act(async () => { completeAuth('obsolete-token'); });
    expect(sockets).toHaveLength(0);
  }
  if (stage === 'handshake') {
    act(() => { oldOpen(); });
    expect(screen.getByText('Wake OFF')).toBeInTheDocument();
  }
  stop = vi.fn();
  await grantPermission();
  act(() => { const fresh = sockets.at(-1); fresh.readyState = 1; fresh.onopen(); });
  expect(screen.getByText('Wake ON')).toBeInTheDocument(); expect(stop).not.toHaveBeenCalled();
});

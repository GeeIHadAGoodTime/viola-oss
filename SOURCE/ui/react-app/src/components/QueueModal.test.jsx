import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import { act, cleanup, render, renderHook, screen } from '@testing-library/react';
import QueueModal from './QueueModal';
import { usePlayerState } from '../hooks/usePlayerState';
const ws = vi.hoisted(() => ({
  handler: null
}));
vi.mock('../hooks/useWebSocket', () => ({
  useWebSocket: handler => {
    ws.handler = handler;
    return {
      send: vi.fn(),
      connectCount: 0
    };
  }
}));
const api = vi.hoisted(() => ({
  getState: vi.fn(),
  getQueue: vi.fn(),
  playQueueItem: vi.fn(),
  removeFromQueue: vi.fn(),
  clearQueue: vi.fn()
}));
vi.mock('../hooks/useViolaApi', () => ({
  useViolaApi: () => api
}));
const track = {
  id: 'synthetic-future-a',
  title: 'Synthetic future track'
};
afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  api.getState.mockReturnValue(new Promise(() => {}));
  api.getQueue.mockResolvedValue({
    ok: true,
    queue: [track]
  });
});
describe('Queue authoritative empty state', () => {
  it('does not resurrect removed tracks from stale HTTP fallback after an empty WebSocket queue', async () => {
    const view = render(<QueueModal isOpen onClose={() => {}} wsQueue={[track]} />);
    expect(await screen.findByText(track.title)).toBeInTheDocument();
    view.rerender(<QueueModal isOpen onClose={() => {}} wsQueue={[]} />);
    expect(screen.queryByText(track.title)).not.toBeInTheDocument();
    expect(screen.getByText('Queue is empty. Ask me to play some music!')).toBeInTheDocument();
  });
  it('does not turn a late HTTP response into newer data than an observed empty queue', async () => {
    let resolve;
    api.getQueue.mockReturnValueOnce(new Promise(done => {
      resolve = done;
    }));
    const view = render(<QueueModal isOpen onClose={() => {}} wsQueue={[track]} />);
    view.rerender(<QueueModal isOpen onClose={() => {}} wsQueue={[]} />);
    await act(async () => resolve({
      ok: true,
      queue: [track]
    }));
    expect(screen.queryByText(track.title)).not.toBeInTheDocument();
  });
});
function ActualParentQueue() {
  const state = usePlayerState();
  return <QueueModal isOpen onClose={() => {}} wsQueue={state.hasQueueSnapshot ? state.queue : undefined} />;
}
describe('Queue through actual player-state parent', () => {
  it('keeps HTTP fallback before any authoritative queue arrives', async () => {
    render(<ActualParentQueue />);
    expect(await screen.findByText(track.title)).toBeInTheDocument();
  });
  it('does not mistake an unrelated connection/mute event for a known empty queue', async () => {
    render(<ActualParentQueue />);
    expect(await screen.findByText(track.title)).toBeInTheDocument();
    act(() => ws.handler({
      type: 'hub_mute_update',
      payload: {
        yt_hub_muted: true
      }
    }));
    expect(screen.getByText(track.title)).toBeInTheDocument();
  });
  it('retires HTTP fallback after an actual empty queue state event', async () => {
    render(<ActualParentQueue />);
    expect(await screen.findByText(track.title)).toBeInTheDocument();
    act(() => ws.handler({
      type: 'state',
      payload: {
        queue: [],
        now_playing: null,
        is_playing: false
      }
    }));
    expect(screen.queryByText(track.title)).not.toBeInTheDocument();
  });
});
describe('Queue receipt ordering in actual player-state hook', () => {
  it('does not replace a newer empty queue with an older initial state response', async () => {
    let resolve;
    api.getState.mockReturnValueOnce(new Promise(done => {
      resolve = done;
    }));
    const {
      result
    } = renderHook(() => usePlayerState());
    act(() => ws.handler({
      type: 'state',
      payload: {
        queue: [],
        now_playing: null,
        is_playing: false
      }
    }));
    await act(async () => resolve({
      ok: true,
      queue: [track],
      now_playing: null
    }));
    expect(result.current.queue).toEqual([]);
  });
});
describe('Queue receipt across delayed hub publication', () => {
  afterEach(() => vi.useRealTimers());
  it('keeps a received delayed queue when a later rehydrate read fails', async () => {
    vi.useFakeTimers();
    api.getState.mockReturnValueOnce(new Promise(() => {})).mockRejectedValueOnce(new Error('synthetic read refusal'));
    const {
      result
    } = renderHook(() => usePlayerState());
    await act(async () => ws.handler({
      type: 'state',
      payload: {
        hub_local_playback_active: true,
        hub_buffer_ms: 500,
        queue: [track],
        now_playing: {
          id: 'synthetic-playing',
          title: 'Synthetic playing',
          provider: 'youtube'
        }
      }
    }));
    await act(async () => vi.advanceTimersByTimeAsync(500));
    expect(result.current.hasQueueSnapshot).toBe(true);
    expect(result.current.queue).toEqual([track]);
  });
});
describe('Queue snapshot admission controls', () => {
  const later = () => {
    let resolve;
    let reject;
    const promise = new Promise((yes, no) => {
      resolve = yes;
      reject = no;
    });
    return {
      promise,
      resolve,
      reject
    };
  };
  const queueState = (queue, extra = {}) => ({
    queue,
    now_playing: null,
    is_playing: false,
    ...extra
  });
  const hydratable = {
    id: 'synthetic-playing',
    title: 'Synthetic playing',
    provider: 'youtube'
  };
  afterEach(() => vi.useRealTimers());
  it('accepts an initially observed empty queue instead of an older modal HTTP list', async () => {
    api.getState.mockResolvedValueOnce({
      ok: true,
      queue: []
    });
    render(<ActualParentQueue />);
    await act(async () => {});
    expect(screen.queryByText(track.title)).not.toBeInTheDocument();
    expect(screen.getByText('Queue is empty. Ask me to play some music!')).toBeInTheDocument();
  });
  it('still shows the HTTP fallback after the initial state read fails', async () => {
    api.getState.mockRejectedValueOnce(new Error('synthetic unavailable'));
    render(<ActualParentQueue />);
    expect(await screen.findByText(track.title)).toBeInTheDocument();
  });
  it.each([undefined, null, {}])('does not replace a known queue with a partial/malformed field %j', async queue => {
    const {
      result
    } = renderHook(() => usePlayerState());
    act(() => ws.handler({
      type: 'state',
      payload: queueState([track])
    }));
    act(() => ws.handler({
      type: 'state',
      payload: {
        volume: 20,
        queue
      }
    }));
    expect(result.current.hasQueueSnapshot).toBe(true);
    expect(result.current.queue).toEqual([track]);
  });
  it('does not let a delayed older hub queue replace a newer empty state', async () => {
    vi.useFakeTimers();
    const {
      result
    } = renderHook(() => usePlayerState());
    act(() => ws.handler({
      type: 'state',
      payload: queueState([track], {
        hub_local_playback_active: true,
        hub_buffer_ms: 500
      })
    }));
    act(() => ws.handler({
      type: 'state',
      payload: queueState([])
    }));
    await act(async () => vi.advanceTimersByTimeAsync(500));
    expect(result.current.hasQueueSnapshot).toBe(true);
    expect(result.current.queue).toEqual([]);
  });
  it('lets a newer accepted rehydrate queue retire an older delayed publication', async () => {
    vi.useFakeTimers();
    const read = later();
    api.getState.mockReturnValueOnce(new Promise(() => {})).mockReturnValueOnce(read.promise);
    const {
      result
    } = renderHook(() => usePlayerState());
    act(() => ws.handler({
      type: 'state',
      payload: queueState([track], {
        hub_local_playback_active: true,
        hub_buffer_ms: 500,
        now_playing: hydratable
      })
    }));
    await act(async () => read.resolve({
      ok: true,
      now_playing: hydratable,
      queue: []
    }));
    await act(async () => vi.advanceTimersByTimeAsync(500));
    expect(result.current.hasQueueSnapshot).toBe(true);
    expect(result.current.queue).toEqual([]);
  });
  it('does not adopt queue data in a refused rehydrate response', async () => {
    const read = later();
    api.getState.mockReturnValueOnce(new Promise(() => {})).mockReturnValueOnce(read.promise);
    const {
      result
    } = renderHook(() => usePlayerState());
    act(() => ws.handler({
      type: 'state',
      payload: queueState([track], {
        now_playing: hydratable
      })
    }));
    await act(async () => read.resolve({
      ok: false,
      now_playing: hydratable,
      queue: []
    }));
    expect(result.current.queue).toEqual([track]);
  });
  it('retains a newer state event over a late rehydrate queue', async () => {
    const read = later();
    api.getState.mockReturnValueOnce(new Promise(() => {})).mockReturnValueOnce(read.promise);
    const {
      result
    } = renderHook(() => usePlayerState());
    act(() => ws.handler({
      type: 'state',
      payload: queueState([track], {
        now_playing: hydratable
      })
    }));
    act(() => ws.handler({
      type: 'state',
      payload: queueState([])
    }));
    await act(async () => read.resolve({
      ok: true,
      now_playing: hydratable,
      queue: [track]
    }));
    expect(result.current.queue).toEqual([]);
  });
  it('retains the newest overlapping rehydrate read when an older one finishes last', async () => {
    const first = later();
    const second = later();
    api.getState.mockReturnValueOnce(new Promise(() => {})).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const {
      result
    } = renderHook(() => usePlayerState());
    act(() => ws.handler({
      type: 'state',
      payload: {
        now_playing: hydratable
      }
    }));
    act(() => ws.handler({
      type: 'state',
      payload: {
        now_playing: {
          ...hydratable,
          id: 'synthetic-other'
        }
      }
    }));
    await act(async () => second.resolve({
      ok: true,
      now_playing: hydratable,
      queue: []
    }));
    await act(async () => first.resolve({
      ok: true,
      now_playing: hydratable,
      queue: [track]
    }));
    expect(result.current.hasQueueSnapshot).toBe(true);
    expect(result.current.queue).toEqual([]);
  });
});
describe('Queue unknown state remains a placeholder', () => {
  it('does not leak a retired initial read through the unconfirmed state object', async () => {
    let resolve;
    api.getState.mockReturnValueOnce(new Promise(done => {
      resolve = done;
    })).mockReturnValueOnce(new Promise(() => {}));
    const {
      result
    } = renderHook(() => usePlayerState());
    act(() => ws.handler({
      type: 'state',
      payload: {
        now_playing: {
          id: 'synthetic-playing',
          provider: 'youtube'
        }
      }
    }));
    await act(async () => resolve({
      ok: true,
      queue: [track],
      now_playing: null
    }));
    expect(result.current.hasQueueSnapshot).toBe(false);
    expect(result.current.queue).toEqual([]);
  });
});
describe('Queue known-state loading', () => {
  it('shows known empty state while an unrelated fallback GET remains pending', () => {
    api.getQueue.mockReturnValueOnce(new Promise(() => {}));
    render(<QueueModal isOpen onClose={() => {}} wsQueue={[]} />);
    expect(screen.getByText('Queue is empty. Ask me to play some music!')).toBeInTheDocument();
    expect(screen.queryByText('Loading queue...')).not.toBeInTheDocument();
  });
});
describe('Reviewed queue admission boundaries', () => {
  const oldTrack = {
    id: 'older-track',
    title: 'Removed earlier track'
  };
  const hydratable = {
    id: 'synthetic-embedded',
    title: 'Synthetic embedded',
    provider: 'youtube'
  };
  const deferred = () => {
    let resolve;
    const promise = new Promise(done => {
      resolve = done;
    });
    return {
      promise,
      resolve
    };
  };
  beforeEach(() => {
    api.getState.mockReset().mockReturnValue(new Promise(() => {}));
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });
  it.each([false, true].flatMap(known => ['absent', 'undefined', 'null', 'object'].map(kind => [known, kind])))('a delayed observed queue survives a later delayed partial frame (known=%s, %s)', async (known, kind) => {
    vi.useFakeTimers();
    const {
      result
    } = renderHook(() => usePlayerState());
    if (known) act(() => ws.handler({
      type: 'state',
      payload: {
        queue: [oldTrack],
        now_playing: null
      }
    }));
    act(() => ws.handler({
      type: 'state',
      payload: {
        queue: [],
        now_playing: null,
        hub_local_playback_active: true,
        hub_buffer_ms: 500
      }
    }));
    const partial = {
      volume: 30,
      hub_local_playback_active: true,
      hub_buffer_ms: 500
    };
    if (kind !== 'absent') partial.queue = kind === 'undefined' ? undefined : kind === 'null' ? null : {};
    act(() => ws.handler({
      type: 'state',
      payload: partial
    }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect({
      known: result.current.hasQueueSnapshot,
      queue: result.current.queue
    }).toEqual({
      known: true,
      queue: []
    });
  });
  it.each([false, true].flatMap(known => [false, true].map(wrapped => [known, wrapped])))('a successful queue snapshot does not require non-null track metadata (known=%s, wrapped=%s)', async (known, wrapped) => {
    const read = deferred();
    api.getState.mockReturnValueOnce(new Promise(() => {})).mockReturnValueOnce(read.promise);
    const {
      result
    } = renderHook(() => usePlayerState());
    if (known) act(() => ws.handler({
      type: 'state',
      payload: {
        queue: [oldTrack],
        now_playing: null
      }
    }));
    act(() => ws.handler({
      type: 'state',
      payload: {
        now_playing: hydratable
      }
    }));
    expect(api.getState).toHaveBeenCalledTimes(2);
    const payload = {
      queue: [],
      now_playing: null
    };
    await act(async () => {
      read.resolve(wrapped ? {
        ok: true,
        data: payload
      } : {
        ok: true,
        ...payload
      });
    });
    expect({
      known: result.current.hasQueueSnapshot,
      queue: result.current.queue
    }).toEqual({
      known: true,
      queue: []
    });
  });
  it('unrelated delayed frames do not postpone the observed queue deadline', async () => {
    vi.useFakeTimers();
    const {
      result
    } = renderHook(() => usePlayerState());
    act(() => ws.handler({
      type: 'state',
      payload: {
        queue: [oldTrack],
        now_playing: null
      }
    }));
    act(() => ws.handler({
      type: 'state',
      payload: {
        queue: [],
        now_playing: null,
        hub_local_playback_active: true,
        hub_buffer_ms: 500
      }
    }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400);
    });
    act(() => ws.handler({
      type: 'state',
      payload: {
        volume: 30,
        hub_local_playback_active: true,
        hub_buffer_ms: 500
      }
    }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(result.current.queue).toEqual([]);
  });
});
describe('Queue delay lifetime', () => {
  afterEach(() => vi.useRealTimers());
  it('does not publish an observed empty queue before its original delay', async () => {
    vi.useFakeTimers();
    const {
      result
    } = renderHook(() => usePlayerState());
    act(() => ws.handler({
      type: 'state',
      payload: {
        queue: [track]
      }
    }));
    act(() => ws.handler({
      type: 'state',
      payload: {
        queue: [],
        hub_local_playback_active: true,
        hub_buffer_ms: 500
      }
    }));
    await act(async () => vi.advanceTimersByTimeAsync(499));
    expect(result.current.queue).toEqual([track]);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(result.current.queue).toEqual([]);
  });
  it('clears both independently owned delays on unmount', () => {
    vi.useFakeTimers();
    const {
      unmount
    } = renderHook(() => usePlayerState());
    act(() => ws.handler({
      type: 'state',
      payload: {
        queue: [track],
        hub_local_playback_active: true,
        hub_buffer_ms: 500
      }
    }));
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});

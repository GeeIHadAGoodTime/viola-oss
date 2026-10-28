/**
 * X8.8 (1.0.5 build 5): a background agent that finished in under the 2 s
 * poll never appeared in `active`, so its stream was never opened and its
 * result never reached Chat History. Results of agents that finish between
 * polls are now read once from `recent_completed`; agents already finished
 * when the page first looked are not replayed.
 */
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ responses: [] }));
vi.mock('./useViolaApi', () => ({
  authFetch: vi.fn(async () => {
    const data = api.responses.length > 1 ? api.responses.shift() : api.responses[0];
    return { ok: true, json: async () => ({ ok: true, data }) };
  }),
  buildStreamUrl: vi.fn(async (streamId) => `http://localhost/v1/stream/${streamId}`),
}));
vi.mock('../utils/featureSurface', () => ({ isFeatureAvailable: () => true }));

let sources;
class FakeEventSource {
  constructor(url) { this.url = url; this.closed = false; sources.push(this); }
  close() { this.closed = true; }
  emit(payload) { this.onmessage?.({ data: JSON.stringify(payload) }); }
}

const done = (id) => ({ agent_id: id, id, stream_id: `stream-${id}`, status: 'completed', task: 'Look it up' });

beforeEach(() => {
  vi.useFakeTimers();
  sources = [];
  vi.stubGlobal('EventSource', FakeEventSource);
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

async function mount(onAgentResult) {
  const { useAgentRegistry } = await import('./useAgentRegistry');
  const hook = renderHook(() => useAgentRegistry({ onAgentResult }));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  return hook;
}

describe('agents that finish between polls', () => {
  it('reads the final result of an agent never seen as active', async () => {
    const onAgentResult = vi.fn();
    api.responses = [
      { active: [], recent_completed: [] },
      { active: [], recent_completed: [done('quick')] },
    ];
    await mount(onAgentResult);
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });

    expect(sources.map((source) => source.url)).toEqual(['http://localhost/v1/stream/stream-quick']);
    act(() => { sources[0].emit({ done: true, content: 'The answer is 12.' }); });

    expect(onAgentResult).toHaveBeenCalledTimes(1);
    expect(onAgentResult.mock.calls[0][0].content).toBe('The answer is 12.');
    expect(sources[0].closed).toBe(true);

    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(sources).toHaveLength(1);
  });

  it('does not replay agents that were already finished when the page loaded', async () => {
    const onAgentResult = vi.fn();
    api.responses = [{ active: [], recent_completed: [done('old')] }];
    await mount(onAgentResult);
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });

    expect(sources).toHaveLength(0);
    expect(onAgentResult).not.toHaveBeenCalled();
  });
});

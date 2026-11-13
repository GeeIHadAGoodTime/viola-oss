/**
 * 1.0.5 build 10 (bug 18): the background-agent card streamed the helper's raw
 * inner reasoning (`thinking` stream events). The card now shows only the
 * plain step the agent reports for each tool (`tool.progress`).
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

const running = { agent_id: 'helper', id: 'helper', stream_id: 'stream-helper', status: 'running', task: 'Compare cellos' };

beforeEach(() => {
  vi.useFakeTimers();
  sources = [];
  vi.stubGlobal('EventSource', FakeEventSource);
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('background agent card activity', () => {
  it('shows the reported step and never the reasoning text', async () => {
    api.responses = [{ active: [running], recent_completed: [] }];
    const { useAgentRegistry } = await import('./useAgentRegistry');
    const { result } = renderHook(() => useAgentRegistry({ onAgentResult: vi.fn() }));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(sources).toHaveLength(1);

    act(() => { sources[0].emit({ thinking: 'The user probably wants the cheaper one, so I should' }); });
    const afterThinking = result.current.streamStates.helper || {};
    expect(JSON.stringify(afterThinking)).not.toContain('cheaper one');

    act(() => { sources[0].emit({ tool: { tool_name: 'web_search', status: 'working', progress: 'step 2: searching' } }); });
    expect(result.current.streamStates.helper.activityText).toBe('step 2: searching');
    expect(JSON.stringify(result.current.streamStates.helper)).not.toContain('cheaper one');
  });
});

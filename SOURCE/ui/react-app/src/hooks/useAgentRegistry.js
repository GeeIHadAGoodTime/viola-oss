import { useCallback, useEffect, useRef, useState } from 'react';
import { authFetch, buildStreamUrl } from './useViolaApi';
import { isFeatureAvailable } from '../utils/featureSurface';

// Background agents run on the desktop hub. The credential-less cloud SPA must
// not poll these routes, but a paired multiroom spoke is hub-backed and should
// see the same agent state as the desktop SmartDisplay.

const POLL_INTERVAL_MS = 2000;
const MAX_AGENT_STREAMS = 5;
const STREAM_STATE_RETENTION_MS = 10000;

function normalizeAgentList(value) {
  return Array.isArray(value) ? value : [];
}

function terminalStatusFromPayload(payload) {
  if (payload?.error && String(payload?.message || '').toLowerCase().includes('cancel')) {
    return 'cancelled';
  }
  if (payload?.error) return 'error';
  if (payload?.done) return 'completed';
  return null;
}

function closeSource(entry) {
  if (entry?.source) {
    entry.source.close();
  }
}

export function useAgentRegistry({ onAgentResult } = {}) {
  const [active, setActive] = useState([]);
  const [recentCompleted, setRecentCompleted] = useState([]);
  const [streamStates, setStreamStates] = useState({});
  const sourcesRef = useRef(new Map());
  const onAgentResultRef = useRef(onAgentResult);

  useEffect(() => {
    onAgentResultRef.current = onAgentResult;
  }, [onAgentResult]);

  const pruneStreamStates = useCallback((nextActive, nextRecentCompleted) => {
    const keepIds = new Set([
      ...nextActive.map((agent) => agent.agent_id),
      ...nextRecentCompleted.map((agent) => agent.agent_id),
    ]);
    const now = Date.now();
    setStreamStates((prev) => {
      let changed = false;
      const next = {};
      Object.entries(prev).forEach(([agentId, state]) => {
        const terminalAgeMs = state.completedAt ? now - state.completedAt : 0;
        if (!keepIds.has(agentId) && state.completedAt && terminalAgeMs > STREAM_STATE_RETENTION_MS) {
          changed = true;
          return;
        }
        next[agentId] = state;
      });
      return changed ? next : prev;
    });
  }, []);

  const refreshAgents = useCallback(async (signal) => {
    if (!isFeatureAvailable('agents')) {
      return;
    }
    const response = await authFetch('/v1/agents', { signal });
    if (!response.ok) {
      return;
    }
    const json = await response.json();
    const data = json?.data || json || {};
    const nextActive = normalizeAgentList(data.active);
    const nextRecentCompleted = normalizeAgentList(data.recent_completed);
    setActive(nextActive);
    setRecentCompleted(nextRecentCompleted);
    pruneStreamStates(nextActive, nextRecentCompleted);
  }, [pruneStreamStates]);

  useEffect(() => {
    if (!isFeatureAvailable('agents')) {
      return undefined;
    }
    let disposed = false;
    let timerId = null;
    let controller = null;

    const tick = async () => {
      controller = new AbortController();
      try {
        await refreshAgents(controller.signal);
      } catch (err) {
        if (err?.name !== 'AbortError') {
          setActive((current) => current);
        }
      } finally {
        if (!disposed) {
          timerId = window.setTimeout(tick, POLL_INTERVAL_MS);
        }
      }
    };

    void tick();
    return () => {
      disposed = true;
      if (timerId) {
        window.clearTimeout(timerId);
      }
      if (controller) {
        controller.abort();
      }
    };
  }, [refreshAgents]);

  useEffect(() => {
    const streamableAgents = active
      .filter((agent) => agent.agent_id && agent.stream_id)
      .slice(0, MAX_AGENT_STREAMS);
    const desiredAgentIds = new Set(streamableAgents.map((agent) => agent.agent_id));

    streamableAgents.forEach((agent) => {
      const existing = sourcesRef.current.get(agent.agent_id);
      if (existing?.streamId === agent.stream_id) {
        return;
      }
      closeSource(existing);
      const pendingEntry = { source: null, streamId: agent.stream_id };
      sourcesRef.current.set(agent.agent_id, pendingEntry);

      buildStreamUrl(agent.stream_id).then((url) => {
        const current = sourcesRef.current.get(agent.agent_id);
        if (current !== pendingEntry) {
          return;
        }

        const source = new EventSource(url, { withCredentials: true });
        pendingEntry.source = source;

        source.onmessage = (event) => {
          let payload = null;
          try {
            payload = JSON.parse(event.data);
          } catch {
            return;
          }

          if (payload?.thinking) {
            setStreamStates((prev) => {
              const previous = prev[agent.agent_id] || {};
              return {
                ...prev,
                [agent.agent_id]: {
                  ...previous,
                  thinkingText: `${previous.thinkingText || ''}${payload.thinking}`,
                },
              };
            });
          }

          const terminalStatus = terminalStatusFromPayload(payload);
          if (terminalStatus) {
            source.close();
            sourcesRef.current.delete(agent.agent_id);
            const content = typeof payload.content === 'string' ? payload.content : '';
            const message = typeof payload.message === 'string' ? payload.message : '';
            setStreamStates((prev) => ({
              ...prev,
              [agent.agent_id]: {
                ...(prev[agent.agent_id] || {}),
                status: terminalStatus,
                result: content,
                errorMessage: message,
                completedAt: Date.now(),
              },
            }));
            if (terminalStatus === 'completed' && content) {
              onAgentResultRef.current?.({ agent, content, payload });
            }
          }
        };

        source.onerror = () => {
          source.close();
          sourcesRef.current.delete(agent.agent_id);
        };
      }).catch(() => {
        sourcesRef.current.delete(agent.agent_id);
      });
    });

    sourcesRef.current.forEach((entry, agentId) => {
      if (!desiredAgentIds.has(agentId)) {
        closeSource(entry);
        sourcesRef.current.delete(agentId);
      }
    });
  }, [active]);

  useEffect(() => () => {
    sourcesRef.current.forEach(closeSource);
    sourcesRef.current.clear();
  }, []);

  const cancelAgent = useCallback(async (agentId) => {
    if (!isFeatureAvailable('agents')) {
      return null;
    }
    const response = await authFetch(`/v1/agents/${encodeURIComponent(agentId)}/cancel`, {
      method: 'POST',
    });
    const json = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(json?.message || 'Unable to cancel agent.');
    }

    const entry = sourcesRef.current.get(agentId);
    closeSource(entry);
    sourcesRef.current.delete(agentId);
    setStreamStates((prev) => ({
      ...prev,
      [agentId]: {
        ...(prev[agentId] || {}),
        status: json?.data?.final_status || 'cancelled',
        completedAt: Date.now(),
      },
    }));
    void refreshAgents(new AbortController().signal).catch(() => {});
    return json?.data || json;
  }, [refreshAgents]);

  return {
    active,
    recent_completed: recentCompleted,
    streamStates,
    cancelAgent,
  };
}

export default useAgentRegistry;

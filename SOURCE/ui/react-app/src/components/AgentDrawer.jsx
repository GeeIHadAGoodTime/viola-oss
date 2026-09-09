/* eslint react/jsx-uses-vars: "error" */
import { useEffect, useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import { THEME } from '../config';
import AgentCard from './AgentCard';

const TERMINAL_VISIBLE_MS = 3000;
const MAX_VISIBLE_AGENTS = 5;

function isTerminalStatus(status) {
  return ['completed', 'cancelled', 'killed', 'error', 'failed'].includes(status);
}

function completionTime(agent, streamState) {
  if (streamState?.completedAt) {
    return streamState.completedAt;
  }
  const parsed = Date.parse(agent.completed_at || '');
  return Number.isFinite(parsed) ? parsed : 0;
}

function visibleAgentRows(active, recentCompleted, streamStates, now) {
  const rows = [];
  const seen = new Set();

  active.forEach((agent) => {
    if (!agent.agent_id || seen.has(agent.agent_id)) return;
    const streamState = streamStates[agent.agent_id] || {};
    const status = streamState.status || agent.status || 'running';
    const completedAt = completionTime(agent, streamState);
    if (isTerminalStatus(status) && completedAt && now - completedAt > TERMINAL_VISIBLE_MS) {
      return;
    }
    seen.add(agent.agent_id);
    rows.push({ agent: { ...agent, status }, thinkingText: streamState.thinkingText || '' });
  });

  recentCompleted.forEach((agent) => {
    if (!agent.agent_id || seen.has(agent.agent_id)) return;
    const streamState = streamStates[agent.agent_id] || {};
    const status = streamState.status || agent.status || 'completed';
    const completedAt = completionTime(agent, streamState);
    if (!completedAt || now - completedAt > TERMINAL_VISIBLE_MS) {
      return;
    }
    seen.add(agent.agent_id);
    rows.push({ agent: { ...agent, status }, thinkingText: streamState.thinkingText || '' });
  });

  return rows.slice(0, MAX_VISIBLE_AGENTS);
}

export default function AgentDrawer({
  active = [],
  recentCompleted = [],
  streamStates = {},
  expanded = true,
  onToggleExpanded,
  onCancel,
}) {
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const hasTerminalState = recentCompleted.length > 0
      || Object.values(streamStates).some((state) => isTerminalStatus(state.status));
    if (!hasTerminalState) {
      return undefined;
    }
    const timerId = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(timerId);
  }, [recentCompleted.length, streamStates]);

  const rows = useMemo(
    () => visibleAgentRows(active, recentCompleted, streamStates, now),
    [active, recentCompleted, streamStates, now],
  );

  if (rows.length === 0) {
    return null;
  }

  const activeCount = active.length;
  const label = activeCount === 1 ? '1 active agent' : `${activeCount || rows.length} active agents`;

  return (
    <aside
      aria-live="polite"
      aria-label="Background agents"
      style={{
        position: 'fixed',
        left: '50%',
        bottom: '16px',
        transform: 'translateX(-50%)',
        zIndex: 1000,
        width: 'min(calc(100vw - 32px), 1720px)',
        pointerEvents: 'none',
        animation: 'agentDrawerSlideUp 220ms ease-out',
      }}
    >
      {expanded ? (
        <div
          style={{
            pointerEvents: 'auto',
            borderRadius: '8px',
            border: `1px solid ${THEME.colors.borderSubtle}`,
            backgroundColor: 'rgba(0, 0, 0, 0.58)',
            boxShadow: `0 24px 70px ${THEME.colors.shadowHeavy}`,
            backdropFilter: 'blur(18px)',
            padding: '10px',
            overflowX: 'auto',
          }}
        >
          <div
            style={{
              display: 'flex',
              gap: '10px',
              alignItems: 'stretch',
            }}
          >
            {rows.map(({ agent, thinkingText }) => (
              <AgentCard
                key={agent.agent_id}
                agent={agent}
                thinkingText={thinkingText}
                onCancel={(agentId) => { void Promise.resolve(onCancel(agentId)).catch(() => {}); }}
              />
            ))}
          </div>
        </div>
      ) : (
        <button
          type="button"
          onClick={onToggleExpanded}
          aria-label="Show background agents"
          style={{
            pointerEvents: 'auto',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            minWidth: '180px',
            height: '36px',
            margin: '0 auto',
            borderRadius: '8px',
            border: `1px solid ${THEME.colors.borderHover}`,
            backgroundColor: 'rgba(0, 0, 0, 0.68)',
            color: THEME.colors.textSecondary,
            boxShadow: `0 18px 48px ${THEME.colors.shadowMedium}`,
            cursor: 'pointer',
            fontSize: '12px',
            fontWeight: 700,
          }}
        >
          {label}
        </button>
      )}
    </aside>
  );
}

AgentDrawer.propTypes = {
  active: PropTypes.arrayOf(PropTypes.object),
  recentCompleted: PropTypes.arrayOf(PropTypes.object),
  streamStates: PropTypes.object,
  expanded: PropTypes.bool,
  onToggleExpanded: PropTypes.func.isRequired,
  onCancel: PropTypes.func.isRequired,
};

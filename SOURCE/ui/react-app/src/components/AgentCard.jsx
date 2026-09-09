import PropTypes from 'prop-types';
import { THEME } from '../config';

function statusStyleFor(status) {
  const styles = {
    running: {
      label: 'Running',
      accent: THEME.colors.accent,
      background: THEME.colors.bgElevated,
    },
    completed: {
      label: 'Completed',
      accent: THEME.colors.statusGreen,
      background: 'rgba(34, 197, 94, 0.08)',
    },
    cancelled: {
      label: 'Cancelled',
      accent: THEME.colors.statusRed,
      background: 'rgba(239, 68, 68, 0.08)',
    },
    killed: {
      label: 'Cancelled',
      accent: THEME.colors.statusRed,
      background: 'rgba(239, 68, 68, 0.08)',
    },
    error: {
      label: 'Error',
      accent: THEME.colors.statusYellow,
      background: 'rgba(234, 179, 8, 0.09)',
    },
    failed: {
      label: 'Error',
      accent: THEME.colors.statusYellow,
      background: 'rgba(234, 179, 8, 0.09)',
    },
  };
  return styles[status] || styles.running;
}

function formatElapsed(seconds) {
  const totalSeconds = Math.max(0, Math.floor(Number(seconds) || 0));
  const minutes = String(Math.floor(totalSeconds / 60)).padStart(2, '0');
  const remainingSeconds = String(totalSeconds % 60).padStart(2, '0');
  return `${minutes}:${remainingSeconds}`;
}

function elapsedSecondsFor(agent) {
  if (Number.isFinite(agent.elapsed_seconds)) {
    return agent.elapsed_seconds;
  }
  const parsed = Date.parse(agent.started_at || '');
  if (!Number.isFinite(parsed)) {
    return 0;
  }
  return (Date.now() - parsed) / 1000;
}

export default function AgentCard({ agent, thinkingText = '', onCancel }) {
  const status = agent.status || 'running';
  const statusStyle = statusStyleFor(status);
  const terminal = ['completed', 'cancelled', 'killed', 'error', 'failed'].includes(status);
  const visibleThinking = thinkingText ? thinkingText.slice(-80) : 'Waiting for reasoning...';
  const task = agent.task || agent.reason || 'Background task';

  return (
    <article
      data-testid="agent-card"
      data-status={status}
      style={{
        width: '320px',
        minWidth: '320px',
        height: '120px',
        borderRadius: '8px',
        border: `1px solid ${statusStyle.accent}55`,
        backgroundColor: statusStyle.background,
        boxShadow: `0 18px 42px ${THEME.colors.shadowMedium}`,
        color: THEME.colors.textPrimary,
        padding: '12px',
        boxSizing: 'border-box',
        display: 'grid',
        gridTemplateRows: 'auto 1fr auto',
        gap: '8px',
      }}
    >
      <div
        title={task}
        style={{
          display: '-webkit-box',
          WebkitLineClamp: 2,
          WebkitBoxOrient: 'vertical',
          overflow: 'hidden',
          color: THEME.colors.textPrimary,
          fontSize: '13px',
          fontWeight: 700,
          lineHeight: 1.25,
        }}
      >
        {task}
      </div>

      <div
        key={visibleThinking}
        style={{
          color: THEME.colors.textMuted,
          fontSize: '12px',
          fontStyle: 'italic',
          lineHeight: 1.35,
          overflow: 'hidden',
          whiteSpace: 'nowrap',
          textOverflow: 'ellipsis',
          animation: 'agentThinkingFadeIn 220ms ease-out',
        }}
      >
        {visibleThinking}
      </div>

      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: '10px',
        }}
      >
        <span
          style={{
            color: statusStyle.accent,
            fontSize: '11px',
            fontWeight: 700,
            textTransform: 'uppercase',
            letterSpacing: 0,
          }}
        >
          {statusStyle.label} {formatElapsed(elapsedSecondsFor(agent))}
        </span>
        <button
          type="button"
          aria-label={`Cancel agent: ${task}`}
          title="Cancel"
          disabled={terminal}
          onClick={() => onCancel(agent.agent_id)}
          style={{
            width: '28px',
            height: '28px',
            borderRadius: '8px',
            border: `1px solid ${THEME.colors.borderHover}`,
            backgroundColor: terminal ? THEME.colors.bgSurface : THEME.colors.bgCard,
            color: terminal ? THEME.colors.textDisabled : THEME.colors.textSecondary,
            cursor: terminal ? 'default' : 'pointer',
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: '13px',
            lineHeight: 1,
          }}
        >
          X
        </button>
      </div>
    </article>
  );
}

AgentCard.propTypes = {
  agent: PropTypes.shape({
    agent_id: PropTypes.string.isRequired,
    task: PropTypes.string,
    reason: PropTypes.string,
    status: PropTypes.string,
    started_at: PropTypes.string,
    elapsed_seconds: PropTypes.number,
  }).isRequired,
  thinkingText: PropTypes.string,
  onCancel: PropTypes.func.isRequired,
};
